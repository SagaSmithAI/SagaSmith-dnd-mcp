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
from sagasmith_dnd.standard_content import build_standard2014_content
from sagasmith_dnd.standard_spell_ids import (
    CORE_BLADE_WARD_SPELL_ID,
    CORE_FLY_MECHANIC_ID,
    CORE_FLY_SPELL_ID,
    CORE_HYPNOTIC_PATTERN_MECHANIC_ID,
    CORE_HYPNOTIC_PATTERN_SPELL_ID,
    CORE_INVISIBILITY_MECHANIC_ID,
    CORE_INVISIBILITY_SPELL_ID,
    CORE_WITCH_BOLT_SPELL_ID,
)

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
    if name in {"combat_movement", "combat_hp_change"}:
        return result["result"]
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


def _hypnotic_pattern() -> dict:
    return {
        "id": CORE_HYPNOTIC_PATTERN_SPELL_ID,
        "name": "Hypnotic Pattern",
        "level": 3,
        "grant": {
            "source_type": "class",
            "source_key": "bard",
            "method": "known",
        },
        "access": {"known": True, "prepared": True},
        "definition": {
            "casting_time": "1 action",
            "range": {
                "kind": "distance",
                "normal_ft": 120,
                "long_ft": 120,
            },
            "duration": {
                "kind": "timed",
                "value": 1,
                "unit": "minute",
                "concentration": True,
            },
            "components": {
                "verbal": False,
                "somatic": True,
                "material": True,
                "material_description": (
                    "a glowing stick of incense or a crystal vial "
                    "filled with phosphorescent material"
                ),
            },
            "effect": (
                "Each creature in the area who sees the pattern must make a Wisdom saving throw."
            ),
        },
        "mechanic_refs": [CORE_HYPNOTIC_PATTERN_MECHANIC_ID],
    }


def _fly() -> dict:
    return {
        "id": CORE_FLY_SPELL_ID,
        "name": "Fly",
        "level": 3,
        "grant": {
            "source_type": "class",
            "source_key": "wizard",
            "method": "known",
        },
        "access": {"known": True, "prepared": True},
        "definition": {
            "casting_time": "1 action",
            "range": {"kind": "touch"},
            "duration": {
                "kind": "timed",
                "value": 10,
                "unit": "minute",
                "concentration": True,
            },
            "components": {
                "verbal": True,
                "somatic": True,
                "material": True,
                "material_description": "a wing feather from any bird",
            },
            "effect": ("The target gains a flying speed of 60 feet for the duration."),
        },
        "mechanic_refs": [CORE_FLY_MECHANIC_ID],
        "pack_id": "dnd5e.content.srd2014",
        "pack_version": "1.16.0",
        "rule_refs": ["bundled:srd2014/07_Spells/Spells_Each/Fly.md"],
    }


def _invisibility() -> dict:
    return {
        "id": CORE_INVISIBILITY_SPELL_ID,
        "name": "Invisibility",
        "level": 2,
        "grant": {
            "source_type": "class",
            "source_key": "bard",
            "method": "known",
        },
        "access": {"known": True, "prepared": True},
        "definition": {
            "casting_time": "1 action",
            "range": {"kind": "touch"},
            "duration": {
                "kind": "timed",
                "value": 1,
                "unit": "hour",
                "concentration": True,
            },
            "components": {
                "verbal": True,
                "somatic": True,
                "material": True,
                "material_description": "an eyelash encased in gum arabic",
            },
            "effect": (
                "A creature you touch becomes invisible until the spell "
                "ends. The spell ends for a target that attacks or casts "
                "a spell."
            ),
        },
        "mechanic_refs": [CORE_INVISIBILITY_MECHANIC_ID],
        "pack_id": "dnd5e.content.srd2014",
        "pack_version": "1.18.0",
        "rule_refs": ["bundled:srd2014/07_Spells/Spells_Each/Invisibility.md"],
    }


def _standard_spell(spell_id: str) -> dict:
    _manifest, artifacts = build_standard2014_content()
    artifact = next(item for item in artifacts if item["id"] == spell_id)
    card = dict(artifact["card"])
    card.pop("classes", None)
    card.update(
        id=artifact["id"],
        pack_id="dnd5e.content.standard2014",
        pack_version="1.0.0",
        rule_refs=list(artifact["rule_refs"]),
        mechanic_refs=list(artifact["mechanic_refs"]),
    )
    card["access"] = {
        **dict(card["access"]),
        "known": True,
        "prepared": True,
    }
    card["grant"] = {
        "source_type": "class",
        "source_key": "wizard",
        "method": "known",
    }
    return card


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
                "character_create_from",
                {
                    "mode": "direct",
                    "payload": {"campaign_id": campaign["id"], "name": name, "sheet": sheet},
                    "principal_id": "system:local",
                    "idempotency_key": f"actor-{index}",
                },
            )
        )
    refreshed = await _call(
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
            "expected_revision": refreshed["revision"],
            "idempotency_key": "play",
        },
    )
    positions = positions or [(index, 0) for index in range(len(actors))]
    started = await _call(
        server,
        "combat_start",
        {
            "positioning_mode": "grid",
            "battle_map": {"width_cells": 40, "height_cells": 40},
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
        caster_after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[0]["id"]},
                "principal_id": "system:local",
            },
        )
        target_after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[1]["id"]},
                "principal_id": "system:local",
            },
        )
        assert caster_after["sheet"]["spellcasting"]["spell_slots"]["1"]["value"] == 0
        assert target_after["sheet"]["combat"]["hp"]["value"] > 1

        replay = await _raw(server, "combat_cast_spell", arguments)
        assert replay["campaign_revision"] == result["campaign_revision"]
        target_replayed = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[1]["id"]},
                "principal_id": "system:local",
            },
        )
        assert (
            target_replayed["sheet"]["combat"]["hp"]["value"]
            == target_after["sheet"]["combat"]["hp"]["value"]
        )

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
        caster_after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[0]["id"]},
                "principal_id": "system:local",
            },
        )
        assert caster_after["sheet"]["spellcasting"]["spell_slots"]["2"]["value"] == 0

    asyncio.run(exercise())


def test_guiding_bolt_commits_locked_on_hit_effect_without_agent_ruling(
    tmp_path: Path, monkeypatch
) -> None:
    _deterministic_rolls(monkeypatch)

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        caster = default_character_sheet()
        caster["abilities"]["wisdom"]["score"] = 18
        caster["spellcasting"].update(ability="wisdom", spell_slots=_slot(1))
        spell = _spell("Guiding Bolt", 1, casting_time="1 action", range_ft=120)
        caster["content"]["spells"] = [spell]
        target = default_character_sheet()
        target["combat"]["hp"] = {"value": 50, "max": 50, "temp": 0}
        target["combat"]["ac"] = {"base": 1, "override": 1}
        campaign_id, revision, actors = await _campaign_with_combat(
            server,
            [("Cleric", caster), ("Target", target)],
            positions=[(0, 0), (5, 0)],
        )

        cast = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[0]["id"],
                "spell_id": spell["id"],
                "cast_level": 1,
                "expected_revision": revision,
                "idempotency_key": "guiding-bolt",
            },
        )
        settled = await _raw(
            server,
            "combat_resolve_attack",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[0]["id"],
                "target_id": actors[1]["id"],
                "action": {"spell_resolution_id": cast["result"]["resolution_id"]},
                "expected_revision": cast["campaign_revision"],
                "idempotency_key": "guiding-bolt-hit",
            },
        )

        assert settled["status"] == "committed"
        assert "pending_on_hit_ruling_id" not in settled["result"]
        effects = settled["result"]["standard_on_hit_effects"]
        assert len(effects) == 1
        assert effects[0]["kind"] == "next_attack_advantage"
        assert effects[0]["target_id"] == actors[1]["id"]
        assert not any(
            item.get("trigger") == "attack_on_hit_effect"
            for item in settled["combat"].get("pending", [])
            if isinstance(item, dict)
        )

    asyncio.run(exercise())


def test_witch_bolt_hard_runtime_tethers_sustains_and_breaks_on_range(
    tmp_path: Path, monkeypatch
) -> None:
    _deterministic_rolls(monkeypatch)

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        caster = default_character_sheet()
        caster["abilities"]["intelligence"]["score"] = 18
        caster["combat"]["hp"] = {"value": 100, "max": 100, "temp": 0}
        caster["spellcasting"].update(
            ability="intelligence",
            spell_slots=_slot(1),
        )
        witch_bolt = _standard_spell(CORE_WITCH_BOLT_SPELL_ID)
        caster["content"]["spells"] = [witch_bolt]
        target = default_character_sheet()
        target["combat"]["hp"] = {"value": 50, "max": 50, "temp": 0}
        target["combat"]["ac"] = {"base": 1, "override": 1}
        campaign_id, revision, actors = await _campaign_with_combat(
            server,
            [("Wizard", caster), ("Target", target)],
            positions=[(0, 0), (4, 0)],
        )

        cast = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[0]["id"],
                "spell_id": witch_bolt["id"],
                "cast_level": 1,
                "expected_revision": revision,
                "idempotency_key": "witch-bolt-cast",
            },
        )
        resolved = await _raw(
            server,
            "combat_resolve_attack",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[0]["id"],
                "target_id": actors[1]["id"],
                "action": {"spell_resolution_id": cast["result"]["resolution_id"]},
                "expected_revision": cast["campaign_revision"],
                "idempotency_key": "witch-bolt-hit",
            },
        )

        assert resolved["status"] == "committed"
        tether = resolved["result"]["witch_bolt"]["effect"]
        assert tether["active"] is True
        assert tether["repeat_damage"] == "1d12"
        caster_after_hit = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[0]["id"]},
                "principal_id": "system:local",
            },
        )
        assert any(
            item.get("active")
            and item.get("concentration")
            and item.get("id") == tether["concentration_effect_id"]
            for item in caster_after_hit["sheet"]["effects"]
        )

        caster_end = await _raw(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[0]["id"],
                "expected_revision": resolved["campaign_revision"],
                "idempotency_key": "witch-caster-end",
            },
        )
        target_end = await _raw(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[1]["id"],
                "expected_revision": caster_end["campaign_revision"],
                "idempotency_key": "witch-target-end",
            },
        )
        target_before = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[1]["id"]},
                "principal_id": "system:local",
            },
        )
        sustained = await _raw(
            server,
            "combat_common_action",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[0]["id"],
                "action": "sustain_spell",
                "payload": {
                    "effect_id": tether["id"],
                    "target_total_cover": False,
                    "agent_ruling": {
                        "default_resolver": "agent",
                        "ruling_kind": "agent_dm_adjudication",
                        "decision": "The target has no total cover.",
                        "reason": "Both tokens remain in an unobstructed line on the combat map.",
                    },
                },
                "expected_revision": target_end["campaign_revision"],
                "idempotency_key": "witch-sustain",
            },
        )
        assert sustained["status"] == "committed"
        assert sustained["result"]["kind"] == "witch_bolt_sustain"
        assert sustained["result"]["damage_roll"]["expression"] == "1d12"
        target_after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[1]["id"]},
                "principal_id": "system:local",
            },
        )
        assert (
            target_after["sheet"]["combat"]["hp"]["value"]
            < target_before["sheet"]["combat"]["hp"]["value"]
        )

        next_turn = await _raw(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[0]["id"],
                "expected_revision": sustained["campaign_revision"],
                "idempotency_key": "witch-caster-second-end",
            },
        )
        moved = await _raw(
            server,
            "combat_movement",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[1]["id"],
                "action": "move",
                "payload": {"distance": 15, "destination": {"x": 7, "y": 0}},
                "principal_id": "system:local",
                "expected_revision": next_turn["campaign_revision"],
                "idempotency_key": "witch-target-out-of-range",
            },
        )
        assert moved["ended_witch_bolt_tether_ids"] == [tether["id"]]
        caster_after_move = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[0]["id"]},
                "principal_id": "system:local",
            },
        )
        concentration = next(
            item
            for item in caster_after_move["sheet"]["effects"]
            if item["id"] == tether["concentration_effect_id"]
        )
        assert concentration["active"] is False
        assert concentration["ended_reason"] == "target_outside_spell_range"

    asyncio.run(exercise())


def test_blade_ward_cast_uses_hard_standard_mechanic_without_agent_fill(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        caster = default_character_sheet()
        blade_ward = _standard_spell(CORE_BLADE_WARD_SPELL_ID)
        caster["content"]["spells"] = [blade_ward]
        target = default_character_sheet()
        campaign_id, revision, actors = await _campaign_with_combat(
            server,
            [("Wizard", caster), ("Target", target)],
        )

        cast = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[0]["id"],
                "spell_id": blade_ward["id"],
                "expected_revision": revision,
                "idempotency_key": "blade-ward-cast",
            },
        )

        assert cast["status"] == "committed"
        assert cast["result"]["automatic_effect"] == "blade_ward"
        assert "semantic_solution" not in cast["result"]
        actor = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[0]["id"]},
                "principal_id": "system:local",
            },
        )
        effect = next(
            item for item in actor["sheet"]["effects"] if item["id"] == cast["result"]["effect_id"]
        )
        assert effect["active"] is True
        assert effect["concentration"] is False
        assert effect["duration"] == {"period": "turn_end", "remaining": 2}

    asyncio.run(exercise())


def test_noncombat_fly_commits_willing_targets_and_reconciles_replacement(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Fly outside combat",
                "edition": "2014",
                "idempotency_key": "fly-campaign",
            },
        )
        caster_sheet = default_character_sheet()
        caster_sheet["spellcasting"].update(
            ability="intelligence",
            spell_slots={
                "3": {
                    "label": "3rd",
                    "value": 2,
                    "max": 2,
                    "recovers_on": "long_rest",
                    "source_key": "wizard",
                }
            },
        )
        caster_sheet["content"]["spells"] = [_fly()]
        caster = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Caster", "sheet": caster_sheet},
                "principal_id": "system:local",
                "idempotency_key": "fly-caster",
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
                    "sheet": default_character_sheet(),
                },
                "principal_id": "system:local",
                "idempotency_key": "fly-target",
            },
        )
        first_arguments = {
            "character_id": caster["id"],
            "action": "cast_spell",
            "payload": {
                "spell_id": CORE_FLY_SPELL_ID,
                "cast_level": 3,
                "target_character_ids": [target["id"]],
                "willing_target_ids": [target["id"]],
            },
            "expected_revision": caster["revision"],
            "idempotency_key": "fly-first",
        }
        first = await _call(
            server,
            "character_action",
            first_arguments,
        )
        assert first["status"] == "committed"
        assert first["result"]["automatic_effect"] == "fly"
        assert first["result"]["target_ids"] == [target["id"]]
        assert await _call(server, "character_action", first_arguments) == first
        target_flying = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": target["id"]},
                "principal_id": "system:local",
            },
        )
        assert target_flying["derived"]["speed"]["fly"] == 60

        caster_after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": caster["id"]},
                "principal_id": "system:local",
            },
        )
        second = await _call(
            server,
            "character_action",
            {
                "character_id": caster["id"],
                "action": "cast_spell",
                "payload": {
                    "spell_id": CORE_FLY_SPELL_ID,
                    "cast_level": 3,
                    "target_character_ids": [caster["id"]],
                    "willing_target_ids": [caster["id"]],
                },
                "expected_revision": caster_after["revision"],
                "idempotency_key": "fly-second",
            },
        )
        assert second["status"] == "committed"
        target_grounded = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": target["id"]},
                "principal_id": "system:local",
            },
        )
        caster_flying = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": caster["id"]},
                "principal_id": "system:local",
            },
        )
        assert target_grounded["derived"]["speed"]["fly"] == 0
        assert caster_flying["derived"]["speed"]["fly"] == 60
        old_effect = next(
            effect
            for effect in target_grounded["sheet"]["effects"]
            if effect["kind"] == "spell_fly"
        )
        assert old_effect["active"] is False
        assert old_effect["ended_reason"] == "source_effect_ended"

    asyncio.run(exercise())


def test_combat_fly_uses_touch_range_and_encounter_dependency(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        caster = default_character_sheet()
        caster["spellcasting"].update(
            ability="intelligence",
            spell_slots=_slot(3),
        )
        caster["content"]["spells"] = [_fly()]
        campaign_id, revision, actors = await _campaign_with_combat(
            server,
            [("Caster", caster), ("Willing ally", default_character_sheet())],
            positions=[(0, 0), (1, 0)],
        )
        cast = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[0]["id"],
                "spell_id": CORE_FLY_SPELL_ID,
                "cast_level": 3,
                "declaration": {
                    "target_ids": [actors[1]["id"]],
                    "willing_target_ids": [actors[1]["id"]],
                },
                "expected_revision": revision,
                "idempotency_key": "combat-fly",
            },
        )

        assert cast["status"] == "committed"
        assert cast["result"]["kind"] == "fly"
        assert cast["result"]["targets"][0]["flying_speed_ft"] == 60
        assert cast["combat"]["dependent_effects"][0]["mechanic_id"] == CORE_FLY_MECHANIC_ID
        target = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[1]["id"]},
                "principal_id": "system:local",
            },
        )
        assert target["derived"]["speed"]["fly"] == 60

    asyncio.run(exercise())


def test_noncombat_invisibility_commits_targets_and_reconciles_replacement(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Invisibility outside combat",
                "edition": "2014",
                "idempotency_key": "invisibility-campaign",
            },
        )
        caster_sheet = default_character_sheet()
        caster_sheet["spellcasting"].update(
            ability="charisma",
            spell_slots={
                "2": {
                    "label": "2nd",
                    "value": 2,
                    "max": 2,
                    "recovers_on": "long_rest",
                    "source_key": "bard",
                }
            },
        )
        caster_sheet["content"]["spells"] = [_invisibility()]
        caster = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Caster", "sheet": caster_sheet},
                "principal_id": "system:local",
                "idempotency_key": "invisibility-caster",
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
                    "sheet": default_character_sheet(),
                },
                "principal_id": "system:local",
                "idempotency_key": "invisibility-target",
            },
        )
        arguments = {
            "character_id": caster["id"],
            "action": "cast_spell",
            "payload": {
                "spell_id": CORE_INVISIBILITY_SPELL_ID,
                "cast_level": 2,
                "target_character_ids": [target["id"]],
            },
            "expected_revision": caster["revision"],
            "idempotency_key": "invisibility-first",
        }
        first = await _call(server, "character_action", arguments)

        assert first["status"] == "committed"
        assert first["result"]["automatic_effect"] == "invisibility"
        assert first["result"]["target_ids"] == [target["id"]]
        assert await _call(server, "character_action", arguments) == first
        target_invisible = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": target["id"]},
                "principal_id": "system:local",
            },
        )
        assert "invisible" in target_invisible["sheet"]["conditions"]

        caster_after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": caster["id"]},
                "principal_id": "system:local",
            },
        )
        second = await _call(
            server,
            "character_action",
            {
                "character_id": caster["id"],
                "action": "cast_spell",
                "payload": {
                    "spell_id": CORE_INVISIBILITY_SPELL_ID,
                    "cast_level": 2,
                    "target_character_ids": [caster["id"]],
                },
                "expected_revision": caster_after["revision"],
                "idempotency_key": "invisibility-second",
            },
        )

        assert second["status"] == "committed"
        target_visible = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": target["id"]},
                "principal_id": "system:local",
            },
        )
        caster_invisible = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": caster["id"]},
                "principal_id": "system:local",
            },
        )
        assert "invisible" not in target_visible["sheet"]["conditions"]
        assert "invisible" in caster_invisible["sheet"]["conditions"]
        old_effect = next(
            effect
            for effect in target_visible["sheet"]["effects"]
            if effect["source_spell_id"] == CORE_INVISIBILITY_SPELL_ID
        )
        assert old_effect["active"] is False
        assert old_effect["ended_reason"] == "source_effect_ended"

    asyncio.run(exercise())


def test_combat_invisibility_uses_touch_range_and_encounter_dependency(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        caster = default_character_sheet()
        caster["spellcasting"].update(
            ability="charisma",
            spell_slots=_slot(2),
        )
        caster["content"]["spells"] = [_invisibility()]
        campaign_id, revision, actors = await _campaign_with_combat(
            server,
            [("Caster", caster), ("Target", default_character_sheet())],
            positions=[(0, 0), (1, 0)],
        )
        cast = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[0]["id"],
                "spell_id": CORE_INVISIBILITY_SPELL_ID,
                "cast_level": 2,
                "declaration": {
                    "target_ids": [actors[1]["id"]],
                },
                "expected_revision": revision,
                "idempotency_key": "combat-invisibility",
            },
        )

        assert cast["status"] == "committed"
        assert cast["result"]["kind"] == "invisibility"
        assert cast["result"]["targets"][0]["condition"] == "invisible"
        assert (
            cast["combat"]["dependent_effects"][0]["mechanic_id"] == CORE_INVISIBILITY_MECHANIC_ID
        )
        target = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[1]["id"]},
                "principal_id": "system:local",
            },
        )
        assert "invisible" in target["sheet"]["conditions"]

    asyncio.run(exercise())


def test_witch_bolt_stale_tether_ends_without_spending_action(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _deterministic_rolls(monkeypatch)

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        caster = default_character_sheet()
        caster["abilities"]["intelligence"]["score"] = 18
        caster["combat"]["hp"] = {"value": 100, "max": 100, "temp": 0}
        caster["spellcasting"].update(
            ability="intelligence",
            spell_slots=_slot(1),
        )
        witch_bolt = _standard_spell(CORE_WITCH_BOLT_SPELL_ID)
        caster["content"]["spells"] = [witch_bolt]
        target = default_character_sheet()
        target["combat"]["ac"] = {"base": 1, "override": 1}
        campaign_id, revision, actors = await _campaign_with_combat(
            server,
            [("Wizard", caster), ("Target", target)],
            positions=[(0, 0), (4, 0)],
        )
        cast = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[0]["id"],
                "spell_id": witch_bolt["id"],
                "cast_level": 1,
                "expected_revision": revision,
                "idempotency_key": "stale-witch-cast",
            },
        )
        resolved = await _raw(
            server,
            "combat_resolve_attack",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[0]["id"],
                "target_id": actors[1]["id"],
                "action": {"spell_resolution_id": cast["result"]["resolution_id"]},
                "expected_revision": cast["campaign_revision"],
                "idempotency_key": "stale-witch-hit",
            },
        )
        tether = resolved["result"]["witch_bolt"]["effect"]
        damaged = await _raw(
            server,
            "combat_hp_change",
            {
                "campaign_id": campaign_id,
                "target_id": actors[0]["id"],
                "action": "damage",
                "payload": {"parts": [{"amount": 60, "damage_type": "force"}]},
                "principal_id": "system:local",
                "expected_revision": resolved["campaign_revision"],
                "idempotency_key": "damage-witch-concentration",
            },
        )
        concentration = next(
            item for item in damaged["combat"]["pending"] if item["kind"] == "concentration"
        )
        checked = await _raw(
            server,
            "combat_concentration_check",
            {
                "campaign_id": campaign_id,
                "target_id": actors[0]["id"],
                "dc": concentration["dc"],
                "effect_ids": concentration["effect_ids"],
                "expected_revision": damaged["campaign_revision"],
                "idempotency_key": "fail-witch-concentration",
            },
        )
        assert checked["result"]["success"] is False
        sustained = await _raw(
            server,
            "combat_common_action",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[0]["id"],
                "action": "sustain_spell",
                "payload": {
                    "effect_id": tether["id"],
                    "target_total_cover": False,
                    "agent_ruling": {
                        "default_resolver": "agent",
                        "ruling_kind": "agent_dm_adjudication",
                        "decision": "The target has no total cover.",
                        "reason": "The stale tether is checked before any action payment.",
                    },
                },
                "expected_revision": checked["campaign_revision"],
                "idempotency_key": "stale-witch-sustain",
            },
        )

        assert sustained["result"]["status"] == "spell_ended"
        assert sustained["result"]["payment"] is None
        assert sustained["result"]["ended_reason"] == "concentration_ended"

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
        target_after = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[1]["id"]},
                "principal_id": "system:local",
            },
        )
        assert target_after["sheet"]["spellcasting"]["spell_slots"]["1"]["value"] == 0
        assert any(
            effect["active"] and effect["kind"] == "spell_shield"
            for effect in target_after["sheet"]["effects"]
        )

    asyncio.run(exercise())


def test_fireball_settles_saves_and_area_enumeration(tmp_path: Path, monkeypatch) -> None:
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
        first["content"]["features"] = [
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
                            "The creature has advantage on saving throws against "
                            "spells and other magical effects."
                        ),
                    }
                },
            },
            {
                "id": "evasion-passive",
                "name": "Evasion",
                "mechanic_refs": ["dnd5e.core.save.evasion"],
                "choices": {
                    "source_trait": {
                        "kind": "evasion",
                        "trigger": "dexterity_save_for_half_damage",
                        "save_ability": "dexterity",
                        "ordinary_successful_save": "half",
                        "successful_save": "none",
                        "failed_save": "half",
                        "automatic": True,
                        "source_excerpt": (
                            "If the creature is subjected to an effect that allows "
                            "it to make a Dexterity saving throw to take only half "
                            "damage, it instead takes no damage on a success and "
                            "only half damage on a failure."
                        ),
                    }
                },
            },
            {
                "id": "dark-devotion-passive",
                "name": "Dark Devotion",
                "choices": {
                    "source_trait": {
                        "kind": "save_advantage_against_conditions",
                        "trigger": "saving_throw",
                        "effect_conditions": ["charmed", "frightened"],
                        "grants": "advantage",
                        "automatic": True,
                        "source_excerpt": (
                            "The creature has advantage on saving throws against "
                            "being charmed or frightened."
                        ),
                    }
                },
            },
        ]
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
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[0]["id"]},
                "principal_id": "system:local",
            },
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
        trait_target = next(
            item for item in result["result"]["targets"] if item["target_id"] == actors[1]["id"]
        )
        assert trait_target["damage_reduction"] in {"none", "half"}
        assert trait_target["save"]["rule_receipts"] == []
        assert [receipt["mechanic_id"] for receipt in trait_target["rule_receipts"]] == [
            "dnd5e.core.save.evasion"
        ]
        assert result["combat"]["combatants"][0]["turn_budget"]["main_action"] == 0

    asyncio.run(exercise())


def test_exact_srd_lightning_bolt_id_uses_engine_contract_for_line(
    tmp_path: Path, monkeypatch
) -> None:
    _deterministic_rolls(monkeypatch)

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        caster = default_character_sheet()
        caster["abilities"]["intelligence"]["score"] = 18
        caster["spellcasting"].update(
            ability="intelligence",
            spell_slots=_slot(3),
        )
        lightning_bolt = {
            "id": "dnd5e.content.srd2014.spell.lightning-bolt",
            "name": "Lightning Bolt",
            "level": 3,
            "grant": {
                "source_type": "class",
                "source_key": "wizard",
                "method": "known",
            },
            "access": {"known": True, "prepared": True},
            "definition": {
                "casting_time": "1 action",
                "range": {"kind": "self"},
                "duration": {"kind": "instantaneous", "concentration": False},
                "components": {"verbal": True, "somatic": True, "material": True},
            },
            "resolution": None,
            "mechanic_refs": [],
        }
        caster["content"]["spells"] = [lightning_bolt]
        first = default_character_sheet()
        first["combat"]["hp"] = {"value": 50, "max": 50, "temp": 0}
        second = default_character_sheet()
        second["combat"]["hp"] = {"value": 50, "max": 50, "temp": 0}
        off_line = default_character_sheet()
        off_line["combat"]["hp"] = {"value": 50, "max": 50, "temp": 0}
        campaign_id, revision, actors = await _campaign_with_combat(
            server,
            [
                ("Wizard", caster),
                ("First", first),
                ("Second", second),
                ("Off line", off_line),
            ],
            positions=[(0, 0), (2, 0), (4, 0), (2, 1)],
        )

        result = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[0]["id"],
                "spell_id": lightning_bolt["id"],
                "cast_level": 3,
                "declaration": {
                    "origin": {"x": 11, "y": 0},
                    "target_contexts": [
                        {"target_id": actors[1]["id"], "cover": "none"},
                        {"target_id": actors[2]["id"], "cover": "none"},
                    ],
                },
                "expected_revision": revision,
                "idempotency_key": "srd-lightning-bolt",
            },
        )

        assert result["status"] == "committed"
        assert result["result"]["area"] == {
            "shape": "line",
            "origin": {"x": 11.0, "y": 0.0},
            "distance_ft": 55.0,
            "length_ft": 100,
            "width_ft": 5,
            "targets": [
                {"target_id": actors[1]["id"], "distance_ft": 10.0, "cover": "none"},
                {"target_id": actors[2]["id"], "distance_ft": 20.0, "cover": "none"},
            ],
        }
        assert result["result"]["damage_roll"]["expression"] == "8d6"
        assert {item["target_id"] for item in result["result"]["targets"]} == {
            actors[1]["id"],
            actors[2]["id"],
        }

    asyncio.run(exercise())


def test_hypnotic_pattern_hard_settles_cube_saves_and_every_end_condition(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        caster = default_character_sheet()
        caster["abilities"]["charisma"]["score"] = 30
        caster["spellcasting"].update(
            ability="charisma",
            spell_slots=_slot(3),
        )
        caster["content"]["spells"] = [_hypnotic_pattern()]
        first = default_character_sheet()
        first["abilities"]["wisdom"]["score"] = 1
        second = default_character_sheet()
        second["abilities"]["wisdom"]["score"] = 1
        blinded = default_character_sheet()
        blinded["conditions"] = ["blinded"]
        outsider = default_character_sheet()
        campaign_id, revision, actors = await _campaign_with_combat(
            server,
            [
                ("Bard", caster),
                ("First enemy", first),
                ("Second enemy", second),
                ("Blinded helper", blinded),
                ("Outsider", outsider),
            ],
            positions=[(0, 0), (2, 1), (3, 1), (2, 2), (8, 8)],
        )
        with pytest.raises(Exception, match="exactly 6 by 6"):
            await _raw(
                server,
                "combat_cast_spell",
                {
                    "campaign_id": campaign_id,
                    "actor_id": actors[0]["id"],
                    "spell_id": CORE_HYPNOTIC_PATTERN_SPELL_ID,
                    "cast_level": 3,
                    "declaration": {
                        "origin": {"x": 1, "y": 0},
                        "cube": {
                            "min": {"x": 1, "y": 0},
                            "max": {"x": 5, "y": 5},
                        },
                    },
                    "expected_revision": revision,
                    "idempotency_key": "invalid-hypnotic-cube",
                },
            )
        arguments = {
            "campaign_id": campaign_id,
            "actor_id": actors[0]["id"],
            "spell_id": CORE_HYPNOTIC_PATTERN_SPELL_ID,
            "cast_level": 3,
            "declaration": {
                "origin": {"x": 1, "y": 0},
                "cube": {
                    "min": {"x": 1, "y": 0},
                    "max": {"x": 6, "y": 5},
                },
            },
            "expected_revision": revision,
            "idempotency_key": "hypnotic-pattern",
        }
        cast = await _raw(server, "combat_cast_spell", arguments)
        replay = await _raw(server, "combat_cast_spell", arguments)

        assert replay == cast
        assert cast["status"] == "committed"
        assert cast["result"]["kind"] == "hypnotic_pattern"
        assert cast["result"]["save_dc"] == 20
        results = {item["target_id"]: item for item in cast["result"]["targets"]}
        assert set(results) == {
            actors[1]["id"],
            actors[2]["id"],
            actors[3]["id"],
        }
        assert results[actors[1]["id"]]["outcome"] == "affected"
        assert results[actors[2]["id"]]["outcome"] == "affected"
        assert results[actors[3]["id"]]["outcome"] == "did_not_see_pattern"
        assert results[actors[3]["id"]]["save"] is None

        first_card = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[1]["id"]},
                "principal_id": "system:local",
            },
        )
        assert {"charmed", "incapacitated"} <= set(first_card["sheet"]["conditions"])
        first_combatant = next(
            item for item in cast["combat"]["combatants"] if item["actor_id"] == actors[1]["id"]
        )
        assert first_combatant["speed_multiplier"] == 0.0

        state = cast
        for index in range(3):
            state = await _raw(
                server,
                "combat_end_turn",
                {
                    "campaign_id": campaign_id,
                    "actor_id": actors[index]["id"],
                    "expected_revision": state["campaign_revision"],
                    "idempotency_key": f"hypnotic-end-{index}",
                },
            )
        shaken = await _raw(
            server,
            "combat_common_action",
            {
                "campaign_id": campaign_id,
                "actor_id": actors[3]["id"],
                "action": "shake_hypnotic_pattern",
                "target_id": actors[1]["id"],
                "expected_revision": state["campaign_revision"],
                "idempotency_key": "shake-first-awake",
            },
        )
        assert shaken["condition_resolution"]["ended_reason"] == "shaken_awake"
        first_card = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[1]["id"]},
                "principal_id": "system:local",
            },
        )
        assert "charmed" not in first_card["sheet"]["conditions"]
        assert "incapacitated" not in first_card["sheet"]["conditions"]

        damaged = await _raw(
            server,
            "combat_hp_change",
            {
                "campaign_id": campaign_id,
                "target_id": actors[0]["id"],
                "action": "damage",
                "payload": {"parts": [{"amount": 100, "damage_type": "force"}]},
                "principal_id": "system:local",
                "expected_revision": shaken["campaign_revision"],
                "idempotency_key": "break-pattern-concentration",
            },
        )
        second_card = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actors[2]["id"]},
                "principal_id": "system:local",
            },
        )
        assert "charmed" not in second_card["sheet"]["conditions"]
        assert "incapacitated" not in second_card["sheet"]["conditions"]
        links = damaged["combat"]["dependent_effects"]
        assert links
        assert all(link["active"] is False for link in links)
        assert {link["ended_reason"] for link in links} == {
            "target_effect_ended",
            "source_effect_ended",
        }

    asyncio.run(exercise())


def test_sacred_flame_direct_save_needs_no_manual_damage_step(tmp_path: Path, monkeypatch) -> None:
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
