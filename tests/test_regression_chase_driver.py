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
            "--checkpoint-label",
            "Street chase resolved",
            "--defer-checkpoint",
        ],
    )

    assert regression_chase._arguments().defer_checkpoint is True


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
