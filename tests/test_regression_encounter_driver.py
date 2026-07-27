import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.regression_encounter import (
    GUIDING_BOLT_ID,
    GUIDING_BOLT_ON_HIT,
    HEALING_WORD_ID,
    MAGIC_MISSILE_ID,
    EncounterRulingRequiredError,
    _agent_party_absences,
    _agent_positions,
    _agent_target_priorities,
    _apply_agent_positions,
    _apply_party_loadouts,
    _apply_source_casualty_rolls,
    _apply_source_separations,
    _body_thief_sides,
    _body_thief_target_ids,
    _characters,
    _choose_destination,
    _choose_party_spell,
    _defense_selection,
    _encounter_actor_groups,
    _encounter_operation_scope,
    _encounter_start_operation_token,
    _has_action_budget,
    _has_blocking_pending,
    _has_multiattack_followup,
    _observable_target_ids,
    _operation_token,
    _participant_config,
    _participant_manifest,
    _party_ids,
    _party_loadouts,
    _postcombat_stabilization_target,
    _preferred_hostile_weapon_id,
    _preferred_multiattack_option_id,
    _preflight_attack,
    _prepared_actor_ids,
    _primary_hostile_source_excerpt,
    _prioritize_targets,
    _record_source_flee_damage,
    _reinforcement_config,
    _require_live_active_party,
    _require_pending_on_hit_choice_id,
    _resolve_pending,
    _roll_total,
    _selected_prepared_actor_ids,
    _settle_source_casualty_pool_turn,
    _should_stand,
    _source_attack_environments,
    _source_avoidances,
    _source_casualty_pools,
    _source_contest_activities,
    _source_declared_conditions,
    _source_declared_surprise,
    _source_delayed_actions,
    _source_departure_patch,
    _source_flee_damage_history,
    _source_flee_ready,
    _source_on_hit_rulings,
    _source_opening_casts,
    _source_opening_weapons,
    _source_outcome,
    _source_passive_allies,
    _source_precombat_casts,
    _source_random_activities,
    _source_save_activities,
    _source_separation_target,
    _source_separations,
    _source_surrender_outcome,
    _source_target_priorities,
    _source_traits,
    _source_truce_outcome,
    _source_zero_hp_finisher,
    _source_zero_hp_finisher_stage,
    _source_zero_hp_stabilization,
    _start_or_resume_auto_run,
    _status,
    _surprise_from_check_report,
    _surprise_from_hostile_stealth_totals,
    _surprise_from_party_stealth_reports,
    _validate_hostile_attacks,
    _validate_source_flee_configuration,
    _wound_priority,
)


def test_encounter_operation_scope_separates_consecutive_encounters() -> None:
    start_args = SimpleNamespace(
        action="start",
        campaign_id="campaign-1",
        checkpoint_label="First group defeated",
        encounter_name="Raider group",
        home="home",
        location_key="keep-route",
        no_surprise=True,
        operation_scope="",
        output="start.json",
        run_id="campaign-run",
        scene_id="scene-1",
        source_excerpt="A group consists of 1d6 kobolds and 1d4 cultists.",
    )
    auto_args = SimpleNamespace(
        **{
            **vars(start_args),
            "action": "auto-run",
            "output": "complete.json",
        }
    )

    first = _encounter_operation_scope(
        start_args,
        branch_id="branch-1",
        party_ids=["pc-1", "pc-2"],
        hostile_ids=["kobold-1", "cultist-1"],
    )
    retried = _encounter_operation_scope(
        auto_args,
        branch_id="branch-1",
        party_ids=["pc-1", "pc-2"],
        hostile_ids=["kobold-1", "cultist-1"],
    )
    second_group = _encounter_operation_scope(
        start_args,
        branch_id="branch-1",
        party_ids=["pc-1", "pc-2"],
        hostile_ids=["kobold-2", "cultist-2"],
    )
    isolated_branch = _encounter_operation_scope(
        start_args,
        branch_id="branch-2",
        party_ids=["pc-1", "pc-2"],
        hostile_ids=["kobold-1", "cultist-1"],
    )

    assert first == retried
    assert first != second_group
    assert first != isolated_branch
    start_args.operation_scope = first
    auto_args.operation_scope = second_group
    assert _operation_token(start_args, 1, "attack") != _operation_token(
        auto_args,
        1,
        "attack",
    )


def test_encounter_start_token_binds_the_complete_public_request() -> None:
    request = {
        "campaign_id": "campaign-1",
        "participant_ids": ["pc-1", "kobold-1"],
        "participant_config": [
            {"actor_id": "pc-1", "position": {"x": 1, "y": 1}},
            {"actor_id": "kobold-1", "position": {"x": 8, "y": 1}},
        ],
        "participant_manifest": {"groups": [{"actor_ids": ["kobold-1"]}]},
        "name": "First group",
        "scene_id": "scene-1",
        "battle_map": {"location_key": "keep-route"},
        "ruleset": "2014",
        "branch_id": "branch-1",
        "expected_revision": 12,
    }

    token = _encounter_start_operation_token(request)

    assert token == _encounter_start_operation_token(dict(reversed(list(request.items()))))
    assert token != _encounter_start_operation_token(
        {**request, "participant_ids": ["pc-1", "kobold-2"]}
    )
    assert token != _encounter_start_operation_token({**request, "expected_revision": 13})


def test_preflight_capture_uses_only_melee_and_declares_knockout() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def domain(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "combat_preflight_attack"
            self.calls.append(arguments)
            return {"status": "ready", **arguments["action"]}

    client = Client()
    actor = {
        "id": "pc-1",
        "derived": {
            "inventory": {
                "weapon_attacks": [
                    {"item_id": "shortbow", "attack_type": "ranged"},
                    {"item_id": "shortsword", "attack_type": "melee"},
                ]
            }
        },
    }

    target_id, action, plan = asyncio.run(
        _preflight_attack(
            client,
            SimpleNamespace(campaign_id="campaign-1"),
            actor,
            ["cultist-1"],
            knock_out_target_ids={"cultist-1"},
        )
    )

    assert target_id == "cultist-1"
    assert action == {
        "weapon_id": "shortsword",
        "attack_mode": "melee",
        "knock_out": True,
    }
    assert plan["knock_out"] is True
    assert [call["action"] for call in client.calls] == [action]


def test_preflight_tries_a_recorded_thrown_weapon_at_range() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def domain(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "combat_preflight_attack"
            self.calls.append(arguments)
            if arguments["action"]["attack_mode"] == "melee":
                raise RuntimeError("target is beyond melee reach")
            return {"status": "ready", **arguments["action"]}

    client = Client()
    actor = {
        "id": "pc-1",
        "derived": {
            "inventory": {
                "weapon_attacks": [
                    {
                        "item_id": "dagger",
                        "attack_type": "melee",
                        "properties": ["finesse", "light", "thrown"],
                        # PC inventory-derived weapon cards expose their authored
                        # thrown distance through the canonical range field.
                        "range_ft": {"normal": 20, "long": 60},
                    }
                ]
            }
        },
    }

    target_id, action, _ = asyncio.run(
        _preflight_attack(
            client,
            SimpleNamespace(campaign_id="campaign-1"),
            actor,
            ["dragon-1"],
        )
    )

    assert target_id == "dragon-1"
    assert action == {
        "weapon_id": "dagger",
        "attack_mode": "ranged",
    }
    assert [call["action"]["attack_mode"] for call in client.calls] == [
        "melee",
        "ranged",
    ]


def test_preflight_returns_agent_ruling_instead_of_misclassifying_it_as_on_hit() -> None:
    pending = {
        "status": "pending_ruling",
        "default_resolver": "agent",
        "ruling_kind": "agent_dm_adjudication",
        "reason": "direct sunlight is required to settle Sunlight Sensitivity",
        "missing": ["direct_sunlight"],
        "committed": False,
        "retry_contract": {"reuse_current_revision": True},
    }

    class Client:
        async def domain(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "combat_preflight_attack"
            return pending

    actor = {
        "id": "kobold-1",
        "derived": {
            "inventory": {
                "weapon_attacks": [
                    {"item_id": "dagger", "attack_type": "melee"},
                ]
            }
        },
    }

    with pytest.raises(EncounterRulingRequiredError) as captured:
        asyncio.run(
            _preflight_attack(
                Client(),
                SimpleNamespace(campaign_id="campaign-1"),
                actor,
                ["pc-1"],
            )
        )

    requirement = captured.value.requirement
    assert requirement["operation"] == "combat_preflight_attack"
    assert requirement["actor_id"] == "kobold-1"
    assert requirement["target_id"] == "pc-1"
    assert requirement["ruling"] == pending
    assert "--source-attack-environment-json" in requirement["retry_hint"]


def test_preflight_agent_declines_unsatisfied_positional_attack_and_continues() -> None:
    calls: list[dict] = []

    class Client:
        async def domain(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "combat_preflight_attack"
            calls.append(arguments)
            if arguments["action"]["weapon_id"] == "dagger":
                raise RuntimeError("target is beyond melee reach")
            return {
                "status": "pending_ruling",
                "default_resolver": "agent",
                "ruling_kind": "agent_dm_adjudication",
                "reason": "Dropped Rock uses a source-defined positional target restriction",
                "missing": ["weapon.targeting:dropped-rock"],
                "committed": False,
            }

    actor = {
        "id": "winged-kobold",
        "derived": {
            "inventory": {
                "weapon_attacks": [
                    {"item_id": "dagger", "attack_type": "melee"},
                    {"item_id": "dropped-rock", "attack_type": "ranged"},
                ]
            }
        },
    }
    rulings: list[dict] = []

    result = asyncio.run(
        _preflight_attack(
            Client(),
            SimpleNamespace(campaign_id="campaign-1"),
            actor,
            ["pc-1"],
            preferred_weapon_id="dagger",
            agent_rulings=rulings,
        )
    )

    assert result is None
    assert [call["action"]["weapon_id"] for call in calls] == [
        "dagger",
        "dropped-rock",
    ]
    assert rulings == [
        {
            "operation": "combat_preflight_attack",
            "actor_id": "winged-kobold",
            "target_id": "pc-1",
            "action": {
                "weapon_id": "dropped-rock",
                "attack_mode": "ranged",
            },
            "decision": "decline_optional_attack",
            "reason": (
                "The current two-dimensional temporary map has no vertical-position "
                "fact satisfying the source-defined target restriction."
            ),
            "ruling": {
                "status": "pending_ruling",
                "default_resolver": "agent",
                "ruling_kind": "agent_dm_adjudication",
                "reason": (
                    "Dropped Rock uses a source-defined positional target restriction"
                ),
                "missing": ["weapon.targeting:dropped-rock"],
                "committed": False,
            },
        }
    ]


def test_unclassified_encounter_ruling_defaults_to_agent_reasoning() -> None:
    error = EncounterRulingRequiredError(
        {"reason": "the module has a one-off narrative procedure"},
        operation="module_special_procedure",
    )

    ruling = error.requirement["ruling"]
    assert ruling["status"] == "pending_ruling"
    assert ruling["default_resolver"] == "agent"
    assert ruling["ruling_kind"] == "agent_dm_adjudication"


def test_precommit_attack_ruling_is_not_treated_as_owned_on_hit_window() -> None:
    pending = {
        "status": "pending_ruling",
        "default_resolver": "agent",
        "ruling_kind": "environmental_consequence",
        "reason": "direct sunlight must be established from the scene",
        "committed": False,
        "result": {},
    }

    with pytest.raises(EncounterRulingRequiredError) as raised:
        _require_pending_on_hit_choice_id(
            pending,
            operation="combat_resolve_attack.guiding_bolt",
            actor_id="cleric",
            target_id="kobold",
            action={"spell_id": GUIDING_BOLT_ID},
            retry_hint="Resolve the environmental fact and retry.",
        )

    assert raised.value.requirement["ruling"]["ruling_kind"] == (
        "environmental_consequence"
    )


def test_status_uses_play_character_exposure_before_combat() -> None:
    class Client:
        def __init__(self) -> None:
            self.loaded: list[tuple[str, ...]] = []
            self.calls: list[tuple[str, dict]] = []

        async def open(self, campaign_id: str) -> dict:
            assert campaign_id == "campaign-1"
            return {"phase": "play"}

        async def load(self, *group_ids: str) -> None:
            self.loaded.append(group_ids)

        async def core(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "campaign_query"
            assert arguments["payload"]["campaign_id"] == "campaign-1"
            return {"id": "campaign-1", "state": {}}

        async def domain(self, tool_id: str, arguments: dict) -> list[dict]:
            self.calls.append((tool_id, arguments))
            assert tool_id == "character_query"
            return [
                {
                    "id": actor_id,
                    "name": actor_id,
                    "character_type": "pc",
                    "sheet": {
                        "combat": {"hp": {"value": 8, "max": 8}},
                        "conditions": [],
                        "resources": {
                            "test": {
                                "value": 1,
                                "max": 1,
                                "recovers_on": "short_rest",
                            }
                        },
                        "spellcasting": {
                            "spell_slots": {
                                "1": {
                                    "value": 2,
                                    "max": 2,
                                    "recovers_on": "long_rest",
                                }
                            }
                        },
                    },
                    "derived": {
                        "armor_class": 12,
                        "spellcasting": {"prepared_spell_ids": ["spell-1"]},
                    },
                }
                for actor_id in arguments["payload"]["character_ids"]
            ]

    client = Client()
    result = asyncio.run(_status(client, campaign_id="campaign-1", actor_ids=["pc-1", "pc-2"]))

    assert result["phase"] == "play"
    assert result["combat"] is None
    assert client.loaded == [("play.characters",)]
    assert [actor["id"] for actor in result["actors"]] == ["pc-1", "pc-2"]
    assert result["actors"][0]["resources"]["test"]["value"] == 1
    assert result["actors"][0]["spell_slots"]["1"]["value"] == 2
    assert result["actors"][0]["prepared_spell_ids"] == ["spell-1"]


def test_party_loadout_equips_owned_weapon_before_initiative() -> None:
    actor = {
        "id": "pc-1",
        "revision": 4,
        "sheet": {
            "inventory": {
                "items": [
                    {
                        "id": "shortsword",
                        "kind": "weapon",
                        "equipped": True,
                        "equipped_slot": "main_hand",
                    },
                    {
                        "id": "shortbow",
                        "kind": "weapon",
                        "equipped": False,
                        "equipped_slot": None,
                    },
                ]
            }
        },
    }

    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def domain(self, tool_id: str, arguments: dict) -> list[dict] | dict:
            self.calls.append((tool_id, arguments))
            if tool_id == "inventory_change":
                actor["revision"] += 1
                actor["sheet"]["inventory"]["items"][0].update(
                    {"equipped": False, "equipped_slot": None}
                )
                actor["sheet"]["inventory"]["items"][1].update(
                    {"equipped": True, "equipped_slot": "main_hand"}
                )
                return {"status": "committed"}
            assert tool_id == "character_query"
            return [actor]

    declarations = [
        {"actor_id": "pc-1", "item_id": "shortbow", "slot": "main_hand"}
    ]
    assert _party_loadouts(
        declarations,
        party_ids=["pc-1"],
        actors={"pc-1": actor},
    ) == declarations

    client = Client()
    results, actors = asyncio.run(
        _apply_party_loadouts(
            client,
            SimpleNamespace(
                campaign_id="campaign-1",
                party_loadout_json=declarations,
                action="start",
                checkpoint_label="",
                home="home",
                output="output.json",
                run_id="run-1",
            ),
            party_ids=["pc-1"],
            actors={"pc-1": actor},
        )
    )

    assert results[0]["status"] == "equipped"
    assert actors["pc-1"]["revision"] == 5
    assert [tool_id for tool_id, _ in client.calls] == [
        "inventory_change",
        "character_query",
    ]
    assert client.calls[0][1]["payload"] == {
        "item_id": "shortbow",
        "slot": "main_hand",
    }


def test_party_ids_combine_public_party_reports_and_require_global_uniqueness(
    tmp_path,
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps({"characters": [{"actor_id": "pc-1"}]}), encoding="utf-8")
    second.write_text(json.dumps({"characters": [{"actor_id": "pc-2"}]}), encoding="utf-8")

    assert _party_ids([first, second]) == ["pc-1", "pc-2"]

    second.write_text(json.dumps({"characters": [{"actor_id": "pc-1"}]}), encoding="utf-8")
    try:
        _party_ids([first, second])
    except ValueError as exc:
        assert "unique character actor_id" in str(exc)
    else:
        raise AssertionError("duplicate actor ids must be rejected")


def test_party_ids_accept_playthrough_status_and_exclude_inactive_members(tmp_path) -> None:
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps(
            {
                "result": {
                    "manifest": {
                        "party": {
                            "members": [
                                {"actor_id": "pc-active", "status": "active"},
                                {"actor_id": "pc-dead", "status": "dead"},
                                {"actor_id": "pc-left", "status": "departed"},
                            ]
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert _party_ids([status]) == ["pc-active"]


def test_prepared_actor_reports_support_batched_rule_actors_and_module_actors(
    tmp_path,
) -> None:
    rule_report = tmp_path / "rule.json"
    module_report = tmp_path / "module.json"
    rule_report.write_text(
        json.dumps({"actors": [{"id": "stirge-1"}, {"id": "stirge-2"}]}),
        encoding="utf-8",
    )
    module_report.write_text(
        json.dumps({"created": {"character": {"id": "durnan"}}}),
        encoding="utf-8",
    )

    assert _prepared_actor_ids(
        [rule_report, module_report],
        report_kind="encounter",
    ) == ["stirge-1", "stirge-2", "durnan"]


def test_agent_party_absence_selects_encounter_participants_without_relabeling_party(
    tmp_path,
) -> None:
    report = tmp_path / "party.json"
    report.write_text(
        json.dumps(
            {
                "characters": [
                    {"actor_id": "cleric"},
                    {"actor_id": "bard"},
                    {"actor_id": "rogue"},
                    {"actor_id": "wizard"},
                ]
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        party_report=[report],
        agent_party_absence_json=[
            {
                "actor_id": "cleric",
                "ruling_reason": (
                    "The stable unconscious cleric remains at the keep and cannot "
                    "participate in the mill mission."
                ),
            }
        ],
        ally_report=[],
        ally_actor_id=[],
        hostile_report=[],
        hostile_actor_id=[],
        additional_hostile_report=[],
        additional_hostile_actor_id=[],
        reinforcement_hostile_report=[],
        reinforcement_hostile_actor_id=[],
        required_hostile_count=None,
        hostile_count_basis="",
    )

    groups = _encounter_actor_groups(args)

    assert groups["party_ids"] == ["bard", "rogue", "wizard"]
    assert groups["agent_party_absences"] == args.agent_party_absence_json
    manifest_result = {
        "manifest": {
            "party": {
                "members": [
                    {"actor_id": "cleric", "status": "active"},
                    {"actor_id": "bard", "status": "active"},
                    {"actor_id": "rogue", "status": "active"},
                    {"actor_id": "wizard", "status": "active"},
                ]
            }
        }
    }
    assert _require_live_active_party(
        groups["party_ids"],
        manifest_result,
        agent_party_absences=groups["agent_party_absences"],
    ) == ["cleric", "bard", "rogue", "wizard"]

    with pytest.raises(ValueError, match="active party reports"):
        _agent_party_absences(
            [{"actor_id": "guard", "ruling_reason": "The guard stays outside."}],
            reported_party_ids=["cleric", "bard"],
        )
    with pytest.raises(ValueError, match="at least one participating"):
        _agent_party_absences(
            [
                {"actor_id": "cleric", "ruling_reason": "The cleric stays outside."},
                {"actor_id": "bard", "ruling_reason": "The bard stays outside."},
            ],
            reported_party_ids=["cleric", "bard"],
        )


def test_prepared_actor_reports_support_exact_source_group_selection(tmp_path) -> None:
    report = tmp_path / "kenku.json"
    report.write_text(
        json.dumps(
            {
                "actors": [
                    {"id": "kenku-1"},
                    {"id": "kenku-2"},
                    {"id": "kenku-3"},
                    {"id": "kenku-4"},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert _selected_prepared_actor_ids(
        [report],
        ["kenku-3", "kenku-1"],
        report_kind="hostile",
    ) == ["kenku-3", "kenku-1"]
    assert _selected_prepared_actor_ids(
        [report],
        [],
        report_kind="hostile",
    ) == ["kenku-1", "kenku-2", "kenku-3", "kenku-4"]

    with pytest.raises(ValueError, match="absent from prepared reports.*kenku-5"):
        _selected_prepared_actor_ids(
            [report],
            ["kenku-5"],
            report_kind="hostile",
        )
    with pytest.raises(ValueError, match="non-empty and unique"):
        _selected_prepared_actor_ids(
            [report],
            ["kenku-1", "kenku-1"],
            report_kind="hostile",
        )


def test_source_passive_allies_require_unique_allies_and_exact_evidence() -> None:
    passive = _source_passive_allies(
        [
            {
                "actor_id": "losser",
                "source_excerpt": "The characters find Losser cowering in one corner.",
            }
        ],
        ally_ids=["losser", "skeleton-1"],
    )

    assert passive == {
        "losser": {
            "actor_id": "losser",
            "source_excerpt": ("The characters find Losser cowering in one corner."),
        }
    }
    with pytest.raises(ValueError, match="requires one unique allied actor"):
        _source_passive_allies(
            [{"actor_id": "kenku", "source_excerpt": "cowering"}],
            ally_ids=["losser"],
        )
    with pytest.raises(ValueError, match="unsupported fields"):
        _source_passive_allies(
            [
                {
                    "actor_id": "losser",
                    "source_excerpt": "cowering",
                    "until_round": 99,
                }
            ],
            ally_ids=["losser"],
        )


def test_source_random_activity_requires_exact_actor_card_evidence() -> None:
    values = [
        {
            "actor_id": "gazer",
            "activity_id": "eye-rays-action",
            "source_excerpt": ("shoots two of the following magical eye rays at random"),
        }
    ]
    actors = {
        "gazer": {
            "sheet": {
                "content": {
                    "activities": [
                        {
                            "id": "eye-rays-action",
                            "description": (
                                "The gazer shoots two of the following magical eye "
                                "rays at random (reroll duplicates)."
                            ),
                        }
                    ]
                }
            }
        }
    }

    assert _source_random_activities(
        values,
        participant_ids=["gazer", "pc"],
        actors=actors,
    ) == {
        "gazer": {
            "actor_id": "gazer",
            "activity_id": "eye-rays-action",
            "source_excerpt": ("shoots two of the following magical eye rays at random"),
        }
    }
    with pytest.raises(ValueError, match="contain the exact excerpt"):
        _source_random_activities(
            [
                {
                    **values[0],
                    "source_excerpt": "invented automatic rays",
                }
            ],
            participant_ids=["gazer", "pc"],
            actors=actors,
        )
    with pytest.raises(ValueError, match="unique participant"):
        _source_random_activities(
            [{**values[0], "actor_id": "not-present"}],
            participant_ids=["gazer", "pc"],
        )


def test_source_save_activity_requires_structured_card_and_brain_ruling() -> None:
    excerpt = "The target must succeed on a DC 12 Intelligence saving throw against this magic"
    values = [
        {
            "actor_id": "devourer",
            "activity_id": "devour-intellect-action",
            "target_has_brain": True,
            "source_excerpt": excerpt,
        }
    ]
    actors = {
        "devourer": {
            "sheet": {
                "content": {
                    "activities": [
                        {
                            "id": "devour-intellect-action",
                            "description": excerpt,
                            "choices": {"source_save_effect": {"target_requirement": "has_brain"}},
                        }
                    ]
                }
            }
        }
    }

    assert _source_save_activities(
        values,
        participant_ids=["devourer", "pc"],
        actors=actors,
    ) == {"devourer": values[0]}
    with pytest.raises(ValueError, match="true target_has_brain"):
        _source_save_activities(
            [{**values[0], "target_has_brain": False}],
            participant_ids=["devourer", "pc"],
            actors=actors,
        )
    with pytest.raises(ValueError, match="structured actor card"):
        _source_save_activities(
            values,
            participant_ids=["devourer", "pc"],
            actors={
                "devourer": {
                    "sheet": {
                        "content": {
                            "activities": [
                                {
                                    "id": "devour-intellect-action",
                                    "description": excerpt,
                                    "choices": {},
                                }
                            ]
                        }
                    }
                }
            },
        )


def test_source_contest_activity_requires_body_thief_card_and_humanoid_ruling() -> None:
    excerpt = (
        "The intellect devourer initiates an Intelligence contest with an "
        "incapacitated humanoid within 5 feet of it."
    )
    values = [
        {
            "actor_id": "devourer",
            "activity_id": "body-thief-action",
            "target_is_humanoid": True,
            "source_excerpt": excerpt,
        }
    ]
    actors = {
        "devourer": {
            "sheet": {
                "content": {
                    "activities": [
                        {
                            "id": "body-thief-action",
                            "description": excerpt,
                            "choices": {
                                "source_contest_effect": {
                                    "kind": ("intellect_devourer_body_thief_2014"),
                                    "target_requirements": [
                                        "incapacitated",
                                        "humanoid",
                                    ],
                                }
                            },
                        }
                    ]
                }
            }
        }
    }

    assert _source_contest_activities(
        values,
        participant_ids=["devourer", "pc"],
        actors=actors,
    ) == {"devourer": values[0]}
    with pytest.raises(ValueError, match="true target_is_humanoid"):
        _source_contest_activities(
            [{**values[0], "target_is_humanoid": False}],
            participant_ids=["devourer", "pc"],
            actors=actors,
        )
    with pytest.raises(ValueError, match="structured actor card"):
        _source_contest_activities(
            values,
            participant_ids=["devourer", "pc"],
            actors={
                "devourer": {
                    "sheet": {
                        "content": {
                            "activities": [
                                {
                                    "id": "body-thief-action",
                                    "description": excerpt,
                                    "choices": {},
                                }
                            ]
                        }
                    }
                }
            },
        )


def test_body_thief_sides_substitutes_host_without_erasing_actors() -> None:
    combat = {
        "combatants": [
            {
                "actor_id": "devourer",
                "inside_host": {
                    "host_actor_id": "pc-1",
                    "total_cover": True,
                },
            },
            {
                "actor_id": "pc-1",
                "controlled_by_actor_id": "devourer",
                "body_thief_host": {"source_actor_id": "devourer"},
            },
            {"actor_id": "pc-2"},
            {"actor_id": "kobold"},
        ]
    }

    sides = _body_thief_sides(
        combat,
        party_ids=["pc-1", "pc-2"],
        hostile_ids=["devourer", "kobold"],
    )

    assert sides["controlled_hosts"] == {"pc-1": "devourer"}
    assert sides["inside_sources"] == {"devourer"}
    assert sides["effective_party_ids"] == ["pc-2"]
    assert sides["attackable_hostile_ids"] == ["kobold", "pc-1"]
    assert sides["hostile_turn_actor_ids"] == {"kobold", "pc-1"}


def test_body_thief_targets_living_zero_hp_incapacitated_creature() -> None:
    combat = {
        "combatants": [
            {"actor_id": "devourer", "position": {"x": 3, "y": 2}},
            {"actor_id": "downed", "position": {"x": 2, "y": 2}},
            {"actor_id": "dead", "position": {"x": 3, "y": 3}},
            {"actor_id": "far", "position": {"x": 5, "y": 2}},
        ]
    }
    actors = {
        "downed": {
            "sheet": {
                "combat": {"hp": {"value": 0, "max": 10}},
                "conditions": ["unconscious", "stable"],
            }
        },
        "dead": {
            "sheet": {
                "combat": {"hp": {"value": 0, "max": 10}},
                "conditions": ["dead", "unconscious"],
            }
        },
        "far": {
            "sheet": {
                "combat": {"hp": {"value": 5, "max": 10}},
                "conditions": ["stunned"],
            }
        },
    }

    assert _body_thief_target_ids(
        combat,
        actors=actors,
        source_actor_id="devourer",
        party_ids=["downed", "dead", "far"],
        range_ft=5,
    ) == ["downed"]


def test_body_thief_requires_unspent_action_budget() -> None:
    combat = {
        "combatants": [
            {
                "actor_id": "devourer",
                "turn_budget": {
                    "main_action": 0,
                    "extra_action": 0,
                    "attack_budget": 1,
                },
            }
        ]
    }

    assert not _has_action_budget(combat, "devourer")
    combat["combatants"][0]["turn_budget"]["extra_action"] = 1
    assert _has_action_budget(combat, "devourer")


def test_action_budget_preserves_bonus_action_spell_followup() -> None:
    healing_word_combat = {
        "combatants": [
            {
                "actor_id": "bard",
                "turn_budget": {
                    "main_action": 1,
                    "bonus_action": 0,
                    "extra_action": 0,
                },
            }
        ]
    }
    main_action_spell_combat = {
        "combatants": [
            {
                "actor_id": "wizard",
                "turn_budget": {
                    "main_action": 0,
                    "bonus_action": 1,
                    "extra_action": 0,
                },
            }
        ]
    }

    assert _has_action_budget(healing_word_combat, "bard")
    assert not _has_action_budget(main_action_spell_combat, "wizard")


def test_encounter_actor_groups_keep_allies_out_of_registered_party_and_reject_overlap(
    tmp_path,
) -> None:
    party_report = tmp_path / "party.json"
    ally_report = tmp_path / "ally.json"
    hostile_report = tmp_path / "hostile.json"
    party_report.write_text(
        json.dumps(
            {
                "characters": [
                    {"actor_id": "pc-1"},
                    {"actor_id": "pc-2"},
                ]
            }
        ),
        encoding="utf-8",
    )
    ally_report.write_text(
        json.dumps({"created": {"character": {"id": "durnan"}}}),
        encoding="utf-8",
    )
    hostile_report.write_text(
        json.dumps({"actors": [{"id": "troll"}, {"id": "stirge"}]}),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        party_report=[party_report],
        ally_report=[ally_report],
        hostile_report=[hostile_report],
        hostile_actor_id=[],
        additional_hostile_report=[],
        additional_hostile_actor_id=[],
        reinforcement_hostile_report=[],
        reinforcement_hostile_actor_id=[],
        ally_actor_id=[],
    )

    groups = _encounter_actor_groups(args)

    assert groups["party_ids"] == ["pc-1", "pc-2"]
    assert groups["ally_ids"] == ["durnan"]
    assert groups["hostile_ids"] == ["troll", "stirge"]

    hostile_report.write_text(
        json.dumps({"actors": [{"id": "durnan"}]}),
        encoding="utf-8",
    )
    try:
        _encounter_actor_groups(args)
    except ValueError as exc:
        assert "must be disjoint" in str(exc)
    else:
        raise AssertionError("the same actor cannot be both an ally and a hostile")


def test_encounter_actor_groups_reject_incomplete_source_hostile_selection(
    tmp_path,
) -> None:
    party_report = tmp_path / "party.json"
    hostile_report = tmp_path / "hostile.json"
    party_report.write_text(
        json.dumps({"characters": [{"actor_id": "pc-1"}]}),
        encoding="utf-8",
    )
    hostile_report.write_text(
        json.dumps(
            {
                "actors": [
                    {"id": "cultist-1"},
                    {"id": "cultist-2"},
                    {"id": "acolyte-1"},
                ]
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        party_report=[party_report],
        ally_report=[],
        ally_actor_id=[],
        hostile_report=[hostile_report],
        hostile_actor_id=["cultist-1", "cultist-2"],
        required_hostile_count=3,
        hostile_count_basis="Episode 1 table roll 5: 2 cultists and 1 acolyte.",
        additional_hostile_report=[],
        additional_hostile_actor_id=[],
        reinforcement_hostile_report=[],
        reinforcement_hostile_actor_id=[],
    )

    with pytest.raises(ValueError, match="does not match"):
        _encounter_actor_groups(args)

    args.hostile_actor_id.append("acolyte-1")
    assert _encounter_actor_groups(args)["hostile_ids"] == [
        "cultist-1",
        "cultist-2",
        "acolyte-1",
    ]


def test_live_manifest_party_rejects_departed_predecessor_and_missing_replacement() -> None:
    manifest_result = {
        "manifest": {
            "party": {
                "members": [
                    {"actor_id": "cleric", "status": "active"},
                    {"actor_id": "replacement", "status": "active"},
                ]
            }
        }
    }

    assert _require_live_active_party(
        ["replacement", "cleric"],
        manifest_result,
    ) == ["cleric", "replacement"]

    with pytest.raises(ValueError, match="missing=.*replacement.*unexpected=.*predecessor"):
        _require_live_active_party(
            ["cleric", "predecessor"],
            manifest_result,
        )


def test_character_reads_are_batched_per_encounter_step() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def domain(self, tool_id: str, arguments: dict) -> list[dict]:
            self.calls.append((tool_id, arguments))
            actor_ids = arguments["payload"]["character_ids"]
            return [{"id": actor_id, "name": actor_id} for actor_id in actor_ids]

    client = Client()
    result = asyncio.run(_characters(client, "campaign-1", ["pc-1", "goblin-1"]))

    assert list(result) == ["pc-1", "goblin-1"]
    assert client.calls == [
        (
            "character_query",
            {
                "view": "batch",
                "payload": {
                    "campaign_id": "campaign-1",
                    "character_ids": ["pc-1", "goblin-1"],
                },
            },
        )
    ]


def _spell_actor(*spell_ids: str, hp: int = 10, slots: int = 1) -> dict:
    return {
        "sheet": {
            "combat": {"hp": {"value": hp}},
            "conditions": [],
            "spellcasting": {"spell_slots": {"1": {"value": slots}}},
            "content": {"spells": [{"id": spell_id} for spell_id in spell_ids]},
        }
    }


def test_party_spell_tactics_prioritize_recovery_then_supported_offense() -> None:
    actors = {
        "cleric": _spell_actor(HEALING_WORD_ID, GUIDING_BOLT_ID),
        "wizard": _spell_actor(MAGIC_MISSILE_ID),
        "ally": _spell_actor(hp=0, slots=0),
        "goblin": _spell_actor(slots=0),
    }

    assert _choose_party_spell(
        "cleric",
        party_ids=["cleric", "wizard", "ally"],
        actors=actors,
        living_targets=["goblin"],
    ) == (HEALING_WORD_ID, "ally", 1)

    actors["ally"]["sheet"]["combat"]["hp"]["value"] = 3
    assert _choose_party_spell(
        "cleric",
        party_ids=["cleric", "wizard", "ally"],
        actors=actors,
        living_targets=["goblin"],
    ) == (GUIDING_BOLT_ID, "goblin", 1)
    assert _choose_party_spell(
        "wizard",
        party_ids=["cleric", "wizard", "ally"],
        actors=actors,
        living_targets=["goblin"],
    ) == (MAGIC_MISSILE_ID, "goblin", 1)
    assert (
        _choose_party_spell(
            "cleric",
            party_ids=["cleric", "wizard", "ally"],
            actors=actors,
            living_targets=["goblin"],
            leveled_spell_available=False,
        )
        is None
    )
    actors["evil-mage"] = _spell_actor(MAGIC_MISSILE_ID)
    assert _choose_party_spell(
        "evil-mage",
        party_ids=["cleric", "wizard", "ally"],
        actors=actors,
        living_targets=["wizard"],
    ) == (MAGIC_MISSILE_ID, "wizard", 1)


def test_party_spell_tactics_respect_preparation_and_upcast_when_needed() -> None:
    wizard = _spell_actor(MAGIC_MISSILE_ID, "unprepared-spell", slots=0)
    wizard["sheet"]["spellcasting"].update(
        {
            "preparation": {
                "mode": "spellbook",
                "selected_spell_ids": [MAGIC_MISSILE_ID],
            },
            "spell_slots": {
                "1": {"value": 0, "max": 4},
                "2": {"value": 2, "max": 2},
            },
        }
    )
    wizard["sheet"]["content"]["spells"][1]["access"] = {
        "known": False,
        "prepared": False,
        "in_spellbook": True,
    }
    wizard["derived"] = {"spellcasting": {"prepared_spell_ids": [MAGIC_MISSILE_ID]}}
    actors = {"wizard": wizard, "goblin": _spell_actor(slots=0)}

    assert _choose_party_spell(
        "wizard",
        party_ids=["wizard"],
        actors=actors,
        living_targets=["goblin"],
    ) == (MAGIC_MISSILE_ID, "goblin", 2)


def test_party_tactics_do_not_target_unobserved_hidden_combatants() -> None:
    combat = {
        "combatants": [
            {"actor_id": "pc", "hidden": False},
            {"actor_id": "hidden", "hidden": True, "visible_to_actor_ids": None},
            {
                "actor_id": "spotted",
                "hidden": True,
                "visible_to_actor_ids": ["pc"],
            },
            {"actor_id": "revealed", "hidden": False},
        ]
    }

    assert _observable_target_ids(
        combat,
        observer_id="pc",
        target_ids=["hidden", "spotted", "revealed"],
    ) == ["spotted", "revealed"]


def test_party_tactics_focus_observably_wounded_targets() -> None:
    healthy = {"sheet": {"combat": {"hp": {"value": 7, "max": 7}}}}
    wounded = {"sheet": {"combat": {"hp": {"value": 22, "max": 27}}}}

    assert _wound_priority(wounded) < _wound_priority(healthy)


def test_conscious_prone_combatant_stands_before_moving() -> None:
    actor = {
        "sheet": {
            "combat": {"hp": {"value": 7, "max": 8}},
            "conditions": ["prone"],
        }
    }

    assert _should_stand(actor, {"move", "attack"})
    assert not _should_stand(actor, {"attack"})

    actor["sheet"]["conditions"].append("unconscious")
    actor["sheet"]["combat"]["hp"]["value"] = 0
    assert not _should_stand(actor, {"move", "attack"})


def test_movement_pending_reaction_blocks_followup_attack() -> None:
    assert _has_blocking_pending(
        {
            "pending": [
                {
                    "id": "reaction-1",
                    "kind": "reaction",
                    "trigger": "opportunity_attack",
                    "status": "pending",
                }
            ]
        }
    )
    assert not _has_blocking_pending({"pending": [{"id": "reaction-1", "status": "resolved"}]})


def test_reaction_tactics_spend_shield_only_when_it_changes_the_attack() -> None:
    base = {
        "trigger": "attack_hit_defense",
        "candidates": [
            {
                "id": "shield",
                "projected_hit": False,
                "cast_levels": [2, 1],
            },
            {"id": "decline"},
        ],
    }

    assert _defense_selection(base) == {"id": "shield", "cast_level": 1}
    base["candidates"][0]["projected_hit"] = True
    assert _defense_selection(base) == {"id": "decline"}


def test_reaction_tactics_block_magic_missile_when_shield_is_available() -> None:
    assert _defense_selection(
        {
            "trigger": "magic_missile_targeted",
            "candidates": [
                {"id": "shield", "cast_levels": [1, 2]},
                {"id": "decline"},
            ],
        }
    ) == {"id": "shield", "cast_level": 1}


def test_all_source_hostiles_defeated_is_victory_without_flee_rule() -> None:
    assert _source_outcome(
        defeated_hostiles=2,
        hostile_count=2,
        unresolved_party=False,
        party_down=False,
    ) == ("victory", "All 2 source-defined hostiles were defeated.")


def test_specific_source_flee_counts_only_that_hostile_as_resolved() -> None:
    assert _source_outcome(
        defeated_hostiles=3,
        fled_hostiles=1,
        hostile_count=4,
        unresolved_party=False,
        party_down=False,
    ) == (
        "victory",
        "3 source-defined hostiles were defeated and 1 followed a source instruction to flee.",
    )
    assert (
        _source_outcome(
            defeated_hostiles=2,
            fled_hostiles=1,
            hostile_count=4,
            unresolved_party=False,
            party_down=False,
        )
        is None
    )


def test_party_defeat_does_not_invent_a_source_defined_aftermath() -> None:
    assert _source_outcome(
        defeated_hostiles=1,
        hostile_count=4,
        unresolved_party=False,
        party_down=True,
    ) == (
        "defeat",
        "The party was defeated. Combat ended with resolved unconscious or dead "
        "characters; their later treatment requires explicit source support or "
        "Agent-as-DM adjudication.",
    )


def test_source_flee_count_threshold_targets_every_designated_survivor() -> None:
    defeated = ["bugbear-1", "bugbear-3"]
    assert _source_flee_ready(
        acting_actor_id="vhalak",
        flee_actor_ids={"vhalak", "bugbear-2"},
        defeated_hostile_ids=defeated,
        flee_after_defeated=2,
        trigger_defeated_actor_id="",
    )
    assert _source_flee_ready(
        acting_actor_id="bugbear-2",
        flee_actor_ids={"vhalak", "bugbear-2"},
        defeated_hostile_ids=defeated,
        flee_after_defeated=2,
        trigger_defeated_actor_id="",
    )
    assert not _source_flee_ready(
        acting_actor_id="bugbear-4",
        flee_actor_ids={"vhalak", "bugbear-2"},
        defeated_hostile_ids=defeated,
        flee_after_defeated=2,
        trigger_defeated_actor_id="",
    )
    assert not _source_flee_ready(
        acting_actor_id="vhalak",
        flee_actor_ids={"vhalak", "bugbear-2"},
        defeated_hostile_ids=["bugbear-1"],
        flee_after_defeated=2,
        trigger_defeated_actor_id="",
    )


def test_source_flee_supports_damage_or_critical_hit_thresholds() -> None:
    assert _source_flee_ready(
        acting_actor_id="lennithon",
        flee_actor_ids={"lennithon"},
        defeated_hostile_ids=[],
        flee_after_defeated=0,
        trigger_defeated_actor_id="",
        damage_taken_by_actor={"lennithon": 24},
        flee_after_damage=24,
        critical_hit_actor_ids=set(),
        flee_on_critical=True,
    )
    assert _source_flee_ready(
        acting_actor_id="lennithon",
        flee_actor_ids={"lennithon"},
        defeated_hostile_ids=[],
        flee_after_defeated=0,
        trigger_defeated_actor_id="",
        damage_taken_by_actor={"lennithon": 3},
        flee_after_damage=24,
        critical_hit_actor_ids={"lennithon"},
        flee_on_critical=True,
    )
    assert not _source_flee_ready(
        acting_actor_id="lennithon",
        flee_actor_ids={"lennithon"},
        defeated_hostile_ids=[],
        flee_after_defeated=0,
        trigger_defeated_actor_id="",
        damage_taken_by_actor={"lennithon": 23},
        flee_after_damage=24,
        critical_hit_actor_ids=set(),
        flee_on_critical=True,
    )


def test_source_flee_configuration_allows_authored_damage_or_critical_alternatives() -> None:
    assert _validate_source_flee_configuration(
        SimpleNamespace(
            flee_actor_id=["lennithon"],
            flee_trigger_defeated_actor_id="",
            flee_on_start_actor_id="",
            flee_after_defeated=0,
            flee_after_damage=24,
            flee_on_critical=True,
            flee_source_excerpt="After it has taken 24 damage or one critical hit, it leaves.",
            source_excerpt=(
                "The dragon attacks the defenders. After it has taken 24 damage "
                "or one critical hit, it leaves."
            ),
        ),
        hostile_ids=["lennithon"],
    ) == {"lennithon"}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"flee_actor_id": []}, "--flee-actor-id"),
        ({"flee_after_damage": 0, "flee_on_critical": False}, "at least one"),
        ({"flee_after_damage": -1}, "must not be negative"),
        ({"flee_source_excerpt": ""}, "require --flee-source-excerpt"),
    ],
)
def test_source_flee_configuration_fails_closed(
    overrides: dict[str, object],
    message: str,
) -> None:
    values = {
        "flee_actor_id": ["lennithon"],
        "flee_trigger_defeated_actor_id": "",
        "flee_on_start_actor_id": "",
        "flee_after_defeated": 0,
        "flee_after_damage": 24,
        "flee_on_critical": True,
        "flee_source_excerpt": "After it has taken 24 damage or one critical hit, it leaves.",
        "source_excerpt": (
            "The dragon attacks the defenders. After it has taken 24 damage "
            "or one critical hit, it leaves."
        ),
    }
    values.update(overrides)
    with pytest.raises(ValueError, match=message):
        _validate_source_flee_configuration(
            SimpleNamespace(**values),
            hostile_ids=["lennithon"],
        )


def test_source_flee_configuration_rejects_uncited_trigger_excerpt() -> None:
    with pytest.raises(ValueError, match="must be contained"):
        _validate_source_flee_configuration(
            SimpleNamespace(
                flee_actor_id=["lennithon"],
                flee_trigger_defeated_actor_id="",
                flee_on_start_actor_id="",
                flee_after_defeated=0,
                flee_after_damage=24,
                flee_on_critical=True,
                flee_source_excerpt="After 24 damage, the dragon leaves.",
                source_excerpt="The dragon attacks the defenders.",
            ),
            hostile_ids=["lennithon"],
        )


def test_source_flee_damage_tracking_uses_server_applied_damage_and_critical() -> None:
    damage = {"lennithon": 0}
    critical: set[str] = set()
    observations = _record_source_flee_damage(
        {
            "result": {
                "target_id": "lennithon",
                "hit": True,
                "critical": True,
                "damage": {
                    "input_amount": 18,
                    "applied_amount": 9,
                    "hp_damage": 9,
                },
            }
        },
        flee_actor_ids={"lennithon"},
        damage_taken_by_actor=damage,
        critical_hit_actor_ids=critical,
    )

    assert observations == [
        {
            "target_id": "lennithon",
            "applied_damage": 9,
            "cumulative_applied_damage": 9,
            "critical_hit": True,
        }
    ]
    assert damage == {"lennithon": 9}
    assert critical == {"lennithon"}


def test_source_flee_damage_tracking_counts_each_magic_missile_dart() -> None:
    damage = {"lennithon": 0}
    critical: set[str] = set()
    observations = _record_source_flee_damage(
        {
            "result": {
                "kind": "magic_missile",
                "targets": [
                    {
                        "target_id": "lennithon",
                        "dart_results": [
                            {"input_amount": 4, "applied_amount": 4},
                            {"input_amount": 5, "applied_amount": 5},
                            {"input_amount": 3, "applied_amount": 3},
                        ],
                    }
                ],
            }
        },
        flee_actor_ids={"lennithon"},
        damage_taken_by_actor=damage,
        critical_hit_actor_ids=critical,
    )

    assert observations[0]["applied_damage"] == 12
    assert damage == {"lennithon": 12}
    assert critical == set()


def test_source_flee_damage_history_restores_interrupted_combat_counter() -> None:
    damage, critical = _source_flee_damage_history(
        {
            "log": [
                {
                    "type": "attack",
                    "result": {
                        "target_id": "lennithon",
                        "hit": True,
                        "critical": False,
                        "damage": {"applied_amount": 11},
                    },
                },
                {
                    "type": "attack_defense_resolved",
                    "result": {
                        "target_id": "lennithon",
                        "hit": True,
                        "critical": True,
                        "damage": {"applied_amount": 7},
                    },
                },
                {
                    "type": "attack",
                    "result": {
                        "target_id": "other",
                        "hit": True,
                        "critical": True,
                        "damage": {"applied_amount": 99},
                    },
                },
            ]
        },
        flee_actor_ids={"lennithon"},
    )

    assert damage == {"lennithon": 18}
    assert critical == {"lennithon"}


def test_source_casualty_pool_requires_exact_cohort_dice_and_reviewed_recharge() -> None:
    source_excerpt = (
        "There are twenty NPC defenders on the walls at the beginning of the mission. "
        "Every breath attack not directed at them kills 1d4 NPC defenders and injures "
        "1d6 more. After each attack, Lennithon swoops away until his breath weapon "
        "recharges, then swings in for another attack."
    )
    pools = _source_casualty_pools(
        [
            {
                "actor_id": "lennithon",
                "pool_key": "greenest-wall-defenders",
                "initial_count": 20,
                "activity_name": "Lightning Breath (Recharge 5-6)",
                "kill_expression": "1d4",
                "injury_expression": "1d6",
                "source_excerpt": source_excerpt,
            }
        ],
        hostile_ids=["lennithon"],
        actors={
            "lennithon": {
                "sheet": {
                    "content": {
                        "activities": [
                            {
                                "id": "lightning-breath",
                                "name": "Lightning Breath (Recharge 5-6)",
                                "source_key": "monster-manual/adult-blue-dragon",
                                "rule_refs": [{"page": 92}],
                                "description": "The dragon exhales lightning.",
                                "choices": {
                                    "manual_ruling": {
                                        "kind": "descriptive_activity",
                                        "source_excerpt": (
                                            "The dragon exhales lightning."
                                        ),
                                    }
                                },
                            }
                        ]
                    }
                }
            }
        },
        encounter_source_excerpt=f"Dragon Attack. {source_excerpt} Rewards.",
    )

    assert pools["lennithon"] == {
        "actor_id": "lennithon",
        "pool_key": "greenest-wall-defenders",
        "initial_count": 20,
        "activity_id": "lightning-breath",
        "activity_name": "Lightning Breath (Recharge 5-6)",
        "activity_source_key": "monster-manual/adult-blue-dragon",
        "activity_rule_refs": [{"page": 92}],
        "kill_expression": "1d4",
        "injury_expression": "1d6",
        "recharge_expression": "1d6",
        "recharge_minimum": 5,
        "recharge_maximum": 6,
        "source_excerpt": source_excerpt,
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("initial_count", 19, "does not match"),
        ("kill_expression", "1d6", "does not match"),
        ("activity_name", "Lightning Breath", "reviewed rechargeable"),
    ],
)
def test_source_casualty_pool_fails_closed_on_uncorroborated_facts(
    field: str,
    value: object,
    message: str,
) -> None:
    source_excerpt = (
        "There are twenty NPC defenders on the walls. Every breath attack kills "
        "1d4 NPC defenders and injures 1d6 more. Lennithon swoops away until his "
        "breath weapon recharges."
    )
    declaration = {
        "actor_id": "lennithon",
        "pool_key": "greenest-wall-defenders",
        "initial_count": 20,
        "activity_name": "Lightning Breath (Recharge 5-6)",
        "kill_expression": "1d4",
        "injury_expression": "1d6",
        "source_excerpt": source_excerpt,
    }
    declaration[field] = value
    with pytest.raises(ValueError, match=message):
        _source_casualty_pools(
            [declaration],
            hostile_ids=["lennithon"],
            actors={
                "lennithon": {
                    "sheet": {
                        "content": {
                            "activities": [
                                {
                                    "id": "lightning-breath",
                                    "name": "Lightning Breath (Recharge 5-6)",
                                }
                            ]
                        }
                    }
                }
            },
            encounter_source_excerpt=source_excerpt,
        )


def test_source_casualty_pool_requires_a_descriptive_agent_ruling_marker() -> None:
    source_excerpt = (
        "There are twenty NPC defenders on the walls. Every breath attack kills "
        "1d4 NPC defenders and injures 1d6 more. Lennithon swoops away until his "
        "breath weapon recharges."
    )

    with pytest.raises(ValueError, match="descriptive activity"):
        _source_casualty_pools(
            [
                {
                    "actor_id": "lennithon",
                    "pool_key": "greenest-wall-defenders",
                    "initial_count": 20,
                    "activity_name": "Lightning Breath (Recharge 5-6)",
                    "kill_expression": "1d4",
                    "injury_expression": "1d6",
                    "source_excerpt": source_excerpt,
                }
            ],
            hostile_ids=["lennithon"],
            actors={
                "lennithon": {
                    "sheet": {
                        "content": {
                            "activities": [
                                {
                                    "id": "lightning-breath",
                                    "name": "Lightning Breath (Recharge 5-6)",
                                    "description": "The dragon exhales lightning.",
                                    "choices": {},
                                }
                            ]
                        }
                    }
                }
            },
            encounter_source_excerpt=source_excerpt,
        )


def test_source_casualty_pool_state_is_bounded_and_idempotent() -> None:
    declaration = {
        "actor_id": "lennithon",
        "initial_count": 5,
        "activity_name": "Lightning Breath (Recharge 5-6)",
        "kill_expression": "1d4",
        "injury_expression": "1d6",
        "recharge_minimum": 5,
        "recharge_maximum": 6,
    }
    first, event, replayed = _apply_source_casualty_rolls(
        None,
        declaration=declaration,
        combat_id="combat-1",
        round_number=1,
        recharge_roll=None,
        kill_roll=4,
        injury_roll=6,
    )
    assert replayed is False
    assert event["killed"] == 4
    assert event["injured"] == 1
    assert first["able"] == 0
    assert first["killed"] == 4
    assert first["injured"] == 1

    restored, repeated, replayed = _apply_source_casualty_rolls(
        first,
        declaration=declaration,
        combat_id="combat-1",
        round_number=1,
        recharge_roll=None,
        kill_roll=1,
        injury_roll=1,
    )
    assert replayed is True
    assert repeated == event
    assert restored == first

    waited, wait_event, replayed = _apply_source_casualty_rolls(
        first,
        declaration=declaration,
        combat_id="combat-1",
        round_number=2,
        recharge_roll=4,
        kill_roll=None,
        injury_roll=None,
    )
    assert replayed is False
    assert wait_event["recharged"] is False
    assert waited["attacks"] == 1
    assert waited["able"] == 0


def test_source_casualty_pool_turn_uses_only_public_action_dice_and_manifest() -> None:
    calls: list[tuple[str, dict]] = []

    class Client:
        revision = 7

        async def core(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "campaign_query"
            return {"revision": self.revision}

        async def domain(self, tool_id: str, arguments: dict) -> dict:
            calls.append((tool_id, arguments))
            if tool_id == "playthrough_manifest" and arguments["action"] == "get":
                return {"manifest": {"world_state": {}}}
            if tool_id == "combat_use_activity":
                self.revision += 1
                return {
                    "status": "pending_ruling",
                    "result": {
                        "activity_id": "lightning-breath",
                        "payment": {"kind": "main_action"},
                    },
                }
            if tool_id == "dnd_dice_roll":
                self.revision += 1
                return {"total": 2 if arguments["expression"] == "1d4" else 3}
            if tool_id == "playthrough_manifest" and arguments["action"] == "replace":
                self.revision += 1
                return {"manifest": arguments["payload"]["manifest"]}
            raise AssertionError((tool_id, arguments))

    result = asyncio.run(
        _settle_source_casualty_pool_turn(
            Client(),
            SimpleNamespace(
                campaign_id="campaign-1",
                run_id="run-1",
                operation_scope="encounter-1",
            ),
            branch_id="branch-1",
            combat={"id": "combat-1", "round": 1},
            declaration={
                "actor_id": "lennithon",
                "pool_key": "greenest-wall-defenders",
                "initial_count": 20,
                "activity_id": "lightning-breath",
                "activity_name": "Lightning Breath (Recharge 5-6)",
                "kill_expression": "1d4",
                "injury_expression": "1d6",
                "recharge_expression": "1d6",
                "recharge_minimum": 5,
                "recharge_maximum": 6,
                "source_excerpt": "Exact module excerpt.",
            },
        )
    )

    assert [item[0] for item in calls] == [
        "playthrough_manifest",
        "combat_use_activity",
        "dnd_dice_roll",
        "dnd_dice_roll",
        "playthrough_manifest",
        "playthrough_manifest",
    ]
    assert result["event"]["killed"] == 2
    assert result["event"]["injured"] == 3
    replaced_manifest = calls[-1][1]["payload"]["manifest"]
    pool = replaced_manifest["world_state"]["source_casualty_pools"][
        "greenest-wall-defenders"
    ]
    assert pool["able"] == 15
    assert calls[1][1]["activity_id"] == "lightning-breath"
    assert calls[1][1]["declaration"]["kind"] == "source_casualty_pool_activity"
    assert calls[2][1]["branch_id"] == "branch-1"


def test_source_casualty_pool_stops_on_precommit_activity_ruling() -> None:
    calls: list[str] = []

    class Client:
        async def core(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "campaign_query"
            return {"revision": 7}

        async def domain(self, tool_id: str, arguments: dict) -> dict:
            calls.append(tool_id)
            if tool_id == "playthrough_manifest":
                return {"manifest": {"world_state": {}}}
            if tool_id == "combat_use_activity":
                return {
                    "status": "pending_ruling",
                    "default_resolver": "agent",
                    "ruling_kind": "module_specific_procedure",
                    "reason": "the scene fact must be adjudicated before payment",
                    "committed": False,
                    "result": {"activity_id": "lightning-breath"},
                }
            raise AssertionError("casualty dice must not roll before activity payment")

    with pytest.raises(EncounterRulingRequiredError) as raised:
        asyncio.run(
            _settle_source_casualty_pool_turn(
                Client(),
                SimpleNamespace(
                    campaign_id="campaign-1",
                    run_id="run-1",
                    operation_scope="encounter-1",
                ),
                branch_id="branch-1",
                combat={"id": "combat-1", "round": 1},
                declaration={
                    "actor_id": "lennithon",
                    "pool_key": "greenest-wall-defenders",
                    "initial_count": 20,
                    "activity_id": "lightning-breath",
                    "activity_name": "Lightning Breath (Recharge 5-6)",
                    "kill_expression": "1d4",
                    "injury_expression": "1d6",
                    "recharge_expression": "",
                    "recharge_minimum": 5,
                    "recharge_maximum": 6,
                    "source_excerpt": "Exact module excerpt.",
                },
            )
        )

    assert calls == ["playthrough_manifest", "combat_use_activity"]
    assert raised.value.requirement["ruling"]["default_resolver"] == "agent"


def test_source_separation_is_cited_and_places_dragon_at_least_twenty_five_feet_away() -> None:
    excerpt = (
        "During this attack, Lennithon flies over the keep and uses his breath "
        "weapon without moving closer than 25 feet from the parapet."
    )
    separation = _source_separations(
        [
            {
                "actor_id": "lennithon",
                "other_actor_ids": ["pc-1", "pc-2"],
                "minimum_distance_ft": 25,
                "source_excerpt": excerpt,
            }
        ],
        participant_ids=["pc-1", "pc-2", "lennithon"],
        hostile_ids=["lennithon"],
        encounter_source_excerpt=f"Dragon Attack. {excerpt} The defenders hold.",
    )
    positioned = _apply_source_separations(
        [
            {"actor_id": "pc-1", "position": {"x": 1, "y": 1}},
            {"actor_id": "pc-2", "position": {"x": 1, "y": 2}},
            {"actor_id": "lennithon", "position": {"x": 2, "y": 2}},
        ],
        separation,
    )
    by_actor = {item["actor_id"]: item for item in positioned}

    assert max(
        abs(by_actor["lennithon"]["position"]["x"] - by_actor["pc-1"]["position"]["x"]),
        abs(by_actor["lennithon"]["position"]["y"] - by_actor["pc-1"]["position"]["y"]),
    ) >= 5
    assert max(
        abs(by_actor["lennithon"]["position"]["x"] - by_actor["pc-2"]["position"]["x"]),
        abs(by_actor["lennithon"]["position"]["y"] - by_actor["pc-2"]["position"]["y"]),
    ) >= 5
    assert _source_separation_target("pc-1", ["lennithon"], separation) == separation[
        "lennithon"
    ]
    assert _source_separation_target("outsider", ["lennithon"], separation) is None


def test_source_separation_rejects_an_uncorroborated_distance() -> None:
    excerpt = "Lennithon does not move closer than 25 feet from the parapet."
    with pytest.raises(ValueError, match="not corroborated"):
        _source_separations(
            [
                {
                    "actor_id": "lennithon",
                    "other_actor_ids": ["pc-1"],
                    "minimum_distance_ft": 30,
                    "source_excerpt": excerpt,
                }
            ],
            participant_ids=["pc-1", "lennithon"],
            hostile_ids=["lennithon"],
            encounter_source_excerpt=excerpt,
        )


def test_agent_positions_are_source_cited_and_preserve_the_ruling() -> None:
    excerpt = (
        "Group C consists of two cultists and six kobolds clustered tightly "
        "around the temple's back door."
    )
    positions = _agent_positions(
        [
            {
                "actor_id": "kobold-1",
                "x": 2,
                "y": 1,
                "source_excerpt": excerpt,
                "ruling_reason": "Place the tightly clustered rear-door group together.",
            },
            {
                "actor_id": "kobold-2",
                "x": 2,
                "y": 2,
                "source_excerpt": excerpt,
                "ruling_reason": "Place the tightly clustered rear-door group together.",
            },
        ],
        participant_ids=["pc-1", "kobold-1", "kobold-2"],
        encounter_source_excerpt=f"Sanctuary. {excerpt}",
    )

    config = _participant_config(
        ["pc-1"],
        ["kobold-1", "kobold-2"],
        surprise_by_actor={"kobold-1": True, "kobold-2": True},
        agent_positions=positions,
    )
    by_actor = {item["actor_id"]: item for item in config}

    assert by_actor["kobold-1"]["position"] == {"x": 2, "y": 1}
    assert by_actor["kobold-2"]["position"] == {"x": 2, "y": 2}
    assert positions["kobold-1"] == {
        "actor_id": "kobold-1",
        "position": {"x": 2, "y": 1},
        "source_excerpt": excerpt,
        "ruling_reason": "Place the tightly clustered rear-door group together.",
    }


def test_agent_positions_reject_missing_evidence_and_participant_overlap() -> None:
    with pytest.raises(ValueError, match="exact encounter excerpt"):
        _agent_positions(
            [
                {
                    "actor_id": "kobold-1",
                    "x": 2,
                    "y": 1,
                    "source_excerpt": "A different encounter.",
                    "ruling_reason": "Place the hostile.",
                }
            ],
            participant_ids=["pc-1", "kobold-1"],
            encounter_source_excerpt="Group C is clustered tightly.",
        )

    with pytest.raises(ValueError, match="overlap"):
        _apply_agent_positions(
            [
                {"actor_id": "pc-1", "position": {"x": 1, "y": 1}},
                {"actor_id": "kobold-1", "position": {"x": 2, "y": 2}},
            ],
            {
                "kobold-1": {
                    "actor_id": "kobold-1",
                    "position": {"x": 1, "y": 1},
                    "source_excerpt": "Group C is clustered tightly.",
                    "ruling_reason": "Place the hostile.",
                }
            },
        )


def test_source_flee_does_not_end_while_other_hostiles_remain() -> None:
    assert (
        _source_outcome(
            defeated_hostiles=2,
            fled_hostiles=1,
            hostile_count=4,
            unresolved_party=False,
            party_down=False,
        )
        is None
    )


def test_source_departure_is_distinct_from_hiding() -> None:
    assert _source_departure_patch(
        "goblin-3",
        reason="As soon as a fight breaks out, one goblin flees to warn Klarg.",
        destination_location_key="8-klarg-s-cave",
    ) == {
        "key": "combatant_departure",
        "value": {
            "actor_id": "goblin-3",
            "reason": "As soon as a fight breaks out, one goblin flees to warn Klarg.",
            "destination_location_key": "8-klarg-s-cave",
        },
    }


def test_source_hostage_truce_requires_a_living_leader_and_resolved_party() -> None:
    assert _source_truce_outcome(
        defeated_hostiles=2,
        truce_after_defeated=2,
        truce_actor_alive=True,
        unresolved_party=False,
    ) == (
        "truce",
        "After 2 source-defined hostiles were defeated, "
        "the source-designated leader invoked the hostage truce.",
    )
    assert (
        _source_truce_outcome(
            defeated_hostiles=2,
            truce_after_defeated=2,
            truce_actor_alive=False,
            unresolved_party=False,
        )
        is None
    )
    assert (
        _source_truce_outcome(
            defeated_hostiles=2,
            truce_after_defeated=2,
            truce_actor_alive=True,
            unresolved_party=True,
        )
        is None
    )


def test_source_declared_surprise_marks_only_cited_participants() -> None:
    surprise, basis = _source_declared_surprise(
        party_ids=["pc-1", "pc-2"],
        hostile_ids=["iarno"],
        surprised_actor_ids=["iarno"],
        source_excerpt=(
            "If the characters approach this room through the secret passage "
            "from area 7, they can surprise the leader."
        ),
    )

    assert surprise == {"pc-1": False, "pc-2": False, "iarno": True}
    assert basis["mode"] == "source_declared_surprise"
    assert basis["surprised_actor_ids"] == ["iarno"]


def test_source_declared_conditions_are_scoped_to_cited_participants() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 24,
        "page_end": 25,
        "heading_path": ["Redbrand Hideout", "10. Common Room"],
        "content_sha256": "a" * 64,
    }
    conditions = _source_declared_conditions(
        [
            {
                "condition": "Poisoned",
                "actor_ids": ["ruffian-1", "ruffian-2"],
                "source_ref": source_ref,
                "source_excerpt": "All four are heavily drunk and poisoned.",
            }
        ],
        participant_ids=["pc-1", "ruffian-1", "ruffian-2"],
    )

    assert set(conditions) == {"ruffian-1", "ruffian-2"}
    assert conditions["ruffian-1"] == [
        {
            "condition": "poisoned",
            "duration": "encounter",
            "source_ref": source_ref,
            "source_excerpt": "All four are heavily drunk and poisoned.",
        }
    ]
    config = _participant_config(
        ["pc-1"],
        ["ruffian-1", "ruffian-2"],
        surprise_by_actor={},
        source_conditions_by_actor=conditions,
    )
    by_actor = {item["actor_id"]: item for item in config}
    assert "source_conditions" not in by_actor["pc-1"]
    assert by_actor["ruffian-1"]["source_conditions"][0]["condition"] == "poisoned"


def test_source_traits_and_allied_npcs_preserve_distinct_zero_hp_rules() -> None:
    traits = _source_traits(
        [
            {
                "actor_id": "troll",
                "kind": "regeneration",
                "feature_id": "regeneration-passive",
                "source_excerpt": ("The troll regains 10 hit points at the start of its turn."),
            }
        ],
        participant_ids=["pc-1", "durnan", "troll"],
    )

    config = _participant_config(
        ["pc-1"],
        ["troll"],
        ally_ids=["durnan"],
        surprise_by_actor={},
        source_traits_by_actor=traits,
    )
    by_actor = {item["actor_id"]: item for item in config}

    assert by_actor["pc-1"]["death_saves"] is True
    assert by_actor["durnan"]["disposition"] == "friendly"
    assert by_actor["durnan"]["death_saves"] is False
    assert by_actor["troll"]["death_saves"] is False
    assert by_actor["troll"]["source_traits"] == [
        {
            "kind": "regeneration",
            "feature_id": "regeneration-passive",
            "source_excerpt": ("The troll regains 10 hit points at the start of its turn."),
        }
    ]


def test_source_zero_hp_finisher_requires_module_and_oil_rule_evidence() -> None:
    source = (
        "Durnan calls on the characters to focus on slaying the stirges and then "
        "douse the troll with lamp oil and set it on fire when it falls."
    )
    oil_rule = (
        "If the target takes any fire damage before the oil dries (after 1 minute), "
        "the target takes an additional 5 fire damage from the burning oil."
    )
    finisher = _source_zero_hp_finisher(
        {
            "target_id": "troll",
            "actor_ids": ["pc-1", "pc-2"],
            "source_excerpt": ("douse the troll with lamp oil and set it on fire when it falls"),
            "oil_rule_excerpt": oil_rule,
        },
        participant_ids=["pc-1", "pc-2", "troll"],
        encounter_source_excerpt=source,
    )

    assert finisher is not None
    assert finisher["fire_damage"] == 5
    assert _source_zero_hp_finisher_stage(
        {"round": 3, "log": []},
        finisher,
    ) == ("douse", None)
    douse_event = {
        "type": "common_action",
        "payload": {
            "source_finisher_id": finisher["id"],
            "stage": "douse",
            "round": 3,
        },
    }
    assert _source_zero_hp_finisher_stage(
        {"round": 4, "log": [douse_event]},
        finisher,
    ) == ("ignite", douse_event)
    assert _source_zero_hp_finisher_stage(
        {"round": 13, "log": [douse_event]},
        finisher,
    ) == ("douse", douse_event)


def test_source_zero_hp_stabilization_requires_exact_pc_only_instruction() -> None:
    excerpt = (
        "If any of the characters are reduced to 0 hit points during the fight, "
        "employees of the Yawning Portal step forward to stabilize them."
    )

    assert _source_zero_hp_stabilization(
        {"actor_ids": ["pc-1", "pc-2"], "source_excerpt": excerpt},
        participant_ids=["pc-1", "pc-2"],
    ) == {
        "actor_ids": ["pc-1", "pc-2"],
        "source_excerpt": excerpt,
    }
    with pytest.raises(ValueError, match="unique participant PCs"):
        _source_zero_hp_stabilization(
            {
                "actor_ids": ["pc-1", "anonymous-employee"],
                "source_excerpt": excerpt,
            },
            participant_ids=["pc-1", "pc-2"],
        )


def test_source_target_priorities_preserve_authored_roles_and_tactical_order() -> None:
    excerpt = (
        "The stirges attack the nearest characters as Durnan confronts the monster. "
        "He calls on the characters to focus on slaying the stirges."
    )
    priorities = _source_target_priorities(
        [
            {
                "actor_ids": ["pc-1", "pc-2"],
                "priority_groups": [["stirge-1", "stirge-2"], ["troll"]],
                "source_excerpt": "He calls on the characters to focus on slaying the stirges.",
            },
            {
                "actor_ids": ["durnan"],
                "priority_groups": [["troll"]],
                "source_excerpt": "Durnan confronts the monster.",
            },
            {
                "actor_ids": ["stirge-1", "stirge-2"],
                "priority_groups": [["pc-1", "pc-2"]],
                "source_excerpt": "The stirges attack the nearest characters",
            },
        ],
        participant_ids=[
            "pc-1",
            "pc-2",
            "durnan",
            "troll",
            "stirge-1",
            "stirge-2",
        ],
        encounter_source_excerpt=excerpt,
    )

    assert set(priorities) == {"pc-1", "pc-2", "durnan", "stirge-1", "stirge-2"}
    assert _prioritize_targets(
        "pc-1",
        ["troll", "stirge-2", "stirge-1"],
        priorities,
    ) == ["stirge-2", "stirge-1", "troll"]
    assert _prioritize_targets(
        "durnan",
        ["stirge-1", "troll"],
        priorities,
    ) == ["troll", "stirge-1"]

    try:
        _source_target_priorities(
            [
                {
                    "actor_ids": ["pc-1"],
                    "priority_groups": [["unknown"]],
                    "source_excerpt": "focus on slaying the stirges",
                }
            ],
            participant_ids=["pc-1", "stirge-1"],
            encounter_source_excerpt=excerpt,
        )
    except ValueError as exc:
        assert "participant ids" in str(exc)
    else:
        raise AssertionError("target priorities cannot cite nonparticipants")


def test_agent_target_priorities_are_tactical_and_party_scoped() -> None:
    priorities = _agent_target_priorities(
        [
            {
                "actor_ids": ["cleric", "rogue"],
                "priority_groups": [["kobold-1", "kobold-2"], ["drake"]],
                "ruling_reason": (
                    "Remove the fragile bomb throwers before focusing the guard drake."
                ),
            }
        ],
        party_ids=["cleric", "rogue", "wizard"],
        hostile_ids=["kobold-1", "kobold-2", "drake"],
    )

    assert priorities["cleric"] == priorities["rogue"]
    assert priorities["cleric"]["default_resolver"] == "agent"
    assert priorities["cleric"]["ruling_kind"] == "tactical_decision"
    assert _prioritize_targets(
        "cleric",
        ["drake", "kobold-2", "kobold-1"],
        priorities,
    ) == ["kobold-2", "kobold-1", "drake"]


def test_agent_target_priorities_reject_wrong_side_actors_and_targets() -> None:
    with pytest.raises(ValueError, match="unique party actor_ids"):
        _agent_target_priorities(
            [
                {
                    "actor_ids": ["drake"],
                    "priority_groups": [["kobold"]],
                    "ruling_reason": "Invalid hostile actor.",
                }
            ],
            party_ids=["cleric"],
            hostile_ids=["kobold", "drake"],
        )
    with pytest.raises(ValueError, match="unique encounter hostiles"):
        _agent_target_priorities(
            [
                {
                    "actor_ids": ["cleric"],
                    "priority_groups": [["cleric"]],
                    "ruling_reason": "Invalid friendly target.",
                }
            ],
            party_ids=["cleric"],
            hostile_ids=["kobold"],
        )


def test_source_opening_item_casts_preserve_authored_order_and_evidence() -> None:
    casts = _source_opening_casts(
        [
            {
                "actor_id": "iarno",
                "spell_id": "mage-armor",
                "source_item_id": "staff-of-defense",
                "source_excerpt": "If threatened, Iarno uses his staff to cast mage armor.",
            },
            {
                "actor_id": "iarno",
                "spell_id": "shield",
                "source_item_id": "staff-of-defense",
                "source_excerpt": "Iarno uses the shield power of his staff.",
            },
        ],
        participant_ids=["pc-1", "iarno"],
    )

    assert [item["sequence"] for item in casts] == [1, 2]
    assert [item["spell_id"] for item in casts] == ["mage-armor", "shield"]
    assert all(item["source_item_id"] == "staff-of-defense" for item in casts)


def test_source_attack_environment_requires_the_structured_actor_trait() -> None:
    excerpt = (
        "While in sunlight, the kobold has disadvantage on attack rolls, "
        "as well as on Wisdom (Perception) checks that rely on sight."
    )
    actors = {
        "kobold": {
            "sheet": {
                "content": {
                    "features": [
                        {
                            "name": "Sunlight Sensitivity",
                            "description": excerpt,
                            "choices": {
                                "source_trait": {
                                    "kind": "sunlight_sensitivity",
                                }
                            },
                        }
                    ]
                }
            }
        }
    }

    environments = _source_attack_environments(
        [
            {
                "actor_id": "kobold",
                "direct_sunlight": False,
                "source_excerpt": excerpt,
                "ruling_reason": "The encounter occurs after midnight.",
            }
        ],
        participant_ids=["kobold"],
        actors=actors,
    )
    assert environments["kobold"] == {
        "actor_id": "kobold",
        "direct_sunlight": False,
        "source_excerpt": excerpt,
        "ruling_reason": "The encounter occurs after midnight.",
    }

    with pytest.raises(ValueError, match="ruling reason"):
        _source_attack_environments(
            [
                {
                    "actor_id": "kobold",
                    "direct_sunlight": False,
                    "source_excerpt": excerpt,
                }
            ],
            participant_ids=["kobold"],
            actors=actors,
        )

    with pytest.raises(ValueError, match="must match"):
        _source_attack_environments(
            [
                {
                    "actor_id": "other",
                    "direct_sunlight": True,
                    "source_excerpt": excerpt,
                    "ruling_reason": "The encounter occurs at noon.",
                }
            ],
            participant_ids=["other"],
            actors={"other": {"sheet": {"content": {"features": []}}}},
        )


def test_source_avoidance_requires_public_actor_knowledge(
    tmp_path,
) -> None:
    report_path = tmp_path / "trap-event.json"
    report_path.write_text(
        json.dumps(
            {
                "passed": True,
                "campaign_id": "campaign-1",
                "result": {
                    "continuity": {
                        "event": {
                            "id": "event-1",
                            "event_type": "trap_detected",
                            "summary": (
                                "The marked traps are at cells 3,3; 5,3; 6,3; 8,3; and 10,3."
                            ),
                            "payload": {
                                "scene_id": "scene-1",
                                "source_excerpt": "Five hidden bear traps.",
                                "source_ref": {"chunk_id": "chunk-1"},
                            },
                        },
                        "actor_knowledge": [
                            {
                                "actor_id": "pc-1",
                                "proposition": (
                                    "The actor knows and avoids cells 3,3; 5,3; 6,3; 8,3; and 10,3."
                                ),
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    avoided, evidence = _source_avoidances(
        [report_path],
        campaign_id="campaign-1",
        scene_id="scene-1",
        participant_ids=["pc-1"],
    )

    assert avoided == {"pc-1": {"3,3", "5,3", "6,3", "8,3", "10,3"}}
    assert evidence[0]["event_id"] == "event-1"
    assert evidence[0]["source_ref"] == {"chunk_id": "chunk-1"}


def test_source_authored_precombat_and_attack_tactics_are_structured() -> None:
    precombat = _source_precombat_casts(
        [
            {
                "actor_id": "nezznar",
                "spell_id": "invisibility",
                "cast_level": 2,
                "source_excerpt": "Nezznar casts invisibility on himself.",
            }
        ],
        participant_ids=["nezznar", "spider-1"],
    )
    openings = _source_opening_weapons(
        [
            {
                "actor_id": "spider-1",
                "weapon_id": "web",
                "source_excerpt": (
                    "The spiders try to web the characters before closing to melee."
                ),
            }
        ],
        participant_ids=["nezznar", "spider-1"],
    )
    rulings = _source_on_hit_rulings(
        [
            {
                "actor_id": "durnan",
                "weapon_id": "grimvault",
                "id": "critical_followup",
                "target_has_limbs": True,
                "source_excerpt": (
                    "If the target is a creature and Durnan rolls a 20 on the "
                    "d20 for the attack roll, the target takes an extra 14 "
                    "slashing damage, and Durnan rolls another d20."
                ),
            },
            {
                "actor_id": "stirge",
                "weapon_id": "blood-drain",
                "id": "attachment",
                "source_excerpt": (
                    "the stirge attaches to the target. While attached, the stirge doesn't attack."
                ),
            },
            {
                "actor_id": "duergar",
                "weapon_id": "war-pick",
                "id": "dismiss",
                "source_excerpt": ("or 11 (2d8 + 2) piercing damage while enlarged."),
            },
            {
                "actor_id": "spider-1",
                "weapon_id": "web",
                "condition": "restrained",
                "escape_dc": 12,
                "escape_abilities": ["strength"],
                "source_excerpt": (
                    "The target is restrained by webbing. As an action, the restrained "
                    "target can make a DC 12 Strength check, bursting the webbing on "
                    "a success."
                ),
            },
            {
                "actor_id": "spider-1",
                "weapon_id": "bite",
                "id": "saving_throw_damage",
                "save_ability": "constitution",
                "save_dc": 11,
                "damage_formula": "2d8",
                "damage_type": "poison",
                "half_on_success": True,
                "zero_hp_effect": {
                    "stable": True,
                    "conditions": ["poisoned", "paralyzed"],
                    "duration": {"period": "hour", "remaining": 1},
                },
                "source_excerpt": (
                    "The target must make a DC 11 Constitution saving throw, "
                    "taking 9 (2d8) poison damage on a failed save, or half as "
                    "much damage on a successful one. If the poison reduces the "
                    "target to 0 hit points, the target is stable but poisoned "
                    "for 1 hour, and paralyzed while poisoned in this way."
                ),
            },
            {
                "actor_id": "ettercap-1",
                "weapon_id": "bite",
                "id": "saving_throw_condition",
                "condition": "poisoned",
                "save_ability": "constitution",
                "save_dc": 11,
                "repeat_save_timing": "turn_end",
                "duration": {"period": "minute", "remaining": 1},
                "source_excerpt": (
                    "The target must succeed on a DC 11 Constitution saving throw "
                    "or be poisoned for 1 minute. The creature can repeat the saving "
                    "throw at the end of each of its turns, ending the effect on "
                    "itself on a success."
                ),
            },
        ],
        participant_ids=[
            "nezznar",
            "spider-1",
            "ettercap-1",
            "durnan",
            "stirge",
            "duergar",
        ],
    )
    delayed = _source_delayed_actions(
        [
            {
                "actor_id": "nezznar",
                "until_round": 2,
                "source_excerpt": ("Nezznar joins the fray in the round after the spiders attack."),
            }
        ],
        participant_ids=["nezznar", "spider-1"],
    )

    assert precombat[0]["cast_level"] == 2
    assert openings["spider-1"]["weapon_id"] == "web"
    assert rulings[("spider-1", "web")]["escape_dc"] == 12
    assert rulings[("spider-1", "bite")]["id"] == "saving_throw_damage"
    assert rulings[("spider-1", "bite")]["zero_hp_effect"]["stable"] is True
    assert rulings[("ettercap-1", "bite")] == {
        "actor_id": "ettercap-1",
        "weapon_id": "bite",
        "id": "saving_throw_condition",
        "condition": "poisoned",
        "save_ability": "constitution",
        "save_dc": 11,
        "repeat_save_timing": "turn_end",
        "duration": {"period": "minute", "remaining": 1},
        "source_excerpt": (
            "The target must succeed on a DC 11 Constitution saving throw or be "
            "poisoned for 1 minute. The creature can repeat the saving throw at "
            "the end of each of its turns, ending the effect on itself on a success."
        ),
    }
    assert rulings[("durnan", "grimvault")]["target_has_limbs"] is True
    assert rulings[("stirge", "blood-drain")]["id"] == "attachment"
    assert rulings[("duergar", "war-pick")] == {
        "actor_id": "duergar",
        "weapon_id": "war-pick",
        "id": "dismiss",
        "source_excerpt": "or 11 (2d8 + 2) piercing damage while enlarged.",
    }
    assert delayed["nezznar"]["until_round"] == 2


@pytest.mark.parametrize(
    "ruling",
    [
        {
            "actor_id": "ettercap-1",
            "weapon_id": "bite",
            "id": "saving_throw_condition",
            "condition": "poisoned",
            "save_ability": "constitution",
            "save_dc": 11,
            "repeat_save_timing": "turn_end",
            "duration": {"period": "minute", "remaining": 0},
            "source_excerpt": "Exact attack text.",
        },
        {
            "actor_id": "ettercap-1",
            "weapon_id": "web",
            "id": "apply_condition",
            "condition": "restrained",
            "escape_dc": 11,
            "escape_abilities": ["strength"],
            "save_ability": "strength",
            "source_excerpt": "Exact attack text.",
        },
    ],
)
def test_source_on_hit_rulings_reject_mixed_or_invalid_condition_terms(
    ruling: dict,
) -> None:
    with pytest.raises(ValueError):
        _source_on_hit_rulings(
            [ruling],
            participant_ids=["ettercap-1"],
        )


def test_auto_run_starts_from_play_before_loading_combat_tools() -> None:
    calls: list[tuple[str, object]] = []

    class Client:
        async def open(self, campaign_id: str) -> dict[str, str]:
            calls.append(("open", campaign_id))
            return {"phase": "play"}

    async def start(
        client: object,
        args: object,
        party_ids: list[str],
        hostile_ids: list[str],
        additional_hostile_ids: list[str],
        reinforcement_hostile_ids: list[str],
    ) -> dict[str, bool]:
        calls.append(
            (
                "start",
                (
                    party_ids,
                    hostile_ids,
                    additional_hostile_ids,
                    reinforcement_hostile_ids,
                ),
            )
        )
        return {"started": True}

    async def auto_run(
        client: object,
        args: object,
        party_ids: list[str],
        hostile_ids: list[str],
    ) -> dict[str, bool]:
        calls.append(("auto_run", (party_ids, hostile_ids)))
        return {"completed": True}

    with (
        patch("scripts.regression_encounter._start", start),
        patch("scripts.regression_encounter._auto_run", auto_run),
    ):
        result = asyncio.run(
            _start_or_resume_auto_run(
                Client(),
                SimpleNamespace(campaign_id="campaign-1"),
                ["pc-1"],
                ["hostile-1"],
                ["hostile-2"],
                ["hostile-3"],
            )
        )

    assert calls == [
        ("open", "campaign-1"),
        ("start", (["pc-1"], ["hostile-1"], ["hostile-2"], ["hostile-3"])),
        ("auto_run", (["pc-1"], ["hostile-1", "hostile-2", "hostile-3"])),
    ]
    assert result == {
        "completed": True,
        "auto_start": {"started": True},
    }


def test_interrupted_guiding_bolt_ruling_resumes_with_exact_effect() -> None:
    calls: list[tuple[str, dict]] = []

    class Client:
        async def core(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "campaign_query"
            return {"revision": 17}

        async def domain(self, tool_id: str, arguments: dict) -> dict:
            calls.append((tool_id, arguments))
            return {"status": "committed"}

    result = asyncio.run(
        _resolve_pending(
            Client(),
            SimpleNamespace(campaign_id="campaign-1"),
            "branch-1",
            {
                "pending": [
                    {
                        "id": "choice-1",
                        "kind": "ruling",
                        "actor_id": "target-1",
                        "target_id": "target-1",
                        "trigger": "attack_on_hit_effect",
                        "effect": GUIDING_BOLT_ON_HIT,
                        "status": "pending",
                    }
                ]
            },
        )
    )

    assert result == {"status": "committed"}
    assert calls[0][0] == "combat_choice"
    assert calls[0][1]["action"] == "on_hit_ruling"
    assert calls[0][1]["payload"]["selection"] == {
        "id": "next_attack_advantage",
        "source_excerpt": GUIDING_BOLT_ON_HIT,
    }


def test_interrupted_source_attachment_resumes_with_declared_settlement() -> None:
    calls: list[tuple[str, dict]] = []
    excerpt = "the stirge attaches to the target. While attached, the stirge doesn't attack."

    class Client:
        async def core(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "campaign_query"
            return {"revision": 18}

        async def domain(self, tool_id: str, arguments: dict) -> dict:
            calls.append((tool_id, arguments))
            return {"status": "committed"}

    result = asyncio.run(
        _resolve_pending(
            Client(),
            SimpleNamespace(
                campaign_id="campaign-1",
                source_on_hit_ruling_json=[
                    {
                        "actor_id": "stirge-1",
                        "weapon_id": "blood-drain",
                        "id": "attachment",
                        "source_excerpt": excerpt,
                    }
                ],
            ),
            "branch-1",
            {
                "pending": [
                    {
                        "id": "choice-2",
                        "kind": "ruling",
                        "actor_id": "target-1",
                        "attacker_id": "stirge-1",
                        "target_id": "target-1",
                        "weapon_id": "blood-drain",
                        "trigger": "attack_on_hit_effect",
                        "effect": excerpt,
                        "status": "pending",
                    }
                ]
            },
        )
    )

    assert result == {"status": "committed"}
    assert calls == [
        (
            "combat_choice",
            {
                "campaign_id": "campaign-1",
                "action": "on_hit_ruling",
                "actor_id": "target-1",
                "payload": {
                    "choice_id": "choice-2",
                    "selection": {
                        "id": "attachment",
                        "source_excerpt": excerpt,
                    },
                },
                "branch_id": "branch-1",
                "expected_revision": 18,
                "idempotency_key": (calls[0][1]["idempotency_key"] if calls else ""),
            },
        )
    ]


def test_interrupted_source_alternative_damage_resumes_with_reviewed_dismissal() -> None:
    calls: list[tuple[str, dict]] = []
    excerpt = "or 11 (2d8 + 2) piercing damage while enlarged."

    class Client:
        async def core(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "campaign_query"
            return {"revision": 19}

        async def domain(self, tool_id: str, arguments: dict) -> dict:
            calls.append((tool_id, arguments))
            return {"status": "committed"}

    result = asyncio.run(
        _resolve_pending(
            Client(),
            SimpleNamespace(
                campaign_id="campaign-1",
                source_on_hit_ruling_json=[
                    {
                        "actor_id": "duergar-1",
                        "weapon_id": "war-pick",
                        "id": "dismiss",
                        "source_excerpt": excerpt,
                    }
                ],
            ),
            "branch-1",
            {
                "pending": [
                    {
                        "id": "choice-3",
                        "kind": "ruling",
                        "actor_id": "target-1",
                        "attacker_id": "duergar-1",
                        "target_id": "target-1",
                        "weapon_id": "war-pick",
                        "trigger": "attack_on_hit_effect",
                        "effect": excerpt,
                        "status": "pending",
                    }
                ]
            },
        )
    )

    assert result == {"status": "committed"}
    assert calls[0][0] == "combat_choice"
    assert calls[0][1]["action"] == "on_hit_ruling"
    assert calls[0][1]["payload"]["selection"] == {
        "id": "dismiss",
        "source_excerpt": excerpt,
    }


def test_source_surrender_requires_threshold_life_no_escape_and_resolved_party() -> None:
    assert _source_surrender_outcome(
        actor_hit_points=8,
        surrender_at_hp=8,
        actor_alive=True,
        no_escape=True,
        unresolved_party=False,
    ) == (
        "surrender",
        "The source-designated hostile surrendered at 8 hit points "
        "(threshold 8) with no avenue of escape.",
    )
    assert (
        _source_surrender_outcome(
            actor_hit_points=8,
            surrender_at_hp=8,
            actor_alive=True,
            no_escape=False,
            unresolved_party=False,
        )
        is None
    )


def test_encounter_manifest_preserves_exact_source_count_without_scaling() -> None:
    hostile_ids = ["goblin-1", "goblin-2", "goblin-3", "goblin-4"]
    manifest = _participant_manifest(
        hostile_ids,
        label="Four goblins",
        source_excerpt="Four goblins are hiding in the woods, two on each side of the road.",
    )

    assert manifest["groups"][0]["required_count"] == 4
    assert manifest["groups"][0]["actor_ids"] == hostile_ids
    assert manifest["notes"] == "Exact source count; no party-size scaling was applied."


def test_primary_hostile_evidence_can_be_narrower_than_encounter_procedure() -> None:
    procedure = (
        "There are twenty defenders. Every breath attack kills 1d4 defenders. "
        "The dragon leaves after taking 24 damage."
    )

    assert (
        _primary_hostile_source_excerpt(
            SimpleNamespace(
                source_excerpt=procedure,
                hostile_source_excerpt=(
                    "The adult blue dragon Lennithon accompanied this raid."
                ),
            )
        )
        == "The adult blue dragon Lennithon accompanied this raid."
    )
    assert (
        _primary_hostile_source_excerpt(SimpleNamespace(source_excerpt=procedure))
        == procedure
    )


def test_encounter_manifest_tracks_arrived_source_group_separately() -> None:
    manifest = _participant_manifest(
        ["klarg", "ripper", "goblin-1", "goblin-2"],
        label="Klarg, Ripper, and two goblins",
        source_excerpt="Klarg shares this cave with Ripper and two goblins.",
        additional_hostile_ids=["messenger"],
        additional_label="Twin-pools messenger",
        additional_source_excerpt="One goblin flees to area 8 to warn Klarg.",
    )

    assert manifest["groups"] == [
        {
            "key": "source-hostiles",
            "label": "Klarg, Ripper, and two goblins",
            "role": "combatant",
            "required_count": 4,
            "actor_ids": ["klarg", "ripper", "goblin-1", "goblin-2"],
            "source_excerpt": "Klarg shares this cave with Ripper and two goblins.",
        },
        {
            "key": "additional-source-hostiles",
            "label": "Twin-pools messenger",
            "role": "combatant",
            "required_count": 1,
            "actor_ids": ["messenger"],
            "source_excerpt": "One goblin flees to area 8 to warn Klarg.",
        },
    ]


def test_encounter_manifest_tracks_delayed_source_reinforcements() -> None:
    manifest = _participant_manifest(
        ["guard", "vhalak"],
        label="Main cavern occupants",
        source_excerpt="One more stands guard in the western half of the cavern.",
        reinforcement_hostile_ids=["rift-1", "rift-2"],
        reinforcement_label="Rift workers",
        reinforcement_source_excerpt=(
            "If a fight breaks out in the main cavern, the two bugbears in "
            "the rift climb up the ropes to join the fray."
        ),
    )

    assert manifest["groups"][1] == {
        "key": "source-reinforcements",
        "label": "Rift workers",
        "role": "reinforcement",
        "required_count": 2,
        "actor_ids": ["rift-1", "rift-2"],
        "source_excerpt": (
            "If a fight breaks out in the main cavern, the two bugbears in "
            "the rift climb up the ropes to join the fray."
        ),
    }


def test_source_reinforcements_enter_openly_at_configured_round_positions() -> None:
    first = _reinforcement_config("rift-1", 0)
    second = _reinforcement_config("rift-2", 1, join_round=7, tie_breaker=8)

    assert first == {
        "position": {"x": 7, "y": 2},
        "disposition": "hostile",
        "hidden": False,
        "surprised": False,
        "death_saves": False,
    }
    assert second["position"] == {"x": 7, "y": 4}
    assert second["join_round"] == 7
    assert second["tie_breaker"] == 8


def test_default_ambush_layout_keeps_two_goblins_thirty_feet_away() -> None:
    party_ids = ["pc-1", "pc-2", "pc-3", "pc-4", "pc-5"]
    hostile_ids = ["goblin-1", "goblin-2", "goblin-3", "goblin-4"]
    config = _participant_config(
        party_ids,
        hostile_ids,
        surprise_by_actor={"pc-1": True},
    )
    by_actor = {item["actor_id"]: item for item in config}

    assert by_actor["pc-1"]["surprised"] is True
    assert by_actor["pc-2"]["surprised"] is False
    assert by_actor["goblin-1"]["position"]["x"] == 2
    assert by_actor["goblin-3"]["position"]["x"] == 7
    assert by_actor["goblin-3"]["hidden"] is True
    assert by_actor["goblin-1"]["surprised"] is False
    surprised_config = _participant_config(
        party_ids,
        hostile_ids,
        surprise_by_actor={"goblin-1": True},
    )
    surprised_by_actor = {item["actor_id"]: item for item in surprised_config}
    assert surprised_by_actor["goblin-1"]["surprised"] is True
    assert surprised_by_actor["goblin-1"]["hidden"] is False
    warned_hidden = _participant_config(
        party_ids,
        hostile_ids,
        surprise_by_actor={actor_id: False for actor_id in [*party_ids, *hostile_ids]},
        hostiles_hidden=True,
    )
    warned_by_actor = {item["actor_id"]: item for item in warned_hidden}
    assert warned_by_actor["goblin-1"]["surprised"] is False
    assert warned_by_actor["goblin-1"]["hidden"] is True


def test_hidden_hostile_visibility_preserves_each_observer_detection() -> None:
    config = _participant_config(
        ["pc-1", "pc-2"],
        ["ruffian-1", "ruffian-2"],
        surprise_by_actor={},
        hostiles_hidden=True,
        visible_to_actor_ids_by_hostile={
            "ruffian-1": ["pc-1", "pc-2"],
            "ruffian-2": [],
        },
    )
    by_actor = {item["actor_id"]: item for item in config}

    assert by_actor["ruffian-1"]["hidden"] is True
    assert by_actor["ruffian-1"]["visible_to_actor_ids"] == ["pc-1", "pc-2"]
    assert by_actor["ruffian-2"]["hidden"] is True
    assert by_actor["ruffian-2"]["visible_to_actor_ids"] == []


def test_mixed_encounter_hides_only_source_selected_hostiles() -> None:
    config = _participant_config(
        ["pc-1"],
        ["spider-1", "bugbear-1"],
        surprise_by_actor={},
        hostiles_hidden=False,
        hidden_actor_ids=["spider-1"],
        visible_to_actor_ids_by_hostile={"spider-1": []},
    )
    by_actor = {item["actor_id"]: item for item in config}

    assert by_actor["spider-1"]["hidden"] is True
    assert by_actor["spider-1"]["visible_to_actor_ids"] == []
    assert by_actor["bugbear-1"]["hidden"] is False
    assert by_actor["bugbear-1"]["visible_to_actor_ids"] is None


def test_source_six_hostile_layout_keeps_every_actor_on_a_unique_space() -> None:
    party_ids = [f"pc-{index}" for index in range(1, 6)]
    hostile_ids = [f"goblin-{index}" for index in range(1, 7)]

    config = _participant_config(party_ids, hostile_ids, surprise_by_actor={})
    positions = [(item["position"]["x"], item["position"]["y"]) for item in config]

    assert len(config) == 11
    assert len(positions) == len(set(positions))
    assert {item["actor_id"] for item in config} == {*party_ids, *hostile_ids}


def test_no_surprise_layout_marks_neither_side_surprised() -> None:
    party_ids = ["pc-1", "pc-2"]
    hostile_ids = ["goblin-1", "goblin-2"]

    config = _participant_config(
        party_ids,
        hostile_ids,
        surprise_by_actor={actor_id: False for actor_id in [*party_ids, *hostile_ids]},
        hostiles_hidden=False,
    )

    assert all(item["surprised"] is False for item in config)
    assert all(item.get("hidden") is False for item in config if item["actor_id"] in hostile_ids)


def test_source_cited_scout_check_prevents_party_surprise(tmp_path) -> None:
    path = tmp_path / "check.json"
    path.write_text(
        json.dumps(
            {
                "action": "resolve-check",
                "campaign_id": "campaign-1",
                "passed": True,
                "result": {
                    "scene": {"scene_id": "scene-1", "location_key": "blind"},
                    "actor": {"id": "pc-1", "name": "Scout"},
                    "check": {"success": True, "natural": 16, "total": 21},
                },
            }
        ),
        encoding="utf-8",
    )

    surprise, basis = _surprise_from_check_report(
        path,
        campaign_id="campaign-1",
        scene_id="scene-1",
        location_key="blind",
        party_ids=["pc-1", "pc-2"],
        hostile_ids=["goblin-1", "goblin-2"],
    )

    assert surprise == {
        "pc-1": False,
        "pc-2": False,
        "goblin-1": False,
        "goblin-2": False,
    }
    assert basis["mode"] == "source_cited_party_scout"


def test_failed_source_cited_scout_check_surprises_only_party(tmp_path) -> None:
    path = tmp_path / "check.json"
    path.write_text(
        json.dumps(
            {
                "action": "resolve-check",
                "campaign_id": "campaign-1",
                "passed": True,
                "result": {
                    "scene": {"scene_id": "scene-1", "location_key": "blind"},
                    "actor": {"id": "pc-1", "name": "Scout"},
                    "check": {"success": False, "natural": 4, "total": 9},
                },
            }
        ),
        encoding="utf-8",
    )

    surprise, basis = _surprise_from_check_report(
        path,
        campaign_id="campaign-1",
        scene_id="scene-1",
        location_key="blind",
        party_ids=["pc-1", "pc-2"],
        hostile_ids=["goblin-1", "goblin-2"],
    )

    assert surprise == {
        "pc-1": True,
        "pc-2": True,
        "goblin-1": False,
        "goblin-2": False,
    }
    assert basis["check"]["success"] is False


def test_complete_party_stealth_can_surprise_shared_passive_hostiles(tmp_path) -> None:
    paths = []
    for actor_id, total in (("pc-1", 14), ("pc-2", 10)):
        path = tmp_path / f"{actor_id}.json"
        path.write_text(
            json.dumps(
                {
                    "action": "resolve-check",
                    "campaign_id": "campaign-1",
                    "passed": True,
                    "result": {
                        "scene": {"scene_id": "scene-1", "location_key": "entrance"},
                        "actor": {"id": actor_id, "name": actor_id},
                        "check": {
                            "success": True,
                            "dc": 10,
                            "natural": total,
                            "total": total,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        paths.append(path)

    surprise, basis = _surprise_from_party_stealth_reports(
        paths,
        campaign_id="campaign-1",
        scene_id="scene-1",
        location_key="entrance",
        party_ids=["pc-1", "pc-2"],
        hostile_ids=["dragonclaw-1", "dragonclaw-2"],
    )

    assert surprise == {
        "pc-1": False,
        "pc-2": False,
        "dragonclaw-1": True,
        "dragonclaw-2": True,
    }
    assert basis["mode"] == "party_stealth_vs_shared_hostile_passive"
    assert basis["passive_perception"] == 10
    assert basis["all_party_hidden"] is True


def test_one_failed_party_stealth_check_prevents_group_surprise(tmp_path) -> None:
    paths = []
    for actor_id, success in (("pc-1", True), ("pc-2", False)):
        path = tmp_path / f"{actor_id}.json"
        path.write_text(
            json.dumps(
                {
                    "action": "resolve-check",
                    "campaign_id": "campaign-1",
                    "passed": True,
                    "result": {
                        "scene": {"scene_id": "scene-1", "location_key": "entrance"},
                        "actor": {"id": actor_id},
                        "check": {"success": success, "dc": 10, "total": 12 if success else 7},
                    },
                }
            ),
            encoding="utf-8",
        )
        paths.append(path)

    surprise, basis = _surprise_from_party_stealth_reports(
        paths,
        campaign_id="campaign-1",
        scene_id="scene-1",
        location_key="entrance",
        party_ids=["pc-1", "pc-2"],
        hostile_ids=["dragonclaw-1"],
    )

    assert surprise == {"pc-1": False, "pc-2": False, "dragonclaw-1": False}
    assert basis["all_party_hidden"] is False


def test_party_stealth_surprise_requires_complete_shared_dc_evidence(tmp_path) -> None:
    path = tmp_path / "pc-1.json"
    path.write_text(
        json.dumps(
            {
                "action": "resolve-check",
                "campaign_id": "campaign-1",
                "passed": True,
                "result": {
                    "scene": {"scene_id": "scene-1", "location_key": "entrance"},
                    "actor": {"id": "pc-1"},
                    "check": {"success": True, "dc": 10, "total": 14},
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="exactly one check report"):
        _surprise_from_party_stealth_reports(
            [path],
            campaign_id="campaign-1",
            scene_id="scene-1",
            location_key="entrance",
            party_ids=["pc-1", "pc-2"],
            hostile_ids=["dragonclaw-1"],
        )


def test_hostile_stealth_uses_every_actor_total_and_ties_are_detected() -> None:
    surprise = _surprise_from_hostile_stealth_totals(
        party_ids=["unaware", "noticed-one", "tied"],
        hostile_ids=["ruffian-1", "ruffian-2"],
        passive_perception={
            "unaware": 10,
            "noticed-one": 12,
            "tied": 17,
        },
        stealth_totals={"ruffian-1": 17, "ruffian-2": 11},
    )

    assert surprise == {
        "unaware": True,
        "noticed-one": False,
        "tied": False,
        "ruffian-1": False,
        "ruffian-2": False,
    }


def test_hostile_stealth_requires_complete_party_and_hostile_evidence() -> None:
    try:
        _surprise_from_hostile_stealth_totals(
            party_ids=["pc-1"],
            hostile_ids=["ruffian-1", "ruffian-2"],
            passive_perception={"pc-1": 12},
            stealth_totals={"ruffian-1": 15},
        )
    except ValueError as exc:
        assert str(exc) == "Stealth totals must be available for every source hostile"
    else:
        raise AssertionError("missing hostile Stealth evidence must be rejected")


def test_movement_destination_stops_next_to_target_without_sharing_space() -> None:
    combat = {
        "battle_map": {"bounds": {"width_cells": 12, "height_cells": 12}},
        "combatants": [
            {
                "actor_id": "pc",
                "position": {"x": 1, "y": 1},
                "turn_budget": {"movement": 30},
            },
            {
                "actor_id": "goblin",
                "position": {"x": 7, "y": 2},
                "turn_budget": {"movement": 30},
            },
        ],
    }

    destination = _choose_destination(combat, "pc", "goblin")

    assert destination is not None
    assert destination[0] != {"x": 7, "y": 2}
    assert (
        max(
            abs(destination[0]["x"] - 7),
            abs(destination[0]["y"] - 2),
        )
        == 1
    )
    assert destination[1] <= 30


def test_movement_destination_never_approaches_a_visible_fear_source() -> None:
    combat = {
        "battle_map": {
            "bounds": {"width_cells": 12, "height_cells": 12},
            "blocked_cells": [],
            "difficult_cells": [],
        },
        "combatants": [
            {
                "actor_id": "frightened-pc",
                "position": {"x": 1, "y": 3},
                "conditions": ["frightened"],
                "condition_sources": {"frightened": ["gazer"]},
                "turn_budget": {"movement": 25},
            },
            {
                "actor_id": "gazer",
                "position": {"x": 7, "y": 2},
                "conditions": [],
                "hidden": False,
                "turn_budget": {"movement": 30},
            },
        ],
    }

    assert _choose_destination(combat, "frightened-pc", "gazer") is None


def test_movement_destination_excludes_blocked_cells_but_not_dead_occupants() -> None:
    combat = {
        "battle_map": {
            "bounds": {"width_cells": 12, "height_cells": 12},
            "blocked_cells": ["6,2"],
            "difficult_cells": [],
        },
        "combatants": [
            {
                "actor_id": "pc",
                "position": {"x": 1, "y": 1},
                "conditions": [],
                "turn_budget": {"movement": 30},
            },
            {
                "actor_id": "goblin",
                "position": {"x": 7, "y": 2},
                "conditions": [],
                "turn_budget": {"movement": 30},
            },
            {
                "actor_id": "dead-guard",
                "position": {"x": 6, "y": 1},
                "conditions": ["dead", "prone"],
                "turn_budget": {"movement": 0},
            },
        ],
    }

    destination = _choose_destination(combat, "pc", "goblin")

    assert destination is not None
    assert destination[0] == {"x": 6, "y": 1}
    assert destination[1] == 25
    assert destination[2][-1] == destination[0]


def test_movement_path_routes_around_source_known_hazard_cells() -> None:
    combat = {
        "battle_map": {
            "bounds": {"width_cells": 12, "height_cells": 12},
            "blocked_cells": [],
            "difficult_cells": [],
        },
        "combatants": [
            {
                "actor_id": "devourer",
                "position": {"x": 8, "y": 6},
                "conditions": [],
                "turn_budget": {"movement": 30},
            },
            {
                "actor_id": "pc",
                "position": {"x": 1, "y": 0},
                "conditions": [],
                "turn_budget": {"movement": 30},
            },
        ],
    }

    destination = _choose_destination(
        combat,
        "devourer",
        "pc",
        avoided_cells={"5,3"},
    )

    assert destination is not None
    assert destination[1] <= 30
    assert "5,3" not in {f"{cell['x']},{cell['y']}" for cell in destination[2]}
    assert destination[2][-1] == destination[0]


def test_roll_total_accepts_public_facade_and_raw_shapes() -> None:
    assert _roll_total({"total": 8, "rolls": [2]}) == 8
    assert _roll_total({"result": {"total": 14}}) == 14


def test_mixed_source_hostiles_accept_their_own_reviewed_attacks() -> None:
    _validate_hostile_attacks(
        "wolf",
        [
            {
                "item_id": "bite",
                "attack_type": "melee",
                "on_hit_effect": "DC 11 Strength save or knocked prone.",
            }
        ],
        required_weapon_ids=[],
    )
    _validate_hostile_attacks(
        "bugbear",
        [
            {"item_id": "morningstar", "attack_type": "melee"},
            {"item_id": "javelin", "attack_type": "ranged"},
        ],
        required_weapon_ids=["morningstar", "javelin"],
    )


def test_required_hostile_attack_still_rejects_incomplete_statblock() -> None:
    try:
        _validate_hostile_attacks(
            "goblin",
            [{"item_id": "scimitar", "attack_type": "melee"}],
            required_weapon_ids=["scimitar", "shortbow"],
        )
    except RuntimeError as error:
        assert "shortbow" in str(error)
    else:
        raise AssertionError("incomplete reviewed statblock was accepted")


def test_hostile_weapon_preference_is_capability_based() -> None:
    wolf = {
        "derived": {
            "inventory": {
                "weapon_attacks": [
                    {"item_id": "bite", "attack_type": "melee"},
                ]
            }
        }
    }
    goblin = {
        "derived": {
            "inventory": {
                "weapon_attacks": [
                    {"item_id": "scimitar", "attack_type": "melee"},
                    {"item_id": "shortbow", "attack_type": "ranged"},
                ]
            }
        }
    }

    assert _preferred_hostile_weapon_id(wolf, hostile_index=1) == "bite"
    assert _preferred_hostile_weapon_id(goblin, hostile_index=0) == "scimitar"
    assert _preferred_hostile_weapon_id(goblin, hostile_index=2) == "shortbow"


def test_hostile_multiattack_selection_follows_the_preferred_weapon() -> None:
    actor = {
        "derived": {
            "multiattack_options": [
                {
                    "id": "melee",
                    "attacks": [{"weapon_id": "shortsword", "attack_mode": "melee", "count": 2}],
                },
                {
                    "id": "ranged",
                    "attacks": [{"weapon_id": "shortbow", "attack_mode": "ranged", "count": 2}],
                },
            ]
        }
    }

    assert _preferred_multiattack_option_id(actor, preferred_weapon_id="shortsword") == "melee"
    assert _preferred_multiattack_option_id(actor, preferred_weapon_id="shortbow") == "ranged"

    actor["derived"]["multiattack_options"] = [
        {
            "id": "mixed-special-action",
            "attacks": [{"weapon_id": "claws", "attack_mode": "melee", "count": 1}],
            "activities": [{"activity_id": "devour-intellect-action", "count": 1}],
        }
    ]
    assert (
        _preferred_multiattack_option_id(actor, preferred_weapon_id="claws")
        == "mixed-special-action"
    )


def test_conscious_party_member_stabilizes_after_all_hostiles_are_resolved() -> None:
    actors = {
        "helper": {
            "sheet": {
                "combat": {"hp": {"value": 5, "max": 8}},
                "conditions": [],
            }
        },
        "dying": {
            "sheet": {
                "combat": {"hp": {"value": 0, "max": 8}},
                "conditions": ["prone", "unconscious"],
            }
        },
    }

    assert (
        _postcombat_stabilization_target(
            actor_id="helper",
            party_ids=["helper", "dying"],
            actors=actors,
            defeated_hostiles=2,
            fled_hostiles=0,
            hostile_count=2,
        )
        == "dying"
    )
    assert (
        _postcombat_stabilization_target(
            actor_id="helper",
            party_ids=["helper", "dying"],
            actors=actors,
            defeated_hostiles=1,
            fled_hostiles=0,
            hostile_count=2,
        )
        is None
    )
    actors["dying"]["sheet"]["conditions"].append("stable")
    assert (
        _postcombat_stabilization_target(
            actor_id="helper",
            party_ids=["helper", "dying"],
            actors=actors,
            defeated_hostiles=2,
            fled_hostiles=0,
            hostile_count=2,
        )
        is None
    )


def test_source_surrender_can_follow_a_source_hostile_defeat() -> None:
    assert _source_surrender_outcome(
        actor_hit_points=4,
        surrender_at_hp=0,
        defeated_hostiles=1,
        surrender_after_defeated=1,
        actor_alive=True,
        no_escape=True,
        unresolved_party=False,
    ) == (
        "surrender",
        "After 1 source-defined hostiles were defeated, the source-designated "
        "survivor surrendered with no avenue of escape.",
    )
    assert (
        _source_surrender_outcome(
            actor_hit_points=4,
            surrender_at_hp=0,
            defeated_hostiles=0,
            surrender_after_defeated=1,
            actor_alive=True,
            no_escape=True,
            unresolved_party=False,
        )
        is None
    )


def test_structured_multiattack_followup_prevents_early_end_turn() -> None:
    active = {
        "combatants": [
            {
                "actor_id": "ruffian",
                "turn_budget": {"attack_budget": 1},
                "turn_flags": {
                    "multiattack": {
                        "option_id": "melee",
                        "remaining": [
                            {
                                "weapon_id": "shortsword",
                                "attack_mode": "melee",
                                "count": 1,
                            }
                        ],
                    }
                },
            }
        ]
    }

    assert _has_multiattack_followup(active, "ruffian")
    active["combatants"][0]["turn_budget"]["attack_budget"] = 0
    assert not _has_multiattack_followup(active, "ruffian")
