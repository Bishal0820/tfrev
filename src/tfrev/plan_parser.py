"""Parse Terraform plan JSON output into structured resource changes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AttributeChange:
    """A single attribute that changed on a resource."""

    name: str
    before: Any
    after: Any
    is_sensitive: bool = False
    is_computed: bool = False  # True if value is unknown until apply


@dataclass
class ResourceChange:
    """A single resource change from the Terraform plan."""

    address: str  # e.g., "aws_instance.web[0]"
    resource_type: str  # e.g., "aws_instance"
    provider: str  # e.g., "registry.terraform.io/hashicorp/aws"
    action: str  # create, update, delete, replace, no-op
    attribute_changes: list[AttributeChange] = field(default_factory=list)
    module_address: str | None = None  # e.g., "module.vpc"


@dataclass
class PlanSummary:
    """Parsed summary of a Terraform plan."""

    resource_changes: list[ResourceChange]
    terraform_version: str
    format_version: str
    total_resources: int = 0
    creating: int = 0
    updating: int = 0
    deleting: int = 0
    replacing: int = 0
    no_op: int = 0
    @property
    def has_changes(self) -> bool:
        return (self.creating + self.updating + self.deleting + self.replacing) > 0


# --- Action mapping ---
# Terraform JSON uses arrays like ["create"], ["update"], ["delete", "create"] for replace
_ACTION_MAP = {
    frozenset(["create"]): "create",
    frozenset(["update"]): "update",
    frozenset(["delete"]): "delete",
    frozenset(["delete", "create"]): "replace",  # covers both orderings
    frozenset(["read"]): "no-op",
    frozenset(["no-op"]): "no-op",
}


def _resolve_action(actions: list[str]) -> str:
    """Convert Terraform's action array to a single action string."""
    key = frozenset(actions)
    return _ACTION_MAP.get(key, "unknown")


def _extract_attribute_changes(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    after_unknown: dict[str, Any] | None,
    before_sensitive: Any = None,
    after_sensitive: Any = None,
) -> list[AttributeChange]:
    """Extract individual attribute changes between before and after states."""
    changes = []
    before = before or {}
    after = after or {}
    after_unknown = after_unknown or {}

    # Normalize sensitive flags
    before_sens = before_sensitive if isinstance(before_sensitive, dict) else {}
    after_sens = after_sensitive if isinstance(after_sensitive, dict) else {}

    all_keys = before.keys() | after.keys()

    for key in sorted(all_keys):
        before_val = before.get(key)
        after_val = after.get(key)
        is_computed = after_unknown.get(key, False)
        is_sensitive = before_sens.get(key, False) or after_sens.get(key, False)

        # Skip attributes that haven't changed
        if before_val == after_val and not is_computed:
            continue

        # Redact sensitive values
        display_before = "(sensitive)" if is_sensitive and before_val is not None else before_val
        display_after = "(sensitive)" if is_sensitive and after_val is not None else after_val
        if is_computed:
            display_after = "(known after apply)"

        changes.append(
            AttributeChange(
                name=key,
                before=display_before,
                after=display_after,
                is_sensitive=is_sensitive,
                is_computed=is_computed,
            )
        )

    return changes


def parse_plan_json(plan_json: dict[str, Any]) -> PlanSummary:
    """Parse the output of `terraform show -json tfplan` into a PlanSummary."""
    resource_changes = []
    counts = {"create": 0, "update": 0, "delete": 0, "replace": 0, "no-op": 0}

    for rc in plan_json.get("resource_changes", []):
        change = rc.get("change", {})
        actions = change.get("actions", ["no-op"])
        action = _resolve_action(actions)

        # Count
        if action in counts:
            counts[action] += 1

        # Extract attribute-level changes
        attr_changes = _extract_attribute_changes(
            before=change.get("before"),
            after=change.get("after"),
            after_unknown=change.get("after_unknown"),
            before_sensitive=change.get("before_sensitive"),
            after_sensitive=change.get("after_sensitive"),
        )

        # Determine module address
        module_addr = rc.get("module_address")

        resource_changes.append(
            ResourceChange(
                address=rc.get("address", "unknown"),
                resource_type=rc.get("type", "unknown"),
                provider=rc.get("provider_name", "unknown"),
                action=action,
                attribute_changes=attr_changes,
                module_address=module_addr,
            )
        )

    return PlanSummary(
        resource_changes=resource_changes,
        terraform_version=plan_json.get("terraform_version", "unknown"),
        format_version=plan_json.get("format_version", "unknown"),
        total_resources=len(resource_changes),
        creating=counts["create"],
        updating=counts["update"],
        deleting=counts["delete"],
        replacing=counts["replace"],
        no_op=counts["no-op"],
    )


def load_plan_file(path: str | Path) -> PlanSummary:
    """Load and parse a Terraform plan JSON file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Plan file not found: {path}")

    with open(path) as f:
        try:
            plan_json = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in plan file: {e}") from e

    # Validate it looks like a Terraform plan
    if "resource_changes" not in plan_json and "planned_values" not in plan_json:
        raise ValueError(
            "File does not appear to be a Terraform plan JSON. "
            "Generate it with: terraform show -json tfplan > plan.json"
        )

    return parse_plan_json(plan_json)


def format_plan_for_prompt(summary: PlanSummary) -> str:
    """Format a PlanSummary into a human-readable string for the Claude prompt."""
    lines = []
    lines.append(f"Terraform Version: {summary.terraform_version}")
    lines.append(
        f"Summary: {summary.creating} to create, {summary.updating} to update, "
        f"{summary.deleting} to delete, {summary.replacing} to replace "
        f"({summary.total_resources} total resources reviewed)"
    )
    lines.append("")

    for rc in summary.resource_changes:
        if rc.action == "no-op":
            continue  # Skip unchanged resources to save tokens

        action_symbol = {
            "create": "+",
            "update": "~",
            "delete": "-",
            "replace": "-/+",
        }.get(rc.action, "?")

        module_prefix = f" (in {rc.module_address})" if rc.module_address else ""
        lines.append(f"[{action_symbol}] {rc.address}{module_prefix}")
        lines.append(f"    Action: {rc.action}")
        lines.append(f"    Type: {rc.resource_type}")
        lines.append(f"    Provider: {rc.provider}")

        if rc.attribute_changes:
            lines.append("    Changed attributes:")
            for attr in rc.attribute_changes:
                lines.append(f"      {attr.name}:")
                lines.append(f"        before: {attr.before}")
                lines.append(f"        after:  {attr.after}")
        lines.append("")

    if not any(rc.action != "no-op" for rc in summary.resource_changes):
        lines.append("(No changes — all resources are up to date)")

    return "\n".join(lines)
