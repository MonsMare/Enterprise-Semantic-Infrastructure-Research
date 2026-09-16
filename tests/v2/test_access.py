from __future__ import annotations

import pytest

from knowledge_runtime.errors import KRStaleLocator
from knowledge_runtime.models import Locator, ReadOptions
from knowledge_runtime.v2.access import KnowledgeAccessRuntime
from tests.v2.test_context import make_runtime


def test_access_search_returns_locator_and_read_is_the_only_body_operation() -> None:
    access = KnowledgeAccessRuntime(make_runtime())

    hit = access.search("reserve margin").items[0]
    status = access.stat(hit.locator)
    evidence = access.read(hit.locator, ReadOptions(max_bytes=10_000))

    assert hit.locator.selector["element_id"] == "el-1"
    assert "content" not in status
    assert evidence.content == "The reserve margin is twelve percent for annuity risk."


def test_access_read_rejects_stale_locator_before_source_body_is_returned() -> None:
    access = KnowledgeAccessRuntime(make_runtime())
    stale = Locator(1, "v2", "doc-1", "old-rev", {"element_id": "el-1"})

    with pytest.raises(KRStaleLocator):
        access.read(stale)
