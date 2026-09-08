"""Kubernetes-native telemetry source: events, pod logs, metrics-server.

Needs no telemetry stack beyond the cluster API, so it runs anywhere. Two
limits are inherent, not bugs: core Event objects are retained about an hour
(so event polling sees a short window), and metrics-server exposes only current
usage (no history), so metrics are a point-in-time memory reading.
"""
import logging
from datetime import datetime, timezone
from typing import List, Optional

from clickhouse import CrashEvent, LogEntry, MetricsSummary
from config import config
from backends.base import load_core_v1_api, parse_level, to_utc, workload_from_pod

logger = logging.getLogger(__name__)

_MEM_UNITS = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
              "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}


def _mem_to_mib(q: str) -> float:
    """metrics-server memory quantity (e.g. '123456Ki', '512Mi', bytes) -> MiB."""
    q = (q or "").strip()
    if not q:
        return 0.0
    for unit in sorted(_MEM_UNITS, key=len, reverse=True):
        if q.endswith(unit):
            try:
                return float(q[:-len(unit)]) * _MEM_UNITS[unit] / (1024**2)
            except ValueError:
                return 0.0
    try:
        return float(q) / (1024**2)
    except ValueError:
        return 0.0


class KubernetesSource:
    def __init__(self, api=None):
        self._api = api
        self._custom = None

    @property
    def api(self):
        if self._api is None:
            self._api = load_core_v1_api()
        return self._api

    def get_crash_events(self, since_timestamp: Optional[datetime] = None) -> List[CrashEvent]:
        if not self.api:
            return []
        reasons = set(config.event_reasons)
        exclude = set(config.exclude_namespaces)
        try:
            resp = self.api.list_event_for_all_namespaces(field_selector="type=Warning")
        except Exception as e:
            logger.error(f"k8s event list failed: {e}")
            from clickhouse import BackendError
            raise BackendError(f"k8s event list failed: {e}") from e
        events = []
        for it in resp.items:
            if it.reason not in reasons:
                continue
            obj = it.involved_object
            ns = obj.namespace or ""
            if not ns or ns in exclude:
                continue
            ts = it.last_timestamp or it.event_time
            if ts is None:
                continue
            ts = to_utc(ts)
            if since_timestamp and ts <= to_utc(since_timestamp):
                continue
            pod_name = obj.name or ""
            workload = workload_from_pod(pod_name) if obj.kind == "Pod" else pod_name
            events.append(CrashEvent(
                timestamp=ts, namespace=ns, workload=workload,
                pod_name=pod_name, reason=it.reason, message=it.message or "",
            ))
        events.sort(key=lambda e: e.timestamp, reverse=True)
        logger.info(f"Found {len(events)} crash events (kubernetes)")
        return events[:100]

    def _pod_log_entries(self, namespace: str, pod_name: str, minutes: int) -> List[LogEntry]:
        lookback = minutes if minutes > 0 else config.log_lookback_minutes
        try:
            raw = self.api.read_namespaced_pod_log(
                name=pod_name, namespace=namespace, since_seconds=lookback * 60,
                timestamps=True, tail_lines=500,
            )
        except Exception as e:
            logger.debug(f"k8s log read failed for {namespace}/{pod_name}: {e}")
            return []
        entries = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            ts_str, _, body = line.partition(" ")
            try:
                ts = to_utc(datetime.fromisoformat(ts_str.replace("Z", "+00:00")))
            except ValueError:
                ts, body = datetime.now(timezone.utc), line
            entries.append(LogEntry(timestamp=ts, level=parse_level(body), body=body))
        return entries

    def get_logs_for_pod(self, namespace: str, pod_name: str, minutes: int = 0) -> List[LogEntry]:
        if not self.api:
            return []
        logs = self._pod_log_entries(namespace, pod_name, minutes)
        logs.sort(key=lambda l: l.timestamp)
        return logs[:200]

    def get_logs_for_workload(self, namespace: str, workload: str, minutes: int = 0) -> List[LogEntry]:
        if not self.api:
            return []
        try:
            pods = self.api.list_namespaced_pod(namespace=namespace).items
        except Exception as e:
            logger.debug(f"k8s pod list failed for {namespace}: {e}")
            return []
        names = [p.metadata.name for p in pods
                 if p.metadata.name == workload or p.metadata.name.startswith(workload + "-")]
        logs: List[LogEntry] = []
        for name in names[:5]:  # bound fan-out across a workload's pods
            logs.extend(self._pod_log_entries(namespace, name, minutes))
        logs.sort(key=lambda l: l.timestamp)
        return logs[:200]

    def get_metrics_for_pod(self, namespace: str, pod_name: str, minutes: int = 15) -> Optional[MetricsSummary]:
        if not self.api:
            return None
        if self._custom is None:
            from kubernetes import client
            self._custom = client.CustomObjectsApi(self.api.api_client)
        try:
            obj = self._custom.get_namespaced_custom_object(
                "metrics.k8s.io", "v1beta1", namespace, "pods", pod_name)
        except Exception as e:
            # 404 here usually means metrics-server is not installed.
            logger.debug(f"metrics-server unavailable for {namespace}/{pod_name}: {e}")
            return None
        mem_mib = sum(_mem_to_mib(c.get("usage", {}).get("memory", "0"))
                      for c in obj.get("containers", []))
        if mem_mib <= 0:
            return None
        # metrics-server is a single current sample with no percent-of-limit basis;
        # cpu is reported in cores, not the percent this summary expects, so leave
        # cpu at 0 and report memory, which is the OOM signal that matters.
        return MetricsSummary(
            samples=1, memory_max_mb=mem_mib, memory_avg_mb=mem_mib,
            memory_last_mb=mem_mib, cpu_max_pct=0.0, cpu_avg_pct=0.0,
            window_minutes=minutes,
        )
