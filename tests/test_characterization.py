"""Characterization tests for alert-analyzer.

These pin the CURRENT behaviour of the code at HEAD, bugs included, so that
when a fix changes one of these behaviours the test goes red and the change is
made consciously. Comments marked BUG note where current behaviour is wrong;
the fix PR flips that assertion and adds a fail-without-the-fix test alongside.

Runnable with: .venv/bin/python -m pytest tests/ -q
"""
import types
from datetime import datetime, timezone

import pytest

import agent
import clickhouse
import config as config_mod
import main as main_mod
import tools as tools_mod


# --------------------------------------------------------------------------
# agent._parse_response  (the model's verdict -> Analysis)
# --------------------------------------------------------------------------

def test_status_resolved_bare_suppresses():
    """'STATUS: resolved' sets resolved=True (this is the alert-suppression path)."""
    a = agent.AgentAnalyzer._parse_response("SUMMARY: x\nSTATUS: resolved")
    assert a.resolved is True


def test_status_resolved_with_trailing_text_now_matches():
    """FIXED (M9): 'resolved (recovered)' is recognised as resolved via startswith."""
    a = agent.AgentAnalyzer._parse_response("SUMMARY: x\nSTATUS: resolved (pod recovered)")
    assert a.resolved is True


def test_empty_response_is_low_confidence():
    """FIXED (M2): an empty model response is marked low confidence, not medium."""
    a = agent.AgentAnalyzer._parse_response("")
    assert a.confidence == "low"
    assert a.resolved is False


def test_numbered_recommendations_are_parsed():
    """FIXED (M8): numbered/bulleted recommendations are captured, not dropped."""
    resp = "SUMMARY: s\nROOT_CAUSE: r\nRECOMMENDATIONS:\n1. restart it\n2. scale up"
    a = agent.AgentAnalyzer._parse_response(resp)
    assert a.recommendations == ["restart it", "scale up"]


def test_multiline_summary_drops_continuation():
    """BUG (M14): only the first SUMMARY line is kept; continuation lines vanish."""
    resp = "SUMMARY: first line\nsecond line of summary\nROOT_CAUSE: r"
    a = agent.AgentAnalyzer._parse_response(resp)
    assert a.summary == "first line"


# --------------------------------------------------------------------------
# tools pod-read ops (read_file / list_dir / get_env) -- the security boundary.
# After the P1 fix the model supplies only a path, never a command or binary;
# each tool builds a FIXED argv with no shell.
# --------------------------------------------------------------------------

def _capture_argv(monkeypatch):
    rec = {}
    def fake(self, namespace, pod_name, argv):
        rec["argv"] = argv
        return "OK"
    monkeypatch.setattr(tools_mod.ToolHandler, "_pod_exec", fake)
    return rec


def test_arbitrary_command_tool_is_gone():
    h = tools_mod.ToolHandler()
    assert h.execute("exec_in_pod", {"namespace": "n", "pod_name": "p", "command": "id"}) == "Unknown tool: exec_in_pod"


def test_read_file_builds_fixed_cat_argv(monkeypatch):
    rec = _capture_argv(monkeypatch)
    tools_mod.ToolHandler()._read_file({"namespace": "n", "pod_name": "p", "path": "/app/config.json"})
    assert rec["argv"] == ["cat", "--", "/app/config.json"]


def test_list_dir_builds_fixed_ls_argv(monkeypatch):
    rec = _capture_argv(monkeypatch)
    tools_mod.ToolHandler()._list_dir({"namespace": "n", "pod_name": "p", "path": "/app"})
    assert rec["argv"] == ["ls", "-la", "--", "/app"]


def test_old_bypass_payload_is_now_an_invalid_path(monkeypatch):
    rec = _capture_argv(monkeypatch)
    out = tools_mod.ToolHandler()._read_file(
        {"namespace": "n", "pod_name": "p", "path": "awk 'BEGIN{system(1)}'"})
    assert "Invalid path" in out
    assert "argv" not in rec  # never reached an exec


def test_read_file_rejects_relative_and_empty_path(monkeypatch):
    rec = _capture_argv(monkeypatch)
    h = tools_mod.ToolHandler()
    assert "Invalid path" in h._read_file({"namespace": "n", "pod_name": "p", "path": "etc/passwd"})
    assert "Invalid path" in h._read_file({"namespace": "n", "pod_name": "p", "path": ""})
    assert "argv" not in rec


def test_get_env_redacts_secret_keyed_vars(monkeypatch):
    def fake(self, namespace, pod_name, argv):
        assert argv == ["printenv"]
        return "PATH=/usr/bin\nDB_PASSWORD=hunter2\nSLACK_WEBHOOK_URL=https://hooks/x\nAUTH_TOKEN=abc\nHOME=/root"
    monkeypatch.setattr(tools_mod.ToolHandler, "_pod_exec", fake)
    out = tools_mod.ToolHandler()._get_env({"namespace": "n", "pod_name": "p"})
    assert "PATH=/usr/bin" in out and "HOME=/root" in out
    assert "hunter2" not in out
    assert "hooks/x" not in out
    assert "abc" not in out


def test_get_env_redacts_multiline_secret_value(monkeypatch):
    """Regression: a multi-line secret value (PEM key) must be dropped whole, not
    just its NAME= header line. Fails against the old single-line filter."""
    def fake(self, namespace, pod_name, argv):
        return ("PATH=/usr/bin\n"
                "PRIVATE_KEY=-----BEGIN PRIVATE KEY-----\n"
                "MIIEvQIBADANBgkqSECRETMATERIAL\n"
                "-----END PRIVATE KEY-----\n"
                "HOME=/root")
    monkeypatch.setattr(tools_mod.ToolHandler, "_pod_exec", fake)
    out = tools_mod.ToolHandler()._get_env({"namespace": "n", "pod_name": "p"})
    assert "PATH=/usr/bin" in out and "HOME=/root" in out
    assert "SECRETMATERIAL" not in out       # continuation line no longer leaks
    assert "BEGIN PRIVATE KEY" not in out

# --------------------------------------------------------------------------
# tools._get_logs  model-supplied 'minutes' reaches the backend uncoerced
# --------------------------------------------------------------------------

def test_minutes_is_coerced_and_bounded():
    """FIXED (H7): a model-supplied 'minutes' is coerced to a bounded int before the query."""
    captured = {}

    class FakeCH:
        def get_logs_for_workload(self, ns, wl, minutes):
            captured["minutes"] = minutes
            return []
        def get_logs_for_pod(self, ns, pod, minutes):
            captured["minutes"] = minutes
            return []

    h = tools_mod.ToolHandler()
    h.clickhouse = FakeCH()
    h._get_logs({"namespace": "n", "workload": "w", "minutes": "999999"})
    assert captured["minutes"] == 1440  # bounded int, not a raw string
    assert isinstance(captured["minutes"], int)


# --------------------------------------------------------------------------
# clickhouse.get_crash_events  timezone serialization
# --------------------------------------------------------------------------

def test_since_timestamp_serialized_without_offset(monkeypatch):
    """BUG (M6): a tz-aware UTC datetime is sent to ClickHouse as a naive string,
    to be reinterpreted in the server's session timezone."""
    captured = {}

    def fake_exec(self, query, params=None):
        captured["params"] = params or {}
        return {"data": []}

    def fake_exec2(self, query, params=None):
        captured["params"] = params or {}
        captured["query"] = query
        return {"data": []}
    monkeypatch.setattr(clickhouse.ClickhouseClient, "_execute_query", fake_exec2)
    ch = clickhouse.ClickhouseClient()
    ts = datetime(2026, 9, 7, 10, 0, 0, tzinfo=timezone.utc)
    ch.get_crash_events(since_timestamp=ts)
    assert captured["params"]["since_ts"] == "2026-09-07 10:00:00"  # naive string
    # FIXED (M6): the query pins the string to UTC so the server session TZ cannot shift it.
    assert "'UTC'" in captured["query"]


# --------------------------------------------------------------------------
# main.AlertAnalyzer  dedup + watermark
# --------------------------------------------------------------------------

def _analyzer():
    """Build an AlertAnalyzer without running its real collaborators' side effects."""
    a = object.__new__(main_mod.AlertAnalyzer)
    a.seen_events = {}
    from datetime import timedelta  # noqa
    return a


def test_duplicate_within_window_is_suppressed():
    a = _analyzer()
    ev = clickhouse.CrashEvent(
        timestamp=datetime.now(timezone.utc), namespace="ns", workload="wl",
        pod_name="pod", reason="OOMKilled", message="m",
    )
    assert a._is_duplicate(ev) is False   # first sighting records it
    assert a._is_duplicate(ev) is True    # second within window suppressed


def test_dedup_key_ignores_pod(monkeypatch):
    """BUG (M13): key is namespace/workload/reason, so two different pods collapse."""
    ev1 = clickhouse.CrashEvent(datetime.now(timezone.utc), "ns", "wl", "pod-a", "Unhealthy", "m")
    ev2 = clickhouse.CrashEvent(datetime.now(timezone.utc), "ns", "wl", "pod-b", "Unhealthy", "m")
    assert ev1.key == ev2.key == "ns/wl/Unhealthy"


def test_watermark_advances_on_empty_success():
    """Empty result = a successful read with no events; the watermark advances."""
    a = _analyzer()
    a.last_poll_time = None
    class FakeCH:
        def get_crash_events(self, since_timestamp=None):
            return []
    a.clickhouse = FakeCH()
    a._cleanup_seen_events = lambda: None
    a.poll()
    assert a.last_poll_time is not None


def test_watermark_holds_on_backend_error():
    """FIXED (C4): a backend error raises; the watermark is NOT advanced, so the
    next poll re-reads the window instead of silently dropping its events."""
    a = _analyzer()
    a.last_poll_time = None
    class FakeCH:
        def get_crash_events(self, since_timestamp=None):
            raise clickhouse.BackendError("clickhouse down")
    a.clickhouse = FakeCH()
    a._cleanup_seen_events = lambda: None
    a.poll()
    assert a.last_poll_time is None  # held


def test_logs_backend_error_is_not_reported_as_no_data(monkeypatch):
    """FIXED (H1): a backend failure is surfaced as 'unavailable', not 'no logs found',
    so the model does not treat an outage as evidence of a silent workload."""
    class FakeCH:
        def get_logs_for_workload(self, ns, wl, minutes):
            raise clickhouse.BackendError("clickhouse down")
        def get_logs_for_pod(self, ns, pod, minutes):
            raise clickhouse.BackendError("clickhouse down")
    h = tools_mod.ToolHandler()
    h.clickhouse = FakeCH()
    out = h._get_logs({"namespace": "n", "workload": "w", "minutes": 30})
    assert "unavailable" in out.lower()
    assert "no logs found" not in out.lower()


def test_grace_wait_is_shutdown_aware(monkeypatch):
    """FIXED (H4): process_event returns during shutdown instead of blocking in the
    grace period, so SIGTERM is honoured (no SIGKILL on rollout)."""
    import threading
    a = _analyzer()
    a._shutdown = threading.Event()
    a._shutdown.set()  # simulate SIGTERM already received
    called = {"analyzed": False}

    class FakeAgent:
        def analyze(self, event):
            called["analyzed"] = True
            raise AssertionError("must not analyze during shutdown")
    a.agent = FakeAgent()
    a.k8s_tools = None
    ev = clickhouse.CrashEvent(datetime.now(timezone.utc), "ns", "wl", "pod", "OOMKilled", "m")
    a.process_event(ev)
    assert called["analyzed"] is False


def test_tool_namespace_is_forced_to_investigation_scope(monkeypatch):
    """FIXED (C1 cross-namespace leg): a tool call naming a different namespace is
    overridden to the namespace of the event under investigation."""
    rec = {}
    def fake(self, namespace, pod_name, argv):
        rec["ns"] = namespace
        return "OK"
    monkeypatch.setattr(tools_mod.ToolHandler, "_pod_exec", fake)
    h = tools_mod.ToolHandler()
    h.investigation_namespace = "team-a"
    h._read_file({"namespace": "kube-system", "pod_name": "p", "path": "/etc/hosts"})
    assert rec["ns"] == "team-a"  # not kube-system


# --------------------------------------------------------------------------
# agent.analyze wiring: namespace scoping + max-turns summary must keep toolConfig
# --------------------------------------------------------------------------

def _end_turn(text):
    return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}},
            "stopReason": "end_turn"}


def test_analyze_sets_investigation_namespace(monkeypatch):
    """Pins the security wiring: analyze() scopes tools to the event namespace.
    Fails if the investigation_namespace assignment is removed."""
    a = agent.AgentAnalyzer()
    a.bedrock = types.SimpleNamespace(converse=lambda **kw: _end_turn("SUMMARY: s\nROOT_CAUSE: r"))
    ev = clickhouse.CrashEvent(datetime.now(timezone.utc), "team-a", "wl", "pod", "OOMKilled", "m")
    a.analyze(ev)
    assert a.tools.investigation_namespace == "team-a"


def test_max_turns_summary_call_keeps_toolConfig(monkeypatch):
    """Regression guard (H1): once the loop has emitted tool calls, the max-turns
    summary converse() must still pass toolConfig, or real Bedrock rejects it."""
    monkeypatch.setattr(config_mod.config, "max_agent_turns", 2)
    calls = []
    def fake_converse(**kw):
        calls.append(kw)
        # always ask for a tool -> loop never gets end_turn -> hits max turns
        return {"output": {"message": {"role": "assistant", "content": [
                    {"toolUse": {"toolUseId": "t", "name": "get_logs",
                                 "input": {"namespace": "team-a", "workload": "wl"}}}]}},
                "stopReason": "tool_use"}
    a = agent.AgentAnalyzer()
    a.bedrock = types.SimpleNamespace(converse=fake_converse)
    a.tools.execute = lambda name, inp: "ok"  # don't touch clickhouse/k8s
    ev = clickhouse.CrashEvent(datetime.now(timezone.utc), "team-a", "wl", "pod", "OOMKilled", "m")
    result = a.analyze(ev)                      # must not raise
    assert result is not None
    assert "toolConfig" in calls[-1]           # the summary call kept it
