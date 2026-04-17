"""Tests for tfrev.tf_discovery."""

from __future__ import annotations

from tfrev.diff_parser import DiffSummary, FileDiff
from tfrev.plan_parser import PlanSummary, ResourceChange
from tfrev.tf_discovery import (
    discover_context_files,
    format_context_for_prompt,
    infer_root_dir,
)


class TestInferRootDir:
    def test_single_tf_file(self):
        diff = DiffSummary(files=[FileDiff(path="main.tf", status="modified")])
        root = infer_root_dir(diff)
        assert root is not None
        assert root.exists()

    def test_multiple_tf_files_same_dir(self):
        diff = DiffSummary(
            files=[
                FileDiff(path="infra/main.tf", status="modified"),
                FileDiff(path="infra/variables.tf", status="modified"),
            ]
        )
        root = infer_root_dir(diff)
        assert root is not None

    def test_no_tf_files(self):
        diff = DiffSummary(files=[FileDiff(path="README.md", status="modified")])
        root = infer_root_dir(diff)
        assert root is None

    def test_root_level_tf_files(self):
        diff = DiffSummary(files=[FileDiff(path="main.tf", status="modified")])
        root = infer_root_dir(diff)
        assert root is not None


class TestDiscoverContextFiles:
    def test_discovers_tf_files(self, tmp_path, monkeypatch):
        # Create .tf files in the root
        (tmp_path / "main.tf").write_text('resource "aws_instance" "web" {}')
        (tmp_path / "variables.tf").write_text('variable "region" {}')
        (tmp_path / "outputs.tf").write_text('output "id" { value = "" }')

        # Diff only changes main.tf — use absolute path so resolve() matches
        monkeypatch.chdir(tmp_path)
        diff = DiffSummary(files=[FileDiff(path="main.tf", status="modified")])
        plan = PlanSummary(resource_changes=[], terraform_version="1.7.0", format_version="1.2")

        context = discover_context_files(diff, plan, tmp_path)

        # Should discover variables.tf and outputs.tf but NOT main.tf (already in diff)
        paths = list(context.keys())
        assert any("variables.tf" in p for p in paths)
        assert any("outputs.tf" in p for p in paths)
        assert not any(p.endswith("main.tf") for p in paths)

    def test_skips_large_files(self, tmp_path):
        (tmp_path / "huge.tf").write_text("x" * 25_000)  # > _MAX_FILE_BYTES

        diff = DiffSummary(files=[])
        plan = PlanSummary(resource_changes=[], terraform_version="1.7.0", format_version="1.2")

        context = discover_context_files(diff, plan, tmp_path)
        assert not any("huge.tf" in p for p in context)

    def test_module_directories(self, tmp_path):
        # Create module directory
        mod_dir = tmp_path / "modules" / "vpc"
        mod_dir.mkdir(parents=True)
        (mod_dir / "main.tf").write_text('resource "aws_vpc" "main" {}')

        diff = DiffSummary(files=[])
        plan = PlanSummary(
            resource_changes=[
                ResourceChange(
                    address="module.vpc.aws_vpc.main",
                    resource_type="aws_vpc",
                    provider="aws",
                    action="create",
                    module_address="module.vpc",
                )
            ],
            terraform_version="1.7.0",
            format_version="1.2",
        )

        context = discover_context_files(diff, plan, tmp_path)
        assert any("vpc" in p for p in context)

    def test_empty_directory(self, tmp_path):
        diff = DiffSummary(files=[])
        plan = PlanSummary(resource_changes=[], terraform_version="1.7.0", format_version="1.2")
        context = discover_context_files(diff, plan, tmp_path)
        assert len(context) == 0

    def test_dedup_honors_diff_base(self, tmp_path, monkeypatch):
        """Diff paths are relative to the repo root, not cwd.

        When tfrev is run from a subdirectory of the repo, dedup must still
        exclude files already covered by the diff.
        """
        (tmp_path / "main.tf").write_text('resource "aws_instance" "web" {}')
        (tmp_path / "variables.tf").write_text('variable "region" {}')
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        monkeypatch.chdir(subdir)

        diff = DiffSummary(files=[FileDiff(path="main.tf", status="modified")])
        plan = PlanSummary(resource_changes=[], terraform_version="1.7.0", format_version="1.2")

        context = discover_context_files(diff, plan, tmp_path, diff_base=tmp_path)

        # main.tf is already in the diff — must not be re-included as context.
        assert not any(p.endswith("main.tf") for p in context)
        assert any("variables.tf" in p for p in context)


class TestFormatContextForPrompt:
    def test_empty(self):
        output = format_context_for_prompt({})
        assert "No additional source files" in output

    def test_with_files(self):
        files = {
            "variables.tf": 'variable "region" {\n  default = "us-east-1"\n}',
            "outputs.tf": 'output "id" {\n  value = "test"\n}',
        }
        output = format_context_for_prompt(files)
        assert "variables.tf" in output
        assert "outputs.tf" in output
        assert "us-east-1" in output
        assert "```hcl" in output
