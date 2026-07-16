import asyncio
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


def test_public_combat_attack_enforces_monster_multiattack_sequence(tmp_path: Path) -> None:
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

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {
                "name": "Multiattack",
                "edition": "2014",
                "idempotency_key": "multiattack-campaign",
            },
        )
        captain_sheet = default_character_sheet()
        captain_sheet["combat"]["hp"] = {"value": 65, "max": 65, "temp": 0}
        captain_sheet["combat"]["ac"]["override"] = 15
        captain_sheet["inventory"]["items"] = [
            {
                "id": "scimitar",
                "name": "Scimitar",
                "kind": "weapon",
                "equipped": True,
                "equipped_slot": "main_hand",
                "mechanics": {
                    "attack_type": "melee",
                    "attack_ability": "dexterity",
                    "damage_formula": "1d6",
                    "damage_type": "slashing",
                    "properties": ["finesse", "light"],
                },
            },
            {
                "id": "dagger",
                "name": "Dagger",
                "kind": "weapon",
                "equipped": True,
                "equipped_slot": "off_hand",
                "mechanics": {
                    "attack_type": "melee",
                    "attack_ability": "dexterity",
                    "damage_formula": "1d4",
                    "damage_type": "piercing",
                    "properties": ["finesse", "light", "thrown"],
                    "thrown_normal_range_ft": 20,
                    "thrown_long_range_ft": 60,
                },
            },
        ]
        captain_sheet["inventory"]["equipment_slots"].update(
            {"main_hand": "scimitar", "off_hand": "dagger"}
        )
        captain_sheet["content"]["activities"] = [
            {
                "id": "bandit-captain-multiattack",
                "name": "Multiattack",
                "source_key": "Bandit Captain",
                "activation": {"type": "action"},
                "choices": {
                    "multiattack_options": [
                        {
                            "id": "melee",
                            "attacks": [
                                {
                                    "weapon_id": "scimitar",
                                    "attack_mode": "melee",
                                    "count": 2,
                                },
                                {
                                    "weapon_id": "dagger",
                                    "attack_mode": "melee",
                                    "count": 1,
                                },
                            ],
                        }
                    ]
                },
            }
        ]
        captain = await call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Captain",
                "character_type": "monster",
                "sheet": captain_sheet,
                "idempotency_key": "multiattack-captain",
            },
        )
        target_sheet = default_character_sheet()
        target_sheet["combat"]["hp"] = {"value": 100, "max": 100, "temp": 0}
        target_sheet["combat"]["ac"]["override"] = 1
        target = await call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Target",
                "sheet": target_sheet,
                "idempotency_key": "multiattack-target",
            },
        )
        campaign = await call(
            server, "campaign_get", {"campaign_id": campaign["id"]}
        )
        phase = await call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": campaign["revision"],
                "idempotency_key": "multiattack-play",
            },
        )
        started = await call(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [captain["id"], target["id"]],
                "participant_config": [
                    {
                        "actor_id": captain["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                        "disposition": "hostile",
                    },
                    {
                        "actor_id": target["id"],
                        "initiative": 10,
                        "position": {"x": 1, "y": 0},
                        "disposition": "friendly",
                    },
                ],
                "expected_revision": phase["campaign_revision"],
                "idempotency_key": "multiattack-start",
            },
        )
        revision = started["campaign_revision"]

        async def attack(weapon_id: str, key: str, *, first: bool = False):
            _, result = await server.call_tool(
                "combat_resolve_attack",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": captain["id"],
                    "target_id": target["id"],
                    "action": {
                        "weapon_id": weapon_id,
                        **({"multiattack_option_id": "melee"} if first else {}),
                    },
                    "expected_revision": revision,
                    "idempotency_key": key,
                },
            )
            return result

        first = await attack("scimitar", "multiattack-first", first=True)
        assert first["result"]["attack_payment"]["attack_count"] == 3
        revision = first["campaign_revision"]
        second = await attack("scimitar", "multiattack-second")
        revision = second["campaign_revision"]
        with pytest.raises(Exception, match="remaining Multiattack"):
            await attack("scimitar", "multiattack-illegal-third")
        third = await attack("dagger", "multiattack-third")
        current = next(
            item
            for item in third["combat"]["combatants"]
            if item["actor_id"] == captain["id"]
        )
        assert current["turn_budget"]["attack_budget"] == 0

    asyncio.run(exercise())
