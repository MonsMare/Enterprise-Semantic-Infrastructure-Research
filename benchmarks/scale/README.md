# SQLite retrieval scale benchmark

This benchmark measures the current `AssetKnowledgeProvider` search-and-read
path against a fully local, deterministic synthetic corpus. It makes no LLM,
MinerU, or network calls. Each size reuses the same gold documents and adds
more distractors, so the benchmark can show both query latency and ranking
changes as corpus volume increases.

Run from the repository root:

```powershell
python -m benchmarks.scale.run_scale_benchmark `
  --sizes 20,100,1000,5000 `
  --case-count 20 `
  --top-k 20 `
  --repeats 3 `
  --warmups 1 `
  --seed 20260914 `
  --output .kr-data/scale-benchmark-report.json
```

Each gold document has two queries. The **precise** query includes a stable,
document-specific marker. The **broad** query contains common actuarial terms
shared by every synthetic document. This contrast makes it possible to see
when lexical retrieval still finds distinctive facts and when a vague query
cannot separate the expected source from an expanding set of plausible hits.
The broad-query set is a retrieval stress control; it does not measure whether
an Agent can rewrite vague user language. That requires the separate live Agent
benchmark and should be reported independently.

The JSON report records document count, generated Markdown bytes, SQLite file
bytes, database build time, Search p50/p95, Read and end-to-end p50/p95,
Hit@1/3/5/10/20 (bounded by `--top-k`), MRR, and exact gold-phrase Evidence
location rates. It reports Evidence location both across all cases and
conditional on retrieving the target source. Temporary databases are removed
after each run; only the report is kept.

These synthetic results isolate corpus-size behavior for the current SQLite
FTS implementation. They are not a substitute for the actuarial public-source
benchmark: real-document parsing quality, domain query distribution, Agent
rewrites, and LLM answer grounding must also be measured before making
production claims.
