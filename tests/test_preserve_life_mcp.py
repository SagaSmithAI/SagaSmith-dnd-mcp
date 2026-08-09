from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server

PRESERVE_LIFE_ID = "dnd5e.content.srd2024.feature.life-domain-preserve-life"


def _config(tmp_path: Path) -> McpConfig:
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


async def _raw(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result


def _cleric_sheet() -> dict:
    sheet = default_character_sheet()
    sheet["edition"] = "2024"
    sheet["progression"]["level"] = 3
    sheet["progression"]["classes"] = [
        {"name": "Cleric", "level": 3, "subclass": "Life Domain", "hit_die": 8}
    ]
    sheet["resources"]["channel_divinity"] = {
        "label": "Channel Divinity",
        "value": 2,
        "max": 2,
        "recovers_on": "long_rest",
        "recovery_amounts": {"short_rest": 1, "long_rest": "all"},
        "source_key": "Cleric",
    }
    sheet["content"]["features"] = [
        {
            "id": PRESERVE_LIFE_ID,
            "name": "Preserve Life",
            "source_key": "Life Domain",
            "description": "Restore Hit Points to Bloodied creatures within 30 feet.",
            "resource_key": "channel_divinity",
            "activation": {"type": "action", "cost": 1, "trigger": ""},
            "choices": {"action_kind": "magic"},
            "mechanic_refs": ["dnd5e.core.activity.preserve_life"],
        }
    ]
    return sheet


def _wounded_undead_sheet() -> dict:
    sheet = default_character_sheet()
    sheet["edition"] = "2024"
    sheet["progression"]["species"] = "undead"
    sheet["combat"]["hp"] = {"value": 1, "max": 20, "temp": 0}
    return sheet


def test_preserve_life_is_a_combat_transaction_with_engine_measured_range(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Preserve Life", "edition": "2024", "idempotency_key": "campaign"},
        )
        cleric = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Cleric",
                    "sheet": _cleric_sheet(),
                },
                "principal_id": "system:local",
                "idempotency_key": "cleric",
            },
        )
        near = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Near Undead",
                    "sheet": _wounded_undead_sheet(),
                },
                "principal_id": "system:local",
                "idempotency_key": "near",
            },
        )
        far = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Far Undead",
                    "sheet": _wounded_undead_sheet(),
                },
                "principal_id": "system:local",
                "idempotency_key": "far",
            },
        )
        campaign = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        phase = await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": campaign["revision"],
                "idempotency_key": "play",
            },
        )
        started = await _raw(
            server,
            "combat_start",
            {
                "positioning_mode": "grid",
                "battle_map": {"width_cells": 40, "height_cells": 40},
                "campaign_id": campaign["id"],
                "participant_ids": [cleric["id"], near["id"], far["id"]],
                "participant_config": [
                    {
                        "actor_id": cleric["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                        "disposition": "friendly",
                    },
                    {
                        "actor_id": near["id"],
                        "initiative": 10,
                        "position": {"x": 3, "y": 0},
                        "disposition": "friendly",
                    },
                    {
                        "actor_id": far["id"],
                        "initiative": 5,
                        "position": {"x": 7, "y": 0},
                        "disposition": "friendly",
                    },
                ],
                "expected_revision": phase["campaign_revision"],
                "idempotency_key": "start",
            },
        )

        with pytest.raises(Exception, match="outside 30 feet"):
            await _raw(
                server,
                "combat_use_activity",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": cleric["id"],
                    "activity_id": PRESERVE_LIFE_ID,
                    "declaration": {"allocations": [{"target_id": far["id"], "amount": 9}]},
                    "expected_revision": started["campaign_revision"],
                    "idempotency_key": "far-invalid",
                },
            )

        unchanged = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": cleric["id"]},
                "principal_id": "system:local",
            },
        )
        assert unchanged["sheet"]["resources"]["channel_divinity"]["value"] == 2
        current = started["combat"]["combatants"][started["combat"]["turn_index"]]
        assert current["turn_budget"]["main_action"] == 1

        resolved = await _raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": cleric["id"],
                "activity_id": PRESERVE_LIFE_ID,
                "declaration": {"allocations": [{"target_id": near["id"], "amount": 9}]},
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "near-valid",
            },
        )

        assert resolved["status"] == "committed"
        effect = resolved["result"]["core_effect"]
        assert effect["kind"] == "preserve_life"
        assert effect["edition"] == "2024"
        assert effect["pool"] == 15
        assert effect["allocated"] == 9
        assert effect["targets"][0]["after_hp"] == 10
        assert any(
            receipt["mechanic_id"] == "dnd5e.core.activity.preserve_life"
            for receipt in resolved["result"]["rule_receipts"]
        )
        current = resolved["combat"]["combatants"][resolved["combat"]["turn_index"]]
        assert current["turn_budget"]["main_action"] == 0
        cleric_after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": cleric["id"]},
                "principal_id": "system:local",
            },
        )
        near_after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": near["id"]},
                "principal_id": "system:local",
            },
        )
        assert cleric_after["sheet"]["resources"]["channel_divinity"]["value"] == 1
        assert near_after["sheet"]["combat"]["hp"]["value"] == 10

    asyncio.run(exercise())
