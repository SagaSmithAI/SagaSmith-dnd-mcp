import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from tests.authoring_helpers import finalize_and_activate_module


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def test_scene_preflight_blocks_only_missing_or_invalid_combatants(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        module_import_roots=(tmp_path,),
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        long_evidence = (
            "The encounter record preserves the complete authored sequence, "
            "including starting hit points, exact participant counts, target "
            "priorities, delayed arrivals, allied actions, and the stated ending "
            "condition. "
        ) * 4
        source = tmp_path / "ambush.md"
        source.write_text(
            "# Chapter\n## Ambush\n"
            "Captain Rusk\u2019s two band\u00adits attack the party. "
            "A tavern guard can be persuaded to join on the next round. "
            f"{long_evidence}",
            encoding="utf-8",
        )
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Participant preflight", "idempotency_key": "campaign"},
        )
        staged = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "ambush",
                    "title": "Ambush",
                },
                "idempotency_key": "module:stage",
            },
        )
        await finalize_and_activate_module(
            _call,
            server,
            campaign["id"],
            staged,
            source_key="ambush",
            title="Ambush",
            portable_id="dnd5e.module.ambush-test",
        )
        scene = next(
            item
            for item in await _call(
                server,
                "module_query",
                {
                    "campaign_id": campaign["id"],
                    "view": "index",
                    "payload": {},
                    "principal_id": "system:local",
                },
            )
            if item["title"] == "Ambush"
        )

        with pytest.raises(
            Exception,
            match=r"allowed fields: \['groups', 'notes', 'schema_version'\]",
        ):
            await _call(
                server,
                "module_query",
                {
                    "campaign_id": campaign["id"],
                    "view": "preflight",
                    "payload": {
                        "scene_id": scene["scene_id"],
                        "participant_manifest": {"actor_ids": []},
                    },
                },
            )

        with pytest.raises(Exception, match=r"allowed fields: .*'key'.*'role'"):
            await _call(
                server,
                "module_query",
                {
                    "campaign_id": campaign["id"],
                    "view": "preflight",
                    "payload": {
                        "scene_id": scene["scene_id"],
                        "participant_manifest": {
                            "groups": [{"name": "unsupported"}]
                        },
                    },
                },
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
                "character_create_from",
                {
                    "mode": "direct",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "name": key,
                        "character_type": character_type,
                        "notes": {
                            "profile": {
                                "dm_notes": (
                                    "Statblock import: test. Manual rulings: "
                                    "Parry requires a reaction decision; "
                                    "Multiattack: Multiattack composition requires a DM ruling; "
                                    "Multiattack: descriptive action is not automatically "
                                    "settled.\n"
                                    "Variant source: module-chunk:test; "
                                    "applied fields: current_hit_points.\n"
                                    "Normalization notes: Club: trailing creature prose "
                                    "excluded from action settlement."
                                )
                            }
                        }
                        if key == "captain"
                        else {
                            "profile": {
                                "dm_notes": (
                                    "Reviewed rule statblock: old review.\n"
                                    "Manual rulings: Claw: on-hit effect requires DM "
                                    "settlement.\n"
                                    "Reviewed rule statblock: current review.\n"
                                    "Agent statblock fill: multiattack-activity."
                                )
                            }
                        }
                        if key == "guard"
                        else None,
                    },
                    "principal_id": "system:local",
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
                        "source_excerpt": "Captain Rusk's two bandits attack the party.",
                    },
                    {
                        "key": "rusk-bandits",
                        "label": "Rusk's bandits",
                        "role": "combatant",
                        "required_count": 2,
                        "actor_ids": bandit_ids,
                        "source_excerpt": "Captain Rusk's two bandits attack the party.",
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
                "view": "preflight",
                "payload": {
                    "scene_id": scene["scene_id"],
                    "participant_manifest": manifest([actors["bandit1"]["id"]]),
                },
            },
        )
        assert incomplete["ready"] is False
        assert (
            next(item for item in incomplete["groups"] if item["key"] == "rusk-bandits")[
                "missing_count"
            ]
            == 1
        )

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
                "view": "preflight",
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
        assert unusable["ready"] is True
        assert unusable_bandits["missing_count"] == 0
        assert unusable_bandits["invalid_actor_ids"] == []
        dead_card = unusable_bandits["actors"][1]["combat_card"]
        assert dead_card["card_valid"] is True
        assert dead_card["state_flags"] == ["dead", "zero_hit_points"]
        assert dead_card["can_take_turn"] is False
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
                "ruling_requirements": [
                    {
                        "kind": "effect_semantics",
                        "reason": "Resolve the module spell from its reviewed text.",
                        "source_excerpt": "The module spell affects one creature.",
                        "default_resolver": "agent",
                        "ruling_kind": "generic_spell_effect",
                        "policy_ref": "resolution_plan.v1",
                        "requires_external_input_only_for": [],
                    }
                ],
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
                "view": "preflight",
                "payload": {
                    "scene_id": scene["scene_id"],
                    "participant_manifest": complete_manifest,
                },
            },
        )
        assert ready["ready"] is True
        captain_group = next(item for item in ready["groups"] if item["key"] == "captain-rusk")
        assert captain_group["actors"][0]["combat_card"]["settlement"] == "mixed"
        captain_card = captain_group["actors"][0]["combat_card"]
        assert captain_card["manual_rulings"] == [
            "Parry requires a reaction decision",
            "Multiattack: Multiattack composition requires a DM ruling",
        ]
        assert captain_card["normalization_notes"] == [
            "Club: trailing creature prose excluded from action settlement"
        ]
        assert captain_card["default_dm_resolver"] == "agent"
        assert captain_card["agent_rulings"] == captain_card["manual_rulings"]
        assert captain_card["external_source_gaps"] == []
        assert {
            (item["reason"], item["default_resolver"], item["ruling_kind"])
            for item in captain_card["ruling_requirements"]
        } == {
            (
                "Parry requires a reaction decision",
                "agent",
                "agent_dm_adjudication",
            ),
            (
                "Multiattack: Multiattack composition requires a DM ruling",
                "agent",
                "agent_dm_adjudication",
            ),
        }
        bandit_group = next(item for item in ready["groups"] if item["key"] == "rusk-bandits")
        mixed_card = bandit_group["actors"][0]["combat_card"]
        assert mixed_card["settlement"] == "mixed"
        assert mixed_card["ruling_spell_ids"] == ["module-spell"]
        assert mixed_card["cantrip_spell_ids"] == ["module-spell"]
        assert (
            mixed_card["spell_resolution_audit"]["entries"][0]["resolution_path"] == "agent_ruling"
        )
        assert mixed_card["automatic_spell_ids"] == []
        assert mixed_card["unarmed_fallback"] is True
        assert mixed_card["unarmed_attack_id"] == "unarmed-strike"
        assert mixed_card["manual_rulings"] == [
            "Mystery Bow: ranged weapon range is missing",
            "Available spells require Agent effect settlement: module-spell",
        ]
        assert mixed_card["hard_blockers"] == []
        assert {
            (item["id"], item["kind"], item["reason"])
            for item in mixed_card["disabled_capabilities"]
        } == {
            (
                "mystery-bow",
                "attack",
                "Mystery Bow: ranged weapon range is missing",
            )
        }
        assert [
            (item["default_resolver"], item["ruling_kind"])
            for item in mixed_card["ruling_requirements"]
        ] == [
            ("external_input", "missing_or_conflicting_source_review"),
            ("agent", "agent_dm_adjudication"),
        ]
        assert mixed_card["agent_rulings"] == [
            "Available spells require Agent effect settlement: module-spell"
        ]
        assert mixed_card["external_source_gaps"] == [
            "Mystery Bow: ranged weapon range is missing"
        ]
        assert ready["initial_actor_ids"] == [
            actors["captain"]["id"],
            actors["bandit1"]["id"],
            actors["bandit2"]["id"],
        ]
        assert ready["reinforcement_actor_ids"] == [actors["guard"]["id"]]
        guard_group = next(item for item in ready["groups"] if item["key"] == "tavern-guard")
        assert guard_group["actors"][0]["combat_card"]["manual_rulings"] == []
        assert ready["checksum"]

        repaired_sheet = deepcopy(actors["bandit1"]["sheet"])
        repaired_sheet["inventory"]["items"][0]["mechanics"].update(
            {"normal_range_ft": 80, "long_range_ft": 320}
        )
        actors["bandit1"] = await _call(
            server,
            "character_sheet_replace",
            {
                "character_id": actors["bandit1"]["id"],
                "sheet": repaired_sheet,
                "expected_revision": actors["bandit1"]["revision"],
                "idempotency_key": "repair-mystery-bow-range",
            },
        )
        ready = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "preflight",
                "payload": {
                    "scene_id": scene["scene_id"],
                    "participant_manifest": complete_manifest,
                },
            },
        )
        assert ready["ready"] is True
        repaired_bandit_group = next(
            item for item in ready["groups"] if item["key"] == "rusk-bandits"
        )
        assert repaired_bandit_group["actors"][0]["combat_card"]["manual_rulings"] == [
            "Available spells require Agent effect settlement: module-spell"
        ]
        assert (
            repaired_bandit_group["actors"][0]["combat_card"]["ruling_requirements"][0][
                "default_resolver"
            ]
            == "agent"
        )

        positional_sheet = deepcopy(original_bandit_sheet)
        positional_sheet["inventory"]["items"] = [
            {
                "id": "dagger",
                "name": "Dagger",
                "kind": "weapon",
                "equipped": True,
                "equipped_slot": "main_hand",
                "description": (
                    "Melee Weapon Attack: +5 to hit, reach 5 ft., one target. "
                    "Hit: 5 (1d4 + 3) piercing damage."
                ),
                "mechanics": {
                    "attack_type": "melee",
                    "attack_ability": "strength",
                    "damage_formula": "1d4",
                    "damage_type": "piercing",
                    "reach_ft": 5,
                },
            },
            {
                "id": "dropped-rock",
                "name": "Dropped Rock",
                "kind": "weapon",
                "description": (
                    "Ranged Weapon Attack: +5 to hit, one target directly below "
                    "the attacker. Hit: 6 (1d6 + 3) bludgeoning damage."
                ),
                "mechanics": {
                    "attack_type": "ranged",
                    "attack_ability": "dexterity",
                    "damage_formula": "1d6",
                    "damage_type": "bludgeoning",
                    "always_available": True,
                },
            },
        ]
        positional_sheet["inventory"]["equipment_slots"]["main_hand"] = "dagger"
        actors["bandit1"] = await _call(
            server,
            "character_sheet_replace",
            {
                "character_id": actors["bandit1"]["id"],
                "sheet": positional_sheet,
                "expected_revision": actors["bandit1"]["revision"],
                "idempotency_key": "source-positional-targeting",
            },
        )
        ready = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "preflight",
                "payload": {
                    "scene_id": scene["scene_id"],
                    "participant_manifest": complete_manifest,
                },
            },
        )
        positional_card = next(item for item in ready["groups"] if item["key"] == "rusk-bandits")[
            "actors"
        ][0]["combat_card"]
        assert ready["ready"] is True
        assert positional_card["hard_blockers"] == []
        assert positional_card["manual_rulings"] == [
            "Dropped Rock: source-defined positional targeting requires a DM ruling"
        ]
        assert positional_card["agent_rulings"] == positional_card["manual_rulings"]
        assert positional_card["external_source_gaps"] == []

        long_manifest = deepcopy(complete_manifest)
        long_manifest["groups"][0]["source_excerpt"] = long_evidence.strip()
        long_ready = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "preflight",
                "payload": {
                    "scene_id": scene["scene_id"],
                    "participant_manifest": long_manifest,
                },
            },
        )
        assert len(long_manifest["groups"][0]["source_excerpt"]) > 500
        assert long_ready["ready"] is True

        original_bandit_notes = deepcopy(actors["bandit1"]["notes"])
        incomplete_notes = deepcopy(original_bandit_notes)
        incomplete_notes["profile"]["dm_notes"] = (
            "Statblock import: test. Manual rulings: "
            "invisihility: no active spell artifact or complete statblock action exists."
        )
        actors["bandit1"] = await _call(
            server,
            "character_sheet_replace",
            {
                "character_id": actors["bandit1"]["id"],
                "sheet": actors["bandit1"]["sheet"],
                "notes": incomplete_notes,
                "expected_revision": actors["bandit1"]["revision"],
                "idempotency_key": "incomplete-spell-hydration",
            },
        )
        incomplete = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "preflight",
                "payload": {
                    "scene_id": scene["scene_id"],
                    "participant_manifest": complete_manifest,
                },
            },
        )
        incomplete_bandits = next(
            item for item in incomplete["groups"] if item["key"] == "rusk-bandits"
        )
        assert incomplete["ready"] is True
        assert incomplete_bandits["invalid_actor_ids"] == []
        assert incomplete_bandits["actors"][0]["combat_card"]["hard_blockers"] == []
        assert {
            (item["id"], item["kind"], item["reason"])
            for item in incomplete_bandits["actors"][0]["combat_card"][
                "disabled_capabilities"
            ]
        } == {
            (
                "spellcasting",
                "spellcasting",
                "incomplete_statblock_spell_hydration",
            )
        }
        actors["bandit1"] = await _call(
            server,
            "character_sheet_replace",
            {
                "character_id": actors["bandit1"]["id"],
                "sheet": actors["bandit1"]["sheet"],
                "notes": original_bandit_notes,
                "expected_revision": actors["bandit1"]["revision"],
                "idempotency_key": "restore-complete-spell-hydration",
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
        with pytest.raises(Exception, match="omit manifest combatants"):
            await _call(
                server,
                "combat_start",
                {
                    "positioning_mode": "agent",
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
                "positioning_mode": "grid",
                "campaign_id": campaign["id"],
                "participant_ids": participant_ids,
                "participant_config": [
                    {
                        "actor_id": actor_id,
                        "initiative": (30 if actor_id == actors["bandit1"]["id"] else 20 - index),
                        "tie_breaker": index,
                        "position": {"x": index, "y": 0},
                    }
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
        positional_preflight = await _call(
            server,
            "combat_preflight_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": actors["bandit1"]["id"],
                "target_id": actors["hero"]["id"],
                "action": {
                    "weapon_id": "dropped-rock",
                    "attack_mode": "ranged",
                },
            },
        )
        assert positional_preflight["status"] == "pending_ruling"
        assert positional_preflight["default_resolver"] == "agent"
        assert positional_preflight["ruling_kind"] == "agent_dm_adjudication"
        assert positional_preflight["missing"] == ["weapon.targeting:dropped-rock"]

    asyncio.run(exercise())
