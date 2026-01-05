"""Agentic analyzer - uses tools autonomously to investigate crashes."""
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config as BotoConfig

from config import config
from tools import TOOL_CONFIG, execute_tool

logger = logging.getLogger(__name__)

# Agent configuration
MAX_ITERATIONS = 8
MAX_TOKENS = 4096


@dataclass
class CrashEvent:
    """Represents a crash event to investigate."""
    timestamp: str
    namespace: str
    workload: str
    pod_name: str
    reason: str
    message: str

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.workload}/{self.reason}"


@dataclass
class Analysis:
    """Result of the agentic investigation."""
    summary: str
    root_cause: str
    recommendations: List[str]
    tools_used: List[str]
    iterations: int
    raw_response: str


# ============================================================================
# SYSTEM PROMPT
# ============================================================================

SYSTEM_PROMPT = """You are an expert Kubernetes/DevOps engineer investigating a production incident.
A pod crash or unhealthy event has occurred. Your job is to find the ROOT CAUSE and provide actionable fixes.

You have access to these investigation tools:
- query_logs: Search container logs for errors, exceptions, patterns
- query_traces: Find slow requests and high-latency operations (>10s often means event loop blocking)
- web_search: Search the web for error solutions (Stack Overflow, GitHub issues)
- query_docs: Look up official library documentation
- read_pod_file: Read config files from the pod (package.json, requirements.txt, .env)
- list_pod_files: List files in a pod directory
- get_pod_env: Check environment variables

INVESTIGATION STRATEGY:
1. Start with query_logs to find error messages and exceptions
2. If you see slow operations mentioned, use query_traces to find latency issues
3. When you find a specific error, use web_search to find solutions
4. If it's a library issue, use query_docs to check correct usage
5. If config might be wrong, use read_pod_file or get_pod_env

IMPORTANT PATTERNS TO RECOGNIZE:
- "ECONNREFUSED" = Can't connect to a service (database, redis, etc.)
- "ETIMEDOUT" = Connection timeout, service not responding
- "OOMKilled" = Out of memory, need to increase limits or fix memory leak
- "CrashLoopBackOff" = App keeps crashing, check startup errors
- High latency traces (>10s) = Event loop blocking, sync operations
- "SIGTERM" / "SIGKILL" = Pod being killed, check liveness probes

RESPONSE FORMAT (when you have enough info):
Provide a BRIEF, actionable analysis:

SUMMARY: <one short sentence - what happened>
ROOT_CAUSE: <1-2 sentences - why it happened>
RECOMMENDATIONS:
- <most important fix>
- <second fix if needed>

Keep it concise. DevOps engineers need quick answers, not essays."""


# ============================================================================
# AGENTIC INVESTIGATION LOOP
# ============================================================================

class AgentAnalyzer:
    """Agentic analyzer that autonomously investigates crashes."""

    def __init__(self):
        boto_config = BotoConfig(
            region_name=config.bedrock_region,
            retries={'max_attempts': 3, 'mode': 'adaptive'}
        )
        self.client = boto3.client('bedrock-runtime', config=boto_config)

    def investigate(self, event: CrashEvent) -> Analysis:
        """Run the agentic investigation loop."""
        logger.info("=" * 60)
        logger.info(f"[Agent] Starting investigation: {event.namespace}/{event.workload}")
        logger.info(f"[Agent] Reason: {event.reason}, Message: {event.message}")
        logger.info("=" * 60)

        # Build initial message with crash context
        initial_prompt = f"""A Kubernetes incident has occurred. Investigate and find the root cause.

## Incident Details
- **Reason**: {event.reason}
- **Namespace**: {event.namespace}
- **Workload**: {event.workload}
- **Pod**: {event.pod_name}
- **Message**: {event.message}
- **Time**: {event.timestamp}

Start your investigation. Use the tools to gather information and find the root cause."""

        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": [{"text": initial_prompt}]}
        ]

        tools_used: List[str] = []
        iteration = 0
        final_response = None

        while iteration < MAX_ITERATIONS:
            iteration += 1
            logger.info(f"[Agent] Iteration {iteration}/{MAX_ITERATIONS}")

            try:
                # Call Bedrock Converse API with tools
                response = self.client.converse(
                    modelId=config.bedrock_model,
                    messages=messages,
                    system=[{"text": SYSTEM_PROMPT}],
                    toolConfig=TOOL_CONFIG,
                    inferenceConfig={
                        "maxTokens": MAX_TOKENS,
                        "temperature": 0.2,
                    },
                )

                stop_reason = response.get("stopReason")
                output_message = response.get("output", {}).get("message")

                logger.info(f"[Agent] Stop reason: {stop_reason}")

                if not output_message:
                    logger.error("[Agent] No output message from model")
                    break

                # Add assistant response to messages
                messages.append(output_message)

                if stop_reason == "tool_use":
                    # Agent wants to use tools
                    tool_results: List[Dict[str, Any]] = []

                    for content_block in output_message.get("content", []):
                        if "toolUse" in content_block:
                            tool = content_block["toolUse"]
                            tool_name = tool.get("name", "")
                            tool_input = tool.get("input", {})

                            logger.info(f"[Agent] Calling tool: {tool_name}")
                            logger.info(f"[Agent] Input: {json.dumps(tool_input)[:200]}")

                            # Track tools used
                            if tool_name not in tools_used:
                                tools_used.append(tool_name)

                            # Execute the tool
                            tool_result = execute_tool(tool_name, tool_input)
                            logger.info(f"[Agent] Result: {json.dumps(tool_result)[:200]}...")

                            tool_results.append({
                                "toolResult": {
                                    "toolUseId": tool.get("toolUseId"),
                                    "content": [{"json": tool_result}],
                                }
                            })

                        elif "text" in content_block:
                            # Agent is thinking
                            logger.info(f"[Agent] Thinking: {content_block['text'][:150]}...")

                    # Add tool results to messages
                    if tool_results:
                        messages.append({
                            "role": "user",
                            "content": tool_results,
                        })

                elif stop_reason == "end_turn":
                    # Agent is done, extract final response
                    for content_block in output_message.get("content", []):
                        if "text" in content_block:
                            final_response = content_block["text"]
                            break
                    logger.info(f"[Agent] Investigation complete ({len(final_response or '')} chars)")
                    break

                else:
                    logger.warning(f"[Agent] Unexpected stop reason: {stop_reason}")
                    break

            except Exception as e:
                logger.error(f"[Agent] Error in iteration {iteration}: {e}")
                break

        # If max iterations reached, force a summary
        if not final_response and iteration >= MAX_ITERATIONS:
            logger.info("[Agent] Max iterations reached, forcing summary...")
            final_response = self._force_summary(messages)

        # Fallback
        if not final_response:
            final_response = (
                "SUMMARY: Investigation incomplete\n"
                "ROOT_CAUSE: Could not determine - check logs manually\n"
                "RECOMMENDATIONS:\n- Review pod logs in Groundcover"
            )

        logger.info(f"[Agent] Done after {iteration} iterations, used tools: {tools_used}")

        return Analysis(
            summary=self._extract_section(final_response, "SUMMARY"),
            root_cause=self._extract_section(final_response, "ROOT_CAUSE"),
            recommendations=self._extract_recommendations(final_response),
            tools_used=tools_used,
            iterations=iteration,
            raw_response=final_response,
        )

    def _force_summary(self, messages: List[Dict[str, Any]]) -> Optional[str]:
        """Force the agent to provide a summary when max iterations reached."""
        try:
            messages.append({
                "role": "user",
                "content": [{
                    "text": (
                        "You've gathered enough information. Provide your final analysis NOW. "
                        "Use the SUMMARY/ROOT_CAUSE/RECOMMENDATIONS format."
                    )
                }],
            })

            # Call without tools to force text response
            response = self.client.converse(
                modelId=config.bedrock_model,
                messages=messages,
                system=[{"text": SYSTEM_PROMPT}],
                inferenceConfig={
                    "maxTokens": MAX_TOKENS,
                    "temperature": 0.2,
                },
            )

            output = response.get("output", {}).get("message", {})
            for content_block in output.get("content", []):
                if "text" in content_block:
                    return content_block["text"]

        except Exception as e:
            logger.error(f"[Agent] Error forcing summary: {e}")

        return None

    def _extract_section(self, response: str, section: str) -> str:
        """Extract a section from the response."""
        lines = response.split("\n")
        for i, line in enumerate(lines):
            if line.startswith(f"{section}:"):
                content = line[len(section) + 1:].strip()
                # Include continuation lines
                for j in range(i + 1, len(lines)):
                    next_line = lines[j].strip()
                    if next_line.startswith(("SUMMARY:", "ROOT_CAUSE:", "RECOMMENDATIONS:")):
                        break
                    if next_line and not next_line.startswith("-"):
                        content += " " + next_line
                return content
        return response[:200] if section == "SUMMARY" else "See analysis"

    def _extract_recommendations(self, response: str) -> List[str]:
        """Extract recommendations from the response."""
        recommendations = []
        in_recommendations = False

        for line in response.split("\n"):
            line = line.strip()
            if "RECOMMENDATIONS:" in line:
                in_recommendations = True
                continue
            if in_recommendations:
                if line.startswith("-"):
                    recommendations.append(line[1:].strip())
                elif line.startswith(("SUMMARY:", "ROOT_CAUSE:")):
                    break

        return recommendations if recommendations else ["Review logs manually"]
