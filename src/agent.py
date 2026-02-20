"""Agentic analyzer using Bedrock Converse API with tool use."""
import json
import logging
from dataclasses import dataclass
from typing import List

import boto3
from botocore.config import Config as BotoConfig

from config import config
from tools import ToolHandler

logger = logging.getLogger(__name__)

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
                        "namespace": {"type": "string", "description": "Kubernetes namespace"},
                        "workload": {"type": "string", "description": "Workload/deployment name"},
                        "pod_name": {"type": "string", "description": "Specific pod name (use if workload query returns nothing)"},
                        "minutes": {"type": "integer", "description": "How far back to look in minutes (default: 30)"}
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
                        "namespace": {"type": "string", "description": "Kubernetes namespace"},
                        "workload": {"type": "string", "description": "Workload/deployment name"}
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
                        "namespace": {"type": "string", "description": "Kubernetes namespace"},
                        "pod_name": {"type": "string", "description": "Pod name to exec into"},
                        "command": {"type": "string", "description": "Command to run (read-only, e.g. 'cat /app/package.json')"}
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
                        "query": {"type": "string", "description": "Search query (e.g. 'nodejs ECONNREFUSED redis kubernetes')"}
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
            retries={'max_attempts': 10, 'mode': 'adaptive'}
        )
        self.bedrock = boto3.client('bedrock-runtime', config=boto_config)
        self.tools = ToolHandler()

    def analyze(self, event) -> Analysis:
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
                raw_text = self._extract_text(assistant_msg)
                logger.info(f"Agent finished after {tool_calls_made} tool calls")
                analysis = self._parse_response(raw_text)
                analysis.tool_calls_made = tool_calls_made
                return analysis

            elif stop_reason == "tool_use":
                tool_results = []
                for block in assistant_msg["content"]:
                    if "toolUse" in block:
                        tool = block["toolUse"]
                        tool_name = tool["name"]
                        tool_input = tool["input"]
                        tool_calls_made += 1

                        logger.info(f"Tool call #{tool_calls_made}: {tool_name}({json.dumps(tool_input)[:200]})")

                        try:
                            result = self.tools.execute(tool_name, tool_input)
                            logger.info(f"Tool result #{tool_calls_made} ({tool_name}): {result[:300]}")
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
                logger.warning(f"Unexpected stop reason: {stop_reason}")
                raw_text = self._extract_text(assistant_msg)
                return Analysis(
                    summary=raw_text[:200],
                    root_cause="Analysis interrupted",
                    recommendations=["Review logs manually"],
                    raw_response=raw_text,
                    tool_calls_made=tool_calls_made
                )

        logger.warning(f"Agent hit max turns ({config.max_agent_turns})")
        return Analysis(
            summary="Investigation inconclusive — could not determine root cause",
            root_cause="Complex issue requiring manual review. The automated investigation gathered data but could not reach a definitive conclusion.",
            recommendations=["Review logs and traces manually in Groundcover"],
            raw_response="Investigation inconclusive",
            tool_calls_made=tool_calls_made,
            confidence="low"
        )

    @staticmethod
    def _extract_text(message: dict) -> str:
        parts = []
        for block in message.get("content", []):
            if "text" in block:
                parts.append(block["text"])
        return "\n".join(parts)

    @staticmethod
    def _parse_response(response: str) -> Analysis:
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
