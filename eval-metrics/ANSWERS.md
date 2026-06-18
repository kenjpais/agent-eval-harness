# ANSWERS.md

Consolidated answers to all questions in `QUESTIONS.md`.

> **Legend:** `[FACT]` = verified from PR diffs/code. `[REC]` = recommendation or design guidance. `[GAP]` = unanswered or ambiguous in current implementation.

---

## PR Relationship Overview

The three PRs form a stack across two separate ecosystems:

| PR | Repo | Role | Telemetry sink |
|----|------|------|---------------|
| agentic-ci #84 | `opendatahub-io/agentic-ci` | Generic CI harness — adds MLflow trace push | MLflow (OTLP) |
| ai-helpers #536 | `openshift-eng/ai-helpers` | OpenShift plugin — ships collector + BigQuery extractor | BigQuery (autodl JSON) |
| release #80547 | `openshift/release` | Prow CI deployment — wires #536 into payload agent step | BigQuery via #536 |

**Chronology** `[FACT]`: #536 was created June 9 and #84 June 11 — they are parallel PRs from collaborating teams, both building on pre-existing OTEL infrastructure in agentic-ci. #80547 was created the same day #536 merged (June 15) and depends explicitly on it.

---

## PR #84 — `opendatahub-io/agentic-ci`

### What is agentic-ci?

`[FACT]` A Python CLI (`pip install agentic-ci`) that wraps Claude Code for CI pipelines (GitLab CI, GitHub Actions). It manages the full agent-run lifecycle: starting a local OTEL collector, setting environment variables, running `claude -p`, and collecting telemetry artifacts. Generic — not OpenShift-specific.

Before PR #84, agentic-ci already had OTEL infrastructure collecting metrics and logs (`OTEL_METRICS_EXPORTER=otlp`, `OTEL_LOGS_EXPORTER=otlp` in `harness.py`). PR #84 adds:
- `OTEL_TRACES_EXPORTER=otlp` — enables span/trace export (call hierarchy)
- `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`, `OTEL_LOG_USER_PROMPTS=1`, `OTEL_LOG_TOOL_DETAILS=1`, `OTEL_LOG_TOOL_CONTENT=1` — adds prompt text, tool inputs, tool outputs to telemetry
- `agentic-ci mlflow-push` CLI subcommand — reads `claude-otel.jsonl`, POSTs `/v1/traces` records to an MLflow OTLP endpoint

### What is an OTel span?

`[FACT]` A named, timed unit of work in a distributed trace: e.g., "turn 3", "Bash tool call", "API request to claude-opus-4-6". Spans have start/end times, attributes (model, cost, tool name), and a parent span ID forming a call tree. A *trace* is the complete span tree for one Claude session. Before PR #84, only aggregate metrics and per-request logs were collected — not the span hierarchy.

### Problem solved / use-case

`[FACT]` Without traces you can see "session cost $3, 45 tool calls" but not which calls were expensive. Traces enable per-request debugging: which tool calls drove cost, what Claude was reasoning about at each step. Combined with `OTEL_LOG_USER_PROMPTS=1` and `OTEL_LOG_TOOL_CONTENT=1`, full prompt text and tool I/O appear in telemetry, enabling deep debugging in MLflow.

### Usage example

```yaml
# GitLab CI
agent-run:
  script: agentic-ci run --harness claude-code "Fix the bug"
  artifacts:
    paths: [claude-otel.jsonl]    # collector writes here during run

trace-push:
  needs: [agent-run]
  allow_failure: true             # never blocks pipeline
  script:
    agentic-ci mlflow-push claude-otel.jsonl
      --endpoint $MLFLOW_TRACKING_URI
      --experiment rfe-autofixer
      --token $MLFLOW_TRACKING_TOKEN
```

### "Separate follow-up CI job" / "pipeline"

`[FACT]` In GitLab CI / GitHub Actions, a separate job with `allow_failure: true` means trace-push failures never fail the overall pipeline. It can be conditionally skipped if `MLFLOW_TRACKING_URI` is unset. In the OpenShift context (PR #80547), there is no separate job yet — the JSONL is saved as a Prow artifact and MLflow upload is marked "Future" in `metrics-collection.md`.

### Relation to agent-eval-harness

`[FACT]` They are independent systems. agentic-ci is a CI runtime (runs Claude agents, collects telemetry). agent-eval-harness is an evaluation framework (runs skills/prompts against test cases, scores outputs). PR #84 does not touch agent-eval-harness. However, the `agentic-ci mlflow-push` CLI can be used after eval runs to push traces produced by #536's collector — no `agentic-ci run` needed.

---

## PR #536 — `openshift-eng/ai-helpers`

### What is the prow-agent plugin?

`[FACT]` A Claude Code plugin installable via `claude plugin install prow-agent@ai-helpers`, registered in the ai-helpers marketplace at version `0.0.3`. Ships two standalone Python scripts (zero external dependencies):
- `otel_collector.py` — minimal OTLP HTTP server; accepts Claude's telemetry pushes on an ephemeral port, writes each request as a JSONL line
- `extract_metrics.py` — parses `claude-otel.jsonl` and produces a BigQuery-loadable autodl JSON file

### OTel collector approach — ported from agentic-ci

`[FACT]` The pre-existing `agentic_ci/otel.py` already implemented this pattern. PR #536 extracts it into a standalone script with: ephemeral port `0` with port file for discovery, `127.0.0.1`-only binding, 1MB max payload, stdlib only. Added: `--summary` mode for human-readable cost/token breakdown.

### What is autodl?

`[FACT]` OpenShift CI's BigQuery ingestion format. A JSON file with `table_name`, `schema` (column types), and `rows` (all values as strings). Files matching `*-autodl.json` in `$ARTIFACT_DIR` are automatically loaded into BigQuery. The new table `claude_session_metrics` is additive — no migration needed.

### What is `--stream-log` and why?

`[FACT]` Points `extract_metrics.py` to Claude's `--output-format stream-json` output captured via `tee`. OTEL provides cost/tokens/tool counts but NOT identity fields. The stream-json `init` message contains `session_id`, `model`, `claude_code_version`, `plugins_loaded`; the `result` message contains `duration_ms`, `num_turns`, `terminal_reason`. Without `--stream-log`, these fields are blank in BigQuery. Not strictly required — extraction works without it, just with empty identity fields.

### OTel collection vs. legacy stream-JSON

| | OTEL | Legacy stream-JSON |
|--|--|--|
| Granularity | Per API request (10s metric flush; per-call logs/traces) | Session totals only |
| Cost | Per model, per invocation | Session total only |
| Tool usage | Name + duration per call | Total count only |
| Identity | NOT available (session_id, model version) | Available in `init` message |
| Session outcome | NOT available | Available in `result` message |
| Collection | Pushed to local HTTP server | `--output-format stream-json \| tee` |

`extract_metrics.py` auto-detects format from JSONL content and produces the same autodl schema either way.

### Open Questions — answered

**Q1: Should agent-eval step also use the collector?**
`[REC]` Yes. Add OTEL collection to eval runs to get per-request breakdown per test case. The existing run data (case-002 cost $6.77 / 100 turns) has no visibility into why. Implementation: wrap each case's `claude` invocation with collector start/stop.

**Q2: `OTEL_LOG_TOOL_CONTENT=1` data volume?**
`[REC]` Default off for production runs. Tool outputs can be multi-KB per call — 10-50MB JSONL for complex sessions. BigQuery extraction ignores tool content anyway. Enable only for debugging specific sessions via opt-in flag.

**Q3: Identity fields — dual-file or OTEL resource attributes?**
`[REC]` Continue dual-file approach. Embedding `session_id` in OTEL resource attributes requires an upstream Claude Code API change. The `--stream-log` path is tested (5 test cases) and correct.

### prow-agent vs. agent-eval-harness

| | prow-agent plugin | agent-eval-harness |
|--|--|--|
| Purpose | Collect observability data | Score output quality |
| Measures | Cost, tokens, latency, tool frequency | Judge pass rates, LLM quality scores |
| Answers | "What did the agent do and how much did it cost?" | "Did the agent do the right thing?" |
| Output | BigQuery rows, OTEL JSONL | Eval report, MLflow run |

They are complementary: prow-agent provides continuous production signal; agent-eval-harness provides controlled point-in-time quality measurement. Together: "Did docs improve quality?" (eval-harness) AND "Did production behavior change?" (OTEL in BigQuery).

### prow-agent vs. agentic-ci

| | agentic-ci | prow-agent plugin |
|--|--|--|
| Install | `pip install agentic-ci` | `claude plugin install prow-agent@ai-helpers` |
| CI system | GitLab CI, GitHub Actions | Prow (OpenShift CI) |
| Telemetry sink | MLflow | BigQuery (autodl) |
| Collector | Built into package | Standalone script, zero deps |
| Scope | Generic | OpenShift-specific |

---

## PR #80547 — `openshift/release`

### What it does and where it runs

`[FACT]` Modifies `ci-operator/step-registry/openshift/claude/payload/agent/openshift-claude-payload-agent-commands.sh` — the **OpenShift payload agent Prow step** that analyzes nightly build failures using Claude. Runs inside Prow job containers on the OpenShift CI cluster. Triggered via Gangway or (after this PR) automatically using the latest rejected nightly.

### Can ai-helpers #536 be used without Prow?

`[FACT]` Yes. `otel_collector.py` and `extract_metrics.py` have zero Prow-specific dependencies — stdlib only, no `$ARTIFACT_DIR`, no Prow APIs. The `$ARTIFACT_DIR` variable appears only in PR #80547's script, not in #536. The plugin can be installed and used in any Claude Code environment including local dev.

### prow-agent vs. must-gather plugin

- `must-gather@ai-helpers`: Domain skill — gives Claude knowledge to analyze must-gather archives. The model uses it actively during analysis.
- `prow-agent@ai-helpers`: Infrastructure plugin — OTEL collector and metrics extraction scripts. Claude never invokes it; it runs before and after Claude as sidecar processes.

### Port file

`[FACT]` A temporary file whose sole content is a TCP port number (e.g., `"54321"`). The collector binds to port `0` (OS-assigned), writes the actual port to this file, then serves. The shell script polls until the file is non-empty, then reads the port. Avoids hardcoded ports (collision risk).

### Lifecycle explained

```bash
python3 otel_collector.py --port-file /tmp/port --log-file $ARTIFACT_DIR/claude-otel.jsonl &
while [ ! -s /tmp/port ]; do sleep 0.1; done
export OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:$(cat /tmp/port)"
export CLAUDE_CODE_ENABLE_TELEMETRY=1
# ...other OTEL vars...
claude -p "..." --output-format stream-json | tee "$ARTIFACT_DIR/claude-output.log"
kill $COLLECTOR_PID; wait $COLLECTOR_PID
python3 extract_metrics.py claude-otel.jsonl metrics-autodl.json --stream-log claude-output.log
```

### `claude-otel.jsonl` as Prow artifact

`[FACT]` Saved to `$ARTIFACT_DIR` (backed by GCS), browsable in the Prow UI. Enables per-request debugging: download and run `otel_collector.py --summary claude-otel.jsonl` for a human-readable cost breakdown, or grep `/v1/logs` records for expensive tool calls. Future: feed to `agentic-ci mlflow-push` for MLflow upload.

### JUnit test cases

`[FACT]` JUnit XML is parsed natively by Prow and shown on the test grid. PR #80547 adds metrics extraction as a testcase: pass = `extract_metrics.py` succeeded; fail = extraction errored. Makes collection failures visible on the test grid without log inspection.

### Payload-triage table

`[FACT]` BigQuery table `payload_triage` records one row per job analysis: `payload_tag`, `version`, `stream`, `phase`, `failed_blocking_jobs`, `job_name`, `failure_type`, `root_cause_summary`, `candidate_pr_url`, etc. For rejected payloads, Claude writes analysis rows. PR #80547 adds deterministic zero-failure rows for accepted payloads (no Claude invocation needed).

---

## General: Evaluation Framework

### Can agent-eval-harness use agentic-ci #84 for OTEL + MLflow?

`[FACT]` Partially. Two parts of #84 have different relevance:

| Part of #84 | Usable with agent-eval-harness? | How |
|--|--|--|
| env vars in `harness.py` | Not automatically | Set manually before `eval-run` |
| `agentic-ci mlflow-push` CLI | Yes, directly | Run after `eval-run` against JSONL from #536's collector |

`agentic-ci run` cannot wrap agent-eval-harness: `agentic-ci run` expects a single `claude -p <prompt>` invocation; agent-eval-harness is a Python orchestrator that spawns many Claude invocations internally. The two operate at different abstraction levels.

**Practical workflow** — no `agentic-ci run` needed:
```bash
# Set OTEL env vars manually
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_TRACES_EXPORTER=otlp
# ... (11 vars from #84's harness.py)

# Start #536's collector
python3 otel_collector.py --port-file /tmp/port --log-file ./claude-otel.jsonl &
export OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:$(cat /tmp/port)"

# Run eval normally
eval-run --model opus

# Push to MLflow using #84's CLI
agentic-ci mlflow-push ./claude-otel.jsonl --endpoint $MLFLOW_TRACKING_URI \
    --experiment agentic-docs-component-eval --token $MLFLOW_TRACKING_TOKEN
```

### Do we need PR #80547 to collect OTEL with agent-eval-harness?

`[FACT]` No. PR #80547 is exclusively Prow-specific wiring for one shell script. Only #536's two standalone scripts are needed for agent-eval-harness integration.

### Combined evaluation workflow

**Phase 1 — Skill quality** (current, already working):
```bash
eval-run --model opus-4-6
# → quality scores (6 judges), cost/turns/tokens per case
# → MLflow experiment: agentic-docs-component-eval (already configured)
```

**Phase 2 — Documentation effectiveness** (requires PR #77 prompt mode):
```bash
/eval-analyze --prompt builtin:docs   # taxonomy-based eval.yaml
/eval-dataset                          # generate navigation/anti-pattern test cases
/eval-run --model sonnet               # agent tested against generated docs
# → documentation_tracking: which files were Read
# → LLM rubric: did agent follow constraints?
```

**Phase 3 — Production signal** (uses PR #536/#80547, already deployed):
```
Deploy agentic-docs to Prow container
→ Query BigQuery: claude_session_metrics WHERE analyzed_at > deploy_date
→ Compare cost_per_turn, cache_hit_rate, total_tool_calls before/after
```

### Most useful features for evaluating agentic-docs

**Highest priority:**
1. Existing case-mode eval — structural + LLM quality scores already working
2. PR #77 prompt mode + `documentation_tracking: true` — directly tests whether agents use and follow generated docs

**High priority (cost/efficiency):**
3. OTEL metrics during eval runs — per-case cost breakdown (current: $0.95–$6.77 range with no per-request visibility)
4. MLflow run comparison — already configured in eval.yaml

**Medium (production signal):**
5. prow-agent OTEL → BigQuery — requires deploying docs to Prow first

### Metrics to collect

**Already collected** (from `run_result.json`):
- Cost per case (USD), turns per case, token breakdown (input/output/cache_read/cache_create)
- Cache hit rate: `cache_read / (input + cache_read + cache_create)`
- Task completion time

**Collectible via PR #77 prompt mode + `documentation_tracking`:**
- Documentation consultation: which files were Read, were the right docs consulted
- Constraint compliance: LLM rubric judge
- Anti-pattern rejection: did agent refuse forbidden patterns

**Currently missing — requires OTEL in eval runner:**
- Per-request latency (`duration_api_ms`)
- Tool usage frequency breakdown (Read vs. Write vs. Bash vs. Skill)
- Per-model cost attribution

**Key agentic-docs-specific metrics:**
- LLM quality score (0–100): component focus, Platform separation, completeness, leanness
- No-generic-duplication pass rate (most specific to the docs' design intent)
- Cache hit rate (good docs written early → higher cache reuse in later turns)
- Turns to completion (better docs → less redundant exploration)

### Should we use LangFuse?

`[REC]` No. LangFuse is not mentioned in any of the three PRs or the existing eval.yaml. The current stack commits to MLflow across both agentic-ci and agent-eval-harness. Adding LangFuse would create a redundant, competing telemetry pipeline — exactly what the OTEL_SUPPORT.md spec aims to avoid. If LangFuse is already deployed and MLflow is not, substitute it for MLflow; do not run both.

### Should we use OTel?

`[REC]` Yes — it is already the chosen standard across all three PRs. The gap is that agent-eval-harness does not yet start the collector around eval runs. Closing that gap (Open Question #1 in `metrics-collection.md`) gives per-request observability during evaluation without changing judge behavior.

### LangFuse and OTel integration

`[REC]` Do not use both. Architecture: **OTel → MLflow**. If LangFuse is preferred, substitute it as the OTel backend, not alongside MLflow.

### LLM-as-a-judge via agent-eval-harness

`[FACT]` Already implemented. The current `eval.yaml` uses an LLM judge (`content_quality_and_separation`, 0–100). PR #77 adds `llm_rubric` shorthand. For agentic-docs, add prompt-mode judges:

```yaml
- name: docs_navigability
  llm_rubric: |
    The agent used generated docs to answer questions about the component's
    CRDs, architecture, and development workflow. It read relevant ai-docs/
    files and cited them in its reasoning.

- name: constraint_compliance
  llm_rubric: |
    The agent did not generate generic platform patterns inline (testing pyramid,
    controller-runtime reconciliation, STRIDE). All such content was delegated
    to Platform references as documented in AGENTS.md.
```

### Baseline vs. experimental comparison

`[FACT]` MLflow is already configured (`experiment: agentic-docs-component-eval` in eval.yaml). Regression thresholds already set: `content_quality_and_separation: min_mean: 75.0`, all structural judges `min_pass_rate: 1.0`.

**Workflow:**
1. Baseline: `eval-run` without agentic-docs plugin → MLflow run tagged `baseline`
2. Experimental: `eval-run` with agentic-docs plugin → MLflow run tagged `with-agentic-docs`
3. Compare in MLflow run comparison view

**Critical gap** `[GAP]`: No baseline run exists yet. All existing runs are "with plugin" — there is no control group. Also: with only 5–6 test cases, the cost variance is 7x ($0.95–$6.77). Add more cases or run 3+ passes and average before drawing conclusions.

---

## Gaps and Open Issues

| Issue | Status |
|-------|--------|
| No OTEL in eval runs | `[GAP]` Requires adding collector lifecycle to `claude_code.py` (Open Question #1) |
| No baseline eval run | `[GAP]` No control group for before/after comparison |
| Case-006 incomplete | `[GAP]` Has `input.yaml` but no `annotations.yaml` — no judges defined |
| MLflow not connected | `[GAP]` `eval.yaml` declares experiment but no evidence of a running MLflow server |
| PR #77 not merged | `[GAP]` Prompt mode + `documentation_tracking` depend on this open PR |
| `OTEL_LOG_TOOL_CONTENT` | `[REC]` Default off; 10-50MB JSONL risk per complex case |
| Identity fields in OTel | `[ACCEPTED]` session_id, terminal_reason remain stream-json only — upstream API change needed |
| `collection.json` unused | `[FACT]` Empty object in current runs — custom data collection point not utilized |
