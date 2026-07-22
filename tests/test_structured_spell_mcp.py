import asyncio
import random
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet
from sagasmith_dnd.combat_engine import roll_attack_action as engine_roll_attack_action
from sagasmith_dnd.engine import roll as engine_roll
from sagasmith_dnd.spell_resolution import (
    SPELL_RESOLUTION_MECHANIC_ID,
    known_spell_resolution,
)
from sagasmith_dnd.spells import CORE_SHIELD_MECHANIC_ID, CORE_SHIELD_SPELL_ID

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


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


def _spell(name: str, level: int, *, casting_time: str, range_ft: int) -> dict:
    resolution = known_spell_resolution(name)
    assert resolution is not None
    identifier = f"test.spell.{name.casefold().replace(' ', '-')}"
    return {
        "id": identifier,
        "name": name,
        "level": level,
        "grant": {"source_type": "class", "source_key": "test", "method": "known"},
        "access": {"known": True, "prepared": True},
        "definition": {
            "casting_time": casting_time,
            "range": {"kind": "distance", "normal_ft": range_ft, "long_ft": range_ft},
            "duration": {"kind": "instantaneous", "concentration": False},
            "components": {"verbal": True, "somatic": True},
        },
        "resolution": resolution,
        "mechanic_refs": [SPELL_RESOLUTION_MECHANIC_ID],
    }


def _slot(level: int, value: int = 1) -> dict:
    return {
        str(level): {
            "label": f"Level {level}",
            "value": value,
            "max": value,
            "recovers_on": "long_rest",
            "source_key": "test",
        }
    }


def _shield() -> dict:
    return {
        "id": CORE_SHIELD_SPELL_ID,
        "name": "Shield",
        "level": 1,
        "grant": {"source_type": "class", "source_key": "wizard", "method": "known"},
        "access": {"known": True, "prepared": True},
        "definition": {
            "casting_time": "1 reaction",
            "range": {"kind": "self"},
            "duration": {"kind": "timed", "value": 1, "unit": "round"},
            "components": {"verbal": True, "somatic": True},
        },
        "mechanic_refs": [CORE_SHIELD_MECHANIC_ID],
    }


async def _campaign_with_combat(
    server,
    sheets: list[tuple[str, dict]],
    *,
    positions: list[tuple[int, int]] | None = None,
) -> tuple[str, int, list[dict]]:
    campaign = await _call(
        server,
        "campaign_create",
        {"name": "Structured spells", "edition": "2014", "idempotency_key": "campaign"},
    )
    actors = []
    for index, (name, sheet) in enumerate(sheets):
        actors.append(
            await _call(
                server,
                "character_create",
                {
                    "campaign_id": campaign["id"],
                    "name": name,
                    "sheet": sheet,
                    "idempotency_key": f"actor-{index}",
                },
            )
        )
    refreshed = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
    phase = await _call(
        server,
        "game_phase",
        {
            "campaign_id": campaign["id"],
            "action": "set",
            "tool_profile": "play",
            "expected_revision": refreshed["revision"],
            "idempotency_key": "play",
        },
    )
    positions = positions or [(index, 0) for index in range(len(actors))]
    started = await _call(
        server,
        "combat_start",
        {
            "campaign_id": campaign["id"],
            "participant_ids": [item["id"] for item in actors],
            "participant_config": [
                {
                    "actor_id": item["id"],
                    "initiative": 20 - index,
                    "position": {"x": positions[index][0], "y": positions[index][1]},
                    "disposition": "friendly" if index == 0 else "hostile",
                }
                for index, item in enumerate(actors)
            ],
            "expected_revision": phase["campaign_revision"],
            "idempotency_key": "start",
        },
    )
    return campaign["id"], started["campaign_revision"], actors


def _deterministic_rolls(monkeypatch) -> None:
    monkeypatch.setattr(
        server_module,
        "roll",
        lambda expression: engine_roll(expression, rng=random.Random(7)),
    )
    monkeypatch.setattr(
        server_module,
        "roll_attack_action",
        lambda *, plan: engine_roll_attack_action(plan=plan, rng=random.Random(7)),
    )


def test_healing_word_cast_roll_and_feature_bonus_commit_once(tmp_path: Path, monkeypatch) -> None:
    _deterministic_rolls(monkeypatch)

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        caster = default_character_sheet()
        caster["abilities"]["wisdom"]["score"] = 16
        caster["spellcasting"].update(ability="wisdom", spell_slots=_slot(1))
        spell = _spell("Healing Word", 1, casting_time="1 bonus action", range_ft=60)
        caster["content"]["spells"] = [spell]
        caster["content"]["features"] = [
            {
                "id": "dnd5e.content.srd2014.feature.life-domain-disciple-of-life",
                "name": "Disciple of Life",
                "source_key": "Life Domain",
            }
        ]
        target = default_character_sheet()
        target["combat"]["hp"] = {"value": 1, "max": 20, "temp": 0}
        campaign_id, revision, actors = await _campaign_with_combat(
            server, [("Cleric", caster), ("Ally", target)], positions=[(0, 0), (4, 0)]
        )
        arguments = {
            "campaign_id": campaign_id,
            "actor_id": actors[0]["id"],
            "spell_id": spell["id"],
            "cast_level": 1,
            "declaration": {"target_id": actors[1]["id"]},
            "expected_revision": revision,
            "idempotency_key": "healing-word",
        }

        result = await _raw(server, "combat_cast_spell", arguments)

        assert result["status"] == "committed"
        assert result["result"]["kind"] == "healing"
        assert result["result"]["healing"]["bonus_amount"] == 3
        assert result["combat"]["combatants"][0]["turn_budget"]["bonus_action"] == 0
        caster_after = await _call(server, "character_get", {"character_id": actors[0]["id"]})
        target_after = await _call(server, "character_get", {"character_id": actors[1]["id"]})
        assert caster_after["sheet"]["spellcasting"]["spell_slots"]["1"]["value"] == 0
        assert target_after["sheet"]["combat"]["hp"]["value"] > 1

        replay = await _raw(server, "combat_cast_spell", arguments)
        assert replay["campaign_revision"] == result["campaign_revision"]
        target_replayed = await _call(
            server, "character_get", {"character_id": actors[1]["id"]}
        )
        assert target_replayed["sheet"]["combat"]["hp"]["value"] == target_after["sheet"][
            "combat"
        ]["hp"]["value"]

    asyncio.run(exercise())


def test_scorching_ray_cast_locks_then_settles_each_source_bound_attack(
    tmp_path: Path, monkeypatch
) -> None:
    _deterministic_rolls(monkeypatch)

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        caster = default_character_sheet()
        caster["abilities"]["intelligence"]["score"] = 18
        caster["spellcasting"].update(ability="intelligence", spell_slots=_slot(2))
        spell = _spell("Scorching Ray", 2, casting_time="1 action", range_ft=120)
        caster["content"]["spells"] = [spell]
        target = default_character_sheet()
        target["combat"]["hp"] = {"value": 100, "max": 100, "temp": 0}
        target["combat"]["ac"] = {"base": 1, "override": 1}
        campaign_id, revision, actors = await _campaign_with_combat(
            server, [("Mage", caster), ("Target", target)], positions=[(0, 0), (5, 0)]
        )

        cast = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[0]["id"],
                "spell_id": spell["id"],
                "cast_level": 2,
                "expected_revision": revision,
                "idempotency_key": "scorching-ray",
            },
        )
        assert cast["status"] == "pending_resolution"
        resolution_id = cast["result"]["resolution_id"]
        assert cast["result"]["remaining_attacks"] == 3

        with pytest.raises(Exception, match="pending"):
            await _raw(
                server,
                "combat_end_turn",
                {
                    "campaign_id": campaign_id,
                    "actor_id": actors[0]["id"],
                    "expected_revision": cast["campaign_revision"],
                    "idempotency_key": "ray-premature-turn-end",
                },
            )
        with pytest.raises(Exception, match="pending"):
            await _raw(
                server,
                "combat_end",
                {
                    "campaign_id": campaign_id,
                    "expected_revision": cast["campaign_revision"],
                    "idempotency_key": "ray-premature-combat-end",
                },
            )

        current_revision = cast["campaign_revision"]
        results = []
        for index, remaining in enumerate((2, 1, 0), start=1):
            settled = await _raw(
                server,
                "combat_resolve_attack",
                {
                    "campaign_id": campaign_id,
                    "actor_id": actors[0]["id"],
                    "target_id": actors[1]["id"],
                    "action": {"spell_resolution_id": resolution_id},
                    "expected_revision": current_revision,
                    "idempotency_key": f"ray-{index}",
                },
            )
            assert settled["status"] == "committed"
            assert settled["result"]["spell_id"] == spell["id"]
            assert settled["result"]["spell_resolution"]["remaining_attacks"] == remaining
            results.append(settled["result"])
            current_revision = settled["campaign_revision"]
        assert all(item["damage"]["damage_type"] == "fire" for item in results)
        assert not any(
            item.get("kind") == "spell_attack_resolution"
            for item in settled["combat"].get("pending", [])
        )
        caster_after = await _call(server, "character_get", {"character_id": actors[0]["id"]})
        assert caster_after["sheet"]["spellcasting"]["spell_slots"]["2"]["value"] == 0

    asyncio.run(exercise())


def test_scorching_ray_reuses_shield_reaction_before_each_damage_roll(
    tmp_path: Path, monkeypatch
) -> None:
    _deterministic_rolls(monkeypatch)

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        caster = default_character_sheet()
        caster["abilities"]["intelligence"]["score"] = 18
        caster["spellcasting"].update(ability="intelligence", spell_slots=_slot(2))
        ray = _spell("Scorching Ray", 2, casting_time="1 action", range_ft=120)
        caster["content"]["spells"] = [ray]
        target = default_character_sheet()
        target["combat"]["hp"] = {"value": 30, "max": 30, "temp": 0}
        target["combat"]["ac"] = {"base": 13, "override": 13}
        target["abilities"]["intelligence"]["score"] = 16
        target["spellcasting"].update(ability="intelligence", spell_slots=_slot(1))
        target["content"]["spells"] = [_shield()]
        campaign_id, revision, actors = await _campaign_with_combat(
            server, [("Mage", caster), ("Shielded", target)], positions=[(0, 0), (5, 0)]
        )
        cast = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[0]["id"],
                "spell_id": ray["id"],
                "cast_level": 2,
                "expected_revision": revision,
                "idempotency_key": "ray-cast",
            },
        )
        resolution_id = cast["result"]["resolution_id"]
        first = await _raw(
            server,
            "combat_resolve_attack",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[0]["id"],
                "target_id": actors[1]["id"],
                "action": {"spell_resolution_id": resolution_id},
                "expected_revision": cast["campaign_revision"],
                "idempotency_key": "ray-hit",
            },
        )
        assert first["status"] == "pending_reaction"

        defended = await _raw(
            server,
            "combat_choice",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[1]["id"],
                "action": "resolve_defense",
                "payload": {
                    "choice_id": first["choice"]["id"],
                    "selection": {"id": CORE_SHIELD_SPELL_ID, "cast_level": 1},
                },
                "expected_revision": first["campaign_revision"],
                "idempotency_key": "shield-ray",
            },
        )

        assert defended["status"] == "committed"
        defense_result = defended["result"]["result"]
        assert defense_result["hit"] is False
        assert defense_result["damage"] is None
        assert defense_result["spell_resolution"]["remaining_attacks"] == 2
        target_after = await _call(server, "character_get", {"character_id": actors[1]["id"]})
        assert target_after["sheet"]["spellcasting"]["spell_slots"]["1"]["value"] == 0
        assert any(
            effect["active"] and effect["kind"] == "spell_shield"
            for effect in target_after["sheet"]["effects"]
        )

    asyncio.run(exercise())


def test_fireball_settles_saves_and_area_enumeration(
    tmp_path: Path, monkeypatch
) -> None:
    _deterministic_rolls(monkeypatch)

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        caster = default_character_sheet()
        caster["abilities"]["wisdom"]["score"] = 18
        caster["spellcasting"].update(ability="wisdom", spell_slots=_slot(3))
        fireball = _spell("Fireball", 3, casting_time="1 action", range_ft=150)
        caster["content"]["spells"] = [fireball]
        first = default_character_sheet()
        first["combat"]["hp"] = {"value": 50, "max": 50, "temp": 0}
        second = default_character_sheet()
        second["combat"]["hp"] = {"value": 50, "max": 50, "temp": 0}
        campaign_id, revision, actors = await _campaign_with_combat(
            server,
            [("Wizard", caster), ("Enemy", first), ("Bystander", second)],
            positions=[(0, 0), (6, 0), (7, 0)],
        )
        declaration = {
            "origin": {"x": 6, "y": 0},
            "target_contexts": [
                {"target_id": actors[1]["id"], "cover": "none"},
                {"target_id": actors[2]["id"], "cover": "half"},
            ],
        }
        with pytest.raises(Exception, match="every living combatant"):
            await _raw(
                server,
                "combat_cast_spell",
                {
                    "campaign_id": campaign_id,
                    "actor_id": actors[0]["id"],
                    "spell_id": fireball["id"],
                    "cast_level": 3,
                    "declaration": {
                        **declaration,
                        "target_contexts": declaration["target_contexts"][:1],
                    },
                    "expected_revision": revision,
                    "idempotency_key": "incomplete-fireball",
                },
            )
        unchanged = await _call(
            server, "character_get", {"character_id": actors[0]["id"]}
        )
        assert unchanged["sheet"]["spellcasting"]["spell_slots"]["3"]["value"] == 1
        result = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[0]["id"],
                "spell_id": fireball["id"],
                "cast_level": 3,
                "declaration": declaration,
                "expected_revision": revision,
                "idempotency_key": "fireball",
            },
        )

        assert result["status"] == "committed"
        assert result["result"]["kind"] == "saving_throw"
        assert {item["target_id"] for item in result["result"]["targets"]} == {
            actors[1]["id"],
            actors[2]["id"],
        }
        assert result["result"]["area"]["radius_ft"] == 20
        assert result["result"]["damage_roll"]["expression"] == "8d6"
        assert result["combat"]["combatants"][0]["turn_budget"]["main_action"] == 0

    asyncio.run(exercise())


def test_sacred_flame_direct_save_needs_no_manual_damage_step(
    tmp_path: Path, monkeypatch
) -> None:
    _deterministic_rolls(monkeypatch)

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        caster = default_character_sheet()
        caster["abilities"]["wisdom"]["score"] = 18
        caster["spellcasting"]["ability"] = "wisdom"
        sacred_flame = _spell("Sacred Flame", 0, casting_time="1 action", range_ft=60)
        caster["content"]["spells"] = [sacred_flame]
        target = default_character_sheet()
        target["combat"]["hp"] = {"value": 30, "max": 30, "temp": 0}
        campaign_id, revision, actors = await _campaign_with_combat(
            server, [("Cleric", caster), ("Target", target)], positions=[(0, 0), (2, 0)]
        )

        result = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[0]["id"],
                "spell_id": sacred_flame["id"],
                "cast_level": 0,
                "declaration": {"target_id": actors[1]["id"]},
                "expected_revision": revision,
                "idempotency_key": "sacred-flame",
            },
        )

        assert result["status"] == "committed"
        assert result["result"]["damage_roll"]["expression"] == "1d8"
        assert result["result"]["targets"][0]["save"]["kind"] == "save"
        assert result["combat"]["combatants"][0]["turn_budget"]["main_action"] == 0

    asyncio.run(exercise())
