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


def test_agent_benchmark_exposes_vague_query_rewrite_and_first_search_quality():
    provider = MemoryProvider(
        {
            "a-overview.md": "General actuarial data guidance.",
            "gold.md": "ASOP 23 says data supplied by others should be reviewed.",
        }
    )
    gold_locator = provider.search("data supplied by others").items[0].locator
    evidence_id = provider.read(gold_locator).evidence_id
    model = ScriptedModel(
        [
            {"tool_calls": [call("search", {"query": "data"}, "weak-query")]},
            {"tool_calls": [call("search", {"query": "data supplied by others"}, "rewritten-query")]},
            {"tool_calls": [call("read", {"locator": gold_locator.to_json()}, "read-gold")]},
            {"content": f"ASOP 23 says data supplied by others should be reviewed. [{evidence_id}]"},
        ]
    )

    report = BenchmarkRunner(model, model_name="qwen3.8-max").run(
        [
            BenchmarkCase(
                case_id="vague-query-rewrite",
                question="那个别人给的数据怎么处理？",
                expected_sources=("gold.md",),
                required_phrases=("data supplied by others", "reviewed"),
                required_evidence_phrases=("data supplied by others", "reviewed"),
                query_style="vague",
                query_anchor_groups=(("data supplied by others", "third-party data"),),
            )
        ],
        provider,
    )

    case = report.cases[0]
    assert case.passed is True
    assert case.search_queries == ("data", "data supplied by others")
    assert case.first_expected_rank == 2
    assert case.expected_rank == 1
    assert case.first_query_anchor_coverage == 0.0
    assert case.best_query_anchor_coverage == 1.0
    assert report.as_dict()["agent_query_metrics"]["by_query_style"]["vague"]["first_search_hit_at_1"] == 0.0


def test_retrieval_benchmark_can_fail_a_vague_query_that_ranks_gold_too_low():
    provider = MemoryProvider(
        {
            "a-overview.md": "General actuarial data guidance.",
            "gold.md": "ASOP 23 says data supplied by others should be reviewed.",
        }
    )

    report = RetrievalBenchmarkRunner().run(
        [
            BenchmarkCase(
                case_id="vague-direct-kr-query",
                question="那个别人给的数据怎么处理？",
                retrieval_query="data",
                expected_sources=("gold.md",),
                required_evidence_phrases=("data supplied by others",),
                query_style="vague",
                retrieval_max_expected_rank=1,
            )
        ],
        provider,
    )

    case = report.cases[0]
    assert case.expected_rank == 2
    assert case.passed is False
    assert "expected source rank 2 exceeds maximum allowed rank 1" in case.failures
    assert report.as_dict()["query_style_metrics"]["vague"]["hit_at_1"] == 0.0


def test_weak_query_probes_have_rank_cutoffs_and_human_auditable_gold():
    cases = load_cases("benchmarks/actuarial/weak_query_probes.jsonl")

    assert {case.query_style for case in cases} == {"vague", "short"}
    assert all(case.expected_sources for case in cases)
    assert all(case.required_evidence_phrases for case in cases)
    assert all(case.retrieval_query for case in cases)
    assert all(case.retrieval_max_expected_rank for case in cases)


def test_fuzzy_agent_cases_pair_chinese_answer_gold_with_source_language_evidence():
    cases = load_cases("benchmarks/actuarial/questions-fuzzy.jsonl")
    answerable_cases = [case for case in cases if case.answerable]

    assert len(answerable_cases) == 4
    assert all(case.required_claims for case in answerable_cases)
    assert all(not case.required_phrases for case in answerable_cases)


def test_benchmark_supports_bilingual_claim_gold_with_source_bound_evidence():
    provider = MemoryProvider(
        {"asop23.md": "ASOP 23 says data supplied by others should be reviewed."}
    )
    locator = provider.search("data supplied by others").items[0].locator
    evidence_id = provider.read(locator).evidence_id
    model = ScriptedModel(
        [
            {"tool_calls": [call("search", {"query": "data supplied by others"})]},
            {"tool_calls": [call("read", {"locator": locator.to_json()})]},
            {"content": f"必须审查他人提供的数据。 [{evidence_id}]"},
        ]
    )

    case = BenchmarkCase.from_dict(
        {
            "case_id": "bilingual-claim",
            "question": "别人给的数据要怎么处理？",
            "expected_sources": ["asop23.md"],
            "required_claims": [
                {
                    "answer_phrases": [
                        "review data supplied by others",
                        "审查他人提供的数据",
                    ],
                    "evidence_phrases": ["data supplied by others should be reviewed"],
                }
            ],
        }
    )
    report = BenchmarkRunner(model, model_name="qwen3.8-max").run([case], provider)

    assert case.required_claims[0].answer_phrases == (
        "review data supplied by others",
        "审查他人提供的数据",
    )
    assert report.cases[0].passed is True


def test_benchmark_rejects_bilingual_claim_missing_from_agent_answer():
    provider = MemoryProvider(
        {"asop23.md": "ASOP 23 says data supplied by others should be reviewed."}
    )
    locator = provider.search("data supplied by others").items[0].locator
    evidence_id = provider.read(locator).evidence_id
    model = ScriptedModel(
        [
            {"tool_calls": [call("search", {"query": "data supplied by others"})]},
            {"tool_calls": [call("read", {"locator": locator.to_json()})]},
            {"content": f"ASOP 23 is a standard for actuarial practice. [{evidence_id}]"},
        ]
    )
    case = BenchmarkCase.from_dict(
        {
            "case_id": "missing-bilingual-claim",
            "question": "别人给的数据要怎么处理？",
            "expected_sources": ["asop23.md"],
            "required_claims": [
                {
                    "answer_phrases": ["review data supplied by others", "审查他人提供的数据"],
                    "evidence_phrases": ["data supplied by others should be reviewed"],
                }
            ],
        }
    )

    report = BenchmarkRunner(model, model_name="qwen3.8-max").run([case], provider)

    assert report.cases[0].passed is False
    assert "required answer claim missing: review data supplied by others | 审查他人提供的数据" in report.cases[0].failures


def test_benchmark_rejects_bilingual_claim_not_supported_by_the_expected_source():
    provider = MemoryProvider(
        {
            "gold.md": "ASOP 23 covers data supplied by others.",
            "distractor.md": "Data supplied by others should be reviewed every year.",
        }
    )
    gold_locator = provider.find("gold").items[0].locator
    distractor_locator = provider.find("distractor").items[0].locator
    distractor_evidence_id = provider.read(distractor_locator).evidence_id
    model = ScriptedModel(
        [
            {
                "tool_calls": [
                    call("read", {"locator": gold_locator.to_json()}, "read-gold"),
                    call("read", {"locator": distractor_locator.to_json()}, "read-distractor"),
                ]
            },
            {"content": f"必须每年审查。 [{distractor_evidence_id}]"},
        ]
    )
    case = BenchmarkCase.from_dict(
        {
            "case_id": "bilingual-claim-wrong-source",
            "question": "应该多久审查一次？",
            "expected_sources": ["gold.md"],
            "required_claims": [
                {
                    "answer_phrases": ["review every year", "必须每年审查"],
                    "evidence_phrases": ["reviewed every month"],
                }
            ],
        }
    )

    report = BenchmarkRunner(model, model_name="qwen3.8-max").run([case], provider)

    assert report.cases[0].passed is False
    assert "required answer claim unsupported by expected source: review every year | 必须每年审查" in report.cases[0].failures


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
