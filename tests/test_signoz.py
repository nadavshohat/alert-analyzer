"""SigNoz source: request shape, row parsing and the degenerate rows SigNoz can return."""
import types
from datetime import datetime, timedelta, timezone

import pytest
import requests

import backends
from backends.signoz import SignozSource
from clickhouse import BackendError
from config import config


class FakeSession:
    def __init__(self, payload=None, status=200, exc=None):
        self.payload, self.status, self.exc = payload, status, exc
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.exc:
            raise self.exc
        resp = types.SimpleNamespace(status_code=self.status, json=lambda: self.payload)

        def raise_for_status():
            if self.status >= 400:
                raise requests.HTTPError(f"{self.status}")
        resp.raise_for_status = raise_for_status
        return resp


def _raw(rows):
    return {"status": "success", "data": {"type": "raw", "data": {"results": [
        {"queryName": "A", "nextCursor": "", "rows": rows}]}}}


def _src(session):
    s = SignozSource(base_url="http://signoz:8080/", api_key="k")
    s.session = session
    return s


TS = "2026-10-24T21:37:56Z"


def _event(name="api-7d9f8c6b5c-x2k9p", ns="shop", reason="BackOff", ts=TS):
    row = {"data": {"body": "Back-off restarting failed container", "k8s.event.reason": reason,
                    "k8s.namespace.name": ns, "k8s.object.name": name}}
    if ts is not None:
        row["timestamp"] = ts
    return row


def test_events_request_shape_and_parse(monkeypatch):
    monkeypatch.setattr(config, "event_reasons", ["BackOff", "Unhealthy"])
    monkeypatch.setattr(config, "exclude_namespaces", [])
    sess = FakeSession(_raw([_event()]))
    events = _src(sess).get_crash_events()
    call = sess.calls[0]
    assert call["url"] == "http://signoz:8080/api/v5/query_range"
    assert call["headers"] == {"SIGNOZ-API-KEY": "k"}
    spec = call["json"]["compositeQuery"]["queries"][0]["spec"]
    assert call["json"]["requestType"] == "raw" and spec["signal"] == "logs"
    assert "k8s.event.reason IN ('BackOff', 'Unhealthy')" in spec["filter"]["expression"]
    assert len(events) == 1
    e = events[0]
    assert (e.namespace, e.pod_name, e.workload, e.reason) == ("shop", "api-7d9f8c6b5c-x2k9p", "api", "BackOff")
    assert e.timestamp == datetime(2026, 10, 24, 21, 37, 56, tzinfo=timezone.utc)


def test_events_degenerate_rows_skipped(monkeypatch):
    monkeypatch.setattr(config, "event_reasons", ["BackOff"])
    monkeypatch.setattr(config, "exclude_namespaces", ["kube-system"])
    rows = [
        _event(ts=None),                         # SigNoz can omit timestamp
        _event(name=""),                         # no involved object
        _event(ns=""),                           # no namespace
        _event(name='p"} OR 1=1'),               # injection from stored data
        _event(ns="kube-system"),                # excluded namespace
        {"timestamp": TS},                       # no data map at all
        _event(name="ok-0"),
    ]
    events = _src(FakeSession(_raw(rows))).get_crash_events()
    assert [e.pod_name for e in events] == ["ok-0"]
    assert events[0].workload == "ok"


def test_events_since_filter_accepts_naive_timestamp(monkeypatch):
    monkeypatch.setattr(config, "event_reasons", ["BackOff"])
    monkeypatch.setattr(config, "exclude_namespaces", [])
    since = datetime(2026, 10, 24, 21, 37, 56)  # naive, equal to the row
    assert _src(FakeSession(_raw([_event()]))).get_crash_events(since) == []


def test_events_reason_list_rejects_unsafe_reasons(monkeypatch):
    monkeypatch.setattr(config, "event_reasons", ["BackOff')", ""])
    sess = FakeSession(_raw([]))
    assert _src(sess).get_crash_events() == []
    assert sess.calls == []


def test_zero_rows_and_empty_results(monkeypatch):
    monkeypatch.setattr(config, "event_reasons", ["BackOff"])
    assert _src(FakeSession(_raw([]))).get_crash_events() == []
    empty = {"status": "success", "data": {"type": "raw", "data": {"results": []}}}
    assert _src(FakeSession(empty)).get_logs_for_pod("shop", "api-0") == []


@pytest.mark.parametrize("session", [
    FakeSession({}, status=401),
    FakeSession(exc=requests.ConnectionError("down")),
    FakeSession({"status": "error", "error": {"code": "bad_request"}}),
])
def test_failures_raise_backend_error(session):
    with pytest.raises(BackendError):
        _src(session).get_logs_for_pod("shop", "api-0")


def test_missing_url_raises(monkeypatch):
    monkeypatch.setattr(config, "signoz_url", "")
    with pytest.raises(BackendError):
        SignozSource(api_key="k").get_logs_for_pod("shop", "api-0")


@pytest.mark.parametrize("ns,pod", [("shop", "p' OR '1'='1"), ("shop'", "api-0"), ("", "api-0")])
def test_injection_rejected_before_any_request(ns, pod):
    sess = FakeSession(_raw([]))
    with pytest.raises(BackendError):
        _src(sess).get_logs_for_pod(ns, pod)
    assert sess.calls == []


def test_logs_sorted_and_level_fallback():
    rows = [
        {"timestamp": "2026-10-24T21:00:02Z", "data": {"body": "later ERROR boom", "severity_text": ""}},
        {"timestamp": "2026-10-24T21:00:01Z", "data": {"body": "first", "severity_text": "WARN"}},
    ]
    logs = _src(FakeSession(_raw(rows))).get_logs_for_workload("shop", "api")
    assert [l.body for l in logs] == ["first", "later ERROR boom"]
    assert [l.level for l in logs] == ["warn", "error"]


def _ts(values_by_call):
    def payload(vals):
        return {"status": "success", "data": {"type": "time_series", "data": {"results": [
            {"queryName": "A", "aggregations": [{"index": 0, "series": [
                {"values": [{"timestamp": 1, "value": v} for v in vals]}]}]}]}}}
    return iter(payload(v) for v in values_by_call)


class SeqSession(FakeSession):
    def __init__(self, payloads):
        super().__init__()
        self.payloads = payloads

    def post(self, url, json=None, headers=None, timeout=None):
        self.payload = next(self.payloads)
        return super().post(url, json=json, headers=headers, timeout=timeout)


def test_metrics_memory_mib_and_cpu_pct():
    mib = 1024 ** 2
    sess = SeqSession(_ts([[100 * mib, 300 * mib], [0.5, 0.25]]))
    m = _src(sess).get_metrics_for_pod("shop", "api-0", minutes=15)
    assert (m.memory_max_mb, m.memory_avg_mb, m.memory_last_mb) == (300, 200, 300)
    assert (m.cpu_max_pct, m.cpu_avg_pct) == (50, 37.5)
    assert sess.calls[0]["json"]["compositeQuery"]["queries"][0]["spec"]["aggregations"][0]["metricName"] == "k8s.pod.memory.working_set"


def test_metrics_empty_is_none_and_no_cpu_limit_is_zero():
    assert _src(SeqSession(_ts([[]]))).get_metrics_for_pod("shop", "api-0") is None
    m = _src(SeqSession(_ts([[1024 ** 2], []]))).get_metrics_for_pod("shop", "api-0")
    assert m.cpu_max_pct == 0.0 and m.samples == 1


def test_traces_parse_duration_and_skip_untimed():
    rows = [
        {"timestamp": TS, "data": {"name": "GET /", "duration_nano": 2_500_000_000, "status_code": 2,
                                   "status_message": "boom"}},
        {"data": {"name": "no ts"}},
    ]
    t = _src(FakeSession(_raw(rows))).get_slow_traces("shop", "api")
    assert len(t) == 1 and t[0].duration_seconds == 2.5 and t[0].span_name == "GET /"


def test_factory_selects_signoz_for_every_signal(monkeypatch):
    for k in ("event_source", "log_source", "metric_source", "trace_source"):
        monkeypatch.setattr(config, k, "signoz")
    assert {type(f()).__name__ for f in (backends.build_event_source, backends.build_log_source,
                                         backends.build_metric_source, backends.build_trace_source)} == {"SignozSource"}


def test_slack_link_only_for_groundcover_events(monkeypatch):
    import notifier
    from agent import Analysis
    sent = []
    monkeypatch.setattr(notifier.requests, "post", lambda url, **kw: sent.append(kw) or types.SimpleNamespace(status_code=200, raise_for_status=lambda: None, text="ok"))
    monkeypatch.setattr(config, "slack_webhook_url", "https://hooks.example/x")
    ev = notifier.CrashEvent(datetime.now(timezone.utc), "shop", "api", "api-0", "BackOff", "m")
    an = Analysis("s", "r", ["do x"], "raw")
    for src, expected in (("clickhouse", True), ("signoz", False)):
        sent.clear()
        monkeypatch.setattr(config, "event_source", src)
        notifier.SlackNotifier().send(ev, an)
        assert ("View in Groundcover" in str(sent)) is expected
