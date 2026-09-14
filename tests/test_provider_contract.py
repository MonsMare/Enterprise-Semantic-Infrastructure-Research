from pathlib import Path

import pytest

from knowledge_runtime.errors import KRCursorInvalid, KRInvalidLocator, KRLimitExceeded, KRQueryInvalid, KRStaleLocator
from knowledge_runtime.file_provider import FileProvider
from knowledge_runtime.memory_provider import MemoryProvider
from knowledge_runtime.models import ReadOptions, SearchOptions


DOCUMENTS = {
    "guide.md": "# Guide\n\nAuthentication uses sessions.\n\n## Recovery\nRotate the key.",
    "runbook.txt": "Restart the service after changing configuration.\nAuthentication failures are logged.",
}


@pytest.fixture(params=["memory", "file"])
def provider(request, tmp_path: Path):
    if request.param == "memory":
        return MemoryProvider(DOCUMENTS)
    for name, content in DOCUMENTS.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    return FileProvider(tmp_path)


def test_list_and_find_return_stable_resources(provider):
    page = provider.list(limit=10)
    found = provider.find("guide", limit=10)

    assert [item.name for item in page.items] == ["guide.md", "runbook.txt"]
    assert [item.name for item in found.items] == ["guide.md"]


def test_search_hit_locator_can_be_read_as_evidence(provider):
    page = provider.search("authentication", options=SearchOptions(limit=10))

    assert len(page.items) == 2
    evidence = provider.read(page.items[0].locator, ReadOptions(max_bytes=500))

    assert "authentication" in str(evidence.content).lower()
    assert evidence.locator == page.items[0].locator
    assert evidence.content_hash


def test_search_pagination_has_no_duplicates(provider):
    first = provider.search("authentication", options=SearchOptions(limit=1))
    second = provider.search("authentication", options=SearchOptions(limit=1, cursor=first.next_cursor))

    assert first.next_cursor is not None
    assert len(second.items) == 1
    assert first.items[0].locator.resource_id != second.items[0].locator.resource_id
    assert first.snapshot_id == second.snapshot_id


def test_read_applies_max_bytes_and_marks_truncation(provider):
    resource = provider.list(limit=1).items[0]
    evidence = provider.read(resource.locator, ReadOptions(max_bytes=8))

    assert len(str(evidence.content).encode("utf-8")) <= 8
    assert evidence.truncated is True


def test_read_rejects_stale_locator(provider):
    resource = provider.list(limit=1).items[0]
    with pytest.raises(KRStaleLocator):
        provider.read(resource.locator.__class__(1, resource.locator.provider, resource.locator.resource_id, "old", None))


def test_read_rejects_unknown_selector(provider):
    resource = provider.list(limit=1).items[0]
    locator = resource.locator.__class__(
        1,
        resource.locator.provider,
        resource.locator.resource_id,
        resource.locator.revision,
        {"type": "unknown"},
    )

    with pytest.raises(KRInvalidLocator):
        provider.read(locator)


def test_file_provider_detects_source_change_after_search(tmp_path):
    source = tmp_path / "guide.md"
    source.write_text("The token expires tomorrow.", encoding="utf-8")
    provider = FileProvider(tmp_path)
    locator = provider.search("token").items[0].locator
    source.write_text("The token expires today.", encoding="utf-8")

    with pytest.raises(KRStaleLocator):
        provider.read(locator)


def test_cursor_is_bound_to_query_and_snapshot(provider):
    first = provider.search("authentication", options=SearchOptions(limit=1))

    with pytest.raises(KRCursorInvalid):
        provider.search("service", options=SearchOptions(limit=1, cursor=first.next_cursor))


def test_search_rejects_empty_query(provider):
    with pytest.raises(KRQueryInvalid):
        provider.search("   ")


def test_page_size_has_a_hard_upper_bound(provider):
    with pytest.raises(KRLimitExceeded):
        provider.list(limit=201)


def test_search_options_expose_hybrid_ranking_defaults():
    options = SearchOptions()

    assert options.semantic_weight == 0.35
    assert options.rrf_k == 60


@pytest.mark.parametrize(
    "kwargs",
    [
        {"semantic_weight": -0.01},
        {"semantic_weight": 1.01},
        {"rrf_k": 0},
        {"rrf_k": -1},
        {"rrf_k": 1.5},
    ],
)
def test_search_options_reject_invalid_hybrid_ranking_values(kwargs):
    with pytest.raises(ValueError):
        SearchOptions(**kwargs)
