from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

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


def _devourer_sheet() -> dict:
    sheet = default_character_sheet()
    sheet["edition"] = "2014"
    sheet["progression"]["species"] = "aberration"
    sheet["traits"]["size"] = "tiny"
    sheet["combat"]["hp"] = {"value": 21, "max": 21, "temp": 0}
    sheet["inventory"]["items"] = [
        {
            "id": "claws",
            "name": "Claws",
            "kind": "weapon",
            "equipped": True,
            "equipped_slot": "main_hand",
            "mechanics": {
                "attack_type": "melee",
                "attack_ability": "dexterity",
                "damage_formula": "2d4 + 2",
                "damage_type": "slashing",
                "properties": [],
            },
        }
    ]
    sheet["inventory"]["equipment_slots"]["main_hand"] = "claws"
    source_excerpt = (
        "The intellect devourer targets one creature it can see within 10 feet "
        "of it that has a brain. The target must succeed on a DC 12 Intelligence "
        "saving throw against this magic or take 11 (2d10) psychic damage. Also "
        "on a failure, roll 3d6: If the total equals or exceeds the target's "
        "Intelligence score, that score is reduced to 0. The target is stunned "
        "until it regains at least one point of Intelligence."
    )
    sheet["content"]["activities"] = [
        {
            "id": "multiattack-activity",
            "name": "Multiattack",
            "activation": {"type": "action"},
            "choices": {
                "multiattack_options": [
                    {
                        "id": "claws-and-devour-intellect",
                        "attacks": [
                            {
                                "weapon_id": "claws",
                                "attack_mode": "melee",
                                "count": 1,
                            }
                        ],
                        "activities": [
                            {
                                "activity_id": "devour-intellect-action",
                                "count": 1,
                            }
                        ],
                    }
                ]
            },
        },
        {
            "id": "devour-intellect-action",
            "name": "Devour Intellect",
            "description": source_excerpt,
            "activation": {"type": "action"},
            "choices": {
                "source_save_effect": {
                    "kind": "intellect_devourer_devour_intellect_2014",
                    "range_ft": 10,
                    "target_count": 1,
                    "target_requirement": "has_brain",
                    "save": {"ability": "intelligence", "dc": 12},
                    "failure": {
                        "damage_expression": "2d10",
                        "damage_type": "psychic",
                        "secondary_roll": "3d6",
                        "secondary_threshold": "target_intelligence_score",
                        "ability_override": {
                            "ability": "intelligence",
                            "score": 0,
                        },
                        "condition": "stunned",
                        "ends_when": "target_intelligence_score_at_least_1",
                    },
                    "source_excerpt": source_excerpt,
                }
            },
        },
    ]
    return sheet


def test_public_devour_intellect_completes_mixed_multiattack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = server_module.resolve_source_save_effect

    def deterministic(source_actor, target_actor, **kwargs):
        return original(
            source_actor,
            target_actor,
            **kwargs,
            rng=_SequenceRng(1, 5, 6, 4, 4, 4),
        )

    monkeypatch.setattr(
        server_module,
        "resolve_source_save_effect",
        deterministic,
    )

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Devour Intellect",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        devourer = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Intellect Devourer",
                "character_type": "monster",
                "sheet": _devourer_sheet(),
                "idempotency_key": "devourer",
            },
        )
        target_sheet = default_character_sheet()
        target_sheet["combat"]["hp"] = {"value": 50, "max": 50, "temp": 0}
        target_sheet["combat"]["ac"]["override"] = 1
        target_sheet["abilities"]["intelligence"]["score"] = 10
        target = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Target",
                "sheet": target_sheet,
                "idempotency_key": "target",
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
        started = await _call(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [devourer["id"], target["id"]],
                "participant_config": [
                    {
                        "actor_id": devourer["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                        "disposition": "hostile",
                    },
                    {
                        "actor_id": target["id"],
                        "initiative": 10,
                        "position": {"x": 1, "y": 0},
                        "disposition": "friendly",
                        "death_saves": True,
                    },
                ],
                "expected_revision": phase["campaign_revision"],
                "idempotency_key": "start",
            },
        )
        first = await _call(
            server,
            "combat_resolve_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": devourer["id"],
                "target_id": target["id"],
                "action": {
                    "weapon_id": "claws",
                    "multiattack_option_id": "claws-and-devour-intellect",
                },
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "claws",
            },
        )
        assert first["attack_payment"]["attack_count"] == 2
        campaign_after_claws = await _call(
            server,
            "campaign_get",
            {"campaign_id": campaign["id"]},
        )
        settled = await _raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": devourer["id"],
                "activity_id": "devour-intellect-action",
                "declaration": {
                    "target_id": target["id"],
                    "target_has_brain": True,
                },
                "expected_revision": campaign_after_claws["revision"],
                "idempotency_key": "devour-intellect",
            },
        )

        assert settled["status"] == "committed"
        effect = settled["result"]["core_effect"]
        assert effect["kind"] == "source_save_effect"
        assert effect["activation_payment"]["kind"] == (
            "multiattack_activity_followup"
        )
        assert effect["damage_roll"]["total"] == 11
        assert effect["secondary_roll"]["total"] == 12
        assert effect["ability_reduced"] is True
        assert any(
            item["mechanic_id"]
            == "dnd5e.core.activity.source_save_effect"
            for item in settled["result"]["rule_receipts"]
        )
        current = next(
            item
            for item in settled["combat"]["combatants"]
            if item["actor_id"] == devourer["id"]
        )
        assert current["turn_budget"]["main_action"] == 0
        assert current["turn_budget"]["attack_budget"] == 0
        assert "multiattack" not in current.get("turn_flags", {})
        target_after = await _call(
            server,
            "character_get",
            {"character_id": target["id"]},
        )
        assert "stunned" in target_after["sheet"]["conditions"]
        assert target_after["derived"]["ability_scores"]["intelligence"] == 0

    asyncio.run(exercise())
