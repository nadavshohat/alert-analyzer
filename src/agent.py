"""Agentic analyzer using Bedrock Converse API with tool use."""
import json
import logging
import re
from dataclasses import dataclass
from typing import List

import boto3
from botocore.config import Config as BotoConfig

from config import config
from clickhouse import ClickhouseClient, CrashEvent, LogEntry, TraceEntry

logger = logging.getLogger(__name__)

# Commands that are never allowed in pod exec
BLOCKED_COMMANDS = frozenset([
    'rm', 'kill', 'dd', 'mkfs', 'shutdown', 'reboot', 'halt',
    'poweroff', 'mv', 'chmod', 'chown', 'curl', 'wget', 'nc',
    'ncat', 'python', 'node', 'bash', 'sh', 'exec',
])

# Env var keys to filter from printenv output
SECRET_KEYWORDS = frozenset([
    'PASSWORD', 'SECRET', 'TOKEN', 'KEY', 'CREDENTIAL', 'PRIVATE', 'API_KEY',
])

SYSTEM_PROMPT = """You are a Kubernetes incident responder investigating a pod crash.

You have tools to investigate. Use them strategically:
1. ALWAYS start by fetching logs (get_logs with the workload name)
2. If you need more context, exec into the pod to read source code, config files, or environment variables
3. If logs show specific errors you don't recognize, search the web
4. If the issue might be latency-related (health check timeouts, slow responses), check traces

Important investigation guidelines:
- Be efficient - use the minimum number of tool calls needed
- For OOMKilled: this could be a code issue (memory leak) OR the memory limit is simply too low for the workload. Check BOTH possibilities. Try to exec and read the source code to verify.
- If exec fails (pod restarting), retry once - there may be a brief window when the container is up
- Be TRANSPARENT: if you could not exec into the pod or verify something, say so explicitly in your analysis. Do not present guesses as confirmed findings.

CRITICAL: Your final message MUST use ONLY this format with NO other text before or after:

SUMMARY: <one sentence, max 15 words>
ROOT_CAUSE: <1-2 sentences explaining WHY this happened>
CONFIDENCE: <high/medium/low - based on what you were able to verify>
RECOMMENDATIONS:
- <actionable fix>

Do NOT add any commentary, explanation, or thinking outside this format. Just the four fields."""

TOOL_DEFINITIONS = [
    {
        "toolSpec": {
            "name": "get_logs",
            "description": "Fetch recent logs from the observability platform for a workload or specific pod. Returns log entries with timestamp, level, and content. Start with workload name; if empty, try the specific pod name.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "namespace": {
                            "type": "string",
                            "description": "Kubernetes namespace"
                        },
                        "workload": {
                            "type": "string",
                            "description": "Workload/deployment name"
                        },
                        "pod_name": {
                            "type": "string",
                            "description": "Specific pod name (use if workload query returns nothing)"
                        },
                        "minutes": {
                            "type": "integer",
                            "description": "How far back to look in minutes (default: 30)"
                        }
                    },
                    "required": ["namespace"]
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "get_traces",
            "description": "Fetch the slowest traces/spans for a workload, sorted by duration descending. Useful for diagnosing latency issues, event loop blocking, slow DB queries, or health check timeouts.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "namespace": {
                            "type": "string",
                            "description": "Kubernetes namespace"
                        },
                        "workload": {
                            "type": "string",
                            "description": "Workload/deployment name"
                        }
                    },
                    "required": ["namespace", "workload"]
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "exec_in_pod",
            "description": "Execute a read-only command inside a running pod. Use to inspect files, check config, list directories, or view environment variables. Secrets are filtered from printenv output. The pod may be restarting so this can fail.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "namespace": {
                            "type": "string",
                            "description": "Kubernetes namespace"
                        },
                        "pod_name": {
                            "type": "string",
                            "description": "Pod name to exec into"
                        },
                        "command": {
                            "type": "string",
                            "description": "Command to run (read-only, e.g. 'cat /app/package.json')"
                        }
                    },
                    "required": ["namespace", "pod_name", "command"]
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "search_web",
            "description": "Search the web for error solutions, documentation, or best practices. Use specific error messages combined with the technology name for best results.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query (e.g. 'nodejs ECONNREFUSED redis kubernetes')"
                        }
                    },
                    "required": ["query"]
                }
            }
        }
    }
]


@dataclass
class Analysis:
    """Result of AI analysis."""
    summary: str
    root_cause: str
    recommendations: List[str]
    raw_response: str
    tool_calls_made: int = 0
    confidence: str = "medium"


class AgentAnalyzer:
    """Agentic crash analyzer using Bedrock Converse API with tool use."""

    def __init__(self):
        boto_config = BotoConfig(
            region_name=config.bedrock_region,
            retries={'max_attempts': 3, 'mode': 'adaptive'}
        )
        self.bedrock = boto3.client('bedrock-runtime', config=boto_config)
        self.clickhouse = ClickhouseClient()
        self._k8s_api = None
        self._web_searcher = None

    @property
    def k8s_api(self):
        if self._k8s_api is None:
            try:
                from kubernetes import client, config as k8s_config
                try:
                    k8s_config.load_incluster_config()
                except k8s_config.ConfigException:
                    k8s_config.load_kube_config()
                self._k8s_api = client.CoreV1Api()
            except Exception as e:
                logger.warning(f"Could not load kubernetes config: {e}")
        return self._k8s_api

    @property
    def web_searcher(self):
        if self._web_searcher is None:
            from duckduckgo_search import DDGS
            self._web_searcher = DDGS()
        return self._web_searcher

    def analyze(self, event: CrashEvent) -> Analysis:
        """Run an agentic investigation of a crash event."""
        system = [{"text": SYSTEM_PROMPT}]
        tool_config = {"tools": TOOL_DEFINITIONS}

        initial_prompt = (
            f"Investigate this crash event:\n"
            f"- Reason: {event.reason}\n"
            f"- Namespace: {event.namespace}\n"
            f"- Workload: {event.workload}\n"
            f"- Pod: {event.pod_name}\n"
            f"- Message: {event.message}\n\n"
            f"Start investigating."
        )

        messages = [{"role": "user", "content": [{"text": initial_prompt}]}]
        tool_calls_made = 0

        for turn in range(config.max_agent_turns):
            try:
                response = self.bedrock.converse(
                    modelId=config.bedrock_model,
                    messages=messages,
                    system=system,
                    toolConfig=tool_config,
                    inferenceConfig={
                        "maxTokens": config.bedrock_max_tokens,
                        "temperature": 0.2
                    }
                )
            except Exception as e:
                logger.error(f"Bedrock converse failed on turn {turn}: {e}")
                return Analysis(
                    summary=f"Analysis failed: {e}",
                    root_cause="Bedrock API error",
                    recommendations=["Check Bedrock connectivity and IAM permissions"],
                    raw_response=str(e),
                    tool_calls_made=tool_calls_made
                )

            assistant_msg = response["output"]["message"]
            messages.append(assistant_msg)
            stop_reason = response["stopReason"]

            if stop_reason == "end_turn":
                # Final answer
                raw_text = self._extract_text(assistant_msg)
                logger.info(f"Agent finished after {tool_calls_made} tool calls")
                analysis = self._parse_response(raw_text)
                analysis.tool_calls_made = tool_calls_made
                return analysis

            elif stop_reason == "tool_use":
                # Execute requested tools
                tool_results = []
                for block in assistant_msg["content"]:
                    if "toolUse" in block:
                        tool = block["toolUse"]
                        tool_name = tool["name"]
                        tool_input = tool["input"]
                        tool_calls_made += 1

                        logger.info(f"Tool call #{tool_calls_made}: {tool_name}({json.dumps(tool_input)[:200]})")

                        try:
                            result = self._execute_tool(tool_name, tool_input)
                            logger.info(f"Tool result #{tool_calls_made} ({tool_name}): {result[:300]}")
                            # Truncate large results
                            if len(result) > 8000:
                                result = result[:8000] + "\n... (truncated)"
                            tool_results.append({
                                "toolResult": {
                                    "toolUseId": tool["toolUseId"],
                                    "content": [{"text": result}],
                                    "status": "success"
                                }
                            })
                        except Exception as e:
                            logger.warning(f"Tool {tool_name} failed: {e}")
                            tool_results.append({
                                "toolResult": {
                                    "toolUseId": tool["toolUseId"],
                                    "content": [{"text": f"Error: {e}"}],
                                    "status": "error"
                                }
                            })

                messages.append({"role": "user", "content": tool_results})

            else:
                # max_tokens, guardrail, etc.
                logger.warning(f"Unexpected stop reason: {stop_reason}")
                raw_text = self._extract_text(assistant_msg)
                return Analysis(
                    summary=raw_text[:200],
                    root_cause="Analysis interrupted",
                    recommendations=["Review logs manually"],
                    raw_response=raw_text,
                    tool_calls_made=tool_calls_made
                )

        # Max turns reached
        logger.warning(f"Agent hit max turns ({config.max_agent_turns})")
        return Analysis(
            summary="Investigation reached max turns without conclusion",
            root_cause="Insufficient data or complex issue requiring manual review",
            recommendations=["Review logs and traces manually"],
            raw_response="Max agent turns reached",
            tool_calls_made=tool_calls_made
        )

    def _execute_tool(self, name: str, tool_input: dict) -> str:
        """Dispatch and execute a tool call."""
        if name == "get_logs":
            return self._tool_get_logs(tool_input)
        elif name == "get_traces":
            return self._tool_get_traces(tool_input)
        elif name == "exec_in_pod":
            return self._tool_exec_in_pod(tool_input)
        elif name == "search_web":
            return self._tool_search_web(tool_input)
        else:
            return f"Unknown tool: {name}"

    def _tool_get_logs(self, params: dict) -> str:
        """Fetch logs from ClickHouse."""
        namespace = params.get("namespace", "")
        workload = params.get("workload", "")
        pod_name = params.get("pod_name", "")
        minutes = params.get("minutes", config.log_lookback_minutes)

        logs: List[LogEntry] = []

        if workload:
            logs = self.clickhouse.get_logs_for_workload(namespace, workload)
        if not logs and pod_name:
            logs = self.clickhouse.get_logs_for_pod(namespace, pod_name)

        if not logs:
            return "No logs found for this workload/pod in the last {} minutes.".format(minutes)

        lines = []
        for log in logs[:150]:
            ts = log.timestamp.strftime('%H:%M:%S')
            level = (log.level or 'info').upper()
            content = log.content[:500] if log.content else ''
            lines.append(f"[{ts}] [{level}] {content}")

        return f"Found {len(logs)} log entries (showing first {min(len(logs), 150)}):\n" + "\n".join(lines)

    def _tool_get_traces(self, params: dict) -> str:
        """Fetch slow traces from ClickHouse."""
        namespace = params["namespace"]
        workload = params["workload"]

        traces = self.clickhouse.get_slow_traces(namespace, workload)

        if not traces:
            return "No traces found for this workload."

        lines = []
        for t in traces[:20]:
            ts = t.timestamp.strftime('%H:%M:%S')
            dur = f"{t.duration_seconds:.1f}s"
            status = t.status_code or t.status
            lines.append(f"[{ts}] {dur} - {t.span_name} ({status})")

        return f"Found {len(traces)} traces (slowest first):\n" + "\n".join(lines)

    def _find_running_pod(self, namespace: str, pod_name: str) -> str:
        """Find a running pod for the same workload. Falls back to original pod_name."""
        if not self.k8s_api:
            return pod_name
        try:
            # Extract workload name from pod name (remove replicaset hash and pod hash)
            # e.g. "leaky-service-645b96f484-f22dv" -> "leaky-service"
            parts = pod_name.rsplit('-', 2)
            if len(parts) >= 3:
                workload = parts[0]
            else:
                workload = pod_name.rsplit('-', 1)[0]

            pods = self.k8s_api.list_namespaced_pod(namespace)
            for pod in pods.items:
                if (pod.metadata.name.startswith(workload)
                        and pod.status.phase == 'Running'
                        and pod.status.container_statuses
                        and pod.status.container_statuses[0].ready):
                    logger.info(f"Resolved running pod: {pod.metadata.name} (original: {pod_name})")
                    return pod.metadata.name
        except Exception as e:
            logger.debug(f"Failed to resolve running pod: {e}")
        return pod_name

    def _tool_exec_in_pod(self, params: dict) -> str:
        """Execute a command in a pod, with retry if container is restarting."""
        import time
        namespace = params["namespace"]
        pod_name = params["pod_name"]
        command_str = params["command"]

        # Security: block dangerous commands
        first_word = command_str.strip().split()[0] if command_str.strip() else ""
        if first_word in BLOCKED_COMMANDS:
            return f"Command '{first_word}' is not allowed. Only read-only commands are permitted."

        if not self.k8s_api:
            return "Kubernetes API not available - cannot exec into pod."

        from kubernetes.stream import stream
        exec_command = command_str.split()

        max_retries = 3
        for attempt in range(max_retries):
            # On first failure, try resolving a fresh running pod
            target_pod = pod_name if attempt == 0 else self._find_running_pod(namespace, pod_name)

            try:
                resp = stream(
                    self.k8s_api.connect_get_namespaced_pod_exec,
                    name=target_pod,
                    namespace=namespace,
                    command=exec_command,
                    stderr=True,
                    stdin=False,
                    stdout=True,
                    tty=False,
                )

                if not resp:
                    return "Command returned empty output."

                # Filter secrets from env output
                if first_word in ('printenv', 'env'):
                    lines = []
                    for line in resp.split('\n'):
                        key = line.split('=')[0] if '=' in line else ''
                        if not any(s in key.upper() for s in SECRET_KEYWORDS):
                            lines.append(line)
                    return '\n'.join(lines)

                return resp

            except Exception as e:
                error_msg = str(e).lower()
                is_not_running = ('container not found' in error_msg
                                  or 'not running' in error_msg
                                  or 'not found' in error_msg
                                  or '403' in error_msg
                                  or 'handshake' in error_msg)

                if is_not_running and attempt < max_retries - 1:
                    wait = 5 * (attempt + 1)
                    logger.info(f"Exec attempt {attempt + 1} on {target_pod} failed, retrying in {wait}s...")
                    time.sleep(wait)
                    continue

                if is_not_running:
                    return "Pod container is not running after multiple retries (CrashLoopBackOff). Could not exec into pod to inspect files or memory."
                return f"Exec failed: {e}"

        return "Exec failed after retries."

    def _tool_search_web(self, params: dict) -> str:
        """Search the web using DuckDuckGo."""
        query = params["query"]

        try:
            results = list(self.web_searcher.text(query, max_results=5, region='wt-wt'))

            if not results:
                return "No web results found."

            formatted = []
            for r in results:
                title = r.get('title', '')
                body = r.get('body', '')
                url = r.get('href', '')
                formatted.append(f"**{title}**\n{body}\nURL: {url}")

            return f"Found {len(results)} results:\n\n" + "\n\n".join(formatted)

        except Exception as e:
            return f"Web search failed: {e}"

    def _extract_text(self, message: dict) -> str:
        """Extract text content from an assistant message."""
        parts = []
        for block in message.get("content", []):
            if "text" in block:
                parts.append(block["text"])
        return "\n".join(parts)

    def _parse_response(self, response: str) -> Analysis:
        """Parse the structured SUMMARY/ROOT_CAUSE/RECOMMENDATIONS response."""
        summary = ""
        root_cause = ""
        confidence = "medium"
        recommendations = []

        lines = response.strip().split('\n')
        current_section = None

        for line in lines:
            line = line.strip()
            if line.upper().startswith('SUMMARY:'):
                summary = line[8:].strip()
                current_section = 'summary'
            elif line.upper().startswith('ROOT_CAUSE:') or line.upper().startswith('ROOT CAUSE:'):
                colon_idx = line.index(':')
                root_cause = line[colon_idx + 1:].strip()
                current_section = 'root_cause'
            elif line.upper().startswith('CONFIDENCE:'):
                val = line[11:].strip().lower()
                if val in ('high', 'medium', 'low'):
                    confidence = val
                current_section = 'confidence'
            elif line.upper().startswith('RECOMMENDATION'):
                current_section = 'recommendations'
            elif current_section == 'recommendations' and line.startswith('-'):
                recommendations.append(line[1:].strip())
            elif current_section == 'root_cause' and line and not line.upper().startswith(('RECOMMENDATION', 'CONFIDENCE')):
                root_cause += ' ' + line if root_cause else line

        # Fallback: if Claude didn't follow format, use the raw text intelligently
        if not summary and not root_cause:
            sentences = [s.strip() for s in response.replace('\n', ' ').split('.') if s.strip()]
            if sentences:
                summary = sentences[0][:200]
                if len(sentences) > 1:
                    root_cause = '. '.join(sentences[1:3])
        elif not summary:
            summary = root_cause[:200] if root_cause else response[:200]

        return Analysis(
            summary=summary or response[:200],
            root_cause=root_cause or summary or response[:200],
            recommendations=recommendations or ["Review logs manually"],
            raw_response=response,
            confidence=confidence
        )
