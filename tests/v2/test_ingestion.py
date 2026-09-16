from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from knowledge_runtime.v2.artifacts import FilesystemArtifactStore
from knowledge_runtime.v2.canonical import InMemoryCanonicalStore
from knowledge_runtime.v2.config import RuntimeConfig
from knowledge_runtime.v2.context import ContextRuntime
from knowledge_runtime.v2.index import InMemoryIndexBackend
from knowledge_runtime.v2.contracts import IndexPublishResult
from knowledge_runtime.v2.ingestion import IngestionService, stable_document_id
from knowledge_runtime.v2.providers import LocalProvider, ParserRouter, markdown_to_ir
from knowledge_runtime.v2.quality import QualityDecision


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


def test_ingest_persists_raw_and_document_ir_artifacts_with_a_terminal_job(tmp_path: Path) -> None:
    source = tmp_path / "policy.md"
    source.write_text("# Scope\n\nReserve margin applies to annuity risk.\n", encoding="utf-8")
    service = make_service(tmp_path)

    result = service.ingest(source, provider="local")
    ir = service.canonical.get_ir(result.document_id, result.revision_id)
    jobs = service.canonical.list_ingestion_jobs(result.document_id)

    assert result.state == "CURRENT_REVISION_PUBLISHED"
    assert {ref.kind for ref in ir.source_artifacts} == {"source", "document-ir"}
    assert {ref.object_key.rsplit("/", 1)[-1] for ref in ir.source_artifacts} == {"policy.md", "document-ir.json"}
    assert jobs[-1]["state"] == "SEMANTIC_ENRICHMENT_PENDING"
    assert jobs[-1]["history"][-2:] == ["CURRENT_REVISION_PUBLISHED", "SEMANTIC_ENRICHMENT_PENDING"]


class EscalatingQualityGate:
    def evaluate(self, report):
        return QualityDecision("ESCALATED", "manual_review_required")


def test_quality_escalation_records_a_terminal_job_without_publishing(tmp_path: Path) -> None:
    source = tmp_path / "policy.md"
    source.write_text("# Scope\n\nReserve margin applies to annuity risk.\n", encoding="utf-8")
    service = make_service(tmp_path)
    service.quality = EscalatingQualityGate()

    result = service.ingest(source, provider="local")
    jobs = service.canonical.list_ingestion_jobs(result.document_id)

    assert result.state == "ESCALATED"
    assert service.canonical.current_revision(result.document_id) is None
    assert jobs[-1]["state"] == "ESCALATED"
    assert jobs[-1]["history"][-1] == "ESCALATED"


class FailingIndex:
    def publish_revision(self, revision, **kwargs):
        raise RuntimeError("index unavailable")

    def remove_revision(self, document_id, revision_id):
        pass


class PublicationFailingCanonical(InMemoryCanonicalStore):
    fail_publication = False

    def publish_current(self, document_id, revision_id, *, index_version):
        if self.fail_publication:
            raise RuntimeError("canonical publication unavailable")
        return super().publish_current(document_id, revision_id, index_version=index_version)


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
    job = next(
        item
        for item in service.canonical.list_ingestion_jobs(first.document_id)
        if item["revision_id"] == result.revision_id
    )
    assert job["state"] == "FAILED"
    assert job["history"][-1] == "FAILED"


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


def test_failed_current_pointer_publish_keeps_prior_canonical_revision_searchable(tmp_path: Path) -> None:
    source = tmp_path / "policy.md"
    source.write_text("# Scope\n\nThe legacy reserve margin rule applies.\n", encoding="utf-8")
    config = RuntimeConfig.test_private()
    canonical = PublicationFailingCanonical()
    index = InMemoryIndexBackend(canonical=canonical)
    service = IngestionService(
        router=ParserRouter(config=config, local=LocalProvider(config)),
        quality=None,
        canonical=canonical,
        artifacts=FilesystemArtifactStore(tmp_path / "artifacts"),
        index=index,
    )
    first = service.ingest(source, provider="local")
    canonical.fail_publication = True
    source.write_text("# Scope\n\nThe replacement reserve margin rule applies.\n", encoding="utf-8")

    failed = service.ingest(source, provider="local")
    page = ContextRuntime(canonical=canonical, index=index).search_evidence("reserve margin")

    assert failed.state == "FAILED"
    assert canonical.current_revision(first.document_id) == first.revision_id
    assert {hit.ref.revision_id for hit in page.items} == {first.revision_id}


def test_stable_document_id_is_path_based(tmp_path: Path) -> None:
    source = tmp_path / "policy.md"
    assert stable_document_id(source) == stable_document_id(source)


class QualityControlledProvider:
    def __init__(self, name: str, *, low_quality: bool) -> None:
        self.name = name
        self.low_quality = low_quality
        self.calls = 0

    def parse(self, source, *, document_id: str, revision_id: str):
        self.calls += 1
        ir = markdown_to_ir(
            "# Reserves\n\nThe reserve margin is calculated from adverse deviation.\n",
            document_id=document_id,
            revision_id=revision_id,
            parser=self.name,
            version="test-v1",
            source_name=Path(source).name,
            provider_name=self.name,
        )
        if not self.low_quality:
            return ir
        return replace(
            ir,
            parse_report=replace(ir.parse_report, overall_grade="degraded", table_quality=0.1),
        )


def make_routed_service(tmp_path: Path, *, local, remote, allow_remote: bool) -> IngestionService:
    config = RuntimeConfig.test_private(allow_remote_parser=allow_remote)
    canonical = InMemoryCanonicalStore()
    return IngestionService(
        router=ParserRouter(config=config, local=local, remote=remote),
        quality=None,
        canonical=canonical,
        artifacts=FilesystemArtifactStore(tmp_path / "artifacts"),
        index=InMemoryIndexBackend(canonical=canonical),
    )


def test_retry_uses_next_explicitly_enabled_provider_and_persists_attempt_diagnostics(tmp_path: Path) -> None:
    source = tmp_path / "policy.pdf"
    source.write_bytes(b"%PDF-test")
    local = QualityControlledProvider("local", low_quality=True)
    remote = QualityControlledProvider("mineru-cloud", low_quality=False)
    service = make_routed_service(tmp_path, local=local, remote=remote, allow_remote=True)

    result = service.ingest(source)
    ir = service.canonical.get_ir(result.document_id, result.revision_id)

    assert result.state == "CURRENT_REVISION_PUBLISHED"
    assert [local.calls, remote.calls] == [1, 1]
    assert [(row["provider"], row["outcome"]) for row in ir.metadata["parse_attempts"]] == [
        ("local", "RETRY"),
        ("mineru-cloud", "ACCEPTED"),
    ]


def test_retry_without_an_eligible_fallback_escalates_with_diagnostics(tmp_path: Path) -> None:
    source = tmp_path / "policy.pdf"
    source.write_bytes(b"%PDF-test")
    local = QualityControlledProvider("local", low_quality=True)
    remote = QualityControlledProvider("mineru-cloud", low_quality=False)
    service = make_routed_service(tmp_path, local=local, remote=remote, allow_remote=False)

    result = service.ingest(source)
    job = service.canonical.list_ingestion_jobs(result.document_id)[0]

    assert result.state == "ESCALATED"
    assert [local.calls, remote.calls] == [1, 0]
    assert result.history[-1] == "ESCALATED"
    assert job["diagnostics"]["parse_attempts"][-1]["outcome"] == "RETRY"
