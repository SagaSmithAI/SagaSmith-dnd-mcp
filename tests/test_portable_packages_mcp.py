from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from sagasmith_core.content_pack import dumps_content_archive
from sagasmith_core.portable import build_rule_pack, portable_rule_chunk_key
from sagasmith_dnd.character_schema import default_character_notes, default_character_sheet
from sagasmith_dnd.content_packages import build_addon_content_package
from sagasmith_dnd.content_validation import (
    build_catalog_review,
    build_selection_contract,
)
from sagasmith_dnd.portable_cards import build_dnd_actor_card

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import (
    _artifact_statblock_source_chunks,
    _cached_rapidocr_provider,
    _index_statblock_source_chunks,
    audit_dnd_addon_semantics,
    audit_dnd_addon_validation_components,
    create_server,
    finalize_dnd_addon_resolution_components,
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


def _passing_catalog_decisions() -> list[dict]:
    checks = {
        "identity": True,
        "classification": True,
        "entry_boundary": True,
        "references": True,
    }
    return [
        {
            "role": "primary",
            "reviewer": "deterministic:catalog-parser",
            "method": "deterministic",
            "checks": checks,
            "notes": "Exact source entry passed structural validation.",
        },
        {
            "role": "critic",
            "reviewer": "deterministic:reference-auditor",
            "method": "deterministic",
            "checks": checks,
            "notes": "Independent boundary and reference audit passed.",
        },
    ]


def test_addon_validation_recomputes_all_four_dimensions_from_content() -> None:
    pack_id = "dnd5e.example.ready-addon"
    source_text = "A harmless imported creature description."
    source_hash = hashlib.sha256(source_text.encode()).hexdigest()
    source_key = "example.ready-addon"
    chunk_key = f"{source_key}/section-0/chunk-0-{source_hash[:16]}"
    artifact = {
        "id": f"{pack_id}.statblock.harmless",
        "kind": "statblock",
        "application_state": "catalog_only",
        "mechanical_scope": "descriptive",
        "execution_state": "descriptive_ready",
        "card": {
            "name": "Harmless",
            "description": source_text,
        },
        "rule_clauses": [
            {
                "schema_version": 1,
                "id": "description",
                "title": "Harmless",
                "scope": "descriptive",
                "source_citations": [
                    {
                        "source": f"rule-source:{source_key}",
                        "source_ref": {"chunk_key": chunk_key},
                        "source_excerpt": source_text,
                    }
                ],
                "settlement": {"mode": "descriptive"},
            }
        ],
        "semantic_resolution": {
            "status": "resolved",
            "mode": "descriptive",
            "first_use_compilation_required": False,
            "clause_ids": ["description"],
        },
    }
    artifact["catalog_review"] = build_catalog_review(
        artifact,
        decisions=_passing_catalog_decisions(),
    )
    artifact["selection_contract"] = build_selection_contract(
        artifact,
        status="not_applicable",
    )
    component = build_rule_pack(
        portable_id=pack_id,
        version="1.0.0",
        system_id="dnd5e",
        manifest={
            "id": pack_id,
            "version": "1.0.0",
            "title": "Ready Addon",
            "namespace": pack_id,
            "system_id": "dnd5e",
            "editions": ["2014"],
            "dependencies": [],
            "conflicts": [],
            "capabilities": [],
            "content_kinds": ["statblock"],
        },
        artifacts=[artifact],
        mechanics=[],
        provenance={"distribution": "private"},
        sources=[
            {
                "source_key": source_key,
                "title": "Ready Addon Source",
                "edition": "2014",
                "locale": "en",
                "version": "1.0.0",
                "publication_id": source_key,
                "authority": "supplement",
                "canonical_source_key": None,
                "checksum": source_hash,
                "metadata": {},
                "sections": [
                    {
                        "ordinal": 0,
                        "parent_ordinal": None,
                        "level": 1,
                        "title": "Harmless",
                        "path": ["Harmless"],
                        "content": source_text,
                        "content_hash": source_hash,
                        "start_offset": 0,
                        "end_offset": len(source_text),
                        "chunks": [
                            {
                                "key": chunk_key,
                                "ordinal": 0,
                                "heading_path": ["Harmless"],
                                "content": source_text,
                                "content_hash": source_hash,
                                "token_count": 5,
                                "metadata": {},
                            }
                        ],
                    }
                ],
            }
        ],
        metadata={"distribution": "private"},
        dependencies=[],
    )

    report = audit_dnd_addon_validation_components([component])

    assert report["complete"] is True
    assert report["source"]["verified_count"] == 1
    assert report["catalog"]["reviewed_count"] == 1
    assert report["selection"] == {
        "applicable_count": 0,
        "ready_count": 0,
        "not_applicable_count": 1,
        "complete": True,
        "blockers": [],
    }
    assert report["runtime"]["modes"] == {"descriptive": 1}

    blocked = copy.deepcopy(component)
    blocked_artifact = blocked["payload"]["artifacts"][0]
    blocked_artifact["selection_contract"] = build_selection_contract(
        blocked_artifact,
        status="blocked",
        blockers=["reviewed card is not selection-ready"],
    )
    blocked_report = audit_dnd_addon_validation_components([blocked])
    assert blocked_report["selection"]["complete"] is False
    assert blocked_report["selection"]["blockers"][0]["reason"] == (
        "reviewed card is not selection-ready"
    )

    stale = copy.deepcopy(component)
    stale["payload"]["artifacts"][0]["card"]["name"] = "Changed"
    stale_report = audit_dnd_addon_validation_components([stale])
    assert stale_report["catalog"]["complete"] is False
    assert "stale" in stale_report["catalog"]["blockers"][0]["reason"]


def test_addon_resolution_audit_ignores_items_without_semantic_effects() -> None:
    report = audit_dnd_addon_semantics(
        [
            {
                "kind": "preset_pack",
                "id": "example.actors",
                "payload": {
                    "cards": [
                        {
                            "id": "example.guard",
                            "payload": {
                                "name": "Guard",
                                "sheet": {
                                    "content": {},
                                    "inventory": {
                                        "items": [
                                            {
                                                "id": "chain-shirt",
                                                "name": "Chain Shirt",
                                                "kind": "armor",
                                                "mechanics": {
                                                    "armor_class_base": 13,
                                                },
                                            }
                                        ]
                                    },
                                },
                            },
                        }
                    ]
                },
            }
        ]
    )

    assert report["complete"] is True
    assert report["actor_entry_count"] == 0
    assert report["unresolved"] == []


def test_addon_resolution_audit_is_independent_of_component_order() -> None:
    empty_semantic_validation = {
        "schema_version": 1,
        "complete": True,
        "artifact_count": 0,
        "resolved_count": 0,
        "modes": {},
        "unresolved": [],
        "first_use_compilation_required": False,
    }

    def component(component_id: str) -> dict:
        return {
            "kind": "rule_pack",
            "id": component_id,
            "version": "1.0.0",
            "payload": {
                "manifest": {
                    "resolution_policy": "build_time_complete",
                    "semantic_validation": copy.deepcopy(empty_semantic_validation),
                },
                "artifacts": [],
                "mechanics": [],
            },
        }

    first = component("dnd5e.example.first")
    second = component("dnd5e.example.second")

    forward = audit_dnd_addon_semantics([first, second])
    reverse = audit_dnd_addon_semantics([second, first])

    assert reverse == forward
    assert [item["id"] for item in reverse["rule_packs"]] == [
        "dnd5e.example.first",
        "dnd5e.example.second",
    ]


def test_addon_resolution_audit_rejects_partial_mechanic_refs_without_ruling() -> None:
    report = audit_dnd_addon_semantics(
        [
            {
                "kind": "preset_pack",
                "id": "example.actors",
                "payload": {
                    "cards": [
                        {
                            "id": "example.odd-mage",
                            "payload": {
                                "name": "Odd Mage",
                                "sheet": {
                                    "content": {
                                        "features": [
                                            {
                                                "id": "odd-aura",
                                                "name": "Odd Aura",
                                                "description": (
                                                    "Creatures in the aura suffer the "
                                                    "source-defined effect."
                                                ),
                                                "mechanic_refs": [
                                                    "dnd5e.core.activity.resource_accounting"
                                                ],
                                            }
                                        ]
                                    },
                                    "inventory": {"items": []},
                                },
                            },
                        }
                    ]
                },
            }
        ]
    )

    assert report["complete"] is False
    assert report["actor_entry_count"] == 1
    assert report["unresolved"][0]["artifact_id"] == "Odd Mage:odd-aura"


def test_addon_export_finalizes_stale_resolved_agent_state() -> None:
    pack_id = "dnd5e.example.resolved-addon"
    source_text = "Use the exact source-defined procedure."
    source_hash = hashlib.sha256(source_text.encode()).hexdigest()
    source_key = "example.resolved-addon"
    source = {
        "source_key": source_key,
        "title": "Resolved Addon Source",
        "edition": "2014",
        "locale": "en",
        "version": "1.0.0",
        "publication_id": source_key,
        "authority": "supplement",
        "canonical_source_key": None,
        "checksum": source_hash,
        "metadata": {},
        "sections": [
            {
                "ordinal": 0,
                "parent_ordinal": None,
                "level": 1,
                "title": "Odd Device",
                "path": ["Odd Device"],
                "content": source_text,
                "content_hash": source_hash,
                "start_offset": 0,
                "end_offset": len(source_text),
                "chunks": [
                    {
                        "key": f"{source_key}/section-0/chunk-0-{source_hash[:16]}",
                        "ordinal": 0,
                        "heading_path": ["Odd Device"],
                        "content": source_text,
                        "content_hash": source_hash,
                        "token_count": 7,
                        "metadata": {},
                    }
                ],
            }
        ],
    }
    component = build_rule_pack(
        portable_id=pack_id,
        version="1.0.0",
        system_id="dnd5e",
        manifest={
            "id": pack_id,
            "version": "1.0.0",
            "title": "Resolved Addon",
            "namespace": pack_id,
            "system_id": "dnd5e",
            "editions": ["2014"],
            "dependencies": [],
            "conflicts": [],
            "capabilities": [],
            "content_kinds": ["feature"],
        },
        artifacts=[
            {
                "id": f"{pack_id}.feature.odd-device",
                "kind": "feature",
                "application_state": "catalog_only",
                "mechanical_scope": "mechanical",
                "execution_state": "agent_resolution_required",
                "card": {
                    "name": "Odd Device",
                    "description": "Use the exact source-defined procedure.",
                },
                "rule_clauses": [
                    {
                        "schema_version": 1,
                        "id": "source-resolution",
                        "title": "Odd Device",
                        "scope": "mechanical",
                        "source_citations": [
                            {
                                "source": "rule-source:example",
                                "source_ref": {"page_number": 1},
                                "source_excerpt": ("Use the exact source-defined procedure."),
                            }
                        ],
                        "settlement": {
                            "mode": "agent_ruling",
                            "default_resolver": "agent",
                            "ruling_kind": "agent_dm_adjudication",
                            "reason": (
                                "The exact imported procedure is resolved by the "
                                "Agent-as-DM boundary."
                            ),
                        },
                    }
                ],
                "semantic_resolution": {
                    "status": "resolved",
                    "mode": "agent_ruling",
                    "first_use_compilation_required": False,
                    "clause_ids": ["source-resolution"],
                },
            }
        ],
        mechanics=[],
        provenance={"distribution": "private"},
        sources=[source],
        metadata={"distribution": "private"},
        dependencies=[],
    )

    before = audit_dnd_addon_semantics([component])
    finalized = finalize_dnd_addon_resolution_components([component])

    artifact = finalized[0]["payload"]["artifacts"][0]
    manifest = finalized[0]["payload"]["manifest"]
    assert artifact["execution_state"] == "ruling_ready"
    assert before["complete"] is False
    assert any(
        item["artifact_id"].endswith(":resolution-manifest") for item in before["unresolved"]
    )
    assert manifest["resolution_policy"] == "build_time_complete"
    assert manifest["semantic_validation"]["complete"] is True
    assert manifest["semantic_validation"]["unresolved"] == []
    assert finalized[0]["checksum"] != component["checksum"]
    assert audit_dnd_addon_semantics(finalized)["complete"] is True


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


def test_character_card_export_and_import_uses_fresh_identity(tmp_path: Path) -> None:
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
        exported = await _call(
            server,
            "character_query",
            {
                "view": "content_package",
                "payload": {
                    "character_id": actor["id"],
                    "portable_id": "example.portable-scout",
                    "image": {
                        "media_type": "image/png",
                        "data_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nportable-scout").decode(
                            "ascii"
                        ),
                        "checksum": hashlib.sha256(b"\x89PNG\r\n\x1a\nportable-scout").hexdigest(),
                        "size": 22,
                        "alt": "Portable Scout portrait",
                        "license": "CC0-1.0",
                        "attribution": "Test fixture",
                        "source_ref": "fixture:portable-scout.png",
                    },
                },
            },
        )
        imported = await _call(
            server,
            "character_create_from",
            {
                "mode": "content_actor",
                "payload": {
                    "artifact": exported["artifact"]["artifact"],
                    "artifact_id": exported["actor"]["id"],
                },
                "idempotency_key": "import",
            },
        )

        assert imported["character"]["id"] != actor["id"]
        assert imported["character"]["campaign_id"] is None
        assert imported["content_actor"]["id"] == "example.portable-scout.actor"
        assert imported["actor_knowledge_imported"] is False
        assert imported["content_actor"]["image_retained_by_runtime"] is False
        assert "image" not in imported["character"]
        assert exported["artifact"]["artifact"].endswith(".sagasmith-pack")
        assert "campaign_id" not in exported["actor"]
        requirement = exported["actor"]["sheet"]["content"]["features"][0]["ruling_requirements"][0]
        assert requirement["policy_ref"] == "actor_card.import.v1"
        assert requirement["default_resolver"] == "agent"
        assert imported["character"]["sheet"]["content"]["features"][0]["ruling_requirements"] == [
            requirement
        ]

    asyncio.run(exercise())


def test_module_package_round_trip_recreates_cast_bindings(tmp_path: Path) -> None:
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
        server = create_server(_config(tmp_path))
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
                    "portable_actor_id": "example.keep.guard",
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
        target_campaign = await _call(
            server,
            "campaign_create",
            {"name": "Package target", "edition": "2014", "idempotency_key": "target"},
        )
        with pytest.raises(ToolError, match="payload.activate must be a boolean"):
            await _call(
                server,
                "content_pack",
                {
                    "action": "import",
                    "payload": {
                        "kind": "module",
                        "campaign_id": target_campaign["id"],
                        "artifact": exported["artifact"],
                        "activate": "false",
                    },
                    "idempotency_key": "invalid-activation-type",
                },
            )
        import_arguments = {
            "action": "import",
            "payload": {
                "kind": "module",
                "campaign_id": target_campaign["id"],
                "artifact": exported["artifact"],
                "activate": False,
            },
            "idempotency_key": "package-import",
        }
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

        assert exported["summary"]["actors"] == 1
        assert imported["activated"] is False
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
            {
                "name": "Preset catalog",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        catalog = await _call(
            server,
            "content_pack",
            {
                "action": "list",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "catalog",
                    "content_kind": "actor_card",
                },
            },
        )
        frog = next(item for item in catalog if item["name"] == "Frog")
        shared = await _call(
            server,
            "content_pack",
            {
                "action": "list",
                "payload": {
                    "kind": "actor_preset",
                    "edition": "2014",
                    "include_package": True,
                },
            },
        )
        imported = await _call(
            server,
            "character_create_from",
            {
                "mode": "content_actor",
                "payload": {"campaign_id": campaign["id"], "artifact_id": frog["id"]},
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


def test_unified_addon_archive_import_reexport_and_actor_creation(tmp_path: Path) -> None:
    source_text = "# Archive Rule\nA source-backed archive rule."
    chunk_key = portable_rule_chunk_key("example.archive-source", 0, 0, source_text)
    component = build_rule_pack(
        portable_id="dnd5e.example.archive-rules",
        version="2.0.0",
        system_id="dnd5e",
        manifest={
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
        artifacts=[],
        mechanics=[],
        sources=[
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
        metadata={"distribution": "private"},
    )
    notes = default_character_notes()
    notes["profile"]["summary"] = "A source-backed archive actor."
    card = build_dnd_actor_card(
        portable_id="dnd5e.example.archive-actor",
        version="2.0.0",
        actor_type="monster",
        name="Archive Actor",
        sheet=default_character_sheet(),
        notes=notes,
    )
    package, blobs = build_addon_content_package(
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
        rule_components=[component],
        preset_cards=[card],
        metadata={
            "distribution": "private",
            "license": "user-supplied",
            "attribution": "Test source",
        },
    )
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
        assert imported["installed"] is True
        assert imported["activated"] is False
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

    asyncio.run(exercise())
