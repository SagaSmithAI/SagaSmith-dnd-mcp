from __future__ import annotations

import asyncio
import random
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


async def _call_raw(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    if name in {"character_action", "combat_movement", "combat_hp_change"}:
        return result["result"]
    return result


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


def test_combat_query_exposes_dm_transaction_history_and_receipts(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Combat transaction evidence",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Receipt actor"},
                "principal_id": "system:local",
                "idempotency_key": "receipt-actor",
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
                "idempotency_key": "receipt-phase",
            },
        )

        history = await _call(
            server,
            "combat_query",
            {
                "campaign_id": campaign["id"],
                "view": "transaction_history",
                "payload": {"limit": 100},
            },
        )
        actor_revision = next(
            item for item in history if item["idempotency_key"] == "receipt-phase"
        )
        assert actor_revision["request_hash"]

        receipt = await _call(
            server,
            "combat_query",
            {
                "campaign_id": campaign["id"],
                "view": "transaction_receipt",
                "payload": {"idempotency_key": "receipt-phase"},
            },
        )
        assert receipt["key"] == "receipt-phase"
        assert receipt["response"]["campaign_id"] == campaign["id"]
        assert receipt["response"]["campaign_revision"] == phase["campaign_revision"]
        assert actor["id"]

    asyncio.run(exercise())


def test_end_turn_does_not_revision_unchanged_character_documents(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Lean turn revisions", "edition": "2014", "idempotency_key": "campaign"},
        )
        actors = []
        for index in range(2):
            actors.append(
                await _call(
                    server,
                    "character_create_from",
                    {
                        "mode": "direct",
                        "payload": {"campaign_id": campaign["id"], "name": f"Actor {index + 1}"},
                        "principal_id": "system:local",
                        "idempotency_key": f"actor-{index + 1}",
                    },
                )
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
        started = await _call(
            server,
            "combat_start",
            {
                "positioning_mode": "agent",
                "campaign_id": campaign["id"],
                "participant_ids": [item["id"] for item in actors],
                "participant_config": [
                    {"actor_id": actors[0]["id"], "initiative": 20},
                    {"actor_id": actors[1]["id"], "initiative": 10},
                ],
                "expected_revision": campaign["revision"],
                "idempotency_key": "start",
            },
        )

        ended = await _call(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": actors[0]["id"],
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "end",
            },
        )
        current = [
            await _call(
                server,
                "character_query",
                {
                    "view": "get",
                    "payload": {"character_id": item["id"]},
                    "principal_id": "system:local",
                },
            )
            for item in actors
        ]

        assert [item["entity_type"] for item in ended["revisions"]] == ["campaign"]
        assert [item["revision"] for item in current] == [item["revision"] for item in actors]

    asyncio.run(exercise())


def test_available_actions_explicitly_discovers_required_death_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_death_save = server_module.resolve_death_save_to_sheet

    def deterministic_death_save(sheet, **kwargs):
        return original_death_save(sheet, **kwargs, rng=random.Random(1))

    monkeypatch.setattr(server_module, "resolve_death_save_to_sheet", deterministic_death_save)

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Death action", "edition": "2014", "idempotency_key": "campaign"},
        )
        sheet = default_character_sheet()
        sheet["combat"]["hp"] = {"value": 0, "max": 10, "temp": 0}
        sheet["conditions"] = ["prone", "unconscious"]
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Dying PC", "sheet": sheet},
                "principal_id": "system:local",
                "idempotency_key": "actor",
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
        started = await _call(
            server,
            "combat_start",
            {
                "positioning_mode": "agent",
                "campaign_id": campaign["id"],
                "participant_ids": [actor["id"]],
                "participant_config": [
                    {"actor_id": actor["id"], "initiative": 10, "death_saves": True}
                ],
                "expected_revision": campaign["revision"],
                "idempotency_key": "start",
            },
        )

        available = await _call(
            server,
            "combat_query",
            {
                "campaign_id": campaign["id"],
                "view": "available_actions",
                "actor_id": actor["id"],
                "principal_id": "system:local",
            },
        )

        assert started["combat"]["round"] == 1
        assert available["actions"] == ["death_save"]
        resolved = await _call_raw(
            server,
            "combat_check",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "kind": "death_save",
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "death-save",
            },
        )
        assert resolved["result"]["kind"] == "death_save"
        assert resolved["result"]["outcome"] == "pending"

        after = await _call(
            server,
            "combat_query",
            {
                "campaign_id": campaign["id"],
                "view": "available_actions",
                "actor_id": actor["id"],
                "principal_id": "system:local",
            },
        )
        assert after["actions"] == []

    asyncio.run(exercise())


def test_invalid_branch_is_rejected_before_noncombat_check_rolls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rolled = False

    def forbidden_roll(*args, **kwargs):
        nonlocal rolled
        rolled = True
        raise AssertionError("the check must not roll")

    monkeypatch.setattr(server_module, "resolve_actor_check", forbidden_roll)

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Branch guard", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Checker"},
                "principal_id": "system:local",
                "idempotency_key": "actor",
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
        await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": campaign["revision"],
                "idempotency_key": "enter-play",
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

        with pytest.raises(Exception, match="checked-out branch"):
            await _call(
                server,
                "character_check",
                {
                    "campaign_id": campaign["id"],
                    "action": "check",
                    "payload": {
                        "actor_id": actor["id"],
                        "kind": "check",
                        "ability": "wisdom",
                        "dc": 10,
                    },
                    "branch_id": "not-the-current-branch",
                    "expected_revision": campaign["revision"],
                    "idempotency_key": "invalid-branch-check",
                },
            )

    asyncio.run(exercise())
    assert rolled is False


def test_jack_of_all_trades_is_applied_and_receipted_by_public_tools(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Jack audit", "edition": "2014", "idempotency_key": "campaign"},
        )
        sheet = default_character_sheet()
        sheet["progression"] = {
            "level": 2,
            "classes": [{"name": "Bard", "level": 2, "hit_die": 8}],
        }
        sheet["abilities"]["charisma"]["score"] = 16
        sheet["abilities"]["dexterity"]["score"] = 14
        sheet["content"]["features"] = [
            {
                "id": "dnd5e.content.srd2014.feature.bard-jack-of-all-trades",
                "name": "Jack of All Trades",
                "source_key": "Bard",
                "mechanic_refs": ["dnd5e.core.check.jack_of_all_trades"],
            }
        ]
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Bard", "sheet": sheet},
                "principal_id": "system:local",
                "idempotency_key": "actor",
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
        await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": campaign["revision"],
                "idempotency_key": "enter-play",
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
        checked = await _call(
            server,
            "character_check",
            {
                "campaign_id": campaign["id"],
                "action": "check",
                "payload": {
                    "actor_id": actor["id"],
                    "kind": "check",
                    "ability": "intimidation",
                    "dc": 0,
                },
                "expected_revision": campaign["revision"],
                "idempotency_key": "untrained-check",
            },
        )
        assert checked["ability_modifier"] == 3
        assert checked["bonus"] == 1
        assert [item["mechanic_id"] for item in checked["rule_receipts"]] == [
            "dnd5e.core.check.jack_of_all_trades"
        ]

        campaign = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        started = await _call(
            server,
            "combat_start",
            {
                "positioning_mode": "agent",
                "campaign_id": campaign["id"],
                "participant_ids": [actor["id"]],
                "expected_revision": campaign["revision"],
                "idempotency_key": "combat-start",
            },
        )
        combatant = started["combat"]["combatants"][0]
        assert combatant["initiative_bonus"] == 3
        assert started["combat"]["rule_boundary_ids"] == ["dnd5e.core.check.jack_of_all_trades"]

        receipts = await _call(
            server,
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "receipts",
                "payload": {},
                "principal_id": "system:local",
            },
        )
        jack_receipts = [
            item
            for item in receipts
            if item["mechanic_id"] == "dnd5e.core.check.jack_of_all_trades"
        ]
        assert {item["receipt"]["event"] for item in jack_receipts} == {
            "check.resolve",
            "combat.start",
        }

    asyncio.run(exercise())


def test_action_surge_is_settled_without_a_manual_ruling(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Action Surge", "edition": "2014", "idempotency_key": "campaign"},
        )
        sheet = default_character_sheet()
        sheet["content"]["features"] = [
            {
                "id": "dnd5e.content.srd2014.feature.fighter-action-surge",
                "name": "Action Surge",
                "source_key": "Fighter",
                "description": "Take one additional action on your turn.",
                "uses": {
                    "label": "Action Surge",
                    "value": 1,
                    "max": 1,
                    "recovers_on": "short_rest",
                },
                "resource_key": "",
                "activation": {"type": "special", "cost": 0, "trigger": ""},
                "scaling": [],
                "choices": {"outcome": "take one additional action on this turn"},
            }
        ]
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Fighter", "sheet": sheet},
                "principal_id": "system:local",
                "idempotency_key": "actor",
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
        started = await _call_raw(
            server,
            "combat_start",
            {
                "positioning_mode": "agent",
                "campaign_id": campaign["id"],
                "participant_ids": [actor["id"]],
                "participant_config": [{"actor_id": actor["id"], "initiative": 10}],
                "expected_revision": campaign["revision"],
                "idempotency_key": "start",
            },
        )
        surged = await _call_raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "activity_id": "dnd5e.content.srd2014.feature.fighter-action-surge",
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "surge",
            },
        )

        assert surged["status"] == "committed"
        assert surged["result"]["requires_ruling"] is False
        assert surged["result"]["core_effect"]["extra_actions_granted"] == 1
        current = surged["combat"]["combatants"][surged["combat"]["turn_index"]]
        assert current["turn_budget"]["extra_action"] == 1
        assert any(
            item["mechanic_id"] == "dnd5e.core.activity.action_surge"
            for item in surged["result"]["rule_receipts"]
        )
        actor_after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actor["id"]},
                "principal_id": "system:local",
            },
        )
        assert actor_after["sheet"]["content"]["features"][0]["uses"]["value"] == 0

    asyncio.run(exercise())


def test_recharge_weapon_use_is_committed_on_attack_declaration(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Recharge weapon transaction",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        sheet = default_character_sheet()
        sheet["inventory"]["items"] = [
            {
                "id": "breath-recharge-5-6",
                "name": "Breath (Recharge 5-6)",
                "kind": "weapon",
                "mechanics": {
                    "attack_type": "ranged",
                    "attack_ability": "dexterity",
                    "damage_formula": "1d6",
                    "damage_type": "fire",
                    "attack_bonus_override": 20,
                    "always_available": True,
                    "normal_range_ft": 30,
                    "long_range_ft": 30,
                    "recharge": {
                        "kind": "d6_turn_start",
                        "minimum": 5,
                        "maximum": 6,
                        "source_marker": "(Recharge 5-6)",
                    },
                },
                "uses": {
                    "label": "Breath (Recharge 5-6)",
                    "value": 1,
                    "max": 1,
                    "recovers_on": "manual",
                    "source_key": "test:recharge",
                },
            }
        ]
        attacker = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Recharge attacker",
                    "sheet": sheet,
                },
                "principal_id": "system:local",
                "idempotency_key": "attacker",
            },
        )
        target = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Target"},
                "principal_id": "system:local",
                "idempotency_key": "target",
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
        started = await _call_raw(
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

        attacked = await _call_raw(
            server,
            "combat_resolve_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": attacker["id"],
                "target_id": target["id"],
                "action": {
                    "weapon_id": "breath-recharge-5-6",
                    "attack_mode": "ranged",
                },
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "attack",
            },
        )
        after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": attacker["id"]},
                "principal_id": "system:local",
            },
        )

        assert attacked["result"]["limited_use"]["remaining"] == 0
        assert after["sheet"]["inventory"]["items"][0]["uses"]["value"] == 0

    asyncio.run(exercise())


def test_locked_dragonborn_breath_uses_generic_area_and_save_primitives(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Dragonborn Breath Weapon",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        activity_id = "dnd5e.content.standard2014.species.dragonborn.activity.breath-weapon"
        source_sheet = default_character_sheet()
        source_sheet["progression"]["level"] = 1
        source_sheet["content"]["activities"] = [
            {
                "id": activity_id,
                "name": "Breath Weapon",
                "source_key": "Dragonborn",
                "description": "Reviewed standard Breath Weapon settlement.",
                "uses": {
                    "label": "Breath Weapon",
                    "value": 1,
                    "max": 1,
                    "recovers_on": "short_rest",
                    "source_key": "Dragonborn",
                },
                "activation": {"type": "action", "cost": 1},
                "choices": {
                    "standard_resolution": {
                        "kind": "area_save_damage",
                        "origin": {"kind": "self"},
                        "area": {"shape": "cone", "length_ft": 15},
                        "targets": "each_creature",
                        "save_ability": "dexterity",
                        "save_dc_formula": {
                            "base": 8,
                            "ability": "constitution",
                            "include_proficiency": True,
                        },
                        "damage_formula_by_level": {
                            "1": "2d6",
                            "6": "3d6",
                            "11": "4d6",
                            "16": "5d6",
                        },
                        "damage_type": "fire",
                        "half_on_success": True,
                        "save_source_kind": "nonmagical_effect",
                        "source_excerpt": "Reviewed standard Breath Weapon settlement.",
                    }
                },
                "pack_id": "dnd5e.content.standard2014",
                "pack_version": "1.4.0",
                "rule_refs": ["book:players-handbook-2014:p34"],
                "mechanic_refs": ["dnd5e.core.activity.dragonborn_breath_weapon"],
            }
        ]
        target_sheet = default_character_sheet()
        target_sheet["combat"]["hp"] = {"value": 30, "max": 30, "temp": 0}
        source = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Dragonborn",
                    "sheet": source_sheet,
                },
                "principal_id": "system:local",
                "idempotency_key": "source",
            },
        )
        target = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Target", "sheet": target_sheet},
                "principal_id": "system:local",
                "idempotency_key": "target",
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
        started = await _call_raw(
            server,
            "combat_start",
            {
                "positioning_mode": "grid",
                "battle_map": {"width_cells": 12, "height_cells": 12},
                "campaign_id": campaign["id"],
                "participant_ids": [source["id"], target["id"]],
                "participant_config": [
                    {
                        "actor_id": source["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                    },
                    {
                        "actor_id": target["id"],
                        "initiative": 10,
                        "position": {"x": 2, "y": 0},
                    },
                ],
                "expected_revision": campaign["revision"],
                "idempotency_key": "start",
            },
        )

        used = await _call_raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": source["id"],
                "activity_id": activity_id,
                "declaration": {
                    "origin": {"x": 1, "y": 0},
                    "target_contexts": [{"target_id": target["id"], "cover": "none"}],
                },
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "breath",
            },
        )
        source_after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": source["id"]},
                "principal_id": "system:local",
            },
        )
        target_after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": target["id"]},
                "principal_id": "system:local",
            },
        )

        effect = used["result"]["core_effect"]
        assert used["status"] == "committed"
        assert effect["kind"] == "dragonborn_breath_weapon"
        assert effect["damage_expression"] == "2d6"
        assert effect["target_contexts"] == [
            {"target_id": target["id"], "distance_ft": 10.0, "cover": "none"}
        ]
        assert source_after["sheet"]["content"]["activities"][0]["uses"]["value"] == 0
        assert target_after["sheet"]["combat"]["hp"]["value"] < 30

    asyncio.run(exercise())


@pytest.mark.parametrize("edition", ["2014", "2024"])
def test_second_wind_heals_and_pays_bonus_action_atomically(
    tmp_path: Path,
    edition: str,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Second Wind", "edition": edition, "idempotency_key": "campaign"},
        )
        sheet = default_character_sheet()
        sheet["edition"] = edition
        sheet["progression"]["level"] = 2
        sheet["progression"]["classes"] = [
            {"name": "Fighter", "level": 2, "subclass": "", "hit_die": 10}
        ]
        sheet["combat"]["hp"] = {"value": 1, "max": 20, "temp": 0}
        second_wind_id = f"dnd5e.content.srd{edition}.feature.fighter-second-wind"
        if edition == "2024":
            sheet["resources"]["second_wind"] = {
                "label": "Second Wind",
                "value": 2,
                "max": 2,
                "recovers_on": "long_rest",
                "recovery_amounts": {"short_rest": 1, "long_rest": "all"},
                "source_key": "Fighter",
            }
        feature = {
            "id": second_wind_id,
            "name": "Second Wind",
            "source_key": "Fighter",
            "description": "Regain 1d10 + Fighter level hit points.",
            **(
                {
                    "uses": {
                        "label": "Second Wind",
                        "value": 1,
                        "max": 1,
                        "recovers_on": "short_rest",
                    }
                }
                if edition == "2014"
                else {}
            ),
            "resource_key": "second_wind" if edition == "2024" else "",
            "activation": {"type": "bonus_action", "cost": 1, "trigger": ""},
            "scaling": [],
            "choices": {"outcome": "roll 1d10 + fighter level"},
            "mechanic_refs": ["dnd5e.core.activity.second_wind"],
        }
        sheet["content"]["features"] = [feature]
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Fighter", "sheet": sheet},
                "principal_id": "system:local",
                "idempotency_key": "actor",
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
        started = await _call_raw(
            server,
            "combat_start",
            {
                "positioning_mode": "agent",
                "campaign_id": campaign["id"],
                "participant_ids": [actor["id"]],
                "participant_config": [{"actor_id": actor["id"], "initiative": 10}],
                "expected_revision": campaign["revision"],
                "idempotency_key": "start",
            },
        )

        result = await _call_raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "activity_id": second_wind_id,
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "second-wind",
            },
        )

        assert result["status"] == "committed"
        assert result["result"]["requires_ruling"] is False
        effect = result["result"]["core_effect"]
        assert effect["kind"] == "second_wind"
        assert effect["fighter_level"] == 2
        assert 4 <= effect["after_hp"] <= 13
        current = result["combat"]["combatants"][result["combat"]["turn_index"]]
        assert current["turn_budget"]["bonus_action"] == 0
        actor_after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actor["id"]},
                "principal_id": "system:local",
            },
        )
        assert actor_after["sheet"]["combat"]["hp"]["value"] == effect["after_hp"]
        if edition == "2014":
            assert actor_after["sheet"]["content"]["features"][0]["uses"]["value"] == 0
        else:
            assert actor_after["sheet"]["resources"]["second_wind"]["value"] == 1
        assert any(
            item["mechanic_id"] == "dnd5e.core.activity.second_wind"
            for item in result["result"]["rule_receipts"]
        )

    asyncio.run(exercise())


def test_second_wind_heals_and_advances_random_stream_outside_combat(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Noncombat Second Wind",
                "edition": "2014",
                "random_seed": "noncombat-second-wind",
                "idempotency_key": "campaign",
            },
        )
        sheet = default_character_sheet()
        sheet["progression"]["level"] = 1
        sheet["progression"]["classes"] = [
            {"name": "Fighter", "level": 1, "subclass": "", "hit_die": 10}
        ]
        sheet["combat"]["hp"] = {"value": 2, "max": 12, "temp": 0}
        sheet["content"]["features"] = [
            {
                "id": "dnd5e.content.srd2014.feature.fighter-second-wind",
                "name": "Second Wind",
                "source_key": "Fighter",
                "description": "Regain 1d10 + Fighter level hit points.",
                "uses": {
                    "label": "Second Wind",
                    "value": 1,
                    "max": 1,
                    "recovers_on": "short_rest",
                },
                "resource_key": "",
                "activation": {"type": "bonus_action", "cost": 1, "trigger": ""},
                "scaling": [],
                "choices": {"outcome": "roll 1d10 + fighter level"},
            }
        ]
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Fighter", "sheet": sheet},
                "principal_id": "system:local",
                "idempotency_key": "actor",
            },
        )
        arguments = {
            "character_id": actor["id"],
            "activity_id": "dnd5e.content.srd2014.feature.fighter-second-wind",
            "expected_revision": actor["revision"],
            "idempotency_key": "second-wind",
        }

        before = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        stream = server_module.CampaignRandomStream.from_campaign_state(
            campaign["id"],
            before["state"],
            operation="character_action",
            idempotency_key="second-wind",
        )
        with server_module.use_random_stream(stream):
            result = await _call_raw(
                server,
                "character_action",
                {
                    "character_id": arguments["character_id"],
                    "action": "use_activity",
                    "payload": {"activity_id": arguments["activity_id"]},
                    "expected_revision": arguments["expected_revision"],
                    "idempotency_key": arguments["idempotency_key"],
                },
            )
        assert stream.has_unpersisted_draws is False
        assert (
            await _call_raw(
                server,
                "character_action",
                {
                    "character_id": actor["id"],
                    "action": "use_activity",
                    "payload": {"activity_id": "dnd5e.content.srd2014.feature.fighter-second-wind"},
                    "expected_revision": actor["revision"],
                    "idempotency_key": "second-wind",
                },
            )
            == result
        )
        assert result["status"] == "committed"
        effect = result["result"]["core_effect"]
        assert effect["kind"] == "second_wind"
        assert effect["fighter_level"] == 1
        assert 4 <= effect["after_hp"] <= 12
        assert effect["after_hp"] == result["character"]["sheet"]["combat"]["hp"]["value"]
        assert result["character"]["sheet"]["content"]["features"][0]["uses"]["value"] == 0
        assert any(
            item["mechanic_id"] == "dnd5e.core.activity.second_wind"
            for item in result["result"]["rule_receipts"]
        )
        current = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        assert current["state"]["random_stream"]["position"] == 1

    asyncio.run(exercise())


def test_cunning_action_dash_uses_bonus_action_and_doubles_movement(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Cunning Action", "edition": "2014", "idempotency_key": "campaign"},
        )
        sheet = default_character_sheet()
        sheet["progression"]["level"] = 2
        sheet["progression"]["classes"] = [
            {"name": "Rogue", "level": 2, "subclass": "", "hit_die": 8}
        ]
        sheet["content"]["features"] = [
            {
                "id": "dnd5e.content.srd2014.feature.rogue-cunning-action",
                "name": "Cunning Action",
                "source_key": "Rogue",
                "description": "Dash, Disengage, or Hide as a bonus action.",
                "uses": {
                    "label": "",
                    "value": 0,
                    "max": 0,
                    "unlimited": True,
                    "recovers_on": "none",
                },
                "resource_key": "",
                "activation": {"type": "bonus_action", "cost": 1, "trigger": ""},
                "scaling": [],
                "choices": {"options": ["Dash", "Disengage", "Hide"]},
            }
        ]
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Rogue", "sheet": sheet},
                "principal_id": "system:local",
                "idempotency_key": "actor",
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
        started = await _call_raw(
            server,
            "combat_start",
            {
                "positioning_mode": "agent",
                "campaign_id": campaign["id"],
                "participant_ids": [actor["id"]],
                "participant_config": [{"actor_id": actor["id"], "initiative": 10}],
                "expected_revision": campaign["revision"],
                "idempotency_key": "start",
            },
        )

        result = await _call_raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "activity_id": "dnd5e.content.srd2014.feature.rogue-cunning-action",
                "declaration": {"action": "dash"},
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "cunning-dash",
            },
        )

        assert result["status"] == "committed"
        assert result["result"]["requires_ruling"] is False
        current = result["combat"]["combatants"][result["combat"]["turn_index"]]
        assert current["turn_budget"]["movement"] == 60
        assert current["turn_budget"]["bonus_action"] == 0
        assert current["turn_budget"]["main_action"] == 1
        assert any(
            item["mechanic_id"] == "dnd5e.core.activity.cunning_action"
            for item in result["result"]["rule_receipts"]
        )

    asyncio.run(exercise())


def test_combat_move_charges_reviewed_difficult_cells_and_records_core_receipt(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Difficult terrain", "edition": "2014", "idempotency_key": "campaign"},
        )
        mover = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Mover"},
                "principal_id": "system:local",
                "idempotency_key": "mover",
            },
        )
        other = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Other"},
                "principal_id": "system:local",
                "idempotency_key": "other",
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
        started = await _call_raw(
            server,
            "combat_start",
            {
                "positioning_mode": "grid",
                "campaign_id": campaign["id"],
                "participant_ids": [mover["id"], other["id"]],
                "participant_config": [
                    {
                        "actor_id": mover["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                        "hidden": True,
                        "visible_to_actor_ids": [mover["id"]],
                    },
                    {
                        "actor_id": other["id"],
                        "initiative": 10,
                        "position": {"x": 4, "y": 0},
                    },
                ],
                "battle_map": {
                    "width_cells": 6,
                    "height_cells": 4,
                    "difficult_cells": [{"x": 1, "y": 0}],
                },
                "expected_revision": campaign["revision"],
                "idempotency_key": "start",
            },
        )

        pending = await _call(
            server,
            "combat_movement",
            {
                "campaign_id": campaign["id"],
                "actor_id": mover["id"],
                "action": "move",
                "payload": {"distance": 10, "destination": {"x": 2, "y": 0}},
                "principal_id": "system:local",
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "move-without-path",
            },
        )

        assert pending["status"] == "pending_ruling"
        assert pending["default_resolver"] == "agent"
        assert pending["committed"] is False
        assert pending["missing"] == ["movement_path_for_difficult_terrain"]

        moved = await _call_raw(
            server,
            "combat_movement",
            {
                "campaign_id": campaign["id"],
                "actor_id": mover["id"],
                "action": "move",
                "payload": {
                    "distance": 10,
                    "destination": {"x": 2, "y": 0},
                    "path": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 2, "y": 0}],
                },
                "principal_id": "system:local",
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "move",
            },
        )

        current = moved["combat"]["combatants"][moved["combat"]["turn_index"]]
        assert current["turn_budget"]["movement"] == 15
        receipts = await _call(
            server,
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "receipts",
                "payload": {},
                "principal_id": "system:local",
            },
        )
        assert any(
            item["mechanic_id"] == "dnd5e.core.movement.difficult_terrain" for item in receipts
        )
        revealed = await _call_raw(
            server,
            "combat_map_patch",
            {
                "campaign_id": campaign["id"],
                "patches": [
                    {
                        "key": "combatant_visibility",
                        "value": {
                            "actor_id": mover["id"],
                            "hidden": False,
                            "visible_to_actor_ids": None,
                            "reason": "The hidden actor shouted from its new position.",
                        },
                    }
                ],
                "expected_revision": moved["campaign_revision"],
                "idempotency_key": "reveal",
            },
        )
        mover_after = next(
            item for item in revealed["combat"]["combatants"] if item["actor_id"] == mover["id"]
        )
        assert mover_after["hidden"] is False
        assert mover_after["visible_to_actor_ids"] is None
        assert revealed["campaign_revision"] == moved["campaign_revision"] + 1
        departed = await _call_raw(
            server,
            "combat_map_patch",
            {
                "campaign_id": campaign["id"],
                "patches": [
                    {
                        "key": "combatant_departure",
                        "value": {
                            "actor_id": other["id"],
                            "reason": "The source says one guard flees to warn the leader.",
                            "destination_location_key": "8-klarg-s-cave",
                        },
                    }
                ],
                "expected_revision": revealed["campaign_revision"],
                "idempotency_key": "source-departure",
            },
        )
        other_after = next(
            item for item in departed["combat"]["combatants"] if item["actor_id"] == other["id"]
        )
        assert other_after["departed"] == {
            "reason": "The source says one guard flees to warn the leader.",
            "destination_location_key": "8-klarg-s-cave",
        }
        assert other_after["hidden"] is True
        assert departed["world_patches"] == [
            {
                "key": "combatant_departure",
                "value": {
                    "actor_id": other["id"],
                    "reason": "The source says one guard flees to warn the leader.",
                    "destination_location_key": "8-klarg-s-cave",
                },
            }
        ]

    asyncio.run(exercise())
