"""Tests for tfrev.plan_parser."""

from __future__ import annotations

import pytest

from tfrev.plan_parser import (
    _extract_attribute_changes,
    _resolve_action,
    format_plan_for_prompt,
    load_plan_file,
    parse_plan_json,
)

# --- _resolve_action ---


class TestResolveAction:
    def test_create(self):
        assert _resolve_action(["create"]) == "create"

    def test_update(self):
        assert _resolve_action(["update"]) == "update"

    def test_delete(self):
        assert _resolve_action(["delete"]) == "delete"

    def test_replace_delete_create(self):
        assert _resolve_action(["delete", "create"]) == "replace"

    def test_replace_create_delete(self):
        assert _resolve_action(["create", "delete"]) == "replace"

    def test_noop(self):
        assert _resolve_action(["no-op"]) == "no-op"

    def test_read(self):
        assert _resolve_action(["read"]) == "no-op"

    def test_unknown(self):
        assert _resolve_action(["something-weird"]) == "unknown"


# --- _extract_attribute_changes ---


class TestExtractAttributeChanges:
    def test_basic_change(self):
        changes = _extract_attribute_changes(
            before={"ami": "ami-old"},
            after={"ami": "ami-new"},
            after_unknown=None,
        )
        assert len(changes) == 1
        assert changes[0].name == "ami"
        assert changes[0].before == "ami-old"
        assert changes[0].after == "ami-new"

    def test_no_change(self):
        changes = _extract_attribute_changes(
            before={"ami": "ami-same"},
            after={"ami": "ami-same"},
            after_unknown=None,
        )
        assert len(changes) == 0

    def test_sensitive_values(self):
        changes = _extract_attribute_changes(
            before={"password": "old-secret"},
            after={"password": "new-secret"},
            after_unknown=None,
            before_sensitive={"password": True},
            after_sensitive={"password": True},
        )
        assert len(changes) == 1
        assert changes[0].before == "(sensitive)"
        assert changes[0].after == "(sensitive)"
        assert changes[0].is_sensitive is True

    def test_computed_values(self):
        changes = _extract_attribute_changes(
            before=None,
            after={"id": "placeholder"},
            after_unknown={"id": True},
        )
        assert len(changes) == 1
        assert changes[0].after == "(known after apply)"
        assert changes[0].is_computed is True

    def test_new_attribute(self):
        changes = _extract_attribute_changes(
            before={},
            after={"new_attr": "value"},
            after_unknown=None,
        )
        assert len(changes) == 1
        assert changes[0].name == "new_attr"
        assert changes[0].before is None

    def test_removed_attribute(self):
        changes = _extract_attribute_changes(
            before={"old_attr": "value"},
            after={},
            after_unknown=None,
        )
        assert len(changes) == 1
        assert changes[0].name == "old_attr"
        assert changes[0].after is None

    def test_both_none(self):
        changes = _extract_attribute_changes(before=None, after=None, after_unknown=None)
        assert len(changes) == 0


# --- parse_plan_json ---


class TestParsePlanJson:
    def test_counts_minimal(self, minimal_plan_json):
        plan = parse_plan_json(minimal_plan_json)
        assert plan.creating == 1
        assert plan.updating == 0
        assert plan.deleting == 0
        assert plan.replacing == 0
        assert plan.total_resources == 1

    def test_counts_complex(self, complex_plan_json):
        plan = parse_plan_json(complex_plan_json)
        assert plan.creating == 2  # aws_instance.web + module.vpc.aws_subnet.private
        assert plan.updating == 1  # aws_security_group.web_sg
        assert plan.deleting == 1  # aws_db_instance.old_db
        assert plan.replacing == 1  # aws_s3_bucket.assets
        assert plan.no_op == 1  # aws_route53_record.www
        assert plan.total_resources == 6

    def test_has_changes(self, minimal_plan_json):
        plan = parse_plan_json(minimal_plan_json)
        assert plan.has_changes is True

    def test_no_changes(self, empty_plan_json):
        plan = parse_plan_json(empty_plan_json)
        assert plan.has_changes is False

    def test_module_address(self, complex_plan_json):
        plan = parse_plan_json(complex_plan_json)
        module_resources = [rc for rc in plan.resource_changes if rc.module_address]
        assert len(module_resources) == 1
        assert module_resources[0].module_address == "module.vpc"

    def test_resource_fields(self, minimal_plan_json):
        plan = parse_plan_json(minimal_plan_json)
        rc = plan.resource_changes[0]
        assert rc.address == "aws_instance.web"
        assert rc.resource_type == "aws_instance"
        assert rc.provider == "registry.terraform.io/hashicorp/aws"
        assert rc.action == "create"

    def test_sensitive_plan(self, sensitive_plan_json):
        plan = parse_plan_json(sensitive_plan_json)
        rc = plan.resource_changes[0]
        password_change = [a for a in rc.attribute_changes if a.name == "password"]
        assert len(password_change) == 1
        assert password_change[0].after == "(sensitive)"

    def test_terraform_version(self, minimal_plan_json):
        plan = parse_plan_json(minimal_plan_json)
        assert plan.terraform_version == "1.7.0"
        assert plan.format_version == "1.2"

    def test_empty_resource_changes(self):
        plan = parse_plan_json({"resource_changes": [], "terraform_version": "1.7.0"})
        assert plan.total_resources == 0
        assert plan.has_changes is False


# --- load_plan_file ---


class TestLoadPlanFile:
    def test_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_plan_file("/nonexistent/plan.json")

    def test_invalid_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json {{{")
        with pytest.raises(ValueError, match="Invalid JSON"):
            load_plan_file(bad)

    def test_not_terraform(self, tmp_path):
        not_tf = tmp_path / "not_tf.json"
        not_tf.write_text('{"some": "random json"}')
        with pytest.raises(ValueError, match="does not appear to be"):
            load_plan_file(not_tf)

    def test_valid_file(self, fixtures_dir):
        plan = load_plan_file(fixtures_dir / "plan_minimal.json")
        assert plan.creating == 1

    def test_open_uses_utf8_encoding(self, fixtures_dir):
        """Plan loads must pass encoding='utf-8' explicitly for cross-platform safety."""
        from unittest.mock import patch

        real_open = open
        seen_kwargs: list[dict] = []

        def tracking_open(*args, **kwargs):
            seen_kwargs.append(kwargs)
            return real_open(*args, **kwargs)

        with patch("builtins.open", side_effect=tracking_open):
            load_plan_file(fixtures_dir / "plan_minimal.json")

        assert any(kw.get("encoding") == "utf-8" for kw in seen_kwargs)


# --- format_plan_for_prompt ---


class TestFormatPlanForPrompt:
    def test_basic_output(self, minimal_plan_json):
        plan = parse_plan_json(minimal_plan_json)
        output = format_plan_for_prompt(plan)
        assert "aws_instance.web" in output
        assert "[+]" in output
        assert "create" in output

    def test_no_changes_message(self, empty_plan_json):
        plan = parse_plan_json(empty_plan_json)
        output = format_plan_for_prompt(plan)
        assert "No changes" in output

    def test_complex_output(self, complex_plan_json):
        plan = parse_plan_json(complex_plan_json)
        output = format_plan_for_prompt(plan)
        assert "[+]" in output  # create
        assert "[~]" in output  # update
        assert "[-]" in output  # delete
        assert "[-/+]" in output  # replace
        assert "module.vpc" in output
