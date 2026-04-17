"""Configuration loader for tfrev."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class PolicyRule:
    """A custom review policy."""

    name: str
    description: str
    severity: str = "medium"
    threshold: int | None = None
    required_tags: list[str] | None = None


@dataclass
class TfrevConfig:
    """tfrev configuration."""

    # Claude settings
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 4096

    # Review settings
    severity_threshold: str = "low"  # Minimum severity to include in output
    fail_on: str = "high"  # Exit code 1 if any finding >= this

    # Custom policies
    policies: list[PolicyRule] = field(default_factory=list)

    # Sensitive resources (always flagged for extra scrutiny)
    sensitive_resources: list[str] = field(default_factory=list)

    # Ignore patterns
    ignore: list[str] = field(default_factory=list)


# Severity ordering for comparisons
SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def severity_meets_threshold(severity: str, threshold: str) -> bool:
    """Check if a severity level meets or exceeds a threshold."""
    return SEVERITY_ORDER.get(severity, 0) >= SEVERITY_ORDER.get(threshold, 0)


_VALID_SEVERITIES = set(SEVERITY_ORDER.keys())


def _validate_severity(value: str, field_name: str) -> str:
    """Validate that a severity value is recognized."""
    value = value.lower()
    if value not in _VALID_SEVERITIES:
        valid = ", ".join(sorted(_VALID_SEVERITIES, key=lambda s: SEVERITY_ORDER[s]))
        raise ValueError(f"Invalid {field_name} '{value}'. Must be one of: {valid}")
    return value


def load_config(config_path: str | Path | None = None) -> TfrevConfig:
    """Load configuration from .tfrev.yaml, falling back to defaults."""
    config = TfrevConfig()

    # Search order: explicit path > current directory > parent directories
    if config_path:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
    else:
        found = _find_config_file()
        if found is None:
            return config  # Use defaults
        path = found

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not raw or not isinstance(raw, dict):
        return config

    # Apply overrides
    if "model" in raw:
        config.model = raw["model"]
    if "max_tokens" in raw:
        config.max_tokens = int(raw["max_tokens"])
    if "severity_threshold" in raw:
        config.severity_threshold = _validate_severity(
            raw["severity_threshold"], "severity_threshold"
        )
    if "fail_on" in raw:
        config.fail_on = _validate_severity(raw["fail_on"], "fail_on")
    if "sensitive_resources" in raw:
        config.sensitive_resources = raw["sensitive_resources"]
    if "ignore" in raw:
        config.ignore = raw["ignore"]

    # Parse policies
    for policy_raw in raw.get("policies", []):
        policy_name = policy_raw.get("name", "unnamed")
        severity = _validate_severity(
            policy_raw.get("severity", "medium"),
            f"policy '{policy_name}' severity",
        )
        config.policies.append(
            PolicyRule(
                name=policy_name,
                description=policy_raw.get("description", ""),
                severity=severity,
                threshold=policy_raw.get("threshold"),
                required_tags=policy_raw.get("required_tags"),
            )
        )

    return config


def _find_config_file() -> Path | None:
    """Search for .tfrev.yaml in current and parent directories."""
    current = Path.cwd()
    for _ in range(10):  # Limit search depth
        candidate = current / ".tfrev.yaml"
        if candidate.exists():
            return candidate
        candidate = current / ".tfrev.yml"
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def format_policies_for_prompt(config: TfrevConfig) -> str:
    """Format custom policies for inclusion in the Claude prompt."""
    if not config.policies and not config.sensitive_resources:
        return ""

    lines = ["## CUSTOM POLICIES", ""]
    lines.append("The following team-specific policies MUST be evaluated:")
    lines.append("")

    for policy in config.policies:
        lines.append(f"### Policy: {policy.name}")
        lines.append(f"- Description: {policy.description}")
        lines.append(f"- Severity: {policy.severity}")
        if policy.threshold is not None:
            lines.append(f"- Threshold: {policy.threshold}")
        if policy.required_tags:
            lines.append(f"- Required tags: {', '.join(policy.required_tags)}")
        lines.append("")

    if config.sensitive_resources:
        lines.append("### Sensitive Resources (require extra scrutiny)")
        lines.append(
            "Any changes to resources matching these patterns should be flagged "
            "at minimum 'medium' severity:"
        )
        for pattern in config.sensitive_resources:
            lines.append(f"  - {pattern}")
        lines.append("")

    return "\n".join(lines)
