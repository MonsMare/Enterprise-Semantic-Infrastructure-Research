from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import KRNotFound


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


def source_digest(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stable_resource_id(path: str | Path) -> str:
    canonical_path = str(Path(path).resolve()).replace("\\", "/").casefold()
    return "src-" + hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()
