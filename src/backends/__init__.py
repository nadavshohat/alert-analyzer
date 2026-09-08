"""Backend selection. Each signal is chosen independently from config; instances
are memoized so signals that pick the same backend share one client/session."""
import logging
from functools import lru_cache

from config import config
from backends.base import (  # noqa: F401  re-export the shared contracts
    EventSource, LogSource, MetricSource, TraceSource, NullTraceSource,
)

logger = logging.getLogger(__name__)


@lru_cache(maxsize=None)
def _clickhouse():
    from clickhouse import ClickhouseClient
    return ClickhouseClient()


@lru_cache(maxsize=None)
def _kubernetes():
    from backends.kubernetes import KubernetesSource
    return KubernetesSource()


@lru_cache(maxsize=None)
def _loki():
    from backends.loki import LokiSource
    return LokiSource()


@lru_cache(maxsize=None)
def _prometheus():
    from backends.prometheus import PrometheusSource
    return PrometheusSource()


@lru_cache(maxsize=None)
def _null_traces():
    return NullTraceSource()


def build_event_source() -> EventSource:
    src = config.event_source
    if src == "kubernetes":
        return _kubernetes()
    if src not in ("clickhouse", ""):
        logger.warning(f"Unknown EVENT_SOURCE {src!r}, using clickhouse")
    return _clickhouse()


def build_log_source() -> LogSource:
    src = config.log_source
    if src == "kubernetes":
        return _kubernetes()
    if src == "loki":
        return _loki()
    if src not in ("clickhouse", ""):
        logger.warning(f"Unknown LOG_SOURCE {src!r}, using clickhouse")
    return _clickhouse()


def build_metric_source() -> MetricSource:
    src = config.metric_source
    if src == "kubernetes":
        return _kubernetes()
    if src == "prometheus":
        return _prometheus()
    if src not in ("clickhouse", ""):
        logger.warning(f"Unknown METRIC_SOURCE {src!r}, using clickhouse")
    return _clickhouse()


def build_trace_source() -> TraceSource:
    src = config.trace_source
    if src in ("none", "null"):
        return _null_traces()
    if src not in ("clickhouse", ""):
        logger.warning(f"Unknown TRACE_SOURCE {src!r}, using clickhouse")
    return _clickhouse()
