import asyncio
from pathlib import Path

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
        await call(server, "module_inspect", {"artifact": artifact["artifact"]})
        await call(
            server,
            "module_import",
            {"campaign_id": campaign["id"], "artifact": artifact["artifact"]},
        )
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
