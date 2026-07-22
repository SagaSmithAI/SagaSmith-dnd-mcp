from __future__ import annotations

import asyncio
from pathlib import Path

from mcp.server.fastmcp.exceptions import ToolError

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


def test_character_query_inspects_ability_score_options_without_module_import(
    tmp_path: Path,
) -> None:
    imports = tmp_path / "imports"
    imports.mkdir()
    source = imports / "PCStats.txt"
    source.write_text(
        "9, 8, 10, 14, 8, 12\n17, 14, 16, 12, 11, 14\n",
        encoding="utf-8",
    )
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        module_import_roots=(imports,),
    )

    async def exercise() -> None:
        server = create_server(config)
        _, campaign = await server.call_tool(
            "campaign_create",
            {"name": "Character documents", "idempotency_key": "campaign"},
        )
        _, response = await server.call_tool(
            "character_query",
            {
                "view": "document",
                "payload": {
                    "campaign_id": campaign["id"],
                    "source_path": str(source),
                },
            },
        )

        result = response["result"]
        assert result["document_kind"] == "ability_score_options"
        assert result["ability_score_sets"][0] == [9, 8, 10, 14, 8, 12]
        assert result["manual_input"]["modes"][0] == "manual"
        assert result["workflow"]["module_import_allowed"] is False
        assert result["workflow"]["next"] == "complete_missing_fields_then_character_create_from"

    asyncio.run(exercise())


def test_character_document_inspection_rejects_checksum_mismatch(tmp_path: Path) -> None:
    imports = tmp_path / "imports"
    imports.mkdir()
    source = imports / "PCStats.txt"
    source.write_text("9, 8, 10, 14, 8, 12\n", encoding="utf-8")
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        module_import_roots=(imports,),
    )

    async def exercise() -> None:
        server = create_server(config)
        _, campaign = await server.call_tool(
            "campaign_create",
            {"name": "Checksum", "idempotency_key": "campaign"},
        )
        try:
            await server.call_tool(
                "character_query",
                {
                    "view": "document",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "source_path": str(source),
                        "expected_checksum": "0" * 64,
                    },
                },
            )
        except ToolError as error:
            assert "checksum" in str(error)
        else:
            raise AssertionError("checksum mismatch was accepted")

    asyncio.run(exercise())


def test_module_import_rejects_character_support_document(tmp_path: Path) -> None:
    imports = tmp_path / "imports"
    imports.mkdir()
    source = imports / "PCStats.txt"
    source.write_text("9, 8, 10, 14, 8, 12\n", encoding="utf-8")
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        module_import_roots=(imports,),
    )

    async def exercise() -> None:
        server = create_server(config)
        _, campaign = await server.call_tool(
            "campaign_create",
            {"name": "Reject support file", "idempotency_key": "campaign"},
        )
        try:
            await server.call_tool(
                "module_import",
                {
                    "campaign_id": campaign["id"],
                    "action": "stage",
                    "payload": {"source_path": str(source)},
                    "idempotency_key": "stage",
                },
            )
        except ToolError as error:
            assert "not modules" in str(error)
        else:
            raise AssertionError("character support document was imported as a module")

    asyncio.run(exercise())
