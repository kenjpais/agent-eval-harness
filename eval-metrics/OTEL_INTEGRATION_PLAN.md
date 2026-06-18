# OTel Integration Plan for agent-eval-harness

## Context

The OTEL_SUPPORT.md at `agentic-docs/OTEL_SUPPORT.md` defines five tasks for adopting the telemetry
architecture from a set of related PRs (opendatahub-io/agentic-ci#84, openshift-eng/ai-helpers#536,
openshift/release#80547) into the agent-eval-harness.

The current state: `eval.yaml` declares `traces: {stdout, stderr, events, metrics}` but the eval runner
never starts an OTel collector — telemetry data is not collected. `ANSWERS.md` already contains
fact-verified research on all three PRs. The existing eval runs have no visibility into *why* case-002
costs $6.77 vs case-003's $0.95.

This plan proposes an architecture that makes OTel the primary source of observability for evaluation
— without creating competing telemetry pipelines.

> **Note**: This plan was updated after discovering that PR #90 (`opendatahub-io/agent-eval-harness#90`,
> DRAFT, `astefanutti`) already implements in-process OTel trace capture. The plan adopts PR #90's
> approach as the foundation rather than the subprocess-based approach from PR #536.

---

## Task 1: PR #80547 Architecture Summary

**Repo**: `openshift/release`
**File**: `ci-operator/step-registry/openshift/claude/payload/agent/openshift-claude-payload-agent-commands.sh`
**Role**: Prow CI step — wires the prow-agent plugin (#536) into the OpenShift payload triage workflow.

### What it does

1. Installs `prow-agent@ai-helpers` plugin into the Prow container
2. Starts `otel_collector.py` in the background before running Claude
3. Uses a **port file** pattern (`--port-file /tmp/port`) — collector binds to OS-assigned port 0,
   writes the actual port to the file; shell polls until non-empty
4. Exports OTel environment variables pointing to the discovered endpoint
5. Runs `claude -p "..." --output-format stream-json | tee "$ARTIFACT_DIR/claude-output.log"`
6. Kills the collector and runs `extract_metrics.py claude-otel.jsonl metrics-autodl.json`
7. Saves `claude-otel.jsonl` to `$ARTIFACT_DIR` (GCS-backed Prow artifact)
8. Uploads BigQuery autodl JSON for `claude_session_metrics` table
9. Outputs JUnit XML so collection failures appear on the Prow test grid

### Lifecycle (shell pseudocode)

```bash
python3 otel_collector.py --port-file /tmp/port --log-file $ARTIFACT_DIR/claude-otel.jsonl &
COLLECTOR_PID=$!
while [ ! -s /tmp/port ]; do sleep 0.1; done
export OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:$(cat /tmp/port)"
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_TRACES_EXPORTER=otlp
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
# ... 8 more vars
claude -p "..." --output-format stream-json | tee "$ARTIFACT_DIR/claude-output.log"
kill $COLLECTOR_PID; wait $COLLECTOR_PID
python3 extract_metrics.py claude-otel.jsonl metrics-autodl.json --stream-log claude-output.log
```

### How telemetry flows

- Claude Code pushes OTLP payloads (HTTP) to the local collector
- Collector writes each request as a JSONL line to `claude-otel.jsonl`
- `extract_metrics.py` reads JSONL → produces BigQuery-loadable rows
- `agentic-ci mlflow-push` (PR #84) reads JSONL → POSTs `/v1/traces` to MLflow

### Rationale

- **Ephemeral local collector**: no external dependencies, no network egress during run, zero collision
  risk with hardcoded ports
- **Two-phase extraction**: raw JSONL preserved as artifact (debuggable), metrics derived post-run
- **Dual-file identity enrichment**: OTel lacks session_id/model; stream-json `--stream-log` fills the gap
- **JUnit for visibility**: makes collection failures first-class CI failures on the test grid

---

## Task 2: Reusable Patterns — PR #90 vs PR #536

Two implementations exist. This plan adopts **PR #90's in-process approach** for the eval harness.

### PR #90 (`opendatahub-io/agent-eval-harness#90`, DRAFT, `astefanutti`)

**Approach**: In-process Python threading HTTP server (`OTLPReceiver`)

```python
class OTLPReceiver:
    def start(self) -> int:
        self._server = HTTPServer(("127.0.0.1", 0), _OTLPHandler)
        # Port read directly: self._server.server_address[1]
        # Only accepts POST /v1/traces

    def stop(self, flush_timeout_s=5.0) -> Path:
        # Writes {"resourceSpans": [...]} to otel_spans.json
```

Already provides:
- `OTLPReceiver` lifecycle wired into `ClaudeCodeRunner.run_skill()` (per-case start/stop)
- `OTelConfig` dataclass → `runner.otel:` in eval.yaml schema
- `ClaudeCodeSpanMapper`: tokens, turns, tool calls extracted from spans
- `collect.py` OTel-first path: prefers `otel_spans.json`, falls back to stream-json
- 8 of 11 OTel env vars injected into Claude subprocess

**Gap**: Only handles `POST /v1/traces`. Returns 404 for `/v1/metrics` and `/v1/logs`.

### PR #536 (`openshift-eng/ai-helpers#536`)

**Approach**: External subprocess (`otel_collector.py`)

- All three OTLP signals: `/v1/traces` + `/v1/metrics` + `/v1/logs`
- Port file pattern (shell polling)
- Output: `claude-otel.jsonl` (one line per OTLP request, mixed signals)
- Requires `claude plugin install prow-agent@ai-helpers`

### Design Decision: PR #90's in-process approach

| Concern | PR #536 (subprocess) | PR #90 (in-process) |
|---|---|---|
| Dependencies | `claude plugin install prow-agent@ai-helpers` | Zero — stdlib only |
| Port discovery | Port file + shell polling | Direct from `server_address[1]` |
| Integration | External process, shell lifecycle | Wired into Python runner |
| Events → judges | `extract_metrics.py` + custom parsing | `SpanMapper` (already in repo) |
| OTel signals | All three (traces + metrics + logs) | `/v1/traces` only |
| Output format | JSONL (one line per push) | Single JSON file |

**Decision**: Use PR #90's approach. The eval harness is a Python-managed runner that owns the
subprocess lifecycle — in-process is the right fit. The subprocess approach (PR #536 / PR #80547) is
designed for CI sidecars where Claude is invoked from shell.

**Tradeoff accepted**: Traces-only capture. Token counts are in `claude_code.llm_request` span
attributes and are extracted correctly by `ClaudeCodeSpanMapper`. The only data not in `/v1/traces`
is per-request latency histograms (in `/v1/metrics`) — a minor gap for our eval use case.

---

## Task 3: Single Source of Truth Architecture

### Principle

OTel is the primary source for per-request observability. Stream-json is retained only for two
specific fields that OTel does not expose.

### Data flow

```
Claude Code (per case)
    │
    │  OTLP/HTTP push (traces only)
    ▼
OTLPReceiver (in-process, ephemeral port)           [PR #90]
    │
    │  writes accumulated ResourceSpans
    ▼
eval/runs/{run-id}/cases/{case-id}/otel_spans.json
    │
    ├──► ClaudeCodeSpanMapper                        [PR #90]
    │       → events.json  (judge inputs: turns, tool calls, Read calls)
    │
    └──► POST /v1/traces to MLflow                  [new addition]
             │
             └──► MLflow experiment: agentic-docs-component-eval
                  (native OTel spans — real timing, real hierarchy)

stream-json stdout.log (retained)
    │
    └──► extract_usage()
             → cost_usd, session_id, terminal_reason   [stream-json only]
```

### What OTel is authoritative for

| Metric | OTel source | Signal |
|--------|-------------|--------|
| Token counts (input, output, cache) | `claude_code.llm_request` span attrs | `/v1/traces` |
| Agent turns | Count of `llm_request` spans with output > 0 | `/v1/traces` |
| Tool calls + breakdown | `claude_code.tool` child spans | `/v1/traces` |
| Documentation access | `claude_code.tool` spans where `tool_name=Read` | `/v1/traces` |
| Trace hierarchy | Parent/child span relationships | `/v1/traces` |
| Per-turn timing | Span start/end timestamps | `/v1/traces` |

### What stream-json retains (accepted limitations)

| Field | Why OTel cannot provide it |
|-------|---------------------------|
| `cost_usd` | Claude Code does not emit cost as a span attribute. Deriving it requires a model pricing table that would need maintenance. Accepted: keep from stream-json `result` event. |
| `session_id` | Application-level concept, not an OTel resource attribute |
| `terminal_reason`, `stop_reason` | Only in stream-json `result` event |

**Tradeoff**: OTel is not the *single* source of truth for all metrics — cost_usd still comes from
stream-json. This is an accepted limitation. Everything judges need (events.json, read_calls.json)
comes from OTel spans.

### Avoiding competing pipelines

- `trace_builder.py` (874-line stream-json reconstruction) is kept as fallback only — used when
  `otel_spans.json` is absent (pre-OTel runs, OTel disabled). Not removed.
- No `agentic-ci mlflow-push` dependency — direct POST to MLflow's verified OTLP endpoint.
- No LangFuse.

---

## Task 4: OTel-Based Evaluation Model

### Trace hierarchy (what Claude Code emits)

```
claude_code.interaction  (root span — one per session)
    ├── claude_code.llm_request  (one per API call)
    │     attrs: model, input_tokens, output_tokens, cache_read_tokens,
    │            cache_creation_tokens, gen_ai.response.id
    ├── claude_code.tool  (one per tool invocation)
    │     attrs: tool_name, tool_input (with OTEL_LOG_TOOL_DETAILS=1)
    └── claude_code.hook  (one per hook execution)
```

### How each metric maps

| Evaluation Dimension | OTel Representation |
|---------------------|---------------------|
| Agent turns | Count of `llm_request` spans where `output_tokens > 0` |
| Tool calls | Count of `claude_code.tool` spans |
| Tool usage patterns | Frequency histogram of `tool_name` attribute |
| Token usage | Sum of token attrs across all `llm_request` spans |
| Per-turn latency | Span duration (start/end timestamps) |
| Documentation access | `claude_code.tool` spans where `tool_name=Read` and `tool_input.file_path` is in `ai-docs/` |
| Trace hierarchy | Parent-child span relationships in `otel_spans.json` |
| Task success/failure | Root span status (OK/ERROR) — requires `terminal_reason` from stream-json |

### What is explicitly out of scope

- **Judge result injection** (synthetic spans): MLflow's Assessment API already attaches judge
  verdicts to runs as assessments. Synthetic spans add complexity for marginal UI benefit. Skip.
- **`collection.json` population**: Not consumed by any judge or report. Skip.
- **`/v1/metrics` and `/v1/logs`**: Token/turn data already in `/v1/traces` spans. Skip.

---

## Task 5: Implementation Plan

### Verified facts

- **PR #90 head**: `fef960d` — confirmed current (GitHub and local `origin/feat/otel-traces-opencode-runner` match)
- **PR #77 head**: `3bfe1f2` — local `pr-77` branch has a kenjpais-specific extra commit (`b5d5198`)
  and is NOT the upstream PR head. Use `pr-77-upstream` (fetched as `refs/pull/77/head`).
- **Common ancestor**: `a2e7f05` (both PRs diverge from this commit on `origin/main`)
- **MLflow OTLP endpoint**: Accepts `application/json` with `{"resourceSpans": [...]}` structure.
  Requires `x-mlflow-experiment-id` header. Source-verified at
  `/Users/kpais/.pyenv/versions/3.13.3/lib/python3.13/site-packages/mlflow/server/otel_api.py`.
- **`otel_spans.json` format**: Written by `OTLPReceiver.stop()` as `{"resourceSpans": [...]}`.
  This IS a valid OTLP `ExportTraceServiceRequest` JSON body. No transformation needed — MLflow
  handles hex→base64 ID conversion internally.

### Merge conflicts

Both PR #77 and PR #90 modify these 8 files — merge conflicts are expected:

```
agent_eval/agent/base.py
agent_eval/agent/claude_code.py       ← most significant conflict
agent_eval/agent/cli_runner.py
agent_eval/agent/responses_api.py
agent_eval/config.py
skills/eval-run/scripts/collect.py
skills/eval-run/scripts/execute.py
skills/eval-run/scripts/workspace.py
```

The critical conflicts involve both the abstract interface (`base.py`) and its implementation
(`claude_code.py`). PR #77 renamed the ABC method from `run_skill(self, skill_name, ...)` to
`execute(self, target, ...)`. PR #90 wired the `OTLPReceiver` lifecycle into `run_skill()` and
added `output_dir` to it. Resolution: keep PR #77's `execute()` interface, add `output_dir` to it,
and split its body into `execute()` (receiver lifecycle) + `_execute_inner()` (Claude subprocess
logic), mirroring PR #90's `run_skill()` / `_run_skill_inner()` split.

---

### Prerequisite: Branch Merge

Start from the actual upstream PR #77 head (not the local `pr-77` branch):

```bash
cd /Users/kpais/kpais-workspace/claude-tmp/agent-eval-harness

git fetch origin
git fetch origin refs/pull/77/head:pr-77-upstream   # if not already fetched

git checkout -b feat/otel-on-pr77 pr-77-upstream     # 3bfe1f2
git merge origin/feat/otel-traces-opencode-runner    # resolve conflicts
```

**Conflict resolutions:**

`agent_eval/agent/claude_code.py` — PR #77 has a single `execute()` method (no inner split).
PR #90 has `run_skill()` + `_run_skill_inner()`. Resolution: add `output_dir`, split method body:
```python
def execute(self, target, args, workspace, model, ..., output_dir=None):
    receiver = None
    if self._otel_config and self._otel_config.enabled:
        from agent_eval.otel.receiver import OTLPReceiver
        receiver = OTLPReceiver(output_dir=output_dir or workspace)
        self._otel_port = receiver.start()
    else:
        self._otel_port = None
    try:
        return self._execute_inner(target, args, workspace, model, ...)
    finally:
        if receiver:
            receiver.stop(flush_timeout_s=5)
            self._otel_port = None

def _execute_inner(self, target, args, workspace, model, ...):
    # PR #77's original execute() body moved here unchanged
    ...
```

`agent_eval/agent/base.py` — ABC interface conflict: PR #90 has `@abstractmethod run_skill()`
with `output_dir`; PR #77 has `@abstractmethod execute()` without it, plus a concrete `run_skill()`
deprecation shim. Resolution: keep PR #77's `execute()` as abstract, add
`output_dir: Optional[Path] = None` to it; update the `run_skill()` shim to forward `output_dir`.

`agent_eval/config.py` — additive: all fields from both PRs coexist (`OTelConfig` from PR #90;
`prompt`, `workspace_mode`, `TestCategory` from PR #77).

`skills/eval-run/scripts/execute.py` — PR #90 calls `runner.run_skill(...)` at lines 201 and 359;
PR #77 calls `runner.execute(...)`. Resolution: use `runner.execute(...)` throughout (PR #77's
interface). The `output_dir` param needs to be threaded through from the call sites.

`skills/eval-run/scripts/collect.py`, `workspace.py` — PR #77's changes are minor
(comments, prompt-mode routing). PR #90's OTel additions are substantive. Take PR #90's version,
re-apply PR #77's changes on top.

`agent_eval/agent/cli_runner.py`, `responses_api.py` — PR #77 renamed `run_skill` → `execute`;
PR #90 made other changes. Apply rename, keep PR #90's other changes.

---

### Step 1: Enable OTel in `agentic-docs/eval.yaml`

Add to `agentic-docs/eval.yaml`:

```yaml
runner:
  type: claude-code
  otel:
    enabled: true
    content: false     # OTEL_LOG_TOOL_CONTENT=0 — prevents 10-50MB logs per complex case
    api_bodies: true   # Required for ClaudeCodeSpanMapper to capture assistant text
```

**What this activates with zero additional code** (all provided by PR #90):
- `OTLPReceiver` starts and stops per case in `ClaudeCodeRunner.execute()`
- Claude subprocess receives 8 OTel env vars (traces exporter, endpoint, tool details, user prompts)
- `otel_spans.json` written to each case output directory
- `events.json` built from spans via `ClaudeCodeSpanMapper` (not stream-json)
- `read_calls.json` populated from `claude_code.tool` spans where `tool_name=Read`

**Tradeoff**: `content: false` means `OTEL_LOG_TOOL_CONTENT=0`. Tool outputs are not captured in
spans. This avoids 10-50MB JSONL files for cases like case-001 (38 turns, $2.53). Enable only for
targeted debugging runs.

---

### Step 2: Add MLflow OTLP Push in `skills/eval-mlflow/scripts/log_results.py`

Replace the per-case `build_trace` + `log_trace` call with a direct POST of `otel_spans.json`.
Keep `trace_builder.py` as fallback for pre-OTel runs.

```python
def _push_otel_spans(otel_path, tracking_uri, experiment_id, run_id=None):
    """POST otel_spans.json to MLflow's OTLP endpoint.

    MLflow 3.x accepts OTLP/HTTP JSON at /v1/traces.
    The otel_spans.json format {"resourceSpans": [...]} is a valid
    ExportTraceServiceRequest body. MLflow handles hex->base64 ID
    conversion internally. Two headers required; no data transformation.
    """
    import requests
    headers = {
        "Content-Type": "application/json",
        "x-mlflow-experiment-id": experiment_id,
    }
    if run_id:
        headers["x-mlflow-run-id"] = run_id
    resp = requests.post(
        f"{tracking_uri.rstrip('/')}/v1/traces",
        json=json.loads(otel_path.read_text()),
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
```

In the per-case loop, replace:
```python
# OLD: stream-json reconstruction (874-line trace_builder.py)
trace_dict = build_trace(stdout_path, run_result, run_id, experiment_id, ...)
if trace_dict:
    tid = log_trace(trace_dict)
```

With:
```python
# NEW: OTel-native push
otel_path = case_dir / "otel_spans.json"
if otel_path.exists():
    _push_otel_spans(otel_path, tracking_uri, experiment_id, run_id=mlflow_run_id)
else:
    # Fallback: stream-json reconstruction (pre-OTel runs or otel.enabled: false)
    trace_dict = build_trace(stdout_path, run_result, run_id, experiment_id, ...)
    if trace_dict:
        log_trace(trace_dict)
```

**What MLflow receives** with the OTel path vs the old path:

| | `trace_builder.py` (old) | Direct OTLP push (new) |
|---|---|---|
| Span timing | Fabricated (wall-clock approximations) | Accurate (nanosecond from Claude) |
| Span hierarchy | Reconstructed heuristically from event order | Real parent/child IDs from Claude |
| Token breakdown | Session total only | Per-turn (per `llm_request` span) |
| Tool call details | From stream-json tool_use blocks | From `claude_code.tool` spans |
| `service.name` | Not set | `"claude-code"` (on MLflow allowlist) |

---

### Step 3: Upstream Contribution

```bash
git push fork feat/otel-on-pr77

gh pr create \
  --repo opendatahub-io/agent-eval-harness \
  --base main \
  --title "feat: OTel trace capture on top of prompt-mode evaluation" \
  --body "$(cat <<'EOF'
## Summary

Combines PR #77 (prompt-mode evaluation, @Prashanth684) and PR #90 (OTel trace
capture, @astefanutti) and adds two net-new changes needed for the agentic-docs
evaluation workflow:

1. `runner.otel:` configuration in `eval.yaml` schema — enables OTel per eval
2. Direct OTLP push to MLflow from `otel_spans.json` — replaces `trace_builder.py`
   stream-json reconstruction with native OTel spans

### What's included from PR #77
All prompt-mode changes: `execution.prompt`, in-repo execution mode, `eval_name()`,
Jinja2 template support, permission filtering fix.

### What's included from PR #90
`OTLPReceiver`, `ClaudeCodeSpanMapper`, `OpenCodeRunner`, `OTelConfig`,
`collect.py` OTel-first events path, `workspace.py` runner-aware setup.

### Net-new
- `runner.otel.enabled: true` in `eval.yaml` schema documentation
- `_push_otel_spans()` in `eval-mlflow/scripts/log_results.py` with
  `trace_builder.py` fallback

### Merge conflict resolution
The `run_skill()` → `execute()` rename from PR #77 conflicted with PR #90's
OTLPReceiver wiring. Resolved by transplanting the receiver lifecycle into the
renamed `execute()` method.

Closes #77, Closes #90
EOF
)"
```

**If PR #77 merges before this PR is opened**: rebase onto the new `main` instead of
`pr-77-upstream`. Conflicts reduce significantly — only PR #90's changes remain.

```bash
git fetch origin
git rebase origin/main   # conflicts are PR #90 only at that point
```

---

## Implementation Order

| Priority | Work | Effort | Depends on |
|----------|------|--------|------------|
| P0 | Merge PR #77 + PR #90 locally, resolve 8-file conflict | 1 day | — |
| P1 | Add `runner.otel: {enabled: true}` to `agentic-docs/eval.yaml` | 30 min | P0 |
| P2 | Add `_push_otel_spans()` to `log_results.py` | 2 hours | P0 |
| P3 | Run one eval case, verify `otel_spans.json` produced | 1 hour | P1 |
| P4 | Run `eval-mlflow` skill, verify native spans in MLflow | 30 min | P2, P3 |
| P5 | Push to fork, open upstream PR | 30 min | P3, P4 |

---

## Design Decisions and Tradeoffs Summary

| Decision | Chosen approach | Alternative | Why |
|----------|----------------|-------------|-----|
| Collector type | In-process `OTLPReceiver` (PR #90) | Subprocess `otel_collector.py` (PR #536) | No external deps; port discovery is immediate; already integrated in Python runner lifecycle |
| OTel signals | `/v1/traces` only | All three signals | Token/turn data already in traces; tool details in spans with `OTEL_LOG_TOOL_DETAILS=1`; `/v1/metrics` adds complexity without new judge-relevant data |
| cost_usd source | Stream-json `result` event | Derive from tokens × model pricing table | Pricing table requires maintenance; stream-json cost is authoritative and exact |
| MLflow push | Direct POST to `/v1/traces` | `agentic-ci mlflow-push` CLI | Zero extra dependencies; format verified against MLflow 3.14.0 source; two headers required |
| trace_builder.py | Keep as fallback | Remove | Backward compatibility for pre-OTel runs and `otel.enabled: false` configs |
| Judge result injection | Skip | Synthetic spans appended to trace | MLflow Assessment API already handles this; synthetic spans add 100+ lines for marginal UI benefit |
| collection.json | Skip | Populate from span data | Not consumed by any judge or report in current pipeline |
| content: false | Default off | Default on | Case-001 has 38 turns; tool content can produce 10-50MB per case |

---

## Verification Checklist

1. **Smoke test**: Run one case with `otel.enabled: true` → confirm `otel_spans.json` exists in
   `eval/runs/{run-id}/cases/{case-id}/` with non-empty `resourceSpans`
2. **Events check**: Confirm `events.json` is generated from OTel spans (not stream-json) —
   look for `source: otel` or absence of stream-json fallback log line
3. **Token parity**: Compare token counts in `run_result.json` (stream-json) vs
   `ClaudeCodeSpanMapper.extract_usage()` from spans — should match within rounding
4. **MLflow traces**: After `eval-mlflow` run, confirm traces appear in experiment
   `agentic-docs-component-eval` with real span hierarchy (not flat `trace_builder.py` structure)
5. **Read calls**: Confirm `read_calls.json` is populated with `ai-docs/` file accesses from
   `claude_code.tool` spans
6. **Fallback**: Run with `otel.enabled: false` → confirm `trace_builder.py` path still works
