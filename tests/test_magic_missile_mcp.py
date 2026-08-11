import asyncio
import random
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet
from sagasmith_dnd.engine import roll as engine_roll
from sagasmith_dnd.spells import (
    CORE_MAGIC_MISSILE_MECHANIC_ID,
    CORE_MAGIC_MISSILE_SPELL_ID,
    CORE_SHIELD_MECHANIC_ID,
    CORE_SHIELD_SPELL_ID,
)

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


def _spell(spell_id: str, name: str, mechanic_id: str, casting_time: str) -> dict:
    return {
        "id": spell_id,
        "name": name,
        "level": 1,
        "grant": {"source_type": "class", "source_key": "wizard", "method": "known"},
        "access": {"known": True, "prepared": True},
        "definition": {
            "casting_time": casting_time,
            "range": {"kind": "distance", "normal_ft": 120, "long_ft": 120},
            "duration": {"kind": "instantaneous", "concentration": False},
            "components": {"verbal": True, "somatic": True},
        },
        "mechanic_refs": [mechanic_id],
    }


def _slots(value: int = 1) -> dict:
    return {
        "1": {
            "label": "1st",
            "value": value,
            "max": value,
            "recovers_on": "long_rest",
            "source_key": "wizard",
        }
    }


def _agent_adjudicated_darkness() -> tuple[dict, str]:
    excerpt = (
        "Magical darkness spreads from a point you choose within range to fill a "
        "15-foot radius sphere for the duration. A creature with darkvision can't "
        "see through this darkness, and nonmagical light can't illuminate it."
    )
    return (
        {
            "id": "dnd5e.content.srd2014.spell.darkness",
            "name": "Darkness",
            "level": 2,
            "grant": {
                "source_type": "statblock",
                "source_key": "test",
                "method": "innate",
            },
            "access": {"known": True, "prepared": True},
            "definition": {
                "casting_time": "1 action",
                "range": {"kind": "distance", "normal_ft": 60, "long_ft": 0},
                "duration": {
                    "kind": "timed",
                    "value": 10,
                    "unit": "minute",
                    "concentration": True,
                },
                "components": {"verbal": True},
                "effect": excerpt,
            },
            "pack_id": "dnd5e.content.srd2014",
            "custom_definition": {
                "innate_spellcasting": True,
                "innate_resource_key": "innate_spell:test-darkness",
            },
            "rule_refs": ["bundled:srd2014/07_Spells/Spells_Each/Darkness.md"],
            "ruling_requirements": [
                {
                    "kind": "effect_semantics",
                    "reason": "Apply the exact persisted Agent-as-DM clause.",
                    "source_excerpt": excerpt,
                    "default_resolver": "agent",
                    "ruling_kind": "generic_spell_effect",
                    "policy_ref": "rule_clause.v1",
                }
            ],
        },
        excerpt,
    )


def _config(tmp_path: Path) -> McpConfig:
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


async def _raw(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result


async def _campaign_with_combat(
    server,
    sheets: list[tuple[str, dict]],
    *,
    hidden_caster: bool = False,
) -> tuple[dict, list[dict]]:
    campaign = await _call(
        server,
        "campaign_create",
        {"name": "Magic Missile", "edition": "2014", "idempotency_key": "mm-campaign"},
    )
    actors = []
    for index, (name, sheet) in enumerate(sheets):
        actors.append(
            await _call(
                server,
                "character_create_from",
                {
                    "mode": "direct",
                    "payload": {"campaign_id": campaign["id"], "name": name, "sheet": sheet},
                    "principal_id": "system:local",
                    "idempotency_key": f"mm-actor-{index}",
                },
            )
        )
    campaign = await _call(
        server,
        "campaign_query",
        {"view": "get", "payload": {"campaign_id": campaign["id"]}, "principal_id": "system:local"},
    )
    phase = await _call(
        server,
        "game_phase",
        {
            "campaign_id": campaign["id"],
            "action": "set",
            "tool_profile": "play",
            "expected_revision": campaign["revision"],
            "idempotency_key": "mm-play",
        },
    )
    started = await _call(
        server,
        "combat_start",
        {
            "positioning_mode": "grid",
            "battle_map": {"width_cells": 20, "height_cells": 20},
            "campaign_id": campaign["id"],
            "participant_ids": [item["id"] for item in actors],
            "participant_config": [
                {
                    "actor_id": item["id"],
                    "initiative": 20 - index,
                    "position": {"x": index, "y": 0},
                    "disposition": "friendly" if index == 0 else "hostile",
                    **(
                        {
                            "hidden": True,
                            "visible_to_actor_ids": [actors[0]["id"]],
                        }
                        if hidden_caster and index == 0
                        else {}
                    ),
                }
                for index, item in enumerate(actors)
            ],
            "expected_revision": phase["campaign_revision"],
            "idempotency_key": "mm-start",
        },
    )
    return {**campaign, "revision": started["campaign_revision"]}, actors


def test_standard_agent_spell_ruling_pays_and_records_exact_clause(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        darkness, excerpt = _agent_adjudicated_darkness()
        caster_sheet = default_character_sheet()
        caster_sheet["resources"]["innate_spell:test-darkness"] = {
            "label": "Darkness",
            "value": 1,
            "max": 1,
            "recovers_on": "long_rest",
        }
        caster_sheet["content"]["spells"] = [darkness]
        campaign, actors = await _campaign_with_combat(
            server,
            [("Caster", caster_sheet), ("Target", default_character_sheet())],
        )
        arguments = {
            "campaign_id": campaign["id"],
            "actor_id": actors[0]["id"],
            "spell_id": darkness["id"],
            "cast_level": 2,
            "expected_revision": campaign["revision"],
            "idempotency_key": "agent-darkness",
        }
        pending = await _raw(server, "combat_cast_spell", arguments)
        assert pending["status"] == "pending_ruling"
        assert pending["result"]["payment_required"] is True
        contract = pending["result"]["agent_ruling_contract"]
        assert contract["source_excerpt"] == excerpt
        assert contract["source_card_id"] == darkness["id"]
        assert contract["submission_parameter"] == "declaration"
        assert contract["submission_shape"] == {
            "agent_ruling": {
                "application_id": "<unique stable application id>",
                "default_resolver": "agent",
                "ruling_kind": "generic_spell_effect",
                "decision": "<bounded Agent decision>",
                "reason": "<source-grounded reason>",
                "source_excerpt": excerpt,
            }
        }
        assert contract["casting_source"] == {
            "grant_method": "innate",
            "instruction": (
                "for grant_method=innate, omit signature_free_cast so the engine consumes "
                "the recorded innate resource; for other grants, signature_free_cast is "
                "legal only when the actor card records this spell as a Signature Spell"
            ),
        }

        ruling = {
            "application_id": "darkness-at-grid-origin",
            "default_resolver": "agent",
            "ruling_kind": "generic_spell_effect",
            "decision": "The darkness originates at the declared encounter point.",
            "reason": "The exact persisted spell clause permits an Agent-selected point.",
            "source_excerpt": excerpt,
        }
        with pytest.raises(
            Exception,
            match=r'declaration=\{"agent_ruling": \{\.\.\.\}\}',
        ):
            await _raw(
                server,
                "combat_cast_spell",
                {
                    **arguments,
                    "component_ruling": ruling,
                    "idempotency_key": "agent-darkness-wrong-parameter",
                },
            )
        with pytest.raises(
            Exception,
            match=r'declaration=\{"agent_ruling": \{\.\.\.\}\}',
        ):
            await _raw(
                server,
                "combat_cast_spell",
                {
                    **arguments,
                    "declaration": ruling,
                    "idempotency_key": "agent-darkness-wrong-shape",
                },
            )
        with pytest.raises(Exception, match="exact persisted source-card clause"):
            await _raw(
                server,
                "combat_cast_spell",
                {
                    **arguments,
                    "declaration": {
                        "agent_ruling": {
                            **ruling,
                            "source_excerpt": "A remembered paraphrase is not evidence.",
                        }
                    },
                    "idempotency_key": "agent-darkness-invalid",
                },
            )
        committed_arguments = {
            **arguments,
            "declaration": {"agent_ruling": ruling},
            "idempotency_key": "agent-darkness-commit",
        }
        committed = await _raw(server, "combat_cast_spell", committed_arguments)
        assert committed["status"] == "committed"
        assert committed["campaign_revision"] == campaign["revision"] + 1
        assert committed["result"]["payment"]["economy"] == "innate_spell"
        solution = committed["result"]["semantic_solution"]
        assert solution["status"] == "agent_ruling_committed"
        assert solution["payment_recorded"] is True
        assert solution["agent_ruling"]["source_excerpt"] == excerpt

        caster = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[0]["id"]},
                "principal_id": "system:local",
            },
        )
        assert caster["sheet"]["resources"]["innate_spell:test-darkness"]["value"] == 0
        assert any(
            effect.get("active")
            and effect.get("concentration")
            and effect.get("source_spell_id") == darkness["id"]
            for effect in caster["sheet"]["effects"]
        )
        replay = await _raw(server, "combat_cast_spell", committed_arguments)
        assert replay == committed

    asyncio.run(exercise())


def test_hidden_perceivable_cast_requires_dm_observer_matrix(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        caster_sheet = default_character_sheet()
        caster_sheet["spellcasting"]["spell_slots"] = _slots()
        caster_sheet["content"]["spells"] = [
            _spell(
                CORE_MAGIC_MISSILE_SPELL_ID,
                "Magic Missile",
                CORE_MAGIC_MISSILE_MECHANIC_ID,
                "1 action",
            )
        ]
        target_sheet = default_character_sheet()
        target_sheet["combat"]["hp"] = {"value": 20, "max": 20, "temp": 0}
        campaign, actors = await _campaign_with_combat(
            server,
            [("Hidden Caster", caster_sheet), ("Observer", target_sheet)],
            hidden_caster=True,
        )

        pending = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign["id"],
                "actor_id": actors[0]["id"],
                "spell_id": CORE_MAGIC_MISSILE_SPELL_ID,
                "cast_level": 1,
                "target_allocations": [{"target_id": actors[1]["id"], "darts": 3}],
                "expected_revision": campaign["revision"],
                "idempotency_key": "missing-perception",
            },
        )
        assert pending["status"] == "pending_ruling"
        assert pending["default_resolver"] == "agent"
        assert pending["committed"] is False
        assert pending["missing"] == ["spell_casting_perception"]
        unchanged = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[0]["id"]},
                "principal_id": "system:local",
            },
        )
        assert unchanged["sheet"]["spellcasting"]["spell_slots"]["1"]["value"] == 1

        cast = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign["id"],
                "actor_id": actors[0]["id"],
                "spell_id": CORE_MAGIC_MISSILE_SPELL_ID,
                "cast_level": 1,
                "component_ruling": {
                    "casting_perception": [
                        {
                            "observer_id": actors[1]["id"],
                            "perceived": True,
                            "reason": "The observer hears the verbal component.",
                        }
                    ]
                },
                "target_allocations": [{"target_id": actors[1]["id"], "darts": 3}],
                "expected_revision": campaign["revision"],
                "idempotency_key": "perceived-cast",
            },
        )

        assert cast["status"] == "committed"
        caster = next(
            item for item in cast["combat"]["combatants"] if item["actor_id"] == actors[0]["id"]
        )
        assert caster["hidden"] is False
        assert caster["visible_to_actor_ids"] is None
        assert any(item["type"] == "spell_casting_perception" for item in cast["combat"]["log"])

    asyncio.run(exercise())


def test_magic_missile_targeting_opens_real_shield_reaction(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        server_module,
        "roll",
        lambda expression: engine_roll(expression, rng=random.Random(0)),
    )

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        caster_sheet = default_character_sheet()
        caster_sheet["spellcasting"]["spell_slots"] = _slots()
        caster_sheet["content"]["spells"] = [
            _spell(
                CORE_MAGIC_MISSILE_SPELL_ID,
                "Magic Missile",
                CORE_MAGIC_MISSILE_MECHANIC_ID,
                "1 action",
            )
        ]
        target_sheet = default_character_sheet()
        target_sheet["combat"]["hp"] = {"value": 20, "max": 20, "temp": 0}
        target_sheet["spellcasting"]["spell_slots"] = _slots()
        target_sheet["content"]["spells"] = [
            _spell(
                CORE_SHIELD_SPELL_ID,
                "Shield",
                CORE_SHIELD_MECHANIC_ID,
                "1 reaction, when targeted by Magic Missile",
            )
        ]
        campaign, actors = await _campaign_with_combat(
            server, [("Caster", caster_sheet), ("Shielded", target_sheet)]
        )
        cast = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign["id"],
                "actor_id": actors[0]["id"],
                "spell_id": CORE_MAGIC_MISSILE_SPELL_ID,
                "cast_level": 1,
                "target_allocations": [{"target_id": actors[1]["id"], "darts": 3}],
                "expected_revision": campaign["revision"],
                "idempotency_key": "mm-cast-shield",
            },
        )
        assert cast["status"] == "pending_reaction"
        choice = cast["choices"][0]
        assert choice["trigger"] == "magic_missile_targeted"
        before = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[1]["id"]},
                "principal_id": "system:local",
            },
        )
        assert before["sheet"]["combat"]["hp"]["value"] == 20

        resolved = await _raw(
            server,
            "combat_choice",
            {
                "campaign_id": campaign["id"],
                "actor_id": actors[1]["id"],
                "action": "resolve_defense",
                "payload": {
                    "choice_id": choice["id"],
                    "selection": {"id": CORE_SHIELD_SPELL_ID, "cast_level": 1},
                },
                "expected_revision": cast["campaign_revision"],
                "idempotency_key": "mm-shield",
            },
        )
        resolved = resolved["result"]
        assert resolved["status"] == "committed"
        assert resolved["result"]["targets"][0]["shielded"] is True
        after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[1]["id"]},
                "principal_id": "system:local",
            },
        )
        assert after["sheet"]["combat"]["hp"]["value"] == 20
        assert after["sheet"]["spellcasting"]["spell_slots"]["1"]["value"] == 0
        combatant = next(
            item for item in resolved["combat"]["combatants"] if item["actor_id"] == actors[1]["id"]
        )
        assert combatant["turn_budget"]["reaction"] == 0

    asyncio.run(exercise())


def test_magic_missile_applies_each_dart_as_separate_damage(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        server_module,
        "roll",
        lambda expression: engine_roll(expression, rng=random.Random(0)),
    )

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        caster_sheet = default_character_sheet()
        caster_sheet["spellcasting"]["spell_slots"] = _slots()
        caster_sheet["content"]["spells"] = [
            _spell(
                CORE_MAGIC_MISSILE_SPELL_ID,
                "Magic Missile",
                CORE_MAGIC_MISSILE_MECHANIC_ID,
                "1 action",
            )
        ]
        target_sheet = default_character_sheet()
        target_sheet["combat"]["hp"] = {"value": 6, "max": 6, "temp": 0}
        campaign, actors = await _campaign_with_combat(
            server, [("Caster", caster_sheet), ("Target", target_sheet)]
        )
        cast = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign["id"],
                "actor_id": actors[0]["id"],
                "spell_id": CORE_MAGIC_MISSILE_SPELL_ID,
                "cast_level": 1,
                "target_allocations": [{"target_id": actors[1]["id"], "darts": 3}],
                "expected_revision": campaign["revision"],
                "idempotency_key": "mm-cast-damage",
            },
        )
        assert cast["status"] == "committed"
        dart_results = cast["result"]["targets"][0]["dart_results"]
        assert [item["roll"]["total"] for item in dart_results] == [5, 5, 5]
        target = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[1]["id"]},
                "principal_id": "system:local",
            },
        )
        assert target["sheet"]["combat"]["hp"]["value"] == 0
        assert target["sheet"]["combat"]["death_saves"]["failures"] == 1

    asyncio.run(exercise())


def test_magic_missile_creates_per_dart_concentration_saves_and_prunes_after_failure(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        server_module,
        "roll",
        lambda expression: engine_roll(expression, rng=random.Random(0)),
    )

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        caster_sheet = default_character_sheet()
        caster_sheet["spellcasting"]["spell_slots"] = _slots()
        caster_sheet["content"]["spells"] = [
            _spell(
                CORE_MAGIC_MISSILE_SPELL_ID,
                "Magic Missile",
                CORE_MAGIC_MISSILE_MECHANIC_ID,
                "1 action",
            )
        ]
        target_sheet = default_character_sheet()
        target_sheet["combat"]["hp"] = {"value": 30, "max": 30, "temp": 0}
        target_sheet["content"]["spells"] = [
            _spell("bless", "Bless", "test.spell.bless", "1 action")
        ]
        target_sheet["content"]["features"] = [
            {
                "id": "magic-resistance-passive",
                "name": "Magic Resistance",
                "choices": {
                    "source_trait": {
                        "kind": "magic_resistance",
                        "trigger": "saving_throw",
                        "save_source_kinds": ["spell", "magical_effect"],
                        "grants": "advantage",
                        "automatic": True,
                        "source_excerpt": (
                            "The archmage has advantage on saving throws against "
                            "spells and other magical effects."
                        ),
                    }
                },
            }
        ]
        target_sheet["effects"] = [
            {
                "id": "bless-effect",
                "name": "Bless",
                "kind": "concentration",
                "source": "spell.cast",
                "source_spell_id": "bless",
                "active": True,
                "concentration": True,
                "duration": {"period": "round", "remaining": 10},
                "changes": [],
                "description": "",
            }
        ]
        campaign, actors = await _campaign_with_combat(
            server, [("Caster", caster_sheet), ("Concentrating", target_sheet)]
        )
        cast = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign["id"],
                "actor_id": actors[0]["id"],
                "spell_id": CORE_MAGIC_MISSILE_SPELL_ID,
                "cast_level": 1,
                "target_allocations": [{"target_id": actors[1]["id"], "darts": 3}],
                "expected_revision": campaign["revision"],
                "idempotency_key": "mm-cast-concentration",
            },
        )
        windows = [
            item for item in cast["combat"]["pending"] if item.get("kind") == "concentration"
        ]
        assert len(windows) == 3
        assert {item["dc"] for item in windows} == {10}

        def fail_concentration(*args, **kwargs):
            assert kwargs["rules"].facts["save_purpose"] == "concentration"
            return {
                "kind": "save",
                "ability": "constitution",
                "dc": kwargs["dc"],
                "total": 1,
                "success": False,
            }

        monkeypatch.setattr(
            server_module,
            "resolve_actor_check",
            fail_concentration,
        )
        checked = await _raw(
            server,
            "combat_concentration_check",
            {
                "campaign_id": campaign["id"],
                "target_id": actors[1]["id"],
                "dc": windows[0]["dc"],
                "effect_ids": windows[0]["effect_ids"],
                "expected_revision": cast["campaign_revision"],
                "idempotency_key": "mm-concentration-fail",
            },
        )
        assert checked["effects_active"] is False
        status = await _call(
            server,
            "combat_query",
            {"campaign_id": campaign["id"], "view": "status"},
        )
        assert not [
            item
            for item in status["pending"]
            if item.get("kind") == "concentration" and item.get("actor_id") == actors[1]["id"]
        ]

    asyncio.run(exercise())


def test_combat_cast_accepts_numbered_bonus_action_from_imported_spell_card(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        caster_sheet = default_character_sheet()
        caster_sheet["spellcasting"]["spell_slots"] = _slots()
        caster_sheet["content"]["spells"] = [
            _spell(
                "dnd5e.content.srd2014.spell.healing-word",
                "Healing Word",
                "test.spell.healing_word",
                "1 bonus action",
            )
        ]
        target_sheet = default_character_sheet()
        target_sheet["combat"]["hp"] = {"value": 1, "max": 10, "temp": 0}
        campaign, actors = await _campaign_with_combat(
            server, [("Caster", caster_sheet), ("Target", target_sheet)]
        )
        cast = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign["id"],
                "actor_id": actors[0]["id"],
                "spell_id": "dnd5e.content.srd2014.spell.healing-word",
                "cast_level": 1,
                "declaration": {"target_id": actors[1]["id"]},
                "expected_revision": campaign["revision"],
                "idempotency_key": "numbered-bonus-action",
            },
        )
        assert cast["status"] == "committed"
        combatant = next(
            item for item in cast["combat"]["combatants"] if item["actor_id"] == actors[0]["id"]
        )
        assert combatant["turn_budget"]["bonus_action"] == 0
        assert combatant["turn_budget"]["main_action"] == 1
        caster = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[0]["id"]},
                "principal_id": "system:local",
            },
        )
        assert caster["sheet"]["spellcasting"]["spell_slots"]["1"]["value"] == 0
        target = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[1]["id"]},
                "principal_id": "system:local",
            },
        )
        assert target["sheet"]["combat"]["hp"]["value"] > 1

    asyncio.run(exercise())
