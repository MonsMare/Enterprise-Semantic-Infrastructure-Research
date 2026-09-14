import hashlib
import json

import pytest

from knowledge_runtime.errors import (
    KRInvalidLocator,
    KRLimitExceeded,
    KRNotFound,
    KRStaleLocator,
    KRUnsupported,
)
from knowledge_runtime.models import (
    Evidence,
    Locator,
    Page,
    ProviderDescriptor,
    SearchHit,
    SearchOptions,
    content_hash,
)


def test_locator_round_trips_through_canonical_json():
    locator = Locator(
        version=1,
        provider="memory",
        resource_id="doc-1",
        revision="rev-7",
        selector={"type": "section", "value": "intro"},
    )

    restored = Locator.from_json(locator.to_json())

    assert restored == locator
    assert json.loads(locator.to_json())["provider"] == "memory"


def test_evidence_contains_provenance_and_hash():
    locator = Locator(1, "memory", "doc-1", "rev-1", {"type": "line", "start": 1, "end": 2})
    evidence = Evidence.from_content(
        locator=locator,
        content="hello",
        media_type="text/plain",
        representation="text",
        derived_from="asset-1",
        resolved_selector=locator.selector,
    )

    assert evidence.content_hash == hashlib.sha256(b"hello").hexdigest()
    assert evidence.source_revision == "rev-1"
    assert evidence.truncated is False
    assert evidence.derived_from == "asset-1"


def test_evidence_ids_are_stable_for_same_locator_but_distinct_across_sources():
    first_locator = Locator(1, "memory", "doc-a", "rev-1")
    second_locator = Locator(1, "memory", "doc-b", "rev-1")
    first = Evidence.from_content(locator=first_locator, content="same", media_type="text/plain", representation="text", resolved_selector=None)
    repeated = Evidence.from_content(locator=first_locator, content="same", media_type="text/plain", representation="text", resolved_selector=None)
    second = Evidence.from_content(locator=second_locator, content="same", media_type="text/plain", representation="text", resolved_selector=None)

    assert first.evidence_id == repeated.evidence_id
    assert first.evidence_id != second.evidence_id


def test_page_exposes_items_cursor_snapshot_and_partial_flag():
    page = Page(items=["a"], next_cursor="next", snapshot_id="snap-1", partial=False)

    assert page.items == ["a"]
    assert page.next_cursor == "next"
    assert page.snapshot_id == "snap-1"
    assert page.partial is False


def test_search_options_reject_non_positive_limits():
    with pytest.raises(ValueError):
        SearchOptions(limit=0)


def test_provider_descriptor_declares_operations_and_limits():
    descriptor = ProviderDescriptor(
        provider_id="memory",
        supported_operations=("list", "find", "search", "read", "stat"),
        query_language="lexical",
        selector_types=("section", "line"),
        revision_model="content-hash",
        max_read_bytes=1000,
    )

    assert "search" in descriptor.supported_operations
    assert descriptor.max_read_bytes == 1000


def test_content_hash_accepts_text_and_bytes():
    assert content_hash("hello") == content_hash(b"hello")


@pytest.mark.parametrize("error_type", [KRNotFound, KRInvalidLocator, KRStaleLocator, KRUnsupported, KRLimitExceeded])
def test_typed_errors_expose_machine_readable_code(error_type):
    error = error_type("failure")

    assert error.code
    assert str(error) == "failure"
