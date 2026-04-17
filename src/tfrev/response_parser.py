"""Parse and validate Claude's structured JSON response."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


@dataclass
class Finding:
    """A single review finding."""

    id: str
    severity: str
    category: str
    resource: str
    title: str
    description: str
    code_reference: dict[str, str] | None = None
    plan_reference: dict[str, str] | None = None
    recommendation: str = ""


@dataclass
class ReviewStats:
    """Statistics from the review."""

    resources_reviewed: int = 0
    resources_changing: int = 0
    resources_created: int = 0
    resources_updated: int = 0
    resources_deleted: int = 0
    resources_replaced: int = 0
    findings_by_severity: dict[str, int] = field(
        default_factory=lambda: {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    )


@dataclass
class ReviewResult:
    """Parsed review result from Claude."""

    verdict: str  # PASS, WARN, FAIL
    confidence: float
    summary: str
    findings: list[Finding] = field(default_factory=list)
    stats: ReviewStats = field(default_factory=ReviewStats)
    unmapped_plan_changes: list[str] = field(default_factory=list)
    unmapped_code_changes: list[str] = field(default_factory=list)
    raw_response: str = ""  # Original response text
    parse_failed: bool = False  # True if the response could not be parsed as JSON


def parse_response(response_text: str) -> ReviewResult:
    """Parse Claude's JSON response into a ReviewResult.

    Handles cases where the response may be wrapped in markdown code fences
    or contain extra text before/after the JSON.
    """
    # Try to extract JSON from the response
    json_text = _extract_json(response_text)

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        # Fall back to a basic result with the raw text
        return ReviewResult(
            verdict="WARN",
            confidence=0.3,
            summary="Could not parse structured response. Raw review follows.",
            raw_response=response_text,
            parse_failed=True,
        )

    # Navigate to the review object
    review = data.get("review", data)  # Handle both {review: {...}} and flat format

    # Parse findings
    findings = []
    for f in review.get("findings", []):
        findings.append(
            Finding(
                id=f.get("id", "F000"),
                severity=f.get("severity", "info"),
                category=f.get("category", "unknown"),
                resource=f.get("resource", "unknown"),
                title=f.get("title", "Untitled finding"),
                description=f.get("description", ""),
                code_reference=f.get("code_reference"),
                plan_reference=f.get("plan_reference"),
                recommendation=f.get("recommendation", ""),
            )
        )

    # Parse stats
    stats_raw = review.get("stats", {})
    severity_counts = stats_raw.get("findings_by_severity", {})
    stats = ReviewStats(
        resources_reviewed=stats_raw.get("resources_reviewed", 0),
        resources_changing=stats_raw.get("resources_changing", 0),
        resources_created=stats_raw.get("resources_created", 0),
        resources_updated=stats_raw.get("resources_updated", 0),
        resources_deleted=stats_raw.get("resources_deleted", 0),
        resources_replaced=stats_raw.get("resources_replaced", 0),
        findings_by_severity={
            "critical": severity_counts.get("critical", 0),
            "high": severity_counts.get("high", 0),
            "medium": severity_counts.get("medium", 0),
            "low": severity_counts.get("low", 0),
            "info": severity_counts.get("info", 0),
        },
    )

    return ReviewResult(
        verdict=review.get("verdict", "WARN"),
        confidence=float(review.get("confidence", 0.5)),
        summary=review.get("summary", "No summary provided."),
        findings=findings,
        stats=stats,
        unmapped_plan_changes=review.get("unmapped_plan_changes", []),
        unmapped_code_changes=review.get("unmapped_code_changes", []),
        raw_response=response_text,
    )


def _extract_json(text: str) -> str:
    """Extract JSON from text that may contain markdown code fences or extra text."""
    # Prefer an explicitly ```json-tagged fence when one exists, since Claude may
    # include other fenced blocks (hcl examples, etc.) alongside the JSON answer.
    json_fence = re.search(r"```json\s*\n?(.*?)\n?```", text, re.DOTALL)
    if json_fence:
        return json_fence.group(1).strip()

    # Fall back to any fenced block.
    fence_match = re.search(r"```\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()

    # Try: find outermost { ... }, skipping braces inside strings
    brace_start = text.find("{")
    if brace_start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(brace_start, len(text)):
            c = text[i]
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[brace_start : i + 1]

    # Fall back to the whole text
    return text.strip()
