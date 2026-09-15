from __future__ import annotations

from pathlib import Path

import yaml

from knowledge_runtime.v2.cli import build_parser, v2_command_names


def test_v2_cli_exposes_v2_operations() -> None:
    assert "v2" in build_parser().format_help()
    assert set(v2_command_names()) == {
        "schema-migrate",
        "ingest",
        "search",
        "evidence",
        "ask",
        "status",
        "rebuild-index",
    }


def test_compose_contains_only_kr_v2_owned_names() -> None:
    document = yaml.safe_load(Path("deploy/kr-v2.compose.yml").read_text(encoding="utf-8"))
    assert document["networks"]["kr-v2-net"]["name"] == "kr-v2-net"
    assert all(name.startswith("kr-v2-") for name in document["volumes"])
    assert all(service["container_name"].startswith("kr-v2-") for service in document["services"].values())
    assert "prune" not in Path("scripts/kr-v2.ps1").read_text(encoding="utf-8").casefold()

