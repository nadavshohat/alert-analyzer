"""MODEL_PROVIDER=vertex: Converse<->Messages translation and the agent loop end to end."""
import types
from datetime import datetime, timezone

import agent
import vertex
from clickhouse import CrashEvent
from config import config


def _msg(stop, *blocks):
    return types.SimpleNamespace(stop_reason=stop, content=list(blocks))


def _text(t):
    return types.SimpleNamespace(type="text", text=t)


def _tool_use(i, name, inp):
    return types.SimpleNamespace(type="tool_use", id=i, name=name, input=inp)


def test_request_translation_covers_every_converse_field():
    req = vertex.to_messages_request(
        messages=[
            {"role": "user", "content": [{"text": "go"}]},
            {"role": "assistant", "content": [{"text": "x"}, {"toolUse": {"toolUseId": "t1", "name": "get_logs", "input": {"a": 1}}}]},
            {"role": "user", "content": [
                {"toolResult": {"toolUseId": "t1", "content": [{"text": "ok"}], "status": "success"}},
                {"toolResult": {"toolUseId": "t2", "content": [{"text": "Error: x"}], "status": "error"}},
            ]},
        ],
        system=[{"text": "sys"}],
        toolConfig={"tools": agent.TOOL_DEFINITIONS},
        inferenceConfig={"maxTokens": 77},
    )
    assert req["system"] == "sys" and req["max_tokens"] == 77
    assert req["messages"][1]["content"][1] == {"type": "tool_use", "id": "t1", "name": "get_logs", "input": {"a": 1}}
    results = req["messages"][2]["content"]
    assert [r["is_error"] for r in results] == [False, True]
    assert results[0] == {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "ok"}], "is_error": False}
    assert len(req["tools"]) == len(agent.TOOL_DEFINITIONS)
    spec = agent.TOOL_DEFINITIONS[0]["toolSpec"]
    assert req["tools"][0] == {"name": spec["name"], "description": spec["description"], "input_schema": spec["inputSchema"]["json"]}


def test_empty_tool_output_has_no_empty_text_block():
    req = vertex.to_messages_request([{"role": "user", "content": [
        {"toolResult": {"toolUseId": "t1", "content": [{"text": ""}, {"text": " \n"}], "status": "success"}},
        {"toolResult": {"toolUseId": "t2", "content": [{"text": "x"}], "status": "success"}}]}])
    assert [r["content"] for r in req["messages"][0]["content"]] == [[], [{"type": "text", "text": "x"}]]


def test_request_without_tools_or_system_has_no_such_keys():
    req = vertex.to_messages_request([{"role": "user", "content": [{"text": "hi"}]}])
    assert "tools" not in req and "system" not in req
    assert req["max_tokens"] == config.bedrock_max_tokens


def test_response_translation_keeps_stop_reason_and_drops_unknown_blocks():
    out = vertex.to_converse_response(_msg("max_tokens", _text("a"), types.SimpleNamespace(type="thinking"),
                                           _tool_use("t1", "get_logs", {})))
    assert out["stopReason"] == "max_tokens"
    assert out["output"]["message"]["content"] == [
        {"text": "a"}, {"toolUse": {"toolUseId": "t1", "name": "get_logs", "input": {}}}]


class FakeAnthropic:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.requests.append(kw)
        return self.replies.pop(0)


def test_agent_loop_runs_on_vertex_including_summariser(monkeypatch):
    monkeypatch.setattr(config, "vertex_model", "claude-sonnet-5-5")
    fake = FakeAnthropic([
        _msg("tool_use", _tool_use("t1", "get_logs", {"workload": "api"})),
        _msg("end_turn", _text("done")),
        _msg("end_turn", _text("SUMMARY: s\nROOT_CAUSE: r\nCONFIDENCE: high\nSTATUS: active")),
    ])
    tools = types.SimpleNamespace(investigation_namespace=None, execute=lambda name, inp: "log line")
    a = agent.AgentAnalyzer(tools=tools)
    a.bedrock = vertex.VertexConverseClient(client=fake)
    ev = CrashEvent(datetime.now(timezone.utc), "shop", "api", "api-0", "BackOff", "m")
    result = a.analyze(ev)
    assert (result.summary, result.root_cause, result.tool_calls_made) == ("s", "r", 1)
    assert [r["model"] for r in fake.requests] == ["claude-sonnet-5-5"] * 3
    second = fake.requests[1]["messages"][-1]["content"][0]
    assert second == {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "log line"}], "is_error": False}
    assert "tools" not in fake.requests[2]  # verdict is written by a no-tools call


def test_provider_selects_client(monkeypatch):
    monkeypatch.setattr(config, "model_provider", "bedrock")
    assert type(agent.AgentAnalyzer(tools=object()).bedrock).__name__ == "BedrockRuntime"
    monkeypatch.setattr(config, "model_provider", "vertex")
    monkeypatch.setattr(vertex, "VertexConverseClient", lambda: "vertex-client")
    assert agent.AgentAnalyzer(tools=object()).bedrock == "vertex-client"
