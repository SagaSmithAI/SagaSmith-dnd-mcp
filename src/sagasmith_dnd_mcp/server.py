"""MCP surface for the SagaSmith D&D runtime and bundled skill packs."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from mcp.server.fastmcp import FastMCP
from sagasmith_core import (
    CampaignService,
    CharacterService,
    MemoryService,
    ModuleService,
    RuleService,
)
from sagasmith_core.modules import MarkdownModuleParser
from sagasmith_core.systems import SystemRegistry
from sagasmith_dnd.module_profile import DndModuleProfile
from sagasmith_dnd.system import DND5E

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.skills import SkillCatalog
from sagasmith_dnd_mcp.storage import SagaSmithStorage


def create_server(config: McpConfig | None = None) -> FastMCP:
    """Create a stdio-capable server with one MCP-owned local data directory."""
    config = config or McpConfig.from_environment()
    storage = SagaSmithStorage(config)
    storage.migrate()
    campaigns = CampaignService(storage.database)
    characters = CharacterService(storage.database)
    memories = MemoryService(storage.database)
    modules = ModuleService(storage.database)
    rules = RuleService(storage.database)
    catalog = SkillCatalog(
        dnd_root=config.dnd_skills_dir,
        modulegen_root=config.modulegen_skills_dir,
    )
    mcp = FastMCP(
        "SagaSmith D&D",
        instructions="D&D 5e campaign runtime, module storage, and skill packs.",
    )

    @mcp.tool()
    def storage_status() -> dict[str, Any]:
        """Return the MCP-owned SQLite, ChromaDB, and artifact locations."""
        return storage.status()

    @mcp.tool()
    def storage_migrate() -> dict[str, str]:
        """Run the embedded SQLite schema migrations."""
        storage.migrate()
        return {"status": "ok", "database": storage.database.url}

    @mcp.tool()
    def system_list() -> list[dict[str, Any]]:
        """List systems exposed by this MCP server."""
        registry = SystemRegistry()
        registry.register(DND5E)
        return [
            {
                "id": system.id,
                "display_name": system.display_name,
                "character_types": list(system.character_types),
                "campaign_defaults": system.campaign_defaults,
            }
            for system in registry.list()
        ]

    @mcp.tool()
    def campaign_create(
        name: str,
        description: str = "",
        edition: str = "2024",
        locale: str = "en",
    ) -> dict[str, Any]:
        """Create a D&D 5e campaign inside the MCP-owned SQLite database."""
        return asdict(
            campaigns.create(
                system_id="dnd5e",
                name=name,
                description=description,
                settings={"edition": edition, "locale": locale},
            )
        )

    @mcp.tool()
    def campaign_list(status: str | None = None) -> list[dict[str, Any]]:
        """List D&D 5e campaigns."""
        return [asdict(item) for item in campaigns.list(system_id="dnd5e", status=status)]

    @mcp.tool()
    def character_create(
        name: str,
        campaign_id: str | None = None,
        character_type: str = "pc",
        player_name: str | None = None,
        summary: str = "",
        sheet: dict[str, Any] | None = None,
        notes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a D&D PC, NPC, or monster; optionally bind it to a campaign."""
        return asdict(
            characters.create(
                system_id="dnd5e",
                name=name,
                campaign_id=campaign_id,
                character_type=character_type,
                player_name=player_name,
                summary=summary,
                sheet=sheet,
                notes=notes,
            )
        )

    @mcp.tool()
    def character_list(campaign_id: str | None = None) -> list[dict[str, Any]]:
        """List D&D characters, optionally restricted to a campaign."""
        return [
            asdict(item)
            for item in characters.list(system_id="dnd5e", campaign_id=campaign_id)
        ]

    @mcp.tool()
    def character_update(
        character_id: str,
        name: str | None = None,
        player_name: str | None = None,
        summary: str | None = None,
        sheet: dict[str, Any] | None = None,
        notes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update a D&D character sheet or supporting notes."""
        return asdict(
            characters.update(
                character_id,
                name=name,
                player_name=player_name,
                summary=summary,
                sheet=sheet,
                notes=notes,
            )
        )

    @mcp.tool()
    def memory_add(
        campaign_id: str,
        content: str,
        kind: str = "fact",
        subject: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a durable campaign fact, event, relationship, or NPC memory."""
        return asdict(
            memories.add(
                campaign_id,
                content=content,
                kind=kind,
                subject=subject,
                metadata=metadata,
            )
        )

    @mcp.tool()
    def memory_search(campaign_id: str, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Retrieve durable campaign memories."""
        return [asdict(item) for item in memories.search(campaign_id, query, limit=limit)]

    @mcp.tool()
    def module_write(name: str, content: str) -> dict[str, str]:
        """Write generated Markdown to the managed artifact directory before importing it."""
        path = storage.write_module(name, content)
        return {"artifact": path.name, "path": str(path)}

    @mcp.tool()
    def module_import(campaign_id: str, artifact: str, title: str | None = None) -> dict[str, Any]:
        """Import a Markdown artifact created by module_write into a campaign."""
        path = storage.artifact_module_path(artifact)
        embedder, vectors = storage.dense_components()
        result = modules.ingest_path(
            campaign_id=campaign_id,
            path=path,
            title=title,
            parser=MarkdownModuleParser(profile=DndModuleProfile()),
            embedder=embedder,
            vector_store=vectors,
        )
        return asdict(result)

    @mcp.tool()
    def module_list(campaign_id: str) -> list[dict[str, Any]]:
        """List a campaign's imported modules."""
        return modules.list(campaign_id)

    @mcp.tool()
    def module_search(campaign_id: str, query: str, top_k: int = 8) -> list[dict[str, Any]]:
        """Search imported adventure content using SQLite FTS and optional Chroma vectors."""
        embedder, vectors = storage.dense_components()
        hits = modules.search(
            campaign_id=campaign_id,
            query=query,
            top_k=top_k,
            embedder=embedder,
            vector_store=vectors,
        )
        return [asdict(hit) for hit in hits]

    @mcp.tool()
    def rule_search(
        query: str,
        edition: str | None = None,
        locale: str | None = None,
        top_k: int = 8,
    ) -> list[dict[str, Any]]:
        """Search D&D rule documents previously ingested into the MCP-owned database."""
        embedder, vectors = storage.dense_components()
        hits = rules.search(
            system_id="dnd5e",
            query=query,
            edition=edition,
            locale=locale,
            top_k=top_k,
            embedder=embedder,
            vector_store=vectors,
        )
        return [asdict(hit) for hit in hits]

    @mcp.tool()
    def rule_ingest(
        source_key: str,
        title: str,
        content: str,
        locale: str = "en",
        edition: str = "",
        publication_id: str = "",
    ) -> dict[str, Any]:
        """Ingest Markdown rule content into the MCP-owned D&D rule index."""
        embedder, vectors = storage.dense_components()
        result = rules.ingest(
            system_id="dnd5e",
            source_key=source_key,
            title=title,
            content=content,
            locale=locale,
            edition=edition,
            publication_id=publication_id,
            embedder=embedder,
            vector_store=vectors,
        )
        return asdict(result)

    @mcp.tool()
    def skill_list() -> list[dict[str, str]]:
        """List installed D&D DM, campaign-manager, and module-generator skill documents."""
        return [
            {"id": item.id, "title": item.title, "source": item.source}
            for item in catalog.list()
        ]

    @mcp.tool()
    def skill_read(skill_id: str) -> str:
        """Read one source-of-truth SKILL.md document."""
        return catalog.read(skill_id)

    @mcp.resource("sagasmith://skill/{skill_id}")
    def skill_resource(skill_id: str) -> str:
        """Skill document resource addressed by its id from skill_list."""
        return catalog.read(skill_id)

    @mcp.prompt()
    def dnd_dm(campaign_id: str, objective: str) -> str:
        """Start a D&D DM turn with the bundled D&D DM instructions available as a resource."""
        return (
            f"You are running campaign {campaign_id}. Objective: {objective}\n\n"
            "Read sagasmith://skill/dnd.full.skills.dnd-dm before acting. Use module_search and "
            "rule_search for factual retrieval; record durable changes through the MCP tools."
        )

    @mcp.prompt()
    def module_generator(campaign_id: str, brief: str) -> str:
        """Generate an importable adventure module using the bundled module-generation workflow."""
        return (
            f"Create a D&D module for campaign {campaign_id}. Brief: {brief}\n\n"
            "Read sagasmith://skill/modulegen.root first. Write the resulting Markdown with "
            "module_write, "
            "then call module_import using the returned artifact name."
        )

    return mcp


def main() -> None:
    create_server().run(transport="stdio")
