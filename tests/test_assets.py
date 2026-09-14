import hashlib
import json
import sqlite3
import zipfile
from dataclasses import replace
from io import BytesIO

import pytest

from knowledge_runtime.assets import KnowledgeAsset, KnowledgeAssetStore, SQLiteKnowledgeAssetStore
from knowledge_runtime.asset_provider import AssetKnowledgeProvider
from knowledge_runtime.errors import KRStaleLocator
from knowledge_runtime.extractors import LocalExtractionBackend
from knowledge_runtime.mineru_backend import MinerUCloudBackend


def test_local_backend_creates_asset_with_source_and_parser_provenance(tmp_path):
    source = tmp_path / "guide.md"
    source.write_text("# Guide\n\nHello knowledge runtime.", encoding="utf-8")
    store = KnowledgeAssetStore(tmp_path / "assets")

    asset = LocalExtractionBackend().extract(source, store=store)

    assert asset.source_hash == hashlib.sha256(source.read_bytes()).hexdigest()
    assert asset.parser_name == "local"
    assert "Hello knowledge runtime." in asset.markdown
    assert store.get(asset.asset_id).asset_id == asset.asset_id
    assert asset.content_list[0]["text"] == "Guide"


def test_asset_identity_stays_stable_while_source_revision_changes(tmp_path):
    source = tmp_path / "guide.md"
    source.write_text("revision one", encoding="utf-8")
    backend = LocalExtractionBackend()
    first = backend.extract(source)
    source.write_text("revision two", encoding="utf-8")
    second = backend.extract(source)

    assert first.asset_id == second.asset_id
    assert first.source_hash != second.source_hash


def test_sqlite_store_persists_current_asset_and_revision_history(tmp_path):
    source = tmp_path / "guide.md"
    source.write_text("revision one", encoding="utf-8")
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    backend = LocalExtractionBackend()

    first = backend.extract(source, store=store)
    source.write_text("revision two", encoding="utf-8")
    second = backend.extract(source, store=store)

    assert store.get(first.asset_id).markdown == "revision two\n"
    assert first.asset_id == second.asset_id
    assert {item["source_hash"] for item in store.list_revisions(first.asset_id)} == {
        first.source_hash,
        second.source_hash,
    }
    store.close()


def test_sqlite_store_keeps_reparse_history_for_the_same_source_hash(tmp_path):
    source = tmp_path / "policy.md"
    source.write_text("old policy", encoding="utf-8")
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    backend = LocalExtractionBackend()
    first = backend.extract(source, store=store)
    provider = AssetKnowledgeProvider(store)
    stale_locator = provider.search("old policy").items[0].locator
    reparsed = replace(
        first,
        parser_version="local-v2",
        markdown="# Updated parse\n\nnew policy\n",
    )

    current = store.put(reparsed)
    revisions = store.list_revisions(first.asset_id)
    previous = store.get_revision(first.asset_id, first.revision_id)

    assert current.source_hash == first.source_hash
    assert current.revision_id != first.revision_id
    assert len(revisions) == 2
    assert previous.markdown == first.markdown
    assert previous.parser_version == first.parser_version
    assert store.get(first.asset_id).markdown == reparsed.markdown
    with pytest.raises(KRStaleLocator):
        provider.read(stale_locator)
    store.close()


def test_sqlite_store_migrates_v1_database_without_losing_assets_or_artifacts(tmp_path):
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE assets (
            asset_id TEXT PRIMARY KEY, source_path TEXT NOT NULL, source_name TEXT NOT NULL,
            current_source_hash TEXT NOT NULL, parser_name TEXT NOT NULL,
            parser_version TEXT NOT NULL, metadata_json TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE asset_revisions (
            asset_id TEXT NOT NULL, source_hash TEXT NOT NULL, source_path TEXT NOT NULL,
            source_name TEXT NOT NULL, parser_name TEXT NOT NULL, parser_version TEXT NOT NULL,
            markdown TEXT NOT NULL, content_list_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (asset_id, source_hash),
            FOREIGN KEY (asset_id) REFERENCES assets(asset_id) ON DELETE CASCADE
        );
        CREATE TABLE asset_artifacts (
            asset_id TEXT NOT NULL, source_hash TEXT NOT NULL, name TEXT NOT NULL,
            contents BLOB NOT NULL, PRIMARY KEY (asset_id, source_hash, name),
            FOREIGN KEY (asset_id, source_hash)
                REFERENCES asset_revisions(asset_id, source_hash) ON DELETE CASCADE
        );
        CREATE INDEX idx_asset_revisions_hash ON asset_revisions(source_hash);
        CREATE INDEX idx_asset_artifacts_asset ON asset_artifacts(asset_id, source_hash);
        """
    )
    legacy_asset = KnowledgeAsset(
        asset_id="asset-v1",
        source_path="legacy.md",
        source_name="legacy.md",
        source_hash="source-hash-v1",
        parser_name="local",
        parser_version="v1",
        markdown="Legacy parsed content.",
        content_list=[{"text": "Legacy parsed content."}],
        metadata={"origin": "v1"},
    )
    connection.execute(
        "INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (legacy_asset.asset_id, legacy_asset.source_path, legacy_asset.source_name,
         legacy_asset.source_hash, legacy_asset.parser_name, legacy_asset.parser_version,
         json.dumps(legacy_asset.metadata), "2026-01-01T00:00:00+00:00"),
    )
    connection.execute(
        "INSERT INTO asset_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (legacy_asset.asset_id, legacy_asset.source_hash, legacy_asset.source_path,
         legacy_asset.source_name, legacy_asset.parser_name, legacy_asset.parser_version,
         legacy_asset.markdown, json.dumps(legacy_asset.content_list),
         json.dumps(legacy_asset.metadata), "2026-01-01T00:00:00+00:00"),
    )
    connection.execute(
        "INSERT INTO asset_artifacts VALUES (?, ?, ?, ?)",
        (legacy_asset.asset_id, legacy_asset.source_hash, "page.png", sqlite3.Binary(b"legacy-bytes")),
    )
    connection.commit()
    connection.close()

    store = SQLiteKnowledgeAssetStore(database)

    migrated = store.get("asset-v1")
    assert migrated.markdown == "Legacy parsed content."
    assert migrated.metadata == {"origin": "v1"}
    assert store.derived_artifacts("asset-v1") == {"page.png": b"legacy-bytes"}
    assert [asset.asset_id for asset in store.search_current("Legacy parsed content")] == ["asset-v1"]
    assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    store.close()


def test_sqlite_store_is_idempotent_when_reingesting_identical_artifacts(tmp_path):
    source = tmp_path / "guide.md"
    source.write_text("Stable parsed content.", encoding="utf-8")
    asset = LocalExtractionBackend().extract(source)
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")

    first = store.put(asset, artifacts={"page.png": b"same artifact"})
    second = store.put(asset, artifacts={"page.png": b"same artifact"})

    assert second.revision_id == first.revision_id
    assert store.derived_artifacts(asset.asset_id) == {"page.png": b"same artifact"}
    assert len(store.list_revisions(asset.asset_id)) == 1
    store.close()


def test_asset_provider_refreshes_after_sqlite_reingestion(tmp_path):
    source = tmp_path / "guide.md"
    source.write_text("old policy", encoding="utf-8")
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    backend = LocalExtractionBackend()
    backend.extract(source, store=store)
    provider = AssetKnowledgeProvider(store)
    old_locator = provider.search("old policy").items[0].locator

    source.write_text("new policy", encoding="utf-8")
    backend.extract(source, store=store)

    with pytest.raises(KRStaleLocator):
        provider.read(old_locator)
    assert provider.search("new policy").items
    store.close()


def test_sqlite_fts_multiword_search_reads_all_matching_terms(tmp_path):
    source = tmp_path / "actuarial.md"
    source.write_text(
        "The actuarial estimate uses a chain ladder method.\n\nA quarterly review is required.",
        encoding="utf-8",
    )
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    LocalExtractionBackend().extract(source, store=store)
    provider = AssetKnowledgeProvider(store)

    hit = provider.search("actuarial quarterly").items[0]
    evidence = provider.read(hit.locator)

    assert "actuarial estimate" in evidence.content
    assert "quarterly review" in evidence.content
    store.close()


def test_sqlite_store_falls_back_to_literal_search_without_fts(tmp_path):
    source = tmp_path / "fallback.md"
    source.write_text("The reserve estimate is reviewed annually.", encoding="utf-8")
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    asset = LocalExtractionBackend().extract(source, store=store)
    store._fts_available = False

    results = store.search_current("reserve estimate")

    assert [item.asset_id for item in results] == [asset.asset_id]
    store.close()


def test_provider_uses_narrowest_window_when_query_terms_are_repeated():
    window = AssetKnowledgeProvider._best_match_window(
        [
            "models appear in an earlier section",
            "unrelated discussion",
            "models are evaluated",
            "when performing",
            "professional services",
            "unrelated conclusion",
        ],
        "models when performing professional services",
    )

    assert window == (2, 6)


def test_provider_reads_a_compact_window_that_covers_multi_part_answers():
    lines = [
        "2.2 Structure",
        "The participants are assessed from two",
        "perspectives:",
        "capital (Own Funds and Solvency Capital Requirement)",
        "Capital calculations use the prescribed stress scenario.",
        "Results are compared against the baseline position.",
        "Liquidity assumptions are documented separately.",
        "liquidity, based on a hybrid stocks and flows assessment.",
        "The two components use a common narrative.",
    ]

    start, end = AssetKnowledgeProvider._best_match_window(
        lines, "two perspectives capital liquidity"
    )
    selected = " ".join(lines[start - 1 : end]).casefold()

    assert "perspectives" in selected
    assert "capital" in selected
    assert "liquidity" in selected
    assert end - start <= 12


def test_provider_prefers_specific_scope_passage_over_long_article_preamble():
    lines = [
        "Models and modeling applications have increased in actuarial science.",
        "Models are reflected in financial statements and other actuarial work.",
        "The committee began work on a modeling standard for actuaries.",
        "A draft discussed models used by actuaries in several practice areas.",
        "The scope might be expanded to all practice areas.",
        "The task force considered models and comments received.",
        "1.2 Scope — This standard applies to actuaries in any practice ar ea when performing actuarial",
        "services with respect to designing, developing, selecting, modifying, or using all types of",
        "models.",
    ]

    start, end = AssetKnowledgeProvider._best_match_window(lines, "models any practice area scope")
    selected = " ".join(lines[start - 1 : end]).casefold()

    assert "this standard applies to actuaries" in selected
    assert "all types of" in selected


def test_sqlite_search_handles_natural_question_and_returns_compact_evidence(tmp_path):
    target = tmp_path / "asop-23-data-quality.md"
    target.write_text(
        "# ASOP No. 23 — Data Quality\n\n"
        "When actuaries rely on data supplied by others, they should assess data quality.\n",
        encoding="utf-8",
    )
    distractor = tmp_path / "asop-56-modeling.md"
    distractor.write_text(
        "# ASOP No. 56 — Modeling\n\n"
        "The appendix discusses models and references ASOP No. 23.\n"
        "It also mentions an actuary, data, another party, and estimates.\n",
        encoding="utf-8",
    )
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    backend = LocalExtractionBackend()
    backend.extract(target, store=store)
    backend.extract(distractor, store=store)
    provider = AssetKnowledgeProvider(store)

    hit = provider.search(
        "What does ASOP No. 23 cover when an actuary uses data supplied by another party?"
    ).items[0]
    evidence = provider.read(hit.locator)

    assert hit.display_name == target.name
    assert "data supplied by others" in evidence.content
    assert len(evidence.content.encode("utf-8")) < 1_000
    store.close()


class FakeTransport:
    def __init__(self):
        self.calls = []
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr("full.md", "# Parsed\n\nCloud extracted text.")
            archive.writestr("content_list.json", "[]")
        self.zip_data = zip_buffer.getvalue()

    def request(self, method, url, *, headers=None, json_body=None, data=None):
        self.calls.append((method, url, headers, json_body, data))
        if url.endswith("/api/v4/file-urls/batch"):
            return {"status": 200, "json": {"code": 0, "data": {"batch_id": "batch-1", "file_urls": ["https://upload.test/file"]}}}
        if url == "https://upload.test/file":
            return {"status": 200, "content": b""}
        if url.endswith("/api/v4/extract-results/batch/batch-1"):
            return {
                "status": 200,
                "json": {
                    "code": 0,
                    "data": {
                        "extract_result": [
                            {"state": "done", "full_zip_url": "https://download.test/result.zip", "data_id": "doc-1"}
                        ]
                    },
                },
            }
        if url == "https://download.test/result.zip":
            return {"status": 200, "content": self.zip_data}
        raise AssertionError(f"unexpected request {method} {url}")


def test_mineru_backend_uploads_polls_and_persists_derived_assets(tmp_path):
    source = tmp_path / "contract.pdf"
    source.write_bytes(b"fake pdf bytes")
    transport = FakeTransport()
    store = KnowledgeAssetStore(tmp_path / "assets")
    backend = MinerUCloudBackend(api_key="rotated-test-token", transport=transport, poll_interval=0)

    asset = backend.extract(source, store=store)

    assert asset.parser_name == "mineru-cloud"
    assert asset.parser_version == "vlm"
    assert "Cloud extracted text." in asset.markdown
    assert len(transport.calls) == 4
    submit = transport.calls[0]
    assert submit[2]["Authorization"] == "Bearer rotated-test-token"
    assert json.loads(json.dumps(submit[3]))["files"][0]["name"] == "contract.pdf"
    assert submit[3]["files"][0]["is_ocr"] is False


def test_mineru_backend_selects_html_model_for_html_sources(tmp_path):
    source = tmp_path / "page.html"
    source.write_text("<h1>HTML</h1>", encoding="utf-8")
    transport = FakeTransport()
    backend = MinerUCloudBackend(api_key="test-token", transport=transport, poll_interval=0)

    asset = backend.extract(source)

    assert transport.calls[0][3]["model_version"] == "MinerU-HTML"
    assert asset.parser_version == "MinerU-HTML"
