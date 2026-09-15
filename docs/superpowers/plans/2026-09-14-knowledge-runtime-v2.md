# Knowledge Runtime v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前 SQLite/chunk 原型迁移为三层私有化 Knowledge Runtime：以 `DocumentIR` 和不可变 Evidence 为事实来源，以 PostgreSQL/S3 为规范存储，以可替换索引和有界 Context Runtime 为 Agent 提供可重放、可验证的知识。

**Architecture:** L1 Provider 将本地解析器和 MinerU 统一归一化为 `DocumentIR`，经 QualityGate 后写入 PostgreSQL CanonicalStore 和 S3-compatible ArtifactStore；L2 在 CanonicalStore 之上异步生成受 Evidence 支撑的语义提案，并通过 OpenMetadata/Neo4j 管理；L3 只暴露五个 Context Runtime 原语，使用 OpenSearch 的词法/向量混合候选，再回到 CanonicalStore 读取 Evidence，最后由 qwen3.8-max Agent 生成带有局部证据引用的回答。旧的 `list/find/search/read/stat` 通过适配层继续工作。

**Tech Stack:** Python 3.11+, `psycopg` PostgreSQL driver, `boto3` S3 client, `opensearch-py`, optional `neo4j` driver, HTTP OpenMetadata adapter, Docling/Unstructured/MinerU adapters, OpenAI-compatible Qwen Agent and embedding gateways, Docker Compose, pytest.

**Spec:** `docs/superpowers/specs/2026-09-14-knowledge-runtime-v2-design.md`

## Global Constraints

- The canonical representation of a parsed document is `DocumentIR`; Markdown is an export view.
- The canonical semantic unit is `DocumentElement`; ChunkSets are versioned derived views for particular retrieval or model configurations.
- `Evidence` is immutable, revision-aware, hashable, and must be the actual model input for sourced claims.
- An IndexBackend contains rebuildable derived data. It never becomes the source of truth.
- Agent-generated semantic metadata is a proposal until its lifecycle status authorizes use.
- Agent operations cannot create top-level schema, change primitive meanings, overwrite certified definitions, delete conflicting facts, or merge entities without a gate.
- A new document revision becomes current only after its canonical data is committed and its required index publication succeeds.
- A failed parse or index job never replaces the last published revision.
- API keys are read only from environment variables, never persisted, logged, or sent to another provider.
- The embedding gateway and Agent gateway are separate clients with separate keys, models, telemetry, and failure policies.
- Local private mode performs no external model or parser request unless explicitly enabled by configuration.
- Docker commands must use `docker compose -p kr-v2` and resource names prefixed with `kr-v2-`; no global prune, stop, remove, or network/volume operation is allowed.
- The Agent gateway uses `QWEN_LLM_API_KEY` with model `qwen3.8-max`; the embedding gateway uses `DASHSCOPE_API_KEY` with model `qwen3.7-text-embedding` only.
- The POC does not add authentication, tenant isolation, billing, management UI, or a mutable LLM-generated enterprise Ontology.

## File Map

Create the v2 implementation as a focused subpackage so the existing POC remains runnable during migration:

- `knowledge_runtime/v2/contracts.py`: immutable DocumentIR, Evidence, search, index, and proposal value objects.
- `knowledge_runtime/v2/config.py`: environment-backed runtime configuration and strict model/key policy.
- `knowledge_runtime/v2/providers.py`: `LocalProvider`, `MinerUProvider`, parser router, and provider normalization.
- `knowledge_runtime/v2/quality.py`: ParseReport thresholds and QualityGate decisions.
- `knowledge_runtime/v2/artifacts.py`: ArtifactStore protocol plus filesystem test store and S3/MinIO implementation.
- `knowledge_runtime/v2/canonical.py`: CanonicalStore protocol, in-memory contract store, and PostgreSQL implementation.
- `knowledge_runtime/v2/migrations/001_initial.sql`: PostgreSQL tables, indexes, constraints, and publication transaction helpers.
- `knowledge_runtime/v2/embedding.py`: DashScope-compatible embedding gateway and cache key policy.
- `knowledge_runtime/v2/index.py`: IndexBackend protocol, in-memory contract backend, and OpenSearch implementation.
- `knowledge_runtime/v2/ingestion.py`: idempotent ingestion state machine and revision publication.
- `knowledge_runtime/v2/semantic.py`: proposal lifecycle, Neo4j adapter, OpenMetadata adapter, and enrichment worker.
- `knowledge_runtime/v2/context.py`: five Context Runtime primitives, bounded budgets, and context-run telemetry.
- `knowledge_runtime/v2/agent.py`: strict Qwen Agent gateway, five-tool retrieval loop, and citation validator.
- `knowledge_runtime/v2/compat.py`: adapter from legacy `KnowledgeProvider`/`Locator` objects to v2 primitives.
- `knowledge_runtime/v2/cli.py`: v2 CLI commands; existing `knowledge_runtime/cli.py` remains compatible.
- `knowledge_runtime/v2/migration.py`: replay of existing SQLite assets into CanonicalStore and ArtifactStore.
- `deploy/kr-v2.compose.yml`, `deploy/kr-v2.env.example`, `deploy/README.md`: scoped private deployment.
- `scripts/kr-v2.ps1`: Windows-safe, project-scoped Docker operations.
- `benchmarks/v2/run_benchmark.py` and `benchmarks/v2/cases/*.jsonl`: evidence, Agent, scale, revision, and private-egress benchmarks.
- `tests/v2/`: unit and contract tests; Docker and external-service tests are explicitly opt-in.

Every task below ends with a focused test run and a separate commit. Later tasks consume only the interfaces named in earlier tasks.

---

### Task 1: Establish v2 contracts and runtime configuration

**Files:**
- Create: `knowledge_runtime/v2/__init__.py`
- Create: `knowledge_runtime/v2/contracts.py`
- Create: `knowledge_runtime/v2/config.py`
- Create: `tests/v2/test_contracts.py`
- Create: `tests/v2/test_config.py`
- Modify: `pyproject.toml` to add the `runtime` optional dependency group and `PyYAML` to development tools without making external services mandatory for the legacy POC.

**Interfaces:**
- Produces `DocumentIR`, `DocumentElement`, `ParseReport.empty()`, `ArtifactRef`, `EvidenceRef`, `Evidence`, `content_hash`, `EvidenceFilters`, `AssetFilters`, `EvidenceSearchRequest`, `AssetSearchRequest`, `SearchHitV2`, `EvidenceSearchPage`, `AssetSearchPage`, `DocumentRevision`, `RevisionIndexInput.from_ir()`, `IndexPublishResult`, `IndexRebuildRequest`, `IndexBuildReport`, `ProposalStatus`, and `SemanticProposal`.
- Produces `RuntimeConfig.from_env()` with `private_mode`, provider flags, storage endpoints, `embedding_model="qwen3.7-text-embedding"`, and `agent_model="qwen3.8-max"`.
- Produces `RuntimeConfig.test_private(**overrides)` for deterministic tests; it sets both remote flags to `False` and never reads secrets from the process environment.
- Later tasks import these types and must not duplicate their fields.

- [ ] **Step 1: Write failing contract tests**

```python
def test_evidence_identity_contains_revision_and_full_content_hash():
    ref = EvidenceRef("doc-1", "rev-1", "el-1", {"start": 1, "end": 2})
    evidence = Evidence.from_content(
        ref=ref,
        content="reserve margin is 12%",
        media_type="text/plain",
        representation="structured",
        source_label="policy.md",
    )
    assert evidence.source_revision == "rev-1"
    assert evidence.content_hash == content_hash("reserve margin is 12%")
    assert evidence.as_model_input()["evidence_id"] == evidence.evidence_id

def test_document_element_rejects_cross_revision_reference():
    element = DocumentElement(
        element_id="el-1", revision_id="rev-1", element_type="paragraph",
        text="text", section_path=(), page=None, bbox=None, payload={},
        provenance={}, confidence=1.0, content_hash=content_hash("text"),
    )
    with pytest.raises(ValueError, match="revision"):
        DocumentIR("doc-1", "rev-2", {}, (element,), ParseReport.empty("local", "1"), ())
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `python -m pytest tests/v2/test_contracts.py tests/v2/test_config.py -q`

Expected: collection or import failures because the v2 package and contracts do not yet exist.

- [ ] **Step 3: Implement the immutable contracts**

Implement frozen dataclasses with constructor validation. `Evidence.from_content()` hashes the complete canonical content; `Evidence.as_model_input()` preserves `evidence_id`, `EvidenceRef`, full `content_hash`, media type, representation, and `truncated`. `DocumentIR.__post_init__()` rejects elements with a different `revision_id`. `EvidenceSearchPage` and `AssetSearchPage` carry `next_cursor`, `snapshot_id`, `partial`, and diagnostics needed for replay.

- [ ] **Step 4: Implement strict environment configuration**

```python
@classmethod
def from_env(cls) -> "RuntimeConfig":
    return cls(
        private_mode=_bool_env("KR_PRIVATE_MODE", True),
        allow_remote_parser=_bool_env("KR_ALLOW_REMOTE_PARSER", False),
        allow_remote_embedding=_bool_env("KR_ALLOW_REMOTE_EMBEDDING", True),
        embedding_model="qwen3.7-text-embedding",
        agent_model="qwen3.8-max",
        agent_api_key=os.environ.get("QWEN_LLM_API_KEY", ""),
        embedding_api_key=os.environ.get("DASHSCOPE_API_KEY", ""),
        agent_base_url=os.environ.get("QWEN_LLM_BASE_URL", ""),
        embedding_base_url=os.environ.get("DASHSCOPE_BASE_URL", ""),
    )
```

Reject an explicitly enabled remote gateway when its key or endpoint is missing. Never read one gateway's key from the other gateway's environment variable. Keep the legacy `LLM_API_KEY` fallback outside v2 only.

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/v2/test_contracts.py tests/v2/test_config.py -q`

Expected: all v2 contract and key-separation tests pass. Commit with `git add knowledge_runtime/v2 tests/v2 pyproject.toml && git commit -m "feat: add knowledge runtime v2 contracts"`.

### Task 2: Normalize local and MinerU providers into DocumentIR

**Files:**
- Create: `knowledge_runtime/v2/providers.py`
- Create: `knowledge_runtime/v2/quality.py`
- Create: `tests/v2/test_providers.py`
- Create: `tests/v2/test_quality.py`
- Modify: `knowledge_runtime/mineru_backend.py` only where a typed result or egress policy hook is needed; preserve its existing transport tests.

**Interfaces:**
- Consumes `DocumentIR`, `RuntimeConfig`, and existing `LocalExtractionBackend`/`MinerUCloudBackend`.
- Produces `ParserProvider.parse(source, *, document_id, revision_id) -> DocumentIR`, `LocalProvider`, `MinerUProvider`, `ParserRouter`, `QualityDecision`, and `QualityGate.evaluate(report) -> QualityDecision`.
- Provider output contains no parser-specific object after normalization.

- [ ] **Step 1: Write failing provider and gate tests**

```python
def test_local_provider_emits_page_and_heading_provenance(tmp_path):
    source = tmp_path / "policy.md"
    source.write_text("# Scope\n\nThe model applies to annuity risk.\n", encoding="utf-8")
    ir = LocalProvider(config=RuntimeConfig.test_private()).parse(
        source, document_id="doc-1", revision_id="rev-1"
    )
    assert ir.elements[0].element_type == "heading"
    assert ir.elements[1].section_path == ("Scope",)
    assert ir.elements[1].revision_id == "rev-1"

def test_private_router_does_not_select_remote_provider(monkeypatch, tmp_path):
    config = RuntimeConfig.test_private(allow_remote_parser=False)
    router = ParserRouter(config=config, local=LocalProvider(config), remote=FailingProvider())
    assert router.choose(tmp_path / "input.pdf").name == "local"

def test_quality_gate_retries_low_table_quality():
    report = ParseReport(
        document_type="pdf", page_count=1, text_coverage=0.9,
        layout_quality=0.9, ocr_quality=0.9, table_quality=0.2,
        reading_order_quality=0.8, missing_regions=(), suspicious_regions=(),
        parser_name="local", parser_version="1", overall_grade="degraded", warnings=(),
    )
    assert QualityGate().evaluate(report).action == "RETRY"
```

- [ ] **Step 2: Run tests to confirm the missing provider boundary**

Run: `python -m pytest tests/v2/test_providers.py tests/v2/test_quality.py -q`

Expected: failures for missing provider classes and `QualityGate`.

- [ ] **Step 3: Implement the common provider boundary**

Convert the existing local asset result into `DocumentElement` records for headings, paragraphs, lists, tables, pictures, formulas, code, headers, and footers when the parser exposes those types. Preserve line/page/bbox information in `provenance` and keep the original structured payload in `payload`. The fallback text parser must still produce a valid `DocumentIR` when Docling is unavailable.

```python
class ParserProvider(Protocol):
    name: str
    def parse(self, source: Path, *, document_id: str, revision_id: str) -> DocumentIR:
        pass

def markdown_to_ir(markdown: str, *, document_id: str, revision_id: str, parser: str, version: str) -> DocumentIR:
    elements = tuple(_elements_from_markdown(markdown, revision_id=revision_id))
    return DocumentIR(
        document_id=document_id, revision_id=revision_id, metadata={},
        elements=elements,
        parse_report=ParseReport.empty(parser, version), source_artifacts=(),
    )
```

- [ ] **Step 4: Implement `MinerUProvider` and `ParserRouter`**

Wrap `MinerUCloudBackend` and normalize its Markdown/content-list/archive artifacts into the same `DocumentIR`. The router records provider name, parser version, endpoint policy, and egress decision in `ParseReport`. A remote retry is allowed only when `allow_remote_parser=True`; private mode never implicitly falls back from local to remote.

- [ ] **Step 5: Implement QualityGate and run tests**

Use explicit thresholds for text coverage, layout, OCR, table, and reading order. Return exactly `ACCEPTED`, `RETRY`, or `ESCALATED`. Persist missing and suspicious regions in the report. Run `python -m pytest tests/v2/test_providers.py tests/v2/test_quality.py -q` and commit `feat: normalize document providers into document ir`.

```python
def evaluate(self, report: ParseReport) -> QualityDecision:
    if report.overall_grade == "failed":
        return QualityDecision("ESCALATED", "parser_failed")
    if min(report.text_coverage, report.reading_order_quality, report.table_quality) < self.retry_threshold:
        return QualityDecision("RETRY", "quality_below_threshold")
    return QualityDecision("ACCEPTED", "quality_passed")
```

### Task 3: Add canonical PostgreSQL and S3-compatible artifact stores

**Files:**
- Create: `knowledge_runtime/v2/canonical.py`
- Create: `knowledge_runtime/v2/artifacts.py`
- Create: `knowledge_runtime/v2/migrations/001_initial.sql`
- Create: `tests/v2/test_canonical.py`
- Create: `tests/v2/test_artifacts.py`
- Modify: `pyproject.toml` runtime dependencies for `psycopg[binary]` and `boto3`.

**Interfaces:**
- Produces `CanonicalStore` with `put_revision`, `get_revision`, `get_element`, `current_revision`, `begin_publication`, `publish_current`, `record_ingestion_job`, `record_index_run`, `record_context_run`, `list_current_documents`, and `list_revisions(document_id)`.
- Produces `ArtifactStore` with `put_bytes`, `get_bytes`, `head`, and `delete` (delete requires an explicit retention operation).
- Produces `PostgresCanonicalStore`, `InMemoryCanonicalStore`, `S3ArtifactStore`, and `FilesystemArtifactStore`.

- [ ] **Step 1: Write failing store contract tests**

```python
def test_current_revision_is_not_published_before_index_success(store, sample_ir):
    store.put_revision(sample_ir, source_hash="src-1")
    store.begin_publication(sample_ir.document_id, sample_ir.revision_id, index_version="idx-1")
    assert store.current_revision(sample_ir.document_id) is None
    store.publish_current(sample_ir.document_id, sample_ir.revision_id, index_version="idx-1")
    assert store.current_revision(sample_ir.document_id) == sample_ir.revision_id

def test_failed_revision_keeps_previous_current(store, revision_one, revision_two):
    store.put_revision(revision_one, source_hash="src-1")
    store.publish_current("doc-1", "rev-1", index_version="idx-1")
    store.put_revision(revision_two, source_hash="src-2")
    store.record_index_run("idx-2", state="FAILED")
    assert store.current_revision("doc-1") == "rev-1"
```

- [ ] **Step 2: Run the store tests and verify they fail**

Run: `python -m pytest tests/v2/test_canonical.py tests/v2/test_artifacts.py -q`

Expected: failures because no v2 store implementations exist.

- [ ] **Step 3: Add the PostgreSQL schema and transaction boundary**

Create tables `documents`, `document_revisions`, `document_elements`, `parse_reports`, `artifact_refs`, `ingestion_jobs`, `index_runs`, `semantic_proposals`, and `context_runs`. Add unique constraints on `(document_id, revision_id)`, `content_hash`, and idempotency keys. `publish_current` must lock the document row, verify canonical state and a successful index run, then update `current_revision_id` in one transaction.

```python
class CanonicalStore(Protocol):
    def put_revision(self, ir: DocumentIR, *, source_hash: str) -> None:
        pass
    def list_revisions(self, document_id: str) -> list[DocumentRevision]:
        pass
    def begin_publication(self, document_id: str, revision_id: str, *, index_version: str) -> None:
        pass
    def publish_current(self, document_id: str, revision_id: str, *, index_version: str) -> None:
        pass

def publish_current(self, document_id: str, revision_id: str, *, index_version: str) -> None:
    with connection.transaction():
        connection.execute("SELECT 1 FROM documents WHERE document_id=%s FOR UPDATE", (document_id,))
        assert canonical_state(document_id, revision_id) == "COMMITTED"
        assert index_state(index_version) == "SUCCEEDED"
        connection.execute(
            "UPDATE documents SET current_revision_id=%s WHERE document_id=%s",
            (revision_id, document_id),
        )
```

- [ ] **Step 4: Implement stores and hash-preserving artifact writes**

`PostgresCanonicalStore` uses parameterized SQL only and never stores document bodies in logs. `S3ArtifactStore` writes immutable keys under `documents/{document_id}/revisions/{revision_id}/{artifact_name}`, verifies SHA-256 on read, and uses MinIO-compatible endpoint configuration. `FilesystemArtifactStore` is a deterministic test/local adapter and does not replace the PostgreSQL production path.

```python
def artifact_key(document_id: str, revision_id: str, name: str) -> str:
    return f"documents/{document_id}/revisions/{revision_id}/{Path(name).name}"

def put_bytes(self, *, document_id: str, revision_id: str, name: str, data: bytes, media_type: str) -> ArtifactRef:
    key = artifact_key(document_id, revision_id, name)
    digest = hashlib.sha256(data).hexdigest()
    self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=media_type)
    return ArtifactRef(key, key, media_type, digest, len(data), name, revision_id)
```

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/v2/test_canonical.py tests/v2/test_artifacts.py -q`

Expected: contract tests pass against `InMemoryCanonicalStore` and `FilesystemArtifactStore`; PostgreSQL/MinIO integration tests remain opt-in through `KR_V2_POSTGRES_DSN` and `KR_V2_S3_ENDPOINT`. Commit `feat: add canonical and artifact stores`.

### Task 4: Add the embedding gateway and rebuildable OpenSearch IndexBackend

**Files:**
- Create: `knowledge_runtime/v2/embedding.py`
- Create: `knowledge_runtime/v2/index.py`
- Create: `tests/v2/test_embedding.py`
- Create: `tests/v2/test_index.py`
- Modify: `pyproject.toml` runtime dependencies for `opensearch-py`.

**Interfaces:**
- Produces `EmbeddingGateway.embed_batch(texts) -> list[list[float]]` and `DashScopeEmbeddingGateway` using only `DASHSCOPE_API_KEY` and `qwen3.7-text-embedding`.
- Produces `IndexBackend.publish_revision`, `remove_revision`, `search_evidence`, `search_assets`, and `rebuild` exactly as defined in the v2 spec.
- Produces `InMemoryIndexBackend` for contract tests and `OpenSearchIndexBackend` for deployment.

- [ ] **Step 1: Write failing key-separation and index contract tests**

```python
def test_embedding_gateway_uses_dashscope_key_only(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "embedding-secret")
    monkeypatch.setenv("QWEN_LLM_API_KEY", "agent-secret")
    transport = RecordingTransport()
    DashScopeEmbeddingGateway.from_env(transport=transport).embed_batch(["claim"])
    assert transport.headers["Authorization"] == "Bearer embedding-secret"
    assert transport.body["model"] == "qwen3.7-text-embedding"

def test_search_returns_only_published_revision(index_backend, revision_input):
    index_backend.publish_revision(revision_input)
    page = index_backend.search_evidence(EvidenceSearchRequest("reserve"))
    assert page.items
    assert all(item.ref.revision_id == revision_input.revision_id for item in page.items)
```

- [ ] **Step 2: Run the tests and confirm missing implementations**

Run: `python -m pytest tests/v2/test_embedding.py tests/v2/test_index.py -q`

Expected: failures for the gateway and backend contracts.

- [ ] **Step 3: Implement the embedding client and cache policy**

Use an OpenAI-compatible `/embeddings` request. The request body always contains `model="qwen3.7-text-embedding"`; cache keys are `(content_hash, model, dimensions, endpoint)`. If remote embedding is disabled or the key is absent, return an explicit unavailable state instead of reading the Agent key or silently changing models.

- [ ] **Step 4: Implement OpenSearch mappings and bounded hybrid search**

Create one versioned index per schema/encoder configuration. Store Element text, headings, provenance, document/revision/status filters, and vectors when available. Search performs bounded lexical and vector candidate retrieval, weighted RRF, deterministic tie-breaking, and returns only `EvidenceRef`; `get_evidence` later reads the canonical store. `rebuild` enumerates published revisions from CanonicalStore and never materializes the full corpus in Python.

```python
def search_evidence(self, request: EvidenceSearchRequest) -> EvidenceSearchPage:
    lexical = self.client.search(index=self.index_name, body=self._lexical_query(request))
    semantic = self.client.search(index=self.index_name, body=self._vector_query(request)) if self.embedding else {"hits": {"hits": []}}
    hits = rrf_merge(lexical_hits(lexical), lexical_hits(semantic), limit=request.limit)
    return EvidenceSearchPage(items=tuple(self._to_search_hit(hit) for hit in hits), snapshot_id=self._snapshot(request))
```

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/v2/test_embedding.py tests/v2/test_index.py -q`

Expected: all gateway and fake-index tests pass. Commit `feat: add rebuildable hybrid index backend`.

### Task 5: Implement the idempotent ingestion and revision publication pipeline

**Files:**
- Create: `knowledge_runtime/v2/ingestion.py`
- Create: `tests/v2/test_ingestion.py`
- Modify: `knowledge_runtime/v2/canonical.py` if publication records need a typed result.

**Interfaces:**
- Produces `IngestionService.ingest(source, *, document_id=None, provider="auto") -> IngestionResult`, `stable_document_id`, and `new_revision_id`.
- Consumes `ParserRouter`, `QualityGate`, `CanonicalStore`, `ArtifactStore`, and `IndexBackend`.
- State sequence is `RECEIVED -> ARTIFACT_STORED -> PARSED -> QUALITY_CHECKED -> CANONICAL_COMMITTED -> INDEX_PUBLISHED -> CURRENT_REVISION_PUBLISHED -> SEMANTIC_ENRICHMENT_PENDING`.

- [ ] **Step 1: Write failing state and idempotency tests**

```python
def test_ingest_is_idempotent_for_same_source_hash(service, source):
    first = service.ingest(source, provider="local")
    second = service.ingest(source, provider="local")
    assert second.revision_id == first.revision_id
    assert len(service.canonical.list_revisions(first.document_id)) == 1

def test_index_failure_does_not_replace_current(service, source, changed_source, failing_index):
    first = service.ingest(source, provider="local")
    service.index = failing_index
    result = service.ingest(changed_source, provider="local")
    assert result.state == "FAILED"
    assert service.canonical.current_revision(first.document_id) == first.revision_id
```

- [ ] **Step 2: Run the ingestion tests to verify missing service code**

Run: `python -m pytest tests/v2/test_ingestion.py -q`

Expected: failures because `IngestionService` and `IngestionResult` do not exist.

- [ ] **Step 3: Implement source hashing, artifact capture, and parser routing**

Hash the original bytes before parsing. Record the raw object reference, provider, parser version, and an idempotency key. A repeated hash returns the existing revision without a second parse or embedding call.

- [ ] **Step 4: Implement publication ordering and failure recovery**

Persist the full `DocumentIR` and ParseReport before indexing. Publish an index version, verify its document count/checksum, then call `CanonicalStore.publish_current`. Any parser, quality, artifact, or index error records a retry/escalation/failure state and leaves the old current revision untouched.

```python
def ingest(self, source: Path, *, document_id: str | None = None, provider: str = "auto") -> IngestionResult:
    raw_hash = sha256(source.read_bytes()).hexdigest()
    existing = self.canonical.find_revision_by_source_hash(raw_hash)
    if existing is not None:
        return IngestionResult(existing.document_id, existing.revision_id, "CURRENT_REVISION_PUBLISHED", reused=True)
    ir = self.router.parse(source, document_id=document_id or stable_document_id(source), revision_id=new_revision_id())
    decision = self.quality.evaluate(ir.parse_report)
    if decision.action != "ACCEPTED":
        return self._record_quality_failure(ir, decision)
    self.canonical.put_revision(ir, source_hash=raw_hash)
    published = self.index.publish_revision(RevisionIndexInput.from_ir(ir))
    self.canonical.record_index_run(published.index_version, state="SUCCEEDED")
    self.canonical.publish_current(ir.document_id, ir.revision_id, index_version=published.index_version)
    return IngestionResult(ir.document_id, ir.revision_id, "CURRENT_REVISION_PUBLISHED", reused=False)
```

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/v2/test_ingestion.py -q`

Expected: idempotency, revision, and failure-preservation tests pass. Commit `feat: add revisioned ingestion pipeline`.

### Task 6: Add the governed Semantic Overlay

**Files:**
- Create: `knowledge_runtime/v2/semantic.py`
- Create: `tests/v2/test_semantic.py`
- Modify: `knowledge_runtime/v2/canonical.py` for proposal persistence methods.
- Modify: `pyproject.toml` runtime dependencies for the optional `neo4j` driver.

**Interfaces:**
- Produces `ProposalStore.create`, `transition`, `list_eligible`, `Neo4jSemanticAdapter`, `OpenMetadataAdapter`, and `SemanticEnrichmentWorker`.
- Uses fixed primitives `Document`, `Evidence`, `Entity`, `Claim`, `Term`, and `Metadata`.
- Proposal lifecycle is `PROPOSED -> AUTO_ACCEPTED -> VERIFIED -> CERTIFIED`, `PROPOSED -> CONFLICTED -> REVIEW`, or `PROPOSED -> REJECTED`.

- [ ] **Step 1: Write failing lifecycle and evidence-link tests**

```python
def test_claim_without_evidence_cannot_be_verified(proposals):
    proposal = proposals.create(kind="claim", payload={"predicate": "applies_to"}, evidence_refs=())
    with pytest.raises(ValueError, match="Evidence"):
        proposals.transition(proposal.id, "VERIFIED")

def test_conflicted_claim_is_excluded_from_default_context(proposals, evidence_ref):
    proposal = proposals.create(kind="claim", payload={}, evidence_refs=(evidence_ref,))
    proposals.transition(proposal.id, "CONFLICTED")
    assert proposals.list_eligible(statuses=("VERIFIED", "CERTIFIED")) == []
```

- [ ] **Step 2: Run tests and confirm missing overlay code**

Run: `python -m pytest tests/v2/test_semantic.py -q`

Expected: failures for proposal storage and adapters.

- [ ] **Step 3: Implement proposal persistence and transition validation**

Store payload, Evidence refs, extractor/model version, confidence, conflict details, and deterministic idempotency key in PostgreSQL. Reject direct writes that skip the lifecycle or lack Evidence refs. Keep raw Evidence searchable regardless of proposal status.

`SemanticEnrichmentWorker` uses the strict `QWEN_LLM_API_KEY`/`qwen3.8-max` gateway when enabled, sends only bounded DocumentIR Evidence, and stores model output as a proposal. It never writes a verified claim directly.

- [ ] **Step 4: Implement Neo4j and OpenMetadata adapters**

Neo4j writes only the fixed meta-schema and uses `RELATED_TO {predicate}` properties instead of unbounded relationship types. OpenMetadata receives collections, selected assets, terms, classifications, ownership, certification, and lineage through supported APIs; it never writes internal tables. Both adapters are optional and disabled in the core profile.

```python
def transition(self, proposal_id: str, target: ProposalStatus) -> SemanticProposal:
    proposal = self.get(proposal_id)
    allowed = {
        "PROPOSED": {"AUTO_ACCEPTED", "CONFLICTED", "REJECTED"},
        "AUTO_ACCEPTED": {"VERIFIED", "CONFLICTED"},
        "VERIFIED": {"CERTIFIED", "CONFLICTED"},
        "CONFLICTED": {"REVIEW", "REJECTED"},
        "REVIEW": {"VERIFIED", "REJECTED"},
    }
    if target not in allowed.get(proposal.status, set()):
        raise ValueError(f"invalid proposal transition: {proposal.status} -> {target}")
    if target in {"VERIFIED", "CERTIFIED"} and not proposal.evidence_refs:
        raise ValueError("verified semantic proposals require Evidence")
    return self._save(replace(proposal, status=target))
```

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/v2/test_semantic.py -q`

Expected: proposal lifecycle tests pass with fake adapters. Commit `feat: add governed semantic overlay`.

### Task 7: Implement the Context Runtime primitives and bounded budgets

**Files:**
- Create: `knowledge_runtime/v2/context.py`
- Create: `tests/v2/test_context.py`

**Interfaces:**
- Produces `ContextRuntime.search_evidence`, `search_assets`, `lookup_entity`, `get_claims`, and `get_evidence` with the exact signatures in the v2 spec, plus `QueryHints`, `RetrievalBudget`, and `ContextRunRecord`.
- Consumes `CanonicalStore`, `IndexBackend`, and optional Semantic Overlay.
- Produces `RetrievalBudget`, `ContextRunRecord`, replayable result objects, and no `compile_context` method until telemetry proves a stable call sequence.

- [ ] **Step 1: Write failing primitive and budget tests**

```python
def test_get_evidence_reads_canonical_content_after_search(runtime, question):
    page = runtime.search_evidence(question, limit=5)
    evidence = runtime.get_evidence(page.items[0].ref, max_bytes=1000)
    assert evidence.ref == page.items[0].ref
    assert evidence.content_hash

def test_budget_deduplicates_same_evidence(runtime, evidence_ref):
    budget = RetrievalBudget(max_evidence=1, max_bytes=100)
    assert budget.accept(evidence_ref, "same content")
    assert not budget.accept(evidence_ref, "same content")
```

- [ ] **Step 2: Run tests and verify missing Context Runtime**

Run: `python -m pytest tests/v2/test_context.py -q`

Expected: failures for the five primitives and budget class.

- [ ] **Step 3: Implement search routing and optional hints**

`search_evidence` calls IndexBackend and returns stable refs; `search_assets` returns document-level metadata; `lookup_entity` and `get_claims` query the optional overlay and return empty pages when the semantic profile is disabled. No result method returns canonical body text except `get_evidence`.

The route order is explicit: optional OpenMetadata term/domain hints, optional Neo4j entity/claim hints, bounded OpenSearch lexical/vector candidates, deterministic fusion/rerank, then `get_evidence` from CanonicalStore/ArtifactStore. The overlay may improve addressing but cannot replace the L1 source.

```python
def search_evidence(self, query: str, *, filters=None, limit=20, cursor=None) -> EvidenceSearchPage:
    hints = self.overlay.query_hints(query, filters=filters) if self.overlay else QueryHints.empty()
    request = EvidenceSearchRequest(query=query, filters=filters, hints=hints, limit=limit, cursor=cursor)
    page = self.index.search_evidence(request)
    return self._record_and_return("search_evidence", request, page)
```

- [ ] **Step 4: Implement bounded retrieval and telemetry**

Enforce maximum Agent rounds, rewrites, candidates, Evidence count, bytes/tokens, and no-improvement stop. Deduplicate by `evidence_id` and `content_hash`. Persist query, refs, budgets, timings, and outcome to `context_runs` with content logging disabled by default.

```python
class RetrievalBudget:
    def accept(self, evidence_id: str, content_hash: str, byte_count: int) -> bool:
        if evidence_id in self.evidence_ids or content_hash in self.content_hashes:
            return False
        if len(self.evidence_ids) >= self.max_evidence or self.bytes_used + byte_count > self.max_bytes:
            return False
        self.evidence_ids.add(evidence_id)
        self.content_hashes.add(content_hash)
        self.bytes_used += byte_count
        return True
```

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/v2/test_context.py -q`

Expected: primitive, provenance, stale revision, and budget tests pass. Commit `feat: add context runtime primitives`.

### Task 8: Add the Qwen Agent Retrieval Loop and legacy compatibility adapter

**Files:**
- Create: `knowledge_runtime/v2/agent.py`
- Create: `knowledge_runtime/v2/compat.py`
- Create: `tests/v2/test_agent.py`
- Create: `tests/v2/test_compat.py`
- Modify: `knowledge_runtime/llm_client.py` to expose a strict v2 client without changing legacy fallback behavior.

**Interfaces:**
- Produces `QwenAgentClient` using only `QWEN_LLM_API_KEY`, `QWEN_LLM_BASE_URL`, and `qwen3.8-max`.
- Produces `AgentRetrievalLoop.run(question, runtime, *, budgets) -> AgentResultV2`, `AgentResultV2`, and `CitationValidationError`.
- Produces `LegacyProviderAdapter` mapping `list -> search_assets`, `find -> search_assets`, `search -> search_evidence`, `read -> get_evidence`, and `stat -> revision/index status`.

- [ ] **Step 1: Write failing Agent and adapter tests**

```python
def test_agent_sends_only_read_evidence_to_answer_turn(scripted_model, runtime):
    scripted_model.tool_calls = [
        {"id": "1", "function": {"name": "search_evidence", "arguments": '{"query":"reserve margin"}'}},
        {"id": "2", "function": {"name": "get_evidence", "arguments": '{"ref": {"document_id":"doc-1","revision_id":"rev-1","element_id":"el-1"}}'}},
    ]
    result = AgentRetrievalLoop(scripted_model).run("What is the reserve margin?", runtime)
    assert result.evidence
    assert all(item.evidence_id in result.answer for item in result.evidence)
    assert scripted_model.answer_messages_contain_only_evidence()

def test_legacy_read_rejects_currently_stale_locator(runtime, legacy_locator):
    adapter = LegacyProviderAdapter(runtime)
    with pytest.raises(KRStaleLocator):
        adapter.read(legacy_locator)
```

- [ ] **Step 2: Run tests and confirm missing Agent/adapter code**

Run: `python -m pytest tests/v2/test_agent.py tests/v2/test_compat.py -q`

Expected: failures for the strict gateway, tool loop, and adapter.

- [ ] **Step 3: Implement strict Qwen gateway and five tool definitions**

Use the existing OpenAI-compatible transport shape but reject any model other than `qwen3.8-max` in v2. Tool results send hit metadata first; only selected `Evidence.as_model_input()` payloads enter the answer context. Text/table/image Evidence use compatible content parts without converting away provenance.

- [ ] **Step 4: Implement stop conditions and citation validation**

Stop after an answer with Evidence, after no-improvement, or at the configured maximum rounds/bytes. Reject or repair answers whose factual claims lack a nearby Evidence ID. Ambiguous short queries return a clarification result; the loop never broad-searches an unanchored pronoun.

```python
for round_no in range(1, budget.max_rounds + 1):
    turn = self.model.complete(messages, TOOLS if evidence_budget.has_room() else [])
    if not turn.tool_calls:
        answer = validate_citations(turn.content or "", evidence)
        return AgentResultV2(answer=answer, evidence=tuple(evidence), rounds=round_no)
    for call in turn.tool_calls:
        result = self._execute(call, runtime, evidence_budget)
        messages.append(tool_message(call, result))
    if no_improvement_since_last_round():
        break
return AgentResultV2.insufficient(evidence, reason="budget_or_no_improvement")
```

- [ ] **Step 5: Implement the compatibility adapter, run tests, and commit**

Preserve legacy Locator JSON, current-revision and stale-revision errors, pagination cursors, and `Evidence` serialization. Run `python -m pytest tests/v2/test_agent.py tests/v2/test_compat.py tests/test_agent_loop.py tests/test_provider_contract.py -q` and commit `feat: add v2 agent loop and legacy adapter`.

### Task 9: Add v2 CLI and scoped private Docker deployment

**Files:**
- Create: `knowledge_runtime/v2/cli.py`
- Create: `deploy/kr-v2.compose.yml`
- Create: `deploy/kr-v2.env.example`
- Create: `deploy/README.md`
- Create: `scripts/kr-v2.ps1`
- Create: `tests/v2/test_deployment_scope.py`
- Modify: `knowledge_runtime/cli.py` to dispatch a `v2` command while preserving all existing commands.

**Interfaces:**
- CLI commands: `kr v2 ingest`, `kr v2 search`, `kr v2 evidence`, `kr v2 ask`, `kr v2 status`, and `kr v2 rebuild-index`.
- Compose profiles: `core` (PostgreSQL, MinIO, OpenSearch, API, workers), `semantic` (Neo4j), and `governance` (OpenMetadata and explicitly declared dependencies).
- Every resource and named volume/network is prefixed with `kr-v2-`; all scripts use `docker compose -p kr-v2 -f deploy/kr-v2.compose.yml`.

- [ ] **Step 1: Write failing CLI and static-scope tests**

```python
def test_v2_cli_exposes_five_primitives(capsys):
    assert "v2" in build_parser().format_help()
    assert set(v2_command_names()) == {"ingest", "search", "evidence", "ask", "status", "rebuild-index"}

def test_compose_contains_only_kr_v2_owned_names():
    document = yaml.safe_load(Path("deploy/kr-v2.compose.yml").read_text())
    assert document["networks"]["kr-v2-net"]["name"] == "kr-v2-net"
    assert all(name.startswith("kr-v2-") for name in document["volumes"])
```

- [ ] **Step 2: Run tests to verify the new CLI/deployment surface is absent**

Run: `python -m pytest tests/v2/test_deployment_scope.py -q`

Expected: failures because the v2 command and Compose file do not yet exist.

- [ ] **Step 3: Implement v2 CLI dispatch and environment loading**

The CLI constructs `RuntimeConfig`, stores, providers, index, Context Runtime, and Agent loop from explicit configuration. It never prints keys or document bodies in status output. Existing commands continue using the old store until the migration acceptance gate is met.

```python
v2 = commands.add_parser("v2", help="Knowledge Runtime v2")
v2_sub = v2.add_subparsers(dest="v2_command", required=True)
for name in ("ingest", "search", "evidence", "ask", "status", "rebuild-index"):
    v2_sub.add_parser(name)

if args.command == "v2":
    return v2_main(args, RuntimeConfig.from_env())
```

- [ ] **Step 4: Implement the scoped Compose project and PowerShell wrapper**

Use health checks, named volumes, configurable host ports, and a preflight port check. The wrapper exposes only `up`, `down`, `logs`, `inventory`, `backup`, and `restore`; `down` runs only the exact `kr-v2` Compose project. Do not include `docker system prune`, global process termination, or unscoped network/volume removal. Local private mode disables external parser/embedding calls unless explicitly enabled in `.env`.

```powershell
$Compose = @("compose", "-p", "kr-v2", "-f", "deploy/kr-v2.compose.yml")
switch ($Action) {
  "up" { docker @Compose --profile $Profile up -d }
  "down" { docker @Compose down }
  "inventory" { docker @Compose ps }
  default { throw "unsupported KR v2 action" }
}
```

- [ ] **Step 5: Validate configuration without starting containers, then commit**

Run: `docker compose -p kr-v2 -f deploy/kr-v2.compose.yml --profile core config` and `python -m pytest tests/v2/test_deployment_scope.py -q`.

Expected: Compose renders successfully and the test sees only KR-prefixed resources. Commit `feat: add v2 cli and scoped private deployment`.

### Task 10: Add legacy SQLite migration, actuarial benchmarks, and scale gates

**Files:**
- Create: `knowledge_runtime/v2/migration.py`
- Create: `benchmarks/v2/run_benchmark.py`
- Create: `benchmarks/v2/cases/actuarial-v2.jsonl`
- Create: `benchmarks/v2/cases/fuzzy-v2.jsonl`
- Create: `benchmarks/v2/cases/scale-v2.jsonl`
- Create: `tests/v2/test_migration.py`
- Create: `tests/v2/test_benchmark_metrics.py`
- Modify: `benchmarks/actuarial/README.md` and `README.md` with v2 commands and metric definitions.

**Interfaces:**
- Produces `replay_legacy_sqlite(path, canonical, artifacts, index) -> MigrationReport`, `MigrationReport`, and `score_case(source_rank, evidence_rank, cited_claims, total_claims) -> BenchmarkRow`.
- Benchmark reports include `source_hit_at_k`, `evidence_hit_at_k`, `evidence_coverage`, `claim_local_citation_coverage`, `agent_loop_count`, `ingestion_seconds`, `search_p50_ms`, `search_p95_ms`, `index_build_seconds`, and `evidence_bytes`.
- Uses the existing public actuarial fixtures plus explicit cases for fuzzy, short, ambiguous, repeated-follow-up, stale-revision, reindex, and provider parity behavior.

- [ ] **Step 1: Write failing migration and metric tests**

```python
def test_legacy_replay_preserves_asset_revision_and_locator(tmp_path, legacy_store, v2_stores):
    report = replay_legacy_sqlite(legacy_store.path, *v2_stores)
    assert report.revisions_copied >= 1
    assert v2_stores.canonical.current_revision("asset-1")

def test_benchmark_separates_source_hit_from_evidence_coverage():
    row = score_case(source_rank=1, evidence_rank=8, cited_claims=1, total_claims=2)
    assert row.source_hit_at_k[1] == 1.0
    assert row.evidence_hit_at_k[3] == 0.0
    assert row.claim_local_citation_coverage == 0.5
```

- [ ] **Step 2: Run tests to verify migration and metrics are missing**

Run: `python -m pytest tests/v2/test_migration.py tests/v2/test_benchmark_metrics.py -q`

Expected: failures for the replay function and metric scorer.

- [ ] **Step 3: Implement SQLite replay and v2 benchmark runner**

Read every retained legacy revision, convert old chunks to paragraph `DocumentElement`s with line provenance, copy raw/Markdown/derived artifacts, and re-embed only the new index version. Benchmark every answerable case through search/read and, when the Agent key is present, through the real qwen3.8-max loop. Never use an oracle read to claim Agent success.

```python
def replay_legacy_sqlite(path: Path, canonical: CanonicalStore, artifacts: ArtifactStore, index: IndexBackend) -> MigrationReport:
    legacy = SQLiteKnowledgeAssetStore(path)
    copied = 0
    for asset in legacy.list_assets():
        for revision in legacy.list_revisions(asset.asset_id):
            ir = legacy_revision_to_document_ir(legacy.get_revision(asset.asset_id, revision["revision_id"]))
            canonical.put_revision(ir, source_hash=revision["source_hash"])
            artifacts.put_bytes(document_id=ir.document_id, revision_id=ir.revision_id, name="full.md", data=ir_to_markdown(ir).encode(), media_type="text/markdown")
            index.publish_revision(RevisionIndexInput.from_ir(ir))
            copied += 1
    return MigrationReport(revisions_copied=copied)
```

- [ ] **Step 4: Add scale and edge-case gates**

Measure initial versus incremental ingestion, index rebuild, search p50/p95, candidate count, Evidence bytes, and Agent rounds at multiple corpus sizes. Include deliberately vague Chinese and English prompts, pronoun-only follow-ups, missing identifiers, wrong-language terminology, and conflicting revisions. Record whether the Agent asks for clarification, rewrites once or twice, stops after sufficient Evidence, or exhausts its budget.

```python
metrics = {
    "source_hit_at_k": hit_rate(source_ranks, k_values=(1, 3, 5, 10)),
    "evidence_hit_at_k": hit_rate(evidence_ranks, k_values=(1, 3, 5, 10)),
    "evidence_coverage": mean(evidence_coverages),
    "claim_local_citation_coverage": mean(citation_coverages),
    "search_p50_ms": percentile(search_latencies, 50),
    "search_p95_ms": percentile(search_latencies, 95),
}
```

- [ ] **Step 5: Run the complete offline suite and commit**

Run: `python -m pytest -q` and `python benchmarks/v2/run_benchmark.py --cases benchmarks/v2/cases/actuarial-v2.jsonl --offline --output artifacts/v2-actuarial.json`.

Expected: all existing tests remain green and the v2 source/Evidence baseline is no worse than the recorded pre-migration baseline unless the report includes a measured, explained trade-off. The benchmark report contains separate source, Evidence, citation, latency, and loop metrics. Commit `test: add v2 migration and benchmark gates`.

### Task 11: Execute migration review and final verification

**Files:**
- Modify: `README.md` with the final v2 quickstart and compatibility note.
- Modify: `docs/reports/2026-09-14-private-deployment-reference-path.md` to point at the implemented Compose profile and operational commands.
- Create: `docs/reports/2026-09-14-knowledge-runtime-v2-baseline.md`.

**Interfaces:**
- The final report is evidence-based and records exact dataset counts, parser/index versions, latency percentiles, source/Evidence/citation metrics, Agent loop counts, and known limitations.

- [ ] **Step 1: Verify the code and repository state**

Run: `git diff --check`, `python -m pytest -q`, and `python -m compileall knowledge_runtime`.

Expected: no whitespace errors, all tests pass, and Python compilation succeeds.

- [ ] **Step 2: Verify the core Docker profile without touching unrelated resources**

Run: `docker compose -p kr-v2 -f deploy/kr-v2.compose.yml --profile core config`, then `.\scripts\kr-v2.ps1 up -Profile core`, `.\scripts\kr-v2.ps1 inventory`, and `.\scripts\kr-v2.ps1 down`. Inspect only resources selected by `docker compose -p kr-v2`; never run a global Docker cleanup command.

- [ ] **Step 3: Run the actuarial and edge-case benchmark with configured services**

Run the offline benchmark first. If the user explicitly enables external embedding/Agent calls, rerun with `DASHSCOPE_API_KEY` and `QWEN_LLM_API_KEY` loaded from the environment and redact request bodies from artifacts.

- [ ] **Step 4: Review the final report against the spec**

Confirm that every sourced answer resolves a canonical Evidence ref, every index can be rebuilt from CanonicalStore, failed revisions do not become current, local private mode has no egress, and legacy API behavior is preserved through the adapter.

- [ ] **Step 5: Commit the verified migration report**

Run: `git add README.md docs/reports/2026-09-14-private-deployment-reference-path.md docs/reports/2026-09-14-knowledge-runtime-v2-baseline.md && git commit -m "docs: record knowledge runtime v2 baseline"`.

The migration is accepted only when the final report contains the measurements above and the complete test suite passes without changing resources outside the `kr-v2` Compose project.
