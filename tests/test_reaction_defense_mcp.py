import asyncio
import random
from pathlib import Path

from sagasmith_dnd.character_schema import default_character_sheet
from sagasmith_dnd.combat_engine import roll_attack_action as engine_roll_attack_action
from sagasmith_dnd.spells import CORE_SHIELD_MECHANIC_ID, CORE_SHIELD_SPELL_ID

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


def test_public_attack_pauses_for_parry_before_damage(tmp_path: Path, monkeypatch) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    def deterministic_attack(*, plan):
        return engine_roll_attack_action(plan=plan, rng=random.Random(0))

    monkeypatch.setattr(server_module, "roll_attack_action", deterministic_attack)

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
            {"name": "Parry", "edition": "2014", "idempotency_key": "parry-campaign"},
        )
        attacker_sheet = default_character_sheet()
        attacker_sheet["abilities"]["strength"]["score"] = 16
        attacker_sheet["inventory"]["items"] = [
            {
                "id": "longsword",
                "name": "Longsword",
                "kind": "weapon",
                "equipped": True,
                "equipped_slot": "main_hand",
                "mechanics": {
                    "attack_type": "melee",
                    "attack_ability": "strength",
                    "damage_formula": "1d8",
                    "damage_type": "slashing",
                    "properties": ["versatile"],
                },
            }
        ]
        attacker_sheet["inventory"]["equipment_slots"]["main_hand"] = "longsword"
        attacker = await call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Attacker",
                "sheet": attacker_sheet,
                "idempotency_key": "parry-attacker",
            },
        )
        target_sheet = default_character_sheet()
        target_sheet["combat"]["hp"] = {"value": 20, "max": 20, "temp": 0}
        target_sheet["combat"]["ac"]["override"] = 18
        target_sheet["inventory"]["items"] = [
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
            }
        ]
        target_sheet["inventory"]["equipment_slots"]["main_hand"] = "scimitar"
        target_sheet["content"]["activities"] = [
            {
                "id": "bandit-captain-parry",
                "name": "Parry",
                "source_key": "Bandit Captain",
                "activation": {"type": "reaction"},
                "choices": {
                    "reaction_defense": {
                        "kind": "armor_class_bonus",
                        "bonus": 2,
                        "attack_modes": ["melee"],
                        "requires_visible_attacker": True,
                        "requires_wielded_melee_weapon": True,
                    }
                },
            }
        ]
        target = await call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Target",
                "sheet": target_sheet,
                "idempotency_key": "parry-target",
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
                "idempotency_key": "parry-play",
            },
        )
        started = await call(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [attacker["id"], target["id"]],
                "participant_config": [
                    {
                        "actor_id": attacker["id"],
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
                "idempotency_key": "parry-start",
            },
        )
        rolled = await call_raw(
            server,
            "combat_resolve_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": attacker["id"],
                "target_id": target["id"],
                "action": {"weapon_id": "longsword"},
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "parry-attack",
            },
        )
        assert rolled["status"] == "pending_reaction"
        assert rolled["result"]["hit"] is True
        assert rolled["result"]["damage"] is None
        reactions = await call(
            server,
            "combat_query",
            {
                "campaign_id": campaign["id"],
                "view": "reactions",
                "actor_id": target["id"],
            },
        )
        choice = reactions[0]
        assert choice["trigger"] == "attack_hit_defense"
        resolved = await call(
            server,
            "combat_choice",
            {
                "campaign_id": campaign["id"],
                "actor_id": target["id"],
                "action": "resolve_defense",
                "payload": {
                    "choice_id": choice["id"],
                    "selection": {"id": "bandit-captain-parry"},
                },
                "expected_revision": rolled["campaign_revision"],
                "idempotency_key": "parry-resolve",
            },
        )
        assert resolved["result"]["hit"] is False
        assert resolved["result"]["damage"] is None
        assert resolved["result"]["reaction_defense"]["used"] is True
        target_state = next(
            item
            for item in resolved["combat"]["combatants"]
            if item["actor_id"] == target["id"]
        )
        assert target_state["turn_budget"]["reaction"] == 0
        reread = await call(
            server, "character_get", {"character_id": target["id"]}
        )
        assert reread["sheet"]["combat"]["hp"]["value"] == 20

    asyncio.run(exercise())


def test_shield_reaction_atomically_pays_and_expires_at_next_turn_start(
    tmp_path: Path, monkeypatch
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

    def deterministic_attack(*, plan):
        return engine_roll_attack_action(plan=plan, rng=random.Random(0))

    monkeypatch.setattr(server_module, "roll_attack_action", deterministic_attack)

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
            {"name": "Shield", "edition": "2014", "idempotency_key": "shield-campaign"},
        )
        attacker_sheet = default_character_sheet()
        attacker_sheet["abilities"]["strength"]["score"] = 16
        attacker_sheet["inventory"]["items"] = [
            {
                "id": "longsword",
                "name": "Longsword",
                "kind": "weapon",
                "equipped": True,
                "equipped_slot": "main_hand",
                "mechanics": {
                    "attack_type": "melee",
                    "attack_ability": "strength",
                    "damage_formula": "1d8",
                    "damage_type": "slashing",
                    "properties": ["versatile"],
                },
            }
        ]
        attacker_sheet["inventory"]["equipment_slots"]["main_hand"] = "longsword"
        attacker = await call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Attacker",
                "sheet": attacker_sheet,
                "idempotency_key": "shield-attacker",
            },
        )
        target_sheet = default_character_sheet()
        target_sheet["combat"]["hp"] = {"value": 20, "max": 20, "temp": 0}
        target_sheet["combat"]["ac"]["override"] = 18
        target_sheet["spellcasting"]["spell_slots"] = {
            "1": {
                "label": "1st",
                "value": 1,
                "max": 1,
                "recovers_on": "long_rest",
                "source_key": "wizard",
            }
        }
        target_sheet["content"]["spells"] = [
            {
                "id": CORE_SHIELD_SPELL_ID,
                "name": "Shield",
                "level": 1,
                "grant": {"source_type": "class", "source_key": "wizard", "method": "known"},
                "access": {"known": True, "prepared": True},
                "definition": {
                    "casting_time": "1 reaction, which you take when hit by an attack",
                    "duration": {
                        "kind": "timed",
                        "value": 1,
                        "unit": "round",
                        "concentration": False,
                    },
                    "components": {"verbal": True, "somatic": True},
                },
                "mechanic_refs": [CORE_SHIELD_MECHANIC_ID],
            }
        ]
        target = await call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Wizard",
                "sheet": target_sheet,
                "idempotency_key": "shield-target",
            },
        )
        campaign = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
        phase = await call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": campaign["revision"],
                "idempotency_key": "shield-play",
            },
        )
        started = await call(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [attacker["id"], target["id"]],
                "participant_config": [
                    {
                        "actor_id": attacker["id"],
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
                "idempotency_key": "shield-start",
            },
        )
        rolled = await call_raw(
            server,
            "combat_resolve_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": attacker["id"],
                "target_id": target["id"],
                "action": {"weapon_id": "longsword"},
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "shield-attack",
            },
        )
        assert rolled["status"] == "pending_reaction"
        assert rolled["result"]["hit"] is True
        assert rolled["result"]["damage"] is None
        choice = (
            await call(
                server,
                "combat_query",
                {
                    "campaign_id": campaign["id"],
                    "view": "reactions",
                    "actor_id": target["id"],
                },
            )
        )[0]
        shield = next(item for item in choice["candidates"] if item["id"] == CORE_SHIELD_SPELL_ID)
        assert shield["cast_levels"] == [1]

        resolved = await call(
            server,
            "combat_choice",
            {
                "campaign_id": campaign["id"],
                "actor_id": target["id"],
                "action": "resolve_defense",
                "payload": {
                    "choice_id": choice["id"],
                    "selection": {"id": CORE_SHIELD_SPELL_ID, "cast_level": 1},
                },
                "expected_revision": rolled["campaign_revision"],
                "idempotency_key": "shield-resolve",
            },
        )
        assert resolved["result"]["hit"] is False
        assert resolved["result"]["damage"] is None
        assert resolved["result"]["reaction_defense"]["source_type"] == "spell"
        assert resolved["result"]["reaction_defense"]["cast_level"] == 1
        assert resolved["result"]["reaction_defense"]["payment"]["economy"] == "slots"
        target_state = next(
            item for item in resolved["combat"]["combatants"] if item["actor_id"] == target["id"]
        )
        assert target_state["turn_budget"]["reaction"] == 0
        shielded = await call(server, "character_get", {"character_id": target["id"]})
        assert shielded["sheet"]["spellcasting"]["spell_slots"]["1"]["value"] == 0
        assert shielded["derived"]["armor_class"] == 23

        ended = await call(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": attacker["id"],
                "expected_revision": resolved["campaign_revision"],
                "idempotency_key": "shield-end-attacker",
            },
        )
        assert resolved["result"]["reaction_defense"]["effect_id"] in ended["effects_expired"]
        expired = await call(server, "character_get", {"character_id": target["id"]})
        assert expired["derived"]["armor_class"] == 18
        effect = next(
            item
            for item in expired["sheet"]["effects"]
            if item["id"] == resolved["result"]["reaction_defense"]["effect_id"]
        )
        assert effect["active"] is False

    asyncio.run(exercise())
