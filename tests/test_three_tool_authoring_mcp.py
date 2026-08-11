import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
from sagasmith_core.rule_packs import RulePackService

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from sagasmith_dnd_mcp.storage import SagaSmithStorage
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
                "id": job["candidates"][0]["id"],
                "review_status": "pending",
                "note": "The Agent leaves this out of the current Pack.",
            }
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
                        "note": "Finalize the Agent-selected Pack; unselected candidates stay out.",
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


def test_rulebook_finalize_rejects_missing_manifest_without_freezing(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    source = import_root / "rules.md"
    source.write_text(
        "# Optional Rules\n\n## Spark\n\nOne target takes 1 fire damage.\n",
        encoding="utf-8",
    )

    async def exercise() -> None:
        server = create_server(_config(tmp_path, import_root))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Retryable rulebook finalize", "idempotency_key": "campaign"},
        )
        started = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "retryable-rules",
                    "title": "Retryable Rules",
                    "edition": "2014",
                },
                "idempotency_key": "draft-start",
            },
        )
        before = started["job"]
        confirmation = {
            "confirmed": True,
            "note": "Freeze the reviewed candidate set and build the immutable Pack.",
        }
        with pytest.raises(Exception, match="payload.manifest is required"):
            await _call(
                server,
                "rulebook_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "finalize",
                    "payload": {"job_id": before["id"], "confirmation": confirmation},
                    "expected_revision": before["revision"],
                    "idempotency_key": "missing-manifest",
                },
            )
        unchanged = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": before["id"]},
            },
        )
        assert unchanged["job"]["state"] == before["state"]
        assert unchanged["job"]["revision"] == before["revision"]
        assert "review_finalization" not in unchanged["job"]["result"]

        finalized = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "finalize",
                "payload": {
                    "job_id": before["id"],
                    "confirmation": confirmation,
                    "manifest": {
                        "id": "dnd5e.retryable-rules",
                        "version": "1.0.0",
                        "title": "Retryable Rules",
                        "namespace": "dnd5e.retryable-rules",
                        "system_id": "dnd5e",
                        "editions": ["2014"],
                    },
                },
                "expected_revision": before["revision"],
                "idempotency_key": "valid-finalize",
            },
        )
        assert finalized["job"]["state"] == "compiled"
        assert finalized["draft"]["status"] == "validated"

    asyncio.run(exercise())


def test_rulebook_finalize_resumes_after_candidate_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    source = import_root / "rules.md"
    source.write_text(
        "# Optional Rules\n\n## Spark\n\nOne target takes 1 fire damage.\n",
        encoding="utf-8",
    )

    async def exercise() -> None:
        server = create_server(_config(tmp_path, import_root))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Interrupted rulebook finalize", "idempotency_key": "campaign"},
        )
        started = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "interrupted-rules",
                    "title": "Interrupted Rules",
                    "edition": "2014",
                },
                "idempotency_key": "draft-start",
            },
        )
        job = started["job"]
        confirmation = {
            "confirmed": True,
            "note": "Freeze this candidate set once and resume Pack compilation safely.",
        }
        manifest = {
            "id": "dnd5e.interrupted-rules",
            "version": "1.0.0",
            "title": "Interrupted Rules",
            "namespace": "dnd5e.interrupted-rules",
            "system_id": "dnd5e",
            "editions": ["2014"],
        }
        original_save_draft = RulePackService.save_draft
        interrupted = False

        def interrupt_once(self, *args, **kwargs):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise RuntimeError("simulated compiler interruption")
            return original_save_draft(self, *args, **kwargs)

        monkeypatch.setattr(RulePackService, "save_draft", interrupt_once)
        with pytest.raises(Exception, match="simulated compiler interruption"):
            await _call(
                server,
                "rulebook_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "finalize",
                    "payload": {
                        "job_id": job["id"],
                        "confirmation": confirmation,
                        "manifest": manifest,
                    },
                    "expected_revision": job["revision"],
                    "idempotency_key": "interrupted-finalize",
                },
            )
        frozen = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job["id"]},
            },
        )
        assert frozen["job"]["state"] == "reviewed"
        frozen_revision = frozen["job"]["revision"]
        candidate_revision = frozen["job"]["result"]["review_finalization"][
            "candidate_revision"
        ]
        assert candidate_revision == frozen_revision

        finalized = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "finalize",
                "payload": {
                    "job_id": job["id"],
                    "confirmation": confirmation,
                    "manifest": manifest,
                },
                "expected_revision": frozen_revision,
                "idempotency_key": "resumed-finalize",
            },
        )
        assert finalized["job"]["state"] == "compiled"
        assert finalized["draft"]["status"] == "validated"
        assert finalized["job"]["result"]["review_finalization"][
            "candidate_revision"
        ] == candidate_revision
        assert finalized["job"]["result"]["finalized_package"]["artifact"]

    asyncio.run(exercise())


def test_rulebook_finalize_resumes_after_archive_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    source = import_root / "rules.md"
    source.write_text(
        "# Optional Rules\n\n## Spark\n\nOne target takes 1 fire damage.\n",
        encoding="utf-8",
    )

    async def exercise() -> None:
        server = create_server(_config(tmp_path, import_root))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Interrupted rulebook archive", "idempotency_key": "campaign"},
        )
        started = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "interrupted-archive-rules",
                    "title": "Interrupted Archive Rules",
                    "edition": "2014",
                },
                "idempotency_key": "draft-start",
            },
        )
        job = started["job"]
        confirmation = {
            "confirmed": True,
            "note": "Resume the same reviewed Pack after an archive write interruption.",
        }
        manifest = {
            "id": "dnd5e.interrupted-archive-rules",
            "version": "1.0.0",
            "title": "Interrupted Archive Rules",
            "namespace": "dnd5e.interrupted-archive-rules",
            "system_id": "dnd5e",
            "editions": ["2014"],
        }
        original_write_archive = SagaSmithStorage.write_content_archive
        interrupted = False

        def interrupt_once(self, *args, **kwargs):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise RuntimeError("simulated archive interruption")
            return original_write_archive(self, *args, **kwargs)

        monkeypatch.setattr(SagaSmithStorage, "write_content_archive", interrupt_once)
        with pytest.raises(Exception, match="simulated archive interruption"):
            await _call(
                server,
                "rulebook_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "finalize",
                    "payload": {
                        "job_id": job["id"],
                        "confirmation": confirmation,
                        "manifest": manifest,
                    },
                    "expected_revision": job["revision"],
                    "idempotency_key": "interrupted-finalize",
                },
            )
        compiled = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job["id"]},
            },
        )
        assert compiled["job"]["state"] == "compiled"
        compiled_revision = compiled["job"]["revision"]
        assert compiled["job"]["validation"]["draft"]["status"] == "validated"
        assert "finalized_package" not in compiled["job"]["result"]

        finalized = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "finalize",
                "payload": {
                    "job_id": job["id"],
                    "confirmation": confirmation,
                    "manifest": manifest,
                },
                "expected_revision": compiled_revision,
                "idempotency_key": "resumed-finalize",
            },
        )
        assert finalized["job"]["state"] == "compiled"
        assert finalized["draft"]["status"] == "validated"
        assert finalized["job"]["revision"] == compiled_revision + 1
        assert finalized["job"]["result"]["finalized_package"]["artifact"]

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
        assert started["job_id"] == started["job"]["id"]
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
        malformed = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "job_id": started["job"]["id"],
                    "operation": "package",
                    "narrative": {"endings": "The end"},
                },
                "expected_revision": started["job"]["revision"],
                "idempotency_key": "module-malformed-narrative",
            },
        )
        with pytest.raises(
            Exception,
            match="module narrative is missing required fields: dossiers",
        ):
            await _call(
                server,
                "module_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "finalize",
                    "payload": {
                        "job_id": started["job"]["id"],
                        "pack_id": "dnd5e.module.malformed",
                        "confirmation": {
                            "confirmed": True,
                            "note": "This malformed draft must not finalize.",
                        },
                    },
                    "idempotency_key": "reject-malformed-narrative",
                },
            )
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
                    "narrative": {"dossiers": [], "endings": []},
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
                "expected_revision": malformed["job"]["revision"],
                "idempotency_key": "module-package-edit",
            },
        )
        assert edited["job"]["result"]["pack_edit_history"]
        invalid_manifest = deepcopy(edited["pack_draft"]["manifest"])
        invalid_manifest["play_profile"]["pregenerated_characters"]["source_refs"][0][
            "chunk_hash"
        ] = "0" * 64
        invalid_ref = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "job_id": started["job"]["id"],
                    "operation": "package",
                    "manifest": invalid_manifest,
                },
                "expected_revision": edited["job"]["revision"],
                "idempotency_key": "module-invalid-source-ref",
            },
        )
        with pytest.raises(
            Exception,
            match="module source_ref.chunk_hash does not match imported draft evidence",
        ):
            await _call(
                server,
                "module_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "finalize",
                    "payload": {
                        "job_id": started["job"]["id"],
                        "pack_id": "dnd5e.module.invalid-source-ref",
                        "confirmation": {
                            "confirmed": True,
                            "note": "This draft contains a fabricated source reference.",
                        },
                    },
                    "idempotency_key": "reject-invalid-source-ref",
                },
            )
        restored = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "job_id": started["job"]["id"],
                    "operation": "package",
                    "manifest": edited["pack_draft"]["manifest"],
                },
                "expected_revision": invalid_ref["job"]["revision"],
                "idempotency_key": "module-restore-source-ref",
            },
        )
        assert restored["pack_draft"]["manifest"] == edited["pack_draft"]["manifest"]
        finalized = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "finalize",
                "payload": {
                    "job_id": started["job"]["id"],
                    "pack_id": "dnd5e.module.three-tool",
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
        assert "package" not in finalized
        draft = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": started["job"]["id"]},
            },
        )
        assert draft["job"]["result"]["finalized_package"]["artifact"] == finalized[
            "artifact"
        ]
        assert "package" not in draft["job"]["result"]["finalized_package"]
        package_view = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": started["job"]["id"], "view": "package"},
            },
        )
        assert package_view["job"]["state"] == "compiled"
        assert package_view["job"]["finalized_artifact"] == finalized["artifact"]
        assert package_view["finalized_package"]["artifact"] == finalized["artifact"]
        assert "package" not in package_view["finalized_package"]
        with pytest.raises(Exception, match="imported from a finalized Pack artifact"):
            await _call(
                server,
                "content_pack",
                {
                    "action": "activate",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "kind": "module",
                        "module_id": started["module_id"],
                    },
                    "idempotency_key": "reject-draft-activation",
                },
            )
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
        assert inspected["metadata"]["agent_finalization"] == finalized["confirmation"]
        assert inspected["metadata"]["authoring_review"] == {
            "schema_version": 1,
            "draft_kind": "module",
            "draft_revision": restored["job"]["revision"],
            "package_edit_history": restored["job"]["result"]["pack_edit_history"],
        }
        assert inspected["kind"] == "module"
        with pytest.raises(Exception, match="payload.kind is required"):
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


def test_module_get_lists_compact_restart_handles(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    source = import_root / "restart.md"
    source.write_text("# Chapter\n\n## Scene\n\nResume this draft.\n", encoding="utf-8")

    async def exercise() -> None:
        server = create_server(_config(tmp_path, import_root))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Restart handles", "idempotency_key": "campaign"},
        )
        started = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "restart-source",
                    "title": "Restart Source",
                },
                "idempotency_key": "restart-start",
            },
        )

        listed = await _call(
            server,
            "module_draft",
            {"campaign_id": campaign["id"], "action": "get"},
        )
        assert listed["order"] == "newest_first"
        assert listed["jobs"] == [
            {
                "job_id": started["job_id"],
                "state": "imported",
                "resumable": True,
                "artifact": started["job"]["artifact"],
                "artifact_checksum": started["job"]["artifact_checksum"],
                "source_key": "restart-source",
                "title": "Restart Source",
                "module_id": started["module_id"],
                "revision": started["job"]["revision"],
                "created_at": started["job"].get("created_at"),
                "updated_at": started["job"].get("updated_at"),
                "pack_decision_fields": [],
                "statblock_review_count": 0,
                "finalized_artifact": "",
                "finalized_pack_id": "",
            }
        ]
        assert "inspection" not in listed["jobs"][0]
        assert "result" not in listed["jobs"][0]

        package_edit = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "job_id": started["job_id"],
                    "operation": "package",
                    "manifest": {"title": "Restart Source"},
                },
                "expected_revision": started["job"]["revision"],
                "idempotency_key": "restart-package-edit",
            },
        )
        package_view = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": started["job_id"], "view": "package"},
            },
        )
        assert package_view == {
            "job": {
                **listed["jobs"][0],
                "revision": package_edit["job"]["revision"],
                "pack_decision_fields": ["manifest"],
            },
            "pack_draft": {"manifest": {"title": "Restart Source"}},
            "finalized_package": {},
        }
        assert "inspection" not in package_view["job"]
        detailed = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": started["job_id"]},
            },
        )
        assert detailed["job"]["id"] == started["job_id"]
        assert detailed["job"]["inspection"]

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
