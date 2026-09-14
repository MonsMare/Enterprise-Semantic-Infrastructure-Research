from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def read_sources(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def filename(source: dict, content_type: str) -> str:
    url = source.get("download_url") or source["landing_url"]
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    if suffix not in {".pdf", ".html", ".htm", ".xlsx", ".xls", ".docx", ".pptx"}:
        suffix = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) or ".bin"
    return f"{source['source_id']}{suffix}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Download selected public actuarial benchmark sources")
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("public_sources.jsonl"))
    parser.add_argument("--output", type=Path, default=Path(".kr-data/actuarial-downloads"))
    parser.add_argument("--source-id", action="append")
    args = parser.parse_args()

    selected = read_sources(args.manifest)
    if args.source_id:
        selected = [source for source in selected if source["source_id"] in set(args.source_id)]
    args.output.mkdir(parents=True, exist_ok=True)
    records = []
    failures = 0
    for source in selected:
        url = source.get("download_url") or source["landing_url"]
        request = urllib.request.Request(url, headers={"User-Agent": "KnowledgeRuntimeBenchmark/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
                content_type = response.headers.get("Content-Type", "application/octet-stream")
            target = args.output / filename(source, content_type)
            target.write_bytes(payload)
            records.append(
                {
                    **source,
                    "local_path": str(target),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "content_type": content_type,
                    "download_status": "ok",
                }
            )
            print(f"downloaded {source['source_id']} -> {target} ({len(payload)} bytes)")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            failures += 1
            status = getattr(exc, "code", None)
            records.append(
                {
                    **source,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "download_status": "error",
                    "error_type": exc.__class__.__name__,
                    "http_status": status,
                }
            )
            print(f"failed {source['source_id']}: {exc.__class__.__name__}" + (f" HTTP {status}" if status else ""))
    (args.output / ".download_manifest.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
