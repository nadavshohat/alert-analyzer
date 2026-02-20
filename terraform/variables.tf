# Required
variable "cluster_name" {
  description = "EKS cluster name"
  type        = string
}

variable "groundcover_tenant_uuid" {
  description = "Groundcover tenant UUID"
  type        = string
}

# Optional — override defaults
variable "namespace" {
  description = "Kubernetes namespace"
  type        = string
  default     = "groundcover"
}

variable "bedrock_region" {
  description = "AWS region for Bedrock API"
  type        = string
  default     = "us-west-2"
}

variable "bedrock_model" {
  description = "Bedrock model ID"
  type        = string
  default     = "us.anthropic.claude-opus-4-6-v1"
}

variable "bedrock_max_tokens" {
  description = "Max tokens for Bedrock response"
  type        = number
  default     = 2048
}

variable "max_agent_turns" {
  description = "Max agentic turns per analysis"
  type        = number
  default     = 10
}

variable "poll_interval" {
  description = "Polling interval in seconds"
  type        = number
  default     = 30
}

variable "dedup_window" {
  description = "Deduplication window in seconds"
  type        = number
  default     = 1800
}

variable "log_lookback_minutes" {
  description = "Minutes of logs to fetch"
  type        = number
  default     = 30
}

variable "event_reasons" {
  description = "Comma-separated event reasons to watch"
  type        = string
  default     = "CrashLoopBackOff,OOMKilled,BackOff,Failed,Error,Unhealthy"
}

variable "exclude_namespaces" {
  description = "Comma-separated namespaces to exclude"
  type        = string
  default     = "kube-system,groundcover"
}

variable "image_tag" {
  description = "Docker image tag"
  type        = string
  default     = "latest"
}

variable "slack_webhook_secret_name" {
  description = "AWS Secrets Manager secret name for Slack webhook URL"
  type        = string
  default     = "alert-analyzer/slack-webhook-url"
}

variable "clickhouse_host" {
  description = "ClickHouse service hostname"
  type        = string
  default     = "groundcover-clickhouse"
}

variable "groundcover_base_url" {
  description = "Groundcover UI base URL"
  type        = string
  default     = "https://app.groundcover.com"
}

variable "timezone" {
  description = "Timezone for the container"
  type        = string
  default     = "Asia/Jerusalem"
}
