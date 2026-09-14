import json

from knowledge_runtime.evaluation import (
    BenchmarkCase,
    BenchmarkRunner,
    RetrievalBenchmarkRunner,
    load_cases,
)
from knowledge_runtime.memory_provider import MemoryProvider


class ScriptedModel:
    def __init__(self, turns):
        self.turns = iter(turns)

    def complete(self, messages, tools):
        return next(self.turns)


def call(name, arguments, call_id="call-1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def test_benchmark_runner_checks_source_fact_and_citation():
    provider = MemoryProvider({"actuarial.md": "The reserve review occurs quarterly."})
    locator = provider.search("reserve review").items[0].locator
    evidence_id = provider.read(locator).evidence_id
    model = ScriptedModel(
        [
            {"tool_calls": [call("search", {"query": "reserve review"})]},
            {"tool_calls": [call("read", {"locator": locator.to_json()})]},
            {"content": f"The reserve review occurs quarterly. [{evidence_id}]"},
        ]
    )

    report = BenchmarkRunner(model, model_name="qwen3.8-max").run(
        [
            BenchmarkCase(
                case_id="reserve-001",
                question="How often is the reserve reviewed?",
                expected_sources=("actuarial.md",),
                required_phrases=("quarterly",),
                required_evidence_phrases=("reserve review occurs quarterly",),
            )
        ],
        provider,
    )

    assert report.passed == 1
    assert report.cases[0].passed is True
    assert report.cases[0].evidence_sources == ("actuarial.md",)
    assert report.cases[0].tool_calls == 2
    assert report.cases[0].evidence_bytes > 0
    assert report.cases[0].expected_rank == 1
    assert report.hit_at_1 == 1.0
    assert report.hit_at_3 == 1.0
    assert report.mean_reciprocal_rank == 1.0


def test_benchmark_runner_fails_when_gold_fact_is_not_in_read_evidence():
    provider = MemoryProvider({"actuarial.md": "The reserve review occurs quarterly."})
    locator = provider.list(limit=1).items[0].locator
    model = ScriptedModel(
        [
            {"tool_calls": [call("search", {"query": "reserve review"})]},
            {"tool_calls": [call("read", {"locator": locator.to_json()})]},
            {"content": "The reserve review occurs quarterly."},
        ]
    )

    report = BenchmarkRunner(model, model_name="qwen3.8-max").run(
        [
            BenchmarkCase(
                case_id="unsupported-evidence",
                question="How often is the reserve reviewed?",
                required_evidence_phrases=("every month",),
            )
        ],
        provider,
    )

    assert report.cases[0].passed is False
    assert "required evidence phrase missing from expected source: every month" in report.cases[0].failures


def test_benchmark_requires_gold_phrases_in_the_expected_source():
    provider = MemoryProvider(
        {
            "gold.md": "The standard applies to actuaries in any practice area.",
            "distractor.md": "Data supplied by others should be reviewed.",
        }
    )
    gold_locator = provider.find("gold").items[0].locator
    distractor_locator = provider.find("distractor").items[0].locator
    gold_evidence_id = provider.read(gold_locator).evidence_id
    model = ScriptedModel(
        [
            {
                "tool_calls": [
                    call("read", {"locator": gold_locator.to_json()}, "read-gold"),
                    call("read", {"locator": distractor_locator.to_json()}, "read-distractor"),
                ]
            },
            {"content": f"Data supplied by others should be reviewed. [{gold_evidence_id}]"},
        ]
    )

    report = BenchmarkRunner(model, model_name="qwen3.8-max").run(
        [
            BenchmarkCase(
                case_id="source-bound-gold",
                question="What does the expected standard require?",
                expected_sources=("gold.md",),
                required_phrases=("data supplied by others",),
                required_evidence_phrases=("data supplied by others",),
            )
        ],
        provider,
    )

    assert report.cases[0].passed is False
    assert "required answer phrase unsupported by expected source: data supplied by others" in report.cases[0].failures
    assert "required evidence phrase missing from expected source: data supplied by others" in report.cases[0].failures


def test_benchmark_rank_uses_the_best_rank_from_an_individual_search():
    provider = MemoryProvider(
        {
            "a-irrelevant.md": "An unrelated fact is included.",
            "gold.md": "The reserve review occurs quarterly.",
        }
    )
    gold_locator = provider.search("reserve review").items[0].locator
    evidence_id = provider.read(gold_locator).evidence_id
    model = ScriptedModel(
        [
            {"tool_calls": [call("search", {"query": "unrelated fact"}, "search-1")]},
            {"tool_calls": [call("search", {"query": "reserve review"}, "search-2")]},
            {"tool_calls": [call("read", {"locator": gold_locator.to_json()}, "read-1")]},
            {"content": f"The review is quarterly. [{evidence_id}]"},
        ]
    )

    report = BenchmarkRunner(model, model_name="qwen3.8-max").run(
        [
            BenchmarkCase(
                case_id="per-search-rank",
                question="How often is the reserve reviewed?",
                expected_sources=("gold.md",),
                required_phrases=("quarterly",),
                required_evidence_phrases=("reserve review occurs quarterly",),
            )
        ],
        provider,
    )

    assert report.cases[0].expected_rank == 1


def test_retrieval_benchmark_checks_target_rank_and_source_bound_evidence():
    provider = MemoryProvider(
        {
            "distractor.md": "A generic model overview.",
            "gold.md": "This scope covers all actuarial models.",
        }
    )
    report = RetrievalBenchmarkRunner().run(
        [
            BenchmarkCase(
                case_id="retrieval-only",
                question="What is covered?",
                expected_sources=("gold.md",),
                required_evidence_phrases=("all actuarial models",),
                retrieval_query="all actuarial models",
            )
        ],
        provider,
    )

    assert report.passed == 1
    assert report.cases[0].expected_rank == 1
    assert report.cases[0].evidence_sources == ("gold.md",)
    assert report.cases[0].evidence_bytes > 0


def test_retrieval_benchmark_reports_missing_gold_evidence():
    provider = MemoryProvider({"gold.md": "The scope is limited to model oversight."})
    report = RetrievalBenchmarkRunner().run(
        [
            BenchmarkCase(
                case_id="missing-gold",
                question="What is the scope?",
                expected_sources=("gold.md",),
                required_evidence_phrases=("all actuarial models",),
                retrieval_query="scope model oversight",
            )
        ],
        provider,
    )

    assert report.cases[0].passed is False
    assert "required evidence phrase missing from expected source: all actuarial models" in report.cases[0].failures


def test_load_cases_reads_jsonl(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text(
        '# comment\n{"case_id":"a","question":"Q","expected_sources":["a.md"],"answerable":false}\n',
        encoding="utf-8",
    )

    cases = load_cases(path)

    assert cases == [BenchmarkCase("a", "Q", ("a.md",), (), False, True)]
