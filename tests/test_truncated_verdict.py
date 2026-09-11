"""Truncated (max_tokens) verdict must strip SUMMARY:/ROOT_CAUSE: labels for display."""
import os, sys, types
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import agent as agent_mod
from clickhouse import CrashEvent
from datetime import datetime, timezone


def _agent_with_response(stop_reason, text):
    a = object.__new__(agent_mod.AgentAnalyzer)
    a.tools = types.SimpleNamespace(
        k8s_api=None, investigation_namespace=None,
        execute=lambda n, i: "")
    a.bedrock = types.SimpleNamespace(converse=lambda **kw: {
        "stopReason": stop_reason,
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
    })
    return a


EV = CrashEvent(datetime.now(timezone.utc), "prod", "solar-service", "solar-service-x", "Unhealthy", "probe failed")


def test_max_tokens_verdict_strips_prefixes():
    a = _agent_with_response("max_tokens",
        "SUMMARY: Readiness probe timed out under load\nROOT_CAUSE: Traces show SQS lon")
    r = a.analyze(EV)
    assert r.summary == "Readiness probe timed out under load"   # prefix stripped
    assert "SUMMARY:" not in r.summary and "ROOT_CAUSE:" not in r.summary
    assert r.root_cause == "Analysis truncated (max_tokens) — raise BEDROCK_MAX_TOKENS"
    assert r.confidence == "low"


def test_max_tokens_with_no_parseable_text_falls_back_to_note():
    a = _agent_with_response("max_tokens", "")
    r = a.analyze(EV)
    assert r.summary == "Analysis truncated (max_tokens) — raise BEDROCK_MAX_TOKENS"
    assert r.confidence == "low"
