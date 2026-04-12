# tfrev — Terraform Plan Reviewer (Architecture Document)

## 1. Project Overview

**tfrev** is an open-source CLI tool that uses Claude AI to review Terraform plan outputs against the corresponding infrastructure code changes. It acts as an automated safety gate that catches mismatches between developer *intent* (code diff) and Terraform's *execution plan* (plan JSON), surfacing security risks, unexpected side effects, and blast radius concerns before `terraform apply` is run.

### 1.1 Problem Statement

Terraform plans can be complex and easy to misread. A developer might change a tag and accidentally trigger a resource replacement. A module upgrade might silently widen a security group. Manual review of plans is tedious, error-prone, and doesn't scale across teams. Current static analysis tools (tfsec, checkov, OPA) check policy compliance but don't answer the fundamental question: **"Does this plan actually do what the code change intended?"**

### 1.2 Design Goals

| Goal | Description |
|------|-------------|
| **CI/CD Agnostic** | Single CLI binary usable in any pipeline — GitHub Actions, GitLab CI, Jenkins, Atlantis, Spacelift, or locally |
| **Zero Infrastructure** | No servers, databases, or queues. Just an API key and a CLI call |
| **Structured Output** | Machine-readable JSON output alongside human-readable Markdown for PR comments |
| **Configurable Policies** | Teams define custom review policies via `.tfrev.yaml` |
| **Open Source Friendly** | MIT license, pip-installable, minimal dependencies |
| **Cost Efficient** | Single API call per review. Typical cost: $0.01–$0.05 per review |

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                      CI/CD Pipeline                         │
│  (GitHub Actions / GitLab CI / Jenkins / Atlantis / Local)  │
└───────────────┬─────────────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────────────┐
│                        tfrev CLI                             │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │  Input Layer  │  │ Review Engine│  │   Output Layer     │  │
│  │              │  │              │  │                   │  │
│  │ • Plan JSON  │──▶│ • Prompt     │──▶│ • JSON report     │  │
│  │   Parser     │  │   Builder    │  │ • Markdown comment│  │
│  │ • Git Diff   │  │ • Claude API │  │ • Exit codes      │  │
│  │   Parser     │  │   Client     │  │ • SARIF (future)  │  │
│  │ • Config     │  │ • Response   │  │                   │  │
│  │   Loader     │  │   Parser     │  │                   │  │
│  └──────────────┘  └──────────────┘  └───────────────────┘  │
│                                                              │
└──────────────────────────────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────────────┐
│                   Anthropic Claude API                        │
│              (claude-sonnet-4-6 default)                    │
└──────────────────────────────────────────────────────────────┘
```

---

## 3. Component Design

### 3.1 Input Layer

The input layer is responsible for collecting, parsing, and normalizing the three data sources tfrev needs.

#### 3.1.1 Terraform Plan JSON Parser (`tfrev/plan_parser.py`)

**Source:** Output of `terraform show -json tfplan`

The JSON plan format (Terraform v0.12+) provides structured data including:

- `resource_changes[]` — Array of every resource being created, updated, replaced, or deleted
- `prior_state` — The state before changes
- `configuration` — The root module and child module configurations
- `planned_values` — The full resource tree after apply

**Key extraction logic:**

```
For each resource_change:
  ├── address        → e.g., "aws_instance.web[0]"
  ├── action         → create | update | delete | replace | no-op
  ├── before         → attribute map before change (null if creating)
  ├── after          → attribute map after change (null if deleting)
  └── after_unknown  → attributes that will be computed
```

**Why JSON over human-readable plan:**
The JSON format is deterministic, parseable, and includes metadata (like `after_unknown`) that the human-readable plan omits. It also avoids the fragile regex parsing needed for the pretty-printed output.

#### 3.1.2 Git Diff Parser (`tfrev/diff_parser.py`)

**Source:** Output of `git diff <base>...<head> -- '*.tf' '*.tfvars'`

Parses unified diff format to extract:

- Files changed (added, modified, deleted)
- Hunks with line-level additions and removals
- Context lines for surrounding code

The parser preserves file-level grouping so Claude can reason about which file drove which plan change.

#### 3.1.3 Configuration Loader (`tfrev/config.py`)

**Source:** `.tfrev.yaml` in project root (optional)

```yaml
# .tfrev.yaml
model: claude-sonnet-4-6         # Claude model to use
max_tokens: 4096                   # Max response tokens
severity_threshold: medium         # Minimum severity to report (low/medium/high/critical)
fail_on: high                      # Exit code 1 if any finding >= this severity

# Custom review policies
policies:
  - name: no-public-access
    description: "Flag any security group rule with 0.0.0.0/0"
    severity: critical

  - name: blast-radius-limit
    description: "Flag if more than 10 resources are being modified"
    threshold: 10
    severity: high

  - name: require-tags
    description: "Flag resources missing required tags"
    required_tags: ["Environment", "Team", "CostCenter"]
    severity: medium

# Resources to always flag for extra scrutiny
sensitive_resources:
  - aws_iam_*
  - aws_security_group*
  - aws_kms_key
  - aws_s3_bucket_policy

# Ignore patterns
ignore:
  - "*.auto.tfvars"
  - "backend.tf"
```

### 3.2 Review Engine

#### 3.2.1 Prompt Builder (`tfrev/prompt.py`)

The prompt builder constructs the full Claude prompt by assembling:

1. **System prompt** — The production review prompt (see Section 5)
2. **Plan data** — Formatted resource changes from the JSON plan
3. **Code diff** — The git diff of Terraform files
4. **Policy context** — Custom policies from `.tfrev.yaml`
5. **Output schema** — JSON schema for structured response

The prompt builder also handles **context window management**:

- If the combined input exceeds token limits, context files are dropped first; if the plan + diff alone still exceed the limit, the review is rejected with an error
- Each review is a single API call — there is no chunking or multi-pass synthesis

#### 3.2.2 Claude API Client (`tfrev/client.py`)

Thin wrapper around the Anthropic Python SDK:

```python
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
response = client.messages.create(
    model=config.model,
    max_tokens=config.max_tokens,
    system=system_prompt,
    messages=[{"role": "user", "content": user_prompt}]
)
```

**Error handling:**
- Rate limit retries with exponential backoff (3 attempts)
- API key validation on startup
- Token count estimation before sending (warn if approaching limits)
- Timeout after 120 seconds

#### 3.2.3 Response Parser (`tfrev/response_parser.py`)

Parses Claude's structured JSON response into internal `ReviewResult` objects. Validates against the output schema and handles malformed responses gracefully (falls back to raw text output).

### 3.3 Output Layer

#### 3.3.1 Output Formats

| Format | Flag | Use Case |
|--------|------|----------|
| **JSON** | `--output json` | Machine consumption, downstream tooling |
| **Markdown** | `--output markdown` | PR comments (GitHub, GitLab) |
| **Table** | `--output table` (default) | Terminal / local development |
| **SARIF** | `--output sarif` (future) | IDE integration, GitHub Code Scanning |

#### 3.3.2 Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Review passed — no findings at or above `fail_on` severity |
| `1` | Review failed — findings at or above `fail_on` severity |
| `2` | Error — API failure, invalid input, or configuration error |

This allows CI/CD pipelines to gate on `tfrev` results using standard exit code checks.

---

## 4. Data Flow

```
Developer pushes code
        │
        ▼
CI/CD Pipeline Triggered
        │
        ├──▶ terraform init
        ├──▶ terraform plan -out=tfplan
        ├──▶ terraform show -json tfplan > plan.json
        │
        ▼
tfrev review --plan plan.json
        │
        ├──▶ Parse plan.json → extract resource_changes
        ├──▶ Auto-generate git diff → extract code modifications
        ├──▶ Load .tfrev.yaml → merge with defaults
        ├──▶ Build prompt (system + user + policies)
        ├──▶ Call Claude API
        ├──▶ Parse structured response
        ├──▶ Apply severity thresholds
        │
        ▼
Output review (JSON/Markdown/Table)
        │
        ├──▶ Post as PR comment (if in CI)
        └──▶ Exit with appropriate code (0 = pass, 1 = fail)
```

---

## 5. CLI Interface

```bash
# Basic usage (diff is auto-generated via git)
tfrev review --plan plan.json

# Diff against a specific ref (e.g. last deployed SHA or tag)
tfrev review --plan plan.json --base-ref abc1234

# Auto-detect plan file (runs in git repo with existing tfplan)
tfrev review --auto

# Output as markdown for PR comment
tfrev review --plan plan.json --output markdown > comment.md

# Override config
tfrev review --plan plan.json \
  --model claude-sonnet-4-6 \
  --fail-on critical \
  --severity-threshold low

```

### 5.1 `--auto` Mode

When `--auto` is specified, tfrev will:

1. Look for `tfplan` or `*.tfplan` in the current directory
2. Run `terraform show -json` on it
3. Look for `.tfrev.yaml` in the project root

The diff is always auto-generated via `git diff`. The base ref is resolved in this order:

1. `--base-ref` flag (explicit)
2. `GITHUB_BASE_REF` env var (GitHub Actions)
3. `CI_MERGE_REQUEST_TARGET_BRANCH_NAME` env var (GitLab CI)
4. `CHANGE_TARGET` env var (Jenkins multibranch)
5. `main` (fallback)

If no changes are found against the base ref (e.g. first commit), tfrev falls back to diffing against the git empty tree, showing all current `.tf` files as new additions.

This makes the simplest CI integration a single line: `tfrev review --auto`

---

## 6. Technology Stack

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| Language | **Python 3.9+** | Universal CI availability, easy pip install, largest contributor pool |
| API Client | **anthropic** (official SDK) | Maintained by Anthropic, handles auth/retries |
| CLI Framework | **click** | Industry standard for Python CLIs |
| Packaging | **pip / PyPI** | `pip install tfrev` — works everywhere |
| Alternative Install | **Docker** | `docker run ghcr.io/org/tfrev` for hermetic environments |
| Testing | **pytest** | Standard Python testing |
| CI/CD Config | **YAML** | GitHub Actions + GitLab CI native format |

### 6.1 Why Python Over Go

While Go is Terraform's native language, Python was chosen because:

1. **CI runner availability** — Python is pre-installed on GitHub Actions runners, GitLab CI images, and most Jenkins agents. Go requires a build step or pre-compiled binary distribution
2. **Contributor accessibility** — Python has a larger open-source contributor base for DevOps tooling
3. **Anthropic SDK** — The official Python SDK is the most mature and best-documented
4. **Rapid iteration** — For a prompt-engineering-heavy tool, Python's iteration speed matters
5. **Single-file deployability** — Can be vendored as a single script in emergencies

---

## 7. Security Considerations

### 7.1 API Key Management

- API key is read exclusively from `ANTHROPIC_API_KEY` environment variable
- Never logged, never written to disk, never included in output
- In CI: stored as a pipeline secret/variable

### 7.2 Data Sensitivity

Terraform plans may contain sensitive values (passwords, keys). tfrev handles this by:

- Respecting Terraform's `sensitive = true` attribute (values show as `(sensitive)` in plan JSON)
- Never persisting plan data to disk beyond the review session
- All API calls use HTTPS (enforced by the Anthropic SDK)

### 7.3 Supply Chain

- Minimal dependencies (anthropic, click, pyyaml)
- Dependencies declared in `pyproject.toml` with minimum version pins
- GitHub Actions workflow for dependency auditing (dependabot)

---

## 8. Project Structure

```
tfrev/
├── README.md                          # User-facing documentation
├── ARCHITECTURE.md                    # This document
├── CHANGELOG.md                       # Release history
├── CONTRIBUTING.md                    # Contributor guide
├── LICENSE                            # MIT License
├── pyproject.toml                     # Python package configuration
├── .tfrev.yaml.example                # Example configuration
│
├── src/
│   └── tfrev/                         # Core Python package (src layout)
│       ├── __init__.py
│       ├── __main__.py                # Entry point (python -m tfrev)
│       ├── cli.py                     # Click CLI definition
│       ├── plan_parser.py             # Terraform plan JSON parser
│       ├── diff_parser.py             # Git unified diff parser
│       ├── config.py                  # Configuration loader
│       ├── prompt.py                  # Prompt builder (system + user)
│       ├── client.py                  # Anthropic API client wrapper
│       ├── response_parser.py         # Structured response parser
│       ├── output.py                  # Output formatters (JSON, Markdown, Table)
│       ├── tf_discovery.py            # Terraform source file context discovery
│       ├── py.typed                   # PEP 561 type marker
│       └── templates/
│           ├── system_prompt.txt      # Production system prompt
│           └── user_prompt.txt        # User prompt template
│
├── ci/                                # CI/CD integration configs
│   ├── github-action/
│   │   ├── action.yml                 # GitHub Action definition
│   │   └── example-workflow.yml       # Example workflow
│   ├── gitlab/
│   │   └── .gitlab-ci-template.yml    # GitLab CI template
│   └── jenkins/
│       └── Jenkinsfile                # Jenkins pipeline example
│
├── tests/
│   ├── conftest.py                    # Shared fixtures
│   ├── fixtures/                      # Sample plan.json, .diff, response files
│   ├── test_plan_parser.py
│   ├── test_diff_parser.py
│   ├── test_response_parser.py
│   ├── test_config.py
│   ├── test_prompt.py
│   ├── test_output.py
│   ├── test_tf_discovery.py
│   ├── test_client.py
│   └── test_cli.py
│
├── examples/
│   └── local-test/                    # Local testing example
│
└── Dockerfile                         # Container image for hermetic runs
```

---

## 9. Roadmap

### v1.0.0 (current)
- CLI with `--plan`, `--auto`, `--base-ref`
- Auto-generated git diff (no manual `--diff` required)
- Claude API integration with production prompt
- JSON, Markdown, and Table output
- `.tfrev.yaml` configuration with custom policies
- GitHub Action, GitLab CI, and Jenkins templates
- Context file discovery for surrounding Terraform files
- PyPI package

### Future
- Atlantis integration (webhook)
- Spacelift integration (custom action)
- SARIF output for GitHub Code Scanning
- Review history and trend tracking
- Multi-model support (configurable LLM backend)

---

## 10. Review Output Schema

```json
{
  "review": {
    "verdict": "FAIL",
    "confidence": 0.92,
    "summary": "The plan includes an unintended security group replacement that will cause downtime.",
    "findings": [
      {
        "id": "F001",
        "severity": "critical",
        "category": "intent_mismatch",
        "resource": "aws_security_group.web",
        "title": "Unintended resource replacement",
        "description": "The code change modifies a tag, but the plan shows a full replacement (destroy + create) of the security group. This will cause all associated ENIs to be detached, resulting in downtime.",
        "code_reference": {
          "file": "security.tf",
          "lines": "12-15"
        },
        "plan_reference": {
          "action": "replace",
          "address": "aws_security_group.web"
        },
        "recommendation": "Use lifecycle { create_before_destroy = true } or investigate why a tag change triggers replacement."
      }
    ],
    "stats": {
      "resources_reviewed": 12,
      "resources_changing": 3,
      "findings_by_severity": {
        "critical": 1,
        "high": 0,
        "medium": 2,
        "low": 1
      }
    }
  }
}
```
