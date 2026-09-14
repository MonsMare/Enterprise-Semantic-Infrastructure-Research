import hashlib
import json
import sqlite3
import zipfile
from dataclasses import replace
from io import BytesIO

import pytest

import knowledge_runtime.assets as assets_module
from knowledge_runtime.assets import KnowledgeAsset, KnowledgeAssetStore, SQLiteKnowledgeAssetStore
from knowledge_runtime.asset_provider import AssetKnowledgeProvider
from knowledge_runtime.errors import KRStaleLocator
from knowledge_runtime.extractors import LocalExtractionBackend
from knowledge_runtime.mineru_backend import MinerUCloudBackend
from knowledge_runtime.models import SearchOptions
from knowledge_runtime.retrieval import LocalNgramEncoder, SparseVector


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


def test_sqlite_store_indexes_markdown_into_revisioned_heading_chunks(tmp_path):
    source = tmp_path / "guide.md"
    source.write_text(
        "# Guide\n\nIntro paragraph.\n\n## Details\n\nDetail paragraph.\n",
        encoding="utf-8",
    )
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")

    asset = LocalExtractionBackend().extract(source, store=store)

    chunks = store.list_current_chunks()

    assert [chunk.asset_id for chunk in chunks] == [asset.asset_id, asset.asset_id]
    assert [chunk.revision_id for chunk in chunks] == [asset.revision_id, asset.revision_id]
    assert [chunk.heading_path for chunk in chunks] == [("Guide",), ("Guide", "Details")]
    assert [(chunk.start_line, chunk.end_line) for chunk in chunks] == [(1, 3), (5, 7)]
    assert [chunk.text for chunk in chunks] == [
        "# Guide\n\nIntro paragraph.",
        "## Details\n\nDetail paragraph.",
    ]
    store.close()


def test_sqlite_store_searches_current_chunks_and_honors_asset_scope(tmp_path):
    first_source = tmp_path / "first.md"
    first_source.write_text(
        "# First\n\nA shared phrase appears here.\n",
        encoding="utf-8",
    )
    second_source = tmp_path / "second.md"
    second_source.write_text(
        "# Second\n\nA shared phrase appears there.\n",
        encoding="utf-8",
    )
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    backend = LocalExtractionBackend()
    first = backend.extract(first_source, store=store)
    backend.extract(second_source, store=store)

    results = store.search_chunks("shared phrase", scope=first.asset_id, limit=10)

    assert len(results) == 1
    chunk, score = results[0]
    assert chunk.asset_id == first.asset_id
    assert "shared phrase" in chunk.text
    assert isinstance(score, float)
    store.close()


def test_sqlite_store_persists_revision_scoped_local_chunk_vectors(tmp_path):
    source = tmp_path / "policy.md"
    source.write_text("# Policy\n\nAnnual reserve review is required.", encoding="utf-8")
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    backend = LocalExtractionBackend()
    first = backend.extract(source, store=store)
    first_chunks = store.list_current_chunks_with_vectors(
        encoder_id=LocalNgramEncoder().encoder_id,
    )
    first_vector = first_chunks[0][1]

    source.write_text("# Policy\n\nQuarterly capital review is required.", encoding="utf-8")
    second = backend.extract(source, store=store)
    current = store.list_current_chunks_with_vectors(
        encoder_id=LocalNgramEncoder().encoder_id,
    )

    assert isinstance(first_vector, SparseVector)
    assert first_chunks[0][0].revision_id == first.revision_id
    assert len(current) == 1
    assert current[0][0].revision_id == second.revision_id
    assert current[0][1] != first_vector
    assert store.connection.execute("SELECT count(*) FROM asset_chunk_vectors").fetchone()[0] == 2
    store.close()


def test_sqlite_store_backfills_local_vectors_when_opening_v2_database(tmp_path):
    source = tmp_path / "policy.md"
    source.write_text("# Policy\n\nQuarterly reserve review is required.", encoding="utf-8")
    database = tmp_path / "knowledge.db"
    store = SQLiteKnowledgeAssetStore(database)
    LocalExtractionBackend().extract(source, store=store)
    store.connection.execute("DROP TABLE asset_chunk_vectors")
    store.connection.execute("PRAGMA user_version = 2")
    store.close()

    upgraded = SQLiteKnowledgeAssetStore(database)

    rows = upgraded.list_current_chunks_with_vectors(
        encoder_id=LocalNgramEncoder().encoder_id,
    )
    assert len(rows) == 1
    assert rows[0][1].values
    assert upgraded.connection.execute("PRAGMA user_version").fetchone()[0] == 6
    upgraded.close()


def test_asset_provider_caches_persisted_local_vectors_across_queries(tmp_path):
    source = tmp_path / "policy.md"
    source.write_text(
        "# Policy\n\nAnnual reserve review is required.\n\nCapital review is documented.",
        encoding="utf-8",
    )
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    LocalExtractionBackend().extract(source, store=store)
    original = store.list_current_chunks_with_vectors
    vector_loads = 0

    def count_vector_loads(**kwargs):
        nonlocal vector_loads
        vector_loads += 1
        return original(**kwargs)

    store.list_current_chunks_with_vectors = count_vector_loads
    provider = AssetKnowledgeProvider(store)
    provider.search("reserve review")
    provider.search("capital review")

    assert vector_loads == 1
    store.close()


def test_asset_provider_finds_morphological_match_without_fts_token_overlap(tmp_path):
    source = tmp_path / "financial-condition.md"
    source.write_text(
        "# Financial condition\n\nSolvencies are assessed at the end of each year.",
        encoding="utf-8",
    )
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    LocalExtractionBackend().extract(source, store=store)
    provider = AssetKnowledgeProvider(store)

    assert store.search_chunks("solvency", scope=None, limit=10) == []
    semantic_candidates = store.search_chunk_vectors(
        LocalNgramEncoder().encode("solvency"),
        encoder_id=LocalNgramEncoder().encoder_id,
        scope=None,
        limit=10,
    )
    assert semantic_candidates
    assert semantic_candidates[0][0].source_name == source.name
    hits = provider.search("solvency", options=SearchOptions(limit=10)).items

    assert hits
    assert hits[0].display_name == source.name
    assert "Solvencies" in provider.read(hits[0].locator).content
    store.close()


def test_asset_provider_rejects_local_hash_collision_only_candidates(tmp_path):
    source = tmp_path / "actuarial-guide.md"
    source.write_text(
        "# Actuarial guide\n\nA point estimate and probability distribution support reserve review.",
        encoding="utf-8",
    )
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    LocalExtractionBackend().extract(source, store=store)

    hits = AssetKnowledgeProvider(store).search(
        "那四种？",
        options=SearchOptions(limit=10),
    ).items

    assert hits == []
    store.close()


def test_asset_provider_preserves_semantic_only_recall_for_large_stores(tmp_path):
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    backend = LocalExtractionBackend()
    for index in range(205):
        source = tmp_path / f"policy-{index:03d}.md"
        body = "Portfolio solvency cushion." if index == 204 else f"Policy note {index}."
        source.write_text(f"# Policy {index}\n\n{body}", encoding="utf-8")
        backend.extract(source, store=store)

    class CountingEncoder:
        def __init__(self):
            self.inputs = []

        def encode(self, text):
            self.inputs.append(text)
            if text == "How are future claims protected?" or "solvency cushion" in text:
                return (1.0, 0.0)
            return (0.0, 1.0)

    encoder = CountingEncoder()
    hits = AssetKnowledgeProvider(store, semantic_encoder=encoder).search(
        "How are future claims protected?",
        options=SearchOptions(limit=10, semantic_weight=1.0),
    ).items

    assert hits
    assert hits[0].display_name == "policy-204.md"
    assert len(encoder.inputs) > 201
    store.close()


def test_asset_provider_falls_back_when_local_encoder_dimension_is_not_indexed(tmp_path):
    source = tmp_path / "financial-condition.md"
    source.write_text(
        "# Financial condition\n\nSolvencies are assessed at the end of each year.",
        encoding="utf-8",
    )
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    LocalExtractionBackend().extract(source, store=store)

    hits = AssetKnowledgeProvider(
        store,
        semantic_encoder=LocalNgramEncoder(dimensions=4096),
    ).search("solvency", options=SearchOptions(limit=10)).items

    assert hits
    assert hits[0].display_name == source.name
    store.close()


def test_asset_provider_case_sensitive_search_requires_the_complete_query(tmp_path):
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    backend = LocalExtractionBackend()
    for name, content in (
        ("standard-23.md", "# ASOP 23\n\nData quality guidance."),
        ("standard-43.md", "# ASOP 43\n\nUnpaid claim estimates."),
    ):
        source = tmp_path / name
        source.write_text(content, encoding="utf-8")
        backend.extract(source, store=store)

    hits = AssetKnowledgeProvider(store).search(
        "ASOP 43",
        options=SearchOptions(limit=10, case_sensitive=True),
    ).items

    assert [hit.display_name for hit in hits] == ["standard-43.md"]
    separated_words = AssetKnowledgeProvider(store).search(
        "ASOP 43 estimates",
        options=SearchOptions(limit=10, case_sensitive=True),
    ).items
    # case_sensitive retains MemoryProvider's literal-substring contract; FTS
    # may find individual terms in separate locations, but the post-filter must
    # not turn that into a case-sensitive multi-term search.
    assert separated_words == []
    store.close()


def test_asset_provider_uses_heading_weight_for_lexical_ranking(tmp_path):
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    backend = LocalExtractionBackend()
    title_source = tmp_path / "heading.md"
    title_source.write_text("# Precision marker\n\nBackground material only.", encoding="utf-8")
    backend.extract(title_source, store=store)
    body_source = tmp_path / "other.md"
    body_source.write_text(
        "# Other\n\n" + "precision " * 30,
        encoding="utf-8",
    )
    backend.extract(body_source, store=store)

    hits = AssetKnowledgeProvider(store).search(
        "precision",
        options=SearchOptions(limit=10, semantic_weight=0.0),
    ).items

    assert hits[0].display_name == title_source.name
    store.close()


def test_sqlite_store_does_not_rebuild_healthy_chunk_fts_on_open(tmp_path, monkeypatch):
    source = tmp_path / "guide.md"
    source.write_text("# Guide\n\nStable searchable content.", encoding="utf-8")
    database = tmp_path / "knowledge.db"
    store = SQLiteKnowledgeAssetStore(database)
    LocalExtractionBackend().extract(source, store=store)
    store.close()

    statements = []
    original_connect = assets_module.sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(assets_module.sqlite3, "connect", traced_connect)
    reopened = SQLiteKnowledgeAssetStore(database)

    assert not any("DELETE FROM chunk_fts" in statement for statement in statements)
    assert reopened.search_chunks("searchable content", scope=None, limit=10)
    reopened.close()


def test_sqlite_vector_search_does_not_depend_on_large_sql_bind_limit(tmp_path):
    source = tmp_path / "long-policy.md"
    source.write_text(
        "# Policy\n\n" + " ".join(f"specialterm{index}" for index in range(180)),
        encoding="utf-8",
    )
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    LocalExtractionBackend().extract(source, store=store)
    old_limit = store.connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 32)
    try:
        hits = store.search_chunk_vectors(
            LocalNgramEncoder().encode(" ".join(f"specialterm{index}" for index in range(180))),
            encoder_id=LocalNgramEncoder().encoder_id,
            scope=None,
            limit=10,
        )
    finally:
        store.connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, old_limit)

    assert hits
    assert hits[0][0].source_name == source.name
    store.close()


def test_sqlite_v5_posting_table_is_removed_during_schema_upgrade(tmp_path):
    source = tmp_path / "policy.md"
    source.write_text("# Policy\n\nStable current evidence.", encoding="utf-8")
    database = tmp_path / "knowledge.db"
    store = SQLiteKnowledgeAssetStore(database)
    asset = LocalExtractionBackend().extract(source, store=store)
    chunk = store.list_current_chunks(asset.asset_id)[0]
    store.connection.execute(
        """CREATE TABLE asset_chunk_vector_terms (
            chunk_id TEXT NOT NULL,
            encoder_id TEXT NOT NULL,
            coordinate INTEGER NOT NULL,
            weight REAL NOT NULL,
            PRIMARY KEY (chunk_id, encoder_id, coordinate)
        )"""
    )
    store.connection.execute(
        "INSERT INTO asset_chunk_vector_terms VALUES (?, ?, ?, ?)",
        (chunk.chunk_id, LocalNgramEncoder().encoder_id, 1, 0.5),
    )
    store.connection.commit()
    store.connection.execute("PRAGMA user_version = 5")
    store.connection.commit()
    store.close()

    upgraded = SQLiteKnowledgeAssetStore(database)

    assert upgraded.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='asset_chunk_vector_terms'"
    ).fetchone() is None
    assert upgraded.connection.execute("PRAGMA user_version").fetchone()[0] == 6
    assert len(
        upgraded.list_current_chunks_with_vectors(
            encoder_id=LocalNgramEncoder().encoder_id,
        )
    ) == 1
    upgraded.close()


def test_asset_provider_returns_chunk_locators_and_chunk_evidence(tmp_path):
    source = tmp_path / "guide.md"
    source.write_text(
        "# Guide\n\nIntroductory material.\n\n"
        "## Payment rule\n\nAnnual review preserves the reserve evidence.\n",
        encoding="utf-8",
    )
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    asset = LocalExtractionBackend().extract(source, store=store)
    provider = AssetKnowledgeProvider(store)
    expected_chunk = next(
        chunk for chunk in store.list_current_chunks(asset.asset_id)
        if "Annual review" in chunk.text
    )

    hit = provider.search("reserve evidence").items[0]

    assert hit.locator.resource_id == asset.asset_id
    assert hit.locator.selector == {
        "type": "chunk",
        "id": expected_chunk.chunk_id,
        "start": expected_chunk.start_line,
        "end": expected_chunk.end_line,
    }
    evidence = provider.read(hit.locator)
    assert evidence.content == expected_chunk.text
    assert evidence.resolved_selector == hit.locator.selector
    assert evidence.derived_from == asset.asset_id
    assert evidence.source_label == source.name
    assert provider.stat(hit.locator)["size_bytes"] == len(expected_chunk.text.encode("utf-8"))
    store.close()


@pytest.mark.parametrize("scope_field", ["asset_id", "source_name", "source_path"])
def test_asset_provider_scoped_multiterm_search_stays_in_sqlite_chunks(tmp_path, scope_field):
    target_source = tmp_path / "target-policy.md"
    target_source.write_text(
        "# Target policy\n\nThe reserve is documented here.\n\n"
        "Annual review is required later.\n",
        encoding="utf-8",
    )
    other_source = tmp_path / "other-policy.md"
    other_source.write_text(
        "# Other policy\n\nThe reserve schedule includes an annual review.\n",
        encoding="utf-8",
    )
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    backend = LocalExtractionBackend()
    target = backend.extract(target_source, store=store)
    backend.extract(other_source, store=store)
    provider = AssetKnowledgeProvider(store)
    scope = {
        "asset_id": target.asset_id,
        "source_name": target.source_name,
        "source_path": target.source_path,
    }[scope_field]

    page = provider.search("reserve annual", scope=scope, options=SearchOptions(limit=10))

    assert page.items
    assert all(hit.locator.resource_id == target.asset_id for hit in page.items)
    assert any("Annual review" in provider.read(hit.locator).content for hit in page.items)
    assert all(hit.locator.selector["type"] == "chunk" for hit in page.items)
    store.close()


def test_asset_provider_fuses_local_ngram_rank_with_fts_rank(tmp_path):
    lexical_source = tmp_path / "solvency-solvency-solvency.md"
    lexical_source.write_text(
        "# Archive\n\nThe old record is retained.",
        encoding="utf-8",
    )
    semantic_source = tmp_path / "target.md"
    semantic_source.write_text(
        "# Target\n\nSolvencies and claims are reviewed together.",
        encoding="utf-8",
    )
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    backend = LocalExtractionBackend()
    backend.extract(lexical_source, store=store)
    backend.extract(semantic_source, store=store)
    provider = AssetKnowledgeProvider(store)
    query = "solvency claims"

    lexical_hits = provider.search(
        query,
        options=SearchOptions(limit=10, semantic_weight=0.0),
    ).items
    hybrid_hits = provider.search(
        query,
        options=SearchOptions(limit=10, semantic_weight=0.8),
    ).items

    assert lexical_hits[0].display_name == lexical_source.name
    assert hybrid_hits[0].display_name == semantic_source.name
    assert [hit.locator.selector["id"] for hit in hybrid_hits] == [
        hit.locator.selector["id"]
        for hit in provider.search(
            query,
            options=SearchOptions(limit=10, semantic_weight=0.8),
        ).items
    ]
    store.close()


def test_asset_provider_accepts_an_injected_semantic_encoder(tmp_path):
    lexical_source = tmp_path / "lexical.md"
    lexical_source.write_text("# Lexical\n\nQuery marker appears here.", encoding="utf-8")
    semantic_source = tmp_path / "semantic.md"
    semantic_source.write_text("# Semantic\n\nSemantic target phrase is related.", encoding="utf-8")
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    backend = LocalExtractionBackend()
    backend.extract(lexical_source, store=store)
    backend.extract(semantic_source, store=store)

    class MarkerEncoder:
        def __init__(self):
            self.inputs = []

        def encode(self, text):
            self.inputs.append(text)
            if text.casefold().strip() == "query marker" or "semantic target phrase" in text.casefold():
                return (1.0, 0.0)
            return (0.0, 1.0)

    encoder = MarkerEncoder()
    try:
        provider = AssetKnowledgeProvider(store, semantic_encoder=encoder)
    except TypeError as exc:
        pytest.fail(f"asset provider must allow an injected semantic encoder: {exc}")

    hits = provider.search(
        "query marker",
        options=SearchOptions(limit=10, semantic_weight=0.8),
    ).items

    assert hits[0].display_name == semantic_source.name
    assert "query marker" in encoder.inputs
    assert any("semantic target phrase" in item.casefold() for item in encoder.inputs)

    semantic_only_hits = provider.search(
        "query marker",
        options=SearchOptions(limit=10, semantic_weight=1.0),
    ).items
    assert [hit.display_name for hit in semantic_only_hits] == [semantic_source.name]
    store.close()


def test_sqlite_store_keeps_old_revision_chunks_out_of_current_projection(tmp_path):
    source = tmp_path / "policy.md"
    source.write_text("# Policy\n\nOld rule.\n", encoding="utf-8")
    store = SQLiteKnowledgeAssetStore(tmp_path / "knowledge.db")
    backend = LocalExtractionBackend()
    first = backend.extract(source, store=store)
    source.write_text("# Policy\n\nNew rule.\n", encoding="utf-8")
    second = backend.extract(source, store=store)

    current = store.list_current_chunks()
    retained = store.connection.execute(
        "SELECT count(*) FROM asset_chunks WHERE asset_id = ? AND revision_id = ?",
        (first.asset_id, first.revision_id),
    ).fetchone()[0]

    assert [chunk.revision_id for chunk in current] == [second.revision_id]
    assert "New rule." in current[0].text
    assert retained == 1
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
    assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 6
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

    hits = provider.search("actuarial quarterly").items
    evidence = [provider.read(hit.locator).content for hit in hits]

    assert any("actuarial estimate" in item for item in evidence)
    assert any("quarterly review" in item for item in evidence)
    assert all(hit.locator.selector["type"] == "chunk" for hit in hits)
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
