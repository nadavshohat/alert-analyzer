"""Configuration for Alert Analyzer."""
import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class Config:
    # Clickhouse
    clickhouse_host: str = field(default_factory=lambda: os.environ.get('CLICKHOUSE_HOST', 'groundcover-clickhouse'))
    clickhouse_port: int = field(default_factory=lambda: int(os.environ.get('CLICKHOUSE_PORT', '8123')))
    clickhouse_user: str = field(default_factory=lambda: os.environ.get('CLICKHOUSE_USER', 'default'))
    clickhouse_password: str = field(default_factory=lambda: os.environ.get('CLICKHOUSE_PASSWORD', ''))
    clickhouse_database: str = field(default_factory=lambda: os.environ.get('CLICKHOUSE_DATABASE', 'groundcover'))

    # Telemetry source selection. Each signal is chosen independently so a stack
    # can mix backends (e.g. Kubernetes events + Loki logs + Prometheus metrics).
    # Defaults are all-clickhouse (Groundcover), so existing deployments are unchanged.
    #   EVENT_SOURCE  : clickhouse | kubernetes
    #   LOG_SOURCE    : clickhouse | kubernetes | loki
    #   METRIC_SOURCE : clickhouse | kubernetes | prometheus
    #   TRACE_SOURCE  : clickhouse | none   (only clickhouse/Groundcover has traces)
    event_source: str = field(default_factory=lambda: os.environ.get('EVENT_SOURCE', 'clickhouse').strip().lower())
    log_source: str = field(default_factory=lambda: os.environ.get('LOG_SOURCE', 'clickhouse').strip().lower())
    metric_source: str = field(default_factory=lambda: os.environ.get('METRIC_SOURCE', 'clickhouse').strip().lower())
    trace_source: str = field(default_factory=lambda: os.environ.get('TRACE_SOURCE', 'clickhouse').strip().lower())

    # Loki (LOG_SOURCE=loki). LOKI_TENANT sets X-Scope-OrgID for multi-tenant Loki.
    loki_url: str = field(default_factory=lambda: os.environ.get('LOKI_URL', '').rstrip('/'))
    loki_tenant: str = field(default_factory=lambda: os.environ.get('LOKI_TENANT', ''))
    # Prometheus (METRIC_SOURCE=prometheus).
    prometheus_url: str = field(default_factory=lambda: os.environ.get('PROMETHEUS_URL', '').rstrip('/'))

    # Polling
    poll_interval_seconds: int = field(default_factory=lambda: int(os.environ.get('POLL_INTERVAL_SECONDS', '30')))
    dedup_window_seconds: int = field(default_factory=lambda: int(os.environ.get('DEDUP_WINDOW_SECONDS', '300')))
    log_lookback_minutes: int = field(default_factory=lambda: int(os.environ.get('LOG_LOOKBACK_MINUTES', '30')))

    # Filtering
    exclude_namespaces: List[str] = field(default_factory=lambda: os.environ.get(
        'EXCLUDE_NAMESPACES', ''
    ).split(',') if os.environ.get('EXCLUDE_NAMESPACES', '') else [])
    event_reasons: List[str] = field(default_factory=lambda: os.environ.get(
        'EVENT_REASONS', 'CrashLoopBackOff,OOMKilled,BackOff,Failed,Error,Unhealthy'
    ).split(','))
    unhealthy_skip_namespaces: List[str] = field(default_factory=lambda: os.environ.get(
        'UNHEALTHY_SKIP_NAMESPACES', 'kube-system,groundcover,istio-system,external-secrets,kubescape'
    ).split(','))

    # Bedrock
    bedrock_region: str = field(default_factory=lambda: os.environ.get('BEDROCK_REGION', 'us-west-2'))
    bedrock_model: str = field(default_factory=lambda: os.environ.get(
        'BEDROCK_MODEL', 'us.anthropic.claude-sonnet-5'
    ))
    bedrock_max_tokens: int = field(default_factory=lambda: int(os.environ.get('BEDROCK_MAX_TOKENS', '2048')))
    max_agent_turns: int = field(default_factory=lambda: int(os.environ.get('MAX_AGENT_TURNS', '20')))

    # Slack
    slack_webhook_url: str = field(default_factory=lambda: os.environ.get('SLACK_WEBHOOK_URL', ''))

    # Groundcover UI
    groundcover_base_url: str = field(default_factory=lambda: os.environ.get(
        'GROUNDCOVER_BASE_URL', 'https://app.groundcover.com'
    ))
    # Cluster info
    cluster_name: str = field(default_factory=lambda: os.environ.get('CLUSTER_NAME', ''))
    timezone: str = field(default_factory=lambda: os.environ.get('TZ', 'UTC'))



config = Config()
