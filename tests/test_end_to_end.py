import json

from knowledge_runtime.agent_loop import AgentLoop
from knowledge_runtime.assets import KnowledgeAssetStore
from knowledge_runtime.asset_provider import AssetKnowledgeProvider
from knowledge_runtime.extractors import LocalExtractionBackend
from knowledge_runtime.errors import KRStaleLocator
import pytest


class DemoModel:
    def __init__(self, turns):
        self.turns = iter(turns)

    def complete(self, messages, tools):
        return next(self.turns)


def call(name, arguments):
    return {"id": "call-1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def test_document_ingestion_to_answer_preserves_evidence_provenance(tmp_path):
    source = tmp_path / "runbook.md"
    source.write_text("# Account recovery\n\nThe recovery code expires after 15 minutes.", encoding="utf-8")
    store = KnowledgeAssetStore(tmp_path / "assets")
    asset = LocalExtractionBackend().extract(source, store=store)
    provider = AssetKnowledgeProvider(store)
    hit_provider = provider.search("recovery code").items[0]
    model = DemoModel(
        [
            {"tool_calls": [call("search", {"query": "recovery code"})]},
            {"tool_calls": [call("read", {"locator": hit_provider.locator.to_json()})]},
            {"content": "The code expires after 15 minutes."},
        ]
    )

    result = AgentLoop(model).run("When does the recovery code expire?", provider)

    assert result.evidence[0].derived_from == asset.asset_id
    assert result.evidence[0].content_hash
    assert result.evidence[0].source_revision == asset.source_hash
    assert result.evidence[0].evidence_id in result.answer


def test_reingestion_keeps_resource_identity_and_invalidates_old_locator(tmp_path):
    source = tmp_path / "guide.md"
    source.write_text("# Guide\n\nOld policy text.", encoding="utf-8")
    store = KnowledgeAssetStore(tmp_path / "assets")
    first_asset = LocalExtractionBackend().extract(source, store=store)
    old_locator = AssetKnowledgeProvider(store).search("old policy").items[0].locator

    source.write_text("# Guide\n\nUpdated policy text.", encoding="utf-8")
    second_asset = LocalExtractionBackend().extract(source, store=store)
    provider = AssetKnowledgeProvider(store)

    assert first_asset.asset_id == second_asset.asset_id
    assert first_asset.source_hash != second_asset.source_hash
    with pytest.raises(KRStaleLocator):
        provider.read(old_locator)
