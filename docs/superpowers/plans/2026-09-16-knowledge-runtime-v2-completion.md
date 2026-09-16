# Knowledge Runtime v2 Completion Implementation Plan

> **For agentic workers:** Execute this plan in the `codex/kr-v2-completion` worktree task-by-task. Every production behavior starts with a failing test and each task ends with focused tests plus the full suite.

**Goal:** Make the v2 Evidence Runtime, Semantic Overlay, Context Runtime, and Agent Runtime usable as one evidence-first Knowledge Runtime POC with durable private deployment paths and measurable end-to-end behavior.

**Architecture:** L1 remains the only canonical source for revisions, immutable artifacts, elements, and Evidence. L2 consumes L1 Evidence references to create governed semantic proposals without blocking ingestion or raw evidence reads. L3 exposes the stable `list/find/search/read/stat` knowledge-access protocol and compiles bounded context; Agent Runtime owns multi-turn reasoning and can only receive source text through `read`/`get_evidence`.

**Tech Stack:** Python 3.11, PostgreSQL-compatible CanonicalStore, S3-compatible ArtifactStore, OpenSearch IndexBackend, optional Neo4j/OpenMetadata adapters, standard-library HTTP service, Qwen `qwen3.8-max` Agent client, DashScope `qwen3.7-text-embedding` gateway, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-14-knowledge-runtime-v2-design.md`

## Global Constraints

- `DocumentIR`, `DocumentElement`, `EvidenceRef`, and immutable `Evidence` remain the source and citation contract.
- Search returns locators and metadata only; canonical source content enters an Agent turn only through a successful `read`/`get_evidence` call.
- A document revision becomes current only after canonical persistence and index publication both succeed.
- L2 enrichment is asynchronous and cannot block L1 ingestion or L3 raw-Evidence retrieval.
- Remote parser, embedding, and Agent calls remain disabled unless their individual `KR_ALLOW_REMOTE_*` flag is explicitly true. Keys are read only from their dedicated environment variables and never stored or logged.
- Docker resources must stay in the `kr-v2` Compose project and use only `kr-v2-*` names.
- Every new behavior uses a red-green-refactor test cycle; do not use live cloud credentials in automated tests.

---

### Task 1: Make L1 ingestion durable, traceable, and publication-safe

**Files:**
- Create: `knowledge_runtime/v2/runtime.py`
- Modify: `knowledge_runtime/v2/canonical.py`
- Modify: `knowledge_runtime/v2/config.py`
- Modify: `knowledge_runtime/v2/artifacts.py`
- Modify: `knowledge_runtime/v2/ingestion.py`
- Modify: `knowledge_runtime/v2/cli.py`
- Modify: `knowledge_runtime/v2/migrations/001_initial.sql`
- Create: `tests/v2/test_runtime.py`
- Modify: `tests/v2/test_ingestion.py`
- Modify: `tests/v2/test_canonical.py`
- Modify: `tests/v2/test_artifacts.py`

**Interfaces:**
- Produces `RuntimeBundle(canonical, artifacts, index, context, ingestion)` from `RuntimeConfig`; later tasks add L2 proposals and L3 access without changing the L1 construction contract.
- Extends `CanonicalStore` with artifact/job persistence methods used by `IngestionService`.
- Preserves `IngestionResult.history` as the authoritative state-transition audit trail.
- Uses a standard-library SQLite `CanonicalStore` for durable local development; its derived in-memory index is rebuilt from current canonical revisions at process start. PostgreSQL remains the production-compatible canonical backend.

- [ ] **Step 1: Write failing tests for a durable runtime bundle.**

```python
def test_runtime_bundle_reopens_durable_local_canonical_store(tmp_path):
    config = RuntimeConfig.test_private(local_state_path=str(tmp_path / "runtime.sqlite"))
    first = build_runtime(config)
    first.canonical.put_revision(ir, source_hash="a" * 64)
    second = build_runtime(config)
    assert second.canonical.get_revision(ir.document_id, ir.revision_id).source_hash == "a" * 64

def test_ingest_stores_raw_and_document_ir_artifacts_before_publication(tmp_path):
    result = service.ingest(source)
    assert result.state == "CURRENT_REVISION_PUBLISHED"
    assert {ref.kind for ref in canonical.get_ir(result.document_id, result.revision_id).source_artifacts} >= {"source.md", "document-ir.json"}
```

- [ ] **Step 2: Run the focused tests and confirm they fail because the bundle and IR artifact persistence do not exist.**

Run: `python -m pytest tests/v2/test_runtime.py tests/v2/test_ingestion.py -q`

- [ ] **Step 3: Implement one runtime construction path and an explicit artifact manifest.**

```python
@dataclass(frozen=True)
class RuntimeBundle:
    canonical: CanonicalStore
    artifacts: ArtifactStore
    index: IndexBackend
    context: ContextRuntime
    ingestion: IngestionService

def build_runtime(
    config: RuntimeConfig,
    *,
    canonical: CanonicalStore | None = None,
    artifacts: ArtifactStore | None = None,
    index: IndexBackend | None = None,
) -> RuntimeBundle:
    """Choose configured external backends or the durable local development backend."""
```

`IngestionService` must write raw source and a deterministic JSON `DocumentIR` artifact, attach both references before canonical persistence, record accepted/retry/escalated/failed job states, and only write the current pointer after index success.

- [ ] **Step 4: Add the smallest storage methods needed by the state machine.**

```python
| Method | Required behavior |
| --- | --- |
| `record_artifacts(document_id, revision_id, refs)` | Persist the immutable artifact references linked to one revision. |
| `record_ingestion_job(job_id, details)` | Upsert the latest state and append the transition audit for one ingestion attempt. |
| `list_ingestion_jobs(document_id=None)` | Return redacted state, transition, and timing metadata in deterministic order. |
```

The PostgreSQL implementation writes `artifact_refs` and `ingestion_jobs`; the development implementation persists the same facts rather than recreating a blank in-memory runtime for each CLI process.

- [ ] **Step 5: Verify focused tests, then the full suite.**

Run: `python -m pytest tests/v2/test_runtime.py tests/v2/test_ingestion.py tests/v2/test_canonical.py tests/v2/test_artifacts.py -q`

Run: `python -m pytest -q`

- [ ] **Step 6: Commit the L1 durability change.**

```bash
git add knowledge_runtime/v2 tests/v2 pyproject.toml
git commit -m "feat: make v2 evidence ingestion durable"
```

### Task 2: Complete L1 provider quality routing and index lifecycle

**Files:**
- Modify: `knowledge_runtime/v2/providers.py`
- Modify: `knowledge_runtime/v2/quality.py`
- Modify: `knowledge_runtime/v2/ingestion.py`
- Modify: `knowledge_runtime/v2/index.py`
- Modify: `knowledge_runtime/v2/embedding.py`
- Modify: `tests/v2/test_providers.py`
- Modify: `tests/v2/test_quality.py`
- Modify: `tests/v2/test_index.py`
- Modify: `tests/v2/test_ingestion.py`

**Interfaces:**
- `ParserRouter.parse_candidates(source, document_id, revision_id)` returns an ordered tuple of `ParserProvider` candidates and makes fallback order visible.
- `QualityGate.evaluate(report)` determines `ACCEPTED`, `RETRY`, or `ESCALATED` without interpreting source content.
- `IndexBackend.rebuild(request: IndexRebuildRequest)` rebuilds solely from current canonical revisions.

- [ ] **Step 1: Write failing tests for quality retry, explicit remote fallback, and canonical-driven rebuild.**

```python
def test_retry_uses_next_local_candidate_without_remote_egress(tmp_path):
    result = service.ingest(scanned_pdf)
    assert result.state == "ESCALATED"
    assert result.history[-1] == "ESCALATED"

def test_rebuild_reads_current_canonical_revisions_not_prior_index_rows():
    report = backend.rebuild(IndexRebuildRequest("rebuild-v1"))
    assert report.state == "SUCCEEDED"
```

- [ ] **Step 2: Run focused tests and confirm the missing candidate lifecycle or rebuild behavior.**

Run: `python -m pytest tests/v2/test_providers.py tests/v2/test_quality.py tests/v2/test_index.py tests/v2/test_ingestion.py -q`

- [ ] **Step 3: Implement ordered parser candidates and retain every decision in ParseReport/job diagnostics.**

Local Docling and Unstructured candidates must be preferred in private mode. MinerU is considered only after `KR_ALLOW_REMOTE_PARSER=true`; a failed or low-quality result records its reason and never silently replaces source evidence with flattened text.

- [ ] **Step 4: Implement idempotent index publication and rebuild diagnostics.**

The index stores only derived text, vectors, filters, and stable `EvidenceRef` values. Failed publish/rebuild attempts leave the prior current revision searchable. Embedding cache keys retain content hash, model, dimensions, and gateway configuration.

- [ ] **Step 5: Verify focused tests and the full suite.**

Run: `python -m pytest tests/v2/test_providers.py tests/v2/test_quality.py tests/v2/test_index.py tests/v2/test_ingestion.py -q`

Run: `python -m pytest -q`

- [ ] **Step 6: Commit the provider/index lifecycle change.**

```bash
git add knowledge_runtime/v2 tests/v2
git commit -m "feat: complete v2 provider and index lifecycle"
```

### Task 3: Replace the L2 semantic skeleton with a persisted, evidence-governed overlay

**Files:**
- Modify: `knowledge_runtime/v2/semantic.py`
- Modify: `knowledge_runtime/v2/canonical.py`
- Modify: `knowledge_runtime/v2/migrations/001_initial.sql`
- Modify: `knowledge_runtime/v2/runtime.py`
- Create: `tests/v2/test_semantic_persistence.py`
- Modify: `tests/v2/test_semantic.py`

**Interfaces:**
- `PostgresProposalStore` implements `ProposalStore` with deterministic idempotency keys.
- `SemanticEnrichmentWorker.propose_from_ir(ir: DocumentIR)` emits typed `entity`, `claim`, `term`, and `metadata` proposals linked to Evidence.
- `SemanticProjectionService.publish_verified(proposal: SemanticProposal)` sends only verified/certified proposals to Neo4j/OpenMetadata adapters.

- [ ] **Step 1: Write failing persistence and lifecycle tests.**

```python
def test_persisted_verified_claim_survives_store_reopen(sqlite_store):
    proposal = store.create(
        kind="claim",
        payload={"subject_id": "entity-reserve-margin", "predicate": "has_definition", "value": "minimum capital buffer"},
        evidence_refs=(ref,),
    )
    store.transition(proposal.proposal_id, "AUTO_ACCEPTED")
    assert reopened.get(proposal.proposal_id).status == "VERIFIED"

def test_projection_refuses_unverified_proposal(fake_graph):
    with pytest.raises(ValueError, match="VERIFIED"):
        projection.publish_verified(proposed_claim)
```

- [ ] **Step 2: Run focused tests and confirm the persistence/projector behavior is absent.**

Run: `python -m pytest tests/v2/test_semantic.py tests/v2/test_semantic_persistence.py -q`

- [ ] **Step 3: Implement proposal persistence and deterministic enrichment.**

Extractors create bounded proposals from canonical elements. Every meaningful proposal has `EvidenceRef`, extractor version, confidence, and an idempotency key. Implement both a SQLite development `ProposalStore` and a PostgreSQL-compatible store, with the same lifecycle semantics. Deterministic extractors provide the private baseline; optional model enrichment remains separately gated. Extend `RuntimeBundle` with the chosen proposal store after this task.

- [ ] **Step 4: Implement fixed-schema projection adapters.**

Neo4j projection writes `Document`, `Evidence`, `Entity`, `Claim`, and `Term` nodes plus fixed relation types. OpenMetadata projection publishes only document collection metadata, business terms, certification status, and lineage through client APIs.

- [ ] **Step 5: Verify focused tests and the full suite.**

Run: `python -m pytest tests/v2/test_semantic.py tests/v2/test_semantic_persistence.py -q`

Run: `python -m pytest -q`

- [ ] **Step 6: Commit the L2 semantic overlay change.**

```bash
git add knowledge_runtime/v2 tests/v2
git commit -m "feat: persist governed semantic overlay"
```

### Task 4: Make L3 a first-class Knowledge Access Runtime

**Files:**
- Create: `knowledge_runtime/v2/access.py`
- Modify: `knowledge_runtime/v2/context.py`
- Modify: `knowledge_runtime/v2/compat.py`
- Modify: `knowledge_runtime/v2/runtime.py`
- Modify: `knowledge_runtime/v2/cli.py`
- Create: `tests/v2/test_access.py`
- Modify: `tests/v2/test_context.py`
- Modify: `tests/v2/test_compat.py`

**Interfaces:**
- `KnowledgeAccessRuntime.list/find/search/read/stat` is the stable Agent-facing protocol.
- `read(locator, options)` resolves a current revision and returns actual immutable Evidence.
- `ContextRuntime` records bounded retrieval decisions without retaining model prompt bodies by default.

- [ ] **Step 1: Write failing protocol tests.**

```python
def test_search_returns_locator_without_source_body(runtime):
    hit = runtime.search("reserve margin").items[0]
    assert hit.locator.revision_id
    assert runtime.read(hit.locator).content == "The reserve margin is calculated from adverse deviation."

def test_stat_exposes_revision_and_provenance_without_reading_body(runtime):
    status = runtime.stat(locator)
    assert status["revision"] == locator.revision
    assert "content" not in status
```

- [ ] **Step 2: Run focused tests and confirm the first-class protocol does not exist.**

Run: `python -m pytest tests/v2/test_access.py tests/v2/test_context.py tests/v2/test_compat.py -q`

- [ ] **Step 3: Implement the protocol as a thin adapter over L1/L3 contracts.**

`list`, `find`, `search`, `read`, and `stat` must preserve opaque selectors and revision identity. The Agent Runtime never serializes search preview text to model context; `read` is the only body-producing operation. Keep the legacy adapter as a compatibility wrapper over this first-class runtime. Extend `RuntimeBundle` with `access` after this task.

- [ ] **Step 4: Persist retrieval audit records and enforce context budgets.**

Record primitive name, stable references, byte/evidence budgets, timings, outcome, and redacted diagnostics. Reject stale locators before reading.

- [ ] **Step 5: Verify focused tests and the full suite.**

Run: `python -m pytest tests/v2/test_access.py tests/v2/test_context.py tests/v2/test_compat.py -q`

Run: `python -m pytest -q`

- [ ] **Step 6: Commit the L3 access-runtime change.**

```bash
git add knowledge_runtime/v2 tests/v2
git commit -m "feat: expose v2 knowledge access runtime"
```

### Task 5: Add a real Agent Runtime with Evidence-only tool use

**Files:**
- Create: `knowledge_runtime/v2/agent_runtime.py`
- Modify: `knowledge_runtime/v2/agent.py`
- Modify: `knowledge_runtime/v2/runtime.py`
- Modify: `knowledge_runtime/v2/cli.py`
- Create: `tests/v2/test_agent_runtime.py`
- Modify: `tests/v2/test_agent.py`

**Interfaces:**
- `AgentRuntime.run(task, session=None, budget=None) -> AgentRunResult`
- `AgentRunResult` contains answer, citations, tool trace metadata, budgets, stop reason, and no raw prompt log by default.
- `QwenAgentClient` remains fixed to `qwen3.8-max` and uses only `QWEN_LLM_API_KEY` plus `QWEN_LLM_BASE_URL` when the remote Agent gate is open.

- [ ] **Step 1: Write failing multi-turn tests using a scripted model.**

```python
def test_agent_runtime_requires_read_before_answering(scripted_model, access_runtime):
    result = AgentRuntime(scripted_model, access_runtime).run("What is the reserve margin?")
    assert result.evidence
    assert result.answer.endswith(f"[{result.evidence[0].evidence_id}]")

def test_agent_runtime_never_puts_search_preview_in_model_tool_result(scripted_model, access_runtime):
    AgentRuntime(scripted_model, access_runtime).run("reserve margin")
    assert "The reserve margin is" not in scripted_model.search_tool_payload
```

- [ ] **Step 2: Run focused tests and confirm `AgentRuntime` is absent.**

Run: `python -m pytest tests/v2/test_agent_runtime.py tests/v2/test_agent.py -q`

- [ ] **Step 3: Implement the tool registry, session state, and answer guard.**

The runtime supplies only `list/find/search/read/stat` to the model, applies round/evidence/byte budgets, serializes search hits without bodies, sends body content only after `read`, and rejects uncited answers. It must return a safe clarification/no-evidence response without inventing a sourced claim.

- [ ] **Step 4: Add a CLI agent-run command with JSON-safe trace summaries.**

The command accepts a task and explicit budget settings, prints the answer with citations, and writes no API key, document body, or model request body to status output.

- [ ] **Step 5: Verify focused tests and the full suite.**

Run: `python -m pytest tests/v2/test_agent_runtime.py tests/v2/test_agent.py -q`

Run: `python -m pytest -q`

- [ ] **Step 6: Commit the Agent Runtime change.**

```bash
git add knowledge_runtime/v2 tests/v2
git commit -m "feat: add evidence-only agent runtime"
```

### Task 6: Make deployment and benchmarks prove the complete runtime

**Files:**
- Create: `knowledge_runtime/v2/server.py`
- Create: `knowledge_runtime/v2/worker.py`
- Modify: `deploy/kr-v2.compose.yml`
- Modify: `deploy/Dockerfile`
- Modify: `deploy/README.md`
- Modify: `scripts/kr-v2.ps1`
- Modify: `benchmarks/v2/run_benchmark.py`
- Create: `benchmarks/v2/corpus/README.md`
- Create: `tests/v2/test_server.py`
- Modify: `tests/v2/test_deployment_scope.py`
- Modify: `tests/v2/test_benchmark_metrics.py`

**Interfaces:**
- HTTP service exposes health, ingestion, search, read, and Agent-run endpoints over a single `RuntimeBundle`.
- Worker processes pending semantic enrichment without blocking L1 publication.
- Benchmark runner evaluates retrieval and Agent Runtime separately and records redacted run metrics.

- [ ] **Step 1: Write failing service and benchmark tests.**

```python
def test_http_search_returns_refs_and_read_returns_evidence(server_client):
    assert "content" not in server_client.post("/v1/search", {"query": "reserve"})["items"][0]
    assert server_client.post("/v1/read", {"locator": locator})["evidence_id"]

def test_agent_benchmark_records_real_runtime_metrics_without_answer_body(tmp_path):
    report = run_cases(cases, corpus=corpus, live_agent=False)
    assert report["agent_runtime"] is True
    assert "answer" not in json.dumps(report)
```

- [ ] **Step 2: Run focused tests and confirm server/worker integration is absent.**

Run: `python -m pytest tests/v2/test_server.py tests/v2/test_deployment_scope.py tests/v2/test_benchmark_metrics.py -q`

- [ ] **Step 3: Implement a long-running runtime service and semantic worker.**

Use a standard-library JSON HTTP server to avoid an unreviewed framework dependency. Create the runtime once at process start, apply migrations when a PostgreSQL endpoint is configured, and retain no prompt or source body in request logs. Replace Compose `status` commands with the server and worker entry points.

- [ ] **Step 4: Add benchmark fixtures and an end-to-end evidence test.**

Keep third-party actuarial PDFs outside Git. Commit a synthetic, source-attributed mini-corpus and case file that exercise exact terminology, fuzzy wording, multi-turn follow-up, stale locator rejection, no-evidence refusal, and revision replacement.

- [ ] **Step 5: Verify code, deployment configuration, and the runtime path.**

Run: `python -m pytest -q`

Run: `python -m compileall knowledge_runtime`

Run: `docker compose -p kr-v2 -f deploy/kr-v2.compose.yml --profile core config --quiet`

Run the local synthetic benchmark and record its report under `.kr-data/`, which remains untracked.

- [ ] **Step 6: Commit deployment and validation support.**

```bash
git add knowledge_runtime/v2 deploy scripts benchmarks/v2 tests/v2
git commit -m "feat: run v2 knowledge runtime end to end"
```

## Final Acceptance Checklist

- [ ] Every L1 write produces immutable raw and DocumentIR artifacts, revision-aware canonical records, quality/provenance diagnostics, and a safe publication result.
- [ ] Every L2 claim, entity, term, or relation proposal has source Evidence and cannot become high-trust context before its lifecycle permits it.
- [ ] Every L3 search returns locators/metadata, while read is the only source-body operation.
- [ ] Agent Runtime can complete a multi-turn evidence-grounded answer, refuses unsupported answers, and reports bounded/redacted trace metrics.
- [ ] The runtime runs as long-lived API and worker services in the `kr-v2` Compose project.
- [ ] Offline benchmark reports retrieval quality and Agent Runtime behavior separately; live Qwen benchmark remains explicitly gated.
- [ ] The full test suite, compile check, diff check, and Compose config check pass on `codex/kr-v2-completion`.
