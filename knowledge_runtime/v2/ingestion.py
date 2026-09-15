from __future__ import annotations

import hashlib
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

            artifact_ref = None
            if self.artifacts is not None:
                artifact_ref = self.artifacts.put_bytes(
                    document_id=document_id,
                    revision_id=new_revision_id(document_id, source_hash),
                    name=path.name,
                    data=raw,
                    media_type=_media_type(path),
                )
                history.append("ARTIFACT_STORED")

            revision_id = existing.revision_id if existing is not None else new_revision_id(document_id, source_hash)
            ir = self._parse(path, provider=provider, document_id=document_id, revision_id=revision_id)
            if artifact_ref is not None:
                ir = replace(ir, source_artifacts=(artifact_ref,))
            history.append("PARSED")

            decision = self.quality.evaluate(ir.parse_report)
            history.append("QUALITY_CHECKED")
            if decision.action != "ACCEPTED":
                return IngestionResult(
                    document_id,
                    revision_id,
                    decision.action,
                    history=tuple(history),
                    quality=decision,
                    error=decision.reason,
                )

            self.canonical.put_revision(ir, source_hash=source_hash)
            history.append("CANONICAL_COMMITTED")
            index_version = f"{ir.revision_id}:{self.chunk_builder.version}"
            try:
                indexed = self.index.publish_revision(
                    RevisionIndexInput.from_ir(ir, chunks=self.chunk_builder.build(ir)),
                    index_version=index_version,
                )
            except Exception as exc:
                self.canonical.record_index_run(index_version, state="FAILED", error=str(exc))
                remove_revision = getattr(self.index, "remove_revision", None)
                if remove_revision is not None:
                    remove_revision(ir.document_id, ir.revision_id)
                raise
            history.append("INDEX_PUBLISHED")
            self.canonical.record_index_run(indexed.index_version, state="SUCCEEDED", indexed_count=indexed.indexed_count)
            self.canonical.begin_publication(ir.document_id, ir.revision_id, index_version=indexed.index_version)
            try:
                self.canonical.publish_current(ir.document_id, ir.revision_id, index_version=indexed.index_version)
            except Exception:
                remove_revision = getattr(self.index, "remove_revision", None)
                if remove_revision is not None:
                    remove_revision(ir.document_id, ir.revision_id)
                raise
            history.extend(("CURRENT_REVISION_PUBLISHED", "SEMANTIC_ENRICHMENT_PENDING"))
            self._record_job(ir, state="SEMANTIC_ENRICHMENT_PENDING", history=history)
            return IngestionResult(
                ir.document_id,
                ir.revision_id,
                "CURRENT_REVISION_PUBLISHED",
                history=tuple(history),
            )
        except Exception as exc:
            self._record_failure(document_id, locals().get("revision_id", "unknown"), str(exc), history)
            return IngestionResult(
                document_id,
                locals().get("revision_id", "unknown"),
                "FAILED",
                history=tuple(history),
                error=str(exc),
            )

    def _parse(self, path: Path, *, provider: str, document_id: str, revision_id: str) -> DocumentIR:
        if provider == "local":
            return self.router.local.parse(path, document_id=document_id, revision_id=revision_id)
        if provider in {"remote", "mineru", "mineru-cloud"}:
            if self.router.remote is None:
                raise RuntimeError("remote parser is not configured")
            return self.router.remote.parse(path, document_id=document_id, revision_id=revision_id)
        return self.router.parse(path, document_id=document_id, revision_id=revision_id)

    def _record_job(self, ir: DocumentIR, *, state: str, history: list[str]) -> None:
        recorder = getattr(self.canonical, "record_ingestion_job", None)
        if recorder is not None:
            recorder(
                f"ingest:{ir.document_id}:{ir.revision_id}",
                document_id=ir.document_id,
                revision_id=ir.revision_id,
                state=state,
                history=tuple(history),
            )

    def _record_failure(self, document_id: str, revision_id: str, error: str, history: list[str]) -> None:
        recorder = getattr(self.canonical, "record_ingestion_job", None)
        if recorder is not None:
            recorder(
                f"ingest:{document_id}:{revision_id}",
                document_id=document_id,
                revision_id=revision_id,
                state="FAILED",
                error=error,
                history=tuple(history),
            )


def _media_type(path: Path) -> str:
    return {
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".txt": "text/plain",
        ".pdf": "application/pdf",
        ".json": "application/json",
        ".html": "text/html",
    }.get(path.suffix.lower(), "application/octet-stream")
