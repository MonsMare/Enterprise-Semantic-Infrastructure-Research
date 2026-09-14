from __future__ import annotations

import json
import math
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .agent_loop import AgentLoop, AgentResult
from .models import ReadOptions, SearchOptions
from .provider import KnowledgeProvider


@dataclass(frozen=True)
class BenchmarkCase:
    """A small, auditable case definition for KR retrieval and grounding tests."""

    case_id: str
    question: str
    expected_sources: tuple[str, ...] = ()
    required_phrases: tuple[str, ...] = ()
    answerable: bool = True
    must_cite: bool = True
    required_evidence_phrases: tuple[str, ...] = ()
    retrieval_query: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BenchmarkCase":
        return cls(
            case_id=str(value["case_id"]),
            question=str(value["question"]),
            expected_sources=tuple(str(item) for item in value.get("expected_sources", [])),
            required_phrases=tuple(str(item) for item in value.get("required_phrases", [])),
            required_evidence_phrases=tuple(
                str(item) for item in value.get("required_evidence_phrases", [])
            ),
            answerable=bool(value.get("answerable", True)),
            must_cite=bool(value.get("must_cite", True)),
            retrieval_query=(str(value["retrieval_query"]) if value.get("retrieval_query") else None),
        )


@dataclass(frozen=True)
class BenchmarkCaseResult:
    case_id: str
    passed: bool
    failures: tuple[str, ...]
    answer: str
    evidence_ids: tuple[str, ...]
    evidence_sources: tuple[str, ...]
    iterations: int
    stopped_reason: str
    elapsed_ms: float
    tool_calls: int
    search_ms: float
    read_ms: float
    evidence_bytes: int
    ranking_applicable: bool
    expected_rank: int | None
    retrieved_sources: tuple[str, ...]


@dataclass(frozen=True)
class BenchmarkReport:
    model: str
    cases: tuple[BenchmarkCaseResult, ...]

    @property
    def passed(self) -> int:
        return sum(1 for case in self.cases if case.passed)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def ranking_cases(self) -> list[BenchmarkCaseResult]:
        return [case for case in self.cases if case.ranking_applicable]

    @property
    def hit_at_1(self) -> float | None:
        cases = self.ranking_cases
        if not cases:
            return None
        return sum(case.expected_rank == 1 for case in cases) / len(cases)

    @property
    def hit_at_3(self) -> float | None:
        cases = self.ranking_cases
        if not cases:
            return None
        return sum(case.expected_rank is not None and case.expected_rank <= 3 for case in cases) / len(cases)

    @property
    def mean_reciprocal_rank(self) -> float | None:
        cases = self.ranking_cases
        if not cases:
            return None
        reciprocal_ranks = [1 / case.expected_rank if case.expected_rank else 0 for case in cases]
        return sum(reciprocal_ranks) / len(cases)

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = max(0, math.ceil(percentile * len(ordered)) - 1)
        return round(ordered[index], 3)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "passed": self.passed,
            "total": self.total,
            "retrieval_metrics": {
                "cases": len(self.ranking_cases),
                "hit_at_1": self.hit_at_1,
                "hit_at_3": self.hit_at_3,
                "mrr": self.mean_reciprocal_rank,
            },
            "runtime_metrics": {
                "e2e_p50_ms": self._percentile([case.elapsed_ms for case in self.cases], 0.50),
                "e2e_p95_ms": self._percentile([case.elapsed_ms for case in self.cases], 0.95),
                "search_ms_total": round(sum(case.search_ms for case in self.cases), 3),
                "read_ms_total": round(sum(case.read_ms for case in self.cases), 3),
                "evidence_bytes_total": sum(case.evidence_bytes for case in self.cases),
            },
            "cases": [asdict(case) for case in self.cases],
        }


def load_cases(path: str | Path) -> list[BenchmarkCase]:
    cases: list[BenchmarkCase] = []
    for line_number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid benchmark JSON on line {line_number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"benchmark line {line_number} must be an object")
        cases.append(BenchmarkCase.from_dict(value))
    return cases


class BenchmarkRunner:
    def __init__(self, model: Any, *, model_name: str = "unknown") -> None:
        self.model = model
        self.model_name = model_name

    def run(
        self,
        cases: Iterable[BenchmarkCase],
        provider: KnowledgeProvider,
        *,
        max_iterations: int = 8,
        max_read_bytes: int = 20_000,
    ) -> BenchmarkReport:
        results = [
            self._run_case(case, provider, max_iterations=max_iterations, max_read_bytes=max_read_bytes)
            for case in cases
        ]
        return BenchmarkReport(self.model_name, tuple(results))

    def _run_case(
        self,
        case: BenchmarkCase,
        provider: KnowledgeProvider,
        *,
        max_iterations: int,
        max_read_bytes: int,
    ) -> BenchmarkCaseResult:
        started = time.perf_counter()
        result: AgentResult = AgentLoop(self.model).run(
            case.question,
            provider,
            max_iterations=max_iterations,
            max_read_bytes=max_read_bytes,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        evidence_sources = tuple(
            source
            for evidence in result.evidence
            if (source := evidence.source_label or evidence.locator.resource_id)
        )
        expected_source_names = {source.casefold() for source in case.expected_sources}
        expected_evidence = [
            evidence
            for evidence in result.evidence
            if not expected_source_names
            or any(
                expected in (evidence.source_label or evidence.locator.resource_id).casefold()
                for expected in expected_source_names
            )
        ]
        failures: list[str] = []
        for expected in case.expected_sources:
            if not any(expected.casefold() in source.casefold() for source in evidence_sources):
                failures.append(f"expected source was not read: {expected}")
        if case.answerable and not result.evidence:
            failures.append("answerable case produced no evidence")
        if not case.answerable and result.evidence:
            failures.append("unanswerable case produced evidence")
        expected_evidence_text = "\n".join(str(evidence.content) for evidence in expected_evidence).casefold()
        expected_evidence_text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", expected_evidence_text)
        expected_evidence_text = " ".join(expected_evidence_text.split())
        for phrase in case.required_phrases:
            if phrase.casefold() not in result.answer.casefold():
                failures.append(f"required phrase missing: {phrase}")
            if phrase.casefold() not in expected_evidence_text:
                failures.append(f"required answer phrase unsupported by expected source: {phrase}")
            answer_sentence = next(
                (
                    sentence
                    for sentence in re.split(
                        r"(?<=[.!?。！？])\s+(?!\[ev-)|\n+", result.answer
                    )
                    if phrase.casefold() in sentence.casefold()
                ),
                "",
            )
            if answer_sentence and not re.search(r"\[ev-[^\]]+\]", answer_sentence):
                failures.append(f"required answer phrase lacks a local Evidence citation: {phrase}")
        for phrase in case.required_evidence_phrases:
            normalized_phrase = " ".join(phrase.casefold().split())
            if normalized_phrase not in expected_evidence_text:
                failures.append(f"required evidence phrase missing from expected source: {phrase}")
        if case.must_cite and result.evidence and not any(
            evidence.evidence_id in result.answer for evidence in result.evidence
        ):
            failures.append("answer does not cite a read Evidence ID")
        tool_results = [event.payload for event in result.events if event.kind == "tool_result"]
        tool_events = [
            event.payload
            for event in result.events
            if event.kind in {"tool_result", "tool_error"}
        ]
        retrieved_sources: list[str] = []
        expected_ranks: list[int] = []
        for event in tool_results:
            if event.get("tool") != "search":
                continue
            page = event.get("result") or {}
            page_sources: list[str] = []
            for item in page.get("items", []):
                name = item.get("display_name")
                if name and name not in retrieved_sources:
                    retrieved_sources.append(name)
                if name:
                    page_sources.append(name)
            page_rank = next(
                (
                    index
                    for index, source in enumerate(page_sources, 1)
                    if any(expected.casefold() == source.casefold() for expected in case.expected_sources)
                ),
                None,
            )
            if page_rank is not None:
                expected_ranks.append(page_rank)
        expected_rank = min(expected_ranks) if expected_ranks else None
        search_ms = sum(
            float(event.get("duration_ms", 0)) for event in tool_events if event.get("tool") == "search"
        )
        read_ms = sum(
            float(event.get("duration_ms", 0)) for event in tool_events if event.get("tool") == "read"
        )
        evidence_bytes = sum(len(str(evidence.content).encode("utf-8")) for evidence in result.evidence)
        return BenchmarkCaseResult(
            case_id=case.case_id,
            passed=not failures,
            failures=tuple(failures),
            answer=result.answer,
            evidence_ids=tuple(evidence.evidence_id for evidence in result.evidence),
            evidence_sources=evidence_sources,
            iterations=result.iterations,
            stopped_reason=result.stopped_reason,
            elapsed_ms=round(elapsed_ms, 3),
            tool_calls=len(tool_events),
            search_ms=round(search_ms, 3),
            read_ms=round(read_ms, 3),
            evidence_bytes=evidence_bytes,
            ranking_applicable=bool(case.expected_sources),
            expected_rank=expected_rank,
            retrieved_sources=tuple(retrieved_sources),
        )


@dataclass(frozen=True)
class RetrievalCaseResult:
    case_id: str
    passed: bool
    failures: tuple[str, ...]
    retrieval_query: str
    expected_rank: int | None
    retrieved_sources: tuple[str, ...]
    evidence_sources: tuple[str, ...]
    evidence_bytes: int
    elapsed_ms: float
    search_ms: float
    read_ms: float
    ranking_applicable: bool


@dataclass(frozen=True)
class RetrievalBenchmarkReport:
    cases: tuple[RetrievalCaseResult, ...]

    @property
    def passed(self) -> int:
        return sum(case.passed for case in self.cases)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def ranking_cases(self) -> list[RetrievalCaseResult]:
        return [case for case in self.cases if case.ranking_applicable]

    @property
    def hit_at_1(self) -> float | None:
        cases = self.ranking_cases
        return sum(case.expected_rank == 1 for case in cases) / len(cases) if cases else None

    @property
    def hit_at_3(self) -> float | None:
        cases = self.ranking_cases
        if not cases:
            return None
        return sum(case.expected_rank is not None and case.expected_rank <= 3 for case in cases) / len(cases)

    @property
    def mean_reciprocal_rank(self) -> float | None:
        cases = self.ranking_cases
        if not cases:
            return None
        return sum(1 / case.expected_rank if case.expected_rank else 0 for case in cases) / len(cases)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pipeline": "retrieval-only",
            "passed": self.passed,
            "total": self.total,
            "retrieval_metrics": {
                "cases": len(self.ranking_cases),
                "hit_at_1": self.hit_at_1,
                "hit_at_3": self.hit_at_3,
                "mrr": self.mean_reciprocal_rank,
            },
            "runtime_metrics": {
                "e2e_p50_ms": BenchmarkReport._percentile(
                    [case.elapsed_ms for case in self.cases], 0.50
                ),
                "e2e_p95_ms": BenchmarkReport._percentile(
                    [case.elapsed_ms for case in self.cases], 0.95
                ),
                "search_ms_total": round(sum(case.search_ms for case in self.cases), 3),
                "read_ms_total": round(sum(case.read_ms for case in self.cases), 3),
                "evidence_bytes_total": sum(case.evidence_bytes for case in self.cases),
            },
            "cases": [asdict(case) for case in self.cases],
        }


class RetrievalBenchmarkRunner:
    """Run deterministic search-and-read checks without making an LLM call."""

    def run(
        self,
        cases: Iterable[BenchmarkCase],
        provider: KnowledgeProvider,
        *,
        limit: int = 200,
        max_read_bytes: int = 20_000,
    ) -> RetrievalBenchmarkReport:
        results = [
            self._run_case(case, provider, limit=limit, max_read_bytes=max_read_bytes)
            for case in cases
        ]
        return RetrievalBenchmarkReport(tuple(results))

    @staticmethod
    def _run_case(
        case: BenchmarkCase,
        provider: KnowledgeProvider,
        *,
        limit: int,
        max_read_bytes: int,
    ) -> RetrievalCaseResult:
        started = time.perf_counter()
        query = case.retrieval_query or case.question
        search_started = time.perf_counter()
        page = provider.search(query, options=SearchOptions(limit=limit))
        search_ms = (time.perf_counter() - search_started) * 1000
        retrieved_sources = tuple(hit.display_name for hit in page.items)
        failures: list[str] = []
        expected_hits = []
        target_ranks: list[int] = []
        for expected in case.expected_sources:
            match = next(
                (
                    (rank, hit)
                    for rank, hit in enumerate(page.items, 1)
                    if hit.display_name.casefold() == expected.casefold()
                ),
                None,
            )
            if match is None:
                failures.append(f"expected source was not retrieved: {expected}")
            else:
                target_ranks.append(match[0])
                expected_hits.append(match[1])
        if case.answerable and not expected_hits:
            failures.append("answerable case produced no target source evidence")
        if not case.answerable and page.items:
            failures.append("unanswerable case retrieved candidate sources")

        evidence = []
        read_ms = 0.0
        evidence_bytes = 0
        for hit in expected_hits:
            read_started = time.perf_counter()
            try:
                item = provider.read(hit.locator, ReadOptions(max_bytes=max_read_bytes))
                evidence.append(item)
                evidence_bytes += len(str(item.content).encode("utf-8"))
            except Exception as exc:
                failures.append(f"could not read expected source {hit.display_name}: {exc}")
            finally:
                read_ms += (time.perf_counter() - read_started) * 1000
        evidence_text = "\n".join(str(item.content) for item in evidence).casefold()
        evidence_text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", evidence_text)
        evidence_text = " ".join(evidence_text.split())
        for phrase in case.required_evidence_phrases:
            if " ".join(phrase.casefold().split()) not in evidence_text:
                failures.append(f"required evidence phrase missing from expected source: {phrase}")
        evidence_sources = tuple(
            item.source_label or item.locator.resource_id for item in evidence
        )
        return RetrievalCaseResult(
            case_id=case.case_id,
            passed=not failures,
            failures=tuple(failures),
            retrieval_query=query,
            expected_rank=max(target_ranks) if case.expected_sources and len(target_ranks) == len(case.expected_sources) else None,
            retrieved_sources=retrieved_sources,
            evidence_sources=evidence_sources,
            evidence_bytes=evidence_bytes,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
            search_ms=round(search_ms, 3),
            read_ms=round(read_ms, 3),
            ranking_applicable=bool(case.expected_sources),
        )


def write_report(report: BenchmarkReport, path: str | Path) -> None:
    Path(path).write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def write_retrieval_report(report: RetrievalBenchmarkReport, path: str | Path) -> None:
    Path(path).write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
