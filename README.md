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

Terraform module + Helm chart live in [ProjectCircleIL/terraform-modules](https://github.com/ProjectCircleIL/terraform-modules) under `modules/extras/alert-analyzer/`.

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
| `BEDROCK_MODEL` | `us.anthropic.claude-opus-4-6-v1` | Claude model ID |
| `SLACK_WEBHOOK_URL` | - | Slack webhook (secret) |
| `CLUSTER_NAME` | - | Kubernetes cluster name |
| `TZ` | `UTC` | Timezone for Slack timestamps (invalid value falls back to UTC) |
| `BEDROCK_MAX_TOKENS` | `2048` | Max output tokens per Bedrock call |
| `MAX_AGENT_TURNS` | `20` | Max investigation steps before forced summary |
| `UNHEALTHY_SKIP_NAMESPACES` | `kube-system,groundcover,...` | Namespaces whose Unhealthy events are skipped |

## Deployment

### Prerequisites

Create a Slack webhook URL secret in AWS Secrets Manager:

```bash
aws secretsmanager create-secret \
  --name "alert-analyzer/slack-webhook-url" \
  --secret-string "https://hooks.slack.com/services/T.../B.../xxx"
```

### With Terraform (recommended)

The Terraform module lives in [ProjectCircleIL/terraform-modules](https://github.com/ProjectCircleIL/terraform-modules) at `modules/extras/alert-analyzer/`.

```hcl
module "alert_analyzer" {
  source       = "git::https://github.com/ProjectCircleIL/terraform-modules.git//modules/extras/alert-analyzer"
  cluster_name = "my-cluster"
}
```

Image defaults to `public.ecr.aws/j5u9j5q0/alert-analyzer`. Override with `image_repository` if needed.

The module auto-discovers:
- ClickHouse password from `groundcover-clickhouse` K8s secret
- Slack webhook from `alert-analyzer/slack-webhook-url` in Secrets Manager
- OIDC provider from the EKS cluster (for IRSA)

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

