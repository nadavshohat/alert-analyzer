"""Tool definitions and implementations for the agentic analyzer."""
import logging
import re
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta

import requests
from duckduckgo_search import DDGS
from kubernetes import client as k8s_client, config as k8s_config
from kubernetes.stream import stream

from config import config

logger = logging.getLogger(__name__)

# ============================================================================
# TOOL DEFINITIONS - What the agent can use
# ============================================================================

TOOL_CONFIG: Dict[str, Any] = {
    "tools": [
        {
            "toolSpec": {
                "name": "query_logs",
                "description": (
                    "Search container logs in Clickhouse. Use this to find log entries "
                    "related to errors, exceptions, or specific patterns. "
                    "Returns log messages with timestamps and log levels."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "namespace": {
                                "type": "string",
                                "description": "Kubernetes namespace to search logs in",
                            },
                            "workload": {
                                "type": "string",
                                "description": "Workload name (deployment/statefulset name)",
                            },
                            "level": {
                                "type": "string",
                                "description": "Log level filter: error, warn, info, debug (optional)",
                            },
                            "pattern": {
                                "type": "string",
                                "description": "Text pattern to search for in log content (optional)",
                            },
                            "minutes": {
                                "type": "number",
                                "description": "How many minutes back to search (default: 30)",
                                "default": 30,
                            },
                            "limit": {
                                "type": "number",
                                "description": "Maximum logs to return (default: 100)",
                                "default": 100,
                            },
                        },
                        "required": ["namespace", "workload"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "query_traces",
                "description": (
                    "Search distributed traces in Clickhouse. Use this to find slow requests, "
                    "high latency operations, or failed HTTP calls. Traces show request flow "
                    "and timing across services. High latency (>10s) often indicates event loop blocking."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "namespace": {
                                "type": "string",
                                "description": "Kubernetes namespace",
                            },
                            "workload": {
                                "type": "string",
                                "description": "Workload name",
                            },
                            "min_duration_ms": {
                                "type": "number",
                                "description": "Minimum duration in milliseconds to filter slow traces (default: 1000)",
                                "default": 1000,
                            },
                            "status_code": {
                                "type": "string",
                                "description": "HTTP status code filter, e.g., '500', '4xx', '5xx' (optional)",
                            },
                            "limit": {
                                "type": "number",
                                "description": "Maximum traces to return (default: 20)",
                                "default": 20,
                            },
                        },
                        "required": ["namespace", "workload"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "web_search",
                "description": (
                    "Search the web for solutions to errors, exceptions, or technical issues. "
                    "Use this when you see a specific error message and want to find solutions "
                    "or explanations from Stack Overflow, GitHub issues, or documentation."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": (
                                    "Search query. Include the error message or technology name. "
                                    "Example: 'Node.js ECONNREFUSED connection refused postgresql'"
                                ),
                            },
                            "max_results": {
                                "type": "number",
                                "description": "Maximum results to return (default: 5)",
                                "default": 5,
                            },
                        },
                        "required": ["query"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "query_docs",
                "description": (
                    "Look up official documentation for a library or framework using Context7. "
                    "Use this when you need to find correct usage, configuration options, "
                    "or best practices for a specific technology."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "library": {
                                "type": "string",
                                "description": (
                                    "Library or framework name. Examples: 'express', 'fastify', "
                                    "'prisma', 'typeorm', 'nestjs', 'axios'"
                                ),
                            },
                            "query": {
                                "type": "string",
                                "description": (
                                    "What to look up in the docs. Example: 'connection timeout configuration'"
                                ),
                            },
                        },
                        "required": ["library", "query"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "read_pod_file",
                "description": (
                    "Read a file from inside the crashing pod. Use this to check configuration files, "
                    "package.json, requirements.txt, or source code that might be causing the issue."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "namespace": {
                                "type": "string",
                                "description": "Kubernetes namespace",
                            },
                            "pod_name": {
                                "type": "string",
                                "description": "Pod name",
                            },
                            "file_path": {
                                "type": "string",
                                "description": (
                                    "File path to read. Common paths: '/app/package.json', "
                                    "'/app/requirements.txt', '/app/config.js', '/app/.env'"
                                ),
                            },
                            "container": {
                                "type": "string",
                                "description": "Container name (optional, uses first container if not specified)",
                            },
                        },
                        "required": ["namespace", "pod_name", "file_path"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "list_pod_files",
                "description": (
                    "List files in a directory inside the pod. Use this to discover "
                    "what configuration files or source files exist before reading them."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "namespace": {
                                "type": "string",
                                "description": "Kubernetes namespace",
                            },
                            "pod_name": {
                                "type": "string",
                                "description": "Pod name",
                            },
                            "directory": {
                                "type": "string",
                                "description": "Directory to list. Common: '/app', '/etc', '/config'",
                                "default": "/app",
                            },
                            "container": {
                                "type": "string",
                                "description": "Container name (optional)",
                            },
                        },
                        "required": ["namespace", "pod_name"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "get_pod_env",
                "description": (
                    "Get environment variables from the pod. Use this to check if "
                    "required configuration is missing or incorrect."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "namespace": {
                                "type": "string",
                                "description": "Kubernetes namespace",
                            },
                            "pod_name": {
                                "type": "string",
                                "description": "Pod name",
                            },
                            "filter": {
                                "type": "string",
                                "description": "Filter env vars containing this string (optional). Example: 'DATABASE'",
                            },
                            "container": {
                                "type": "string",
                                "description": "Container name (optional)",
                            },
                        },
                        "required": ["namespace", "pod_name"],
                    }
                },
            }
        },
    ]
}


# ============================================================================
# CLICKHOUSE CLIENT
# ============================================================================

class ClickhouseClient:
    """Client for querying Clickhouse."""

    def __init__(self):
        self.base_url = f"http://{config.clickhouse_host}:{config.clickhouse_port}"
        self.auth = (config.clickhouse_user, config.clickhouse_password)

    def query(self, sql: str) -> List[Dict[str, Any]]:
        """Execute a query and return results as list of dicts."""
        try:
            response = requests.post(
                self.base_url,
                params={"database": config.clickhouse_database, "default_format": "JSON"},
                data=sql,
                auth=self.auth,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("data", [])
        except Exception as e:
            logger.error(f"Clickhouse query failed: {e}")
            return []


# ============================================================================
# TOOL IMPLEMENTATIONS
# ============================================================================

clickhouse = ClickhouseClient()


def execute_query_logs(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Search container logs in Clickhouse."""
    namespace = tool_input.get("namespace", "")
    workload = tool_input.get("workload", "")
    level = tool_input.get("level", "")
    pattern = tool_input.get("pattern", "")
    minutes = tool_input.get("minutes", 30)
    limit = min(tool_input.get("limit", 100), 200)

    logger.info(f"[Tool: query_logs] namespace={namespace}, workload={workload}, level={level}, pattern={pattern}")

    try:
        # Build query
        conditions = [
            f"namespace = '{namespace}'",
            f"workload = '{workload}'",
            f"timestamp > now() - INTERVAL {minutes} MINUTE",
        ]

        if level:
            conditions.append(f"lower(level) = '{level.lower()}'")

        if pattern:
            # Escape single quotes in pattern
            safe_pattern = pattern.replace("'", "\\'")
            conditions.append(f"content ILIKE '%{safe_pattern}%'")

        sql = f"""
            SELECT timestamp, level, content, pod_name, container_name
            FROM logs
            WHERE {' AND '.join(conditions)}
            ORDER BY timestamp DESC
            LIMIT {limit}
        """

        results = clickhouse.query(sql)

        if not results:
            return {
                "success": True,
                "logs": [],
                "message": f"No logs found for {namespace}/{workload}",
            }

        # Format logs
        logs = []
        for row in results:
            logs.append({
                "timestamp": row.get("timestamp"),
                "level": row.get("level", "INFO"),
                "content": (row.get("content") or "")[:500],
                "pod": row.get("pod_name"),
                "container": row.get("container_name"),
            })

        return {
            "success": True,
            "logCount": len(logs),
            "logs": logs,
            "namespace": namespace,
            "workload": workload,
        }
    except Exception as e:
        logger.error(f"[Tool: query_logs] Error: {e}")
        return {"success": False, "error": str(e)}


def execute_query_traces(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Search distributed traces in Clickhouse."""
    namespace = tool_input.get("namespace", "")
    workload = tool_input.get("workload", "")
    min_duration_ms = tool_input.get("min_duration_ms", 1000)
    status_code = tool_input.get("status_code", "")
    limit = min(tool_input.get("limit", 20), 50)

    logger.info(f"[Tool: query_traces] namespace={namespace}, workload={workload}, min_duration={min_duration_ms}ms")

    try:
        conditions = [
            f"namespace = '{namespace}'",
            f"workload = '{workload}'",
            f"timestamp > now() - INTERVAL 30 MINUTE",
            f"durationNano / 1000000 >= {min_duration_ms}",
        ]

        if status_code:
            if status_code.endswith("xx"):
                prefix = status_code[0]
                conditions.append(f"statusCode >= {prefix}00 AND statusCode < {int(prefix)+1}00")
            else:
                conditions.append(f"statusCode = {status_code}")

        sql = f"""
            SELECT
                timestamp,
                spanName,
                durationNano / 1000000000.0 as duration_seconds,
                statusCode,
                status
            FROM traces
            WHERE {' AND '.join(conditions)}
            ORDER BY durationNano DESC
            LIMIT {limit}
        """

        results = clickhouse.query(sql)

        if not results:
            return {
                "success": True,
                "traces": [],
                "message": f"No slow traces found for {namespace}/{workload}",
            }

        traces = []
        for row in results:
            traces.append({
                "timestamp": row.get("timestamp"),
                "spanName": row.get("spanName"),
                "duration_seconds": round(row.get("duration_seconds", 0), 2),
                "statusCode": row.get("statusCode"),
                "status": row.get("status"),
            })

        return {
            "success": True,
            "traceCount": len(traces),
            "traces": traces,
            "namespace": namespace,
            "workload": workload,
        }
    except Exception as e:
        logger.error(f"[Tool: query_traces] Error: {e}")
        return {"success": False, "error": str(e)}


def execute_web_search(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Search the web using DuckDuckGo."""
    query = tool_input.get("query", "")
    max_results = min(tool_input.get("max_results", 5), 10)

    logger.info(f"[Tool: web_search] query={query}")

    try:
        results = DDGS().text(query, max_results=max_results)

        if not results:
            return {
                "success": True,
                "results": [],
                "message": f"No web results found for: {query}",
            }

        formatted = []
        for r in results:
            formatted.append({
                "title": r.get("title", ""),
                "snippet": r.get("body", "")[:300],
                "url": r.get("href", ""),
            })

        return {
            "success": True,
            "resultCount": len(formatted),
            "results": formatted,
            "query": query,
        }
    except Exception as e:
        logger.error(f"[Tool: web_search] Error: {e}")
        return {"success": False, "error": str(e)}


def execute_query_docs(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Query Context7 for library documentation."""
    library = tool_input.get("library", "")
    query = tool_input.get("query", "")

    logger.info(f"[Tool: query_docs] library={library}, query={query}")

    if not config.context7_api_key:
        return {"success": False, "error": "Context7 API key not configured"}

    try:
        headers = {"Authorization": f"Bearer {config.context7_api_key}"}

        # First resolve library ID
        resolve_resp = requests.get(
            "https://api.context7.com/v1/resolve",
            params={"query": library, "libraryName": library},
            headers=headers,
            timeout=10,
        )

        if resolve_resp.status_code != 200:
            return {"success": False, "error": f"Library not found: {library}"}

        resolve_data = resolve_resp.json()
        libraries = resolve_data.get("libraries", [])

        if not libraries:
            return {"success": False, "error": f"No documentation found for: {library}"}

        library_id = libraries[0].get("id")

        # Query docs
        docs_resp = requests.post(
            "https://api.context7.com/v1/query",
            json={"libraryId": library_id, "query": query},
            headers=headers,
            timeout=15,
        )

        if docs_resp.status_code != 200:
            return {"success": False, "error": "Failed to query documentation"}

        docs_data = docs_resp.json()
        snippets = docs_data.get("snippets", [])

        if not snippets:
            return {
                "success": True,
                "snippets": [],
                "message": f"No relevant docs found for {query} in {library}",
            }

        formatted = []
        for s in snippets[:5]:
            formatted.append({
                "content": s.get("content", "")[:400],
                "source": s.get("source", ""),
            })

        return {
            "success": True,
            "library": library,
            "snippetCount": len(formatted),
            "snippets": formatted,
        }
    except Exception as e:
        logger.error(f"[Tool: query_docs] Error: {e}")
        return {"success": False, "error": str(e)}


# Initialize K8s client
try:
    k8s_config.load_incluster_config()
except:
    try:
        k8s_config.load_kube_config()
    except:
        pass

core_v1 = k8s_client.CoreV1Api()


def execute_read_pod_file(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Read a file from inside a pod."""
    namespace = tool_input.get("namespace", "")
    pod_name = tool_input.get("pod_name", "")
    file_path = tool_input.get("file_path", "")
    container = tool_input.get("container")

    logger.info(f"[Tool: read_pod_file] pod={namespace}/{pod_name}, path={file_path}")

    try:
        exec_command = ["cat", file_path]

        resp = stream(
            core_v1.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            container=container,
            command=exec_command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
        )

        if not resp or "No such file" in resp:
            return {
                "success": False,
                "error": f"File not found: {file_path}",
            }

        # Truncate large files
        content = resp[:2000] if len(resp) > 2000 else resp

        return {
            "success": True,
            "path": file_path,
            "content": content,
            "truncated": len(resp) > 2000,
        }
    except Exception as e:
        logger.error(f"[Tool: read_pod_file] Error: {e}")
        return {"success": False, "error": str(e)}


def execute_list_pod_files(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """List files in a pod directory."""
    namespace = tool_input.get("namespace", "")
    pod_name = tool_input.get("pod_name", "")
    directory = tool_input.get("directory", "/app")
    container = tool_input.get("container")

    logger.info(f"[Tool: list_pod_files] pod={namespace}/{pod_name}, dir={directory}")

    try:
        exec_command = ["ls", "-la", directory]

        resp = stream(
            core_v1.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            container=container,
            command=exec_command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
        )

        if not resp:
            return {"success": False, "error": f"Directory not found: {directory}"}

        # Parse ls output
        files = []
        for line in resp.strip().split("\n")[1:]:  # Skip total line
            parts = line.split()
            if len(parts) >= 9:
                files.append({
                    "permissions": parts[0],
                    "size": parts[4],
                    "name": " ".join(parts[8:]),
                })

        return {
            "success": True,
            "directory": directory,
            "fileCount": len(files),
            "files": files[:30],  # Limit to 30 files
        }
    except Exception as e:
        logger.error(f"[Tool: list_pod_files] Error: {e}")
        return {"success": False, "error": str(e)}


def execute_get_pod_env(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Get environment variables from a pod."""
    namespace = tool_input.get("namespace", "")
    pod_name = tool_input.get("pod_name", "")
    env_filter = tool_input.get("filter", "")
    container = tool_input.get("container")

    logger.info(f"[Tool: get_pod_env] pod={namespace}/{pod_name}, filter={env_filter}")

    try:
        exec_command = ["env"]

        resp = stream(
            core_v1.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            container=container,
            command=exec_command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
        )

        if not resp:
            return {"success": False, "error": "Could not get environment variables"}

        # Parse and filter env vars
        env_vars = {}
        for line in resp.strip().split("\n"):
            if "=" in line:
                key, _, value = line.partition("=")
                # Filter if specified
                if env_filter and env_filter.upper() not in key.upper():
                    continue
                # Mask sensitive values
                if any(s in key.upper() for s in ["PASSWORD", "SECRET", "TOKEN", "KEY", "CREDENTIAL"]):
                    value = "***MASKED***"
                env_vars[key] = value[:200]  # Truncate long values

        return {
            "success": True,
            "envCount": len(env_vars),
            "envVars": env_vars,
            "filter": env_filter or "none",
        }
    except Exception as e:
        logger.error(f"[Tool: get_pod_env] Error: {e}")
        return {"success": False, "error": str(e)}


# ============================================================================
# TOOL EXECUTOR DISPATCHER
# ============================================================================

def execute_tool(tool_name: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a tool by name."""
    executors = {
        "query_logs": execute_query_logs,
        "query_traces": execute_query_traces,
        "web_search": execute_web_search,
        "query_docs": execute_query_docs,
        "read_pod_file": execute_read_pod_file,
        "list_pod_files": execute_list_pod_files,
        "get_pod_env": execute_get_pod_env,
    }

    executor = executors.get(tool_name)
    if executor:
        return executor(tool_input)
    return {"success": False, "error": f"Unknown tool: {tool_name}"}
