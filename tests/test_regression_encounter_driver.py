from scripts.regression_encounter import (
    _choose_destination,
    _participant_config,
    _participant_manifest,
    _roll_total,
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
