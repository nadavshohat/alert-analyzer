"""Research module - web search and pod exec."""
import logging
import re
import concurrent.futures
from dataclasses import dataclass
from typing import List, Optional

from kubernetes import client, config as k8s_config
from kubernetes.stream import stream
from duckduckgo_search import DDGS

logger = logging.getLogger(__name__)


@dataclass
class ResearchResult:
    """Combined research results."""
    web_results: List[str]
    doc_results: List[str]  # Kept for compatibility, but not used
    pod_files: dict  # filename -> content


class WebSearcher:
    """Free web search using DuckDuckGo."""

    def search(self, query: str, max_results: int = 5) -> List[str]:
        """Search the web for solutions."""
        try:
            ddgs = DDGS()
            # Use list() to materialize results, region='wt-wt' for worldwide
            results = list(ddgs.text(query, max_results=max_results, region='wt-wt'))

            formatted = []
            for r in results:
                title = r.get('title', '')
                body = r.get('body', '')
                url = r.get('href', '')
                formatted.append(f"**{title}**\n{body}\nURL: {url}")

            logger.info(f"DuckDuckGo found {len(formatted)} results for: {query[:50]}...")
            return formatted
        except Exception as e:
            logger.warning(f"DuckDuckGo search failed: {e}")
            return []


class PodExec:
    """Execute commands in pods to read files."""

    def __init__(self):
        self._api = None

    @property
    def api(self):
        """Lazy-load K8s API."""
        if self._api is None:
            try:
                k8s_config.load_incluster_config()
                self._api = client.CoreV1Api()
                logger.info("Loaded in-cluster K8s config")
            except k8s_config.ConfigException:
                try:
                    k8s_config.load_kube_config()
                    self._api = client.CoreV1Api()
                    logger.info("Loaded kubeconfig")
                except Exception as e:
                    logger.warning(f"Could not load kubernetes config: {e}")
        return self._api

    def read_file(self, namespace: str, pod_name: str, file_path: str, container: Optional[str] = None) -> Optional[str]:
        """Read a file from a pod."""
        if not self.api:
            return None

        try:
            exec_command = ['cat', file_path]
            kwargs = {
                'name': pod_name,
                'namespace': namespace,
                'command': exec_command,
                'stderr': True,
                'stdin': False,
                'stdout': True,
                'tty': False,
            }
            if container:
                kwargs['container'] = container

            resp = stream(self.api.connect_get_namespaced_pod_exec, **kwargs)

            if resp and 'No such file' not in resp and 'cannot access' not in resp.lower():
                logger.info(f"Read {len(resp)} bytes from {pod_name}:{file_path}")
                return resp
            return None
        except Exception as e:
            logger.debug(f"Failed to read {file_path} from {pod_name}: {e}")
            return None

    def get_env_vars(self, namespace: str, pod_name: str, container: Optional[str] = None) -> Optional[str]:
        """Get environment variables from a pod (filtered for safety)."""
        if not self.api:
            return None

        try:
            exec_command = ['printenv']
            kwargs = {
                'name': pod_name,
                'namespace': namespace,
                'command': exec_command,
                'stderr': True,
                'stdin': False,
                'stdout': True,
                'tty': False,
            }
            if container:
                kwargs['container'] = container

            resp = stream(self.api.connect_get_namespaced_pod_exec, **kwargs)

            # Filter out sensitive env vars
            lines = []
            for line in resp.split('\n'):
                key = line.split('=')[0] if '=' in line else ''
                if not any(s in key.upper() for s in ['PASSWORD', 'SECRET', 'TOKEN', 'KEY', 'CREDENTIAL', 'PRIVATE']):
                    lines.append(line)
            return '\n'.join(lines)
        except Exception as e:
            logger.debug(f"Failed to get env vars from {pod_name}: {e}")
            return None


class ResearchAgent:
    """Orchestrates all research capabilities."""

    def __init__(self, context7_api_key: Optional[str] = None):
        self.web_search = WebSearcher()
        self.pod_exec = PodExec()
        # Note: context7_api_key kept for compatibility but Context7 requires MCP, not REST API

    def extract_technologies(self, logs: str, traces: str) -> List[str]:
        """Extract technology keywords from logs/traces."""
        techs = set()

        patterns = {
            r'node|nodejs|npm': 'nodejs',
            r'mongo|mongodb': 'mongodb',
            r'redis': 'redis',
            r'postgres|postgresql|pg': 'postgresql',
            r'mysql': 'mysql',
            r'express': 'express',
            r'fastify': 'fastify',
            r'nest|nestjs': 'nestjs',
            r'prisma': 'prisma',
            r'typeorm': 'typeorm',
            r'axios': 'axios',
        }

        combined = (logs + traces).lower()
        for pattern, tech in patterns.items():
            if re.search(pattern, combined):
                techs.add(tech)

        return list(techs)

    def extract_errors(self, logs: str) -> List[str]:
        """Extract error messages for web search."""
        errors = []
        error_patterns = [
            r'Error:?\s*(.{20,100})',
            r'Exception:?\s*(.{20,100})',
            r'FATAL:?\s*(.{20,100})',
            r'failed:?\s*(.{20,100})',
            r'ECONNREFUSED|ETIMEDOUT|ENOTFOUND',
            r'OOM|OutOfMemory|heap out of memory',
        ]

        for pattern in error_patterns:
            matches = re.findall(pattern, logs, re.IGNORECASE)
            if isinstance(matches, list) and matches:
                if isinstance(matches[0], tuple):
                    errors.extend([m[0] for m in matches[:3]])
                else:
                    errors.extend(matches[:3])

        # Deduplicate and limit
        unique_errors = list(set(errors))[:5]
        return unique_errors

    def research(self, namespace: str, pod_name: str, workload: str,
                 logs_text: str, traces_text: str) -> ResearchResult:
        """Run all research in parallel."""

        web_results = []
        pod_files = {}

        # Extract what to search for
        technologies = self.extract_technologies(logs_text, traces_text)
        errors = self.extract_errors(logs_text)

        logger.info(f"Research: found {len(errors)} errors, {len(technologies)} techs")

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = {}

            # Web search for errors (most important)
            for error in errors[:3]:
                # Clean up error message for search
                clean_error = re.sub(r'[^\w\s\-]', ' ', error)[:80]
                query = f"{' '.join(technologies[:2])} {clean_error}"
                future = executor.submit(self._web_search_task, query)
                futures[future] = ('web', query)

            # Web search for general issue pattern
            if technologies:
                query = f"{technologies[0]} health check timeout kubernetes"
                future = executor.submit(self._web_search_task, query)
                futures[future] = ('web', query)

            # Pod file reads
            common_files = [
                '/app/package.json',
                '/app/requirements.txt',
                '/app/Dockerfile',
            ]
            for file_path in common_files:
                future = executor.submit(self._pod_read_task, namespace, pod_name, file_path)
                futures[future] = ('file', file_path)

            # Collect results with timeout
            for future in concurrent.futures.as_completed(futures, timeout=15):
                task_type, task_id = futures[future]
                try:
                    result = future.result(timeout=5)
                    if result:
                        if task_type == 'web' and result.get('data'):
                            web_results.extend(result.get('data', []))
                            logger.info(f"Web search returned {len(result.get('data', []))} results")
                        elif task_type == 'file' and result.get('data'):
                            pod_files[result.get('path')] = result.get('data')
                            logger.info(f"Read pod file: {result.get('path')}")
                except concurrent.futures.TimeoutError:
                    logger.warning(f"Research task timed out: {task_type}:{task_id}")
                except Exception as e:
                    logger.warning(f"Research task failed ({task_type}): {e}")

        logger.info(f"Research complete: {len(web_results)} web results, {len(pod_files)} pod files")

        return ResearchResult(
            web_results=web_results[:5],
            doc_results=[],  # Context7 requires MCP, not available here
            pod_files=pod_files
        )

    def _web_search_task(self, query: str) -> dict:
        results = self.web_search.search(query, max_results=3)
        return {'type': 'web', 'data': results}

    def _pod_read_task(self, namespace: str, pod_name: str, file_path: str) -> dict:
        content = self.pod_exec.read_file(namespace, pod_name, file_path)
        if content:
            return {'type': 'file', 'path': file_path, 'data': content}
        return {}
