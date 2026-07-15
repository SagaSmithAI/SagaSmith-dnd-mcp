import asyncio
from pathlib import Path

import pytest
from sagasmith_core import Database, RuleProfileService
from sagasmith_core.database import sqlite_database_url
from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


def test_rule_pack_authoring_activation_and_explanation(tmp_path: Path) -> None:
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
        with pytest.raises(Exception, match="unsupported D&D core edition"):
            await call(
                server,
                "campaign_create",
                {
                    "name": "Unsupported edition",
                    "edition": "2030",
                    "idempotency_key": "unsupported-edition",
                },
            )
        assert await call(server, "campaign_list", {}) == []
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Rule packs", "idempotency_key": "campaign-rule-packs"},
        )
        profile = await call(
            server,
            "campaign_rule_profile_set",
            {
                "campaign_id": campaign["id"],
                "edition": "2014",
                "expected_revision": campaign["revision"],
                "idempotency_key": "profile-2014",
            },
        )
        assert (
            await call(
                server,
                "campaign_rule_profile_set",
                {
                    "campaign_id": campaign["id"],
                    "edition": "2014",
                    "expected_revision": campaign["revision"],
                    "idempotency_key": "profile-2014",
                },
            )
            == profile
        )
        with pytest.raises(Exception, match="campaign revision conflict"):
            await call(
                server,
                "campaign_rule_profile_set",
                {
                    "campaign_id": campaign["id"],
                    "edition": "2014",
                    "locale": "zh-CN",
                    "expected_revision": campaign["revision"],
                    "idempotency_key": "stale-profile-2014",
                },
            )
        draft = await call(
            server,
            "rule_pack_draft",
            {
                "manifest": {
                    "id": "dnd5e.xgte",
                    "version": "1.0.0",
                    "title": "Xanathar pilot",
                    "namespace": "dnd5e.xgte",
                    "system_id": "dnd5e",
                    "editions": ["2014"],
                    "capabilities": ["activity.after"],
                    "tests": [
                        {
                            "name": "recovers pilot resource",
                            "event": "activity.after",
                            "sheet": {"resources": {"pilot": {"value": 0, "max": 1}}},
                            "expect": [{"path": "resources.pilot.value", "equals": 1}],
                        }
                    ],
                },
                "mechanics": [
                    {
                        "id": "dnd5e.xgte.pilot.recover",
                        "event": "activity.after",
                        "operations": [
                            {
                                "op": "resource.recover",
                                "path": "resources.pilot",
                                "amount": 1,
                            }
                        ],
                        "citations": [{"source": "local:xgte", "section": "Pilot"}],
                    }
                ],
                "artifacts": [
                    {
                        "id": "dnd5e.xgte.feature.pilot",
                        "kind": "feature",
                        "card": {
                            "name": "Pilot Feature",
                            "activation": {"type": "action"},
                            "uses": {"value": 1, "max": 1, "recovers_on": "long_rest"},
                        },
                        "rule_refs": ["local:xgte#pilot"],
                        "mechanic_refs": ["dnd5e.xgte.pilot.recover"],
                    }
                ],
                "provenance": {"source": "local-private-book"},
            },
        )
        assert draft["status"] == "validated"
        test_report = await call(
            server, "rule_pack_test", {"pack_id": "dnd5e.xgte", "version": "1.0.0"}
        )
        assert test_report["passed"] is True
        await call(server, "rule_pack_install", {"pack_id": "dnd5e.xgte", "version": "1.0.0"})
        activated = await call(
            server,
            "campaign_rule_pack_set",
            {
                "campaign_id": campaign["id"],
                "pack_id": "dnd5e.xgte",
                "version": "1.0.0",
                "expected_revision": profile["campaign_revision"],
                "idempotency_key": "activate-xgte",
            },
        )
        assert (
            await call(
                server,
                "campaign_rule_pack_set",
                {
                    "campaign_id": campaign["id"],
                    "pack_id": "dnd5e.xgte",
                    "version": "1.0.0",
                    "expected_revision": profile["campaign_revision"],
                    "idempotency_key": "activate-xgte",
                },
            )
            == activated
        )
        with pytest.raises(Exception, match="does not support campaign edition 2024"):
            await call(
                server,
                "campaign_rule_profile_set",
                {
                    "campaign_id": campaign["id"],
                    "edition": "2024",
                    "expected_revision": activated["campaign_revision"],
                    "idempotency_key": "reject-profile-2024",
                },
            )
        explained = await call(
            server,
            "campaign_rules_explain",
            {"campaign_id": campaign["id"], "event": "activity.after"},
        )
        assert explained["fingerprint"] == activated["effective"]["fingerprint"]
        assert explained["core_pack"]["id"] == "dnd5e.core.2014"
        assert any(
            item["id"] == "dnd5e.core.attack.cover"
            for item in explained["core_boundaries"]
        )
        assert explained["mechanics"][0]["citations"][0]["source"] == "local:xgte"
        sheet = default_character_sheet()
        sheet["resources"]["pilot"] = {"value": 0, "max": 1, "recovers_on": "none"}
        character = await call(
            server,
            "character_create",
            {
                "name": "Pack User",
                "campaign_id": campaign["id"],
                "sheet": sheet,
                "idempotency_key": "pack-user",
            },
        )
        updated = await call(
            server,
            "character_rule_artifact_add",
            {
                "character_id": character["id"],
                "pack_id": "dnd5e.xgte",
                "version": "1.0.0",
                "artifact_id": "dnd5e.xgte.feature.pilot",
                "expected_revision": character["revision"],
                "idempotency_key": "add-pilot-feature",
            },
        )
        assert updated["sheet"]["content"]["features"][0]["pack_id"] == "dnd5e.xgte"
        settled = await call(
            server,
            "character_use_activity",
            {
                "character_id": character["id"],
                "activity_id": "dnd5e.xgte.feature.pilot",
                "expected_revision": updated["revision"],
                "idempotency_key": "use-pilot-feature",
            },
        )
        assert settled["status"] == "committed"
        receipts = await call(
            server,
            "campaign_rule_receipts",
            {"campaign_id": campaign["id"]},
        )
        assert {item["mechanic_id"] for item in receipts} >= {
            "dnd5e.core.activity.resource_accounting",
            "dnd5e.xgte.pilot.recover",
        }
        assert all(item["mutation_group_id"] for item in receipts)
        assert all(item["ruleset_fingerprint"] == explained["fingerprint"] for item in receipts)

        rejected = await call(
            server,
            "rule_pack_draft",
            {
                "manifest": {
                    "id": "dnd5e.unsafe",
                    "version": "1.0.0",
                    "system_id": "dnd5e",
                    "editions": ["2014"],
                },
                "mechanics": [
                    {
                        "id": "dnd5e.unsafe.eval",
                        "event": "rest.after",
                        "operations": [{"op": "python.eval"}],
                        "citations": [{"source": "local:test", "section": "Unsafe"}],
                    }
                ],
            },
        )
        assert rejected["status"] == "rejected"

    asyncio.run(exercise())


def test_rulebook_import_source_bound_pack_and_noncombat_settlement(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    rulebook = import_root / "xanathar-pilot.md"
    rulebook.write_text(
        "# Dungeon Master's Tools\n"
        "## Tool Proficiencies\n"
        "### Tools and Skills Together\n"
        "When both proficiencies apply, use the optional synergy procedure.\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        rule_import_roots=(import_root,),
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Imported rules", "idempotency_key": "import-campaign"},
        )
        with pytest.raises(Exception, match="outside configured import roots"):
            await call(
                server,
                "rule_document_stage",
                {"campaign_id": campaign["id"], "source_path": str(outside)},
            )
        staged = await call(
            server,
            "rule_document_stage",
            {"campaign_id": campaign["id"], "source_path": str(rulebook)},
        )
        inspection = await call(
            server,
            "rule_document_inspect",
            {"campaign_id": campaign["id"], "artifact": staged["artifact"]},
        )
        assert inspection["sections"] == 3
        imported = await call(
            server,
            "rule_document_import",
            {
                "campaign_id": campaign["id"],
                "artifact": staged["artifact"],
                "source_key": "xgte-user",
                "title": "Xanathar User Import",
                "edition": "2014",
                "publication_id": "xgte",
                "idempotency_key": "import-xgte",
            },
        )
        replayed = await call(
            server,
            "rule_document_import",
            {
                "campaign_id": campaign["id"],
                "artifact": staged["artifact"],
                "source_key": "xgte-user",
                "title": "Xanathar User Import",
                "edition": "2014",
                "publication_id": "xgte",
                "idempotency_key": "import-xgte",
            },
        )
        assert replayed == imported
        hits = await call(
            server,
            "rule_search",
            {"query": "Tools and Skills Together", "edition": "2014", "top_k": 1},
        )
        chunk_id = hits[0]["id"]
        draft = await call(
            server,
            "rule_pack_draft_from_source",
            {
                "source_id": imported["source_id"],
                "manifest": {
                    "id": "dnd5e.xgte.tool_synergy",
                    "version": "1.0.0",
                    "title": "Tool Synergy",
                    "namespace": "dnd5e.xgte.tool_synergy",
                    "system_id": "dnd5e",
                    "editions": ["2014"],
                    "capabilities": ["check.before"],
                    "tests": [
                        {
                            "name": "both proficiencies activate synergy",
                            "event": "check.before",
                            "facts": {
                                "skill_proficiency_applies": True,
                                "tool_proficiency_applies": True,
                            },
                            "expect": [],
                        }
                    ],
                },
                "mechanics": [
                    {
                        "id": "dnd5e.xgte.tool_synergy.advantage",
                        "event": "check.before",
                        "predicates": [
                            {
                                "kind": "fact_equals",
                                "key": "skill_proficiency_applies",
                                "value": True,
                            },
                            {
                                "kind": "fact_equals",
                                "key": "tool_proficiency_applies",
                                "value": True,
                            },
                        ],
                        "operations": [{"op": "advantage.add"}],
                        "citations": [{"chunk_id": chunk_id}],
                    }
                ],
            },
        )
        assert draft["status"] == "validated"
        citation = draft["mechanics"][0]["citations"][0]
        assert citation["source_id"] == imported["source_id"]
        assert citation["source_checksum"] == staged["checksum"]
        with pytest.raises(Exception):
            await call(
                server,
                "rule_pack_draft_from_source",
                {
                    "source_id": "not-the-source",
                    "manifest": {},
                    "mechanics": [],
                },
            )
        await call(
            server,
            "rule_pack_install",
            {"pack_id": "dnd5e.xgte.tool_synergy", "version": "1.0.0"},
        )
        profile = await call(
            server,
            "campaign_rule_profile_set",
            {
                "campaign_id": campaign["id"],
                "edition": "2014",
                "expected_revision": campaign["revision"],
                "idempotency_key": "xgte-profile",
            },
        )
        activated = await call(
            server,
            "campaign_rule_pack_set",
            {
                "campaign_id": campaign["id"],
                "pack_id": "dnd5e.xgte.tool_synergy",
                "version": "1.0.0",
                "expected_revision": profile["campaign_revision"],
                "idempotency_key": "xgte-activate",
            },
        )
        character = await call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Artificer",
                "sheet": default_character_sheet(),
                "idempotency_key": "xgte-character",
            },
        )
        current = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
        settled = await call(
            server,
            "character_check",
            {
                "campaign_id": campaign["id"],
                "actor_id": character["id"],
                "kind": "check",
                "ability": "intelligence",
                "dc": 12,
                "rule_facts": {
                    "skill_proficiency_applies": True,
                    "tool_proficiency_applies": True,
                },
                "expected_revision": current["revision"],
                "idempotency_key": "xgte-tool-check",
            },
        )
        assert len(settled["rolls"]) == 2
        receipts = await call(
            server,
            "campaign_rule_receipts",
            {"campaign_id": campaign["id"]},
        )
        extension = next(
            item
            for item in receipts
            if item["mechanic_id"] == "dnd5e.xgte.tool_synergy.advantage"
        )
        assert extension["receipt"]["citations"][0]["chunk_id"] == chunk_id
        assert activated["effective"]["lock"][0]["pack_id"] == "dnd5e.xgte.tool_synergy"

    asyncio.run(exercise())


def test_legacy_campaign_without_core_lock_fails_closed(tmp_path: Path) -> None:
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
            {"name": "Legacy core lock", "idempotency_key": "legacy-core-lock"},
        )
        database = Database(sqlite_database_url(config.database_path))
        try:
            RuleProfileService(database).set(
                campaign["id"], edition="2014", options={}
            )
        finally:
            database.dispose()
        with pytest.raises(Exception, match="no locked built-in core rule pack"):
            await call(
                server,
                "campaign_rules_explain",
                {"campaign_id": campaign["id"]},
            )
        diagnostic = await call(
            server,
            "campaign_rule_profile_get",
            {"campaign_id": campaign["id"]},
        )
        assert diagnostic["effective"] is None
        assert "no locked built-in core rule pack" in diagnostic["effective_error"]

    asyncio.run(exercise())


def test_snapshot_and_branch_checkout_reject_unavailable_core_lock(
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
            {"name": "Legacy snapshot", "idempotency_key": "legacy-snapshot"},
        )
        database = Database(sqlite_database_url(config.database_path))
        try:
            RuleProfileService(database).set(
                campaign["id"], edition="2024", options={}
            )
        finally:
            database.dispose()

        current = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
        branch = (await call(server, "branch_list", {"campaign_id": campaign["id"]}))[0]
        legacy_snapshot = await call(
            server,
            "snapshot_create",
            {
                "campaign_id": campaign["id"],
                "label": "missing core lock",
                "expected_revision": current["revision"],
                "expected_head_snapshot_id": "",
                "idempotency_key": "legacy-snapshot-create",
            },
        )
        repaired = await call(
            server,
            "campaign_rule_profile_set",
            {
                "campaign_id": campaign["id"],
                "edition": "2024",
                "expected_revision": current["revision"],
                "idempotency_key": "repair-core-lock",
            },
        )
        with pytest.raises(Exception, match="cannot be restored without explicit conversion"):
            await call(
                server,
                "branch_create",
                {
                    "campaign_id": campaign["id"],
                    "name": "legacy-direct-checkout",
                    "from_snapshot_id": legacy_snapshot["id"],
                    "checkout": True,
                    "expected_revision": repaired["campaign_revision"],
                    "expected_branch_id": branch["id"],
                    "idempotency_key": "reject-legacy-create-checkout",
                },
            )
        assert len(await call(server, "branch_list", {"campaign_id": campaign["id"]})) == 1
        legacy_branch = await call(
            server,
            "branch_create",
            {
                "campaign_id": campaign["id"],
                "name": "legacy-core",
                "from_snapshot_id": legacy_snapshot["id"],
                "expected_revision": repaired["campaign_revision"],
                "expected_branch_id": branch["id"],
                "idempotency_key": "legacy-core-branch",
            },
        )

        with pytest.raises(Exception, match="cannot be restored without explicit conversion"):
            await call(
                server,
                "snapshot_restore",
                {
                    "campaign_id": campaign["id"],
                    "slot": legacy_snapshot["slot"],
                    "expected_revision": repaired["campaign_revision"],
                    "expected_branch_id": branch["id"],
                    "idempotency_key": "reject-legacy-restore",
                },
            )
        with pytest.raises(Exception, match="cannot be restored without explicit conversion"):
            await call(
                server,
                "branch_checkout",
                {
                    "campaign_id": campaign["id"],
                    "branch_id": legacy_branch["id"],
                    "expected_revision": repaired["campaign_revision"],
                    "expected_branch_id": branch["id"],
                    "idempotency_key": "reject-legacy-checkout",
                },
            )
        after = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
        current_branch = (
            await call(server, "branch_list", {"campaign_id": campaign["id"]})
        )
        assert after["revision"] == repaired["campaign_revision"]
        assert next(item for item in current_branch if item["id"] == branch["id"])[
            "is_current"
        ] is True

    asyncio.run(exercise())
