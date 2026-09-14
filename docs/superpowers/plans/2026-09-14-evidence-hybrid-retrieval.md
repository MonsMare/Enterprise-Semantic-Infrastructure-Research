# Knowledge Runtime Evidence Hybrid Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 KR 的检索单位改为可追溯的段落级证据片段，并以词项检索与可注入的本地语义编码器融合排序，验证证据定位和 Agent 支撑质量是否提升。

**Architecture:** 在 SQLite 中新增按 revision 生成的 chunks 与 chunk FTS 投影；每个 chunk 保存标题路径、页标记和原文行范围。Asset provider 直接返回 chunk locator；混合检索用 FTS 排名和确定性本地向量的 RRF 合并，允许未来替换真正 embedding encoder。scope 过滤在 SQLite 查询中完成，Agent 仍使用原有 provider 合约。

**Tech Stack:** Python 3.11 标准库、SQLite FTS5、现有 provider/models/evaluation、pytest。

**Spec:** `docs/superpowers/specs/2026-09-14-evidence-hybrid-retrieval-design.md`

## Global Constraints

- 保留现有 revision、locator、Evidence 和 provider 合约。
- 默认运行不调用云端模型或 embedding API。
- 旧数据库必须可打开；chunk 索引可按当前资产版本重建。
- 搜索结果必须具有稳定的 asset/revision/chunk 行定位。
- `scope` 不得绕过 SQLite FTS，也不得将多词查询降级为连续字面子串匹配。
- 每个任务先写一个会失败的行为测试，确认失败后再写生产代码。

---

### Task 1: Chunk 领域模型与 SQLite 片段索引

**Files:**
- Modify: `knowledge_runtime/assets.py`
- Modify: `knowledge_runtime/models.py`
- Test: `tests/test_assets.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Add an internal `KnowledgeChunk` value object with `chunk_id`, `asset_id`, `revision_id`, `source_name`, `heading_path`, `start_line`, `end_line`, and `text`.
- Add `SQLiteKnowledgeAssetStore.list_current_chunks(asset_id: str | None = None) -> list[KnowledgeChunk]`.
- Add `SQLiteKnowledgeAssetStore.search_chunks(query: str, *, scope: str | None, limit: int) -> list[tuple[KnowledgeChunk, float]]` for FTS candidates.

- [x] Write a failing test that an ingested Markdown asset creates multiple chunks with heading path and exact line ranges.
- [x] Run `pytest tests/test_assets.py -k chunk -q`; expect failure because chunk schema/API is absent.
- [x] Add `KnowledgeChunk`, chunk table, and chunk FTS schema. Use a deterministic chunk id derived from asset/revision/start/end/text.
- [x] Rebuild only current-revision chunks after `put`; remove stale current projections while retaining revision history.
- [x] Implement paragraph/heading/page-aware splitting with a bounded fallback chunk for long paragraphs.
- [x] Run the focused tests and then `pytest tests/test_assets.py tests/test_models.py -q`.
- [x] Commit as `feat: add revisioned paragraph evidence index`.

### Task 2: Chunk-first provider, scope fix, and hybrid scoring

**Files:**
- Modify: `knowledge_runtime/asset_provider.py`
- Modify: `knowledge_runtime/assets.py`
- Modify: `knowledge_runtime/models.py`
- Test: `tests/test_assets.py`
- Test: `tests/test_provider_contract.py`

**Interfaces:**
- Extend `SearchOptions` with `semantic_weight: float = 0.35` and `rrf_k: int = 60` while preserving existing defaults for callers.
- `AssetKnowledgeProvider.search` returns one `SearchHit` per ranked chunk and its locator selector is `{type: "chunk", id, start, end}`.
- `read` resolves chunk selectors through the current revision and returns the same Evidence contract.

- [x] Write failing tests for a weak query whose target evidence is in a later paragraph, and for a scoped multiword search that must return the target chunk.
- [x] Run the focused tests and confirm the old document-window behavior fails the new assertions.
- [x] Implement chunk FTS ranking, deterministic local vector score (word/character n-gram hashing), and RRF fusion; expose score only through ordering, not as evidence text.
- [x] Implement scope filtering by asset id, source name, or source path in SQLite before ranking; never delegate scoped asset search to `MemoryProvider.search`.
- [x] Add chunk selector support to read and stat paths without invalidating old line locators.
- [x] Run provider/asset tests and the full suite.
- [x] Commit as `feat: retrieve ranked evidence chunks`.

### Task 3: Retrieval benchmark and evidence-rank regression

**Files:**
- Modify: `knowledge_runtime/evaluation.py`
- Modify: `benchmarks/actuarial/README.md`
- Test: `tests/test_evaluation.py`
- Test: `tests/test_end_to_end.py`

**Interfaces:**
- Benchmark report separates source hit rank, first expected chunk rank, oracle-style gold-evidence rank, evidence-location rate, and latency percentiles. The live Agent benchmark remains a separate measurement.
- Existing report fields remain readable for downstream tooling.

- [x] Write failing evaluator tests proving source hit can be 1 while gold evidence appears in a later chunk, and that the report keeps both metrics.
- [x] Run focused evaluator tests and confirm missing evidence-rank metrics.
- [x] Implement chunk-aware gold phrase checks, source/evidence rank separation, and evidence reads ordered by global chunk rank.
- [x] Use the deterministic `benchmark-retrieval` CLI on the actuarial corpus and compare its source-level baseline with the hybrid report, while recording that evidence-read scope changed.
- [x] Run offline benchmark without LLM calls; save reports under `.kr-data/` and document interpretation limits.
- [x] Run the full test suite and `git diff --check`.
- [x] Commit as `test: benchmark evidence chunk retrieval`.

### Task 4: End-to-end verification and review

**Files:**
- Modify: `README.md`
- Modify: `benchmarks/actuarial/README.md`
- Create: `docs/reports/2026-09-14-hybrid-retrieval-results.md`

- [x] Run all tests and the offline actuarial benchmark.
- [x] Compare old source-level reports with new source, evidence, and latency metrics; live answer quality remains unmeasured without an Agent API key in the process environment.
- [x] Record metric regressions and root causes in the results report.
- [x] Update usage examples for chunk locators and semantic-weight configuration.
- [x] Commit as `docs: report hybrid evidence retrieval results`.
