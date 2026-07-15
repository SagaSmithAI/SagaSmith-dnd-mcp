import asyncio
from pathlib import Path

import pytest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.parity import required_tool_names
from sagasmith_dnd_mcp.server import create_server


def test_server_covers_full_skill_tool_contract(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
    )

    async def inspect_tools() -> set[str]:
        server = create_server(config)
        return {tool.name for tool in await server.list_tools()}

    assert required_tool_names() <= asyncio.run(inspect_tools())


def test_module_scene_reads_do_not_cross_player_scope_or_leak_keeper_structure(
    tmp_path: Path,
) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Private scenes", "idempotency_key": "private-scenes"},
        )
        alice = await call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Alice",
                "idempotency_key": "private-alice",
            },
        )
        for principal, actor_id in (("player:alice", alice["id"]), ("player:bob", None)):
            await call(
                server,
                "campaign_member_grant",
                {"campaign_id": campaign["id"], "principal_id": principal, "role": "player"},
            )
            if actor_id:
                await call(
                    server,
                    "actor_grant",
                    {
                        "campaign_id": campaign["id"],
                        "principal_id": principal,
                        "actor_id": actor_id,
                        "can_view_private": True,
                    },
                )
        artifact = await call(
            server,
            "module_write",
            {
                "name": "private.md",
                "content": "# Secret\n## Hidden Vault\n#### A1. Reliquary\nThe crown is cursed.",
            },
        )
        await call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "artifact": artifact["artifact"],
                "idempotency_key": "private-module-import",
            },
        )
        scene = (await call(server, "module_index", {"campaign_id": campaign["id"]}))[0]
        await call(
            server,
            "module_set_progress",
            {
                "campaign_id": campaign["id"],
                "scene_id": scene["scene_id"],
                "scope_id": f"player:{alice['id']}",
                "state": {"secret": "cursed crown"},
                "expected_state_version": 0,
                "idempotency_key": "private-progress",
            },
        )
        with pytest.raises(Exception, match="owned player scene scope"):
            await call(
                server,
                "module_current",
                {
                    "campaign_id": campaign["id"],
                    "scope_id": f"player:{alice['id']}",
                    "principal_id": "player:bob",
                },
            )
        redacted = await call(
            server,
            "module_read_scene",
            {
                "campaign_id": campaign["id"],
                "scene_id": scene["scene_id"],
                "principal_id": "player:bob",
            },
        )
        assert set(redacted) == {"campaign_id", "scene_id", "redacted", "content"}

    asyncio.run(exercise())


def test_mcp_first_full_workflow(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise_workflow() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Parity", "idempotency_key": "create-parity"},
        )
        actor = await call(
            server,
            "character_create",
            {
                "name": "Aria",
                "campaign_id": campaign["id"],
                "idempotency_key": "create-aria",
            },
        )
        await call(
            server,
            "actor_knowledge_add",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "knowledge_key": "gate",
                "proposition": "The gate is sealed.",
                "idempotency_key": "knowledge-gate",
            },
        )
        assert await call(
            server,
            "actor_knowledge_search",
            {"campaign_id": campaign["id"], "actor_id": actor["id"], "query": "gate"},
        )
        artifact = await call(
            server,
            "module_write",
            {"name": "parity.md", "content": "# Parity\n## Gate\nThe sealed gate."},
        )
        inspection = await call(server, "module_inspect", {"artifact": artifact["artifact"]})
        assert inspection["parser_profile"] == "dnd5e"
        imported = await call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "artifact": artifact["artifact"],
                "idempotency_key": "parity-module-import",
            },
        )
        replayed = await call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "artifact": artifact["artifact"],
                "idempotency_key": "parity-module-import",
            },
        )
        assert replayed == imported
        scenes = await call(server, "module_index", {"campaign_id": campaign["id"]})
        await call(
            server,
            "module_set_progress",
            {
                "campaign_id": campaign["id"],
                "scene_id": scenes[0]["scene_id"],
                "progress": 25,
                "expected_state_version": 0,
                "idempotency_key": "parity-scene-progress",
            },
        )
        assert (await call(server, "module_current", {"campaign_id": campaign["id"]}))["progress"][
            "percent"
        ] == 25
        campaign = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
        wallet = await call(
            server,
            "party_wallet_adjust",
            {
                "campaign_id": campaign["id"],
                "denomination": "gp",
                "amount": 10,
                "expected_revision": campaign["revision"],
                "idempotency_key": "parity-wallet",
            },
        )
        snapshot = await call(
            server,
            "snapshot_create",
            {
                "campaign_id": campaign["id"],
                "label": "parity",
                "expected_revision": wallet["campaign"]["revision"],
                "expected_head_snapshot_id": "",
                "idempotency_key": "parity-snapshot",
            },
        )
        verified = await call(
            server,
            "snapshot_verify",
            {"campaign_id": campaign["id"], "slot": snapshot["slot"]},
        )
        assert verified["valid"]
        assert await call(server, "state_history", {"campaign_id": campaign["id"]})

    asyncio.run(exercise_workflow())
