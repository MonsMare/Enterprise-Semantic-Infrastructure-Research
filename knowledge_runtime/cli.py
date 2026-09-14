from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent_loop import AgentLoop
from .asset_provider import AssetKnowledgeProvider
from .assets import KnowledgeAssetStore
from .enrichment import OpenAICompatibleEnricher
from .errors import KnowledgeRuntimeError
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
    ask.add_argument("--model", choices=("deepseek-v4.1-flash", "qwen3.8-max"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = KnowledgeAssetStore(args.store)
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
                    assets.append({"asset_id": asset.asset_id, "source_name": asset.source_name, "source_hash": asset.source_hash, "parser": asset.parser_name, "parser_version": asset.parser_version})
                except KnowledgeRuntimeError as exc:
                    errors.append({"source": str(source), "error": str(exc)})
            print(json.dumps({"assets": assets, "errors": errors, "asset_store": str(args.store)}, ensure_ascii=False, indent=2))
            return 2 if errors else 0

        provider = AssetKnowledgeProvider(store)
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


if __name__ == "__main__":
    raise SystemExit(main())
