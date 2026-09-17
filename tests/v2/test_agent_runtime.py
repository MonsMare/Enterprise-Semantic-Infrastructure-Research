from __future__ import annotations

import json

from knowledge_runtime.v2.access import KnowledgeAccessRuntime
from knowledge_runtime.v2.agent_runtime import AgentRuntime
from tests.v2.test_context import make_runtime


class ScriptedAgentModel:
    def __init__(self, *, cite: bool = True) -> None:
        self.cite = cite
        self.turn = 0
        self.search_payloads: list[str] = []
        self.messages: list[list[dict]] = []

    def complete(self, messages, tools):
        self.messages.append(list(messages))
        self.turn += 1
        if self.turn == 1:
            return {
                "content": None,
                "tool_calls": [{"id": "search-1", "function": {"name": "search", "arguments": '{"query":"reserve margin"}'}}],
            }
        if self.turn == 2:
            payload = messages[-1]["content"]
            self.search_payloads.append(payload)
            locator = json.loads(payload)["items"][0]["locator"]
            return {
                "content": None,
                "tool_calls": [{"id": "read-1", "function": {"name": "read", "arguments": json.dumps({"locator": locator})}}],
            }
        evidence_payload = next(
            json.loads(message["content"])
            for message in reversed(messages)
            if message.get("role") == "tool" and "evidence_id" in message.get("content", "")
        )
        citation = f" [{evidence_payload['evidence_id']}]" if self.cite else ""
        return {"content": "The reserve margin is twelve percent." + citation, "tool_calls": []}


class UnsupportedAnswerModel:
    def complete(self, messages, tools):
        return {"content": "The reserve margin is twelve percent.", "tool_calls": []}


def test_agent_runtime_requires_read_and_never_sends_search_preview_to_model() -> None:
    model = ScriptedAgentModel()
    runtime = AgentRuntime(model, KnowledgeAccessRuntime(make_runtime()))

    result = runtime.run("What is the reserve margin?")

    assert result.stop_reason == "completed"
    assert result.evidence
    assert result.evidence[0].evidence_id in result.answer
    assert "twelve percent" not in model.search_payloads[0]
    assert result.tool_trace[0].tool == "search"
    assert result.tool_trace[1].evidence_ids == (result.evidence[0].evidence_id,)


def test_agent_runtime_returns_safe_no_evidence_response_for_unsupported_answer() -> None:
    runtime = AgentRuntime(UnsupportedAnswerModel(), KnowledgeAccessRuntime(make_runtime()))

    result = runtime.run("What is the reserve margin?")

    assert result.stop_reason == "no_evidence"
    assert result.evidence == ()
    assert "不能" in result.answer


def test_agent_runtime_rejects_uncited_answer_after_reading_evidence() -> None:
    runtime = AgentRuntime(ScriptedAgentModel(cite=False), KnowledgeAccessRuntime(make_runtime()))

    result = runtime.run("What is the reserve margin?")

    assert result.stop_reason == "citation_validation_failed"
    assert "引用" in result.answer
