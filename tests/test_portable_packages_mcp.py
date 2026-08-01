from __future__ import annotations

import asyncio
from pathlib import Path

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def _config(tmp_path: Path, *, presets: bool = False) -> McpConfig:
    workspace = Path(__file__).resolve().parents[2]
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=(workspace / "SagaSmith-dnd-skills" if presets else tmp_path / "dnd"),
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=presets,
    )


def test_character_card_export_and_import_uses_fresh_identity(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Portable actors", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Portable Scout",
                    "character_type": "npc",
                    "summary": "A source campaign scout.",
                    "notes": {"profile": {"summary": "A source campaign scout."}},
                },
                "idempotency_key": "actor",
            },
        )
        exported = await _call(
            server,
            "character_query",
            {
                "view": "portable_card",
                "payload": {
                    "character_id": actor["id"],
                    "portable_id": "example.portable-scout",
                },
            },
        )
        imported = await _call(
            server,
            "character_create_from",
            {
                "mode": "portable_card",
                "payload": {"card": exported["card"]},
                "idempotency_key": "import",
            },
        )

        assert imported["character"]["id"] != actor["id"]
        assert imported["character"]["campaign_id"] is None
        assert imported["portable_card"]["id"] == "example.portable-scout"
        assert imported["actor_knowledge_imported"] is False
        assert exported["artifact"]["artifact"].endswith(".sagasmith.json")
        assert "campaign_id" not in exported["card"]["payload"]

    asyncio.run(exercise())


def test_module_package_round_trip_recreates_cast_bindings(tmp_path: Path) -> None:
    async def import_markdown(server, campaign_id: str) -> str:
        staged = await _call(
            server,
            "module_import",
            {
                "campaign_id": campaign_id,
                "action": "stage",
                "payload": {
                    "name": "keep.md",
                    "content": (
                        "# Chapter One\nArrival.\n"
                        "## Gate\nThe guard waits.\n"
                        "## Hall\nThe magistrate waits."
                    ),
                    "source_key": "example.keep",
                    "title": "The Keep",
                },
                "idempotency_key": "stage",
            },
        )
        job_id = staged["job"]["id"]
        for action in ("inspect", "validate", "ingest"):
            ingested = await _call(
                server,
                "module_import",
                {
                    "campaign_id": campaign_id,
                    "action": action,
                    "payload": {"job_id": job_id},
                    "idempotency_key": action,
                },
            )
        campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign_id}},
        )
        await _call(
            server,
            "module_import",
            {
                "campaign_id": campaign_id,
                "action": "activate",
                "payload": {"job_id": job_id},
                "expected_revision": campaign["revision"],
                "idempotency_key": "activate",
            },
        )
        return ingested["module_id"]

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        source_campaign = await _call(
            server,
            "campaign_create",
            {"name": "Package source", "edition": "2014", "idempotency_key": "source"},
        )
        module_id = await import_markdown(server, source_campaign["id"])
        scene_index = await _call(
            server,
            "module_query",
            {
                "campaign_id": source_campaign["id"],
                "view": "index",
                "payload": {"module_id": module_id},
            },
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": source_campaign["id"],
                    "name": "Gate Guard",
                    "character_type": "npc",
                    "summary": "Guards the gate.",
                    "notes": {"profile": {"summary": "Guards the gate."}},
                },
                "idempotency_key": "guard",
            },
        )
        await _call(
            server,
            "module_import",
            {
                "campaign_id": source_campaign["id"],
                "action": "bind_actor",
                "payload": {
                    "module_id": module_id,
                    "scene_id": scene_index[0]["scene_id"],
                    "character_id": actor["id"],
                    "portable_actor_id": "example.keep.guard",
                    "binding_kind": "cast",
                    "role": "gate guard",
                },
            },
        )
        exported = await _call(
            server,
            "module_query",
            {
                "campaign_id": source_campaign["id"],
                "view": "package",
                "payload": {
                    "module_id": module_id,
                    "portable_id": "example.keep",
                    "include_package": True,
                },
            },
        )
        target_campaign = await _call(
            server,
            "campaign_create",
            {"name": "Package target", "edition": "2014", "idempotency_key": "target"},
        )
        import_arguments = {
            "campaign_id": target_campaign["id"],
            "action": "import_package",
            "payload": {"package": exported["package"]},
            "idempotency_key": "package-import",
        }
        imported = await _call(
            server,
            "module_import",
            import_arguments,
        )
        replay = await _call(server, "module_import", import_arguments)
        bindings = await _call(
            server,
            "module_query",
            {
                "campaign_id": target_campaign["id"],
                "view": "actors",
                "payload": {"module_id": imported["module_id"]},
            },
        )

        assert exported["summary"]["actors"] == 1
        assert replay["module_id"] == imported["module_id"]
        assert replay["actor_map"] == imported["actor_map"]
        assert len(bindings) == 1
        assert imported["actor_map"]["example.keep.guard"] != actor["id"]
        assert bindings[0]["portable_actor_id"] == "example.keep.guard"
        assert bindings[0]["scene_key"] == scene_index[0]["stable_key"]
        assert bindings[0]["role"] == "gate guard"

    asyncio.run(exercise())


def test_bundled_srd_monster_presets_are_catalog_imports(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path, presets=True))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Preset catalog", "edition": "2014", "idempotency_key": "campaign"},
        )
        catalog = await _call(
            server,
            "rule_pack_query",
            {
                "view": "content_catalog",
                "payload": {"campaign_id": campaign["id"], "kind": "actor_card"},
            },
        )
        frog = next(item for item in catalog if item["name"] == "Frog")
        shared = await _call(
            server,
            "rule_pack_query",
            {
                "view": "actor_presets",
                "payload": {"edition": "2014", "include_package": True},
            },
        )
        imported = await _call(
            server,
            "character_create_from",
            {
                "mode": "portable_card",
                "payload": {"campaign_id": campaign["id"], "artifact_id": frog["id"]},
                "idempotency_key": "frog",
            },
        )
        imported_from_shared_pack = await _call(
            server,
            "character_create_from",
            {
                "mode": "portable_card",
                "payload": {
                    "campaign_id": campaign["id"],
                    "card": shared["portable_package"],
                    "artifact_id": frog["id"],
                    "name": "Shared Frog",
                },
                "idempotency_key": "shared-frog",
            },
        )

        assert len(catalog) == 317
        assert shared["package"]["cards"] == 317
        assert shared["artifact"]["kind"] == "preset_pack"
        assert imported["character"]["character_type"] == "monster"
        assert imported["character"]["name"] == "Frog"
        assert imported["character"]["sheet"]["inventory"]["items"] == []
        assert imported_from_shared_pack["character"]["name"] == "Shared Frog"
        assert imported_from_shared_pack["portable_card"]["id"] == frog["id"]

    asyncio.run(exercise())
