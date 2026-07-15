from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.exposure import ExposureError, ExposureRegistry
from sagasmith_dnd_mcp.server import create_server
from sagasmith_dnd_mcp.tool_profiles import CORE_TOOLS


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

    assert "module_import" in registry.visible_tools(first)
    assert "module_import" not in registry.visible_tools(second)
    with pytest.raises(ExposureError):
        registry.load(first, "combat.actions")
    with pytest.raises(ExposureError):
        registry.get(first.id, "session:second")

    assert registry.refresh_phase(first, "play") is True
    assert "module_import" not in registry.visible_tools(first)
    assert registry.visible_tools(first) == set(CORE_TOOLS)


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
        server.exposure_registry.load(exposure, "lobby.rules")
        visible = {tool.name for tool in await server.list_tools()}
        assert set(CORE_TOOLS) <= visible
        assert "rule_import" in visible
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

                opened = await session.call_tool("exposure_open", {})
                exposure_id = json.loads(opened.content[0].text)["exposure_id"]
                loaded = await session.call_tool(
                    "exposure_load",
                    {"exposure_id": exposure_id, "group_id": "lobby.rules"},
                )
                assert not loaded.isError
                assert "rule_import" in {tool.name for tool in (await session.list_tools()).tools}

                fallback = await session.call_tool(
                    "exposure_call",
                    {
                        "exposure_id": exposure_id,
                        "tool_id": "rule_seed_status",
                        "arguments": {},
                    },
                )
                assert not fallback.isError

    asyncio.run(exercise())
