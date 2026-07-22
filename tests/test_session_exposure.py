from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import ImageContent
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.exposure import ExposureError, ExposureRegistry
from sagasmith_dnd_mcp.server import create_server
from sagasmith_dnd_mcp.tool_profiles import CORE_TOOLS, GROUP_BY_ID


def _write_exposure_module_pdf(path: Path) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    lines = [
        "Chapter 1: Dungeon",
        "D1. Entry",
        "A stone corridor descends into darkness.",
        "D2. Guard Room",
        "Two doors connect this room to the dungeon.",
    ]
    operators = [b"BT /F1 12 Tf 30 250 Td 16 TL"]
    for index, line in enumerate(lines):
        if index:
            operators.append(b"T*")
        operators.append(f"({line}) Tj".encode("ascii"))
    operators.append(b"ET")
    stream = DecodedStreamObject()
    stream.set_data(b"\n".join(operators))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as output:
        writer.write(output)


def test_exposures_are_session_scoped_and_phase_safe() -> None:
    registry = ExposureRegistry()
    first = registry.open(
        session_key="session:first",
        principal_id="system:local",
        campaign_id="campaign-1",
        phase="lobby",
    )
    second = registry.open(
        session_key="session:second",
        principal_id="system:local",
        campaign_id="campaign-1",
        phase="lobby",
    )
    registry.load(first, "lobby.modules")
    registry.load(first, "lobby.rules")

    assert "module_import" in registry.visible_tools(first)
    assert "campaign_rules" in registry.visible_tools(first)
    assert "module_import" not in registry.visible_tools(second)
    with pytest.raises(ExposureError):
        registry.load(first, "combat.actions")
    with pytest.raises(ExposureError):
        registry.get(first.id, "session:second")

    assert registry.refresh_phase(first, "play") is True
    assert "module_import" not in registry.visible_tools(first)
    assert registry.visible_tools(first) == set(CORE_TOOLS)


def test_unbound_exposure_only_loads_bootstrap_or_local_admin() -> None:
    registry = ExposureRegistry()
    exposure = registry.open(
        session_key="session:bootstrap",
        principal_id="discord:user",
        campaign_id=None,
        phase="lobby",
    )
    registry.load(exposure, "lobby.bootstrap")
    with pytest.raises(ExposureError, match="campaign-bound"):
        registry.load(exposure, "lobby.rules")
    with pytest.raises(ExposureError, match="system:local"):
        registry.load(exposure, "lobby.storage_admin")


def test_phase_groups_separate_player_reads_from_dm_control() -> None:
    assert GROUP_BY_ID["lobby.memory"].roles == frozenset()
    assert GROUP_BY_ID["lobby.memory_control"].roles == frozenset({"owner", "dm"})
    assert "memory_query" not in GROUP_BY_ID["lobby.memory"].tools
    assert "memory_query" in GROUP_BY_ID["lobby.memory_control"].tools

    assert GROUP_BY_ID["play.scene"].roles == frozenset()
    assert "branch_query" in GROUP_BY_ID["play.scene"].tools
    assert GROUP_BY_ID["play.scene_control"].roles == frozenset({"owner", "dm"})
    assert "snapshot_query" not in GROUP_BY_ID["play.scene"].tools
    assert "snapshot_query" in GROUP_BY_ID["play.scene_control"].tools
    assert "campaign_rules" in GROUP_BY_ID["play.scene_control"].tools
    assert "combat_start" not in GROUP_BY_ID["play.resolution"].tools
    assert GROUP_BY_ID["play.combat_control"].roles == frozenset({"owner", "dm"})

    assert "combat_end" not in GROUP_BY_ID["combat.turn"].tools
    assert "branch_query" in GROUP_BY_ID["combat.observe"].tools
    assert GROUP_BY_ID["combat.control"].roles == frozenset({"owner", "dm"})
    assert GROUP_BY_ID["combat.save"].roles == frozenset({"owner", "dm"})
    assert GROUP_BY_ID["combat.maintenance"].roles == frozenset({"owner", "dm"})
    assert GROUP_BY_ID["combat.maintenance"].tools == frozenset(
        {"campaign_core_relock", "campaign_rules"}
    )
    assert GROUP_BY_ID["combat.map"].roles == frozenset({"owner", "dm"})


def test_player_exposure_loads_scene_reads_but_not_scene_control(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)

        async def call(name: str, arguments: dict):
            _, result = await server.call_tool(name, arguments)
            return result.get("result", result) if isinstance(result, dict) else result

        campaign = await call(
            "campaign_create",
            {"name": "Player exposure", "idempotency_key": "campaign"},
        )
        campaign = await call(
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "update",
                "payload": {"state": {"game_phase": "play"}},
                "expected_revision": campaign["revision"],
                "idempotency_key": "play-phase",
            },
        )
        await call(
            "access_grant",
            {
                "scope": "campaign",
                "campaign_id": campaign["id"],
                "principal_id": "player:alice",
                "payload": {"role": "player"},
            },
        )
        opened = await call(
            "exposure_open",
            {"campaign_id": campaign["id"], "principal_id": "player:alice"},
        )

        loaded = await call(
            "exposure_load",
            {"exposure_id": opened["exposure_id"], "group_id": "play.scene"},
        )
        assert "module_query" in loaded["visible_tools"]
        assert "snapshot_query" not in loaded["visible_tools"]
        assert "memory_query" not in loaded["visible_tools"]

        with pytest.raises(Exception, match="cannot access"):
            await call(
                "exposure_load",
                {
                    "exposure_id": opened["exposure_id"],
                    "group_id": "play.scene_control",
                },
            )

    asyncio.run(exercise())


def test_exposure_ttl_is_deterministic_and_expired_sessions_are_pruned() -> None:
    registry = ExposureRegistry(ttl=timedelta(microseconds=-1))
    expired = registry.open(
        session_key="session:expired",
        principal_id="system:local",
        campaign_id=None,
        phase="lobby",
    )
    with pytest.raises(ExposureError, match="expired"):
        registry.get(expired.id, "session:expired")

    registry = ExposureRegistry()
    exposure = registry.open(
        session_key="session:ttl",
        principal_id="system:local",
        campaign_id="campaign-1",
        phase="combat",
    )
    registry.load(exposure, "combat.observe")
    registry.load(exposure, "combat.actions", ttl_calls=1)
    assert registry.consume_tool(exposure, "rule_search") is False
    assert "combat.actions" in exposure.loaded_groups
    assert registry.consume_tool(exposure, "combat_check") is True
    assert "combat.actions" not in exposure.loaded_groups
    with pytest.raises(ExposureError, match="not exposed"):
        registry.require_tool(exposure, "combat_check")


def test_native_tool_list_starts_core_and_expands_per_session(tmp_path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
    )

    async def exercise() -> None:
        server = create_server(config)
        server._request_session = lambda: ("mcp:first", object())  # type: ignore[method-assign]
        assert {tool.name for tool in await server.list_tools()} == set(CORE_TOOLS)

        exposure = server.exposure_registry.open(
            session_key="mcp:first",
            principal_id="system:local",
            campaign_id=None,
            phase="lobby",
        )
        server.exposure_registry.load(exposure, "lobby.bootstrap")
        visible = {tool.name for tool in await server.list_tools()}
        assert set(CORE_TOOLS) <= visible
        assert "campaign_create" in visible
        assert "combat_resolve_attack" not in visible

        server._request_session = lambda: ("mcp:second", object())  # type: ignore[method-assign]
        assert {tool.name for tool in await server.list_tools()} == set(CORE_TOOLS)

    asyncio.run(exercise())


def test_stdio_session_uses_native_refresh_and_exposure_call_fallback(tmp_path) -> None:
    async def exercise() -> None:
        env = dict(os.environ)
        env.update(
            {
                "SAGASMITH_DND_MCP_HOME": str(tmp_path / "home"),
                "SAGASMITH_DND_MCP_AUTO_SEED": "0",
            }
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "sagasmith_dnd_mcp.server"],
            cwd=Path(__file__).parents[1],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert {tool.name for tool in (await session.list_tools()).tools} == set(CORE_TOOLS)

                principal_id = "discord:user-42"
                opened = await session.call_tool(
                    "exposure_open", {"principal_id": principal_id}
                )
                exposure_id = json.loads(opened.content[0].text)["exposure_id"]
                loaded = await session.call_tool(
                    "exposure_load",
                    {"exposure_id": exposure_id, "group_id": "lobby.bootstrap"},
                )
                assert not loaded.isError
                assert "campaign_create" in {
                    tool.name for tool in (await session.list_tools()).tools
                }

                created = await session.call_tool(
                    "exposure_call",
                    {
                        "exposure_id": exposure_id,
                        "tool_id": "campaign_create",
                        "arguments": {
                            "name": "Exposure test",
                            "idempotency_key": "exposure-test-create",
                        },
                    },
                )
                assert not created.isError
                created_payload = json.loads(created.content[0].text)
                assert isinstance(created_payload["result"], dict)
                assert created_payload["result"]["name"] == "Exposure test"
                campaigns = await session.call_tool(
                    "campaign_query", {"principal_id": principal_id}
                )
                campaign_id = json.loads(campaigns.content[0].text)["result"][0]["id"]

                reopened = await session.call_tool(
                    "exposure_open",
                    {"campaign_id": campaign_id, "principal_id": principal_id},
                )
                exposure_id = json.loads(reopened.content[0].text)["exposure_id"]
                loaded = await session.call_tool(
                    "exposure_load",
                    {"exposure_id": exposure_id, "group_id": "lobby.rules"},
                )
                assert not loaded.isError
                assert "rule_document_page_render" in {
                    tool.name for tool in (await session.list_tools()).tools
                }
                fallback = await session.call_tool(
                    "exposure_call",
                    {
                        "exposure_id": exposure_id,
                        "tool_id": "rule_seed_status",
                        "arguments": {},
                    },
                )
                assert not fallback.isError
                fallback_payload = json.loads(fallback.content[0].text)
                assert isinstance(fallback_payload["result"], dict)
                assert fallback_payload["result"]["auto_seed"] is False

                cross_campaign = await session.call_tool(
                    "campaign_query",
                    {
                        "view": "get",
                        "payload": {"campaign_id": "another-campaign"},
                        "principal_id": principal_id,
                    },
                )
                assert cross_campaign.isError
                assert "bound to" in cross_campaign.content[0].text

    asyncio.run(exercise())


def test_stdio_exposure_fallback_preserves_rendered_image_content(tmp_path: Path) -> None:
    module_root = tmp_path / "modules"
    module_root.mkdir()
    source = module_root / "exposure-module.pdf"
    _write_exposure_module_pdf(source)

    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
        module_import_roots=(module_root,),
        rule_ocr_enabled=False,
    )

    async def seed() -> tuple[str, str]:
        server = create_server(config)

        async def direct(name: str, arguments: dict):
            called = await server.call_tool(name, arguments)
            if isinstance(called, tuple):
                _, structured = called
                return structured.get("result", structured)
            return called

        campaign = await direct(
            "campaign_create",
            {
                "name": "Image fallback",
                "edition": "2014",
                "idempotency_key": "image-fallback-campaign",
            },
        )
        staged = await direct(
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "stage",
                "payload": {
                    "source_path": str(source),
                    "source_key": "image-fallback",
                    "title": "Image Fallback",
                },
                "idempotency_key": "image-fallback-stage",
            },
        )
        job_id = staged["job"]["id"]
        imported: dict = {}
        for action in ("inspect", "validate", "ingest"):
            imported = await direct(
                "module_import",
                {
                    "campaign_id": campaign["id"],
                    "action": action,
                    "payload": {"job_id": job_id},
                    "idempotency_key": f"image-fallback-{action}",
                },
            )
        current = await direct(
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        await direct(
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "activate",
                "payload": {"job_id": job_id},
                "expected_revision": current["revision"],
                "idempotency_key": "image-fallback-activate",
            },
        )
        return campaign["id"], imported["module_id"]

    campaign_id, module_id = asyncio.run(seed())

    async def exercise() -> None:
        env = dict(os.environ)
        env.update(
            {
                "SAGASMITH_DND_MCP_HOME": str(tmp_path / "home"),
                "SAGASMITH_DND_MCP_AUTO_SEED": "0",
                "SAGASMITH_DND_MCP_MODULE_IMPORT_ROOTS": str(module_root),
                "SAGASMITH_DND_MCP_RULE_OCR": "0",
            }
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "sagasmith_dnd_mcp.server"],
            cwd=Path(__file__).parents[1],
            env=env,
        )

        def decoded(result) -> dict:
            assert not result.isError
            return json.loads(result.content[0].text)

        async def rpc(name: str, arguments: dict, *, timeout_seconds: int = 20):
            try:
                return await session.call_tool(
                    name,
                    arguments,
                    read_timeout_seconds=timedelta(seconds=timeout_seconds),
                )
            except Exception as exc:
                raise AssertionError(f"stdio MCP call {name!r} did not complete") from exc

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                principal_id = "system:local"
                opened = decoded(
                    await rpc(
                        "exposure_open",
                        {"campaign_id": campaign_id, "principal_id": principal_id},
                    )
                )
                exposure_id = opened["exposure_id"]
                await rpc(
                    "exposure_load",
                    {"exposure_id": exposure_id, "group_id": "lobby.modules"},
                )
                rendered = await rpc(
                    "exposure_call",
                    {
                        "exposure_id": exposure_id,
                        "tool_id": "module_page_render",
                        "arguments": {
                            "campaign_id": campaign_id,
                            "module_id": module_id,
                            "page_number": 1,
                            "scale": 0.5,
                        },
                    },
                    timeout_seconds=90,
                )
                envelope = decoded(rendered)
                assert envelope["tool_id"] == "module_page_render"
                assert envelope["result"]["asset"]["metadata"]["source_page"] == 1
                assert len(rendered.content[0].text) < 10_000
                images = [item for item in rendered.content if isinstance(item, ImageContent)]
                assert len(images) == 1
                assert images[0].mimeType == "image/png"
                assert rendered.structuredContent == envelope

    asyncio.run(exercise())
