# Knowledge Runtime POC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Build a source-grounded Knowledge Runtime POC with interchangeable MinerU Cloud and Local extraction backends, five KR primitives, Evidence objects, and a bounded Agent Retrieval Loop.

**Architecture:** Source files are parsed by a pluggable extraction backend into a local Knowledge Asset containing structure, text, and metadata. A Provider exposes `list/find/search/read/stat`; an Agent Loop calls those tools and passes only Read-produced Evidence into the answer context.

**Tech Stack:** Python 3.11+, standard library HTTP/JSON/hashlib/pathlib, pytest, optional `requests`-free HTTP adapters, OpenAI-compatible chat completions.

**Spec:** `docs/superpowers/specs/2026-09-14-knowledge-runtime-poc-design.md`

## Global Constraints

- Never store or print API keys; read `MINERU_API_KEY`, `LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL` only from the environment.
- Keep the mandatory KR surface to `list`, `find`, `search`, `read`, and `stat`.
- Search results are hints; only Read returns Evidence.
- Every SearchHit must contain a replayable Locator with revision and selector.
- Do not add authentication, tenancy, vector databases, or distributed infrastructure.

### Task 1: Protocol and model contracts

**Files:**
- Create: `knowledge_runtime/models.py`
- Create: `knowledge_runtime/errors.py`
- Create: `tests/test_models.py`

**Interfaces:**
- Produces `Locator`, `SearchHit`, `Evidence`, `Page`, `ProviderDescriptor`, and request option dataclasses.
- Produces typed errors: `KRNotFound`, `KRInvalidLocator`, `KRStaleLocator`, `KRUnsupported`, `KRLimitExceeded`.

- [x] Write failing tests for locator round-trip, evidence metadata, stable page envelope, and typed errors.
- [x] Run `pytest tests/test_models.py -q` and verify failure because modules are absent.
- [x] Implement immutable dataclasses with canonical JSON serialization and SHA-256 content hashing.
- [x] Run the focused tests and verify they pass.

### Task 2: Provider contract and in-memory/file providers

**Files:**
- Create: `knowledge_runtime/provider.py`
- Create: `knowledge_runtime/memory_provider.py`
- Create: `knowledge_runtime/file_provider.py`
- Create: `tests/test_provider_contract.py`

**Interfaces:**
- `KnowledgeProvider` protocol with `list`, `find`, `search`, `read`, `stat`.
- `MemoryProvider` and `FileProvider` implement the same protocol and return the same models.

- [x] Write failing contract tests covering list/find/search → read, pagination without duplicates, max bytes, invalid selector, and stale revision.
- [x] Run the contract tests and verify failure because providers are absent.
- [x] Implement the minimal in-memory provider and a local Markdown/text provider using content hash revisions and section/line selectors.
- [x] Run all provider contract tests and verify they pass for both providers.

### Task 3: Knowledge Asset store and extraction backends

**Files:**
- Create: `knowledge_runtime/assets.py`
- Create: `knowledge_runtime/extractors.py`
- Create: `knowledge_runtime/mineru_backend.py`
- Create: `knowledge_runtime/local_backend.py`
- Create: `tests/test_assets.py`

**Interfaces:**
- `KnowledgeAssetStore` persists source metadata and derived artifacts under a local directory.
- `ExtractionBackend.extract(path) -> KnowledgeAsset`.
- `MinerUCloudBackend` uses an injected HTTP transport; tests never call the network.
- `LocalBackend` parses Markdown/text deterministically and optionally enriches structure through an injected callable.

- [x] Write failing tests for asset hashes, parser provenance, local extraction, and MinerU task submit/poll/download request sequencing using a fake transport.
- [x] Run focused tests and verify failure.
- [x] Implement the asset manifest, local extractor, and cloud adapter with environment-only configuration.
- [x] Run tests and verify both backends produce the same asset contract.

### Task 4: Agent Retrieval Loop

**Files:**
- Create: `knowledge_runtime/agent_loop.py`
- Create: `knowledge_runtime/llm_client.py`
- Create: `tests/test_agent_loop.py`

**Interfaces:**
- `AgentLoop.run(question, provider, max_iterations=8, max_read_bytes=20000) -> AgentResult`.
- `OpenAICompatibleClient.complete(messages, tools) -> ModelTurn`.
- Agent tools expose only `list/find/search/read/stat`; Read outputs Evidence IDs and content.

- [x] Write failing tests for search → read → final answer, max iteration stop, read budget stop, and no-evidence refusal.
- [x] Run focused tests and verify failure.
- [x] Implement a deterministic fake model path for tests and an OpenAI-compatible HTTP client for runtime use.
- [x] Run tests and verify the loop preserves Evidence and stops safely.

### Task 5: CLI demo and end-to-end tests

**Files:**
- Create: `knowledge_runtime/cli.py`
- Create: `README.md`
- Create: `.env.example`
- Create: `tests/test_end_to_end.py`

- [x] Write a failing end-to-end test that ingests a fixture document, searches it, reads a section, and asserts the answer cites an Evidence ID.
- [x] Run the test and verify failure.
- [x] Implement `python -m knowledge_runtime.cli ingest|ask` and document Cloud/Local environment variables without real secrets.
- [x] Run the full test suite and verify all tests pass.
