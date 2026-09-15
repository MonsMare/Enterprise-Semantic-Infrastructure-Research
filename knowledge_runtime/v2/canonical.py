from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .contracts import DocumentElement, DocumentIR, DocumentRevision, EvidenceRef, ParseReport


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
        self._ingestion_jobs: list[dict[str, Any]] = []
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

    def record_ingestion_job(self, job_id: str, **details: Any) -> None:
        with self._lock:
            self._ingestion_jobs.append({"job_id": job_id, **details})

    def record_context_run(self, run_id: str, **details: Any) -> None:
        with self._lock:
            self._context_runs.append({"run_id": run_id, **details})

    def index_state(self, index_version: str) -> str | None:
        with self._lock:
            row = self._index_runs.get(index_version)
            return row.get("state") if row else None


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

    def find_revision_by_source_hash(self, source_hash: str) -> DocumentRevision | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT document_id, revision_id, source_hash, source_name, parser_name, "
                "parser_version, state, created_at FROM document_revisions WHERE source_hash=%s",
                (source_hash,),
            )
            row = cursor.fetchone()
        return _revision_from_row(row) if row else None

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
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO ingestion_jobs(job_id, state, details_json) VALUES (%s, %s, %s::jsonb) "
                    "ON CONFLICT (job_id) DO UPDATE SET state=EXCLUDED.state, details_json=EXCLUDED.details_json",
                    (job_id, details.pop("state", "RECEIVED"), json.dumps(details, ensure_ascii=False)),
                )

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

