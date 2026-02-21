"""Slack notifier with proper mrkdwn formatting."""
import logging
from datetime import datetime
import pytz
import requests

from config import config
from clickhouse import CrashEvent
from agent import Analysis

logger = logging.getLogger(__name__)

# Emoji for severity based on reason
SEVERITY_EMOJI = {
    'CrashLoopBackOff': '\U0001F534',  # Red circle
    'OOMKilled': '\U0001F534',          # Red circle
    'Failed': '\U0001F534',             # Red circle
    'Error': '\U0001F534',              # Red circle
    'BackOff': '\U0001F7E1',            # Yellow circle
    'Unhealthy': '\U0001F7E1',          # Yellow circle
}


class SlackNotifier:
    """Sends crash analysis to Slack with proper mrkdwn formatting."""

    def __init__(self):
        self.webhook_url = config.slack_webhook_url
        self.tz = pytz.timezone(config.timezone)

    def send(self, event: CrashEvent, analysis: Analysis) -> bool:
        """Send a crash analysis notification to Slack."""
        if not self.webhook_url:
            logger.warning("No Slack webhook URL configured, skipping notification")
            return False

        # Get emoji for severity
        emoji = SEVERITY_EMOJI.get(event.reason, '\U0001F514')  # Bell as default

        # Format timestamp in Israel time
        now_israel = datetime.now(self.tz)
        timestamp_str = now_israel.strftime('%H:%M')

        # Build Groundcover deep link
        gc_link = self._build_groundcover_link(event)

        # Get first recommendation only
        recommendation = analysis.recommendations[0] if analysis.recommendations else "Review logs manually"

        # Build the message using Slack mrkdwn (NOT markdown!)
        message_text = f"""*{emoji} {event.reason}: {event.workload}*

*Summary*
{analysis.summary}

*Findings*
\u2022 *Event:* `{event.reason}`
  _Namespace:_ {event.namespace}
  _Pod:_ `{event.pod_name}`
  _Message:_ {self._clean_message(event.message)}

*Root Cause* _({analysis.confidence} confidence)_
> {analysis.root_cause}

*Recommended Action*
{recommendation}

_Last seen:_ {timestamp_str} | _Investigation: {analysis.tool_calls_made} tool calls_ | <{gc_link}|View in Groundcover>"""

        # Slack section block text has a 3000 char limit
        if len(message_text) > 2900:
            message_text = message_text[:2900] + "\n... (truncated)"

        payload = {
            "text": f"{emoji} {event.reason}: {event.workload} in {event.namespace}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": message_text
                    }
                }
            ]
        }

        try:
            response = requests.post(
                self.webhook_url,
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=10
            )

            if response.status_code == 200:
                logger.info(f"Successfully sent Slack notification for {event.namespace}/{event.workload}")
                return True
            else:
                logger.error(f"Slack returned status {response.status_code}: {response.text}")
                return False

        except requests.RequestException as e:
            logger.error(f"Failed to send Slack notification: {e}")
            return False

    @staticmethod
    def _clean_message(message: str) -> str:
        """Strip Kubernetes pod UIDs and noise from event messages."""
        import re
        # Remove _namespace(uuid) pattern e.g. _staging(5c7c10d3-...)
        cleaned = re.sub(r'_\S+\([0-9a-f-]{36}\)', '', message)
        return cleaned[:200]

    def _build_groundcover_link(self, event: CrashEvent) -> str:
        """Build a deep link to the workload in Groundcover UI."""
        base = config.groundcover_base_url.rstrip('/')
        cluster = config.cluster_name
        workload = event.workload

        return (
            f"{base}/workloads?"
            f"duration=Last+hour&"
            f"backendId={cluster}&"
            f"freeText={workload}"
        )
