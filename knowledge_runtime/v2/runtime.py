from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .artifacts import ArtifactStore, FilesystemArtifactStore, S3ArtifactStore
from .canonical import CanonicalStore, PostgresCanonicalStore, SqliteCanonicalStore
from .config import RuntimeConfig
from .context import ContextRuntime
from .contracts import IndexRebuildRequest
from .index import InMemoryIndexBackend, IndexBackend, OpenSearchIndexBackend
from .ingestion import IngestionService
from .providers import LocalProvider, MinerUProvider, ParserRouter
from .semantic import (
    InMemoryProposalStore,
    PostgresProposalStore,
    ProposalStore,
    SemanticEnrichmentWorker,
    SqliteProposalStore,
)


@dataclass(frozen=True)
class RuntimeBundle:
    """A single coherent L1 runtime assembled from one configuration source."""

    canonical: CanonicalStore
    artifacts: ArtifactStore
    index: IndexBackend
    context: ContextRuntime
    ingestion: IngestionService
    proposals: ProposalStore
    semantic_worker: SemanticEnrichmentWorker

    def close(self) -> None:
        proposal_close = getattr(self.proposals, "close", None)
        if proposal_close is not None:
            proposal_close()
        canonical_close = getattr(self.canonical, "close", None)
        if canonical_close is not None:
            canonical_close()


def build_runtime(
    config: RuntimeConfig,
    *,
    canonical: CanonicalStore | None = None,
    artifacts: ArtifactStore | None = None,
    index: IndexBackend | None = None,
    proposals: ProposalStore | None = None,
) -> RuntimeBundle:
    """Build L1 once and make the in-memory derived index recoverable.

    Explicit service endpoints select their matching external adapters. Without
    those endpoints, SQLite and the filesystem keep the developer POC durable
    across CLI processes while keeping all content on the local host.
    """

    chosen_canonical = canonical or _canonical_store(config)
    chosen_artifacts = artifacts or _artifact_store(config)
    chosen_index = index or _index_store(config, chosen_canonical)
    chosen_proposals = proposals or _proposal_store(chosen_canonical)
    if isinstance(chosen_index, InMemoryIndexBackend):
        chosen_index.rebuild(IndexRebuildRequest(index_version="local-startup-rebuild"))
    local = LocalProvider(config)
    remote = MinerUProvider(config=config) if config.allow_remote_parser else None
    router = ParserRouter(config=config, local=local, remote=remote)
    context = ContextRuntime(canonical=chosen_canonical, index=chosen_index, overlay=chosen_proposals)
    ingestion = IngestionService(
        router=router,
        quality=None,
        canonical=chosen_canonical,
        artifacts=chosen_artifacts,
        index=chosen_index,
    )
    semantic_worker = SemanticEnrichmentWorker(proposals=chosen_proposals)
    return RuntimeBundle(
        canonical=chosen_canonical,
        artifacts=chosen_artifacts,
        index=chosen_index,
        context=context,
        ingestion=ingestion,
        proposals=chosen_proposals,
        semantic_worker=semantic_worker,
    )


def _canonical_store(config: RuntimeConfig) -> CanonicalStore:
    if config.database_url:
        return PostgresCanonicalStore(config.database_url)
    return SqliteCanonicalStore(config.local_state_path)


def _proposal_store(canonical: CanonicalStore) -> ProposalStore:
    if isinstance(canonical, PostgresCanonicalStore):
        return PostgresProposalStore(canonical.connection)
    if isinstance(canonical, SqliteCanonicalStore):
        return SqliteProposalStore(canonical.connection)
    return InMemoryProposalStore()


def _artifact_store(config: RuntimeConfig) -> ArtifactStore:
    if not config.artifact_endpoint:
        state_path = Path(config.local_state_path)
        return FilesystemArtifactStore(state_path.parent / "artifacts")
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("install the runtime extra to use the S3-compatible ArtifactStore") from exc
    client = boto3.client(
        "s3",
        endpoint_url=config.artifact_endpoint,
        aws_access_key_id=os.environ.get("KR_ARTIFACT_ACCESS_KEY", "minioadmin"),
        aws_secret_access_key=os.environ.get("KR_ARTIFACT_SECRET_KEY", "minioadmin"),
    )
    return S3ArtifactStore(client=client, bucket=config.artifact_bucket)


def _index_store(config: RuntimeConfig, canonical: CanonicalStore) -> IndexBackend:
    if not config.opensearch_url:
        return InMemoryIndexBackend(canonical=canonical)
    try:
        from opensearchpy import OpenSearch  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("install the runtime extra to use the OpenSearch IndexBackend") from exc
    client = OpenSearch(config.opensearch_url)
    return OpenSearchIndexBackend(
        client=client,
        index_name=f"{config.opensearch_index_prefix}-evidence",
        canonical=canonical,
    )
