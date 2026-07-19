import asyncio
from pathlib import Path

import pytest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def test_actor_scoped_event_is_visible_only_to_witnesses(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Separate witnesses", "idempotency_key": "campaign"},
        )
        witness = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Witness",
                "idempotency_key": "witness",
            },
        )
        unaware = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Unaware",
                "idempotency_key": "unaware",
            },
        )
        event = await _call(
            server,
            "event_add",
            {
                "campaign_id": campaign["id"],
                "summary": "The witness sees the masked visitor leave.",
                "event_type": "revelation",
                "audience_scope": "actor",
                "known_by_actor_ids": [witness["id"]],
                "knowledge_key": "masked-visitor-departed",
                "knowledge_proposition": "The masked visitor left by the east door.",
                "idempotency_key": "event",
            },
        )
        assert event["audience_scope"] == "actor"
        assert len(event["actor_knowledge_ids"]) == 1

        seen = await _call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "actor_id": witness["id"],
                "audience": "player",
                "query": "masked visitor",
            },
        )
        hidden = await _call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "actor_id": unaware["id"],
                "audience": "player",
                "query": "masked visitor",
            },
        )
        assert [item["id"] for item in seen["events"]] == [event["id"]]
        assert [item["knowledge_key"] for item in seen["actor_knowledge"]] == [
            "masked-visitor-departed"
        ]
        assert seen["actor_knowledge"][0]["disclosure_scope"] == "owner"
        assert hidden["events"] == []
        assert hidden["actor_knowledge"] == []

        with pytest.raises(Exception, match="require known_by_actor_ids"):
            await _call(
                server,
                "event_add",
                {
                    "campaign_id": campaign["id"],
                    "summary": "Missing witnesses",
                    "audience_scope": "actor",
                    "idempotency_key": "invalid-event",
                },
            )

    asyncio.run(exercise())


def test_campaign_reads_do_not_bypass_player_visibility_boundaries(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Redacted campaign", "idempotency_key": "campaign"},
        )
        updated = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "update",
                "payload": {"state": {"dm_secret": "the eastern statue is trapped"}},
                "expected_revision": campaign["revision"],
                "idempotency_key": "secret",
            },
        )
        clock = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "clock_set",
                "payload": {"day": 1, "hour": 9},
                "expected_revision": updated["revision"],
                "idempotency_key": "clock",
            },
        )
        party_effect = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "effect_add",
                "payload": {
                    "effect": {
                        "id": "visible-light",
                        "name": "Visible light",
                        "visibility": "party",
                        "target": {"kind": "object", "id": "mace"},
                        "duration": {"period": "hour", "remaining": 1},
                    }
                },
                "expected_revision": clock["campaign_revision"],
                "idempotency_key": "party-effect",
            },
        )
        hidden_effect = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "effect_add",
                "payload": {
                    "effect": {
                        "id": "hidden-trap",
                        "name": "Hidden trap aura",
                        "visibility": "dm",
                        "target": {"kind": "location", "id": "east-statue"},
                        "duration": {"period": "manual", "remaining": 0},
                    }
                },
                "expected_revision": party_effect["campaign_revision"],
                "idempotency_key": "dm-effect",
            },
        )
        assert hidden_effect["effect"]["visibility"] == "dm"
        await _call(
            server,
            "access_grant",
            {
                "scope": "campaign",
                "campaign_id": campaign["id"],
                "principal_id": "player:alice",
                "payload": {"role": "player"},
            },
        )

        player = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "player:alice",
            },
        )
        listed = await _call(
            server,
            "campaign_query",
            {"view": "list", "principal_id": "player:alice"},
        )
        owner = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )

        assert player["state_redacted"] is True
        assert "dm_secret" not in player["state"]
        assert {effect["id"] for effect in player["state"]["world_effects"]} == {
            "visible-light"
        }
        assert listed[0]["state"] == player["state"]
        assert owner["state"]["dm_secret"] == "the eastern statue is trapped"
        assert {effect["id"] for effect in owner["state"]["world_effects"]} == {
            "visible-light",
            "hidden-trap",
        }

    asyncio.run(exercise())
