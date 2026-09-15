from __future__ import annotations

import pytest

from knowledge_runtime.v2.artifacts import FilesystemArtifactStore, artifact_key


def test_filesystem_artifacts_are_immutable_and_hash_verified(tmp_path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    ref = store.put_bytes(
        document_id="doc-1",
        revision_id="rev-1",
        name="original.pdf",
        data=b"pdf-bytes",
        media_type="application/pdf",
    )

    assert ref.object_key == artifact_key("doc-1", "rev-1", "original.pdf")
    assert store.get_bytes(ref) == b"pdf-bytes"
    assert store.head(ref).sha256 == ref.sha256

    with pytest.raises(ValueError, match="immutable"):
        store.put_bytes(
            document_id="doc-1",
            revision_id="rev-1",
            name="original.pdf",
            data=b"different",
            media_type="application/pdf",
        )


def test_artifact_names_are_path_safe(tmp_path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    ref = store.put_bytes(
        document_id="doc-1",
        revision_id="rev-1",
        name="../../full.md",
        data=b"markdown",
        media_type="text/markdown",
    )
    assert ref.object_key.endswith("/full.md")
    assert not (tmp_path / "full.md").exists()

