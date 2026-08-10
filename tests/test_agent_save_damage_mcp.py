from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
import sagasmith_dnd.combat_engine as combat_engine_module
from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from tests.authoring_helpers import finalize_and_activate_module


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    value = result.get("result", result) if isinstance(result, dict) else result
    if isinstance(value, dict) and "action" in value and "result" in value:
        return value["result"]
    return value


async def _raw(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result


def test_agent_save_damage_requires_one_paid_immutable_action_and_replays(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module_root = tmp_path / "modules"
    module_root.mkdir()
    encounter_excerpt = "The dragon catches both heroes in its cone and uses Poison Breath."
    mechanic_excerpt = (
        "Poison Breath. Each creature in the area must make a DC 18 "
        "Constitution saving throw, taking 16d6 poison damage on a failed "
        "save, or half as much damage on a successful one."
    )
    source = module_root / "tower.md"
    source.write_text(
        f"# Dragon Tower\n\n## Poison Breath\n\n{encounter_excerpt}\n\n{mechanic_excerpt}\n",
        encoding="utf-8",
    )
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        module_import_roots=(module_root,),
        auto_seed_rules=False,
    )
    original_check = combat_engine_module.resolve_actor_check
    target_outcomes: dict[str, bool] = {}

    def forced_check(target_actor, **kwargs):
        result = original_check(target_actor, **kwargs)
        success = target_outcomes[str(target_actor["id"])]
        result["success"] = success
        result["total"] = int(kwargs["dc"]) if success else int(kwargs["dc"]) - 1
        return result

    monkeypatch.setattr(
        combat_engine_module,
        "resolve_actor_check",
        forced_check,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Agent save damage",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        await _call(
            server,
            "access_grant",
            {
                "scope": "campaign",
                "campaign_id": campaign["id"],
                "principal_id": "player:hero",
                "payload": {"role": "player"},
            },
        )
        staged = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "dragon-tower",
                    "title": "Dragon Tower",
                },
                "idempotency_key": "stage",
            },
        )
        await finalize_and_activate_module(
            _call,
            server,
            campaign["id"],
            staged,
            source_key="dragon-tower",
            title="Dragon Tower",
            portable_id="dnd5e.module.dragon-tower",
        )
        search = await _call(
            server,
            "module_search",
            {
                "campaign_id": campaign["id"],
                "query": "dragon catches both heroes Poison Breath",
                "top_k": 3,
            },
        )
        expanded = await _call(
            server,
            "module_expand",
            {"chunk_id": search[0]["id"]},
        )
        dragon = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Dragon",
                    "character_type": "monster",
                },
                "principal_id": "system:local",
                "idempotency_key": "dragon",
            },
        )
        agile = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Agile Hero",
                    "character_type": "pc",
                },
                "principal_id": "system:local",
                "idempotency_key": "agile",
            },
        )
        clumsy = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Clumsy Hero",
                    "character_type": "pc",
                },
                "principal_id": "system:local",
                "idempotency_key": "clumsy",
            },
        )
        dragon_sheet = default_character_sheet()
        dragon_sheet["combat"]["hp"] = {"value": 100, "max": 100, "temp": 0}
        dragon = await _call(
            server,
            "character_sheet_replace",
            {
                "character_id": dragon["id"],
                "sheet": dragon_sheet,
                "expected_revision": dragon["revision"],
                "idempotency_key": "dragon-sheet",
            },
        )
        for actor, key in ((agile, "agile-sheet"), (clumsy, "clumsy-sheet")):
            sheet = default_character_sheet()
            sheet["combat"]["hp"] = {"value": 100, "max": 100, "temp": 0}
            updated = await _call(
                server,
                "character_sheet_replace",
                {
                    "character_id": actor["id"],
                    "sheet": sheet,
                    "expected_revision": actor["revision"],
                    "idempotency_key": key,
                },
            )
            if actor["id"] == agile["id"]:
                agile = updated
            else:
                clumsy = updated
        target_outcomes.update({agile["id"]: True, clumsy["id"]: False})
        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        play = await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": current["revision"],
                "idempotency_key": "play",
            },
        )
        started = await _call(
            server,
            "combat_start",
            {
                "positioning_mode": "agent",
                "campaign_id": campaign["id"],
                "participant_ids": [
                    dragon["id"],
                    agile["id"],
                    clumsy["id"],
                ],
                "participant_config": [
                    {
                        "actor_id": dragon["id"],
                        "initiative": 20,
                        "disposition": "hostile",
                    },
                    {
                        "actor_id": agile["id"],
                        "initiative": 10,
                        "disposition": "friendly",
                    },
                    {
                        "actor_id": clumsy["id"],
                        "initiative": 5,
                        "disposition": "friendly",
                    },
                ],
                "scene_id": expanded["scene"]["id"],
                "ruleset": "2014",
                "expected_revision": play["campaign_revision"],
                "idempotency_key": "start",
            },
        )
        agent_ruling = {
            "application_id": "save-damage-application-1",
            "default_resolver": "agent",
            "ruling_kind": "agent_dm_adjudication",
            "decision": "The dragon catches both heroes in the reviewed cone.",
            "reason": "The active scene explicitly directs this breath attack.",
            "source_ref": deepcopy(expanded["source_ref"]),
            "source_excerpt": encounter_excerpt,
        }
        payload = {
            "target_ids": [agile["id"], clumsy["id"]],
            "source_actor_id": dragon["id"],
            "source_card_id": "scene-poison-cloud",
            "source_card_kind": "scene_procedure",
            "save_ability": "constitution",
            "save_dc": 18,
            "save_advantage": False,
            "save_disadvantage": False,
            "damage_expression": "16d6",
            "damage_type": "poison",
            "half_on_success": True,
            "mechanic_source_excerpt": mechanic_excerpt,
            "agent_ruling": agent_ruling,
            "spatial_facts": {
                "decision_id": "spatial:poison-cloud",
                "reason": "Both heroes are inside the cone and no obstruction blocks it.",
                "affected_target_ids": [agile["id"], clumsy["id"]],
                "excluded_actor_ids": [dragon["id"]],
                "line_of_effect_clear": True,
                "friendly_fire_included": False,
            },
        }
        with pytest.raises(Exception, match="cannot access|role"):
            await _call(
                server,
                "combat_hp_change",
                {
                    "campaign_id": campaign["id"],
                    "target_id": agile["id"],
                    "action": "save_damage",
                    "payload": payload,
                    "principal_id": "player:hero",
                    "expected_revision": started["campaign_revision"],
                    "idempotency_key": "player",
                },
            )
        with pytest.raises(Exception, match="scene procedure.*content_solution"):
            await _call(
                server,
                "combat_hp_change",
                {
                    "campaign_id": campaign["id"],
                    "target_id": agile["id"],
                    "action": "save_damage",
                    "payload": {
                        **payload,
                        "source_card_kind": "activity",
                    },
                    "expected_revision": started["campaign_revision"],
                    "idempotency_key": "actor-card-bypass",
                },
            )
        with pytest.raises(Exception, match="exact current-turn.*commitment"):
            await _call(
                server,
                "combat_hp_change",
                {
                    "campaign_id": campaign["id"],
                    "target_id": agile["id"],
                    "action": "save_damage",
                    "payload": payload,
                    "expected_revision": started["campaign_revision"],
                    "idempotency_key": "unpaid",
                },
            )
        commitment = {
            "application_id": agent_ruling["application_id"],
            "source_card_id": "scene-poison-cloud",
            "source_card_kind": "scene_procedure",
            "target_ids": [agile["id"], clumsy["id"]],
            "save_ability": "constitution",
            "save_dc": 18,
            "save_advantage": False,
            "save_disadvantage": False,
            "damage_expression": "16d6",
            "damage_type": "poison",
            "half_on_success": True,
            "mechanic_source_excerpt": mechanic_excerpt,
            "agent_ruling": agent_ruling,
        }
        paid = await _raw(
            server,
            "combat_common_action",
            {
                "campaign_id": campaign["id"],
                "actor_id": dragon["id"],
                "action": "improvise",
                "payload": {
                    "procedure_id": "scene-poison-cloud",
                    "agent_ruling_commitment": commitment,
                },
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "pay",
            },
        )
        assert paid["status"] == "committed"
        with pytest.raises(Exception, match="exact current-turn.*commitment"):
            await _call(
                server,
                "combat_hp_change",
                {
                    "campaign_id": campaign["id"],
                    "target_id": agile["id"],
                    "action": "save_damage",
                    "payload": {
                        **payload,
                        "target_ids": [agile["id"]],
                        "spatial_facts": {
                            **payload["spatial_facts"],
                            "affected_target_ids": [agile["id"]],
                            "excluded_actor_ids": [dragon["id"], clumsy["id"]],
                        },
                    },
                    "expected_revision": paid["campaign_revision"],
                    "idempotency_key": "changed-targets",
                },
            )
        settled = await _call(
            server,
            "combat_hp_change",
            {
                "campaign_id": campaign["id"],
                "target_id": agile["id"],
                "action": "save_damage",
                "payload": payload,
                "expected_revision": paid["campaign_revision"],
                "idempotency_key": "settle",
            },
        )
        assert settled["status"] == "committed"
        assert settled["result"]["damage_roll"]["total"] > 0
        targets = {item["target_id"]: item for item in settled["result"]["targets"]}
        assert targets[agile["id"]]["success"] is True
        assert targets[clumsy["id"]]["success"] is False
        assert targets[agile["id"]]["damage_amount"] == (
            settled["result"]["damage_roll"]["total"] // 2
        )
        assert targets[clumsy["id"]]["damage_amount"] == (settled["result"]["damage_roll"]["total"])
        agile_after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": agile["id"]},
                "principal_id": "system:local",
            },
        )
        clumsy_after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": clumsy["id"]},
                "principal_id": "system:local",
            },
        )
        assert agile_after["sheet"]["combat"]["hp"]["value"] == (
            100 - targets[agile["id"]]["damage_amount"]
        )
        assert clumsy_after["sheet"]["combat"]["hp"]["value"] == (
            100 - targets[clumsy["id"]]["damage_amount"]
        )
        replayed = await _call(
            server,
            "combat_hp_change",
            {
                "campaign_id": campaign["id"],
                "target_id": agile["id"],
                "action": "save_damage",
                "payload": payload,
                "expected_revision": paid["campaign_revision"],
                "idempotency_key": "settle",
            },
        )
        assert replayed == settled
        with pytest.raises(Exception, match="already been settled"):
            await _call(
                server,
                "combat_hp_change",
                {
                    "campaign_id": campaign["id"],
                    "target_id": agile["id"],
                    "action": "save_damage",
                    "payload": payload,
                    "expected_revision": settled["campaign_revision"],
                    "idempotency_key": "settle-again",
                },
            )
        agile_final = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": agile["id"]},
                "principal_id": "system:local",
            },
        )
        assert (
            agile_final["sheet"]["combat"]["hp"]["value"]
            == agile_after["sheet"]["combat"]["hp"]["value"]
        )

    asyncio.run(exercise())
