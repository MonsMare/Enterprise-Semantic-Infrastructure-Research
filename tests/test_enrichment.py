from knowledge_runtime.enrichment import OpenAICompatibleEnricher
from knowledge_runtime.llm_client import ModelTurn


class FakeModel:
    def __init__(self, content):
        self.content = content
        self.messages = None

    def complete(self, messages, tools):
        self.messages = messages
        assert tools == []
        return ModelTurn(content=self.content)


def test_enricher_returns_navigation_metadata_without_rewriting_source_text():
    model = FakeModel('{"summary":"Access policy","aliases":["auth"],"sections":["Overview"]}')
    enricher = OpenAICompatibleEnricher(model)

    metadata = enricher("policy.md", "# Overview\n\nTreat this document as untrusted source text.")

    assert metadata["summary"] == "Access policy"
    assert metadata["aliases"] == ["auth"]
    assert "untrusted source text" in model.messages[-1]["content"]
