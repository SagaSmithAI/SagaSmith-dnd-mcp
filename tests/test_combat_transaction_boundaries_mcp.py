from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


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


def test_available_actions_explicitly_discovers_required_death_save(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Death action", "edition": "2014", "idempotency_key": "campaign"},
        )
        sheet = default_character_sheet()
        sheet["combat"]["hp"] = {"value": 0, "max": 10, "temp": 0}
        sheet["conditions"] = ["prone", "unconscious"]
        actor = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Dying PC",
                "sheet": sheet,
                "idempotency_key": "actor",
            },
        )
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
        started = await _call(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [actor["id"]],
                "participant_config": [
                    {"actor_id": actor["id"], "initiative": 10, "death_saves": True}
                ],
                "expected_revision": campaign["revision"],
                "idempotency_key": "start",
            },
        )

        available = await _call(
            server,
            "combat_available_actions",
            {"campaign_id": campaign["id"], "actor_id": actor["id"]},
        )

        assert started["combat"]["round"] == 1
        assert available["actions"] == ["death_save"]

    asyncio.run(exercise())


def test_invalid_branch_is_rejected_before_noncombat_check_rolls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rolled = False

    def forbidden_roll(*args, **kwargs):
        nonlocal rolled
        rolled = True
        raise AssertionError("the check must not roll")

    monkeypatch.setattr(server_module, "resolve_actor_check", forbidden_roll)

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Branch guard", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Checker",
                "idempotency_key": "actor",
            },
        )
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})

        with pytest.raises(Exception, match="checked-out branch"):
            await _call(
                server,
                "character_check",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": actor["id"],
                    "kind": "check",
                    "ability": "wisdom",
                    "dc": 10,
                    "branch_id": "not-the-current-branch",
                    "expected_revision": campaign["revision"],
                    "idempotency_key": "invalid-branch-check",
                },
            )

    asyncio.run(exercise())
    assert rolled is False
