from __future__ import annotations

import asyncio
import random
from pathlib import Path

from sagasmith_dnd.character_schema import default_character_sheet

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    value = result.get("result", result) if isinstance(result, dict) else result
    if isinstance(value, dict) and "action" in value and "result" in value:
        return value["result"]
    return value


def test_shared_healing_potion_use_is_atomic_rolled_and_idempotent(
    tmp_path: Path, monkeypatch
) -> None:
    original_roll = server_module.roll

    def deterministic_roll(expression: str):
        return original_roll(expression, rng=random.Random(0))

    monkeypatch.setattr(server_module, "roll", deterministic_roll)
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Healing potion",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "update",
                "payload": {"state": {**campaign["state"], "game_phase": "play"}},
                "expected_revision": campaign["revision"],
                "idempotency_key": "play",
            },
        )
        sheet = default_character_sheet()
        sheet["edition"] = "2014"
        sheet["combat"]["hp"] = {"value": 1, "max": 12, "temp": 0}
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Wounded Hero",
                    "sheet": sheet,
                },
                "idempotency_key": "actor",
            },
        )
        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        added = await _call(
            server,
            "inventory_change",
            {
                "owner": "party",
                "action": "add",
                "owner_id": campaign["id"],
                "payload": {
                    "item": {
                        "id": "healing-potions",
                        "name": "Potion of healing",
                        "kind": "consumable",
                        "quantity": 2,
                    }
                },
                "expected_revision": current["revision"],
                "idempotency_key": "potion",
            },
        )
        arguments = {
            "campaign_id": campaign["id"],
            "action": "consumable_use",
            "payload": {
                "use_id": "heal-wounded-hero",
                "item_id": "healing-potions",
                "target_character_id": actor["id"],
                "expected_character_revision": actor["revision"],
                "reason": "The wounded hero drank one potion.",
            },
            "expected_revision": added["campaign"]["revision"],
            "idempotency_key": "drink",
        }
        used = await _call(server, "campaign_change", arguments)
        replay = await _call(server, "campaign_change", arguments)

        assert replay == used
        assert used["status"] == "committed"
        assert used["formula"] == "2d4+2"
        assert used["roll"]["total"] == 10
        assert used["healing"]["before_hp"] == 1
        assert used["healing"]["after_hp"] == 11
        assert used["character"]["revision"] == actor["revision"] + 1
        assert used["party"]["inventory"]["items"][0]["quantity"] == 1
        assert used["campaign"]["revision"] == added["campaign"]["revision"] + 1
        assert used["rule_receipts"][0]["mechanic_id"] == (
            "dnd5e.core.item.healing_potion"
        )

    asyncio.run(exercise())
