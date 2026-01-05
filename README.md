# Alert Analyzer

AI-powered Kubernetes crash analysis using Groundcover + AWS Bedrock Claude.

Automatically detects pod crashes, analyzes logs and traces, and sends enriched Slack alerts with root cause analysis and actionable recommendations.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                 Groundcover Clickhouse                   │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐      │
│  │ events      │  │ logs        │  │ traces      │      │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘      │
└─────────┼────────────────┼────────────────┼─────────────┘
          │                │                │
          ▼                ▼                ▼
┌─────────────────────────────────────────────────────────┐
│              Alert Analyzer (Python Pod)                 │
│                                                          │
│  1. Poll events  →  2. Fetch logs/traces  →  3. Research │
│                                                          │
│  4. Analyze with Claude  →  5. Send to Slack            │
└─────────────────────────────────────────────────────────┘
          │                                    │
          ▼                                    ▼
   ┌──────────────┐                    ┌──────────────┐
   │ AWS Bedrock  │                    │    Slack     │
   │ (Claude)     │                    │  (Webhook)   │
   └──────────────┘                    └──────────────┘
```

## Features

- **Crash Detection**: Polls Groundcover Clickhouse for `CrashLoopBackOff`, `OOMKilled`, `Unhealthy`, etc.
- **Log Analysis**: Fetches recent container logs for context
- **Trace Analysis**: Identifies slow operations that may cause health check failures
- **Web Research**: Searches DuckDuckGo for error solutions
- **Pod Inspection**: Reads config files (package.json, etc.) from crashing pods
- **AI Analysis**: Uses Claude to determine root cause and recommendations
- **Slack Notifications**: Rich mrkdwn formatted alerts with deep links to Groundcover

## Example Alert

```
🟡 Unhealthy: solar-service

Summary
Event loop blocked by synchronous operations causing health check timeouts.

Findings
• Event: Unhealthy
  Namespace: prod
  Pod: solar-service-5dc7b8f7b4-bnlt5
  Message: Readiness probe failed: connection refused

Root Cause
Batch operations taking 90-100+ seconds are blocking the Node.js event loop,
preventing the health check endpoint from responding.

Recommended Action
Offload batch data processing to worker threads or a queue-based service.

Last seen: 09:17 | View in Groundcover
```

## Configuration

All configuration via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `CLICKHOUSE_HOST` | `groundcover-clickhouse` | Clickhouse hostname |
| `CLICKHOUSE_PORT` | `8123` | Clickhouse HTTP port |
| `CLICKHOUSE_USER` | `default` | Clickhouse username |
| `CLICKHOUSE_PASSWORD` | - | Clickhouse password |
| `CLICKHOUSE_DATABASE` | `groundcover` | Clickhouse database |
| `POLL_INTERVAL_SECONDS` | `30` | How often to poll for events |
| `DEDUP_WINDOW_SECONDS` | `300` | Suppress duplicate alerts within this window |
| `LOG_LOOKBACK_MINUTES` | `30` | How far back to fetch logs |
| `EXCLUDE_NAMESPACES` | `kube-system,groundcover` | Namespaces to ignore |
| `EVENT_REASONS` | `CrashLoopBackOff,OOMKilled,...` | Event types to monitor |
| `BEDROCK_REGION` | `us-west-2` | AWS region for Bedrock |
| `BEDROCK_MODEL` | `anthropic.claude-sonnet-4-20250514-v1:0` | Claude model ID |
| `SLACK_WEBHOOK_URL` | - | Slack incoming webhook URL |
| `GROUNDCOVER_BASE_URL` | `https://app.groundcover.com` | Groundcover UI URL |
| `GROUNDCOVER_TENANT_UUID` | - | Your Groundcover tenant ID |
| `CLUSTER_NAME` | - | Kubernetes cluster name |

## Deployment

### Prerequisites

1. **Groundcover** installed in your cluster (provides Clickhouse with events/logs/traces)
2. **AWS Bedrock** access with Claude enabled
3. **Slack webhook** URL for notifications

### IAM Role (for IRSA)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": "arn:aws:bedrock:*::foundation-model/anthropic.*"
    }
  ]
}
```

### Deploy to Kubernetes

1. Update `k8s/configmap.yaml` with your configuration
2. Apply manifests:

```bash
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/deployment.yaml
```

### Build Docker Image

```bash
docker build -t alert-analyzer:latest .
docker tag alert-analyzer:latest <your-ecr-repo>/alert-analyzer:latest
docker push <your-ecr-repo>/alert-analyzer:latest
```

## Project Structure

```
├── src/
│   ├── main.py          # Entry point, polling loop
│   ├── clickhouse.py    # Clickhouse queries (events, logs, traces)
│   ├── analyzer.py      # Bedrock Claude analysis
│   ├── notifier.py      # Slack notifications
│   ├── research.py      # Web search + pod file reading
│   └── config.py        # Configuration
├── k8s/
│   ├── deployment.yaml  # K8s Deployment, ServiceAccount, RBAC
│   └── configmap.yaml   # ConfigMap + Secret
├── Dockerfile
└── requirements.txt
```

## How It Works

1. **Poll**: Every 30s, query Clickhouse `events` table for Warning events
2. **Deduplicate**: Skip if same workload/reason alerted within dedup window
3. **Fetch Context**: Get logs and slowest traces for the workload
4. **Research**: Search web for error solutions, read pod config files
5. **Analyze**: Send all context to Claude for root cause analysis
6. **Notify**: Format and send Slack message with findings

## Cost Estimate

| Scenario | Input Tokens | Output Tokens | Cost (Claude Sonnet) |
|----------|--------------|---------------|----------------------|
| 1 crash | ~4,000 | ~500 | ~$0.02 |
| 10 crashes/day | ~40,000 | ~5,000 | ~$0.20/day |

## License

MIT
