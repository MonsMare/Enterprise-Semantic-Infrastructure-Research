from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..assets import SQLiteKnowledgeAssetStore
from .artifacts import ArtifactStore
from .canonical import CanonicalStore
from .contracts import RevisionIndexInput
from .index import IndexBackend
from .providers import markdown_to_ir


@dataclass(frozen=True)
class MigrationReport:
    revisions_copied: int
    raw_artifact_missing: int = 0
    derived_artifacts_copied: int = 0


def replay_legacy_sqlite(
    path: str | Path,
    canonical: CanonicalStore,
    artifacts: ArtifactStore,
    index: IndexBackend,
) -> MigrationReport:
    legacy = SQLiteKnowledgeAssetStore(path)
    copied = 0
    raw_missing = 0
    derived_copied = 0
    try:
        for asset in legacy.list_assets():
            revision_rows = list(reversed(legacy.list_revisions(asset.asset_id)))
            current_revision_id = asset.revision_id
            for row in revision_rows:
                revision = legacy.get_revision(asset.asset_id, row["revision_id"])
                ir = markdown_to_ir(
                    revision.markdown,
                    document_id=revision.asset_id,
                    revision_id=revision.revision_id,
                    parser="legacy-" + revision.parser_name,
                    version=revision.parser_version,
                    metadata={
                        **revision.metadata,
                        "source_name": revision.source_name,
                        "source_path": revision.source_path,
                        "source_hash": revision.source_hash,
                        "legacy_asset_id": revision.asset_id,
                        "content_list": revision.content_list,
                    },
                    source_name=revision.source_name,
                    provider_name="legacy-replay",
                    egress_allowed=False,
                )
                source_path = Path(revision.source_path)
                if source_path.is_file():
                    raw_ref = artifacts.put_bytes(
                        document_id=ir.document_id,
                        revision_id=ir.revision_id,
                        name=source_path.name,
                        data=source_path.read_bytes(),
                        media_type="application/octet-stream",
                    )
                    ir = ir.__class__(
                        document_id=ir.document_id,
                        revision_id=ir.revision_id,
                        metadata=ir.metadata,
                        elements=ir.elements,
                        parse_report=ir.parse_report,
                        source_artifacts=(raw_ref,),
                    )
                else:
                    raw_missing += 1
                artifacts.put_bytes(
                    document_id=ir.document_id,
                    revision_id=ir.revision_id,
                    name="full.md",
                    data=revision.markdown.encode("utf-8"),
                    media_type="text/markdown",
                )
                canonical.put_revision(ir, source_hash=revision.source_hash)
                published = index.publish_revision(RevisionIndexInput.from_ir(ir), index_version=f"legacy:{ir.revision_id}")
                canonical.record_index_run(published.index_version, state="SUCCEEDED", indexed_count=published.indexed_count)
                copied += 1
            if current_revision_id:
                current = canonical.get_revision(asset.asset_id, current_revision_id)
                index_version = f"legacy:{current.revision_id}"
                canonical.begin_publication(asset.asset_id, current.revision_id, index_version=index_version)
                canonical.publish_current(asset.asset_id, current.revision_id, index_version=index_version)
    finally:
        legacy.close()
    return MigrationReport(copied, raw_missing, derived_copied)

