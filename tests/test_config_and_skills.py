import asyncio
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


def test_skill_catalog_reads_both_repositories(tmp_path: Path) -> None:
    dnd = tmp_path / "dnd"
    modulegen = tmp_path / "modulegen"
    (dnd / "full" / "skills" / "dnd-dm").mkdir(parents=True)
    modulegen.mkdir()
    (dnd / "full" / "skills" / "dnd-dm" / "SKILL.md").write_text("# D&D DM\n", encoding="utf-8")
    (modulegen / "SKILL.md").write_text("# Module Generator\n", encoding="utf-8")
    catalog = SkillCatalog(dnd_root=dnd, modulegen_root=modulegen)

    assert [item.id for item in catalog.list()] == ["dnd.full.skills.dnd-dm", "modulegen.root"]
    assert catalog.read("modulegen.root") == "# Module Generator\n"


def test_skill_catalog_exposes_references_and_templates_as_assets(tmp_path: Path) -> None:
    dnd = tmp_path / "dnd"
    modulegen = tmp_path / "modulegen"
    (dnd / "full" / "references").mkdir(parents=True)
    modulegen.mkdir()
    (dnd / "full" / "references" / "workflow.md").write_text("workflow", encoding="utf-8")
    (dnd / "full" / "examples").mkdir()
    (dnd / "full" / "examples" / "rule-pack.template.json").write_text(
        "{}", encoding="utf-8"
    )
    (modulegen / "template.md").write_text("template", encoding="utf-8")
    catalog = SkillCatalog(dnd_root=dnd, modulegen_root=modulegen)

    assert [asset.id for asset in catalog.assets()] == [
        "dnd:full/examples/rule-pack.template.json",
        "dnd:full/references/workflow.md",
        "modulegen:template.md",
    ]
    assert catalog.read_asset("dnd:full/references/workflow.md") == "workflow"
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
        assert by_name["module_write"].meta["sagasmith_tool_profiles"] == ["authoring"]
        assert by_name["rule_document_import"].meta["sagasmith_tool_profiles"] == [
            "authoring"
        ]
        assert by_name["character_check"].meta["sagasmith_tool_profiles"] == ["play"]
        assert by_name["combat_resolve_attack"].meta["sagasmith_tool_profiles"] == ["combat"]
        assert by_name["combat_start"].meta["sagasmith_tool_profiles"] == ["play"]
        assert by_name["game_phase_get"].meta["sagasmith_tool_profiles"] == [
            "authoring",
            "play",
            "combat",
        ]

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
        assert capabilities["rulebook_import"]["settlement_tools"] == {
            "play": "character_check",
            "combat": "combat_check",
        }
        assert "rule_pack_draft_from_source" in capabilities["rulebook_import"]["stages"]

    asyncio.run(inspect_capabilities())
