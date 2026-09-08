"""Loki log source (LOG_SOURCE=loki). Logs only; query_range over LogQL."""
import logging
from datetime import datetime, timedelta, timezone
from typing import List

import requests

from clickhouse import BackendError, LogEntry
from config import config
from backends.base import parse_level

logger = logging.getLogger(__name__)


class LokiSource:
    def __init__(self, base_url: str = "", tenant: str = ""):
        self.base_url = (base_url or config.loki_url).rstrip("/")
        self.tenant = tenant or config.loki_tenant
        self.session = requests.Session()

    def _query_range(self, logql: str, minutes: int) -> List[LogEntry]:
        if not self.base_url:
            raise BackendError("LOKI_URL is not set")
        lookback = minutes if minutes > 0 else config.log_lookback_minutes
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=lookback)
        params = {
            "query": logql,
            "start": str(int(start.timestamp() * 1e9)),
            "end": str(int(end.timestamp() * 1e9)),
            "limit": "200",
            "direction": "backward",
        }
        headers = {"X-Scope-OrgID": self.tenant} if self.tenant else {}
        try:
            resp = self.session.get(
                f"{self.base_url}/loki/api/v1/query_range",
                params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.error(f"Loki query failed: {e}")
            raise BackendError(f"Loki query failed: {e}") from e
        entries = []
        for stream in data.get("data", {}).get("result", []):
            for ns_ts, line in stream.get("values", []):
                ts = datetime.fromtimestamp(int(ns_ts) / 1e9, tz=timezone.utc)
                entries.append(LogEntry(timestamp=ts, level=parse_level(line), body=line))
        entries.sort(key=lambda l: l.timestamp)
        logger.info(f"Found {len(entries)} log entries (loki)")
        return entries[:200]

    def get_logs_for_pod(self, namespace: str, pod_name: str, minutes: int = 0) -> List[LogEntry]:
        return self._query_range(f'{{namespace="{namespace}",pod="{pod_name}"}}', minutes)

    def get_logs_for_workload(self, namespace: str, workload: str, minutes: int = 0) -> List[LogEntry]:
        return self._query_range(f'{{namespace="{namespace}",pod=~"{workload}.*"}}', minutes)
