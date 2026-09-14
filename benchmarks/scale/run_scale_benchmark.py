from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from knowledge_runtime.asset_provider import AssetKnowledgeProvider
from knowledge_runtime.assets import KnowledgeAsset, SQLiteKnowledgeAssetStore
from knowledge_runtime.models import ReadOptions, SearchOptions


DEFAULT_SIZES = (20, 100, 1_000, 5_000)
DEFAULT_CASE_COUNT = 20
DEFAULT_TOP_K = 20
HIT_K_VALUES = (1, 3, 5, 10, 20)
COMMON_TEXT = (
    "Actuarial capital claim reserve mortality pricing model exposure. "
    "This synthetic document records assumptions and review controls."
)


@dataclass(frozen=True)
class ScaleBenchmarkCase:
    case_id: str
    query_kind: str
    query: str
    expected_source: str
    expected_phrase: str


def build_synthetic_corpus(
    *, size: int, case_count: int = DEFAULT_CASE_COUNT, seed: int = 20260914
) -> tuple[list[KnowledgeAsset], list[ScaleBenchmarkCase]]:
    """Create a stable gold set plus neutral distractors for SQLite scale tests."""
    if case_count <= 0:
        raise ValueError("case_count must be positive")
    if size < case_count:
        raise ValueError("size must be at least case_count so every case has a gold asset")

    rng = random.Random(seed)
    assets: list[KnowledgeAsset] = []
    cases: list[ScaleBenchmarkCase] = []
    for index in range(case_count):
        marker = f"axcase{index:04d}"
        factor = 0.70 + rng.randrange(0, 61) / 100
        source_name = f"z-target-{index:04d}.md"
        evidence_phrase = f"Case {marker}: indicated factor {factor:.2f}."
        markdown = (
            f"# Synthetic actuarial record {index:04d}\n\n"
            f"{COMMON_TEXT}\n\n"
            "## Valuation result\n"
            f"{evidence_phrase}\n"
        )
        assets.append(_asset(f"target-{index:04d}", source_name, markdown))
        cases.extend(
            (
                ScaleBenchmarkCase(
                    case_id=f"precise-{index:04d}",
                    query_kind="precise",
                    query=f"actuarial capital {marker}",
                    expected_source=source_name,
                    expected_phrase=evidence_phrase,
                ),
                ScaleBenchmarkCase(
                    case_id=f"broad-{index:04d}",
                    query_kind="broad",
                    query="actuarial capital claim reserve",
                    expected_source=source_name,
                    expected_phrase=evidence_phrase,
                ),
            )
        )

    for index in range(size - case_count):
        # Fixed-length suffixes avoid accidental collisions with the gold marker
        # and keep distractor documents reproducible across corpus sizes.
        suffix = hashlib.sha256(f"{seed}:{index}".encode("utf-8")).hexdigest()[:12]
        source_name = f"a-distractor-{index:06d}.md"
        markdown = (
            f"# Synthetic actuarial record {index:06d}\n\n"
            f"{COMMON_TEXT}\n\n"
            f"Note {suffix}: unrelated factor 0.70.\n"
        )
        assets.append(_asset(f"distractor-{index:06d}", source_name, markdown))

    assets.sort(key=lambda asset: asset.source_name.casefold())
    return assets, cases


def run_scale_benchmark(
    *,
    sizes: Iterable[int] = DEFAULT_SIZES,
    case_count: int = DEFAULT_CASE_COUNT,
    top_k: int = DEFAULT_TOP_K,
    repeats: int = 3,
    warmups: int = 1,
    seed: int = 20260914,
    workdir: str | Path | None = None,
) -> dict:
    """Measure SQLite FTS search, ranking, and Evidence localization offline."""
    selected_sizes = tuple(int(size) for size in sizes)
    if not selected_sizes or any(size <= 0 for size in selected_sizes):
        raise ValueError("sizes must contain one or more positive integers")
    if len(set(selected_sizes)) != len(selected_sizes):
        raise ValueError("sizes must not contain duplicates")
    if case_count <= 0 or any(size < case_count for size in selected_sizes):
        raise ValueError("each size must be at least the positive case_count")
    if not 1 <= top_k <= 200:
        raise ValueError("top_k must be between 1 and 200")
    if repeats <= 0:
        raise ValueError("repeats must be positive")
    if warmups < 0:
        raise ValueError("warmups cannot be negative")

    temp_parent: Path | None = None
    if workdir is not None:
        temp_parent = Path(workdir)
        temp_parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="kr-scale-", dir=temp_parent) as temporary_root:
        root = Path(temporary_root)
        runs = [
            _run_size(
                root / f"corpus-{size}.db",
                size=size,
                case_count=case_count,
                top_k=top_k,
                repeats=repeats,
                warmups=warmups,
                seed=seed,
            )
            for size in selected_sizes
        ]

    return {
        "benchmark": "knowledge-runtime-sqlite-fts-scale-v1",
        "pipeline": "AssetKnowledgeProvider.search -> SQLite FTS -> Locator -> Read -> Evidence",
        "llm_calls": 0,
        "seed": seed,
        "config": {
            "sizes": list(selected_sizes),
            "case_count": case_count,
            "queries_per_size": case_count * 2,
            "top_k": top_k,
            "repeats": repeats,
            "warmups": warmups,
            "hit_k_values": [k for k in HIT_K_VALUES if k <= top_k],
        },
        "runs": runs,
    }


def _run_size(
    database_path: Path,
    *,
    size: int,
    case_count: int,
    top_k: int,
    repeats: int,
    warmups: int,
    seed: int,
) -> dict:
    assets, cases = build_synthetic_corpus(size=size, case_count=case_count, seed=seed)
    corpus_bytes = sum(len(asset.markdown.encode("utf-8")) for asset in assets)
    store = SQLiteKnowledgeAssetStore(database_path)
    try:
        build_started = time.perf_counter()
        for asset in assets:
            store.put(asset)
        build_ms = (time.perf_counter() - build_started) * 1000

        provider = AssetKnowledgeProvider(store)
        search_options = SearchOptions(limit=top_k)
        for _ in range(warmups):
            for case in cases:
                provider.search(case.query, options=search_options)

        observations: list[dict] = []
        for _ in range(repeats):
            for case in cases:
                end_to_end_started = time.perf_counter()
                search_started = time.perf_counter()
                page = provider.search(case.query, options=search_options)
                search_ms = (time.perf_counter() - search_started) * 1000
                target = next(
                    (
                        (rank, hit)
                        for rank, hit in enumerate(page.items, 1)
                        if hit.display_name.casefold() == case.expected_source.casefold()
                    ),
                    None,
                )
                evidence_found = False
                evidence_bytes = 0
                read_ms = 0.0
                if target is not None:
                    read_started = time.perf_counter()
                    evidence = provider.read(target[1].locator, ReadOptions(max_bytes=20_000))
                    read_ms = (time.perf_counter() - read_started) * 1000
                    evidence_content = " ".join(str(evidence.content).casefold().split())
                    expected_phrase = " ".join(case.expected_phrase.casefold().split())
                    evidence_found = expected_phrase in evidence_content
                    evidence_bytes = len(str(evidence.content).encode("utf-8"))
                observations.append(
                    {
                        "case_id": case.case_id,
                        "query_kind": case.query_kind,
                        "rank": target[0] if target else None,
                        "evidence_found": evidence_found,
                        "evidence_bytes": evidence_bytes,
                        "search_ms": search_ms,
                        "read_ms": read_ms,
                        "end_to_end_ms": (time.perf_counter() - end_to_end_started) * 1000,
                    }
                )
    finally:
        store.close()

    return {
        "corpus": {
            "documents": size,
            "target_documents": case_count,
            "distractor_documents": size - case_count,
            "markdown_bytes": corpus_bytes,
            "database_bytes": database_path.stat().st_size if database_path.exists() else 0,
        },
        "build_ms": round(build_ms, 3),
        "metrics": {
            "overall": _quality_metrics(observations, top_k),
            "by_query_kind": {
                kind: _quality_metrics(
                    [item for item in observations if item["query_kind"] == kind], top_k
                )
                for kind in ("precise", "broad")
            },
        },
        "latency_ms": {
            "search": _latency(observations, "search_ms"),
            "read_when_target_found": _latency(
                [item for item in observations if item["rank"] is not None], "read_ms"
            ),
            "end_to_end": _latency(observations, "end_to_end_ms"),
        },
    }


def _quality_metrics(observations: list[dict], top_k: int) -> dict:
    hit_k_values = [k for k in HIT_K_VALUES if k <= top_k]
    if top_k not in hit_k_values:
        hit_k_values.append(top_k)
    target_found = [item for item in observations if item["rank"] is not None]
    evidence_found = [item for item in observations if item["evidence_found"]]
    return {
        "cases": len(observations),
        "target_retrieval_rate_at_top_k": _ratio(len(target_found), len(observations)),
        "hit_at_k": {
            str(k): _ratio(
                sum(item["rank"] is not None and item["rank"] <= k for item in observations),
                len(observations),
            )
            for k in hit_k_values
        },
        "mrr": _mean(
            [1 / item["rank"] if item["rank"] is not None else 0 for item in observations]
        ),
        "evidence_location_rate": _ratio(len(evidence_found), len(observations)),
        "evidence_location_rate_given_target_found": _ratio(
            len(evidence_found), len(target_found)
        ),
        "mean_evidence_bytes_when_target_found": _mean(
            [item["evidence_bytes"] for item in target_found]
        ),
    }


def _latency(observations: list[dict], key: str) -> dict:
    values = [float(item[key]) for item in observations]
    return {"samples": len(values), "p50": _percentile(values, 0.50), "p95": _percentile(values, 0.95)}


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def _asset(asset_id: str, source_name: str, markdown: str) -> KnowledgeAsset:
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    return KnowledgeAsset(
        asset_id=asset_id,
        source_path=f"synthetic://scale/{asset_id}",
        source_name=source_name,
        source_hash=digest,
        parser_name="synthetic-scale-benchmark",
        parser_version="1",
        markdown=markdown,
        content_list=[],
        metadata={"synthetic": True, "benchmark": "scale-v1"},
    )


def _parse_sizes(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("sizes must be comma-separated positive integers") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an offline SQLite FTS benchmark against a deterministic synthetic corpus."
    )
    parser.add_argument("--sizes", type=_parse_sizes, default=DEFAULT_SIZES)
    parser.add_argument("--case-count", type=int, default=DEFAULT_CASE_COUNT)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20260914)
    parser.add_argument("--workdir", type=Path, help="temporary DB parent; DB files are removed after the run")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".kr-data/scale-benchmark-report.json"),
        help="JSON report path",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_scale_benchmark(
            sizes=args.sizes,
            case_count=args.case_count,
            top_k=args.top_k,
            repeats=args.repeats,
            warmups=args.warmups,
            seed=args.seed,
            workdir=args.workdir,
        )
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
