from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    value = result.get("result", result) if isinstance(result, dict) else result
    if isinstance(value, dict) and "action" in value and "result" in value:
        return value["result"]
    return value


async def _raw(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result


def test_custom_monster_plan_pays_executes_replays_and_rejects_mutation(
    tmp_path: Path,
) -> None:
    module_root = tmp_path / "modules"
    module_root.mkdir()
    encounter_excerpt = (
        "The prism beast releases its pulse when both heroes enter the chamber."
    )
    mechanic_excerpt = (
        "Prismatic Pulse. Each chosen creature must make a DC 14 Wisdom saving "
        "throw, taking 3d8 radiant damage on a failed save, or no damage on a "
        "successful one."
    )
    source = module_root / "prism.md"
    source.write_text(
        "# Prism Chamber\n\n"
        "## Encounter\n\n"
        f"{encounter_excerpt}\n\n{mechanic_excerpt}\n",
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

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Custom semantic plan",
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
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "stage",
                "payload": {
                    "source_path": str(source),
                    "source_key": "prism-chamber",
                    "title": "Prism Chamber",
                },
                "idempotency_key": "stage",
            },
        )
        job_id = staged["job"]["id"]
        for action in ("inspect", "validate", "ingest"):
            await _call(
                server,
                "module_import",
                {
                    "campaign_id": campaign["id"],
                    "action": action,
                    "payload": {"job_id": job_id},
                    "idempotency_key": action,
                },
            )
        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        await _call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "activate",
                "payload": {"job_id": job_id},
                "expected_revision": current["revision"],
                "idempotency_key": "activate",
            },
        )
        search = await _call(
            server,
            "module_search",
            {
                "campaign_id": campaign["id"],
                "query": "prism beast releases pulse both heroes",
                "top_k": 3,
            },
        )
        expanded = await _call(
            server,
            "module_expand",
            {"chunk_id": search[0]["id"]},
        )
        actors = []
        for name, character_type, key in (
            ("Prism Beast", "monster", "beast"),
            ("Hero One", "pc", "hero-one"),
            ("Hero Two", "pc", "hero-two"),
        ):
            actors.append(
                await _call(
                    server,
                    "character_create",
                    {
                        "campaign_id": campaign["id"],
                        "name": name,
                        "character_type": character_type,
                        "idempotency_key": key,
                    },
                )
            )
        beast, hero_one, hero_two = actors
        plan_id = "module.prism-chamber.prismatic-pulse"
        beast_sheet = default_character_sheet()
        beast_sheet["combat"]["hp"] = {"value": 80, "max": 80, "temp": 0}
        beast_sheet["content"]["activities"] = [
            {
                "id": "prismatic-pulse",
                "name": "Prismatic Pulse",
                "description": mechanic_excerpt,
                "activation": {"type": "action", "cost": 1},
                "uses": {"value": 0, "max": 0, "unlimited": True},
                "choices": {
                    "resolution_plan": {"id": plan_id, "fingerprint": "compiled"}
                },
                "resolution_plan": {
                    "schema_version": 1,
                    "id": plan_id,
                    "source_card_id": "prismatic-pulse",
                    "source_card_kind": "monster_action",
                    "trigger": "action",
                    "slots": {
                        "targets": {
                            "kind": "actor_ids",
                            "owner": "agent",
                            "description": (
                                "Creatures selected inside the reviewed pulse area."
                            ),
                            "minimum_items": 1,
                            "maximum_items": 2,
                        }
                    },
                    "steps": [
                        {
                            "id": "save",
                            "op": "check.save",
                            "args": {
                                "target_ids": {"$slot": "targets"},
                                "ability": "wisdom",
                                "dc": 14,
                                "success_reduction": "none",
                                "source": "Prismatic Pulse",
                            },
                        },
                        {
                            "id": "damage",
                            "op": "damage.apply",
                            "args": {
                                "target_ids": {"$slot": "targets"},
                                "expression": "3d8",
                                "damage_type": "radiant",
                                "source": "Prismatic Pulse",
                                "reduction": {
                                    "$result": "save.damage_reduction_by_actor_id"
                                },
                            },
                        },
                    ],
                    "citations": [
                        {
                            "source": "module:prism-chamber",
                            "source_ref": deepcopy(expanded["source_ref"]),
                            "source_excerpt": mechanic_excerpt,
                        }
                    ],
                },
            }
        ]
        beast = await _call(
            server,
            "character_sheet_replace",
            {
                "character_id": beast["id"],
                "sheet": beast_sheet,
                "expected_revision": beast["revision"],
                "idempotency_key": "beast-sheet",
            },
        )
        for actor, key in (
            (hero_one, "hero-one-sheet"),
            (hero_two, "hero-two-sheet"),
        ):
            sheet = default_character_sheet()
            sheet["combat"]["hp"] = {"value": 40, "max": 40, "temp": 0}
            await _call(
                server,
                "character_sheet_replace",
                {
                    "character_id": actor["id"],
                    "sheet": sheet,
                    "expected_revision": actor["revision"],
                    "idempotency_key": key,
                },
            )
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
                "campaign_id": campaign["id"],
                "participant_ids": [
                    beast["id"],
                    hero_one["id"],
                    hero_two["id"],
                ],
                "participant_config": [
                    {
                        "actor_id": beast["id"],
                        "initiative": 20,
                        "disposition": "hostile",
                    },
                    {
                        "actor_id": hero_one["id"],
                        "initiative": 10,
                        "disposition": "friendly",
                    },
                    {
                        "actor_id": hero_two["id"],
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
        pending = await _raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": beast["id"],
                "activity_id": "prismatic-pulse",
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "contract",
            },
        )
        contract = pending["result"]["resolution_plan_contract"]
        assert pending["status"] == "pending_ruling"
        assert contract["plan_id"] == plan_id
        assert "steps" not in contract
        agent_ruling = {
            "application_id": "prismatic-pulse-round-1",
            "default_resolver": "agent",
            "ruling_kind": "agent_dm_adjudication",
            "decision": "Both heroes occupy the reviewed pulse area in this chamber.",
            "reason": "The active scene and recorded encounter positions include both.",
            "source_ref": deepcopy(expanded["source_ref"]),
            "source_excerpt": encounter_excerpt,
        }
        commitment = {
            "application_id": agent_ruling["application_id"],
            "plan_id": plan_id,
            "plan_fingerprint": contract["plan_fingerprint"],
            "source_card_id": "prismatic-pulse",
            "source_card_kind": "monster_action",
            "bindings": {"targets": [hero_one["id"], hero_two["id"]]},
            "agent_ruling": agent_ruling,
        }
        paid = await _raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": beast["id"],
                "activity_id": "prismatic-pulse",
                "declaration": {
                    "agent_resolution_commitment": commitment,
                },
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "pay",
            },
        )
        assert paid["status"] == "pending_ruling"
        normalized_commitment = paid["result"]["declaration"][
            "agent_resolution_commitment"
        ]
        assert normalized_commitment["bound_plan_fingerprint"]
        changed = deepcopy(normalized_commitment)
        changed["bindings"]["targets"] = [hero_one["id"]]
        with pytest.raises(Exception, match="does not match the recorded plan"):
            await _call(
                server,
                "combat_choice",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": beast["id"],
                    "action": "execute_plan",
                    "payload": {"commitment": changed},
                    "expected_revision": paid["campaign_revision"],
                    "idempotency_key": "changed",
                },
            )
        with pytest.raises(Exception, match="cannot access|role"):
            await _call(
                server,
                "combat_choice",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": beast["id"],
                    "action": "execute_plan",
                    "payload": {"commitment": normalized_commitment},
                    "principal_id": "player:hero",
                    "expected_revision": paid["campaign_revision"],
                    "idempotency_key": "player",
                },
            )
        settled = await _call(
            server,
            "combat_choice",
            {
                "campaign_id": campaign["id"],
                "actor_id": beast["id"],
                "action": "execute_plan",
                "payload": {"commitment": normalized_commitment},
                "expected_revision": paid["campaign_revision"],
                "idempotency_key": "settle",
            },
        )
        assert settled["status"] == "committed"
        assert settled["result"]["plan_id"] == plan_id
        damage = settled["result"]["results"]["damage"]
        assert damage["roll"]["total"] > 0
        save_targets = {
            item["target_id"]: item
            for item in settled["result"]["results"]["save"]["targets"]
        }
        damage_targets = {
            item["target_id"]: item for item in damage["targets"]
        }
        for actor in (hero_one, hero_two):
            target_id = actor["id"]
            after = await _call(
                server,
                "character_get",
                {"character_id": target_id},
            )
            expected_damage = (
                0
                if save_targets[target_id]["success"]
                else damage["base_amount"]
            )
            assert damage_targets[target_id]["applied_amount"] == expected_damage
            assert after["sheet"]["combat"]["hp"]["value"] == 40 - expected_damage
        replayed = await _call(
            server,
            "combat_choice",
            {
                "campaign_id": campaign["id"],
                "actor_id": beast["id"],
                "action": "execute_plan",
                "payload": {"commitment": normalized_commitment},
                "expected_revision": paid["campaign_revision"],
                "idempotency_key": "settle",
            },
        )
        assert replayed == settled

    asyncio.run(exercise())
