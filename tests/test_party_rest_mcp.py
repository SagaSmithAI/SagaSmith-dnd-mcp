import asyncio
from pathlib import Path

import pytest
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


def _spent_sheet() -> dict:
    sheet = default_character_sheet()
    sheet["combat"]["hp"] = {"value": 1, "max": 12, "temp": 3}
    sheet["combat"]["hit_dice"] = {
        "fighter:d10": {
            "label": "Fighter d10",
            "value": 0,
            "max": 2,
            "recovers_on": "long_rest",
            "source_key": "Fighter",
            "slot_level": 0,
        }
    }
    sheet["effects"] = [
        {
            "id": "hours",
            "name": "Expires while resting",
            "active": True,
            "duration": {"period": "hour", "remaining": 5},
        }
    ]
    return sheet


def test_party_long_rest_advances_once_and_settles_members_atomically(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Party rest", "edition": "2014", "idempotency_key": "campaign"},
        )
        first = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "First",
                    "sheet": _spent_sheet(),
                },
                "idempotency_key": "first",
            },
        )
        second = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Second",
                    "sheet": _spent_sheet(),
                },
                "idempotency_key": "second",
            },
        )
        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        clock = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "clock_set",
                "payload": {"day": 1, "hour": 21, "minute": 0, "label": "Baldur's Gate"},
                "expected_revision": current["revision"],
                "idempotency_key": "clock",
            },
        )
        with pytest.raises(Exception, match="party_rest"):
            await _call(
                server,
                "character_rest",
                {
                    "character_id": first["id"],
                    "rest_type": "long_rest",
                    "expected_revision": first["revision"],
                    "idempotency_key": "unsafe-individual-long-rest",
                },
            )
        arguments = {
            "campaign_id": campaign["id"],
            "action": "party_rest",
            "payload": {
                "members": [
                    {"character_id": first["id"], "expected_revision": first["revision"]},
                    {"character_id": second["id"], "expected_revision": second["revision"]},
                ]
            },
            "expected_revision": clock["campaign_revision"],
            "idempotency_key": "long-rest",
        }

        rested = await _call(server, "campaign_change", arguments)
        assert await _call(server, "campaign_change", arguments) == rested
        assert rested["world_time"] == {
            "schema_version": 1,
            "day": 2,
            "hour": 5,
            "minute": 0,
            "elapsed_minutes": 1740,
            "label": "Baldur's Gate",
        }
        assert set(rested["member_ids"]) == {first["id"], second["id"]}
        assert rested["expired"] == {first["id"]: ["hours"], second["id"]: ["hours"]}
        receipt = await _call(
            server,
            "state_revision",
            {
                "campaign_id": campaign["id"],
                "action": "receipt",
                "payload": {"idempotency_key": "long-rest"},
            },
        )
        assert receipt["key"] == "long-rest"
        assert receipt["replayed"] is True
        assert receipt["response"] == rested

        updated = []
        for actor in (first, second):
            current_actor = await _call(
                server,
                "character_query",
                {"view": "get", "payload": {"character_id": actor["id"]}},
            )
            updated.append(current_actor)
            assert current_actor["sheet"]["combat"]["hp"] == {
                "value": 12,
                "max": 12,
                "temp": 0,
            }
            assert current_actor["sheet"]["combat"]["hit_dice"]["fighter:d10"]["value"] == 1
            assert current_actor["sheet"]["combat"]["rest_history"] == {
                "last_rest_type": "long_rest",
                "last_rest_started_elapsed_minutes": 1260,
                "last_rest_completed_elapsed_minutes": 1740,
                "last_long_rest_elapsed_minutes": 1740,
            }
            assert current_actor["sheet"]["effects"][0]["active"] is False

        with pytest.raises(Exception, match="in 24 hours"):
            await _call(
                server,
                "campaign_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "party_rest",
                    "payload": {
                        "members": [
                            {
                                "character_id": updated[0]["id"],
                                "expected_revision": updated[0]["revision"],
                            }
                        ]
                    },
                    "expected_revision": rested["campaign_revision"],
                    "idempotency_key": "too-soon",
                },
            )
        unchanged = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        assert unchanged["state"]["world_time"]["elapsed_minutes"] == 1740
        assert unchanged["revision"] == rested["campaign_revision"]

    asyncio.run(exercise())
