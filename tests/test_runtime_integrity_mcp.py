from __future__ import annotations

import asyncio
import random
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from sagasmith_core import IdempotencyService
from sagasmith_dnd.character_schema import default_character_sheet

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from tests.authoring_helpers import finalize_and_activate_module


def test_response_receipt_failure_rolls_back_the_state_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Crash recovery", "edition": "2014", "idempotency_key": "campaign"},
        )
        no_op = await call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "lobby",
                "expected_revision": campaign["revision"],
                "idempotency_key": "already-lobby",
            },
        )
        assert no_op["changed"] is False
        assert no_op["campaign_revision"] == campaign["revision"]
        assert no_op["revisions"] == []
        arguments = {
            "campaign_id": campaign["id"],
            "action": "set",
            "tool_profile": "play",
            "expected_revision": campaign["revision"],
            "idempotency_key": "phase-after-commit",
        }
        original_remember = IdempotencyService.remember_write_in_session
        failed = False

        def fail_in_transaction(
            self,
            session,
            *,
            campaign_id,
            key,
            write,
            result,
            mutation_group_id=None,
        ):
            nonlocal failed
            if key == "phase-after-commit" and not failed:
                failed = True
                raise RuntimeError("simulated atomic receipt failure")
            return original_remember(
                self,
                session,
                campaign_id=campaign_id,
                key=key,
                write=write,
                result=result,
                mutation_group_id=mutation_group_id,
            )

        monkeypatch.setattr(
            IdempotencyService,
            "remember_write_in_session",
            fail_in_transaction,
        )
        with pytest.raises(ToolError, match="simulated atomic receipt failure"):
            await call(server, "game_phase", arguments)
        unchanged = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        assert unchanged["revision"] == campaign["revision"]
        assert unchanged["state"]["game_phase"] == "lobby"
        monkeypatch.setattr(
            IdempotencyService,
            "remember_write_in_session",
            original_remember,
        )

        committed = await call(server, "game_phase", arguments)
        replay = await call(server, "game_phase", arguments)
        assert replay == committed
        with pytest.raises(ToolError, match="different request"):
            await call(
                server,
                "game_phase",
                {**arguments, "tool_profile": "lobby"},
            )

    asyncio.run(exercise())


def test_2024_prepared_spell_changes_follow_phase_and_long_rest_rules(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def call_raw(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result["result"] if isinstance(result, dict) and "action" in result else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Preparation", "edition": "2024", "idempotency_key": "prep-campaign"},
        )
        ranger = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"name": "Ranger", "campaign_id": campaign["id"]},
                "principal_id": "system:local",
                "idempotency_key": "prep-ranger",
            },
        )
        sheet = ranger["sheet"]
        sheet["progression"] = {
            "level": 5,
            "classes": [{"name": "Ranger", "level": 5, "hit_die": 10}],
        }
        sheet["spellcasting"]["preparation"] = {
            "mode": "prepared",
            "max_prepared": 6,
            "changes_on": "long_rest",
            "selected_spell_ids": [],
        }
        sheet["content"]["spells"] = [
            {
                "id": spell_id,
                "name": spell_id,
                "level": 1,
                "grant": {"source_type": "class", "source_key": "ranger"},
                "access": {"known": True},
            }
            for spell_id in ("a", "b", "c", "d")
        ]
        ranger = await call(
            server,
            "character_sheet_replace",
            {
                "character_id": ranger["id"],
                "sheet": sheet,
                "expected_revision": ranger["revision"],
                "idempotency_key": "prep-sheet",
            },
        )
        prepared = await call_raw(
            server,
            "character_spell_prepare",
            {
                "character_id": ranger["id"],
                "mode": "replace_all",
                "payload": {"spell_ids": ["a", "b"], "event": "setup"},
                "principal_id": "system:local",
                "expected_revision": ranger["revision"],
                "idempotency_key": "prep-setup",
            },
        )
        ranger = prepared["character"]
        campaign = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        await call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "principal_id": "system:local",
                "expected_revision": campaign["revision"],
                "idempotency_key": "prep-play",
            },
        )
        campaign = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        assert campaign["state"]["adventure_started"] is True
        lobby = await call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "lobby",
                "principal_id": "system:local",
                "expected_revision": campaign["revision"],
                "idempotency_key": "prep-lobby-after-start",
            },
        )
        with pytest.raises(ToolError, match="setup is closed"):
            await call(
                server,
                "character_spell_prepare",
                {
                    "character_id": ranger["id"],
                    "mode": "replace_all",
                    "payload": {"spell_ids": ["a", "b"], "event": "setup"},
                    "principal_id": "system:local",
                    "expected_revision": ranger["revision"],
                    "idempotency_key": "prep-setup-after-start",
                },
            )
        with pytest.raises(ToolError, match="initial setup only"):
            await call(
                server,
                "character_spell_prepare",
                {
                    "character_id": ranger["id"],
                    "mode": "set",
                    "payload": {"spell_id": "c", "prepared": True},
                    "principal_id": "system:local",
                    "expected_revision": ranger["revision"],
                    "idempotency_key": "prep-toggle-after-start",
                },
            )
        await call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "principal_id": "system:local",
                "expected_revision": lobby["campaign_revision"],
                "idempotency_key": "prep-resume-play",
            },
        )
        campaign = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        clock = await call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "clock_set",
                "payload": {"day": 1, "hour": 21, "minute": 0},
                "expected_revision": campaign["revision"],
                "idempotency_key": "prep-clock",
            },
        )
        with pytest.raises(Exception):
            await call(
                server,
                "character_spell_prepare",
                {
                    "character_id": ranger["id"],
                    "mode": "set",
                    "payload": {"spell_id": "c", "prepared": True},
                    "principal_id": "system:local",
                    "expected_revision": ranger["revision"],
                    "idempotency_key": "prep-live-toggle",
                },
            )
        with pytest.raises(Exception):
            await call(
                server,
                "campaign_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "party_rest",
                    "payload": {
                        "members": [
                            {
                                "character_id": ranger["id"],
                                "expected_revision": ranger["revision"],
                                "prepared_spell_ids": ["a", "missing"],
                            }
                        ]
                    },
                    "expected_revision": clock["campaign_revision"],
                    "idempotency_key": "prep-rest-too-many",
                },
            )
        rested = await call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "party_rest",
                "payload": {
                    "members": [
                        {
                            "character_id": ranger["id"],
                            "expected_revision": ranger["revision"],
                            "prepared_spell_ids": ["a", "c"],
                        }
                    ]
                },
                "expected_revision": clock["campaign_revision"],
                "idempotency_key": "prep-rest",
            },
        )
        assert rested["status"] == "committed"
        assert rested["preparations"][ranger["id"]]["added"] == ["c"]
        assert rested["preparations"][ranger["id"]]["removed"] == ["b"]
        ranger = await call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": ranger["id"]}},
        )
        assert ranger["sheet"]["spellcasting"]["preparation"]["selected_spell_ids"] == [
            "a",
            "c",
        ]
        preparation_receipts = await call(
            server,
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "receipts",
                "payload": {"mechanic_id": "dnd5e.core.spell.preparation"},
                "principal_id": "system:local",
            },
        )
        assert preparation_receipts[0]["event"] == "spell.prepare.long_rest"

    asyncio.run(exercise())


def test_new_live_campaign_actor_gets_one_initial_prepared_spell_setup(
    tmp_path: Path,
) -> None:
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

        async def raw(name: str, arguments: dict):
            _, result = await server.call_tool(name, arguments)
            return result["result"] if isinstance(result, dict) and "action" in result else result

        async def call(name: str, arguments: dict):
            result = await raw(name, arguments)
            return result.get("result", result)

        campaign = await call(
            "campaign_create",
            {
                "name": "Replacement preparation setup",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        await call(
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Original adventurer"},
                "principal_id": "system:local",
                "idempotency_key": "original",
            },
        )
        campaign = await call(
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        played = await call(
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "principal_id": "system:local",
                "expected_revision": campaign["revision"],
                "idempotency_key": "play",
            },
        )
        await call(
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "lobby",
                "principal_id": "system:local",
                "expected_revision": played["campaign_revision"],
                "idempotency_key": "lobby",
            },
        )
        replacement = await call(
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Replacement cleric"},
                "principal_id": "system:local",
                "idempotency_key": "replacement",
            },
        )
        sheet = default_character_sheet()
        sheet["progression"] = {
            "level": 1,
            "xp": 0,
            "classes": [
                {
                    "name": "Cleric",
                    "level": 1,
                    "subclass": "",
                    "hit_die": 8,
                }
            ],
        }
        sheet["spellcasting"]["preparation"] = {
            "mode": "prepared",
            "max_prepared": 2,
            "changes_on": "long_rest",
            "selected_spell_ids": [],
        }
        sheet["content"]["spells"] = [
            {
                "id": spell_id,
                "name": spell_id,
                "level": 1,
                "grant": {
                    "source_type": "class",
                    "source_key": "Cleric",
                    "method": "class_prepared",
                },
                "access": {"known": True},
            }
            for spell_id in ("bless", "cure-wounds")
        ]
        replacement = await call(
            "character_sheet_replace",
            {
                "character_id": replacement["id"],
                "sheet": sheet,
                "expected_revision": replacement["revision"],
                "idempotency_key": "replacement-sheet",
            },
        )
        prepared = await raw(
            "character_spell_prepare",
            {
                "character_id": replacement["id"],
                "mode": "replace_all",
                "payload": {"spell_ids": ["bless"], "event": "setup"},
                "principal_id": "system:local",
                "expected_revision": replacement["revision"],
                "idempotency_key": "replacement-setup",
            },
        )
        assert prepared["character"]["sheet"]["spellcasting"]["preparation"][
            "selected_spell_ids"
        ] == ["bless"]
        with pytest.raises(ToolError, match="setup is closed"):
            await raw(
                "character_spell_prepare",
                {
                    "character_id": replacement["id"],
                    "mode": "replace_all",
                    "payload": {"spell_ids": ["cure-wounds"], "event": "setup"},
                    "principal_id": "system:local",
                    "expected_revision": prepared["character"]["revision"],
                    "idempotency_key": "replacement-setup-again",
                },
            )

    asyncio.run(exercise())


def test_dm_can_read_actor_knowledge_from_a_non_current_branch_snapshot(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Historical actor view", "idempotency_key": "campaign"},
        )
        current = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        base = await call(
            server,
            "snapshot_create",
            {
                "campaign_id": campaign["id"],
                "label": "Before actor",
                "expected_revision": current["revision"],
                "expected_head_snapshot_id": "",
                "idempotency_key": "snapshot-base",
            },
        )
        actor = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "name": "Branch-only witness",
                    "campaign_id": campaign["id"],
                    "character_type": "npc",
                },
                "principal_id": "system:local",
                "idempotency_key": "actor",
            },
        )
        await call(
            server,
            "actor_knowledge_change",
            {
                "action": "add",
                "payload": {
                    "campaign_id": campaign["id"],
                    "actor_id": actor["id"],
                    "knowledge_key": "branch-secret",
                    "proposition": "Only this branch contains the witness.",
                },
                "principal_id": "system:local",
                "idempotency_key": "knowledge",
            },
        )
        current = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        main_branch = next(
            item
            for item in await call(
                server,
                "branch_query",
                {
                    "campaign_id": campaign["id"],
                    "view": "list",
                    "payload": {},
                    "principal_id": "system:local",
                },
            )
            if item["is_current"]
        )
        await call(
            server,
            "snapshot_create",
            {
                "campaign_id": campaign["id"],
                "label": "Actor exists",
                "expected_revision": current["revision"],
                "expected_head_snapshot_id": base["id"],
                "idempotency_key": "snapshot-actor",
            },
        )
        current = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        await call(
            server,
            "branch_change",
            {
                "campaign_id": campaign["id"],
                "action": "create",
                "payload": {
                    "name": "before-actor",
                    "from_snapshot_id": base["id"],
                    "checkout": True,
                },
                "principal_id": "system:local",
                "expected_revision": current["revision"],
                "expected_branch_id": main_branch["id"],
                "idempotency_key": "branch-before-actor",
            },
        )

        historical = await call(
            server,
            "actor_knowledge_query",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "view": "list",
                "payload": {"branch_id": main_branch["id"]},
            },
        )
        assert [item["knowledge_key"] for item in historical] == ["branch-secret"]
        context = await call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "branch_id": main_branch["id"],
            },
        )
        assert [item["knowledge_key"] for item in context["actor_knowledge"]] == ["branch-secret"]

    asyncio.run(exercise())


def test_branch_checkout_rejects_dirty_state_without_leaving_a_branch(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Dirty checkout", "idempotency_key": "campaign"},
        )
        branch = next(
            item
            for item in await call(
                server,
                "branch_query",
                {
                    "campaign_id": campaign["id"],
                    "view": "list",
                    "payload": {},
                    "principal_id": "system:local",
                },
            )
            if item["is_current"]
        )
        saved = await call(
            server,
            "snapshot_create",
            {
                "campaign_id": campaign["id"],
                "label": "Baseline",
                "expected_revision": campaign["revision"],
                "expected_head_snapshot_id": "",
                "idempotency_key": "snapshot",
            },
        )
        changed = await call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "payload": {"description": "This change has not been saved."},
                "principal_id": "system:local",
                "expected_revision": campaign["revision"],
                "idempotency_key": "change",
            },
        )

        with pytest.raises(Exception, match="unsaved changes"):
            await call(
                server,
                "branch_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "create",
                    "payload": {
                        "name": "must-not-remain",
                        "from_snapshot_id": saved["id"],
                        "checkout": True,
                    },
                    "principal_id": "system:local",
                    "expected_revision": changed["revision"],
                    "expected_branch_id": branch["id"],
                    "idempotency_key": "dirty-branch",
                },
            )
        branches = await call(
            server,
            "branch_query",
            {
                "campaign_id": campaign["id"],
                "view": "list",
                "payload": {},
                "principal_id": "system:local",
            },
        )
        assert [item["id"] for item in branches] == [branch["id"]]

    asyncio.run(exercise())


def test_readied_spell_lifecycle_is_atomic_and_rule_complete(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def call_raw(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result["result"] if isinstance(result, dict) and "action" in result else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Ready Spell", "idempotency_key": "ready-campaign"},
        )
        caster = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"name": "Caster", "campaign_id": campaign["id"]},
                "principal_id": "system:local",
                "idempotency_key": "ready-caster",
            },
        )
        target = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"name": "Target", "campaign_id": campaign["id"]},
                "principal_id": "system:local",
                "idempotency_key": "ready-target",
            },
        )
        sheet = caster["sheet"]
        sheet["combat"]["hp"] = {"value": 100, "max": 100, "temp": 0}
        sheet["spellcasting"]["spell_slots"] = {
            "1": {
                "label": "1st",
                "value": 1,
                "max": 1,
                "recovers_on": "long_rest",
                "source_key": "",
            }
        }
        sheet["content"]["spells"] = [
            {
                "id": "magic-missile",
                "name": "Magic Missile",
                "level": 1,
                "access": {"known": True, "prepared": True},
                "definition": {
                    "casting_time": "1 action",
                    "duration": {
                        "kind": "instantaneous",
                        "value": 0,
                        "unit": "special",
                        "concentration": False,
                    },
                },
            },
            {
                "id": "fire-bolt",
                "name": "Fire Bolt",
                "level": 0,
                "access": {"known": True},
                "definition": {
                    "casting_time": "1 action",
                    "duration": {
                        "kind": "instantaneous",
                        "value": 0,
                        "unit": "special",
                        "concentration": False,
                    },
                },
            },
        ]
        caster = await call(
            server,
            "character_sheet_replace",
            {
                "character_id": caster["id"],
                "sheet": sheet,
                "expected_revision": caster["revision"],
                "idempotency_key": "ready-sheet",
            },
        )
        campaign = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        started = await call(
            server,
            "combat_start",
            {
                "positioning_mode": "agent",
                "campaign_id": campaign["id"],
                "participant_ids": [caster["id"], target["id"]],
                "participant_config": [
                    {"actor_id": caster["id"], "initiative": 20},
                    {"actor_id": target["id"], "initiative": 10},
                ],
                "expected_revision": campaign["revision"],
                "idempotency_key": "ready-start",
            },
        )
        armed = await call_raw(
            server,
            "combat_ready",
            {
                "campaign_id": campaign["id"],
                "action": "ready_spell",
                "payload": {
                    "actor_id": caster["id"],
                    "spell_id": "magic-missile",
                    "trigger": "the target moves",
                },
                "principal_id": "system:local",
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "ready-arm",
            },
        )
        readied_id = armed["readied"]["id"]
        caster_after_arm = await call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": caster["id"]},
                "principal_id": "system:local",
            },
        )
        assert caster_after_arm["sheet"]["spellcasting"]["spell_slots"]["1"]["value"] == 0
        assert any(
            effect["active"] and effect["kind"] == "readied_spell"
            for effect in caster_after_arm["sheet"]["effects"]
        )

        triggered = await call_raw(
            server,
            "combat_ready",
            {
                "campaign_id": campaign["id"],
                "action": "trigger_spell",
                "payload": {"readied_id": readied_id, "event": "the target moves"},
                "principal_id": "system:local",
                "expected_revision": armed["campaign_revision"],
                "idempotency_key": "ready-trigger-1",
            },
        )
        declined = await call_raw(
            server,
            "combat_ready",
            {
                "campaign_id": campaign["id"],
                "action": "resolve_spell",
                "payload": {
                    "actor_id": caster["id"],
                    "choice_id": triggered["choice"]["id"],
                    "release": False,
                },
                "principal_id": "system:local",
                "expected_revision": triggered["campaign_revision"],
                "idempotency_key": "ready-decline",
            },
        )
        assert declined["status"] == "armed"
        triggered_again = await call_raw(
            server,
            "combat_ready",
            {
                "campaign_id": campaign["id"],
                "action": "trigger_spell",
                "payload": {"readied_id": readied_id, "event": "the target moves again"},
                "principal_id": "system:local",
                "expected_revision": declined["campaign_revision"],
                "idempotency_key": "ready-trigger-2",
            },
        )
        released = await call_raw(
            server,
            "combat_ready",
            {
                "campaign_id": campaign["id"],
                "action": "resolve_spell",
                "payload": {
                    "actor_id": caster["id"],
                    "choice_id": triggered_again["choice"]["id"],
                    "release": True,
                    "declaration": {"target_id": target["id"]},
                },
                "principal_id": "system:local",
                "expected_revision": triggered_again["campaign_revision"],
                "idempotency_key": "ready-release",
            },
        )
        assert released["status"] == "pending_ruling"
        assert released["combat"]["readied"] == []
        caster_combatant = next(
            item for item in released["combat"]["combatants"] if item["actor_id"] == caster["id"]
        )
        assert caster_combatant["turn_budget"]["reaction"] == 0
        caster_after_release = await call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": caster["id"]},
                "principal_id": "system:local",
            },
        )
        assert not any(
            effect["active"] and effect["kind"] == "readied_spell"
            for effect in caster_after_release["sheet"]["effects"]
        )

        caster_turn_ended = await call_raw(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": caster["id"],
                "expected_revision": released["campaign_revision"],
                "idempotency_key": "ready-end-caster",
            },
        )
        target_turn_ended = await call_raw(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": target["id"],
                "expected_revision": caster_turn_ended["campaign_revision"],
                "idempotency_key": "ready-end-target",
            },
        )
        armed_cantrip = await call_raw(
            server,
            "combat_ready",
            {
                "campaign_id": campaign["id"],
                "action": "ready_spell",
                "payload": {
                    "actor_id": caster["id"],
                    "spell_id": "fire-bolt",
                    "trigger": "the target attacks",
                },
                "principal_id": "system:local",
                "expected_revision": target_turn_ended["campaign_revision"],
                "idempotency_key": "ready-arm-cantrip",
            },
        )
        damaged = await call_raw(
            server,
            "combat_hp_change",
            {
                "campaign_id": campaign["id"],
                "target_id": caster["id"],
                "action": "damage",
                "payload": {"parts": [{"amount": 60, "damage_type": "force"}]},
                "principal_id": "system:local",
                "expected_revision": armed_cantrip["campaign_revision"],
                "idempotency_key": "ready-concentration-damage",
            },
        )
        damage_log = damaged["combat"]["log"][-1]
        assert damage_log["type"] == "damage"
        assert damage_log["result"] == damaged["result"]
        assert "sheet" not in damage_log["result"]
        concentration = next(
            item for item in damaged["combat"]["pending"] if item["kind"] == "concentration"
        )
        checked = await call_raw(
            server,
            "combat_concentration_check",
            {
                "campaign_id": campaign["id"],
                "target_id": caster["id"],
                "dc": concentration["dc"],
                "effect_ids": concentration["effect_ids"],
                "expected_revision": damaged["campaign_revision"],
                "idempotency_key": "ready-concentration-check",
            },
        )
        assert checked["result"]["success"] is False
        status = await call(
            server,
            "combat_query",
            {"campaign_id": campaign["id"], "view": "status", "principal_id": "system:local"},
        )
        assert status["readied"] == []

        after_check_caster = await call_raw(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": caster["id"],
                "expected_revision": checked["campaign_revision"],
                "idempotency_key": "ready-expiry-end-caster-1",
            },
        )
        after_check_target = await call_raw(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": target["id"],
                "expected_revision": after_check_caster["campaign_revision"],
                "idempotency_key": "ready-expiry-end-target-1",
            },
        )
        expiring = await call_raw(
            server,
            "combat_ready",
            {
                "campaign_id": campaign["id"],
                "action": "ready_spell",
                "payload": {
                    "actor_id": caster["id"],
                    "spell_id": "fire-bolt",
                    "trigger": "the target attacks",
                },
                "principal_id": "system:local",
                "expected_revision": after_check_target["campaign_revision"],
                "idempotency_key": "ready-arm-expiring",
            },
        )
        expiring_id = expiring["readied"]["id"]
        expiry_caster = await call_raw(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": caster["id"],
                "expected_revision": expiring["campaign_revision"],
                "idempotency_key": "ready-expiry-end-caster-2",
            },
        )
        expiry_target = await call_raw(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": target["id"],
                "expected_revision": expiry_caster["campaign_revision"],
                "idempotency_key": "ready-expiry-end-target-2",
            },
        )
        assert expiring_id in expiry_target["readied_spells_expired"]
        caster_after_expiry = await call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": caster["id"]},
                "principal_id": "system:local",
            },
        )
        assert not any(
            effect["active"] and effect["kind"] == "readied_spell"
            for effect in caster_after_expiry["sheet"]["effects"]
        )

    asyncio.run(exercise())


def test_party_wallet_transfer_is_one_undoable_and_idempotent(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def call_raw(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result["result"] if isinstance(result, dict) and "action" in result else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Integrity", "idempotency_key": "create-integrity"},
        )
        assert (
            await call(
                server,
                "campaign_create",
                {"name": "Integrity", "idempotency_key": "create-integrity"},
            )
            == campaign
        )
        actor = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"name": "Mira", "campaign_id": campaign["id"]},
                "principal_id": "system:local",
                "idempotency_key": "create-mira",
            },
        )
        wallet = await call(
            server,
            "wallet_change",
            {
                "owner": "party",
                "action": "adjust",
                "owner_id": campaign["id"],
                "denomination": "gp",
                "amount": 10,
                "payload": {},
                "principal_id": "system:local",
                "expected_revision": campaign["revision"],
                "idempotency_key": "initial-wallet",
            },
        )
        args = {
            "owner": "party",
            "action": "transfer_to_character",
            "owner_id": campaign["id"],
            "denomination": "gp",
            "amount": 1,
            "payload": {
                "character_id": actor["id"],
                "expected_campaign_revision": wallet["campaign"]["revision"],
                "expected_character_revision": actor["revision"],
            },
            "principal_id": "system:local",
            "idempotency_key": "wallet-1",
        }
        first = await call(server, "wallet_change", args)
        replay = await call(server, "wallet_change", args)
        assert replay == first
        history = await call(
            server,
            "state_revision",
            {
                "campaign_id": campaign["id"],
                "action": "history",
                "payload": {},
                "principal_id": "system:local",
            },
        )
        await call(
            server,
            "state_revision",
            {
                "campaign_id": campaign["id"],
                "action": "undo",
                "payload": {"expected_history_sequence": history[0]["sequence"]},
                "principal_id": "system:local",
                "idempotency_key": "undo-wallet-1",
            },
        )
        party = await call(
            server,
            "campaign_query",
            {
                "view": "party",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        restored = await call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actor["id"]},
                "principal_id": "system:local",
            },
        )
        assert party["inventory"]["wallet"]["gp"] == 10
        assert restored["sheet"]["inventory"]["wallet"]["gp"] == 0

    asyncio.run(exercise())


def test_player_cannot_read_unassigned_actor_knowledge(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Private", "idempotency_key": "create-private"},
        )
        actor = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "name": "Secret NPC",
                    "campaign_id": campaign["id"],
                    "character_type": "npc",
                },
                "principal_id": "system:local",
                "idempotency_key": "create-secret-npc",
            },
        )
        await call(
            server,
            "access_grant",
            {
                "scope": "campaign",
                "campaign_id": campaign["id"],
                "principal_id": "player:alice",
                "payload": {"role": "player"},
            },
        )
        await call(
            server,
            "actor_knowledge_change",
            {
                "action": "add",
                "payload": {
                    "campaign_id": campaign["id"],
                    "actor_id": actor["id"],
                    "knowledge_key": "secret",
                    "proposition": "The crown is fake.",
                },
                "principal_id": "system:local",
                "idempotency_key": "knowledge-secret",
            },
        )
        with pytest.raises(Exception):
            await call(
                server,
                "actor_knowledge_query",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": actor["id"],
                    "view": "list",
                    "payload": {},
                    "principal_id": "player:alice",
                },
            )

    asyncio.run(exercise())


def test_structured_combat_is_atomic_and_player_filtered(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def call_raw(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result["result"] if isinstance(result, dict) and "action" in result else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Combat", "idempotency_key": "create-combat"},
        )
        first = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"name": "One", "campaign_id": campaign["id"]},
                "principal_id": "system:local",
                "idempotency_key": "create-combat-one",
            },
        )
        second = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"name": "Two", "campaign_id": campaign["id"]},
                "principal_id": "system:local",
                "idempotency_key": "create-combat-two",
            },
        )
        started = await call(
            server,
            "combat_start",
            {
                "positioning_mode": "agent",
                "campaign_id": campaign["id"],
                "participant_ids": [first["id"], second["id"]],
                "participant_config": [
                    {"actor_id": first["id"], "initiative": 20},
                    {"actor_id": second["id"], "initiative": 10, "hidden": True},
                ],
                "idempotency_key": "combat-start",
                "expected_revision": campaign["revision"],
            },
        )
        status = await call(
            server,
            "combat_query",
            {"campaign_id": campaign["id"], "view": "status", "principal_id": "system:local"},
        )
        current = status["combatants"][status["turn_index"]]["actor_id"]
        target = next(
            item["actor_id"] for item in status["combatants"] if item["actor_id"] != current
        )
        attack_action = {
            "attack_bonus": 99,
            "damage_expression": "1d4",
            "damage_type": "slashing",
            "context": {
                "spatial_facts": {
                    "decision_id": "test-direct-attack",
                    "reason": "The target is visible and within the attack's normal range.",
                    "targetable": True,
                    "in_range": True,
                    "long_range": False,
                    "cover_degree": "none",
                    "attacker_can_see_target": True,
                    "target_can_see_attacker": True,
                    "target_within_5_ft": True,
                    "close_threat_actor_ids": [],
                    "helper_actor_ids": [],
                    "target_adjacent_ally_actor_ids": [],
                }
            },
        }
        attack = await call_raw(
            server,
            "combat_resolve_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": current,
                "target_id": target,
                "action": attack_action,
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "combat-attack",
            },
        )
        replay = await call_raw(
            server,
            "combat_resolve_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": current,
                "target_id": target,
                "action": attack_action,
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "combat-attack",
            },
        )
        assert replay == attack
        assert attack["status"] == "committed"
        await call(
            server,
            "access_grant",
            {
                "scope": "campaign",
                "campaign_id": campaign["id"],
                "principal_id": "player:bob",
                "payload": {"role": "player"},
            },
        )
        player_view = await call(
            server,
            "combat_query",
            {"campaign_id": campaign["id"], "view": "status", "principal_id": "player:bob"},
        )
        assert "log" not in player_view
        allowed = {"actor_id", "token_id", "name", "initiative", "position"}
        assert all(set(item) <= allowed for item in player_view["combatants"])
        assert second["id"] not in {item["actor_id"] for item in player_view["combatants"]}

    asyncio.run(exercise())


def test_combat_sneak_attack_persists_the_once_per_turn_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )
    real_roll_attack = server_module.roll_attack_action
    real_resolve_damage = server_module.resolve_attack_damage

    def deterministic_roll_attack(*args, **kwargs):
        kwargs["rng"] = random.Random(5)
        return real_roll_attack(*args, **kwargs)

    def deterministic_resolve_damage(*args, **kwargs):
        kwargs["rng"] = random.Random(5)
        return real_resolve_damage(*args, **kwargs)

    monkeypatch.setattr(server_module, "roll_attack_action", deterministic_roll_attack)
    monkeypatch.setattr(server_module, "resolve_attack_damage", deterministic_resolve_damage)

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def call_raw(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result["result"] if isinstance(result, dict) and "action" in result else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Sneak Attack", "edition": "2014", "idempotency_key": "sa-campaign"},
        )
        actors = []
        for key in ("rogue", "ally", "target"):
            actors.append(
                await call(
                    server,
                    "character_create_from",
                    {
                        "mode": "direct",
                        "payload": {"name": key.title(), "campaign_id": campaign["id"]},
                        "principal_id": "system:local",
                        "idempotency_key": f"sa-{key}",
                    },
                )
            )
        rogue, ally, target = actors
        rogue_sheet = rogue["sheet"]
        rogue_sheet["abilities"]["dexterity"]["score"] = 16
        rogue_sheet["progression"] = {
            "level": 6,
            "classes": [
                {"name": "Fighter", "level": 5, "hit_die": 10},
                {"name": "Rogue", "level": 1, "hit_die": 8},
            ],
        }
        rogue_sheet["combat"]["attacks_per_action"] = 2
        rogue_sheet["content"]["features"] = [
            {
                "id": "dnd5e.content.srd2014.feature.rogue-sneak-attack",
                "name": "Sneak Attack",
                "source_key": "Rogue",
            }
        ]
        rogue_sheet["inventory"]["items"] = [
            {
                "id": "dagger",
                "name": "Dagger",
                "kind": "weapon",
                "equipped": True,
                "equipped_slot": "main_hand",
                "mechanics": {
                    "category": "simple",
                    "attack_type": "melee",
                    "attack_ability": "dexterity",
                    "damage_formula": "1d4",
                    "damage_type": "piercing",
                    "properties": ["finesse", "light", "thrown"],
                },
            }
        ]
        rogue_sheet["inventory"]["equipment_slots"]["main_hand"] = "dagger"
        rogue = await call(
            server,
            "character_sheet_replace",
            {
                "character_id": rogue["id"],
                "sheet": rogue_sheet,
                "expected_revision": rogue["revision"],
                "idempotency_key": "sa-rogue-sheet",
            },
        )
        target_sheet = target["sheet"]
        target_sheet["combat"]["ac"] = {"base": 1, "override": None}
        target = await call(
            server,
            "character_sheet_replace",
            {
                "character_id": target["id"],
                "sheet": target_sheet,
                "expected_revision": target["revision"],
                "idempotency_key": "sa-target-sheet",
            },
        )
        campaign = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        started = await call(
            server,
            "combat_start",
            {
                "positioning_mode": "grid",
                "battle_map": {
                    "width_cells": 12,
                    "height_cells": 12,
                },
                "campaign_id": campaign["id"],
                "participant_ids": [rogue["id"], ally["id"], target["id"]],
                "participant_config": [
                    {
                        "actor_id": rogue["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                        "disposition": "friendly",
                    },
                    {
                        "actor_id": ally["id"],
                        "initiative": 15,
                        "position": {"x": 1, "y": 1},
                        "disposition": "friendly",
                    },
                    {
                        "actor_id": target["id"],
                        "initiative": 10,
                        "position": {"x": 1, "y": 0},
                        "disposition": "hostile",
                    },
                ],
                "expected_revision": campaign["revision"],
                "idempotency_key": "sa-start",
            },
        )
        attack = await call_raw(
            server,
            "combat_resolve_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": rogue["id"],
                "target_id": target["id"],
                "action": {"weapon_id": "dagger", "use_sneak_attack": True},
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "sa-first-attack",
            },
        )
        assert attack["result"]["sneak_attack"]["used"] is True
        status = await call(
            server,
            "combat_query",
            {"campaign_id": campaign["id"], "view": "status", "principal_id": "system:local"},
        )
        rogue_state = next(item for item in status["combatants"] if item["actor_id"] == rogue["id"])
        assert (
            rogue_state["turn_flags"]["sneak_attack_turn_token"]
            == (attack["result"]["sneak_attack"]["turn_token"])
        )
        with pytest.raises(Exception, match="already been used"):
            await call(
                server,
                "combat_resolve_attack",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": rogue["id"],
                    "target_id": target["id"],
                    "action": {"weapon_id": "dagger", "use_sneak_attack": True},
                    "expected_revision": attack["campaign_revision"],
                    "idempotency_key": "sa-second-attack",
                },
            )

    asyncio.run(exercise())


def test_module_scene_creates_a_temporary_battle_map(tmp_path: Path) -> None:
    source = tmp_path / "keep.md"
    source.write_text(
        "# Keep\n## Layout\n#### A1. Gate\nA 30 by 20 foot gatehouse.\n"
        "## Setup\nThe heroes wait near the gate.\n"
        "## Ambush\nRaiders attack at the gate.",
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

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Map", "idempotency_key": "map-campaign"},
        )
        mover = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Mover"},
                "principal_id": "system:local",
                "idempotency_key": "map-mover",
            },
        )
        threat = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Threat"},
                "principal_id": "system:local",
                "idempotency_key": "map-threat",
            },
        )
        staged = await call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "keep",
                    "title": "Keep",
                },
                "idempotency_key": "map-module:stage",
            },
        )
        await finalize_and_activate_module(
            call,
            server,
            campaign["id"],
            staged,
            source_key="keep",
            title="Keep",
            portable_id="dnd5e.module.keep-test",
        )
        scenes = await call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "index",
                "payload": {},
                "principal_id": "system:local",
            },
        )
        spatial_scene = next(item for item in scenes if item["title"] == "Layout")
        setup_scene = next(item for item in scenes if item["title"] == "Setup")
        scene = next(item for item in scenes if item["title"] == "Ambush")
        await call(
            server,
            "module_set_progress",
            {
                "campaign_id": campaign["id"],
                "scene_id": setup_scene["scene_id"],
                "current_location_key": "a1-gate",
                "state": {"location_scene_id": spatial_scene["scene_id"]},
                "expected_state_version": 0,
                "idempotency_key": "map-progress",
            },
        )
        campaign = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        started = await call(
            server,
            "combat_start",
            {
                "positioning_mode": "grid",
                "battle_map": {"width_cells": 12, "height_cells": 12},
                "campaign_id": campaign["id"],
                "participant_ids": [mover["id"], threat["id"]],
                "participant_config": [
                    {"actor_id": mover["id"], "initiative": 20, "position": {"x": 0, "y": 0}},
                    {"actor_id": threat["id"], "initiative": 10, "position": {"x": 3, "y": 0}},
                ],
                "scene_id": scene["scene_id"],
                "expected_revision": campaign["revision"],
                "idempotency_key": "map-combat-start",
            },
        )
        battle_map = started["combat"]["battle_map"]
        assert battle_map["lifecycle"] == "temporary"
        assert battle_map["source"]["scene_id"] == spatial_scene["scene_id"]
        assert battle_map["source"]["encounter_scene_id"] == scene["scene_id"]
        assert battle_map["source"]["location_key"] == "a1-gate"
        await call(
            server,
            "access_grant",
            {
                "scope": "campaign",
                "campaign_id": campaign["id"],
                "principal_id": "player:mover",
                "payload": {"role": "player"},
            },
        )
        await call(
            server,
            "access_grant",
            {
                "scope": "actor",
                "campaign_id": campaign["id"],
                "principal_id": "player:mover",
                "payload": {"actor_id": mover["id"], "can_view_private": True},
            },
        )
        player_view = await call(
            server,
            "combat_query",
            {"campaign_id": campaign["id"], "view": "status", "principal_id": "player:mover"},
        )
        assert "blocked_cells" not in player_view["battle_map"]
        assert "world_patches" not in player_view["battle_map"]
        moved = await call(
            server,
            "combat_movement",
            {
                "campaign_id": campaign["id"],
                "actor_id": mover["id"],
                "action": "move",
                "payload": {"distance": 5, "destination": {"x": 1, "y": 0}},
                "principal_id": "system:local",
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "map-open-move",
            },
        )
        moved_actor = next(
            item for item in moved["combat"]["combatants"] if item["actor_id"] == mover["id"]
        )
        assert moved_actor["position"] == {"x": 1, "y": 0}
        patched = await call(
            server,
            "combat_map_patch",
            {
                "campaign_id": campaign["id"],
                "patches": [{"key": "gate_open", "value": True}],
                "expected_revision": moved["campaign_revision"],
                "idempotency_key": "map-gate-open",
            },
        )
        assert patched["battle_map"]["world_patches"] == [{"key": "gate_open", "value": True}]
        assert patched["battle_map"]["map_revision"] == 2
        assert patched["battle_map"]["checksum"] != battle_map["checksum"]

    asyncio.run(exercise())


def test_positioned_movement_opens_and_resolves_an_owned_reaction(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def call_raw(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result["result"] if isinstance(result, dict) and "action" in result else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Grid", "idempotency_key": "create-grid"},
        )
        mover_sheet = default_character_sheet()
        mover_sheet["combat"]["hp"] = {"value": 20, "max": 20, "temp": 0}
        mover = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"name": "Mover", "campaign_id": campaign["id"], "sheet": mover_sheet},
                "principal_id": "system:local",
                "idempotency_key": "create-mover",
            },
        )
        threat = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"name": "Threat", "campaign_id": campaign["id"]},
                "principal_id": "system:local",
                "idempotency_key": "create-threat",
            },
        )
        started = await call(
            server,
            "combat_start",
            {
                "positioning_mode": "grid",
                "battle_map": {"width_cells": 12, "height_cells": 12},
                "campaign_id": campaign["id"],
                "participant_ids": [mover["id"], threat["id"]],
                "participant_config": [
                    {
                        "actor_id": mover["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                        "disposition": "friendly",
                    },
                    {
                        "actor_id": threat["id"],
                        "initiative": 10,
                        "position": {"x": 1, "y": 0},
                        "disposition": "hostile",
                        "reach_ft": 5,
                    },
                ],
                "expected_revision": campaign["revision"],
                "idempotency_key": "grid-start",
            },
        )
        moved = await call(
            server,
            "combat_movement",
            {
                "campaign_id": campaign["id"],
                "actor_id": mover["id"],
                "action": "move",
                "payload": {"distance": 15, "destination": {"x": 3, "y": 0}},
                "principal_id": "system:local",
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "grid-move",
            },
        )
        movement_receipts = await call(
            server,
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "receipts",
                "payload": {},
                "principal_id": "system:local",
            },
        )
        movement_ids = {item["mechanic_id"] for item in movement_receipts}
        assert "dnd5e.core.reaction.opportunity_path" in movement_ids
        assert "dnd5e.core.movement.grapple_source" not in movement_ids
        reactions = await call(
            server,
            "combat_query",
            {
                "campaign_id": campaign["id"],
                "view": "reactions",
                "actor_id": threat["id"],
                "principal_id": "system:local",
            },
        )
        assert reactions[0]["target_id"] == mover["id"]
        resolved = await call_raw(
            server,
            "combat_reaction_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": threat["id"],
                "choice_id": reactions[0]["id"],
                "target_id": mover["id"],
                "expected_revision": moved["campaign_revision"],
                "idempotency_key": "grid-reaction",
            },
        )
        assert resolved["status"] == "committed"
        assert not resolved["combat"]["pending"]

        before_rejected_move = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        with pytest.raises(ToolError, match="cannot willingly end"):
            await call_raw(
                server,
                "combat_movement",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": mover["id"],
                    "action": "move",
                    "payload": {"distance": 10, "destination": {"x": 1, "y": 0}},
                    "expected_revision": before_rejected_move["revision"],
                    "idempotency_key": "grid-occupied-destination",
                },
            )
        after_rejected_move = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        assert after_rejected_move["revision"] == before_rejected_move["revision"]

    asyncio.run(exercise())


def test_combat_boundaries_and_private_knowledge_filter(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Boundaries", "idempotency_key": "create-boundaries"},
        )
        first = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "name": "PC",
                    "campaign_id": campaign["id"],
                    "sheet": {
                        "resources": {
                            "guard": {
                                "label": "Guard",
                                "value": 1,
                                "max": 1,
                                "recovers_on": "none",
                                "source_key": "test",
                            }
                        }
                    },
                },
                "principal_id": "system:local",
                "idempotency_key": "create-boundary-pc",
            },
        )
        second = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"name": "NPC", "campaign_id": campaign["id"]},
                "principal_id": "system:local",
                "idempotency_key": "create-boundary-npc",
            },
        )
        started = await call(
            server,
            "combat_start",
            {
                "positioning_mode": "agent",
                "campaign_id": campaign["id"],
                "participant_ids": [first["id"], second["id"]],
                "expected_revision": campaign["revision"],
                "idempotency_key": "start-boundary",
            },
        )
        status = await call(
            server,
            "combat_query",
            {"campaign_id": campaign["id"], "view": "status", "principal_id": "system:local"},
        )
        current = status["combatants"][status["turn_index"]]["actor_id"]
        other = next(
            item["actor_id"] for item in status["combatants"] if item["actor_id"] != current
        )
        with pytest.raises(Exception):
            await call(
                server,
                "character_state_change",
                {
                    "character_id": first["id"],
                    "action": "resource_set",
                    "payload": {"resource": "guard", "value": 0},
                    "principal_id": "system:local",
                    "expected_revision": first["revision"],
                    "idempotency_key": "combat-resource-bypass",
                },
            )
        with pytest.raises(Exception):
            await call(
                server,
                "campaign_rules",
                {
                    "campaign_id": campaign["id"],
                    "action": "set_profile",
                    "payload": {"edition": "2014"},
                    "principal_id": "system:local",
                    "expected_revision": started["campaign_revision"],
                    "idempotency_key": "combat-profile-bypass",
                },
            )
        with pytest.raises(Exception):
            await call(
                server,
                "combat_movement",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": other,
                    "action": "move",
                    "payload": {
                        "distance": 5,
                        "spatial_facts": {
                            "decision_id": "test-out-of-turn-move",
                            "reason": "The destination is clear and no enemy gains a reaction.",
                            "destination_legal": True,
                            "distance_ft": 5,
                            "difficult_terrain_extra_ft": 0,
                            "moves_farther_from_turn_source": False,
                            "enters_turn_source_30_ft": False,
                            "moves_closer_to_visible_fear_source": False,
                            "opportunity_attack_actor_ids": [],
                        },
                    },
                    "principal_id": "system:local",
                    "expected_revision": started["campaign_revision"],
                    "idempotency_key": "out-of-turn-move",
                },
            )
        ended = await call(
            server,
            "combat_end",
            {
                "campaign_id": campaign["id"],
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "end-boundary",
            },
        )
        with pytest.raises(Exception):
            await call(
                server,
                "combat_end_turn",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": current,
                    "expected_revision": ended["campaign_revision"],
                    "idempotency_key": "after-end-turn",
                },
            )

        knowledge_args = {
            "campaign_id": campaign["id"],
            "actor_id": first["id"],
            "knowledge_key": "dm-only",
            "proposition": "hidden",
            "disclosure_scope": "dm",
        }
        first_knowledge = await call(
            server,
            "actor_knowledge_change",
            {
                "action": "add",
                "payload": knowledge_args,
                "principal_id": "system:local",
                "idempotency_key": "knowledge-dm-only",
            },
        )
        assert (
            await call(
                server,
                "actor_knowledge_change",
                {
                    "action": "add",
                    "payload": knowledge_args,
                    "principal_id": "system:local",
                    "idempotency_key": "knowledge-dm-only",
                },
            )
            == first_knowledge
        )
        assert (
            await call(
                server,
                "actor_knowledge_query",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": second["id"],
                    "view": "list",
                    "payload": {},
                    "principal_id": "system:local",
                },
            )
            == []
        )
        await call(
            server,
            "access_grant",
            {
                "scope": "campaign",
                "campaign_id": campaign["id"],
                "principal_id": "player:private",
                "payload": {"role": "player"},
            },
        )
        await call(
            server,
            "access_grant",
            {
                "scope": "actor",
                "campaign_id": campaign["id"],
                "principal_id": "player:private",
                "payload": {"actor_id": first["id"], "can_view_private": True},
            },
        )
        visible = await call(
            server,
            "actor_knowledge_query",
            {
                "campaign_id": campaign["id"],
                "actor_id": first["id"],
                "view": "list",
                "payload": {},
                "principal_id": "player:private",
            },
        )
        assert visible == []

    asyncio.run(exercise())
