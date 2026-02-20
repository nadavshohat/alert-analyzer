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

    # Polling
    poll_interval_seconds: int = field(default_factory=lambda: int(os.environ.get('POLL_INTERVAL_SECONDS', '30')))
    dedup_window_seconds: int = field(default_factory=lambda: int(os.environ.get('DEDUP_WINDOW_SECONDS', '300')))
    log_lookback_minutes: int = field(default_factory=lambda: int(os.environ.get('LOG_LOOKBACK_MINUTES', '30')))

    # Filtering
    exclude_namespaces: List[str] = field(default_factory=lambda: os.environ.get(
        'EXCLUDE_NAMESPACES', 'kube-system,groundcover'
    ).split(','))
    event_reasons: List[str] = field(default_factory=lambda: os.environ.get(
        'EVENT_REASONS', 'CrashLoopBackOff,OOMKilled,BackOff,Failed,Error,Unhealthy'
    ).split(','))

    # Bedrock
    bedrock_region: str = field(default_factory=lambda: os.environ.get('BEDROCK_REGION', 'us-west-2'))
    bedrock_model: str = field(default_factory=lambda: os.environ.get(
        'BEDROCK_MODEL', 'us.anthropic.claude-opus-4-6-v1'
    ))
    bedrock_max_tokens: int = field(default_factory=lambda: int(os.environ.get('BEDROCK_MAX_TOKENS', '2048')))
    max_agent_turns: int = field(default_factory=lambda: int(os.environ.get('MAX_AGENT_TURNS', '10')))

    # Slack
    slack_webhook_url: str = field(default_factory=lambda: os.environ.get('SLACK_WEBHOOK_URL', ''))

    # Groundcover UI
    groundcover_base_url: str = field(default_factory=lambda: os.environ.get(
        'GROUNDCOVER_BASE_URL', 'https://app.groundcover.com'
    ))
    groundcover_tenant_uuid: str = field(default_factory=lambda: os.environ.get('GROUNDCOVER_TENANT_UUID', ''))

    # Cluster info
    cluster_name: str = field(default_factory=lambda: os.environ.get('CLUSTER_NAME', ''))
    timezone: str = field(default_factory=lambda: os.environ.get('TZ', 'UTC'))



config = Config()
