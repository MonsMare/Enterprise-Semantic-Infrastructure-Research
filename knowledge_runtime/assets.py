from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import KRNotFound, KRQueryInvalid


@dataclass(frozen=True)
class KnowledgeAsset:
    asset_id: str
    source_path: str
    source_name: str
    source_hash: str
    parser_name: str
    parser_version: str
    markdown: str
    content_list: Any = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    revision_id: str | None = None


class KnowledgeAssetStore:
    """Local store for disposable parsed projections and provenance manifests."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, asset: KnowledgeAsset, *, artifacts: dict[str, bytes] | None = None) -> KnowledgeAsset:
        directory = self.root / asset.asset_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "full.md").write_text(asset.markdown, encoding="utf-8")
        (directory / "content_list.json").write_text(
            json.dumps(asset.content_list, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for name, contents in (artifacts or {}).items():
            safe_name = Path(name).name
            (directory / safe_name).write_bytes(contents)
        manifest = asdict(asset)
        manifest["content_list"] = None
        manifest["markdown"] = None
        (directory / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return asset

    def get(self, asset_id: str) -> KnowledgeAsset:
        directory = self.root / asset_id
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            raise KRNotFound(asset_id)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return KnowledgeAsset(
            **{
                **manifest,
                "markdown": (directory / "full.md").read_text(encoding="utf-8"),
                "content_list": json.loads((directory / "content_list.json").read_text(encoding="utf-8")),
            }
        )

    def list_assets(self) -> list[KnowledgeAsset]:
        assets: list[KnowledgeAsset] = []
        for manifest in sorted(self.root.glob("*/manifest.json")):
            assets.append(self.get(manifest.parent.name))
        return assets

    def derived_artifacts(self, asset_id: str) -> dict[str, bytes]:
        directory = self.root / asset_id
        return {
            path.name: path.read_bytes()
            for path in directory.iterdir()
            if path.is_file() and path.name not in {"manifest.json", "full.md", "content_list.json"}
        }


class SQLiteKnowledgeAssetStore:
    """Transactional SQLite-backed asset store for operational knowledge management.

    The database keeps the current asset pointer and every ingested source revision.
    Markdown and structure stay queryable without requiring the original file to be
    present; the source path and source hash remain part of the provenance record.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), timeout=30.0, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self._lock = threading.RLock()
        self._initialize()

    def _initialize(self) -> None:
        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "assets" in tables and self.connection.execute("PRAGMA user_version").fetchone()[0] < 2:
            self._migrate_legacy_schema()
        self._create_schema()
        try:
            self.connection.execute(
                """CREATE VIRTUAL TABLE IF NOT EXISTS asset_fts USING fts5(
                    asset_id UNINDEXED, source_name, markdown
                )"""
            )
            self._fts_available = True
            self._backfill_fts_if_needed()
        except sqlite3.OperationalError:
            self._fts_available = False
        with self.connection:
            self.connection.execute("PRAGMA user_version = 2")

    def _backfill_fts_if_needed(self) -> None:
        """Restore the current-revision FTS projection after schema upgrades.

        Legacy databases deliberately drop their FTS table before migrating the
        revision schema.  A newly-created table is therefore empty even though
        the asset rows were copied successfully.  Rebuild only when the row
        counts differ so opening a healthy database does not rewrite the index
        on every process start.
        """
        indexed_count = self.connection.execute("SELECT count(*) FROM asset_fts").fetchone()[0]
        asset_count = self.connection.execute("SELECT count(*) FROM assets").fetchone()[0]
        if indexed_count == asset_count:
            return
        with self.connection:
            self.connection.execute("DELETE FROM asset_fts")
            self.connection.execute(
                """
                INSERT INTO asset_fts(asset_id, source_name, markdown)
                SELECT a.asset_id, r.source_name, r.markdown
                FROM assets a
                JOIN asset_revisions r
                  ON r.asset_id = a.asset_id AND r.revision_id = a.current_revision_id
                """
            )

    def _create_schema(self) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS assets (
                asset_id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                source_name TEXT NOT NULL,
                current_source_hash TEXT NOT NULL,
                current_revision_id TEXT NOT NULL,
                parser_name TEXT NOT NULL,
                parser_version TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS asset_revisions (
                asset_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_name TEXT NOT NULL,
                parser_name TEXT NOT NULL,
                parser_version TEXT NOT NULL,
                markdown TEXT NOT NULL,
                content_list_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (asset_id, revision_id),
                FOREIGN KEY (asset_id) REFERENCES assets(asset_id) ON DELETE CASCADE
            )""",
            """CREATE TABLE IF NOT EXISTS asset_artifacts (
                asset_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                name TEXT NOT NULL,
                contents BLOB NOT NULL,
                PRIMARY KEY (asset_id, revision_id, name),
                FOREIGN KEY (asset_id, revision_id)
                    REFERENCES asset_revisions(asset_id, revision_id) ON DELETE CASCADE
            )""",
            "CREATE INDEX IF NOT EXISTS idx_asset_revisions_hash ON asset_revisions(source_hash)",
            "CREATE INDEX IF NOT EXISTS idx_asset_artifacts_asset ON asset_artifacts(asset_id, revision_id)",
        )
        with self.connection:
            for statement in statements:
                self.connection.execute(statement)

    @staticmethod
    def _revision_identifier(
        asset_id: str,
        source_hash: str,
        source_path: str,
        source_name: str,
        parser_name: str,
        parser_version: str,
        markdown: str,
        content_list_json: str,
        metadata_json: str,
        artifact_hashes: list[tuple[str, str]] | None = None,
    ) -> str:
        identity = json.dumps(
            [
                asset_id,
                source_hash,
                source_path,
                source_name,
                parser_name,
                parser_version,
                hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
                hashlib.sha256(content_list_json.encode("utf-8")).hexdigest(),
                hashlib.sha256(metadata_json.encode("utf-8")).hexdigest(),
                sorted(artifact_hashes or []),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _migrate_legacy_schema(self) -> None:
        """Upgrade v1 databases, whose revision key only tracked source bytes."""
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            with self.connection:
                self.connection.execute("DROP TABLE IF EXISTS asset_fts")
                self.connection.execute("DROP INDEX IF EXISTS idx_asset_revisions_hash")
                self.connection.execute("DROP INDEX IF EXISTS idx_asset_artifacts_asset")
                self.connection.execute("ALTER TABLE asset_artifacts RENAME TO asset_artifacts_v1")
                self.connection.execute("ALTER TABLE asset_revisions RENAME TO asset_revisions_v1")
                self.connection.execute("ALTER TABLE assets RENAME TO assets_v1")
                self.connection.execute("PRAGMA user_version = 0")
            self._create_schema()
            with self.connection:
                revision_map: dict[tuple[str, str], str] = {}
                for row in self.connection.execute("SELECT * FROM assets_v1").fetchall():
                    revision_rows = self.connection.execute(
                        "SELECT * FROM asset_revisions_v1 WHERE asset_id = ?",
                        (row["asset_id"],),
                    ).fetchall()
                    for revision in revision_rows:
                        revision_id = self._revision_identifier(
                            revision["asset_id"],
                            revision["source_hash"],
                            revision["source_path"],
                            revision["source_name"],
                            revision["parser_name"],
                            revision["parser_version"],
                            revision["markdown"],
                            revision["content_list_json"],
                            revision["metadata_json"],
                        )
                        revision_map[(revision["asset_id"], revision["source_hash"])] = revision_id
                        self.connection.execute(
                            """INSERT INTO asset_revisions (
                                asset_id, revision_id, source_hash, source_path, source_name,
                                parser_name, parser_version, markdown, content_list_json,
                                metadata_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                revision["asset_id"], revision_id, revision["source_hash"],
                                revision["source_path"], revision["source_name"],
                                revision["parser_name"], revision["parser_version"],
                                revision["markdown"], revision["content_list_json"],
                                revision["metadata_json"], revision["created_at"],
                            ),
                        )
                    current_revision_id = revision_map.get(
                        (row["asset_id"], row["current_source_hash"])
                    )
                    if current_revision_id is None:
                        continue
                    self.connection.execute(
                        """INSERT INTO assets (
                            asset_id, source_path, source_name, current_source_hash,
                            current_revision_id, parser_name, parser_version,
                            metadata_json, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            row["asset_id"], row["source_path"], row["source_name"],
                            row["current_source_hash"], current_revision_id,
                            row["parser_name"], row["parser_version"],
                            row["metadata_json"], row["updated_at"],
                        ),
                    )
                for artifact in self.connection.execute("SELECT * FROM asset_artifacts_v1").fetchall():
                    revision_id = revision_map.get((artifact["asset_id"], artifact["source_hash"]))
                    if revision_id is not None:
                        self.connection.execute(
                            "INSERT INTO asset_artifacts(asset_id, revision_id, name, contents) VALUES (?, ?, ?, ?)",
                            (artifact["asset_id"], revision_id, artifact["name"], artifact["contents"]),
                        )
                self.connection.execute("DROP TABLE asset_artifacts_v1")
                self.connection.execute("DROP TABLE asset_revisions_v1")
                self.connection.execute("DROP TABLE assets_v1")
                self.connection.execute("PRAGMA user_version = 2")
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def put(self, asset: KnowledgeAsset, *, artifacts: dict[str, bytes] | None = None) -> KnowledgeAsset:
        now = self._now()
        metadata_json = json.dumps(asset.metadata, ensure_ascii=False, sort_keys=True)
        content_list_json = json.dumps(asset.content_list, ensure_ascii=False, sort_keys=True)
        artifact_values = {
            Path(name).name: contents for name, contents in (artifacts or {}).items()
        }
        revision_id = self._revision_identifier(
            asset.asset_id,
            asset.source_hash,
            asset.source_path,
            asset.source_name,
            asset.parser_name,
            asset.parser_version,
            asset.markdown,
            content_list_json,
            metadata_json,
            [
                (name, hashlib.sha256(contents).hexdigest())
                for name, contents in artifact_values.items()
            ],
        )
        persisted_asset = replace(asset, revision_id=revision_id)
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO assets (
                    asset_id, source_path, source_name, current_source_hash, current_revision_id,
                    parser_name, parser_version, metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET
                    source_path = excluded.source_path,
                    source_name = excluded.source_name,
                    current_source_hash = excluded.current_source_hash,
                    current_revision_id = excluded.current_revision_id,
                    parser_name = excluded.parser_name,
                    parser_version = excluded.parser_version,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    asset.asset_id,
                    asset.source_path,
                    asset.source_name,
                    asset.source_hash,
                    revision_id,
                    asset.parser_name,
                    asset.parser_version,
                    metadata_json,
                    now,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO asset_revisions (
                    asset_id, revision_id, source_hash, source_path, source_name,
                    parser_name, parser_version, markdown, content_list_json,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_id, revision_id) DO NOTHING
                """,
                (
                    asset.asset_id,
                    revision_id,
                    asset.source_hash,
                    asset.source_path,
                    asset.source_name,
                    asset.parser_name,
                    asset.parser_version,
                    asset.markdown,
                    content_list_json,
                    metadata_json,
                    now,
                ),
            )
            if artifacts is not None:
                self.connection.executemany(
                    "INSERT OR IGNORE INTO asset_artifacts(asset_id, revision_id, name, contents) VALUES (?, ?, ?, ?)",
                    [
                        (asset.asset_id, revision_id, name, sqlite3.Binary(contents))
                        for name, contents in artifact_values.items()
                    ],
                )
            if self._fts_available:
                self.connection.execute("DELETE FROM asset_fts WHERE asset_id = ?", (asset.asset_id,))
                self.connection.execute(
                    "INSERT INTO asset_fts(asset_id, source_name, markdown) VALUES (?, ?, ?)",
                    (asset.asset_id, asset.source_name, asset.markdown),
                )
        return persisted_asset

    @staticmethod
    def _asset_from_row(row: sqlite3.Row) -> KnowledgeAsset:
        return KnowledgeAsset(
            asset_id=row["asset_id"],
            source_path=row["source_path"],
            source_name=row["source_name"],
            source_hash=row["source_hash"],
            parser_name=row["parser_name"],
            parser_version=row["parser_version"],
            markdown=row["markdown"],
            content_list=json.loads(row["content_list_json"]),
            metadata=json.loads(row["metadata_json"]),
            revision_id=row["revision_id"],
        )

    def get(self, asset_id: str) -> KnowledgeAsset:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT r.asset_id, r.revision_id, r.source_hash, r.source_path, r.source_name,
                       r.parser_name, r.parser_version, r.markdown,
                       r.content_list_json, r.metadata_json
                FROM assets a
                JOIN asset_revisions r
                  ON r.asset_id = a.asset_id AND r.revision_id = a.current_revision_id
                WHERE a.asset_id = ?
                """,
                (asset_id,),
            ).fetchone()
        if row is None:
            raise KRNotFound(asset_id)
        return self._asset_from_row(row)

    def get_revision(self, asset_id: str, revision_id: str) -> KnowledgeAsset:
        with self._lock:
            row = self.connection.execute(
                """SELECT asset_id, revision_id, source_hash, source_path, source_name,
                          parser_name, parser_version, markdown, content_list_json, metadata_json
                   FROM asset_revisions WHERE asset_id = ? AND revision_id = ?""",
                (asset_id, revision_id),
            ).fetchone()
        if row is None:
            raise KRNotFound(f"{asset_id}@{revision_id}")
        return self._asset_from_row(row)

    def list_assets(self) -> list[KnowledgeAsset]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT r.asset_id, r.revision_id, r.source_hash, r.source_path, r.source_name,
                       r.parser_name, r.parser_version, r.markdown,
                       r.content_list_json, r.metadata_json
                FROM assets a
                JOIN asset_revisions r
                  ON r.asset_id = a.asset_id AND r.revision_id = a.current_revision_id
                ORDER BY lower(r.source_name), r.asset_id
                """
            ).fetchall()
        return [self._asset_from_row(row) for row in rows]

    def list_revisions(self, asset_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT asset_id, revision_id, source_hash, source_path, source_name,
                       parser_name, parser_version, created_at
                FROM asset_revisions
                WHERE asset_id = ?
                ORDER BY created_at DESC, revision_id
                """,
                (asset_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def derived_artifacts(self, asset_id: str) -> dict[str, bytes]:
        asset = self.get(asset_id)
        with self._lock:
            rows = self.connection.execute(
                "SELECT name, contents FROM asset_artifacts WHERE asset_id = ? AND revision_id = ?",
                (asset.asset_id, asset.revision_id),
            ).fetchall()
        return {row["name"]: bytes(row["contents"]) for row in rows}

    def search_current(self, query: str) -> list[KnowledgeAsset]:
        """Search the current revision through SQLite FTS with a literal fallback."""
        if not query.strip():
            raise KRQueryInvalid("search query must not be empty")
        tokens = list(dict.fromkeys(re.findall(r"\w+", query.casefold())))
        fts_terms = " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
        fts_query = (
            f"(source_name : ({fts_terms}) OR markdown : ({fts_terms}))"
            if fts_terms
            else ""
        )
        rows: list[sqlite3.Row] = []
        if fts_query and self._fts_available:
            try:
                with self._lock:
                    rows = self.connection.execute(
                        """
                        SELECT r.asset_id, r.revision_id, r.source_hash, r.source_path, r.source_name,
                               r.parser_name, r.parser_version, r.markdown,
                               r.content_list_json, r.metadata_json,
                               bm25(asset_fts, 0.0, 5.0, 1.0) AS relevance
                        FROM asset_fts f
                        JOIN assets a ON a.asset_id = f.asset_id
                        JOIN asset_revisions r ON r.asset_id = a.asset_id
                                               AND r.revision_id = a.current_revision_id
                        WHERE asset_fts MATCH ?
                        ORDER BY relevance, lower(f.source_name)
                        """,
                        (fts_query,),
                    ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            pattern = f"%{query.casefold()}%"
            with self._lock:
                rows = self.connection.execute(
                    """
                    SELECT r.asset_id, r.revision_id, r.source_hash, r.source_path, r.source_name,
                           r.parser_name, r.parser_version, r.markdown,
                           r.content_list_json, r.metadata_json
                    FROM assets a
                    JOIN asset_revisions r
                      ON r.asset_id = a.asset_id AND r.revision_id = a.current_revision_id
                    WHERE instr(lower(r.source_name), ?) > 0
                       OR instr(lower(r.markdown), ?) > 0
                    ORDER BY lower(r.source_name), a.asset_id
                    """,
                    (pattern.strip("%"), pattern.strip("%")),
                ).fetchall()
        return [self._asset_from_row(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self.connection.close()


def source_digest(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stable_resource_id(path: str | Path) -> str:
    canonical_path = str(Path(path).resolve()).replace("\\", "/").casefold()
    return "src-" + hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()
