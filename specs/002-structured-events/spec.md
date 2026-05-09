# Feature Specification: Structured Event Stream for Judges

**Feature Branch**: `002-structured-events`
**Created**: 2026-05-06
**Status**: Draft
**Input**: Brainstorm document `brainstorm/01-structured-events.md`

## Clarifications

### Session 2026-05-06

- Q: What should the new template variable be named? → A: `{{ conversation }}` (renders assistant text from events, distinct from the full `outputs["events"]` list)
- Q: What is the default size cap for tool results in events? → A: 50K characters default, configurable via `traces.event_result_cap` in eval.yaml

### Session 2026-05-08

- Q: How should subagent events be structured? → A: Flat list with tags. All events (root + subagent) in one ordered list, subagent events tagged with `parent_tool_use_id` and `agent_id`. Judges filter to root-only with `if not e.get("parent_tool_use_id")`. Matches Claude Code's native streaming format (>= 2.1.108). Alternatives rejected: nesting (forces recursion for simple queries), separate lists by agent (loses ordering).

### Session 2026-05-09

- Q: How should structured events work in batch execution mode? → A: Batch mode produces no `events.json`. Structured events are only supported in case mode where each case has its own `stdout.log`. Splitting a single batch stream into per-case events would require heuristics that don't exist yet. Documented as a known limitation.
- Q: Should tool input content be capped like tool result content? → A: Yes. String values in tool inputs are capped at the same `event_result_cap` threshold as tool results, with the same truncation metadata (`truncated: true`, `original_length: N`). Keeps events.json size bounded for skills with many large Write calls.
- Q: What should `traces.events: false` produce in the case record? → A: Always `[]` (empty list, never a missing key). Judges can iterate `outputs["events"]` without checking for key existence.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Evaluate skill output through structured events (Priority: P1)

A judge author configures an LLM judge or check judge to evaluate a skill's output. Instead of parsing raw JSONL from `stdout.log`, the judge accesses pre-parsed structured events in `outputs["events"]`. The events contain assistant text, tool calls with inputs, and tool results with content, all extracted from the Claude Code stream-json output at collection time.

**Why this priority**: This is the core feature. Without structured events, every judge that needs conversation data must independently parse the same JSONL stream, leading to redundant code and inconsistent extraction logic.

**Independent Test**: Configure a check judge that accesses `outputs["events"]` to count tool calls. Run scoring against a case with JSONL stdout. Verify the events list contains the expected tool calls with correct names and inputs.

**Acceptance Scenarios**:

1. **Given** a completed eval run with JSONL stdout, **When** collection runs, **Then** an `events.json` file is written per case containing structured events
2. **Given** `events.json` exists for a case, **When** `load_case_record()` runs, **Then** `record["events"]` contains the parsed event list
3. **Given** a check judge accessing `outputs["events"]`, **When** scoring runs, **Then** the judge receives a list of structured event objects with assistant text, tool calls, and tool results

---

### User Story 2 - Render conversation text in LLM judge prompts (Priority: P2)

A judge author uses `{{ conversation }}` in an LLM judge prompt to include the skill's conversation output. The template variable renders the assistant text from the structured events.

**Why this priority**: LLM judges need a template variable to include conversation text in their prompts.

**Independent Test**: Configure an LLM judge with the new template variable. Run scoring. Verify the rendered prompt contains the assistant conversation text and not raw JSONL.

**Acceptance Scenarios**:

1. **Given** an LLM judge prompt containing `{{ conversation }}`, **When** scoring runs, **Then** the variable is replaced with concatenated assistant text from the structured events

---

### User Story 3 - Evaluate skill process quality (Priority: P3)

A judge author writes a check judge that evaluates the skill's process rather than just its final output. Using `outputs["events"]`, the judge inspects the sequence of tool calls to verify the skill followed expected patterns (e.g., read input before writing output, used efficient tools, didn't retry excessively).

**Why this priority**: Process quality evaluation is a new capability that structured events unlock. It enables judges that couldn't exist before (tool selection quality, efficiency analysis, behavioral pattern detection).

**Independent Test**: Configure a check judge that verifies the skill called `Read` before `Write` by inspecting the tool call sequence in `outputs["events"]`. Run against a case. Verify the judge correctly detects the ordering.

**Acceptance Scenarios**:

1. **Given** a check judge that inspects tool call ordering in `outputs["events"]`, **When** scoring runs, **Then** the judge can iterate events and check tool names in sequence
2. **Given** a check judge that counts tool call retries, **When** scoring a case with repeated tool calls, **Then** the judge can detect and report the retry count

---

### User Story 4 - Evaluate subagent behavior (Priority: P4)

A judge author writes a check judge that evaluates how a skill delegates work to subagents. Using `outputs["events"]`, the judge can identify subagent events (tagged with `parent_tool_use_id` and `agent_id`), assess delegation decisions, and evaluate subagent output quality alongside root agent behavior.

**Why this priority**: Skills that delegate to subagents are increasingly common. Without subagent visibility, judges can only evaluate the final merged output, missing delegation quality issues (wrong agent for the task, excessive delegation, subagent errors).

**Independent Test**: Configure a check judge that counts subagent tool calls and compares against root agent calls. Run against a case where the skill spawns a subagent. Verify the judge can distinguish root vs. subagent events.

**Acceptance Scenarios**:

1. **Given** a skill run that spawns subagents, **When** events are collected, **Then** subagent events appear in the flat event list with `parent_tool_use_id` and `agent_id` tags
2. **Given** a check judge filtering `outputs["events"]` by `parent_tool_use_id`, **When** scoring runs, **Then** the judge can separate root events from subagent events
3. **Given** subagent transcript files in `subagents/*.jsonl`, **When** collection runs, **Then** transcript events are merged into the event list with proper `agent_id` tagging, deduplicated against events already streamed in stdout

---

### Edge Cases

- What happens when `stdout.log` is empty or missing? `events.json` should contain an empty list, and `record["events"]` should be `[]`.
- What happens when `stdout.log` contains non-JSONL content (non-Claude-Code runner)? Lines that fail JSON parsing are skipped. If no valid events are found, `events.json` contains an empty list.
- What happens when a tool result exceeds the size cap? The content is truncated at the configured threshold with a `"[truncated]"` marker appended.
- What happens when `traces.events` is set to `false`? No `events.json` is written. `record["events"]` is set to `[]` (empty list, never a missing key). Judges can safely iterate without checking for key existence.
- What happens when `stdout.log` contains subagent messages (with `parent_tool_use_id`)? They are included in the event list with `parent_tool_use_id` and `agent_id` tags. Judges filter to root-only with `if not e.get("parent_tool_use_id")`.
- What happens when subagent transcript files exist but their events overlap with streamed stdout events? Events are deduplicated by message ID to prevent double-counting.
- What happens when `events.json` is corrupted (partial write, invalid JSON)? `load_case_record()` sets `record["events"]` to `[]` and logs a warning. Judges receive an empty list rather than crashing.
- What happens when tool result content contains non-UTF-8 bytes? The content is replaced with a `"(binary content, N bytes)"` placeholder. No crash, no encoding errors.
- What is the difference between foreground and background subagents? Foreground subagents (Claude Code >= 2.1.108) are streamed in stdout with `parent_tool_use_id`. Background subagents only appear in `subagents/*.jsonl` transcript files. Both types get `parent_tool_use_id` and `agent_id` tags in the event list. The distinction is transparent to judges.
- What happens in batch mode (`execution.mode: batch`)? No `events.json` is produced. `record["events"]` MUST be set to `[]` (same contract as `traces.events: false`). Structured events require case mode where each case has its own `stdout.log`.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST provide a shared parsing function that converts JSONL stdout into a list of structured event objects
- **FR-002**: The system MUST parse events at collection time and persist them as `events.json` per case in the run output directory using atomic writes (write to temp file, then rename)
- **FR-003**: The scoring system MUST load `events.json` into `record["events"]` when building the case record. If `events.json` is missing or malformed, `record["events"]` MUST be set to `[]` and a warning logged to stderr
- **FR-004**: The system MUST NOT include `record["stdout"]` in the case record. Judges access execution data exclusively through `record["events"]`
- **FR-005**: The system MUST provide a `{{ conversation }}` template variable for LLM judge prompts that renders assistant conversation text from the structured events
- **FR-007**: Each structured event MUST include: event type, content (text or tool data), and timestamp when available
- **FR-008**: Tool call events MUST include the tool name and input parameters. String values in tool inputs MUST be capped at `event_result_cap` with the same truncation metadata as tool results
- **FR-009**: Tool result events MUST include the result content as valid UTF-8 text, capped at 50K characters by default (configurable via `traces.event_result_cap` in eval.yaml) with a `"[truncated]"` marker for oversized results. Non-UTF-8 content MUST be replaced with a `"(binary content, N bytes)"` placeholder. Truncated results MUST include `"truncated": true` and `"original_length": N` metadata fields
- **FR-010**: Subagent events MUST be included in the flat event list with `parent_tool_use_id` and `agent_id` tags. The `{{ conversation }}` template variable MUST render only root-level assistant text (no `parent_tool_use_id`)
- **FR-015**: The parser MUST merge subagent transcript files (`subagents/*.jsonl`) into the event list with proper `agent_id` tagging
- **FR-016**: Events from subagent transcripts that were already streamed in stdout (Claude Code >= 2.1.108) MUST be deduplicated by message ID
- **FR-011**: The `traces.events` configuration flag MUST control whether event parsing occurs at collection time. When `execution.mode: batch`, event parsing MUST be skipped regardless of `traces.events` flag value (batch mode takes precedence). In both cases (batch mode or `traces.events: false`), no `events.json` is produced and `record["events"]` MUST be set to `[]`
- **FR-012**: The `traces.stdout` configuration flag MUST continue to control whether raw `stdout.log` is retained on disk for human debugging
- **FR-013**: The existing `_extract_tool_calls()` function MUST be replaced by a lookup over `record["events"]`
- **FR-014**: The existing `_extract_assistant_text()` function MUST be replaced by a lookup over `record["events"]`

### Key Entities

- **Event**: A single interaction step in the skill's execution (assistant message, tool call, tool result, system event). Core attributes: type, content, timestamp.
- **Event List**: Ordered sequence of events representing the full conversation flow for one case. Stored as `events.json`, loaded into `record["events"]`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Judges can access structured events via `outputs["events"]` without writing any JSONL parsing code
- **SC-002**: When `traces.events` is enabled and `execution.mode: case`, JSONL parsing occurs exactly once per case (at collection time), never at scoring time
- **SC-003**: Existing check judges that used `outputs["tool_calls"]` continue to work (tool calls derived from events)
- **SC-004**: LLM judges using `{{ conversation }}` receive assistant conversation text from the structured events
- **SC-005**: Event parsing overhead scales linearly with stdout size, verified by benchmark test against representative JSONL sizes (1KB, 100KB, 1MB)

## Assumptions

- The Claude Code stream-json JSONL format is stable and the event types (`assistant`, `user`, `system`, `result`) are well-defined
- Non-Claude-Code runners produce plain text stdout that yields zero structured events (empty events list)
- The `trace_builder.py` module (used for MLflow traces) will not be refactored as part of this feature. It may adopt the shared parser in a future iteration
- `extract_usage()` in `stream_capture.py` will not be changed in this feature. It runs at execution time (before collection) and may adopt the shared parser later
- The default value for `traces.events` will be `true`, matching the expectation that structured events are the primary interface for judges
- Subagent events use a flat-with-tags structure (same list as root events, tagged with `parent_tool_use_id` and `agent_id`). This matches Claude Code's native streaming format and keeps judge iteration simple
- Subagent transcript deduplication reuses the existing `seen_msg_ids` pattern from `stream_capture.py`
