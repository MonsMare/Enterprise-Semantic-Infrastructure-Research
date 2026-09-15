# Knowledge Runtime v2 Design Specification

**Status:** Draft for review

**Date:** 2026-09-14

## Goal

重构 Knowledge Runtime，使它从“文档 chunk 检索器”变成三层架构的私有化 Knowledge Runtime：L1 保真地保存文档结构和 Evidence，L2 用弱约束 Semantic Overlay 提供可定位、可关联、可治理的语义，L3 根据 Agent 的任务把可靠知识编译成可控上下文。重构保留旧工具的兼容适配层，但内部数据模型、解析管道、语义层和 Agent 接口全部迁移到 v2 契约。

## Scope and non-goals

本阶段以核心功能 POC 和可验证的私有化基线为目标。鉴权、租户隔离、计费、管理 UI、跨组织协作和自动生成企业级 Ontology 不属于 v2 POC 的交付范围；它们只能建立在本规格定义的稳定身份、revision、Evidence 和审计事件之上。核心 POC 必须先证明三件事：资料可以被可靠解析并重放，查询可以返回精确且可验证的 Evidence，Agent 可以只使用这些 Evidence 完成回答。

## Architecture

```text
L1 Evidence Runtime
  Provider -> DocumentIR -> QualityGate -> Canonical Store / Artifact Store
                                      -> Search Index

L2 Semantic Overlay
  Document/Evidence -> Entity/Claim/BusinessTerm proposals
                    -> OpenMetadata + Neo4j fixed meta-schema

L3 Context Runtime
  search_evidence / search_assets / lookup_entity / get_claims / get_evidence
  -> bounded retrieval planning -> Evidence-only context -> Agent answer
```

PostgreSQL-compatible storage is the canonical metadata and Element store. An S3-compatible store contains raw files and immutable parse artifacts. OpenSearch is the first production-oriented IndexBackend. Neo4j is the knowledge graph plane, and OpenMetadata is the governance/metadata plane. L2 enrichment is asynchronous and must never prevent L1 Evidence retrieval from working.

## Tech Stack

- Python 3.11+ and the existing package entry points.
- PostgreSQL-compatible CanonicalStore with JSONB metadata and transactional revision publication.
- S3-compatible ArtifactStore; MinIO is the local private deployment default.
- Docling as the primary local Document IR parser; Unstructured high-resolution/VLM as fallback; MinerU remains an adapter option.
- A provider-neutral local model gateway may be injected into the local parser/enrichment path. The POC may use the configured cloud Qwen gateway explicitly; the long-term private deployment can replace it with a local OpenAI-compatible endpoint without changing Provider or Evidence contracts.
- OpenSearch for lexical, vector, hybrid, filtering, and ranking indexes.
- Neo4j for the fixed Semantic Overlay graph meta-schema.
- OpenMetadata for domains, glossary terms, classifications, ownership, certification, and lineage.
- `DASHSCOPE_API_KEY` exclusively for `qwen3.7-text-embedding` when a remote embedding service is enabled.
- `QWEN_LLM_API_KEY` exclusively for the Agent model `qwen3.8-max`.
- Remote Agent, parser, and embedding calls are disabled by default in private mode and require explicit configuration flags. The POC may explicitly enable the cloud Agent gateway for the supplied Qwen resource.
- Docker Compose profiles with a `kr-v2` project name for local services. Docker operations are restricted to KR-prefixed containers, networks, volumes, and ports.

## Global Invariants

1. The canonical representation of a parsed document is `DocumentIR`; Markdown is an export view.
2. The canonical semantic unit is `DocumentElement`; ChunkSets are versioned derived views for particular retrieval or model configurations.
3. `Evidence` is immutable, revision-aware, hashable, and must be the actual model input for sourced claims.
4. An IndexBackend contains rebuildable derived data. It never becomes the source of truth.
5. Agent-generated semantic metadata is a proposal until its lifecycle status authorizes use.
6. Agent operations cannot create top-level schema, change primitive meanings, overwrite certified definitions, delete conflicting facts, or merge entities without a gate.
7. A new document revision becomes current only after its canonical data is committed and its required index publication succeeds.
8. A failed parse or index job never replaces the last published revision.
9. API keys are read only from environment variables, never persisted, logged, or sent to another provider.
10. The embedding gateway and Agent gateway are separate clients with separate keys, models, telemetry, and failure policies.
11. Local private mode performs no external model or parser request unless explicitly enabled by configuration.
12. Docker commands must use `docker compose -p kr-v2` and resource names prefixed with `kr-v2-`; no global prune, stop, remove, or network/volume operation is allowed.

## Layer 1: Evidence Runtime

### DocumentIR

`DocumentIR` is the parser-neutral intermediate representation:

```python
@dataclass(frozen=True)
class DocumentIR:
    document_id: str
    revision_id: str
    metadata: Mapping[str, Any]
    elements: tuple[DocumentElement, ...]
    parse_report: ParseReport
    source_artifacts: tuple[ArtifactRef, ...]
```

`DocumentElement` contains:

```python
@dataclass(frozen=True)
class DocumentElement:
    element_id: str
    revision_id: str
    element_type: Literal[
        "heading", "paragraph", "list", "table", "picture",
        "formula", "code", "header", "footer", "unknown"
    ]
    text: str
    section_path: tuple[str, ...]
    page: int | None
    bbox: tuple[float, float, float, float] | None
    payload: Mapping[str, Any]
    provenance: Mapping[str, Any]
    confidence: float | None
    content_hash: str
```

Table structure, merged cells, image references, formulas, and reading order remain in `payload` and `provenance`; they are not flattened into plain text during canonical storage.

ChunkSets are derived after canonical persistence. A ChunkSet records its version, source Element IDs, section/page provenance, and bounded parent/neighbor relations. It can use Docling HybridChunker when available and a deterministic Element-boundary fallback otherwise; deleting or rebuilding a ChunkSet never changes `DocumentIR`.

### ParserRouter and QualityGate

The router selects a parser by MIME type, source hints, and previous failures. Docling is the primary local parser. Unstructured high-resolution/VLM and MinerU are fallback adapters. Every parser returns `DocumentIR` plus `ParseReport`; no parser-specific object is exposed to L2 or L3.

`ParseReport` contains document/page count, text coverage, layout/ OCR/table/reading-order quality, missing and suspicious regions, parser identity/version, and an overall grade. The QualityGate has three outcomes:

```text
ACCEPTED  -> persist and index
RETRY     -> run the next configured parser or adjusted settings
ESCALATED -> persist diagnostic artifacts and require review
```

The provider boundary has two first-class implementations:

- `LocalProvider` runs Docling and Unstructured inside the private deployment. It is the default for private mode and must not call an external parser or model.
- `MinerUProvider` calls the configured MinerU service only when remote processing is explicitly enabled. It is the high-throughput/high-fidelity option for documents whose local parse quality is insufficient. Its output is normalized into the same `DocumentIR`, so provider choice cannot change the L2/L3 contract.

Provider selection, endpoint, parser version, and egress decision are recorded in `ParseReport` and the ingestion audit event. A failed remote request can fall back to the local provider only when the caller's policy allows it; the opposite direction is never implicit in private mode.

### Evidence model

```python
@dataclass(frozen=True)
class EvidenceRef:
    document_id: str
    revision_id: str
    element_id: str
    selector: Mapping[str, Any] | None

@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    ref: EvidenceRef
    content: str | bytes
    content_hash: str
    source_label: str
    media_type: str
    representation: str
    resolved_provenance: Mapping[str, Any]
    truncated: bool
```

`get_evidence` resolves the reference against the canonical revision, calculates or verifies the content hash, and returns the exact selected Element/range. Search results never substitute for Evidence.

`content_hash` always identifies the complete canonical selected content. A bounded `max_bytes` or representation conversion may return a truncated/view payload, but it must preserve the full hash, selector, and `truncated=true` marker so the Agent cannot mistake a preview for the complete source.

## Canonical and Artifact Storage

### PostgreSQL-compatible CanonicalStore

The first schema consists of:

- `documents`: stable identity, source URI/name, current revision pointer, lifecycle state, metadata.
- `document_revisions`: immutable source hash, parser identity/version, DocumentIR manifest, publication state, timestamps.
- `document_elements`: Element identity, type, text, hierarchy, page/bbox, payload JSONB, provenance JSONB, content hash.
- `parse_reports`: quality scores and missing/suspicious region diagnostics.
- `artifact_refs`: object-store keys, media types, hashes, sizes, and revision association.
- `ingestion_jobs`: idempotency key, state, attempt count, error details, checkpoints.
- `index_runs`: backend, encoder/chunk-set version, revision set, state, and build diagnostics.
- `semantic_proposals`: proposal payload, evidence references, extractor version, status, and conflict details.
- `context_runs`: query, selected references, evidence IDs, budgets, timings, and outcome; prompt/content logging is disabled by default and must be explicitly redacted/configured.

All write paths use `content_hash`, `revision_id`, and idempotency keys. `current_revision_id` is updated only by the publish transaction after canonical and required index states are ready.

### S3-compatible ArtifactStore

The object store contains immutable raw and derived artifacts. Object keys include document and revision IDs. The database stores references and hashes, never an opaque dependency on a local filesystem path. Raw files, DocumentIR snapshots, page images, table assets, parser diagnostics, and export views can be independently retained or expired according to policy.

## IndexBackend

The backend interface is independent of storage vendor:

```python
class IndexBackend(Protocol):
    def publish_revision(self, revision: RevisionIndexInput) -> IndexPublishResult: ...
    def remove_revision(self, document_id: str, revision_id: str) -> None: ...
    def search_evidence(self, request: EvidenceSearchRequest) -> EvidenceSearchPage: ...
    def search_assets(self, request: AssetSearchRequest) -> AssetSearchPage: ...
    def rebuild(self, request: IndexRebuildRequest) -> IndexBuildReport: ...
```

The OpenSearch implementation stores Element text, section fields, provenance fields, dense vectors from `qwen3.7-text-embedding`, and filterable revision/domain/status fields. It performs lexical and vector retrieval in bounded candidate sets, combines rankings, and returns only stable `EvidenceRef` values. The CanonicalStore remains authoritative when Evidence is read.

Embedding requests use a separate `EmbeddingGateway`:

```python
class EmbeddingGateway(Protocol):
    model: str  # qwen3.7-text-embedding
    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]: ...
```

The gateway reads `DASHSCOPE_API_KEY`; it never reads `QWEN_LLM_API_KEY`. Embedding cache keys include content hash, model, dimensions, and gateway configuration. Batch requests and cache hits are required for ingestion scalability.

Embedding is an optional index feature. If `DASHSCOPE_API_KEY` is absent or remote embedding is disabled, ingestion and lexical search remain available and the index records that its vector channel is unavailable. The runtime must not silently substitute the Agent key or a different model.

## Layer 2: Semantic Overlay

### Fixed primitives

The Semantic Overlay uses `Document`, `Evidence`, `Entity`, `Claim`, `BusinessTerm`, and `Metadata` as stable primitives. It does not create a company-wide Ontology automatically.

### OpenMetadata adapter

OpenMetadata receives document collections and selected important assets, domains, Business Terms, classifications, owners/reviewers, certifications, and lineage. Fine-grained Elements and every chunk remain in CanonicalStore and the index backend. The adapter uses OpenMetadata APIs and its supported ingestion mechanisms; it never writes directly to OpenMetadata internal tables.

### Neo4j adapter

Neo4j uses a fixed graph meta-schema:

```text
(:Document)
(:Evidence)
(:Entity {canonical_name, entity_class, aliases})
(:Term)
(:Claim {predicate, value, confidence, validity, status})

(:Evidence)-[:PART_OF]->(:Document)
(:Evidence)-[:MENTIONS]->(:Entity)
(:Claim)-[:ABOUT]->(:Entity)
(:Claim)-[:SUPPORTED_BY]->(:Evidence)
(:Entity)-[:RELATED_TO {predicate}]->(:Entity)
(:Entity)-[:DESCRIBED_BY]->(:Term)
```

Relationship semantics initially live in properties such as `predicate`, avoiding unbounded relationship-type growth. A dedicated relationship type can be promoted only through an explicit schema decision.

### Proposal lifecycle

Agent or enrichment workers may emit proposals for terms, aliases, entities, claims, relations, metadata, and merge candidates. Every proposal includes source Evidence refs, extractor/model version, confidence, and a deterministic idempotency key.

```text
PROPOSED -> AUTO_ACCEPTED -> VERIFIED -> CERTIFIED
PROPOSED -> CONFLICTED -> REVIEW
PROPOSED -> REJECTED
```

Only `VERIFIED` and `CERTIFIED` claims are eligible for default high-trust Context compilation. Raw Evidence remains queryable even when no semantic proposal exists.

## Layer 3: Context Runtime

### Public v2 primitives

```python
def search_evidence(
    query: str,
    *,
    filters: EvidenceFilters | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> EvidenceSearchPage: ...

def search_assets(
    query: str,
    *,
    filters: AssetFilters | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> AssetSearchPage: ...

def lookup_entity(
    entity: str,
    *,
    domain: str | None = None,
    limit: int = 20,
) -> EntitySearchPage: ...

def get_claims(
    entity_id: str,
    *,
    predicate: str | None = None,
    statuses: tuple[str, ...] = ("VERIFIED", "CERTIFIED"),
) -> ClaimPage: ...

def get_evidence(
    ref: EvidenceRef,
    *,
    max_bytes: int = 20_000,
    representation: str = "structured",
) -> Evidence: ...
```

All result objects include replayable references, revision identity, source labels, score/rank diagnostics, and provenance needed by the next call. Search returns hints; `get_evidence` returns model input.

### Bounded retrieval planning

The Context Runtime may route a request across OpenSearch, OpenMetadata, and Neo4j, but it uses explicit budgets:

- a configurable maximum number of Agent tool rounds;
- a configurable maximum number of query rewrites;
- per-query candidate and Evidence limits;
- a no-improvement stop condition;
- an Evidence byte/token budget;
- deduplication by `evidence_id` and `content_hash`.

`compile_context` is intentionally deferred until telemetry shows repeatable search/lookup/claims/evidence sequences. When introduced, it will be a pure compilation layer over the five primitives and will not become a second source of truth.

### Agent gateway

The Agent client reads `QWEN_LLM_API_KEY` and calls `qwen3.8-max`. It does not read `DASHSCOPE_API_KEY`, use the embedding model for chat, or silently fall back between keys. Text, table, image, and other media Evidence are passed using the model's compatible content parts when the selected representation requires them. Tool messages contain SearchHit metadata first; only selected `Evidence.as_model_input()` payloads are appended as answer evidence. The answer validator checks citation locality and unsupported claims before returning success.

The Agent gateway exposes exactly the five Context Runtime primitives. A `ConversationState` and `QueryPlanner` preserve document, entity, standard, and unresolved-subquestion anchors across turns, cap query rewrites, and return clarification when a short follow-up has no stable anchor. In private mode the gateway refuses to construct a remote client unless its explicit Agent egress flag is enabled.

## Compatibility Adapter

The current public surface remains available during migration:

```text
list  -> search_assets / catalog view
find  -> search_assets
search -> search_evidence
read  -> get_evidence
stat  -> document/revision/index status view
```

The adapter translates old `Locator` values into `EvidenceRef` and preserves stale-revision errors. New code and benchmarks use v2 primitives directly. The adapter is removed only after all existing benchmark suites and migration replay tests pass on the new store.

## Ingestion and Query State Machines

### Ingestion

```text
RECEIVED
  -> ARTIFACT_STORED
  -> PARSED
  -> QUALITY_CHECKED
  -> CANONICAL_COMMITTED
  -> INDEX_PUBLISHED
  -> CURRENT_REVISION_PUBLISHED
  -> SEMANTIC_ENRICHMENT_PENDING/COMPLETE
```

Failures route to `RETRY`, `ESCALATED`, or `FAILED` without changing the previous current revision. Semantic enrichment may lag publication; it cannot hide or alter Evidence.

### Query

```text
Agent task
  -> Context Runtime route
  -> OpenMetadata term/domain hints (optional)
  -> Neo4j entity/claim hints (optional)
  -> OpenSearch lexical/vector candidates
  -> bounded fusion and rerank
  -> EvidenceRef selection
  -> get_evidence from CanonicalStore/ArtifactStore
  -> Evidence-only context assembly
  -> Agent answer and citation validation
```

## Private Docker Deployment

The repository will provide a KR-owned Compose project with profiles:

- `core`: PostgreSQL, MinIO, OpenSearch, KR API, workers.
- `semantic`: Neo4j and semantic enrichment worker.
- `governance`: OpenMetadata and only its explicitly declared dependencies.

Every resource uses a `kr-v2-` prefix and a dedicated `kr-v2-net` network. Host ports are configurable and checked before startup. Startup fails without changes if a requested port is occupied. Operations use only `docker compose -p kr-v2`; no command may stop or remove containers, networks, or volumes outside that project. The local profile disables outbound model/parser calls unless explicitly enabled.

The deployment includes health checks, named persistent volumes, backup/restore commands scoped to the project, and a read-only inventory command that lists only KR-owned resources. No `docker system prune`, global process kill, or unscoped volume/network cleanup is permitted.

## Migration from Current KR

1. Add v2 package boundaries and contracts without deleting the current implementation.
2. Build a `DocumentIR` from current asset revisions; represent existing text chunks as `DocumentElement(type="paragraph")` with preserved revision and line provenance.
3. Copy current artifacts to the S3-compatible store and record hashes.
4. Implement PostgreSQL CanonicalStore and replay current assets; old sparse vectors are discarded or retained only as a diagnostic artifact because the new embedding version is different.
5. Implement OpenSearch IndexBackend and build a versioned index from Elements.
6. Add Docling, Unstructured, and MinerU parser adapters with the common QualityGate.
7. Add OpenMetadata and Neo4j adapters, fixed graph schema, proposal lifecycle, and asynchronous enrichment.
8. Implement the five Context Runtime primitives and map the legacy interface through the compatibility adapter.
9. Run retrieval-only, Evidence, fuzzy Agent, revision, reindex, failure-retry, and private-egress benchmarks.
10. Remove old internals only after v2 meets the acceptance criteria and the compatibility adapter has no remaining callers.

## Testing and Acceptance

### Unit and contract tests

- DocumentIR validation preserves Element type, hierarchy, page, bbox, table payload, and provenance.
- LocalProvider and MinerUProvider produce equivalent `DocumentIR`/Evidence contracts for the same fixture class, while private mode proves that LocalProvider emits no external request.
- QualityGate routes good, low-confidence, retry, and escalated parse reports correctly.
- CanonicalStore is idempotent by content/revision hash and rejects invalid revision publication.
- ArtifactStore round-trips bytes and hashes without changing media metadata.
- IndexBackend returns only published revisions and rebuilds from canonical data.
- Semantic proposals enforce lifecycle status and Evidence references.
- `get_evidence` rejects stale revisions and returns hashable content.
- Model gateways use only their assigned environment variable and model.
- Legacy API translations preserve Locator and stale-revision behavior.

### Integration and benchmark gates

- The existing actuarial source/evidence benchmark must not regress from its current source and Evidence baseline.
- Every answerable gold case must be evaluated separately for source hit, Evidence coverage, claim-local citation coverage, and Agent loop count.
- Scale benchmarks must show no Python full-corpus vector materialization in the OpenSearch path and must measure incremental ingestion separately from initial indexing.
- Reindexing, revision switching, deletion, and failed parser/index jobs must preserve the canonical source and Evidence contract.
- Docker integration tests must inspect and manipulate only `kr-v2-*` resources.
- Private mode tests must fail closed when an external model/parser endpoint is configured without explicit opt-in; logs and test output must not contain API keys or document bodies.

### POC acceptance targets

- Deterministic gold questions meet or exceed the current recorded source/Evidence Hit@3 baseline, with Evidence coverage at least 0.80.
- Every successful Agent answer resolves at least one Evidence ref, and claim-local citation coverage targets at least 0.90.
- Fuzzy and ambiguous prompts distinguish successful retrieval, justified clarification, and budget exhaustion; they are not collapsed into one accuracy number.
- Rebuilds reproduce Evidence refs, failed revisions never become current, and private-mode egress remains zero until explicitly enabled.

The v2 implementation is complete only when the canonical store can rebuild every index, every sourced answer can resolve its Evidence, and the Agent can use the five primitives without depending on a mutable, LLM-generated top-level Ontology.
