from __future__ import annotations

import base64
import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Literal, Mapping, Sequence


ElementType = Literal[
    "heading",
    "paragraph",
    "list",
    "table",
    "picture",
    "formula",
    "code",
    "header",
    "footer",
    "unknown",
]

QualityGrade = Literal["excellent", "good", "degraded", "failed", "unknown"]


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(_canonical(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def content_hash(content: str | bytes) -> str:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()


def _validate_score(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return float(value)


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    object_key: str
    media_type: str
    sha256: str
    size_bytes: int
    kind: str
    revision_id: str

    def __post_init__(self) -> None:
        if not self.artifact_id or not self.object_key or not self.revision_id:
            raise ValueError("artifact identity is required")
        if self.size_bytes < 0:
            raise ValueError("artifact size cannot be negative")


@dataclass(frozen=True)
class ParseReport:
    document_type: str
    page_count: int
    text_coverage: float
    layout_quality: float
    ocr_quality: float
    table_quality: float
    reading_order_quality: float
    missing_regions: tuple[Mapping[str, Any], ...]
    suspicious_regions: tuple[Mapping[str, Any], ...]
    parser_name: str
    parser_version: str
    overall_grade: QualityGrade
    warnings: tuple[str, ...] = ()
    provider_name: str | None = None
    egress_allowed: bool = False

    def __post_init__(self) -> None:
        if self.page_count < 0:
            raise ValueError("page_count cannot be negative")
        for name in (
            "text_coverage",
            "layout_quality",
            "ocr_quality",
            "table_quality",
            "reading_order_quality",
        ):
            _validate_score(name, float(getattr(self, name)))
        if not self.parser_name or not self.parser_version:
            raise ValueError("parser identity is required")

    @classmethod
    def empty(cls, parser_name: str, parser_version: str) -> "ParseReport":
        return cls(
            document_type="unknown",
            page_count=0,
            text_coverage=1.0,
            layout_quality=1.0,
            ocr_quality=1.0,
            table_quality=1.0,
            reading_order_quality=1.0,
            missing_regions=(),
            suspicious_regions=(),
            parser_name=parser_name,
            parser_version=parser_version,
            overall_grade="good",
        )


@dataclass(frozen=True)
class DocumentElement:
    element_id: str
    revision_id: str
    element_type: ElementType
    text: str
    section_path: tuple[str, ...]
    page: int | None
    bbox: tuple[float, float, float, float] | None
    payload: Mapping[str, Any]
    provenance: Mapping[str, Any]
    confidence: float | None
    content_hash: str

    def __post_init__(self) -> None:
        if not self.element_id or not self.revision_id:
            raise ValueError("element identity is required")
        if self.page is not None and self.page <= 0:
            raise ValueError("page must be positive")
        if self.bbox is not None and len(self.bbox) != 4:
            raise ValueError("bbox must contain four coordinates")
        if self.confidence is not None:
            _validate_score("confidence", self.confidence)
        object.__setattr__(self, "section_path", tuple(str(value) for value in self.section_path))


@dataclass(frozen=True)
class DocumentIR:
    document_id: str
    revision_id: str
    metadata: Mapping[str, Any]
    elements: tuple[DocumentElement, ...]
    parse_report: ParseReport
    source_artifacts: tuple[ArtifactRef, ...]

    def __post_init__(self) -> None:
        if not self.document_id or not self.revision_id:
            raise ValueError("document identity is required")
        for element in self.elements:
            if element.revision_id != self.revision_id:
                raise ValueError("all DocumentIR elements must use the DocumentIR revision")
        for artifact in self.source_artifacts:
            if artifact.revision_id != self.revision_id:
                raise ValueError("all source artifacts must use the DocumentIR revision")


@dataclass(frozen=True)
class EvidenceRef:
    document_id: str
    revision_id: str
    element_id: str
    selector: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.document_id or not self.revision_id or not self.element_id:
            raise ValueError("EvidenceRef identity is required")

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "revision_id": self.revision_id,
            "element_id": self.element_id,
            "selector": _canonical(self.selector) if self.selector is not None else None,
        }

    def to_json(self) -> str:
        return _json_bytes(self.as_dict()).decode("utf-8")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvidenceRef":
        return cls(
            document_id=str(value["document_id"]),
            revision_id=str(value["revision_id"]),
            element_id=str(value["element_id"]),
            selector=value.get("selector"),
        )


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    ref: EvidenceRef
    content: str | bytes
    content_hash: str
    source_label: str
    media_type: str
    representation: str
    resolved_provenance: Mapping[str, Any]
    truncated: bool
    source_revision: str
    encoding: str
    retrieved_at: str
    derived_from: str | None = None

    @classmethod
    def from_content(
        cls,
        *,
        ref: EvidenceRef,
        content: str | bytes,
        media_type: str,
        representation: str,
        source_label: str,
        resolved_provenance: Mapping[str, Any] | None = None,
        derived_from: str | None = None,
        max_bytes: int | None = None,
        evidence_id: str | None = None,
    ) -> "Evidence":
        full_hash = content_hash(content)
        visible = content
        truncated = False
        if max_bytes is not None:
            if max_bytes <= 0:
                raise ValueError("max_bytes must be positive")
            raw = content.encode("utf-8") if isinstance(content, str) else content
            if len(raw) > max_bytes:
                raw = raw[:max_bytes]
                visible = raw.decode("utf-8", errors="ignore") if isinstance(content, str) else raw
                truncated = True
        identity = hashlib.sha256(f"{ref.to_json()}\n{full_hash}".encode("utf-8")).hexdigest()[:16]
        return cls(
            evidence_id=evidence_id or f"ev-{identity}",
            ref=ref,
            content=visible,
            content_hash=full_hash,
            source_label=source_label,
            media_type=media_type,
            representation=representation,
            resolved_provenance=dict(resolved_provenance or {}),
            truncated=truncated,
            source_revision=ref.revision_id,
            encoding="utf-8" if isinstance(visible, str) else "base64",
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            derived_from=derived_from,
        )

    def as_model_input(self) -> dict[str, Any]:
        visible = self.content
        if isinstance(visible, bytes):
            visible = base64.b64encode(visible).decode("ascii")
        return {
            "evidence_id": self.evidence_id,
            "ref": self.ref.as_dict(),
            "source_revision": self.source_revision,
            "content": visible,
            "content_hash": self.content_hash,
            "media_type": self.media_type,
            "representation": self.representation,
            "resolved_provenance": dict(self.resolved_provenance),
            "truncated": self.truncated,
            "source_label": self.source_label,
            "derived_from": self.derived_from,
        }


@dataclass(frozen=True)
class Entity:
    entity_id: str
    canonical_name: str
    entity_class: str
    aliases: tuple[str, ...] = ()
    domain: str | None = None


@dataclass(frozen=True)
class Claim:
    claim_id: str
    subject_id: str
    predicate: str
    value: Any
    evidence_refs: tuple[EvidenceRef, ...]
    confidence: float
    validity: Mapping[str, Any] = field(default_factory=dict)
    status: str = "PROPOSED"

    def __post_init__(self) -> None:
        _validate_score("confidence", self.confidence)
        if not self.evidence_refs:
            raise ValueError("Claim requires at least one Evidence ref")


@dataclass(frozen=True)
class BusinessTerm:
    term_id: str
    term: str
    definition: str | None = None
    aliases: tuple[str, ...] = ()
    domain: str | None = None
    status: str = "PROPOSED"


@dataclass(frozen=True)
class Metadata:
    values: Mapping[str, Any]
    source: str = "canonical"


@dataclass(frozen=True)
class DocumentRevision:
    document_id: str
    revision_id: str
    source_hash: str
    source_name: str
    parser_name: str
    parser_version: str
    state: str
    created_at: str


@dataclass(frozen=True)
class EvidenceFilters:
    document_ids: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    current_only: bool = True


@dataclass(frozen=True)
class AssetFilters:
    domains: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    current_only: bool = True


@dataclass(frozen=True)
class EvidenceSearchRequest:
    query: str
    filters: EvidenceFilters | None = None
    hints: Mapping[str, Any] = field(default_factory=dict)
    limit: int = 20
    cursor: str | None = None

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("query must not be empty")
        if self.limit <= 0 or self.limit > 200:
            raise ValueError("limit must be between 1 and 200")


@dataclass(frozen=True)
class AssetSearchRequest:
    query: str
    filters: AssetFilters | None = None
    limit: int = 20
    cursor: str | None = None

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("query must not be empty")
        if self.limit <= 0 or self.limit > 200:
            raise ValueError("limit must be between 1 and 200")


@dataclass(frozen=True)
class SearchHitV2:
    ref: EvidenceRef
    display_name: str
    preview: str | None = None
    score: float | None = None
    rank: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvidenceSearchPage:
    items: tuple[SearchHitV2, ...]
    next_cursor: str | None = None
    snapshot_id: str | None = None
    partial: bool = False
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AssetSearchHit:
    document_id: str
    revision_id: str
    display_name: str
    source_label: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    score: float | None = None


@dataclass(frozen=True)
class AssetSearchPage:
    items: tuple[AssetSearchHit, ...]
    next_cursor: str | None = None
    snapshot_id: str | None = None
    partial: bool = False
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RevisionIndexInput:
    document_id: str
    revision_id: str
    source_name: str
    elements: tuple[DocumentElement, ...]
    chunks: tuple[Any, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_ir(cls, ir: DocumentIR, *, chunks: Sequence[Any] = ()) -> "RevisionIndexInput":
        return cls(
            document_id=ir.document_id,
            revision_id=ir.revision_id,
            source_name=str(ir.metadata.get("source_name", ir.document_id)),
            elements=ir.elements,
            chunks=tuple(chunks),
            metadata=ir.metadata,
        )


@dataclass(frozen=True)
class IndexPublishResult:
    index_version: str
    document_id: str
    revision_id: str
    indexed_count: int
    checksum: str
    state: str = "SUCCEEDED"


@dataclass(frozen=True)
class IndexRebuildRequest:
    index_version: str
    document_ids: tuple[str, ...] = ()
    dry_run: bool = False


@dataclass(frozen=True)
class IndexBuildReport:
    index_version: str
    revisions_seen: int
    elements_indexed: int
    chunks_indexed: int
    state: str
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


ProposalStatus = Literal[
    "PROPOSED",
    "AUTO_ACCEPTED",
    "VERIFIED",
    "CERTIFIED",
    "CONFLICTED",
    "REVIEW",
    "REJECTED",
]


@dataclass(frozen=True)
class SemanticProposal:
    proposal_id: str
    kind: str
    payload: Mapping[str, Any]
    evidence_refs: tuple[EvidenceRef, ...]
    extractor_version: str
    confidence: float
    status: ProposalStatus = "PROPOSED"
    idempotency_key: str = ""
    conflict_details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_score("confidence", self.confidence)
        if (
            not self.evidence_refs
            and self.status in {"AUTO_ACCEPTED", "VERIFIED", "CERTIFIED"}
            and self.kind in {"claim", "entity", "term", "relation"}
        ):
            raise ValueError("semantic proposal requires Evidence refs")


def object_name(value: str) -> str:
    """Return a safe final object name for artifact adapters."""
    return PurePosixPath(value.replace("\\", "/")).name
