from __future__ import annotations

import asyncio
import random
from copy import deepcopy
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import (
    default_character_sheet,
    validate_character_sheet,
)
from sagasmith_dnd.content_solution import build_content_solution
from sagasmith_dnd.resolution_plan import (
    compile_resolution_plan,
    resolution_plan_template,
)

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


async def _call_raw(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
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
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Receipt actor",
                "idempotency_key": "receipt-actor",
            },
        )
        campaign = await _call(
            server,
            "campaign_get",
            {"campaign_id": campaign["id"]},
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
                    "character_create",
                    {
                        "campaign_id": campaign["id"],
                        "name": f"Actor {index + 1}",
                        "idempotency_key": f"actor-{index + 1}",
                    },
                )
            )
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
        started = await _call(
            server,
            "combat_start",
            {
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
            await _call(server, "character_get", {"character_id": item["id"]}) for item in actors
        ]

        assert [item["entity_type"] for item in ended["revisions"]] == ["campaign"]
        assert [item["revision"] for item in current] == [item["revision"] for item in actors]

    asyncio.run(exercise())


def test_agent_source_damage_is_authorized_and_settled_in_one_attack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_excerpt = (
        "If the peryton is flying and dives at least 30 feet straight toward a "
        "target and then hits it with a melee weapon attack, the attack deals an "
        "extra 9 (2d8) damage to the target."
    )
    original_attack_roll = server_module.roll_attack_action

    def forced_critical(*, plan, rng=None):
        result = original_attack_roll(plan=plan, rng=rng)
        result.update(
            natural=20,
            total=max(int(plan["target_ac"]), int(result.get("total", 0) or 0)),
            armor_class=int(plan["target_ac"]),
            hit=True,
            critical=True,
            fumble=False,
        )
        return result

    monkeypatch.setattr(server_module, "roll_attack_action", forced_critical)

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Atomic Agent source damage",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        attacker_sheet = default_character_sheet()
        source_feature = {
            "id": "dive-attack-passive",
            "name": "Dive Attack",
            "description": source_excerpt,
            "choices": {
                "manual_ruling": {
                    "kind": "descriptive_passive",
                    "default_resolver": "agent",
                    "source_excerpt": source_excerpt,
                }
            },
        }
        compiled_plan = compile_resolution_plan(
            {
                "schema_version": 2,
                "id": "custom.peryton.dive-attack",
                "source_card_id": "dive-attack-passive",
                "source_card_kind": "feature",
                "trigger": "attack.after_hit",
                "trigger_filter": {"hit": True},
                "slots": {},
                "steps": [
                    {
                        "id": "extra-damage",
                        "op": "damage.apply",
                        "args": {
                            "target_ids": ["semantic-target"],
                            "expression": "2d8",
                            "damage_type": "piercing",
                            "source": "Dive Attack",
                            "critical": True,
                        },
                    }
                ],
                "citations": [
                    {
                        "source": "unit-test",
                        "source_ref": {"chunk_id": "unit-test"},
                        "source_excerpt": source_excerpt,
                    }
                ],
            }
        )
        source_feature["resolution_plan"] = resolution_plan_template(compiled_plan)
        attacker_sheet["content"]["features"] = [source_feature]
        attacker_sheet = validate_character_sheet(attacker_sheet)
        source_feature = attacker_sheet["content"]["features"][0]
        source_feature["resolution_solution"] = build_content_solution(
            compiled_plan,
            source_card=source_feature,
            application_id="content:feature:peryton-dive-test",
            agent_ruling={
                "default_resolver": "agent",
                "ruling_kind": "agent_dm_adjudication",
                "decision": "Persist the exact Dive Attack extra-damage solution.",
                "reason": "The source card records the condition and damage dice.",
            },
        )
        attacker_sheet["content"]["activities"] = [
            {
                "id": "peryton-multiattack",
                "name": "Multiattack",
                "source_key": "Peryton",
                "activation": {"type": "action"},
                "choices": {
                    "multiattack_options": [
                        {
                            "id": "two-strikes",
                            "attacks": [
                                {
                                    "weapon_id": "gore",
                                    "attack_mode": "melee",
                                    "count": 2,
                                }
                            ],
                        }
                    ]
                },
            }
        ]
        attacker_sheet["inventory"]["items"] = [
            {
                "id": "gore",
                "name": "Gore",
                "kind": "weapon",
                "equipped": True,
                "equipped_slot": "main_hand",
                "mechanics": {
                    "attack_type": "melee",
                    "attack_ability": "strength",
                    "damage_formula": "1d6",
                    "damage_type": "piercing",
                },
            }
        ]
        attacker_sheet["inventory"]["equipment_slots"]["main_hand"] = "gore"
        target_sheet = default_character_sheet()
        target_sheet["combat"]["hp"] = {"value": 100, "max": 100, "temp": 0}
        attacker = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Peryton",
                "character_type": "monster",
                "sheet": attacker_sheet,
                "idempotency_key": "attacker",
            },
        )
        target = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Target",
                "sheet": target_sheet,
                "idempotency_key": "target",
            },
        )
        campaign = await _call(
            server, "campaign_get", {"campaign_id": campaign["id"]}
        )
        await _call_raw(
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
        ruling = {
            "source": "dm_ruling",
            "kind": "source_conditional_extra_damage",
            "application_id": "peryton:dive:round-1",
            "feature_id": "dive-attack-passive",
            "target_actor_ids": [target["id"]],
            "solution_plan_fingerprint": compiled_plan.fingerprint,
            "source_excerpt": source_excerpt,
            "damage_expression": "2d8",
            "damage_type": "weapon",
            "condition_satisfied": True,
            "trigger_facts": {
                "flying": True,
                "straight_dive_ft": 30,
                "requires_attack_advantage": True,
                "max_applications_per_turn": 1,
            },
            "default_resolver": "agent",
            "ruling_kind": "agent_dm_adjudication",
            "decision": "Apply Dive Attack to this qualifying hit.",
            "reason": "The peryton completed the printed 30-foot straight dive.",
        }
        action = {
            "weapon_id": "gore",
            "attack_mode": "melee",
            "multiattack_option_id": "two-strikes",
            "context": {"advantage": True},
            "rulings": [ruling],
        }
        with pytest.raises(Exception, match="requires this attack to have advantage"):
            await _call(
                server,
                "combat_preflight_attack",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": attacker["id"],
                    "target_id": target["id"],
                    "action": {
                        **action,
                        "context": {},
                    },
                },
            )
        with pytest.raises(Exception, match="does not apply to this target"):
            await _call(
                server,
                "combat_preflight_attack",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": attacker["id"],
                    "target_id": target["id"],
                    "action": {
                        **action,
                        "rulings": [
                            {
                                **ruling,
                                "target_actor_ids": [attacker["id"]],
                            }
                        ],
                    },
                },
            )
        with pytest.raises(Exception, match="qualifying ally conflicts"):
            await _call(
                server,
                "combat_preflight_attack",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": attacker["id"],
                    "target_id": target["id"],
                    "action": {
                        **action,
                        "rulings": [
                            {
                                **ruling,
                                "trigger_facts": {
                                    "applicability_mode": (
                                        "attack_advantage_or_target_adjacent_to_ally_"
                                        "without_disadvantage"
                                    ),
                                    "applicability_branch": "adjacent_ally",
                                    "requires_no_attack_disadvantage": True,
                                    "target_adjacent_to_nonincapacitated_ally": True,
                                    "qualifying_ally_actor_ids": [attacker["id"]],
                                },
                            }
                        ],
                    },
                },
            )
        plan = await _call(
            server,
            "combat_preflight_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": attacker["id"],
                "target_id": target["id"],
                "action": action,
            },
        )
        assert plan["additional_damage"] == [
            {
                "damage_expression": "2d8",
                "damage_type": "piercing",
                "source": "agent-ruling:peryton:dive:round-1",
            }
        ]
        assert [item["kind"] for item in plan["rulings"]].count(
            "source_conditional_extra_damage"
        ) == 1

        with pytest.raises(Exception, match="exact Agent-owned passive"):
            await _call(
                server,
                "combat_preflight_attack",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": attacker["id"],
                    "target_id": target["id"],
                    "action": {
                        **action,
                        "rulings": [
                            {
                                **ruling,
                                "source_excerpt": "The peryton deals 2d8.",
                            }
                        ],
                    },
                },
            )
        with pytest.raises(Exception, match="is incomplete"):
            await _call(
                server,
                "combat_preflight_attack",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": attacker["id"],
                    "target_id": target["id"],
                    "action": {
                        **action,
                        "rulings": [
                            ruling,
                            {
                                **ruling,
                                "application_id": "peryton:dive:duplicate",
                            },
                        ],
                    },
                },
            )

        await _call(
            server,
            "campaign_member_grant",
            {
                "campaign_id": campaign["id"],
                "principal_id": "player:test",
                "role": "player",
            },
        )
        await _call(
            server,
            "actor_grant",
            {
                "campaign_id": campaign["id"],
                "principal_id": "player:test",
                "actor_id": attacker["id"],
                "can_control": True,
                "can_view_private": True,
            },
        )
        with pytest.raises(Exception, match="Owner/DM authority"):
            await _call(
                server,
                "combat_preflight_attack",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": attacker["id"],
                    "target_id": target["id"],
                    "action": action,
                    "principal_id": "player:test",
                },
            )

        current = await _call(
            server, "campaign_get", {"campaign_id": campaign["id"]}
        )
        resolved = await _call_raw(
            server,
            "combat_resolve_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": attacker["id"],
                "target_id": target["id"],
                "action": action,
                "expected_revision": current["revision"],
                "idempotency_key": "attack",
            },
        )
        parts = resolved["result"]["damage"]["roll_parts"]
        assert len(parts) == 2
        assert parts[1]["expression"] == "2d8"
        assert parts[1]["rolled_expression"] == "4d8"
        assert parts[1]["source"] == "agent-ruling:peryton:dive:round-1"
        assert [item["entity_type"] for item in resolved["revisions"]] == [
            "campaign",
            "character",
        ]
        assert resolved["revisions"][1]["entity_id"] == target["id"]

        followup_action = {
            key: value for key, value in action.items() if key != "multiattack_option_id"
        }
        with pytest.raises(Exception, match="reached its per-turn limit"):
            await _call(
                server,
                "combat_preflight_attack",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": attacker["id"],
                    "target_id": target["id"],
                    "action": followup_action,
                },
            )
        followup = await _call_raw(
            server,
            "combat_resolve_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": attacker["id"],
                "target_id": target["id"],
                "action": {
                    key: value
                    for key, value in followup_action.items()
                    if key != "rulings"
                },
                "expected_revision": resolved["campaign_revision"],
                "idempotency_key": "attack-followup",
            },
        )
        assert len(followup["result"]["damage"]["roll_parts"]) == 1

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
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Dying PC",
                "sheet": sheet,
                "idempotency_key": "actor",
            },
        )
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
        started = await _call(
            server,
            "combat_start",
            {
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
            "combat_available_actions",
            {"campaign_id": campaign["id"], "actor_id": actor["id"]},
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
            "combat_available_actions",
            {"campaign_id": campaign["id"], "actor_id": actor["id"]},
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
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Checker",
                "idempotency_key": "actor",
            },
        )
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
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
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})

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
            }
        ]
        actor = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Bard",
                "sheet": sheet,
                "idempotency_key": "actor",
            },
        )
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
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
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
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

        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
        started = await _call(
            server,
            "combat_start",
            {
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
            "campaign_rule_receipts",
            {"campaign_id": campaign["id"]},
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
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Fighter",
                "sheet": sheet,
                "idempotency_key": "actor",
            },
        )
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
        started = await _call_raw(
            server,
            "combat_start",
            {
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
        actor_after = await _call(server, "character_get", {"character_id": actor["id"]})
        assert actor_after["sheet"]["content"]["features"][0]["uses"]["value"] == 0

    asyncio.run(exercise())


def test_battle_cry_uses_engine_settlement_and_persists_daily_use(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Battle Cry", "edition": "2014", "idempotency_key": "campaign"},
        )
        sheet = default_character_sheet()
        sheet["content"]["activities"] = [
            {
                "id": "dnd5e.core.monster.battle-cry",
                "name": "Battle Cry (1/Day)",
                "source_key": "monster-manual-2014:p246",
                "description": (
                    "Each creature of the war chief's choice that is within 30 feet "
                    "of it, can hear it, and is not already affected by Battle Cry "
                    "gains advantage on attack rolls until the start of the war "
                    "chief's next turn. The war chief can then make one attack as "
                    "a bonus action."
                ),
                "uses": {
                    "label": "Battle Cry (1/Day)",
                    "value": 1,
                    "max": 1,
                    "recovers_on": "long_rest",
                },
                "activation": {"type": "action", "cost": 1},
                "choices": {
                    "source_trait": {
                        "kind": "battle_cry",
                        "range_ft": 30,
                        "requires_hearing": True,
                    }
                },
            }
        ]
        war_chief = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "War Chief",
                "sheet": sheet,
                "idempotency_key": "war-chief",
            },
        )
        ally = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Orc Ally",
                "idempotency_key": "ally",
            },
        )
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
        started = await _call_raw(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [war_chief["id"], ally["id"]],
                "participant_config": [
                    {
                        "actor_id": war_chief["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                        "disposition": "hostile",
                    },
                    {
                        "actor_id": ally["id"],
                        "initiative": 10,
                        "position": {"x": 1, "y": 0},
                        "disposition": "hostile",
                    },
                ],
                "expected_revision": campaign["revision"],
                "idempotency_key": "start",
            },
        )
        cried = await _call_raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": war_chief["id"],
                "activity_id": "dnd5e.core.monster.battle-cry",
                "declaration": {
                    "targets": [
                        {
                            "actor_id": ally["id"],
                            "can_hear": True,
                            "reason": "Adjacent in the same open combat area.",
                        }
                    ]
                },
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "battle-cry",
            },
        )

        assert cried["status"] == "committed"
        assert cried["result"]["requires_ruling"] is False
        assert cried["result"]["core_effect"]["target_ids"] == [ally["id"]]
        assert any(
            item["mechanic_id"] == "dnd5e.core.activity.battle_cry"
            for item in cried["result"]["rule_receipts"]
        )
        actor_after = await _call(
            server,
            "character_get",
            {"character_id": war_chief["id"]},
        )
        assert actor_after["sheet"]["content"]["activities"][0]["uses"]["value"] == 0

    asyncio.run(exercise())


def test_descriptive_activity_requires_compilation_before_payment(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Agent ruling boundary",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        source_excerpt = (
            "The dragon exhales lightning at the defenders; use the authored "
            "mission procedure to determine casualties."
        )
        sheet = default_character_sheet()
        sheet["content"]["features"] = [
            {
                "id": "lightning-breath-action",
                "name": "Lightning Breath",
                "source_key": "module-review:dragon",
                "description": source_excerpt,
                "activation": {"type": "action", "cost": 1},
                "choices": {
                    "manual_ruling": {
                        "kind": "descriptive_activity",
                        "source_excerpt": source_excerpt,
                    }
                },
            }
        ]
        actor = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Dragon",
                "sheet": sheet,
                "idempotency_key": "actor",
            },
        )
        outside = await _call_raw(
            server,
            "character_use_activity",
            {
                "character_id": actor["id"],
                "activity_id": "lightning-breath-action",
                "expected_revision": actor["revision"],
                "idempotency_key": "outside-breath",
            },
        )
        assert outside["result"]["payment_required"] is False
        assert outside["result"]["semantic_solution"][
            "source_card_kind"
        ] == "feature"
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
        started = await _call_raw(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [actor["id"]],
                "participant_config": [{"actor_id": actor["id"], "initiative": 10}],
                "expected_revision": campaign["revision"],
                "idempotency_key": "start",
            },
        )

        ruled = await _call_raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "activity_id": "lightning-breath-action",
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "breath",
            },
        )

        assert ruled["status"] == "pending_ruling"
        assert ruled["result"]["payment_required"] is False
        assert ruled["result"]["semantic_solution"] == {
            "status": "compilation_required",
            "source_card_id": "lightning-breath-action",
            "source_card_kind": "feature",
            "required_action": "content_solution(compile)",
            "character_revision": actor["revision"],
        }
        current = started["combat"]["combatants"][
            started["combat"]["turn_index"]
        ]
        assert current["turn_budget"]["main_action"] == 1
        actor_after = await _call(server, "character_get", {"character_id": actor["id"]})
        assert actor_after["revision"] == actor["revision"]

    asyncio.run(exercise())


def test_custom_spell_requires_compilation_before_action_or_slot_payment(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Custom spell compilation",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        effect = (
            "A ribbon of moonlight circles one creature and leaves a silver "
            "brand until the caster's next turn."
        )
        sheet = default_character_sheet()
        sheet["spellcasting"]["spell_slots"] = {
            "1": {
                "label": "Level 1",
                "value": 1,
                "max": 1,
                "recovers_on": "long_rest",
                "source_key": "custom",
            }
        }
        sheet["content"]["spells"] = [
            {
                "id": "moon-ribbon",
                "name": "Moon Ribbon",
                "level": 1,
                "grant": {
                    "source_type": "module",
                    "source_key": "moon-vault",
                    "method": "known",
                },
                "access": {
                    "known": True,
                    "prepared": True,
                    "always_prepared": False,
                    "ritual_available": False,
                    "at_will": False,
                    "at_will_sources": [],
                },
                "definition": {
                    "casting_time": "1 action",
                    "duration": {
                        "kind": "timed",
                        "value": 1,
                        "unit": "round",
                        "concentration": False,
                    },
                    "effect": effect,
                },
                # A registered accounting mechanic is not an implementation
                # of this card's authored outcome.
                "mechanic_refs": [
                    "dnd5e.core.activity.resource_accounting"
                ],
            }
        ]
        standard_gap = deepcopy(sheet["content"]["spells"][0])
        standard_gap.update(
            {
                "id": "locked-standard-gap",
                "name": "Locked Standard Gap",
                "pack_id": "dnd5e.content.srd2014",
                "pack_version": "1.44.0",
            }
        )
        sheet["content"]["spells"].append(standard_gap)
        caster = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Moon Mage",
                "sheet": sheet,
                "idempotency_key": "caster",
            },
        )
        standard_pending = await _call_raw(
            server,
            "character_cast_spell",
            {
                "character_id": caster["id"],
                "spell_id": "locked-standard-gap",
                "cast_level": 1,
                "expected_revision": caster["revision"],
                "idempotency_key": "standard-gap",
            },
        )
        assert standard_pending["result"]["semantic_solution"] == {
            "status": "engine_implementation_required",
            "source_card_id": "locked-standard-gap",
            "source_card_kind": "spell",
            "required_action": "implement_standard_mechanic",
            "character_revision": caster["revision"],
        }
        outside = await _call_raw(
            server,
            "character_cast_spell",
            {
                "character_id": caster["id"],
                "spell_id": "moon-ribbon",
                "cast_level": 1,
                "expected_revision": caster["revision"],
                "idempotency_key": "outside-cast",
            },
        )
        assert outside["result"]["payment_required"] is False
        assert outside["result"]["semantic_solution"][
            "source_card_kind"
        ] == "spell"
        campaign = await _call(
            server,
            "campaign_get",
            {"campaign_id": campaign["id"]},
        )
        started = await _call_raw(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [caster["id"]],
                "participant_config": [
                    {"actor_id": caster["id"], "initiative": 10}
                ],
                "expected_revision": campaign["revision"],
                "idempotency_key": "start",
            },
        )

        pending = await _call_raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign["id"],
                "actor_id": caster["id"],
                "spell_id": "moon-ribbon",
                "cast_level": 1,
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "cast",
            },
        )

        assert pending["status"] == "pending_ruling"
        assert pending["result"]["payment_required"] is False
        assert pending["result"]["semantic_solution"] == {
            "status": "compilation_required",
            "source_card_id": "moon-ribbon",
            "source_card_kind": "spell",
            "required_action": "content_solution(compile)",
            "character_revision": caster["revision"],
        }
        current = started["combat"]["combatants"][
            started["combat"]["turn_index"]
        ]
        assert current["turn_budget"]["main_action"] == 1
        caster_after = await _call(
            server,
            "character_get",
            {"character_id": caster["id"]},
        )
        assert caster_after["revision"] == caster["revision"]
        assert (
            caster_after["sheet"]["spellcasting"]["spell_slots"]["1"]["value"]
            == 1
        )

    asyncio.run(exercise())


def test_second_wind_heals_and_pays_bonus_action_atomically(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Second Wind", "edition": "2014", "idempotency_key": "campaign"},
        )
        sheet = default_character_sheet()
        sheet["progression"]["level"] = 2
        sheet["progression"]["classes"] = [
            {"name": "Fighter", "level": 2, "subclass": "", "hit_die": 10}
        ]
        sheet["combat"]["hp"] = {"value": 1, "max": 20, "temp": 0}
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
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Fighter",
                "sheet": sheet,
                "idempotency_key": "actor",
            },
        )
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
        started = await _call_raw(
            server,
            "combat_start",
            {
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
                "activity_id": "dnd5e.content.srd2014.feature.fighter-second-wind",
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
        actor_after = await _call(server, "character_get", {"character_id": actor["id"]})
        assert actor_after["sheet"]["combat"]["hp"]["value"] == effect["after_hp"]
        assert actor_after["sheet"]["content"]["features"][0]["uses"]["value"] == 0
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
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Fighter",
                "sheet": sheet,
                "idempotency_key": "actor",
            },
        )
        arguments = {
            "character_id": actor["id"],
            "activity_id": "dnd5e.content.srd2014.feature.fighter-second-wind",
            "expected_revision": actor["revision"],
            "idempotency_key": "second-wind",
        }

        before = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
        stream = server_module.CampaignRandomStream.from_campaign_state(
            campaign["id"],
            before["state"],
            operation="character_action",
            idempotency_key="second-wind",
        )
        with server_module.use_random_stream(stream):
            result = await _call_raw(server, "character_use_activity", arguments)
        assert stream.has_unpersisted_draws is False
        assert await _call_raw(server, "character_use_activity", arguments) == result
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
        current = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
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
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Rogue",
                "sheet": sheet,
                "idempotency_key": "actor",
            },
        )
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
        started = await _call_raw(
            server,
            "combat_start",
            {
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
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Mover",
                "idempotency_key": "mover",
            },
        )
        other = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Other",
                "idempotency_key": "other",
            },
        )
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
        started = await _call_raw(
            server,
            "combat_start",
            {
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
            "combat_move",
            {
                "campaign_id": campaign["id"],
                "actor_id": mover["id"],
                "distance": 10,
                "destination": {"x": 2, "y": 0},
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
            "combat_move",
            {
                "campaign_id": campaign["id"],
                "actor_id": mover["id"],
                "distance": 10,
                "destination": {"x": 2, "y": 0},
                "path": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 2, "y": 0}],
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "move",
            },
        )

        current = moved["combat"]["combatants"][moved["combat"]["turn_index"]]
        assert current["turn_budget"]["movement"] == 15
        receipts = await _call(
            server,
            "campaign_rule_receipts",
            {"campaign_id": campaign["id"]},
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
