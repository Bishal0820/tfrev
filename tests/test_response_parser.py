"""Tests for tfrev.response_parser."""

from __future__ import annotations

import json

from tfrev.response_parser import (
    _extract_json,
    parse_response,
)


class TestParseResponse:
    def test_pass_verdict(self, pass_response_text):
        result = parse_response(pass_response_text)
        assert result.verdict == "PASS"
        assert result.confidence == 0.95
        assert len(result.findings) == 0

    def test_fail_verdict(self, fail_response_text):
        result = parse_response(fail_response_text)
        assert result.verdict == "FAIL"
        assert result.confidence == 0.92
        assert len(result.findings) == 3

    def test_finding_fields(self, fail_response_text):
        result = parse_response(fail_response_text)
        f = result.findings[0]
        assert f.id == "F001"
        assert f.severity == "critical"
        assert f.category == "security"
        assert f.resource == "aws_security_group.web_sg"
        assert "SSH" in f.title
        assert f.code_reference is not None
        assert f.code_reference["file"] == "main.tf"
        assert f.plan_reference is not None
        assert f.plan_reference["action"] == "update"
        assert f.recommendation != ""

    def test_stats_fields(self, fail_response_text):
        result = parse_response(fail_response_text)
        assert result.stats.resources_reviewed == 3
        assert result.stats.resources_changing == 2
        assert result.stats.findings_by_severity["critical"] == 1
        assert result.stats.findings_by_severity["medium"] == 1

    def test_unmapped_changes(self, fail_response_text):
        result = parse_response(fail_response_text)
        assert "aws_route53_record.drift" in result.unmapped_plan_changes
        assert "outputs.tf" in result.unmapped_code_changes

    def test_fenced_json(self, fenced_response_text):
        result = parse_response(fenced_response_text)
        assert result.verdict == "PASS"
        assert len(result.findings) == 1
        assert result.findings[0].severity == "info"

    def test_malformed_response(self, malformed_response_text):
        result = parse_response(malformed_response_text)
        assert result.verdict == "WARN"
        assert result.confidence == 0.3
        assert "Could not parse" in result.summary
        assert result.raw_response == malformed_response_text

    def test_malformed_response_flags_parse_failed(self, malformed_response_text):
        result = parse_response(malformed_response_text)
        assert result.parse_failed is True

    def test_valid_response_does_not_flag_parse_failed(self, pass_response_text):
        result = parse_response(pass_response_text)
        assert result.parse_failed is False

    def test_flat_format(self):
        """Test response without the 'review' wrapper."""
        flat = json.dumps(
            {
                "verdict": "PASS",
                "confidence": 0.9,
                "summary": "All good.",
                "findings": [],
                "stats": {
                    "resources_reviewed": 1,
                    "resources_changing": 0,
                    "findings_by_severity": {},
                },
            }
        )
        result = parse_response(flat)
        assert result.verdict == "PASS"

    def test_raw_response_preserved(self, pass_response_text):
        result = parse_response(pass_response_text)
        assert result.raw_response == pass_response_text


class TestExtractJson:
    def test_plain_json(self):
        text = '{"key": "value"}'
        assert _extract_json(text) == '{"key": "value"}'

    def test_fenced_json(self):
        text = 'Some text\n```json\n{"key": "value"}\n```\nMore text'
        assert _extract_json(text) == '{"key": "value"}'

    def test_fenced_no_language(self):
        text = 'Text\n```\n{"key": "value"}\n```\nMore'
        assert _extract_json(text) == '{"key": "value"}'

    def test_json_with_surrounding_text(self):
        text = 'Here is the result: {"key": "value"} and done.'
        assert json.loads(_extract_json(text)) == {"key": "value"}

    def test_nested_braces(self):
        text = '{"outer": {"inner": "value"}}'
        extracted = _extract_json(text)
        assert json.loads(extracted) == {"outer": {"inner": "value"}}

    def test_no_json(self):
        text = "Just plain text with no JSON"
        result = _extract_json(text)
        assert result == text.strip()

    def test_braces_in_strings(self):
        text = '{"msg": "a {nested} brace"}'
        extracted = _extract_json(text)
        assert json.loads(extracted) == {"msg": "a {nested} brace"}

    def test_prefers_json_tagged_fence_over_earlier_fence(self):
        """When multiple fenced blocks exist, prefer the ```json one."""
        text = (
            "Here's a code example:\n"
            "```hcl\n"
            'resource "aws_s3_bucket" "b" {}\n'
            "```\n"
            "And the review output:\n"
            "```json\n"
            '{"verdict": "PASS"}\n'
            "```\n"
        )
        extracted = _extract_json(text)
        assert json.loads(extracted) == {"verdict": "PASS"}
