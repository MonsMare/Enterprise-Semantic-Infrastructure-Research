from __future__ import annotations

from pathlib import Path

import pytest

from knowledge_runtime.assets import KnowledgeAsset
from knowledge_runtime.v2.config import RuntimeConfig
from knowledge_runtime.v2.contracts import ParseReport
from knowledge_runtime.v2.providers import (
    LocalProvider,
    MinerUProvider,
    ParserRouter,
    markdown_to_ir,
)


def test_local_provider_emits_page_and_heading_provenance(tmp_path: Path) -> None:
    source = tmp_path / "policy.md"
    source.write_text("# Scope\n\nThe model applies to annuity risk.\n", encoding="utf-8")

    ir = LocalProvider(config=RuntimeConfig.test_private()).parse(
        source, document_id="doc-1", revision_id="rev-1"
    )

    assert ir.elements[0].element_type == "heading"
    assert ir.elements[1].section_path == ("Scope",)
    assert ir.elements[1].revision_id == "rev-1"
    assert ir.metadata["source_name"] == "policy.md"
    assert ir.parse_report.egress_allowed is False


def test_local_provider_preserves_tables_and_lists(tmp_path: Path) -> None:
    source = tmp_path / "facts.md"
    source.write_text(
        "# Facts\n\n- alpha\n- beta\n\n| item | value |\n| --- | --- |\n| rate | 12% |\n",
        encoding="utf-8",
    )

    ir = LocalProvider(config=RuntimeConfig.test_private()).parse(
        source, document_id="doc-1", revision_id="rev-1"
    )

    assert any(element.element_type == "list" for element in ir.elements)
    table = next(element for element in ir.elements if element.element_type == "table")
    assert table.payload["rows"] == [["item", "value"], ["rate", "12%"]]


def test_private_router_does_not_select_remote_provider(tmp_path: Path) -> None:
    class FailingProvider:
        name = "mineru-cloud"

        def parse(self, *args, **kwargs):
            raise AssertionError("remote parser must not be selected")

    source = tmp_path / "input.pdf"
    source.write_bytes(b"%PDF-unsupported-for-this-routing-test")
    config = RuntimeConfig.test_private(allow_remote_parser=False)
    router = ParserRouter(
        config=config,
        local=LocalProvider(config, degraded_mode=True),
        remote=FailingProvider(),
    )

    assert router.choose(source).name == "local"


def test_markdown_to_ir_rejects_cross_revision_elements() -> None:
    ir = markdown_to_ir(
        "# Heading\n\nbody\n",
        document_id="doc-1",
        revision_id="rev-1",
        parser="test",
        version="1",
    )
    assert all(element.revision_id == "rev-1" for element in ir.elements)
    assert ir.parse_report.parser_name == "test"


class FakeMinerUBackend:
    def __init__(self) -> None:
        self.sources: list[str] = []

    def extract(self, source: str | Path, *, store=None) -> KnowledgeAsset:
        self.sources.append(str(source))
        return KnowledgeAsset(
            asset_id="asset-1",
            source_path=str(source),
            source_name=Path(source).name,
            source_hash="source-hash",
            parser_name="mineru-cloud",
            parser_version="vlm",
            markdown="# Remote\n\ncloud table\n",
            content_list=[{"type": "table", "line": 3}],
            metadata={"batch_id": "batch-1"},
        )


def test_mineru_provider_normalizes_to_same_ir() -> None:
    backend = FakeMinerUBackend()
    config = RuntimeConfig.test_private(allow_remote_parser=True)
    ir = MinerUProvider(config=config, backend=backend).parse(
        Path("document.pdf"), document_id="doc-1", revision_id="rev-1"
    )

    assert ir.elements[0].element_type == "heading"
    assert ir.metadata["batch_id"] == "batch-1"
    assert ir.parse_report.egress_allowed is True
    assert backend.sources == ["document.pdf"]


def test_mineru_provider_obeys_private_egress_policy() -> None:
    with pytest.raises(RuntimeError, match="remote parser"):
        MinerUProvider(config=RuntimeConfig.test_private(), backend=FakeMinerUBackend()).parse(
            Path("document.pdf"), document_id="doc-1", revision_id="rev-1"
        )

