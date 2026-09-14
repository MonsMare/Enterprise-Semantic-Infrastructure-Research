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

The retrieval-only run does not call an LLM. It verifies source rank, reads the
expected source, checks evidence phrases, and records latency and evidence size.
The separate `benchmark` command runs the Qwen 3.8 Agent Retrieval Loop when
`LLM_API_KEY` is configured.
