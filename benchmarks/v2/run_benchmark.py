from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class BenchmarkRow:
    case_id: str = ""
    source_hit_at_k: dict[int, float] = field(default_factory=dict)
    evidence_hit_at_k: dict[int, float] = field(default_factory=dict)
    evidence_coverage: float = 0.0
    claim_local_citation_coverage: float = 0.0
    agent_loop_count: int = 0
    ingestion_seconds: float = 0.0
    search_p50_ms: float = 0.0
    search_p95_ms: float = 0.0
    index_build_seconds: float = 0.0
    evidence_bytes: int = 0
    query_rewrite_count: int = 0
    clarification_rate: float = 0.0
    retrieval_success_after_rewrite: float = 0.0
    no_improvement_stop_rate: float = 0.0
    egress_events: int = 0


def score_case(
    *,
    source_rank: int | None,
    evidence_rank: int | None,
    cited_claims: int,
    total_claims: int,
    case_id: str = "",
) -> BenchmarkRow:
    ks = (1, 3, 5, 10)
    source = {k: float(source_rank is not None and source_rank <= k) for k in ks}
    evidence = {k: float(evidence_rank is not None and evidence_rank <= k) for k in ks}
    citation = cited_claims / total_claims if total_claims else 1.0
    return BenchmarkRow(
        case_id=case_id,
        source_hit_at_k=source,
        evidence_hit_at_k=evidence,
        evidence_coverage=float(evidence_rank is not None),
        claim_local_citation_coverage=citation,
    )


def _load_cases(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            rows.append(json.loads(line))
    return rows


def run_cases(path: Path, *, offline: bool = True) -> dict[str, Any]:
    cases = _load_cases(path)
    rows: list[BenchmarkRow] = []
    for case in cases:
        rows.append(
            score_case(
                source_rank=case.get("source_rank"),
                evidence_rank=case.get("evidence_rank"),
                cited_claims=case.get("cited_claims", 0),
                total_claims=case.get("total_claims", len(case.get("required_claims", []))),
                case_id=case.get("case_id", ""),
            )
        )
    def mean(name: str) -> float:
        values = [getattr(row, name) for row in rows]
        return statistics.mean(values) if values else 0.0
    return {
        "cases": [asdict(row) for row in rows],
        "case_count": len(rows),
        "offline": offline,
        "source_hit_at_k": {str(k): mean_dict(rows, "source_hit_at_k", k) for k in (1, 3, 5, 10)},
        "evidence_hit_at_k": {str(k): mean_dict(rows, "evidence_hit_at_k", k) for k in (1, 3, 5, 10)},
        "evidence_coverage": mean("evidence_coverage"),
        "claim_local_citation_coverage": mean("claim_local_citation_coverage"),
        "agent_loop_count": 0,
        "ingestion_seconds": 0.0,
        "search_p50_ms": 0.0,
        "search_p95_ms": 0.0,
        "index_build_seconds": 0.0,
        "evidence_bytes": 0,
        "query_rewrite_count": 0,
        "clarification_rate": 0.0,
        "retrieval_success_after_rewrite": 0.0,
        "no_improvement_stop_rate": 0.0,
        "egress_events": 0,
    }


def mean_dict(rows: list[BenchmarkRow], name: str, key: int) -> float:
    values = [getattr(row, name).get(key, 0.0) for row in rows]
    return sum(values) / len(values) if values else 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Knowledge Runtime v2 benchmark")
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = run_cases(args.cases, offline=args.offline)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

