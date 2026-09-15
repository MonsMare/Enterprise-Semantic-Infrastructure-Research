from __future__ import annotations

from knowledge_runtime.assets import SQLiteKnowledgeAssetStore, KnowledgeAsset
from knowledge_runtime.v2.artifacts import FilesystemArtifactStore
from knowledge_runtime.v2.canonical import InMemoryCanonicalStore
from knowledge_runtime.v2.index import InMemoryIndexBackend
from knowledge_runtime.v2.migration import replay_legacy_sqlite


def test_legacy_replay_preserves_asset_revision_and_locator(tmp_path) -> None:
    legacy_path = tmp_path / "legacy.sqlite"
    legacy = SQLiteKnowledgeAssetStore(legacy_path)
    source = tmp_path / "policy.md"
    source.write_text("# Scope\n\nReserve margin is 12%.\n", encoding="utf-8")
    asset = KnowledgeAsset(
        asset_id="asset-1",
        source_path=str(source),
        source_name="policy.md",
        source_hash="source-hash",
        parser_name="local",
        parser_version="1",
        markdown=source.read_text(encoding="utf-8"),
        content_list=[],
        metadata={},
    )
    legacy.put(asset)
    legacy.close()

    canonical = InMemoryCanonicalStore()
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    index = InMemoryIndexBackend(canonical=canonical)
    report = replay_legacy_sqlite(legacy_path, canonical, artifacts, index)

    assert report.revisions_copied >= 1
    assert canonical.current_revision("asset-1")
    assert report.raw_artifact_missing == 0

