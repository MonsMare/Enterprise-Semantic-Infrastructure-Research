from __future__ import annotations

import json

import pytest

from knowledge_runtime.v2.agent import (
    AgentRetrievalLoop,
    CitationValidationError,
    CitationValidator,
    QwenAgentClient,
)
from knowledge_runtime.v2.config import RuntimeConfig
from knowledge_runtime.v2.context import ContextRuntime
from knowledge_runtime.v2.contracts import Evidence, EvidenceRef
from knowledge_runtime.v2.query_planner import ConversationState
from tests.v2.test_context import make_runtime


class ScriptedModel:
    def __init__(self) -> None:
        self.turns = [
            {
                "tool_calls": [
                    {"id": "1", "function": {"name": "search_evidence", "arguments": '{"query":"reserve margin"}'}},
                ],
                "content": None,
            },
            {
                "tool_calls": [
                    {"id": "2", "function": {"name": "get_evidence", "arguments": ""}},
                ],
                "content": None,
            },
            {"tool_calls": [], "content": "The reserve margin is twelve percent. [ev-placeholder]"},
        ]
        self.messages: list[list[dict]] = []

    def complete(self, messages, tools):
        self.messages.append(messages)
        turn = self.turns.pop(0)
        if turn["tool_calls"] and turn["tool_calls"][0]["function"]["name"] == "get_evidence":
            # Fill the ref from the latest search result in the tool message.
            refs = json.loads(messages[-1]["content"])["items"]
            ref = refs[0]["ref"]
            turn["tool_calls"][0]["function"]["arguments"] = json.dumps({"ref": ref})
        if not turn["tool_calls"]:
            evidence_ids = []
            for message in messages:
                if message.get("role") == "tool" and "evidence_id" in message.get("content", ""):
                    payload = json.loads(message["content"])
                    evidence_ids.append(payload["evidence_id"])
            turn["content"] = f"The reserve margin is twelve percent. [{evidence_ids[0]}]"
        return turn

    def answer_messages_contain_only_evidence(self):
        final_messages = self.messages[-1]
        return all(
            message.get("role") != "tool"
            or "content" not in message
            or "twelve percent" not in message["content"]
            or "evidence_id" in message["content"]
            for message in final_messages
        )


def test_agent_sends_read_evidence_to_answer_turn() -> None:
    model = ScriptedModel()
    result = AgentRetrievalLoop(model).run("What is the reserve margin?", make_runtime())
    assert result.evidence
    assert all(item.evidence_id in result.answer for item in result.evidence)
    assert model.answer_messages_contain_only_evidence()


def test_private_mode_agent_requires_explicit_remote_flag() -> None:
    with pytest.raises(Exception, match="remote Agent"):
        QwenAgentClient(config=RuntimeConfig.test_private())


def test_agent_preserves_conversation_anchor() -> None:
    model = ScriptedModel()
    conversation = ConversationState.from_turns(
        [{"role": "user", "content": "ASOP 56 的适用范围是什么？"}, {"role": "assistant", "content": "已找到 ASOP 56。"}]
    )
    result = AgentRetrievalLoop(model).run("那它的报告要求呢？", make_runtime(), conversation=conversation)
    assert result.query_plan.rewrites


def test_citation_validator_rejects_uncited_claims() -> None:
    ref = EvidenceRef("doc-1", "rev-1", "el-1")
    evidence = Evidence.from_content(
        ref=ref,
        content="范围是全国市场。",
        media_type="text/plain",
        representation="structured",
        source_label="policy.md",
        evidence_id="ev-1",
    )
    with pytest.raises(CitationValidationError):
        CitationValidator().validate("范围是全国市场。[ev-1] 还要求季度复核。", (evidence,))

