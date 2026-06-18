# Plan: OTel as Single Source of Truth for agent-eval-harness

## Context

The agentic-docs eval harness currently collects metrics by parsing Claude's
`--output-format stream-json` output post-hoc. This produces session-level
aggregates (total cost, total tokens, turn count) but no per-request detail:
which tool calls were slow, what cache hit rates per turn were, or which
documentation files were accessed and when.

PR #536 (ai-helpers) shipped `otel_collector.py` — a zero-dependency Python
OTLP server that receives structured telemetry directly from Claude Code in
real time, per API request. PR #84 (agentic-ci) showed the complete env-var
set needed to enable full trace export and added `agentic-ci mlflow-push` to
forward those traces to MLflow.

The goal is to adopt this architecture inside agent-eval-harness so that:
- OTel is the **single source of truth** for all metrics, traces, and events
- Metrics are derived from OTel data, not separate stream-json parsing
- Documentation access (Read calls) comes from OTel logs, not events.json
- The same JSONL file feeds MLflow traces, judge inputs, and BigQuery (if used)
- Stream-json is retained only for identity fields (session_id, model version,
  prompt text) that OTel does not expose

This plan targets changes to be contributed to agent-eval-harness PR #77 (not
yet merged at `opendatahub-io/agent-eval-harness`). The installed package is at
`/Users/kpais/.claude/plugins/marketplaces/kenjpais-skills/agent_eval/` for
reference during implementation.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│  agent-eval-harness: per-case execution                      │
│                                                              │
│  1. start otel_collector.py  ←── from ai-helpers #536        │
│  2. set OTEL env vars        ←── from agentic-ci #84         │
│  3. subprocess.Popen(claude) ←── existing claude_code.py:250 │
│       │                                                      │
│       │  OTLP/JSON push every 10s                            │
│       ▼                                                      │
│  otel_collector → claude-otel.jsonl  (per case artifact)    │
│  tee stdout     → stdout.log         (existing)              │
│  4. stop collector                                           │
│  5. parse otel.jsonl → RunResult metrics (replaces stream)   │
│  6. parse stdout.log → identity fields only                  │
│  7. mlflow-push otel.jsonl → MLflow traces                   │
│                                                              │
└─────────────────────────────────────────────────────────────┘
         ↓                          ↓
    judges receive             MLflow receives
    otel-derived metrics       native OTel spans
    + identity from stdout     (replacing post-hoc trace builder)
```

---

## Components to Reuse (no modification needed)

| Component | Source | Reused as-is |
|-----------|--------|-------------|
| `otel_collector.py` | ai-helpers #536, `plugins/prow-agent/scripts/` | Start/stop around each case |
| `extract_metrics.py` | ai-helpers #536, `plugins/prow-agent/scripts/` | Parse otel.jsonl → metrics dict |
| `agentic-ci mlflow-push` | agentic-ci #84, `agentic-ci mlflow-push` CLI | Post-case trace upload |
| OTEL env vars | agentic-ci #84, `harness.py::build_env_script_lines()` | Set in subprocess env |

The collector is located after `claude plugin install prow-agent@ai-helpers` at:
`~/.claude/plugins/<hash>/prow-agent/scripts/otel_collector.py`

For eval harness usage, find it with:
```python
import subprocess, shutil
collector = shutil.which("otel_collector.py") or \
    subprocess.check_output(
        ["find", os.path.expanduser("~/.claude/plugins"),
         "-name", "otel_collector.py", "-print", "-quit"],
        text=True
    ).strip()
```

---

## Changes to agent-eval-harness

### 1. `agent/claude_code.py` — Collector lifecycle

**Hook point**: around `subprocess.Popen()` at line 250.

Add a `_OtelCollector` context manager:

```python
class _OtelCollector:
    """Start otel_collector.py before Claude, stop it after."""
    def __init__(self, log_file: Path):
        self.log_file = log_file
        self._proc = None
        self._port = None

    def __enter__(self):
        port_file = tempfile.mktemp()
        self._proc = subprocess.Popen(
            ["python3", OTEL_COLLECTOR_PATH,
             "--port-file", port_file,
             "--log-file", str(self.log_file)],
            stderr=subprocess.DEVNULL,
        )
        # Wait for port (max 5s)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if os.path.getsize(port_file) > 0:
                break
            time.sleep(0.05)
        self._port = open(port_file).read().strip()
        return self._port

    def __exit__(self, *_):
        if self._proc:
            self._proc.terminate()
            self._proc.wait(timeout=10)
```

Wrap the existing `subprocess.Popen` block:

```python
otel_log = workspace / "claude-otel.jsonl"
with _OtelCollector(otel_log) as otel_port:
    otel_env = _otel_env(otel_port)   # see env vars below
    proc = subprocess.Popen(cmd, env={**base_env, **otel_env}, ...)
    # ... existing stdout drain logic unchanged ...
```

Add OTEL env vars function:

```python
def _otel_env(port: str, include_content: bool = False) -> dict:
    return {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{port}",
        "OTEL_METRIC_EXPORT_INTERVAL": "10000",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
        "OTEL_LOG_USER_PROMPTS": "1",
        "OTEL_LOG_TOOL_DETAILS": "1",
        "OTEL_LOG_TOOL_CONTENT": "1" if include_content else "0",
    }
```

Also add these keys to the env allowlist (lines 449-461 in claude_code.py):
`CLAUDE_CODE_ENABLE_TELEMETRY`, `OTEL_*`, `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA`

### 2. `agent/stream_capture.py` — Replace metric extraction with OTel

Current: `extract_usage(stdout_lines)` parses stream-json for token/cost totals.

Replace with: `extract_usage_from_otel(otel_jsonl_path)` using the same logic
as `extract_metrics.py` from #536 (already has full test coverage).

Keep stream-json parsing **only** for identity fields not in OTel:
- `session_id` (from `{"type":"system","subtype":"init"}.session_id`)
- `claude_code_version` (from init event)
- `plugins_loaded` (from init event)
- `terminal_reason`, `stop_reason` (from `{"type":"result"}`)

The `RunResult` dataclass gains one new field:
```python
otel_log: Optional[Path] = None   # path to claude-otel.jsonl for this case
```

### 3. `events.py` — Documentation tracking from OTel

Current: `extract_read_calls()` parses stream-json for `Read` tool invocations.

New: Extract from OTel `/v1/logs` records where `event.name = claude_code.api_request`
and `tool_name = Read`. With `OTEL_LOG_TOOL_DETAILS=1`, `tool_input` contains
`file_path`. This replaces the stream-json path when `otel_log` is present.

```python
def extract_read_calls_from_otel(otel_jsonl_path: Path) -> list[dict]:
    """Extract Read tool calls from OTel JSONL log records."""
    calls = []
    for line in open(otel_jsonl_path):
        rec = json.loads(line)
        if "/v1/logs" not in rec.get("path", ""):
            continue
        for rl in rec["payload"].get("resourceLogs", []):
            for sl in rl.get("scopeLogs", []):
                for lr in sl.get("logRecords", []):
                    attrs = {a["key"]: _attr_val(a["value"])
                             for a in lr.get("attributes", [])}
                    if (attrs.get("event.name") == "claude_code.api_request"
                            and attrs.get("tool_name") == "Read"):
                        calls.append({
                            "file_path": attrs.get("tool_input", {}).get("file_path"),
                            "duration_ms": attrs.get("duration_ms"),
                            "model": attrs.get("model"),
                            "timestamp": rec.get("ts"),
                        })
    return calls
```

### 4. `mlflow/` — Replace post-hoc trace builder with OTel-native traces

Current: `trace_builder.py` reconstructs spans from stream-json events post-hoc.
This is the competing telemetry pipeline the spec wants to eliminate.

New: After collecting `claude-otel.jsonl`, call `agentic-ci mlflow-push` (from
#84) to upload native OTel traces. These are richer (actual timing per span, not
reconstructed) and structurally correct.

The existing `trace_builder.py` / `log_trace()` should be **deprecated** and
replaced by the mlflow-push path. Keep as fallback if prow-agent plugin is not
installed.

```python
def push_otel_to_mlflow(otel_log: Path, experiment: str, endpoint: str, token: str):
    subprocess.run([
        "agentic-ci", "mlflow-push", str(otel_log),
        "--endpoint", endpoint,
        "--experiment", experiment,
        "--token", token,
    ], check=True)
```

### 5. `eval.yaml` — New `otel` configuration block

Add to the agentic-docs eval.yaml (and document in PR #77's schema):

```yaml
otel:
  enabled: true
  tool_content: false      # OTEL_LOG_TOOL_CONTENT (large data, off by default)
  artifact: claude-otel.jsonl   # filename saved per case in case output dir
  mlflow_push: true        # run agentic-ci mlflow-push after each case
```

### 6. `execute.py` / `collect.py` — Save OTEL artifact per case

Save `claude-otel.jsonl` as a case artifact alongside `stdout.log`, `stderr.log`,
`run_result.json` in the case output directory.

---

## OTel as Single Source of Truth — Metric Mapping

| Metric | OTel Source | OTEL Signal |
|--------|-------------|-------------|
| Total cost | `claude_code.cost.usage` sum | `/v1/metrics` |
| Cost per model | `claude_code.cost.usage` with `model` attribute | `/v1/metrics` |
| Input/output tokens | `claude_code.token.usage` with `type` attribute | `/v1/metrics` |
| Cache hit rate | `cacheRead / (input + cacheRead + cacheCreation)` | `/v1/metrics` |
| API latency | `claude_code.active_time.total` with `type=api` | `/v1/metrics` |
| Tool execution time | `claude_code.active_time.total` with `type=tool_execution` | `/v1/metrics` |
| Tool call count | Count of `claude_code.api_request` log records with `tool_name` | `/v1/logs` |
| Tool usage breakdown | Group api_request records by `tool_name` | `/v1/logs` |
| Per-request latency | `duration_ms` attribute on each api_request log | `/v1/logs` |
| Documentation access | api_request records where `tool_name=Read`, `tool_input.file_path` | `/v1/logs` |
| Trace hierarchy | Parent/child span relationships | `/v1/traces` |
| Agent decision points | Span events within trace spans | `/v1/traces` |
| Error recovery | Span status=ERROR + subsequent OK spans | `/v1/traces` |
| Num turns | Count distinct api_request records at root level | `/v1/logs` |
| Session identity | stream-json init message only (not in OTel) | stream-json |
| Terminal reason | stream-json result message only | stream-json |

---

## Implementation Steps

**Step 1** — Add `_OtelCollector` context manager to `claude_code.py`.
Wrap the `subprocess.Popen` block. Save `claude-otel.jsonl` to case output dir.
Add OTEL env vars to the allowlist.

**Step 2** — Add `extract_usage_from_otel()` to `stream_capture.py`.
Port the parsing logic from `extract_metrics.py` (#536). Keep `extract_usage()`
as fallback when OTel is disabled. Update `RunResult` to include `otel_log`.

**Step 3** — Add `extract_read_calls_from_otel()` to `events.py`.
Used by `collect.py` when `documentation_tracking: true` and OTel is enabled.
Falls back to stream-json path when `otel_log` is absent.

**Step 4** — Add `mlflow_push_otel()` to `mlflow/` module.
Called after each case when `otel.mlflow_push: true` in eval.yaml.
Deprecate `trace_builder.py` with a warning when OTel is available.

**Step 5** — Add `otel:` section to eval.yaml schema in PR #77.
Document in the eval.yaml schema description. Update the agentic-docs `eval.yaml`
to enable OTel.

**Step 6** — Update `execute.py` / `collect.py` to save `claude-otel.jsonl`
as a case artifact alongside `stdout.log`, `stderr.log`, `run_result.json`.

---

## Verification

1. **Unit**: Run `python3 plugins/prow-agent/scripts/test_extract_metrics.py`
   (the existing 5 tests from #536 verify OTel parsing logic).

2. **Integration**: Run one eval case with OTel enabled:
   ```bash
   eval-run --model sonnet --cases case-001-simple-operator
   ls eval/runs/<latest>/cases/case-001-simple-operator/
   # Expect: claude-otel.jsonl present
   # Expect: run_result.json cost_usd matches otel-derived value
   ```

3. **Documentation tracking**: With `documentation_tracking: true`, verify
   `read_calls.json` is populated from OTel (not stream-json) and contains
   `duration_ms` and `model` fields (new fields, only available from OTel).

4. **MLflow**: After `eval-run`, check MLflow experiment
   `agentic-docs-component-eval` for native OTel traces with span hierarchy
   (not reconstructed from stream-json events).

5. **Metric parity**: Compare `cost_usd` in `run_result.json` (OTel-derived)
   vs. existing stream-json-derived value for the same run. They should match
   within float rounding.

---

## Gaps / Out of Scope

- **Identity fields**: `session_id`, `claude_code_version`, `plugins_loaded`,
  `terminal_reason` remain stream-json only. Open Question #3 in
  metrics-collection.md. Accepted limitation.

- **`OTEL_LOG_TOOL_CONTENT`**: Default off. Enabling it adds tool output text
  to OTel logs — useful for verifying what `Read` returned, but can produce
  10-50MB JSONL for complex cases. Opt-in via `otel.tool_content: true`.

- **Collector discovery**: Assumes `prow-agent@ai-helpers` plugin is installed.
  If not found, eval harness falls back to stream-json path with a warning.
  Does not fail hard.

- **BigQuery / autodl**: Not relevant for local eval runs. The `extract_metrics.py`
  autodl output is Prow-specific (#80547). Not added to the eval harness.
