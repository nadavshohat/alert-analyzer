"""Deterministic first-pass triage.

For failure classes whose cause is unambiguous from Kubernetes pod status
(OOMKilled, image-pull failure, eviction, container-config error), return a verdict
directly from that ground truth - no LLM call. Anything whose cause lives in
application logs returns None, and the caller escalates to the agent. Conservative:
when the class is not one of the unambiguous ones, escalate.

This is the K8sGPT pattern: deterministic analyzers first, the model only on the
residual. It removes the model (and its cost and variance) from cases where the
answer is already known. A class is diagnosed deterministically only when the pod's
CURRENT state - or a recent termination on a still-unhealthy container - makes the
cause unambiguous; otherwise it returns None and the agent investigates as before.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from agent import Analysis

logger = logging.getLogger(__name__)

_IMAGE_PULL = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull"}
_CONFIG_ERR = {"CreateContainerConfigError", "CreateContainerError"}

# Mirrors the staleness window in main._is_pod_healthy: a past termination older
# than this is not treated as the current cause.
_RECENT_TERMINATION_SECONDS = 600


def _recent(finished_at) -> bool:
    if not finished_at:
        return False
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - finished_at).total_seconds() <= _RECENT_TERMINATION_SECONDS


def _verdict(summary: str, root_cause: str, recommendation: str) -> Analysis:
    return Analysis(
        summary=summary,
        root_cause=root_cause,
        recommendations=[recommendation],
        raw_response="deterministic classifier (no LLM call)",
        tool_calls_made=0,
        confidence="high",
        resolved=False,
    )


def classify(event, k8s_api) -> Optional[Analysis]:
    """Return a deterministic verdict for an unambiguous failure class, else None."""
    if not k8s_api:
        return None
    try:
        pod = k8s_api.read_namespaced_pod(name=event.pod_name, namespace=event.namespace)
    except Exception as e:
        # Cannot read pod state -> do not guess; let the agent investigate.
        logger.debug(f"classifier could not read pod {event.namespace}/{event.pod_name}: {e}")
        return None

    status = getattr(pod, "status", None)
    if status is None:
        return None

    # Pod-level eviction (current, unambiguous).
    if getattr(status, "reason", None) == "Evicted":
        return _verdict(
            "Pod evicted under node pressure",
            "The node evicted this pod under resource pressure (memory or disk).",
            "Check node resource pressure and this pod's requests/limits.",
        )

    # Include init containers: image-pull / config errors commonly occur there.
    containers = list(status.init_container_statuses or []) + list(status.container_statuses or [])

    # 1. CURRENT state describes the pod now, so it wins over any past termination.
    for cs in containers:
        cur = cs.state.terminated if cs.state else None
        if cur and cur.reason == "OOMKilled":
            return _verdict(
                f"Container {cs.name} was OOMKilled",
                f"Container {cs.name} exceeded its memory limit and was OOMKilled "
                f"(exit {cur.exit_code}).",
                "Raise the container memory limit or fix the memory leak.",
            )
    for cs in containers:
        waiting = cs.state.waiting if cs.state else None
        if not waiting or not waiting.reason:
            continue
        reason = waiting.reason
        msg = (waiting.message or "").strip()
        if reason in _IMAGE_PULL:
            image = getattr(cs, "image", None) or "the image"
            return _verdict(
                f"Image pull failed for {cs.name}",
                f"Container {cs.name} could not pull image {image}: {reason}"
                + (f" ({msg})" if msg else "") + ".",
                "Check the image tag, registry path, and the pull secret.",
            )
        if reason in _CONFIG_ERR:
            return _verdict(
                f"Container config error for {cs.name}",
                f"Container {cs.name} failed to start: {reason}"
                + (f" ({msg})" if msg else "")
                + ". This is usually a missing or invalid ConfigMap or Secret reference.",
                "Fix the referenced ConfigMap/Secret in the pod spec.",
            )

    # 2. A crashlooping container's cause is in its LAST termination. Accept an OOM
    #    there only if it is recent AND the container is still not ready, so a stale
    #    OOM (already recovered) or a healthy sidecar's old OOM cannot shadow the
    #    real, current failure - those fall through to the model.
    for cs in containers:
        last = cs.last_state.terminated if cs.last_state else None
        if (last and last.reason == "OOMKilled"
                and not getattr(cs, "ready", False)
                and _recent(last.finished_at)):
            return _verdict(
                f"Container {cs.name} was OOMKilled",
                f"Container {cs.name} exceeded its memory limit and was OOMKilled "
                f"(exit {last.exit_code}).",
                "Raise the container memory limit or fix the memory leak.",
            )

    return None  # residual - cause is in the logs; escalate to the agent.
