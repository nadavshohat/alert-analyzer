"""Unit tests for the pluggable telemetry sources and the selection factory."""
import os
import sys
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import backends  # noqa: E402
from backends import base  # noqa: E402
from backends.kubernetes import KubernetesSource, _mem_to_mib  # noqa: E402
from backends.loki import LokiSource  # noqa: E402
from backends.prometheus import PrometheusSource  # noqa: E402
from clickhouse import BackendError  # noqa: E402
from config import config  # noqa: E402

NOW = datetime.now(timezone.utc)


# ---- helpers ----

def test_workload_from_pod_strips_replicaset_and_ordinal():
    assert base.workload_from_pod("api-gw-7d9f8c6b5c-x2k9p") == "api-gw"
    assert base.workload_from_pod("postgres-0") == "postgres"
    assert base.workload_from_pod("standalone") == "standalone"


def test_mem_to_mib_units():
    assert _mem_to_mib("1048576Ki") == 1024.0
    assert _mem_to_mib("512Mi") == 512.0
    assert round(_mem_to_mib("1Gi")) == 1024
    assert _mem_to_mib("") == 0.0


# ---- factory selection (defaults unchanged = clickhouse) ----

def _select(monkeypatch, **kw):
    for k, v in kw.items():
        monkeypatch.setattr(config, k, v)


def test_factory_defaults_are_clickhouse(monkeypatch):
    _select(monkeypatch, event_source="clickhouse", log_source="clickhouse",
            metric_source="clickhouse", trace_source="clickhouse")
    assert type(backends.build_event_source()).__name__ == "ClickhouseClient"
    assert type(backends.build_log_source()).__name__ == "ClickhouseClient"
    assert type(backends.build_metric_source()).__name__ == "ClickhouseClient"
    assert type(backends.build_trace_source()).__name__ == "ClickhouseClient"


def test_factory_selects_alternate_backends(monkeypatch):
    _select(monkeypatch, event_source="kubernetes", log_source="loki",
            metric_source="prometheus", trace_source="none")
    assert type(backends.build_event_source()).__name__ == "KubernetesSource"
    assert type(backends.build_log_source()).__name__ == "LokiSource"
    assert type(backends.build_metric_source()).__name__ == "PrometheusSource"
    assert type(backends.build_trace_source()).__name__ == "NullTraceSource"


def test_unknown_source_falls_back_to_clickhouse(monkeypatch):
    _select(monkeypatch, log_source="splunk")
    assert type(backends.build_log_source()).__name__ == "ClickhouseClient"


# ---- KubernetesSource ----

def _event(reason, ns, name, kind="Pod", ts=None):
    return types.SimpleNamespace(
        reason=reason, type="Warning", message="boom",
        last_timestamp=ts or NOW, event_time=None,
        involved_object=types.SimpleNamespace(namespace=ns, name=name, kind=kind))


def _k8s_api(events=None, log="", pods=None):
    return types.SimpleNamespace(
        list_event_for_all_namespaces=lambda field_selector=None: types.SimpleNamespace(items=events or []),
        read_namespaced_pod_log=lambda **kw: log,
        list_namespaced_pod=lambda namespace: types.SimpleNamespace(items=pods or []),
    )


def test_k8s_events_map_and_derive_workload():
    ev = _event("CrashLoopBackOff", "team-a", "api-gw-7d9f8c6b5c-x2k9p")
    src = KubernetesSource(api=_k8s_api(events=[ev]))
    out = src.get_crash_events()
    assert len(out) == 1
    assert out[0].namespace == "team-a" and out[0].workload == "api-gw"
    assert out[0].pod_name == "api-gw-7d9f8c6b5c-x2k9p" and out[0].reason == "CrashLoopBackOff"


def test_k8s_events_filter_reason_and_since():
    keep = _event("OOMKilled", "team-a", "p-1", ts=NOW)
    drop_reason = _event("SomethingElse", "team-a", "p-2", ts=NOW)
    drop_old = _event("OOMKilled", "team-a", "p-3", ts=NOW - timedelta(hours=2))
    src = KubernetesSource(api=_k8s_api(events=[keep, drop_reason, drop_old]))
    out = src.get_crash_events(since_timestamp=NOW - timedelta(minutes=10))
    assert [e.pod_name for e in out] == ["p-1"]


def test_k8s_pod_logs_parse_timestamped_lines():
    log = "2026-09-08T10:00:00.000Z starting up\n2026-09-08T10:00:01.000Z ERROR boom\n"
    src = KubernetesSource(api=_k8s_api(log=log))
    logs = src.get_logs_for_pod("team-a", "p-1")
    assert len(logs) == 2
    assert logs[1].level == "error" and "boom" in logs[1].body


def test_k8s_metrics_sum_containers_memory():
    src = KubernetesSource(api=_k8s_api())
    src._custom = types.SimpleNamespace(get_namespaced_custom_object=lambda *a, **k: {
        "containers": [{"usage": {"memory": "512Mi"}}, {"usage": {"memory": "512Mi"}}]})
    m = src.get_metrics_for_pod("team-a", "p-1")
    assert m.memory_max_mb == 1024.0 and m.cpu_max_pct == 0.0


def test_k8s_metrics_none_when_server_absent():
    src = KubernetesSource(api=_k8s_api())
    def boom(*a, **k):
        raise RuntimeError("404 metrics.k8s.io not found")
    src._custom = types.SimpleNamespace(get_namespaced_custom_object=boom)
    assert src.get_metrics_for_pod("team-a", "p-1") is None


# ---- LokiSource ----

class _Resp:
    def __init__(self, payload):
        self._p = payload
    def raise_for_status(self): pass
    def json(self): return self._p


def test_loki_maps_streams_to_log_entries(monkeypatch):
    payload = {"data": {"result": [
        {"stream": {"pod": "p-1"}, "values": [
            ["1757325600000000000", "line one"],
            ["1757325601000000000", "ERROR line two"]]}]}}
    src = LokiSource(base_url="http://loki:3100")
    captured = {}
    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url; captured["query"] = params["query"]; return _Resp(payload)
    monkeypatch.setattr(src.session, "get", fake_get)
    logs = src.get_logs_for_pod("team-a", "p-1")
    assert captured["url"].endswith("/loki/api/v1/query_range")
    assert captured["query"] == '{namespace="team-a",pod="p-1"}'
    assert len(logs) == 2 and logs[1].level == "error"


def test_loki_without_url_raises_backend_error():
    src = LokiSource(base_url="")
    try:
        src.get_logs_for_pod("team-a", "p-1")
        assert False, "expected BackendError"
    except BackendError:
        pass


# ---- PrometheusSource ----

def test_prometheus_aggregates_container_memory(monkeypatch):
    payload = {"data": {"result": [
        {"metric": {"container": "a"}, "values": [[1, str(256*1024**2)], [2, str(512*1024**2)]]},
        {"metric": {"container": "b"}, "values": [[1, str(128*1024**2)], [2, str(128*1024**2)]]}]}}
    src = PrometheusSource(base_url="http://prom:9090")
    monkeypatch.setattr(src.session, "get", lambda url, params=None, timeout=None: _Resp(payload))
    m = src.get_metrics_for_pod("team-a", "p-1", minutes=15)
    # peak = max(a)=512 + max(b)=128 = 640; last = 512 + 128 = 640
    assert round(m.memory_max_mb) == 640 and round(m.memory_last_mb) == 640
    assert m.samples == 4 and m.cpu_max_pct == 0.0


def test_prometheus_empty_series_returns_none(monkeypatch):
    src = PrometheusSource(base_url="http://prom:9090")
    monkeypatch.setattr(src.session, "get", lambda url, params=None, timeout=None: _Resp({"data": {"result": []}}))
    assert src.get_metrics_for_pod("team-a", "p-1") is None


def test_prometheus_without_url_raises_backend_error():
    src = PrometheusSource(base_url="")
    try:
        src.get_metrics_for_pod("team-a", "p-1")
        assert False, "expected BackendError"
    except BackendError:
        pass


# ---- protocol conformance (the seam's contract) ----

def test_sources_satisfy_their_protocols():
    from clickhouse import ClickhouseClient
    from backends.base import EventSource, LogSource, MetricSource, TraceSource
    ch = ClickhouseClient()
    assert isinstance(ch, EventSource) and isinstance(ch, LogSource)
    assert isinstance(ch, MetricSource) and isinstance(ch, TraceSource)
    assert isinstance(KubernetesSource(), EventSource)
    assert isinstance(KubernetesSource(), LogSource)
    assert isinstance(KubernetesSource(), MetricSource)
    assert isinstance(LokiSource(), LogSource)
    assert isinstance(PrometheusSource(), MetricSource)
    assert isinstance(backends.NullTraceSource(), TraceSource)
