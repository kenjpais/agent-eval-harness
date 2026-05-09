# Deep Review Findings

**Date:** 2026-05-09
**Branch:** 002-structured-events
**Rounds:** 1
**Gate Outcome:** PASS
**Invocation:** manual

## Summary

| Severity | Found | Fixed | Remaining |
|----------|-------|-------|-----------|
| Critical | 1 | 1 | 0 |
| Important | 4 | 4 | 0 |
| Minor | 6 | - | 6 |
| **Total** | **11** | **5** | **6** |

**Agents completed:** 5/5 (+ 1 external tool)
**Agents failed:** none

## Findings

### FINDING-1
- **Severity:** Critical
- **Confidence:** 90
- **File:** agent_eval/events.py:157-189
- **Category:** architecture
- **Source:** architecture-agent
- **Round found:** 1
- **Resolution:** fixed (round 1)

**What is wrong:**
Two functions were defined but never called: `_resolve_tool_results` (body was just `pass`) and `_make_tool_result_event` (23-line function for creating tool result events). Both were artifacts of planned-but-unimplemented functionality for extracting tool results from stream-json content_block events.

**Why this matters:**
Dead code with complex logic increases maintenance burden and suggests incomplete implementation. The stub `_resolve_tool_results` was called from `parse_stream_events` but did nothing, wasting a function call per parse.

**How it was resolved:**
Removed both functions and the call to `_resolve_tool_results` from `parse_stream_events`. If tool result extraction is needed later, it can be recovered from git history.

### FINDING-2
- **Severity:** Important
- **Confidence:** 95
- **File:** agent_eval/events.py:82-86
- **Category:** correctness
- **Source:** correctness-agent (also reported by: architecture-agent)
- **Round found:** 1
- **Resolution:** fixed (round 1)

**What is wrong:**
When multiple tool input fields were truncated, `original_length` was computed as the sum of all oversized field lengths. This metric was ambiguous: a judge seeing `original_length: 8000` couldn't tell if it was one 8K field or four 2K fields.

**Why this matters:**
Misleading metadata undermines the utility of truncation information for judges.

**How it was resolved:**
Changed from `sum()` to `max()` so `original_length` reports the largest truncated field's original size. This is unambiguous and most useful for judges assessing truncation impact.

### FINDING-3
- **Severity:** Important
- **Confidence:** 80
- **File:** tests/test_events.py:524-558
- **Category:** test-quality
- **Source:** test-quality-agent (also reported by: architecture-agent)
- **Round found:** 1
- **Resolution:** fixed (round 1)

**What is wrong:**
The `test_subagent_dedup` test created two events with the same `msg_id` but verified deduplication using a set that unconditionally added a hardcoded string, making the assertion meaningless.

**Why this matters:**
A weak assertion on deduplication (FR-016) means a regression could go undetected.

**How it was resolved:**
Simplified the assertion to verify `len(result) == 1` and that the kept event has the correct text content (`"First block"`), confirming the first event was preserved and the duplicate was dropped.

### FINDING-4
- **Severity:** Important
- **Confidence:** 85
- **File:** tests/test_events.py:198-210
- **Category:** test-quality
- **Source:** test-quality-agent
- **Round found:** 1
- **Resolution:** fixed (round 1)

**What is wrong:**
The `test_multiple_large_inputs` test checked `truncated: true` but didn't verify the `original_length` metadata value, leaving the calculation logic untested.

**Why this matters:**
The `original_length` semantics changed from sum to max. Without a test pinning the expected value, the calculation could drift silently.

**How it was resolved:**
Added `assert tool["original_length"] == 2000` to verify the max truncated field length is reported correctly.

### FINDING-5
- **Severity:** Important
- **Confidence:** 85
- **File:** agent_eval/events.py:275-277
- **Category:** correctness
- **Source:** correctness-agent
- **Round found:** 1
- **Resolution:** not fixed (acceptable design)

**What is wrong:**
When a transcript event lacks `parent_tool_use_id`, the code uses the transcript filename stem (agent_id) as a fallback. This is a category error since `parent_tool_use_id` should be the tool use ID that spawned the agent.

**Why this matters:**
The fallback creates technically invalid metadata. However, transcript events without `parent_tool_use_id` are rare (only from very old Claude Code versions), and the fallback ensures all transcript events are tagged as subagent events so they're correctly filtered by judges.

## Remaining Findings (Minor)

- **Correctness**: Tool input capping doesn't recurse into nested dicts/lists. Acceptable because Claude tool inputs are flat key-value dicts.
- **Correctness**: Weak fallback agent_id generation could collide on empty message IDs. Edge case unlikely in practice.
- **Architecture**: `_extract_msg_id_from_event` adds unnecessary indirection. Acceptable as an abstraction point for future changes.
- **Architecture**: Tool pattern matching duplicated between events-based and raw stdout extraction. Two call sites, different data shapes.
- **Test Quality**: Missing boundary test for content at exactly `result_cap` length. Low risk.
- **Test Quality**: Benchmark test allows 50x ratio. Guards against quadratic-or-worse, not exact linearity.
