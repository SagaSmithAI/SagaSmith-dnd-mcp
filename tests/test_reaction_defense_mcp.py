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
    import_root = tmp_path / "rules"
    import_root.mkdir()
    effect = (
        "The defender adds 2 to its AC against one melee attack that would hit it, "
        "provided the defender can see the attacker and is wielding a melee weapon."
    )
    source = import_root / "parry-lore.md"
    source.write_text(f"# Parry Lore\n\n## Parry\n\n{effect}\n", encoding="utf-8")
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        rule_import_roots=(import_root,),
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
        return result["result"] if isinstance(result, dict) and "action" in result else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Parry", "edition": "2014", "idempotency_key": "parry-campaign"},
        )
        staged = await call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "parry-lore",
                    "title": "Parry Lore",
                    "edition": "2014",
                },
                "idempotency_key": "parry-rule:stage",
            },
        )
        job_id = staged["job"]["id"]
        await call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "idempotency_key": "parry-rule:inspect",
            },
        )
        await call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "idempotency_key": "parry-rule:ingest",
            },
        )
        hits = await call(
            server,
            "rule_search",
            {
                "campaign_id": campaign["id"],
                "query": "defender adds 2 to its AC",
                "filters": {"edition": "2014"},
                "top_k": 1,
            },
        )
        chunk_id = hits[0]["id"]
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
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Attacker",
                    "sheet": attacker_sheet,
                },
                "principal_id": "system:local",
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
                "id": "source-bound-parry",
                "name": "Parry",
                "source_key": "rule-source:parry-lore",
                "description": effect,
                "activation": {"type": "reaction"},
                "choices": {
                    "manual_ruling": {
                        "kind": "descriptive_activity",
                        "default_resolver": "agent",
                        "source_excerpt": effect,
                    }
                },
            }
        ]
        target = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Target", "sheet": target_sheet},
                "principal_id": "system:local",
                "idempotency_key": "parry-target",
            },
        )
        compiled = await call(
            server,
            "content_solution",
            {
                "campaign_id": campaign["id"],
                "actor_id": target["id"],
                "action": "compile",
                "source_card_id": "source-bound-parry",
                "source_card_kind": "activity",
                "payload": {
                    "resolution_plan": {
                        "schema_version": 2,
                        "id": "addon.test.parry-defense",
                        "source_card_id": "source-bound-parry",
                        "source_card_kind": "activity",
                        "trigger": "attack.after_hit",
                        "trigger_filter": {"hit": True},
                        "slots": {},
                        "steps": [
                            {
                                "id": "defend",
                                "op": "attack.ac_bonus",
                                "args": {
                                    "bonus": 2,
                                    "attack_modes": ["melee"],
                                    "requires_visible_attacker": True,
                                    "requires_wielded_melee_weapon": True,
                                },
                            }
                        ],
                        "citations": [
                            {
                                "source": "rule-source:parry-lore",
                                "source_ref": {"chunk_id": chunk_id},
                                "source_excerpt": effect,
                            }
                        ],
                    },
                    "agent_ruling": {
                        "default_resolver": "agent",
                        "ruling_kind": "agent_dm_adjudication",
                        "decision": "Store the quoted reaction as a contextual AC bonus.",
                        "reason": "The cited text states the bonus and all triggering limits.",
                    },
                },
                "expected_revision": target["revision"],
                "idempotency_key": "compile-parry-defense",
            },
        )
        assert compiled["status"] == "compiled"
        campaign = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
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
                "positioning_mode": "grid",
                "battle_map": {"width_cells": 12, "height_cells": 12},
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
        pending_presentation = await call(
            server,
            "resolution_presentation",
            {
                "campaign_id": campaign["id"],
                "resolution_id": rolled["resolution_id"],
            },
        )
        assert pending_presentation["status"] == "pending"
        assert pending_presentation["event_sequence"] == 1
        assert "source-bound-parry" in pending_presentation["pending_choice"][
            "available_actions"
        ]
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
                    "selection": {"id": "source-bound-parry"},
                },
                "expected_revision": rolled["campaign_revision"],
                "idempotency_key": "parry-resolve",
            },
        )
        assert resolved["result"]["hit"] is False
        assert resolved["result"]["damage"] is None
        assert resolved["result"]["reaction_defense"]["used"] is True
        settled_presentation = await call(
            server,
            "resolution_presentation",
            {
                "campaign_id": campaign["id"],
                "resolution_id": resolved["resolution_id"],
            },
        )
        assert settled_presentation["thread_id"] == pending_presentation["thread_id"]
        assert settled_presentation["event_sequence"] == 2
        assert settled_presentation["status"] == "settled"
        semantic = resolved["result"]["reaction_defense"]["semantic_solution"]
        assert semantic["plan_id"] == "addon.test.parry-defense"
        assert semantic["plan_fingerprint"] == compiled["solution"]["plan_fingerprint"]
        assert semantic["compiled_by"]["default_resolver"] == "agent"
        target_state = next(
            item for item in resolved["combat"]["combatants"] if item["actor_id"] == target["id"]
        )
        assert target_state["turn_budget"]["reaction"] == 0
        reread = await call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": target["id"]},
                "principal_id": "system:local",
            },
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
        return result["result"] if isinstance(result, dict) and "action" in result else result

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
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Attacker",
                    "sheet": attacker_sheet,
                },
                "principal_id": "system:local",
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
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Wizard", "sheet": target_sheet},
                "principal_id": "system:local",
                "idempotency_key": "shield-target",
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
                "positioning_mode": "grid",
                "battle_map": {"width_cells": 12, "height_cells": 12},
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
        shielded = await call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": target["id"]},
                "principal_id": "system:local",
            },
        )
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
        expired = await call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": target["id"]},
                "principal_id": "system:local",
            },
        )
        assert expired["derived"]["armor_class"] == 18
        effect = next(
            item
            for item in expired["sheet"]["effects"]
            if item["id"] == resolved["result"]["reaction_defense"]["effect_id"]
        )
        assert effect["active"] is False

    asyncio.run(exercise())
