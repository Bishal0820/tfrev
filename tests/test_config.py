"""Tests for tfrev.config."""

from __future__ import annotations

import pytest

from tfrev.config import (
    PolicyRule,
    TfrevConfig,
    _validate_severity,
    format_policies_for_prompt,
    load_config,
    severity_meets_threshold,
)


class TestSeverityMeetsThreshold:
    @pytest.mark.parametrize(
        "severity,threshold,expected",
        [
            ("critical", "high", True),
            ("high", "high", True),
            ("medium", "high", False),
            ("low", "high", False),
            ("info", "high", False),
            ("critical", "info", True),
            ("info", "info", True),
            ("low", "medium", False),
            ("medium", "medium", True),
        ],
    )
    def test_threshold(self, severity, threshold, expected):
        assert severity_meets_threshold(severity, threshold) == expected

    def test_unknown_severity_defaults_zero(self):
        assert severity_meets_threshold("unknown", "info") is True
        assert severity_meets_threshold("unknown", "low") is False


class TestValidateSeverity:
    @pytest.mark.parametrize("value", ["info", "low", "medium", "high", "critical"])
    def test_valid_values(self, value):
        assert _validate_severity(value, "test") == value

    def test_case_insensitive(self):
        assert _validate_severity("HIGH", "test") == "high"
        assert _validate_severity("Critical", "test") == "critical"

    def test_invalid_value(self):
        with pytest.raises(ValueError, match="Invalid test"):
            _validate_severity("banana", "test")


class TestLoadConfig:
    def test_defaults(self):
        config = load_config(None)
        assert config.model == "claude-sonnet-4-6"
        assert config.max_tokens == 4096
        assert config.fail_on == "high"
        assert config.severity_threshold == "low"
        assert config.policies == []
        assert config.sensitive_resources == []
        assert config.ignore == []

    def test_full_config(self, full_config_path):
        config = load_config(full_config_path)
        assert config.model == "claude-sonnet-4-6"
        assert config.max_tokens == 8192
        assert config.fail_on == "critical"
        assert config.severity_threshold == "medium"
        assert len(config.policies) == 3
        assert config.policies[0].name == "no-public-ingress"
        assert config.policies[0].severity == "critical"
        assert len(config.sensitive_resources) == 3
        assert "aws_iam_*" in config.sensitive_resources
        assert len(config.ignore) == 2
        assert "*.auto.tfvars" in config.ignore

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/.tfrev.yaml")

    def test_empty_yaml(self, tmp_path):
        config_file = tmp_path / ".tfrev.yaml"
        config_file.write_text("")
        config = load_config(config_file)
        assert config.model == "claude-sonnet-4-6"

    def test_partial_config(self, tmp_path):
        config_file = tmp_path / ".tfrev.yaml"
        config_file.write_text("model: claude-opus-4-6\nfail_on: critical\n")
        config = load_config(config_file)
        assert config.model == "claude-opus-4-6"
        assert config.fail_on == "critical"
        assert config.max_tokens == 4096  # default

    def test_open_uses_utf8_encoding(self, tmp_path):
        """Config loads must pass encoding='utf-8' explicitly for cross-platform safety."""
        from unittest.mock import patch

        config_file = tmp_path / ".tfrev.yaml"
        config_file.write_text("model: claude-sonnet-4-6\n")

        real_open = open
        seen_kwargs: list[dict] = []

        def tracking_open(*args, **kwargs):
            seen_kwargs.append(kwargs)
            return real_open(*args, **kwargs)

        with patch("builtins.open", side_effect=tracking_open):
            load_config(config_file)

        assert any(kw.get("encoding") == "utf-8" for kw in seen_kwargs)

    def test_policy_with_invalid_severity_rejected(self, tmp_path):
        config_file = tmp_path / ".tfrev.yaml"
        config_file.write_text(
            "policies:\n"
            "  - name: typo-policy\n"
            "    description: has bad severity\n"
            "    severity: hign\n"
        )
        with pytest.raises(ValueError, match="policy 'typo-policy'"):
            load_config(config_file)

    def test_policy_parsing(self, full_config_path):
        config = load_config(full_config_path)
        blast = [p for p in config.policies if p.name == "blast-radius-limit"]
        assert len(blast) == 1
        assert blast[0].threshold == 10
        assert blast[0].severity == "high"

        tags = [p for p in config.policies if p.name == "require-standard-tags"]
        assert len(tags) == 1
        assert tags[0].required_tags == ["Environment", "Team"]


class TestFormatPoliciesForPrompt:
    def test_empty_policies(self):
        config = TfrevConfig()
        assert format_policies_for_prompt(config) == ""

    def test_with_policies(self):
        config = TfrevConfig(
            policies=[
                PolicyRule(
                    name="no-public-ingress",
                    description="Flag 0.0.0.0/0 rules",
                    severity="critical",
                )
            ]
        )
        output = format_policies_for_prompt(config)
        assert "CUSTOM POLICIES" in output
        assert "no-public-ingress" in output
        assert "critical" in output

    def test_with_sensitive_resources(self):
        config = TfrevConfig(sensitive_resources=["aws_iam_*", "aws_kms_key"])
        output = format_policies_for_prompt(config)
        assert "Sensitive Resources" in output
        assert "aws_iam_*" in output
        assert "aws_kms_key" in output

    def test_with_both(self):
        config = TfrevConfig(
            policies=[PolicyRule(name="test", description="test policy")],
            sensitive_resources=["aws_iam_*"],
        )
        output = format_policies_for_prompt(config)
        assert "CUSTOM POLICIES" in output
        assert "test" in output
        assert "aws_iam_*" in output
