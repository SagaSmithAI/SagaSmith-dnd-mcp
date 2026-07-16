import asyncio
from pathlib import Path

import pytest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def config(tmp_path: Path) -> McpConfig:
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )


def test_character_build_replays_one_template_and_instance(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(config(tmp_path))
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Build table", "edition": "2014", "idempotency_key": "campaign"},
        )
        arguments = {
            "mode": "build",
            "payload": {"campaign_id": campaign["id"], "name": "Mira"},
            "idempotency_key": "build-mira",
        }

        first = await call(server, "character_create_from", arguments)
        replay = await call(server, "character_create_from", arguments)
        roster = await call(
            server,
            "character_query",
            {"view": "list", "payload": {"campaign_id": campaign["id"]}},
        )

        assert replay["template"]["id"] == first["template"]["id"]
        assert replay["instance"]["id"] == first["instance"]["id"]
        assert [item["id"] for item in roster] == [first["instance"]["id"]]

    asyncio.run(exercise())


def test_dm_two_players_restart_and_combat_projection(tmp_path: Path) -> None:
    async def exercise() -> None:
        runtime = config(tmp_path)
        first_server = create_server(runtime)
        campaign = await call(
            first_server,
            "campaign_create",
            {"name": "Three Seat Table", "idempotency_key": "e2e-campaign"},
        )

        async def create_actor(name: str, character_type: str = "pc"):
            return await call(
                first_server,
                "character_create_from",
                {
                    "mode": "direct",
                    "payload": {
                        "name": name,
                        "campaign_id": campaign["id"],
                        "character_type": character_type,
                    },
                    "idempotency_key": f"e2e-actor-{name.lower()}",
                },
            )

        alice = await create_actor("Alice")
        bob = await create_actor("Bob")
        stalker = await create_actor("Hidden Stalker", "npc")

        for principal, actor in (("player:alice", alice), ("player:bob", bob)):
            await call(
                first_server,
                "access_grant",
                {
                    "scope": "campaign",
                    "campaign_id": campaign["id"],
                    "principal_id": principal,
                    "payload": {"role": "player"},
                    "by_principal_id": "system:local",
                },
            )
            await call(
                first_server,
                "access_grant",
                {
                    "scope": "actor",
                    "campaign_id": campaign["id"],
                    "principal_id": principal,
                    "payload": {
                        "actor_id": actor["id"],
                        "can_control": True,
                        "can_view_private": True,
                    },
                    "by_principal_id": "system:local",
                },
            )

        for actor, key, proposition in (
            (alice, "moon-mark", "The moon mark opens the east gate."),
            (bob, "bell-code", "The bell code is three short strikes."),
        ):
            await call(
                first_server,
                "actor_knowledge_change",
                {
                    "action": "add",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "actor_id": actor["id"],
                        "knowledge_key": key,
                        "proposition": proposition,
                        "disclosure_scope": "owner",
                    },
                    "idempotency_key": f"e2e-knowledge-{key}",
                },
            )

        alice_knowledge = await call(
            first_server,
            "actor_knowledge_query",
            {
                "campaign_id": campaign["id"],
                "actor_id": alice["id"],
                "view": "list",
                "principal_id": "player:alice",
            },
        )
        assert [item["knowledge_key"] for item in alice_knowledge] == ["moon-mark"]
        with pytest.raises(Exception):
            await call(
                first_server,
                "actor_knowledge_query",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": bob["id"],
                    "view": "list",
                    "principal_id": "player:alice",
                },
            )

        current_campaign = await call(
            first_server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
            },
        )
        phase = await call(
            first_server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": current_campaign["revision"],
                "idempotency_key": "e2e-play",
            },
        )
        started = await call(
            first_server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [alice["id"], bob["id"], stalker["id"]],
                "participant_config": [
                    {"actor_id": alice["id"], "initiative": 18, "position": {"x": 0, "y": 0}},
                    {"actor_id": bob["id"], "initiative": 14, "position": {"x": 1, "y": 0}},
                    {
                        "actor_id": stalker["id"],
                        "initiative": 10,
                        "position": {"x": 4, "y": 0},
                        "hidden": True,
                        "visible_to_actor_ids": [alice["id"]],
                    },
                ],
                "expected_revision": phase["campaign_revision"],
                "idempotency_key": "e2e-combat",
            },
        )
        assert started["tool_profile"] == "combat"

        alice_view = await call(
            first_server,
            "combat_query",
            {
                "campaign_id": campaign["id"],
                "view": "status",
                "principal_id": "player:alice",
            },
        )
        bob_view = await call(
            first_server,
            "combat_query",
            {
                "campaign_id": campaign["id"],
                "view": "status",
                "principal_id": "player:bob",
            },
        )
        assert stalker["id"] in {item["actor_id"] for item in alice_view["combatants"]}
        assert stalker["id"] not in {item["actor_id"] for item in bob_view["combatants"]}

        # A fresh server over the same MCP-owned home represents an Agent/MCP restart.
        restarted_server = create_server(runtime)
        persisted = await call(
            restarted_server,
            "combat_query",
            {
                "campaign_id": campaign["id"],
                "view": "status",
                "principal_id": "player:bob",
            },
        )
        assert persisted["id"] == bob_view["id"]
        bob_knowledge = await call(
            restarted_server,
            "actor_knowledge_query",
            {
                "campaign_id": campaign["id"],
                "actor_id": bob["id"],
                "view": "list",
                "principal_id": "player:bob",
            },
        )
        assert [item["knowledge_key"] for item in bob_knowledge] == ["bell-code"]

    asyncio.run(exercise())
