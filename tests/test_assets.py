import hashlib
import json
import zipfile
from io import BytesIO

from knowledge_runtime.assets import KnowledgeAssetStore
from knowledge_runtime.extractors import LocalExtractionBackend
from knowledge_runtime.mineru_backend import MinerUCloudBackend


def test_local_backend_creates_asset_with_source_and_parser_provenance(tmp_path):
    source = tmp_path / "guide.md"
    source.write_text("# Guide\n\nHello knowledge runtime.", encoding="utf-8")
    store = KnowledgeAssetStore(tmp_path / "assets")

    asset = LocalExtractionBackend().extract(source, store=store)

    assert asset.source_hash == hashlib.sha256(source.read_bytes()).hexdigest()
    assert asset.parser_name == "local"
    assert "Hello knowledge runtime." in asset.markdown
    assert store.get(asset.asset_id).asset_id == asset.asset_id
    assert asset.content_list[0]["text"] == "Guide"


def test_asset_identity_stays_stable_while_source_revision_changes(tmp_path):
    source = tmp_path / "guide.md"
    source.write_text("revision one", encoding="utf-8")
    backend = LocalExtractionBackend()
    first = backend.extract(source)
    source.write_text("revision two", encoding="utf-8")
    second = backend.extract(source)

    assert first.asset_id == second.asset_id
    assert first.source_hash != second.source_hash


class FakeTransport:
    def __init__(self):
        self.calls = []
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr("full.md", "# Parsed\n\nCloud extracted text.")
            archive.writestr("content_list.json", "[]")
        self.zip_data = zip_buffer.getvalue()

    def request(self, method, url, *, headers=None, json_body=None, data=None):
        self.calls.append((method, url, headers, json_body, data))
        if url.endswith("/api/v4/file-urls/batch"):
            return {"status": 200, "json": {"code": 0, "data": {"batch_id": "batch-1", "file_urls": ["https://upload.test/file"]}}}
        if url == "https://upload.test/file":
            return {"status": 200, "content": b""}
        if url.endswith("/api/v4/extract-results/batch/batch-1"):
            return {
                "status": 200,
                "json": {
                    "code": 0,
                    "data": {
                        "extract_result": [
                            {"state": "done", "full_zip_url": "https://download.test/result.zip", "data_id": "doc-1"}
                        ]
                    },
                },
            }
        if url == "https://download.test/result.zip":
            return {"status": 200, "content": self.zip_data}
        raise AssertionError(f"unexpected request {method} {url}")


def test_mineru_backend_uploads_polls_and_persists_derived_assets(tmp_path):
    source = tmp_path / "contract.pdf"
    source.write_bytes(b"fake pdf bytes")
    transport = FakeTransport()
    store = KnowledgeAssetStore(tmp_path / "assets")
    backend = MinerUCloudBackend(api_key="rotated-test-token", transport=transport, poll_interval=0)

    asset = backend.extract(source, store=store)

    assert asset.parser_name == "mineru-cloud"
    assert asset.parser_version == "vlm"
    assert "Cloud extracted text." in asset.markdown
    assert len(transport.calls) == 4
    submit = transport.calls[0]
    assert submit[2]["Authorization"] == "Bearer rotated-test-token"
    assert json.loads(json.dumps(submit[3]))["files"][0]["name"] == "contract.pdf"
    assert submit[3]["files"][0]["is_ocr"] is False


def test_mineru_backend_selects_html_model_for_html_sources(tmp_path):
    source = tmp_path / "page.html"
    source.write_text("<h1>HTML</h1>", encoding="utf-8")
    transport = FakeTransport()
    backend = MinerUCloudBackend(api_key="test-token", transport=transport, poll_interval=0)

    asset = backend.extract(source)

    assert transport.calls[0][3]["model_version"] == "MinerU-HTML"
    assert asset.parser_version == "MinerU-HTML"
