from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

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


def test_compact_facades_enforce_authoritative_phases_and_roles(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Strict facade contract",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Facade actor"},
                "principal_id": "system:local",
                "idempotency_key": "actor",
            },
        )

        with pytest.raises(Exception, match="only available during play"):
            await _call(
                server,
                "character_check",
                {
                    "campaign_id": campaign["id"],
                    "action": "check",
                    "payload": {
                        "actor_id": actor["id"],
                        "kind": "ability",
                        "ability": "wisdom",
                    },
                    "expected_revision": campaign["revision"],
                    "idempotency_key": "lobby-check",
                },
            )

        current = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": current["revision"],
                "idempotency_key": "enter-play",
            },
        )
        before = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        await _call(
            server,
            "access_grant",
            {
                "scope": "campaign",
                "campaign_id": campaign["id"],
                "principal_id": "player:facade",
                "payload": {"role": "player"},
            },
        )

        with pytest.raises(Exception, match="only available during lobby"):
            await _call(
                server,
                "module_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "edit", "payload": {"operation": "content",
                        "module_id": "module",
                        "scene_id": "scene",
                        "content_key": "key",
                        "normalized_content": "content",
                        "observation": "review",
                    },
                    "idempotency_key": "play-content-review",
                },
            )
        with pytest.raises(Exception, match="only available during lobby"):
            await _call(
                server,
                "rulebook_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "evidence",
                    "payload": {"job_id": "job", "kind": "page", "page_number": 1},
                },
            )
        with pytest.raises(Exception, match="only available during combat"):
            await _call(
                server,
                "combat_choice",
                {
                    "campaign_id": campaign["id"],
                    "action": "on_hit_ruling",
                    "actor_id": actor["id"],
                    "payload": {
                        "choice_id": "choice",
                        "selection": {"id": "dismiss"},
                    },
                    "expected_revision": before["revision"],
                    "idempotency_key": "play-on-hit",
                },
            )
        with pytest.raises(Exception, match="cannot access"):
            await _call(
                server,
                "combat_choice",
                {
                    "campaign_id": campaign["id"],
                    "action": "on_hit_ruling",
                    "actor_id": actor["id"],
                    "payload": {
                        "choice_id": "choice",
                        "selection": {"id": "dismiss"},
                    },
                    "principal_id": "player:facade",
                    "expected_revision": before["revision"],
                    "idempotency_key": "player-on-hit",
                },
            )

        after = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        assert after["revision"] == before["revision"]

    asyncio.run(exercise())
