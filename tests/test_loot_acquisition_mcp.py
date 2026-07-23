from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    value = result.get("result", result) if isinstance(result, dict) else result
    if isinstance(value, dict) and "action" in value and "result" in value:
        return value["result"]
    return value


def test_source_bound_loot_is_atomic_idempotent_and_branch_audited(tmp_path: Path) -> None:
    module_root = tmp_path / "modules"
    module_root.mkdir()
    source = module_root / "adventure.md"
    source.write_text(
        "# Chapter One\n\n"
        "## Treasure Room\n\n"
        "The chest contains 60 cp, two healing draughts, and a jade frog.\n",
        encoding="utf-8",
    )
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        module_import_roots=(module_root,),
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Loot acquisition", "idempotency_key": "campaign"},
        )
        staged = await _call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "stage",
                "payload": {
                    "source_path": str(source),
                    "source_key": "loot-module",
                    "title": "Loot Module",
                },
                "idempotency_key": "stage",
            },
        )
        job_id = staged["job"]["id"]
        for action in ("inspect", "validate"):
            await _call(
                server,
                "module_import",
                {
                    "campaign_id": campaign["id"],
                    "action": action,
                    "payload": {"job_id": job_id},
                    "idempotency_key": action,
                },
            )
        ingested = await _call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "ingest",
                "payload": {"job_id": job_id},
                "idempotency_key": "ingest",
            },
        )
        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        await _call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "activate",
                "payload": {"job_id": job_id},
                "expected_revision": current["revision"],
                "idempotency_key": "activate",
            },
        )
        search = await _call(
            server,
            "module_search",
            {
                "campaign_id": campaign["id"],
                "query": "jade frog",
                "top_k": 3,
            },
        )
        chunk_id = search[0]["id"]
        expanded = await _call(server, "module_expand", {"chunk_id": chunk_id})
        source_ref = json.dumps(
            {
                "module_id": ingested["module_id"],
                "scene_id": expanded["scene"]["id"],
                "chunk_id": chunk_id,
                "content_sha256": hashlib.sha256(
                    expanded["content"].encode("utf-8")
                ).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        play = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "update",
                "payload": {"state": {**current["state"], "game_phase": "play"}},
                "expected_revision": current["revision"],
                "idempotency_key": "play",
            },
        )
        arguments = {
            "campaign_id": campaign["id"],
            "action": "loot_acquire",
            "payload": {
                "acquisition_id": "chapter-one-chest",
                "coins": {"cp": 60},
                "items": [
                    {
                        "id": "healing-draught",
                        "name": "Healing draught",
                        "kind": "consumable",
                        "quantity": 2,
                    },
                    {
                        "id": "jade-frog",
                        "name": "Jade frog",
                        "kind": "loot",
                        "quantity": 1,
                        "price_cp": 4000,
                    },
                ],
                "reason": "The party opened the source-defined treasure chest.",
                "source_ref": source_ref,
            },
            "expected_revision": play["revision"],
            "idempotency_key": "loot",
        }
        invalid_source = json.loads(source_ref)
        invalid_source["content_sha256"] = "0" * 64
        with pytest.raises(Exception, match="content_sha256 does not match"):
            await _call(
                server,
                "campaign_change",
                {
                    **arguments,
                    "payload": {
                        **arguments["payload"],
                        "source_ref": json.dumps(
                            invalid_source, sort_keys=True, separators=(",", ":")
                        ),
                    },
                    "idempotency_key": "invalid-source",
                },
            )
        acquired = await _call(server, "campaign_change", arguments)
        replay = await _call(server, "campaign_change", arguments)

        assert replay == acquired
        assert acquired["status"] == "committed"
        assert acquired["party"]["inventory"]["wallet"]["cp"] == 60
        assert [item["id"] for item in acquired["items"]] == [
            "healing-draught",
            "jade-frog",
        ]
        assert acquired["campaign"]["revision"] == play["revision"] + 1
        assert acquired["campaign"]["state"]["loot_acquisitions"][0]["id"] == (
            "chapter-one-chest"
        )
        with pytest.raises(Exception, match="acquisition_id already exists"):
            await _call(
                server,
                "campaign_change",
                {
                    **arguments,
                    "expected_revision": acquired["campaign"]["revision"],
                    "idempotency_key": "duplicate-loot",
                },
            )

    asyncio.run(exercise())
