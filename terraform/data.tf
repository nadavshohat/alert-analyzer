data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "aws_eks_cluster" "this" {
  name = var.cluster_name
}

# ClickHouse password from Kubernetes secret
data "kubernetes_secret" "clickhouse" {
  metadata {
    name      = "groundcover-clickhouse"
    namespace = var.namespace
  }
}

# Slack webhook URL from AWS Secrets Manager
data "aws_secretsmanager_secret_version" "slack" {
  secret_id = var.slack_webhook_secret_name
}

# Extract OIDC provider from EKS cluster
locals {
  oidc_provider     = replace(data.aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://", "")
  oidc_provider_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/${local.oidc_provider}"
}
