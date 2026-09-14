import json

from knowledge_runtime.cli import main
from knowledge_runtime.assets import KnowledgeAssetStore


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
