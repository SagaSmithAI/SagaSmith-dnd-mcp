import asyncio
from pathlib import Path

import pytest
import sagasmith_dnd.ability_generation as ability_module
from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    value = result.get("result", result) if isinstance(result, dict) else result
    if isinstance(value, dict) and "action" in value and "result" in value:
        return value["result"]
    return value


def _config(tmp_path: Path) -> McpConfig:
    workspace = Path(__file__).resolve().parents[2]
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=workspace / "SagaSmith-dnd-skills",
        modulegen_skills_dir=workspace / "SagaSmith-module-gen-skills",
        auto_seed_rules=True,
    )


def test_rolled_ability_generation_is_two_phase_engine_owned_and_idempotent(
    tmp_path: Path, monkeypatch
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Ability rolls", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Unassigned Hero",
                    "sheet": default_character_sheet(),
                },
                "idempotency_key": "actor",
            },
        )
        roll_arguments = {
            "character_id": actor["id"],
            "method": "roll_4d6_drop_lowest",
            "expected_revision": actor["revision"],
            "idempotency_key": "roll-scores",
        }

        pending = await _call(server, "character_ability_apply", roll_arguments)

        def unexpected_roll(*_args, **_kwargs):
            raise AssertionError("recorded ability rolls must not be regenerated")

        monkeypatch.setattr(ability_module, "roll_ability_scores", unexpected_roll)
        replay = await _call(server, "character_ability_apply", roll_arguments)
        assert replay == pending
        assert pending["status"] == "pending_choice"
        assert len(pending["rolls"]) == 6
        assert (
            pending["character"]["sheet"]["ability_generation"]["method"]
            == "roll_4d6_drop_lowest_pending"
        )

        with pytest.raises(Exception, match="already been generated|pending"):
            await _call(
                server,
                "character_ability_apply",
                {
                    **roll_arguments,
                    "expected_revision": pending["character"]["revision"],
                    "idempotency_key": "reroll-scores",
                },
            )

        scores = sorted(item["score"] for item in pending["rolls"])
        assignments = dict(
            zip(
                ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"),
                scores,
                strict=True,
            )
        )
        completed = await _call(
            server,
            "character_ability_apply",
            {
                "character_id": actor["id"],
                "method": "roll_4d6_drop_lowest",
                "assignments": assignments,
                "expected_revision": pending["character"]["revision"],
                "idempotency_key": "assign-scores",
            },
        )
        assert completed["status"] == "committed"
        assert completed["character"]["sheet"]["ability_generation"]["rolls"] == pending["rolls"]
        assert completed["character"]["sheet"]["abilities"]["strength"]["score"] == scores[0]

    asyncio.run(exercise())


def test_ability_roll_rejects_stale_revision_and_caller_roll_payload(
    tmp_path: Path, monkeypatch
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Ability safety", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Safe Hero",
                    "sheet": default_character_sheet(),
                },
                "idempotency_key": "actor",
            },
        )
        manual_actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Manual Hero",
                    "sheet": default_character_sheet(),
                },
                "idempotency_key": "manual-actor",
            },
        )
        manual_scores = {
            "strength": 18,
            "dexterity": 14,
            "constitution": 16,
            "intelligence": 10,
            "wisdom": 12,
            "charisma": 8,
        }
        manual = await _call(
            server,
            "character_ability_apply",
            {
                "character_id": manual_actor["id"],
                "method": "manual",
                "assignments": manual_scores,
                "expected_revision": manual_actor["revision"],
                "idempotency_key": "manual-scores",
            },
        )
        assert manual["status"] == "committed"
        assert manual["character"]["sheet"]["ability_generation"]["method"] == "manual"
        assert manual["character"]["sheet"]["ability_generation"]["rolls"] == []

        def unexpected_roll(*_args, **_kwargs):
            raise AssertionError("ability RNG must follow revision validation")

        monkeypatch.setattr(ability_module, "roll_ability_scores", unexpected_roll)
        with pytest.raises(Exception, match="character revision conflict"):
            await _call(
                server,
                "character_ability_apply",
                {
                    "character_id": actor["id"],
                    "method": "roll_4d6_drop_lowest",
                    "expected_revision": actor["revision"] + 1,
                    "idempotency_key": "stale-roll",
                },
            )
        with pytest.raises(Exception, match="rolls|unexpected"):
            await _call(
                server,
                "character_ability_apply",
                {
                    "character_id": actor["id"],
                    "method": "roll_4d6_drop_lowest",
                    "rolls": [18, 18, 18, 18, 18, 18],
                    "expected_revision": actor["revision"],
                    "idempotency_key": "forged-roll",
                },
            )

    asyncio.run(exercise())
