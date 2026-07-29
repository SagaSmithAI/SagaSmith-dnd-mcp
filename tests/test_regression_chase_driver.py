from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

import scripts.regression_chase as regression_chase


def test_chase_parser_accepts_deferred_scene_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "regression_chase.py",
            "--home",
            str(tmp_path / "home"),
            "--campaign-id",
            "campaign-1",
            "--output",
            str(tmp_path / "report.json"),
            "--run-id",
            "run-1",
            "--party-report",
            str(tmp_path / "party.json"),
            "--quarry-actor-id",
            "quarry-1",
            "--scene-id",
            "scene-1",
            "--source-ref-json",
            '{"module_id":"module-1"}',
            "--source-excerpt",
            "Use the chase rules.",
            "--initial-distance-ft",
            "60",
            "--agent-start-ruling-json",
            (
                '{"initial_distance_ft":60,"decision":"The chase begins sixty '
                'feet apart.","ruling_reason":"The reviewed scene states the '
                'exact starting separation."}'
            ),
            "--agent-turn-policy-json",
            (
                '{"actor_id":"pursuer-1","turn_action":"dash",'
                '"stand_from_prone":true,"complication_choices":'
                '{"1":"acrobatics","2":"athletics","3":"strength",'
                '"4":"intelligence","5":"dexterity","6":"acrobatics",'
                '"7":"athletics","8":"athletics","9":"","10":"dexterity"},'
                '"decision":"The pursuer runs at full speed.",'
                '"ruling_reason":"Closing the source-defined lead is the '
                'pursuer strategy."}'
            ),
            "--agent-turn-policy-json",
            (
                '{"actor_id":"quarry-1","turn_action":"dash",'
                '"stand_from_prone":true,"complication_choices":'
                '{"1":"acrobatics","2":"acrobatics","3":"strength",'
                '"4":"acrobatics","5":"dexterity","6":"acrobatics",'
                '"7":"acrobatics","8":"acrobatics","9":"","10":"dexterity"},'
                '"decision":"The quarry runs at full speed.",'
                '"ruling_reason":"Escaping the visible pursuer is the quarry '
                'strategy."}'
            ),
            "--agent-quarry-visibility-ruling-json",
            (
                '{"quarry_visibility":{"quarry-1":true},'
                '"decision":"The quarry remains visible in the street.",'
                '"ruling_reason":"The reviewed street has no intervening '
                'obstruction at this point."}'
            ),
            "--checkpoint-label",
            "Street chase resolved",
            "--defer-checkpoint",
        ],
    )

    assert regression_chase._arguments().defer_checkpoint is True


def _complication_choices(*, second: str = "athletics") -> dict[str, str]:
    return {
        "1": "acrobatics",
        "2": second,
        "3": "strength",
        "4": "intelligence",
        "5": "dexterity",
        "6": "acrobatics",
        "7": "athletics",
        "8": "athletics",
        "9": "",
        "10": "dexterity",
    }


def test_chase_agent_policies_preserve_decisions_instead_of_optimizing() -> None:
    policies = regression_chase._agent_turn_policies(
        [
            {
                "actor_id": "pursuer",
                "turn_action": "move",
                "stand_from_prone": False,
                "complication_choices": _complication_choices(
                    second="acrobatics"
                ),
                "decision": "The pursuer advances cautiously without dashing.",
                "ruling_reason": (
                    "The actor preserves stamina despite having a higher "
                    "Athletics modifier."
                ),
            },
            {
                "actor_id": "quarry",
                "turn_action": "dash",
                "stand_from_prone": True,
                "complication_choices": _complication_choices(),
                "decision": "The quarry keeps running and stands when prone.",
                "ruling_reason": (
                    "The quarry values escape over conserving its free dashes."
                ),
            },
        ],
        participant_ids=["pursuer", "quarry"],
    )

    assert policies["pursuer"]["turn_action"] == "move"
    assert policies["pursuer"]["stand_from_prone"] is False
    assert policies["pursuer"]["complication_choices"]["2"] == "acrobatics"
    assert policies["pursuer"]["default_resolver"] == "agent"


def test_chase_agent_policy_requires_every_participant() -> None:
    with pytest.raises(ValueError, match="cover every participant"):
        regression_chase._agent_turn_policies(
            [
                {
                    "actor_id": "pursuer",
                    "turn_action": "dash",
                    "stand_from_prone": True,
                    "complication_choices": _complication_choices(),
                    "decision": "The pursuer runs at full speed each turn.",
                    "ruling_reason": "The declared strategy is to close the lead.",
                }
            ],
            participant_ids=["pursuer", "quarry"],
        )


def test_chase_visibility_and_speed_adjustments_are_explicit() -> None:
    visibility = regression_chase._agent_quarry_visibility_ruling(
        {
            "quarry_visibility": {"quarry": False},
            "decision": "The quarry passes behind the ruined pantry wall.",
            "ruling_reason": (
                "The current scene gives the quarry full visual obstruction."
            ),
        },
        quarry_ids=["quarry"],
    )
    speed = regression_chase._source_speed_adjustments(
        [
            {
                "actor_id": "quarry",
                "speed_adjustment_ft": -10,
                "source_excerpt": (
                    "While dragging the heavily laden sack, Gum-Gum suffers "
                    "a 10-foot reduction to her speed."
                ),
            }
        ],
        participant_ids=["pursuer", "quarry"],
        source_excerpt=(
            "While dragging the heavily laden sack, Gum-Gum suffers a "
            "10-foot reduction to her speed."
        ),
    )

    assert visibility["quarry_visibility"] == {"quarry": False}
    assert visibility["default_resolver"] == "agent"
    assert speed == [
        {
            "actor_id": "quarry",
            "speed_adjustment_ft": -10,
            "source_excerpt": (
                "While dragging the heavily laden sack, Gum-Gum suffers "
                "a 10-foot reduction to her speed."
            ),
        }
    ]


def test_deferred_chase_does_not_create_a_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    async def checkpoint(client, **kwargs):
        calls.append(kwargs)
        return {"snapshot": {"id": "snapshot-1"}}

    monkeypatch.setattr(regression_chase, "_checkpoint", checkpoint)

    result = asyncio.run(
        regression_chase._finalize_chase_checkpoint(
            object(),
            campaign_id="campaign-1",
            run_id="run-1",
            label="Street chase resolved",
            chase_id="chase-1",
            defer_checkpoint=True,
        )
    )

    assert result is None
    assert calls == []


def test_non_deferred_chase_keeps_terminal_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    async def checkpoint(client, **kwargs):
        calls.append(kwargs)
        return {"snapshot": {"id": "snapshot-1"}}

    monkeypatch.setattr(regression_chase, "_checkpoint", checkpoint)

    result = asyncio.run(
        regression_chase._finalize_chase_checkpoint(
            object(),
            campaign_id="campaign-1",
            run_id="run-1",
            label="Street chase resolved",
            chase_id="chase-1",
            defer_checkpoint=False,
        )
    )

    assert result == {"snapshot": {"id": "snapshot-1"}}
    assert calls == [
        {
            "campaign_id": "campaign-1",
            "run_id": "run-1",
            "label": "Street chase resolved",
            "checkpoint_id": "chase:chase-1",
        }
    ]
