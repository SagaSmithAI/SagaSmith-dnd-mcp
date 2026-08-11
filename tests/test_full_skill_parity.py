import asyncio
from pathlib import Path

import pytest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.parity import required_tool_names
from sagasmith_dnd_mcp.server import create_server


async def _import_module(call, server, campaign_id: str, source_path: Path, key: str):
    staged = await call(
        server,
        "module_draft",
        {
            "campaign_id": campaign_id,
            "action": "start",
            "payload": {
                "source_path": str(source_path),
                "source_key": key,
                "title": source_path.stem,
            },
            "idempotency_key": f"{key}:start",
        },
    )
    job_id = staged["job"]["id"]
    chunks = await call(
        server,
        "module_draft",
        {
            "campaign_id": campaign_id,
            "action": "evidence",
            "payload": {"job_id": job_id, "kind": "chunks", "limit": 1},
        },
    )
    assert chunks
    source_ref = {
        "source_key": key,
        "page": None,
        "chunk_hash": chunks[0]["content_hash"],
        "note": "Reviewed test fixture source.",
    }
    finalized = await call(
        server,
        "module_draft",
        {
            "campaign_id": campaign_id,
            "action": "finalize",
            "payload": {
                "job_id": job_id,
                "pack_id": f"dnd5e.module.{key}",
                "version": "1.0.0",
                "manifest": {
                    "title": source_path.stem,
                    "classification": "adventure",
                    "compatibility": {
                        "editions": ["2024"],
                        "required_capabilities": ["module_pack_v2"],
                    },
                    "play_profile": {
                        "party_size": {
                            "minimum": 3,
                            "maximum": 5,
                            "source_refs": [source_ref],
                        },
                        "starting_level": {"value": 1, "source_refs": [source_ref]},
                        "expected_end_level": {"value": 1, "source_refs": [source_ref]},
                        "advancement": {
                            "modes": ["milestone"],
                            "recommended": "milestone",
                            "source_refs": [source_ref],
                        },
                        "pregenerated_characters": {
                            "available": False,
                            "applicability": "Reviewed; none are included.",
                            "source_refs": [source_ref],
                        },
                    },
                    "continuity": {
                        "series_id": None,
                        "order": None,
                        "continues_from": None,
                        "state_policy": {},
                    },
                    "activation": {"mode": "campaign_attach", "default_active": False},
                    "content_summary": {},
                },
                "confirmation": {
                    "confirmed": True,
                    "note": (
                        "Agent reviewed the module package and confirmed it is ready to finalize."
                    ),
                },
            },
            "idempotency_key": f"{key}:finalize",
        },
    )
    imported = await call(
        server,
        "content_pack",
        {
            "action": "import",
            "payload": {
                "campaign_id": campaign_id,
                "kind": "module",
                "artifact": finalized["artifact"],
            },
            "idempotency_key": f"{key}:import",
        },
    )
    campaign = await call(
        server,
        "campaign_query",
        {
            "view": "get",
            "payload": {"campaign_id": campaign_id},
            "principal_id": "system:local",
        },
    )
    return await call(
        server,
        "content_pack",
        {
            "action": "activate",
            "payload": {
                "campaign_id": campaign_id,
                "kind": "module",
                "module_id": imported["module_id"],
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": f"{key}:activate",
        },
    )


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
    source = tmp_path / "private.md"
    source.write_text(
        "# Secret\n## Hidden Vault\n#### A1. Reliquary\nThe crown is cursed.",
        encoding="utf-8",
    )
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        module_import_roots=(tmp_path,),
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
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Alice"},
                "principal_id": "system:local",
                "idempotency_key": "private-alice",
            },
        )
        for principal, actor_id in (("player:alice", alice["id"]), ("player:bob", None)):
            await call(
                server,
                "access_grant",
                {
                    "scope": "campaign",
                    "campaign_id": campaign["id"],
                    "principal_id": principal,
                    "payload": {"role": "player"},
                },
            )
            if actor_id:
                await call(
                    server,
                    "access_grant",
                    {
                        "scope": "actor",
                        "campaign_id": campaign["id"],
                        "principal_id": principal,
                        "payload": {"actor_id": actor_id, "can_view_private": True},
                    },
                )
        await _import_module(call, server, campaign["id"], source, "private-module")
        scene = (
            await call(
                server,
                "module_query",
                {
                    "campaign_id": campaign["id"],
                    "view": "index",
                    "payload": {},
                    "principal_id": "system:local",
                },
            )
        )[0]
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
                "module_query",
                {
                    "campaign_id": campaign["id"],
                    "view": "current",
                    "payload": {"scope_id": f"player:{alice['id']}"},
                    "principal_id": "player:bob",
                },
            )
        with pytest.raises(Exception, match="owned player scene scope"):
            await call(
                server,
                "continuity_context",
                {
                    "campaign_id": campaign["id"],
                    "scope_id": f"player:{alice['id']}",
                    "audience": "player",
                    "principal_id": "player:bob",
                },
            )
        player_context = await call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "scope_id": f"player:{alice['id']}",
                "audience": "player",
                "principal_id": "player:alice",
            },
        )
        assert player_context["scoped_scene"]["redacted"] is True
        assert "cursed crown" not in str(player_context["scoped_scene"])
        assert "The crown is cursed" not in str(player_context["scoped_scene"])
        redacted = await call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "scene",
                "payload": {"scene_id": scene["scene_id"]},
                "principal_id": "player:bob",
            },
        )
        assert set(redacted) == {"campaign_id", "scene_id", "redacted", "content"}

    asyncio.run(exercise())


def test_mcp_first_full_workflow(tmp_path: Path) -> None:
    source = tmp_path / "parity.md"
    source.write_text("# Parity\n## Gate\nThe sealed gate.", encoding="utf-8")
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        module_import_roots=(tmp_path,),
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
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"name": "Aria", "campaign_id": campaign["id"]},
                "principal_id": "system:local",
                "idempotency_key": "create-aria",
            },
        )
        await call(
            server,
            "actor_knowledge_change",
            {
                "action": "add",
                "payload": {
                    "campaign_id": campaign["id"],
                    "actor_id": actor["id"],
                    "knowledge_key": "gate",
                    "proposition": "The gate is sealed.",
                },
                "principal_id": "system:local",
                "idempotency_key": "knowledge-gate",
            },
        )
        assert await call(
            server,
            "actor_knowledge_query",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "view": "search",
                "payload": {"query": "gate"},
                "principal_id": "system:local",
            },
        )
        imported = await _import_module(call, server, campaign["id"], source, "parity-module")
        assert imported["activation"]["active"] is True
        scenes = await call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "index",
                "payload": {},
                "principal_id": "system:local",
            },
        )
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
        assert (
            await call(
                server,
                "module_query",
                {
                    "campaign_id": campaign["id"],
                    "view": "current",
                    "payload": {},
                    "principal_id": "system:local",
                },
            )
        )["progress"]["percent"] == 25
        campaign = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        wallet = await call(
            server,
            "wallet_change",
            {
                "owner": "party",
                "action": "adjust",
                "owner_id": campaign["id"],
                "denomination": "gp",
                "amount": 10,
                "payload": {},
                "principal_id": "system:local",
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
            "snapshot_query",
            {
                "campaign_id": campaign["id"],
                "view": "verify",
                "payload": {"slot": snapshot["slot"]},
                "principal_id": "system:local",
            },
        )
        assert verified["valid"]
        assert await call(
            server,
            "state_revision",
            {
                "campaign_id": campaign["id"],
                "action": "history",
                "payload": {},
                "principal_id": "system:local",
            },
        )

    asyncio.run(exercise_workflow())
