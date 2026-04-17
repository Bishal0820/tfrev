"""Discover Terraform source files for additional context in reviews."""

from __future__ import annotations

import re
from pathlib import Path

from tfrev.diff_parser import DiffSummary
from tfrev.plan_parser import PlanSummary

# Max total bytes of context files to include, to avoid blowing up the prompt
_MAX_CONTEXT_BYTES = 100_000
# Max individual file size to include
_MAX_FILE_BYTES = 20_000

# Patterns to match resource/module blocks in .tf files
_RESOURCE_BLOCK_RE = re.compile(r'^\s*resource\s+"([^"]+)"\s+"([^"]+)"', re.MULTILINE)
_MODULE_BLOCK_RE = re.compile(r'^\s*module\s+"([^"]+)"', re.MULTILINE)


def infer_root_dir(diff: DiffSummary) -> Path | None:
    """Infer the Terraform project root from the diff's changed .tf files."""
    tf_paths = [
        Path(f.path) for f in diff.files if f.path.endswith(".tf") or f.path.endswith(".tfvars")
    ]
    if not tf_paths:
        return None

    # Find the common parent of all changed .tf files
    parents = [p.parent for p in tf_paths]
    common = parents[0]
    for parent in parents[1:]:
        # Walk up until we find a common ancestor
        while common != parent and common not in parent.parents:
            common = common.parent
        if common == Path("."):
            break

    root = Path.cwd() / common
    if root.exists():
        return root
    return Path.cwd()


def _index_tf_files(root: Path) -> dict[Path, str]:
    """Read all .tf files under root into a path → content mapping."""
    index: dict[Path, str] = {}
    for tf_file in sorted(root.rglob("*.tf")):
        if ".terraform" in tf_file.parts:
            continue
        if tf_file.stat().st_size > _MAX_FILE_BYTES:
            continue
        try:
            index[tf_file] = tf_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return index


def _find_files_defining_resources(
    resource_addresses: set[str],
    tf_index: dict[Path, str],
) -> set[Path]:
    """Find .tf files that define any of the given resource addresses.

    Addresses are like 'aws_s3_bucket.logs' (type.name).
    """
    hits: set[Path] = set()
    for path, content in tf_index.items():
        for m in _RESOURCE_BLOCK_RE.finditer(content):
            addr = f"{m.group(1)}.{m.group(2)}"
            if addr in resource_addresses:
                hits.add(path)
    return hits


def _find_files_defining_modules(
    module_names: set[str],
    tf_index: dict[Path, str],
) -> set[Path]:
    """Find .tf files that contain module blocks for the given module names."""
    hits: set[Path] = set()
    for path, content in tf_index.items():
        for m in _MODULE_BLOCK_RE.finditer(content):
            if m.group(1) in module_names:
                hits.add(path)
    return hits


def _find_module_source_dirs(
    module_names: set[str],
    tf_index: dict[Path, str],
) -> set[Path]:
    """Find local module source directories referenced by module blocks.

    Scans for module blocks whose name matches, then looks for a 'source'
    attribute with a relative path (starting with ./ or ../).
    """
    _source_re = re.compile(r'\bsource\s*=\s*"(\./[^"]+|\.\.\/[^"]+)"')
    dirs: set[Path] = set()
    for path, content in tf_index.items():
        in_target_module = False
        brace_depth = 0
        found_opening_brace = False
        for line in content.splitlines():
            if not in_target_module:
                mod_match = _MODULE_BLOCK_RE.match(line)
                if mod_match and mod_match.group(1) in module_names:
                    in_target_module = True
                    brace_depth = 0
                    found_opening_brace = False
            if in_target_module:
                brace_depth += line.count("{") - line.count("}")
                if brace_depth > 0:
                    found_opening_brace = True
                source_match = _source_re.search(line)
                if source_match:
                    source_path = (path.parent / source_match.group(1)).resolve()
                    if source_path.is_dir():
                        dirs.add(source_path)
                    in_target_module = False
                elif found_opening_brace and brace_depth <= 0:
                    # Exited the module block without finding a local source
                    in_target_module = False
    return dirs


def discover_context_files(
    diff: DiffSummary,
    plan: PlanSummary,
    root: Path,
    diff_base: Path | None = None,
) -> dict[str, str]:
    """Discover relevant .tf files in root that aren't already in the diff.

    Uses the plan's resource changes to find files that define those resources,
    their modules, and related source directories. Falls back to root-level
    .tf files for basic context.

    `diff_base` is the directory that diff paths are resolved against (typically
    the git repo toplevel). Defaults to the current working directory for
    backward compatibility.

    Returns a mapping of relative path → file contents.
    """
    # Paths already covered by the diff — skip them
    base = diff_base or Path.cwd()
    diff_paths = {(base / f.path).resolve() for f in diff.files}

    # Index all .tf files under root
    tf_index = _index_tf_files(root)

    # --- Resource-aware discovery ---
    # Build set of resource addresses from the plan (strip index like [0])
    resource_addrs: set[str] = set()
    module_names: set[str] = set()
    for rc in plan.resource_changes:
        if rc.action == "no-op":
            continue
        # address: "aws_instance.web[0]" or "module.vpc.aws_subnet.private[1]"
        # Strip module prefix and index suffix to get "type.name"
        addr = rc.address
        # Remove module prefix: "module.vpc.aws_subnet.private" → "aws_subnet.private"
        while addr.startswith("module.") and addr.count(".") >= 2:
            addr = addr.split(".", 2)[-1]
        # Remove index: "aws_instance.web[0]" → "aws_instance.web"
        addr = re.sub(r"\[.*?\]", "", addr)
        resource_addrs.add(addr)

        if rc.module_address:
            # "module.vpc.module.subnets" → extract ["vpc", "subnets"]
            parts = rc.module_address.split(".")
            for i in range(len(parts)):
                if parts[i] == "module" and i + 1 < len(parts):
                    module_names.add(parts[i + 1])

    relevant_files: set[Path] = set()

    # Find files defining resources from the plan
    if resource_addrs:
        relevant_files |= _find_files_defining_resources(resource_addrs, tf_index)

    # Find files defining module calls and their source directories
    if module_names:
        relevant_files |= _find_files_defining_modules(module_names, tf_index)
        for mod_dir in _find_module_source_dirs(module_names, tf_index):
            for tf_file in mod_dir.rglob("*.tf"):
                if tf_file in tf_index:
                    relevant_files.add(tf_file)

    # Always include root-level .tf files (variables, providers, versions, etc.)
    for tf_file in sorted(root.glob("*.tf")):
        if tf_file in tf_index:
            relevant_files.add(tf_file)

    # Read files, skipping those already in diff or too large
    context_files: dict[str, str] = {}
    total_bytes = 0

    for tf_file in sorted(relevant_files):
        if tf_file.resolve() in diff_paths:
            continue

        content = tf_index.get(tf_file)
        if content is None:
            continue

        file_size = len(content.encode("utf-8"))
        if total_bytes + file_size > _MAX_CONTEXT_BYTES:
            break

        try:
            display_path = str(tf_file.relative_to(Path.cwd()))
        except ValueError:
            display_path = str(tf_file)

        context_files[display_path] = content
        total_bytes += file_size

    return context_files


def format_context_for_prompt(context_files: dict[str, str]) -> str:
    """Format discovered context files for inclusion in the Claude prompt."""
    if not context_files:
        return "(No additional source files discovered)"

    parts = []
    for path in sorted(context_files):
        content = context_files[path]
        parts.append(f"### {path}\n```hcl\n{content}\n```")

    return "\n\n".join(parts)
