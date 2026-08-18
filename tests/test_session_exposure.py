from __future__ import annotations

import asyncio
import json
import os
import sys
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from sagasmith_core.access import CAMPAIGN_DM_ROLES
from sagasmith_dnd.character_schema import default_character_sheet
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
from tests.authoring_helpers import finalize_and_activate_module


def test_role_refresh_crops_loaded_tools_without_changing_phase() -> None:
    registry = ExposureRegistry()
    exposure = registry.open(
        session_key="session",
        principal_id="player",
        campaign_id="campaign",
        phase="play",
    )
    registry.set_tools(
        exposure,
        add=["character_check", "character_content_apply"],
    )

    changed = registry.refresh_phase(
        exposure,
        "play",
        allowed_tools={"character_check"},
    )

    assert changed is True
    assert exposure.phase == "play"
    assert exposure.loaded_tools == {"character_check"}


def test_character_creation_is_lobby_only_and_recovery_survives_combat() -> None:
    assert policy_for_tool("character_create_from").phases == frozenset({"lobby"})
    assert policy_for_tool("character_create_from").roles("lobby") == frozenset()
    assert policy_for_tool("character_content_apply").roles("play") == frozenset(
        CAMPAIGN_DM_ROLES
    )
    assert policy_for_tool("state_revision").roles("combat") == frozenset(
        CAMPAIGN_DM_ROLES
    )


def test_campaign_admission_is_independent_of_game_phase() -> None:
    for tool_id in ("access_grant", "access_revoke"):
        policy = policy_for_tool(tool_id)
        assert policy is not None
        assert policy.phases == frozenset({"lobby", "play", "combat"})
        for phase in policy.phases:
            assert policy.roles(phase) == frozenset(CAMPAIGN_DM_ROLES)


def test_role_demotion_refreshes_only_the_affected_native_session(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(
            McpConfig(
                home=tmp_path / "home",
                database_url=None,
                chroma_url=None,
                chroma_path_override=None,
                dnd_skills_dir=tmp_path / "dnd",
                modulegen_skills_dir=tmp_path / "modulegen",
                auto_seed_rules=False,
            )
        )

        async def call(name: str, arguments: dict):
            _, result = await server.call_tool(name, arguments)
            return result.get("result", result) if isinstance(result, dict) else result

        campaign = await call(
            "campaign_create",
            {"name": "Role refresh", "idempotency_key": "campaign"},
        )
        principal_id = "discord:role-refresh"
        await call(
            "access_grant",
            {
                "scope": "campaign",
                "campaign_id": campaign["id"],
                "principal_id": principal_id,
                "payload": {"role": "dm"},
                "by_principal_id": "system:local",
            },
        )
        await call(
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": campaign["revision"],
                "idempotency_key": "play",
            },
        )
        exposure = server.exposure_registry.open(
            session_key="player-session",
            principal_id=principal_id,
            campaign_id=campaign["id"],
            phase="play",
        )
        server.exposure_registry.set_tools(
            exposure,
            add=["character_check", "character_content_apply"],
        )
        owner_exposure = server.exposure_registry.open(
            session_key="owner-session",
            principal_id="system:local",
            campaign_id=campaign["id"],
            phase="play",
        )
        server.exposure_registry.set_tools(
            owner_exposure,
            add=["character_content_apply"],
        )

        class Session:
            notifications = 0

            async def send_tool_list_changed(self) -> None:
                self.notifications += 1

        session = Session()
        owner_session = Session()
        server._sessions["player-session"] = session
        server._sessions["owner-session"] = owner_session
        await call(
            "access_grant",
            {
                "scope": "campaign",
                "campaign_id": campaign["id"],
                "principal_id": principal_id,
                "payload": {"role": "player"},
                "by_principal_id": "system:local",
            },
        )

        assert await server._refresh("player-session", campaign["id"]) is True
        assert exposure.loaded_tools == {"character_check"}
        assert session.notifications == 1
        assert owner_exposure.loaded_tools == {"character_content_apply"}
        assert owner_session.notifications == 0

    asyncio.run(exercise())


def test_actor_private_downgrade_advances_context_without_tool_delta(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(
            McpConfig(
                home=tmp_path / "home",
                database_url=None,
                chroma_url=None,
                chroma_path_override=None,
                dnd_skills_dir=tmp_path / "dnd",
                modulegen_skills_dir=tmp_path / "modulegen",
                auto_seed_rules=False,
            )
        )

        async def call(name: str, arguments: dict):
            _, result = await server.call_tool(name, arguments)
            return result.get("result", result) if isinstance(result, dict) else result

        campaign = await call(
            "campaign_create", {"name": "Private barrier", "idempotency_key": "campaign"}
        )
        actor = await call(
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Secret actor"},
                "idempotency_key": "actor",
            },
        )
        target = "discord:private-reader"
        await call(
            "access_grant",
            {
                "scope": "campaign",
                "campaign_id": campaign["id"],
                "principal_id": target,
                "payload": {"role": "player"},
                "by_principal_id": "system:local",
            },
        )
        await call(
            "access_grant",
            {
                "scope": "actor",
                "campaign_id": campaign["id"],
                "principal_id": target,
                "payload": {
                    "actor_id": actor["id"],
                    "can_control": True,
                    "can_view_private": True,
                },
                "by_principal_id": "system:local",
            },
        )
        exposure = server.exposure_registry.open(
            session_key="target-session",
            principal_id=target,
            campaign_id=campaign["id"],
            phase="lobby",
        )

        class Session:
            notifications = 0

            async def send_tool_list_changed(self) -> None:
                self.notifications += 1

        session = Session()
        server._sessions["target-session"] = session
        await server._refresh("target-session", campaign["id"])
        before = exposure.authorization_fingerprint
        await call(
            "access_grant",
            {
                "scope": "actor",
                "campaign_id": campaign["id"],
                "principal_id": target,
                "payload": {"actor_id": actor["id"], "can_view_private": False},
                "by_principal_id": "system:local",
            },
        )

        assert await server._refresh("target-session", campaign["id"]) is True
        assert exposure.authorization_fingerprint != before
        assert session.notifications == 1

    asyncio.run(exercise())


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


def test_membership_revoke_crops_loaded_tools_and_notifies_session(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(
            McpConfig(
                home=tmp_path / "home",
                database_url=None,
                chroma_url=None,
                chroma_path_override=None,
                dnd_skills_dir=tmp_path / "dnd",
                modulegen_skills_dir=tmp_path / "modulegen",
                auto_seed_rules=False,
            )
        )

        async def call(name: str, arguments: dict):
            _, result = await server.call_tool(name, arguments)
            return result.get("result", result) if isinstance(result, dict) else result

        campaign = await call(
            "campaign_create",
            {"name": "Membership refresh", "idempotency_key": "campaign"},
        )
        principal_id = "player:removed-exposure"
        await call(
            "access_grant",
            {
                "scope": "campaign",
                "campaign_id": campaign["id"],
                "principal_id": principal_id,
                "payload": {"role": "player"},
                "by_principal_id": "system:local",
            },
        )
        exposure = server.exposure_registry.open(
            session_key="removed-session",
            principal_id=principal_id,
            campaign_id=campaign["id"],
            phase="lobby",
        )
        server.exposure_registry.set_tools(exposure, add=["character_query"])

        class Session:
            notifications = 0

            async def send_tool_list_changed(self) -> None:
                self.notifications += 1

        native_session = Session()
        server._sessions["removed-session"] = native_session
        await call(
            "access_revoke",
            {
                "campaign_id": campaign["id"],
                "principal_id": principal_id,
                "by_principal_id": "system:local",
            },
        )

        assert await server._refresh("removed-session", campaign["id"]) is True
        assert exposure.loaded_tools == set()
        assert native_session.notifications == 1
        with pytest.raises(Exception, match="cannot access campaign"):
            await call(
                "campaign_query",
                {
                    "view": "get",
                    "payload": {"campaign_id": campaign["id"]},
                    "principal_id": principal_id,
                },
            )

    asyncio.run(exercise())


def test_same_server_runs_two_campaign_sessions_without_cross_talk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        server = create_server(
            McpConfig(
                home=tmp_path / "home",
                database_url=None,
                chroma_url=None,
                chroma_path_override=None,
                dnd_skills_dir=tmp_path / "dnd",
                modulegen_skills_dir=tmp_path / "modulegen",
                auto_seed_rules=False,
            )
        )

        async def direct(name: str, arguments: dict) -> dict:
            _, structured = await server.call_tool(name, arguments)
            return structured.get("result", structured)

        campaign_a = await direct(
            "campaign_create",
            {"name": "Parallel campaign A", "idempotency_key": "campaign-a"},
        )
        campaign_b = await direct(
            "campaign_create",
            {"name": "Parallel campaign B", "idempotency_key": "campaign-b"},
        )
        await direct(
            "game_phase",
            {
                "campaign_id": campaign_a["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": campaign_a["revision"],
                "idempotency_key": "campaign-a-play",
            },
        )

        class Session:
            def __init__(self) -> None:
                self.notifications = 0

            async def send_tool_list_changed(self) -> None:
                self.notifications += 1

        session_a = Session()
        session_b = Session()
        request_session: ContextVar[tuple[str, Session]] = ContextVar("request_session")

        def current_request() -> tuple[str, Session]:
            key, session = request_session.get()
            server._sessions[key] = session
            return key, session

        monkeypatch.setattr(server, "_request_session", current_request)

        async def session_call(
            key: str, session: Session, name: str, arguments: dict
        ) -> dict:
            token = request_session.set((key, session))
            try:
                _, structured = await server.call_tool(name, arguments)
                return structured.get("result", structured)
            finally:
                request_session.reset(token)

        async def session_tools(key: str, session: Session) -> set[str]:
            token = request_session.set((key, session))
            try:
                return {tool.name for tool in await server.list_tools()}
            finally:
                request_session.reset(token)

        assert await asyncio.gather(
            session_tools("session:a", session_a),
            session_tools("session:b", session_b),
        ) == [set(CORE_TOOLS), set(CORE_TOOLS)]

        await asyncio.gather(
            session_call(
                "session:a",
                session_a,
                "exposure",
                {
                    "action": "open",
                    "campaign_id": campaign_a["id"],
                    "principal_id": "system:local",
                },
            ),
            session_call(
                "session:b",
                session_b,
                "exposure",
                {
                    "action": "open",
                    "campaign_id": campaign_b["id"],
                    "principal_id": "system:local",
                },
            ),
        )
        await asyncio.gather(
            session_call(
                "session:a",
                session_a,
                "exposure",
                {
                    "action": "set",
                    "add_tool_ids": ["character_check"],
                    "principal_id": "system:local",
                },
            ),
            session_call(
                "session:b",
                session_b,
                "exposure",
                {
                    "action": "set",
                    "add_tool_ids": ["character_create_from"],
                    "principal_id": "system:local",
                },
            ),
        )
        assert "character_check" in await session_tools("session:a", session_a)
        assert "character_create_from" not in await session_tools("session:a", session_a)
        assert "character_create_from" in await session_tools("session:b", session_b)
        assert "character_check" not in await session_tools("session:b", session_b)

        # Holding campaign A's write lock must not serialize a public mutation for B.
        campaign_a_lock = server._campaign_lock(campaign_a["id"])
        await campaign_a_lock.acquire()
        try:
            actor_b = await asyncio.wait_for(
                session_call(
                    "session:b",
                    session_b,
                    "character_create_from",
                    {
                        "mode": "direct",
                        "payload": {
                            "campaign_id": campaign_b["id"],
                            "name": "Parallel B Hero",
                            "sheet": default_character_sheet(),
                        },
                        "idempotency_key": "campaign-b-actor",
                    },
                ),
                timeout=5,
            )
        finally:
            campaign_a_lock.release()
        assert actor_b["campaign_id"] == campaign_b["id"]

        binding_a, binding_b = await asyncio.gather(
            session_call(
                "session:a",
                session_a,
                "campaign_query",
                {"view": "get", "payload": {"campaign_id": campaign_a["id"]}},
            ),
            session_call(
                "session:b",
                session_b,
                "campaign_query",
                {"view": "get", "payload": {"campaign_id": campaign_b["id"]}},
            ),
        )
        assert binding_a["host_context_binding"]["campaign_id"] == campaign_a["id"]
        assert binding_b["host_context_binding"]["campaign_id"] == campaign_b["id"]
        assert len(binding_a["host_context_binding"]["authorization_fingerprint"]) == 64
        assert (
            binding_a["host_context_binding"]["context_epoch"]
            != binding_b["host_context_binding"]["context_epoch"]
        )

        session_a.notifications = 0
        session_b.notifications = 0
        entered_b = await session_call(
            "session:b",
            session_b,
            "game_phase",
            {
                "campaign_id": campaign_b["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": binding_b["revision"],
                "idempotency_key": "campaign-b-play",
            },
        )
        assert entered_b["tool_profile"] == "play"
        assert session_b.notifications >= 1
        assert session_a.notifications == 0
        assert "character_create_from" not in await session_tools("session:b", session_b)
        assert "character_check" in await session_tools("session:a", session_a)

        await session_call(
            "session:b",
            session_b,
            "exposure",
            {
                "action": "set",
                "add_tool_ids": ["character_query"],
                "principal_id": "system:local",
            },
        )
        queried_b = await session_call(
            "session:b",
            session_b,
            "character_query",
            {"view": "get", "payload": {"character_id": actor_b["id"]}},
        )
        assert queried_b["id"] == actor_b["id"]
        assert queried_b["campaign_id"] == campaign_b["id"]

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
                    {
                        "action": "open",
                        "campaign_id": "",
                        "query": "",
                        "add_tool_ids": [],
                        "remove_tool_ids": [],
                        "principal_id": principal_id,
                    },
                )
                assert not opened.isError
                opened_payload = json.loads(opened.content[0].text)
                assert opened_payload["campaign_id"] is None
                assert {tool.name for tool in (await session.list_tools()).tools} == set(CORE_TOOLS)
                broad = await session.call_tool(
                    "exposure",
                    {
                        "action": "search",
                        "query": "campaign_create system_list",
                        "principal_id": principal_id,
                    },
                )
                broad_payload = json.loads(broad.content[0].text)
                assert broad_payload["matches"] == []
                assert broad_payload["query_semantics"] == "all_terms_match_one_tool"
                assert "Retry with one short capability phrase" in broad_payload["next"]
                exact = await session.call_tool(
                    "exposure",
                    {
                        "action": "search",
                        "query": "campaign_create",
                        "principal_id": principal_id,
                    },
                )
                exact_payload = json.loads(exact.content[0].text)
                assert [item["tool_id"] for item in exact_payload["matches"]] == [
                    "campaign_create"
                ]
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

                repeated = await session.call_tool(
                    "exposure",
                    {
                        "action": "open",
                        "campaign_id": second_campaign_id,
                        "principal_id": principal_id,
                    },
                )
                assert repeated.isError
                assert "already bound" in repeated.content[0].text
                retained = await session.call_tool(
                    "exposure",
                    {"action": "get", "principal_id": principal_id},
                )
                assert not retained.isError
                assert json.loads(retained.content[0].text)["exposure_id"] == rebound_payload[
                    "exposure_id"
                ]

    asyncio.run(exercise())


def test_stdio_player_loads_only_player_safe_module_and_continuity_projections(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = tmp_path / "private-module.md"
        source.write_text(
            "# Secret Keep\n## Hidden Vault\n#### A1. Reliquary\nThe crown is cursed.",
            encoding="utf-8",
        )
        config = McpConfig(
            home=tmp_path / "home",
            database_url=None,
            chroma_url=None,
            chroma_path_override=None,
            dnd_skills_dir=tmp_path / "dnd",
            modulegen_skills_dir=tmp_path / "modulegen",
            module_import_roots=(tmp_path,),
            auto_seed_rules=False,
        )
        server = create_server(config)

        async def call(name: str, arguments: dict):
            _, result = await server.call_tool(name, arguments)
            return result.get("result", result) if isinstance(result, dict) else result

        campaign = await call(
            "campaign_create",
            {"name": "Player-safe projections", "idempotency_key": "campaign"},
        )
        actors = []
        for index in range(2):
            actors.append(
                await call(
                    "character_create_from",
                    {
                        "mode": "direct",
                        "payload": {
                            "campaign_id": campaign["id"],
                            "name": f"Combatant {index + 1}",
                        },
                        "idempotency_key": f"actor-{index + 1}",
                    },
                )
            )
        staged = await call(
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "private-module",
                    "title": "Private Module",
                },
                "idempotency_key": "module:start",
            },
        )
        installed = await finalize_and_activate_module(
            lambda _server, name, arguments: call(name, arguments),
            server,
            campaign["id"],
            staged,
            source_key="private-module",
            title="Private Module",
            portable_id="dnd5e.module.player-safe-projection",
        )
        module_id = installed["imported"]["module_id"]
        player_id = "player:projection"
        await call(
            "access_grant",
            {
                "scope": "campaign",
                "campaign_id": campaign["id"],
                "principal_id": player_id,
                "payload": {"role": "player"},
            },
        )
        current = await call(
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        await call(
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": current["revision"],
                "idempotency_key": "enter-play",
            },
        )

        env = dict(os.environ)
        env.update(
            {
                "SAGASMITH_DND_MCP_HOME": str(config.home),
                "SAGASMITH_DND_MCP_AUTO_SEED": "0",
                "SAGASMITH_DND_MCP_MODULE_IMPORT_ROOTS": str(tmp_path),
                "SAGASMITH_DND_MCP_BOUND_PRINCIPAL_ID": player_id,
            }
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "sagasmith_dnd_mcp.server"],
            cwd=Path(__file__).parents[1],
            env=env,
        )
        player_tools = {"module_query", "module_search", "continuity_context"}

        def response_payload(response):
            structured = getattr(response, "structuredContent", None)
            if structured is not None:
                return structured
            return json.loads(response.content[0].text)

        def response_result(response):
            payload = response_payload(response)
            return payload.get("result", payload) if isinstance(payload, dict) else payload

        async def assert_player_projection(expected_phase: str) -> None:
            notifications: list[str] = []

            async def on_message(message) -> None:
                notifications.append(type(getattr(message, "root", message)).__name__)

            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, message_handler=on_message) as session:
                    initialized = await session.initialize()
                    assert initialized.capabilities.tools.listChanged is True
                    opened = await session.call_tool(
                        "exposure",
                        {"action": "open", "campaign_id": campaign["id"]},
                    )
                    assert not opened.isError
                    assert response_payload(opened)["phase"] == expected_phase
                    for tool_id in sorted(player_tools):
                        searched = await session.call_tool(
                            "exposure",
                            {
                                "action": "search",
                                "campaign_id": campaign["id"],
                                "query": tool_id,
                            },
                        )
                        assert not searched.isError
                        matches = response_payload(searched)["matches"]
                        assert [(item["tool_id"], item["roles"]) for item in matches] == [
                            (tool_id, [])
                        ]

                    notifications.clear()
                    loaded = await session.call_tool(
                        "exposure",
                        {
                            "action": "set",
                            "campaign_id": campaign["id"],
                            "add_tool_ids": sorted(player_tools),
                        },
                    )
                    assert not loaded.isError
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    assert "ToolListChangedNotification" in notifications
                    assert player_tools <= {
                        tool.name for tool in (await session.list_tools()).tools
                    }

                    listed = await session.call_tool(
                        "module_query",
                        {"campaign_id": campaign["id"], "view": "list", "payload": {}},
                    )
                    assert not listed.isError
                    modules = response_result(listed)
                    assert len(modules) == 1
                    assert "source_path" not in modules[0]
                    assert "metadata" not in modules[0]

                    indexed = await session.call_tool(
                        "module_query",
                        {
                            "campaign_id": campaign["id"],
                            "view": "index",
                            "payload": {"module_id": module_id},
                        },
                    )
                    assert not indexed.isError
                    assert response_result(indexed) == []
                    searched = await session.call_tool(
                        "module_search",
                        {
                            "campaign_id": campaign["id"],
                            "query": "cursed crown",
                            "module_ids": [module_id],
                        },
                    )
                    assert not searched.isError
                    assert response_result(searched) == []
                    context = await session.call_tool(
                        "continuity_context",
                        {
                            "campaign_id": campaign["id"],
                            "audience": "player",
                            "purpose": "general",
                            "scope_id": "party",
                        },
                    )
                    assert not context.isError
                    assert response_result(context)["module_evidence"] == []

                    for view in ("content", "assets", "candidates"):
                        private_view = await session.call_tool(
                            "module_query",
                            {
                                "campaign_id": campaign["id"],
                                "view": view,
                                "payload": {"module_id": module_id},
                            },
                        )
                        assert private_view.isError
                        assert "cannot access campaign" in private_view.content[0].text
                    dm_context = await session.call_tool(
                        "continuity_context",
                        {
                            "campaign_id": campaign["id"],
                            "audience": "dm",
                            "purpose": "source_interpretation",
                            "scope_id": "party",
                        },
                    )
                    assert dm_context.isError
                    assert "only to Owner/DM" in dm_context.content[0].text

        await assert_player_projection("play")
        current = await call(
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        await call(
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "positioning_mode": "agent",
                "participant_ids": [actor["id"] for actor in actors],
                "participant_config": [
                    {"actor_id": actors[0]["id"], "initiative": 20},
                    {"actor_id": actors[1]["id"], "initiative": 10},
                ],
                "expected_revision": current["revision"],
                "idempotency_key": "combat:start",
            },
        )
        await assert_player_projection("combat")

    asyncio.run(exercise())


def test_stdio_play_transition_removes_character_creation_and_rejects_stale_call(
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
                principal_id = "discord:lobby-builder"
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
                    {"name": "Lobby creation boundary", "idempotency_key": "create"},
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
                loaded = await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": ["character_create_from"],
                        "principal_id": principal_id,
                    },
                )
                assert not loaded.isError
                assert "character_create_from" in {
                    tool.name for tool in (await session.list_tools()).tools
                }
                current = await session.call_tool(
                    "campaign_query",
                    {
                        "view": "get",
                        "payload": {"campaign_id": campaign_id},
                        "principal_id": principal_id,
                    },
                )
                revision = json.loads(current.content[0].text)["result"]["revision"]
                notifications.clear()

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
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert "ToolListChangedNotification" in notifications
                assert "character_create_from" not in {
                    tool.name for tool in (await session.list_tools()).tools
                }

                stale_call = await session.call_tool(
                    "character_create_from",
                    {
                        "mode": "direct",
                        "payload": {"campaign_id": campaign_id, "name": "Late Actor"},
                        "idempotency_key": "late-actor",
                    },
                )
                assert stale_call.isError

                stale_load = await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": ["character_create_from"],
                        "principal_id": principal_id,
                    },
                )
                assert stale_load.isError

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


def test_stdio_redo_to_lobby_requires_reloading_snapshot_restore(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        notifications: list[str] = []

        async def on_message(message) -> None:
            notifications.append(type(getattr(message, "root", message)).__name__)

        async def settle_notifications() -> None:
            await asyncio.sleep(0)
            await asyncio.sleep(0)

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
                initialized = await session.initialize()
                assert initialized.capabilities.tools.listChanged is True
                principal_id = "discord:redo-snapshot-recovery"
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
                    {"name": "Redo snapshot recovery", "idempotency_key": "create"},
                )
                assert not created.isError
                campaign_id = json.loads(created.content[0].text)["id"]
                await session.call_tool(
                    "exposure",
                    {
                        "action": "open",
                        "campaign_id": campaign_id,
                        "principal_id": principal_id,
                    },
                )
                loaded = await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": ["snapshot_create", "state_revision"],
                        "principal_id": principal_id,
                    },
                )
                assert not loaded.isError
                current = await session.call_tool(
                    "campaign_query",
                    {
                        "view": "get",
                        "payload": {"campaign_id": campaign_id},
                        "principal_id": principal_id,
                    },
                )
                revision = json.loads(current.content[0].text)["result"]["revision"]
                checkpoint_result = await session.call_tool(
                    "snapshot_create",
                    {
                        "campaign_id": campaign_id,
                        "label": "Initial lobby",
                        "expected_revision": revision,
                        "expected_head_snapshot_id": "",
                        "idempotency_key": "initial-lobby-snapshot",
                    },
                )
                assert not checkpoint_result.isError
                checkpoint = json.loads(checkpoint_result.content[0].text)

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
                in_play = await session.call_tool(
                    "campaign_query",
                    {
                        "view": "get",
                        "payload": {"campaign_id": campaign_id},
                        "principal_id": principal_id,
                    },
                )
                play_revision = json.loads(in_play.content[0].text)["result"]["revision"]
                returned = await session.call_tool(
                    "game_phase",
                    {
                        "campaign_id": campaign_id,
                        "action": "set",
                        "tool_profile": "lobby",
                        "expected_revision": play_revision,
                        "idempotency_key": "return-lobby",
                    },
                )
                assert not returned.isError
                loaded_restore = await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": ["snapshot_restore"],
                        "principal_id": principal_id,
                    },
                )
                assert not loaded_restore.isError
                assert "snapshot_restore" in {
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
                return_lobby_sequence = json.loads(history.content[0].text)["result"][0][
                    "sequence"
                ]
                await settle_notifications()
                notifications.clear()

                undone = await session.call_tool(
                    "state_revision",
                    {
                        "campaign_id": campaign_id,
                        "action": "undo",
                        "payload": {
                            "expected_history_sequence": return_lobby_sequence,
                        },
                        "idempotency_key": "undo-return-lobby",
                    },
                )
                assert not undone.isError
                await settle_notifications()
                assert "ToolListChangedNotification" in notifications
                play_tools = {tool.name for tool in (await session.list_tools()).tools}
                assert "snapshot_restore" not in play_tools
                assert "state_revision" in play_tools
                undone_campaign = await session.call_tool(
                    "campaign_query",
                    {
                        "view": "get",
                        "payload": {"campaign_id": campaign_id},
                        "principal_id": principal_id,
                    },
                )
                assert json.loads(undone_campaign.content[0].text)["result"]["state"][
                    "game_phase"
                ] == "play"
                undone_history = await session.call_tool(
                    "state_revision",
                    {
                        "campaign_id": campaign_id,
                        "action": "history",
                        "payload": {},
                    },
                )
                redo_cursor = next(
                    item["sequence"]
                    for item in json.loads(undone_history.content[0].text)["result"]
                    if item["applied"]
                )
                await settle_notifications()
                notifications.clear()

                redone = await session.call_tool(
                    "state_revision",
                    {
                        "campaign_id": campaign_id,
                        "action": "redo",
                        "payload": {"expected_history_sequence": redo_cursor},
                        "idempotency_key": "redo-return-lobby",
                    },
                )
                assert not redone.isError
                await settle_notifications()
                assert "ToolListChangedNotification" in notifications
                lobby_tools = {tool.name for tool in (await session.list_tools()).tools}
                assert "snapshot_restore" not in lobby_tools
                assert "state_revision" in lobby_tools
                redone_campaign = await session.call_tool(
                    "campaign_query",
                    {
                        "view": "get",
                        "payload": {"campaign_id": campaign_id},
                        "principal_id": principal_id,
                    },
                )
                redone_payload = json.loads(redone_campaign.content[0].text)["result"]
                assert redone_payload["state"]["game_phase"] == "lobby"

                searched = await session.call_tool(
                    "exposure",
                    {
                        "action": "search",
                        "query": "snapshot_restore",
                        "principal_id": principal_id,
                    },
                )
                assert not searched.isError
                search_payload = json.loads(searched.content[0].text)
                assert [item["tool_id"] for item in search_payload["matches"]] == [
                    "snapshot_restore"
                ]
                assert search_payload["matches"][0]["loaded"] is False
                await settle_notifications()
                notifications.clear()
                reloaded = await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": ["snapshot_restore"],
                        "principal_id": principal_id,
                    },
                )
                assert not reloaded.isError
                await settle_notifications()
                assert "ToolListChangedNotification" in notifications
                assert "snapshot_restore" in {
                    tool.name for tool in (await session.list_tools()).tools
                }

                restored = await session.call_tool(
                    "snapshot_restore",
                    {
                        "campaign_id": campaign_id,
                        "slot": checkpoint["slot"],
                        "expected_revision": redone_payload["revision"],
                        "expected_branch_id": checkpoint["branch_id"],
                        "idempotency_key": "restore-initial-lobby",
                    },
                )
                assert not restored.isError
                resumed = await session.call_tool(
                    "campaign_query",
                    {
                        "view": "get",
                        "payload": {"campaign_id": campaign_id},
                        "principal_id": principal_id,
                    },
                )
                assert not resumed.isError
                resumed_payload = json.loads(resumed.content[0].text)["result"]
                assert resumed_payload["state"]["game_phase"] == "lobby"

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
