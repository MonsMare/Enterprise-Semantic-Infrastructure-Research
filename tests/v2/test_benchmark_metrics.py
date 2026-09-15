from __future__ import annotations

from benchmarks.v2.run_benchmark import score_case


def test_benchmark_separates_source_hit_from_evidence_coverage() -> None:
    row = score_case(source_rank=1, evidence_rank=8, cited_claims=1, total_claims=2)
    assert row.source_hit_at_k[1] == 1.0
    assert row.evidence_hit_at_k[3] == 0.0
    assert row.claim_local_citation_coverage == 0.5

