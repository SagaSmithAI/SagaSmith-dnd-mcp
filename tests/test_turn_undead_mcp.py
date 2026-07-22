from __future__ import annotations

import asyncio
import random
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server

CHANNEL_DIVINITY_ID = "dnd5e.content.srd2014.feature.cleric-channel-divinity"


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
    sheet["edition"] = "2014"
    sheet["progression"]["level"] = 2
    sheet["progression"]["classes"] = [
        {"name": "Cleric", "level": 2, "subclass": "", "hit_die": 8}
    ]
    sheet["abilities"]["wisdom"]["score"] = 16
    sheet["spellcasting"]["ability"] = "wisdom"
    sheet["resources"]["channel_divinity"] = {
        "label": "Channel Divinity",
        "value": 1,
        "max": 1,
        "recovers_on": "short_rest",
        "source_key": "Cleric",
    }
    sheet["content"]["features"] = [
        {
            "id": CHANNEL_DIVINITY_ID,
            "name": "Channel Divinity",
            "source_key": "Cleric",
            "description": "As an action, present your holy symbol and turn undead.",
            "uses": {"label": "", "value": 0, "max": 0, "recovers_on": "none"},
            "resource_key": "channel_divinity",
            "activation": {"type": "action", "cost": 1, "trigger": ""},
            "scaling": [],
            "choices": {"options": ["Turn Undead", "selected-domain option"]},
        }
    ]
    return sheet


def _undead_sheet() -> dict:
    sheet = default_character_sheet()
    sheet["edition"] = "2014"
    sheet["progression"]["species"] = "undead"
    sheet["combat"]["hp"] = {"value": 12, "max": 12, "temp": 0}
    return sheet


def test_turn_undead_preflights_then_commits_all_actors_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = server_module.resolve_turn_undead_to_sheets

    def deterministic_turn(source_actor, target_actors, **kwargs):
        return original(
            source_actor,
            target_actors,
            **kwargs,
            rng=random.Random(1),
        )

    monkeypatch.setattr(
        server_module, "resolve_turn_undead_to_sheets", deterministic_turn
    )

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Turn Undead", "edition": "2014", "idempotency_key": "campaign"},
        )
        cleric = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Cleric",
                "sheet": _cleric_sheet(),
                "idempotency_key": "cleric",
            },
        )
        undead = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Skeleton",
                "sheet": _undead_sheet(),
                "idempotency_key": "undead",
            },
        )
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
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
                "campaign_id": campaign["id"],
                "participant_ids": [cleric["id"], undead["id"]],
                "participant_config": [
                    {
                        "actor_id": cleric["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                        "disposition": "friendly",
                    },
                    {
                        "actor_id": undead["id"],
                        "initiative": 10,
                        "position": {"x": 3, "y": 0},
                        "disposition": "hostile",
                    },
                ],
                "expected_revision": phase["campaign_revision"],
                "idempotency_key": "start",
            },
        )

        with pytest.raises(
            Exception, match="adjudicate every living undead within 30 feet"
        ):
            await _raw(
                server,
                "combat_use_activity",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": cleric["id"],
                    "activity_id": CHANNEL_DIVINITY_ID,
                    "declaration": {"option": "turn_undead", "perception": []},
                    "expected_revision": started["campaign_revision"],
                    "idempotency_key": "invalid",
                },
            )

        unchanged = await _call(server, "character_get", {"character_id": cleric["id"]})
        assert unchanged["sheet"]["resources"]["channel_divinity"]["value"] == 1
        current = started["combat"]["combatants"][started["combat"]["turn_index"]]
        assert current["turn_budget"]["main_action"] == 1

        resolved = await _raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": cleric["id"],
                "activity_id": CHANNEL_DIVINITY_ID,
                "declaration": {
                    "option": "turn_undead",
                    "perception": [
                        {"target_id": undead["id"], "can_see_or_hear": True}
                    ],
                },
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "valid",
            },
        )

        assert resolved["status"] == "committed"
        assert resolved["result"]["requires_ruling"] is False
        effect = resolved["result"]["core_effect"]
        assert effect["kind"] == "turn_undead"
        assert effect["save_dc"] == 13
        assert effect["targets"][0]["turned"] is True
        assert any(
            item["mechanic_id"] == "dnd5e.core.activity.turn_undead"
            for item in resolved["result"]["rule_receipts"]
        )
        current = resolved["combat"]["combatants"][resolved["combat"]["turn_index"]]
        assert current["turn_budget"]["main_action"] == 0
        undead_combatant = next(
            item
            for item in resolved["combat"]["combatants"]
            if item["actor_id"] == undead["id"]
        )
        assert undead_combatant["turned"]["source_actor_id"] == cleric["id"]
        assert undead_combatant["turn_budget"]["reaction"] == 0

        cleric_after = await _call(
            server, "character_get", {"character_id": cleric["id"]}
        )
        undead_after = await _call(
            server, "character_get", {"character_id": undead["id"]}
        )
        assert cleric_after["sheet"]["resources"]["channel_divinity"]["value"] == 0
        assert "turned" in undead_after["sheet"]["conditions"]
        assert undead_after["sheet"]["effects"][-1]["kind"] == "turn_undead"

    asyncio.run(exercise())
