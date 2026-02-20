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
        self.israel_tz = pytz.timezone('Asia/Jerusalem')

    def send(self, event: CrashEvent, analysis: Analysis) -> bool:
        """Send a crash analysis notification to Slack."""
        if not self.webhook_url:
            logger.warning("No Slack webhook URL configured, skipping notification")
            return False

        # Get emoji for severity
        emoji = SEVERITY_EMOJI.get(event.reason, '\U0001F514')  # Bell as default

        # Format timestamp in Israel time
        now_israel = datetime.now(self.israel_tz)
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
  _Message:_ {event.message[:200]}

*Root Cause* _{analysis.confidence} confidence_
> {analysis.root_cause}

*Recommended Action*
{recommendation}

_Last seen:_ {timestamp_str} | _Investigation: {analysis.tool_calls_made} tool calls_ | <{gc_link}|View in Groundcover>"""

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

    def _build_groundcover_link(self, event: CrashEvent) -> str:
        """Build a deep link to the workload in Groundcover UI."""
        base = config.groundcover_base_url.rstrip('/')
        tenant_uuid = config.groundcover_tenant_uuid
        cluster = config.cluster_name
        workload = event.workload

        return (
            f"{base}/infrastructure?"
            f"duration=Last+hour&"
            f"tenantUUID={tenant_uuid}&"
            f"backendId={cluster}&"
            f"selectedTab=Pods&"
            f"freeText={workload}"
        )
