"""Record-replay evaluation for the alert-analyzer verdict path.

Each cassette holds a crash event plus the tool outputs an investigation gathered,
and the expected diagnosis. The harness feeds that evidence to the agent's no-tools
verdict step (AgentAnalyzer._summarize_no_tools) and scores the verdict against the
expected root cause. This checks whether the verdict names the
cause that is present in the evidence and does not falsely mark it resolved (a
regression/smoke signal, not a deep measure of diagnosis quality), deterministically
with one Bedrock call per cassette - no cluster, no Slack (the notifier is never
involved), and no dependence on the model's tool-choice nondeterminism.

Usage (needs AWS creds for Bedrock; keep SLACK_WEBHOOK_URL unset - not used anyway):
    PYTHONPATH=src python eval/run_eval.py
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from clickhouse import CrashEvent  # noqa: E402


def load_cassettes(directory):
    out = []
    for f in sorted(Path(directory).glob("*.json")):
        out.append(json.loads(f.read_text()))
    return out


def _has_word(haystack, word):
    return re.search(r"\b" + re.escape(word.lower()) + r"\b", haystack) is not None


def score(analysis, expected):
    """Return (passed, reasons). Pure; unit-tested without Bedrock.

    Word-boundary matching, so 'oom' does not match 'room' and 'tag' does not match
    'stage'. `root_cause_contains` passes if ANY listed term appears as a word;
    `root_cause_must_not_contain` fails if any listed term appears (red-herring guard)."""
    reasons = []
    haystack = f"{analysis.root_cause}\n{analysis.summary}".lower()
    wanted = expected.get("root_cause_contains", [])
    hit = any(_has_word(haystack, w) for w in wanted) if wanted else True
    if not hit:
        reasons.append(f"root cause names none of {wanted}")
    ok = hit
    for bad in expected.get("root_cause_must_not_contain", []):
        if _has_word(haystack, bad):
            reasons.append(f"root cause blamed a red herring: {bad!r}")
            ok = False
    if expected.get("must_not_resolve") and analysis.resolved:
        reasons.append("verdict wrongly marked resolved")
        ok = False
    return ok, reasons


def run_cassette(cassette, analyzer):
    ev = CrashEvent(
        timestamp=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        namespace=cassette["event"]["namespace"],
        workload=cassette["event"]["workload"],
        pod_name=cassette["event"]["pod_name"],
        reason=cassette["event"]["reason"],
        message=cassette["event"].get("message", ""),
    )
    evidence = [f"[{c['name']}] {c['result']}" for c in cassette["tool_calls"]]
    analysis = analyzer._summarize_no_tools(ev, evidence, len(evidence))
    return score(analysis, cassette["expected"])


def main():
    from agent import AgentAnalyzer
    cassettes = load_cassettes(Path(__file__).parent / "cassettes")
    if not cassettes:
        print("no cassettes found"); return 1
    analyzer = AgentAnalyzer()
    passed = 0
    for c in cassettes:
        ok, reasons = run_cassette(c, analyzer)
        passed += ok
        print(f"[{'PASS' if ok else 'FAIL'}] {c['name']}" + ("" if ok else f"  -> {'; '.join(reasons)}"))
    print(f"\n{passed}/{len(cassettes)} cassettes passed")
    return 0 if passed == len(cassettes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
