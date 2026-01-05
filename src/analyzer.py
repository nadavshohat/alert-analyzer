"""Bedrock Claude analyzer for crash root cause analysis."""
import logging
from dataclasses import dataclass
from typing import List
import boto3
from botocore.config import Config as BotoConfig

from config import config
from clickhouse import CrashEvent, LogEntry, TraceEntry
from research import ResearchResult

logger = logging.getLogger(__name__)


@dataclass
class Analysis:
    """Result of AI analysis."""
    summary: str
    root_cause: str
    recommendations: List[str]
    raw_response: str


class BedrockAnalyzer:
    """Analyzes crash events using AWS Bedrock Claude."""

    def __init__(self):
        boto_config = BotoConfig(
            region_name=config.bedrock_region,
            retries={'max_attempts': 3, 'mode': 'adaptive'}
        )
        self.client = boto3.client('bedrock-runtime', config=boto_config)

    def analyze(self, event: CrashEvent, logs: List[LogEntry], traces: List[TraceEntry] = None,
                research: ResearchResult = None) -> Analysis:
        """Analyze a crash event with logs, traces, and research using Claude."""
        # Format logs for the prompt
        log_text = self._format_logs(logs)
        trace_text = self._format_traces(traces) if traces else "No trace data available."
        research_text = self._format_research(research) if research else ""

        prompt = f"""You are a Kubernetes expert analyzing a production incident.
An alert has fired and you need to determine the ROOT CAUSE by analyzing the logs, traces, and research.

## Alert Information
- Reason: {event.reason}
- Namespace: {event.namespace}
- Workload: {event.workload}
- Pod: {event.pod_name}
- Message: {event.message}

## Slowest Traces (high latency = potential event loop blocking)
```
{trace_text}
```

## Recent Logs (most recent first)
```
{log_text}
```
{research_text}
## Your Task
Analyze ALL the data above to provide BRIEF responses:

1. **Summary**: One short sentence (max 15 words)
2. **Root Cause**: 1-2 sentences max. If traces show high latency (>10s), mention event loop blocking.
3. **Recommendation**: ONE actionable fix (most important) - be SPECIFIC based on the research/docs

BE EXTREMELY CONCISE. No filler words. No explaining what logs show. Just the answer.

Respond in this exact format:
SUMMARY: <one short sentence>
ROOT_CAUSE: <1-2 sentences only>
RECOMMENDATIONS:
- <single most important action>
"""

        try:
            response = self.client.converse(
                modelId=config.bedrock_model,
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": prompt}]
                    }
                ],
                inferenceConfig={
                    "maxTokens": config.bedrock_max_tokens,
                    "temperature": 0.3
                }
            )

            # Extract response text
            output_message = response.get('output', {}).get('message', {})
            content_blocks = output_message.get('content', [])

            raw_response = ""
            for block in content_blocks:
                if 'text' in block:
                    raw_response += block['text']

            # Parse the structured response
            return self._parse_response(raw_response)

        except Exception as e:
            logger.error(f"Bedrock analysis failed: {e}")
            return Analysis(
                summary=f"Analysis failed: {str(e)}",
                root_cause="Unable to analyze - API error",
                recommendations=["Check Bedrock connectivity", "Verify IAM permissions"],
                raw_response=str(e)
            )

    def _format_logs(self, logs: List[LogEntry]) -> str:
        """Format logs for the prompt."""
        if not logs:
            return "No logs found for this time period."

        # Limit to first 100 logs to control token usage
        lines = []
        for log in logs[:100]:
            timestamp = log.timestamp.strftime('%H:%M:%S')
            level = log.level.upper() if log.level else 'INFO'
            content = log.content[:500] if log.content else ''  # Truncate long lines
            lines.append(f"[{timestamp}] [{level}] {content}")

        return "\n".join(lines)

    def _format_traces(self, traces: List[TraceEntry]) -> str:
        """Format traces for the prompt (slowest first)."""
        if not traces:
            return "No traces found for this time period."

        lines = []
        for trace in traces[:15]:  # Top 15 slowest
            timestamp = trace.timestamp.strftime('%H:%M:%S')
            duration = f"{trace.duration_seconds:.1f}s"
            status = trace.status_code or trace.status
            lines.append(f"[{timestamp}] {duration} - {trace.span_name} ({status})")

        return "\n".join(lines)

    def _format_research(self, research: ResearchResult) -> str:
        """Format research results for the prompt."""
        sections = []

        if research.web_results:
            sections.append("## Web Search Results (solutions found online)")
            for i, result in enumerate(research.web_results[:3], 1):
                sections.append(f"{i}. {result[:300]}...")

        if research.doc_results:
            sections.append("\n## Documentation Snippets")
            for snippet in research.doc_results[:3]:
                sections.append(f"- {snippet[:200]}...")

        if research.pod_files:
            sections.append("\n## Pod Files")
            for path, content in research.pod_files.items():
                # Truncate file content
                truncated = content[:500] if len(content) > 500 else content
                sections.append(f"### {path}\n```\n{truncated}\n```")

        if sections:
            return "\n" + "\n".join(sections) + "\n"
        return ""

    def _parse_response(self, response: str) -> Analysis:
        """Parse Claude's structured response."""
        summary = ""
        root_cause = ""
        recommendations = []

        lines = response.strip().split('\n')
        current_section = None

        for line in lines:
            line = line.strip()
            if line.startswith('SUMMARY:'):
                summary = line[8:].strip()
                current_section = 'summary'
            elif line.startswith('ROOT_CAUSE:'):
                root_cause = line[11:].strip()
                current_section = 'root_cause'
            elif line.startswith('RECOMMENDATIONS:'):
                current_section = 'recommendations'
            elif current_section == 'recommendations' and line.startswith('-'):
                recommendations.append(line[1:].strip())
            elif current_section == 'summary' and not summary:
                summary = line
            elif current_section == 'root_cause' and not root_cause:
                root_cause = line

        # Fallback if parsing fails
        if not summary:
            summary = response[:200] if len(response) > 200 else response

        return Analysis(
            summary=summary,
            root_cause=root_cause or "See logs for details",
            recommendations=recommendations or ["Review the logs manually"],
            raw_response=response
        )
