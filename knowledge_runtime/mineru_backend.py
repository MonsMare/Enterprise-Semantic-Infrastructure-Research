from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol

from .assets import KnowledgeAsset, KnowledgeAssetStore, stable_resource_id
from .errors import KRProviderUnavailable, KRUnsupported
from .models import content_hash


class HTTPTransport(Protocol):
    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None, json_body: dict | None = None, data: bytes | None = None) -> dict[str, Any]: ...


class UrllibTransport:
    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None, json_body: dict | None = None, data: bytes | None = None) -> dict[str, Any]:
        body = json.dumps(json_body).encode("utf-8") if json_body is not None else data
        request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                content = response.read()
                result: dict[str, Any] = {"status": response.status, "content": content}
                if "json" in response.headers.get("Content-Type", ""):
                    result["json"] = json.loads(content.decode("utf-8"))
                return result
        except urllib.error.HTTPError as exc:
            return {"status": exc.code, "content": exc.read()}


class MinerUCloudBackend:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model_version: str | None = None,
        transport: HTTPTransport | None = None,
        poll_interval: float = 3.0,
        timeout: float = 600.0,
        is_ocr: bool | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("MINERU_API_KEY", "")
        self.base_url = (base_url or os.environ.get("MINERU_BASE_URL", "https://mineru.net/api/v4")).rstrip("/")
        self.model_version = model_version or os.environ.get("MINERU_MODEL", "vlm")
        self.is_ocr = is_ocr if is_ocr is not None else os.environ.get("MINERU_OCR", "false").strip().lower() in {"1", "true", "yes"}
        self.transport = transport or UrllibTransport()
        self.poll_interval = poll_interval
        self.timeout = timeout

    def extract(self, source: str | Path, *, store: KnowledgeAssetStore | None = None) -> KnowledgeAsset:
        if not self.api_key:
            raise KRProviderUnavailable("MINERU_API_KEY is not set")
        path = Path(source)
        raw = path.read_bytes()
        source_hash = content_hash(raw)
        model_version = "MinerU-HTML" if path.suffix.lower() in {".html", ".htm"} else self.model_version
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        data_id = f"kr-{source_hash[:32]}"
        response = self._json_response(
            self.transport.request(
                "POST",
                f"{self.base_url}/file-urls/batch",
                headers=headers,
                json_body={
                    "files": [{"name": path.name, "data_id": data_id, "is_ocr": self.is_ocr}],
                    "model_version": model_version,
                    "enable_table": True,
                    "enable_formula": True,
                },
            )
        )
        batch_id = response["data"]["batch_id"]
        upload_urls = response["data"]["file_urls"]
        if not upload_urls:
            raise KRProviderUnavailable("MinerU did not return an upload URL")
        upload = self.transport.request("PUT", upload_urls[0], data=raw)
        if not 200 <= upload["status"] < 300:
            raise KRProviderUnavailable(f"MinerU upload failed with HTTP {upload['status']}")

        deadline = time.monotonic() + self.timeout
        result: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            poll = self._json_response(
                self.transport.request(
                    "GET",
                    f"{self.base_url}/extract-results/batch/{batch_id}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
            )
            extraction = poll["data"].get("extract_result", [])
            result = next((item for item in extraction if item.get("data_id") == data_id), extraction[0] if extraction else None)
            if result and result.get("state") == "done":
                break
            if result and result.get("state") == "failed":
                raise KRProviderUnavailable(f"MinerU extraction failed: {result.get('err_msg', 'unknown error')}")
            time.sleep(self.poll_interval)
        else:
            raise KRProviderUnavailable("MinerU extraction timed out")
        if not result or not result.get("full_zip_url"):
            raise KRProviderUnavailable("MinerU completed without a result archive")

        archive_response = self.transport.request("GET", result["full_zip_url"])
        if not 200 <= archive_response["status"] < 300:
            raise KRProviderUnavailable(f"MinerU result download failed with HTTP {archive_response['status']}")
        try:
            with zipfile.ZipFile(BytesIO(archive_response["content"])) as archive:
                names = archive.namelist()
                markdown_name = next((name for name in names if name.endswith("full.md")), None)
                if markdown_name is None:
                    raise KRUnsupported("MinerU result archive does not contain full.md")
                markdown = archive.read(markdown_name).decode("utf-8", errors="replace")
                json_name = next((name for name in names if name.endswith("_content_list.json") or name.endswith("content_list.json")), None)
                content_list_bytes = archive.read(json_name) if json_name else b"[]"
                content_list = json.loads(content_list_bytes.decode("utf-8", errors="replace"))
                artifacts = {Path(name).name: archive.read(name) for name in names if not name.endswith("/")}
        except zipfile.BadZipFile as exc:
            raise KRUnsupported("MinerU returned an invalid result archive") from exc

        asset = KnowledgeAsset(
            asset_id=stable_resource_id(path),
            source_path=str(path.resolve()),
            source_name=path.name,
            source_hash=source_hash,
            parser_name="mineru-cloud",
            parser_version=model_version,
            markdown=markdown,
            content_list=content_list,
            metadata={"batch_id": batch_id, "data_id": data_id, "artifact_hashes": {name: content_hash(value) for name, value in artifacts.items()}},
        )
        return store.put(asset, artifacts=artifacts) if store else asset

    @staticmethod
    def _json_response(response: dict[str, Any]) -> dict[str, Any]:
        if not 200 <= response.get("status", 500) < 300:
            raise KRProviderUnavailable(f"MinerU request failed with HTTP {response.get('status')}")
        payload = response.get("json")
        if payload is None:
            try:
                payload = json.loads(response.get("content", b"").decode("utf-8"))
            except Exception as exc:
                raise KRProviderUnavailable("MinerU returned an invalid JSON response") from exc
        if payload.get("code") != 0:
            raise KRProviderUnavailable(f"MinerU error: {payload.get('msg', 'unknown error')}")
        return payload
