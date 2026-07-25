from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sagasmith_dnd import combat_engine as combat_engine_module
from sagasmith_dnd.character_schema import default_character_sheet
from sagasmith_dnd.engine import DiceResult

from sagasmith_dnd_mcp import server as server_module
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


async def _raw(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result


async def _call(server, name: str, arguments: dict):
    result = await _raw(server, name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def test_public_regeneration_recovers_then_fire_suppression_kills_at_turn_start(
    tmp_path: Path,
) -> None:
    regeneration = (
        "The troll regains 10 hit points at the start of its turn. If the troll "
        "takes acid or fire damage, this trait doesn't function at the start of "
        "the troll's next turn. The troll dies only if it starts its turn with "
        "0 hit points and doesn't regenerate."
    )

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Source regeneration",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        troll = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Troll",
                "character_type": "monster",
                "idempotency_key": "troll",
            },
        )
        hero = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Hero",
                "character_type": "pc",
                "idempotency_key": "hero",
            },
        )
        troll_sheet = default_character_sheet()
        troll_sheet["combat"]["hp"] = {"value": 44, "max": 84, "temp": 0}
        troll_sheet["content"]["features"] = [
            {
                "id": "regeneration-passive",
                "name": "Regeneration",
                "description": regeneration,
                "activation": {"type": "passive", "cost": 0},
            }
        ]
        await _call(
            server,
            "character_sheet_replace",
            {
                "character_id": troll["id"],
                "sheet": troll_sheet,
                "expected_revision": troll["revision"],
                "idempotency_key": "troll-sheet",
            },
        )
        current = await _call(
            server,
            "campaign_get",
            {"campaign_id": campaign["id"]},
        )
        started = await _raw(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [troll["id"], hero["id"]],
                "participant_config": [
                    {
                        "actor_id": troll["id"],
                        "initiative": 20,
                        "disposition": "hostile",
                        "death_saves": False,
                        "source_traits": [
                            {
                                "kind": "regeneration",
                                "feature_id": "regeneration-passive",
                                "source_excerpt": regeneration,
                            }
                        ],
                    },
                    {
                        "actor_id": hero["id"],
                        "initiative": 10,
                        "disposition": "friendly",
                        "death_saves": True,
                    },
                ],
                "expected_revision": current["revision"],
                "idempotency_key": "start",
            },
        )
        troll_after_start = await _call(
            server,
            "character_get",
            {"character_id": troll["id"]},
        )
        assert troll_after_start["sheet"]["combat"]["hp"]["value"] == 54
        assert started["source_turn_start"][0]["result"]["amount"] == 10

        damaged = await _raw(
            server,
            "combat_apply_damage",
            {
                "campaign_id": campaign["id"],
                "target_id": troll["id"],
                "parts": [{"amount": 54, "damage_type": "fire"}],
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "fire",
            },
        )
        troll_at_zero = await _call(
            server,
            "character_get",
            {"character_id": troll["id"]},
        )
        assert troll_at_zero["sheet"]["combat"]["hp"]["value"] == 0
        assert "unconscious" in troll_at_zero["sheet"]["conditions"]
        assert "dead" not in troll_at_zero["sheet"]["conditions"]

        ended_troll = await _raw(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": troll["id"],
                "expected_revision": damaged["campaign_revision"],
                "idempotency_key": "end-troll",
            },
        )
        ended_hero = await _raw(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": hero["id"],
                "expected_revision": ended_troll["campaign_revision"],
                "idempotency_key": "end-hero",
            },
        )
        troll_dead = await _call(
            server,
            "character_get",
            {"character_id": troll["id"]},
        )
        settlement = ended_hero["source_turn_start"][0]
        assert settlement["result"]["suppressed"] is True
        assert settlement["result"]["died"] is True
        assert "dead" in troll_dead["sheet"]["conditions"]
        assert "unconscious" not in troll_dead["sheet"]["conditions"]

    asyncio.run(exercise())


def test_public_source_stabilization_requires_scene_evidence_and_no_helper_actor(
    tmp_path: Path,
) -> None:
    source_excerpt = (
        "If any of the characters are reduced to 0 hit points during the fight, "
        "employees of the Yawning Portal step forward to stabilize them."
    )

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Source stabilization",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        artifact = await _call(
            server,
            "module_write",
            {
                "name": "opening.md",
                "content": f"# Chapter\n## Fight\n{source_excerpt}",
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
            for item in await _call(
                server,
                "module_index",
                {"campaign_id": campaign["id"]},
            )
            if item["title"] == "Fight"
        )
        employee = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Encounter threat",
                "character_type": "monster",
                "idempotency_key": "threat",
            },
        )
        hero = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Hero",
                "character_type": "pc",
                "idempotency_key": "hero",
            },
        )
        current = await _call(
            server,
            "campaign_get",
            {"campaign_id": campaign["id"]},
        )
        started = await _raw(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [employee["id"], hero["id"]],
                "participant_config": [
                    {
                        "actor_id": employee["id"],
                        "initiative": 20,
                        "disposition": "hostile",
                        "death_saves": False,
                    },
                    {
                        "actor_id": hero["id"],
                        "initiative": 10,
                        "disposition": "friendly",
                        "death_saves": True,
                    },
                ],
                "scene_id": scene["scene_id"],
                "expected_revision": current["revision"],
                "idempotency_key": "start",
            },
        )
        damaged = await _call(
            server,
            "combat_hp_change",
            {
                "campaign_id": campaign["id"],
                "target_id": hero["id"],
                "action": "damage",
                "payload": {
                    "parts": [{"amount": 1, "damage_type": "bludgeoning"}]
                },
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "damage",
            },
        )
        revision = (
            damaged.get("campaign_revision")
            or (await _call(
                server,
                "campaign_get",
                {"campaign_id": campaign["id"]},
            ))["revision"]
        )
        with pytest.raises(Exception, match="not present in the encounter scene"):
            await _call(
                server,
                "combat_hp_change",
                {
                    "campaign_id": campaign["id"],
                    "target_id": hero["id"],
                    "action": "stabilize",
                    "payload": {"source_excerpt": "An invented helper stabilizes them."},
                    "expected_revision": revision,
                    "idempotency_key": "invented",
                },
            )
        stabilized = await _call(
            server,
            "combat_hp_change",
            {
                "campaign_id": campaign["id"],
                "target_id": hero["id"],
                "action": "stabilize",
                "payload": {"source_excerpt": source_excerpt},
                "expected_revision": revision,
                "idempotency_key": "source-stabilize",
            },
        )
        hero_after = await _call(
            server,
            "character_get",
            {"character_id": hero["id"]},
        )

        assert stabilized["result"]["kind"] == "source_stabilization"
        assert set(hero_after["sheet"]["conditions"]) == {
            "prone",
            "stable",
            "unconscious",
        }
        assert not any(
            item.get("name") == "Yawning Portal employee"
            for item in await _call(
                server,
                "character_list",
                {"campaign_id": campaign["id"]},
            )
        )

    asyncio.run(exercise())


def test_public_stirge_attachment_drains_hit_points_at_source_turn_start(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original_attack_roll = server_module.roll_attack_action
    original_roll = server_module.roll

    def forced_hit(*, plan, rng=None):
        result = original_attack_roll(plan=plan, rng=rng)
        result.update(
            natural=10,
            total=max(int(plan["target_ac"]), int(result.get("total", 0) or 0)),
            armor_class=int(plan["target_ac"]),
            hit=True,
            critical=False,
            fumble=False,
        )
        return result

    def forced_drain(expression: str, **kwargs):
        if expression.replace(" ", "").casefold() == "1d4+3":
            return DiceResult(
                total=7,
                rolls=(4,),
                expression=expression,
                detail="1d4[4] + 3",
            )
        return original_roll(expression, **kwargs)

    monkeypatch.setattr(server_module, "roll_attack_action", forced_hit)
    monkeypatch.setattr(server_module, "roll", forced_drain)
    blood_drain = (
        "and the stirge attaches to the target. While attached, the stirge "
        "doesn't attack. Instead, at the start of each of the stirge's turns, "
        "the target loses 5 (1d4+3) hit points due to blood loss. The stirge "
        "can detach itself by spending 5 feet of its movement. It does so after "
        "it drains 10 hit points of blood from the target or the target dies. "
        "A creature, including the target, can use its action to detach the stirge."
    )

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Source attachment",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        stirge = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Stirge",
                "character_type": "monster",
                "idempotency_key": "stirge",
            },
        )
        target = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Target",
                "character_type": "pc",
                "idempotency_key": "target",
            },
        )
        stirge_sheet = default_character_sheet()
        stirge_sheet["combat"]["hp"] = {"value": 2, "max": 2, "temp": 0}
        stirge_sheet["inventory"]["items"] = [
            {
                "id": "blood-drain",
                "name": "Blood Drain",
                "kind": "weapon",
                "equipped": True,
                "equipped_slot": "main_hand",
                "mechanics": {
                    "attack_type": "melee",
                    "attack_ability": "dexterity",
                    "damage_formula": "1",
                    "damage_type": "piercing",
                    "on_hit_effect": blood_drain,
                    "reach_ft": 5,
                    "attack_bonus_override": 5,
                    "always_available": True,
                },
            }
        ]
        stirge_sheet["inventory"]["equipment_slots"]["main_hand"] = "blood-drain"
        target_sheet = default_character_sheet()
        target_sheet["combat"]["hp"] = {"value": 20, "max": 20, "temp": 5}
        for actor, sheet, key in (
            (stirge, stirge_sheet, "stirge-sheet"),
            (target, target_sheet, "target-sheet"),
        ):
            await _call(
                server,
                "character_sheet_replace",
                {
                    "character_id": actor["id"],
                    "sheet": sheet,
                    "expected_revision": actor["revision"],
                    "idempotency_key": key,
                },
            )
        current = await _call(
            server,
            "campaign_get",
            {"campaign_id": campaign["id"]},
        )
        started = await _raw(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [stirge["id"], target["id"]],
                "participant_config": [
                    {
                        "actor_id": stirge["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                    },
                    {
                        "actor_id": target["id"],
                        "initiative": 10,
                        "position": {"x": 1, "y": 0},
                        "death_saves": True,
                    },
                ],
                "expected_revision": current["revision"],
                "idempotency_key": "start",
            },
        )
        attacked = await _raw(
            server,
            "combat_resolve_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": stirge["id"],
                "target_id": target["id"],
                "action": {
                    "weapon_id": "blood-drain",
                    "attack_mode": "melee",
                },
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "attack",
            },
        )
        attached = await _raw(
            server,
            "combat_on_hit_ruling",
            {
                "campaign_id": campaign["id"],
                "target_id": target["id"],
                "choice_id": attacked["result"]["pending_on_hit_ruling_id"],
                "selection": {
                    "id": "attachment",
                    "source_excerpt": blood_drain,
                },
                "expected_revision": attacked["campaign_revision"],
                "idempotency_key": "attach",
            },
        )
        ended_stirge = await _raw(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": stirge["id"],
                "expected_revision": attached["campaign_revision"],
                "idempotency_key": "end-stirge",
            },
        )
        ended_target = await _raw(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": target["id"],
                "expected_revision": ended_stirge["campaign_revision"],
                "idempotency_key": "end-target",
            },
        )
        target_after = await _call(
            server,
            "character_get",
            {"character_id": target["id"]},
        )
        drain = ended_target["source_turn_start"][0]
        assert drain["kind"] == "attachment_drain"
        assert drain["roll"]["total"] == 7
        assert drain["result"]["hit_point_loss"] == 7
        assert drain["result"]["bypassed_temp_hp"] == 4
        assert target_after["sheet"]["combat"]["hp"] == {
            "value": 13,
            "max": 20,
            "temp": 4,
        }

    asyncio.run(exercise())


def test_public_grimvault_only_opens_dm_boundary_after_both_natural_twenty_rolls(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original_attack_roll = server_module.roll_attack_action
    original_engine_roll = combat_engine_module.roll

    def forced_critical(*, plan, rng=None):
        result = original_attack_roll(plan=plan, rng=rng)
        result.update(
            natural=20,
            total=20 + int(plan["attack_bonus"]),
            armor_class=int(plan["target_ac"]),
            hit=True,
            critical=True,
            fumble=False,
        )
        return result

    def forced_damage_and_followup(expression: str, **kwargs):
        normalized = expression.replace(" ", "").casefold()
        if normalized == "4d6+4":
            return DiceResult(
                total=18,
                rolls=(3, 4, 3, 4),
                expression=expression,
                detail="4d6[3, 4, 3, 4] + 4",
            )
        if normalized == "14":
            return DiceResult(
                total=14,
                rolls=(),
                expression=expression,
                detail="14",
            )
        if normalized == "1d20":
            return DiceResult(
                total=20,
                rolls=(20,),
                expression=expression,
                detail="1d20[20]",
            )
        return original_engine_roll(expression, **kwargs)

    monkeypatch.setattr(server_module, "roll_attack_action", forced_critical)
    monkeypatch.setattr(combat_engine_module, "roll", forced_damage_and_followup)
    grimvault_effect = (
        "If the target is an object, the hit instead deals 16 slashing damage. "
        "If the target is a creature and Durnan rolls a 20 on the d20 for the "
        "attack roll, the target takes an extra 14 slashing damage, and Durnan "
        "rolls another d20. On a roll of 20, he lops off one of the target's "
        "limbs, or some other part of its body if it is limbless."
    )

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Grimvault critical follow-up",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        durnan = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Durnan",
                "character_type": "npc",
                "idempotency_key": "durnan",
            },
        )
        troll = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Troll",
                "character_type": "monster",
                "idempotency_key": "troll",
            },
        )
        durnan_sheet = default_character_sheet()
        durnan_sheet["inventory"]["items"] = [
            {
                "id": "grimvault",
                "name": "Grimvault",
                "kind": "weapon",
                "equipped": True,
                "equipped_slot": "main_hand",
                "mechanics": {
                    "attack_type": "melee",
                    "attack_ability": "strength",
                    "damage_formula": "2d6",
                    "damage_bonus_override": 4,
                    "damage_type": "slashing",
                    "on_hit_effect": grimvault_effect,
                    "reach_ft": 5,
                    "attack_bonus_override": 8,
                    "always_available": True,
                },
            }
        ]
        durnan_sheet["inventory"]["equipment_slots"]["main_hand"] = "grimvault"
        troll_sheet = default_character_sheet()
        troll_sheet["combat"]["hp"] = {"value": 84, "max": 84, "temp": 0}
        for actor, sheet, key in (
            (durnan, durnan_sheet, "durnan-sheet"),
            (troll, troll_sheet, "troll-sheet"),
        ):
            await _call(
                server,
                "character_sheet_replace",
                {
                    "character_id": actor["id"],
                    "sheet": sheet,
                    "expected_revision": actor["revision"],
                    "idempotency_key": key,
                },
            )
        current = await _call(
            server,
            "campaign_get",
            {"campaign_id": campaign["id"]},
        )
        started = await _raw(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [durnan["id"], troll["id"]],
                "participant_config": [
                    {
                        "actor_id": durnan["id"],
                        "initiative": 20,
                        "disposition": "friendly",
                        "death_saves": False,
                    },
                    {
                        "actor_id": troll["id"],
                        "initiative": 10,
                        "disposition": "hostile",
                        "death_saves": False,
                    },
                ],
                "expected_revision": current["revision"],
                "idempotency_key": "start",
            },
        )
        attacked = await _raw(
            server,
            "combat_resolve_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": durnan["id"],
                "target_id": troll["id"],
                "action": {"weapon_id": "grimvault", "attack_mode": "melee"},
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "attack",
            },
        )
        assert attacked["status"] == "pending_ruling"
        assert attacked["result"]["damage"]["input_amount"] == 32
        assert attacked["result"]["critical_followup"]["triggered"] is True
        assert (
            attacked["result"]["critical_followup"]["anatomical_loss_triggered"]
            is True
        )
        ruled = await _raw(
            server,
            "combat_on_hit_ruling",
            {
                "campaign_id": campaign["id"],
                "target_id": troll["id"],
                "choice_id": attacked["result"]["pending_on_hit_ruling_id"],
                "selection": {
                    "id": "critical_followup",
                    "target_has_limbs": True,
                    "source_excerpt": grimvault_effect,
                },
                "expected_revision": attacked["campaign_revision"],
                "idempotency_key": "record-loss",
            },
        )
        troll_after = await _call(
            server,
            "character_get",
            {"character_id": troll["id"]},
        )

        assert ruled["result"]["part_category"] == "limb"
        assert ruled["result"]["mechanical_effect"] == "dm_unspecified"
        assert any(
            effect.get("kind") == "anatomical_loss"
            and effect.get("active")
            for effect in troll_after["sheet"]["effects"]
        )

    asyncio.run(exercise())
