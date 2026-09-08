"""Prometheus metric source (METRIC_SOURCE=prometheus). Metrics only."""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from clickhouse import BackendError, MetricsSummary
from config import config

logger = logging.getLogger(__name__)


class PrometheusSource:
    def __init__(self, base_url: str = ""):
        self.base_url = (base_url or config.prometheus_url).rstrip("/")
        self.session = requests.Session()

    def get_metrics_for_pod(self, namespace: str, pod_name: str, minutes: int = 15) -> Optional[MetricsSummary]:
        if not self.base_url:
            raise BackendError("PROMETHEUS_URL is not set")
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=minutes)
        expr = f'container_memory_working_set_bytes{{namespace="{namespace}",pod="{pod_name}",container!=""}}'
        params = {
            "query": expr,
            "start": str(start.timestamp()),
            "end": str(end.timestamp()),
            "step": "30s",
        }
        try:
            resp = self.session.get(f"{self.base_url}/api/v1/query_range", params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.error(f"Prometheus query failed: {e}")
            raise BackendError(f"Prometheus query failed: {e}") from e
        series = data.get("data", {}).get("result", [])
        if not series:
            logger.info(f"No metrics for {namespace}/{pod_name} (prometheus)")
            return None
        # One series per container. Sum each container's max/avg/last so the totals
        # approximate the pod's peak/avg/last memory. cpu has no percent-of-limit
        # basis here, so it is left at 0; memory is the OOM signal that matters.
        total_max = total_avg = total_last = 0.0
        samples = 0
        for s in series:
            vals = [float(v) / (1024**2) for _, v in s.get("values", [])]
            if not vals:
                continue
            samples += len(vals)
            total_max += max(vals)
            total_avg += sum(vals) / len(vals)
            total_last += vals[-1]
        if samples == 0:
            return None
        return MetricsSummary(
            samples=samples, memory_max_mb=total_max, memory_avg_mb=total_avg,
            memory_last_mb=total_last, cpu_max_pct=0.0, cpu_avg_pct=0.0,
            window_minutes=minutes,
        )
