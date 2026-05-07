# Agent Eval — EvalHub Provider

Custom EvalHub provider for evaluating AI coding agent skills on Red Hat OpenShift AI.

## Build

```bash
docker build --platform linux/amd64 -f deploy/evalhub/Containerfile -t quay.io/rhoai/agent-eval-provider:latest .
```

## Push to Internal Registry

```bash
# Create ImageStream first (required before pushing)
oc create imagestream agent-eval-provider -n <namespace>

# Tag and push
docker tag quay.io/rhoai/agent-eval-provider:latest \
  image-registry.openshift-image-registry.svc:5000/<namespace>/agent-eval-provider:latest
docker push image-registry.openshift-image-registry.svc:5000/<namespace>/agent-eval-provider:latest
```

## Register Provider

Providers are registered via ConfigMap in the same namespace as the TrustyAI
operator (typically `redhat-ods-applications`), not the EvalHub CR namespace.
EvalHub discovers providers using two labels (`evalhub-provider-type` and
`evalhub-provider-name`); the remaining labels are standard ODH labels:

```bash
oc apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: evalhub-provider-agent-eval
  namespace: redhat-ods-applications
  labels:
    app.kubernetes.io/part-of: trustyai
    app.opendatahub.io/trustyai: "true"
    trustyai.opendatahub.io/evalhub-provider-type: system
    trustyai.opendatahub.io/evalhub-provider-name: agent-eval
    opendatahub.io/managed: "true"
data:
  provider.yaml: |
    $(cat deploy/evalhub/provider.yaml | sed 's/^/    /')
EOF
```

After applying the ConfigMap, add `agent-eval` to the EvalHub CR `spec.providers[]`
list and restart the EvalHub pod.

## Submit Job

```bash
evalhub eval run --config job-config.yaml
```

## Configuration

The provider expects:
- `eval.yaml` baked into the container at `/app/eval-config/eval.yaml`
- Test cases in S3 (referenced via `s3_bucket` and `s3_prefix` parameters)
  or baked into the container at `/app/eval-config/cases/`
- Claude Code CLI available in the container
- `ANTHROPIC_API_KEY` or Vertex AI credentials as environment variables
