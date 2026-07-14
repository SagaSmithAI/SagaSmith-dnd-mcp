from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


def test_party_wallet_transfer_is_one_undoable_and_idempotent(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(server, "campaign_create", {"name": "Integrity"})
        actor = await call(
            server,
            "character_create",
            {"name": "Mira", "campaign_id": campaign["id"]},
        )
        await call(
            server,
            "party_wallet_adjust",
            {"campaign_id": campaign["id"], "denomination": "gp", "amount": 10},
        )
        args = {
            "campaign_id": campaign["id"],
            "character_id": actor["id"],
            "denomination": "gp",
            "amount": 1,
            "direction": "withdraw",
            "idempotency_key": "wallet-1",
        }
        first = await call(server, "party_wallet_transfer", args)
        replay = await call(server, "party_wallet_transfer", args)
        assert replay == first
        await call(server, "state_undo", {"campaign_id": campaign["id"]})
        party = await call(server, "party_show", {"campaign_id": campaign["id"]})
        restored = await call(server, "character_get", {"character_id": actor["id"]})
        assert party["inventory"]["wallet"]["gp"] == 10
        assert restored["sheet"]["inventory"]["wallet"]["gp"] == 0

    asyncio.run(exercise())


def test_player_cannot_read_unassigned_actor_knowledge(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(server, "campaign_create", {"name": "Private"})
        actor = await call(
            server,
            "character_create",
            {"name": "Secret NPC", "campaign_id": campaign["id"], "character_type": "npc"},
        )
        await call(
            server,
            "campaign_member_grant",
            {
                "campaign_id": campaign["id"],
                "principal_id": "player:alice",
                "role": "player",
            },
        )
        await call(
            server,
            "actor_knowledge_add",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "knowledge_key": "secret",
                "proposition": "The crown is fake.",
            },
        )
        with pytest.raises(Exception):
            await call(
                server,
                "actor_knowledge_list",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": actor["id"],
                    "principal_id": "player:alice",
                },
            )

    asyncio.run(exercise())

