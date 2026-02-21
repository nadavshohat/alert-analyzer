resource "helm_release" "alert_analyzer" {
  name             = "alert-analyzer"
  namespace        = var.namespace
  create_namespace = false
  chart            = "${path.module}/chart"

  values = [yamlencode({
    image = {
      repository = var.image_repository
      tag        = var.image_tag
    }

    bedrock = {
      region    = var.bedrock_region
      model     = var.bedrock_model
      maxTokens = var.bedrock_max_tokens
    }

    agent = {
      maxTurns           = var.max_agent_turns
      pollInterval       = var.poll_interval
      dedupWindow        = var.dedup_window
      logLookbackMinutes = var.log_lookback_minutes
    }

    events = {
      reasons           = var.event_reasons
      excludeNamespaces = var.exclude_namespaces
    }

    clickhouse = {
      host = var.clickhouse_host
    }

    groundcover = {
      baseUrl     = var.groundcover_base_url
      clusterName = var.cluster_name
    }

    timezone = var.timezone

    serviceAccount = {
      annotations = {
        "eks.amazonaws.com/role-arn" = aws_iam_role.alert_analyzer.arn
      }
    }

    secrets = {
      clickhousePassword = data.kubernetes_secret.clickhouse.data["admin-password"]
      slackWebhookUrl    = data.aws_secretsmanager_secret_version.slack.secret_string
    }
  })]
}
