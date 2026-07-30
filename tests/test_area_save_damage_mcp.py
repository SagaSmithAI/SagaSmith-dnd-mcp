from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet
from sagasmith_dnd.statblocks import parse_2014_statblock

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


class _SequenceRng:
    def __init__(self, *values: int) -> None:
        self.values = list(values)

    def randint(self, minimum: int, maximum: int) -> int:
        value = self.values.pop(0)
        assert minimum <= value <= maximum
        return value


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


def _storm_giant_sheet() -> dict:
    parsed = parse_2014_statblock(
        """### Storm Giant

*Huge giant, chaotic good*

**Armor Class** 16 (scale mail)
**Hit Points** 230 (20d12 + 100)
**Speed** 50 ft., swim 50 ft.

| STR | DEX | CON | INT | WIS | CHA |
|:---:|:---:|:---:|:---:|:---:|:---:|
| 29 (+9) | 14 (+2) | 20 (+5) | 16 (+3) | 18 (+4) | 18 (+4) |

**Saving Throws** Str +14, Con +10, Wis +9, Cha +9
**Skills** Arcana +8, Athletics +14, History +8, Perception +9
**Damage Resistances** cold
**Damage Immunities** lightning, thunder
**Senses** passive Perception 19
**Languages** Common, Giant
**Challenge** 13 (10,000 XP)

###### Actions

***Greatsword***. Melee Weapon Attack: +14 to hit, reach 10 ft., one target.
Hit: 30 (6d6 + 9) slashing damage.

***Lightning Strike (Recharge 5-6)***. The giant hurls a magical lightning
bolt at a point it can see within 500 feet of it. Each creature within 10 feet
of that point must make a DC 17 Dexterity saving throw, taking 54 (12d8)
lightning damage on a failed save, or half as much damage on a successful one.
""",
        source_key="monster-manual-2014:p157",
    )
    return parsed.sheet


def test_area_save_damage_derives_every_target_and_recharges_at_turn_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_save_damage = server_module.resolve_save_damage_to_sheets
    original_recharge = server_module.recharge_activities_at_turn_start

    def deterministic_save_damage(target_actors, **kwargs):
        return original_save_damage(
            target_actors,
            **kwargs,
            rng=_SequenceRng(*([1] * 12), 1, 20),
        )

    def deterministic_recharge(sheet, **kwargs):
        return original_recharge(
            sheet,
            **kwargs,
            rng=_SequenceRng(5),
        )

    monkeypatch.setattr(
        server_module,
        "resolve_save_damage_to_sheets",
        deterministic_save_damage,
    )
    monkeypatch.setattr(
        server_module,
        "recharge_activities_at_turn_start",
        deterministic_recharge,
    )

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Area save damage",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        source = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Storm Giant",
                "sheet": _storm_giant_sheet(),
                "idempotency_key": "source",
            },
        )
        actors = []
        for name in ("Target", "Nearby Ally", "Outside Creature"):
            sheet = default_character_sheet()
            sheet["edition"] = "2014"
            sheet["combat"]["hp"] = {"value": 40, "max": 40, "temp": 0}
            actors.append(
                await _call(
                    server,
                    "character_create",
                    {
                        "campaign_id": campaign["id"],
                        "name": name,
                        "sheet": sheet,
                        "idempotency_key": name.casefold().replace(" ", "-"),
                    },
                )
            )
        campaign = await _call(
            server,
            "campaign_get",
            {"campaign_id": campaign["id"]},
        )
        phase = await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": campaign["revision"],
                "idempotency_key": "play",
            },
        )
        participants = [source, *actors]
        positions = (
            {"x": 0, "y": 0},
            {"x": 4, "y": 0},
            {"x": 5, "y": 0},
            {"x": 7, "y": 0},
        )
        started = await _raw(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [item["id"] for item in participants],
                "participant_config": [
                    {
                        "actor_id": item["id"],
                        "initiative": 20 - index,
                        "position": positions[index],
                        "disposition": "hostile" if index == 0 else "friendly",
                        "death_saves": index != 0,
                    }
                    for index, item in enumerate(participants)
                ],
                "expected_revision": phase["campaign_revision"],
                "idempotency_key": "start",
            },
        )
        activity_id = "lightning-strike-recharge-5-6-action"

        with pytest.raises(Exception, match="outside the recorded range"):
            await _raw(
                server,
                "combat_use_activity",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": source["id"],
                    "activity_id": activity_id,
                    "declaration": {
                        "origin": {"x": 101, "y": 0},
                        "target_contexts": [],
                    },
                    "expected_revision": started["campaign_revision"],
                    "idempotency_key": "bad-origin",
                },
            )

        resolved = await _raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": source["id"],
                "activity_id": activity_id,
                "declaration": {
                    "origin": {"x": 4, "y": 0},
                    "target_contexts": [
                        {"target_id": actors[0]["id"], "cover": "none"},
                        {
                            "target_id": actors[1]["id"],
                            "cover": "three_quarters",
                        },
                    ],
                },
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "lightning",
            },
        )

        assert resolved["status"] == "committed"
        effect = resolved["result"]["core_effect"]
        assert effect["kind"] == "area_save_damage"
        assert effect["origin"] == {"x": 4, "y": 0}
        assert [item["target_id"] for item in effect["targets"]] == [
            actors[0]["id"],
            actors[1]["id"],
        ]
        assert [item["damage_amount"] for item in effect["targets"]] == [12, 6]
        assert [item["save_bonus"] for item in effect["targets"]] == [0, 5]
        assert effect["damage_roll"]["total"] == 12
        assert any(
            item["mechanic_id"] == "dnd5e.core.activity.area_save_damage"
            for item in resolved["result"]["rule_receipts"]
        )
        source_after = await _call(
            server,
            "character_get",
            {"character_id": source["id"]},
        )
        activity = next(
            item
            for item in source_after["sheet"]["content"]["activities"]
            if item["id"] == activity_id
        )
        assert activity["uses"]["value"] == 0
        target_after = await _call(
            server,
            "character_get",
            {"character_id": actors[0]["id"]},
        )
        ally_after = await _call(
            server,
            "character_get",
            {"character_id": actors[1]["id"]},
        )
        outside_after = await _call(
            server,
            "character_get",
            {"character_id": actors[2]["id"]},
        )
        assert target_after["sheet"]["combat"]["hp"]["value"] == 28
        assert ally_after["sheet"]["combat"]["hp"]["value"] == 34
        assert outside_after["sheet"]["combat"]["hp"]["value"] == 40

        current = resolved
        for index, actor in enumerate(actors):
            current = await _raw(
                server,
                "combat_end_turn",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": (
                        source["id"] if index == 0 else actors[index - 1]["id"]
                    ),
                    "expected_revision": current["campaign_revision"],
                    "idempotency_key": f"end-{index}",
                },
            )
        current = await _raw(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": actors[-1]["id"],
                "expected_revision": current["campaign_revision"],
                "idempotency_key": "end-last",
            },
        )

        assert current["activity_recharges"][0]["activity_id"] == activity_id
        assert current["activity_recharges"][0]["recharged"] is True
        assert any(
            item["mechanic_id"] == "dnd5e.core.activity.recharge"
            for item in current["rule_receipts"]
        )
        source_recharged = await _call(
            server,
            "character_get",
            {"character_id": source["id"]},
        )
        activity = next(
            item
            for item in source_recharged["sheet"]["content"]["activities"]
            if item["id"] == activity_id
        )
        assert activity["uses"]["value"] == 1

    asyncio.run(exercise())
