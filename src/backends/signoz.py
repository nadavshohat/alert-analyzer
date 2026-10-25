"""SigNoz telemetry source (EVENT/LOG/METRIC/TRACE_SOURCE=signoz).

Reads through SigNoz's query API (POST /api/v5/query_range, header
SIGNOZ-API-KEY), not its ClickHouse tables, whose layout SigNoz changes between
releases. Kubernetes events arrive in SigNoz as log records from the k8s-infra
chart's event receiver: reason in attribute k8s.event.reason, the involved
object in resource k8s.object.name / k8s.object.kind.

Filter expressions are built by string interpolation, so every name is checked
with validate_k8s_name first, including names read back from SigNoz.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from clickhouse import BackendError, CrashEvent, LogEntry, MetricsSummary, TraceEntry
from config import config
from backends.base import parse_level, to_utc, validate_k8s_name, workload_from_pod

logger = logging.getLogger(__name__)

# k8s event records reach SigNoz after a collector batch; look back past the
# poll watermark by this much and let the caller's dedup drop repeats.
_INGEST_LAG = timedelta(minutes=2)

_REASON_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")


def _field(name: str, context: str) -> Dict[str, str]:
    return {"name": name, "fieldContext": context}


_WORKLOAD_KEYS = ("k8s.deployment.name", "k8s.statefulset.name", "k8s.daemonset.name", "k8s.cronjob.name")
_KEYS_TTL = 600


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


class SignozSource:
    def __init__(self, base_url: str = "", api_key: str = ""):
        self.base_url = (base_url or config.signoz_url).rstrip("/")
        self.api_key = api_key or config.signoz_api_key
        self.session = requests.Session()
        self._keys: Dict[str, Any] = {}

    def _known_keys(self, signal: str) -> set:
        """k8s.* field keys SigNoz has ingested for a signal. SigNoz rejects a
        filter on a key it has never seen, so filters use only these."""
        cached = self._keys.get(signal)
        if cached and time.monotonic() - cached[0] < _KEYS_TTL:
            return cached[1]
        try:
            resp = self.session.get(
                f"{self.base_url}/api/v1/fields/keys", params={"signal": signal, "searchText": "k8s."},
                headers={"SIGNOZ-API-KEY": self.api_key}, timeout=30)
            resp.raise_for_status()
            keys = set(((resp.json().get("data") or {}).get("keys") or {}).keys())
        except (requests.RequestException, ValueError) as e:
            raise BackendError(f"SigNoz field keys failed: {e}") from e
        self._keys[signal] = (time.monotonic(), keys)
        return keys

    def _workload_match(self, signal: str, wl: str) -> str:
        """Filter matching a workload by its controller name (wl is pre-validated)."""
        keys = [k for k in _WORKLOAD_KEYS if k in self._known_keys(signal)]
        if not keys:
            raise BackendError(f"SigNoz {signal} carry no workload name fields (k8sattributes processor)")
        return "(" + " OR ".join(f"{k} = '{wl}'" for k in keys) + ")"

    def _query(self, request_type: str, spec: Dict[str, Any], minutes: int) -> Dict[str, Any]:
        if not self.base_url:
            raise BackendError("SIGNOZ_URL is not set")
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=minutes)
        body = {
            "schemaVersion": "v1",
            "start": int(start.timestamp() * 1000),
            "end": int(end.timestamp() * 1000),
            "requestType": request_type,
            "compositeQuery": {"queries": [{"type": "builder_query", "spec": {"name": "A", **spec}}]},
        }
        try:
            resp = self.session.post(
                f"{self.base_url}/api/v5/query_range", json=body,
                headers={"SIGNOZ-API-KEY": self.api_key}, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.error(f"SigNoz query failed: {e}")
            raise BackendError(f"SigNoz query failed: {e}") from e
        if payload.get("status") != "success":
            raise BackendError(f"SigNoz query failed: {payload.get('error')}")
        results = (payload.get("data") or {}).get("data", {}).get("results") or []
        return results[0] if results else {}

    # ---- events ----

    def get_crash_events(self, since_timestamp: Optional[datetime] = None) -> List[CrashEvent]:
        reasons = [r for r in config.event_reasons if r and set(r) <= _REASON_OK]
        if not reasons:
            return []
        reason_list = ", ".join(f"'{r}'" for r in reasons)
        result = self._query("raw", {
            "signal": "logs",
            # signoz.component narrows to records from the k8s-infra event
            # collector. Any OTLP sender can still set these attributes, so
            # EVENT_SOURCE=kubernetes is the trusted choice where it fits.
            "filter": {"expression": f"k8s.event.reason IN ({reason_list}) AND "
                                     f"severity_text = 'Warning' AND k8s.object.kind = 'Pod' AND "
                                     f"signoz.component = 'otel-deployment'"},
            "selectFields": [
                _field("body", "log"),
                _field("k8s.event.reason", "attribute"),
                _field("k8s.namespace.name", "attribute"),
                _field("k8s.object.name", "resource"),
            ],
            "order": [{"key": _field("timestamp", "log"), "direction": "desc"}],
            "limit": 200,
        }, minutes=max(config.poll_interval_seconds // 60 * 4, 10))
        exclude = set(config.exclude_namespaces)
        events = []
        for row in result.get("rows") or []:
            data = row.get("data") or {}
            ts = _parse_ts(row.get("timestamp"))
            if ts is None:
                continue
            if since_timestamp and ts <= to_utc(since_timestamp) - _INGEST_LAG:
                continue
            try:
                ns = validate_k8s_name(str(data.get("k8s.namespace.name") or ""), "namespace")
                pod = validate_k8s_name(str(data.get("k8s.object.name") or ""), "pod name")
            except BackendError:
                logger.warning(f"Skipping SigNoz event with invalid object: {data}")
                continue
            if ns in exclude:
                continue
            events.append(CrashEvent(
                timestamp=ts, namespace=ns, workload=workload_from_pod(pod),
                pod_name=pod, reason=str(data.get("k8s.event.reason") or ""),
                message=str(data.get("body") or ""),
            ))
        logger.info(f"Found {len(events)} crash events (signoz)")
        return events

    # ---- logs ----

    def _logs(self, expression: str, minutes: int) -> List[LogEntry]:
        lookback = minutes if minutes > 0 else config.log_lookback_minutes
        result = self._query("raw", {
            "signal": "logs",
            "filter": {"expression": expression},
            "selectFields": [_field("body", "log"), _field("severity_text", "log")],
            "order": [{"key": _field("timestamp", "log"), "direction": "desc"}],
            "limit": 200,
        }, minutes=lookback)
        entries = []
        for row in result.get("rows") or []:
            ts = _parse_ts(row.get("timestamp"))
            if ts is None:
                continue
            data = row.get("data") or {}
            body = str(data.get("body") or "")
            level = str(data.get("severity_text") or "").lower() or parse_level(body)
            entries.append(LogEntry(timestamp=ts, level=level, body=body))
        entries.sort(key=lambda l: l.timestamp)
        logger.info(f"Found {len(entries)} log entries (signoz)")
        return entries

    def get_logs_for_pod(self, namespace: str, pod_name: str, minutes: int = 0) -> List[LogEntry]:
        ns = validate_k8s_name(namespace, "namespace")
        pod = validate_k8s_name(pod_name, "pod name")
        return self._logs(f"k8s.namespace.name = '{ns}' AND k8s.pod.name = '{pod}'", minutes)

    def get_logs_for_workload(self, namespace: str, workload: str, minutes: int = 0) -> List[LogEntry]:
        ns = validate_k8s_name(namespace, "namespace")
        wl = validate_k8s_name(workload, "workload")
        return self._logs(f"k8s.namespace.name = '{ns}' AND {self._workload_match('logs', wl)}", minutes)

    # ---- metrics ----

    def _series_mb_or_pct(self, metric: str, ns: str, pod: str, minutes: int) -> List[float]:
        result = self._query("time_series", {
            "signal": "metrics",
            "aggregations": [{"metricName": metric, "timeAggregation": "avg", "spaceAggregation": "sum"}],
            "stepInterval": "60s",
            "filter": {"expression": f"k8s.namespace.name = '{ns}' AND k8s.pod.name = '{pod}'"},
        }, minutes=minutes)
        values: List[float] = []
        for bucket in result.get("aggregations") or []:
            for series in bucket.get("series") or []:
                for v in series.get("values") or []:
                    try:
                        values.append(float(v.get("value")))
                    except (TypeError, ValueError):
                        continue
        return values

    def get_metrics_for_pod(self, namespace: str, pod_name: str, minutes: int = 15) -> Optional[MetricsSummary]:
        ns = validate_k8s_name(namespace, "namespace")
        pod = validate_k8s_name(pod_name, "pod name")
        mem = [v / (1024**2) for v in self._series_mb_or_pct("k8s.pod.memory.working_set", ns, pod, minutes)]
        if not mem:
            logger.info(f"No metrics for {namespace}/{pod_name} (signoz)")
            return None
        # k8s.pod.cpu_limit_utilization is a 0-1 ratio of the pod's CPU limit;
        # empty when the pod has no limit, which leaves cpu at 0.
        cpu = [v * 100 for v in self._series_mb_or_pct("k8s.pod.cpu_limit_utilization", ns, pod, minutes)]
        return MetricsSummary(
            samples=len(mem), memory_max_mb=max(mem), memory_avg_mb=sum(mem) / len(mem),
            memory_last_mb=mem[-1], cpu_max_pct=max(cpu) if cpu else 0.0,
            cpu_avg_pct=sum(cpu) / len(cpu) if cpu else 0.0, window_minutes=minutes,
        )

    # ---- traces ----

    def get_slow_traces(self, namespace: str, workload: str) -> List[TraceEntry]:
        ns = validate_k8s_name(namespace, "namespace")
        wl = validate_k8s_name(workload, "workload")
        match = self._workload_match("traces", wl)
        result = self._query("raw", {
            "signal": "traces",
            "filter": {"expression": f"k8s.namespace.name = '{ns}' AND {match}"},
            "selectFields": [
                _field("name", "span"), _field("duration_nano", "span"),
                _field("status_code", "span"), _field("status_message", "span"),
            ],
            "order": [{"key": _field("duration_nano", "span"), "direction": "desc"}],
            "limit": 20,
        }, minutes=config.log_lookback_minutes)
        traces = []
        for row in result.get("rows") or []:
            ts = _parse_ts(row.get("timestamp"))
            data = row.get("data") or {}
            try:
                duration = float(data.get("duration_nano") or 0) / 1e9
            except (TypeError, ValueError):
                duration = 0.0
            if ts is None:
                continue
            traces.append(TraceEntry(
                timestamp=ts, duration_seconds=duration, span_name=str(data.get("name") or ""),
                status_code=str(data.get("status_code") or ""), status=str(data.get("status_message") or ""),
            ))
        return traces
