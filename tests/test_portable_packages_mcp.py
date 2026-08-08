from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest
from sagasmith_core.portable import build_rule_pack
from sagasmith_dnd.content_readiness import (
    build_catalog_review,
    build_selection_contract,
)
from sagasmith_dnd.resolution_plan import (
    compile_resolution_plan,
    resolution_plan_template,
)

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import (
    audit_dnd_addon_readiness_components,
    audit_dnd_addon_resolution_components,
    create_server,
    finalize_dnd_addon_resolution_components,
)


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


def test_addon_readiness_recomputes_all_four_dimensions_from_content() -> None:
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

    report = audit_dnd_addon_readiness_components([component])

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
    blocked_report = audit_dnd_addon_readiness_components([blocked])
    assert blocked_report["selection"]["complete"] is False
    assert blocked_report["selection"]["blockers"][0]["reason"] == (
        "reviewed card is not selection-ready"
    )

    stale = copy.deepcopy(component)
    stale["payload"]["artifacts"][0]["card"]["name"] = "Changed"
    stale_report = audit_dnd_addon_readiness_components([stale])
    assert stale_report["catalog"]["complete"] is False
    assert "stale" in stale_report["catalog"]["blockers"][0]["reason"]


def test_addon_resolution_audit_ignores_items_without_semantic_effects() -> None:
    report = audit_dnd_addon_resolution_components(
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
    empty_rule_readiness = {
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
                    "resolution_readiness": copy.deepcopy(empty_rule_readiness),
                },
                "artifacts": [],
                "mechanics": [],
            },
        }

    first = component("dnd5e.example.first")
    second = component("dnd5e.example.second")

    forward = audit_dnd_addon_resolution_components([first, second])
    reverse = audit_dnd_addon_resolution_components([second, first])

    assert reverse == forward
    assert [item["id"] for item in reverse["rule_packs"]] == [
        "dnd5e.example.first",
        "dnd5e.example.second",
    ]


def test_addon_resolution_audit_rejects_partial_mechanic_refs_without_ruling() -> None:
    report = audit_dnd_addon_resolution_components(
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

    before = audit_dnd_addon_resolution_components([component])
    finalized = finalize_dnd_addon_resolution_components([component])

    artifact = finalized[0]["payload"]["artifacts"][0]
    manifest = finalized[0]["payload"]["manifest"]
    assert artifact["execution_state"] == "ruling_ready"
    assert before["complete"] is False
    assert any(
        item["artifact_id"].endswith(":resolution-manifest") for item in before["unresolved"]
    )
    assert manifest["resolution_policy"] == "build_time_complete"
    assert manifest["resolution_readiness"]["complete"] is True
    assert manifest["resolution_readiness"]["unresolved"] == []
    assert finalized[0]["checksum"] != component["checksum"]
    assert audit_dnd_addon_resolution_components(finalized)["complete"] is True


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
                "view": "portable_card",
                "payload": {
                    "character_id": actor["id"],
                    "portable_id": "example.portable-scout",
                    "image": {
                        "media_type": "image/png",
                        "data_base64": base64.b64encode(
                            b"\x89PNG\r\n\x1a\nportable-scout"
                        ).decode("ascii"),
                        "checksum": hashlib.sha256(
                            b"\x89PNG\r\n\x1a\nportable-scout"
                        ).hexdigest(),
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
                "mode": "portable_card",
                "payload": {"card": exported["card"]},
                "idempotency_key": "import",
            },
        )

        assert imported["character"]["id"] != actor["id"]
        assert imported["character"]["campaign_id"] is None
        assert imported["portable_card"]["id"] == "example.portable-scout"
        assert imported["actor_knowledge_imported"] is False
        assert imported["portable_card"]["image_retained_by_runtime"] is False
        assert "image" not in imported["character"]
        assert exported["card"]["payload"]["image"]["license"] == "CC0-1.0"
        assert exported["artifact"]["artifact"].endswith(".sagasmith.json")
        assert "campaign_id" not in exported["card"]["payload"]
        requirement = exported["card"]["payload"]["sheet"]["content"]["features"][0][
            "ruling_requirements"
        ][0]
        assert requirement["policy_ref"] == "actor_card.import.v1"
        assert requirement["default_resolver"] == "agent"
        assert imported["character"]["sheet"]["content"]["features"][0]["ruling_requirements"] == [
            requirement
        ]

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
        addon_export = await _call(
            server,
            "rule_pack_query",
            {
                "view": "addon_package",
                "payload": {
                    "campaign_id": source_campaign["id"],
                    "portable_id": "example.keep.addon",
                    "version": "1.0.0",
                    "manifest": {
                        "id": "example.keep.addon",
                        "version": "1.0.0",
                        "system_id": "dnd5e",
                        "title": "The Keep Addon",
                        "editions": ["2014"],
                        "classification": "homebrew",
                        "content_summary": {"module": 1, "npc": 1},
                        "activation": {
                            "rule_policy": "none",
                            "preset_policy": "none",
                            "module_policy": "campaign",
                        },
                    },
                    "components": [exported["package"]],
                    "metadata": {
                        "distribution": "private",
                        "license": "user-supplied",
                    },
                    "include_package": True,
                },
            },
        )
        module_addon = addon_export["package"]
        addon_server = create_server(_config(tmp_path / "module-addon"))
        addon_campaign = await _call(
            addon_server,
            "campaign_create",
            {
                "name": "Module addon target",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        addon_imported = await _call(
            addon_server,
            "rule_import",
            {
                "campaign_id": addon_campaign["id"],
                "action": "import_addon",
                "payload": {"addon": module_addon},
                "idempotency_key": "import-addon",
            },
        )
        assert addon_imported["components"][0]["status"] == ("campaign_import_required")
        profile = await _call(
            addon_server,
            "campaign_rules",
            {"campaign_id": addon_campaign["id"], "action": "get_profile"},
        )
        activated_addon = await _call(
            addon_server,
            "campaign_rules",
            {
                "campaign_id": addon_campaign["id"],
                "action": "set_addon",
                "payload": {
                    "addon_id": module_addon["id"],
                    "version": module_addon["version"],
                },
                "expected_revision": profile["campaign_revision"],
                "idempotency_key": "enable-addon",
            },
        )
        module_lock = activated_addon["activation"]["component_locks"][0]
        assert module_lock["module_id"]
        addon_modules = await _call(
            addon_server,
            "module_query",
            {"campaign_id": addon_campaign["id"], "view": "list"},
        )
        assert len(addon_modules) == 1
        assert addon_modules[0]["active"] is True
        addon_campaign_state = await _call(
            addon_server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": addon_campaign["id"]}},
        )
        disabled_addon = await _call(
            addon_server,
            "campaign_rules",
            {
                "campaign_id": addon_campaign["id"],
                "action": "set_addon",
                "payload": {
                    "addon_id": module_addon["id"],
                    "version": module_addon["version"],
                    "enabled": False,
                },
                "expected_revision": addon_campaign_state["revision"],
                "idempotency_key": "disable-addon",
            },
        )
        assert disabled_addon["activation"]["enabled"] is False
        addon_modules = await _call(
            addon_server,
            "module_query",
            {"campaign_id": addon_campaign["id"], "view": "list"},
        )
        assert addon_modules == []
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
            {
                "name": "Preset catalog",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
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
        preset_readiness = audit_dnd_addon_resolution_components([shared["portable_package"]])
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
        assert preset_readiness["complete"] is True
        assert preset_readiness["unresolved"] == []
        assert preset_readiness["first_use_compilation_required"] is False
        assert imported["character"]["character_type"] == "monster"
        assert imported["character"]["name"] == "Frog"
        assert imported["character"]["sheet"]["inventory"]["items"] == []
        assert imported_from_shared_pack["character"]["name"] == "Shared Frog"
        assert imported_from_shared_pack["portable_card"]["id"] == frog["id"]

    asyncio.run(exercise())


def test_extension_rule_pack_export_import_rebinds_sources_and_stays_inactive(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "rulebooks"
    source_root.mkdir()
    rulebook = source_root / "luminous-ward.md"
    rulebook.write_text(
        "# Luminous Ward\n"
        "A creature invoking the ward gains the reviewed optional benefit.\n"
        "\n## Echo\n"
        "The ward leaves a visible echo until the scene ends.\n",
        encoding="utf-8",
    )

    async def exercise() -> None:
        source_server = create_server(
            _config(
                tmp_path / "source",
                rule_import_roots=(source_root,),
            )
        )
        campaign = await _call(
            source_server,
            "campaign_create",
            {
                "name": "Portable extension source",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        with pytest.raises(Exception, match="reserved for validated package import"):
            await _call(
                source_server,
                "rule_pack_compile",
                {
                    "action": "draft",
                    "payload": {
                        "manifest": {
                            "id": "dnd5e.example.forged-portable-proof",
                            "version": "1.0.0",
                            "title": "Forged proof",
                            "namespace": "dnd5e.example.forged-portable-proof",
                            "system_id": "dnd5e",
                            "editions": ["2014"],
                            "dependencies": [],
                            "conflicts": [],
                            "capabilities": [],
                        },
                        "provenance": {
                            "portable_package": {
                                "definition_checksum": "a" * 64,
                            }
                        },
                    },
                },
            )
        staged = await _call(
            source_server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "stage",
                "payload": {
                    "source_path": str(rulebook),
                    "source_key": "example.luminous-ward",
                    "title": "Luminous Ward",
                    "edition": "2014",
                    "publication_id": "example-extension",
                    "version": "1.0.0",
                },
                "idempotency_key": "stage",
            },
        )
        job_id = staged["job"]["id"]
        await _call(
            source_server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "inspect",
                "payload": {"job_id": job_id},
                "idempotency_key": "inspect",
            },
        )
        ingested = await _call(
            source_server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "ingest",
                "payload": {"job_id": job_id},
                "idempotency_key": "ingest",
            },
        )
        source_id = ingested["source_id"]
        chunks = await _call(
            source_server,
            "rule_pack_query",
            {"view": "source_chunks", "payload": {"source_id": source_id}},
        )
        draft = await _call(
            source_server,
            "rule_pack_compile",
            {
                "action": "from_source",
                "payload": {
                    "source_id": source_id,
                    "manifest": {
                        "id": "dnd5e.example.luminous-ward",
                        "version": "1.0.0",
                        "title": "Luminous Ward",
                        "namespace": "dnd5e.example.luminous-ward",
                        "system_id": "dnd5e",
                        "editions": ["2014"],
                        "dependencies": [],
                        "conflicts": [],
                        "capabilities": [],
                    },
                    "artifacts": [
                        {
                            "id": "dnd5e.example.luminous-ward.feature.ward",
                            "kind": "feature",
                            "card": {"name": "Luminous Ward"},
                            "application_state": "catalog_only",
                            "mechanical_scope": "descriptive",
                            "source_chunk_ids": [chunks[0]["id"]],
                            "resolution_plan": resolution_plan_template(
                                compile_resolution_plan(
                                    {
                                        "schema_version": 2,
                                        "id": ("dnd5e.example.luminous-ward.feature.ward.plan"),
                                        "source_card_id": (
                                            "dnd5e.example.luminous-ward.feature.ward"
                                        ),
                                        "source_card_kind": "feature",
                                        "trigger": "scene",
                                        "trigger_filter": {},
                                        "slots": {
                                            "target": {
                                                "kind": "actor_id",
                                                "owner": "agent",
                                                "description": (
                                                    "The creature selected from the reviewed scene."
                                                ),
                                            }
                                        },
                                        "steps": [
                                            {
                                                "id": "mark",
                                                "op": "condition.apply",
                                                "args": {
                                                    "target_ids": [{"$slot": "target"}],
                                                    "condition_id": "marked",
                                                    "source": "Luminous Ward",
                                                },
                                            }
                                        ],
                                        "citations": [
                                            {
                                                "source": ("rule-source:example.luminous-ward"),
                                                "source_ref": {"chunk_id": chunks[0]["id"]},
                                                "source_excerpt": (
                                                    "A creature invoking the ward "
                                                    "gains the reviewed optional "
                                                    "benefit."
                                                ),
                                            }
                                        ],
                                    }
                                )
                            ),
                        },
                        {
                            "id": "dnd5e.example.luminous-ward.statblock.sentinel",
                            "kind": "statblock",
                            "card": {"name": "Luminous Sentinel"},
                            "application_state": "catalog_only",
                            "mechanical_scope": "descriptive",
                            "semantic_resolution": {
                                "status": "resolved",
                                "mode": "descriptive",
                                "first_use_compilation_required": False,
                                "clause_ids": ["source-description"],
                            },
                            "source_chunk_ids": [chunks[1]["id"]],
                        },
                    ],
                    "provenance": {
                        "license": "CC-BY-4.0",
                        "attribution": "Example Author",
                    },
                },
            },
        )
        assert draft["status"] == "validated"
        with pytest.raises(Exception, match="explicit license and attribution"):
            await _call(
                source_server,
                "rule_pack_query",
                {
                    "view": "package",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "pack_id": "dnd5e.example.luminous-ward",
                        "version": "1.0.0",
                        "metadata": {
                            "distribution": "shareable",
                            "license": "",
                        },
                    },
                },
            )
        exported = await _call(
            source_server,
            "rule_pack_query",
            {
                "view": "package",
                "payload": {
                    "campaign_id": campaign["id"],
                    "pack_id": "dnd5e.example.luminous-ward",
                    "version": "1.0.0",
                    "include_package": True,
                    "metadata": {"distribution": "shareable"},
                },
            },
        )
        package = exported["package"]
        serialized = json.dumps(package, ensure_ascii=False)
        assert package["kind"] == "rule_pack"
        readiness = package["payload"]["manifest"]["resolution_readiness"]
        assert package["payload"]["manifest"]["resolution_policy"] == ("build_time_complete")
        assert readiness["complete"] is True
        assert readiness["unresolved"] == []
        assert readiness["first_use_compilation_required"] is False
        assert package["payload"]["sources"][0]["source_key"] == ("example.luminous-ward")
        portable_chunks = [
            item
            for section in package["payload"]["sources"][0]["sections"]
            for item in section["chunks"]
        ]
        assert len(portable_chunks) == 2
        assert len({item["key"] for item in portable_chunks}) == 2
        assert "source_id" not in serialized
        assert "chunk_id" not in serialized
        portable_plan = package["payload"]["artifacts"][0]["resolution_plan"]
        assert compile_resolution_plan(portable_plan).fingerprint == (portable_plan["fingerprint"])

        dependent_draft = await _call(
            source_server,
            "rule_pack_compile",
            {
                "action": "from_source",
                "payload": {
                    "source_id": source_id,
                    "manifest": {
                        "id": "dnd5e.example.prismatic-aegis",
                        "version": "1.0.0",
                        "title": "Prismatic Aegis",
                        "namespace": "dnd5e.example.prismatic-aegis",
                        "system_id": "dnd5e",
                        "editions": ["2014"],
                        "dependencies": [
                            {
                                "id": package["id"],
                                "version": package["version"],
                                "checksum": draft["checksum"],
                            }
                        ],
                        "conflicts": [],
                        "capabilities": [],
                    },
                    "artifacts": [
                        {
                            "id": "dnd5e.example.prismatic-aegis.feature.aegis",
                            "kind": "feature",
                            "card": {"name": "Prismatic Aegis"},
                            "application_state": "catalog_only",
                            "mechanical_scope": "descriptive",
                            "semantic_resolution": {
                                "status": "resolved",
                                "mode": "descriptive",
                                "first_use_compilation_required": False,
                                "clause_ids": ["source-description"],
                            },
                            "source_chunk_ids": [chunks[1]["id"]],
                        }
                    ],
                },
            },
        )
        assert dependent_draft["status"] == "validated"
        dependent_export = await _call(
            source_server,
            "rule_pack_query",
            {
                "view": "package",
                "payload": {
                    "campaign_id": campaign["id"],
                    "pack_id": "dnd5e.example.prismatic-aegis",
                    "version": "1.0.0",
                    "include_package": True,
                    "metadata": {
                        "distribution": "shareable",
                        "license": "CC-BY-4.0",
                        "attribution": "Example Author",
                    },
                },
            },
        )
        dependent_package = dependent_export["package"]
        assert (
            dependent_package["dependencies"][0]["checksum"]
            == package["metadata"]["definition_checksum"]
        )
        assert dependent_package["dependencies"][0]["checksum"] != package["checksum"]

        release = await _call(
            source_server,
            "rule_pack_query",
            {
                "view": "release",
                "payload": {
                    "campaign_id": campaign["id"],
                    "portable_id": "dnd5e.example.luminous-ward.release",
                    "version": "1.0.0",
                    "components": [
                        {
                            "kind": "rule_pack",
                            "id": package["id"],
                            "version": package["version"],
                            "checksum": package["checksum"],
                            "optional": False,
                        },
                        {
                            "kind": "rule_pack",
                            "id": dependent_package["id"],
                            "version": dependent_package["version"],
                            "checksum": dependent_package["checksum"],
                            "optional": False,
                        },
                    ],
                    "include_manifest": True,
                },
            },
        )
        assert release["artifact"]["kind"] == "release_manifest"
        addon_export = await _call(
            source_server,
            "rule_pack_query",
            {
                "view": "addon_package",
                "payload": {
                    "campaign_id": campaign["id"],
                    "portable_id": "dnd5e.example.luminous-ward.addon",
                    "version": "1.0.0",
                    "manifest": {
                        "id": "dnd5e.example.luminous-ward.addon",
                        "version": "1.0.0",
                        "system_id": "dnd5e",
                        "title": "Luminous Ward Addon",
                        "editions": ["2014"],
                        "classification": "third_party",
                        "content_summary": {"feature": 2},
                        "activation": {
                            "rule_policy": "branch",
                            "preset_policy": "none",
                            "module_policy": "none",
                        },
                    },
                    "components": [package, dependent_package],
                    "metadata": {
                        "distribution": "shareable",
                        "license": "CC-BY-4.0",
                        "attribution": "Example Author",
                    },
                    "include_package": True,
                },
            },
        )
        addon = addon_export["package"]
        assert addon_export["summary"]["components"] == 2
        readiness = addon["payload"]["manifest"]["resolution_readiness"]
        assert addon["payload"]["manifest"]["resolution_policy"] == ("build_time_complete")
        assert readiness["complete"] is True
        assert readiness["first_use_compilation_required"] is False
        assert readiness["modes"] == {
            "descriptive": 2,
            "primitive_plan": 1,
        }

        local_addon_import = await _call(
            source_server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "import_addon",
                "payload": {"addon": addon},
                "idempotency_key": "reuse-equivalent-local-rule-packs",
            },
        )
        assert local_addon_import["installed"] is True
        assert {
            (component["id"], component["status"])
            for component in local_addon_import["components"]
            if component["kind"] == "rule_pack"
        } == {
            ("dnd5e.example.luminous-ward", "installed"),
            ("dnd5e.example.prismatic-aegis", "installed"),
        }

        addon_server = create_server(_config(tmp_path / "addon-target"))
        addon_campaign = await _call(
            addon_server,
            "campaign_create",
            {
                "name": "Portable addon target",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        addon_import_arguments = {
            "campaign_id": addon_campaign["id"],
            "action": "import_addon",
            "payload": {"addon": addon},
            "idempotency_key": "addon-import",
        }
        addon_imported = await _call(addon_server, "rule_import", addon_import_arguments)
        assert await _call(addon_server, "rule_import", addon_import_arguments) == addon_imported
        assert addon_imported["installed"] is True
        assert [item["status"] for item in addon_imported["components"]] == [
            "installed",
            "installed",
        ]
        addon_detail = await _call(
            addon_server,
            "rule_pack_query",
            {
                "view": "addon",
                "payload": {
                    "campaign_id": addon_campaign["id"],
                    "addon_id": addon["id"],
                    "version": addon["version"],
                    "include_package": True,
                },
            },
        )
        assert addon_detail["package"] == addon
        profile = await _call(
            addon_server,
            "campaign_rules",
            {"campaign_id": addon_campaign["id"], "action": "get_profile"},
        )
        enable_arguments = {
            "campaign_id": addon_campaign["id"],
            "action": "set_addon",
            "payload": {"addon_id": addon["id"], "version": addon["version"]},
            "expected_revision": profile["campaign_revision"],
            "idempotency_key": "addon-enable",
        }
        enabled = await _call(addon_server, "campaign_rules", enable_arguments)
        assert await _call(addon_server, "campaign_rules", enable_arguments) == enabled
        assert enabled["activation"]["enabled"] is True
        assert len(enabled["effective_ruleset"]["lock"]) == 2
        listed_addons = await _call(
            addon_server,
            "rule_pack_query",
            {
                "view": "addons",
                "payload": {"campaign_id": addon_campaign["id"]},
            },
        )
        assert listed_addons[0]["activation"]["enabled"] is True
        statblock_catalog = await _call(
            addon_server,
            "content_catalog_list",
            {
                "campaign_id": addon_campaign["id"],
                "kind": "statblock",
                "query": "Luminous Sentinel",
            },
        )
        assert len(statblock_catalog) == 1
        statblock = statblock_catalog[0]
        assert statblock["source_citations"]
        assert statblock["application_state"] == "catalog_only"
        assert statblock["selection_requirements"] == {
            "fields": ["source_id", "chunk_ids", "source_statblock_name"],
            "creation_tool": "character_create_from",
            "creation_mode": "statblock",
            "source_statblock_name": "Luminous Sentinel",
            "source_resolution": "source_citations",
            "build_time_actor_card_required": True,
            "normalization_authority": "engine",
        }

        target_server = create_server(_config(tmp_path / "target"))
        target_campaign = await _call(
            target_server,
            "campaign_create",
            {
                "name": "Portable extension target",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        inspected_release = await _call(
            target_server,
            "rule_import",
            {
                "campaign_id": target_campaign["id"],
                "action": "inspect_release",
                "payload": {"release_manifest": release["release_manifest"]},
            },
        )
        assert inspected_release["authority"] == "manifest_only"
        assert inspected_release["components"][0]["local_status"] == ("external_package_required")
        assert inspected_release["auto_install"] is False
        incomplete_manifest = copy.deepcopy(package["payload"]["manifest"])
        incomplete_manifest.pop("resolution_policy")
        incomplete_manifest.pop("resolution_readiness")
        incomplete_package = build_rule_pack(
            portable_id=package["id"],
            version=package["version"],
            system_id=package["system_id"],
            manifest=incomplete_manifest,
            artifacts=package["payload"]["artifacts"],
            mechanics=package["payload"]["mechanics"],
            provenance=package["payload"]["provenance"],
            sources=package["payload"]["sources"],
            metadata=package["metadata"],
            dependencies=package["dependencies"],
        )
        with pytest.raises(Exception, match="build-time-complete"):
            await _call(
                target_server,
                "rule_import",
                {
                    "campaign_id": target_campaign["id"],
                    "action": "import_package",
                    "payload": {"package": incomplete_package},
                    "idempotency_key": "missing-resolution-audit",
                },
            )
        wrong_sources = copy.deepcopy(package["payload"]["sources"])
        wrong_sources[0]["edition"] = "2024"
        wrong_edition = build_rule_pack(
            portable_id=package["id"],
            version=package["version"],
            system_id=package["system_id"],
            manifest=package["payload"]["manifest"],
            artifacts=package["payload"]["artifacts"],
            mechanics=package["payload"]["mechanics"],
            provenance=package["payload"]["provenance"],
            sources=wrong_sources,
            metadata=package["metadata"],
            dependencies=package["dependencies"],
        )
        with pytest.raises(Exception, match="editions not declared"):
            await _call(
                target_server,
                "rule_import",
                {
                    "campaign_id": target_campaign["id"],
                    "action": "import_package",
                    "payload": {"package": wrong_edition},
                    "idempotency_key": "wrong-edition",
                },
            )
        import_arguments = {
            "campaign_id": target_campaign["id"],
            "action": "import_package",
            "payload": {"package": package},
            "idempotency_key": "import-package",
        }
        imported = await _call(target_server, "rule_import", import_arguments)
        replayed = await _call(target_server, "rule_import", import_arguments)

        assert replayed == imported
        assert imported["status"] == "imported"
        assert imported["draft"]["status"] == "validated"
        assert imported["installed"] is False
        assert imported["activated"] is False
        assert imported["sources"][0]["source_id"] != source_id
        target_chunks = await _call(
            target_server,
            "rule_pack_query",
            {
                "view": "source_chunks",
                "payload": {"source_id": imported["sources"][0]["source_id"]},
            },
        )
        assert [item["section_ordinal"] for item in target_chunks] == [0, 1]
        assert {item["content"] for item in target_chunks} == {
            item["content"] for item in portable_chunks
        }
        inspected = await _call(
            target_server,
            "rule_pack_query",
            {
                "view": "inspect",
                "payload": {
                    "pack_id": package["id"],
                    "version": package["version"],
                },
            },
        )
        local_citation = inspected["artifacts"][0]["source_citations"][0]
        assert local_citation["source_id"] == imported["sources"][0]["source_id"]
        assert local_citation["chunk_id"] != chunks[0]["id"]
        local_plan = inspected["artifacts"][0]["resolution_plan"]
        assert compile_resolution_plan(local_plan).fingerprint == local_plan["fingerprint"]
        assert local_plan["citations"][0]["source_ref"]["chunk_id"] == (local_citation["chunk_id"])
        assert local_plan["fingerprint"] != portable_plan["fingerprint"]
        reexported = await _call(
            target_server,
            "rule_pack_query",
            {
                "view": "package",
                "payload": {
                    "campaign_id": target_campaign["id"],
                    "pack_id": package["id"],
                    "version": package["version"],
                    "include_package": True,
                    "metadata": {"distribution": "shareable"},
                },
            },
        )
        assert reexported["package"] == package

        dependent_imported = await _call(
            target_server,
            "rule_import",
            {
                "campaign_id": target_campaign["id"],
                "action": "import_package",
                "payload": {"package": dependent_package},
                "idempotency_key": "import-dependent-package",
            },
        )
        assert dependent_imported["status"] == "imported"
        assert dependent_imported["dependencies"][0]["status"] == "validated"
        for imported_package in (package, dependent_package):
            installed = await _call(
                target_server,
                "rule_pack_change",
                {
                    "action": "install",
                    "pack_id": imported_package["id"],
                    "version": imported_package["version"],
                },
            )
            assert installed["status"] == "installed"
        for index, imported_package in enumerate((package, dependent_package)):
            profile = await _call(
                target_server,
                "campaign_rules",
                {
                    "campaign_id": target_campaign["id"],
                    "action": "get_profile",
                },
            )
            activated = await _call(
                target_server,
                "campaign_rules",
                {
                    "campaign_id": target_campaign["id"],
                    "action": "set_pack",
                    "payload": {
                        "pack_id": imported_package["id"],
                        "version": imported_package["version"],
                    },
                    "expected_revision": profile["campaign_revision"],
                    "idempotency_key": f"activate-portable-{index}",
                },
            )
            assert activated["activation"]["enabled"] is True
        inspected_after_import = await _call(
            target_server,
            "rule_import",
            {
                "campaign_id": target_campaign["id"],
                "action": "inspect_release",
                "payload": {"release_manifest": release["release_manifest"]},
            },
        )
        assert all(
            item["local_status"] == "installed" for item in inspected_after_import["components"]
        )
        assert all(
            item["portable_checksum_status"] == "match"
            for item in inspected_after_import["components"]
        )

    asyncio.run(exercise())
