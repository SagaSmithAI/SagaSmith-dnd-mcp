from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sagasmith_dnd.statblocks import parse_2014_statblock

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import (
    _agent_evidence_supports_fact,
    _compact_agent_evidence,
    create_server,
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


def _ancient_blue_dragon_sheet() -> dict:
    return parse_2014_statblock(
        """# Ancient Blue Dragon

*Gargantuan dragon, lawful evil*

**Armor Class** 22 (natural armor)
**Hit Points** 481 (26d20 + 208)
**Speed** 40 ft., burrow 40 ft., fly 80 ft.

| STR | DEX | CON | INT | WIS | CHA |
|---|---|---|---|---|---|
| 29 (+9) | 10 (+0) | 27 (+8) | 18 (+4) | 17 (+3) | 21 (+5) |

**Saving Throws** Dex +7, Con +15, Wis +10, Cha +12
**Skills** Perception +17, Stealth +7
**Damage Immunities** lightning
**Senses** blindsight 60 ft., darkvision 120 ft., passive Perception 27
**Languages** Common, Draconic
**Challenge** 23 (32,500 XP)

***Legendary Resistance (3/Day).*** If the dragon fails a saving throw, it can
choose to succeed instead.

## Actions

***Multiattack.*** The dragon can use its Frightful Presence. It then makes
three attacks: one with its bite and two with its claws.

***Bite.*** *Melee Weapon Attack:* +16 to hit, reach 15 ft., one target.
*Hit:* 20 (2d10 + 9) piercing damage plus 11 (2d10) lightning damage.

***Claw.*** *Melee Weapon Attack:* +16 to hit, reach 10 ft., one target.
*Hit:* 16 (2d6 + 9) slashing damage.

***Tail.*** *Melee Weapon Attack:* +16 to hit, reach 20 ft., one target.
*Hit:* 18 (2d8 + 9) bludgeoning damage.

***Frightful Presence.*** Each creature of the dragon's choice that is within
120 feet of the dragon and aware of it must succeed on a DC 20 Wisdom saving
throw or become frightened for 1 minute. A creature can repeat the saving throw
at the end of each of its turns, ending the effect on itself on a success. If a
creature's saving throw is successful or the effect ends for it, the creature
is immune to the dragon's Frightful Presence for the next 24 hours.

***Lightning Breath (Recharge 5-6).*** The dragon exhales lightning in a
120-foot line that is 10 feet wide. Each creature in that line must make a DC
23 Dexterity saving throw, taking 88 (16d10) lightning damage on a failed save,
or half as much damage on a successful one.

## Legendary Actions

The dragon can take 3 legendary actions, choosing from the options below. Only
one legendary action option can be used at a time and only at the end of
another creature's turn. The dragon regains spent legendary actions at the
start of its turn.

***Detect.*** The dragon makes a Wisdom (Perception) check.

***Tail Attack.*** The dragon makes a tail attack.

***Wing Attack (Costs 2 Actions).*** The dragon beats its wings. Each creature
within 15 feet of the dragon must succeed on a DC 24 Dexterity saving throw or
take 16 (2d6 + 9) bludgeoning damage and be knocked prone. The dragon can then
fly up to half its flying speed.
""",
        source_key="monster-manual-2014:p91",
    ).sheet


def _magic_resistant_hero_sheet() -> dict:
    sheet = parse_2014_statblock(
        """# Hero

*Medium humanoid, neutral*

**Armor Class** 10
**Hit Points** 500 (1d8)
**Speed** 30 ft.

| STR | DEX | CON | INT | WIS | CHA |
|---|---|---|---|---|---|
| 10 (+0) | 10 (+0) | 10 (+0) | 10 (+0) | 1 (-5) | 10 (+0) |

***Magic Resistance.*** The hero has advantage on saving throws against
spells and other magical effects.

## Actions

***Club.*** *Melee Weapon Attack:* +2 to hit, reach 5 ft., one target.
*Hit:* 2 (1d4) bludgeoning damage.
""",
        source_key="test:magic-resistant-hero",
    ).sheet
    sheet["combat"]["hp"] = {"value": 500, "max": 500, "temp": 0}
    return sheet


def test_text_only_review_repairs_contextual_dragon_ocr_without_changing_numbers() -> None:
    evidence = _compact_agent_evidence(
        "Tail. Melee Weapon Attack:+ 16 to hit, reacl:1 20ft., one target. "
        "Hit: 18 (2d8 + 9) bludgeoning damage. "
        "Wing Attack (Costs 2 Acti9ns)."
    )
    assert _agent_evidence_supports_fact(
        _compact_agent_evidence(
            "Tail. Melee Weapon Attack: +16 to hit, reach 20 ft., one target. "
            "Hit: 18 (2d8 + 9) bludgeoning damage."
        ),
        evidence,
    )
    assert _agent_evidence_supports_fact(
        _compact_agent_evidence("Wing Attack (Costs 2 Actions)."),
        evidence,
    )
    assert _agent_evidence_supports_fact(
        _compact_agent_evidence("Wing Attack (Costs 2 Acti9ns)."),
        _compact_agent_evidence("Wing Attack (Costs 2 Actions)."),
    )
    joined_numeric_evidence = _compact_agent_evidence(
        "Skills Perception + 17, Stealth +7 Damage Immunities lightning"
    )
    assert _agent_evidence_supports_fact(
        _compact_agent_evidence("Skills Perception +17, Stealth +7"),
        joined_numeric_evidence,
    )
    assert not _agent_evidence_supports_fact(
        _compact_agent_evidence(
            "Tail. Melee Weapon Attack: +16 to hit, reach 25 ft., one target."
        ),
        evidence,
    )


def test_standard_dragon_actions_execute_through_public_mcp(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Standard dragon actions",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        dragon = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Ancient Blue Dragon",
                "character_type": "monster",
                "sheet": _ancient_blue_dragon_sheet(),
                "idempotency_key": "dragon",
            },
        )
        hero_sheet = _magic_resistant_hero_sheet()
        hero = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Hero",
                "sheet": hero_sheet,
                "idempotency_key": "hero",
            },
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
        current = await _raw(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [hero["id"], dragon["id"]],
                "participant_config": [
                    {
                        "actor_id": hero["id"],
                        "initiative": 20,
                        "position": {"x": 1, "y": 0},
                        "disposition": "friendly",
                    },
                    {
                        "actor_id": dragon["id"],
                        "initiative": 10,
                        "position": {"x": 0, "y": 0},
                        "disposition": "hostile",
                        "death_saves": False,
                    },
                ],
                "expected_revision": phase["campaign_revision"],
                "idempotency_key": "start",
            },
        )

        detected = await _raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": dragon["id"],
                "activity_id": "detect-special",
                "declaration": {},
                "expected_revision": current["campaign_revision"],
                "idempotency_key": "detect",
            },
        )
        assert detected["status"] == "committed"
        assert detected["result"]["core_effect"]["effect_kind"] == "skill_check"
        assert detected["result"]["core_effect"]["activation_payment"][
            "remaining"
        ] == 2
        with pytest.raises(Exception, match="only one legendary action"):
            await _raw(
                server,
                "combat_use_activity",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": dragon["id"],
                    "activity_id": "wing-attack-costs-2-actions-special",
                    "declaration": {
                        "target_contexts": [
                            {
                                "target_id": hero["id"],
                                "cover": "none",
                            }
                        ]
                    },
                    "expected_revision": detected["campaign_revision"],
                    "idempotency_key": "same-turn-wing",
                },
            )

        current = await _raw(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": hero["id"],
                "expected_revision": detected["campaign_revision"],
                "idempotency_key": "end-hero-1",
            },
        )
        breath = await _raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": dragon["id"],
                "activity_id": "lightning-breath-recharge-5-6-action",
                "declaration": {
                    "endpoint": {"x": 24, "y": 0},
                    "target_contexts": [
                        {
                            "target_id": hero["id"],
                            "cover": "none",
                        }
                    ],
                },
                "expected_revision": current["campaign_revision"],
                "idempotency_key": "breath",
            },
        )
        assert breath["result"]["core_effect"]["contract"] == (
            "self_line_save_damage"
        )
        assert [
            item["target_id"]
            for item in breath["result"]["core_effect"]["targets"]
        ] == [hero["id"]]
        assert {
            receipt["mechanic_id"]
            for receipt in breath["result"]["core_effect"]["targets"][0][
                "save"
            ]["rule_receipts"]
        }.isdisjoint({"dnd5e.core.save.magic_resistance"})

        current = await _raw(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": dragon["id"],
                "expected_revision": breath["campaign_revision"],
                "idempotency_key": "end-dragon-1",
            },
        )
        wing = await _raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": dragon["id"],
                "activity_id": "wing-attack-costs-2-actions-special",
                "declaration": {
                    "target_contexts": [
                        {
                            "target_id": hero["id"],
                            "cover": "none",
                        }
                    ]
                },
                "expected_revision": current["campaign_revision"],
                "idempotency_key": "wing",
            },
        )
        assert wing["result"]["core_effect"]["effect_kind"] == (
            "wing_attack_2014"
        )
        assert wing["result"]["core_effect"]["activation_payment"][
            "remaining"
        ] == 1
        assert {
            receipt["mechanic_id"]
            for receipt in wing["result"]["core_effect"]["targets"][0][
                "save"
            ]["rule_receipts"]
        }.isdisjoint({"dnd5e.core.save.magic_resistance"})

        current = await _raw(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": hero["id"],
                "expected_revision": wing["campaign_revision"],
                "idempotency_key": "end-hero-2",
            },
        )
        frightful = await _raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": dragon["id"],
                "activity_id": "frightful-presence-action",
                "declaration": {
                    "targets": [
                        {"target_id": hero["id"], "aware": True}
                    ]
                },
                "expected_revision": current["campaign_revision"],
                "idempotency_key": "frightful",
            },
        )
        assert frightful["status"] == "committed"
        target = frightful["result"]["core_effect"]["targets"][0]
        assert target["target_id"] == hero["id"]
        assert target["condition_applied"] or target[
            "source_immunity_applied"
        ]
        assert {
            receipt["mechanic_id"]
            for receipt in target["save"]["rule_receipts"]
        }.isdisjoint({"dnd5e.core.save.magic_resistance"})
        assert {
            receipt["mechanic_id"]
            for receipt in frightful["result"]["rule_receipts"]
        } >= {"dnd5e.core.activity.frightful_presence"}
        assert target["condition_applied"] is True

        current = frightful
        repeat_saves: list[dict] = []
        for index in range(10):
            current = await _raw(
                server,
                "combat_end_turn",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": dragon["id"],
                    "expected_revision": current["campaign_revision"],
                    "idempotency_key": f"expire-dragon-{index}",
                },
            )
            current = await _raw(
                server,
                "combat_end_turn",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": hero["id"],
                    "expected_revision": current["campaign_revision"],
                    "idempotency_key": f"expire-hero-{index}",
                },
            )
            repeat_saves.extend(current.get("repeat_saves") or [])

        assert all(
            {
                receipt["mechanic_id"]
                for receipt in dict(item.get("save") or {}).get(
                    "rule_receipts", []
                )
            }.isdisjoint({"dnd5e.core.save.magic_resistance"})
            for item in repeat_saves
        )
        hero_after = await _call(
            server,
            "character_get",
            {"character_id": hero["id"]},
        )
        assert "frightened" not in hero_after["sheet"]["conditions"]
        assert any(
            effect.get("active", True)
            and effect.get("kind") == "frightful_presence_immunity"
            and effect.get("source") == dragon["id"]
            for effect in hero_after["sheet"]["effects"]
        )

    asyncio.run(exercise())
