# PR #84 Questions

- What is the agentic-ci generic harness?
- What do you mean by "adds full OTel span export to the agentic-ci generic harness and a new agentic-ci mlflow-push CLI subcommand"?
- What is OTel span?
- What is the use-case for PR #84?
- What is the problem that PR #84 solves?
- Give me a short example of how PR #84 is used.
- How does PR #84 relate to the agent-eval-harness?
- What do you mean by "separate follow-up CI job"?
- What pipeline are you referring to?

# PR #536 Questions

- What is the prow-agent plugin?
- What is the OTel collector approach from agentic-ci?
- How does it port the OTel collector approach from agentic-ci into an OpenShift-specific Claude Code plugin called prow-agent?
- What do you mean by "adding BigQuery extraction"?
- What is the new prow-agent plugin in the ai-helpers marketplace with version 0.0.3?
- What is autodl?
- What is a BigQuery-compatible autodl JSON schema?
- What is `--stream-log`?
- Why is it required?
- How does it enrich metrics with identity fields (session ID, model, prompt, version) from Claude's stream-json output?
- How is prow-agent used in the agentic-ci generic harness?
- How is prow-agent different from the agent-eval-harness?
- Can the agent-eval-harness be used with the prow-agent plugin?
- How does the prow-agent plugin compare to the agent-eval-harness?
- How does the prow-agent plugin complement the point-in-time evaluation provided by agent-eval-harness with production-level signal?
- What do you mean by production-level signal?
- What is payload-triage data?
- What is the payload-triage table?
- How is https://github.com/opendatahub-io/agentic-ci different from this plugin?
- What is the use of agentic-ci?
- Simplify and answer the Open Questions in `@ai-helpers/plugins/prow-agent/docs/metrics-collection.md` in PR #536.
- What is the difference between OTel collection and legacy stream-JSON?
- How is legacy stream-JSON collected?
- Where and how is it collected?

# PR #80547 Questions

- What is the difference between `prow-agent@ai-helpers` and `must-gather@ai-helpers` plugin?
- Explain the statement:
  > "Starts otel_collector.py in the background before running Claude, waits for its port file, exports OTEL env vars, then runs Claude with --output-format stream-json | tee."
- What is a port file?
- Explain the statement:
  > "Saves claude-otel.jsonl as a Prow artifact for per-request debugging."
- How is per-request debugging done?
- What is a JUnit test?
- Why is it used in this context?

# General Questions

- How should we use the features from PRs #84, #536, and #80547 with the agent-eval-harness [agent-eval-harness PR #77](https://github.com/opendatahub-io/agent-eval-harness/pull/77) to evaluate the agentic-docs Claude Code plugin located at:
  `@/Users/kpais/kpais-workspace/claude-tmp/eval-harness/agentic-docs`?
- Which of these features are most useful for evaluating the agentic-docs Claude Code plugin?
- How should we combine the capabilities from PRs #84, #536, and #80547 with the [agent-eval-harness PR #77](https://github.com/opendatahub-io/agent-eval-harness/pull/77) to create a complete evaluation workflow for the agentic-docs Claude Code plugin?
- What metrics should be collected to assess whether agentic-docs improve agent performance, efficiency, and task completion quality?

  Consider metrics such as:
  - Task success rate
  - Tool usage patterns
  - Number of turns
  - Token consumption
  - Cost
  - Latency
  - Reasoning quality
  - Trace and span analysis
  - Events and agent decision points
  - Error rates and recovery behavior

- Should we use LangFuse for evaluating:
  - Agent behavior
  - Reasoning traces
  - Prompt quality
  - LLM-as-a-judge workflows

- Should we use OpenTelemetry (OTel) for:
  - Infrastructure observability
  - Spans
  - Traces
  - Latency
  - Operational metrics

- How should LangFuse and OTel be integrated to provide a comprehensive evaluation framework without duplicating functionality?
- How can agent-eval-harness be leveraged to perform LLM-as-a-judge evaluations of the agentic-docs Claude Code plugin?
- What qualitative and quantitative evaluation criteria should be used to determine whether generated agentic-docs improve agent outcomes?
- How should baseline and experimental runs be compared?

# IMPORTANT

- Verify all responses.
- Clearly distinguish between facts derived from the referenced PRs and assumptions or recommendations.
- Include references to the relevant code, documentation, or PR discussions that support each answer.
- Call out any gaps, ambiguities, or unanswered questions in the current implementation.

# Goal

Establish a rigorous framework for measuring the impact of generated agentic-docs on:

- Claude agent behavior
- Reasoning quality
- Efficiency
- Observability
- Tool usage
- Cost
- Latency
- Overall task success

The framework should combine evaluation, observability, and production telemetry to quantify the value of generated agentic-docs and determine whether they improve agent outcomes compared to a baseline without agentic-docs.