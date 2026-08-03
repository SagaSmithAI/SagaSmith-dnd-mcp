from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from sagasmith_dnd_mcp.config import McpConfig
from scripts import regression_addons as driver


class _FakeServer:
    def __init__(self, package: dict[str, Any]) -> None:
        self.package = package
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[None, Any]:
        self.calls.append((name, arguments))
        responses: dict[int, Any] = {
            1: {"id": "campaign"},
            2: {
                "result": {
                    "installed": True,
                    "activated": False,
                    "components": [{"kind": "rule_pack", "status": "installed"}],
                }
            },
            3: {"result": {"package": self.package}},
            4: {"result": {"campaign_revision": 1}},
            5: {"result": {"activation": {"enabled": True}}},
            6: {"result": []},
            7: {"result": {"revision": 2}},
            8: {"result": {"activation": {"enabled": False}}},
            9: {"result": {"package": self.package}},
        }
        return None, responses[len(self.calls)]


def _package() -> dict[str, Any]:
    return {
        "kind": "addon_pack",
        "id": "dnd5e.example.addon",
        "version": "1.0.0",
        "checksum": "a" * 64,
        "metadata": {"distribution": "private"},
        "payload": {
            "manifest": {
                "classification": "third_party",
                "content_summary": {"feature": 1},
                "resolution_readiness": {
                    "complete": True,
                    "first_use_compilation_required": False,
                    "unresolved": [],
                },
            },
            "components": [],
        },
    }


def test_addon_directory_regression_uses_only_public_mcp_calls(
    tmp_path: Path, monkeypatch
) -> None:
    package = _package()
    package_path = tmp_path / "example.addon.sagasmith.json"
    package_path.write_text(json.dumps(package), encoding="utf-8")
    base = McpConfig(
        home=tmp_path / "base",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd-skills",
        modulegen_skills_dir=tmp_path / "modulegen-skills",
        auto_seed_rules=False,
    )
    fake = _FakeServer(package)
    monkeypatch.setattr(driver.McpConfig, "from_environment", lambda: base)
    monkeypatch.setattr(driver, "create_server", lambda _config: fake)
    args = argparse.Namespace(
        roots=[tmp_path],
        home=tmp_path / "target",
        edition="2014",
        locale="en",
        run_id="test",
        output=None,
    )

    report = asyncio.run(driver._run(args))

    assert report["passed"] is True
    assert report["package_count"] == 1
    assert report["packages"][0]["reexport_identical"] is True
    assert [name for name, _arguments in fake.calls] == [
        "campaign_create",
        "rule_import",
        "rule_pack_query",
        "campaign_rules",
        "campaign_rules",
        "rule_pack_query",
        "campaign_query",
        "campaign_rules",
        "rule_pack_query",
    ]
    assert fake.calls[4][1]["idempotency_key"].endswith("-r1")
    assert fake.calls[7][1]["idempotency_key"].endswith("-r2")
    assert fake.calls[8][1]["view"] == "addon_package"
