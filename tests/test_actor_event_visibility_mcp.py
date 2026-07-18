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
