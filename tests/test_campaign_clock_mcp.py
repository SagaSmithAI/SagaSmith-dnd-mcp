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


def test_campaign_clock_and_elapsed_effects_advance_atomically(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Clock", "edition": "2014", "idempotency_key": "campaign"},
        )
        sheet = default_character_sheet()
        sheet["effects"] = [
            {
                "id": "minutes",
                "name": "Minutes",
                "active": True,
                "duration": {"period": "minute", "remaining": 120},
            },
            {
                "id": "hours",
                "name": "Hours",
                "active": True,
                "duration": {"period": "hour", "remaining": 3},
            },
            {
                "id": "days",
                "name": "Days",
                "active": True,
                "duration": {"period": "day", "remaining": 2},
            },
        ]
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Timed Actor",
                    "sheet": sheet,
                },
                "idempotency_key": "actor",
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
                "payload": {"day": 2, "hour": 9, "minute": 30, "label": "Baldur's Gate"},
                "expected_revision": current["revision"],
                "idempotency_key": "clock-set",
            },
        )
        arguments = {
            "campaign_id": campaign["id"],
            "action": "clock_advance",
            "payload": {"period": "hour", "count": 2},
            "expected_revision": clock["campaign_revision"],
            "idempotency_key": "clock-advance",
        }

        advanced = await _call(server, "campaign_change", arguments)
        replay = await _call(server, "campaign_change", arguments)

        assert replay == advanced
        assert advanced["world_time"] == {
            "schema_version": 1,
            "day": 2,
            "hour": 11,
            "minute": 30,
            "elapsed_minutes": 2130,
            "label": "Baldur's Gate",
        }
        updated = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": actor["id"]}},
        )
        effects = {item["id"]: item for item in updated["sheet"]["effects"]}
        assert effects["minutes"]["active"] is False
        assert effects["hours"]["duration"]["remaining"] == 1
        assert effects["days"]["duration"]["remaining"] == 2
        persisted = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        assert persisted["state"]["world_time"] == advanced["world_time"]

    asyncio.run(exercise())


def test_campaign_clock_must_be_set_before_time_advance(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Unset Clock", "edition": "2014", "idempotency_key": "campaign"},
        )
        with pytest.raises(Exception, match="set the campaign clock"):
            await _call(
                server,
                "campaign_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "clock_advance",
                    "payload": {"period": "hour"},
                    "expected_revision": campaign["revision"],
                    "idempotency_key": "advance",
                },
            )

    asyncio.run(exercise())
