from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .canonical import CanonicalStore
from .chunking import ChunkSetBuilder
from .contracts import DocumentIR, IndexPublishResult, RevisionIndexInput
from .index import IndexBackend
from .providers import ParserRouter
from .quality import QualityDecision, QualityGate


INGESTION_STATES = (
    "RECEIVED",
    "ARTIFACT_STORED",
    "PARSED",
    "DOCUMENT_IR_ARTIFACT_STORED",
    "QUALITY_CHECKED",
    "CANONICAL_COMMITTED",
    "INDEX_PUBLISHED",
    "CURRENT_REVISION_PUBLISHED",
    "SEMANTIC_ENRICHMENT_PENDING",
)


def stable_document_id(source: str | Path) -> str:
    identity = str(Path(source).resolve()).replace("\\", "/").casefold()
    return "doc-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def new_revision_id(document_id: str, source_hash: str) -> str:
    return "rev-" + hashlib.sha256(f"{document_id}\x00{source_hash}".encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class IngestionResult:
    document_id: str
    revision_id: str
    state: str
    reused: bool = False
    history: tuple[str, ...] = ()
    error: str | None = None
    quality: QualityDecision | None = None
    diagnostics: dict[str, Any] | None = None


@dataclass(frozen=True)
class _ParseSelection:
    ir: DocumentIR | None
    decision: QualityDecision
    attempts: tuple[dict[str, Any], ...]


class IngestionService:
    def __init__(
        self,
        *,
        router: ParserRouter,
        quality: QualityGate | None,
        canonical: CanonicalStore,
        artifacts: ArtifactStore | None,
        index: IndexBackend,
        chunk_builder: ChunkSetBuilder | None = None,
    ) -> None:
        self.router = router
        self.quality = quality or QualityGate()
        self.canonical = canonical
        self.artifacts = artifacts
        self.index = index
        self.chunk_builder = chunk_builder or ChunkSetBuilder()

    def ingest(self, source: str | Path, *, document_id: str | None = None, provider: str = "auto") -> IngestionResult:
        path = Path(source)
        document_id = document_id or stable_document_id(path)
        history: list[str] = ["RECEIVED"]
        revision_id = "unknown"
        try:
            raw = path.read_bytes()
            source_hash = hashlib.sha256(raw).hexdigest()
            existing = self.canonical.find_revision_by_source_hash(source_hash)
            if existing is not None and self.canonical.current_revision(existing.document_id) == existing.revision_id:
                return IngestionResult(
                    existing.document_id,
                    existing.revision_id,
                    "CURRENT_REVISION_PUBLISHED",
                    reused=True,
                    history=tuple(INGESTION_STATES),
                )
            if existing is not None:
                document_id = existing.document_id
                revision_id = existing.revision_id
            else:
                revision_id = new_revision_id(document_id, source_hash)

            source_artifact = None
            if self.artifacts is not None:
                source_artifact = self.artifacts.put_bytes(
                    document_id=document_id,
                    revision_id=revision_id,
                    name=path.name,
                    data=raw,
                    media_type=_media_type(path),
                    kind="source",
                )
                history.append("ARTIFACT_STORED")

            selection = self._select_parser(path, provider=provider, document_id=document_id, revision_id=revision_id)
            ir = selection.ir
            decision = selection.decision
            diagnostics = {"parse_attempts": [dict(attempt) for attempt in selection.attempts]}
            if ir is not None:
                if source_artifact is not None:
                    ir = replace(ir, source_artifacts=(*ir.source_artifacts, source_artifact))
                history.append("PARSED")
                if self.artifacts is not None:
                    ir_artifact = self.artifacts.put_bytes(
                        document_id=document_id,
                        revision_id=revision_id,
                        name="document-ir.json",
                        data=document_ir_bytes(ir),
                        media_type="application/json",
                        kind="document-ir",
                    )
                    ir = replace(ir, source_artifacts=(*ir.source_artifacts, ir_artifact))
                    history.append("DOCUMENT_IR_ARTIFACT_STORED")
            history.append("QUALITY_CHECKED")
            if decision.action != "ACCEPTED":
                history.append(decision.action)
                self._record_job(
                    document_id=document_id,
                    revision_id=revision_id,
                    state=decision.action,
                    history=history,
                    error=decision.reason,
                    diagnostics=diagnostics,
                )
                return IngestionResult(
                    document_id,
                    revision_id,
                    decision.action,
                    history=tuple(history),
                    quality=decision,
                    error=decision.reason,
                    diagnostics=diagnostics,
                )

            if ir is None:
                raise RuntimeError("accepted parser selection did not produce DocumentIR")

            self.canonical.put_revision(ir, source_hash=source_hash)
            self.canonical.record_artifacts(ir.document_id, ir.revision_id, ir.source_artifacts)
            committed_ir = self.canonical.get_ir(ir.document_id, ir.revision_id)
            history.append("CANONICAL_COMMITTED")
            index_version = f"{committed_ir.revision_id}:{self.chunk_builder.version}"
            self.canonical.begin_publication(committed_ir.document_id, committed_ir.revision_id, index_version=index_version)
            self.canonical.record_index_run(index_version, state="PENDING")
            try:
                self.canonical.record_index_run(index_version, state="RUNNING")
                indexed = self.index.publish_revision(
                    RevisionIndexInput.from_ir(committed_ir, chunks=self.chunk_builder.build(committed_ir)),
                    index_version=index_version,
                )
                if indexed.state != "SUCCEEDED":
                    raise RuntimeError(f"index publication returned state {indexed.state}")
                if indexed.index_version != index_version:
                    raise RuntimeError("index publication returned an unexpected index version")
            except Exception as exc:
                self.canonical.record_index_run(index_version, state="FAILED", error=str(exc))
                remove_revision = getattr(self.index, "remove_revision", None)
                if remove_revision is not None:
                    remove_revision(committed_ir.document_id, committed_ir.revision_id)
                raise
            history.append("INDEX_PUBLISHED")
            self.canonical.record_index_run(index_version, state="SUCCEEDED", indexed_count=indexed.indexed_count)
            try:
                self.canonical.publish_current(committed_ir.document_id, committed_ir.revision_id, index_version=index_version)
            except Exception:
                remove_revision = getattr(self.index, "remove_revision", None)
                if remove_revision is not None:
                    remove_revision(committed_ir.document_id, committed_ir.revision_id)
                raise
            history.extend(("CURRENT_REVISION_PUBLISHED", "SEMANTIC_ENRICHMENT_PENDING"))
            self._record_job(
                document_id=committed_ir.document_id,
                revision_id=committed_ir.revision_id,
                state="SEMANTIC_ENRICHMENT_PENDING",
                history=history,
                diagnostics=diagnostics,
            )
            return IngestionResult(
                committed_ir.document_id,
                committed_ir.revision_id,
                "CURRENT_REVISION_PUBLISHED",
                history=tuple(history),
                quality=decision,
                diagnostics=diagnostics,
            )
        except Exception as exc:
            if not history or history[-1] != "FAILED":
                history.append("FAILED")
            self._record_failure(document_id, revision_id, str(exc), history)
            return IngestionResult(
                document_id,
                revision_id,
                "FAILED",
                history=tuple(history),
                error=str(exc),
            )

    def _select_parser(
        self,
        path: Path,
        *,
        provider: str,
        document_id: str,
        revision_id: str,
    ) -> _ParseSelection:
        attempts: list[dict[str, Any]] = []
        last_ir: DocumentIR | None = None
        last_decision: QualityDecision | None = None
        candidates = self.router.parse_candidates(
            path,
            document_id=document_id,
            revision_id=revision_id,
            provider=provider,
        )
        for candidate in candidates:
            try:
                candidate_ir = candidate.parse(path, document_id=document_id, revision_id=revision_id)
            except Exception as exc:
                attempts.append(
                    {
                        "provider": candidate.name,
                        "outcome": "FAILED",
                        "reason": _diagnostic_error(exc),
                    }
                )
                continue
            decision = self.quality.evaluate(candidate_ir.parse_report)
            attempts.append(
                {
                    "provider": candidate.name,
                    "outcome": decision.action,
                    "reason": decision.reason,
                    "parser_name": candidate_ir.parse_report.parser_name,
                    "parser_version": candidate_ir.parse_report.parser_version,
                    "egress_allowed": candidate_ir.parse_report.egress_allowed,
                }
            )
            last_ir = candidate_ir
            last_decision = decision
            if decision.action == "ACCEPTED":
                return _ParseSelection(
                    ir=_annotate_parse_attempts(candidate_ir, attempts),
                    decision=decision,
                    attempts=tuple(attempts),
                )

        if last_ir is None:
            decision = QualityDecision("ESCALATED", "no_eligible_parser_succeeded")
            return _ParseSelection(ir=None, decision=decision, attempts=tuple(attempts))
        if last_decision is None or last_decision.action == "RETRY":
            decision = QualityDecision("ESCALATED", "quality_below_threshold_no_eligible_fallback")
        else:
            decision = last_decision
        return _ParseSelection(
            ir=_annotate_parse_attempts(last_ir, attempts),
            decision=decision,
            attempts=tuple(attempts),
        )

    def _record_job(
        self,
        *,
        document_id: str,
        revision_id: str,
        state: str,
        history: list[str],
        error: str | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        recorder = getattr(self.canonical, "record_ingestion_job", None)
        if recorder is not None:
            details: dict[str, Any] = {
                "document_id": document_id,
                "revision_id": revision_id,
                "state": state,
                "history": list(history),
            }
            if error:
                details["error"] = error
            if diagnostics:
                details["diagnostics"] = diagnostics
            recorder(f"ingest:{document_id}:{revision_id}", **details)

    def _record_failure(self, document_id: str, revision_id: str, error: str, history: list[str]) -> None:
        recorder = getattr(self.canonical, "record_ingestion_job", None)
        if recorder is not None:
            recorder(
                f"ingest:{document_id}:{revision_id}",
                document_id=document_id,
                revision_id=revision_id,
                state="FAILED",
                error=error,
                history=list(history),
            )


def _annotate_parse_attempts(ir: DocumentIR, attempts: list[dict[str, Any]]) -> DocumentIR:
    metadata = {**dict(ir.metadata), "parse_attempts": [dict(attempt) for attempt in attempts]}
    warnings = tuple(ir.parse_report.warnings) + tuple(
        f"parse_attempt:{attempt['provider']}:{attempt['outcome']}" for attempt in attempts
    )
    return replace(ir, metadata=metadata, parse_report=replace(ir.parse_report, warnings=warnings))


def _diagnostic_error(exc: Exception) -> str:
    message = str(exc).replace("\n", " ").strip()
    return f"{type(exc).__name__}: {message[:300]}" if message else type(exc).__name__


def document_ir_bytes(ir: DocumentIR) -> bytes:
    """Return the deterministic immutable snapshot stored beside raw input.

    The snapshot includes source artifacts already known at parse time. Its own
    ArtifactRef is intentionally not embedded because that would require a
    self-referential content hash.
    """

    payload = {
        "document_id": ir.document_id,
        "revision_id": ir.revision_id,
        "metadata": dict(ir.metadata),
        "elements": [
            {
                "element_id": element.element_id,
                "revision_id": element.revision_id,
                "element_type": element.element_type,
                "text": element.text,
                "section_path": list(element.section_path),
                "page": element.page,
                "bbox": list(element.bbox) if element.bbox else None,
                "payload": dict(element.payload),
                "provenance": dict(element.provenance),
                "confidence": element.confidence,
                "content_hash": element.content_hash,
            }
            for element in ir.elements
        ],
        "parse_report": {
            "document_type": ir.parse_report.document_type,
            "page_count": ir.parse_report.page_count,
            "text_coverage": ir.parse_report.text_coverage,
            "layout_quality": ir.parse_report.layout_quality,
            "ocr_quality": ir.parse_report.ocr_quality,
            "table_quality": ir.parse_report.table_quality,
            "reading_order_quality": ir.parse_report.reading_order_quality,
            "missing_regions": list(ir.parse_report.missing_regions),
            "suspicious_regions": list(ir.parse_report.suspicious_regions),
            "parser_name": ir.parse_report.parser_name,
            "parser_version": ir.parse_report.parser_version,
            "overall_grade": ir.parse_report.overall_grade,
            "warnings": list(ir.parse_report.warnings),
            "provider_name": ir.parse_report.provider_name,
            "egress_allowed": ir.parse_report.egress_allowed,
        },
        "source_artifacts": [
            {
                "artifact_id": artifact.artifact_id,
                "object_key": artifact.object_key,
                "media_type": artifact.media_type,
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "kind": artifact.kind,
                "revision_id": artifact.revision_id,
            }
            for artifact in ir.source_artifacts
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default).encode("utf-8")


def _json_default(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"DocumentIR contains a non-serializable value: {type(value).__name__}")


def _media_type(path: Path) -> str:
    return {
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".txt": "text/plain",
        ".pdf": "application/pdf",
        ".json": "application/json",
        ".html": "text/html",
    }.get(path.suffix.lower(), "application/octet-stream")
