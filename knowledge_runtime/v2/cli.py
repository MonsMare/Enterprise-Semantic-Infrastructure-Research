from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .agent import AgentBudget, AgentRetrievalLoop, QwenAgentClient
from .canonical import SchemaMigrator
from .config import RuntimeConfig
from .contracts import EvidenceRef, EvidenceSearchRequest, IndexRebuildRequest
from .runtime import RuntimeBundle, build_runtime


def v2_command_names() -> tuple[str, ...]:
    return ("schema-migrate", "ingest", "search", "evidence", "ask", "status", "rebuild-index")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kr", description="Knowledge Runtime v2")
    commands = parser.add_subparsers(dest="command", required=True)
    v2 = commands.add_parser("v2", help="Knowledge Runtime v2 operations")
    sub = v2.add_subparsers(dest="v2_command", required=True)
    sub.add_parser("schema-migrate", help="apply PostgreSQL schema migrations")
    ingest = sub.add_parser("ingest", help="ingest a document into the v2 runtime")
    ingest.add_argument("source", type=Path)
    ingest.add_argument("--document-id")
    ingest.add_argument("--provider", choices=("auto", "local", "mineru"), default="auto")
    search = sub.add_parser("search", help="search evidence references")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    evidence = sub.add_parser("evidence", help="read one canonical Evidence ref")
    evidence.add_argument("document_id")
    evidence.add_argument("revision_id")
    evidence.add_argument("element_id")
    evidence.add_argument("--max-bytes", type=int, default=20_000)
    ask = sub.add_parser("ask", help="run the qwen3.8-max Agent retrieval loop")
    ask.add_argument("question")
    ask.add_argument("--max-rounds", type=int, default=8)
    ask.add_argument("--max-bytes", type=int, default=100_000)
    sub.add_parser("status", help="show v2 operational status without document bodies")
    rebuild = sub.add_parser("rebuild-index", help="rebuild the derived index")
    rebuild.add_argument("--index-version", default="memory-v1")
    return parser


def _runtime(config: RuntimeConfig) -> RuntimeBundle:
    return build_runtime(config)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    config = RuntimeConfig.from_env()
    runtime = _runtime(config)
    canonical = runtime.canonical
    index = runtime.index
    service = runtime.ingestion
    context = runtime.context
    command = args.v2_command
    if command == "schema-migrate":
        if not config.database_url:
            print(json.dumps({"status": "skipped", "reason": "KR_DATABASE_URL is not configured"}))
            return 0
        connection = getattr(canonical, "connection", None)
        if connection is None:
            raise RuntimeError("configured canonical store does not expose a PostgreSQL connection")
        print(json.dumps({"applied": SchemaMigrator(connection).apply()}))
        return 0
    if command == "ingest":
        result = service.ingest(args.source, document_id=args.document_id, provider=args.provider)
        print(json.dumps({"document_id": result.document_id, "revision_id": result.revision_id, "state": result.state, "reused": result.reused, "error": result.error}, ensure_ascii=False))
        return 0 if result.state == "CURRENT_REVISION_PUBLISHED" else 2
    if command == "search":
        page = context.search_evidence(args.query, limit=args.limit)
        print(json.dumps({"items": [{"ref": item.ref.as_dict(), "display_name": item.display_name, "score": item.score} for item in page.items], "next_cursor": page.next_cursor}, ensure_ascii=False))
        return 0
    if command == "evidence":
        evidence = context.get_evidence(EvidenceRef(args.document_id, args.revision_id, args.element_id), max_bytes=args.max_bytes)
        print(json.dumps(evidence.as_model_input(), ensure_ascii=False))
        return 0
    if command == "ask":
        agent = QwenAgentClient(config=config)
        result = AgentRetrievalLoop(agent).run(args.question, context, budgets=AgentBudget(max_rounds=args.max_rounds, max_bytes=args.max_bytes))
        print(result.answer)
        return 0
    if command == "status":
        print(json.dumps({"mode": "private" if config.private_mode else "remote-enabled", "current_documents": len(canonical.list_current_documents()), "index_backend": type(index).__name__, "agent_model": config.agent_model, "embedding_model": config.embedding_model}, ensure_ascii=False))
        return 0
    if command == "rebuild-index":
        report = index.rebuild(IndexRebuildRequest(args.index_version))
        print(json.dumps({"index_version": report.index_version, "revisions_seen": report.revisions_seen, "elements_indexed": report.elements_indexed, "state": report.state}, ensure_ascii=False))
        return 0
    raise ValueError(f"unknown v2 command: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
