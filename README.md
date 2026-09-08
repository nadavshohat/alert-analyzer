# Alert Analyzer

AI-powered Kubernetes crash analysis using Groundcover + AWS Bedrock Claude.

Detects pod crashes, analyzes logs and traces, and sends Slack alerts with root cause analysis.

## Architecture

```
Groundcover ClickHouse (events, logs, traces)
         │
         ▼
  Alert Analyzer Pod
   1. Poll events -> 2. Fetch logs/traces -> 3. Read files/env from pod
   4. Claude analysis -> 5. Slack alert
         │                                      │
         ▼                                      ▼
   AWS Bedrock (Claude)                    Slack Webhook
```

## Features

- **Crash Detection**: Polls ClickHouse for CrashLoopBackOff, OOMKilled, Unhealthy, etc.
- **Log + Trace Analysis**: Fetches container logs and slow traces for context
- **Pod Inspection**: Reads files, lists directories, and views (secret-redacted) env vars in the crashing pod via typed, read-only operations scoped to the alert's namespace
- **AI Analysis**: Claude determines root cause with confidence level
- **Slack Notifications**: mrkdwn formatted alerts with Groundcover deep links

## Project Structure

```
├── src/
│   ├── main.py          # Entry point, polling loop
│   ├── config.py         # Configuration (env vars)
│   ├── agent.py          # Bedrock Converse agentic loop
│   ├── tools.py          # Tool handlers (logs, traces, pod reads)
│   ├── clickhouse.py     # ClickHouse queries
│   └── notifier.py       # Slack formatting
├── Dockerfile            # Multi-stage build
└── requirements.txt
```

The Helm chart is in [`chart/`](chart/). A Terraform wrapper for AWS deployments lives in a separate private repo (see Deployment below).

## Configuration

All configuration via environment variables (set in Helm values or ConfigMap):

| Variable | Default | Description |
|----------|---------|-------------|
| `CLICKHOUSE_HOST` | `groundcover-clickhouse` | ClickHouse hostname |
| `CLICKHOUSE_PORT` | `8123` | ClickHouse HTTP port |
| `CLICKHOUSE_PASSWORD` | - | ClickHouse password (secret) |
| `POLL_INTERVAL_SECONDS` | `30` | Polling frequency |
| `DEDUP_WINDOW_SECONDS` | `300` | Suppress duplicate alerts |
| `LOG_LOOKBACK_MINUTES` | `30` | Log fetch window |
| `EXCLUDE_NAMESPACES` | `kube-system,groundcover` | Ignored namespaces |
| `EVENT_REASONS` | `CrashLoopBackOff,OOMKilled,...` | Event types to monitor |
| `BEDROCK_REGION` | `us-west-2` | AWS Bedrock region |
| `BEDROCK_MODEL` | `us.anthropic.claude-sonnet-5` | Claude model ID (Bedrock inference profile) |
| `SLACK_WEBHOOK_URL` | - | Slack webhook (secret) |
| `CLUSTER_NAME` | - | Kubernetes cluster name |
| `TZ` | `UTC` | Timezone for Slack timestamps (invalid value falls back to UTC) |
| `BEDROCK_MAX_TOKENS` | `2048` | Max output tokens per Bedrock call |
| `MAX_AGENT_TURNS` | `20` | Max investigation steps before forced summary |
| `UNHEALTHY_SKIP_NAMESPACES` | `kube-system,groundcover,...` | Namespaces whose Unhealthy events are skipped |
| `EVENT_SOURCE` | `clickhouse` | Where crash events come from: `clickhouse` or `kubernetes` |
| `LOG_SOURCE` | `clickhouse` | Where logs come from: `clickhouse`, `kubernetes`, or `loki` |
| `METRIC_SOURCE` | `clickhouse` | Where metrics come from: `clickhouse`, `kubernetes`, or `prometheus` |
| `TRACE_SOURCE` | `clickhouse` | `clickhouse` or `none` (only Groundcover/ClickHouse has traces) |
| `LOKI_URL` | - | Base URL when `LOG_SOURCE=loki` (e.g. `http://loki:3100`) |
| `LOKI_TENANT` | - | Sets `X-Scope-OrgID` for multi-tenant Loki |
| `PROMETHEUS_URL` | - | Base URL when `METRIC_SOURCE=prometheus` (e.g. `http://prometheus:9090`) |

### Telemetry backends

Each signal is selected independently, so a stack can mix backends. Defaults are all-ClickHouse (Groundcover), so existing deployments are unchanged. What each backend can serve:

| Signal | ClickHouse (Groundcover) | Kubernetes API | Loki | Prometheus |
|--------|--------------------------|----------------|------|------------|
| events (trigger) | yes | yes | - | - |
| logs | yes | yes | yes | - |
| metrics | yes (history) | current only | - | yes (history) |
| traces | yes | - | - | - |

To run on a plain open-source cluster with no ClickHouse:

```bash
EVENT_SOURCE=kubernetes
LOG_SOURCE=loki           # or kubernetes for pod-log reads with no Loki
METRIC_SOURCE=prometheus  # or kubernetes for current memory via metrics-server
TRACE_SOURCE=none
LOKI_URL=http://loki.observability:3100
PROMETHEUS_URL=http://prometheus.observability:9090
```

The Kubernetes-native backend needs no telemetry stack at all (events from the API, logs from pod-log reads, current memory from metrics-server), so `EVENT_SOURCE=kubernetes LOG_SOURCE=kubernetes METRIC_SOURCE=kubernetes TRACE_SOURCE=none` runs anywhere. Two limits to know: core Kubernetes events are retained about an hour, and metrics-server reports only current usage (no history).

## Deployment

The chart is in [`chart/`](chart/). It needs three things: AWS credentials for Bedrock (IRSA on EKS), a Slack webhook, and network reach to your telemetry backend (Groundcover ClickHouse today).

### With Helm

Create a secret holding the two sensitive values, then install:

```bash
kubectl create namespace observability

kubectl -n observability create secret generic alert-analyzer-secrets \
  --from-literal=SLACK_WEBHOOK_URL="https://hooks.slack.com/services/T.../B.../xxx" \
  --from-literal=CLICKHOUSE_PASSWORD="<clickhouse-password>"

helm install alert-analyzer ./chart -n observability \
  --set secrets.existingSecret=alert-analyzer-secrets \
  --set groundcover.clusterName=my-cluster \
  --set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"="arn:aws:iam::<acct>:role/<irsa-role>"
```

To skip the pre-created secret, pass the values inline with `--set secrets.slackWebhookUrl=...` and `--set secrets.clickhousePassword=...` (they are stored in the Helm release). All other settings are in [`chart/values.yaml`](chart/values.yaml); the environment variables they map to are documented under Configuration above.

### AWS deployments (ProjectCircle internal)

ProjectCircle wraps this chart with a private Terraform module that provisions the IRSA role, reads the Slack webhook from AWS Secrets Manager and the ClickHouse password from a K8s secret, and accepts the Bedrock model agreement. It is a convenience for our EKS clusters and is not required to run the tool.

### Build Docker Image

```bash
docker build --platform linux/amd64 -t public.ecr.aws/j5u9j5q0/alert-analyzer:latest .
docker push public.ecr.aws/j5u9j5q0/alert-analyzer:latest
```

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
PYTHONPATH=src pytest tests/ -q
```

## Security

- Pod access is read-only and typed (`read_file` / `list_dir` / `get_env`); there is no arbitrary command execution. All pod access is scoped to the namespace of the alert under investigation.
- Env var values whose key name looks like a secret are redacted on a best-effort basis; this is not a security boundary, so the deployment RBAC should not mount secrets the analyzer does not need.
- Requires a namespaced RBAC Role granting `get,list` on `pods`/`pods/log` and `get,create` on `pods/exec`.

## License

MIT

