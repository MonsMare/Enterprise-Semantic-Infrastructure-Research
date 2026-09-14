from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent_loop import AgentLoop
from .asset_provider import AssetKnowledgeProvider
from .assets import KnowledgeAssetStore, SQLiteKnowledgeAssetStore
from .enrichment import OpenAICompatibleEnricher
from .errors import KnowledgeRuntimeError
from .evaluation import (
    BenchmarkRunner,
    RetrievalBenchmarkRunner,
    load_cases,
    write_report,
    write_retrieval_report,
)
from .extractors import LocalExtractionBackend
from .llm_client import OpenAICompatibleClient
from .mineru_backend import MinerUCloudBackend

LOCAL_EXTENSIONS = {".txt", ".md", ".markdown", ".rst", ".csv", ".json", ".xml", ".html", ".htm", ".docx", ".pptx", ".pdf"}
MINERU_EXTENSIONS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".png", ".jpg", ".jpeg", ".jp2", ".webp", ".gif", ".bmp", ".html"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kr", description="Codex-like Knowledge Runtime POC")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="parse a source document into a local Knowledge Asset")
    ingest.add_argument("source", type=Path)
    ingest.add_argument("--backend", choices=("local", "mineru"), default="local")
    ingest.add_argument("--store", type=Path, default=Path(".kr-data/assets"))
    ingest.add_argument("--enrich", action="store_true", help="generate navigation metadata with the configured OpenAI-compatible LLM")
    ingest.add_argument("--ocr", action="store_true", default=None, help="enable OCR for MinerU Cloud extraction")

    ask = commands.add_parser("ask", help="answer a question through the Evidence-first retrieval loop")
    ask.add_argument("question")
    ask.add_argument("--store", type=Path, default=Path(".kr-data/assets"))
    ask.add_argument("--max-iterations", type=int, default=8)
    ask.add_argument("--max-read-bytes", type=int, default=20_000)
    ask.add_argument("--model", choices=("qwen3.8-max",), default="qwen3.8-max")

    benchmark = commands.add_parser("benchmark", help="run a JSONL benchmark through the Agent Retrieval Loop")
    benchmark.add_argument("cases", type=Path)
    benchmark.add_argument("--store", type=Path, default=Path(".kr-data/assets"))
    benchmark.add_argument("--output", type=Path)
    benchmark.add_argument("--max-iterations", type=int, default=8)
    benchmark.add_argument("--max-read-bytes", type=int, default=20_000)
    benchmark.add_argument("--model", choices=("qwen3.8-max",), default="qwen3.8-max")

    retrieval_benchmark = commands.add_parser(
        "benchmark-retrieval", help="run deterministic search-and-read checks without an LLM call"
    )
    retrieval_benchmark.add_argument("cases", type=Path)
    retrieval_benchmark.add_argument("--store", type=Path, default=Path(".kr-data/assets"))
    retrieval_benchmark.add_argument("--output", type=Path)
    retrieval_benchmark.add_argument("--max-read-bytes", type=int, default=20_000)

    catalog = commands.add_parser("catalog", help="inspect persisted knowledge assets and revisions")
    catalog.add_argument("--store", type=Path, default=Path(".kr-data/assets"))
    catalog.add_argument("--asset-id")
    return parser


def open_asset_store(path: Path):
    """Select the operational store from the path without changing the CLI shape."""
    if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        return SQLiteKnowledgeAssetStore(path)
    return KnowledgeAssetStore(path)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = open_asset_store(args.store)
    try:
        if args.command == "ingest":
            if not args.source.is_file():
                if not args.source.is_dir():
                    raise FileNotFoundError(f"source path not found: {args.source}")
                supported = LOCAL_EXTENSIONS if args.backend == "local" else MINERU_EXTENSIONS
                sources = sorted(
                    path
                    for path in args.source.rglob("*")
                    if path.is_file()
                    and path.suffix.lower() in supported
                    and not any(part.startswith(".") for part in path.relative_to(args.source).parts)
                )
                if not sources:
                    raise ValueError(f"no supported {args.backend} documents found under {args.source}")
            else:
                sources = [args.source]
            if args.backend == "mineru":
                backend = MinerUCloudBackend(is_ocr=args.ocr)
            else:
                enricher = OpenAICompatibleEnricher(OpenAICompatibleClient()) if args.enrich else None
                backend = LocalExtractionBackend(enricher=enricher)
            assets = []
            errors = []
            for source in sources:
                try:
                    asset = backend.extract(source, store=store)
                    assets.append({"asset_id": asset.asset_id, "source_name": asset.source_name, "source_hash": asset.source_hash, "revision_id": asset.revision_id, "parser": asset.parser_name, "parser_version": asset.parser_version})
                except KnowledgeRuntimeError as exc:
                    errors.append({"source": str(source), "error": str(exc)})
            print(json.dumps({"assets": assets, "errors": errors, "asset_store": str(args.store)}, ensure_ascii=False, indent=2))
            return 2 if errors else 0

        if args.command == "catalog":
            assets = store.list_assets()
            if args.asset_id:
                assets = [asset for asset in assets if asset.asset_id == args.asset_id]
            payload = {
                "store": str(args.store),
                "assets": [
                    {
                        "asset_id": asset.asset_id,
                        "source_name": asset.source_name,
                        "source_path": asset.source_path,
                        "source_hash": asset.source_hash,
                        "revision_id": asset.revision_id,
                        "parser": asset.parser_name,
                        "parser_version": asset.parser_version,
                        "revisions": store.list_revisions(asset.asset_id)
                        if hasattr(store, "list_revisions")
                        else [],
                    }
                    for asset in assets
                ],
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        provider = AssetKnowledgeProvider(store)
        if args.command == "benchmark-retrieval":
            report = RetrievalBenchmarkRunner().run(
                load_cases(args.cases),
                provider,
                max_read_bytes=args.max_read_bytes,
            )
            if args.output:
                write_retrieval_report(report, args.output)
            print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
            return 0 if report.passed == report.total else 1

        if args.command == "benchmark":
            model = OpenAICompatibleClient(model=args.model)
            report = BenchmarkRunner(model, model_name=args.model).run(
                load_cases(args.cases),
                provider,
                max_iterations=args.max_iterations,
                max_read_bytes=args.max_read_bytes,
            )
            if args.output:
                write_report(report, args.output)
            print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
            return 0 if report.passed == report.total else 1

        model = OpenAICompatibleClient(model=args.model)
        result = AgentLoop(model).run(
            args.question,
            provider,
            max_iterations=args.max_iterations,
            max_read_bytes=args.max_read_bytes,
        )
        print(result.answer)
        if result.evidence:
            print("\nEvidence:")
            for evidence in result.evidence:
                print(json.dumps(evidence.as_model_input(), ensure_ascii=False, indent=2))
        return 0
    except (KnowledgeRuntimeError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if hasattr(store, "close"):
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
