# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A long-running Kubernetes pod (`src/main.py`) that polls Groundcover's ClickHouse for crash events, runs an agentic Bedrock (Claude) investigation per event, and posts a Slack alert with root cause. Deployed with the Helm chart in `chart/`. ProjectCircle also has a private Terraform wrapper at `ProjectCircleIL/terraform-modules//modules/extras/alert-analyzer/` that installs this chart with AWS IRSA and secret discovery.

## Commands

Tests: `PYTHONPATH=src pytest tests/` (characterization + regression suite). No lint config or Makefile yet. Common ops:

```bash
# Run locally (requires env vars: CLICKHOUSE_*, SLACK_WEBHOOK_URL, AWS creds for Bedrock, CLUSTER_NAME)
pip install -r requirements.txt
python src/main.py

# Build/push image (consumed by the Terraform module above)
docker build --platform linux/amd64 -t public.ecr.aws/j5u9j5q0/alert-analyzer:latest .
docker push public.ecr.aws/j5u9j5q0/alert-analyzer:latest
```

Python 3.14 (`python:3.14-slim-bookworm`). Entry point is `src/main.py` (the Dockerfile sets `WORKDIR /app` and copies `src/` flat, so imports are bare: `from config import config`, not `from src.config`).

## Architecture

Six files in `src/`, each one layer:

- `main.py` -> `AlertAnalyzer`: polling loop, dedup, transient-event filters
- `clickhouse.py` -> `ClickhouseClient`: HTTP queries against Groundcover's `events`, `logs`, `traces`, `infra_measurements` tables
- `agent.py` -> `AgentAnalyzer`: Bedrock Converse API tool-use loop (max 20 turns)
- `tools.py` -> `ToolHandler`: implementations of the tools the agent can call
- `notifier.py` -> `SlackNotifier`: mrkdwn formatting + Groundcover deep link
- `config.py`: env-var-backed dataclass, single global `config`

Data flow per event:
```
ClickHouse events table -> CrashEvent -> dedup -> 30s wait -> _is_pod_healthy check
  -> AgentAnalyzer.analyze() (Bedrock Converse with toolConfig, looped until end_turn)
  -> Analysis (parsed from strict SUMMARY/ROOT_CAUSE/CONFIDENCE/STATUS/RECOMMENDATIONS text)
  -> SlackNotifier.send() (skipped if STATUS=resolved)
```

### Agent loop (the load-bearing part)

The `AgentAnalyzer.analyze` loop in `agent.py` drives Bedrock's Converse API. The model returns either `stop_reason=end_turn` (parse final text) or `stop_reason=tool_use` (execute every `toolUse` block in the message, append `toolResult` blocks as a user message, loop). Tool results are truncated to 20KB before being fed back to the model. If the loop hits `MAX_AGENT_TURNS` (default 20), one final call asks for a summary (still passing toolConfig, as Converse requires once tool blocks exist in the history).

The system prompt in `SYSTEM_PROMPT` enforces a metrics-first investigation order and is deliberately strict about not concluding "GIL contention" / "event loop starvation" from code patterns alone. The output format is parsed line-by-line in `_parse_response` - changes to the prompt's output schema must keep these exact prefixes: `SUMMARY:`, `ROOT_CAUSE:` (or `ROOT CAUSE:`), `CONFIDENCE:`, `STATUS:`, `RECOMMENDATIONS:`.

### Tools the agent can call

Defined in `TOOL_DEFINITIONS`, dispatched in `ToolHandler.execute`:

| Tool | Backed by |
|------|-----------|
| `get_logs` | ClickHouse `logs` table, error/fatal first then backfill |
| `get_traces` | ClickHouse `traces` table, slowest first |
| `get_metrics` | ClickHouse `infra_measurements`, joined with pod's k8s memory limit to compute % |
| `describe_pod` | k8s API, with a hand-rolled "TERMINATION SUMMARY" prepended to the JSON |
| `read_file` / `list_dir` / `get_env` | k8s exec stream running a FIXED argv (`cat`/`ls`/`printenv`), no shell, path validated, namespace forced to the alert's own. `get_env` redacts secret-looking keys best-effort |
| `get_previous_logs` | k8s API previous-container logs |

The pod-read ops retry up to 3 times when the container is restarting and will look up a sibling running pod from the same workload prefix if the original is gone (`_find_running_pod`).

### Noise suppression

Several layers filter out events that aren't worth alerting on:

1. **Dedup by `namespace/workload/reason`** for `DEDUP_WINDOW_SECONDS` (default 300s) - `main.py:_is_duplicate`
2. **Grace period before analysis**: 30s default, 120s for `Failed`/`BackOff` (image pulls retry on kubelet backoff at 10/20/40/80s). After the wait, `_is_pod_healthy` skips the event if any of: the pod is gone (404), `pod.status.phase == 'Succeeded'` (workflow/job completed), or the pod is Ready *and* no container has a `lastState.terminated` with `OOMKilled` or non-zero exit in the last 10 minutes. Checking `lastState` (not just current readiness) matters because OOMKill restarts the container in place - the new instance can be Ready seconds after the kill.
3. **Per-reason skips** in `poll()`: terminating pods (Unhealthy noise during shutdown), `UNHEALTHY_SKIP_NAMESPACES`, and any Unhealthy event whose message contains "Startup probe failed".
4. **Post-analysis recheck**: `_is_pod_healthy` runs again right before sending Slack. Catches pods that recovered while the agent was investigating (typical agent run is 1-2 min — long enough for ImagePullBackOff to clear on the next kubelet retry).
5. **Auto-resolved gate**: if the agent's final `STATUS:` line is `resolved`, no Slack message is sent.

When changing filter logic, remember the dedup key is `namespace/workload/reason` - a per-pod loop on the same workload collapses to one alert by design.

## Deployment context

The container and the Helm chart (`chart/`) live here. The IRSA role, ClickHouse secret discovery, and Slack webhook lookup live in the private Terraform wrapper (`ProjectCircleIL/terraform-modules` -> `modules/extras/alert-analyzer/`), which installs the chart from here. The private wrapper keeps its own copy of the chart today; changes to chart env vars, ports, or service account permissions need a matching PR there until it consumes this chart directly.

Image is published to `public.ecr.aws/j5u9j5q0/alert-analyzer` (public ECR, ProjectCircle account). No CI in this repo - builds are manual.
