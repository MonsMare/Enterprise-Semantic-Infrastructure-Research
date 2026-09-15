from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Any

from .contracts import DocumentIR


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    chunk_set_version: str
    document_id: str
    revision_id: str
    element_ids: tuple[str, ...]
    text: str
    section_path: tuple[str, ...]
    page: int | None
    bbox: tuple[float, float, float, float] | None
    parent_chunk_id: str | None = None
    previous_chunk_id: str | None = None
    next_chunk_id: str | None = None
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.chunk_id or not self.chunk_set_version or not self.element_ids:
            raise ValueError("chunk identity and source element ids are required")
        if not self.text.strip():
            raise ValueError("chunk text must not be empty")
        object.__setattr__(self, "element_ids", tuple(self.element_ids))
        object.__setattr__(self, "section_path", tuple(self.section_path))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))


class ChunkSetBuilder:
    def __init__(self, *, version: str = "hybrid-v1", max_chars: int = 2_000) -> None:
        if not version or max_chars <= 0:
            raise ValueError("chunk set version and positive max_chars are required")
        self.version = version
        self.max_chars = max_chars

    def build(self, ir: DocumentIR) -> tuple[Chunk, ...]:
        chunks: list[Chunk] = []
        for element in ir.elements:
            text = element.text
            parts = [text[index : index + self.max_chars] for index in range(0, len(text), self.max_chars)] or [text]
            for part_index, part in enumerate(parts):
                identity = "\x00".join(
                    (ir.document_id, ir.revision_id, self.version, element.element_id, str(part_index), part)
                )
                chunk_id = "chunk-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
                chunks.append(
                    Chunk(
                        chunk_id=chunk_id,
                        chunk_set_version=self.version,
                        document_id=ir.document_id,
                        revision_id=ir.revision_id,
                        element_ids=(element.element_id,),
                        text=part,
                        section_path=element.section_path,
                        page=element.page,
                        bbox=element.bbox,
                        parent_chunk_id=None if len(parts) == 1 else element.element_id,
                        metadata={
                            "source_element_id": element.element_id,
                            "part_index": part_index,
                            "part_count": len(parts),
                        },
                    )
                )
        for index, chunk in enumerate(chunks):
            chunks[index] = replace(
                chunk,
                previous_chunk_id=chunks[index - 1].chunk_id if index else None,
                next_chunk_id=chunks[index + 1].chunk_id if index + 1 < len(chunks) else None,
            )
        return tuple(chunks)

