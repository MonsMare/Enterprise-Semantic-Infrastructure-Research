from benchmarks.scale.run_scale_benchmark import (
    build_synthetic_corpus,
    run_scale_benchmark,
)


def test_synthetic_scale_corpus_is_deterministic_and_has_source_bound_gold_spans():
    assets_a, cases_a = build_synthetic_corpus(size=8, case_count=4, seed=23)
    assets_b, cases_b = build_synthetic_corpus(size=8, case_count=4, seed=23)

    assert [(asset.asset_id, asset.markdown) for asset in assets_a] == [
        (asset.asset_id, asset.markdown) for asset in assets_b
    ]
    assert cases_a == cases_b
    assert len(assets_a) == 8
    assert len(cases_a) == 8
    assert all(case.expected_phrase in next(
        asset.markdown for asset in assets_a if asset.source_name == case.expected_source
    ) for case in cases_a)


def test_scale_report_separates_discriminative_and_broad_queries_and_measures_evidence(tmp_path):
    report = run_scale_benchmark(
        sizes=(4, 8),
        case_count=4,
        top_k=3,
        repeats=1,
        warmups=0,
        seed=23,
        workdir=tmp_path,
    )

    assert report["llm_calls"] == 0
    assert [run["corpus"]["documents"] for run in report["runs"]] == [4, 8]
    precise = report["runs"][0]["metrics"]["by_query_kind"]["precise"]
    broad_at_larger_scale = report["runs"][1]["metrics"]["by_query_kind"]["broad"]
    assert precise["hit_at_k"]["1"] == 1.0
    assert precise["evidence_location_rate"] == 1.0
    assert broad_at_larger_scale["hit_at_k"]["3"] == 0.0
    assert report["runs"][1]["latency_ms"]["search"]["p95"] >= 0
