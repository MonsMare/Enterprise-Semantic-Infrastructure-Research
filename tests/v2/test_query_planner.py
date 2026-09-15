from __future__ import annotations

from knowledge_runtime.v2.query_planner import ConversationState, QueryPlanner


def test_planner_preserves_conversation_anchor_for_follow_up() -> None:
    conversation = ConversationState.from_turns(
        [
            {"role": "user", "content": "ASOP 56 的适用范围是什么？"},
            {"role": "assistant", "content": "已找到 ASOP 56。"},
        ]
    )

    plan = QueryPlanner().plan("那它的报告要求呢？", conversation=conversation)

    assert plan.rewrites
    assert "ASOP 56" in plan.query
    assert plan.clarification is False


def test_planner_requests_clarification_for_unanchored_pronoun() -> None:
    plan = QueryPlanner().plan("那它呢？")
    assert plan.clarification is True

