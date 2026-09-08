"""Telemetry source contracts and shared helpers.

The analyzer reads four signals that are not tied to one backend: crash events
(the trigger), logs, metrics, and traces. Each is a separate source so a stack
can mix backends. The dataclasses these return (CrashEvent, LogEntry,
MetricsSummary, TraceEntry) and BackendError live in `clickhouse` and predate
this seam; they are the shared wire types every source produces.
"""
import logging
import re
from datetime import datetime, timezone
from typing import List, Optional, Protocol, runtime_checkable

from clickhouse import CrashEvent, LogEntry, MetricsSummary, TraceEntry  # noqa: F401

logger = logging.getLogger(__name__)


@runtime_checkable
class EventSource(Protocol):
    def get_crash_events(self, since_timestamp: Optional[datetime] = None) -> List[CrashEvent]: ...


@runtime_checkable
class LogSource(Protocol):
    def get_logs_for_workload(self, namespace: str, workload: str, minutes: int = 0) -> List[LogEntry]: ...
    def get_logs_for_pod(self, namespace: str, pod_name: str, minutes: int = 0) -> List[LogEntry]: ...


@runtime_checkable
class MetricSource(Protocol):
    def get_metrics_for_pod(self, namespace: str, pod_name: str, minutes: int = 15) -> Optional[MetricsSummary]: ...


@runtime_checkable
class TraceSource(Protocol):
    def get_slow_traces(self, namespace: str, workload: str) -> List[TraceEntry]: ...


class NullTraceSource:
    """Used when no backend has traces (TRACE_SOURCE=none). Honest empty result."""

    def get_slow_traces(self, namespace: str, workload: str) -> List[TraceEntry]:
        return []


def load_core_v1_api():
    """Load a Kubernetes CoreV1Api (in-cluster first, then kubeconfig), or None."""
    try:
        from kubernetes import client, config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
        return client.CoreV1Api()
    except Exception as e:
        logger.warning(f"Could not load kubernetes config: {e}")
        return None


# A pod name is <workload>-<replicaset-hash>-<pod-suffix> for Deployments, or
# <workload>-<ordinal> for StatefulSets/DaemonSets. Groundcover reports the
# workload directly; when a source only has the pod name we derive it so the
# dedup key (namespace/workload/reason) stays stable across a workload's pods.
_RS_POD_SUFFIX = re.compile(r"-[a-z0-9]{6,10}-[a-z0-9]{5}$")
_ORDINAL_SUFFIX = re.compile(r"-\d+$")


def workload_from_pod(pod_name: str) -> str:
    stripped = _RS_POD_SUFFIX.sub("", pod_name)
    if stripped != pod_name:
        return stripped
    return _ORDINAL_SUFFIX.sub("", pod_name)


_LEVEL_RE = re.compile(r"\b(fatal|error|warn(?:ing)?|info|debug|trace)\b", re.IGNORECASE)


def parse_level(line: str) -> str:
    """Best-effort log level from a raw line (sources without a level field)."""
    m = _LEVEL_RE.search(line[:200])
    return m.group(1).lower() if m else "info"


def to_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
