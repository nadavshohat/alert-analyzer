"""Clickhouse queries for events and logs."""
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
import requests
from urllib.parse import quote

from config import config

logger = logging.getLogger(__name__)


@dataclass
class CrashEvent:
    """Represents a crash event from Clickhouse."""
    timestamp: datetime
    namespace: str
    workload: str
    pod_name: str
    reason: str
    message: str

    @property
    def key(self) -> str:
        """Unique key for deduplication."""
        return f"{self.namespace}/{self.workload}/{self.reason}"


@dataclass
class LogEntry:
    """Represents a log entry from Clickhouse."""
    timestamp: datetime
    level: str
    content: str


@dataclass
class TraceEntry:
    """Represents a trace/span from Clickhouse."""
    timestamp: datetime
    duration_seconds: float
    span_name: str
    status_code: str
    status: str


class ClickhouseClient:
    """Client for querying Groundcover Clickhouse."""

    def __init__(self):
        self.base_url = f"http://{config.clickhouse_host}:{config.clickhouse_port}"
        self.auth = (config.clickhouse_user, config.clickhouse_password)
        self.session = requests.Session()
        self.session.auth = self.auth
        self._consecutive_failures = 0

    def _execute_query(self, query: str, params: Optional[dict] = None) -> dict:
        """Execute a Clickhouse query and return JSON results."""
        url = f"{self.base_url}/"
        query_params = {"query": query + " FORMAT JSON"}
        if params:
            for k, v in params.items():
                query_params[f"param_{k}"] = v
        try:
            response = self.session.get(url, params=query_params, timeout=30)
            response.raise_for_status()
            self._consecutive_failures = 0
            return response.json()
        except requests.RequestException as e:
            self._consecutive_failures += 1
            logger.error(f"Clickhouse query failed (consecutive: {self._consecutive_failures}): {e}")
            raise

    def get_crash_events(self, since_timestamp: Optional[datetime] = None) -> List[CrashEvent]:
        """Poll for crash events from the events table."""
        reasons_list = ",".join(f"'{r}'" for r in config.event_reasons)
        exclude_ns = ",".join(f"'{ns}'" for ns in config.exclude_namespaces)

        # Use provided timestamp or default to 1 minute ago
        time_filter = "now() - INTERVAL 1 MINUTE"
        if since_timestamp:
            time_filter = f"toDateTime64('{since_timestamp.strftime('%Y-%m-%d %H:%M:%S')}', 9)"

        query = f"""
        SELECT
            timestamp,
            entity_namespace,
            entity_workload,
            entity_name,
            reason,
            message
        FROM {config.clickhouse_database}.events
        WHERE type = 'Warning'
          AND reason IN ({reasons_list})
          AND timestamp > {time_filter}
          AND entity_namespace NOT IN ({exclude_ns})
          AND entity_namespace != ''
        ORDER BY timestamp DESC
        LIMIT 100
        """

        try:
            result = self._execute_query(query)
            events = []
            for row in result.get('data', []):
                events.append(CrashEvent(
                    timestamp=datetime.fromisoformat(row['timestamp'].replace(' ', 'T')),
                    namespace=row['entity_namespace'],
                    workload=row['entity_workload'],
                    pod_name=row['entity_name'],
                    reason=row['reason'],
                    message=row['message']
                ))
            logger.info(f"Found {len(events)} crash events")
            return events
        except Exception as e:
            logger.error(f"Failed to get crash events: {e}")
            return []

    def get_logs_for_workload(self, namespace: str, workload: str, minutes: int = 0) -> List[LogEntry]:
        """Fetch recent logs for a workload."""
        lookback = minutes if minutes > 0 else config.log_lookback_minutes
        query = f"""
        SELECT
            timestamp,
            level,
            content
        FROM {config.clickhouse_database}.logs
        WHERE namespace = {{ns:String}}
          AND workload = {{wl:String}}
          AND timestamp > now() - INTERVAL {lookback} MINUTE
        ORDER BY timestamp DESC
        LIMIT 200
        """

        try:
            result = self._execute_query(query, {"ns": namespace, "wl": workload})
            logs = []
            for row in result.get('data', []):
                logs.append(LogEntry(
                    timestamp=datetime.fromisoformat(row['timestamp'].replace(' ', 'T')),
                    level=row['level'],
                    content=row['content']
                ))
            logger.info(f"Found {len(logs)} log entries for {namespace}/{workload}")
            return logs
        except Exception as e:
            logger.error(f"Failed to get logs for {namespace}/{workload}: {e}")
            return []

    def get_logs_for_pod(self, namespace: str, pod_name: str, minutes: int = 0) -> List[LogEntry]:
        """Fetch recent logs for a specific pod."""
        lookback = minutes if minutes > 0 else config.log_lookback_minutes
        query = f"""
        SELECT
            timestamp,
            level,
            content
        FROM {config.clickhouse_database}.logs
        WHERE namespace = {{ns:String}}
          AND pod_name = {{pod:String}}
          AND timestamp > now() - INTERVAL {lookback} MINUTE
        ORDER BY timestamp DESC
        LIMIT 200
        """

        try:
            result = self._execute_query(query, {"ns": namespace, "pod": pod_name})
            logs = []
            for row in result.get('data', []):
                logs.append(LogEntry(
                    timestamp=datetime.fromisoformat(row['timestamp'].replace(' ', 'T')),
                    level=row['level'],
                    content=row['content']
                ))
            logger.info(f"Found {len(logs)} log entries for pod {namespace}/{pod_name}")
            return logs
        except Exception as e:
            logger.error(f"Failed to get logs for pod {namespace}/{pod_name}: {e}")
            return []

    def get_slow_traces(self, namespace: str, workload: str) -> List[TraceEntry]:
        """Fetch slowest traces for a workload (sorted by latency desc)."""
        query = f"""
        SELECT
            start_timestamp,
            duration_seconds,
            span_name,
            return_code,
            status
        FROM {config.clickhouse_database}.traces
        WHERE namespace = {{ns:String}}
          AND workload = {{wl:String}}
          AND start_timestamp > now() - INTERVAL {config.log_lookback_minutes} MINUTE
        ORDER BY duration_seconds DESC
        LIMIT 20
        """

        try:
            result = self._execute_query(query, {"ns": namespace, "wl": workload})
            traces = []
            for row in result.get('data', []):
                traces.append(TraceEntry(
                    timestamp=datetime.fromisoformat(row['start_timestamp'].replace(' ', 'T')),
                    duration_seconds=float(row['duration_seconds']),
                    span_name=row['span_name'],
                    status_code=row['return_code'],
                    status=row['status']
                ))
            logger.info(f"Found {len(traces)} traces for {namespace}/{workload}")
            return traces
        except Exception as e:
            logger.error(f"Failed to get traces for {namespace}/{workload}: {e}")
            return []
