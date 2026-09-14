import json

from knowledge_runtime.agent_loop import AgentLoop
from knowledge_runtime.memory_provider import MemoryProvider


class ScriptedModel:
    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append((messages, tools))
        return self.turns.pop(0)


def tool_call(name, arguments, call_id="call-1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def test_agent_loop_searches_reads_then_answers_with_evidence():
    provider = MemoryProvider({"guide.md": "# Guide\n\nThe access token expires after one hour."})
    model = ScriptedModel(
        [
            {"tool_calls": [tool_call("search", {"query": "access token"})]},
            {"tool_calls": [tool_call("read", {"locator": json.dumps(provider.list(limit=1).items[0].locator.as_dict(), ensure_ascii=False)})]},
            {"content": "The token expires after one hour. [ev-example]"},
        ]
    )
    result = AgentLoop(model).run("When does the token expire?", provider)

    assert result.iterations == 3
    assert len(result.evidence) == 1
    evidence_id = result.evidence[0].evidence_id
    assert evidence_id in result.answer
    assert "one hour" in result.answer
    read_tool_result = model.calls[2][0][-1]["content"]
    assert evidence_id in read_tool_result
    assert "one hour" in read_tool_result


def test_agent_loop_stops_at_iteration_budget():
    provider = MemoryProvider({"guide.md": "some text"})
    model = ScriptedModel([{"tool_calls": [tool_call("search", {"query": "missing"})]}] * 3)

    result = AgentLoop(model).run("question", provider, max_iterations=2)

    assert result.iterations == 2
    assert result.stopped_reason == "max_iterations"


def test_agent_loop_stops_reads_at_total_byte_budget():
    provider = MemoryProvider({"guide.md": "# Guide\n\nabcdefghijk"})
    locator = provider.list(limit=1).items[0].locator
    model = ScriptedModel(
        [
            {"tool_calls": [tool_call("read", {"locator": locator.to_json()})]},
            {"content": "Here is the answer."},
        ]
    )
    result = AgentLoop(model).run("read it", provider, max_read_bytes=5)

    assert len(result.evidence) == 1
    assert result.evidence[0].truncated is True
    assert len(result.evidence[0].content.encode("utf-8")) <= 5


def test_agent_loop_refuses_to_answer_when_no_evidence_was_read():
    provider = MemoryProvider({"guide.md": "A factual statement."})
    model = ScriptedModel([{"content": "The unsupported answer is 42."}])

    result = AgentLoop(model).run("What is the answer?", provider)

    assert result.evidence == []
    assert "没有读取到可引用的知识证据" in result.answer


def test_agent_explains_compact_and_multi_part_search_strategy_to_model():
    provider = MemoryProvider({"guide.md": "A factual statement."})
    model = ScriptedModel([{"content": "I need to search first."}])

    AgentLoop(model).run("What is the answer?", provider)

    system_instructions = model.calls[0][0][0]["content"]
    search_tool = next(
        tool["function"] for tool in model.calls[0][1] if tool["function"]["name"] == "search"
    )
    assert "2 to 5" in system_instructions
    assert "separate targeted searches" in system_instructions
    assert "2 to 5" in search_tool["description"]


def test_agent_records_the_exact_search_query_for_benchmark_auditing():
    provider = MemoryProvider({"guide.md": "Data supplied by others must be reviewed."})
    model = ScriptedModel(
        [
            {"tool_calls": [tool_call("search", {"query": "third-party data review"})]},
            {"content": "I found no need to read yet."},
        ]
    )

    result = AgentLoop(model).run("别人给的数据要怎么处理？", provider)

    search_event = next(event for event in result.events if event.kind == "tool_result")
    assert search_event.payload["query"] == "third-party data review"


def test_agent_loop_resolves_model_friendly_document_line_locator():
    provider = MemoryProvider({"guide.md": "The answer is in the first line."})
    model = ScriptedModel(
        [
            {"tool_calls": [tool_call("read", {"locator": "guide.md:00000001"})]},
            {"content": "The answer is in the first line."},
        ]
    )

    result = AgentLoop(model).run("What is the answer?", provider)

    assert len(result.evidence) == 1
    assert "first line" in result.evidence[0].content


def test_agent_loop_forces_answer_after_relevant_evidence():
    provider = MemoryProvider({"guide.md": "The access token expires after one hour."})
    locator = provider.list(limit=1).items[0].locator
    model = ScriptedModel(
        [
            {"tool_calls": [tool_call("read", {"locator": locator.to_json()})]},
            {"content": "The access token expires after one hour. [ev-placeholder]"},
        ]
    )

    result = AgentLoop(model).run("access token", provider)

    assert result.stopped_reason == "completed"
    assert len(model.calls[1][1]) == 0


def test_agent_loop_asks_for_context_for_pronoun_only_fragment():
    provider = MemoryProvider({"guide.md": "There are four options."})
    model = ScriptedModel([])

    result = AgentLoop(model).run("那四种？", provider)

    assert result.stopped_reason == "clarification_needed"
    assert result.evidence == []
    assert model.calls == []
    assert "补充" in result.answer


def test_agent_loop_records_malformed_tool_arguments_without_unbound_local_error():
    provider = MemoryProvider({"guide.md": "A factual statement."})
    model = ScriptedModel(
        [
            {
                "tool_calls": [
                    {
                        "id": "bad-args",
                        "type": "function",
                        "function": {"name": "search", "arguments": "{"},
                    }
                ]
            },
            {"content": "No answer."},
        ]
    )

    result = AgentLoop(model).run("What is the answer?", provider)

    assert any(event.kind == "tool_error" for event in result.events)
    assert result.stopped_reason == "no_evidence"
