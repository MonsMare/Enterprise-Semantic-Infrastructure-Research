from __future__ import annotations

import pytest

from knowledge_runtime.errors import KRStaleLocator
from knowledge_runtime.models import Locator
from knowledge_runtime.v2.compat import LegacyProviderAdapter
from tests.v2.test_context import make_runtime


def test_legacy_search_and_read_map_to_v2_evidence() -> None:
    adapter = LegacyProviderAdapter(make_runtime())
    page = adapter.search("reserve margin")
    assert page.items
    evidence = adapter.read(page.items[0].locator)
    assert evidence.content_hash


def test_legacy_read_rejects_currently_stale_locator() -> None:
    adapter = LegacyProviderAdapter(make_runtime())
    locator = Locator(1, "v2", "doc-1", "old-rev", {"element_id": "el-1"})
    with pytest.raises(KRStaleLocator):
        adapter.read(locator)

