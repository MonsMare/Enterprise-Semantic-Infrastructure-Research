import json

from knowledge_runtime.cli import main
from knowledge_runtime.assets import KnowledgeAssetStore, SQLiteKnowledgeAssetStore


def test_cli_ingests_all_local_documents_from_directory(tmp_path, capsys):
    source_dir = tmp_path / "docs"
    source_dir.mkdir()
    (source_dir / "one.md").write_text("# One\nFirst document.", encoding="utf-8")
    (source_dir / "two.txt").write_text("Second document.", encoding="utf-8")
    (source_dir / "ignore.bin").write_bytes(b"binary")
    store_path = tmp_path / "store"

    status = main(["ingest", str(source_dir), "--backend", "local", "--store", str(store_path)])
    result = json.loads(capsys.readouterr().out)

    assert status == 0
    assert {item["source_name"] for item in result["assets"]} == {"one.md", "two.txt"}
    assert len(KnowledgeAssetStore(store_path).list_assets()) == 2


def test_cli_selects_sqlite_asset_store_from_db_path(tmp_path, capsys):
    source = tmp_path / "one.md"
    source.write_text("# One\nFirst document.", encoding="utf-8")
    store_path = tmp_path / "knowledge.db"

    status = main(["ingest", str(source), "--backend", "local", "--store", str(store_path)])
    result = json.loads(capsys.readouterr().out)

    assert status == 0
    assert result["assets"][0]["source_name"] == "one.md"
    store = SQLiteKnowledgeAssetStore(store_path)
    assert store.list_assets()[0].source_name == "one.md"
    store.close()


def test_cli_catalog_exposes_sqlite_revision_history(tmp_path, capsys):
    source = tmp_path / "one.md"
    source.write_text("revision one", encoding="utf-8")
    store_path = tmp_path / "knowledge.db"
    assert main(["ingest", str(source), "--backend", "local", "--store", str(store_path)]) == 0
    capsys.readouterr()
    source.write_text("revision two", encoding="utf-8")
    assert main(["ingest", str(source), "--backend", "local", "--store", str(store_path)]) == 0
    capsys.readouterr()

    assert main(["catalog", "--store", str(store_path)]) == 0
    result = json.loads(capsys.readouterr().out)

    assert len(result["assets"]) == 1
    assert len(result["assets"][0]["revisions"]) == 2


def test_agent_and_benchmark_cli_default_to_qwen():
    from knowledge_runtime.cli import build_parser

    parser = build_parser()
    ask_args = parser.parse_args(["ask", "question"])
    benchmark_args = parser.parse_args(["benchmark", "cases.jsonl"])

    assert ask_args.model == "qwen3.8-max"
    assert benchmark_args.model == "qwen3.8-max"


def test_retrieval_benchmark_cli_runs_without_llm_credentials(tmp_path, capsys):
    source = tmp_path / "guide.md"
    source.write_text("The standard applies to all actuarial models.", encoding="utf-8")
    store_path = tmp_path / "knowledge.db"
    assert main(["ingest", str(source), "--backend", "local", "--store", str(store_path)]) == 0
    capsys.readouterr()
    cases = tmp_path / "cases.jsonl"
    cases.write_text(
        json.dumps(
            {
                "case_id": "scope-001",
                "question": "What is the scope?",
                "retrieval_query": "all actuarial models",
                "expected_sources": ["guide.md"],
                "required_evidence_phrases": ["all actuarial models"],
            }
        ),
        encoding="utf-8",
    )

    status = main(["benchmark-retrieval", str(cases), "--store", str(store_path)])
    report = json.loads(capsys.readouterr().out)

    assert status == 0
    assert report["pipeline"] == "retrieval-only"
    assert report["passed"] == 1
    assert report["retrieval_metrics"]["hit_at_1"] == 1.0
