# Enterprise Semantic Infrastructure Research

[中文 README](README.zh-CN.md)

An evidence-first **Knowledge Runtime** research prototype for enterprise semantic infrastructure.

The central question is:

> Can an AI Agent navigate a large knowledge space progressively, retrieve only the evidence it needs, and produce answers tied to revision-aware source evidence — without treating automatic RAG as the only retrieval architecture?

This repository explores that question through a working POC, retrieval benchmarks, scale experiments, and explicit failure analysis.

## Design Principles

```text
Search / navigation
      ↓
Locator
      ↓
Read
      ↓
Evidence
      ↓
Answer
```

Search does **not** directly become answer context.

A `Read` operation turns a locator into evidence carrying source revision and SHA-256 provenance. If the Agent has not read supporting evidence, it should not claim an evidence-backed answer.

Key principles:

- the original file remains the canonical source
- parsed assets are rebuildable projections
- navigation and evidence generation are separate
- search results are locators, not proof
- evidence is revision-aware
- ambiguous or underspecified questions may correctly produce no answer
- retrieval quality and Agent query-planning quality are evaluated separately

## Runtime Model

The Agent can navigate through tools such as `list`, `find`, `search`, `read`, and `stat`.

The POC supports local parsing for common text/document formats and an optional MinerU cloud path for more complex documents.

## Retrieval Architecture

The current SQLite-backed research implementation combines:

- paragraph-level chunking
- FTS5 lexical ranking
- deterministic character n-gram sparse similarity
- Reciprocal Rank Fusion (RRF)
- revision-bound locators
- read-time revision validation

The sparse similarity stage is **not a trained embedding model** and does not call an external LLM.

## Benchmark Results

### Public actuarial corpus

On a 10-document / 1,412-chunk benchmark:

- exact-source retrieval: **Hit@1 = 4/4**, MRR = 1.000
- gold evidence located for **4/4** answerable exact questions
- short/fuzzy retrieval queries found the expected source for **4/4** answerable cases
- two deliberately context-free questions correctly returned no evidence

Single-word weak queries could still require reading evidence at ranks **5–36**, showing that finding the right source is not the same as efficiently finding sufficient evidence.

### Scale experiment

At 5,000 synthetic documents:

- target source appeared in top-3 for **100%** of exact queries
- original evidence localisation was **95%**
- median search latency was about **733 ms**

The cost also increased materially because the current sparse stage performs exact scanning. This is treated as a POC boundary, not hidden as a production scaling claim.

## Retrieval Is Not the Same as Agent Quality

A real Qwen Agent benchmark exposed a useful split: retrieval often found the correct source, while strict answer quality still failed because of weak query planning, unnecessary repeated searches, poor stopping behaviour, and evidence not being placed close enough to individual claims.

> **A good knowledge index does not automatically create a good research Agent.**

See:

- [Hybrid retrieval results](docs/reports/2026-09-14-hybrid-retrieval-results.md)
- [Industry scaling options](docs/reports/2026-09-14-industry-scaling-options.md)
- [Private deployment reference path](docs/reports/2026-09-14-private-deployment-reference-path.md)

## Quick Start

Python 3.11+:

```bash
python -m pip install -e ".[dev,pdf]"
python -m knowledge_runtime.cli ingest ./docs/runbook.md --backend local
python -m knowledge_runtime.cli ask "How long is the recovery code valid?" --model qwen3.8-max
python -m pytest -q --basetemp=.test-tmp
```

## POC Boundaries

The current implementation is intentionally not presented as a production enterprise knowledge platform.

Current boundaries include:

- POC-oriented retrieval architecture
- linear-cost sparse scanning at larger corpus sizes
- no production-grade multi-tenant authorisation or permission sync
- no federated cross-provider merge
- no dedicated ANN/vector infrastructure
- local PDF parsing without OCR
- private-deployment and local-model paths that are architectural references rather than fully validated production deployments

## Why I Built This

I am interested in a broader enterprise AI question:

> What should the knowledge layer look like when Agents — rather than only humans or fixed applications — become primary consumers of enterprise information?

This repository makes that question testable.

It treats knowledge infrastructure not as “documents plus embeddings,” but as a runtime with explicit navigation, evidence, provenance, revision semantics, and evaluation.

---

**Research focus:** Enterprise AI · Knowledge Runtime · Semantic Infrastructure · Retrieval · Evidence Systems · Agent Architecture
