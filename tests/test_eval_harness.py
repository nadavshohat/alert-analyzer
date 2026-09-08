"""Unit tests for the eval harness itself (no Bedrock, no cluster)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "eval"))

import agent  # noqa: E402
import run_eval  # noqa: E402


def _analysis(root_cause="", summary="", resolved=False):
    return agent.Analysis(summary=summary, root_cause=root_cause, recommendations=["x"],
                          raw_response="", resolved=resolved, confidence="low")


def test_score_passes_on_match_and_not_resolved():
    ok, reasons = run_eval.score(_analysis(root_cause="container hit its memory limit"),
                                 {"root_cause_contains": ["memory"], "must_not_resolve": True})
    assert ok and reasons == []


def test_score_fails_when_root_cause_absent():
    ok, reasons = run_eval.score(_analysis(root_cause="network blip"),
                                 {"root_cause_contains": ["memory", "oom"], "must_not_resolve": True})
    assert not ok and reasons


def test_score_fails_when_wrongly_resolved():
    ok, reasons = run_eval.score(_analysis(root_cause="memory limit", resolved=True),
                                 {"root_cause_contains": ["memory"], "must_not_resolve": True})
    assert not ok and any("resolved" in r for r in reasons)


def test_score_matches_in_summary_too():
    ok, _ = run_eval.score(_analysis(summary="likely OOM", root_cause=""),
                           {"root_cause_contains": ["oom"], "must_not_resolve": True})
    assert ok


def test_run_cassette_uses_summariser_and_scores():
    cassette = {
        "name": "t", "event": {"reason": "OOMKilled", "namespace": "n", "workload": "w", "pod_name": "p", "message": "m"},
        "tool_calls": [{"name": "get_metrics", "input": {}, "result": "memory peak 99% of limit"}],
        "expected": {"root_cause_contains": ["memory"], "must_not_resolve": True},
    }
    seen = {}
    class FakeAnalyzer:
        def _summarize_no_tools(self, event, evidence, n):
            seen["evidence"] = evidence
            return _analysis(root_cause="the container exceeded its memory limit")
    ok, reasons = run_eval.run_cassette(cassette, FakeAnalyzer())
    assert ok
    assert seen["evidence"] == ["[get_metrics] memory peak 99% of limit"]  # recorded output fed as evidence


def test_shipped_cassettes_load_and_are_well_formed():
    from pathlib import Path
    cassettes = run_eval.load_cassettes(Path(__file__).parent.parent / "eval" / "cassettes")
    assert len(cassettes) >= 4
    for c in cassettes:
        assert c["event"]["reason"] and c["tool_calls"] and c["expected"]["root_cause_contains"]


def test_score_word_boundary_rejects_substring_false_positive():
    ok, _ = run_eval.score(_analysis(root_cause="there is room to tune this"),
                           {"root_cause_contains": ["oom"], "must_not_resolve": True})
    assert not ok


def test_score_must_not_contain_flags_red_herring():
    ok, reasons = run_eval.score(
        _analysis(root_cause="the metrics-exporter connection caused the crash"),
        {"root_cause_contains": ["metrics-exporter"], "root_cause_must_not_contain": ["metrics-exporter"]})
    assert not ok and any("red herring" in r for r in reasons)


def test_score_word_boundary_still_matches_whole_word():
    ok, _ = run_eval.score(_analysis(root_cause="container exceeded its memory limit (OOMKilled)"),
                           {"root_cause_contains": ["memory", "oomkilled"], "must_not_resolve": True})
    assert ok
