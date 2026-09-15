CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    current_revision_id TEXT NULL,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS document_revisions (
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL,
    source_hash TEXT NOT NULL UNIQUE,
    source_name TEXT NOT NULL,
    parser_name TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (document_id, revision_id)
);

CREATE TABLE IF NOT EXISTS document_elements (
    document_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    element_id TEXT NOT NULL,
    element_type TEXT NOT NULL,
    text TEXT NOT NULL,
    section_path TEXT[] NOT NULL DEFAULT '{}',
    page INTEGER,
    bbox DOUBLE PRECISION[],
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    provenance_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence DOUBLE PRECISION,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (document_id, revision_id, element_id),
    FOREIGN KEY (document_id, revision_id)
        REFERENCES document_revisions(document_id, revision_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS parse_reports (
    document_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    report_json JSONB NOT NULL,
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
    size_bytes BIGINT NOT NULL,
    kind TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingestion_jobs (
    job_id TEXT PRIMARY KEY,
    document_id TEXT,
    revision_id TEXT,
    state TEXT NOT NULL,
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS index_runs (
    index_version TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS semantic_proposals (
    proposal_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    evidence_refs_json JSONB NOT NULL,
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS context_runs (
    run_id TEXT PRIMARY KEY,
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

ALTER TABLE documents
    DROP CONSTRAINT IF EXISTS documents_current_revision_fk;
ALTER TABLE documents
    ADD CONSTRAINT documents_current_revision_fk
    FOREIGN KEY (document_id, current_revision_id)
    REFERENCES document_revisions(document_id, revision_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX IF NOT EXISTS idx_document_elements_content_hash
    ON document_elements(content_hash);
CREATE INDEX IF NOT EXISTS idx_document_revisions_document
    ON document_revisions(document_id, created_at);

