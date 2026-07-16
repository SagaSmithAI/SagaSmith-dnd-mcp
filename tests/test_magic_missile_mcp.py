import asyncio
import random
from pathlib import Path

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


async def _campaign_with_combat(server, sheets: list[tuple[str, dict]]) -> tuple[dict, list[dict]]:
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
                "character_create",
                {
                    "campaign_id": campaign["id"],
                    "name": name,
                    "sheet": sheet,
                    "idempotency_key": f"mm-actor-{index}",
                },
            )
        )
    campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
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
            "campaign_id": campaign["id"],
            "participant_ids": [item["id"] for item in actors],
            "participant_config": [
                {
                    "actor_id": item["id"],
                    "initiative": 20 - index,
                    "position": {"x": index, "y": 0},
                    "disposition": "friendly" if index == 0 else "hostile",
                }
                for index, item in enumerate(actors)
            ],
            "expected_revision": phase["campaign_revision"],
            "idempotency_key": "mm-start",
        },
    )
    return {**campaign, "revision": started["campaign_revision"]}, actors


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
        before = await _call(server, "character_get", {"character_id": actors[1]["id"]})
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
        after = await _call(server, "character_get", {"character_id": actors[1]["id"]})
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
        target = await _call(server, "character_get", {"character_id": actors[1]["id"]})
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
            item
            for item in cast["combat"]["pending"]
            if item.get("kind") == "concentration"
        ]
        assert len(windows) == 3
        assert {item["dc"] for item in windows} == {10}

        monkeypatch.setattr(
            server_module,
            "resolve_actor_check",
            lambda *args, **kwargs: {
                "kind": "save",
                "ability": "constitution",
                "dc": kwargs["dc"],
                "total": 1,
                "success": False,
            },
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
                "expected_revision": campaign["revision"],
                "idempotency_key": "numbered-bonus-action",
            },
        )
        assert cast["status"] == "pending_ruling"
        combatant = next(
            item for item in cast["combat"]["combatants"] if item["actor_id"] == actors[0]["id"]
        )
        assert combatant["turn_budget"]["bonus_action"] == 0
        assert combatant["turn_budget"]["main_action"] == 1
        caster = await _call(server, "character_get", {"character_id": actors[0]["id"]})
        assert caster["sheet"]["spellcasting"]["spell_slots"]["1"]["value"] == 0

    asyncio.run(exercise())
