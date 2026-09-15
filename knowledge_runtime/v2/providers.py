from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Protocol

from ..assets import KnowledgeAsset
from ..errors import KRUnsupported
from ..extractors import LocalExtractionBackend
from ..mineru_backend import MinerUCloudBackend
from .config import RuntimeConfig
from .contracts import (
    DocumentElement,
    DocumentIR,
    ParseReport,
    content_hash,
)
from .model_gateway import ModelGateway


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_PAGE_RE = re.compile(r"^\s*<!--\s*page\s*:\s*(\d+)\s*-->\s*$", re.IGNORECASE)
_LIST_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.+)$")
_TABLE_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)(.*)$")
_PLAIN_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".csv", ".json", ".xml"}


class ParserProvider(Protocol):
    name: str

    def parse(self, source: Path, *, document_id: str, revision_id: str) -> DocumentIR:
        ...


def _element_id(revision_id: str, element_type: str, start: int, end: int, text: str) -> str:
    identity = "\x00".join((revision_id, element_type, str(start), str(end), text))
    return "el-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _table_rows(lines: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in lines:
        if not _TABLE_RE.match(line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        rows.append(cells)
    return rows


def _elements_from_markdown(markdown: str, *, revision_id: str) -> list[DocumentElement]:
    """Create deterministic, line-addressable elements from a Markdown view.

    This parser is deliberately small and deterministic.  It is the safe
    fallback for text-compatible formats; layout-aware providers can emit the
    same element contract with richer payload and bounding-box provenance.
    """

    lines = markdown.splitlines()
    elements: list[DocumentElement] = []
    heading_stack: list[str] = []
    page = 1
    index = 0

    def add(
        element_type: str,
        start: int,
        end: int,
        text: str,
        *,
        payload: dict[str, Any] | None = None,
        element_page: int | None = None,
    ) -> None:
        clean = text.strip()
        if not clean:
            return
        elements.append(
            DocumentElement(
                element_id=_element_id(revision_id, element_type, start, end, clean),
                revision_id=revision_id,
                element_type=element_type,  # type: ignore[arg-type]
                text=clean,
                section_path=tuple(heading_stack),
                page=element_page,
                bbox=None,
                payload=payload or {},
                provenance={"line_start": start, "line_end": end, "page": element_page},
                confidence=1.0,
                content_hash=content_hash(clean),
            )
        )

    while index < len(lines):
        line = lines[index]
        page_match = _PAGE_RE.match(line)
        if page_match:
            page = max(1, int(page_match.group(1)))
            index += 1
            continue
        if not line.strip():
            index += 1
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip().rstrip("#").rstrip()
            heading_stack[:] = heading_stack[: level - 1]
            heading_stack.append(title)
            add("heading", index + 1, index + 1, title, payload={"level": level}, element_page=page)
            index += 1
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            start = index + 1
            language = fence.group(2).strip()
            index += 1
            body: list[str] = []
            while index < len(lines) and not _FENCE_RE.match(lines[index]):
                body.append(lines[index])
                index += 1
            end = min(len(lines), index + 1)
            if index < len(lines):
                index += 1
            add(
                "code",
                start,
                end,
                "\n".join(body),
                payload={"language": language, "lines": body},
                element_page=page,
            )
            continue

        if _LIST_RE.match(line):
            start = index + 1
            items: list[str] = []
            while index < len(lines) and _LIST_RE.match(lines[index]):
                items.append(_LIST_RE.match(lines[index]).group(1).strip())  # type: ignore[union-attr]
                index += 1
            add(
                "list",
                start,
                index,
                "\n".join(f"- {item}" for item in items),
                payload={"items": items},
                element_page=page,
            )
            continue

        if _TABLE_RE.match(line):
            start = index + 1
            table_lines: list[str] = []
            while index < len(lines) and _TABLE_RE.match(lines[index]):
                table_lines.append(lines[index])
                index += 1
            rows = _table_rows(table_lines)
            if rows:
                add(
                    "table",
                    start,
                    index,
                    "\n".join(" | ".join(row) for row in rows),
                    payload={"rows": rows},
                    element_page=page,
                )
            continue

        start = index + 1
        paragraph: list[str] = []
        while index < len(lines):
            candidate = lines[index]
            if not candidate.strip() or _PAGE_RE.match(candidate) or _HEADING_RE.match(candidate):
                break
            if paragraph and (_LIST_RE.match(candidate) or _TABLE_RE.match(candidate) or _FENCE_RE.match(candidate)):
                break
            paragraph.append(candidate.strip())
            index += 1
        add("paragraph", start, index, "\n".join(paragraph), element_page=page)

    return elements


def markdown_to_ir(
    markdown: str,
    *,
    document_id: str,
    revision_id: str,
    parser: str,
    version: str,
    metadata: dict[str, Any] | None = None,
    source_name: str | None = None,
    provider_name: str | None = None,
    egress_allowed: bool = False,
    document_type: str = "text/markdown",
    overall_grade: str = "good",
    warnings: tuple[str, ...] = (),
    content_list: Any | None = None,
) -> DocumentIR:
    elements = tuple(_elements_from_markdown(markdown, revision_id=revision_id))
    pages = [element.page for element in elements if element.page is not None]
    report = ParseReport(
        document_type=document_type,
        page_count=max(pages, default=1 if elements else 0),
        text_coverage=1.0 if elements or not markdown.strip() else 0.0,
        layout_quality=1.0 if document_type.startswith("text/") else 0.75,
        ocr_quality=1.0,
        table_quality=1.0,
        reading_order_quality=1.0,
        missing_regions=(),
        suspicious_regions=(),
        parser_name=parser,
        parser_version=version,
        overall_grade=overall_grade,  # type: ignore[arg-type]
        warnings=warnings,
        provider_name=provider_name or parser,
        egress_allowed=egress_allowed,
    )
    result_metadata = dict(metadata or {})
    if source_name:
        result_metadata.setdefault("source_name", source_name)
    if content_list is not None:
        result_metadata.setdefault("content_list", content_list)
    return DocumentIR(
        document_id=document_id,
        revision_id=revision_id,
        metadata=result_metadata,
        elements=elements,
        parse_report=report,
        source_artifacts=(),
    )


def _optional_docling_markdown(source: Path) -> tuple[str, str] | None:
    try:
        from docling.document_converter import DocumentConverter  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        result = DocumentConverter().convert(str(source))
        document = getattr(result, "document", result)
        markdown = document.export_to_markdown()
        return str(markdown), "docling"
    except Exception as exc:
        raise KRUnsupported(f"Docling failed to parse {source.name}: {exc}") from exc


def _optional_unstructured_markdown(source: Path) -> tuple[str, str] | None:
    try:
        from unstructured.partition.auto import partition  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        elements = partition(filename=str(source))
        markdown = "\n\n".join(str(element) for element in elements if str(element).strip())
        return markdown, "unstructured"
    except Exception as exc:
        raise KRUnsupported(f"Unstructured failed to parse {source.name}: {exc}") from exc


class LocalProvider:
    name = "local"

    def __init__(
        self,
        config: RuntimeConfig,
        model_gateway: ModelGateway | None = None,
        *,
        degraded_mode: bool = False,
    ) -> None:
        self.config = config
        self.model_gateway = model_gateway
        self.degraded_mode = degraded_mode
        self._fallback = LocalExtractionBackend()

    def parse(self, source: Path, *, document_id: str, revision_id: str) -> DocumentIR:
        source = Path(source)
        suffix = source.suffix.lower()
        if suffix in _PLAIN_SUFFIXES:
            asset = self._fallback.extract(source)
            return markdown_to_ir(
                asset.markdown,
                document_id=document_id,
                revision_id=revision_id,
                parser="local-text",
                version=asset.parser_version,
                metadata={
                    **asset.metadata,
                    "source_path": asset.source_path,
                    "source_name": asset.source_name,
                    "source_hash": asset.source_hash,
                },
                source_name=asset.source_name,
                provider_name=self.name,
                document_type="text/markdown" if suffix in {".md", ".markdown"} else "text/plain",
                content_list=asset.content_list,
            )

        parsed = _optional_docling_markdown(source)
        if parsed is None:
            parsed = _optional_unstructured_markdown(source)
        if parsed is not None:
            markdown, parser = parsed
            return markdown_to_ir(
                markdown,
                document_id=document_id,
                revision_id=revision_id,
                parser=parser,
                version="adapter-v1",
                metadata={"source_path": str(source.resolve()), "source_name": source.name},
                source_name=source.name,
                provider_name=self.name,
                document_type=f"application/{suffix.lstrip('.') or 'octet-stream'}",
            )

        if not self.degraded_mode:
            raise KRUnsupported(
                f"no layout-aware local parser is installed for {source.suffix or 'extensionless'}; "
                "install the local-parser extra or explicitly enable degraded_mode"
            )
        asset = self._fallback.extract(source)
        return markdown_to_ir(
            asset.markdown,
            document_id=document_id,
            revision_id=revision_id,
            parser="local-degraded",
            version=asset.parser_version,
            metadata={
                "source_path": asset.source_path,
                "source_name": asset.source_name,
                "source_hash": asset.source_hash,
            },
            source_name=asset.source_name,
            provider_name=self.name,
            document_type=f"application/{suffix.lstrip('.') or 'octet-stream'}",
            overall_grade="degraded",
            warnings=("layout-aware local parser unavailable; degraded text extraction used",),
            content_list=asset.content_list,
        )


class DoclingProvider(LocalProvider):
    name = "docling"


class UnstructuredProvider(LocalProvider):
    name = "unstructured"


class MinerUProvider:
    name = "mineru-cloud"

    def __init__(self, *, config: RuntimeConfig, backend: MinerUCloudBackend | Any | None = None) -> None:
        self.config = config
        self.backend = backend or MinerUCloudBackend()

    def parse(self, source: Path, *, document_id: str, revision_id: str) -> DocumentIR:
        self.config.require_remote_parser()
        asset: KnowledgeAsset = self.backend.extract(Path(source))
        metadata = {
            **asset.metadata,
            "source_path": asset.source_path,
            "source_name": asset.source_name,
            "source_hash": asset.source_hash,
        }
        return markdown_to_ir(
            asset.markdown,
            document_id=document_id,
            revision_id=revision_id,
            parser=asset.parser_name,
            version=asset.parser_version,
            metadata=metadata,
            source_name=asset.source_name,
            provider_name=self.name,
            egress_allowed=True,
            document_type="application/pdf" if str(source).lower().endswith(".pdf") else "application/octet-stream",
            content_list=asset.content_list,
        )


class ParserRouter:
    def __init__(
        self,
        *,
        config: RuntimeConfig,
        local: ParserProvider,
        remote: ParserProvider | None = None,
    ) -> None:
        self.config = config
        self.local = local
        self.remote = remote

    def choose(self, source: Path) -> ParserProvider:
        if self.config.allow_remote_parser and self.remote is not None:
            return self.remote
        return self.local

    def parse(self, source: Path, *, document_id: str, revision_id: str) -> DocumentIR:
        return self.choose(source).parse(source, document_id=document_id, revision_id=revision_id)

