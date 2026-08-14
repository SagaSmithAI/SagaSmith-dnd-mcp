from __future__ import annotations

import asyncio
import hashlib
from copy import deepcopy
from io import BytesIO
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from PIL import Image
from sagasmith_core.content_pack import (
    blob_descriptor,
    build_content_package,
    dumps_content_archive,
    loads_content_archive,
)
from sagasmith_core.indexed_source import rule_chunk_key
from sagasmith_core.modules import ModuleService
from sagasmith_dnd.character_schema import default_character_notes, default_character_sheet
from sagasmith_dnd.content_actors import build_dnd_content_actor
from sagasmith_dnd.content_packages import (
    build_preset_content_package,
    build_rule_content_package,
)

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import (
    _artifact_statblock_source_chunks,
    _cached_rapidocr_provider,
    _index_statblock_source_chunks,
    create_server,
)
from tests.authoring_helpers import finalize_and_activate_module


def test_ocr_provider_is_reused_across_pages_of_the_same_profile(tmp_path: Path) -> None:
    providers = {}

    first = _cached_rapidocr_provider(
        providers,
        model_type="small",
        scale=2.0,
        cache_dir=tmp_path,
    )
    second = _cached_rapidocr_provider(
        providers,
        model_type="small",
        scale=2.0004,
        cache_dir=tmp_path,
    )
    different = _cached_rapidocr_provider(
        providers,
        model_type="medium",
        scale=2.0,
        cache_dir=tmp_path,
    )

    assert first is second
    assert different is not first
    assert len(providers) == 2


def test_statblock_preset_evidence_is_indexed_once_and_bounded_per_actor() -> None:
    chunks = [
        {"id": "other", "heading_path": ["Other"], "content": "irrelevant"},
        {"id": "wolf-core", "heading_path": ["Wolf"], "content": "core"},
        {"id": "wolf-actions", "heading_path": ["Wolf", "Actions"], "content": "bite"},
    ]
    by_id, by_heading = _index_statblock_source_chunks(chunks)

    cited = _artifact_statblock_source_chunks(
        {
            "card": {"name": "Wolf"},
            "source_citations": [
                {"chunk_id": "wolf-actions"},
                {"chunk_id": "wolf-core"},
                {"chunk_id": "wolf-actions"},
            ],
        },
        chunks_by_id=by_id,
        chunks_by_heading=by_heading,
    )
    heading_fallback = _artifact_statblock_source_chunks(
        {"card": {"name": "Wolf"}, "source_citations": []},
        chunks_by_id=by_id,
        chunks_by_heading=by_heading,
    )

    assert [chunk["id"] for chunk in cited] == ["wolf-actions", "wolf-core"]
    assert [chunk["id"] for chunk in heading_fallback] == ["wolf-core", "wolf-actions"]
    assert all(chunk["id"] != "other" for chunk in (*cited, *heading_fallback))


def test_immutable_review_page_render_is_reused_until_the_file_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "book.pdf"
    path.write_bytes(b"first")
    calls: list[tuple[Path, int, float]] = []

    class Rendered:
        source_checksum = "checksum"

    def fake_render(source: Path, page_number: int, *, scale: float) -> Rendered:
        calls.append((source, page_number, scale))
        return Rendered()

    monkeypatch.setattr(server_module, "render_pdf_page", fake_render)
    server_module._render_immutable_pdf_page_cached.cache_clear()

    first = server_module._render_immutable_pdf_page(
        path,
        3,
        scale=1.5,
        source_checksum="checksum",
    )
    replay = server_module._render_immutable_pdf_page(
        path,
        3,
        scale=1.5,
        source_checksum="checksum",
    )
    path.write_bytes(b"second version")
    changed = server_module._render_immutable_pdf_page(
        path,
        3,
        scale=1.5,
        source_checksum="checksum",
    )

    assert first is replay
    assert changed is not first
    assert len(calls) == 2


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def _config(
    tmp_path: Path,
    *,
    presets: bool = False,
    rule_import_roots: tuple[Path, ...] = (),
) -> McpConfig:
    workspace = Path(__file__).resolve().parents[2]
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=(workspace / "SagaSmith-dnd-skills" if presets else tmp_path / "dnd"),
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=presets,
        rule_import_roots=rule_import_roots,
    )


def test_character_query_does_not_export_ad_hoc_actor_packages(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Portable actors",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
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
                    "sheet": {
                        "content": {
                            "features": [
                                {
                                    "id": "trail-sign",
                                    "name": "Trail Sign",
                                    "description": (
                                        "The scout reads the source-defined trail sign."
                                    ),
                                    "activation": {"type": "passive", "cost": 0},
                                }
                            ]
                        }
                    },
                },
                "idempotency_key": "actor",
            },
        )
        with pytest.raises(ToolError, match="Input should be"):
            await _call(
                server,
                "character_query",
                {
                    "view": "content_package",
                    "payload": {
                        "character_id": actor["id"],
                        "portable_id": "example.portable-scout",
                    },
                },
            )

    asyncio.run(exercise())


def test_module_package_round_trip_recreates_cast_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def import_markdown(server, campaign_id: str) -> dict:
        staged = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign_id,
                "action": "start",
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
        ingested = staged
        campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign_id}},
        )
        await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign_id,
                "action": "get",
                "payload": {"job_id": job_id},
                "expected_revision": campaign["revision"],
                "idempotency_key": "activate",
            },
        )
        return ingested

    async def exercise() -> None:
        config = _config(tmp_path)
        server = create_server(config)
        source_campaign = await _call(
            server,
            "campaign_create",
            {"name": "Package source", "edition": "2014", "idempotency_key": "source"},
        )
        staged = await import_markdown(server, source_campaign["id"])
        module_id = staged["module_id"]
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
            "module_draft",
            {
                "campaign_id": source_campaign["id"],
                "action": "edit",
                "payload": {
                    "operation": "actor",
                    "module_id": module_id,
                    "scene_id": scene_index[0]["scene_id"],
                    "character_id": actor["id"],
                    "actor_card_id": "example.keep.guard",
                    "binding_kind": "cast",
                    "role": "gate guard",
                },
            },
        )
        finalized = await finalize_and_activate_module(
            _call,
            server,
            source_campaign["id"],
            staged,
            source_key="example.keep",
            title="The Keep",
            portable_id="example.keep",
            activate=False,
        )
        exported = finalized["finalized"]
        exported_package, exported_blobs = loads_content_archive(
            (config.content_packages_dir / exported["artifact"]).read_bytes()
        )
        portrait_output = BytesIO()
        Image.new("RGB", (12, 12), "#654321").save(portrait_output, format="PNG")
        portrait = portrait_output.getvalue()
        portrait_asset = blob_descriptor(
            asset_key="actor.gate-guard.image",
            kind="actor_image",
            name="gate-guard.png",
            media_type="image/png",
            content=portrait,
            license="user-supplied",
            attribution="Test source",
            source_refs=exported_package["actors"][0]["provenance"]["source_refs"],
        )
        actor_with_image = deepcopy(exported_package["actors"][0])
        actor_with_image["image"] = {
            "asset_key": portrait_asset["asset_key"],
            "alt": "Gate Guard portrait",
        }
        image_package = build_content_package(
            kind=exported_package["kind"],
            package_id=exported_package["id"],
            version=exported_package["version"],
            system_id=exported_package["system_id"],
            manifest=exported_package["manifest"],
            sources=exported_package["sources"],
            assets=[*exported_package["assets"], portrait_asset],
            content_reviews=exported_package["content_reviews"],
            actors=[actor_with_image],
            content=exported_package["content"],
            dependencies=exported_package["dependencies"],
            metadata=exported_package["metadata"],
        )
        exported_blobs[portrait_asset["checksum"]] = portrait
        image_artifact = "image-module.sagasmith-pack"
        (config.content_packages_dir / image_artifact).write_bytes(
            dumps_content_archive(image_package, exported_blobs)
        )
        target_campaign = await _call(
            server,
            "campaign_create",
            {"name": "Package target", "edition": "2014", "idempotency_key": "target"},
        )
        import_arguments = {
            "action": "import",
            "payload": {
                "kind": "module",
                "campaign_id": target_campaign["id"],
                "artifact": image_artifact,
            },
            "idempotency_key": "package-import",
        }
        original_bind = ModuleService.bind_actor
        interrupted = False

        def interrupt_after_binding(self, *args, **kwargs):
            nonlocal interrupted
            result = original_bind(self, *args, **kwargs)
            if not interrupted:
                interrupted = True
                raise RuntimeError("simulated interruption after actor binding")
            return result

        monkeypatch.setattr(ModuleService, "bind_actor", interrupt_after_binding)
        with pytest.raises(ToolError, match="simulated interruption"):
            await _call(server, "content_pack", import_arguments)
        assert (
            await _call(
                server,
                "content_pack",
                {
                    "action": "list",
                    "payload": {"kind": "module", "campaign_id": target_campaign["id"]},
                },
            )
            == []
        )
        assert (
            await _call(
                server,
                "character_query",
                {"view": "list", "payload": {"campaign_id": target_campaign["id"]}},
            )
            == []
        )
        monkeypatch.setattr(ModuleService, "bind_actor", original_bind)
        imported = await _call(
            server,
            "content_pack",
            import_arguments,
        )
        replay = await _call(server, "content_pack", import_arguments)
        bindings = await _call(
            server,
            "module_query",
            {
                "campaign_id": target_campaign["id"],
                "view": "actors",
                "payload": {"module_id": imported["module_id"]},
            },
        )
        detail = await _call(
            server,
            "content_pack",
            {
                "action": "get",
                "payload": {
                    "kind": "module",
                    "campaign_id": target_campaign["id"],
                    "module_id": imported["module_id"],
                    "include_package": True,
                },
            },
        )
        reexported = await _call(
            server,
            "content_pack",
            {
                "action": "export",
                "payload": {
                    "kind": "module",
                    "campaign_id": target_campaign["id"],
                    "module_id": imported["module_id"],
                },
            },
        )

        assert exported["summary"]["actors"] == 1
        assert imported["activated"] is False
        assert replay["module_id"] == imported["module_id"]
        assert replay["actor_map"] == imported["actor_map"]
        assert len(bindings) == 1
        assert imported["actor_map"]["example.keep.guard"] != actor["id"]
        assert bindings[0]["actor_card_id"] == "example.keep.guard"
        assert "portable_actor_id" not in bindings[0]
        assert bindings[0]["scene_key"] == scene_index[0]["stable_key"]
        assert bindings[0]["role"] == "gate guard"
        imported_actor = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": imported["actor_map"]["example.keep.guard"]},
            },
        )
        assert imported_actor["notes"]["profile"]["portrait_ref"]["alt"] == ("Gate Guard portrait")
        assert detail["package"]["id"] == "example.keep"
        assert detail["package"]["checksum"] == imported["artifact"]["checksum"]
        assert reexported["artifact"] == imported["artifact"]["artifact"]

    asyncio.run(exercise())


def test_bundled_srd_monster_presets_are_catalog_imports(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path, presets=True))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Preset catalog",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        shared = await _call(
            server,
            "content_pack",
            {
                "action": "list",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "preset",
                    "edition": "2014",
                    "include_package": True,
                },
            },
        )
        catalog = shared["content_package"]["actors"]
        frog = next(item for item in catalog if item["name"] == "Frog")
        imported = await _call(
            server,
            "character_create_from",
            {
                "mode": "content_actor",
                "payload": {
                    "campaign_id": campaign["id"],
                    "artifact": shared["artifact"]["artifact"],
                    "artifact_id": frog["id"],
                },
                "idempotency_key": "frog",
            },
        )
        imported_from_shared_pack = await _call(
            server,
            "character_create_from",
            {
                "mode": "content_actor",
                "payload": {
                    "campaign_id": campaign["id"],
                    "artifact": shared["artifact"]["artifact"],
                    "artifact_id": frog["id"],
                    "name": "Shared Frog",
                },
                "idempotency_key": "shared-frog",
            },
        )

        assert len(catalog) == 317
        assert shared["package"]["cards"] == 317
        assert shared["artifact"]["kind"] == "preset"
        assert "readiness" not in shared["content_package"]
        assert imported["character"]["character_type"] == "monster"
        assert imported["character"]["name"] == "Frog"
        assert imported["character"]["sheet"]["inventory"]["items"] == []
        assert imported_from_shared_pack["character"]["name"] == "Shared Frog"
        assert imported_from_shared_pack["content_actor"]["id"] in {
            actor["id"] for actor in shared["content_package"]["actors"]
        }

    asyncio.run(exercise())


def test_imported_preset_inventory_and_detail_remain_readable_in_play(tmp_path: Path) -> None:
    notes = default_character_notes()
    notes["profile"]["summary"] = "A portable third-party preset."
    card = build_dnd_content_actor(
        actor_id="example.preset.scout",
        version="1.0.0",
        actor_type="npc",
        name="Preset Scout",
        sheet=default_character_sheet(),
        notes=notes,
    )
    package, blobs = build_preset_content_package(
        package_id="example.preset-library",
        version="1.0.0",
        system_id="dnd5e",
        title="Example Preset Library",
        cards=[card],
        metadata={
            "edition": "2014",
            "distribution": "private",
            "license": "user-supplied",
            "attribution": "Test fixture",
        },
    )
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    archive_path = archive_dir / "preset.sagasmith-pack"
    archive_path.write_bytes(dumps_content_archive(package, blobs))

    async def exercise() -> None:
        server = create_server(_config(tmp_path, rule_import_roots=(archive_dir,)))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Preset inventory",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        imported = await _call(
            server,
            "content_pack",
            {
                "action": "import",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "preset",
                    "source_path": str(archive_path),
                },
                "idempotency_key": "import-preset",
            },
        )
        assert imported["actor_catalog"]["id"] == "example.preset-library.actors"
        await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": campaign["revision"],
                "idempotency_key": "enter-play",
            },
        )
        listed = await _call(
            server,
            "content_pack",
            {
                "action": "list",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "preset",
                    "edition": "2014",
                },
            },
        )
        installed = next(item for item in listed if item["pack_id"] == package["id"])
        assert installed["local_ref"] == "example.preset-library.actors"
        assert installed["version"] == package["version"]
        assert installed["checksum"] == package["checksum"]
        assert installed["actors"] == 1
        detail = await _call(
            server,
            "content_pack",
            {
                "action": "get",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "preset",
                    "edition": "2014",
                    "pack_id": package["id"],
                    "version": package["version"],
                    "include_package": True,
                },
            },
        )
        assert detail["content_package"] == package

    asyncio.run(exercise())


def test_unified_addon_archive_import_reexport_and_actor_creation(tmp_path: Path) -> None:
    source_text = "# Archive Rule\nA source-backed archive rule."
    chunk_key = rule_chunk_key("example.archive-source", 0, 0, source_text)
    component = {
        "id": "dnd5e.example.archive-rules",
        "version": "2.0.0",
        "system_id": "dnd5e",
        "manifest": {
            "id": "dnd5e.example.archive-rules",
            "version": "2.0.0",
            "title": "Archive Rules",
            "namespace": "dnd5e.example.archive-rules",
            "system_id": "dnd5e",
            "editions": ["2014"],
            "dependencies": [],
            "conflicts": [],
            "capabilities": [],
        },
        "artifacts": [],
        "mechanics": [],
        "sources": [
            {
                "source_key": "example.archive-source",
                "title": "Archive Source",
                "edition": "2014",
                "locale": "en",
                "version": "2.0.0",
                "publication_id": "example.archive-source",
                "authority": "supplement",
                "canonical_source_key": None,
                "checksum": hashlib.sha256(source_text.encode()).hexdigest(),
                "metadata": {},
                "sections": [
                    {
                        "ordinal": 0,
                        "parent_ordinal": None,
                        "level": 1,
                        "title": "Archive Rule",
                        "path": ["Archive Rule"],
                        "content": source_text,
                        "content_hash": hashlib.sha256(source_text.encode()).hexdigest(),
                        "start_offset": 0,
                        "end_offset": len(source_text),
                        "chunks": [
                            {
                                "key": chunk_key,
                                "ordinal": 0,
                                "heading_path": ["Archive Rule"],
                                "content": source_text,
                                "content_hash": hashlib.sha256(source_text.encode()).hexdigest(),
                                "token_count": len(source_text.split()),
                                "metadata": {
                                    "start_offset": 0,
                                    "end_offset": len(source_text),
                                    "page_start": 1,
                                    "page_end": 1,
                                },
                            }
                        ],
                    }
                ],
            }
        ],
        "metadata": {"distribution": "private"},
        "dependencies": [],
    }
    notes = default_character_notes()
    notes["profile"]["summary"] = "A source-backed archive actor."
    card = build_dnd_content_actor(
        actor_id="dnd5e.example.archive-actor",
        version="2.0.0",
        actor_type="monster",
        name="Archive Actor",
        sheet=default_character_sheet(),
        notes=notes,
    )
    package, blobs = build_rule_content_package(
        package_id="dnd5e.example.archive-addon",
        version="2.0.0",
        system_id="dnd5e",
        manifest={
            "id": "dnd5e.example.archive-addon",
            "version": "2.0.0",
            "system_id": "dnd5e",
            "title": "Archive Addon",
            "classification": "third_party",
            "editions": ["2014"],
            "activation": {
                "rule_policy": "branch",
                "preset_policy": "library",
                "module_policy": "none",
            },
        },
        rule_descriptors=[component],
        preset_actors=[card],
        metadata={
            "distribution": "private",
            "license": "user-supplied",
            "attribution": "Test source",
        },
    )
    portrait_output = BytesIO()
    Image.new("RGB", (16, 20), "#315a72").save(portrait_output, format="PNG")
    portrait = portrait_output.getvalue()
    portrait_asset = blob_descriptor(
        asset_key="actor.archive-actor.image",
        kind="actor_image",
        name="archive-actor.png",
        media_type="image/png",
        content=portrait,
        license="user-supplied",
        attribution="Test source",
        source_refs=package["actors"][0]["provenance"]["source_refs"],
    )
    actor = deepcopy(package["actors"][0])
    actor["image"] = {
        "asset_key": portrait_asset["asset_key"],
        "alt": "Archive Actor portrait",
    }
    package = build_content_package(
        kind=package["kind"],
        package_id=package["id"],
        version=package["version"],
        system_id=package["system_id"],
        manifest=package["manifest"],
        sources=package["sources"],
        assets=[*package["assets"], portrait_asset],
        content_reviews=package["content_reviews"],
        actors=[actor],
        content=package["content"],
        dependencies=package["dependencies"],
        metadata=package["metadata"],
    )
    blobs[portrait_asset["checksum"]] = portrait
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    archive_path = archive_dir / "archive-addon.sagasmith-pack"
    archive_path.write_bytes(dumps_content_archive(package, blobs))

    async def exercise() -> None:
        server = create_server(_config(tmp_path, rule_import_roots=(archive_dir,)))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Unified content receiver",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        imported = await _call(
            server,
            "content_pack",
            {
                "action": "import",
                "payload": {
                    "kind": "addon",
                    "campaign_id": campaign["id"],
                    "source_path": str(archive_path),
                },
                "idempotency_key": "import-addon",
            },
        )
        assert imported["stored"] is True
        assert imported["activated"] is False
        assert {item["status"] for item in imported["components"]} == {"stored"}
        assert imported["actor_catalog"]["status"] == "stored"
        assert imported["addon"]["status"] == "stored"
        detail = await _call(
            server,
            "content_pack",
            {
                "action": "get",
                "payload": {
                    "kind": "addon",
                    "campaign_id": campaign["id"],
                    "addon_id": package["id"],
                    "version": package["version"],
                    "include_package": True,
                },
            },
        )
        assert detail["package"] == package
        assert detail["addon"]["status"] == "stored"
        assert {item["status"] for item in detail["components"]} == {"stored"}
        listed = await _call(
            server,
            "content_pack",
            {
                "action": "list",
                "payload": {
                    "kind": "addon",
                    "campaign_id": campaign["id"],
                    "addon_id": package["id"],
                },
            },
        )
        assert listed[0]["status"] == "stored"
        core_inventory = await _call(
            server,
            "content_pack",
            {
                "action": "list",
                "payload": {
                    "kind": "core_rules",
                    "campaign_id": campaign["id"],
                },
            },
        )
        preset_inventory = await _call(
            server,
            "content_pack",
            {
                "action": "list",
                "payload": {
                    "kind": "preset",
                    "campaign_id": campaign["id"],
                    "edition": "2014",
                },
            },
        )
        assert all(item["pack_id"] != component["id"] for item in core_inventory)
        assert all(item["pack_id"] != package["id"] for item in preset_inventory)
        created = await _call(
            server,
            "character_create_from",
            {
                "mode": "content_actor",
                "payload": {
                    "campaign_id": campaign["id"],
                    "artifact": detail["artifact"]["artifact"],
                    "artifact_id": package["actors"][0]["id"],
                },
                "idempotency_key": "create-actor",
            },
        )
        assert created["character"]["name"] == "Archive Actor"
        assert created["actor_knowledge_imported"] is False
        assert created["content_actor"]["image_retained_by_runtime"] is True
        portrait_ref = created["character"]["notes"]["profile"]["portrait_ref"]
        assert portrait_ref == {
            "asset_key": portrait_asset["asset_key"],
            "checksum": portrait_asset["checksum"],
            "media_type": "image/png",
            "alt": "Archive Actor portrait",
            "source": {
                "kind": "content_pack",
                "package_id": package["id"],
                "package_version": package["version"],
                "package_checksum": package["checksum"],
            },
        }
        assert (
            tmp_path / "home" / "artifacts" / "actor-images" / portrait_asset["checksum"]
        ).read_bytes() == portrait

    asyncio.run(exercise())
