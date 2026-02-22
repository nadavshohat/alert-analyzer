# Alert Analyzer

AI-powered Kubernetes crash analysis using Groundcover + AWS Bedrock Claude.

Detects pod crashes, analyzes logs and traces, and sends Slack alerts with root cause analysis.

## Architecture

```
Groundcover ClickHouse (events, logs, traces)
         │
         ▼
  Alert Analyzer Pod
  1. Poll events → 2. Fetch logs/traces → 3. Exec into pod
  4. Web search  → 5. Claude analysis   → 6. Slack alert
         │                                      │
         ▼                                      ▼
   AWS Bedrock (Claude)                    Slack Webhook
```

## Features

- **Crash Detection**: Polls ClickHouse for CrashLoopBackOff, OOMKilled, Unhealthy, etc.
- **Log + Trace Analysis**: Fetches container logs and slow traces for context
- **Pod Inspection**: Reads source code, config files, cgroup limits from crashing pods
- **Web Research**: Searches DuckDuckGo for unfamiliar errors
- **AI Analysis**: Claude determines root cause with confidence level
- **Slack Notifications**: mrkdwn formatted alerts with Groundcover deep links

## Project Structure

```
├── src/
│   ├── main.py          # Entry point, polling loop
│   ├── config.py         # Configuration (env vars)
│   ├── agent.py          # Bedrock Converse agentic loop
│   ├── tools.py          # Tool handlers (logs, traces, exec, web search)
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
| `TZ` | `Asia/Jerusalem` | Timezone for Slack timestamps |

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

## License

MIT
