"""Alert Analyzer - Main entry point with polling loop."""
import logging
import signal
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from config import config
from clickhouse import ClickhouseClient, CrashEvent
from agent import AgentAnalyzer
from notifier import SlackNotifier

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


class AlertAnalyzer:
    """Main orchestrator for crash detection and analysis."""

    def __init__(self):
        self.clickhouse = ClickhouseClient()
        self.agent = AgentAnalyzer()
        self.notifier = SlackNotifier()
        self.seen_events: Dict[str, datetime] = {}  # key -> last_seen timestamp
        self.last_poll_time: Optional[datetime] = None
        self._shutdown = threading.Event()

    def _is_duplicate(self, event: CrashEvent) -> bool:
        """Check if we've already processed this event recently."""
        key = event.key
        now = datetime.now(timezone.utc)
        dedup_window = timedelta(seconds=config.dedup_window_seconds)

        if key in self.seen_events:
            last_seen = self.seen_events[key]
            if now - last_seen < dedup_window:
                logger.debug(f"Skipping duplicate event: {key}")
                return True

        # Update last seen time
        self.seen_events[key] = now
        return False

    def _cleanup_seen_events(self):
        """Remove old entries from the dedup cache."""
        now = datetime.now(timezone.utc)
        dedup_window = timedelta(seconds=config.dedup_window_seconds)

        expired_keys = [
            key for key, last_seen in self.seen_events.items()
            if now - last_seen > dedup_window * 2
        ]

        for key in expired_keys:
            del self.seen_events[key]

    def process_event(self, event: CrashEvent):
        """Process a single crash event."""
        logger.info(f"Processing crash event: {event.namespace}/{event.workload} - {event.reason}")

        # Agent investigates autonomously using tools
        analysis = self.agent.analyze(event)
        logger.info(f"Analysis complete ({analysis.tool_calls_made} tool calls): {analysis.summary[:100]}...")

        # Send to Slack
        success = self.notifier.send(event, analysis)
        if success:
            logger.info(f"Sent notification for {event.namespace}/{event.workload}")
        else:
            logger.warning(f"Failed to send notification for {event.namespace}/{event.workload}")

    def poll(self):
        """Poll for new crash events and process them."""
        try:
            poll_start = datetime.now(timezone.utc)
            events = self.clickhouse.get_crash_events(since_timestamp=self.last_poll_time)
            self.last_poll_time = poll_start

            for event in events:
                if not self._is_duplicate(event):
                    self.process_event(event)

            # Cleanup old dedup entries periodically
            self._cleanup_seen_events()

        except Exception as e:
            logger.error(f"Error during polling: {e}")

    def run(self):
        """Main run loop."""
        logger.info(f"Alert Analyzer starting - monitoring cluster {config.cluster_name}")
        logger.info(f"Polling interval: {config.poll_interval_seconds}s")
        logger.info(f"Dedup window: {config.dedup_window_seconds}s")
        logger.info(f"Monitoring events: {config.event_reasons}")
        logger.info(f"Excluding namespaces: {config.exclude_namespaces}")

        while not self._shutdown.is_set():
            self.poll()
            self._shutdown.wait(timeout=config.poll_interval_seconds)

    def stop(self):
        """Stop the analyzer gracefully."""
        logger.info("Shutting down Alert Analyzer...")
        self._shutdown.set()


def main():
    """Entry point."""
    analyzer = AlertAnalyzer()

    # Handle graceful shutdown
    def signal_handler(signum, frame):
        analyzer.stop()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        analyzer.run()
    except KeyboardInterrupt:
        analyzer.stop()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
