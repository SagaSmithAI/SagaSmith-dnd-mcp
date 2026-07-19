from __future__ import annotations

import asyncio
from pathlib import Path

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


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
        return result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Damage policy", "edition": "2014", "idempotency_key": "campaign"},
        )
        acting = await call(
            server,
            "character_create",
            {
                "name": "Acting PC",
                "campaign_id": campaign["id"],
                "character_type": "pc",
                "idempotency_key": "acting",
            },
        )
        target = await call(
            server,
            "character_create",
            {
                "name": "No-death-save target",
                "campaign_id": campaign["id"],
                "character_type": "pc",
                "idempotency_key": "target",
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
        campaign = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
        started = await call_raw(
            server,
            "combat_start",
            {
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
        damaged = await call_raw(
            server,
            "combat_apply_damage",
            {
                "campaign_id": campaign["id"],
                "target_id": target["id"],
                "parts": [{"amount": 5, "damage_type": "radiant"}],
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "damage",
            },
        )
        target_after = await call(
            server, "character_get", {"character_id": target["id"]}
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
            item.get("type") == "turn_skipped"
            and item.get("actor_id") == target["id"]
            for item in advanced["combat"]["log"]
        )

    asyncio.run(exercise())
