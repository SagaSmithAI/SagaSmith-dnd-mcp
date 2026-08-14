import asyncio
import os
from pathlib import Path

import pytest
from sagasmith_dnd.module_profile import DndModuleProfile

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from sagasmith_dnd_mcp.skills import SkillCatalog
from sagasmith_dnd_mcp.storage import SagaSmithStorage
from sagasmith_dnd_mcp.tool_profiles import campaign_phase, profile_catalog


def test_config_owns_local_storage(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
    )

    config.prepare()

    assert config.database_path.parent.is_dir()
    assert config.chroma_path.is_dir()
    assert config.modules_dir.is_dir()
    assert config.rulebooks_dir.is_dir()
    assert config.normalized_rulebooks_dir.is_dir()
    assert config.normalized_modules_dir.is_dir()


def test_config_can_share_only_content_addressed_document_cache(tmp_path: Path) -> None:
    shared = tmp_path / "shared-documents"
    first = McpConfig(
        home=tmp_path / "first-home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        document_cache_dir=shared,
    )
    second = McpConfig(
        home=tmp_path / "second-home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        document_cache_dir=shared,
    )

    first.prepare()
    second.prepare()

    assert first.normalized_rulebooks_dir == second.normalized_rulebooks_dir
    assert first.normalized_modules_dir == second.normalized_modules_dir
    assert first.database_path != second.database_path
    assert first.content_packages_dir != second.content_packages_dir


def test_storage_accepts_only_the_unified_content_archive_extension(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    retired = import_root / "retired.sagasmith-module"
    retired.write_bytes(b"not a unified content archive")
    storage = SagaSmithStorage(
        McpConfig(
            home=tmp_path / "home",
            database_url=None,
            chroma_url=None,
            chroma_path_override=None,
            dnd_skills_dir=tmp_path / "dnd",
            modulegen_skills_dir=tmp_path / "modulegen",
            rule_import_roots=(import_root,),
            module_import_roots=(import_root,),
        )
    )

    with pytest.raises(LookupError):
        storage.read_content_archive(source_path=retired)
    assert not hasattr(storage, "read_portable_package")
    assert not hasattr(storage, "read_module_archive")
    assert not hasattr(storage, "write_portable_package")
    assert not hasattr(storage, "write_module_archive")


def test_environment_config_has_separate_rule_and_module_import_roots(monkeypatch) -> None:
    monkeypatch.setenv(
        "SAGASMITH_DND_MCP_RULE_IMPORT_ROOTS", os.pathsep.join(("rules-a", "rules-b"))
    )
    monkeypatch.setenv(
        "SAGASMITH_DND_MCP_MODULE_IMPORT_ROOTS", os.pathsep.join(("modules-a", "modules-b"))
    )
    monkeypatch.setenv("SAGASMITH_DND_MCP_MODULE_OCR", "0")
    monkeypatch.setenv("SAGASMITH_DND_MCP_MODULE_OCR_SCALE", "1.5")
    monkeypatch.setenv("SAGASMITH_DND_MCP_RULE_OCR_MODEL", "medium")
    monkeypatch.setenv("SAGASMITH_DND_MCP_BOUND_PRINCIPAL_ID", "trusted-user")
    monkeypatch.setenv("SAGASMITH_DOCUMENT_CACHE_DIR", "shared-documents")

    config = McpConfig.from_environment()

    assert [path.name for path in config.rule_import_roots] == ["rules-a", "rules-b"]
    assert [path.name for path in config.module_import_roots] == ["modules-a", "modules-b"]
    assert config.module_ocr_enabled is False
    assert config.module_ocr_scale == 1.5
    assert config.rule_ocr_model == "medium"
    assert config.module_ocr_model == "medium"
    assert config.bound_principal_id == "trusted-user"
    assert config.document_cache_dir is not None
    assert config.document_cache_dir.name == "shared-documents"
    assert config.ocr_page_cache_dir == (config.document_cache_dir / "ocr-page-cache")


def test_default_rule_import_roots_include_the_dnd_skill_corpus(monkeypatch) -> None:
    monkeypatch.delenv("SAGASMITH_DND_MCP_RULE_IMPORT_ROOTS", raising=False)
    monkeypatch.delenv("SAGASMITH_DND_MCP_RULE_OCR_MODEL", raising=False)
    monkeypatch.delenv("SAGASMITH_DND_MCP_MODULE_OCR_MODEL", raising=False)

    config = McpConfig.from_environment()

    assert config.rule_ocr_model == "medium"
    assert config.module_ocr_model == "medium"
    assert config.rule_import_roots[0].name == "DnD-Books"
    assert (
        config.rule_import_roots[1]
        == (config.dnd_skills_dir / "full" / "skills" / "dnd-dm" / "srd").resolve()
    )


def test_bundled_rule_seed_reports_complete_multilingual_corpus(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[2]
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=workspace / "SagaSmith-dnd-skills",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=True,
    )

    async def inspect_seed() -> None:
        server = create_server(config)
        _, status = await server.call_tool("rule_seed_status", {})
        coverage = status["coverage"]

        assert coverage["complete"] is True
        assert coverage["expected_sources"] == 42
        assert coverage["indexed_sources"] == 42
        assert coverage["corpus"]["files"] == 2032
        assert coverage["corpus"]["corpora"]["2014:zh"]["files"] == 991
        source_keys = {item["source_key"] for item in status["sources"]}
        assert "bundled:srd2014:en:10-monsters" in source_keys
        assert "bundled:srd2014:zh:monsters-alt" in source_keys
        assert "bundled:srd2024:en:dnd5esrd-333-364" in source_keys

        _, campaign = await server.call_tool(
            "campaign_create",
            {
                "name": "Bundled search",
                "edition": "2014",
                "locale": "en",
                "idempotency_key": "bundled-search-campaign",
            },
        )
        _, filtered = await server.call_tool(
            "rule_seed_status",
            {
                "campaign_id": campaign["id"],
                "query": "10-monsters",
                "limit": 5,
            },
        )
        assert filtered["edition"] == "2014"
        assert filtered["source_count"] == 1
        assert filtered["sources"][0]["source_key"] == "bundled:srd2014:en:10-monsters"

        _, response = await server.call_tool(
            "rule_search",
            {
                "campaign_id": campaign["id"],
                "query": "Tarrasque",
                "filters": {"edition": "2014", "locale": "en"},
                "top_k": 3,
            },
        )
        _, response_with_empty_optional_filters = await server.call_tool(
            "rule_search",
            {
                "campaign_id": campaign["id"],
                "query": "Tarrasque",
                "filters": {"edition": "2014", "locale": "en"},
                "top_k": 3,
            },
        )
        monster_source_id = next(
            item["id"]
            for item in status["sources"]
            if item["source_key"] == "bundled:srd2014:en:10-monsters"
        )
        assert any(item["source_id"] == monster_source_id for item in response["result"])
        assert response_with_empty_optional_filters["result"] == response["result"]

        _, zh_campaign = await server.call_tool(
            "campaign_create",
            {
                "name": "Bundled cross-locale search",
                "edition": "2014",
                "locale": "zh",
                "idempotency_key": "bundled-cross-locale-campaign",
            },
        )
        _, default_zh = await server.call_tool(
            "rule_search",
            {
                "campaign_id": zh_campaign["id"],
                "query": "Swarm of Rats",
                "top_k": 8,
            },
        )
        assert default_zh["result"]
        assert {
            item["metadata"]["locale"] for item in default_zh["result"]
        } == {"zh"}

        _, explicit_en = await server.call_tool(
            "rule_search",
            {
                "campaign_id": zh_campaign["id"],
                "query": "Swarm of Rats",
                "filters": {"edition": "2014", "locale": "en"},
                "top_k": 30,
            },
        )
        assert explicit_en["result"]
        assert {
            item["metadata"]["locale"] for item in explicit_en["result"]
        } == {"en"}
        rat_hits = [
            item
            for item in explicit_en["result"]
            if any("Swarm of Rats" in value for value in item["heading_path"])
        ]
        single_card_root = next(
            item["heading_path"][0]
            for item in rat_hits
            if item["heading_path"][0].startswith("Bundled source file:")
        )
        rat_chunks = [
            item for item in rat_hits if item["heading_path"][0] == single_card_root
        ]
        rat_source_id = rat_chunks[0]["source_id"]
        assert len(rat_chunks) >= 2
        assert {item["source_id"] for item in rat_chunks} == {rat_source_id}

        _, expanded_rat = await server.call_tool(
            "rule_expand",
            {
                "campaign_id": zh_campaign["id"],
                "chunk_id": rat_chunks[0]["id"],
            },
        )
        assert expanded_rat["source"]["locale"] == "en"

        _, created_rat = await server.call_tool(
            "character_create_from",
            {
                "mode": "statblock",
                "payload": {
                    "campaign_id": zh_campaign["id"],
                    "name": "Cross-locale Rat Swarm",
                    "source_id": rat_source_id,
                    "chunk_ids": [item["id"] for item in rat_chunks],
                    "source_statblock_name": "Swarm of Rats",
                },
                "idempotency_key": "create-cross-locale-rat-swarm",
            },
        )
        created_rat_result = created_rat["result"]
        assert created_rat_result["statblock"]["source_identity"] == "Swarm of Rats"
        assert created_rat_result["source"]["id"] == rat_source_id

    asyncio.run(inspect_seed())


def test_skill_catalog_reads_both_repositories(tmp_path: Path) -> None:
    dnd = tmp_path / "dnd"
    modulegen = tmp_path / "modulegen"
    (dnd / "full" / "skills" / "dnd-dm").mkdir(parents=True)
    modulegen.mkdir()
    (dnd / "full" / "skills" / "dnd-dm" / "SKILL.md").write_text("# D&D DM\n", encoding="utf-8")
    (modulegen / "SKILL.md").write_text("# Module Generator\n", encoding="utf-8")
    shadow = modulegen / ".agents" / "skills" / "modulegen"
    shadow.mkdir(parents=True)
    (shadow / "SKILL.md").write_text("# Stale Shadow\n", encoding="utf-8")
    catalog = SkillCatalog(dnd_root=dnd, modulegen_root=modulegen)

    assert [item.id for item in catalog.list()] == ["dnd.full.skills.dnd-dm", "modulegen.root"]
    assert catalog.read("modulegen.root") == "# Module Generator\n"
    assert all(len(item.checksum) == 64 for item in catalog.list())
    assert catalog.manifest() == [
        {"id": item.id, "source": item.source, "checksum": item.checksum} for item in catalog.list()
    ]


def test_skill_catalog_exposes_references_and_templates_as_assets(tmp_path: Path) -> None:
    dnd = tmp_path / "dnd"
    modulegen = tmp_path / "modulegen"
    (dnd / "full" / "references").mkdir(parents=True)
    modulegen.mkdir()
    (dnd / "full" / "references" / "workflow.md").write_text("workflow", encoding="utf-8")
    (dnd / "full" / "examples").mkdir()
    (dnd / "full" / "examples" / "rule-pack.template.json").write_text("{}", encoding="utf-8")
    (modulegen / "template.md").write_text("template", encoding="utf-8")
    catalog = SkillCatalog(dnd_root=dnd, modulegen_root=modulegen)

    assert [asset.id for asset in catalog.assets()] == [
        "dnd:full/examples/rule-pack.template.json",
        "dnd:full/references/workflow.md",
        "modulegen:template.md",
    ]
    assert catalog.read_asset("dnd:full/references/workflow.md") == "workflow"
    assert all(len(item.checksum) == 64 for item in catalog.assets())
    resource_id = catalog.resource_id("dnd:full/references/workflow.md")
    assert catalog.read_resource_asset(resource_id) == "workflow"


def test_skill_catalog_supports_bounded_outline_section_and_search(tmp_path: Path) -> None:
    dnd = tmp_path / "dnd"
    modulegen = tmp_path / "modulegen"
    dnd.mkdir()
    modulegen.mkdir()
    (dnd / "SKILL.md").write_text(
        "# D&D\n\n## Startup\n\nOpen an exposure.\n\n## Turn Loop\n\nRead exact module evidence.\n",
        encoding="utf-8",
    )
    catalog = SkillCatalog(dnd_root=dnd, modulegen_root=modulegen)

    outline = catalog.outline(kind="skill", identifier="dnd.root")
    assert [heading["title"] for heading in outline["headings"]] == [
        "D&D",
        "Startup",
        "Turn Loop",
    ]
    section = catalog.section(
        kind="skill",
        identifier="dnd.root",
        heading="Startup",
        max_chars=512,
    )
    assert section["content"] == "## Startup\n\nOpen an exposure.\n"
    search = catalog.search(
        kind="skill",
        identifier="dnd.root",
        query="module evidence",
    )
    assert search["matches"][0]["heading"] == "Turn Loop"
    assert "exact module evidence" in search["matches"][0]["excerpt"]


def test_skill_catalog_reuses_indexes_until_an_explicit_refresh(
    tmp_path: Path,
) -> None:
    dnd = tmp_path / "dnd"
    modulegen = tmp_path / "modulegen"
    dnd.mkdir()
    modulegen.mkdir()
    (dnd / "SKILL.md").write_text("# D&D\n", encoding="utf-8")
    references = dnd / "references"
    references.mkdir()
    (references / "one.md").write_text("# One\n", encoding="utf-8")
    catalog = SkillCatalog(dnd_root=dnd, modulegen_root=modulegen)

    assert [item.id for item in catalog.list()] == ["dnd.root"]
    assert [item.id for item in catalog.assets()] == ["dnd:references/one.md"]
    child = dnd / "skills" / "child"
    child.mkdir(parents=True)
    (child / "SKILL.md").write_text("# Child\n", encoding="utf-8")
    (references / "two.md").write_text("# Two\n", encoding="utf-8")

    assert [item.id for item in catalog.list()] == ["dnd.root"]
    assert [item.id for item in catalog.assets()] == ["dnd:references/one.md"]
    catalog.refresh()
    assert [item.id for item in catalog.list()] == [
        "dnd.root",
        "dnd.skills.child",
    ]
    assert [item.id for item in catalog.assets()] == [
        "dnd:references/one.md",
        "dnd:references/two.md",
    ]
    with pytest.raises(LookupError, match="unknown skill asset"):
        SkillCatalog(dnd_root=dnd, modulegen_root=modulegen).get_asset("dnd:../outside.md")


def test_character_writes_store_raw_sheet_and_return_derived_view(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
    )

    async def exercise_server() -> None:
        server = create_server(config)
        _, campaign = await server.call_tool(
            "campaign_create",
            {"name": "Test campaign", "idempotency_key": "create-test-campaign"},
        )
        _, character = await server.call_tool(
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"name": "Aria", "campaign_id": campaign["id"]},
                "principal_id": "system:local",
                "idempotency_key": "create-aria",
            },
        )
        character = character["result"]
        _, updated = await server.call_tool(
            "wallet_change",
            {
                "owner": "character",
                "action": "adjust",
                "owner_id": character["id"],
                "denomination": "gp",
                "amount": 25,
                "payload": {},
                "principal_id": "system:local",
                "expected_revision": character["revision"],
                "idempotency_key": "wallet-test-1",
            },
        )
        updated = updated["result"]
        _, replayed = await server.call_tool(
            "wallet_change",
            {
                "owner": "character",
                "action": "adjust",
                "owner_id": character["id"],
                "denomination": "gp",
                "amount": 25,
                "payload": {},
                "principal_id": "system:local",
                "expected_revision": character["revision"],
                "idempotency_key": "wallet-test-1",
            },
        )
        replayed = replayed["result"]

        assert updated["sheet"]["inventory"]["wallet"]["gp"] == 25
        assert replayed == updated
        assert updated["derived"]["inventory"]["wallet_value_cp"] == 2500
        assert "derived" not in updated["sheet"]

    asyncio.run(exercise_server())


def test_campaign_resume_bundle_reloads_branch_scene_and_continuity(
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

    async def exercise_server() -> None:
        server = create_server(config)
        _, created = await server.call_tool(
            "campaign_create",
            {"name": "Resume test", "idempotency_key": "resume-campaign"},
        )
        _, envelope = await server.call_tool(
            "campaign_query",
            {
                "view": "resume",
                "payload": {"campaign_id": created["id"]},
            },
        )
        result = envelope["result"]
        assert result["campaign"]["id"] == created["id"]
        assert result["current_branch"]["is_current"] is True
        assert result["manifest"] is None
        assert result["continuity"]["context_receipt"]["campaign_id"] == created["id"]
        assert result["resume_invariants"] == {
            "discard_pre_restore_context": True,
            "context_receipt_revision": result["continuity"]["context_receipt"][
                "campaign_revision"
            ],
            "reuse_bound_exposure_after_restore": True,
            "refresh_tools_after_phase_or_checkout_change": True,
        }

    asyncio.run(exercise_server())


def test_server_exposes_static_skill_overview_resource(tmp_path: Path) -> None:
    dnd = tmp_path / "dnd"
    modulegen = tmp_path / "modulegen"
    dnd.mkdir()
    modulegen.mkdir()
    (dnd / "SKILL.md").write_text("# D&D\n", encoding="utf-8")
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=dnd,
        modulegen_skills_dir=modulegen,
    )

    async def inspect_resources() -> None:
        server = create_server(config)
        resources = await server.list_resources()
        assert [str(resource.uri) for resource in resources] == [
            "sagasmith://bootstrap",
            "sagasmith://skills/assets",
            "sagasmith://skills/overview",
            "sagasmith://delegation",
        ]
        bootstrap = await server.read_resource("sagasmith://bootstrap")
        assert "zero-knowledge bootstrap" in bootstrap[0].content
        content = await server.read_resource("sagasmith://skills/overview")
        assert "dnd.root" in content[0].content
        assert "skill_query" in content[0].content
        assert "skill_read" not in content[0].content
        assert "skill_asset_list" not in content[0].content
        assert "skill_asset_read" not in content[0].content
        delegation = await server.read_resource("sagasmith://delegation")
        assert "awaited worker" in delegation[0].content
        assert "zero tools" in delegation[0].content

    asyncio.run(inspect_resources())


def test_server_advertises_native_tools_list_changed(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
    )
    server = create_server(config)

    capabilities = server._mcp_server.create_initialization_options().capabilities.model_dump(
        exclude_none=True
    )

    assert capabilities["tools"]["listChanged"] is True


def test_server_tools_keep_domain_context_metadata_without_host_profiles(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
    )

    async def inspect_tools() -> None:
        server = create_server(config)
        tools = await server.list_tools()
        by_name = {tool.name: tool for tool in tools}
        assert set(by_name) == set().union(*map(set, profile_catalog().values()))
        assert all(
            tool.meta["sagasmith_domain_context"] == "sagasmith-dnd"
            for tool in by_name.values()
        )
        assert by_name["campaign_query"].meta["sagasmith_context_sync"] is True
        rule_properties = by_name["rule_search"].inputSchema["properties"]
        assert "Omit for the first lookup" in rule_properties["filters"]["description"]
        assert "edition" not in rule_properties
        assert "page" not in rule_properties

    asyncio.run(inspect_tools())


def test_campaign_phase_uses_combat_as_the_only_effective_override() -> None:
    assert campaign_phase({}) == "lobby"
    assert campaign_phase({"game_phase": "play"}) == "play"
    assert campaign_phase({"game_phase": "play", "combat": {"active": True}}) == "combat"
    with pytest.raises(ValueError, match="unsupported persisted campaign phase"):
        campaign_phase({"game_phase": "combat"})


def test_server_capabilities_publish_the_rulebook_import_contract(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
    )

    async def inspect_capabilities() -> None:
        server = create_server(config)
        _, capabilities = await server.call_tool("server_capabilities", {})
        assert capabilities["npc_conversations"] == {
            "schema_version": 3,
            "contract": "npc-conversation.v3",
            "phase": "play",
            "execution_mode": "client_subagents_required",
            "proposal_contract": "npc-conversation-proposal.v4",
            "public_tool": "npc_conversation",
            "public_actions": [
                "open",
                "list",
                "get",
                "ingest",
                "publish",
                "close",
                "abort",
            ],
            "host_transport": "private_authenticated_unlisted",
            "actor_scoped_activation_refs": True,
            "agent_resolved_audience": True,
            "per_actor_redacted_inbox": True,
            "selective_response_activation": True,
            "conversation_revision": True,
            "write_idempotency": True,
            "actor_local_authority_refresh": True,
                "local_resolution_waits": True,
                "incremental_actor_context": True,
                "stable_memory_candidate_ids": True,
                "symmetric_heard_statement_candidates": True,
                "actor_safe_transcript_recall": True,
                "terminal_journal_compaction": True,
                "durable_semantic_journal": True,
            "server_managed_inference": False,
            "server_managed_kv": False,
            "minimum_host_capabilities": [
                "isolated_actor_message_contexts",
                "persistent_subagent_workers",
                "zero_tool_npc_workers",
                "structured_json_output",
                "private_host_side_mcp_routing",
            ],
        }
        assert capabilities["features"]["structured_rulebook_import"] is True
        assert capabilities["features"]["source_bound_rule_packs"] is True
        assert capabilities["features"]["structured_content_selection_requirements"] is True
        assert capabilities["features"]["editable_rulebook_drafts"] is True
        assert capabilities["features"]["advisory_candidate_review"] is True
        assert capabilities["features"]["explicit_rulebook_finalization"] is True
        assert capabilities["features"]["durable_finalization_idempotency"] is True
        assert capabilities["features"]["managed_module_document_staging"] is True
        assert capabilities["features"]["core_pdf_module_normalization"] is True
        assert capabilities["features"]["module_document_cache"] is True
        assert capabilities["features"]["module_selective_ocr"] is True
        assert capabilities["features"]["visionless_page_ocr_text"] is True
        assert capabilities["features"]["persistent_ocr_page_cache"] is True
        assert capabilities["features"]["per_page_ocr_confidence_fallback"] is True
        assert capabilities["features"]["lexical_pdf_damage_detection"] is True
        assert capabilities["features"]["agent_bounded_ocr_text_review"] is True
        assert capabilities["features"]["agent_rendered_empty_page_recovery"] is True
        assert capabilities["module_draft"]["stage_inputs"] == [
            "source_path",
            "name+content",
            "module-scoped asset",
        ]
        assert "module_draft(edit)" in capabilities["module_draft"]["stages"]
        assert capabilities["module_draft"]["normalization_cache"] == "content-addressed"
        assert capabilities["module_draft"]["page_extraction_cache"] == "content-addressed"
        assert capabilities["module_draft"]["ocr_page_cache"] == (
            "content-addressed-per-model-page"
        )
        assert capabilities["module_draft"]["ocr_selection"] == {
            "scope": "per-page",
            "minimum_layout_confidence": 0.86,
            "lexical_damage_detection": True,
            "fallback_model": "small",
        }
        assert capabilities["module_draft"]["text_review"] == {
            "actions": [
                "module_draft(evidence)",
                "module_draft(edit:source_text)",
            ],
            "evidence_bases": [
                "cross_text",
                "agent_context",
                "rendered_page",
            ],
            "raw_source_immutable": True,
            "unique_exact_page_replacements": True,
            "rendered_empty_page_recovery": {
                "replacement_shape": [{"old": "", "new": "full_transcript"}],
                "requires_wholly_empty_normalized_page": True,
                "rendered_page_checksum_required": True,
                "review_methods": ["agent", "human"],
            },
            "max_revisions_per_page": 8,
            "post_ingest_revision": "new_import_job_required",
            "vision_required": False,
            "agent_context_numeric_changes": False,
            "agent_context_written_quantity_changes": False,
            "submission_ocr": {
                "cross_text": "required",
                "agent_context": "not_run",
                "rendered_page": "not_run",
            },
            "printed_source_typo_policy": "preserve_source_text_author_structured_card",
        }
        assert capabilities["module_draft"]["normalizer"].startswith("sagasmith-core/pdf-layout-v")
        assert capabilities["module_draft"]["parser"] == (
            f"{DndModuleProfile.name}-v{DndModuleProfile.version}"
        )
        assert capabilities["features"]["player_safe_scene_scopes"] is True
        assert capabilities["features"]["player_safe_combat_maps"] is True
        assert capabilities["features"]["stable_campaign_fact_identity"] is True
        assert capabilities["features"]["atomic_continuity_commit"] is True
        assert capabilities["features"]["source_bound_dm_context_anchors"] is True
        assert capabilities["features"]["pinned_non_executable_module_evidence"] is True
        assert capabilities["features"]["skill_manifest_checksums"] is True
        assert capabilities["features"]["validated_module_runtime_manifest"] is True
        assert capabilities["features"]["shared_continuity_budget"] is True
        assert capabilities["features"]["continuity_diagnostics"] is True
        assert capabilities["contract_version"] == "2026-08-session-exposure-v1"
        assert capabilities["features"]["source_bound_hypnotic_pattern"] is True
        assert capabilities["features"]["compiled_or_agent_content_resolution"] is True
        assert capabilities["ruling_policy"] == {
            "default_dm_resolver": "agent",
            "agent_adjudicates": [
                "agent_dm_adjudication",
                "source_or_scene_fact",
                "descriptive_activity",
                "generic_spell_effect",
                "ready_release_effect",
                "environmental_consequence",
                "module_specific_procedure",
            ],
            "requires_external_input": [
                "player_owned_choice",
                "owner_approval",
                "permission_escalation",
                "missing_or_conflicting_source_review",
            ],
            "transaction_rules": [
                "inspect_existing_payment_before_settlement",
                "do_not_pay_twice",
                "use_public_tools_only",
                "preserve_source_revision_and_random_receipts",
                "use_combat_choice_only_for_an_owned_window",
            ],
        }
        assert capabilities["module_draft"]["runtime_manifest_schema"] == 1
        assert capabilities["rulebook_import"]["settlement_tools"] == {
            "play": "character_check",
            "combat": "combat_check",
        }
        assert "rulebook_draft(start)" in capabilities["rulebook_import"]["stages"]
        assert "rulebook_draft(edit)" in capabilities["rulebook_import"]["stages"]
        assert "rulebook_draft(finalize)" in capabilities["rulebook_import"]["stages"]
        assert "content_pack(import)" in capabilities["rulebook_import"]["stages"]
        assert "content_pack(install)" not in capabilities["rulebook_import"]["stages"]
        assert capabilities["rulebook_import"]["content_package_lifecycle"] == {
            "import_result": "stored_inactive_pack",
            "storage": "rulebook_draft(finalize) or content_pack(import)",
            "activation": "content_pack(activate)",
            "release_manifest_authority": "none",
        }
        assert capabilities["rulebook_import"]["text_review"] == {
            "actions": [
                "rulebook_draft(evidence)",
                "rulebook_draft(edit:source_text)",
            ],
            "evidence_bases": [
                "cross_text",
                "agent_context",
                "rendered_page",
            ],
            "raw_source_immutable": True,
            "unique_exact_page_replacements": True,
            "rendered_empty_page_recovery": {
                "replacement_shape": [{"old": "", "new": "full_transcript"}],
                "requires_wholly_empty_normalized_page": True,
                "rendered_page_checksum_required": True,
                "review_methods": ["agent", "human"],
            },
            "max_revisions_per_page": 8,
            "post_ingest_revision": "new_import_job_required",
            "vision_required": False,
            "agent_context_numeric_changes": False,
            "agent_context_written_quantity_changes": False,
            "submission_ocr": {
                "cross_text": "required",
                "agent_context": "not_run",
                "rendered_page": "not_run",
            },
            "printed_source_typo_policy": "preserve_source_text_author_structured_card",
        }
        assert capabilities["rulebook_import"]["statblock_ocr_correction"] == {
            "evidence_bases": ["staged_text", "rendered_page"],
            "rendered_page_checksum_required": True,
            "vision_required": False,
            "agent_authored_values": True,
            "engine_owned_layout_and_normalization": True,
        }
        assert capabilities["rulebook_import"]["normalization_cache"] == "content-addressed"
        assert capabilities["rulebook_import"]["page_extraction_cache"] == "content-addressed"
        assert capabilities["rulebook_import"]["normalizer"].startswith(
            "sagasmith-core/pdf-layout-v"
        )

    asyncio.run(inspect_capabilities())
