"""Alert Analyzer - Main entry point with polling loop."""
import logging
import signal
import sys
import time
from datetime import datetime, timedelta
from typing import Dict

from config import config
from clickhouse import ClickhouseClient, CrashEvent
from analyzer import BedrockAnalyzer
from notifier import SlackNotifier
from research import ResearchAgent

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
        self.analyzer = BedrockAnalyzer()
        self.notifier = SlackNotifier()
        self.research = ResearchAgent(context7_api_key=config.context7_api_key)
        self.running = True
        self.seen_events: Dict[str, datetime] = {}  # key -> last_seen timestamp

    def _is_duplicate(self, event: CrashEvent) -> bool:
        """Check if we've already processed this event recently."""
        key = event.key
        now = datetime.now()
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
        now = datetime.now()
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

        # Fetch logs for the workload
        logs = self.clickhouse.get_logs_for_workload(event.namespace, event.workload)

        # If no workload logs, try pod-specific logs
        if not logs:
            logs = self.clickhouse.get_logs_for_pod(event.namespace, event.pod_name)

        # Fetch slow traces for latency analysis
        traces = self.clickhouse.get_slow_traces(event.namespace, event.workload)

        # Format logs/traces for research extraction
        logs_text = "\n".join([log.content for log in logs[:50]])
        traces_text = "\n".join([t.span_name for t in traces[:20]])

        # Run research: web search, pod exec (in parallel)
        logger.info(f"Running research for {event.namespace}/{event.workload}...")
        research_result = self.research.research(
            namespace=event.namespace,
            pod_name=event.pod_name,
            workload=event.workload,
            logs_text=logs_text,
            traces_text=traces_text
        )
        logger.info(f"Research complete: {len(research_result.web_results)} web, {len(research_result.pod_files)} files")

        # Analyze with Bedrock (now includes traces + research)
        analysis = self.analyzer.analyze(event, logs, traces, research_result)
        logger.info(f"Analysis complete: {analysis.summary[:100]}...")

        # Send to Slack
        success = self.notifier.send(event, analysis)
        if success:
            logger.info(f"Sent notification for {event.namespace}/{event.workload}")
        else:
            logger.warning(f"Failed to send notification for {event.namespace}/{event.workload}")

    def poll(self):
        """Poll for new crash events and process them."""
        try:
            events = self.clickhouse.get_crash_events()

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

        while self.running:
            self.poll()
            time.sleep(config.poll_interval_seconds)

    def stop(self):
        """Stop the analyzer gracefully."""
        logger.info("Shutting down Alert Analyzer...")
        self.running = False


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
