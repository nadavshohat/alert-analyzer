"""Deterministic first-pass triage.

For failure classes whose cause is COMPLETE in Kubernetes pod status - image-pull
failure, container-config error, eviction - return a verdict directly, no LLM call.
OOMKilled is deliberately NOT here: pod status shows THAT a container OOMed, not
WHY (leak vs spike vs undersized limit), and the "why" is what an investigation of
logs and the memory trend surfaces - so OOM is left to the agent.
Conservative: when the class is not one of the complete ones, escalate to the agent.

This is the K8sGPT pattern: deterministic analyzers first, the model only on the
residual. It removes the model (and its cost and variance) from cases where the
answer is already complete in pod status; everything else the agent investigates.
"""
import logging
from typing import Optional

from agent import Analysis

logger = logging.getLogger(__name__)

_IMAGE_PULL = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull"}
# Only CreateContainerConfigError is unambiguous (an unresolvable ConfigMap/Secret
# reference). CreateContainerError is a generic creation failure whose cause is not
# in pod status, so it escalates to the agent.
_CONFIG_ERR = {"CreateContainerConfigError"}


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

    return None  # residual - cause is in the logs; escalate to the agent.
