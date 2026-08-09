import asyncio
from pathlib import Path

import pytest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from tests.authoring_helpers import finalize_and_activate_module


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def _config(tmp_path: Path, import_root: Path) -> McpConfig:
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
        rule_import_roots=(import_root,),
        module_import_roots=(import_root,),
    )


def test_only_three_public_authoring_facades_are_registered(tmp_path: Path) -> None:
    server = create_server(_config(tmp_path, tmp_path))
    names = {tool.name for tool in server._tool_manager.list_tools()}

    assert {"rulebook_draft", "module_draft", "content_pack"} <= names
    assert (
        not {
            "import_query",
            "rule_import",
            "module_import",
            "module_review",
            "rule_pack_compile",
            "rule_pack_query",
            "rule_pack_change",
        }
        & names
    )


def test_rulebook_start_edit_finalize_builds_an_immutable_pack(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    source = import_root / "rules.md"
    source.write_text(
        "# Optional Spells\n\n## Spark\n\n"
        "1st-level evocation spell\nCasting Time: 1 action\n"
        "One target takes 1d6 fire damage.\n",
        encoding="utf-8",
    )

    async def exercise() -> None:
        server = create_server(_config(tmp_path, import_root))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Three-tool rules", "idempotency_key": "campaign"},
        )
        started = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "three-tool-rules",
                    "title": "Three Tool Rules",
                    "edition": "2014",
                },
                "idempotency_key": "draft-start",
            },
        )
        assert started["status"] == "editing"
        job = started["job"]
        assert job["state"] == "review_required"
        decisions = [
            {
                "id": candidate["id"],
                "review_status": "rejected",
                "reason": "The test intentionally excludes this candidate.",
            }
            for candidate in job["candidates"]
        ]
        edited = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "job_id": job["id"],
                    "operation": "candidates",
                    "decisions": decisions,
                },
                "idempotency_key": "draft-edit",
            },
        )
        finalized = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "finalize",
                "payload": {
                    "job_id": job["id"],
                    "confirmation": {
                        "confirmed": True,
                        "note": "All mechanically extracted candidates were explicitly reviewed.",
                    },
                    "manifest": {
                        "id": "dnd5e.three-tool-rules",
                        "version": "1.0.0",
                        "title": "Three Tool Rules",
                        "namespace": "dnd5e.three-tool-rules",
                        "system_id": "dnd5e",
                        "editions": ["2014"],
                    },
                    "include_package": True,
                },
                "expected_revision": edited["job"]["revision"],
                "idempotency_key": "draft-finalize",
            },
        )
        assert finalized["job"]["state"] == "compiled"
        assert finalized["draft"]["status"] == "validated"
        assert finalized["stored"]["status"] == "stored"
        assert "installed" not in finalized
        assert finalized["confirmation"]["reviewer"] == "system:local"
        authoring_review = finalized["package"]["metadata"]["authoring_review"]
        assert authoring_review["draft_kind"] == "rulebook"
        assert authoring_review["candidate_set_fingerprint"]
        assert [item["id"] for item in authoring_review["candidate_decisions"]] == [
            item["id"] for item in job["candidates"]
        ]
        assert {
            item["disposition"] for item in authoring_review["candidate_decisions"]
        } == {"exclude"}

    asyncio.run(exercise())


def test_module_start_finalize_writes_a_finalized_module_pack(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    source = import_root / "module.md"
    source.write_text(
        "# Chapter One\n\n## Arrival\n\n#### A1. Courtyard\n30 by 20 feet\n",
        encoding="utf-8",
    )

    async def exercise() -> None:
        server = create_server(_config(tmp_path, import_root))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Three-tool module", "idempotency_key": "campaign"},
        )
        started = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "three-tool-module",
                    "title": "Three Tool Module",
                },
                "idempotency_key": "module-start",
            },
        )
        assert started["job"]["state"] == "imported"
        with pytest.raises(Exception, match=r"unsupported module_draft\(finalize\).*readiness"):
            await _call(
                server,
                "module_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "finalize",
                    "payload": {
                        "job_id": started["job"]["id"],
                        "pack_id": "dnd5e.module.old-readiness",
                        "confirmation": {
                            "confirmed": True,
                            "note": "This request must fail before finalization.",
                        },
                        "readiness": {},
                    },
                    "idempotency_key": "reject-caller-readiness",
                },
            )
        with pytest.raises(Exception, match="explicitly confirm"):
            await _call(
                server,
                "module_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "finalize",
                    "payload": {
                        "job_id": started["job"]["id"],
                        "pack_id": "dnd5e.module.incomplete",
                        "confirmation": {
                            "confirmed": False,
                            "note": "The Agent has not completed review.",
                        },
                    },
                    "idempotency_key": "reject-incomplete-finalize",
                },
            )
        evidence_chunks = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "evidence",
                "payload": {
                    "job_id": started["job"]["id"],
                    "kind": "chunks",
                    "limit": 1,
                },
            },
        )
        source_ref = {
            "source_key": "three-tool-module",
            "page": None,
            "chunk_hash": evidence_chunks[0]["content_hash"],
            "note": "Agent-reviewed source fixture.",
        }
        edited = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "job_id": started["job"]["id"],
                    "operation": "package",
                    "note": "All publication dimensions were reviewed.",
                    "manifest": {
                        "title": "Three Tool Module",
                        "classification": "adventure",
                        "compatibility": {
                            "editions": ["2014"],
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
                },
                "expected_revision": started["job"]["revision"],
                "idempotency_key": "module-package-edit",
            },
        )
        assert edited["job"]["result"]["pack_edit_history"]
        finalized = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "finalize",
                "payload": {
                    "job_id": started["job"]["id"],
                    "pack_id": "dnd5e.module.three-tool",
                    "include_package": True,
                    "confirmation": {
                        "confirmed": True,
                        "note": "The Agent reviewed the complete module fixture.",
                    },
                },
                "idempotency_key": "module-finalize",
            },
        )
        assert finalized["job"]["state"] == "compiled"
        assert finalized["confirmation"]["reviewer"] == "system:local"
        assert finalized["package"]["metadata"]["agent_finalization"] == finalized["confirmation"]
        assert finalized["package"]["metadata"]["authoring_review"] == {
            "schema_version": 1,
            "draft_kind": "module",
            "draft_revision": edited["job"]["revision"],
            "package_edit_history": edited["job"]["result"]["pack_edit_history"],
        }
        assert finalized["package"]["kind"] == "module"
        inspected = await _call(
            server,
            "content_pack",
            {
                "action": "get",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "module",
                    "artifact": finalized["artifact"],
                },
            },
        )
        assert inspected["id"] == "dnd5e.module.three-tool"
        with pytest.raises(Exception, match="exact variant"):
            await _call(
                server,
                "content_pack",
                {
                    "action": "get",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "artifact": finalized["artifact"],
                    },
                },
            )
        with pytest.raises(Exception, match="does not match archive kind module"):
            await _call(
                server,
                "content_pack",
                {
                    "action": "import",
                    "payload": {
                        "kind": "addon",
                        "campaign_id": campaign["id"],
                        "artifact": finalized["artifact"],
                    },
                    "idempotency_key": "reject-wrong-archive-kind",
                },
            )

    asyncio.run(exercise())


def test_content_pack_activation_applies_agent_scene_key_remaps(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    source = import_root / "revision.md"
    source.write_text("# Chapter\n\n## Old Cave\n\nThe party enters.\n", encoding="utf-8")

    async def exercise() -> None:
        server = create_server(_config(tmp_path, import_root))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Pack remap", "edition": "2014", "idempotency_key": "campaign"},
        )
        first = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "revision-source",
                    "title": "Revision One",
                },
                "idempotency_key": "revision-one-start",
            },
        )
        first_release = await finalize_and_activate_module(
            _call,
            server,
            campaign["id"],
            first,
            source_key="revision-source",
            title="Revision One",
            portable_id="dnd5e.module.revision-source",
            edition="2014",
            request_key="revision-one",
        )
        old_module_id = first_release["activated"]["activation"]["module_id"]
        old_index = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "index",
                "payload": {"module_id": old_module_id},
            },
        )
        old_scene_id = old_index[0]["scene_id"]
        await _call(
            server,
            "module_set_progress",
            {
                "campaign_id": campaign["id"],
                "scene_id": old_scene_id,
                "status": "active",
                "expected_state_version": 0,
                "idempotency_key": "old-scene-progress",
            },
        )

        source.write_text("# Chapter\n\n## New Cave\n\nThe party enters.\n", encoding="utf-8")
        second = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "revision-source",
                    "title": "Revision Two",
                },
                "idempotency_key": "revision-two-start",
            },
        )
        draft_index = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "index",
                "payload": {"module_id": second["module_id"]},
            },
        )
        released = await finalize_and_activate_module(
            _call,
            server,
            campaign["id"],
            second,
            source_key="revision-source",
            title="Revision Two",
            portable_id="dnd5e.module.revision-source",
            edition="2014",
            request_key="revision-two",
            progress_remaps=[
                {
                    "from_scene_id": old_scene_id,
                    "to_scene_key": draft_index[0]["stable_key"],
                    "reason": "The Agent reviewed the renamed scene and matched its content.",
                }
            ],
        )
        activation = released["activated"]["activation"]
        assert activation["progress_migrations"][0]["from_scene_id"] == old_scene_id
        assert activation["progress_migrations"][0]["mode"] == "dm_ruling"
        assert activation["progress_remap_rulings"][0]["resolver"] == "agent"
        assert (
            activation["progress_remap_rulings"][0]["to_scene_key"] == draft_index[0]["stable_key"]
        )

    asyncio.run(exercise())
