import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def test_scene_readiness_blocks_missing_combatants_and_reserves(tmp_path: Path) -> None:
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
            {"name": "Participant readiness", "idempotency_key": "campaign"},
        )
        artifact = await _call(
            server,
            "module_write",
            {
                "name": "ambush.md",
                "content": (
                    "# Chapter\n## Ambush\n"
                    "Captain Rusk and two bandits attack the party. "
                    "A tavern guard can be persuaded to join on the next round."
                ),
            },
        )
        await _call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "artifact": artifact["artifact"],
                "idempotency_key": "module",
            },
        )
        scene = next(
            item
            for item in await _call(server, "module_index", {"campaign_id": campaign["id"]})
            if item["title"] == "Ambush"
        )

        actors = {}
        for key, character_type in (
            ("hero", "pc"),
            ("captain", "npc"),
            ("bandit1", "monster"),
            ("bandit2", "monster"),
            ("guard", "npc"),
        ):
            actors[key] = await _call(
                server,
                "character_create",
                {
                    "campaign_id": campaign["id"],
                    "name": key,
                    "character_type": character_type,
                    "notes": (
                        {
                            "profile": {
                                "dm_notes": (
                                    "Statblock import: test. Manual rulings: "
                                    "Parry requires a reaction decision; "
                                    "Multiattack: Multiattack composition requires a DM ruling; "
                                    "Multiattack: descriptive action is not automatically settled. "
                                    "Variant source: module-chunk:test; "
                                    "applied fields: current_hit_points."
                                )
                            }
                        }
                        if key == "captain"
                        else None
                    ),
                    "idempotency_key": f"actor-{key}",
                },
            )

        def manifest(bandit_ids: list[str]) -> dict:
            return {
                "schema_version": 1,
                "groups": [
                    {
                        "key": "captain-rusk",
                        "label": "Captain Rusk",
                        "role": "combatant",
                        "required_count": 1,
                        "actor_ids": [actors["captain"]["id"]],
                        "source_excerpt": "Captain Rusk and two bandits attack the party.",
                    },
                    {
                        "key": "rusk-bandits",
                        "label": "Rusk's bandits",
                        "role": "combatant",
                        "required_count": 2,
                        "actor_ids": bandit_ids,
                        "source_excerpt": "Captain Rusk and two bandits attack the party.",
                    },
                    {
                        "key": "tavern-guard",
                        "label": "Persuadable tavern guard",
                        "role": "reinforcement",
                        "required_count": 1,
                        "actor_ids": [actors["guard"]["id"]],
                        "source_excerpt": (
                            "A tavern guard can be persuaded to join on the next round."
                        ),
                    },
                ],
            }

        incomplete = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "readiness",
                "payload": {
                    "scene_id": scene["scene_id"],
                    "participant_manifest": manifest([actors["bandit1"]["id"]]),
                },
            },
        )
        assert incomplete["ready"] is False
        assert next(item for item in incomplete["groups"] if item["key"] == "rusk-bandits")[
            "missing_count"
        ] == 1

        original_bandit_sheet = deepcopy(actors["bandit2"]["sheet"])
        dead_bandit_sheet = deepcopy(original_bandit_sheet)
        dead_bandit_sheet["combat"]["hp"]["value"] = 0
        dead_bandit_sheet["conditions"] = ["dead"]
        dead_bandit = await _call(
            server,
            "character_sheet_replace",
            {
                "character_id": actors["bandit2"]["id"],
                "sheet": dead_bandit_sheet,
                "expected_revision": actors["bandit2"]["revision"],
                "idempotency_key": "dead-bandit-card",
            },
        )
        unusable = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "readiness",
                "payload": {
                    "scene_id": scene["scene_id"],
                    "participant_manifest": manifest(
                        [actors["bandit1"]["id"], actors["bandit2"]["id"]]
                    ),
                },
            },
        )
        unusable_bandits = next(
            item for item in unusable["groups"] if item["key"] == "rusk-bandits"
        )
        assert unusable["ready"] is False
        assert unusable_bandits["missing_count"] == 0
        assert unusable_bandits["unready_actor_ids"] == [actors["bandit2"]["id"]]
        assert unusable_bandits["actors"][1]["combat_card"]["blocking_reasons"] == [
            "dead",
            "zero_hit_points",
        ]
        actors["bandit2"] = await _call(
            server,
            "character_sheet_replace",
            {
                "character_id": actors["bandit2"]["id"],
                "sheet": original_bandit_sheet,
                "expected_revision": dead_bandit["revision"],
                "idempotency_key": "restore-bandit-card",
            },
        )

        mixed_sheet = deepcopy(actors["bandit1"]["sheet"])
        mixed_sheet["inventory"]["items"] = [
            {
                "id": "mystery-bow",
                "name": "Mystery Bow",
                "kind": "weapon",
                "equipped": True,
                "equipped_slot": "main_hand",
                "mechanics": {
                    "attack_type": "ranged",
                    "attack_ability": "dexterity",
                    "damage_formula": "1d6",
                    "damage_type": "piercing",
                },
            }
        ]
        mixed_sheet["inventory"]["equipment_slots"]["main_hand"] = "mystery-bow"
        mixed_sheet["spellcasting"]["ability"] = "intelligence"
        mixed_sheet["content"]["spells"] = [
            {
                "id": "module-spell",
                "name": "Module Spell",
                "level": 0,
                    "access": {
                        "known": True,
                        "prepared": True,
                        "always_prepared": True,
                        "ritual_available": False,
                    },
                "definition": {
                    "casting_time": "1 action",
                    "duration": {
                        "kind": "instantaneous",
                        "value": 0,
                        "unit": "round",
                        "concentration": False,
                    },
                },
            }
        ]
        actors["bandit1"] = await _call(
            server,
            "character_sheet_replace",
            {
                "character_id": actors["bandit1"]["id"],
                "sheet": mixed_sheet,
                "expected_revision": actors["bandit1"]["revision"],
                "idempotency_key": "mixed-bandit-card",
            },
        )

        complete_manifest = manifest([actors["bandit1"]["id"], actors["bandit2"]["id"]])
        ready = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "readiness",
                "payload": {
                    "scene_id": scene["scene_id"],
                    "participant_manifest": complete_manifest,
                },
            },
        )
        assert ready["ready"] is True
        captain_group = next(item for item in ready["groups"] if item["key"] == "captain-rusk")
        assert captain_group["actors"][0]["combat_card"]["settlement"] == "mixed"
        assert captain_group["actors"][0]["combat_card"]["manual_rulings"] == [
            "Parry requires a reaction decision",
            "Multiattack: Multiattack composition requires a DM ruling",
        ]
        bandit_group = next(item for item in ready["groups"] if item["key"] == "rusk-bandits")
        mixed_card = bandit_group["actors"][0]["combat_card"]
        assert mixed_card["settlement"] == "mixed"
        assert mixed_card["ruling_spell_ids"] == ["module-spell"]
        assert mixed_card["automatic_spell_ids"] == []
        assert mixed_card["unarmed_fallback"] is True
        assert mixed_card["unarmed_attack_id"] == "unarmed-strike"
        assert mixed_card["manual_rulings"] == [
            "Mystery Bow: ranged weapon range is missing",
            "Prepared spells require DM effect settlement: module-spell",
        ]
        assert ready["initial_actor_ids"] == [
            actors["captain"]["id"],
            actors["bandit1"]["id"],
            actors["bandit2"]["id"],
        ]
        assert ready["reinforcement_actor_ids"] == [actors["guard"]["id"]]
        assert ready["checksum"]

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
        with pytest.raises(Exception, match="omit manifest combatants"):
            await _call(
                server,
                "combat_start",
                {
                    "campaign_id": campaign["id"],
                    "participant_ids": [
                        actors["hero"]["id"],
                        actors["captain"]["id"],
                        actors["bandit1"]["id"],
                    ],
                    "participant_manifest": complete_manifest,
                    "scene_id": scene["scene_id"],
                    "expected_revision": phase["campaign_revision"],
                    "idempotency_key": "start-missing",
                },
            )

        participant_ids = [
            actors["hero"]["id"],
            actors["captain"]["id"],
            actors["bandit1"]["id"],
            actors["bandit2"]["id"],
        ]
        started = await _call(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": participant_ids,
                "participant_config": [
                    {"actor_id": actor_id, "initiative": 20 - index, "tie_breaker": index}
                    for index, actor_id in enumerate(participant_ids)
                ],
                "participant_manifest": complete_manifest,
                "scene_id": scene["scene_id"],
                "expected_revision": phase["campaign_revision"],
                "idempotency_key": "start-complete",
            },
        )
        assert started["combat"]["participant_manifest"]["checksum"] == ready["checksum"]
        assert actors["guard"]["id"] not in {
            item["actor_id"] for item in started["combat"]["combatants"]
        }

    asyncio.run(exercise())
