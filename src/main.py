"""Alert Analyzer - Main entry point with polling loop."""
import logging
import signal
import sys
import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from config import config
from clickhouse import ClickhouseClient, CrashEvent
from agent import AgentAnalyzer
from notifier import SlackNotifier
from tools import ToolHandler

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
        self.k8s_tools = ToolHandler()
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

    def _is_pod_healthy(self, event: CrashEvent) -> bool:
        """Check if the pod is currently ready (all containers ready)."""
        if not self.k8s_tools.k8s_api:
            return False
        try:
            pod = self.k8s_tools.k8s_api.read_namespaced_pod(
                name=event.pod_name, namespace=event.namespace
            )
            if not pod.status.container_statuses:
                return False
            return all(cs.ready for cs in pod.status.container_statuses)
        except Exception:
            # Pod not found = already replaced
            return False

    def process_event(self, event: CrashEvent):
        """Process a single crash event."""
        logger.info(f"Event detected: {event.namespace}/{event.workload} - {event.reason}, waiting 30s before analysis...")
        time.sleep(30)

        # Pre-check: if pod is already healthy, skip entirely
        if self._is_pod_healthy(event):
            logger.info(f"Skipping {event.namespace}/{event.workload} - pod is healthy after 30s wait (transient)")
            return

        # Agent investigates autonomously using tools
        analysis = self.agent.analyze(event)
        logger.info(f"Analysis complete ({analysis.tool_calls_made} tool calls): {analysis.summary[:100]}...")

        # Skip auto-resolved — no need to notify on transient issues
        if analysis.resolved:
            logger.info(f"Skipping notification for {event.namespace}/{event.workload} - auto-resolved")
            return

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
                if self._is_duplicate(event):
                    continue
                # Skip Unhealthy events for pods that are terminating (shutdown probe noise)
                if event.reason == "Unhealthy" and self.k8s_tools.is_pod_terminating(event.namespace, event.pod_name):
                    logger.info(f"Skipping Unhealthy event for terminating pod: {event.namespace}/{event.pod_name}")
                    continue
                # Skip Unhealthy events from infra namespaces (e.g. istio startup probes)
                if event.reason == "Unhealthy" and event.namespace in config.unhealthy_skip_namespaces:
                    logger.info(f"Skipping Unhealthy event in {event.namespace} (unhealthy_skip_namespaces)")
                    continue
                # Skip startup probe failures — transient during pod initialization
                if event.reason == "Unhealthy" and "Startup probe failed" in (event.message or ""):
                    logger.info(f"Skipping startup probe failure: {event.namespace}/{event.pod_name}")
                    continue
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
