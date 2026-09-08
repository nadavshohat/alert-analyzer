"""Tests for the deterministic classifier and process_event routing."""
import os
import sys
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import classifier  # noqa: E402
import clickhouse  # noqa: E402
import main as main_mod  # noqa: E402

NOW = datetime.now(timezone.utc)
STALE = NOW - timedelta(hours=3)


def _term(reason, exit_code=1, finished_at=None):
    return types.SimpleNamespace(reason=reason, exit_code=exit_code, finished_at=finished_at)


def _cs(name="app", image="repo/app:1", ready=False, waiting=None, terminated=None, last_terminated=None):
    return types.SimpleNamespace(
        name=name, image=image, ready=ready,
        state=types.SimpleNamespace(
            waiting=types.SimpleNamespace(**waiting) if waiting else None,
            terminated=terminated,
        ),
        last_state=types.SimpleNamespace(terminated=last_terminated),
    )


def _pod(reason=None, container_statuses=None, init_container_statuses=None):
    return types.SimpleNamespace(status=types.SimpleNamespace(
        reason=reason,
        container_statuses=container_statuses or [],
        init_container_statuses=init_container_statuses or []))


def _api(pod=None, raises=False):
    def read(name, namespace):
        if raises:
            raise RuntimeError("api down")
        return pod
    return types.SimpleNamespace(read_namespaced_pod=read)


EV = clickhouse.CrashEvent(NOW, "team-a", "wl", "pod-1", "CrashLoopBackOff", "m")


# ---- deterministic classes (current state or recent+unhealthy) ----



def test_image_pull_is_deterministic():
    pod = _pod(container_statuses=[_cs(waiting={"reason": "ImagePullBackOff", "message": "not found"})])
    a = classifier.classify(EV, _api(pod))
    assert a and "Image pull failed" in a.summary and "repo/app:1" in a.root_cause


def test_init_container_image_pull_is_deterministic():
    pod = _pod(init_container_statuses=[_cs(name="init-db", image="repo/init:2",
                                            waiting={"reason": "ErrImagePull", "message": "denied"})])
    a = classifier.classify(EV, _api(pod))
    assert a and "init-db" in a.summary


def test_evicted_is_deterministic():
    a = classifier.classify(EV, _api(_pod(reason="Evicted")))
    assert a and "evicted" in a.summary.lower()


def test_config_error_is_deterministic():
    pod = _pod(container_statuses=[_cs(waiting={"reason": "CreateContainerConfigError", "message": "secret x missing"})])
    a = classifier.classify(EV, _api(pod))
    assert a and "config error" in a.summary.lower()


# ---- residual / escalation (must return None -> model) ----

def test_oomkilled_escalates_to_model():
    """OOM is left to the agent: pod status shows THAT it OOMed, not WHY."""
    pod = _pod(container_statuses=[_cs(ready=False, terminated=_term("OOMKilled", 137, NOW))])
    assert classifier.classify(EV, _api(pod)) is None


def test_crashloop_oom_laststate_escalates_to_model():
    pod = _pod(container_statuses=[_cs(ready=False, last_terminated=_term("OOMKilled", 137, NOW))])
    assert classifier.classify(EV, _api(pod)) is None


def test_plain_error_exit1_escalates():
    pod = _pod(container_statuses=[_cs(last_terminated=_term("Error", 1, NOW))])
    assert classifier.classify(EV, _api(pod)) is None





def test_no_api_escalates():
    assert classifier.classify(EV, None) is None


def test_unreadable_pod_escalates():
    assert classifier.classify(EV, _api(raises=True)) is None


# ---- process_event routing ----

def _analyzer():
    a = object.__new__(main_mod.AlertAnalyzer)
    a.seen_events = {}
    a._shutdown = types.SimpleNamespace(wait=lambda timeout=None: False, is_set=lambda: False)
    a._is_pod_healthy = lambda ev: False
    return a


def test_deterministic_class_skips_the_model():
    a = _analyzer()
    pod = _pod(container_statuses=[_cs(waiting={"reason": "ImagePullBackOff", "message": "no"})])
    a.k8s_tools = types.SimpleNamespace(k8s_api=_api(pod))
    called = {"analyze": False, "sent": False}
    class Agent:
        def analyze(self, ev): called["analyze"] = True; raise AssertionError("model must not run")
    class Notifier:
        def send(self, ev, an): called["sent"] = True; return True
    a.agent = Agent(); a.notifier = Notifier()
    a.process_event(EV)
    assert called["analyze"] is False and called["sent"] is True


def test_residual_calls_the_model():
    a = _analyzer()
    pod = _pod(container_statuses=[_cs(last_terminated=_term("Error", 1, NOW))])
    a.k8s_tools = types.SimpleNamespace(k8s_api=_api(pod))
    called = {"analyze": False, "sent": False}
    import agent as agent_mod
    class Agent:
        def analyze(self, ev):
            called["analyze"] = True
            return agent_mod.Analysis("s", "r", ["x"], "", resolved=False, confidence="low")
    class Notifier:
        def send(self, ev, an): called["sent"] = True; return True
    a.agent = Agent(); a.notifier = Notifier()
    a.process_event(EV)
    assert called["analyze"] is True and called["sent"] is True
