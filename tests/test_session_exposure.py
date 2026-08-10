from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from sagasmith_core.access import CAMPAIGN_DM_ROLES
from sagasmith_dnd.combat_engine import NeedsRulingError
from sagasmith_dnd.engine import roll
from sagasmith_dnd.random_stream import (
    CampaignRandomStream,
    initial_random_stream,
    use_random_stream,
)
from sagasmith_dnd.rule_engine import RuleEventRulingRequiredError

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.exposure import ExposureError, ExposureRegistry
from sagasmith_dnd_mcp.server import (
    _agent_ruling_boundary,
    _agent_ruling_resolution,
    _facade_result,
    _pending_result_ruling_kind,
    _ruling_status,
    create_server,
)
from sagasmith_dnd_mcp.tool_profiles import CORE_TOOLS, policy_for_tool


def test_pending_ruling_envelope_defaults_to_agent_reasoning() -> None:
    assert _ruling_status("committed", "generic_spell_effect") == {"status": "committed"}
    assert _ruling_status("pending_ruling", "generic_spell_effect") == {
        "status": "pending_ruling",
        "default_resolver": "agent",
        "ruling_kind": "generic_spell_effect",
        "policy_ref": "server_capabilities.ruling_policy",
        "requires_external_input_only_for": [
            "player_owned_choice",
            "owner_approval",
            "permission_escalation",
            "missing_or_conflicting_source_review",
        ],
    }
    assert _agent_ruling_resolution({"status": "committed"}) is None
    assert _agent_ruling_resolution({"status": "pending_choice"}) is None
    assert _agent_ruling_resolution({"status": "pending_ruling"}) == {
        "default_resolver": "agent",
        "ruling_kind": "agent_dm_adjudication",
        "policy_ref": "server_capabilities.ruling_policy",
        "requires_external_input_only_for": [
            "player_owned_choice",
            "owner_approval",
            "permission_escalation",
            "missing_or_conflicting_source_review",
        ],
    }
    assert _agent_ruling_resolution(
        {
            "status": "pending_ruling",
            "ruling_kind": "missing_or_conflicting_source_review",
        }
    ) == {
        "default_resolver": "external_input",
        "ruling_kind": "missing_or_conflicting_source_review",
        "policy_ref": "server_capabilities.ruling_policy",
    }


def test_facade_preserves_external_ruling_ownership() -> None:
    result = _facade_result(
        "apply",
        {
            **_ruling_status(
                "pending_ruling",
                "missing_or_conflicting_source_review",
            ),
            "reason": "source card is incomplete",
        },
    )

    assert result["status"] == "pending_ruling"
    assert result["default_resolver"] == "external_input"
    assert result["ruling_kind"] == "missing_or_conflicting_source_review"
    assert result["result"]["reason"] == "source card is incomplete"


def test_facade_preserves_nested_external_ruling_ownership() -> None:
    nested = {
        "status": "pending_ruling",
        "ruling_requirements": [
            {
                "default_resolver": "agent",
                "ruling_kind": "module_specific_procedure",
            },
            {
                "default_resolver": "external_input",
                "ruling_kind": "missing_or_conflicting_source_review",
            },
        ],
    }

    assert _agent_ruling_resolution(nested)["default_resolver"] == "external_input"
    result = _facade_result("apply", nested)
    assert result["default_resolver"] == "external_input"
    assert result["ruling_kind"] == "missing_or_conflicting_source_review"


def test_nested_pending_rulings_and_facade_results_preserve_external_ownership() -> None:
    nested = {
        "status": "pending_ruling",
        "result": {
            "status": "pending_ruling",
            "pending_rulings": [
                {
                    "default_resolver": "external_input",
                    "ruling_kind": "missing_or_conflicting_source_review",
                }
            ],
        },
    }

    assert _agent_ruling_resolution(nested)["default_resolver"] == "external_input"
    result = _facade_result("apply", nested)
    assert result["default_resolver"] == "external_input"
    assert result["ruling_kind"] == "missing_or_conflicting_source_review"


def test_unknown_dm_ruling_kind_defaults_to_agent_adjudication() -> None:
    result = _ruling_status("pending_ruling", "unclassified_manual_review")

    assert result["default_resolver"] == "agent"
    assert result["ruling_kind"] == "agent_dm_adjudication"


def test_needs_ruling_boundary_returns_to_agent_without_committing() -> None:
    @_agent_ruling_boundary
    def operation() -> None:
        raise NeedsRulingError(
            "module procedure needs a narrative fact",
            missing=("module_fact",),
        )

    assert operation() == {
        "status": "pending_ruling",
        "default_resolver": "agent",
        "ruling_kind": "agent_dm_adjudication",
        "policy_ref": "server_capabilities.ruling_policy",
        "requires_external_input_only_for": [
            "player_owned_choice",
            "owner_approval",
            "permission_escalation",
            "missing_or_conflicting_source_review",
        ],
        "reason": "module procedure needs a narrative fact",
        "missing": ["module_fact"],
        "committed": False,
        "retry_contract": {
            "resolver": "agent",
            "reuse_current_revision": True,
            "use_public_tools_only": True,
        },
    }


def test_needs_ruling_boundary_rewinds_uncommitted_random_draws() -> None:
    state = {"random_stream": initial_random_stream("agent-ruling-retry")}
    stream = CampaignRandomStream.from_campaign_state(
        "campaign-1",
        state,
        operation="combat_join",
        idempotency_key="join-retry",
    )

    @_agent_ruling_boundary
    def operation() -> None:
        roll("1d20")
        raise NeedsRulingError(
            "joining initiative ties need an explicit tie_breaker choice",
            missing=("tie_breaker",),
        )

    with use_random_stream(stream):
        first = operation()
        assert stream.position == 0
        replayed_roll = roll("1d20")

    replay = CampaignRandomStream.from_campaign_state(
        "campaign-1",
        state,
        operation="combat_join",
        idempotency_key="join-retry",
    )
    with use_random_stream(replay):
        expected_roll = roll("1d20")

    assert first["status"] == "pending_ruling"
    assert first["default_resolver"] == "agent"
    assert replayed_roll == expected_roll


def test_needs_ruling_boundary_keeps_source_defects_external() -> None:
    @_agent_ruling_boundary
    def operation() -> None:
        raise NeedsRulingError(
            "weapon ranged attack has no recorded range",
            missing=("weapon.range:source-bow",),
        )

    result = operation()

    assert result["status"] == "pending_ruling"
    assert result["default_resolver"] == "external_input"
    assert result["ruling_kind"] == "missing_or_conflicting_source_review"
    assert result["committed"] is False
    assert result["retry_contract"]["resolver"] == "external_input"


def test_needs_ruling_boundary_preserves_an_explicit_player_choice() -> None:
    @_agent_ruling_boundary
    def operation() -> None:
        raise NeedsRulingError(
            "active rule pack needs the actor's choice",
            missing=("choose-recovery",),
            ruling_kind="player_owned_choice",
        )

    result = operation()

    assert result["status"] == "pending_ruling"
    assert result["default_resolver"] == "external_input"
    assert result["ruling_kind"] == "player_owned_choice"
    assert result["retry_contract"]["resolver"] == "external_input"


def test_declarative_rule_pause_returns_to_its_typed_resolver() -> None:
    @_agent_ruling_boundary
    def agent_operation() -> None:
        raise RuleEventRulingRequiredError(
            "active pack needs an environmental ruling",
            event="character.validate",
            status="pending_ruling",
            pending=(
                {
                    "mechanic_id": "weather-rule",
                    "op": "ruling.require",
                    "id": "weather",
                    "default_resolver": "agent",
                    "ruling_kind": "environmental_consequence",
                },
            ),
        )

    agent_result = agent_operation()
    assert agent_result["default_resolver"] == "agent"
    assert agent_result["ruling_kind"] == "environmental_consequence"
    assert agent_result["missing"] == ["weather-rule"]
    assert agent_result["ruling_requirements"][0]["id"] == "weather"

    @_agent_ruling_boundary
    def player_operation() -> None:
        raise RuleEventRulingRequiredError(
            "active pack needs the player's choice",
            event="character.validate",
            status="pending_choice",
            pending=(
                {
                    "mechanic_id": "form-rule",
                    "op": "choice.require",
                    "id": "choose-form",
                    "default_resolver": "external_input",
                    "ruling_kind": "player_owned_choice",
                },
            ),
        )

    player_result = player_operation()
    assert player_result["default_resolver"] == "external_input"
    assert player_result["ruling_kind"] == "player_owned_choice"


def test_nested_pending_results_default_to_agent_and_preserve_exceptions() -> None:
    assert (
        _pending_result_ruling_kind(
            {
                "status": "pending_ruling",
                "pending": [
                    {
                        "ruling_kind": "module_specific_procedure",
                        "default_resolver": "agent",
                    }
                ],
            }
        )
        == "module_specific_procedure"
    )
    assert (
        _pending_result_ruling_kind(
            {
                "status": "pending_ruling",
                "pending": [
                    {
                        "ruling_kind": "environmental_consequence",
                        "default_resolver": "agent",
                    },
                    {
                        "ruling_kind": "missing_or_conflicting_source_review",
                        "default_resolver": "external_input",
                    },
                ],
            }
        )
        == "missing_or_conflicting_source_review"
    )
    assert (
        _pending_result_ruling_kind(
            {
                "status": "pending_ruling",
                "pending": [
                    {"ruling_kind": "missing_or_conflicting_source_review"},
                    {"ruling_kind": "player_owned_choice"},
                ],
            }
        )
        == "player_owned_choice"
    )
    assert _pending_result_ruling_kind({"status": "pending_ruling"}) == ("agent_dm_adjudication")
    assert (
        _pending_result_ruling_kind(
            {
                "status": "pending_ruling",
                "ruling_kind": "missing_or_conflicting_source_review",
            }
        )
        == "missing_or_conflicting_source_review"
    )


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
    assert registry.set_tools(first, add=["module_draft", "rulebook_draft"]) is True

    assert "module_draft" in registry.visible_tools(first)
    assert "module_draft" not in registry.visible_tools(second)
    with pytest.raises(ExposureError, match="unavailable"):
        registry.set_tools(first, add=["combat_query"])
    with pytest.raises(ExposureError, match="another MCP session"):
        registry.get(first.id, "session:second")

    assert registry.refresh_phase(first, "play") is True
    assert first.loaded_tools == set()
    assert registry.visible_tools(first) == set(CORE_TOOLS)
    assert first.revision == 2


def test_unbound_exposure_only_loads_non_campaign_tools() -> None:
    registry = ExposureRegistry()
    exposure = registry.open(
        session_key="session:bootstrap",
        principal_id="discord:user",
        campaign_id=None,
        phase="lobby",
    )
    registry.set_tools(exposure, add=["campaign_create", "system_list"])
    with pytest.raises(ExposureError, match="campaign-bound"):
        registry.set_tools(exposure, add=["rulebook_draft"])
    with pytest.raises(ExposureError, match="system:local"):
        registry.set_tools(exposure, add=["storage_migrate"])


def test_tool_policy_separates_phase_and_role_authority() -> None:
    assert policy_for_tool("content_pack").phases == frozenset({"lobby"})
    assert policy_for_tool("content_pack").roles("lobby") == CAMPAIGN_DM_ROLES
    assert policy_for_tool("module_query").roles("lobby") == CAMPAIGN_DM_ROLES
    assert policy_for_tool("module_query").roles("play") == frozenset()
    assert policy_for_tool("campaign_event").roles("play") == CAMPAIGN_DM_ROLES
    assert policy_for_tool("combat_query").phases == frozenset({"combat"})
    assert policy_for_tool("campaign_create").requires_campaign is False
    assert policy_for_tool("storage_migrate").local_only is True


def test_exposure_time_lease_and_revision_are_deterministic() -> None:
    expired_registry = ExposureRegistry(ttl=timedelta(microseconds=-1))
    expired = expired_registry.open(
        session_key="session:expired",
        principal_id="system:local",
        campaign_id=None,
        phase="lobby",
    )
    with pytest.raises(ExposureError, match="expired"):
        expired_registry.get(expired.id, "session:expired")

    moments = iter(
        [
            datetime(2026, 7, 28, 1, 0, tzinfo=UTC),
            datetime(2026, 7, 28, 1, 1, tzinfo=UTC),
            datetime(2026, 7, 28, 1, 2, tzinfo=UTC),
        ]
    )
    registry = ExposureRegistry(ttl=timedelta(hours=2), clock=lambda: next(moments))
    exposure = registry.open(
        session_key="session:clock",
        principal_id="system:local",
        campaign_id=None,
        phase="lobby",
    )
    assert exposure.created_at == datetime(2026, 7, 28, 1, 1, tzinfo=UTC)
    assert registry.set_tools(exposure, add=["campaign_create"]) is True
    assert exposure.revision == 1
    assert exposure.updated_at == datetime(2026, 7, 28, 1, 2, tzinfo=UTC)


def test_native_tool_list_starts_core_and_varies_per_session(tmp_path: Path) -> None:
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
        server._request_session = lambda: ("mcp:first", object())  # type: ignore[method-assign]
        assert {tool.name for tool in await server.list_tools()} == set(CORE_TOOLS)

        first = server.exposure_registry.open(
            session_key="mcp:first",
            principal_id="system:local",
            campaign_id=None,
            phase="lobby",
        )
        server.exposure_registry.set_tools(first, add=["campaign_create"])
        assert "campaign_create" in {tool.name for tool in await server.list_tools()}

        server._request_session = lambda: ("mcp:second", object())  # type: ignore[method-assign]
        assert {tool.name for tool in await server.list_tools()} == set(CORE_TOOLS)

    asyncio.run(exercise())


def test_stdio_session_mutates_native_tool_list_and_calls_tools_directly(
    tmp_path: Path,
) -> None:
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
                initialized = await session.initialize()
                assert initialized.capabilities.tools.listChanged is True
                assert {tool.name for tool in (await session.list_tools()).tools} == set(CORE_TOOLS)

                principal_id = "discord:user-42"
                opened = await session.call_tool(
                    "exposure",
                    {"action": "open", "principal_id": principal_id},
                )
                assert not opened.isError
                loaded = await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": ["campaign_create", "system_list"],
                        "principal_id": principal_id,
                    },
                )
                assert not loaded.isError
                visible = {tool.name for tool in (await session.list_tools()).tools}
                assert "campaign_create" in visible
                assert "combat_query" not in visible

                created = await session.call_tool(
                    "campaign_create",
                    {
                        "name": "Exposure test",
                        "idempotency_key": "exposure-test-create",
                    },
                )
                assert not created.isError
                campaign_id = json.loads(created.content[0].text)["id"]
                second_created = await session.call_tool(
                    "campaign_create",
                    {
                        "name": "Exposure test second",
                        "idempotency_key": "exposure-test-create-second",
                    },
                )
                assert not second_created.isError
                second_campaign_id = json.loads(second_created.content[0].text)["id"]

                reopened = await session.call_tool(
                    "exposure",
                    {
                        "action": "open",
                        "campaign_id": campaign_id,
                        "principal_id": principal_id,
                    },
                )
                assert not reopened.isError
                loaded = await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": ["rule_seed_status", "rulebook_draft"],
                        "principal_id": principal_id,
                    },
                )
                assert not loaded.isError
                visible = {tool.name for tool in (await session.list_tools()).tools}
                assert "rulebook_draft" in visible
                status = await session.call_tool("rule_seed_status", {})
                assert not status.isError
                assert json.loads(status.content[0].text)["auto_seed"] is False

                rebound = await session.call_tool(
                    "exposure",
                    {
                        "action": "open",
                        "campaign_id": second_campaign_id,
                        "principal_id": principal_id,
                    },
                )
                assert not rebound.isError
                rebound_payload = json.loads(rebound.content[0].text)
                assert rebound_payload["campaign_id"] == second_campaign_id
                assert rebound_payload["loaded_tools"] == []

    asyncio.run(exercise())


def test_stdio_undo_phase_change_immediately_notifies_and_refreshes_tools(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        notifications: list[str] = []

        async def on_message(message) -> None:
            notifications.append(type(getattr(message, "root", message)).__name__)

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
            async with ClientSession(read, write, message_handler=on_message) as session:
                await session.initialize()
                principal_id = "discord:undo-phase"
                await session.call_tool(
                    "exposure", {"action": "open", "principal_id": principal_id}
                )
                await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": ["campaign_create"],
                        "principal_id": principal_id,
                    },
                )
                created = await session.call_tool(
                    "campaign_create",
                    {"name": "Undo phase", "idempotency_key": "create"},
                )
                campaign_id = json.loads(created.content[0].text)["id"]
                await session.call_tool(
                    "exposure",
                    {
                        "action": "open",
                        "campaign_id": campaign_id,
                        "principal_id": principal_id,
                    },
                )
                await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": ["game_phase"],
                        "principal_id": principal_id,
                    },
                )
                current = await session.call_tool(
                    "campaign_query",
                    {
                        "view": "get",
                        "payload": {"campaign_id": campaign_id},
                        "principal_id": principal_id,
                    },
                )
                revision = json.loads(current.content[0].text)["result"]["revision"]
                entered = await session.call_tool(
                    "game_phase",
                    {
                        "campaign_id": campaign_id,
                        "action": "set",
                        "tool_profile": "play",
                        "expected_revision": revision,
                        "idempotency_key": "enter-play",
                    },
                )
                assert not entered.isError
                await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": ["character_check", "state_revision"],
                        "principal_id": principal_id,
                    },
                )
                assert "character_check" in {
                    tool.name for tool in (await session.list_tools()).tools
                }
                history = await session.call_tool(
                    "state_revision",
                    {
                        "campaign_id": campaign_id,
                        "action": "history",
                        "payload": {},
                    },
                )
                expected_history_sequence = json.loads(history.content[0].text)["result"][0][
                    "sequence"
                ]
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                notifications.clear()

                undone = await session.call_tool(
                    "state_revision",
                    {
                        "campaign_id": campaign_id,
                        "action": "undo",
                        "payload": {
                            "expected_history_sequence": expected_history_sequence,
                        },
                        "idempotency_key": "undo-enter-play",
                    },
                )
                assert not undone.isError
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert "ToolListChangedNotification" in notifications
                visible = {tool.name for tool in (await session.list_tools()).tools}
                assert "character_check" not in visible
                resumed = await session.call_tool(
                    "campaign_query",
                    {
                        "view": "get",
                        "payload": {"campaign_id": campaign_id},
                        "principal_id": principal_id,
                    },
                )
                assert json.loads(resumed.content[0].text)["result"]["state"][
                    "game_phase"
                ] == "lobby"

    asyncio.run(exercise())


def test_stdio_process_binding_overwrites_model_authored_principal(tmp_path: Path) -> None:
    async def exercise() -> None:
        env = dict(os.environ)
        env.update(
            {
                "SAGASMITH_DND_MCP_HOME": str(tmp_path / "home"),
                "SAGASMITH_DND_MCP_AUTO_SEED": "0",
                "SAGASMITH_DND_MCP_BOUND_PRINCIPAL_ID": "discord:trusted-user",
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
                opened = await session.call_tool(
                    "exposure",
                    {"action": "open", "principal_id": "model:forged-user"},
                )
                opened_payload = json.loads(opened.content[0].text)
                assert opened_payload["principal_id"] == "discord:trusted-user"
                await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": ["campaign_create"],
                        "principal_id": "model:forged-user",
                    },
                )
                created = await session.call_tool(
                    "campaign_create",
                    {
                        "name": "Principal-bound campaign",
                        "principal_id": "model:forged-user",
                        "idempotency_key": "bound-principal-create",
                    },
                )
                assert not created.isError
                listed = await session.call_tool(
                    "campaign_query",
                    {"principal_id": "another:forged-user"},
                )
                listed_payload = json.loads(listed.content[0].text)["result"]
                assert [item["name"] for item in listed_payload] == [
                    "Principal-bound campaign"
                ]

    asyncio.run(exercise())
