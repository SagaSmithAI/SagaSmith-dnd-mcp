import asyncio
import json
import os
from pathlib import Path

import pytest
from sagasmith_dnd.module_profile import DndModuleProfile

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from sagasmith_dnd_mcp.skills import SkillCatalog
from sagasmith_dnd_mcp.tool_budget import (
    BASELINE_INPUT_SCHEMA_BYTES,
    BASELINE_PUBLIC_TOOL_COUNT,
    PROFILE_TOOL_LIMITS,
    TARGET_CORE_TOOL_COUNT,
    TARGET_INPUT_SCHEMA_BYTES,
    TARGET_PUBLIC_TOOL_COUNT,
    TOOL_BUDGET_VERSION,
)
from sagasmith_dnd_mcp.tool_profiles import CORE_TOOLS, campaign_phase, profile_catalog


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


def test_environment_config_has_separate_rule_and_module_import_roots(monkeypatch) -> None:
    monkeypatch.setenv(
        "SAGASMITH_DND_MCP_RULE_IMPORT_ROOTS", os.pathsep.join(("rules-a", "rules-b"))
    )
    monkeypatch.setenv(
        "SAGASMITH_DND_MCP_MODULE_IMPORT_ROOTS", os.pathsep.join(("modules-a", "modules-b"))
    )
    monkeypatch.setenv("SAGASMITH_DND_MCP_MODULE_OCR", "0")
    monkeypatch.setenv("SAGASMITH_DND_MCP_MODULE_OCR_SCALE", "1.5")

    config = McpConfig.from_environment()

    assert [path.name for path in config.rule_import_roots] == ["rules-a", "rules-b"]
    assert [path.name for path in config.module_import_roots] == ["modules-a", "modules-b"]
    assert config.module_ocr_enabled is False
    assert config.module_ocr_scale == 1.5


def test_default_rule_import_roots_include_the_dnd_skill_corpus(monkeypatch) -> None:
    monkeypatch.delenv("SAGASMITH_DND_MCP_RULE_IMPORT_ROOTS", raising=False)

    config = McpConfig.from_environment()

    assert config.rule_import_roots[0].name == "DnD-Books"
    assert (
        config.rule_import_roots[1]
        == (config.dnd_skills_dir / "full" / "skills" / "dnd-dm" / "srd").resolve()
    )


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
            "character_create",
            {
                "name": "Aria",
                "campaign_id": campaign["id"],
                "idempotency_key": "create-aria",
            },
        )
        _, updated = await server.call_tool(
            "character_wallet_adjust",
            {
                "character_id": character["id"],
                "denomination": "gp",
                "amount": 25,
                "expected_revision": character["revision"],
                "idempotency_key": "wallet-test-1",
            },
        )
        _, replayed = await server.call_tool(
            "character_wallet_adjust",
            {
                "character_id": character["id"],
                "denomination": "gp",
                "amount": 25,
                "expected_revision": character["revision"],
                "idempotency_key": "wallet-test-1",
            },
        )

        assert updated["sheet"]["inventory"]["wallet"]["gp"] == 25
        assert replayed == updated
        assert updated["derived"]["inventory"]["wallet_value_cp"] == 2500
        assert "derived" not in updated["sheet"]

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
        assert [str(resource.uri) for resource in resources] == ["sagasmith://skills/overview"]
        content = await server.read_resource("sagasmith://skills/overview")
        assert "dnd.root" in content[0].content

    asyncio.run(inspect_resources())


def test_server_tool_profiles_are_complete_and_attached_to_tool_metadata(tmp_path: Path) -> None:
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
        assert by_name["module_import"].meta["sagasmith_tool_profiles"] == ["lobby"]
        assert by_name["module_import"].meta["sagasmith_tool_groups"] == ["lobby.modules"]
        assert by_name["rule_import"].meta["sagasmith_tool_profiles"] == ["lobby"]
        assert by_name["character_check"].meta["sagasmith_tool_profiles"] == ["play"]
        assert by_name["combat_resolve_attack"].meta["sagasmith_tool_profiles"] == ["combat"]
        assert by_name["combat_start"].meta["sagasmith_tool_profiles"] == ["play"]
        assert by_name["game_phase"].meta["sagasmith_tool_profiles"] == [
            "lobby",
            "play",
            "combat",
        ]
        assert by_name["game_phase"].meta["sagasmith_tool_groups"] == []

    asyncio.run(inspect_tools())


def test_campaign_phase_uses_combat_as_the_only_effective_override() -> None:
    assert campaign_phase({}) == "lobby"
    assert campaign_phase({"game_phase": "play"}) == "play"
    assert (
        campaign_phase({"game_phase": "play", "combat": {"active": True}})
        == "combat"
    )
    with pytest.raises(ValueError, match="unsupported persisted campaign phase"):
        campaign_phase({"game_phase": "combat"})


def test_compact_public_tool_and_schema_budgets_are_locked(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def inspect_budget() -> None:
        server = create_server(config)
        tools = await server.list_tools()
        schema_bytes = sum(
            len(
                json.dumps(
                    tool.inputSchema,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            )
            for tool in tools
        )

        assert BASELINE_PUBLIC_TOOL_COUNT == 92
        assert BASELINE_INPUT_SCHEMA_BYTES == 56_611
        assert len(CORE_TOOLS) == TARGET_CORE_TOOL_COUNT == 12
        assert len(tools) == TARGET_PUBLIC_TOOL_COUNT == 82
        assert (
            {phase: len(names) for phase, names in profile_catalog().items()}
            == PROFILE_TOOL_LIMITS
            == {
                "lobby": 61,
                "play": 46,
                "combat": 44,
            }
        )
        assert schema_bytes == TARGET_INPUT_SCHEMA_BYTES == 47_752
        assert schema_bytes < BASELINE_INPUT_SCHEMA_BYTES
        by_name = {tool.name: tool for tool in tools}
        assert by_name["chase"].inputSchema["properties"]["action"]["enum"] == [
            "start",
            "query",
            "take_turn",
            "end",
        ]
        assert by_name["character_check"].inputSchema["properties"]["action"]["enum"] == [
            "check",
            "contest",
        ]
        assert (
            "rest"
            not in by_name["character_state_change"]
            .inputSchema["properties"]["action"]["enum"]
        )
        assert not {
            "memory_add",
            "memory_resolve",
        } & set(
            by_name["character_state_change"]
            .inputSchema["properties"]["action"]["enum"]
        )
        assert by_name["rule_import"].inputSchema["properties"]["action"]["enum"] == [
            "discover",
            "stage",
            "inspect",
            "render_page",
            "recover_statblock",
            "ingest",
            "review_statblock",
            "extract_candidates",
            "review",
            "compile",
            "install",
            "activate",
        ]
        assert by_name["module_review"].inputSchema["properties"]["action"]["enum"] == [
            "render_page",
            "submit_content",
        ]
        assert by_name["combat_choice"].inputSchema["properties"]["action"]["enum"] == [
            "open",
            "resolve",
            "resolve_defense",
            "on_hit_ruling",
        ]
        assert by_name["combat_query"].inputSchema["properties"]["view"]["enum"] == [
            "status",
            "available_actions",
            "reactions",
            "transaction_history",
            "transaction_receipt",
        ]

        expected_budget = {
            "version": TOOL_BUDGET_VERSION,
            "baseline_public_tools": BASELINE_PUBLIC_TOOL_COUNT,
            "baseline_input_schema_bytes": BASELINE_INPUT_SCHEMA_BYTES,
            "target_public_tools": TARGET_PUBLIC_TOOL_COUNT,
            "target_core_tools": TARGET_CORE_TOOL_COUNT,
            "target_input_schema_bytes": TARGET_INPUT_SCHEMA_BYTES,
            "profile_limits": PROFILE_TOOL_LIMITS,
        }
        _, capabilities = await server.call_tool("server_capabilities", {})
        assert capabilities["tool_exposure"]["budget"] == expected_budget
        _, profiles = await server.call_tool("server_tool_profiles", {})
        assert profiles["budget"] == expected_budget

    asyncio.run(inspect_budget())


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
        assert capabilities["features"]["structured_rulebook_import"] is True
        assert capabilities["features"]["source_bound_rule_packs"] is True
        assert capabilities["features"]["structured_content_selection_requirements"] is True
        assert capabilities["features"]["module_import_idempotency"] is True
        assert capabilities["features"]["managed_module_document_staging"] is True
        assert capabilities["features"]["core_pdf_module_normalization"] is True
        assert capabilities["features"]["module_document_cache"] is True
        assert capabilities["features"]["module_selective_ocr"] is True
        assert capabilities["module_import"]["stage_inputs"] == [
            "source_path",
            "name+content",
            "module-scoped asset",
        ]
        assert "module_import(attach_asset)" in capabilities["module_import"]["stages"]
        assert capabilities["module_import"]["normalization_cache"] == "content-addressed"
        assert capabilities["module_import"]["page_extraction_cache"] == "content-addressed"
        assert capabilities["module_import"]["normalizer"].startswith("sagasmith-core/pdf-layout-v")
        assert capabilities["module_import"]["parser"] == (
            f"{DndModuleProfile.name}-v{DndModuleProfile.version}"
        )
        assert capabilities["features"]["player_safe_scene_scopes"] is True
        assert capabilities["features"]["player_safe_combat_maps"] is True
        assert capabilities["features"]["stable_campaign_fact_identity"] is True
        assert capabilities["features"]["atomic_continuity_commit"] is True
        assert capabilities["features"]["skill_manifest_checksums"] is True
        assert capabilities["features"]["validated_module_runtime_manifest"] is True
        assert capabilities["features"]["shared_continuity_budget"] is True
        assert capabilities["features"]["continuity_diagnostics"] is True
        assert capabilities["contract_version"] == "2026-07-session-exposure-v4"
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
        assert capabilities["module_import"]["runtime_manifest_schema"] == 1
        assert capabilities["rulebook_import"]["settlement_tools"] == {
            "play": "character_check",
            "combat": "combat_check",
        }
        assert "rule_pack_compile(from_source)" in capabilities["rulebook_import"]["stages"]
        assert "rule_import(extract_candidates)" in capabilities["rulebook_import"]["stages"]
        assert capabilities["rulebook_import"]["normalization_cache"] == "content-addressed"
        assert capabilities["rulebook_import"]["page_extraction_cache"] == "content-addressed"
        assert capabilities["rulebook_import"]["normalizer"].startswith(
            "sagasmith-core/pdf-layout-v"
        )

    asyncio.run(inspect_capabilities())
