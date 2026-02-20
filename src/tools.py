"""Tool implementations for the agentic analyzer."""
import logging
import shlex
import time
from typing import List

from config import config
from clickhouse import ClickhouseClient, LogEntry

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
            "exec_in_pod": self._exec_in_pod,
            "search_web": self._search_web,
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
        for log in logs[:150]:
            ts = log.timestamp.strftime('%H:%M:%S')
            level = (log.level or 'info').upper()
            content = log.content[:500] if log.content else ''
            lines.append(f"[{ts}] [{level}] {content}")

        return f"Found {len(logs)} log entries (showing first {min(len(logs), 150)}):\n" + "\n".join(lines)

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
