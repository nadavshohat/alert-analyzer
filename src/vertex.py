"""Claude on Vertex AI behind the Bedrock Converse call shape (MODEL_PROVIDER=vertex).

AgentAnalyzer is written against `bedrock.converse(...)`. This client accepts the
same arguments, translates them to the Anthropic Messages API, calls Vertex, and
translates the reply back, so the agent loop runs unchanged on either cloud.
Credentials come from Google Application Default Credentials (Workload Identity
on GKE). The modelId the agent passes is ignored; VERTEX_MODEL names the model.
"""
from typing import Any, Dict, List

from config import config


def _block_to_messages(block: Dict[str, Any]) -> Dict[str, Any]:
    if "text" in block:
        return {"type": "text", "text": block["text"]}
    if "toolUse" in block:
        tu = block["toolUse"]
        return {"type": "tool_use", "id": tu["toolUseId"], "name": tu["name"], "input": tu["input"]}
    if "toolResult" in block:
        tr = block["toolResult"]
        # The Messages API rejects empty text blocks, so an empty tool output
        # becomes a result with no content.
        return {
            "type": "tool_result",
            "tool_use_id": tr["toolUseId"],
            "content": [{"type": "text", "text": c["text"]} for c in tr.get("content", []) if c.get("text")],
            "is_error": tr.get("status") == "error",
        }
    raise ValueError(f"unsupported Converse content block: {sorted(block)}")


def _block_to_converse(block: Any) -> Dict[str, Any]:
    if block.type == "text":
        return {"text": block.text}
    if block.type == "tool_use":
        return {"toolUse": {"toolUseId": block.id, "name": block.name, "input": block.input}}
    return {}


def to_messages_request(messages: List[Dict], system: List[Dict] = None,
                        toolConfig: Dict = None, inferenceConfig: Dict = None) -> Dict[str, Any]:
    req: Dict[str, Any] = {
        "max_tokens": (inferenceConfig or {}).get("maxTokens", config.bedrock_max_tokens),
        "messages": [{"role": m["role"], "content": [_block_to_messages(b) for b in m["content"]]}
                     for m in messages],
    }
    if system:
        req["system"] = "\n".join(s["text"] for s in system if "text" in s)
    if toolConfig:
        req["tools"] = [{
            "name": t["toolSpec"]["name"],
            "description": t["toolSpec"].get("description", ""),
            "input_schema": t["toolSpec"]["inputSchema"]["json"],
        } for t in toolConfig.get("tools", [])]
    return req


def to_converse_response(message: Any) -> Dict[str, Any]:
    content = [b for b in (_block_to_converse(b) for b in message.content) if b]
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": message.stop_reason,
    }


class VertexConverseClient:
    def __init__(self, client=None):
        if client is None:
            from anthropic import AnthropicVertex
            # Without VERTEX_PROJECT_ID the SDK takes the project from the credentials.
            kwargs = {"project_id": config.vertex_project_id} if config.vertex_project_id else {}
            client = AnthropicVertex(region=config.vertex_region, max_retries=10, **kwargs)
        self.client = client

    def converse(self, modelId: str = "", messages: List[Dict] = None, system: List[Dict] = None,
                 toolConfig: Dict = None, inferenceConfig: Dict = None) -> Dict[str, Any]:
        req = to_messages_request(messages or [], system, toolConfig, inferenceConfig)
        return to_converse_response(self.client.messages.create(model=config.vertex_model, **req))
