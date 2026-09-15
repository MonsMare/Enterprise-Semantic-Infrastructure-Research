# Knowledge Runtime v2 private deployment

The Compose project is deliberately scoped to `kr-v2`.  Start only the core
profile with `docker compose -p kr-v2 -f deploy/kr-v2.compose.yml --profile core up -d`.
The core profile owns PostgreSQL, MinIO, OpenSearch, the KR API image, and the
worker image.  `semantic` adds Neo4j and `governance` adds OpenMetadata.

Copy `deploy/kr-v2.env.example` to a local `.env`, set private endpoints and
credentials, and keep remote parser/embedding/Agent flags disabled unless the
corresponding egress has been explicitly approved.  Keys are read from the
process environment and are never printed by status or inventory commands.

Use `scripts/kr-v2.ps1` for `up`, `down`, `logs`, `inventory`, `backup`, and
`restore`; it always passes the project name and Compose file explicitly.

