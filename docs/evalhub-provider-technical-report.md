# EvalHub Agent-Eval Provider — Technical Report

## What This Is

A custom EvalHub provider that runs RFE quality assessments on RHOAI. Instead of evaluating LLMs against benchmarks (like lm-evaluation-harness), this provider evaluates *documents* — it calls Claude via Vertex AI to score RFEs against a rubric, then validates the output with inline judges.

## End-to-End Flow

### Step 1: Job Submission

```mermaid
sequenceDiagram
    participant User as User (CLI)
    participant EH as EvalHub API
    participant K8s as Kubernetes API
    participant Pod as Adapter Pod

    User->>EH: evalhub eval run --config job.yaml
    Note over EH: Resolves provider "agent-eval"<br/>and benchmark "skill-eval"
    EH->>K8s: Create ConfigMap (job.json)
    EH->>K8s: Create batch/v1 Job
    K8s->>Pod: Schedule pod with<br/>adapter + MLflow sidecar
    Note over Pod: Pod mounts:<br/>- /meta/job.json (ConfigMap)<br/>- SA token (projected)<br/>- service-ca (TLS)
```

### Step 2: Adapter Execution

```mermaid
flowchart TD
    A[entrypoint.py] --> B[Load /meta/job.json]
    B --> C[Create AgentEvalAdapter]
    C --> D[Load eval.yaml from /app/eval-config/]

    D --> E{skill field empty?}
    E -->|Yes| F[Direct LLM Mode]
    E -->|No| G[Skill Mode via ClaudeCodeRunner]

    F --> F1[Create Anthropic client<br/>Vertex AI or direct]
    F1 --> F2[Read rubric.md]
    F2 --> F3[Copy dataset to writable /tmp]

    F3 --> F4[For each case:]
    F4 --> F5[Read input.yaml]
    F5 --> F6[Call Vertex AI Claude<br/>with rubric + RFE content]
    F6 --> F7[Write results/result.md]
    F7 --> F4

    G --> G1[Create ClaudeCodeRunner]
    G1 --> G2[For each case:]
    G2 --> G3[Resolve arguments from input.yaml]
    G3 --> G4[Run skill via claude --print]
    G4 --> G2

    F4 --> H[Score with judges]
    G2 --> H
    H --> I[Map to JobResults]
    I --> J[Log to MLflow via sidecar]
    J --> K[Report results to EvalHub API]

    style F fill:#2563eb,color:#fff
    style G fill:#7c3aed,color:#fff
```

### Step 3: Scoring Pipeline

```mermaid
flowchart LR
    subgraph Judges
        J1[has_scoring_table<br/>bool: 5 criteria present?]
        J2[has_verdict<br/>bool: PASS/FAIL present?]
        J3[has_feedback<br/>bool: feedback when FAIL?]
        J4[scores_valid<br/>bool: 0-2 per criterion,<br/>total matches sum?]
        J5[rubric_score<br/>int: 0-10 total extracted]
    end

    R[result.md] --> J1
    R --> J2
    R --> J3
    R --> J4
    R --> J5

    J1 --> A[aggregated scores]
    J2 --> A
    J3 --> A
    J4 --> A
    J5 --> A

    A --> M[MLflow metrics:<br/>has_scoring_table: 1.0<br/>has_verdict: 1.0<br/>has_feedback: 1.0<br/>scores_valid: 1.0<br/>rubric_score: 9.0]
```

### Step 4: MLflow Integration

```mermaid
sequenceDiagram
    participant Pod as Adapter Pod
    participant Sidecar as MLflow Sidecar<br/>(localhost:8080)
    participant MLflow as MLflow Server<br/>(8443/TLS, kubernetes-auth)

    Pod->>Sidecar: GET /experiments/get-by-name
    Sidecar->>MLflow: Forward with SA token +<br/>X-MLflow-Workspace header
    MLflow-->>Sidecar: experiment_id=8
    Sidecar-->>Pod: experiment_id=8

    Pod->>Sidecar: POST /runs/create
    Sidecar-->>Pod: run_id

    Pod->>Sidecar: POST /runs/log-batch<br/>(8 metrics)
    Sidecar-->>Pod: OK

    Pod->>Sidecar: POST /runs/update (FINISHED)
    Sidecar-->>Pod: OK
```

## Infrastructure Diagram

```mermaid
graph TD
    subgraph cluster["ROSA Cluster - jeder-evalhub"]
        subgraph ns_evalhub["evalhub namespace"]
            EH[EvalHub API<br/>Go REST service]
            PG[(PostgreSQL<br/>storage)]
            EH --> PG
        end

        subgraph jobpod["Job Pod"]
            ENT[entrypoint.py]
            ADP[adapter.py<br/>AgentEvalAdapter]
            SC[score.py<br/>judges]
            SB[MLflow sidecar]
            ENT --> ADP
            ADP --> SC
            ADP --> SB
        end

        subgraph ns_rhoai["redhat-ods-applications"]
            MLF[MLflow Server<br/>kubernetes-auth]
        end

        SB --> MLF
        EH -->|Create Job| jobpod
        jobpod -->|Callback| EH
    end

    subgraph ext["External"]
        VAI[Vertex AI Claude<br/>Sonnet 4.6]
    end

    ADP -->|Anthropic API| VAI

    subgraph kyv["Kyverno Policies"]
        KP[inject-vertex-ai-credentials]
    end

    KP -.->|Injects env vars +<br/>credentials volume| jobpod
```

## Architecture Alignment with EvalHub Spec

Reviewed against [opendatahub-io/architecture-context eval-hub.md](https://github.com/opendatahub-io/architecture-context/blob/main/architecture/rhoai-3.4/eval-hub.md):

### Aligned

| Requirement | Status | How |
|---|---|---|
| Adapter implements `FrameworkAdapter` | ✅ | `AgentEvalAdapter(FrameworkAdapter)` in adapter.py |
| Job spec at `/meta/job.json` | ✅ | entrypoint.py reads from `/meta/job.json` |
| Status callbacks via `JobCallbacks` | ✅ | Reports INITIALIZING → LOADING_DATA → RUNNING_EVALUATION → POST_PROCESSING → COMPLETED |
| Returns `JobResults` with `EvaluationResult` metrics | ✅ | results_mapper.py maps all metrics |
| MLflow via projected SA token | ✅ | Sidecar handles auth; adapter logs via `callbacks.mlflow.save()` |
| UBI9 base image | ✅ | `registry.access.redhat.com/ubi9/python-311:latest` |
| Non-root execution | ✅ | UBI9 python image runs as non-root by default |
| Provider registered via ConfigMap | ✅ | `deploy/evalhub/configmap-template.yaml` with TrustyAI labels |
| S3 dataset download | ✅ | `s3_dataset.py` with path traversal protection |

### Gaps / Workarounds

| Gap | Workaround | Filed |
|---|---|---|
| No parameter passthrough to benchmark pods | Kyverno ClusterPolicy injects Vertex AI creds | [#50](https://github.com/opendatahub-io/agent-eval-harness/issues/50) |
| No volume mount support in provider spec | Kyverno injects credentials volume | [#51](https://github.com/opendatahub-io/agent-eval-harness/issues/51) |
| No per-job env var injection | Kyverno policy — cannot vary per job | [#52](https://github.com/opendatahub-io/agent-eval-harness/issues/52) |
| Baked-in dataset is read-only | Copy to writable temp dir before execution | N/A — container design, not EvalHub gap |
| MLflow API requires SA token + workspace header | Adapter pod sidecar handles transparently; CLI access requires manual port-forward | [#55](https://github.com/opendatahub-io/agent-eval-harness/issues/55) |
| No job management CLI (list/delete/inspect) | Manual API calls via curl + SA token | [#56](https://github.com/opendatahub-io/agent-eval-harness/issues/56) |
| `overall_score` mixes bool/numeric judge scales | Set to None; individual metrics visible | Design issue in results_mapper, not EvalHub |
| No traces in MLflow (only metrics) | Follow-up: add `mlflow.anthropic.autolog()` | [#54](https://github.com/opendatahub-io/agent-eval-harness/issues/54) |

### Spec Items Not Applicable to This Provider

| Spec Item | Why N/A |
|---|---|
| OCI artifact export | Not wired — future work |
| S3 `testDataRef` init container | Using baked-in dataset, not S3-hosted |
| Model endpoint auth secret | Calls Vertex AI, not a model endpoint |
| Multi-tenant X-Tenant header | Single-tenant deployment |

## Test Runs

| Run | Date | Model | rubric_score | All judges pass? | MLflow run_id |
|---|---|---|---|---|---|
| Final (current) | 2026-05-05 15:16 | claude-sonnet-4-6 | 9/10 | ✅ 5/5 | `0e58068d...` |
| Reference (assess-rfe plugin) | 2026-05-03 04:10 | claude-sonnet-4-6 | 8/10 | N/A | N/A |

Variance (8 vs 9) is expected — LLM non-determinism on subjective rubric criteria (WHY, Right-sized).

## Open Issues

### Filed against [opendatahub-io/agent-eval-harness](https://github.com/opendatahub-io/agent-eval-harness/issues)

| Issue | Type | Description |
|---|---|---|
| [#50](https://github.com/opendatahub-io/agent-eval-harness/issues/50) | RFE | EvalHub should forward job parameters to benchmark pods |
| [#51](https://github.com/opendatahub-io/agent-eval-harness/issues/51) | RFE | EvalHub should support volume mounts in provider spec |
| [#52](https://github.com/opendatahub-io/agent-eval-harness/issues/52) | RFE | Per-job env var injection into benchmark pods |
| [#54](https://github.com/opendatahub-io/agent-eval-harness/issues/54) | Feature | Add MLflow tracing for adapter LLM calls |
| [#55](https://github.com/opendatahub-io/agent-eval-harness/issues/55) | Feature | Document RHOAI MLflow API authentication pattern |
| [#56](https://github.com/opendatahub-io/agent-eval-harness/issues/56) | Feature | Add job management commands to evalhub CLI integration |

### Filed against [eval-hub/eval-hub](https://github.com/eval-hub/eval-hub/issues)

| Issue | Type | Description |
|---|---|---|
| [#538](https://github.com/eval-hub/eval-hub/issues/538) | Bug | `evalhub eval run --wait` polls indefinitely after job completes |
