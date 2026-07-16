from __future__ import annotations

import asyncio
import random
from pathlib import Path

import pytest

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


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
        return result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Preparation", "edition": "2024", "idempotency_key": "prep-campaign"},
        )
        ranger = await call(
            server,
            "character_create",
            {
                "name": "Ranger",
                "campaign_id": campaign["id"],
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
            "character_spell_prepare_list",
            {
                "character_id": ranger["id"],
                "spell_ids": ["a", "b"],
                "event": "setup",
                "expected_revision": ranger["revision"],
                "idempotency_key": "prep-setup",
            },
        )
        ranger = prepared["character"]
        campaign = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
        await call(
            server,
            "game_phase_set",
            {
                "campaign_id": campaign["id"],
                "tool_profile": "play",
                "expected_revision": campaign["revision"],
                "idempotency_key": "prep-play",
            },
        )
        with pytest.raises(Exception):
            await call(
                server,
                "character_spell_prepare",
                {
                    "character_id": ranger["id"],
                    "spell_id": "c",
                    "prepared": True,
                    "expected_revision": ranger["revision"],
                    "idempotency_key": "prep-live-toggle",
                },
            )
        rested = await call_raw(
            server,
            "character_rest",
            {
                "character_id": ranger["id"],
                "rest_type": "long_rest",
                "prepared_spell_ids": ["a", "c"],
                "expected_revision": ranger["revision"],
                "idempotency_key": "prep-rest",
            },
        )
        assert rested["preparation"]["added"] == ["c"]
        assert rested["preparation"]["removed"] == ["b"]
        assert rested["character"]["sheet"]["spellcasting"]["preparation"][
            "selected_spell_ids"
        ] == ["a", "c"]
        preparation_receipts = await call(
            server,
            "campaign_rule_receipts",
            {
                "campaign_id": campaign["id"],
                "mechanic_id": "dnd5e.core.spell.preparation",
            },
        )
        assert preparation_receipts[0]["event"] == "spell.prepare.long_rest"
        with pytest.raises(Exception):
            await call(
                server,
                "character_rest",
                {
                    "character_id": ranger["id"],
                    "rest_type": "long_rest",
                    "prepared_spell_ids": ["b", "d"],
                    "expected_revision": rested["character"]["revision"],
                    "idempotency_key": "prep-rest-too-many",
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
        current = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
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
            "character_create",
            {
                "name": "Branch-only witness",
                "campaign_id": campaign["id"],
                "character_type": "npc",
                "idempotency_key": "actor",
            },
        )
        await call(
            server,
            "actor_knowledge_add",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "knowledge_key": "branch-secret",
                "proposition": "Only this branch contains the witness.",
                "idempotency_key": "knowledge",
            },
        )
        current = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
        main_branch = next(
            item
            for item in await call(server, "branch_list", {"campaign_id": campaign["id"]})
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
        current = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
        await call(
            server,
            "branch_create",
            {
                "campaign_id": campaign["id"],
                "name": "before-actor",
                "from_snapshot_id": base["id"],
                "checkout": True,
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
            for item in await call(server, "branch_list", {"campaign_id": campaign["id"]})
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
            "campaign_update",
            {
                "campaign_id": campaign["id"],
                "description": "This change has not been saved.",
                "expected_revision": campaign["revision"],
                "idempotency_key": "change",
            },
        )

        with pytest.raises(Exception, match="unsaved changes"):
            await call(
                server,
                "branch_create",
                {
                    "campaign_id": campaign["id"],
                    "name": "must-not-remain",
                    "from_snapshot_id": saved["id"],
                    "checkout": True,
                    "expected_revision": changed["revision"],
                    "expected_branch_id": branch["id"],
                    "idempotency_key": "dirty-branch",
                },
            )
        branches = await call(server, "branch_list", {"campaign_id": campaign["id"]})
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
        return result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Ready Spell", "idempotency_key": "ready-campaign"},
        )
        caster = await call(
            server,
            "character_create",
            {
                "name": "Caster",
                "campaign_id": campaign["id"],
                "idempotency_key": "ready-caster",
            },
        )
        target = await call(
            server,
            "character_create",
            {
                "name": "Target",
                "campaign_id": campaign["id"],
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
        campaign = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
        started = await call(
            server,
            "combat_start",
            {
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
            "combat_ready_spell",
            {
                "campaign_id": campaign["id"],
                "actor_id": caster["id"],
                "spell_id": "magic-missile",
                "trigger": "the target moves",
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "ready-arm",
            },
        )
        readied_id = armed["readied"]["id"]
        caster_after_arm = await call(server, "character_get", {"character_id": caster["id"]})
        assert caster_after_arm["sheet"]["spellcasting"]["spell_slots"]["1"]["value"] == 0
        assert any(
            effect["active"] and effect["kind"] == "readied_spell"
            for effect in caster_after_arm["sheet"]["effects"]
        )

        triggered = await call_raw(
            server,
            "combat_readied_spell_trigger",
            {
                "campaign_id": campaign["id"],
                "readied_id": readied_id,
                "event": "the target moves",
                "expected_revision": armed["campaign_revision"],
                "idempotency_key": "ready-trigger-1",
            },
        )
        declined = await call_raw(
            server,
            "combat_readied_spell_resolve",
            {
                "campaign_id": campaign["id"],
                "actor_id": caster["id"],
                "choice_id": triggered["choice"]["id"],
                "release": False,
                "expected_revision": triggered["campaign_revision"],
                "idempotency_key": "ready-decline",
            },
        )
        assert declined["status"] == "armed"
        triggered_again = await call_raw(
            server,
            "combat_readied_spell_trigger",
            {
                "campaign_id": campaign["id"],
                "readied_id": readied_id,
                "event": "the target moves again",
                "expected_revision": declined["campaign_revision"],
                "idempotency_key": "ready-trigger-2",
            },
        )
        released = await call_raw(
            server,
            "combat_readied_spell_resolve",
            {
                "campaign_id": campaign["id"],
                "actor_id": caster["id"],
                "choice_id": triggered_again["choice"]["id"],
                "release": True,
                "declaration": {"target_id": target["id"]},
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
        caster_after_release = await call(server, "character_get", {"character_id": caster["id"]})
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
            "combat_ready_spell",
            {
                "campaign_id": campaign["id"],
                "actor_id": caster["id"],
                "spell_id": "fire-bolt",
                "trigger": "the target attacks",
                "expected_revision": target_turn_ended["campaign_revision"],
                "idempotency_key": "ready-arm-cantrip",
            },
        )
        damaged = await call_raw(
            server,
            "combat_apply_damage",
            {
                "campaign_id": campaign["id"],
                "target_id": caster["id"],
                "parts": [{"amount": 60, "damage_type": "force"}],
                "expected_revision": armed_cantrip["campaign_revision"],
                "idempotency_key": "ready-concentration-damage",
            },
        )
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
        status = await call(server, "combat_status", {"campaign_id": campaign["id"]})
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
            "combat_ready_spell",
            {
                "campaign_id": campaign["id"],
                "actor_id": caster["id"],
                "spell_id": "fire-bolt",
                "trigger": "the target attacks",
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
        caster_after_expiry = await call(server, "character_get", {"character_id": caster["id"]})
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
        return result

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
            "character_create",
            {
                "name": "Mira",
                "campaign_id": campaign["id"],
                "idempotency_key": "create-mira",
            },
        )
        wallet = await call(
            server,
            "party_wallet_adjust",
            {
                "campaign_id": campaign["id"],
                "denomination": "gp",
                "amount": 10,
                "expected_revision": campaign["revision"],
                "idempotency_key": "initial-wallet",
            },
        )
        args = {
            "campaign_id": campaign["id"],
            "character_id": actor["id"],
            "denomination": "gp",
            "amount": 1,
            "direction": "withdraw",
            "expected_campaign_revision": wallet["campaign"]["revision"],
            "expected_character_revision": actor["revision"],
            "idempotency_key": "wallet-1",
        }
        first = await call(server, "party_wallet_transfer", args)
        replay = await call(server, "party_wallet_transfer", args)
        assert replay == first
        history = await call(server, "state_history", {"campaign_id": campaign["id"]})
        await call(
            server,
            "state_undo",
            {
                "campaign_id": campaign["id"],
                "expected_history_sequence": history[0]["sequence"],
                "idempotency_key": "undo-wallet-1",
            },
        )
        party = await call(server, "party_show", {"campaign_id": campaign["id"]})
        restored = await call(server, "character_get", {"character_id": actor["id"]})
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
            "character_create",
            {
                "name": "Secret NPC",
                "campaign_id": campaign["id"],
                "character_type": "npc",
                "idempotency_key": "create-secret-npc",
            },
        )
        await call(
            server,
            "campaign_member_grant",
            {
                "campaign_id": campaign["id"],
                "principal_id": "player:alice",
                "role": "player",
            },
        )
        await call(
            server,
            "actor_knowledge_add",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "knowledge_key": "secret",
                "proposition": "The crown is fake.",
                "idempotency_key": "knowledge-secret",
            },
        )
        with pytest.raises(Exception):
            await call(
                server,
                "actor_knowledge_list",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": actor["id"],
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
        return result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Combat", "idempotency_key": "create-combat"},
        )
        first = await call(
            server,
            "character_create",
            {
                "name": "One",
                "campaign_id": campaign["id"],
                "idempotency_key": "create-combat-one",
            },
        )
        second = await call(
            server,
            "character_create",
            {
                "name": "Two",
                "campaign_id": campaign["id"],
                "idempotency_key": "create-combat-two",
            },
        )
        started = await call(
            server,
            "combat_start",
            {
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
            "combat_status",
            {"campaign_id": campaign["id"]},
        )
        current = status["combatants"][status["turn_index"]]["actor_id"]
        target = next(
            item["actor_id"] for item in status["combatants"] if item["actor_id"] != current
        )
        attack = await call_raw(
            server,
            "combat_resolve_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": current,
                "target_id": target,
                "action": {
                    "attack_bonus": 99,
                    "damage_expression": "1d4",
                    "damage_type": "slashing",
                },
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
                "action": {
                    "attack_bonus": 99,
                    "damage_expression": "1d4",
                    "damage_type": "slashing",
                },
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "combat-attack",
            },
        )
        assert replay == attack
        assert attack["status"] == "committed"
        await call(
            server,
            "campaign_member_grant",
            {
                "campaign_id": campaign["id"],
                "principal_id": "player:bob",
                "role": "player",
            },
        )
        player_view = await call(
            server,
            "combat_status",
            {"campaign_id": campaign["id"], "principal_id": "player:bob"},
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
        return result

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
                    "character_create",
                    {
                        "name": key.title(),
                        "campaign_id": campaign["id"],
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
        campaign = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
        started = await call(
            server,
            "combat_start",
            {
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
                        "position": {"x": 1, "y": 0},
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
        status = await call(server, "combat_status", {"campaign_id": campaign["id"]})
        rogue_state = next(
            item for item in status["combatants"] if item["actor_id"] == rogue["id"]
        )
        assert rogue_state["turn_flags"]["sneak_attack_turn_token"] == (
            attack["result"]["sneak_attack"]["turn_token"]
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
            {"name": "Map", "idempotency_key": "map-campaign"},
        )
        mover = await call(
            server,
            "character_create",
            {"campaign_id": campaign["id"], "name": "Mover", "idempotency_key": "map-mover"},
        )
        threat = await call(
            server,
            "character_create",
            {"campaign_id": campaign["id"], "name": "Threat", "idempotency_key": "map-threat"},
        )
        artifact = await call(
            server,
            "module_write",
            {
                "name": "keep.md",
                "content": (
                    "# Keep\n## Layout\n#### A1. Gate\nA 30 by 20 foot gatehouse.\n"
                    "## Setup\nThe heroes wait near the gate.\n"
                    "## Ambush\nRaiders attack at the gate."
                ),
            },
        )
        await call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "artifact": artifact["artifact"],
                "idempotency_key": "map-module-import",
            },
        )
        scenes = await call(server, "module_index", {"campaign_id": campaign["id"]})
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
        campaign = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
        started = await call(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [mover["id"], threat["id"]],
                "participant_config": [
                    {"actor_id": mover["id"], "initiative": 20, "position": {"x": 0, "y": 0}},
                    {"actor_id": threat["id"], "initiative": 10, "position": {"x": 3, "y": 0}},
                ],
                "scene_id": scene["scene_id"],
                "battle_map": {"blocked_cells": [{"x": 1, "y": 0}]},
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
            "campaign_member_grant",
            {
                "campaign_id": campaign["id"],
                "principal_id": "player:mover",
                "role": "player",
            },
        )
        await call(
            server,
            "actor_grant",
            {
                "campaign_id": campaign["id"],
                "principal_id": "player:mover",
                "actor_id": mover["id"],
                "can_view_private": True,
            },
        )
        player_view = await call(
            server,
            "combat_status",
            {"campaign_id": campaign["id"], "principal_id": "player:mover"},
        )
        assert "blocked_cells" not in player_view["battle_map"]
        assert "world_patches" not in player_view["battle_map"]
        with pytest.raises(Exception, match="blocked"):
            await call(
                server,
                "combat_move",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": mover["id"],
                    "distance": 5,
                    "destination": {"x": 1, "y": 0},
                    "expected_revision": started["campaign_revision"],
                    "idempotency_key": "map-blocked-move",
                },
            )
        patched = await call(
            server,
            "combat_map_patch",
            {
                "campaign_id": campaign["id"],
                "patches": [{"key": "gate_open", "value": True}],
                "expected_revision": started["campaign_revision"],
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
        return result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Grid", "idempotency_key": "create-grid"},
        )
        mover = await call(
            server,
            "character_create",
            {
                "name": "Mover",
                "campaign_id": campaign["id"],
                "idempotency_key": "create-mover",
            },
        )
        threat = await call(
            server,
            "character_create",
            {
                "name": "Threat",
                "campaign_id": campaign["id"],
                "idempotency_key": "create-threat",
            },
        )
        started = await call(
            server,
            "combat_start",
            {
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
            "combat_move",
            {
                "campaign_id": campaign["id"],
                "actor_id": mover["id"],
                "distance": 15,
                "destination": {"x": 3, "y": 0},
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "grid-move",
            },
        )
        movement_receipts = await call(
            server,
            "campaign_rule_receipts",
            {"campaign_id": campaign["id"]},
        )
        movement_ids = {item["mechanic_id"] for item in movement_receipts}
        assert "dnd5e.core.reaction.opportunity_path" in movement_ids
        assert "dnd5e.core.movement.grapple_source" not in movement_ids
        reactions = await call(
            server,
            "combat_reactions",
            {"campaign_id": campaign["id"], "actor_id": threat["id"]},
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
            "character_create",
            {
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
                "idempotency_key": "create-boundary-pc",
            },
        )
        second = await call(
            server,
            "character_create",
            {
                "name": "NPC",
                "campaign_id": campaign["id"],
                "idempotency_key": "create-boundary-npc",
            },
        )
        started = await call(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [first["id"], second["id"]],
                "expected_revision": campaign["revision"],
                "idempotency_key": "start-boundary",
            },
        )
        status = await call(server, "combat_status", {"campaign_id": campaign["id"]})
        current = status["combatants"][status["turn_index"]]["actor_id"]
        other = next(
            item["actor_id"] for item in status["combatants"] if item["actor_id"] != current
        )
        with pytest.raises(Exception):
            await call(
                server,
                "character_resource_set",
                {
                    "character_id": first["id"],
                    "resource": "guard",
                    "value": 0,
                    "expected_revision": first["revision"],
                    "idempotency_key": "combat-resource-bypass",
                },
            )
        with pytest.raises(Exception):
            await call(
                server,
                "campaign_rule_profile_set",
                {
                    "campaign_id": campaign["id"],
                    "edition": "2014",
                    "expected_revision": started["campaign_revision"],
                    "idempotency_key": "combat-profile-bypass",
                },
            )
        with pytest.raises(Exception):
            await call(
                server,
                "combat_move",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": other,
                    "distance": 5,
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
            "idempotency_key": "knowledge-dm-only",
        }
        first_knowledge = await call(
            server,
            "actor_knowledge_add",
            knowledge_args,
        )
        assert await call(server, "actor_knowledge_add", knowledge_args) == first_knowledge
        assert (
            await call(
                server,
                "actor_knowledge_list",
                {"campaign_id": campaign["id"], "actor_id": second["id"]},
            )
            == []
        )
        await call(
            server,
            "campaign_member_grant",
            {
                "campaign_id": campaign["id"],
                "principal_id": "player:private",
                "role": "player",
            },
        )
        await call(
            server,
            "actor_grant",
            {
                "campaign_id": campaign["id"],
                "principal_id": "player:private",
                "actor_id": first["id"],
                "can_view_private": True,
            },
        )
        visible = await call(
            server,
            "actor_knowledge_list",
            {
                "campaign_id": campaign["id"],
                "actor_id": first["id"],
                "principal_id": "player:private",
            },
        )
        assert visible == []

    asyncio.run(exercise())
