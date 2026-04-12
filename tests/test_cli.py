"""Tests for tfrev.cli using Click's CliRunner."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from tfrev.cli import main
from tfrev.client import APIResponse

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def pass_api_response():
    return APIResponse(
        content=(FIXTURES_DIR / "response_pass.json").read_text(),
        model="claude-sonnet-4-6",
        input_tokens=1500,
        output_tokens=800,
        stop_reason="end_turn",
    )


@pytest.fixture
def fail_api_response():
    return APIResponse(
        content=(FIXTURES_DIR / "response_fail.json").read_text(),
        model="claude-sonnet-4-6",
        input_tokens=2000,
        output_tokens=1200,
        stop_reason="end_turn",
    )


@pytest.fixture
def medium_only_api_response():
    return APIResponse(
        content=(FIXTURES_DIR / "response_medium_only.json").read_text(),
        model="claude-sonnet-4-6",
        input_tokens=1500,
        output_tokens=600,
        stop_reason="end_turn",
    )


@pytest.fixture
def mock_git_diff():
    """Patch subprocess.run to return a simple diff for git diff calls."""
    diff_text = (FIXTURES_DIR / "diff_simple.diff").read_text()
    git_ok = MagicMock(returncode=0, stdout=diff_text, stderr="")
    with patch("tfrev.cli.subprocess.run", return_value=git_ok) as mock:
        yield mock


class TestVersion:
    def test_version_flag(self, runner):
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "tfrev" in result.output


class TestReviewCommand:
    @patch("tfrev.cli.ReviewClient")
    def test_basic_review_pass(self, mock_client_cls, runner, pass_api_response, mock_git_diff):
        mock_client_cls.return_value.review.return_value = pass_api_response
        plan_file = str(FIXTURES_DIR / "plan_minimal.json")

        result = runner.invoke(main, ["review", "--plan", plan_file, "--quiet"])
        assert result.exit_code == 0
        mock_client_cls.return_value.review.assert_called_once()

    @patch("tfrev.cli.ReviewClient")
    def test_review_fail_exit_code(self, mock_client_cls, runner, fail_api_response, mock_git_diff):
        mock_client_cls.return_value.review.return_value = fail_api_response
        plan_file = str(FIXTURES_DIR / "plan_minimal.json")

        result = runner.invoke(main, ["review", "--plan", plan_file, "--quiet"])
        assert result.exit_code == 1

    @patch("tfrev.cli.ReviewClient")
    def test_json_output(self, mock_client_cls, runner, pass_api_response, mock_git_diff):
        mock_client_cls.return_value.review.return_value = pass_api_response
        plan_file = str(FIXTURES_DIR / "plan_minimal.json")

        result = runner.invoke(main, ["review", "--plan", plan_file, "--output", "json", "--quiet"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["review"]["verdict"] == "PASS"

    @patch("tfrev.cli.ReviewClient")
    def test_markdown_output(self, mock_client_cls, runner, pass_api_response, mock_git_diff):
        mock_client_cls.return_value.review.return_value = pass_api_response
        plan_file = str(FIXTURES_DIR / "plan_minimal.json")

        result = runner.invoke(
            main, ["review", "--plan", plan_file, "--output", "markdown", "--quiet"]
        )
        assert result.exit_code == 0
        assert "PASS" in result.output
        assert "##" in result.output

    def test_missing_plan(self, runner):
        result = runner.invoke(main, ["review"])
        assert result.exit_code == 2

    @patch("tfrev.cli.ReviewClient")
    def test_fail_on_critical_with_critical_finding(
        self, mock_client_cls, runner, fail_api_response, mock_git_diff
    ):
        mock_client_cls.return_value.review.return_value = fail_api_response
        plan_file = str(FIXTURES_DIR / "plan_minimal.json")

        result = runner.invoke(
            main, ["review", "--plan", plan_file, "--fail-on", "critical", "--quiet"]
        )
        assert result.exit_code == 1

    @patch("tfrev.cli.ReviewClient")
    def test_fail_on_critical_with_medium_only_finding(
        self, mock_client_cls, runner, medium_only_api_response, mock_git_diff
    ):
        """--fail-on critical exits 0 when only medium findings are present."""
        mock_client_cls.return_value.review.return_value = medium_only_api_response
        plan_file = str(FIXTURES_DIR / "plan_minimal.json")

        result = runner.invoke(
            main, ["review", "--plan", plan_file, "--fail-on", "critical", "--quiet"]
        )
        assert result.exit_code == 0

    @patch("tfrev.cli.ReviewClient")
    def test_config_override(self, mock_client_cls, runner, pass_api_response, mock_git_diff):
        mock_client_cls.return_value.review.return_value = pass_api_response
        plan_file = str(FIXTURES_DIR / "plan_minimal.json")
        config_file = str(FIXTURES_DIR / "config_full.yaml")

        result = runner.invoke(
            main, ["review", "--plan", plan_file, "--config", config_file, "--quiet"]
        )
        assert result.exit_code == 0

    @patch("tfrev.cli.ReviewClient")
    def test_no_context_flag(self, mock_client_cls, runner, pass_api_response, mock_git_diff):
        mock_client_cls.return_value.review.return_value = pass_api_response
        plan_file = str(FIXTURES_DIR / "plan_minimal.json")

        result = runner.invoke(main, ["review", "--plan", plan_file, "--no-context", "--quiet"])
        assert result.exit_code == 0

    @patch("tfrev.cli.ReviewClient")
    def test_base_ref_passed_to_git(
        self, mock_client_cls, runner, pass_api_response, mock_git_diff
    ):
        mock_client_cls.return_value.review.return_value = pass_api_response
        plan_file = str(FIXTURES_DIR / "plan_minimal.json")

        result = runner.invoke(
            main, ["review", "--plan", plan_file, "--base-ref", "abc1234", "--quiet"]
        )
        assert result.exit_code == 0
        cmd = mock_git_diff.call_args[0][0]
        assert "abc1234...HEAD" in cmd

    def test_runtime_error_from_client(self, runner, mock_git_diff):
        with patch("tfrev.cli.ReviewClient") as mock_client_cls:
            mock_client_cls.side_effect = RuntimeError("ANTHROPIC_API_KEY is not set")
            plan_file = str(FIXTURES_DIR / "plan_minimal.json")
            result = runner.invoke(main, ["review", "--plan", plan_file, "--quiet"])
        assert result.exit_code == 2

    def test_git_not_found(self, runner):
        with patch("tfrev.cli.subprocess.run", side_effect=FileNotFoundError):
            plan_file = str(FIXTURES_DIR / "plan_minimal.json")
            result = runner.invoke(main, ["review", "--plan", plan_file, "--quiet"])
        assert result.exit_code == 2

    def test_git_diff_both_refs_fail(self, runner):
        fail = MagicMock(returncode=1, stdout="", stderr="unknown revision")
        with patch("tfrev.cli.subprocess.run", return_value=fail):
            plan_file = str(FIXTURES_DIR / "plan_minimal.json")
            result = runner.invoke(main, ["review", "--plan", plan_file, "--quiet"])
        assert result.exit_code == 2

    @patch("tfrev.cli.ReviewClient")
    def test_empty_diff_falls_back_to_full_state(self, mock_client_cls, runner, pass_api_response):
        """When base ref diff is empty, falls back to diffing against the empty tree."""
        mock_client_cls.return_value.review.return_value = pass_api_response
        diff_text = (FIXTURES_DIR / "diff_simple.diff").read_text()
        git_check = MagicMock(returncode=0, stdout="true", stderr="")
        detect_branch = MagicMock(returncode=0)  # _detect_default_branch: rev-parse main
        empty = MagicMock(returncode=0, stdout="", stderr="")
        full_state = MagicMock(returncode=0, stdout=diff_text, stderr="")

        with patch("tfrev.cli.subprocess.run", side_effect=[git_check, detect_branch, empty, full_state]):
            plan_file = str(FIXTURES_DIR / "plan_minimal.json")
            result = runner.invoke(main, ["review", "--plan", plan_file, "--quiet"])
        assert result.exit_code == 0
        mock_client_cls.return_value.review.assert_called_once()


class TestAutoMode:
    @patch("tfrev.cli.subprocess.run")
    @patch("tfrev.cli.Path")
    def test_no_plan_file_found(self, mock_path_cls, mock_subproc, runner):
        mock_path_cls.return_value.glob.return_value = []
        result = runner.invoke(main, ["review", "--auto", "--quiet"])
        assert result.exit_code == 2

    @patch("tfrev.cli.subprocess.run")
    @patch("tfrev.cli.Path")
    def test_terraform_not_in_path(self, mock_path_cls, mock_subproc, runner):
        mock_plan = MagicMock()
        mock_plan.exists.return_value = True
        mock_plan.__str__ = lambda self: "tfplan"
        mock_path_cls.return_value.glob.return_value = [mock_plan]
        mock_subproc.side_effect = FileNotFoundError

        result = runner.invoke(main, ["review", "--auto", "--quiet"])
        assert result.exit_code == 2

    @patch("tfrev.cli.subprocess.run")
    @patch("tfrev.cli.Path")
    def test_terraform_show_fails(self, mock_path_cls, mock_subproc, runner):
        mock_plan = MagicMock()
        mock_plan.exists.return_value = True
        mock_plan.__str__ = lambda self: "tfplan"
        mock_path_cls.return_value.glob.return_value = [mock_plan]
        mock_subproc.return_value = MagicMock(returncode=1, stderr="Error reading plan")

        result = runner.invoke(main, ["review", "--auto", "--quiet"])
        assert result.exit_code == 2

    @patch("tfrev.cli.ReviewClient")
    @patch("tfrev.cli.subprocess.run")
    @patch("tfrev.cli.Path")
    def test_auto_success(
        self, mock_path_cls, mock_subproc, mock_client_cls, runner, pass_api_response
    ):
        mock_plan = MagicMock()
        mock_plan.exists.return_value = True
        mock_plan.__str__ = lambda self: "tfplan"
        mock_path_cls.return_value.glob.return_value = [mock_plan]

        plan_json = json.loads((FIXTURES_DIR / "plan_minimal.json").read_text())
        diff_text = (FIXTURES_DIR / "diff_simple.diff").read_text()
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps(plan_json), stderr=""),
            MagicMock(returncode=0, stdout="true", stderr=""),  # git check
            MagicMock(returncode=0),  # _detect_default_branch: rev-parse main
            MagicMock(returncode=0, stdout=diff_text, stderr=""),  # git diff
        ]
        mock_client_cls.return_value.review.return_value = pass_api_response

        result = runner.invoke(main, ["review", "--auto", "--quiet"])
        assert result.exit_code == 0
        mock_client_cls.return_value.review.assert_called_once()

    @patch("tfrev.cli.ReviewClient")
    @patch("tfrev.cli.subprocess.run")
    @patch("tfrev.cli.Path")
    def test_git_diff_falls_back_to_origin(
        self, mock_path_cls, mock_subproc, mock_client_cls, runner, pass_api_response
    ):
        mock_plan = MagicMock()
        mock_plan.exists.return_value = True
        mock_plan.__str__ = lambda self: "tfplan"
        mock_path_cls.return_value.glob.return_value = [mock_plan]

        plan_json = json.loads((FIXTURES_DIR / "plan_minimal.json").read_text())
        diff_text = (FIXTURES_DIR / "diff_simple.diff").read_text()
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps(plan_json), stderr=""),
            MagicMock(returncode=0, stdout="true", stderr=""),  # git check
            MagicMock(returncode=0),  # _detect_default_branch: rev-parse main
            MagicMock(returncode=1, stdout="", stderr="unknown revision"),  # git diff main
            MagicMock(returncode=0, stdout=diff_text, stderr=""),  # git diff origin/main
        ]
        mock_client_cls.return_value.review.return_value = pass_api_response

        result = runner.invoke(main, ["review", "--auto", "--quiet"])
        assert result.exit_code == 0

    @patch("tfrev.cli.ReviewClient")
    @patch("tfrev.cli.subprocess.run")
    @patch("tfrev.cli.Path")
    def test_both_git_diffs_fail_falls_back(
        self, mock_path_cls, mock_subproc, mock_client_cls, runner, pass_api_response
    ):
        """When both base and origin/base diffs fail, falls back to empty-tree diff."""
        mock_plan = MagicMock()
        mock_plan.exists.return_value = True
        mock_plan.__str__ = lambda self: "tfplan"
        mock_path_cls.return_value.glob.return_value = [mock_plan]

        plan_json = json.loads((FIXTURES_DIR / "plan_minimal.json").read_text())
        diff_text = (FIXTURES_DIR / "diff_simple.diff").read_text()
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps(plan_json), stderr=""),
            MagicMock(returncode=0, stdout="true", stderr=""),  # git check
            MagicMock(returncode=0),  # _detect_default_branch: rev-parse main
            MagicMock(returncode=1, stdout="", stderr="unknown revision"),  # git diff main
            MagicMock(returncode=1, stdout="", stderr="unknown revision"),  # git diff origin/main
            MagicMock(returncode=0, stdout=diff_text, stderr=""),  # empty-tree fallback
        ]
        mock_client_cls.return_value.review.return_value = pass_api_response

        result = runner.invoke(main, ["review", "--auto", "--quiet"])
        assert result.exit_code == 0
