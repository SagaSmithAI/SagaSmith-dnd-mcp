import asyncio
import os
from pathlib import Path

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from sagasmith_dnd_mcp.skills import SkillCatalog
from sagasmith_dnd_mcp.tool_profiles import profile_catalog


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
    assert config.rule_import_roots[1] == (
        config.dnd_skills_dir / "full" / "skills" / "dnd-dm" / "srd"
    ).resolve()


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
        {"id": item.id, "source": item.source, "checksum": item.checksum}
        for item in catalog.list()
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
        assert capabilities["module_import"]["stage_inputs"] == ["source_path", "name+content"]
        assert capabilities["module_import"]["normalization_cache"] == "content-addressed"
        assert capabilities["module_import"]["page_extraction_cache"] == "content-addressed"
        assert capabilities["module_import"]["normalizer"].startswith(
            "sagasmith-core/pdf-layout-v"
        )
        assert capabilities["features"]["player_safe_scene_scopes"] is True
        assert capabilities["features"]["player_safe_combat_maps"] is True
        assert capabilities["features"]["stable_campaign_fact_identity"] is True
        assert capabilities["features"]["atomic_continuity_commit"] is True
        assert capabilities["features"]["skill_manifest_checksums"] is True
        assert capabilities["features"]["validated_module_runtime_manifest"] is True
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
