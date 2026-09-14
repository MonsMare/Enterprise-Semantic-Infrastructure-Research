from __future__ import annotations

import hashlib
import html
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Protocol
from xml.etree import ElementTree

from .assets import KnowledgeAsset, KnowledgeAssetStore, stable_resource_id
from .errors import KRUnsupported


class ExtractionBackend(Protocol):
    def extract(self, source: str | Path, *, store: KnowledgeAssetStore | None = None) -> KnowledgeAsset: ...


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        cleaned = " ".join(data.split())
        if cleaned:
            self.parts.append(cleaned)


def _office_xml_text(path: Path, members: tuple[str, ...]) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            chunks: list[str] = []
            for member in members:
                if member not in archive.namelist():
                    continue
                root = ElementTree.fromstring(archive.read(member))
                chunks.extend(text.strip() for text in root.itertext() if text.strip())
            if not chunks:
                raise KRUnsupported(f"no readable text in {path.suffix} document")
            return "\n".join(chunks)
    except zipfile.BadZipFile as exc:
        raise KRUnsupported(f"invalid Office document: {path.name}") from exc


def _extract_text(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".markdown", ".rst", ".csv", ".json", ".xml"}:
        return path.read_text(encoding="utf-8", errors="replace"), "utf8-v1"
    if suffix in {".html", ".htm"}:
        parser = _HTMLText()
        parser.feed(path.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(parser.parts), "html-text-v1"
    if suffix == ".docx":
        return _office_xml_text(path, ("word/document.xml",)), "docx-xml-v1"
    if suffix == ".pptx":
        with zipfile.ZipFile(path) as archive:
            slide_names = sorted(
                (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                key=lambda name: int(re.search(r"slide(\d+)", name).group(1)),
            )
            chunks = []
            for name in slide_names:
                root = ElementTree.fromstring(archive.read(name))
                chunks.append(" ".join(text.strip() for text in root.itertext() if text.strip()))
        return "\n\n".join(chunks), "pptx-xml-v1"
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]
        except ImportError as exc:
            raise KRUnsupported("local PDF extraction requires pypdf; use MinerU Cloud for this document") from exc
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(f"<!-- page:{i + 1} -->\n{text}" for i, text in enumerate(pages)), "pypdf-v1"
    raise KRUnsupported(f"local extraction does not support {suffix or 'extensionless'} files")


class LocalExtractionBackend:
    """Deterministic local text/layout extraction with optional metadata enrichment."""

    def __init__(self, enricher: Callable[[str, str], dict[str, Any]] | None = None) -> None:
        self.enricher = enricher

    def extract(self, source: str | Path, *, store: KnowledgeAssetStore | None = None) -> KnowledgeAsset:
        path = Path(source)
        raw = path.read_bytes()
        source_hash = hashlib.sha256(raw).hexdigest()
        text, parser_version = _extract_text(path)
        markdown = text if text.endswith("\n") else text + "\n"
        content_list = [
            {"type": "heading", "level": len(match.group(1)), "text": match.group(2).strip(), "line": index}
            for index, line in enumerate(markdown.splitlines(), 1)
            if (match := re.match(r"^(#{1,6})\s+(.+)$", line))
        ]
        metadata = self.enricher(path.name, markdown) if self.enricher else {}
        asset = KnowledgeAsset(
            asset_id=stable_resource_id(path),
            source_path=str(path.resolve()),
            source_name=path.name,
            source_hash=source_hash,
            parser_name="local",
            parser_version=parser_version,
            markdown=markdown,
            content_list=content_list,
            metadata=metadata,
        )
        return store.put(asset) if store else asset
