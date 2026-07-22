from __future__ import annotations

import asyncio
from pathlib import Path

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


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


def test_hidden_attack_reveals_attacker_to_its_target(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))

        async def call(name: str, arguments: dict):
            _, result = await server.call_tool(name, arguments)
            return result.get("result", result) if isinstance(result, dict) else result

        async def raw(name: str, arguments: dict):
            _, result = await server.call_tool(name, arguments)
            return result

        campaign = await call(
            "campaign_create",
            {"name": "Reveal attack", "idempotency_key": "campaign"},
        )
        attacker = await call(
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Hidden attacker",
                "idempotency_key": "attacker",
            },
        )
        target = await call(
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Target",
                "idempotency_key": "target",
            },
        )
        for actor, key in ((attacker, "attacker-sheet"), (target, "target-sheet")):
            sheet = actor["sheet"]
            sheet["combat"]["hp"] = {"value": 10, "max": 10, "temp": 0}
            updated = await call(
                "character_sheet_replace",
                {
                    "character_id": actor["id"],
                    "sheet": sheet,
                    "expected_revision": actor["revision"],
                    "idempotency_key": key,
                },
            )
            if actor["id"] == attacker["id"]:
                attacker = updated
            else:
                target = updated
        campaign = await call(
            "campaign_get", {"campaign_id": campaign["id"]}
        )
        started = await raw(
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [attacker["id"], target["id"]],
                "participant_config": [
                    {
                        "actor_id": attacker["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                        "hidden": True,
                        "visible_to_actor_ids": [attacker["id"]],
                    },
                    {
                        "actor_id": target["id"],
                        "initiative": 10,
                        "position": {"x": 1, "y": 0},
                    },
                ],
                "expected_revision": campaign["revision"],
                "idempotency_key": "start",
            },
        )
        attacked = await raw(
            "combat_resolve_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": attacker["id"],
                "target_id": target["id"],
                "action": {"weapon_id": "unarmed-strike", "attack_mode": "melee"},
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "attack",
            },
        )
        assert attacked["result"]["reveals_attacker"] is True
        attacker_state = next(
            item
            for item in attacked["combat"]["combatants"]
            if item["actor_id"] == attacker["id"]
        )
        assert attacker_state["hidden"] is False
        assert attacker_state["visible_to_actor_ids"] is None

        ended = await raw(
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": attacker["id"],
                "expected_revision": attacked["campaign_revision"],
                "idempotency_key": "end-attacker",
            },
        )
        counterattack = await call(
            "combat_preflight_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": target["id"],
                "target_id": attacker["id"],
                "action": {"weapon_id": "unarmed-strike", "attack_mode": "melee"},
            },
        )
        assert ended["combat"]["combatants"][ended["combat"]["turn_index"]][
            "actor_id"
        ] == target["id"]
        assert counterattack["target_can_see_attacker"] is True
        assert counterattack["disadvantage"] is False

    asyncio.run(exercise())
