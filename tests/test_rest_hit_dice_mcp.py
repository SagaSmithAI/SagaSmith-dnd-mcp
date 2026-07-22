import asyncio
from pathlib import Path

import pytest
import sagasmith_dnd.lifecycle as lifecycle_module
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
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )


def _resting_sheet() -> dict:
    sheet = default_character_sheet()
    sheet["combat"]["hp"] = {"value": 1, "max": 12, "temp": 0}
    sheet["combat"]["hit_dice"] = {
        "fighter:d10": {
            "label": "Fighter d10",
            "value": 2,
            "max": 2,
            "recovers_on": "long_rest",
            "source_key": "Fighter",
            "slot_level": 0,
        }
    }
    return sheet


def test_short_rest_rolls_requested_hit_dice_inside_the_mcp(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Rest dice", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Resting Fighter",
                    "sheet": _resting_sheet(),
                },
                "idempotency_key": "actor",
            },
        )
        arguments = {
            "character_id": actor["id"],
            "action": "rest",
            "payload": {
                "rest_type": "short_rest",
                "hit_dice_spends": [{"key": "fighter:d10", "count": 1}],
            },
            "expected_revision": actor["revision"],
            "idempotency_key": "rest",
        }

        rested = await _call(server, "character_state_change", arguments)
        replay = await _call(server, "character_state_change", arguments)

        assert rested == replay
        assert len(rested["hit_dice_rolls"]) == 1
        assert rested["hit_dice_rolls"][0]["expression"] == "1d10"
        rolled = rested["hit_dice_rolls"][0]["total"]
        assert rested["result"]["hit_die_healing"] == rolled
        assert rested["character"]["sheet"]["combat"]["hp"]["value"] == 1 + rolled
        assert rested["character"]["sheet"]["combat"]["hit_dice"]["fighter:d10"][
            "value"
        ] == 1

    asyncio.run(exercise())


def test_rest_rejects_stale_revision_before_hit_die_rng(
    tmp_path: Path, monkeypatch
) -> None:
    def unexpected_rolls(_expression, *, rng=None):
        raise AssertionError("hit-die RNG must follow revision validation")

    monkeypatch.setattr(lifecycle_module, "roll", unexpected_rolls)

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Stale rest", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Stale Fighter",
                    "sheet": _resting_sheet(),
                },
                "idempotency_key": "actor",
            },
        )

        with pytest.raises(Exception, match="character revision conflict"):
            await _call(
                server,
                "character_state_change",
                {
                    "character_id": actor["id"],
                    "action": "rest",
                    "payload": {
                        "rest_type": "short_rest",
                        "hit_dice_spends": [{"key": "fighter:d10", "count": 1}],
                    },
                    "expected_revision": actor["revision"] + 1,
                    "idempotency_key": "rest",
                },
            )

    asyncio.run(exercise())


def test_rest_rejects_client_supplied_hit_die_results(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "No forged dice", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Honest Fighter",
                    "sheet": _resting_sheet(),
                },
                "idempotency_key": "actor",
            },
        )

        with pytest.raises(Exception, match="only key and count"):
            await _call(
                server,
                "character_state_change",
                {
                    "character_id": actor["id"],
                    "action": "rest",
                    "payload": {
                        "rest_type": "short_rest",
                        "hit_dice_spends": [{"key": "fighter:d10", "roll": 10}],
                    },
                    "expected_revision": actor["revision"],
                    "idempotency_key": "rest",
                },
            )

    asyncio.run(exercise())
