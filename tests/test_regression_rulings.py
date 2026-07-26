from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import scripts.regression_party as regression_party
import scripts.regression_playthrough as regression_playthrough
from scripts.regression_rulings import (
    RegressionRulingRequiredError,
    raise_for_pending_ruling,
    ruling_failure_fields,
)


def test_untyped_pending_ruling_defaults_to_agent_and_keeps_context() -> None:
    try:
        raise_for_pending_ruling(
            {
                "status": "pending_ruling",
                "reason": "module procedure needs a scene fact",
                "committed": False,
            },
            operation="module_procedure",
            context={"scene_id": "scene-1"},
        )
    except RegressionRulingRequiredError as error:
        fields = ruling_failure_fields(error)
    else:
        raise AssertionError("pending ruling did not return to the Agent")

    assert fields["status"] == "pending_ruling"
    assert fields["default_resolver"] == "agent"
    requirement = fields["ruling_requirements"][0]
    assert requirement["operation"] == "module_procedure"
    assert requirement["context"] == {"scene_id": "scene-1"}
    assert requirement["ruling"]["default_resolver"] == "agent"
    assert requirement["ruling"]["ruling_kind"] == "agent_dm_adjudication"


def test_external_source_review_is_not_relabelled_as_agent_ruling() -> None:
    try:
        raise_for_pending_ruling(
            {
                "status": "pending_ruling",
                "default_resolver": "external_input",
                "ruling_kind": "missing_or_conflicting_source_review",
                "reason": "source card is incomplete",
            },
            operation="character_content_apply",
        )
    except RegressionRulingRequiredError as error:
        fields = ruling_failure_fields(error)
    else:
        raise AssertionError("external ruling did not stop the driver")

    assert fields["default_resolver"] == "external_input"
    assert fields["ruling_requirements"][0]["ruling"]["ruling_kind"] == (
        "missing_or_conflicting_source_review"
    )


def test_external_kind_alone_recovers_its_external_resolver() -> None:
    try:
        raise_for_pending_ruling(
            {
                "status": "pending_ruling",
                "ruling_kind": "player_owned_choice",
                "reason": "the player must choose a target",
            },
            operation="spell_target",
        )
    except RegressionRulingRequiredError as error:
        fields = ruling_failure_fields(error)
    else:
        raise AssertionError("player choice did not stop the driver")

    ruling = fields["ruling_requirements"][0]["ruling"]
    assert ruling["default_resolver"] == "external_input"
    assert fields["default_resolver"] == "external_input"


def test_party_driver_writes_structured_agent_handoff(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "party.json"
    args = SimpleNamespace(
        output=output,
        campaign_id="campaign-1",
        party="tyranny-of-dragons",
        run_id="run-1",
        repair_existing_party_report=None,
    )

    async def blocked(_args) -> dict:
        raise RegressionRulingRequiredError(
            {
                "status": "pending_ruling",
                "default_resolver": "agent",
                "ruling_kind": "source_or_scene_fact",
                "reason": "adjudicate a feat prerequisite",
            },
            operation="character_content_apply.party",
        )

    monkeypatch.setattr(regression_party, "_arguments", lambda: args)
    monkeypatch.setattr(regression_party, "_run", blocked)

    assert regression_party.main() == 1
    report = regression_party.json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "pending_ruling"
    assert report["default_resolver"] == "agent"
    assert report["ruling_requirements"][0]["operation"] == (
        "character_content_apply.party"
    )


def test_playthrough_driver_writes_structured_external_handoff(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "playthrough.json"
    args = SimpleNamespace(
        output=output,
        action="advance-level",
        campaign_id="campaign-1",
        run_id="run-1",
    )

    async def blocked(_args) -> dict:
        raise RegressionRulingRequiredError(
            {
                "status": "pending_ruling",
                "default_resolver": "external_input",
                "ruling_kind": "missing_or_conflicting_source_review",
                "reason": "catalog source is incomplete",
            },
            operation="character_content_apply.level_feature",
        )

    monkeypatch.setattr(regression_playthrough, "_arguments", lambda: args)
    monkeypatch.setattr(regression_playthrough, "_run", blocked)

    assert regression_playthrough.main() == 1
    report = regression_playthrough.json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "pending_ruling"
    assert report["default_resolver"] == "external_input"
    assert report["ruling_requirements"][0]["ruling"]["ruling_kind"] == (
        "missing_or_conflicting_source_review"
    )
