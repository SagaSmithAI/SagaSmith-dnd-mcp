import asyncio
from pathlib import Path

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from sagasmith_dnd_mcp.skills import SkillCatalog


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
    (modulegen / "template.md").write_text("template", encoding="utf-8")
    catalog = SkillCatalog(dnd_root=dnd, modulegen_root=modulegen)

    assert [asset.id for asset in catalog.assets()] == [
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
        _, campaign = await server.call_tool("campaign_create", {"name": "Test campaign"})
        _, character = await server.call_tool(
            "character_create", {"name": "Aria", "campaign_id": campaign["id"]}
        )
        _, updated = await server.call_tool(
            "character_wallet_adjust",
            {"character_id": character["id"], "denomination": "gp", "amount": 25},
        )

        assert updated["sheet"]["inventory"]["wallet"]["gp"] == 25
        assert updated["derived"]["inventory"]["wallet_value_cp"] == 2500
        assert "derived" not in updated["sheet"]

    asyncio.run(exercise_server())
