from __future__ import annotations

import json
from pathlib import Path

from knowledge_runtime.v2.cli import main
from knowledge_runtime.v2.config import RuntimeConfig
from knowledge_runtime.v2.runtime import build_runtime


def test_runtime_bundle_reopens_local_canonical_store_and_rebuilds_derived_index(tmp_path: Path) -> None:
    source = tmp_path / "reserve-policy.md"
    source.write_text(
        "# Reserve policy\n\nThe reserve margin is calculated from adverse deviation.\n",
        encoding="utf-8",
    )
    config = RuntimeConfig.test_private(local_state_path=str(tmp_path / "runtime.sqlite"))

    first = build_runtime(config)
    ingested = first.ingestion.ingest(source, provider="local")
    assert ingested.state == "CURRENT_REVISION_PUBLISHED"

    second = build_runtime(config)
    page = second.context.search_evidence("reserve margin")

    assert type(second.canonical).__name__ == "SqliteCanonicalStore"
    assert second.canonical.current_revision(ingested.document_id) == ingested.revision_id
    assert ingested.revision_id in {hit.ref.revision_id for hit in page.items}


def test_separate_cli_commands_share_the_durable_local_runtime(tmp_path: Path, monkeypatch, capsys) -> None:
    source = tmp_path / "reserve-policy.md"
    source.write_text("# Reserve policy\n\nReserve margin protects against adverse deviation.\n", encoding="utf-8")
    monkeypatch.setenv("KR_LOCAL_STATE_PATH", str(tmp_path / "runtime.sqlite"))
    monkeypatch.setenv("KR_PRIVATE_MODE", "true")
    monkeypatch.setenv("KR_ALLOW_REMOTE_PARSER", "false")
    monkeypatch.setenv("KR_ALLOW_REMOTE_EMBEDDING", "false")
    monkeypatch.setenv("KR_ALLOW_REMOTE_AGENT", "false")
    monkeypatch.delenv("KR_DATABASE_URL", raising=False)
    monkeypatch.delenv("KR_ARTIFACT_ENDPOINT", raising=False)
    monkeypatch.delenv("KR_OPENSEARCH_URL", raising=False)

    assert main(["v2", "ingest", str(source), "--provider", "local"]) == 0
    ingested = json.loads(capsys.readouterr().out)
    assert main(["v2", "search", "reserve margin"]) == 0
    searched = json.loads(capsys.readouterr().out)

    assert searched["items"]
    assert {item["ref"]["revision_id"] for item in searched["items"]} == {ingested["revision_id"]}
