"""Tool implementations for the agentic analyzer."""
import logging
import shlex
import time
from typing import List

from config import config
from clickhouse import ClickhouseClient, LogEntry, MetricsSummary

logger = logging.getLogger(__name__)

# Only these commands are allowed in pod exec
ALLOWED_COMMANDS = frozenset([
    'cat', 'head', 'tail', 'less', 'ls', 'dir', 'stat', 'file', 'wc',
    'grep', 'awk', 'sed', 'sort', 'uniq', 'cut', 'tr',
    'printenv', 'env', 'echo',
    'ps', 'top', 'df', 'du', 'free', 'uptime', 'whoami', 'id', 'hostname', 'uname',
    'find', 'which', 'readlink', 'realpath', 'basename', 'dirname',
    'date', 'mount', 'lsof', 'ss', 'ip', 'ifconfig', 'netstat',
])

SHELL_METACHARACTERS = frozenset(['|', ';', '&&', '||', '`', '$(', '>', '<', '&'])

SECRET_KEYWORDS = frozenset([
    'PASSWORD', 'SECRET', 'TOKEN', 'KEY', 'CREDENTIAL', 'PRIVATE', 'API_KEY',
])


def _parse_memory_to_mib(value: str) -> float:
    """Parse a Kubernetes memory string (e.g. '2Gi', '512Mi', '1024M') to MiB."""
    s = value.strip()
    if not s:
        return 0.0
    suffixes = {
        'Ki': 1 / 1024, 'Mi': 1, 'Gi': 1024, 'Ti': 1024 * 1024,
        'K': 1000 / (1024 * 1024), 'M': 1000 * 1000 / (1024 * 1024),
        'G': 1000 ** 3 / (1024 ** 2), 'T': 1000 ** 4 / (1024 ** 2),
    }
    for suffix, factor in sorted(suffixes.items(), key=lambda x: -len(x[0])):
        if s.endswith(suffix):
            try:
                return float(s[:-len(suffix)]) * factor
            except ValueError:
                return 0.0
    try:
        return float(s) / (1024 * 1024)
    except ValueError:
        return 0.0


class ToolHandler:
    """Executes investigation tools: logs, traces, pod exec, web search."""

    def __init__(self):
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
            from ddgs import DDGS
            self._web_searcher = DDGS()
        return self._web_searcher

    def execute(self, name: str, tool_input: dict) -> str:
        """Dispatch a tool call by name."""
        handlers = {
            "get_logs": self._get_logs,
            "get_traces": self._get_traces,
            "get_metrics": self._get_metrics,
            "exec_in_pod": self._exec_in_pod,
            "search_web": self._search_web,
            "describe_pod": self._describe_pod,
            "get_previous_logs": self._get_previous_logs,
        }
        handler = handlers.get(name)
        if not handler:
            return f"Unknown tool: {name}"
        return handler(tool_input)

    # -- get_logs --

    def _get_logs(self, params: dict) -> str:
        namespace = params.get("namespace", "")
        workload = params.get("workload", "")
        pod_name = params.get("pod_name", "")
        minutes = params.get("minutes", config.log_lookback_minutes)

        logs: List[LogEntry] = []
        if workload:
            logs = self.clickhouse.get_logs_for_workload(namespace, workload, minutes)
        if not logs and pod_name:
            logs = self.clickhouse.get_logs_for_pod(namespace, pod_name, minutes)

        if not logs:
            return f"No logs found for this workload/pod in the last {minutes} minutes."

        lines = []
        for log in logs[:200]:
            ts = log.timestamp.strftime('%H:%M:%S')
            level = (log.level or 'info').upper()
            text = (log.body or '')[:1500]
            lines.append(f"[{ts}] [{level}] {text}")

        return f"Found {len(logs)} log entries (showing first {min(len(logs), 200)}):\n" + "\n".join(lines)

    # -- get_traces --

    def _get_traces(self, params: dict) -> str:
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

    # -- get_metrics --

    def _get_metrics(self, params: dict) -> str:
        namespace = params["namespace"]
        pod_name = params["pod_name"]
        minutes = int(params.get("minutes", 15))

        summary: MetricsSummary | None = self.clickhouse.get_metrics_for_pod(namespace, pod_name, minutes)
        if not summary:
            return f"No metrics found for {namespace}/{pod_name} in last {minutes} minutes."

        memory_limit_mb = self._get_memory_limit_mb(namespace, pod_name)
        limit_pct_str = ""
        oom_hint = ""
        if memory_limit_mb:
            pct = (summary.memory_max_mb / memory_limit_mb) * 100
            limit_pct_str = f" ({pct:.0f}% of {memory_limit_mb:.0f} MiB limit)"
            if pct >= 90:
                oom_hint = " [WARNING: peak >= 90% of limit — likely OOMKilled]"

        return (
            f"Metrics for {namespace}/{pod_name} over last {summary.window_minutes} min "
            f"({summary.samples} samples):\n"
            f"  memory peak: {summary.memory_max_mb:.0f} MiB{limit_pct_str}{oom_hint}\n"
            f"  memory avg:  {summary.memory_avg_mb:.0f} MiB\n"
            f"  memory last: {summary.memory_last_mb:.0f} MiB\n"
            f"  cpu peak:    {summary.cpu_max_pct:.1f}%\n"
            f"  cpu avg:     {summary.cpu_avg_pct:.1f}%"
        )

    def _get_memory_limit_mb(self, namespace: str, pod_name: str) -> float:
        """Best-effort memory limit lookup from k8s API (in MiB)."""
        if not self.k8s_api:
            return 0.0
        try:
            pod = self.k8s_api.read_namespaced_pod(name=pod_name, namespace=namespace)
            for c in pod.spec.containers:
                limits = (c.resources.limits or {}) if c.resources else {}
                mem = limits.get("memory")
                if mem:
                    return _parse_memory_to_mib(mem)
        except Exception as e:
            logger.debug(f"Could not fetch memory limit: {e}")
        return 0.0

    # -- exec_in_pod --

    def _exec_in_pod(self, params: dict) -> str:
        namespace = params["namespace"]
        pod_name = params["pod_name"]
        command_str = params["command"]

        # Security: block shell metacharacters
        for meta in SHELL_METACHARACTERS:
            if meta in command_str:
                return f"Command contains disallowed character '{meta}'. Only simple read-only commands are permitted."

        try:
            parsed = shlex.split(command_str)
        except ValueError as e:
            return f"Could not parse command: {e}"

        if not parsed:
            return "Empty command."

        base_cmd = parsed[0].rsplit('/', 1)[-1]
        if base_cmd not in ALLOWED_COMMANDS:
            return f"Command '{base_cmd}' is not allowed. Allowed commands: {', '.join(sorted(ALLOWED_COMMANDS))}"

        if not self.k8s_api:
            return "Kubernetes API not available - cannot exec into pod."

        from kubernetes.stream import stream

        max_retries = 3
        for attempt in range(max_retries):
            target_pod = pod_name if attempt == 0 else self._find_running_pod(namespace, pod_name)

            try:
                resp = stream(
                    self.k8s_api.connect_get_namespaced_pod_exec,
                    name=target_pod,
                    namespace=namespace,
                    command=parsed,
                    stderr=True,
                    stdin=False,
                    stdout=True,
                    tty=False,
                )

                if not resp:
                    return "Command returned empty output."

                if base_cmd in ('printenv', 'env'):
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

    def _find_running_pod(self, namespace: str, pod_name: str, workload: str = "") -> str:
        if not self.k8s_api:
            return pod_name
        try:
            prefix = workload if workload else pod_name.rsplit('-', 1)[0]
            pods = self.k8s_api.list_namespaced_pod(namespace)
            for pod in pods.items:
                if (pod.metadata.name.startswith(prefix + '-')
                        and pod.status.phase == 'Running'
                        and pod.status.container_statuses
                        and pod.status.container_statuses[0].ready):
                    logger.info(f"Resolved running pod: {pod.metadata.name} (original: {pod_name})")
                    return pod.metadata.name
        except Exception as e:
            logger.debug(f"Failed to resolve running pod: {e}")
        return pod_name

    # -- describe_pod --

    def _describe_pod(self, params: dict) -> str:
        namespace = params["namespace"]
        pod_name = params["pod_name"]

        if not self.k8s_api:
            return "Kubernetes API not available."

        try:
            pod = self.k8s_api.read_namespaced_pod(name=pod_name, namespace=namespace)
            from kubernetes.client import ApiClient
            pod_dict = ApiClient().sanitize_for_serialization(pod)
            import json

            # Surface termination reasons up front so they're not buried in JSON.
            highlights = []
            for c in (pod.status.container_statuses or []):
                rc = c.restart_count or 0
                last_term = (c.last_state.terminated if c.last_state else None)
                if last_term and last_term.reason:
                    highlights.append(
                        f"  container {c.name}: lastState.terminated.reason={last_term.reason} "
                        f"exitCode={last_term.exit_code} restartCount={rc} at {last_term.finished_at}"
                    )
                else:
                    highlights.append(f"  container {c.name}: restartCount={rc}")

            header = (
                f"=== TERMINATION SUMMARY (read this FIRST) ===\n"
                + ("\n".join(highlights) if highlights else "  (no container statuses available)")
                + "\n=== FULL POD SPEC ===\n"
            )
            return header + json.dumps(pod_dict, indent=2, default=str)
        except Exception as e:
            return f"Failed to describe pod: {e}"

    # -- get_previous_logs --

    def _get_previous_logs(self, params: dict) -> str:
        """Logs of the previous (crashed) container instance, via the k8s API
        (equivalent to `logs --previous`). This is where a startup panic or
        crash-loop error lives when get_logs (observability platform) is empty,
        because the crashed instance's stdout often never reaches ingestion."""
        namespace = params.get("namespace", "")
        pod_name = params.get("pod_name", "")
        container = params.get("container", "")

        if not namespace or not pod_name:
            return "get_previous_logs requires namespace and pod_name."
        if not self.k8s_api:
            return "Kubernetes API not available."

        try:
            # Default to the crashed container: the one with a terminated
            # lastState, else the first container.
            if not container:
                pod = self.k8s_api.read_namespaced_pod(name=pod_name, namespace=namespace)
                statuses = pod.status.container_statuses or []
                crashed = [c for c in statuses if c.last_state and c.last_state.terminated]
                container = crashed[0].name if crashed else (statuses[0].name if statuses else "")

            logs = self.k8s_api.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                container=container or None,
                previous=True,
                tail_lines=200,
            )
            label = f" [{container}]" if container else ""
            if not logs or not logs.strip():
                return (
                    f"No previous-container logs for {namespace}/{pod_name}{label}. "
                    "The crashed container may have exited before writing anything."
                )
            return f"Previous (crashed) container logs for {namespace}/{pod_name}{label}:\n" + logs[-6000:]
        except Exception as e:
            return f"Failed to fetch previous-container logs: {e}"

    def is_pod_terminating(self, namespace: str, pod_name: str) -> bool:
        """Check if a pod is being deleted (terminating)."""
        if not self.k8s_api:
            return False
        try:
            pod = self.k8s_api.read_namespaced_pod(name=pod_name, namespace=namespace)
            return pod.metadata.deletion_timestamp is not None
        except Exception:
            # Pod not found = already gone
            return True

    # -- search_web --

    def _search_web(self, params: dict) -> str:
        query = params["query"]
        try:
            results = list(self.web_searcher.text(query, max_results=5))
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
