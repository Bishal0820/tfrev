"""CLI entry point for tfrev."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import click

from tfrev import __version__
from tfrev.client import ReviewClient
from tfrev.config import load_config, severity_meets_threshold
from tfrev.diff_parser import DiffHunk, DiffSummary, FileDiff, filter_diff, parse_diff
from tfrev.output import format_json, format_markdown, format_table
from tfrev.plan_parser import PlanSummary, load_plan_file, parse_plan_json
from tfrev.prompt import build_system_prompt, build_user_prompt, estimate_tokens
from tfrev.response_parser import parse_response
from tfrev.tf_discovery import _MAX_FILE_BYTES, discover_context_files, infer_root_dir

_DEFAULT_CONTEXT_LIMIT = 200_000


class _Spinner:
    """Simple terminal spinner for long-running operations."""

    def __init__(self, message: str = "Working"):
        self._message = message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _Spinner:
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
        click.echo(" done", err=True)

    def _spin(self) -> None:
        click.echo(f"  {self._message}", nl=False, err=True)
        while not self._stop.is_set():
            click.echo(".", nl=False, err=True)
            self._stop.wait(1.0)


@click.group()
@click.version_option(version=__version__, prog_name="tfrev")
def main():
    """tfrev — AI-powered Terraform plan reviewer.

    Verify that your Terraform plan matches your code intent before apply.
    """
    pass


@main.command()
@click.option(
    "--plan",
    "plan_path",
    type=click.Path(exists=True),
    help="Path to plan JSON file (terraform show -json)",
)
@click.option(
    "--auto", "auto_mode", is_flag=True, help="Auto-detect plan file from current directory"
)
@click.option(
    "--base-ref",
    "base_ref",
    default=None,
    help="Git ref to diff against (e.g. a SHA, tag, or branch). Defaults to main/CI branch.",
)
@click.option(
    "--config", "config_path", type=click.Path(), default=None, help="Path to .tfrev.yaml config"
)
@click.option(
    "--output",
    "output_format",
    type=click.Choice(["table", "json", "markdown"]),
    default="table",
    help="Output format",
)
@click.option("--model", default=None, help="Override Claude model (e.g., claude-sonnet-4-6)")
@click.option(
    "--fail-on",
    default=None,
    type=click.Choice(["info", "low", "medium", "high", "critical"]),
    help="Exit 1 if any finding >= this severity",
)
@click.option(
    "--severity-threshold",
    default=None,
    type=click.Choice(["info", "low", "medium", "high", "critical"]),
    help="Minimum severity to show",
)
@click.option("--max-tokens", default=None, type=int, help="Max response tokens")
@click.option(
    "--context-dir",
    "context_dir",
    type=click.Path(exists=True, file_okay=False),
    default=None,
    help="Terraform project root for source file context",
)
@click.option(
    "--no-context", "no_context", is_flag=True, help="Disable auto-discovery of source files"
)
@click.option("--quiet", is_flag=True, help="Suppress progress messages")
def review(
    plan_path,
    auto_mode,
    base_ref,
    config_path,
    output_format,
    model,
    fail_on,
    severity_threshold,
    max_tokens,
    context_dir,
    no_context,
    quiet,
):
    """Review a Terraform plan against code changes."""

    # --- Load config ---
    config = load_config(config_path)

    # Apply CLI overrides
    if model:
        config.model = model
    if fail_on:
        config.fail_on = fail_on
    if severity_threshold:
        config.severity_threshold = severity_threshold
    if max_tokens:
        config.max_tokens = max_tokens

    # --- Load plan ---
    if auto_mode:
        plan = _auto_detect_plan(quiet)
    elif plan_path:
        if not quiet:
            click.echo(f"Loading plan: {plan_path}", err=True)
        plan = load_plan_file(plan_path)
    else:
        click.echo("Error: Provide --plan or --auto", err=True)
        sys.exit(2)

    # --- Skip Claude call when the plan has no infrastructure changes ---
    if not plan.has_changes:
        if not quiet:
            click.echo(
                "Plan shows no infrastructure changes (0 create / 0 update / "
                "0 delete / 0 replace). Nothing to review.",
                err=True,
            )
        sys.exit(0)

    # --- Generate diff ---
    # Only prompt about the base ref when we're actually in a git repo.
    # In non-git directories, _generate_diff falls back to scanning all .tf files.
    if not base_ref and not quiet and _is_inside_git_work_tree():
        default_branch = _detect_default_branch()
        click.echo(
            "No --base-ref provided. --base-ref is the previous commit, branch, or tag "
            "to compare your current Terraform code against (e.g. the last known-good state).",
            err=True,
        )
        click.echo(
            f"Will diff against '{default_branch}' — if that yields no Terraform changes, "
            "the entire current state of Terraform files will be reviewed instead.",
            err=True,
        )
        answer = click.prompt(
            "Continue?",
            type=str,
            default="no",
            err=True,
        )
        if answer.lower() not in ("yes", "y"):
            click.echo(
                "Aborting. Re-run with --base-ref <branch/sha/tag> to diff against a specific ref.",
                err=True,
            )
            sys.exit(2)

    diff = _generate_diff(base_ref, quiet)

    # --- Apply ignore patterns ---
    if config.ignore:
        diff = filter_diff(diff, config.ignore)

    # --- Summary ---
    if not quiet:
        click.echo(
            f"Plan: {plan.creating} create, {plan.updating} update, "
            f"{plan.deleting} delete, {plan.replacing} replace",
            err=True,
        )
        click.echo(
            f"Diff: {diff.total_files} files changed "
            f"(+{diff.total_additions}/-{diff.total_deletions})",
            err=True,
        )
        click.echo(f"Model: {config.model}", err=True)

    # --- Discover context files ---
    context_files: dict[str, str] | None = None
    if not no_context:
        if context_dir:
            root = Path(context_dir)
        else:
            root = infer_root_dir(diff)

        if root:
            if not quiet:
                click.echo(f"Scanning for context files in: {root}", err=True)
            diff_base = _git_toplevel() or Path.cwd()
            context_files = discover_context_files(diff, plan, root, diff_base=diff_base)
            if not quiet:
                click.echo(f"Discovered {len(context_files)} additional source file(s):", err=True)
                for ctx_path in sorted(context_files):
                    click.echo(f"  + {ctx_path}", err=True)

    # --- Build prompts ---
    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(plan, diff, config, context_files=context_files)

    total_tokens = estimate_tokens(system_prompt + user_prompt)
    if not quiet:
        click.echo(f"Estimated input tokens: ~{total_tokens:,}", err=True)

    # --- Context window check ---
    available = _DEFAULT_CONTEXT_LIMIT - config.max_tokens - 1000  # reserve for response + overhead

    if total_tokens > available:
        if not quiet:
            click.echo(
                f"Warning: Estimated input (~{total_tokens:,} tokens) exceeds available "
                f"context ({available:,} tokens). Dropping context files.",
                err=True,
            )
        # Rebuild without context files
        user_prompt = build_user_prompt(plan, diff, config, context_files=None)
        total_tokens = estimate_tokens(system_prompt + user_prompt)

        if total_tokens > available:
            click.echo(
                f"Warning: Estimated input (~{total_tokens:,} tokens) still exceeds "
                f"available context ({available:,} tokens) even without context files.",
                err=True,
            )
            answer = click.prompt(
                "Do you want to continue anyway?",
                type=str,
                default="no",
                err=True,
            )
            if answer.lower() not in ("yes", "y"):
                click.echo("Aborting.", err=True)
                sys.exit(2)

    if not quiet:
        if not click.confirm(
            "Send plan + diff to Claude for review?",
            default=True,
            err=True,
        ):
            click.echo("Aborting.", err=True)
            sys.exit(2)
        click.echo("Sending to Claude for review...", err=True)

    # --- Call Claude ---
    try:
        client = ReviewClient(config)
        if not quiet:
            with _Spinner("Waiting for Claude"):
                api_response = client.review(system_prompt, user_prompt)
        else:
            api_response = client.review(system_prompt, user_prompt)
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(2)

    if not quiet:
        actual_in = api_response.input_tokens
        click.echo(
            f"Review complete. Tokens: {actual_in:,} in / {api_response.output_tokens:,} out "
            f"(estimated {total_tokens:,}, {total_tokens / max(actual_in, 1):.0%} accuracy)",
            err=True,
        )

    # --- Parse response ---
    result = parse_response(api_response.content)
    if result.parse_failed:
        click.echo(
            "Error: Claude's response could not be parsed as structured JSON. Raw response:",
            err=True,
        )
        click.echo(result.raw_response, err=True)
        sys.exit(2)

    # --- Format output ---
    if output_format == "json":
        output = format_json(result, config)
    elif output_format == "markdown":
        output = format_markdown(result, config)
    else:
        output = format_table(result, config)

    click.echo(output)

    # --- Exit code ---
    has_failing = any(severity_meets_threshold(f.severity, config.fail_on) for f in result.findings)
    if has_failing:
        sys.exit(1)
    sys.exit(0)


def _auto_detect_plan(quiet: bool) -> PlanSummary:
    """Find a terraform plan file in the current directory and convert it to JSON."""
    plan_candidates = list(Path(".").glob("*.tfplan")) + [Path("tfplan")]
    plan_file = None
    for candidate in plan_candidates:
        if candidate.exists():
            plan_file = candidate
            break

    if plan_file is None:
        click.echo(
            "Error: --auto could not find a plan file. Run `terraform plan -out=tfplan` first.",
            err=True,
        )
        sys.exit(2)

    if not quiet:
        click.echo(f"Auto-detected plan: {plan_file}", err=True)

    try:
        result = subprocess.run(
            ["terraform", "show", "-json", str(plan_file)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            click.echo(f"Error: terraform show -json failed: {result.stderr}", err=True)
            sys.exit(2)
        return parse_plan_json(json.loads(result.stdout))
    except FileNotFoundError:
        click.echo("Error: terraform CLI not found. Is it installed and in PATH?", err=True)
        sys.exit(2)
    except subprocess.TimeoutExpired:
        click.echo("Error: terraform show -json timed out after 60 seconds", err=True)
        sys.exit(2)


# Git empty-tree SHA — diffing against this shows every file as a new addition.
# This is a git constant: `git hash-object -t tree /dev/null`
_EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _git_toplevel() -> Path | None:
    """Return the git repo toplevel path, or None if not inside a git repo."""
    try:
        ret = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if ret.returncode == 0:
            top = ret.stdout.strip()
            if top:
                return Path(top)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _is_inside_git_work_tree() -> bool:
    """Return True if the current directory is inside a git working tree."""
    try:
        ret = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return ret.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _detect_default_branch() -> str:
    """Detect the default branch (main or master), falling back to 'main'."""
    try:
        for candidate in ("main", "master"):
            ret = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", candidate],
                capture_output=True,
                timeout=10,
            )
            if ret.returncode == 0:
                return candidate
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "main"


def _scan_tf_files(directory: Path, quiet: bool) -> DiffSummary:
    """Build a DiffSummary by reading all .tf/.tfvars files in a directory tree."""

    tf_files = sorted(
        p
        for pattern in ("**/*.tf", "**/*.tfvars")
        for p in directory.glob(pattern)
        if ".terraform" not in p.parts
    )

    if not tf_files:
        if not quiet:
            click.echo("No .tf or .tfvars files found in current directory.", err=True)
        return DiffSummary(files=[])

    files: list[FileDiff] = []
    for tf_path in tf_files:
        try:
            if tf_path.stat().st_size > _MAX_FILE_BYTES:
                continue
            rel = str(tf_path.relative_to(directory))
            content = tf_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = content.splitlines()
        hunk_lines = [f"+{line}" for line in lines]
        hunk = DiffHunk(
            old_start=0,
            old_count=0,
            new_start=1,
            new_count=len(lines),
            lines=hunk_lines,
        )
        files.append(FileDiff(path=rel, status="added", hunks=[hunk]))

    if not quiet:
        click.echo(f"Found {len(files)} Terraform file(s).", err=True)

    return DiffSummary(files=files)


def _generate_diff(base_ref: str | None, quiet: bool) -> DiffSummary:
    """Generate a git diff against base_ref (or CI/main fallback).

    If no Terraform files changed vs the base ref (e.g. first commit), falls
    back to diffing against the empty tree so all current files are visible.
    """
    # Check we're inside a git repository
    has_git = False
    try:
        git_check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        has_git = git_check.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    if not has_git:
        # No git — scan current directory for .tf/.tfvars files
        if not quiet:
            click.echo(
                "Not a git repository. Scanning current directory for Terraform files.",
                err=True,
            )
        return _scan_tf_files(Path.cwd(), quiet)

    base = (
        base_ref
        or os.environ.get("GITHUB_BASE_REF")
        or os.environ.get("CI_MERGE_REQUEST_TARGET_BRANCH_NAME")
        or os.environ.get("CHANGE_TARGET")
        or _detect_default_branch()
    )

    if not quiet:
        label = "base ref" if base_ref else "auto-detected base"
        click.echo(f"Generating diff against {label}: {base}", err=True)

    used_empty_tree = False
    try:
        result = subprocess.run(
            ["git", "diff", f"{base}...HEAD", "--", "*.tf", "*.tfvars"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            # Fall back to origin/<base> if the bare ref fails
            result = subprocess.run(
                ["git", "diff", f"origin/{base}...HEAD", "--", "*.tf", "*.tfvars"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                # Both refs failed — fall back to empty-tree diff
                if not quiet:
                    click.echo(
                        f"Could not diff against '{base}' or 'origin/{base}'. "
                        "Reviewing full current state of files.",
                        err=True,
                    )
                result = subprocess.run(
                    ["git", "diff", _EMPTY_TREE_SHA, "HEAD", "--", "*.tf", "*.tfvars"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                used_empty_tree = True
                if result.returncode != 0:
                    click.echo(
                        f"Error: git diff failed: {result.stderr.strip()}",
                        err=True,
                    )
                    sys.exit(2)
    except FileNotFoundError:
        click.echo("Error: git not found. Is it installed and in PATH?", err=True)
        sys.exit(2)
    except subprocess.TimeoutExpired:
        click.echo(
            "Error: git diff timed out. Try a smaller base ref range or "
            "check for a hung git process.",
            err=True,
        )
        sys.exit(2)

    diff = parse_diff(result.stdout)

    if diff.total_files == 0 and not used_empty_tree:
        # No changes vs base ref — fall back to full current state (e.g. first commit)
        if not quiet:
            click.echo(
                "No Terraform changes vs base ref. Reviewing full current state of files.",
                err=True,
            )
        try:
            result = subprocess.run(
                ["git", "diff", _EMPTY_TREE_SHA, "HEAD", "--", "*.tf", "*.tfvars"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                diff = parse_diff(result.stdout)
        except FileNotFoundError:
            pass  # git already confirmed present above
        except subprocess.TimeoutExpired:
            click.echo(
                "Error: git diff timed out. Try a smaller base ref range or "
                "check for a hung git process.",
                err=True,
            )
            sys.exit(2)

    return diff


if __name__ == "__main__":
    main()
