import json

from scripts.regression_encounter import (
    GUIDING_BOLT_ID,
    HEALING_WORD_ID,
    MAGIC_MISSILE_ID,
    _choose_destination,
    _choose_party_spell,
    _has_blocking_pending,
    _has_multiattack_followup,
    _observable_target_ids,
    _participant_config,
    _participant_manifest,
    _preferred_hostile_weapon_id,
    _preferred_multiattack_option_id,
    _roll_total,
    _should_stand,
    _source_departure_patch,
    _source_outcome,
    _source_truce_outcome,
    _surprise_from_check_report,
    _surprise_from_hostile_stealth_totals,
    _validate_hostile_attacks,
    _wound_priority,
)


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
    ) == (HEALING_WORD_ID, "ally")

    actors["ally"]["sheet"]["combat"]["hp"]["value"] = 3
    assert _choose_party_spell(
        "cleric",
        party_ids=["cleric", "wizard", "ally"],
        actors=actors,
        living_targets=["goblin"],
    ) == (GUIDING_BOLT_ID, "goblin")
    assert _choose_party_spell(
        "wizard",
        party_ids=["cleric", "wizard", "ally"],
        actors=actors,
        living_targets=["goblin"],
    ) == (MAGIC_MISSILE_ID, "goblin")
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
    assert not _has_blocking_pending(
        {"pending": [{"id": "reaction-1", "status": "resolved"}]}
    )


def test_all_source_hostiles_defeated_is_victory_without_flee_rule() -> None:
    assert _source_outcome(
        defeated_hostiles=2,
        hostile_count=2,
        flee_after_defeated=0,
        unresolved_party=False,
        party_down=False,
    ) == ("victory", "All 2 source-defined hostiles were defeated.")


def test_specific_source_flee_counts_only_that_hostile_as_resolved() -> None:
    assert _source_outcome(
        defeated_hostiles=3,
        fled_hostiles=1,
        hostile_count=4,
        flee_after_defeated=0,
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
            flee_after_defeated=0,
            unresolved_party=False,
            party_down=False,
        )
        is None
    )


def test_party_defeat_does_not_invent_a_source_defined_aftermath() -> None:
    assert _source_outcome(
        defeated_hostiles=1,
        hostile_count=4,
        flee_after_defeated=0,
        unresolved_party=False,
        party_down=True,
    ) == (
        "defeat",
        "The party was defeated. Combat ended with resolved unconscious or dead "
        "characters; their later treatment requires explicit source support or DM review.",
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


def test_source_six_hostile_layout_keeps_every_actor_on_a_unique_space() -> None:
    party_ids = [f"pc-{index}" for index in range(1, 6)]
    hostile_ids = [f"goblin-{index}" for index in range(1, 7)]

    config = _participant_config(party_ids, hostile_ids, surprise_by_actor={})
    positions = [
        (item["position"]["x"], item["position"]["y"])
        for item in config
    ]

    assert len(config) == 11
    assert len(positions) == len(set(positions))
    assert {item["actor_id"] for item in config} == {*party_ids, *hostile_ids}


def test_no_surprise_layout_marks_neither_side_surprised() -> None:
    party_ids = ["pc-1", "pc-2"]
    hostile_ids = ["goblin-1", "goblin-2"]

    config = _participant_config(
        party_ids,
        hostile_ids,
        surprise_by_actor={
            actor_id: False for actor_id in [*party_ids, *hostile_ids]
        },
        hostiles_hidden=False,
    )

    assert all(item["surprised"] is False for item in config)
    assert all(item.get("hidden") is False for item in config if item["actor_id"] in hostile_ids)


def test_source_cited_scout_check_surprises_only_hostiles(tmp_path) -> None:
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
        "goblin-1": True,
        "goblin-2": True,
    }
    assert basis["mode"] == "source_cited_party_scout"


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
    assert max(
        abs(destination[0]["x"] - 7),
        abs(destination[0]["y"] - 2),
    ) == 1
    assert destination[1] <= 30


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
                    "attacks": [
                        {"weapon_id": "shortsword", "attack_mode": "melee", "count": 2}
                    ],
                },
                {
                    "id": "ranged",
                    "attacks": [
                        {"weapon_id": "shortbow", "attack_mode": "ranged", "count": 2}
                    ],
                },
            ]
        }
    }

    assert (
        _preferred_multiattack_option_id(actor, preferred_weapon_id="shortsword")
        == "melee"
    )
    assert (
        _preferred_multiattack_option_id(actor, preferred_weapon_id="shortbow")
        == "ranged"
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
