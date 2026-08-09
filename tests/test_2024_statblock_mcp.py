from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server

STATBLOCK_2024 = """# Test Beast
*Medium Beast, Unaligned*

**AC** 12
**HP** 10 (3d8 - 3)
**Speed** 30 ft.
**Initiative** +1 (11)

STR 10 (+0) | +0
DEX 12 (+1) | +1
CON 8 (-1) | -1
INT 4 (-3) | -3
WIS 10 (+0) | +0
CHA 6 (-2) | -2

**Senses** Passive Perception 10
**Languages** None
**CR** 1/4 (XP 50; PB +2)

## Actions
**Bite.** *Melee Attack Roll:* +3, reach 5 ft., one target. *Hit:* 4 (1d6 + 1) Piercing damage.
"""


def _config(tmp_path: Path) -> McpConfig:
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=True,
    )


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def test_module_review_and_actor_creation_use_the_2024_statblock_parser(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "2024 statblock", "edition": "2024", "idempotency_key": "campaign"},
        )
        staged = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "name": "test-beast.md",
                    "content": f"# Test Bestiary\n\n## Test Beast\n\n{STATBLOCK_2024}",
                    "source_key": "test-beast",
                    "title": "Test Bestiary",
                },
                "idempotency_key": "stage",
            },
        )
        job_id = staged["job"]["id"]
        ingested = staged
        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "expected_revision": current["revision"],
                "idempotency_key": "activate",
            },
        )
        index = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "index",
                "payload": {"module_id": ingested["module_id"]},
            },
        )
        candidates = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "candidates",
                "payload": {"module_id": ingested["module_id"]},
            },
        )
        assert all(
            item["content_kind"] == "dnd5e_2024_statblock"
            for item in candidates
        )
        hits = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "evidence",
                "payload": {
                    "module_id": ingested["module_id"],
                    "kind": "chunks",
                    "query": "Passive Perception 10",
                },
            },
        )
        chunk_id = hits[0]["id"]
        scene = next(
            item
            for item in index
            if item["scene_id"] == hits[0]["scene_id"]
        )

        with pytest.raises(Exception, match="dnd5e_2024_statblock"):
            await _call(
                server,
                "module_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "edit", "payload": {"operation": "content",
                        "module_id": ingested["module_id"],
                        "scene_id": scene["scene_id"],
                        "content_key": "wrong-edition",
                        "normalized_content": STATBLOCK_2024,
                        "observation": "Reviewed against the exact indexed source chunk.",
                        "source_chunk_ids": [chunk_id],
                        "content_kind": "dnd5e_2014_statblock",
                    },
                    "idempotency_key": "wrong-edition",
                },
            )

        reviewed = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit", "payload": {"operation": "content",
                    "module_id": ingested["module_id"],
                    "scene_id": scene["scene_id"],
                    "content_key": "test-beast",
                    "normalized_content": STATBLOCK_2024,
                    "observation": "Reviewed against the exact indexed source chunk.",
                    "source_chunk_ids": [chunk_id],
                },
                "idempotency_key": "review",
            },
        )

        assert reviewed["review"]["content_kind"] == "dnd5e_2024_statblock"
        created = await _call(
            server,
            "character_create_from",
            {
                "mode": "module_statblock",
                "payload": {
                    "campaign_id": campaign["id"],
                    "review_id": reviewed["review"]["id"],
                    "character_type": "monster",
                },
                "idempotency_key": "create",
            },
        )

        assert created["character"]["name"] == "Test Beast"
        assert created["character"]["sheet"]["edition"] == "2024"
        assert created["character"]["derived"]["armor_class"] == 12
        assert created["statblock"]["challenge_rating"] == "1/4"
        assert any(
            "SRD 5.2.1 presentation fields normalized"
            in item
            for item in created["statblock"]["normalization_notes"]
        )

    asyncio.run(exercise())
