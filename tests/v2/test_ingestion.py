from __future__ import annotations

from pathlib import Path

from knowledge_runtime.v2.artifacts import FilesystemArtifactStore
from knowledge_runtime.v2.canonical import InMemoryCanonicalStore
from knowledge_runtime.v2.config import RuntimeConfig
from knowledge_runtime.v2.index import InMemoryIndexBackend
from knowledge_runtime.v2.contracts import IndexPublishResult
from knowledge_runtime.v2.ingestion import IngestionService, stable_document_id
from knowledge_runtime.v2.providers import LocalProvider, ParserRouter


def make_service(tmp_path: Path, *, index=None) -> IngestionService:
    config = RuntimeConfig.test_private()
    local = LocalProvider(config)
    router = ParserRouter(config=config, local=local)
    canonical = InMemoryCanonicalStore()
    backend = index or InMemoryIndexBackend(canonical=canonical)
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    return IngestionService(
        router=router,
        quality=None,
        canonical=canonical,
        artifacts=artifacts,
        index=backend,
    )


def test_ingest_is_idempotent_for_same_source_hash(tmp_path: Path) -> None:
    source = tmp_path / "policy.md"
    source.write_text("# Scope\n\nReserve margin applies to annuity risk.\n", encoding="utf-8")
    service = make_service(tmp_path)

    first = service.ingest(source, provider="local")
    second = service.ingest(source, provider="local")

    assert second.revision_id == first.revision_id
    assert second.reused is True
    assert len(service.canonical.list_revisions(first.document_id)) == 1


def test_changed_source_publishes_new_revision(tmp_path: Path) -> None:
    source = tmp_path / "policy.md"
    source.write_text("# Scope\n\nold rule\n", encoding="utf-8")
    service = make_service(tmp_path)
    first = service.ingest(source, provider="local")
    source.write_text("# Scope\n\nnew rule\n", encoding="utf-8")

    second = service.ingest(source, provider="local")

    assert second.revision_id != first.revision_id
    assert service.canonical.current_revision(first.document_id) == second.revision_id
    assert len(service.canonical.list_revisions(first.document_id)) == 2


class FailingIndex:
    def publish_revision(self, revision, **kwargs):
        raise RuntimeError("index unavailable")

    def remove_revision(self, document_id, revision_id):
        pass


class FailedResultIndex:
    def publish_revision(self, revision, **kwargs):
        return IndexPublishResult(
            kwargs.get("index_version", "failed"),
            revision.document_id,
            revision.revision_id,
            0,
            "",
            state="FAILED",
        )

    def remove_revision(self, document_id, revision_id):
        pass


def test_index_failure_does_not_replace_current(tmp_path: Path) -> None:
    source = tmp_path / "policy.md"
    source.write_text("# Scope\n\nold rule\n", encoding="utf-8")
    service = make_service(tmp_path)
    first = service.ingest(source, provider="local")
    source.write_text("# Scope\n\nnew rule\n", encoding="utf-8")
    service.index = FailingIndex()

    result = service.ingest(source, provider="local")

    assert result.state == "FAILED"
    assert service.canonical.current_revision(first.document_id) == first.revision_id


def test_failed_index_result_does_not_replace_current(tmp_path: Path) -> None:
    source = tmp_path / "policy.md"
    source.write_text("# Scope\n\nold rule\n", encoding="utf-8")
    service = make_service(tmp_path)
    first = service.ingest(source, provider="local")
    source.write_text("# Scope\n\nnew rule\n", encoding="utf-8")
    service.index = FailedResultIndex()

    result = service.ingest(source, provider="local")

    assert result.state == "FAILED"
    assert service.canonical.current_revision(first.document_id) == first.revision_id


def test_stable_document_id_is_path_based(tmp_path: Path) -> None:
    source = tmp_path / "policy.md"
    assert stable_document_id(source) == stable_document_id(source)
