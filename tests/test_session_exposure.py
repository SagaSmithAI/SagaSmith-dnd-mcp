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

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.exposure import ExposureError, ExposureRegistry
from sagasmith_dnd_mcp.server import create_server
from sagasmith_dnd_mcp.tool_profiles import CORE_TOOLS, GROUP_BY_ID


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
