# alert-analyzer Helm chart

Deploys the alert-analyzer daemon: it polls a telemetry backend for Kubernetes
crash events, investigates each with Claude on Amazon Bedrock, and posts a root
cause to Slack.

## Install

```bash
kubectl create namespace observability
kubectl -n observability create secret generic alert-analyzer-secrets \
  --from-literal=SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..." \
  --from-literal=CLICKHOUSE_PASSWORD="<password>"

helm install alert-analyzer ./chart -n observability \
  --set secrets.existingSecret=alert-analyzer-secrets \
  --set groundcover.clusterName=my-cluster \
  --set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"="<irsa-role-arn>"
```

## Values

| Key | Default | Notes |
|-----|---------|-------|
| `image.repository` / `image.tag` | `public.ecr.aws/j5u9j5q0/alert-analyzer` / `latest` | container image |
| `bedrock.region` / `bedrock.model` | `us-west-2` / `us.anthropic.claude-sonnet-5` | Bedrock inference profile |
| `bedrock.maxTokens` | `2048` | max response tokens |
| `agent.maxTurns` | `10` | Bedrock tool-use loop cap |
| `agent.pollInterval` | `30` | seconds between event polls |
| `agent.dedupWindow` | `1800` | seconds to suppress a repeat alert |
| `events.reasons` | crash reasons | event types that trigger analysis |
| `events.excludeNamespaces` | `kube-system,groundcover` | ignored namespaces |
| `clickhouse.host` / `port` / `user` / `database` | Groundcover defaults | telemetry backend |
| `groundcover.clusterName` | `""` | cluster label for Slack deep links |
| `serviceAccount.annotations` | `{}` | set the IRSA role ARN here on EKS |
| `secrets.existingSecret` | `""` | name of a Secret with `SLACK_WEBHOOK_URL` and `CLICKHOUSE_PASSWORD`; wins over the inline values |
| `secrets.slackWebhookUrl` / `secrets.clickhousePassword` | `""` | inline values, stored in the release |

The service account is bound to a ClusterRole granting `get`/`list` on pods and
`get` on pod logs plus `create` on pod exec, which the agent uses to read pod
state, logs, and files during an investigation.
