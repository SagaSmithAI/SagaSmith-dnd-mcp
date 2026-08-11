from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp import server as server_module
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


def test_custom_monster_plan_pays_executes_replays_and_rejects_mutation(
    tmp_path: Path,
) -> None:
    module_root = tmp_path / "modules"
    module_root.mkdir()
    encounter_excerpt = "The prism beast releases its pulse when both heroes enter the chamber."
    mechanic_excerpt = (
        "Prismatic Pulse. Each chosen creature must make a DC 14 Wisdom saving "
        "throw, taking 3d8 radiant damage on a failed save, or no damage on a "
        "successful one."
    )
    spell_excerpt = (
        "Chromatic Spark. One visible creature within 30 feet takes 1d4 radiant "
        "damage and is frightened until the start of the prism beast's next turn."
    )
    source = module_root / "prism.md"
    source.write_text(
        "# Prism Chamber\n\n"
        "## Encounter\n\n"
        f"{encounter_excerpt}\n\n{mechanic_excerpt}\n\n{spell_excerpt}\n",
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
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "prism-chamber",
                    "title": "Prism Chamber",
                },
                "idempotency_key": "stage",
            },
        )
        await finalize_and_activate_module(
            _call,
            server,
            campaign["id"],
            staged,
            source_key="prism-chamber",
            title="Prism Chamber",
            portable_id="dnd5e.module.prism-chamber-test",
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
                    "character_create_from",
                    {
                        "mode": "direct",
                        "payload": {
                            "campaign_id": campaign["id"],
                            "name": name,
                            "character_type": character_type,
                        },
                        "principal_id": "system:local",
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
                "choices": {"resolution_plan": {"id": plan_id, "fingerprint": "compiled"}},
                "resolution_plan": {
                    "schema_version": 2,
                    "id": plan_id,
                    "source_card_id": "prismatic-pulse",
                    "source_card_kind": "monster_action",
                    "trigger": "action",
                    "slots": {
                        "source_actor": {
                            "kind": "actor_id",
                            "owner": "agent",
                            "description": "The prism beast using this source action.",
                        },
                        "targets": {
                            "kind": "actor_ids",
                            "owner": "agent",
                            "description": ("Creatures selected inside the reviewed pulse area."),
                            "minimum_items": 1,
                            "maximum_items": 2,
                        },
                    },
                    "steps": [
                        {
                            "id": "targets",
                            "op": "target.validate",
                            "args": {
                                "source_actor_id": {"$slot": "source_actor"},
                                "target_ids": {"$slot": "targets"},
                                "exclude_self": True,
                                "require_visible": True,
                                "maximum_range_ft": 30,
                                "source": "Prismatic Pulse",
                            },
                        },
                        {
                            "id": "save",
                            "op": "check.save",
                            "args": {
                                "target_ids": {"$slot": "targets"},
                                "ability": "wisdom",
                                "dc": 14,
                                "success_damage": "none",
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
                                "reduction": {"$result": "save.damage_reduction_by_actor_id"},
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
        spell_plan_id = "module.prism-chamber.chromatic-spark"
        beast_sheet["content"]["spells"] = [
            {
                "id": "chromatic-spark",
                "name": "Chromatic Spark",
                "level": 0,
                "access": {
                    "known": True,
                    "prepared": False,
                    "always_prepared": False,
                    "ritual_available": False,
                    "at_will": False,
                    "at_will_sources": [],
                },
                "definition": {
                    "casting_time": "1 action",
                    "duration": {
                        "kind": "instantaneous",
                        "value": 0,
                        "unit": "round",
                        "concentration": False,
                    },
                    "effect": spell_excerpt,
                },
                "resolution_plan": {
                    "schema_version": 2,
                    "id": spell_plan_id,
                    "source_card_id": "chromatic-spark",
                    "source_card_kind": "spell",
                    "trigger": "action",
                    "slots": {
                        "source_actor": {
                            "kind": "actor_id",
                            "owner": "agent",
                            "description": "The prism beast casting this source spell.",
                        },
                        "target": {
                            "kind": "actor_ids",
                            "owner": "agent",
                            "description": "One visible creature selected for the spark.",
                            "minimum_items": 1,
                            "maximum_items": 1,
                        },
                    },
                    "steps": [
                        {
                            "id": "target",
                            "op": "target.validate",
                            "args": {
                                "source_actor_id": {"$slot": "source_actor"},
                                "target_ids": {"$slot": "target"},
                                "exclude_self": True,
                                "require_visible": True,
                                "maximum_range_ft": 30,
                                "source": "Chromatic Spark",
                            },
                        },
                        {
                            "id": "damage",
                            "op": "damage.apply",
                            "args": {
                                "target_ids": {"$slot": "target"},
                                "expression": "1d4",
                                "damage_type": "radiant",
                                "source": "Chromatic Spark",
                            },
                        },
                        {
                            "id": "frightened",
                            "op": "condition.apply",
                            "args": {
                                "source_actor_id": {"$slot": "source_actor"},
                                "target_ids": {"$slot": "target"},
                                "condition_id": "frightened",
                                "duration": {"kind": "source_turn_start"},
                                "source": "Chromatic Spark",
                            },
                        },
                    ],
                    "citations": [
                        {
                            "source": "module:prism-chamber",
                            "source_ref": deepcopy(expanded["source_ref"]),
                            "source_excerpt": spell_excerpt,
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
                "positioning_mode": "grid",
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
                        "position": {"x": 0, "y": 0},
                        "disposition": "hostile",
                    },
                    {
                        "actor_id": hero_one["id"],
                        "initiative": 10,
                        "position": {"x": 3, "y": 0},
                        "disposition": "friendly",
                    },
                    {
                        "actor_id": hero_two["id"],
                        "initiative": 5,
                        "position": {"x": 6, "y": 0},
                        "disposition": "friendly",
                    },
                ],
                "scene_id": expanded["scene"]["id"],
                "battle_map": {"bounds": {"width_cells": 12, "height_cells": 12}},
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
            "bindings": {
                "source_actor": beast["id"],
                "targets": [hero_one["id"], hero_two["id"]],
            },
            "agent_ruling": agent_ruling,
        }
        wrong_source = deepcopy(commitment)
        wrong_source["bindings"]["source_actor"] = hero_one["id"]
        with pytest.raises(Exception, match="source_actor_id must match"):
            await _call(
                server,
                "combat_use_activity",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": beast["id"],
                    "activity_id": "prismatic-pulse",
                    "declaration": {
                        "agent_resolution_commitment": wrong_source,
                    },
                    "expected_revision": started["campaign_revision"],
                    "idempotency_key": "wrong-source",
                },
            )
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
        normalized_commitment = paid["result"]["declaration"]["agent_resolution_commitment"]
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
            item["target_id"]: item for item in settled["result"]["results"]["save"]["targets"]
        }
        damage_targets = {item["target_id"]: item for item in damage["targets"]}
        for actor in (hero_one, hero_two):
            target_id = actor["id"]
            after = await _call(
                server,
                "character_query",
                {
                    "view": "get",
                    "payload": {"character_id": target_id},
                    "principal_id": "system:local",
                },
            )
            expected_damage = 0 if save_targets[target_id]["success"] else damage["base_amount"]
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

        revision = settled["campaign_revision"]
        for index, actor in enumerate((beast, hero_one, hero_two)):
            ended = await _call(
                server,
                "combat_end_turn",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": actor["id"],
                    "expected_revision": revision,
                    "idempotency_key": f"next-round-{index}",
                },
            )
            revision = ended["campaign_revision"]
        spell_pending = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign["id"],
                "actor_id": beast["id"],
                "spell_id": "chromatic-spark",
                "expected_revision": revision,
                "idempotency_key": "spell-contract",
            },
        )
        spell_contract = spell_pending["result"]["resolution_plan_contract"]
        assert spell_pending["status"] == "pending_ruling"
        assert spell_pending["result"]["payment_required"] is True
        spell_ruling = {
            **agent_ruling,
            "application_id": "chromatic-spark-round-2",
            "decision": "Hero One is the one visible target of Chromatic Spark.",
        }
        spell_commitment = {
            "application_id": spell_ruling["application_id"],
            "plan_id": spell_plan_id,
            "plan_fingerprint": spell_contract["plan_fingerprint"],
            "source_card_id": "chromatic-spark",
            "source_card_kind": "spell",
            "bindings": {
                "source_actor": beast["id"],
                "target": [hero_one["id"]],
            },
            "agent_ruling": spell_ruling,
        }
        spell_paid = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign["id"],
                "actor_id": beast["id"],
                "spell_id": "chromatic-spark",
                "declaration": {
                    "agent_resolution_commitment": spell_commitment,
                },
                "expected_revision": revision,
                "idempotency_key": "spell-pay",
            },
        )
        paid_spell_commitment = spell_paid["result"]["semantic_plan"]["commitment"]
        before_spell = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": hero_one["id"]},
                "principal_id": "system:local",
            },
        )
        spell_settled = await _call(
            server,
            "combat_choice",
            {
                "campaign_id": campaign["id"],
                "actor_id": beast["id"],
                "action": "execute_plan",
                "payload": {"commitment": paid_spell_commitment},
                "expected_revision": spell_paid["campaign_revision"],
                "idempotency_key": "spell-settle",
            },
        )
        after_spell = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": hero_one["id"]},
                "principal_id": "system:local",
            },
        )
        applied_spell_damage = spell_settled["result"]["results"]["damage"]["targets"][0][
            "applied_amount"
        ]
        assert applied_spell_damage > 0
        assert (
            after_spell["sheet"]["combat"]["hp"]["value"]
            == before_spell["sheet"]["combat"]["hp"]["value"] - applied_spell_damage
        )
        assert "frightened" in after_spell["sheet"]["conditions"]
        frightened_result = spell_settled["result"]["results"]["frightened"]
        frightened_effect_id = frightened_result["targets"][0]["effect_id"]
        assert frightened_effect_id
        active_effect = next(
            item for item in after_spell["sheet"]["effects"] if item["id"] == frightened_effect_id
        )
        assert active_effect["source"] == beast["id"]
        assert active_effect["duration"] == {
            "period": "source_turn_start",
            "remaining": 1,
        }

        revision = spell_settled["campaign_revision"]
        for index, actor in enumerate((beast, hero_one, hero_two)):
            ended = await _call(
                server,
                "combat_end_turn",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": actor["id"],
                    "expected_revision": revision,
                    "idempotency_key": f"expire-frightened-{index}",
                },
            )
            revision = ended["campaign_revision"]
        after_expiry = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": hero_one["id"]},
                "principal_id": "system:local",
            },
        )
        assert "frightened" not in after_expiry["sheet"]["conditions"]
        expired_effect = next(
            item for item in after_expiry["sheet"]["effects"] if item["id"] == frightened_effect_id
        )
        assert expired_effect["active"] is False

    asyncio.run(exercise())


def test_content_solution_accepts_only_exact_active_rule_chunk_evidence(
    tmp_path: Path,
) -> None:
    import_root = tmp_path / "rules"
    import_root.mkdir()
    effect = "Moon Ribbon marks one creature until the start of the caster's next turn."
    source = import_root / "moon-lore.md"
    source.write_text(
        f"# Moon Lore\n\n## Moon Ribbon\n\n{effect}\n",
        encoding="utf-8",
    )
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        rule_import_roots=(import_root,),
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Rule evidence compilation",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        staged = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "moon-lore",
                    "title": "Moon Lore",
                    "edition": "2014",
                },
                "idempotency_key": "moon-rule:stage",
            },
        )
        job_id = staged["job"]["id"]
        await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "idempotency_key": "moon-rule:inspect",
            },
        )
        await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {"operation": "advance", "job_id": job_id},
                "idempotency_key": "moon-rule:ingest",
            },
        )
        hits = await _call(
            server,
            "rule_search",
            {
                "campaign_id": campaign["id"],
                "query": "Moon Ribbon marks one creature",
                "filters": {"edition": "2014"},
                "top_k": 1,
            },
        )
        chunk_id = hits[0]["id"]
        sheet = default_character_sheet()
        sheet["content"]["features"] = [
            {
                "id": "moon-ribbon-feature",
                "name": "Moon Ribbon",
                "description": effect,
                "activation": {"type": "special", "cost": 0},
            },
            {
                "id": "dnd5e.content.srd2014.feature.fighter-action-surge",
                "name": "Action Surge",
                "description": "Take one additional action on your turn.",
                "activation": {"type": "special", "cost": 0},
            },
        ]
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Moon Keeper", "sheet": sheet},
                "principal_id": "system:local",
                "idempotency_key": "actor",
            },
        )
        plan = {
            "schema_version": 2,
            "id": "custom.moon-ribbon.mark",
            "source_card_id": "moon-ribbon-feature",
            "source_card_kind": "feature",
            "trigger": "scene",
            "trigger_filter": {},
            "slots": {
                "target": {
                    "kind": "actor_id",
                    "owner": "agent",
                    "description": "The creature selected by the caster.",
                }
            },
            "steps": [
                {
                    "id": "mark",
                    "op": "condition.apply",
                    "args": {
                        "target_ids": [{"$slot": "target"}],
                        "condition_id": "marked",
                        "source": "Moon Ribbon",
                    },
                }
            ],
            "citations": [
                {
                    "source": "rule-source:not-moon-lore",
                    "source_ref": {"chunk_id": chunk_id},
                    "source_excerpt": effect,
                }
            ],
        }
        arguments = {
            "campaign_id": campaign["id"],
            "actor_id": actor["id"],
            "action": "compile",
            "source_card_id": "moon-ribbon-feature",
            "source_card_kind": "feature",
            "payload": {
                "resolution_plan": plan,
                "agent_ruling": {
                    "default_resolver": "agent",
                    "ruling_kind": "module_specific_procedure",
                    "decision": "Store the exact Moon Ribbon procedure.",
                    "reason": "The active rule chunk supplies the source wording.",
                },
            },
            "expected_revision": actor["revision"],
            "idempotency_key": "compile",
        }
        standard_arguments = deepcopy(arguments)
        standard_arguments.update(
            {
                "source_card_id": ("dnd5e.content.srd2014.feature.fighter-action-surge"),
                "idempotency_key": "reject-standard",
            }
        )
        with pytest.raises(Exception, match="locked engine implementation"):
            await _call(server, "content_solution", standard_arguments)
        with pytest.raises(
            Exception,
            match="does not match the active campaign rules",
        ):
            await _call(server, "content_solution", arguments)

        plan["citations"][0]["source"] = "rule-source:moon-lore"
        compiled = await _call(
            server,
            "content_solution",
            arguments,
        )

        assert compiled["status"] == "compiled"
        assert compiled["solution"]["source_fingerprint"]
        assert compiled["solution"]["source_card_fingerprint"]

        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": current["revision"],
                "idempotency_key": "enter-play",
            },
        )
        with pytest.raises(Exception, match="already has a compiled solution"):
            await _call(
                server,
                "content_solution",
                {
                    **arguments,
                    "idempotency_key": "runtime-recompile",
                },
            )
        queried = await _call(
            server,
            "content_solution",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "action": "query",
                "source_card_id": "moon-ribbon-feature",
                "source_card_kind": "feature",
            },
        )
        assert queried["status"] == "compiled"

    asyncio.run(exercise())


def test_item_on_hit_plan_uses_the_attack_event_as_payment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module_root = tmp_path / "modules"
    module_root.mkdir()
    encounter_excerpt = "The binding blade restrains the creature it strikes in the warded room."
    mechanic_excerpt = "On a hit, the binding blade restrains the target."
    feature_excerpt = "The ward lore can mark a creature chosen by the blade's keeper."
    source = module_root / "binding-blade.md"
    source.write_text(
        "# Warded Room\n\n## Encounter\n\n"
        f"{encounter_excerpt}\n\n{mechanic_excerpt}\n\n{feature_excerpt}\n",
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
    original_attack_roll = server_module.roll_attack_action

    def forced_hit(*, plan, rng=None):
        result = original_attack_roll(plan=plan, rng=rng)
        result.update(
            natural=15,
            total=15 + int(plan["attack_bonus"]),
            armor_class=int(plan["target_ac"]),
            hit=True,
            critical=False,
            fumble=False,
        )
        return result

    monkeypatch.setattr(server_module, "roll_attack_action", forced_hit)

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Item semantic plan",
                "edition": "2014",
                "idempotency_key": "campaign",
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
                    "source_key": "binding-blade-room",
                    "title": "Warded Room",
                },
                "idempotency_key": "stage",
            },
        )
        await finalize_and_activate_module(
            _call,
            server,
            campaign["id"],
            staged,
            source_key="binding-blade-room",
            title="Warded Room",
            portable_id="dnd5e.module.binding-blade-test",
        )
        search = await _call(
            server,
            "module_search",
            {
                "campaign_id": campaign["id"],
                "query": "binding blade restrains creature warded room",
                "top_k": 3,
            },
        )
        expanded = await _call(
            server,
            "module_expand",
            {"chunk_id": search[0]["id"]},
        )
        wielder = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Blade Wielder",
                    "character_type": "npc",
                },
                "principal_id": "system:local",
                "idempotency_key": "wielder",
            },
        )
        target = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Target",
                    "character_type": "monster",
                },
                "principal_id": "system:local",
                "idempotency_key": "target",
            },
        )
        plan_id = "module.binding-blade.on-hit"
        wielder_sheet = default_character_sheet()
        wielder_sheet["inventory"]["items"] = [
            {
                "id": "binding-blade",
                "name": "Binding Blade",
                "kind": "weapon",
                "equipped": True,
                "equipped_slot": "main_hand",
                "description": mechanic_excerpt,
                "mechanics": {
                    "attack_type": "melee",
                    "attack_ability": "strength",
                    "damage_formula": "1d6",
                    "damage_type": "slashing",
                    "on_hit_effect": mechanic_excerpt,
                    "reach_ft": 5,
                    "attack_bonus_override": 8,
                    "always_available": True,
                },
                "resolution_plan": {
                    "schema_version": 2,
                    "id": plan_id,
                    "source_card_id": "binding-blade",
                    "source_card_kind": "item",
                    "trigger": "attack.after_hit",
                    "trigger_filter": {
                        "source_actor_id": {"$slot": "source_actor"},
                        "target_actor_id": {"$slot": "target"},
                        "weapon_id": "binding-blade",
                        "hit": True,
                    },
                    "slots": {
                        "source_actor": {
                            "kind": "actor_id",
                            "owner": "agent",
                            "description": ("The wielder that made the triggering attack."),
                        },
                        "target": {
                            "kind": "actor_id",
                            "owner": "agent",
                            "description": ("The creature hit by the triggering attack."),
                        },
                    },
                    "steps": [
                        {
                            "id": "targets",
                            "op": "target.validate",
                            "args": {
                                "source_actor_id": {"$slot": "source_actor"},
                                "target_ids": [{"$slot": "target"}],
                                "exclude_self": True,
                                "maximum_range_ft": 5,
                                "require_visible": True,
                                "source": "Binding Blade",
                            },
                        },
                        {
                            "id": "restrain",
                            "op": "condition.apply",
                            "args": {
                                "target_ids": [{"$slot": "target"}],
                                "condition_id": "restrained",
                                "source": "Binding Blade",
                            },
                        },
                    ],
                    "citations": [
                        {
                            "source": "module:binding-blade-room",
                            "source_ref": deepcopy(expanded["source_ref"]),
                            "source_excerpt": mechanic_excerpt,
                        }
                    ],
                },
            }
        ]
        wielder_sheet["inventory"]["equipment_slots"]["main_hand"] = "binding-blade"
        item_plan = deepcopy(wielder_sheet["inventory"]["items"][0].pop("resolution_plan"))
        wielder_sheet["content"]["features"] = [
            {
                "id": "ward-lore",
                "name": "Ward Lore",
                "description": feature_excerpt,
                "activation": {"type": "special", "cost": 0},
            }
        ]
        target_sheet = default_character_sheet()
        target_sheet["combat"]["hp"] = {
            "value": 50,
            "max": 50,
            "temp": 0,
        }
        for actor, sheet, key in (
            (wielder, wielder_sheet, "wielder-sheet"),
            (target, target_sheet, "target-sheet"),
        ):
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
        wielder_current = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": wielder["id"]},
                "principal_id": "system:local",
            },
        )
        feature_plan = {
            "schema_version": 2,
            "id": "module.ward-lore.mark",
            "source_card_id": "ward-lore",
            "source_card_kind": "feature",
            "trigger": "scene",
            "trigger_filter": {},
            "slots": {
                "target": {
                    "kind": "actor_id",
                    "owner": "agent",
                    "description": "The creature chosen by the keeper.",
                }
            },
            "steps": [
                {
                    "id": "mark",
                    "op": "condition.apply",
                    "args": {
                        "target_ids": [{"$slot": "target"}],
                        "condition_id": "marked",
                        "source": "Ward Lore",
                    },
                }
            ],
            "citations": [
                {
                    "source": "module:binding-blade-room",
                    "source_ref": deepcopy(expanded["source_ref"]),
                    "source_excerpt": feature_excerpt,
                }
            ],
        }
        feature_compile_arguments = {
            "campaign_id": campaign["id"],
            "actor_id": wielder["id"],
            "action": "compile",
            "source_card_id": "ward-lore",
            "source_card_kind": "feature",
            "payload": {
                "resolution_plan": feature_plan,
                "agent_ruling": {
                    "default_resolver": "agent",
                    "ruling_kind": "module_specific_procedure",
                    "decision": ("Store the quoted mark as a reusable feature solution."),
                    "reason": ("The exact feature text identifies the chosen creature."),
                },
            },
            "expected_revision": wielder_current["revision"],
            "idempotency_key": "compile-feature",
        }
        feature_compiled = await _call(
            server,
            "content_solution",
            feature_compile_arguments,
        )
        assert feature_compiled["status"] == "compiled"
        assert (
            await _call(
                server,
                "content_solution",
                feature_compile_arguments,
            )
            == feature_compiled
        )
        feature_query = await _call(
            server,
            "content_solution",
            {
                "campaign_id": campaign["id"],
                "actor_id": wielder["id"],
                "action": "query",
                "source_card_id": "ward-lore",
                "source_card_kind": "feature",
            },
        )
        assert feature_query["status"] == "compiled"
        assert feature_query["resolution_solution"]["plan_fingerprint"]
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
                "positioning_mode": "grid",
                "campaign_id": campaign["id"],
                "participant_ids": [wielder["id"], target["id"]],
                "participant_config": [
                    {
                        "actor_id": wielder["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                        "disposition": "friendly",
                    },
                    {
                        "actor_id": target["id"],
                        "initiative": 10,
                        "position": {"x": 1, "y": 0},
                        "disposition": "hostile",
                    },
                ],
                "scene_id": expanded["scene"]["id"],
                "battle_map": {"bounds": {"width_cells": 8, "height_cells": 8}},
                "ruleset": "2014",
                "expected_revision": play["campaign_revision"],
                "idempotency_key": "start",
            },
        )
        attacked = await _raw(
            server,
            "combat_resolve_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": wielder["id"],
                "target_id": target["id"],
                "action": {
                    "weapon_id": "binding-blade",
                    "attack_mode": "melee",
                },
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "attack",
            },
        )
        semantic = attacked["result"]["semantic_solution"]
        assert attacked["status"] == "pending_ruling"
        assert semantic["status"] == "content_authoring_required"
        assert semantic["first_use_compilation_required"] is True
        assert semantic["application_id"]
        wielder_at_first_use = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": wielder["id"]},
                "principal_id": "system:local",
            },
        )
        compiled_item = await _call(
            server,
            "content_solution",
            {
                "campaign_id": campaign["id"],
                "actor_id": wielder["id"],
                "action": "compile",
                "source_card_id": "binding-blade",
                "source_card_kind": "item",
                "payload": {
                    "resolution_plan": item_plan,
                    "agent_ruling": {
                        "default_resolver": "agent",
                        "ruling_kind": "module_specific_procedure",
                        "decision": "Store the blade's quoted on-hit procedure.",
                        "reason": "The active module chunk contains the exact mechanic.",
                    },
                },
                "expected_revision": wielder_at_first_use["revision"],
                "idempotency_key": "compile-item-at-first-use",
            },
        )
        contract = compiled_item["resolution_plan_contract"]
        assert contract["plan_id"] == plan_id
        assert contract["trigger_filter"]["weapon_id"] == "binding-blade"
        agent_ruling = {
            "application_id": semantic["application_id"],
            "default_resolver": "agent",
            "ruling_kind": "source_or_scene_fact",
            "decision": "The reviewed blade hit this adjacent target.",
            "reason": (
                "The server-recorded attack and current positions satisfy the source trigger."
            ),
            "source_ref": deepcopy(expanded["source_ref"]),
            "source_excerpt": encounter_excerpt,
        }
        commitment = {
            "application_id": semantic["application_id"],
            "plan_id": plan_id,
            "plan_fingerprint": contract["plan_fingerprint"],
            "source_card_id": "binding-blade",
            "source_card_kind": "item",
            "bindings": {
                "source_actor": wielder["id"],
                "target": target["id"],
            },
            "agent_ruling": agent_ruling,
        }
        wrong_target_commitment = deepcopy(commitment)
        wrong_target_commitment["bindings"]["target"] = wielder["id"]
        with pytest.raises(Exception, match="paid engine event"):
            await _call(
                server,
                "combat_choice",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": wielder["id"],
                    "action": "execute_plan",
                    "payload": {
                        "commitment": wrong_target_commitment,
                    },
                    "expected_revision": attacked["campaign_revision"],
                    "idempotency_key": "wrong-target-settle",
                },
            )
        settled = await _call(
            server,
            "combat_choice",
            {
                "campaign_id": campaign["id"],
                "actor_id": wielder["id"],
                "action": "execute_plan",
                "payload": {"commitment": commitment},
                "expected_revision": attacked["campaign_revision"],
                "idempotency_key": "settle",
            },
        )
        target_after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": target["id"]},
                "principal_id": "system:local",
            },
        )

        assert settled["status"] == "committed"
        assert "restrained" in target_after["sheet"]["conditions"]
        wielder_after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": wielder["id"]},
                "principal_id": "system:local",
            },
        )
        stored_item = wielder_after["sheet"]["inventory"]["items"][0]
        assert stored_item["resolution_plan"]["fingerprint"] == (contract["plan_fingerprint"])
        assert (
            stored_item["resolution_solution"]["plan_fingerprint"] == (contract["plan_fingerprint"])
        )
        assert all(
            item.get("id") != semantic["application_id"]
            for item in settled["combat"].get("pending", [])
        )
        revision = settled["campaign_revision"]
        for index, actor in enumerate((wielder, target)):
            ended = await _call(
                server,
                "combat_end_turn",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": actor["id"],
                    "expected_revision": revision,
                    "idempotency_key": f"next-round-{index}",
                },
            )
            revision = ended["campaign_revision"]
        reused = await _raw(
            server,
            "combat_resolve_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": wielder["id"],
                "target_id": target["id"],
                "action": {
                    "weapon_id": "binding-blade",
                    "attack_mode": "melee",
                },
                "expected_revision": revision,
                "idempotency_key": "second-attack",
            },
        )
        assert reused["result"]["semantic_plan"]["status"] == ("payment_recorded")
        assert "semantic_solution" not in reused["result"]

    asyncio.run(exercise())
