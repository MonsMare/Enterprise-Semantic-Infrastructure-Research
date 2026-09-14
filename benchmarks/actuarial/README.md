# Actuarial benchmark sources

This benchmark catalogue focuses on public actuarial and insurance material from
the Actuarial Standards Board (ASB), Casualty Actuarial Society (CAS), Society of
Actuaries (SOA), NAIC, EIOPA, and the UK Government Actuary's Department (GAD).

The repository stores source metadata and URLs, not a redistributed copy of the
third-party PDFs. Download only material whose terms permit your intended use,
record the downloaded file's SHA-256, and keep the local files outside Git. The
catalogue is designed to test document ingestion, tables, headings, formulas,
regulatory language, revisions, and actuarial multi-hop questions.

## Suggested collection

Start with the following mix:

- ASOP 23 (Data Quality), ASOP 43 (Property/Casualty Unpaid Claim Estimates),
  ASOP 56 (Modeling), and ASOP 41 (Actuarial Communications).
- CAS Statements of Principles on P&C ratemaking and unpaid claim estimates.
- NAIC Risk-Based Capital forecasting and model-law material.
- EIOPA Insurance Stress Test 2024 technical specifications and templates.
- SOA mortality-improvement and longevity research reports.
- GAD technical bulletins on pensions and insurance.

These sources have different authority levels and effective dates. The benchmark
manifest therefore records source authority, practice area, document status, and
effective date so that tests can distinguish current guidance from historical or
educational material.

The JSONL manifest is a catalogue. A benchmark case should refer to the local
downloaded filename after collection and include an exact evidence section or
quote. Do not treat a model-generated summary as a gold answer.

## Reproducible POC run

The checked-in case set contains four source-bound questions for ASOP 23, ASOP
43, ASOP 56, and EIOPA's 2024 stress test. Run the deterministic layer after
downloading and ingesting the documents:

```powershell
python .\benchmarks\actuarial\fetch_sources.py --output .kr-data\actuarial-downloads
python -m knowledge_runtime.cli ingest .kr-data\actuarial-downloads --backend local --store .kr-data\actuarial.db
python -m knowledge_runtime.cli benchmark-retrieval .\benchmarks\actuarial\questions.jsonl --store .kr-data\actuarial.db --output .kr-data\actuarial-retrieval-report.json
```

The retrieval-only run does not call an LLM. It verifies source rank and chunk
evidence rank separately, reads expected evidence, checks evidence phrases,
and records latency and evidence size. The latest offline iteration report is
[`docs/reports/2026-09-14-hybrid-retrieval-results.md`](../../docs/reports/2026-09-14-hybrid-retrieval-results.md).
The separate `benchmark` command runs the Qwen 3.8 Agent Retrieval Loop when
`LLM_API_KEY` is configured.

## 模糊问法与弱查询评测

`questions-fuzzy.jsonl` 收录了中文口语化、简短、多语言和上下文不足的精算问题。
`benchmark` 会把原始问题交给 Agent，再记录它实际发出的每条搜索语句、首轮与最佳
来源排名、是否读取到金标准来源、回答是否包含金标准短语，以及 Evidence 引用情况。
该命令固定使用 Qwen 3.8，并需要配置 `LLM_API_KEY`：

```powershell
python -m knowledge_runtime.cli benchmark .\benchmarks\actuarial\questions-fuzzy.jsonl --store .kr-data\actuarial.db --model qwen3.8-max --output .kr-data\actuarial-fuzzy-agent-report.json
```

`weak_query_probes.jsonl` 则是 KR-only 的直接检索压力样例。`benchmark-retrieval`
跳过 Agent，把 `data`、`estimate`、`models` 等刻意压缩的查询原样交给 KR，从而区分
“Agent 是否改写成功”和“KR 面对弱查询本身的排序及证据定位能力”。
`retrieval_max_expected_rank` 是每条弱查询的人工排名门槛：

```powershell
python -m knowledge_runtime.cli benchmark-retrieval .\benchmarks\actuarial\weak_query_probes.jsonl --store .kr-data\actuarial.db --output .kr-data\actuarial-weak-query-report.json
```

`query_anchor_groups` 是 fuzzy Agent 测试中的人工标注关键概念及可接受同义词。Agent 查询锚点覆盖率衡量
改写是否保留问题中的核心概念；它是可解释的诊断指标，不代替真实检索结果。报告按
`query_style` 分组显示首轮 Hit@1/3、查询锚点覆盖率和通过率；同时应对比首轮与最佳
排名、搜索次数、端到端通过率。检索是否有效最终以找到正确来源、读取到支持答案的
Evidence、回答引用该 Evidence 为准。弱查询排名门槛失败是有意义的测量结果，表示
需要 Agent 改写或改善 KR 的语义检索能力，不应为了让整个报告变绿而放宽门槛。

对于中文问题，使用 `required_claims` 将可接受的中英文答案短语与来源中的支持短语
逐条配对；这样答案语言可以跟随用户，而证据核验仍对照原文。

In the fuzzy dataset, `retrieval_query` is a human-authored gold search query,
not an Agent-generated rewrite; only the live `benchmark` command measures the
queries Qwen actually sends to KR. `weak_query_probes.jsonl` uses terse KR-only
queries such as `data`, `estimate`, and `models` to separate source discovery
from evidence localization.

To measure how SQLite search latency, Hit@k/MRR, and Evidence localization
change as synthetic corpus size increases, see [the scale benchmark](../scale/README.md).
