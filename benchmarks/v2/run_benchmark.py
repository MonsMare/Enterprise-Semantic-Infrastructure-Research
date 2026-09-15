from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


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
    evidence_coverage: float | None = None,
) -> BenchmarkRow:
    ks = (1, 3, 5, 10)
    source = {k: float(source_rank is not None and source_rank <= k) for k in ks}
    evidence = {k: float(evidence_rank is not None and evidence_rank <= k) for k in ks}
    citation = cited_claims / total_claims if total_claims else 1.0
    return BenchmarkRow(
        case_id=case_id,
        source_hit_at_k=source,
        evidence_hit_at_k=evidence,
        evidence_coverage=float(evidence_rank is not None) if evidence_coverage is None else evidence_coverage,
        claim_local_citation_coverage=citation,
    )


def _load_cases(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            rows.append(json.loads(line))
    return rows


def run_cases(
    path: Path,
    *,
    offline: bool = True,
    corpus: Path | None = None,
    live_agent: bool = False,
) -> dict[str, Any]:
    cases = _load_cases(path)
    measured = corpus is not None
    measured_rows: dict[str, tuple[int | None, int | None, float]] = {}
    ingestion_seconds = 0.0
    index_build_seconds = 0.0
    if corpus is not None:
        measured_rows, ingestion_seconds, index_build_seconds = _measure_retrieval(corpus, cases)
    live_metrics = _run_live_agent(corpus, cases) if live_agent else {}
    rows: list[BenchmarkRow] = []
    for case in cases:
        source_rank = case.get("source_rank")
        evidence_rank = case.get("evidence_rank")
        coverage = None
        if measured:
            source_rank, evidence_rank, coverage = measured_rows.get(case.get("case_id", ""), (None, None, 0.0))
        rows.append(
            score_case(
                source_rank=source_rank,
                evidence_rank=evidence_rank,
                cited_claims=case.get("cited_claims", 0),
                total_claims=case.get("total_claims", len(case.get("required_claims", []))),
                case_id=case.get("case_id", ""),
                evidence_coverage=coverage,
            )
        )

    def mean(name: str) -> float:
        values = [getattr(row, name) for row in rows]
        return statistics.mean(values) if values else 0.0

    return {
        "cases": [asdict(row) for row in rows],
        "case_count": len(rows),
        "measured": measured,
        "corpus": str(corpus) if corpus is not None else None,
        "offline": offline,
        "agent_live": live_agent,
        "source_hit_at_k": {str(k): mean_dict(rows, "source_hit_at_k", k) for k in (1, 3, 5, 10)},
        "evidence_hit_at_k": {str(k): mean_dict(rows, "evidence_hit_at_k", k) for k in (1, 3, 5, 10)},
        "evidence_coverage": mean("evidence_coverage"),
        "claim_local_citation_coverage": mean("claim_local_citation_coverage"),
        "agent_loop_count": live_metrics.get("agent_loop_count", 0),
        "ingestion_seconds": ingestion_seconds,
        "search_p50_ms": 0.0,
        "search_p95_ms": 0.0,
        "index_build_seconds": index_build_seconds,
        "evidence_bytes": live_metrics.get("evidence_bytes", 0),
        "query_rewrite_count": live_metrics.get("query_rewrite_count", 0),
        "clarification_rate": live_metrics.get("clarification_rate", 0.0),
        "retrieval_success_after_rewrite": live_metrics.get("retrieval_success_after_rewrite", 0.0),
        "no_improvement_stop_rate": live_metrics.get("no_improvement_stop_rate", 0.0),
        "egress_events": live_metrics.get("egress_events", 0),
    }


def _measure_retrieval(corpus: Path, cases: list[dict[str, Any]]) -> tuple[dict[str, tuple[int | None, int | None, float]], float, float]:
    """Measure source and phrase localization over a local corpus without an oracle read."""
    from knowledge_runtime.v2.artifacts import FilesystemArtifactStore
    from knowledge_runtime.v2.canonical import InMemoryCanonicalStore
    from knowledge_runtime.v2.config import RuntimeConfig
    from knowledge_runtime.v2.context import ContextRuntime
    from knowledge_runtime.v2.index import InMemoryIndexBackend
    from knowledge_runtime.v2.ingestion import IngestionService
    from knowledge_runtime.v2.providers import LocalProvider, ParserRouter

    canonical = InMemoryCanonicalStore()
    index = InMemoryIndexBackend(canonical=canonical)
    config = RuntimeConfig.test_private()
    service = IngestionService(
        router=ParserRouter(config=config, local=LocalProvider(config)),
        quality=None,
        canonical=canonical,
        artifacts=FilesystemArtifactStore(Path(tempfile.mkdtemp(prefix="kr-v2-benchmark-"))),
        index=index,
    )
    paths = [
        path
        for path in corpus.rglob("*")
        if path.is_file() and path.suffix.lower() in {".md", ".markdown", ".txt", ".rst", ".csv", ".json", ".xml"}
    ]
    started = time.perf_counter()
    for path in sorted(paths):
        service.ingest(path, provider="local")
    ingestion_seconds = time.perf_counter() - started
    runtime = ContextRuntime(canonical=canonical, index=index)
    measured: dict[str, tuple[int | None, int | None, float]] = {}
    for case in cases:
        query = case.get("retrieval_query") or case.get("question", "")
        page = runtime.search_evidence(query, limit=200)
        expected = {str(name).casefold() for name in case.get("expected_sources", [])}
        source_rank = None
        evidence_rank = None
        phrases = [str(value).casefold() for value in case.get("required_evidence_phrases", [])]
        matched_phrases: set[str] = set()
        for rank, hit in enumerate(page.items, start=1):
            if hit.display_name.casefold() in expected and source_rank is None:
                source_rank = rank
            if hit.display_name.casefold() in expected:
                preview = (hit.preview or "").casefold()
                matched_phrases.update(phrase for phrase in phrases if phrase in preview)
                if phrases and matched_phrases and evidence_rank is None:
                    evidence_rank = rank
        coverage = len(matched_phrases) / len(phrases) if phrases else float(evidence_rank is not None)
        measured[case.get("case_id", "")] = (source_rank, evidence_rank, coverage)
    return measured, ingestion_seconds, 0.0


def _run_live_agent(corpus: Path | None, cases: list[dict[str, Any]]) -> dict[str, Any]:
    if corpus is None:
        raise ValueError("--live-agent requires --corpus")
    from knowledge_runtime.v2.agent import AgentRetrievalLoop, QwenAgentClient
    from knowledge_runtime.v2.artifacts import FilesystemArtifactStore
    from knowledge_runtime.v2.canonical import InMemoryCanonicalStore
    from knowledge_runtime.v2.config import RuntimeConfig
    from knowledge_runtime.v2.context import ContextRuntime
    from knowledge_runtime.v2.index import InMemoryIndexBackend
    from knowledge_runtime.v2.ingestion import IngestionService
    from knowledge_runtime.v2.providers import LocalProvider, ParserRouter

    config = RuntimeConfig.from_env()
    config.require_remote_agent()
    canonical = InMemoryCanonicalStore()
    index = InMemoryIndexBackend(canonical=canonical)
    service = IngestionService(
        router=ParserRouter(config=config, local=LocalProvider(config)),
        quality=None,
        canonical=canonical,
        artifacts=FilesystemArtifactStore(Path(tempfile.mkdtemp(prefix="kr-v2-agent-benchmark-"))),
        index=index,
    )
    for path in sorted(
        path
        for path in corpus.rglob("*")
        if path.is_file() and path.suffix.lower() in {".md", ".markdown", ".txt", ".rst", ".csv", ".json", ".xml"}
    ):
        service.ingest(path, provider="local")
    loop = AgentRetrievalLoop(QwenAgentClient(config=config))
    runtime = ContextRuntime(canonical=canonical, index=index)
    rounds = rewrites = clarifications = successes = evidence_bytes = no_improvement = 0
    for case in cases:
        result = loop.run(str(case.get("question", "")), runtime)
        rounds += result.iterations
        rewrite_count = len(result.query_plan.rewrites) if result.query_plan else 0
        rewrites += rewrite_count
        clarifications += int(result.stopped_reason == "clarification_needed")
        successes += int(bool(result.evidence) and rewrite_count > 0)
        no_improvement += int(result.stopped_reason == "max_rounds")
        evidence_bytes += sum(len(str(item.content).encode("utf-8")) for item in result.evidence)
    count = max(1, len(cases))
    return {
        "agent_loop_count": rounds,
        "query_rewrite_count": rewrites,
        "clarification_rate": clarifications / count,
        "retrieval_success_after_rewrite": successes / count,
        "no_improvement_stop_rate": no_improvement / count,
        "evidence_bytes": evidence_bytes,
        # Every model round is an explicit egress event in this mode; request
        # bodies remain out of the benchmark artifact.
        "egress_events": rounds,
    }


def mean_dict(rows: list[BenchmarkRow], name: str, key: int) -> float:
    values = [getattr(row, name).get(key, 0.0) for row in rows]
    return sum(values) / len(values) if values else 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Knowledge Runtime v2 benchmark")
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--live-agent", action="store_true", help="run real qwen3.8-max; requires explicit remote Agent enablement")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = run_cases(args.cases, offline=args.offline, corpus=args.corpus, live_agent=args.live_agent)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
