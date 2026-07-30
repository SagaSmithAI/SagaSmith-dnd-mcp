import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scripts.regression_encounter import (
    GUIDING_BOLT_ID,
    GUIDING_BOLT_ON_HIT,
    HEALING_WORD_ID,
    HYPNOTIC_PATTERN_ID,
    MAGIC_MISSILE_ID,
    EncounterRulingRequiredError,
    _agent_attack_contexts,
    _agent_casting_perception_rulings,
    _agent_common_action_priorities,
    _agent_death_trigger_rulings,
    _agent_object_interactions,
    _agent_party_absences,
    _agent_positions,
    _agent_reinforcement_triggers,
    _agent_spell_priorities,
    _agent_target_priorities,
    _agent_target_reaction_contexts,
    _agent_turn_rulings,
    _agent_turn_transaction_token,
    _agent_weapon_priorities,
    _apply_agent_positions,
    _apply_party_loadouts,
    _apply_source_casualty_rolls,
    _apply_source_separations,
    _area_spell_target_ids,
    _body_thief_sides,
    _body_thief_target_ids,
    _captured_hostile_ids,
    _character_summary,
    _characters,
    _choose_agent_spell,
    _choose_destination,
    _completed_agent_turn_combat_outcome,
    _completed_source_opening_weapon_actor_ids,
    _consume_agent_forced_target,
    _consume_agent_target_reaction,
    _defense_selection,
    _destination_within_range,
    _encounter_actor_groups,
    _encounter_battle_map_request,
    _encounter_operation_scope,
    _encounter_start_operation_token,
    _has_action_budget,
    _has_blocking_pending,
    _has_multiattack_followup,
    _knockout_objective,
    _missing_source_reinforcement_ids,
    _movement_operation_token,
    _observable_target_ids,
    _operation_token,
    _participant_config,
    _participant_manifest,
    _party_ids,
    _party_loadouts,
    _pending_agent_forced_targets,
    _pending_resolution_made_progress,
    _postcombat_stabilization_target,
    _postcombat_unavailable_grapple_effect_ids,
    _preflight_attack,
    _prepared_actor_ids,
    _primary_hostile_source_excerpt,
    _prioritize_targets,
    _reaction_available_actor_ids,
    _ready_immediate_source_flee_actor_ids,
    _ready_linked_source_flee_actor_ids,
    _record_source_flee_damage,
    _reinforcement_config,
    _require_committed_encounter_start,
    _require_encounter_readiness,
    _require_live_active_party,
    _require_pending_on_hit_choice_id,
    _required_source_opening_weapon,
    _resolve_pending,
    _roll_total,
    _safe_single_target_spell_declaration,
    _selected_prepared_actor_ids,
    _settle_agent_turn_ruling,
    _settle_source_casualty_pool_turn,
    _should_stand,
    _source_ammunition_selections,
    _source_attack_environments,
    _source_avoidances,
    _source_casualty_pools,
    _source_contest_activities,
    _source_declared_conditions,
    _source_declared_surprise,
    _source_delayed_actions,
    _source_departure_patch,
    _source_extra_damage_action_rulings,
    _source_extra_damage_history,
    _source_extra_damage_rulings,
    _source_extra_damage_turn_history,
    _source_flee_damage_history,
    _source_flee_ready,
    _source_on_hit_rulings,
    _source_opening_casts,
    _source_opening_weapons,
    _source_outcome,
    _source_outcome_allows_checkpoint,
    _source_passive_allies,
    _source_precombat_casts,
    _source_random_activities,
    _source_save_activities,
    _source_separation_target,
    _source_separations,
    _source_surprise_evidence_from_report,
    _source_surrender_outcome,
    _source_target_priorities,
    _source_traits,
    _source_truce_outcome,
    _source_zero_hp_finisher,
    _source_zero_hp_finisher_stage,
    _source_zero_hp_stabilization,
    _spell_cast_blocks_turn_progress,
    _start_or_resume_auto_run,
    _status,
    _surprise_from_check_report,
    _surprise_from_hostile_stealth_totals,
    _surprise_from_party_stealth_reports,
    _validate_agent_target_refinements,
    _validate_hostile_attacks,
    _validate_source_flee_configuration,
    _wound_priority,
)

PERYTON_DIVE_ATTACK = (
    "If the peryton is flying and dives at least 30 feet straight toward a target "
    "and then hits it with a melee weapon attack, the attack deals an extra 9 "
    "(2d8) damage to the target."
)


def test_postcombat_cleanup_detects_only_unavailable_source_grapples() -> None:
    combat = {
        "combatants": [
            {"actor_id": "dead-source", "conditions": ["dead", "prone"]},
            {
                "actor_id": "departed-source",
                "conditions": [],
                "departed": {"reason": "The source fled."},
            },
            {"actor_id": "present-source", "conditions": []},
        ],
        "ongoing_effects": [
            {
                "id": "dead-grapple",
                "kind": "on_hit_condition",
                "condition": "grappled",
                "source_actor_id": "dead-source",
                "active": True,
            },
            {
                "id": "departed-grapple",
                "kind": "on_hit_condition",
                "condition": "grappled",
                "source_actor_id": "departed-source",
                "active": True,
            },
            {
                "id": "present-grapple",
                "kind": "on_hit_condition",
                "condition": "grappled",
                "source_actor_id": "present-source",
                "active": True,
            },
            {
                "id": "independent-web",
                "kind": "on_hit_condition",
                "condition": "restrained",
                "source_actor_id": "dead-source",
                "active": True,
            },
        ],
    }

    assert _postcombat_unavailable_grapple_effect_ids(combat) == [
        "dead-grapple",
        "departed-grapple",
    ]


def test_encounter_readiness_rejects_source_participants_before_other_calls() -> None:
    calls: list[tuple[str, dict]] = []
    manifest = {
        "schema_version": 1,
        "groups": [
            {
                "key": "source-hostiles",
                "label": "Three ettercaps",
                "role": "combatant",
                "required_count": 3,
                "actor_ids": ["ettercap-1", "ettercap-2", "ettercap-3"],
                "source_excerpt": "Three ettercaps attack with web garrotes.",
            }
        ],
    }

    class Client:
        async def domain(self, tool_id: str, arguments: dict) -> dict:
            calls.append((tool_id, arguments))
            return {
                "ready": False,
                "groups": [
                    {
                        "key": "source-hostiles",
                        "missing_count": 0,
                        "issues": ["source excerpt was not found in the scene"],
                    }
                ],
            }

    with pytest.raises(RuntimeError, match="readiness failed before mutation"):
        asyncio.run(
            _require_encounter_readiness(
                Client(),
                campaign_id="campaign-1",
                scene_id="scene-1",
                participant_manifest=manifest,
            )
        )

    assert calls == [
        (
            "module_query",
            {
                "campaign_id": "campaign-1",
                "view": "readiness",
                "payload": {
                    "scene_id": "scene-1",
                    "participant_manifest": manifest,
                },
            },
        )
    ]


def test_encounter_readiness_reports_unready_actor_blockers() -> None:
    manifest = {
        "schema_version": 1,
        "groups": [
            {
                "key": "source-hostiles",
                "label": "One mage",
                "role": "combatant",
                "required_count": 1,
                "actor_ids": ["mage-1"],
                "source_excerpt": "One mage attacks.",
            }
        ],
    }

    class Client:
        async def domain(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "module_query"
            return {
                "ready": False,
                "groups": [
                    {
                        "key": "source-hostiles",
                        "missing_count": 0,
                        "unready_count": 1,
                        "unready_actor_ids": ["mage-1"],
                        "actors": [
                            {
                                "id": "mage-1",
                                "combat_card": {
                                    "blocking_reasons": ["missing_attack_range"]
                                },
                            }
                        ],
                    }
                ],
            }

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(
            _require_encounter_readiness(
                Client(),
                campaign_id="campaign-1",
                scene_id="scene-1",
                participant_manifest=manifest,
            )
        )

    message = str(raised.value)
    assert "'unready_count': 1" in message
    assert "'unready_actor_ids': ['mage-1']" in message
    assert "'mage-1': ['missing_attack_range']" in message


def test_agent_attack_contexts_bind_source_and_attack_mode() -> None:
    excerpt = (
        "Clever characters can lure the dragon into a narrow tunnel where it is "
        "unable to maneuver effectively. Under such circumstances, the dragon has "
        "disadvantage on its melee attacks."
    )
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "content_sha256": "a" * 64,
    }

    contexts = _agent_attack_contexts(
        [
            {
                "actor_id": "dragon-1",
                "attack_mode": "melee",
                "advantage": False,
                "disadvantage": True,
                "source_ref": source_ref,
                "source_excerpt": excerpt,
                "decision": "The party lures the dragon into the narrow tunnel.",
                "ruling_reason": "The cited room procedure explicitly applies here.",
            }
        ],
        participant_ids=["dragon-1", "pc-1"],
        scene_id="scene-1",
        encounter_source_excerpt=excerpt,
    )

    ruling = contexts[("dragon-1", "", "melee")]
    assert ruling["context"]["disadvantage"] is True
    assert ruling["context"]["disadvantage_sources"] == [
        f"agent-ruling:{ruling['application_id']}"
    ]
    assert ruling["agent_ruling"]["source_ref"] == source_ref
    assert ruling["agent_ruling"]["ruling_kind"] == "source_or_scene_fact"
    assert ruling["context"]["agent_ruling"] == ruling["agent_ruling"]


def test_agent_attack_contexts_reject_unbound_or_ambiguous_modifier() -> None:
    with pytest.raises(ValueError, match="unambiguous advantage state"):
        _agent_attack_contexts(
            [
                {
                    "actor_id": "dragon-1",
                    "attack_mode": "melee",
                    "advantage": False,
                    "disadvantage": False,
                    "source_ref": {
                        "module_id": "module-1",
                        "scene_id": "other-scene",
                        "chunk_id": "chunk-1",
                        "content_sha256": "a" * 64,
                    },
                    "source_excerpt": "not in the encounter",
                    "decision": "The attack has no contextual modifier.",
                    "ruling_reason": "This should be rejected before combat begins.",
                }
            ],
            participant_ids=["dragon-1"],
            scene_id="scene-1",
            encounter_source_excerpt="The actual encounter excerpt.",
        )


def test_agent_attack_contexts_bind_rules_cover_to_one_target() -> None:
    excerpt = (
        "Longo and Yek are in the rafters and enjoy half cover against ranged "
        "attacks made from below."
    )
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "content_sha256": "a" * 64,
    }

    contexts = _agent_attack_contexts(
        [
            {
                "actor_id": "archer-1",
                "target_id": "yek-1",
                "attack_mode": "ranged",
                "cover": "half",
                "source_ref": source_ref,
                "source_excerpt": excerpt,
                "decision": (
                    "Yek remains in the rafters above this archer and receives "
                    "the printed half cover."
                ),
                "ruling_reason": (
                    "The current positions satisfy the source restriction that "
                    "the ranged attack is made from below."
                ),
            }
        ],
        participant_ids=["archer-1", "yek-1"],
        scene_id="scene-1",
        encounter_source_excerpt=excerpt,
    )

    ruling = contexts[("archer-1", "yek-1", "ranged")]
    assert ruling["context"]["cover"] == {"degree": "half"}
    assert ruling["context"]["agent_ruling"]["source_ref"] == source_ref
    assert ruling["target_id"] == "yek-1"

    with pytest.raises(ValueError, match="distinct target"):
        _agent_attack_contexts(
            [
                {
                    "actor_id": "archer-1",
                    "attack_mode": "ranged",
                    "cover": "half",
                    "source_ref": source_ref,
                    "source_excerpt": excerpt,
                    "decision": "The target is claimed to have half cover here.",
                    "ruling_reason": "This lacks the required target relationship.",
                }
            ],
            participant_ids=["archer-1", "yek-1"],
            scene_id="scene-1",
            encounter_source_excerpt=excerpt,
        )


def test_agent_target_reaction_contexts_bind_source_and_target() -> None:
    excerpt = (
        "When targeted by a melee attack, the tile creature can take a reaction "
        "to turn its narrowest aspect toward the attacker. The attacker has "
        "disadvantage on the attack roll."
    )
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "content_sha256": "a" * 64,
    }

    contexts = _agent_target_reaction_contexts(
        [
            {
                "actor_id": "tile-creature",
                "attack_mode": "melee",
                "advantage": False,
                "disadvantage": True,
                "source_ref": source_ref,
                "source_excerpt": excerpt,
                "decision": "The tile creature uses its reaction against this attack.",
                "ruling_reason": "The cited trait triggers when this melee attack targets it.",
            }
        ],
        participant_ids=["tile-creature", "pc-1"],
        scene_id="scene-1",
        encounter_source_excerpt=excerpt,
    )

    ruling = contexts[("tile-creature", "melee")]
    assert ruling["actor_id"] == "tile-creature"
    assert ruling["context"]["disadvantage"] is True
    assert ruling["context"]["disadvantage_sources"] == [
        f"agent-ruling:{ruling['application_id']}"
    ]
    assert ruling["agent_ruling"]["source_ref"] == source_ref


def test_agent_turn_rulings_reject_descriptive_passive_as_combat_action() -> None:
    actor_excerpt = (
        "The creature can innately cast suggestion (save DC 13), requiring no "
        "material components."
    )
    encounter_excerpt = (
        "The creature uses suggestion to tell a character that a fellow party "
        "member is a spy and should be attacked."
    )
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "content_sha256": "a" * 64,
    }
    actors = {
        "caster": {
            "sheet": {
                "content": {
                    "features": [
                        {
                            "id": "innate-spellcasting",
                            "description": actor_excerpt,
                            "choices": {
                                "manual_ruling": {
                                    "kind": "descriptive_passive",
                                    "default_resolver": "agent",
                                    "source_excerpt": actor_excerpt,
                                }
                            },
                        }
                    ]
                }
            }
        }
    }

    with pytest.raises(ValueError, match="cannot activate a descriptive passive"):
        _agent_turn_rulings(
            [
                {
                    "actor_id": "caster",
                    "feature_id": "innate-spellcasting",
                    "round": 1,
                    "source_ref": source_ref,
                    "actor_source_excerpt": actor_excerpt,
                    "encounter_source_excerpt": encounter_excerpt,
                    "decision": "The caster uses the reviewed feature on the scout.",
                    "ruling_reason": "The cited encounter explicitly selects this tactic.",
                    "target_id": "scout",
                    "save_ability": "wisdom",
                    "save_dc": 13,
                    "success_outcome": "The scout recognizes and rejects the compulsion.",
                    "failure_outcome": "The scout attacks the named ally once.",
                    "forced_target_id": "ally",
                    "ends_if_source_incapacitated": True,
                }
            ],
            participant_ids=["caster", "scout", "ally"],
            actors=actors,
            scene_id="scene-1",
            encounter_source_excerpt=encounter_excerpt,
        )


def test_agent_turn_rulings_bind_reviewed_area_save_damage() -> None:
    actor_excerpt = (
        "The dragon exhales poisonous gas in a 60-foot cone. Each creature in "
        "that area must make a DC 18 Constitution saving throw, taking 56 "
        "(16d6) poison damage on a failed save, or half as much damage on a "
        "successful one."
    )
    encounter_excerpt = (
        "If Chuth is encountered here, he uses his breath weapon, legendary "
        "actions, and lair actions indiscriminately."
    )
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "content_sha256": "a" * 64,
    }
    actors = {
        "dragon": {
            "sheet": {
                "content": {
                    "activities": [
                        {
                            "id": "poison-breath-activity",
                            "description": actor_excerpt,
                            "choices": {
                                "manual_ruling": {
                                    "kind": "descriptive_activity",
                                    "default_resolver": "agent",
                                    "source_excerpt": actor_excerpt,
                                }
                            },
                        }
                    ]
                }
            }
        }
    }

    rulings = _agent_turn_rulings(
        [
            {
                "actor_id": "dragon",
                "activity_id": "poison-breath-activity",
                "round": 1,
                "source_ref": source_ref,
                "actor_source_excerpt": actor_excerpt,
                "encounter_source_excerpt": encounter_excerpt,
                "decision": "Chuth catches both clustered intruders in the cone.",
                "ruling_reason": "The cited lair tactic directs indiscriminate breath use.",
                "target_ids": ["pc-1", "pc-2"],
                "save_ability": "constitution",
                "save_dc": 18,
                "success_outcome": "The target takes half of the shared damage roll.",
                "failure_outcome": "The target takes the full shared damage roll.",
                "damage_expression": "16d6",
                "damage_type": "poison",
                "half_on_success": True,
            }
        ],
        participant_ids=["dragon", "pc-1", "pc-2"],
        actors=actors,
        scene_id="scene-1",
        encounter_source_excerpt=encounter_excerpt,
    )

    ruling = rulings[("dragon", 1)]
    assert ruling["target_ids"] == ["pc-1", "pc-2"]
    assert ruling["save"]["damage"] == {
        "expression": "16d6",
        "damage_type": "poison",
        "half_on_success": True,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("save_ability", "wisdom"),
        ("save_dc", 17),
        ("half_on_success", False),
    ],
)
def test_agent_turn_rulings_reject_area_save_damage_not_matching_source(
    field: str,
    value: object,
) -> None:
    actor_excerpt = (
        "The dragon exhales poisonous gas in a 60-foot cone. Each creature in "
        "that area must make a DC 18 Constitution saving throw, taking 56 "
        "(16d6) poison damage on a failed save, or half as much damage on a "
        "successful one."
    )
    encounter_excerpt = "The dragon uses its breath weapon."
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "content_sha256": "a" * 64,
    }
    actors = {
        "dragon": {
            "sheet": {
                "content": {
                    "activities": [
                        {
                            "id": "poison-breath-activity",
                            "description": actor_excerpt,
                            "choices": {
                                "manual_ruling": {
                                    "kind": "descriptive_activity",
                                    "default_resolver": "agent",
                                    "source_excerpt": actor_excerpt,
                                }
                            },
                        }
                    ]
                }
            }
        }
    }
    declaration: dict[str, object] = {
        "actor_id": "dragon",
        "activity_id": "poison-breath-activity",
        "round": 1,
        "source_ref": source_ref,
        "actor_source_excerpt": actor_excerpt,
        "encounter_source_excerpt": encounter_excerpt,
        "decision": "The dragon catches both clustered intruders in the cone.",
        "ruling_reason": "The cited encounter directs this breath attack.",
        "target_ids": ["pc-1", "pc-2"],
        "save_ability": "constitution",
        "save_dc": 18,
        "success_outcome": "Half damage.",
        "failure_outcome": "Full damage.",
        "damage_expression": "16d6",
        "damage_type": "poison",
        "half_on_success": True,
    }
    declaration[field] = value

    with pytest.raises(ValueError, match="must match the reviewed descriptive card"):
        _agent_turn_rulings(
            [declaration],
            participant_ids=["dragon", "pc-1", "pc-2"],
            actors=actors,
            scene_id="scene-1",
            encounter_source_excerpt=encounter_excerpt,
        )


def test_agent_turn_rulings_bind_innate_spell_resource_and_concentration() -> None:
    actor_excerpt = (
        "The creature can innately cast suggestion 3/day (save DC 13), "
        "requiring no material components."
    )
    encounter_excerpt = (
        "The creature uses suggestion to tell a character that a fellow party "
        "member is a spy and should be attacked."
    )
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "content_sha256": "a" * 64,
    }
    actors = {
        "caster": {
            "sheet": {
                "content": {
                    "features": [
                        {
                            "id": "innate-spellcasting",
                            "name": "Innate Spellcasting",
                            "description": actor_excerpt,
                        }
                    ],
                    "spells": [
                        {
                            "id": "suggestion",
                            "name": "Suggestion",
                            "grant": {"method": "innate"},
                            "access": {
                                "known": True,
                                "always_prepared": True,
                                "at_will": False,
                            },
                            "definition": {
                                "duration": {"concentration": True}
                            },
                        }
                    ],
                }
            }
        }
    }

    rulings = _agent_turn_rulings(
        [
            {
                "actor_id": "caster",
                "spell_id": "suggestion",
                "round": 1,
                "source_ref": source_ref,
                "actor_source_excerpt": actor_excerpt,
                "encounter_source_excerpt": encounter_excerpt,
                "decision": "The caster innately casts Suggestion on the scout.",
                "ruling_reason": "The cited encounter explicitly selects this tactic.",
                "target_id": "scout",
                "save_ability": "wisdom",
                "save_dc": 13,
                "success_outcome": "The scout recognizes and rejects the compulsion.",
                "failure_outcome": "The scout attacks the named ally once.",
                "forced_target_id": "ally",
                "ends_if_source_incapacitated": True,
            }
        ],
        participant_ids=["caster", "scout", "ally"],
        actors=actors,
        scene_id="scene-1",
        encounter_source_excerpt=encounter_excerpt,
    )

    ruling = rulings[("caster", 1)]
    assert ruling["spell_id"] == "suggestion"
    assert ruling["spell_payment_economies"] == ["innate_spell"]
    assert ruling["concentration_required"] is True


def test_agent_turn_rulings_bind_source_cited_scene_procedure() -> None:
    procedure_excerpt = (
        "Each round, at least five Red Wizards must use an action to perform "
        "the ritual in order for it to be successfully focused for that round."
    )
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "content_sha256": "a" * 64,
    }

    rulings = _agent_turn_rulings(
        [
            {
                "actor_id": "red-wizard",
                "procedure_id": "tiamat-ritual-focus",
                "round": 1,
                "source_ref": source_ref,
                "procedure_source_excerpt": procedure_excerpt,
                "encounter_source_excerpt": procedure_excerpt,
                "decision": "The Red Wizard spends this turn focusing the portal.",
                "ruling_reason": (
                    "The cited encounter procedure requires Red Wizard actions "
                    "to accumulate successful ritual rounds."
                ),
            }
        ],
        participant_ids=["red-wizard"],
        actors={"red-wizard": {"sheet": {"content": {}}}},
        scene_id="scene-1",
        encounter_source_excerpt=procedure_excerpt,
    )

    ruling = rulings[("red-wizard", 1)]
    assert ruling["procedure_id"] == "tiamat-ritual-focus"
    assert ruling["spell_payment_economies"] == []
    assert ruling["agent_ruling"]["procedure_source_excerpt"] == procedure_excerpt


def test_agent_turn_rulings_bind_source_cited_action_check_and_truce() -> None:
    procedure_excerpt = (
        "A character can use an action to try to persuade the king to stand down. "
        "That character must succeed on a DC 18 Charisma (Persuasion) check to "
        "calm down the king. If the character mentions Serissa by name, the check "
        "is made with advantage."
    )
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "content_sha256": "a" * 64,
    }

    rulings = _agent_turn_rulings(
        [
            {
                "actor_id": "bard",
                "procedure_id": "calm-hostile-king",
                "round": 1,
                "source_ref": source_ref,
                "procedure_source_excerpt": procedure_excerpt,
                "encounter_source_excerpt": procedure_excerpt,
                "decision": (
                    "The bard names Serissa and asks the king to stand down."
                ),
                "ruling_reason": (
                    "The exact scene procedure permits this action and grants "
                    "advantage when Serissa is named."
                ),
                "check_ability": "persuasion",
                "check_dc": 18,
                "check_action": "improvise",
                "check_advantage": True,
                "success_outcome": "The hostile king calms down.",
                "failure_outcome": "The hostile king remains uncontrolled.",
                "success_combat_outcome": {
                    "status": "truce",
                    "summary": "The party calmed the king by invoking Serissa.",
                },
            }
        ],
        participant_ids=["bard", "king"],
        actors={
            "bard": {"sheet": {"content": {}}},
            "king": {"sheet": {"content": {}}},
        },
        scene_id="scene-1",
        encounter_source_excerpt=procedure_excerpt,
    )

    ruling = rulings[("bard", 1)]
    assert ruling["check"] == {
        "ability": "persuasion",
        "dc": 18,
        "action": "improvise",
        "advantage": True,
        "disadvantage": False,
        "success_outcome": "The hostile king calms down.",
        "failure_outcome": "The hostile king remains uncontrolled.",
        "success_combat_outcome": {
            "status": "truce",
            "summary": "The party calmed the king by invoking Serissa.",
        },
    }


def test_agent_turn_rulings_reject_cross_edition_check_action() -> None:
    procedure_excerpt = (
        "A character can use an action to try to persuade the king to stand down. "
        "That character must succeed on a DC 18 Charisma (Persuasion) check."
    )
    declaration = {
        "actor_id": "bard",
        "procedure_id": "calm-hostile-king",
        "round": 1,
        "source_ref": {
            "module_id": "module-1",
            "scene_id": "scene-1",
            "chunk_id": "chunk-1",
            "content_sha256": "a" * 64,
        },
        "procedure_source_excerpt": procedure_excerpt,
        "encounter_source_excerpt": procedure_excerpt,
        "decision": "The bard asks the hostile king to stand down.",
        "ruling_reason": (
            "The exact scene procedure permits a persuasion action."
        ),
        "check_ability": "persuasion",
        "check_dc": 18,
        "check_action": "influence",
        "success_outcome": "The hostile king calms down.",
        "failure_outcome": "The hostile king remains uncontrolled.",
    }
    arguments = {
        "participant_ids": ["bard", "king"],
        "actors": {
            "bard": {"sheet": {"content": {}}},
            "king": {"sheet": {"content": {}}},
        },
        "scene_id": "scene-1",
        "encounter_source_excerpt": procedure_excerpt,
    }

    with pytest.raises(
        ValueError,
        match="check_action='influence' is not a legal 2014 action primitive",
    ):
        _agent_turn_rulings([declaration], ruleset="2014", **arguments)

    ruling = _agent_turn_rulings(
        [declaration],
        ruleset="2024",
        **arguments,
    )[("bard", 1)]
    assert ruling["check"]["action"] == "influence"


def test_agent_turn_rulings_bind_unstructured_prepared_spell() -> None:
    spell_excerpt = (
        "You create a seismic disturbance at a point on the ground that you "
        "can see within range."
    )
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "content_sha256": "a" * 64,
    }
    encounter_excerpt = (
        "Severe damage is inflicted to the Temple of Tiamat, as from an "
        "earthquake spell."
    )
    actors = {
        "cleric": {
            "sheet": {
                "content": {
                    "spells": [
                        {
                            "id": "earthquake",
                            "level": 8,
                            "grant": {"method": "class_prepared"},
                            "access": {"prepared": True},
                            "definition": {
                                "effect": spell_excerpt,
                                "duration": {"concentration": True},
                            },
                        }
                    ]
                }
            }
        }
    }

    rulings = _agent_turn_rulings(
        [
            {
                "actor_id": "cleric",
                "spell_id": "earthquake",
                "round": 1,
                "source_ref": source_ref,
                "actor_source_excerpt": spell_excerpt,
                "encounter_source_excerpt": encounter_excerpt,
                "decision": "The cleric centers Earthquake on the ritual temple.",
                "ruling_reason": (
                    "The module explicitly identifies this spell as severe "
                    "temple damage that disrupts the summoning."
                ),
            }
        ],
        participant_ids=["cleric"],
        actors=actors,
        scene_id="scene-1",
        encounter_source_excerpt=encounter_excerpt,
    )

    ruling = rulings[("cleric", 1)]
    assert ruling["spell_payment_economies"] == ["slots", "pact_magic"]
    assert ruling["concentration_required"] is True


def test_agent_turn_ruling_pays_action_rolls_save_and_persists_world_patch() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def domain(self, tool_id: str, arguments: dict) -> dict:
            self.calls.append((tool_id, arguments))
            if tool_id == "combat_common_action":
                return {"status": "committed", "combat": {}}
            if tool_id == "combat_check":
                return {"status": "committed", "result": {"success": False}}
            if tool_id == "combat_map_patch":
                return {"status": "committed", "world_patches": arguments["patches"]}
            raise AssertionError(tool_id)

    ruling = {
        "application_id": "turn-ruling-1",
        "actor_id": "caster",
        "feature_id": "",
        "activity_id": "",
        "spell_id": "",
        "procedure_id": "scene-compulsion",
        "round": 1,
        "target_id": "scout",
        "save": {
            "ability": "wisdom",
            "dc": 13,
            "advantage": False,
            "disadvantage": False,
            "success_outcome": "The effect fails.",
            "failure_outcome": "The scout attacks the named ally once.",
            "forced_target_id": "ally",
            "ends_if_source_incapacitated": True,
        },
        "agent_ruling": {
            "default_resolver": "agent",
            "ruling_kind": "agent_dm_adjudication",
            "decision": "The caster uses the reviewed feature on the scout.",
            "reason": "The cited encounter explicitly selects this tactic.",
            "source_ref": {
                "module_id": "module-1",
                "scene_id": "scene-1",
                "chunk_id": "chunk-1",
                "content_sha256": "a" * 64,
            },
        },
    }
    client = Client()
    with patch(
        "scripts.regression_encounter.campaign_view",
        new=AsyncMock(
            side_effect=[
                {"revision": 10},
                {"revision": 11},
                {"revision": 12},
            ]
        ),
    ):
        result = asyncio.run(
            _settle_agent_turn_ruling(
                client,
                SimpleNamespace(campaign_id="campaign-1", run_id="run-1"),
                branch_id="branch-1",
                ruling=ruling,
                sequence=4,
            )
        )

    assert [name for name, _arguments in client.calls] == [
        "combat_common_action",
        "combat_check",
        "combat_map_patch",
    ]
    assert result["save_success"] is False
    assert result["forced_target_id"] == "ally"
    assert result["procedure_id"] == "scene-compulsion"
    assert client.calls[0][1]["payload"]["procedure_id"] == "scene-compulsion"
    patch_value = client.calls[-1][1]["patches"][0]["value"]
    assert patch_value["application_id"] == "turn-ruling-1"
    assert patch_value["procedure_id"] == "scene-compulsion"
    assert patch_value["ends_if_source_incapacitated"] is True


def test_agent_turn_ruling_settles_action_check_and_returns_combat_outcome() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def domain(self, tool_id: str, arguments: dict) -> dict:
            self.calls.append((tool_id, arguments))
            if tool_id == "combat_check":
                return {
                    "status": "committed",
                    "result": {
                        "kind": "check",
                        "skill": "persuasion",
                        "dc": 18,
                        "success": True,
                    },
                }
            if tool_id == "combat_map_patch":
                return {"status": "committed", "world_patches": arguments["patches"]}
            raise AssertionError(tool_id)

    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "content_sha256": "a" * 64,
    }
    ruling = {
        "application_id": "turn-ruling-calm-king",
        "actor_id": "bard",
        "feature_id": "",
        "activity_id": "",
        "spell_id": "",
        "procedure_id": "calm-hostile-king",
        "round": 1,
        "target_id": "",
        "target_ids": [],
        "check": {
            "ability": "persuasion",
            "dc": 18,
            "action": "improvise",
            "advantage": True,
            "disadvantage": False,
            "success_outcome": "The hostile king calms down.",
            "failure_outcome": "The hostile king remains uncontrolled.",
            "success_combat_outcome": {
                "status": "truce",
                "summary": "The party calmed the king by invoking Serissa.",
            },
        },
        "agent_ruling": {
            "default_resolver": "agent",
            "ruling_kind": "agent_dm_adjudication",
            "decision": "The bard names Serissa and asks the king to stand down.",
            "reason": (
                "The exact scene procedure permits the action with advantage."
            ),
            "source_ref": source_ref,
        },
    }
    client = Client()
    with patch(
        "scripts.regression_encounter.campaign_view",
        new=AsyncMock(side_effect=[{"revision": 10}, {"revision": 11}]),
    ):
        result = asyncio.run(
            _settle_agent_turn_ruling(
                client,
                SimpleNamespace(campaign_id="campaign-1", run_id="run-1"),
                branch_id="branch-1",
                ruling=ruling,
                sequence=1,
            )
        )

    assert [name for name, _arguments in client.calls] == [
        "combat_check",
        "combat_map_patch",
    ]
    check_arguments = client.calls[0][1]
    assert check_arguments["kind"] == "check"
    assert check_arguments["action"] == "improvise"
    assert check_arguments["ability"] == "persuasion"
    assert check_arguments["rule_facts"] == {
        "source_ref": source_ref,
        "agent_ruling_id": "turn-ruling-calm-king",
    }
    assert result["check_success"] is True
    assert result["outcome"] == "The hostile king calms down."
    assert result["combat_outcome"] == {
        "status": "truce",
        "summary": "The party calmed the king by invoking Serissa.",
    }
    patch_value = client.calls[-1][1]["patches"][0]["value"]
    assert patch_value["check_success"] is True
    assert patch_value["combat_outcome"]["status"] == "truce"


def test_completed_agent_turn_combat_outcome_requires_successful_server_check() -> None:
    combat = {
        "battle_map": {
            "world_patches": [
                {
                    "key": "agent_turn_ruling:turn-ruling-calm-king",
                    "value": {
                        "check_success": True,
                        "combat_outcome": {
                            "status": "truce",
                            "summary": "The party calmed the king.",
                        },
                    },
                }
            ]
        }
    }

    assert _completed_agent_turn_combat_outcome(combat) == {
        "status": "truce",
        "summary": "The party calmed the king.",
    }

    combat["battle_map"]["world_patches"][0]["value"]["check_success"] = False
    with pytest.raises(
        RuntimeError,
        match="not backed by a successful server check",
    ):
        _completed_agent_turn_combat_outcome(combat)


def test_agent_turn_ruling_settles_area_save_damage_atomically() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def domain(self, tool_id: str, arguments: dict) -> dict:
            self.calls.append((tool_id, arguments))
            if tool_id == "combat_use_activity":
                return {
                    "status": "pending_ruling",
                    "result": {"requires_ruling": True},
                }
            if tool_id == "combat_hp_change":
                assert arguments["action"] == "save_damage"
                return {
                    "status": "committed",
                    "result": {
                        "damage_roll": {"total": 56},
                        "targets": [
                            {
                                "target_id": "pc-1",
                                "success": True,
                                "save": {"success": True},
                                "damage_amount": 28,
                                "damage": {"applied_amount": 28},
                            },
                            {
                                "target_id": "pc-2",
                                "success": False,
                                "save": {"success": False},
                                "damage_amount": 56,
                                "damage": {"applied_amount": 56},
                            },
                        ],
                    },
                    "combat": {},
                }
            if tool_id == "combat_map_patch":
                return {"status": "committed", "world_patches": arguments["patches"]}
            raise AssertionError(tool_id)

    ruling = {
        "application_id": "turn-ruling-breath-1",
        "actor_id": "dragon",
        "feature_id": "",
        "activity_id": "poison-breath-activity",
        "spell_id": "",
        "round": 1,
        "target_id": "",
        "target_ids": ["pc-1", "pc-2"],
        "mechanic_source_excerpt": (
            "Poison Breath. Each creature in the area must make a DC 18 "
            "Constitution saving throw, taking 16d6 poison damage on a failed "
            "save, or half as much damage on a successful one."
        ),
        "save": {
            "ability": "constitution",
            "dc": 18,
            "advantage": False,
            "disadvantage": False,
            "success_outcome": "Half damage.",
            "failure_outcome": "Full damage.",
            "forced_target_id": "",
            "ends_if_source_incapacitated": False,
            "damage": {
                "expression": "16d6",
                "damage_type": "poison",
                "half_on_success": True,
            },
        },
        "agent_ruling": {
            "default_resolver": "agent",
            "ruling_kind": "agent_dm_adjudication",
            "decision": "The dragon catches both targets in its breath.",
            "reason": "The cited encounter directs this breath attack.",
            "encounter_source_excerpt": (
                "The dragon catches both heroes in the cone and uses Poison Breath."
            ),
            "source_ref": {
                "module_id": "module-1",
                "scene_id": "scene-1",
                "chunk_id": "chunk-1",
                "content_sha256": "a" * 64,
            },
        },
    }
    client = Client()
    args = SimpleNamespace(campaign_id="campaign-1", run_id="run-1")
    with patch(
        "scripts.regression_encounter.campaign_view",
        new=AsyncMock(
            side_effect=[{"revision": revision} for revision in range(10, 17)]
        ),
    ):
        result = asyncio.run(
            _settle_agent_turn_ruling(
                client,
                args,
                branch_id="branch-1",
                ruling=ruling,
                sequence=4,
            )
        )

    assert [name for name, _arguments in client.calls] == [
        "combat_use_activity",
        "combat_hp_change",
        "combat_map_patch",
    ]
    assert client.calls[0][1]["idempotency_key"] == (
        "encounter-agent-turn-activity-"
        + _agent_turn_transaction_token(
            args,
            branch_id="branch-1",
            application_id=ruling["application_id"],
            parts=("action", "combat_use_activity"),
        )
    )
    payment_commitment = client.calls[0][1]["declaration"][
        "agent_ruling_commitment"
    ]
    settlement_payload = client.calls[1][1]["payload"]
    assert payment_commitment["target_ids"] == ["pc-1", "pc-2"]
    assert payment_commitment["agent_ruling"] == settlement_payload["agent_ruling"]
    assert settlement_payload["target_ids"] == ["pc-1", "pc-2"]
    assert [item["applied_amount"] for item in result["damage_results"]] == [28, 56]
    assert result["save_results"][0]["success"] is True
    assert result["save_results"][1]["success"] is False


def test_agent_turn_ruling_recovers_legacy_action_payment_after_interruption() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []
            self.activity_attempts = 0

        async def domain(self, tool_id: str, arguments: dict) -> dict:
            self.calls.append((tool_id, arguments))
            if tool_id == "combat_use_activity":
                self.activity_attempts += 1
                if self.activity_attempts < 3:
                    raise RuntimeError("actor has no action remaining")
                return {
                    "status": "pending_ruling",
                    "result": {"requires_ruling": True},
                }
            if tool_id == "combat_query":
                return {
                    "round": 1,
                    "turn_index": 5,
                    "combatants": [{"actor_id": f"actor-{index}"} for index in range(6)],
                }
            if tool_id == "combat_map_patch":
                return {"status": "committed", "world_patches": arguments["patches"]}
            raise AssertionError(tool_id)

    ruling = {
        "application_id": "turn-ruling-interrupted-1",
        "actor_id": "dragon",
        "feature_id": "",
        "activity_id": "poison-breath-activity",
        "spell_id": "",
        "round": 1,
        "target_id": "",
        "target_ids": [],
        "save": {},
        "agent_ruling": {
            "default_resolver": "agent",
            "ruling_kind": "agent_dm_adjudication",
            "decision": "The dragon uses its reviewed breath activity.",
            "reason": "The cited encounter directs this opening action.",
            "source_ref": {
                "module_id": "module-1",
                "scene_id": "scene-1",
                "chunk_id": "chunk-1",
                "content_sha256": "a" * 64,
            },
        },
    }
    client = Client()
    with patch(
        "scripts.regression_encounter.campaign_view",
        new=AsyncMock(
            side_effect=[{"revision": revision} for revision in range(10, 14)]
        ),
    ):
        result = asyncio.run(
            _settle_agent_turn_ruling(
                client,
                SimpleNamespace(campaign_id="campaign-1", run_id="run-1"),
                branch_id="branch-1",
                ruling=ruling,
                sequence=1,
            )
        )

    activity_calls = [
        arguments
        for name, arguments in client.calls
        if name == "combat_use_activity"
    ]
    assert len(activity_calls) == 3
    assert len({call["idempotency_key"] for call in activity_calls}) == 3
    assert result["action_result"]["recovered_legacy_action_payment"] is True
    assert result["action_result"]["legacy_turn_sequence"] == 6


def test_agent_turn_ruling_recovers_action_roll_and_save_from_public_history() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def domain(self, tool_id: str, arguments: dict) -> dict | list[dict]:
            self.calls.append((tool_id, arguments))
            if tool_id == "combat_use_activity":
                raise RuntimeError("actor has no action remaining")
            if tool_id == "combat_query":
                view = arguments["view"]
                if view == "status":
                    return {
                        "round": 1,
                        "turn_index": 1,
                        "combatants": [
                            {"actor_id": "pc-1"},
                            {"actor_id": "dragon"},
                        ],
                    }
                if view == "transaction_history":
                    return [
                        {
                            "sequence": 13,
                            "operation": "combat.save",
                            "idempotency_key": "old-save",
                            "mutation_group_id": "save-group",
                        },
                        {
                            "sequence": 12,
                            "operation": "dnd.dice.roll",
                            "idempotency_key": "old-roll",
                            "mutation_group_id": "roll-group",
                        },
                        {
                            "sequence": 11,
                            "operation": "combat.activity.use",
                            "idempotency_key": "old-action",
                            "mutation_group_id": "action-group",
                        },
                        {
                            "sequence": 10,
                            "operation": "combat.activity.use",
                            "idempotency_key": "old-action",
                            "mutation_group_id": "action-group",
                        },
                    ]
                if view == "transaction_receipt":
                    key = arguments["payload"]["idempotency_key"]
                    if key == "old-action":
                        return {
                            "response": {
                                "status": "pending_ruling",
                                "result": {
                                    "activity_id": "poison-breath-activity",
                                    "requires_ruling": True,
                                },
                                "combat": {
                                    "active": True,
                                    "round": 1,
                                    "turn_index": 1,
                                    "combatants": [
                                        {"actor_id": "pc-1"},
                                        {"actor_id": "dragon"},
                                    ],
                                    "log": [],
                                },
                            }
                        }
                    if key == "old-roll":
                        return {
                            "response": {
                                "expression": "16d6",
                                "total": 56,
                                "rolls": [3] * 16,
                                "random_stream_receipt": {"before": 100, "after": 116},
                            }
                        }
                    if key == "old-save":
                        return {
                            "response": {
                                "status": "committed",
                                "result": {
                                    "kind": "save",
                                    "dc": 18,
                                    "success": False,
                                },
                                "combat": {
                                    "log": [
                                        {
                                            "type": "save",
                                            "actor_id": "pc-1",
                                        }
                                    ]
                                },
                            }
                        }
                    raise AssertionError(key)
            if tool_id == "combat_hp_change":
                return {
                    "status": "committed",
                    "result": {"applied_amount": 56},
                }
            if tool_id == "combat_map_patch":
                return {"status": "committed", "world_patches": arguments["patches"]}
            if tool_id in {"dnd_dice_roll", "combat_check"}:
                raise AssertionError(f"{tool_id} must be recovered, not rerolled")
            raise AssertionError(tool_id)

    ruling = {
        "application_id": "turn-ruling-history-1",
        "actor_id": "dragon",
        "feature_id": "",
        "activity_id": "poison-breath-activity",
        "spell_id": "",
        "round": 1,
        "target_id": "",
        "target_ids": ["pc-1"],
        "mechanic_source_excerpt": (
            "Poison Breath. The target must make a DC 18 Constitution saving "
            "throw, taking 16d6 poison damage on a failed save, or half as "
            "much damage on a successful one."
        ),
        "save": {
            "ability": "constitution",
            "dc": 18,
            "advantage": False,
            "disadvantage": False,
            "success_outcome": "Half damage.",
            "failure_outcome": "Full damage.",
            "forced_target_id": "",
            "ends_if_source_incapacitated": False,
            "damage": {
                "expression": "16d6",
                "damage_type": "poison",
                "half_on_success": True,
            },
        },
        "agent_ruling": {
            "default_resolver": "agent",
            "ruling_kind": "agent_dm_adjudication",
            "decision": "The dragon catches the target in its breath.",
            "reason": "The cited encounter directs this opening action.",
            "encounter_source_excerpt": (
                "The dragon opens with Poison Breath against the hero."
            ),
            "source_ref": {
                "module_id": "module-1",
                "scene_id": "scene-1",
                "chunk_id": "chunk-1",
                "content_sha256": "a" * 64,
            },
        },
    }
    client = Client()
    with patch(
        "scripts.regression_encounter.campaign_view",
        new=AsyncMock(
            side_effect=[{"revision": revision} for revision in range(10, 15)]
        ),
    ):
        result = asyncio.run(
            _settle_agent_turn_ruling(
                client,
                SimpleNamespace(campaign_id="campaign-1", run_id="run-1"),
                branch_id="branch-1",
                ruling=ruling,
                sequence=1,
            )
        )

    assert result["action_result"]["legacy_idempotency_key"] == "old-action"
    assert result["damage_roll"]["legacy_idempotency_key"] == "old-roll"
    assert result["save_results"][0]["result"]["legacy_idempotency_key"] == "old-save"
    assert result["damage_results"][0]["applied_amount"] == 56
    assert not {
        "dnd_dice_roll",
        "combat_check",
    } & {name for name, _arguments in client.calls}


@pytest.mark.parametrize(
    ("payment_economy", "allowed_payment_economies"),
    [
        ("innate_spell", ["innate_spell"]),
        ("slots", ["slots", "pact_magic"]),
    ],
)
def test_agent_turn_spell_pays_use_and_starts_concentration(
    payment_economy: str,
    allowed_payment_economies: list[str],
) -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def domain(self, tool_id: str, arguments: dict) -> dict:
            self.calls.append((tool_id, arguments))
            if tool_id == "combat_cast_spell":
                return {
                    "status": "pending_ruling",
                    "default_resolver": "agent",
                    "result": {
                        "payment": {"economy": payment_economy},
                        "concentration_started": True,
                    },
                }
            if tool_id == "combat_check":
                return {"status": "committed", "result": {"success": False}}
            if tool_id == "combat_map_patch":
                return {"status": "committed", "world_patches": arguments["patches"]}
            raise AssertionError(tool_id)

    ruling = {
        "application_id": "turn-ruling-innate-1",
        "actor_id": "caster",
        "feature_id": "",
        "activity_id": "",
        "spell_id": "suggestion",
        "spell_payment_economies": allowed_payment_economies,
        "concentration_required": True,
        "round": 1,
        "target_id": "scout",
        "save": {
            "ability": "wisdom",
            "dc": 13,
            "advantage": False,
            "disadvantage": False,
            "success_outcome": "The effect fails.",
            "failure_outcome": "The scout attacks the named ally once.",
            "forced_target_id": "ally",
            "ends_if_source_incapacitated": True,
        },
        "agent_ruling": {
            "default_resolver": "agent",
            "ruling_kind": "agent_dm_adjudication",
            "decision": "The caster innately casts Suggestion on the scout.",
            "reason": "The cited encounter explicitly selects this tactic.",
            "source_ref": {
                "module_id": "module-1",
                "scene_id": "scene-1",
                "chunk_id": "chunk-1",
                "content_sha256": "a" * 64,
            },
        },
    }
    client = Client()
    with patch(
        "scripts.regression_encounter.campaign_view",
        new=AsyncMock(
            side_effect=[
                {"revision": 10},
                {"revision": 11},
                {"revision": 12},
            ]
        ),
    ):
        result = asyncio.run(
            _settle_agent_turn_ruling(
                client,
                SimpleNamespace(campaign_id="campaign-1", run_id="run-1"),
                branch_id="branch-1",
                ruling=ruling,
                sequence=4,
            )
        )

    assert [name for name, _arguments in client.calls] == [
        "combat_cast_spell",
        "combat_check",
        "combat_map_patch",
    ]
    assert result["save_success"] is False
    assert result["spell_id"] == "suggestion"


def test_agent_forced_target_receipts_resume_and_close_through_map_patch() -> None:
    combat = {
        "battle_map": {
            "world_patches": [
                {
                    "key": "agent_turn_ruling:turn-ruling-1",
                    "value": {
                        "application_id": "turn-ruling-1",
                        "actor_id": "caster",
                        "target_id": "scout",
                        "forced_target_id": "ally",
                        "ends_if_source_incapacitated": True,
                    },
                }
            ]
        }
    }
    pending = _pending_agent_forced_targets(combat)
    assert pending["scout"]["target_id"] == "ally"
    assert pending["scout"]["source_actor_id"] == "caster"

    class Client:
        async def domain(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "combat_map_patch"
            return {"status": "committed", "patches": arguments["patches"]}

    with patch(
        "scripts.regression_encounter.campaign_view",
        new=AsyncMock(return_value={"revision": 20}),
    ):
        result = asyncio.run(
            _consume_agent_forced_target(
                Client(),
                SimpleNamespace(campaign_id="campaign-1", run_id="run-1"),
                branch_id="branch-1",
                actor_id="scout",
                target_id="ally",
                forced_targets=pending,
            )
        )

    assert result["status"] == "committed"
    assert pending == {}


def test_reaction_availability_and_preflight_limit_target_modifier() -> None:
    combat = {
        "combatants": [
            {"actor_id": "tile-creature", "turn_budget": {"reaction": 1}},
            {"actor_id": "spent-target", "turn_budget": {"reaction": 0}},
        ]
    }
    assert _reaction_available_actor_ids(combat) == {"tile-creature"}

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
                    {
                        "item_id": "longsword",
                        "attack_type": "melee",
                        "properties": [],
                    }
                ]
            }
        },
    }
    contexts = {
        ("tile-creature", "melee"): {
            "context": {
                "disadvantage": True,
                "disadvantage_sources": ["agent-ruling:narrow-dodge"],
            }
        },
        ("spent-target", "melee"): {
            "context": {
                "disadvantage": True,
                "disadvantage_sources": ["agent-ruling:spent"],
            }
        },
    }

    target_id, action, _plan = asyncio.run(
        _preflight_attack(
            client,
            SimpleNamespace(campaign_id="campaign-1"),
            actor,
            ["spent-target", "tile-creature"],
            agent_target_reaction_contexts=contexts,
            reaction_available_actor_ids={"tile-creature"},
        )
    )

    assert target_id == "spent-target"
    assert "context" not in action
    assert "context" not in client.calls[0]["action"]

    target_id, action, _plan = asyncio.run(
        _preflight_attack(
            client,
            SimpleNamespace(campaign_id="campaign-1"),
            actor,
            ["tile-creature"],
            agent_target_reaction_contexts=contexts,
            reaction_available_actor_ids={"tile-creature"},
        )
    )

    assert target_id == "tile-creature"
    assert action["context"]["disadvantage"] is True


def test_preflight_falls_back_from_illegal_multiattack_to_one_ordinary_attack() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def domain(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "combat_preflight_attack"
            action = dict(arguments["action"])
            self.calls.append(action)
            if action.get("multiattack_option_id") or action["attack_mode"] != "ranged":
                raise RuntimeError("attack mode is illegal at this range")
            return {"status": "ready", **action}

    actor = {
        "id": "lizardfolk",
        "derived": {
            "inventory": {
                "weapon_attacks": [
                    {
                        "item_id": "javelin",
                        "attack_type": "melee",
                        "properties": ["thrown"],
                        "range_ft": {"normal": 30, "long": 120},
                    }
                ]
            }
        },
    }
    client = Client()

    target_id, action, plan = asyncio.run(
        _preflight_attack(
            client,
            SimpleNamespace(campaign_id="campaign-1"),
            actor,
            ["pc-1"],
            preferred_weapon_id="javelin",
            multiattack_option_id="two-melee-attacks",
        )
    )

    assert target_id == "pc-1"
    assert action == {"weapon_id": "javelin", "attack_mode": "ranged"}
    assert plan["status"] == "ready"
    assert any("multiattack_option_id" in item for item in client.calls)
    assert any("multiattack_option_id" not in item for item in client.calls)


def test_preflight_never_replaces_a_required_source_opening_weapon() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def domain(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "combat_preflight_attack"
            action = dict(arguments["action"])
            self.calls.append(action)
            if action["weapon_id"] == "web-garrote":
                raise RuntimeError("recorded advantage requirement is not satisfied")
            return {"status": "ready", **action}

    actor = {
        "id": "ettercap-1",
        "derived": {
            "inventory": {
                "weapon_attacks": [
                    {
                        "item_id": "bite",
                        "attack_type": "melee",
                        "properties": [],
                    },
                    {
                        "item_id": "web-garrote",
                        "attack_type": "melee",
                        "properties": [],
                    },
                ]
            }
        },
    }
    client = Client()
    rejections: list[dict[str, str]] = []

    plan = asyncio.run(
        _preflight_attack(
            client,
            SimpleNamespace(campaign_id="campaign-1"),
            actor,
            ["pc-1"],
            preferred_weapon_id="web-garrote",
            multiattack_option_id="bite-and-web-garrote",
            require_preferred_weapon=True,
            preflight_rejections=rejections,
        )
    )

    assert plan is None
    assert [item["weapon_id"] for item in client.calls] == ["web-garrote"]
    assert rejections == [
        {
            "actor_id": "ettercap-1",
            "target_id": "pc-1",
            "weapon_id": "web-garrote",
            "attack_mode": "melee",
            "error": "recorded advantage requirement is not satisfied",
        }
    ]


def test_consume_agent_target_reaction_uses_public_choice_facade() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def domain(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "combat_choice"
            self.calls.append(arguments)
            if arguments["action"] == "open":
                return {
                    "action": "open",
                    "result": {"status": "pending", "choice": {"id": "choice-1"}},
                }
            return {"action": "resolve", "result": {"status": "committed"}}

    context = {
        "application_id": "target-reaction-context-1",
        "actor_id": "tile-creature",
        "attack_mode": "melee",
        "agent_ruling": {
            "decision": "The tile creature turns its narrow side toward the attacker.",
            "reason": "The attack satisfies the cited source trigger.",
            "source_ref": {
                "module_id": "module-1",
                "scene_id": "scene-1",
                "chunk_id": "chunk-1",
                "content_sha256": "a" * 64,
            },
            "source_excerpt": "When targeted by a melee attack, it can take a reaction.",
        },
    }
    client = Client()

    with patch(
        "scripts.regression_encounter.campaign_view",
        new=AsyncMock(side_effect=[{"revision": 10}, {"revision": 11}]),
    ):
        result = asyncio.run(
            _consume_agent_target_reaction(
                client,
                SimpleNamespace(campaign_id="campaign-1", run_id="run-1"),
                branch_id="branch-1",
                context=context,
                attacker_id="pc-1",
                sequence=3,
            )
        )

    assert [call["action"] for call in client.calls] == ["open", "resolve"]
    assert client.calls[0]["expected_revision"] == 10
    assert client.calls[1]["expected_revision"] == 11
    assert client.calls[1]["payload"]["choice_id"] == "choice-1"
    assert result["resolve"]["status"] == "committed"


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


def test_character_summary_surfaces_only_agent_owned_ruling_features() -> None:
    actor = {
        "id": "peryton-1",
        "name": "Peryton",
        "derived": {"hit_points": {"value": 33, "max": 33}},
        "sheet": {
            "conditions": [],
            "content": {
                "features": [
                    {
                        "id": "dive-attack",
                        "name": "Dive Attack",
                        "description": "Printed passive.",
                        "choices": {
                            "manual_ruling": {
                                "kind": "descriptive_passive",
                                "default_resolver": "agent",
                                "source_excerpt": "Printed passive.",
                            }
                        },
                    },
                    {"id": "automatic", "name": "Automatic feature"},
                ]
            },
        },
    }

    assert _character_summary(actor)["agent_ruling_features"] == [
        {
            "id": "dive-attack",
            "name": "Dive Attack",
            "description": "Printed passive.",
            "manual_ruling": {
                "kind": "descriptive_passive",
                "default_resolver": "agent",
                "source_excerpt": "Printed passive.",
            },
        }
    ]


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


def test_preflight_selects_source_ammunition_while_stack_remains() -> None:
    class Client:
        async def domain(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "combat_preflight_attack"
            return {"status": "ready", **arguments["action"]}

    actor = {
        "id": "pc-1",
        "derived": {
            "inventory": {
                "weapon_attacks": [
                    {
                        "item_id": "shortbow",
                        "attack_type": "ranged",
                        "properties": ["ammunition", "two-handed"],
                    }
                ]
            }
        },
        "sheet": {
            "inventory": {
                "items": [
                    {
                        "id": "dragon-slaying-arrow",
                        "kind": "ammunition",
                        "quantity": 2,
                    }
                ]
            }
        },
    }

    _, action, _ = asyncio.run(
        _preflight_attack(
            Client(),
            SimpleNamespace(campaign_id="campaign-1"),
            actor,
            ["dragon-1"],
            source_ammunition_selections={
                ("pc-1", "shortbow"): {
                    "actor_id": "pc-1",
                    "weapon_id": "shortbow",
                    "ammunition_item_id": "dragon-slaying-arrow",
                }
            },
        )
    )

    assert action["ammunition_item_id"] == "dragon-slaying-arrow"


def test_knockout_objective_supports_agent_selected_minimum_without_naming_targets() -> None:
    candidates, minimum = _knockout_objective(
        SimpleNamespace(
            knock_out_hostile_id=[],
            minimum_hostile_knockouts=1,
        ),
        hostile_ids=["lizardfolk-1", "lizardfolk-2", "lizardfolk-3"],
    )

    assert candidates == {"lizardfolk-1", "lizardfolk-2", "lizardfolk-3"}
    assert minimum == 1


def test_knockout_objective_keeps_exact_target_compatibility() -> None:
    candidates, minimum = _knockout_objective(
        SimpleNamespace(
            knock_out_hostile_id=["cultist-1", "cultist-2"],
            minimum_hostile_knockouts=None,
        ),
        hostile_ids=["cultist-1", "cultist-2", "cultist-3"],
    )

    assert candidates == {"cultist-1", "cultist-2"}
    assert minimum is None


def test_knockout_objective_supports_nonlethal_preference_without_hard_minimum() -> None:
    candidates, minimum = _knockout_objective(
        SimpleNamespace(
            knock_out_hostile_id=["zaltember"],
            minimum_hostile_knockouts=0,
        ),
        hostile_ids=["zaltember", "ogre-1"],
    )

    assert candidates == {"zaltember"}
    assert minimum == 0


def test_knockout_objective_rejects_impossible_minimum() -> None:
    with pytest.raises(ValueError, match="eligible hostile count"):
        _knockout_objective(
            SimpleNamespace(
                knock_out_hostile_id=["cultist-1"],
                minimum_hostile_knockouts=2,
            ),
            hostile_ids=["cultist-1", "cultist-2"],
        )


def test_captured_hostile_ids_uses_actual_public_character_state() -> None:
    def actor(hit_points: int, *conditions: str) -> dict:
        return {
            "sheet": {
                "combat": {"hp": {"value": hit_points}},
                "conditions": list(conditions),
            }
        }

    captured = _captured_hostile_ids(
        {
            "stable-1": actor(0, "prone", "stable", "unconscious"),
            "dead-1": actor(0, "dead", "prone"),
            "awake-1": actor(1),
        },
        candidate_ids={"stable-1", "dead-1", "awake-1"},
    )

    assert captured == {"stable-1"}


def test_source_extra_damage_ruling_binds_exact_agent_passive_and_application() -> None:
    actor = {
        "id": "peryton-1",
        "sheet": {
            "content": {
                "features": [
                    {
                        "id": "dive-attack-passive",
                        "name": "Dive Attack",
                        "description": PERYTON_DIVE_ATTACK,
                        "choices": {
                            "manual_ruling": {
                                "kind": "descriptive_passive",
                                "default_resolver": "agent",
                                "source_excerpt": PERYTON_DIVE_ATTACK,
                            }
                        },
                        "resolution_plan": {
                            "schema_version": 2,
                            "source_card_id": "dive-attack-passive",
                            "source_card_kind": "feature",
                            "trigger": "attack.after_hit",
                            "steps": [
                                {
                                    "op": "damage.apply",
                                    "args": {"expression": "2d8"},
                                }
                            ],
                            "fingerprint": "a" * 64,
                        },
                        "resolution_solution": {
                            "status": "compiled",
                            "plan_fingerprint": "a" * 64,
                        },
                    }
                ]
            }
        },
        "derived": {
            "inventory": {
                "weapon_attacks": [
                    {"item_id": "gore", "attack_type": "melee"},
                    {"item_id": "shortbow", "attack_type": "ranged"},
                ]
            }
        },
    }
    declaration = {
        "actor_id": actor["id"],
        "feature_id": "dive-attack-passive",
        "weapon_ids": ["gore", "shortbow"],
        "target_actor_ids": ["target-1"],
        "rounds": [1],
        "max_applications": 1,
        "damage_expression": "2d8",
        "damage_type": "weapon",
        "source_excerpt": PERYTON_DIVE_ATTACK,
        "trigger_facts": {
            "flying": True,
            "straight_dive_ft": 30,
            "requires_attack_advantage": True,
            "max_applications_per_turn": 1,
        },
        "decision": "The first round starts with the source-defined dive.",
        "reason": "The encounter starts the peryton high above the road.",
    }

    normalized = _source_extra_damage_rulings(
        [declaration],
        participant_ids=[actor["id"], "target-1"],
        actors={actor["id"]: actor},
    )
    action_rulings = _source_extra_damage_action_rulings(
        normalized,
        actor_id=actor["id"],
        target_id="target-1",
        weapon_id="shortbow",
        round_number=1,
        applications={},
    )

    assert len(action_rulings) == 1
    assert action_rulings[0]["source"] == "dm_ruling"
    assert action_rulings[0]["damage_expression"] == "2d8"
    assert action_rulings[0]["solution_plan_fingerprint"] == "a" * 64
    assert action_rulings[0]["trigger_facts"]["straight_dive_ft"] == 30
    conditional_declarations = deepcopy(normalized)
    conditional_declarations[actor["id"]][0]["trigger_facts"] = {
        "applicability_mode": (
            "attack_advantage_or_target_adjacent_to_ally_without_disadvantage"
        ),
        "max_applications_per_turn": 1,
    }
    adjacent = _source_extra_damage_action_rulings(
        conditional_declarations,
        actor_id=actor["id"],
        target_id="target-1",
        weapon_id="gore",
        round_number=1,
        applications={},
        plan={"advantage": False, "disadvantage": False},
        combat={
            "combatants": [
                {
                    "actor_id": actor["id"],
                    "disposition": "hostile",
                    "position": {"x": 0, "y": 0},
                    "conditions": [],
                },
                {
                    "actor_id": "target-1",
                    "disposition": "friendly",
                    "position": {"x": 2, "y": 0},
                    "conditions": [],
                },
                {
                    "actor_id": "ally-1",
                    "disposition": "hostile",
                    "position": {"x": 1, "y": 0},
                    "conditions": [],
                },
            ]
        },
    )
    assert adjacent[0]["trigger_facts"]["applicability_branch"] == "adjacent_ally"
    assert adjacent[0]["trigger_facts"]["qualifying_ally_actor_ids"] == ["ally-1"]
    incapacitated_combat = {
        "combatants": deepcopy(
            [
                {
                    "actor_id": actor["id"],
                    "disposition": "hostile",
                    "position": {"x": 0, "y": 0},
                    "conditions": [],
                },
                {
                    "actor_id": "target-1",
                    "disposition": "friendly",
                    "position": {"x": 2, "y": 0},
                    "conditions": [],
                },
                {
                    "actor_id": "ally-1",
                    "disposition": "hostile",
                    "position": {"x": 1, "y": 0},
                    "conditions": ["unconscious"],
                },
            ]
        )
    }
    assert (
        _source_extra_damage_action_rulings(
            conditional_declarations,
            actor_id=actor["id"],
            target_id="target-1",
            weapon_id="gore",
            round_number=1,
            applications={},
            plan={"advantage": False, "disadvantage": False},
            combat=incapacitated_combat,
        )
        == []
    )
    assert (
        _source_extra_damage_action_rulings(
            normalized,
            actor_id=actor["id"],
            target_id="target-1",
            weapon_id="gore",
            round_number=1,
            applications={(actor["id"], "dive-attack-passive"): 1},
        )
        == []
    )
    assert (
        _source_extra_damage_action_rulings(
            normalized,
            actor_id=actor["id"],
            target_id="target-1",
            weapon_id="gore",
            round_number=1,
            applications={},
            turn_applications={
                (actor["id"], "dive-attack-passive", 1): 1
            },
        )
        == []
    )

    with pytest.raises(ValueError, match="exact Agent-owned passive"):
        _source_extra_damage_rulings(
            [{**declaration, "source_excerpt": "It dives and deals 2d8."}],
            participant_ids=[actor["id"], "target-1"],
            actors={actor["id"]: actor},
        )
    unresolved_actor = {
        **actor,
        "sheet": {
            "content": {
                "features": [
                    {
                        key: value
                        for key, value in actor["sheet"]["content"]["features"][0].items()
                        if key not in {"resolution_plan", "resolution_solution"}
                    }
                ]
            }
        },
    }
    with pytest.raises(ValueError, match="persisted first-use attack solution"):
        _source_extra_damage_rulings(
            [declaration],
            participant_ids=[actor["id"], "target-1"],
            actors={actor["id"]: unresolved_actor},
        )

    different_excerpt = PERYTON_DIVE_ATTACK.replace("(2d8)", "(12d8)")
    different_actor = {
        **actor,
        "sheet": {
            "content": {
                "features": [
                    {
                        **actor["sheet"]["content"]["features"][0],
                        "description": different_excerpt,
                        "choices": {
                            "manual_ruling": {
                                "kind": "descriptive_passive",
                                "default_resolver": "agent",
                                "source_excerpt": different_excerpt,
                            }
                        },
                        "resolution_plan": {
                            **actor["sheet"]["content"]["features"][0][
                                "resolution_plan"
                            ],
                        },
                        "resolution_solution": {
                            **actor["sheet"]["content"]["features"][0][
                                "resolution_solution"
                            ],
                        },
                    }
                ]
            }
        },
    }
    with pytest.raises(ValueError, match="printed dice expression"):
        _source_extra_damage_rulings(
            [
                {
                    **declaration,
                    "source_excerpt": different_excerpt,
                    "damage_expression": "2d8",
                }
            ],
            participant_ids=[actor["id"], "target-1"],
            actors={actor["id"]: different_actor},
        )


def test_source_extra_damage_history_recovers_bounded_successes() -> None:
    declarations = {
        "peryton-1": [
            {
                "feature_id": "dive-attack-passive",
                "max_applications": 1,
            }
        ]
    }
    combat = {
        "log": [
            {
                "type": "attack",
                "result": {
                    "damage": {
                        "roll_parts": [
                            {
                                "expression": "2d8",
                                "source": (
                                    "agent-ruling:peryton-1:"
                                    "dive-attack-passive:1:1"
                                ),
                            }
                        ]
                    }
                },
            }
        ]
    }

    assert _source_extra_damage_history(combat, declarations) == {
        ("peryton-1", "dive-attack-passive"): 1
    }
    assert _source_extra_damage_turn_history(combat, declarations) == {
        ("peryton-1", "dive-attack-passive", 1): 1
    }
    with pytest.raises(RuntimeError, match="exceeds"):
        _source_extra_damage_history(
            {"log": [*combat["log"], *combat["log"]]},
            declarations,
        )


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


def test_preflight_uses_only_agent_declared_weapon_modes_in_order() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def domain(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "combat_preflight_attack"
            self.calls.append(arguments)
            if arguments["action"]["weapon_id"] == "shortbow":
                raise RuntimeError("the target is inside the bow's minimum policy range")
            return {"status": "ready", **arguments["action"]}

    client = Client()
    actor = {
        "id": "goblin",
        "derived": {
            "inventory": {
                "weapon_attacks": [
                    {"item_id": "scimitar", "attack_type": "melee"},
                    {"item_id": "shortbow", "attack_type": "ranged"},
                ]
            }
        },
    }

    target_id, action, _ = asyncio.run(
        _preflight_attack(
            client,
            SimpleNamespace(campaign_id="campaign-1"),
            actor,
            ["pc-1"],
            agent_weapon_choices=[
                {"weapon_id": "shortbow", "attack_mode": "ranged"},
                {"weapon_id": "scimitar", "attack_mode": "melee"},
            ],
        )
    )

    assert target_id == "pc-1"
    assert action == {"weapon_id": "scimitar", "attack_mode": "melee"}
    assert [
        (call["action"]["weapon_id"], call["action"]["attack_mode"])
        for call in client.calls
    ] == [("shortbow", "ranged"), ("scimitar", "melee")]


def test_preflight_applies_agent_attack_context_only_to_its_mode() -> None:
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
        "id": "dragon-1",
        "derived": {
            "inventory": {
                "weapon_attacks": [
                    {
                        "item_id": "spear",
                        "attack_type": "melee",
                        "properties": ["thrown"],
                        "range_ft": {"normal": 20, "long": 60},
                    }
                ]
            }
        },
    }
    contexts = {
        ("dragon-1", "", "melee"): {
            "context": {
                "advantage": False,
                "disadvantage": True,
                "disadvantage_sources": ["agent-ruling:narrow-tunnel"],
            }
        }
    }

    asyncio.run(
        _preflight_attack(
            client,
            SimpleNamespace(campaign_id="campaign-1"),
            actor,
            ["pc-1"],
            agent_attack_contexts=contexts,
        )
    )

    assert client.calls[0]["action"]["context"]["disadvantage"] is True
    assert "context" not in client.calls[1]["action"]


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
            if arguments["action"]["attack_mode"] == "melee":
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
        "unarmed-strike",
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


def test_pending_combat_start_returns_to_agent_before_combat_exposure() -> None:
    pending = {
        "status": "pending_ruling",
        "default_resolver": "agent",
        "ruling_kind": "agent_dm_adjudication",
        "reason": "battle-map location_key is not in scene spatial evidence",
        "committed": False,
        "missing": ["battle_map"],
    }

    with pytest.raises(EncounterRulingRequiredError) as raised:
        _require_committed_encounter_start(pending)

    requirement = raised.value.requirement
    assert requirement["operation"] == "combat_start"
    assert requirement["ruling"]["committed"] is False
    assert "temporary-map ruling" in requirement["retry_hint"]


def test_committed_combat_start_requires_an_active_encounter() -> None:
    combat = _require_committed_encounter_start(
        {"combat": {"id": "combat-1", "active": True}}
    )

    assert combat["id"] == "combat-1"
    with pytest.raises(RuntimeError, match="without an active committed"):
        _require_committed_encounter_start({"combat": {"active": False}})


def test_encounter_battle_map_uses_canonical_default_without_indexed_location() -> None:
    assert _encounter_battle_map_request(None) == {}
    assert _encounter_battle_map_request("  ") == {}
    assert _encounter_battle_map_request("area-5") == {"location_key": "area-5"}


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


def test_queued_reinforcements_are_potential_but_not_attackable_sides() -> None:
    sides = _body_thief_sides(
        {
            "combatants": [
                {"actor_id": "pc-1"},
                {"actor_id": "orc-1"},
            ],
            "reinforcements": [
                {"actor_id": "scout-1"},
                {"actor_id": "orc-2"},
            ],
        },
        party_ids=["pc-1", "scout-1"],
        hostile_ids=["orc-1", "orc-2"],
    )

    assert sides["potential_party_ids"] == ["pc-1", "scout-1"]
    assert sides["effective_party_ids"] == ["pc-1"]
    assert sides["attackable_hostile_ids"] == ["orc-1"]


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

    assert _choose_agent_spell(
        "cleric",
        party_ids=["cleric", "wizard", "ally"],
        actors=actors,
        living_targets=["goblin"],
        spell_choices=[
            {"spell_id": HEALING_WORD_ID, "target_policy": "downed_ally"},
            {
                "spell_id": GUIDING_BOLT_ID,
                "target_policy": "prioritized_opponent",
            },
        ],
    ) == (HEALING_WORD_ID, "ally", 1)

    actors["ally"]["sheet"]["combat"]["hp"]["value"] = 3
    assert _choose_agent_spell(
        "cleric",
        party_ids=["cleric", "wizard", "ally"],
        actors=actors,
        living_targets=["goblin"],
        spell_choices=[
            {"spell_id": HEALING_WORD_ID, "target_policy": "downed_ally"},
            {
                "spell_id": GUIDING_BOLT_ID,
                "target_policy": "prioritized_opponent",
            },
        ],
    ) == (GUIDING_BOLT_ID, "goblin", 1)
    assert _choose_agent_spell(
        "wizard",
        party_ids=["cleric", "wizard", "ally"],
        actors=actors,
        living_targets=["goblin"],
        spell_choices=[
            {
                "spell_id": MAGIC_MISSILE_ID,
                "target_policy": "prioritized_opponent",
            }
        ],
    ) == (MAGIC_MISSILE_ID, "goblin", 1)
    assert (
        _choose_agent_spell(
            "cleric",
            party_ids=["cleric", "wizard", "ally"],
            actors=actors,
            living_targets=["goblin"],
            spell_choices=[
                {
                    "spell_id": GUIDING_BOLT_ID,
                    "target_policy": "prioritized_opponent",
                }
            ],
            leveled_spell_available=False,
        )
        is None
    )
    actors["evil-mage"] = _spell_actor(MAGIC_MISSILE_ID)
    assert _choose_agent_spell(
        "evil-mage",
        party_ids=["cleric", "wizard", "ally"],
        actors=actors,
        living_targets=["wizard"],
        spell_choices=[
            {
                "spell_id": MAGIC_MISSILE_ID,
                "target_policy": "prioritized_opponent",
            }
        ],
    ) == (MAGIC_MISSILE_ID, "wizard", 1)


def test_agent_spell_priority_preserves_explicit_order_and_target_policy() -> None:
    actor = _spell_actor(HEALING_WORD_ID, GUIDING_BOLT_ID)
    priorities = _agent_spell_priorities(
        [
            {
                "actor_id": "cleric",
                "choices": [
                    {
                        "spell_id": HEALING_WORD_ID,
                        "target_policy": "downed_ally",
                        "cast_level_policy": "lowest_available",
                    },
                    {
                        "spell_id": GUIDING_BOLT_ID,
                        "target_policy": "prioritized_opponent",
                    },
                ],
                "decision": "Recover a fallen ally before making a spell attack.",
                "ruling_reason": "The Agent explicitly prioritizes party survival.",
            }
        ],
        participant_ids=["cleric"],
        actors={"cleric": actor},
    )

    assert priorities["cleric"]["choices"] == [
        {
            "spell_id": HEALING_WORD_ID,
            "target_policy": "downed_ally",
            "cast_level_policy": "lowest_available",
        },
        {
            "spell_id": GUIDING_BOLT_ID,
            "target_policy": "prioritized_opponent",
            "cast_level_policy": "lowest_available",
        },
    ]


def test_agent_spell_priority_accepts_reviewed_single_target_save_cantrip() -> None:
    actor = _spell_actor("sacred-flame", slots=0)
    actor["sheet"]["content"]["spells"][0].update(
        {
            "level": 0,
            "resolution": {
                "kind": "saving_throw",
                "targeting": {
                    "mode": "creature",
                    "max_targets": 1,
                    "requires_sight": True,
                },
                "save": {
                    "ability": "dexterity",
                    "ignores_cover": True,
                    "success": "none",
                    "damage": {
                        "base_dice": "1d8",
                        "damage_type": "radiant",
                    },
                },
            },
        }
    )

    priorities = _agent_spell_priorities(
        [
            {
                "actor_id": "cleric",
                "choices": [
                    {
                        "spell_id": "sacred-flame",
                        "target_policy": "prioritized_opponent",
                    }
                ],
                "decision": "Use the reviewed saving-throw cantrip against one foe.",
                "ruling_reason": "The spell card completely defines its target and save.",
            }
        ],
        participant_ids=["cleric"],
        actors={"cleric": actor},
    )

    assert priorities["cleric"]["choices"][0]["spell_id"] == "sacred-flame"
    assert _safe_single_target_spell_declaration(
        actor["sheet"]["content"]["spells"][0],
        target_id="goblin",
    ) == {"target_id": "goblin"}
    assert _choose_agent_spell(
        "cleric",
        party_ids=["cleric"],
        actors={"cleric": actor},
        living_targets=["goblin"],
        spell_choices=priorities["cleric"]["choices"],
        leveled_spell_available=False,
    ) == ("sacred-flame", "goblin", 0)


def test_agent_spell_priority_accepts_engine_owned_hypnotic_pattern_old_card() -> None:
    actor = _spell_actor(HYPNOTIC_PATTERN_ID)
    actor["sheet"]["content"]["spells"][0].update(
        {
            "level": 3,
            "access": {"known": True},
            "definition": {"range": {"normal_ft": 120}},
            "resolution": None,
            "mechanic_refs": [],
        }
    )

    priorities = _agent_spell_priorities(
        [
            {
                "actor_id": "bard",
                "choices": [
                    {
                        "spell_id": HYPNOTIC_PATTERN_ID,
                        "target_policy": "maximize_opponents_without_allies",
                    }
                ],
                "decision": "Control the densest hostile group without catching allies.",
                "ruling_reason": (
                    "The engine owns Hypnotic Pattern even when a durable old card "
                    "predates its structured resolution metadata."
                ),
            }
        ],
        participant_ids=["bard"],
        actors={"bard": actor},
    )

    assert priorities["bard"]["choices"] == [
        {
            "spell_id": HYPNOTIC_PATTERN_ID,
            "target_policy": "maximize_opponents_without_allies",
            "cast_level_policy": "lowest_available",
        }
    ]


def test_single_target_dexterity_save_requires_explicit_cover_semantics() -> None:
    spell = {
        "level": 0,
        "resolution": {
            "kind": "saving_throw",
            "targeting": {"mode": "creature", "max_targets": 1},
            "save": {"ability": "dexterity", "success": "none"},
        },
    }

    assert (
        _safe_single_target_spell_declaration(spell, target_id="goblin")
        is None
    )


def test_agent_common_action_priority_is_explicit_and_source_neutral() -> None:
    priorities = _agent_common_action_priorities(
        [
            {
                "actor_id": "guard",
                "choices": [{"action": "dodge"}],
                "decision": "Take the Dodge action when no reviewed attack is suitable.",
                "ruling_reason": "Dodge is a legal common action and invents no source fact.",
            }
        ],
        participant_ids=["guard"],
    )

    assert priorities["guard"]["choices"] == [{"action": "dodge"}]
    assert priorities["guard"]["agent_ruling"] == {
        "default_resolver": "agent",
        "ruling_kind": "agent_dm_adjudication",
        "decision": "Take the Dodge action when no reviewed attack is suitable.",
        "reason": "Dodge is a legal common action and invents no source fact.",
    }
    with pytest.raises(ValueError, match="safe fallback action dodge"):
        _agent_common_action_priorities(
            [
                {
                    "actor_id": "guard",
                    "choices": [{"action": "attack"}],
                    "decision": "Invent an unreviewed attack for this otherwise idle turn.",
                    "ruling_reason": "This must not bypass the recorded character card.",
                }
            ],
            participant_ids=["guard"],
        )


def test_agent_casting_perception_requires_an_explicit_observer_matrix() -> None:
    rulings = _agent_casting_perception_rulings(
        [
            {
                "caster_id": "hidden-mage",
                "observations": [
                    {
                        "observer_id": "guard",
                        "perceived": True,
                        "reason": "The guard hears the spell's verbal component.",
                    },
                    {
                        "observer_id": "ally",
                        "perceived": False,
                        "reason": "A closed stone door blocks the ally from hearing it.",
                    },
                ],
                "decision": "Only the guard perceives the hidden spellcasting.",
                "ruling_reason": (
                    "The Agent applies the recorded doors and relative positions "
                    "instead of treating missing facts as proof."
                ),
            }
        ],
        participant_ids=["hidden-mage", "guard", "ally"],
    )

    ruling = rulings["hidden-mage"]
    assert ruling["component_ruling"]["casting_perception"] == [
        {
            "observer_id": "guard",
            "perceived": True,
            "reason": "The guard hears the spell's verbal component.",
        },
        {
            "observer_id": "ally",
            "perceived": False,
            "reason": "A closed stone door blocks the ally from hearing it.",
        },
    ]
    assert ruling["agent_ruling"]["ruling_kind"] == "agent_dm_adjudication"


@pytest.mark.parametrize(
    "declaration,match",
    [
        (
            {
                "caster_id": "mage",
                "observations": [],
                "decision": "No observer perceives the spellcasting.",
                "ruling_reason": "The Agent reviewed every current observer.",
            },
            "requires one unique",
        ),
        (
            {
                "caster_id": "mage",
                "observations": [
                    {
                        "observer_id": "mage",
                        "perceived": True,
                        "reason": "The caster cannot be its own observer.",
                    }
                ],
                "decision": "The caster perceives its own spellcasting.",
                "ruling_reason": "This invalid declaration must be rejected.",
            },
            "distinct participant observer",
        ),
    ],
)
def test_agent_casting_perception_rejects_incomplete_or_self_authored_matrix(
    declaration: dict,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        _agent_casting_perception_rulings(
            [declaration],
            participant_ids=["mage", "guard"],
        )


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

    assert _choose_agent_spell(
        "wizard",
        party_ids=["wizard"],
        actors=actors,
        living_targets=["goblin"],
        spell_choices=[
            {
                "spell_id": MAGIC_MISSILE_ID,
                "target_policy": "prioritized_opponent",
            }
        ],
    ) == (MAGIC_MISSILE_ID, "goblin", 2)


def test_party_spell_tactics_choose_safe_structured_area_damage() -> None:
    fireball_id = "dnd5e.content.srd2014.spell.fireball"
    wizard = _spell_actor(fireball_id, slots=0)
    wizard["sheet"]["spellcasting"].update(
        {
            "preparation": {
                "mode": "spellbook",
                "selected_spell_ids": [fireball_id],
            },
            "spell_slots": {
                "1": {"value": 0, "max": 4},
                "2": {"value": 0, "max": 3},
                "3": {"value": 1, "max": 3},
            },
        }
    )
    wizard["sheet"]["content"]["spells"][0].update(
        {
            "level": 3,
            "access": {"prepared": True},
            "definition": {"range": {"normal_ft": 150}},
            "resolution": {
                "kind": "saving_throw",
                "targeting": {
                    "mode": "area",
                    "area": {"shape": "sphere", "radius_ft": 20},
                },
                "save": {
                    "ability": "dexterity",
                    "success": "half",
                    "damage": {"base_dice": "8d6", "damage_type": "fire"},
                },
            },
        }
    )
    actors = {
        "wizard": wizard,
        "ally": _spell_actor(slots=0),
        "goblin-1": _spell_actor(slots=0),
        "goblin-2": _spell_actor(slots=0),
        "goblin-3": _spell_actor(hp=0, slots=0),
    }
    positions = {
        "wizard": {"x": 0, "y": 0},
        "ally": {"x": 0, "y": 1},
        "goblin-1": {"x": 2, "y": 0},
        "goblin-2": {"x": 2, "y": 1},
        "goblin-3": {"x": 2, "y": 2},
    }
    combat = {
        "battle_map": {"bounds": {"width_cells": 12, "height_cells": 12}},
        "combatants": [
            {
                "actor_id": actor_id,
                "position": position,
                "conditions": [],
            }
            for actor_id, position in positions.items()
        ],
    }

    choice = _choose_agent_spell(
        "wizard",
        party_ids=["wizard", "ally"],
        actors=actors,
        living_targets=["goblin-1", "goblin-2"],
        spell_choices=[
            {
                "spell_id": fireball_id,
                "target_policy": "maximize_opponents_without_allies",
            }
        ],
        combat=combat,
    )

    assert choice is not None
    assert choice[:3] == (fireball_id, "goblin-1", 3)
    declaration = choice[3]
    assert {item["target_id"] for item in declaration["target_contexts"]} == {
        "goblin-1",
        "goblin-2",
        "goblin-3",
    }
    assert "ally" not in {
        item["target_id"] for item in declaration["target_contexts"]
    }


def test_area_spell_tactics_exclude_dead_combatants_from_target_contexts() -> None:
    fireball_id = "dnd5e.content.srd2014.spell.fireball"
    wizard = _spell_actor(fireball_id, slots=0)
    wizard["sheet"]["spellcasting"].update(
        {
            "preparation": {
                "mode": "spellbook",
                "selected_spell_ids": [fireball_id],
            },
            "spell_slots": {"3": {"value": 1, "max": 3}},
        }
    )
    wizard["sheet"]["content"]["spells"][0].update(
        {
            "level": 3,
            "access": {"prepared": True},
            "definition": {"range": {"normal_ft": 150}},
            "resolution": {
                "kind": "saving_throw",
                "targeting": {
                    "mode": "area",
                    "area": {"shape": "sphere", "radius_ft": 20},
                },
                "save": {
                    "ability": "dexterity",
                    "success": "half",
                    "damage": {"base_dice": "8d6", "damage_type": "fire"},
                },
            },
        }
    )
    actors = {
        "wizard": wizard,
        "goblin-1": _spell_actor(slots=0),
        "goblin-2": _spell_actor(slots=0),
        "goblin-dead": _spell_actor(hp=0, slots=0),
    }
    combat = {
        "battle_map": {"bounds": {"width_cells": 12, "height_cells": 12}},
        "combatants": [
            {
                "actor_id": "wizard",
                "position": {"x": 0, "y": 0},
                "conditions": [],
            },
            {
                "actor_id": "goblin-1",
                "position": {"x": 2, "y": 0},
                "conditions": [],
            },
            {
                "actor_id": "goblin-2",
                "position": {"x": 2, "y": 1},
                "conditions": [],
            },
            {
                "actor_id": "goblin-dead",
                "position": {"x": 2, "y": 2},
                "conditions": ["dead"],
            },
        ],
    }

    choice = _choose_agent_spell(
        "wizard",
        party_ids=["wizard"],
        actors=actors,
        living_targets=["goblin-1", "goblin-2"],
        spell_choices=[
            {
                "spell_id": fireball_id,
                "target_policy": "maximize_opponents_without_allies",
            }
        ],
        combat=combat,
    )

    assert choice is not None
    assert {
        item["target_id"] for item in choice[3]["target_contexts"]
    } == {"goblin-1", "goblin-2"}


def test_party_spell_tactics_choose_safe_hypnotic_pattern_cube() -> None:
    bard = _spell_actor(HYPNOTIC_PATTERN_ID, slots=0)
    bard["sheet"]["spellcasting"].update(
        {
            "preparation": {},
            "spell_slots": {"3": {"value": 1, "max": 3}},
        }
    )
    bard["sheet"]["content"]["spells"][0].update(
        {
            "level": 3,
            "access": {"known": True},
            "definition": {"range": {"normal_ft": 120}},
            # Historical campaign cards may predate the Core mechanic ref.
            "resolution": None,
            "mechanic_refs": [],
        }
    )
    actors = {
        "bard": bard,
        "ally": _spell_actor(slots=0),
        "ogre-1": _spell_actor(slots=0),
        "ogre-2": _spell_actor(slots=0),
        "giant": _spell_actor(slots=0),
    }
    positions = {
        "bard": {"x": 0, "y": 0},
        "ally": {"x": 0, "y": 1},
        "ogre-1": {"x": 5, "y": 4},
        "ogre-2": {"x": 6, "y": 5},
        "giant": {"x": 11, "y": 11},
    }
    combat = {
        "battle_map": {
            "bounds": {"width_cells": 12, "height_cells": 12},
            "grid": {"cell_ft": 5},
        },
        "combatants": [
            {
                "actor_id": actor_id,
                "position": position,
                "conditions": [],
                "disposition": (
                    "friendly" if actor_id in {"bard", "ally"} else "hostile"
                ),
            }
            for actor_id, position in positions.items()
        ],
    }

    choice = _choose_agent_spell(
        "bard",
        party_ids=["bard", "ally"],
        actors=actors,
        living_targets=["ogre-1", "ogre-2", "giant"],
        spell_choices=[
            {
                "spell_id": HYPNOTIC_PATTERN_ID,
                "target_policy": "maximize_opponents_without_allies",
            }
        ],
        combat=combat,
    )

    assert choice is not None
    assert choice[:3] == (HYPNOTIC_PATTERN_ID, "ogre-1", 3)
    assert choice[3] == {
        "origin": {"x": 1, "y": 0},
        "cube": {
            "min": {"x": 1, "y": 0},
            "max": {"x": 6, "y": 5},
        },
    }
    assert _area_spell_target_ids(
        choice[3],
        {
            "result": {
                "targets": [
                    {"target_id": "ogre-1"},
                    {"target_id": "ogre-2"},
                ]
            }
        },
    ) == ["ogre-1", "ogre-2"]


def test_party_spell_tactics_hydrate_legacy_lightning_bolt_line() -> None:
    spell_id = "dnd5e.content.srd2014.spell.lightning-bolt"
    wizard = _spell_actor(spell_id)
    wizard["sheet"]["content"]["spells"][0].update(
        {
            "level": 3,
            "access": {"known": True},
            "definition": {"range": {"kind": "self"}},
            "resolution": None,
            "mechanic_refs": [],
        }
    )
    wizard["sheet"]["spellcasting"]["spell_slots"] = {
        "3": {"value": 1, "max": 1}
    }
    actors = {
        "wizard": wizard,
        "ally": _spell_actor(slots=0),
        "goblin-1": _spell_actor(slots=0),
        "goblin-2": _spell_actor(slots=0),
        "off-line": _spell_actor(slots=0),
    }
    positions = {
        "wizard": {"x": 0, "y": 0},
        "ally": {"x": 0, "y": 1},
        "goblin-1": {"x": 2, "y": 0},
        "goblin-2": {"x": 4, "y": 0},
        "off-line": {"x": 2, "y": 1},
    }
    combat = {
        "battle_map": {
            "bounds": {"width_cells": 12, "height_cells": 12},
            "grid": {"cell_ft": 5},
        },
        "combatants": [
            {
                "actor_id": actor_id,
                "position": position,
                "conditions": [],
                "disposition": (
                    "friendly" if actor_id in {"wizard", "ally"} else "hostile"
                ),
            }
            for actor_id, position in positions.items()
        ],
    }
    priorities = _agent_spell_priorities(
        [
            {
                "actor_id": "wizard",
                "choices": [
                    {
                        "spell_id": spell_id,
                        "target_policy": "maximize_opponents_without_allies",
                    }
                ],
                "decision": "Cast through the two collinear enemies.",
                "ruling_reason": "The built-in line contract supplies exact geometry.",
            }
        ],
        participant_ids=list(actors),
        actors=actors,
    )

    choice = _choose_agent_spell(
        "wizard",
        party_ids=["wizard", "ally"],
        actors=actors,
        living_targets=["goblin-1", "goblin-2", "off-line"],
        spell_choices=priorities["wizard"]["choices"],
        combat=combat,
    )

    assert choice is not None
    assert choice[:3] == (spell_id, "goblin-1", 3)
    assert {
        item["target_id"] for item in choice[3]["target_contexts"]
    } == {"goblin-1", "goblin-2"}


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


def test_spell_damage_pending_concentration_blocks_turn_end() -> None:
    cast = {
        "combat": {
            "pending": [
                {
                    "id": "concentration-1",
                    "kind": "concentration",
                    "status": "pending",
                },
                {
                    "id": "concentration-2",
                    "kind": "concentration",
                    "status": "pending",
                },
            ]
        }
    }

    assert _spell_cast_blocks_turn_progress(cast, pending_reaction=False)
    cast["combat"]["pending"] = [
        {"id": "concentration-1", "kind": "concentration", "status": "resolved"}
    ]
    assert not _spell_cast_blocks_turn_progress(cast, pending_reaction=False)
    assert _spell_cast_blocks_turn_progress(cast, pending_reaction=True)


def test_pending_resolution_must_remove_or_resolve_exact_window() -> None:
    pending = {
        "id": "concentration-1",
        "kind": "concentration",
        "status": "pending",
    }

    assert not _pending_resolution_made_progress(
        pending,
        {"pending": [pending]},
    )
    assert _pending_resolution_made_progress(
        pending,
        {
            "pending": [
                {
                    "id": "concentration-1",
                    "kind": "concentration",
                    "status": "resolved",
                }
            ]
        },
    )
    assert _pending_resolution_made_progress(pending, {"pending": []})


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


def test_party_defeat_cannot_create_a_caller_named_success_checkpoint() -> None:
    assert _source_outcome_allows_checkpoint("victory") is True
    assert _source_outcome_allows_checkpoint("surrender") is True
    assert _source_outcome_allows_checkpoint("defeat") is False


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


def test_source_flee_becomes_ready_immediately_after_another_actors_damage() -> None:
    actors = {
        "glazhael": {
            "sheet": {
                "combat": {"hp": {"value": 39}},
                "conditions": [],
            }
        }
    }

    assert _ready_immediate_source_flee_actor_ids(
        flee_actor_ids={"glazhael"},
        actors=actors,
        already_fled_actor_ids=set(),
        damage_taken_by_actor={"glazhael": 161},
        flee_after_damage=161,
        critical_hit_actor_ids=set(),
        flee_on_critical=False,
    ) == ["glazhael"]
    assert _ready_immediate_source_flee_actor_ids(
        flee_actor_ids={"glazhael"},
        actors=actors,
        already_fled_actor_ids={"glazhael"},
        damage_taken_by_actor={"glazhael": 161},
        flee_after_damage=161,
        critical_hit_actor_ids=set(),
        flee_on_critical=False,
    ) == []


def test_source_flee_uses_authoritative_current_hp_after_driver_resume() -> None:
    actors = {
        "neronvain": {
            "sheet": {
                "combat": {"hp": {"value": 58, "max": 117}},
                "conditions": [],
            }
        }
    }

    assert _ready_immediate_source_flee_actor_ids(
        flee_actor_ids={"neronvain"},
        actors=actors,
        already_fled_actor_ids=set(),
        damage_taken_by_actor={"neronvain": 0},
        flee_after_damage=0,
        critical_hit_actor_ids=set(),
        flee_on_critical=False,
        flee_at_hp=58,
    ) == ["neronvain"]
    assert _source_flee_ready(
        acting_actor_id="neronvain",
        flee_actor_ids={"neronvain"},
        defeated_hostile_ids=[],
        flee_after_defeated=0,
        trigger_defeated_actor_id="",
        actor=actors["neronvain"],
        flee_at_hp=58,
    )

    actors["neronvain"]["sheet"]["combat"]["hp"]["value"] = 59
    assert _ready_immediate_source_flee_actor_ids(
        flee_actor_ids={"neronvain"},
        actors=actors,
        already_fled_actor_ids=set(),
        damage_taken_by_actor={"neronvain": 98},
        flee_after_damage=0,
        critical_hit_actor_ids=set(),
        flee_on_critical=False,
        flee_at_hp=58,
    ) == []


def test_immediate_source_flee_ignores_defeat_turn_triggers() -> None:
    actors = {
        "runner": {
            "sheet": {
                "combat": {"hp": {"value": 12}},
                "conditions": [],
            }
        }
    }

    assert _ready_immediate_source_flee_actor_ids(
        flee_actor_ids={"runner"},
        actors=actors,
        already_fled_actor_ids=set(),
        damage_taken_by_actor={"runner": 0},
        flee_after_damage=0,
        critical_hit_actor_ids=set(),
        flee_on_critical=False,
    ) == []


def test_linked_source_flee_waits_for_cited_trigger_and_active_arrival() -> None:
    actors = {
        "troll-1": {"sheet": {"combat": {"hp": {"value": 84}}, "conditions": []}},
        "troll-2": {"sheet": {"combat": {"hp": {"value": 84}}, "conditions": []}},
    }

    assert _ready_linked_source_flee_actor_ids(
        linked_flee_actor_ids={"troll-1", "troll-2"},
        trigger_fled_actor_id="dragon",
        fled_hostile_ids=set(),
        actors=actors,
        active_combatant_ids={"troll-1", "troll-2"},
    ) == []
    assert _ready_linked_source_flee_actor_ids(
        linked_flee_actor_ids={"troll-1", "troll-2"},
        trigger_fled_actor_id="dragon",
        fled_hostile_ids={"dragon"},
        actors=actors,
        active_combatant_ids={"troll-1"},
    ) == ["troll-1"]


def test_source_flee_configuration_allows_authored_damage_or_critical_alternatives() -> None:
    assert _validate_source_flee_configuration(
        SimpleNamespace(
            flee_actor_id=["lennithon"],
            flee_trigger_defeated_actor_id="",
            flee_on_start_actor_id="",
            flee_after_defeated=0,
            flee_after_damage=24,
            flee_on_critical=True,
            linked_flee_actor_id=["troll-1", "troll-2"],
            linked_flee_trigger_actor_id="lennithon",
            linked_flee_source_excerpt="If the dragon flees, the trolls retreat as well.",
            flee_source_excerpt="After it has taken 24 damage or one critical hit, it leaves.",
            source_excerpt=(
                "The dragon attacks the defenders. After it has taken 24 damage "
                "or one critical hit, it leaves. If the dragon flees, the trolls "
                "retreat as well."
            ),
        ),
        hostile_ids=["lennithon", "troll-1", "troll-2"],
    ) == {"lennithon"}


def test_source_flee_configuration_allows_authored_current_hp_threshold() -> None:
    assert _validate_source_flee_configuration(
        SimpleNamespace(
            flee_actor_id=["neronvain"],
            flee_trigger_defeated_actor_id="",
            flee_on_start_actor_id="",
            flee_after_defeated=0,
            flee_after_damage=0,
            flee_at_hp=58,
            flee_on_critical=False,
            linked_flee_actor_id=[],
            linked_flee_trigger_actor_id="",
            linked_flee_source_excerpt="",
            flee_source_excerpt=(
                "He flees when he is reduced to half his hit points or fewer."
            ),
            source_excerpt=(
                "He flees when he is reduced to half his hit points or fewer."
            ),
        ),
        hostile_ids=["neronvain"],
    ) == {"neronvain"}


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
        "linked_flee_actor_id": [],
        "linked_flee_trigger_actor_id": "",
        "linked_flee_source_excerpt": "",
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
                linked_flee_actor_id=[],
                linked_flee_trigger_actor_id="",
                linked_flee_source_excerpt="",
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


def test_source_surprise_report_preserves_cross_scene_exact_evidence(
    tmp_path,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "source-scene",
        "chunk_id": "chunk-1",
        "page_start": 46,
        "page_end": 46,
        "heading_path": ["Crypt of Diderius"],
        "content_sha256": "a" * 64,
    }
    report_path = tmp_path / "surprise-event.json"
    report_path.write_text(
        json.dumps(
            {
                "action": "record-event",
                "campaign_id": "campaign-1",
                "passed": True,
                "result": {
                    "scene": {
                        "scene_id": "source-scene",
                        "source_scene_id": "source-scene",
                        "source_ref": source_ref,
                    },
                    "continuity": {
                        "event": {
                            "id": "event-1",
                            "event_type": "source_boon_transition",
                            "summary": "Diderius opens the way and grants surprise.",
                            "payload": {
                                "scene_id": "source-scene",
                                "source_scene_id": "source-scene",
                                "source_ref": source_ref,
                                "source_excerpt": (
                                    "The party gains a surprise round to act against "
                                    "the guards there."
                                ),
                            },
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    evidence = _source_surprise_evidence_from_report(
        report_path,
        campaign_id="campaign-1",
    )
    surprise, basis = _source_declared_surprise(
        party_ids=["pc-1"],
        hostile_ids=["guard-1", "guard-2"],
        surprised_actor_ids=["guard-1", "guard-2"],
        source_excerpt=evidence["source_excerpt"],
        source_evidence=evidence,
    )

    assert surprise == {"pc-1": False, "guard-1": True, "guard-2": True}
    assert basis["source_evidence"]["source_ref"] == source_ref
    assert basis["source_evidence"]["event_id"] == "event-1"
    assert basis["source_excerpt"] == (
        "The party gains a surprise round to act against the guards there."
    )


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


def test_agent_object_interactions_require_an_exact_source_condition() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 24,
        "page_end": 24,
        "heading_path": ["Nightstone", "4G. Nesper Farm"],
        "content_sha256": "a" * 64,
    }
    source_excerpt = (
        "While wearing the pumpkins, the goblins are effectively blinded."
    )
    declaration = {
        "actor_id": "goblin-1",
        "round": 1,
        "object_description": "eyeless, hollowed-out pumpkin",
        "interaction": "remove",
        "condition": "Blinded",
        "source_ref": source_ref,
        "source_excerpt": source_excerpt,
        "decision": "The goblin removes the loose pumpkin from its own head.",
        "ruling_reason": (
            "Removing the ordinary worn object ends only the source-authored "
            "blindness it causes."
        ),
    }

    interactions = _agent_object_interactions(
        [declaration],
        participant_ids=["goblin-1", "pc-1"],
        source_conditions=[
            {
                "actor_id": "goblin-1",
                "condition": "blinded",
                "duration": "encounter",
                "source_ref": source_ref,
                "source_excerpt": source_excerpt,
            }
        ],
    )

    assert interactions[("goblin-1", 1)] == {
        "actor_id": "goblin-1",
        "round": 1,
        "object_description": "eyeless, hollowed-out pumpkin",
        "interaction": "remove",
        "condition": "blinded",
        "source_ref": source_ref,
        "source_excerpt": source_excerpt,
        "agent_ruling": {
            "default_resolver": "agent",
            "ruling_kind": "agent_dm_adjudication",
            "decision": "The goblin removes the loose pumpkin from its own head.",
            "reason": (
                "Removing the ordinary worn object ends only the source-authored "
                "blindness it causes."
            ),
        },
    }

    with pytest.raises(ValueError, match="does not match an exact"):
        _agent_object_interactions(
            [{**declaration, "source_excerpt": "A different sentence."}],
            participant_ids=["goblin-1", "pc-1"],
            source_conditions=[
                {
                    "actor_id": "goblin-1",
                    "condition": "blinded",
                    "source_ref": source_ref,
                    "source_excerpt": source_excerpt,
                }
            ],
        )


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


def test_agent_target_priorities_are_explicit_for_both_sides() -> None:
    priorities = _agent_target_priorities(
        [
            {
                "actor_ids": ["cleric", "rogue"],
                "priority_groups": [["kobold-1", "kobold-2"], ["drake"]],
                "decision": "Attack the kobolds in the listed order, then the drake.",
                "ruling_reason": (
                    "Remove the fragile bomb throwers before focusing the guard drake."
                ),
            },
            {
                "actor_ids": ["drake"],
                "priority_groups": [["rogue"], ["wizard"], ["cleric"]],
                "decision": "Attack the rogue first, followed by wizard and cleric.",
                "ruling_reason": "The Agent selected the drake's complete target order.",
            },
        ],
        party_ids=["cleric", "rogue", "wizard"],
        hostile_ids=["kobold-1", "kobold-2", "drake"],
    )

    assert priorities["cleric"] == priorities["rogue"]
    assert priorities["cleric"]["default_resolver"] == "agent"
    assert priorities["cleric"]["ruling_kind"] == "agent_dm_adjudication"
    assert _prioritize_targets(
        "cleric",
        ["drake", "kobold-2", "kobold-1"],
        priorities,
    ) == ["kobold-1", "kobold-2", "drake"]
    assert _prioritize_targets(
        "drake",
        ["cleric", "wizard", "rogue"],
        priorities,
    ) == ["rogue", "wizard", "cleric"]


def test_agent_target_priorities_reject_wrong_side_actors_and_targets() -> None:
    with pytest.raises(ValueError, match="same-side actor_ids"):
        _agent_target_priorities(
            [
                {
                    "actor_ids": ["cleric", "drake"],
                    "priority_groups": [["kobold"]],
                    "decision": "Mix party and hostile actors in one declaration.",
                    "ruling_reason": "The validator must reject this mixed side.",
                }
            ],
            party_ids=["cleric"],
            hostile_ids=["kobold", "drake"],
        )
    with pytest.raises(ValueError, match="opposing encounter participants"):
        _agent_target_priorities(
            [
                {
                    "actor_ids": ["cleric"],
                    "priority_groups": [["cleric"]],
                    "decision": "Select a friendly creature as an opponent.",
                    "ruling_reason": "The validator must reject this target.",
                }
            ],
            party_ids=["cleric"],
            hostile_ids=["kobold"],
        )


def test_agent_target_priority_may_refine_but_not_reverse_source_order() -> None:
    source = {
        "cleric": {
            "actor_ids": ["cleric"],
            "priority_groups": [["kobold-1", "kobold-2"], ["drake"]],
        }
    }
    valid = {
        "cleric": {
            "actor_ids": ["cleric"],
            "priority_groups": [["kobold-2"], ["kobold-1"], ["drake"]],
        }
    }
    _validate_agent_target_refinements(source, valid)

    invalid = {
        "cleric": {
            "actor_ids": ["cleric"],
            "priority_groups": [["drake"], ["kobold-1"], ["kobold-2"]],
        }
    }
    with pytest.raises(ValueError, match="contradicts"):
        _validate_agent_target_refinements(source, invalid)


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


@pytest.mark.parametrize(
    "event_type",
    ["movement_hazard_marked", "trap_detected", "trap_locations_shared"],
)
def test_source_avoidance_requires_public_actor_knowledge(
    tmp_path,
    event_type,
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
                            "event_type": event_type,
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
                "target_actor_ids": ["nezznar"],
                "willing_target_ids": ["nezznar"],
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
                "id": "conditional_extra_damage",
                "applies": False,
                "trigger_facts": {"attacker_enlarged": False},
                "default_resolver": "agent",
                "ruling_kind": "agent_dm_adjudication",
                "decision": "The conditional damage does not apply.",
                "reason": "The duergar is not enlarged for this attack.",
                "source_excerpt": ("or 11 (2d8 + 2) piercing damage while enlarged."),
            },
            {
                "actor_id": "jamna",
                "weapon_id": "shortsword",
                "id": "conditional_extra_damage",
                "applies": True,
                "damage_formula": "1d6",
                "damage_type": "piercing",
                "trigger_facts": {"target_size": "medium"},
                "default_resolver": "agent",
                "ruling_kind": "agent_dm_adjudication",
                "decision": "Apply the additional 1d6 piercing damage.",
                "reason": "The target is Medium, satisfying the exact source threshold.",
                "source_excerpt": (
                    "or 9 (1d6 + 3 plus 1d6) piercing damage if the target "
                    "is Medium or larger."
                ),
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
                    "much damage on a successful one. If the poison damage "
                    "reduces the target to 0 hit points, the target is stable but "
                    "poisoned for 1 hour, even after regaining hit points, and is "
                    "paralyzed while poisoned in this way."
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
            {
                "actor_id": "worg-1",
                "weapon_id": "bite",
                "id": "saving_throw_condition",
                "condition": "prone",
                "save_ability": "strength",
                "save_dc": 13,
                "source_excerpt": (
                    "If the target is a creature, it must succeed on a DC 13 "
                    "Strength saving throw or be knocked prone."
                ),
            },
            {
                "actor_id": "ettercap-1",
                "weapon_id": "web-garrote",
                "condition": "grappled",
                "escape_dc": 12,
                "escape_checks": ["athletics", "acrobatics"],
                "source_excerpt": (
                    "and the target is grappled (escape DC 12). Until this "
                    "grapple ends, the target can't breathe."
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
            "jamna",
            "worg-1",
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
    assert precombat[0]["target_actor_ids"] == ["nezznar"]
    assert precombat[0]["willing_target_ids"] == ["nezznar"]
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
    assert rulings[("ettercap-1", "web-garrote")] == {
        "actor_id": "ettercap-1",
        "weapon_id": "web-garrote",
        "id": "apply_condition",
        "condition": "grappled",
        "escape_dc": 12,
        "escape_checks": ["athletics", "acrobatics"],
        "source_excerpt": (
            "and the target is grappled (escape DC 12). Until this grapple "
            "ends, the target can't breathe."
        ),
    }
    assert rulings[("worg-1", "bite")] == {
        "actor_id": "worg-1",
        "weapon_id": "bite",
        "id": "saving_throw_condition",
        "condition": "prone",
        "save_ability": "strength",
        "save_dc": 13,
        "source_excerpt": (
            "If the target is a creature, it must succeed on a DC 13 "
            "Strength saving throw or be knocked prone."
        ),
    }
    assert rulings[("durnan", "grimvault")]["target_has_limbs"] is True
    assert rulings[("stirge", "blood-drain")]["id"] == "attachment"
    assert rulings[("duergar", "war-pick")] == {
        "actor_id": "duergar",
        "weapon_id": "war-pick",
        "id": "conditional_extra_damage",
        "applies": False,
        "trigger_facts": {"attacker_enlarged": False},
        "default_resolver": "agent",
        "ruling_kind": "agent_dm_adjudication",
        "decision": "The conditional damage does not apply.",
        "reason": "The duergar is not enlarged for this attack.",
        "source_excerpt": "or 11 (2d8 + 2) piercing damage while enlarged.",
    }
    assert rulings[("jamna", "shortsword")] == {
        "actor_id": "jamna",
        "weapon_id": "shortsword",
        "id": "conditional_extra_damage",
        "applies": True,
        "damage_formula": "1d6",
        "damage_type": "piercing",
        "trigger_facts": {"target_size": "medium"},
        "default_resolver": "agent",
        "ruling_kind": "agent_dm_adjudication",
        "decision": "Apply the additional 1d6 piercing damage.",
        "reason": "The target is Medium, satisfying the exact source threshold.",
        "source_excerpt": (
            "or 9 (1d6 + 3 plus 1d6) piercing damage if the target "
            "is Medium or larger."
        ),
    }
    assert delayed["nezznar"]["until_round"] == 2


def test_source_on_hit_ruling_accepts_agent_selected_direct_damage_type() -> None:
    excerpt = (
        "22 (5d8) damage of the type to which the dragonfang has damage resistance."
    )

    rulings = _source_on_hit_rulings(
        [
            {
                "actor_id": "dragonfang",
                "weapon_id": "orb",
                "id": "direct_damage",
                "damage_formula": "5d8",
                "damage_type": "acid",
                "trigger_facts": {"selected_damage_resistance": "acid"},
                "default_resolver": "agent",
                "ruling_kind": "agent_dm_adjudication",
                "decision": "Apply the reviewed orb damage as acid.",
                "reason": "The reviewed variant records acid resistance.",
                "source_excerpt": excerpt,
            }
        ],
        participant_ids=["dragonfang"],
        actors={
            "dragonfang": {
                "derived": {
                    "inventory": {
                        "weapon_attacks": [
                            {
                                "item_id": "orb",
                                "on_hit_effect": excerpt,
                            }
                        ]
                    }
                }
            }
        },
    )

    assert rulings[("dragonfang", "orb")] == {
        "actor_id": "dragonfang",
        "weapon_id": "orb",
        "id": "direct_damage",
        "damage_formula": "5d8",
        "damage_type": "acid",
        "trigger_facts": {"selected_damage_resistance": "acid"},
        "default_resolver": "agent",
        "ruling_kind": "agent_dm_adjudication",
        "decision": "Apply the reviewed orb damage as acid.",
        "reason": "The reviewed variant records acid resistance.",
        "source_excerpt": excerpt,
    }


def test_periodic_on_hit_damage_requires_an_explicit_agent_declaration() -> None:
    excerpt = (
        "If the target is a creature or a flammable object, it ignites. Until "
        "a creature takes an action to douse the fire, the target takes 5 "
        "(1d10) fire damage at the start of each of its turns."
    )
    ruling = {
        "actor_id": "fire-elemental",
        "weapon_id": "touch",
        "id": "ongoing_damage",
        "applies": True,
        "damage_formula": "1d10",
        "damage_type": "fire",
        "trigger_timing": "turn_start",
        "end_action": "improvise",
        "end_action_description": "douse the fire",
        "trigger_facts": {
            "target_id": "hero",
            "target_is_creature": True,
        },
        "default_resolver": "agent",
        "ruling_kind": "agent_dm_adjudication",
        "decision": "Apply the printed ongoing fire damage to the target.",
        "reason": "The target is a creature and the hit ignites it.",
        "source_excerpt": excerpt,
    }

    assert _source_on_hit_rulings(
        [ruling],
        participant_ids=["fire-elemental"],
    )[("fire-elemental", "touch")]["source_excerpt"] == excerpt


def test_source_on_hit_ruling_rejects_nonexact_actor_card_effect() -> None:
    excerpt = (
        "22 (5d8) damage of the type to which the dragonfang has damage resistance."
    )
    declaration = {
        "actor_id": "dragonfang",
        "weapon_id": "orb",
        "id": "direct_damage",
        "damage_formula": "5d8",
        "damage_type": "acid",
        "trigger_facts": {"selected_damage_resistance": "acid"},
        "default_resolver": "agent",
        "ruling_kind": "agent_dm_adjudication",
        "decision": "Apply the reviewed orb damage as acid.",
        "reason": "The reviewed variant records acid resistance.",
        "source_excerpt": f"Hit: {excerpt}",
    }

    with pytest.raises(ValueError, match="exactly match"):
        _source_on_hit_rulings(
            [declaration],
            participant_ids=["dragonfang"],
            actors={
                "dragonfang": {
                    "derived": {
                        "inventory": {
                            "weapon_attacks": [
                                {
                                    "item_id": "orb",
                                    "on_hit_effect": excerpt,
                                }
                            ]
                        }
                    }
                }
            },
        )


def test_source_ammunition_selection_requires_owned_source_stack() -> None:
    selections = _source_ammunition_selections(
        [
            {
                "actor_id": "archer",
                "weapon_id": "shortbow",
                "ammunition_item_id": "dragon-slaying-arrow",
            }
        ],
        participant_ids=["archer", "dragon"],
        actors={
            "archer": {
                "derived": {
                    "inventory": {
                        "weapon_attacks": [
                            {
                                "item_id": "shortbow",
                                "attack_type": "ranged",
                                "properties": ["ammunition", "two-handed"],
                            }
                        ]
                    }
                },
                "sheet": {
                    "inventory": {
                        "items": [
                            {
                                "id": "dragon-slaying-arrow",
                                "kind": "ammunition",
                                "quantity": 2,
                                "source_key": "module:chunk-1",
                            }
                        ]
                    }
                },
            }
        },
    )

    assert selections[("archer", "shortbow")]["ammunition_item_id"] == (
        "dragon-slaying-arrow"
    )


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

        async def core(self, tool_id: str, arguments: dict) -> dict:
            calls.append((tool_id, arguments))
            return {"state": {}}

    async def start(
        client: object,
        args: object,
        party_ids: list[str],
        hostile_ids: list[str],
        additional_hostile_ids: list[str],
        reinforcement_hostile_ids: list[str],
        reinforcement_ally_ids: list[str],
    ) -> dict[str, bool]:
        calls.append(
            (
                "start",
                (
                    party_ids,
                    hostile_ids,
                    additional_hostile_ids,
                    reinforcement_hostile_ids,
                    reinforcement_ally_ids,
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
                ["scout-1"],
            )
        )

    assert calls == [
        ("open", "campaign-1"),
        (
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": "campaign-1"},
                "principal_id": "system:local",
            },
        ),
        (
            "start",
            (
                ["pc-1"],
                ["hostile-1"],
                ["hostile-2"],
                ["hostile-3"],
                ["scout-1"],
            ),
        ),
        (
            "auto_run",
            (
                ["pc-1", "scout-1"],
                ["hostile-1", "hostile-2", "hostile-3"],
            ),
        ),
    ]
    assert result == {
        "completed": True,
        "auto_start": {"started": True},
    }


def test_auto_run_finalizes_retained_completed_combat_before_readiness() -> None:
    calls: list[tuple[str, object]] = []

    class Client:
        async def open(self, campaign_id: str) -> dict[str, str]:
            calls.append(("open", campaign_id))
            return {"phase": "play"}

        async def core(self, tool_id: str, arguments: dict) -> dict:
            calls.append((tool_id, arguments))
            return {
                "state": {
                    "combat": {
                        "id": "combat-1",
                        "name": "Test encounter",
                        "scene_id": "scene-1",
                        "active": False,
                        "combatants": [
                            {"actor_id": "pc-1"},
                            {"actor_id": "hostile-1"},
                            {"actor_id": "hostile-2"},
                        ],
                        "reinforcements": [
                            {"actor_id": "hostile-3"},
                            {"actor_id": "scout-1"},
                        ],
                        "participant_manifest": {
                            "initial_actor_ids": ["hostile-1", "hostile-2"],
                            "reinforcement_actor_ids": ["hostile-3", "scout-1"],
                        },
                        "outcome": {
                            "status": "victory",
                            "summary": "Source hostiles defeated.",
                        },
                    }
                }
            }

    async def finalize(
        client: object,
        args: object,
        actor_ids: list[str],
    ) -> dict[str, object]:
        calls.append(("finalize", actor_ids))
        return {"recovered_after_postcombat_interruption": True}

    async def fail_if_started(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("a retained completed combat must not start a new encounter")

    with (
        patch("scripts.regression_encounter._finalize_ended_encounter", finalize),
        patch("scripts.regression_encounter._start", fail_if_started),
        patch("scripts.regression_encounter._auto_run", fail_if_started),
    ):
        result = asyncio.run(
            _start_or_resume_auto_run(
                Client(),
                SimpleNamespace(
                    campaign_id="campaign-1",
                    scene_id="scene-1",
                    encounter_name="Test encounter",
                ),
                ["pc-1"],
                ["hostile-1"],
                ["hostile-2"],
                ["hostile-3"],
                ["scout-1"],
            )
        )

    assert calls == [
        ("open", "campaign-1"),
        (
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": "campaign-1"},
                "principal_id": "system:local",
            },
        ),
        (
            "finalize",
            [
                "pc-1",
                "scout-1",
                "hostile-1",
                "hostile-2",
                "hostile-3",
            ],
        ),
    ]
    assert result == {"recovered_after_postcombat_interruption": True}


def test_auto_run_does_not_finalize_a_different_completed_encounter() -> None:
    calls: list[tuple[str, object]] = []

    class Client:
        async def open(self, campaign_id: str) -> dict[str, str]:
            return {"phase": "play"}

        async def core(self, tool_id: str, arguments: dict) -> dict:
            return {
                "state": {
                    "combat": {
                        "id": "old-combat",
                        "name": "Old encounter",
                        "scene_id": "scene-1",
                        "active": False,
                        "combatants": [
                            {"actor_id": "pc-1"},
                            {"actor_id": "old-hostile"},
                        ],
                        "participant_manifest": {
                            "initial_actor_ids": ["old-hostile"],
                            "reinforcement_actor_ids": [],
                        },
                        "outcome": {"status": "victory"},
                    }
                }
            }

    async def start(
        client: object,
        args: object,
        party_ids: list[str],
        hostile_ids: list[str],
        additional_hostile_ids: list[str],
        reinforcement_hostile_ids: list[str],
        reinforcement_ally_ids: list[str],
    ) -> dict[str, bool]:
        calls.append(("start", hostile_ids))
        return {"started": True}

    async def auto_run(
        client: object,
        args: object,
        party_ids: list[str],
        hostile_ids: list[str],
    ) -> dict[str, bool]:
        calls.append(("auto_run", hostile_ids))
        return {"completed": True}

    async def fail_if_finalized(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("a different retained encounter must not be finalized")

    with (
        patch("scripts.regression_encounter._start", start),
        patch("scripts.regression_encounter._auto_run", auto_run),
        patch(
            "scripts.regression_encounter._finalize_ended_encounter",
            fail_if_finalized,
        ),
    ):
        result = asyncio.run(
            _start_or_resume_auto_run(
                Client(),
                SimpleNamespace(
                    campaign_id="campaign-1",
                    scene_id="scene-1",
                    encounter_name="New encounter",
                ),
                ["pc-1"],
                ["new-hostile"],
                [],
                [],
                [],
            )
        )

    assert calls == [
        ("start", ["new-hostile"]),
        ("auto_run", ["new-hostile"]),
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


def test_agent_death_trigger_rulings_bind_only_scene_facts() -> None:
    rulings = _agent_death_trigger_rulings(
        [
            {
                "actor_id": "magmin-1",
                "decision": "The loose straw bale in the burst area catches fire.",
                "ruling_reason": (
                    "The recorded object is flammable, unattended, and within "
                    "ten feet of the dying creature."
                ),
                "ignited_objects": [
                    {
                        "id": "loose-straw-bale",
                        "description": (
                            "An unattended loose straw bale beside the combatants."
                        ),
                    }
                ],
            }
        ],
        participant_ids=["magmin-1", "hero-1"],
    )

    assert rulings["magmin-1"] == {
        "actor_id": "magmin-1",
        "environment_ruling": {
            "default_resolver": "agent",
            "ruling_kind": "source_or_scene_fact",
            "decision": "The loose straw bale in the burst area catches fire.",
            "reason": (
                "The recorded object is flammable, unattended, and within "
                "ten feet of the dying creature."
            ),
            "ignited_objects": [
                {
                    "id": "loose-straw-bale",
                    "description": (
                        "An unattended loose straw bale beside the combatants."
                    ),
                }
            ],
        },
    }


def test_agent_death_trigger_rulings_require_explicit_object_list() -> None:
    with pytest.raises(ValueError, match="explicit ignited_objects"):
        _agent_death_trigger_rulings(
            [
                {
                    "actor_id": "magmin-1",
                    "decision": "Nothing else in the recorded area catches fire.",
                    "ruling_reason": (
                        "No qualifying unattended flammable object is established."
                    ),
                }
            ],
            participant_ids=["magmin-1"],
        )


def test_interrupted_standard_death_burst_uses_agent_scene_fact_ruling() -> None:
    calls: list[tuple[str, dict]] = []

    class Client:
        async def core(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "campaign_query"
            return {"revision": 23}

        async def domain(self, tool_id: str, arguments: dict) -> dict:
            calls.append((tool_id, arguments))
            return {"result": {"status": "committed"}}

    result = asyncio.run(
        _resolve_pending(
            Client(),
            SimpleNamespace(
                campaign_id="campaign-1",
                agent_death_trigger_ruling_json=[
                    {
                        "actor_id": "magmin-1",
                        "decision": (
                            "No qualifying unattended flammable object is "
                            "established in the recorded burst area."
                        ),
                        "ruling_reason": (
                            "The combat map records creature positions but no "
                            "eligible loose object at this location."
                        ),
                        "ignited_objects": [],
                    }
                ],
            ),
            "branch-1",
            {
                "combatants": [
                    {"actor_id": "magmin-1"},
                    {"actor_id": "hero-1"},
                ],
                "pending": [
                    {
                        "id": "death-window-1",
                        "kind": "save",
                        "actor_id": "magmin-1",
                        "source_actor_id": "magmin-1",
                        "trigger": "standard_death_burst",
                        "status": "pending",
                    }
                ],
            },
        )
    )

    assert result == {"status": "committed"}
    assert calls == [
        (
            "combat_choice",
            {
                "campaign_id": "campaign-1",
                "action": "resolve_death_trigger",
                "actor_id": "magmin-1",
                "payload": {
                    "choice_id": "death-window-1",
                    "environment_ruling": {
                        "default_resolver": "agent",
                        "ruling_kind": "source_or_scene_fact",
                        "decision": (
                            "No qualifying unattended flammable object is "
                            "established in the recorded burst area."
                        ),
                        "reason": (
                            "The combat map records creature positions but no "
                            "eligible loose object at this location."
                        ),
                        "ignited_objects": [],
                    },
                },
                "branch_id": "branch-1",
                "expected_revision": 23,
                "idempotency_key": calls[0][1]["idempotency_key"],
            },
        )
    ]


def test_interrupted_standard_death_burst_requests_agent_when_absent() -> None:
    class Client:
        async def core(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "campaign_query"
            return {"revision": 24}

    with pytest.raises(
        EncounterRulingRequiredError,
        match="standard death trigger",
    ):
        asyncio.run(
            _resolve_pending(
                Client(),
                SimpleNamespace(campaign_id="campaign-1"),
                "branch-1",
                {
                    "combatants": [{"actor_id": "magmin-1"}],
                    "pending": [
                        {
                            "id": "death-window-2",
                            "actor_id": "magmin-1",
                            "source_actor_id": "magmin-1",
                            "trigger": "standard_death_burst",
                            "status": "pending",
                        }
                    ],
                },
            )
        )


def test_interrupted_periodic_damage_resumes_with_explicit_agent_ruling() -> None:
    calls: list[tuple[str, dict]] = []
    excerpt = (
        "If the target is a creature or a flammable object, it ignites. Until "
        "a creature takes an action to douse the fire, the target takes 5 "
        "(1d10) fire damage at the start of each of its turns."
    )

    class Client:
        async def core(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "campaign_query"
            return {"revision": 22}

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
                        "actor_id": "fire-elemental",
                        "weapon_id": "touch",
                        "id": "ongoing_damage",
                        "applies": True,
                        "damage_formula": "1d10",
                        "damage_type": "fire",
                        "trigger_timing": "turn_start",
                        "end_action": "improvise",
                        "end_action_description": "douse the fire",
                        "trigger_facts": {
                            "target_id": "hero",
                            "target_is_creature": True,
                        },
                        "default_resolver": "agent",
                        "ruling_kind": "agent_dm_adjudication",
                        "decision": "Apply the printed ongoing fire damage.",
                        "reason": "The target is a creature ignited by the hit.",
                        "source_excerpt": excerpt,
                    }
                ],
            ),
            "branch-1",
            {
                "pending": [
                    {
                        "id": "choice-fire",
                        "kind": "ruling",
                        "actor_id": "hero",
                        "attacker_id": "fire-elemental",
                        "target_id": "hero",
                        "weapon_id": "touch",
                        "trigger": "attack_on_hit_effect",
                        "effect": excerpt,
                        "status": "pending",
                    }
                ]
            },
        )
    )

    assert result == {"status": "committed"}
    selection = calls[0][1]["payload"]["selection"]
    assert selection["id"] == "ongoing_damage"
    assert selection["damage_formula"] == "1d10"
    assert selection["trigger_timing"] == "turn_start"
    assert selection["end_action_description"] == "douse the fire"
    assert calls[0][1]["actor_id"] == "hero"


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


def test_interrupted_source_alternative_damage_resumes_with_agent_false_ruling() -> None:
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
                        "id": "conditional_extra_damage",
                        "applies": False,
                        "trigger_facts": {"attacker_enlarged": False},
                        "default_resolver": "agent",
                        "ruling_kind": "agent_dm_adjudication",
                        "decision": "The enlarged alternative does not apply.",
                        "reason": "The attacker is not enlarged in the current encounter state.",
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
        "id": "conditional_extra_damage",
        "applies": False,
        "trigger_facts": {"attacker_enlarged": False},
        "default_resolver": "agent",
        "ruling_kind": "agent_dm_adjudication",
        "decision": "The enlarged alternative does not apply.",
        "reason": "The attacker is not enlarged in the current encounter state.",
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


def test_encounter_manifest_tracks_friendly_source_reinforcements() -> None:
    source_excerpt = (
        "If the characters are in danger of being overwhelmed, eight elves "
        "arrive from the north to assist them."
    )
    manifest = _participant_manifest(
        ["orc"],
        label="Ear Seekers",
        source_excerpt="One orc attacks the village.",
        reinforcement_ally_ids=["scout-1", "scout-2"],
        reinforcement_ally_label="Ardeep Forest scouts",
        reinforcement_ally_source_excerpt=source_excerpt,
    )

    assert manifest["groups"][1] == {
        "key": "source-friendly-reinforcements",
        "label": "Ardeep Forest scouts",
        "role": "reinforcement",
        "required_count": 2,
        "actor_ids": ["scout-1", "scout-2"],
        "source_excerpt": source_excerpt,
    }


def test_agent_semantic_reinforcement_trigger_is_source_bound() -> None:
    source_excerpt = (
        "If the characters are in danger of being overwhelmed, eight elves "
        "arrive from the north to assist them."
    )
    triggers = _agent_reinforcement_triggers(
        [
            {
                "actor_ids": ["scout-1", "scout-2"],
                "trigger_round": 4,
                "source_excerpt": source_excerpt,
                "decision": "The defenders are now in danger of being overwhelmed.",
                "ruling_reason": (
                    "Multiple defenders are down while a large hostile force "
                    "remains active, so the module's semantic condition is met."
                ),
            }
        ],
        reinforcement_ids=["scout-1", "scout-2"],
        reinforcement_round=4,
        encounter_source_excerpt=f"Before. {source_excerpt} After.",
    )

    assert triggers[0]["trigger_round"] == 4
    assert triggers[0]["agent_ruling"]["default_resolver"] == "agent"

    with pytest.raises(ValueError, match="requires unique prepared reinforcements"):
        _agent_reinforcement_triggers(
            [
                {
                    "actor_ids": ["scout-1"],
                    "trigger_round": 4,
                    "source_excerpt": "This sentence is not in the module.",
                    "decision": "The defenders are now in danger of being overwhelmed.",
                    "ruling_reason": "The Agent must not invent source evidence.",
                }
            ],
            reinforcement_ids=["scout-1"],
            reinforcement_round=4,
            encounter_source_excerpt=source_excerpt,
        )


def test_source_reinforcements_enter_openly_at_configured_round_positions() -> None:
    first = _reinforcement_config("rift-1", 0)
    source_conditions = [
        {
            "condition": "restrained",
            "source_excerpt": "The reinforcement arrives restrained.",
        }
    ]
    source_traits = [
        {
            "kind": "regeneration",
            "feature_id": "regeneration-passive",
            "source_excerpt": "The troll regains 10 hit points at the start of its turn.",
        }
    ]
    second = _reinforcement_config(
        "rift-2",
        1,
        join_round=7,
        tie_breaker=8,
        source_conditions=source_conditions,
        source_traits=source_traits,
    )

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
    assert second["source_conditions"] == source_conditions
    assert second["source_traits"] == source_traits

    source_conditions[0]["condition"] = "poisoned"
    source_traits[0]["feature_id"] = "changed"
    assert second["source_conditions"][0]["condition"] == "restrained"
    assert second["source_traits"][0]["feature_id"] == "regeneration-passive"
    friendly = _reinforcement_config(
        "scout-1",
        2,
        disposition="friendly",
        join_round=4,
    )
    assert friendly["disposition"] == "friendly"
    assert friendly["join_round"] == 4


def test_partial_start_recovery_queues_only_missing_source_reinforcements() -> None:
    combat = {
        "active": True,
        "scene_id": "scene-1",
        "participant_manifest": {
            "reinforcement_actor_ids": ["rift-1", "rift-2"],
        },
        "combatants": [{"actor_id": "guard"}],
        "reinforcements": [{"actor_id": "rift-1"}],
    }

    assert _missing_source_reinforcement_ids(
        combat,
        scene_id="scene-1",
        reinforcement_ids=["rift-1", "rift-2"],
    ) == ["rift-2"]

    combat["combatants"].append({"actor_id": "rift-2"})
    assert (
        _missing_source_reinforcement_ids(
            combat,
            scene_id="scene-1",
            reinforcement_ids=["rift-1", "rift-2"],
        )
        == []
    )

    with pytest.raises(RuntimeError, match="manifest does not match"):
        _missing_source_reinforcement_ids(
            combat,
            scene_id="scene-1",
            reinforcement_ids=["other"],
        )


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


def test_default_layout_fits_large_source_authored_battle_without_scaling() -> None:
    party_ids = [f"pc-{index}" for index in range(1, 7)]
    ally_ids = [f"ally-{index}" for index in range(1, 9)]
    hostile_ids = [f"hostile-{index}" for index in range(1, 23)]

    config = _participant_config(
        party_ids,
        hostile_ids,
        ally_ids=ally_ids,
        surprise_by_actor={},
        hostiles_hidden=False,
    )

    assert len(config) == 36
    positions = [
        (int(item["position"]["x"]), int(item["position"]["y"]))
        for item in config
    ]
    assert len(positions) == len(set(positions))
    assert all(0 <= x < 12 and 0 <= y < 12 for x, y in positions)
    by_actor = {item["actor_id"]: item for item in config}
    assert by_actor["pc-6"]["position"] == {"x": 1, "y": 6}
    assert by_actor["ally-8"]["position"] == {"x": 0, "y": 8}
    assert by_actor["hostile-21"]["position"] == {"x": 10, "y": 9}
    assert by_actor["hostile-22"]["position"] == {"x": 2, "y": 1}


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


def test_source_eleven_hostile_layout_keeps_every_actor_on_a_unique_space() -> None:
    party_ids = [f"pc-{index}" for index in range(1, 5)]
    hostile_ids = [f"ritual-hostile-{index}" for index in range(1, 12)]

    config = _participant_config(party_ids, hostile_ids, surprise_by_actor={})
    positions = [(item["position"]["x"], item["position"]["y"]) for item in config]

    assert len(config) == 15
    assert len(positions) == len(set(positions))
    assert {item["actor_id"] for item in config} == {*party_ids, *hostile_ids}
    assert next(
        item["position"] for item in config if item["actor_id"] == "ritual-hostile-11"
    ) == {"x": 10, "y": 6}


def test_movement_idempotency_distinguishes_replanned_destinations() -> None:
    args = SimpleNamespace(operation_scope="encounter-scope", run_id="run-1")
    first = ({"x": 3, "y": 2}, 10, [{"x": 2, "y": 2}, {"x": 3, "y": 2}])
    replanned = ({"x": 4, "y": 2}, 15, [*first[2], {"x": 4, "y": 2}])

    first_token = _movement_operation_token(
        args,
        sequence=17,
        actor_id="mage",
        target_id="hero",
        destination=first,
    )

    assert first_token == _movement_operation_token(
        args,
        sequence=17,
        actor_id="mage",
        target_id="hero",
        destination=first,
    )
    assert first_token != _movement_operation_token(
        args,
        sequence=17,
        actor_id="mage",
        target_id="hero",
        destination=replanned,
    )


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


def test_movement_destination_reports_difficult_terrain_cost() -> None:
    combat = {
        "battle_map": {
            "bounds": {"width_cells": 5, "height_cells": 1},
            "blocked_cells": [],
            "difficult_cells": ["1,0"],
        },
        "combatants": [
            {
                "actor_id": "pc",
                "position": {"x": 0, "y": 0},
                "turn_budget": {"movement": 15},
            },
            {
                "actor_id": "goblin",
                "position": {"x": 4, "y": 0},
                "turn_budget": {"movement": 30},
            },
        ],
    }

    destination = _choose_destination(combat, "pc", "goblin")

    assert destination == (
        {"x": 2, "y": 0},
        15,
        [{"x": 1, "y": 0}, {"x": 2, "y": 0}],
    )
    assert (
        _destination_within_range(
            destination[0],
            {"x": 4, "y": 0},
            range_ft=5,
        )
        is False
    )


def test_destination_range_requires_actual_melee_reach() -> None:
    assert _destination_within_range(
        {"x": 3, "y": 3},
        {"x": 4, "y": 4},
        range_ft=5,
    )
    assert not _destination_within_range(
        {"x": 2, "y": 3},
        {"x": 4, "y": 4},
        range_ft=5,
    )


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


def test_agent_weapon_priority_is_explicit_and_card_validated() -> None:
    goblin = {
        "id": "goblin",
        "derived": {
            "inventory": {
                "weapon_attacks": [
                    {"item_id": "scimitar", "attack_type": "melee"},
                    {"item_id": "shortbow", "attack_type": "ranged"},
                ]
            },
            "multiattack_options": [],
        }
    }

    priorities = _agent_weapon_priorities(
        [
            {
                "actor_id": "goblin",
                "choices": [
                    {"weapon_id": "shortbow", "attack_mode": "ranged"},
                    {"weapon_id": "scimitar", "attack_mode": "melee"},
                ],
                "decision": "Use the bow while possible, then draw the scimitar.",
                "ruling_reason": "This is the Agent's explicit encounter tactic.",
            }
        ],
        participant_ids=["goblin"],
        actors={"goblin": goblin},
    )

    assert priorities["goblin"]["choices"] == [
        {"weapon_id": "shortbow", "attack_mode": "ranged"},
        {"weapon_id": "scimitar", "attack_mode": "melee"},
    ]
    assert (
        priorities["goblin"]["agent_ruling"]["ruling_kind"]
        == "agent_dm_adjudication"
    )


def test_agent_can_select_standard_unarmed_strike_with_other_weapons() -> None:
    fighter = {
        "id": "fighter",
        "derived": {
            "inventory": {
                "weapon_attacks": [
                    {"item_id": "longsword", "attack_type": "melee"},
                ]
            },
            "multiattack_options": [],
        },
    }

    priorities = _agent_weapon_priorities(
        [
            {
                "actor_id": "fighter",
                "choices": [
                    {
                        "weapon_id": "unarmed-strike",
                        "attack_mode": "melee",
                    }
                ],
                "decision": "Use the standard unarmed strike against this target.",
                "ruling_reason": "Every character retains this engine-owned attack.",
            }
        ],
        participant_ids=["fighter"],
        actors={"fighter": fighter},
    )

    assert priorities["fighter"]["choices"] == [
        {"weapon_id": "unarmed-strike", "attack_mode": "melee"}
    ]


def test_agent_multiattack_priority_must_begin_with_the_declared_attack() -> None:
    actor = {
        "id": "archer",
        "derived": {
            "inventory": {
                "weapon_attacks": [
                    {"item_id": "shortsword", "attack_type": "melee"},
                    {"item_id": "shortbow", "attack_type": "ranged"},
                ]
            },
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

    priorities = _agent_weapon_priorities(
        [
            {
                "actor_id": "archer",
                "choices": [
                    {
                        "weapon_id": "shortbow",
                        "attack_mode": "ranged",
                        "multiattack_option_id": "ranged",
                    }
                ],
                "decision": "Use the reviewed ranged Multiattack option.",
                "ruling_reason": "Both attacks can reach the selected target.",
            }
        ],
        participant_ids=["archer"],
        actors={"archer": actor},
    )
    assert priorities["archer"]["choices"][0]["multiattack_option_id"] == "ranged"

    with pytest.raises(ValueError, match="begin with that exact attack"):
        _agent_weapon_priorities(
            [
                {
                    "actor_id": "archer",
                    "choices": [
                        {
                            "weapon_id": "shortbow",
                            "attack_mode": "ranged",
                            "multiattack_option_id": "melee",
                        }
                    ],
                    "decision": "Use an inconsistent Multiattack declaration.",
                    "ruling_reason": "The validator must reject this mismatch.",
                }
            ],
            participant_ids=["archer"],
            actors={"archer": actor},
        )


def test_completed_source_opening_weapons_are_rebuilt_from_public_combat_log() -> None:
    combat = {
        "log": [
            {
                "type": "attack",
                "result": {
                    "attacker_id": "ettercap-1",
                    "weapon_id": "web-garrote",
                    "attack_mode": "melee",
                    "hit": False,
                },
            },
            {
                "type": "attack",
                "result": {
                    "attacker_id": "ettercap-2",
                    "weapon_id": "bite",
                    "attack_mode": "melee",
                    "hit": True,
                },
            },
            {
                "type": "attack",
                "result": {
                    "attacker_id": "unrelated",
                    "weapon_id": "web-garrote",
                    "attack_mode": "melee",
                    "hit": True,
                },
            },
        ]
    }
    openings = {
        "ettercap-1": {"weapon_id": "web-garrote"},
        "ettercap-2": {"weapon_id": "web-garrote"},
    }

    assert _completed_source_opening_weapon_actor_ids(combat, openings) == {
        "ettercap-1"
    }
    assert (
        _required_source_opening_weapon(
            openings,
            actor_id="ettercap-1",
            completed_actor_ids={"ettercap-1"},
        )
        is None
    )
    assert _required_source_opening_weapon(
        openings,
        actor_id="ettercap-2",
        completed_actor_ids={"ettercap-1"},
    ) == {"weapon_id": "web-garrote"}


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
