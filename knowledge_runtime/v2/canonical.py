from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .contracts import ArtifactRef, DocumentElement, DocumentIR, DocumentRevision, EvidenceRef, ParseReport


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CanonicalStore(Protocol):
    def put_revision(self, ir: DocumentIR, *, source_hash: str) -> None: ...

    def find_revision_by_source_hash(self, source_hash: str) -> DocumentRevision | None: ...

    def get_revision(self, document_id: str, revision_id: str) -> DocumentRevision: ...

    def get_ir(self, document_id: str, revision_id: str) -> DocumentIR: ...

    def get_element(self, document_id: str, revision_id: str, element_id: str) -> DocumentElement: ...

    def current_revision(self, document_id: str) -> str | None: ...

    def begin_publication(self, document_id: str, revision_id: str, *, index_version: str) -> None: ...

    def publish_current(self, document_id: str, revision_id: str, *, index_version: str) -> None: ...

    def record_index_run(self, index_version: str, *, state: str, **details: Any) -> None: ...

    def list_current_documents(self) -> list[DocumentRevision]: ...

    def list_revisions(self, document_id: str) -> list[DocumentRevision]: ...

    def record_artifacts(self, document_id: str, revision_id: str, refs: Iterable[ArtifactRef]) -> None: ...

    def record_ingestion_job(self, job_id: str, **details: Any) -> None: ...

    def list_ingestion_jobs(self, document_id: str | None = None) -> list[dict[str, Any]]: ...

    def record_context_run(self, run_id: str, **details: Any) -> None: ...


class InMemoryCanonicalStore:
    """Deterministic canonical store used by the POC and contract tests.

    It models the same publication invariant as PostgreSQL: a revision is
    committed first, an index run must succeed, and only then can the current
    pointer move.  All stored value objects are immutable.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._irs: dict[tuple[str, str], DocumentIR] = {}
        self._revisions: dict[tuple[str, str], DocumentRevision] = {}
        self._source_hashes: dict[str, tuple[str, str]] = {}
        self._current: dict[str, str] = {}
        self._publication: dict[tuple[str, str], str] = {}
        self._index_runs: dict[str, dict[str, Any]] = {}
        self._ingestion_jobs: dict[str, dict[str, Any]] = {}
        self._context_runs: list[dict[str, Any]] = []

    def put_revision(self, ir: DocumentIR, *, source_hash: str) -> None:
        if not source_hash:
            raise ValueError("source_hash is required")
        key = (ir.document_id, ir.revision_id)
        source_name = str(ir.metadata.get("source_name", ir.document_id))
        revision = DocumentRevision(
            document_id=ir.document_id,
            revision_id=ir.revision_id,
            source_hash=source_hash,
            source_name=source_name,
            parser_name=ir.parse_report.parser_name,
            parser_version=ir.parse_report.parser_version,
            state="COMMITTED",
            created_at=_now(),
        )
        with self._lock:
            existing = self._revisions.get(key)
            if existing is not None:
                if existing.source_hash != source_hash:
                    raise ValueError("revision already exists with a different source hash")
                return
            duplicate = self._source_hashes.get(source_hash)
            if duplicate is not None and duplicate != key:
                raise ValueError("source hash already belongs to another revision")
            self._irs[key] = ir
            self._revisions[key] = revision
            self._source_hashes[source_hash] = key

    def find_revision_by_source_hash(self, source_hash: str) -> DocumentRevision | None:
        with self._lock:
            key = self._source_hashes.get(source_hash)
            return self._revisions.get(key) if key else None

    def get_revision(self, document_id: str, revision_id: str) -> DocumentRevision:
        try:
            return self._revisions[(document_id, revision_id)]
        except KeyError as exc:
            raise KeyError(f"unknown revision {document_id}/{revision_id}") from exc

    def get_ir(self, document_id: str, revision_id: str) -> DocumentIR:
        try:
            return self._irs[(document_id, revision_id)]
        except KeyError as exc:
            raise KeyError(f"unknown revision {document_id}/{revision_id}") from exc

    def get_element(self, document_id: str, revision_id: str, element_id: str) -> DocumentElement:
        ir = self.get_ir(document_id, revision_id)
        for element in ir.elements:
            if element.element_id == element_id:
                return element
        raise KeyError(f"unknown element {element_id}")

    def current_revision(self, document_id: str) -> str | None:
        with self._lock:
            return self._current.get(document_id)

    def begin_publication(self, document_id: str, revision_id: str, *, index_version: str) -> None:
        self.get_revision(document_id, revision_id)
        with self._lock:
            self._publication[(document_id, revision_id)] = index_version

    def record_index_run(self, index_version: str, *, state: str, **details: Any) -> None:
        if state not in {"PENDING", "RUNNING", "SUCCEEDED", "FAILED"}:
            raise ValueError(f"invalid index state: {state}")
        with self._lock:
            self._index_runs[index_version] = {"state": state, **details}

    def publish_current(self, document_id: str, revision_id: str, *, index_version: str) -> None:
        self.get_revision(document_id, revision_id)
        with self._lock:
            index_run = self._index_runs.get(index_version)
            if index_run is None or index_run.get("state") != "SUCCEEDED":
                raise ValueError("cannot publish current revision before index succeeds")
            pending = self._publication.get((document_id, revision_id))
            if pending is not None and pending != index_version:
                raise ValueError("publication index version does not match pending publication")
            self._current[document_id] = revision_id

    def list_current_documents(self) -> list[DocumentRevision]:
        with self._lock:
            return [
                self._revisions[(document_id, revision_id)]
                for document_id, revision_id in sorted(self._current.items())
            ]

    def list_revisions(self, document_id: str) -> list[DocumentRevision]:
        with self._lock:
            return sorted(
                (revision for (doc_id, _), revision in self._revisions.items() if doc_id == document_id),
                key=lambda revision: revision.created_at,
            )

    def record_artifacts(self, document_id: str, revision_id: str, refs: Iterable[ArtifactRef]) -> None:
        key = (document_id, revision_id)
        with self._lock:
            ir = self._irs.get(key)
            if ir is None:
                raise KeyError(f"unknown revision {document_id}/{revision_id}")
            existing = {ref.artifact_id: ref for ref in ir.source_artifacts}
            for ref in refs:
                if ref.revision_id != revision_id:
                    raise ValueError("artifact revision does not match canonical revision")
                existing.setdefault(ref.artifact_id, ref)
            self._irs[key] = replace(ir, source_artifacts=tuple(sorted(existing.values(), key=lambda ref: ref.object_key)))

    def record_ingestion_job(self, job_id: str, **details: Any) -> None:
        with self._lock:
            self._ingestion_jobs[job_id] = {"job_id": job_id, **details}

    def list_ingestion_jobs(self, document_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._ingestion_jobs.values()
            if document_id is not None:
                rows = (row for row in rows if row.get("document_id") == document_id)
            return [dict(row) for row in sorted(rows, key=lambda row: str(row["job_id"]))]

    def record_context_run(self, run_id: str, **details: Any) -> None:
        with self._lock:
            self._context_runs.append({"run_id": run_id, **details})

    def index_state(self, index_version: str) -> str | None:
        with self._lock:
            row = self._index_runs.get(index_version)
            return row.get("state") if row else None


class SqliteCanonicalStore:
    """Durable local implementation of the canonical Evidence contract.

    SQLite is deliberately used only as the POC/developer canonical store. It
    persists the same immutable revision facts as PostgreSQL, while the local
    in-memory index remains a rebuildable derived view.
    """

    def __init__(self, path: str | Path) -> None:
        raw_path = str(path)
        self.path = Path(raw_path)
        if raw_path != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(raw_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        with self._lock:
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA journal_mode = WAL")
            self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                current_revision_id TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS document_revisions (
                document_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                source_hash TEXT NOT NULL UNIQUE,
                source_name TEXT NOT NULL,
                parser_name TEXT NOT NULL,
                parser_version TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (document_id, revision_id),
                FOREIGN KEY (document_id) REFERENCES documents(document_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS document_elements (
                document_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                element_id TEXT NOT NULL,
                element_type TEXT NOT NULL,
                text TEXT NOT NULL,
                section_path_json TEXT NOT NULL DEFAULT '[]',
                page INTEGER,
                bbox_json TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                provenance_json TEXT NOT NULL DEFAULT '{}',
                confidence REAL,
                content_hash TEXT NOT NULL,
                PRIMARY KEY (document_id, revision_id, element_id),
                FOREIGN KEY (document_id, revision_id)
                    REFERENCES document_revisions(document_id, revision_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS parse_reports (
                document_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                report_json TEXT NOT NULL,
                PRIMARY KEY (document_id, revision_id),
                FOREIGN KEY (document_id, revision_id)
                    REFERENCES document_revisions(document_id, revision_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS artifact_refs (
                artifact_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                object_key TEXT NOT NULL UNIQUE,
                media_type TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                kind TEXT NOT NULL,
                FOREIGN KEY (document_id, revision_id)
                    REFERENCES document_revisions(document_id, revision_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS ingestion_jobs (
                job_id TEXT PRIMARY KEY,
                document_id TEXT,
                revision_id TEXT,
                state TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS index_runs (
                index_version TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS context_runs (
                run_id TEXT PRIMARY KEY,
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_sqlite_document_revisions_document
                ON document_revisions(document_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_sqlite_artifact_refs_revision
                ON artifact_refs(document_id, revision_id, object_key);
            CREATE INDEX IF NOT EXISTS idx_sqlite_ingestion_jobs_document
                ON ingestion_jobs(document_id, job_id);
            """
        )
        self.connection.commit()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def put_revision(self, ir: DocumentIR, *, source_hash: str) -> None:
        if not source_hash:
            raise ValueError("source_hash is required")
        source_name = str(ir.metadata.get("source_name", ir.document_id))
        created_at = _now()
        with self._lock, self.connection:
            cursor = self.connection.cursor()
            existing = cursor.execute(
                "SELECT source_hash FROM document_revisions WHERE document_id=? AND revision_id=?",
                (ir.document_id, ir.revision_id),
            ).fetchone()
            if existing is not None:
                if str(existing["source_hash"]) != source_hash:
                    raise ValueError("revision already exists with a different source hash")
                return
            duplicate = cursor.execute(
                "SELECT document_id, revision_id FROM document_revisions WHERE source_hash=?",
                (source_hash,),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("source hash already belongs to another revision")
            cursor.execute(
                "INSERT OR IGNORE INTO documents(document_id, current_revision_id, metadata_json) VALUES (?, NULL, ?)",
                (ir.document_id, _sqlite_json(dict(ir.metadata))),
            )
            cursor.execute(
                """
                INSERT INTO document_revisions
                (document_id, revision_id, source_hash, source_name, parser_name, parser_version, state, created_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, 'COMMITTED', ?, ?)
                """,
                (
                    ir.document_id,
                    ir.revision_id,
                    source_hash,
                    source_name,
                    ir.parse_report.parser_name,
                    ir.parse_report.parser_version,
                    created_at,
                    _sqlite_json(dict(ir.metadata)),
                ),
            )
            cursor.execute(
                "INSERT INTO parse_reports(document_id, revision_id, report_json) VALUES (?, ?, ?)",
                (ir.document_id, ir.revision_id, _sqlite_json(_report_json(ir.parse_report))),
            )
            for element in ir.elements:
                cursor.execute(
                    """
                    INSERT INTO document_elements
                    (document_id, revision_id, element_id, element_type, text, section_path_json,
                     page, bbox_json, payload_json, provenance_json, confidence, content_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ir.document_id,
                        ir.revision_id,
                        element.element_id,
                        element.element_type,
                        element.text,
                        _sqlite_json(list(element.section_path)),
                        element.page,
                        _sqlite_json(list(element.bbox)) if element.bbox else None,
                        _sqlite_json(dict(element.payload)),
                        _sqlite_json(dict(element.provenance)),
                        element.confidence,
                        element.content_hash,
                    ),
                )
            self._record_artifacts(cursor, ir.document_id, ir.revision_id, ir.source_artifacts)

    def _record_artifacts(
        self,
        cursor: sqlite3.Cursor,
        document_id: str,
        revision_id: str,
        refs: Iterable[ArtifactRef],
    ) -> None:
        for artifact in refs:
            if artifact.revision_id != revision_id:
                raise ValueError("artifact revision does not match canonical revision")
            cursor.execute(
                """
                INSERT OR IGNORE INTO artifact_refs
                (artifact_id, document_id, revision_id, object_key, media_type, sha256, size_bytes, kind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    document_id,
                    revision_id,
                    artifact.object_key,
                    artifact.media_type,
                    artifact.sha256,
                    artifact.size_bytes,
                    artifact.kind,
                ),
            )

    def record_artifacts(self, document_id: str, revision_id: str, refs: Iterable[ArtifactRef]) -> None:
        self.get_revision(document_id, revision_id)
        with self._lock, self.connection:
            self._record_artifacts(self.connection.cursor(), document_id, revision_id, refs)

    def find_revision_by_source_hash(self, source_hash: str) -> DocumentRevision | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT document_id, revision_id, source_hash, source_name, parser_name, parser_version, state, created_at "
                "FROM document_revisions WHERE source_hash=?",
                (source_hash,),
            ).fetchone()
        return _revision_from_row(row) if row else None

    def get_revision(self, document_id: str, revision_id: str) -> DocumentRevision:
        with self._lock:
            row = self.connection.execute(
                "SELECT document_id, revision_id, source_hash, source_name, parser_name, parser_version, state, created_at "
                "FROM document_revisions WHERE document_id=? AND revision_id=?",
                (document_id, revision_id),
            ).fetchone()
        if not row:
            raise KeyError(f"unknown revision {document_id}/{revision_id}")
        return _revision_from_row(row)

    def get_element(self, document_id: str, revision_id: str, element_id: str) -> DocumentElement:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT element_id, revision_id, element_type, text, section_path_json, page, bbox_json,
                       payload_json, provenance_json, confidence, content_hash
                FROM document_elements WHERE document_id=? AND revision_id=? AND element_id=?
                """,
                (document_id, revision_id, element_id),
            ).fetchone()
        if not row:
            raise KeyError(f"unknown element {element_id}")
        return _element_from_row(row)

    def get_ir(self, document_id: str, revision_id: str) -> DocumentIR:
        with self._lock:
            revision = self.connection.execute(
                "SELECT metadata_json FROM document_revisions WHERE document_id=? AND revision_id=?",
                (document_id, revision_id),
            ).fetchone()
            if revision is None:
                raise KeyError(f"unknown revision {document_id}/{revision_id}")
            elements = self.connection.execute(
                """
                SELECT element_id, revision_id, element_type, text, section_path_json, page, bbox_json,
                       payload_json, provenance_json, confidence, content_hash
                FROM document_elements WHERE document_id=? AND revision_id=?
                ORDER BY page IS NOT NULL, page, element_id
                """,
                (document_id, revision_id),
            ).fetchall()
            report = self.connection.execute(
                "SELECT report_json FROM parse_reports WHERE document_id=? AND revision_id=?",
                (document_id, revision_id),
            ).fetchone()
            artifacts = self.connection.execute(
                """
                SELECT artifact_id, object_key, media_type, sha256, size_bytes, kind, revision_id
                FROM artifact_refs WHERE document_id=? AND revision_id=? ORDER BY object_key
                """,
                (document_id, revision_id),
            ).fetchall()
        report_data = _json_value(report["report_json"] if report else "{}")
        return DocumentIR(
            document_id=document_id,
            revision_id=revision_id,
            metadata=_json_value(revision["metadata_json"]),
            elements=tuple(_element_from_row(row) for row in elements),
            parse_report=_report_from_json(report_data),
            source_artifacts=tuple(_artifact_from_row(row) for row in artifacts),
        )

    def current_revision(self, document_id: str) -> str | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT current_revision_id FROM documents WHERE document_id=?", (document_id,)
            ).fetchone()
        return str(row["current_revision_id"]) if row and row["current_revision_id"] else None

    def begin_publication(self, document_id: str, revision_id: str, *, index_version: str) -> None:
        self.get_revision(document_id, revision_id)
        self.record_ingestion_job(
            f"publication:{document_id}:{revision_id}",
            document_id=document_id,
            revision_id=revision_id,
            state="PUBLICATION_PENDING",
            index_version=index_version,
        )

    def record_index_run(self, index_version: str, *, state: str, **details: Any) -> None:
        if state not in {"PENDING", "RUNNING", "SUCCEEDED", "FAILED"}:
            raise ValueError(f"invalid index state: {state}")
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO index_runs(index_version, state, details_json) VALUES (?, ?, ?)
                ON CONFLICT(index_version) DO UPDATE SET state=excluded.state, details_json=excluded.details_json
                """,
                (index_version, state, _sqlite_json(details)),
            )

    def index_state(self, index_version: str) -> str | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT state FROM index_runs WHERE index_version=?", (index_version,)
            ).fetchone()
        return str(row["state"]) if row else None

    def publish_current(self, document_id: str, revision_id: str, *, index_version: str) -> None:
        with self._lock, self.connection:
            cursor = self.connection.cursor()
            document = cursor.execute(
                "SELECT document_id FROM documents WHERE document_id=?", (document_id,)
            ).fetchone()
            if document is None:
                raise KeyError(document_id)
            revision = cursor.execute(
                "SELECT state FROM document_revisions WHERE document_id=? AND revision_id=?",
                (document_id, revision_id),
            ).fetchone()
            if revision is None or str(revision["state"]) != "COMMITTED":
                raise ValueError("revision is not committed")
            index_run = cursor.execute(
                "SELECT state FROM index_runs WHERE index_version=?", (index_version,)
            ).fetchone()
            if index_run is None or str(index_run["state"]) != "SUCCEEDED":
                raise ValueError("cannot publish current revision before index succeeds")
            cursor.execute(
                "UPDATE documents SET current_revision_id=? WHERE document_id=?",
                (revision_id, document_id),
            )

    def list_revisions(self, document_id: str) -> list[DocumentRevision]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT document_id, revision_id, source_hash, source_name, parser_name, parser_version, state, created_at
                FROM document_revisions WHERE document_id=? ORDER BY created_at, revision_id
                """,
                (document_id,),
            ).fetchall()
        return [_revision_from_row(row) for row in rows]

    def list_current_documents(self) -> list[DocumentRevision]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT r.document_id, r.revision_id, r.source_hash, r.source_name, r.parser_name,
                       r.parser_version, r.state, r.created_at
                FROM documents d JOIN document_revisions r
                  ON r.document_id=d.document_id AND r.revision_id=d.current_revision_id
                ORDER BY r.document_id
                """
            ).fetchall()
        return [_revision_from_row(row) for row in rows]

    def record_ingestion_job(self, job_id: str, **details: Any) -> None:
        values = dict(details)
        state = str(values.pop("state", "RECEIVED"))
        document_id = values.pop("document_id", None)
        revision_id = values.pop("revision_id", None)
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO ingestion_jobs(job_id, document_id, revision_id, state, details_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    document_id=excluded.document_id,
                    revision_id=excluded.revision_id,
                    state=excluded.state,
                    details_json=excluded.details_json
                """,
                (job_id, document_id, revision_id, state, _sqlite_json(values)),
            )

    def list_ingestion_jobs(self, document_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT job_id, document_id, revision_id, state, details_json FROM ingestion_jobs"
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += " WHERE document_id=?"
            params = (document_id,)
        query += " ORDER BY job_id"
        with self._lock:
            rows = self.connection.execute(query, params).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            details = _json_value(row["details_json"])
            result.append(
                {
                    "job_id": str(row["job_id"]),
                    "document_id": row["document_id"],
                    "revision_id": row["revision_id"],
                    "state": str(row["state"]),
                    **details,
                }
            )
        return result

    def record_context_run(self, run_id: str, **details: Any) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO context_runs(run_id, details_json) VALUES (?, ?)",
                (run_id, _sqlite_json(details)),
            )


class SchemaMigrator:
    """Apply numbered PostgreSQL migrations once, transactionally."""

    def __init__(self, connection: Any, migrations_dir: str | Path | None = None) -> None:
        self.connection = connection
        self.migrations_dir = Path(migrations_dir or Path(__file__).with_name("migrations"))

    def apply(self) -> list[str]:
        applied: list[str] = []
        with self.connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS kr_schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            cursor.execute("SELECT version FROM kr_schema_migrations")
            existing = {row[0] for row in cursor.fetchall()}
            for path in sorted(self.migrations_dir.glob("*.sql")):
                version = path.name
                if version in existing:
                    continue
                cursor.execute(path.read_text(encoding="utf-8"))
                cursor.execute("INSERT INTO kr_schema_migrations(version) VALUES (%s)", (version,))
                applied.append(version)
        self.connection.commit()
        return applied


class PostgresCanonicalStore:
    """PostgreSQL implementation of the canonical contract.

    The dependency is optional for the local POC; construction fails clearly if
    the runtime extra is not installed or a DSN is not supplied.
    """

    def __init__(self, dsn: str | None = None, *, connection: Any | None = None) -> None:
        self.dsn = dsn or os.environ.get("KR_DATABASE_URL", "")
        if connection is None:
            if not self.dsn:
                raise ValueError("KR_DATABASE_URL is required for PostgresCanonicalStore")
            try:
                import psycopg  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError("install the runtime extra to use PostgreSQL") from exc
            connection = psycopg.connect(self.dsn)
        self.connection = connection

    def put_revision(self, ir: DocumentIR, *, source_hash: str) -> None:
        source_name = str(ir.metadata.get("source_name", ir.document_id))
        created_at = _now()
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO documents(document_id, current_revision_id, metadata_json)
                    VALUES (%s, NULL, %s::jsonb)
                    ON CONFLICT (document_id) DO NOTHING
                    """,
                    (ir.document_id, json.dumps(dict(ir.metadata), ensure_ascii=False)),
                )
                cursor.execute(
                    """
                    INSERT INTO document_revisions
                    (document_id, revision_id, source_hash, source_name, parser_name,
                     parser_version, state, created_at, metadata_json)
                    VALUES (%s, %s, %s, %s, %s, %s, 'COMMITTED', %s, %s::jsonb)
                    ON CONFLICT (document_id, revision_id) DO NOTHING
                    """,
                    (
                        ir.document_id,
                        ir.revision_id,
                        source_hash,
                        source_name,
                        ir.parse_report.parser_name,
                        ir.parse_report.parser_version,
                        created_at,
                        json.dumps(dict(ir.metadata), ensure_ascii=False),
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO parse_reports
                    (document_id, revision_id, report_json)
                    VALUES (%s, %s, %s::jsonb)
                    ON CONFLICT (document_id, revision_id) DO NOTHING
                    """,
                    (ir.document_id, ir.revision_id, json.dumps(_report_json(ir.parse_report), ensure_ascii=False)),
                )
                for element in ir.elements:
                    cursor.execute(
                        """
                        INSERT INTO document_elements
                        (document_id, revision_id, element_id, element_type, text,
                         section_path, page, bbox, payload_json, provenance_json,
                         confidence, content_hash)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                                 %s::jsonb, %s, %s)
                        ON CONFLICT (document_id, revision_id, element_id) DO NOTHING
                        """,
                        (
                            ir.document_id,
                            ir.revision_id,
                            element.element_id,
                            element.element_type,
                            element.text,
                            list(element.section_path),
                            element.page,
                            list(element.bbox) if element.bbox else None,
                            json.dumps(dict(element.payload), ensure_ascii=False),
                            json.dumps(dict(element.provenance), ensure_ascii=False),
                            element.confidence,
                            element.content_hash,
                        ),
                    )
                for artifact in ir.source_artifacts:
                    cursor.execute(
                        """
                        INSERT INTO artifact_refs
                        (artifact_id, document_id, revision_id, object_key, media_type,
                         sha256, size_bytes, kind)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (artifact_id) DO NOTHING
                        """,
                        (
                            artifact.artifact_id,
                            ir.document_id,
                            ir.revision_id,
                            artifact.object_key,
                            artifact.media_type,
                            artifact.sha256,
                            artifact.size_bytes,
                            artifact.kind,
                        ),
                    )

    def find_revision_by_source_hash(self, source_hash: str) -> DocumentRevision | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT document_id, revision_id, source_hash, source_name, parser_name, "
                "parser_version, state, created_at FROM document_revisions WHERE source_hash=%s",
                (source_hash,),
            )
            row = cursor.fetchone()
        return _revision_from_row(row) if row else None

    def record_artifacts(self, document_id: str, revision_id: str, refs: Iterable[ArtifactRef]) -> None:
        self.get_revision(document_id, revision_id)
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                for artifact in refs:
                    if artifact.revision_id != revision_id:
                        raise ValueError("artifact revision does not match canonical revision")
                    cursor.execute(
                        """
                        INSERT INTO artifact_refs
                        (artifact_id, document_id, revision_id, object_key, media_type,
                         sha256, size_bytes, kind)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (artifact_id) DO NOTHING
                        """,
                        (
                            artifact.artifact_id,
                            document_id,
                            revision_id,
                            artifact.object_key,
                            artifact.media_type,
                            artifact.sha256,
                            artifact.size_bytes,
                            artifact.kind,
                        ),
                    )

    def get_revision(self, document_id: str, revision_id: str) -> DocumentRevision:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT document_id, revision_id, source_hash, source_name, parser_name, "
                "parser_version, state, created_at FROM document_revisions "
                "WHERE document_id=%s AND revision_id=%s",
                (document_id, revision_id),
            )
            row = cursor.fetchone()
        if not row:
            raise KeyError(f"unknown revision {document_id}/{revision_id}")
        return _revision_from_row(row)

    def get_element(self, document_id: str, revision_id: str, element_id: str) -> DocumentElement:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT element_id, revision_id, element_type, text, section_path, page, bbox, "
                "payload_json, provenance_json, confidence, content_hash FROM document_elements "
                "WHERE document_id=%s AND revision_id=%s AND element_id=%s",
                (document_id, revision_id, element_id),
            )
            row = cursor.fetchone()
        if not row:
            raise KeyError(f"unknown element {element_id}")
        return _element_from_row(row)

    def get_ir(self, document_id: str, revision_id: str) -> DocumentIR:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT metadata_json FROM document_revisions WHERE document_id=%s AND revision_id=%s",
                (document_id, revision_id),
            )
            revision_row = cursor.fetchone()
            if not revision_row:
                raise KeyError(f"unknown revision {document_id}/{revision_id}")
            cursor.execute(
                "SELECT element_id, revision_id, element_type, text, section_path, page, bbox, "
                "payload_json, provenance_json, confidence, content_hash FROM document_elements "
                "WHERE document_id=%s AND revision_id=%s ORDER BY page NULLS FIRST, element_id",
                (document_id, revision_id),
            )
            element_rows = cursor.fetchall()
            cursor.execute(
                "SELECT report_json FROM parse_reports WHERE document_id=%s AND revision_id=%s",
                (document_id, revision_id),
            )
            report_row = cursor.fetchone()
            cursor.execute(
                "SELECT artifact_id, object_key, media_type, sha256, size_bytes, kind, revision_id "
                "FROM artifact_refs WHERE document_id=%s AND revision_id=%s ORDER BY object_key",
                (document_id, revision_id),
            )
            artifact_rows = cursor.fetchall()
        metadata = revision_row[0] if isinstance(revision_row[0], dict) else json.loads(revision_row[0] or "{}")
        report_data = report_row[0] if report_row and isinstance(report_row[0], dict) else json.loads(report_row[0] if report_row else "{}")
        report = _report_from_json(report_data)
        return DocumentIR(
            document_id=document_id,
            revision_id=revision_id,
            metadata=metadata,
            elements=tuple(_element_from_row(row) for row in element_rows),
            parse_report=report,
            source_artifacts=tuple(
                ArtifactRef(
                    artifact_id=str(row[0]),
                    object_key=str(row[1]),
                    media_type=str(row[2]),
                    sha256=str(row[3]),
                    size_bytes=int(row[4]),
                    kind=str(row[5]),
                    revision_id=str(row[6]),
                )
                for row in artifact_rows
            ),
        )

    def current_revision(self, document_id: str) -> str | None:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT current_revision_id FROM documents WHERE document_id=%s", (document_id,))
            row = cursor.fetchone()
        return row[0] if row and row[0] else None

    def begin_publication(self, document_id: str, revision_id: str, *, index_version: str) -> None:
        self.get_revision(document_id, revision_id)
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO ingestion_jobs(job_id, document_id, revision_id, state, details_json) "
                    "VALUES (%s, %s, %s, 'PUBLICATION_PENDING', %s::jsonb) "
                    "ON CONFLICT (job_id) DO UPDATE SET details_json=EXCLUDED.details_json",
                    (
                        f"publication:{document_id}:{revision_id}",
                        document_id,
                        revision_id,
                        json.dumps({"index_version": index_version}),
                    ),
                )

    def record_index_run(self, index_version: str, *, state: str, **details: Any) -> None:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO index_runs(index_version, state, details_json) VALUES (%s, %s, %s::jsonb) "
                    "ON CONFLICT (index_version) DO UPDATE SET state=EXCLUDED.state, details_json=EXCLUDED.details_json",
                    (index_version, state, json.dumps(details, ensure_ascii=False)),
                )

    def publish_current(self, document_id: str, revision_id: str, *, index_version: str) -> None:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT current_revision_id FROM documents WHERE document_id=%s FOR UPDATE", (document_id,))
                if cursor.fetchone() is None:
                    raise KeyError(document_id)
                cursor.execute(
                    "SELECT 1 FROM document_revisions WHERE document_id=%s AND revision_id=%s AND state='COMMITTED'",
                    (document_id, revision_id),
                )
                if cursor.fetchone() is None:
                    raise ValueError("revision is not committed")
                cursor.execute("SELECT state FROM index_runs WHERE index_version=%s", (index_version,))
                row = cursor.fetchone()
                if not row or row[0] != "SUCCEEDED":
                    raise ValueError("cannot publish current revision before index succeeds")
                cursor.execute(
                    "UPDATE documents SET current_revision_id=%s WHERE document_id=%s",
                    (revision_id, document_id),
                )

    def list_revisions(self, document_id: str) -> list[DocumentRevision]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT document_id, revision_id, source_hash, source_name, parser_name, "
                "parser_version, state, created_at FROM document_revisions "
                "WHERE document_id=%s ORDER BY created_at",
                (document_id,),
            )
            rows = cursor.fetchall()
        return [_revision_from_row(row) for row in rows]

    def list_current_documents(self) -> list[DocumentRevision]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT r.document_id, r.revision_id, r.source_hash, r.source_name, "
                "r.parser_name, r.parser_version, r.state, r.created_at "
                "FROM documents d JOIN document_revisions r ON r.document_id=d.document_id "
                "AND r.revision_id=d.current_revision_id ORDER BY r.document_id"
            )
            rows = cursor.fetchall()
        return [_revision_from_row(row) for row in rows]

    def record_ingestion_job(self, job_id: str, **details: Any) -> None:
        values = dict(details)
        state = str(values.pop("state", "RECEIVED"))
        document_id = values.pop("document_id", None)
        revision_id = values.pop("revision_id", None)
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO ingestion_jobs(job_id, document_id, revision_id, state, details_json)
                    VALUES (%s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (job_id) DO UPDATE SET
                        document_id=EXCLUDED.document_id,
                        revision_id=EXCLUDED.revision_id,
                        state=EXCLUDED.state,
                        details_json=EXCLUDED.details_json
                    """,
                    (job_id, document_id, revision_id, state, json.dumps(values, ensure_ascii=False)),
                )

    def list_ingestion_jobs(self, document_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT job_id, document_id, revision_id, state, details_json FROM ingestion_jobs"
        params: tuple[Any, ...] = ()
        if document_id is not None:
            query += " WHERE document_id=%s"
            params = (document_id,)
        query += " ORDER BY job_id"
        with self.connection.cursor() as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            values = list(row)
            details = _json_value(values[4])
            result.append(
                {
                    "job_id": str(values[0]),
                    "document_id": values[1],
                    "revision_id": values[2],
                    "state": str(values[3]),
                    **details,
                }
            )
        return result

    def record_context_run(self, run_id: str, **details: Any) -> None:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO context_runs(run_id, details_json) VALUES (%s, %s::jsonb) "
                    "ON CONFLICT (run_id) DO NOTHING",
                    (run_id, json.dumps(details, ensure_ascii=False)),
                )


def _revision_from_row(row: Any) -> DocumentRevision:
    values = list(row)
    return DocumentRevision(
        document_id=str(values[0]),
        revision_id=str(values[1]),
        source_hash=str(values[2]),
        source_name=str(values[3]),
        parser_name=str(values[4]),
        parser_version=str(values[5]),
        state=str(values[6]),
        created_at=values[7].isoformat() if hasattr(values[7], "isoformat") else str(values[7]),
    )


def _report_json(report: ParseReport) -> dict[str, Any]:
    return {
        "document_type": report.document_type,
        "page_count": report.page_count,
        "text_coverage": report.text_coverage,
        "layout_quality": report.layout_quality,
        "ocr_quality": report.ocr_quality,
        "table_quality": report.table_quality,
        "reading_order_quality": report.reading_order_quality,
        "missing_regions": list(report.missing_regions),
        "suspicious_regions": list(report.suspicious_regions),
        "parser_name": report.parser_name,
        "parser_version": report.parser_version,
        "overall_grade": report.overall_grade,
        "warnings": list(report.warnings),
        "provider_name": report.provider_name,
        "egress_allowed": report.egress_allowed,
    }


def _sqlite_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return value
    if value is None:
        return {}
    return json.loads(value)


def _element_from_row(row: Any) -> DocumentElement:
    values = list(row)
    section_path = _json_value(values[4]) or ()
    bbox = _json_value(values[6]) if values[6] is not None else None
    return DocumentElement(
        element_id=str(values[0]),
        revision_id=str(values[1]),
        element_type=str(values[2]),  # type: ignore[arg-type]
        text=str(values[3]),
        section_path=tuple(section_path),
        page=int(values[5]) if values[5] is not None else None,
        bbox=tuple(float(item) for item in bbox) if bbox else None,
        payload=_json_value(values[7]),
        provenance=_json_value(values[8]),
        confidence=float(values[9]) if values[9] is not None else None,
        content_hash=str(values[10]),
    )


def _artifact_from_row(row: Any) -> ArtifactRef:
    values = list(row)
    return ArtifactRef(
        artifact_id=str(values[0]),
        object_key=str(values[1]),
        media_type=str(values[2]),
        sha256=str(values[3]),
        size_bytes=int(values[4]),
        kind=str(values[5]),
        revision_id=str(values[6]),
    )


def _report_from_json(value: dict[str, Any]) -> ParseReport:
    if not value:
        return ParseReport.empty("postgres", "unknown")
    return ParseReport(
        document_type=str(value.get("document_type", "unknown")),
        page_count=int(value.get("page_count", 0)),
        text_coverage=float(value.get("text_coverage", 0.0)),
        layout_quality=float(value.get("layout_quality", 0.0)),
        ocr_quality=float(value.get("ocr_quality", 0.0)),
        table_quality=float(value.get("table_quality", 0.0)),
        reading_order_quality=float(value.get("reading_order_quality", 0.0)),
        missing_regions=tuple(value.get("missing_regions", ())),
        suspicious_regions=tuple(value.get("suspicious_regions", ())),
        parser_name=str(value.get("parser_name", "postgres")),
        parser_version=str(value.get("parser_version", "unknown")),
        overall_grade=str(value.get("overall_grade", "unknown")),  # type: ignore[arg-type]
        warnings=tuple(str(item) for item in value.get("warnings", ())),
        provider_name=value.get("provider_name"),
        egress_allowed=bool(value.get("egress_allowed", False)),
    )
