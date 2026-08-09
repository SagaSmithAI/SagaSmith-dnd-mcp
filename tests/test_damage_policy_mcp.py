from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


def test_combat_end_transitions_unsettled_dying_actor_to_play_recovery(
    tmp_path: Path,
) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        if isinstance(result, dict) and "action" in result and "result" in result:
            return result["result"]
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Recovery transition", "edition": "2014", "idempotency_key": "campaign"},
        )
        sheet = default_character_sheet()
        sheet["edition"] = "2014"
        sheet["combat"]["hp"] = {"value": 0, "max": 8, "temp": 0}
        sheet["combat"]["death_saves"] = {"successes": 1, "failures": 1}
        sheet["conditions"] = ["unconscious", "prone"]
        actor = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "name": "Dying hero",
                    "campaign_id": campaign["id"],
                    "character_type": "pc",
                    "sheet": sheet,
                },
                "idempotency_key": "actor",
            },
        )
        helper = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "name": "Helper",
                    "campaign_id": campaign["id"],
                    "character_type": "pc",
                },
                "idempotency_key": "helper",
            },
        )
        campaign = await call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        started = await call(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "positioning_mode": "agent",
                "participant_ids": [actor["id"]],
                "participant_config": [
                    {"actor_id": actor["id"], "initiative": 10, "death_saves": True}
                ],
                "expected_revision": campaign["revision"],
                "idempotency_key": "start",
            },
        )
        ended = await call(
            server,
            "combat_end",
            {
                "campaign_id": campaign["id"],
                "outcome": {"status": "withdrawal", "summary": "The threat has left."},
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "end",
            },
        )

        assert ended["tool_profile"] == "play"
        assert ended["post_combat_recovery"] == [
            {
                "actor_id": actor["id"],
                "status": "dying",
                "unresolved_at_end": True,
                "death_saves": {"successes": 1, "failures": 1},
                "resolution_actions": ["heal", "stabilize", "death_save"],
            }
        ]
        assert ended["combat"]["active"] is False
        actor_after = await call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": actor["id"]}},
        )
        stabilized = await call(
            server,
            "character_state_change",
            {
                "character_id": actor["id"],
                "action": "stabilize",
                "payload": {
                    "source_actor_id": helper["id"],
                    "reason": (
                        "The Agent determines that immediate aid succeeds once initiative ends."
                    ),
                },
                "expected_revision": actor_after["revision"],
                "idempotency_key": "stabilize",
            },
        )
        assert stabilized["result"]["status"] == "stable"
        assert "stable" in stabilized["character"]["sheet"]["conditions"]

    asyncio.run(exercise())


def test_generic_damage_uses_encounter_death_save_policy_and_skips_dead_turn(
    tmp_path: Path,
) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def call_raw(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Damage policy", "edition": "2014", "idempotency_key": "campaign"},
        )
        acting = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "name": "Acting PC",
                    "campaign_id": campaign["id"],
                    "character_type": "pc",
                },
                "principal_id": "system:local",
                "idempotency_key": "acting",
            },
        )
        target = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "name": "No-death-save target",
                    "campaign_id": campaign["id"],
                    "character_type": "pc",
                },
                "principal_id": "system:local",
                "idempotency_key": "target",
            },
        )
        outsider = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "name": "Outside encounter",
                    "campaign_id": campaign["id"],
                    "character_type": "pc",
                },
                "principal_id": "system:local",
                "idempotency_key": "outsider",
            },
        )
        target_sheet = target["sheet"]
        target_sheet["combat"]["hp"] = {"value": 5, "max": 5, "temp": 0}
        target = await call(
            server,
            "character_sheet_replace",
            {
                "character_id": target["id"],
                "sheet": target_sheet,
                "expected_revision": target["revision"],
                "idempotency_key": "target-sheet",
            },
        )
        campaign = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        started = await call_raw(
            server,
            "combat_start",
            {
                "positioning_mode": "agent",
                "campaign_id": campaign["id"],
                "participant_ids": [acting["id"], target["id"]],
                "participant_config": [
                    {"actor_id": acting["id"], "initiative": 20, "death_saves": True},
                    {"actor_id": target["id"], "initiative": 10, "death_saves": False},
                ],
                "expected_revision": campaign["revision"],
                "idempotency_key": "start",
            },
        )
        with pytest.raises(Exception, match="healing target is not a combatant"):
            await call_raw(
                server,
                "combat_hp_change",
                {
                    "campaign_id": campaign["id"],
                    "target_id": outsider["id"],
                    "action": "heal",
                    "payload": {"amount": 1},
                    "principal_id": "system:local",
                    "expected_revision": started["campaign_revision"],
                    "idempotency_key": "heal-outsider",
                },
            )
        with pytest.raises(Exception, match="healing source is not a combatant"):
            await call_raw(
                server,
                "combat_hp_change",
                {
                    "campaign_id": campaign["id"],
                    "target_id": acting["id"],
                    "action": "heal",
                    "payload": {"source_actor_id": outsider["id"], "amount": 1},
                    "principal_id": "system:local",
                    "expected_revision": started["campaign_revision"],
                    "idempotency_key": "heal-from-outsider",
                },
            )
        with pytest.raises(Exception, match="check actor is not a combatant"):
            await call_raw(
                server,
                "combat_check",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": outsider["id"],
                    "kind": "save",
                    "ability": "dexterity",
                    "dc": 10,
                    "expected_revision": started["campaign_revision"],
                    "idempotency_key": "save-outsider",
                },
            )
        damaged = await call_raw(
            server,
            "combat_hp_change",
            {
                "campaign_id": campaign["id"],
                "target_id": target["id"],
                "action": "damage",
                "payload": {"parts": [{"amount": 5, "damage_type": "radiant"}]},
                "principal_id": "system:local",
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "damage",
            },
        )
        target_after = await call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": target["id"]},
                "principal_id": "system:local",
            },
        )
        assert {"dead", "prone"} <= set(target_after["sheet"]["conditions"])
        assert "unconscious" not in target_after["sheet"]["conditions"]

        advanced = await call_raw(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": acting["id"],
                "expected_revision": damaged["campaign_revision"],
                "idempotency_key": "end-turn",
            },
        )
        current = advanced["combat"]["combatants"][advanced["combat"]["turn_index"]]
        assert current["actor_id"] == acting["id"]
        assert any(
            item.get("type") == "turn_skipped" and item.get("actor_id") == target["id"]
            for item in advanced["combat"]["log"]
        )

    asyncio.run(exercise())
