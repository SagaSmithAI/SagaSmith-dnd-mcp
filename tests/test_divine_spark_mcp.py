from __future__ import annotations

import asyncio
import random
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server

DIVINE_SPARK_ID = "dnd5e.content.srd2024.feature.cleric-channel-divinity"


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
    value = result.get("result", result) if isinstance(result, dict) else result
    if isinstance(value, dict) and "action" in value and "result" in value:
        return value["result"]
    return value


async def _raw(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result


def _cleric_sheet() -> dict:
    sheet = default_character_sheet()
    sheet["edition"] = "2024"
    sheet["progression"] = {
        "level": 7,
        "classes": [{"name": "Cleric", "level": 7, "subclass": "", "hit_die": 8}],
    }
    sheet["abilities"]["wisdom"]["score"] = 16
    sheet["spellcasting"]["ability"] = "wisdom"
    sheet["resources"]["channel_divinity"] = {
        "label": "Channel Divinity",
        "value": 2,
        "max": 3,
        "recovers_on": "long_rest",
        "recovery_amounts": {"short_rest": 1, "long_rest": "all"},
        "source_key": "Cleric",
    }
    sheet["content"]["features"] = [
        {
            "id": DIVINE_SPARK_ID,
            "name": "Channel Divinity",
            "source_key": "Cleric",
            "description": "Use Divine Spark or Turn Undead.",
            "resource_key": "channel_divinity",
            "activation": {"type": "action", "cost": 1, "trigger": ""},
            "choices": {
                "options": ["Divine Spark", "Turn Undead"],
                "action_kind": "magic",
            },
            "mechanic_refs": [
                "dnd5e.core.activity.divine_spark",
                "dnd5e.core.activity.turn_undead",
            ],
        }
    ]
    return sheet


def _target_sheet(*, current_hp: int = 30) -> dict:
    sheet = default_character_sheet()
    sheet["edition"] = "2024"
    sheet["combat"]["hp"] = {"value": current_hp, "max": 30, "temp": 0}
    sheet["abilities"]["constitution"]["score"] = 10
    return sheet


def _deterministic_spark(monkeypatch: pytest.MonkeyPatch) -> None:
    original = server_module.resolve_divine_spark_to_sheet

    def deterministic(source_actor, target_actor, **kwargs):
        return original(
            source_actor,
            target_actor,
            **kwargs,
            rng=random.Random(2),
        )

    monkeypatch.setattr(
        server_module,
        "resolve_divine_spark_to_sheet",
        deterministic,
    )


def test_divine_spark_combat_measures_visibility_and_range_then_commits_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _deterministic_spark(monkeypatch)

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Divine Spark", "edition": "2024", "idempotency_key": "campaign"},
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
        target = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Target",
                    "sheet": _target_sheet(),
                },
                "principal_id": "system:local",
                "idempotency_key": "target",
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
                "battle_map": {"width_cells": 12, "height_cells": 12},
                "campaign_id": campaign["id"],
                "participant_ids": [cleric["id"], target["id"]],
                "participant_config": [
                    {
                        "actor_id": cleric["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                        "disposition": "friendly",
                    },
                    {
                        "actor_id": target["id"],
                        "initiative": 10,
                        "position": {"x": 7, "y": 0},
                        "disposition": "hostile",
                    },
                ],
                "expected_revision": phase["campaign_revision"],
                "idempotency_key": "start",
            },
        )
        declaration = {
            "option": "divine_spark",
            "target_id": target["id"],
            "mode": "damage",
            "damage_type": "radiant",
        }
        with pytest.raises(Exception, match="outside 30 feet"):
            await _raw(
                server,
                "combat_use_activity",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": cleric["id"],
                    "activity_id": DIVINE_SPARK_ID,
                    "declaration": declaration,
                    "expected_revision": started["campaign_revision"],
                    "idempotency_key": "too-far",
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

        moved = await _call(
            server,
            "combat_movement",
            {
                "campaign_id": campaign["id"],
                "actor_id": cleric["id"],
                "action": "move",
                "payload": {
                    "distance": 5,
                    "destination": {"x": 1, "y": 0},
                    "path": [{"x": 1, "y": 0}],
                },
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "move",
            },
        )
        resolved = await _raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": cleric["id"],
                "activity_id": DIVINE_SPARK_ID,
                "declaration": declaration,
                "expected_revision": moved["campaign_revision"],
                "idempotency_key": "valid",
            },
        )
        assert resolved["status"] == "committed"
        effect = resolved["result"]["core_effect"]
        assert effect["kind"] == "divine_spark"
        assert effect["mode"] == "damage"
        assert effect["damage_type"] == "radiant"
        assert effect["requires_ruling"] is False
        assert any(
            item["mechanic_id"] == "dnd5e.core.activity.divine_spark"
            for item in resolved["result"]["rule_receipts"]
        )
        cleric_after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": cleric["id"]},
                "principal_id": "system:local",
            },
        )
        target_after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": target["id"]},
                "principal_id": "system:local",
            },
        )
        assert cleric_after["sheet"]["resources"]["channel_divinity"]["value"] == 1
        assert target_after["sheet"]["combat"]["hp"]["value"] < 30

    asyncio.run(exercise())


def test_divine_spark_noncombat_uses_public_facade_and_target_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _deterministic_spark(monkeypatch)

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Divine Spark", "edition": "2024", "idempotency_key": "campaign"},
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
        target = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Target",
                    "sheet": _target_sheet(current_hp=4),
                },
                "principal_id": "system:local",
                "idempotency_key": "target",
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
        await _call(
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
        used = await _call(
            server,
            "character_action",
            {
                "character_id": cleric["id"],
                "action": "use_activity",
                "payload": {
                    "activity_id": DIVINE_SPARK_ID,
                    "declaration": {
                        "option": "divine_spark",
                        "target_id": target["id"],
                        "mode": "heal",
                        "expected_revision": target["revision"],
                        "within_30_ft": True,
                        "can_see": True,
                    },
                },
                "expected_revision": cleric["revision"],
                "idempotency_key": "heal",
            },
        )
        assert used["status"] == "committed"
        assert used["result"]["core_effect"]["mode"] == "heal"
        assert used["target"]["sheet"]["combat"]["hp"]["value"] > 4
        assert used["character"]["sheet"]["resources"]["channel_divinity"]["value"] == 1

    asyncio.run(exercise())
