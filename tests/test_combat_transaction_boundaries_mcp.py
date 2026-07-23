from __future__ import annotations

import asyncio
import random
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


async def _call_raw(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result


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


def test_available_actions_explicitly_discovers_required_death_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_death_save = server_module.resolve_death_save_to_sheet

    def deterministic_death_save(sheet, **kwargs):
        return original_death_save(sheet, **kwargs, rng=random.Random(1))

    monkeypatch.setattr(
        server_module, "resolve_death_save_to_sheet", deterministic_death_save
    )

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Death action", "edition": "2014", "idempotency_key": "campaign"},
        )
        sheet = default_character_sheet()
        sheet["combat"]["hp"] = {"value": 0, "max": 10, "temp": 0}
        sheet["conditions"] = ["prone", "unconscious"]
        actor = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Dying PC",
                "sheet": sheet,
                "idempotency_key": "actor",
            },
        )
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
        started = await _call(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [actor["id"]],
                "participant_config": [
                    {"actor_id": actor["id"], "initiative": 10, "death_saves": True}
                ],
                "expected_revision": campaign["revision"],
                "idempotency_key": "start",
            },
        )

        available = await _call(
            server,
            "combat_available_actions",
            {"campaign_id": campaign["id"], "actor_id": actor["id"]},
        )

        assert started["combat"]["round"] == 1
        assert available["actions"] == ["death_save"]
        resolved = await _call_raw(
            server,
            "combat_check",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "kind": "death_save",
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "death-save",
            },
        )
        assert resolved["result"]["kind"] == "death_save"
        assert resolved["result"]["outcome"] == "pending"

        after = await _call(
            server,
            "combat_available_actions",
            {"campaign_id": campaign["id"], "actor_id": actor["id"]},
        )
        assert after["actions"] == []

    asyncio.run(exercise())


def test_invalid_branch_is_rejected_before_noncombat_check_rolls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rolled = False

    def forbidden_roll(*args, **kwargs):
        nonlocal rolled
        rolled = True
        raise AssertionError("the check must not roll")

    monkeypatch.setattr(server_module, "resolve_actor_check", forbidden_roll)

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Branch guard", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Checker",
                "idempotency_key": "actor",
            },
        )
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})

        with pytest.raises(Exception, match="checked-out branch"):
            await _call(
                server,
                "character_check",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": actor["id"],
                    "kind": "check",
                    "ability": "wisdom",
                    "dc": 10,
                    "branch_id": "not-the-current-branch",
                    "expected_revision": campaign["revision"],
                    "idempotency_key": "invalid-branch-check",
                },
            )

    asyncio.run(exercise())
    assert rolled is False


def test_action_surge_is_settled_without_a_manual_ruling(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Action Surge", "edition": "2014", "idempotency_key": "campaign"},
        )
        sheet = default_character_sheet()
        sheet["content"]["features"] = [
            {
                "id": "dnd5e.content.srd2014.feature.fighter-action-surge",
                "name": "Action Surge",
                "source_key": "Fighter",
                "description": "Take one additional action on your turn.",
                "uses": {
                    "label": "Action Surge",
                    "value": 1,
                    "max": 1,
                    "recovers_on": "short_rest",
                },
                "resource_key": "",
                "activation": {"type": "special", "cost": 0, "trigger": ""},
                "scaling": [],
                "choices": {"outcome": "take one additional action on this turn"},
            }
        ]
        actor = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Fighter",
                "sheet": sheet,
                "idempotency_key": "actor",
            },
        )
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
        started = await _call_raw(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [actor["id"]],
                "participant_config": [{"actor_id": actor["id"], "initiative": 10}],
                "expected_revision": campaign["revision"],
                "idempotency_key": "start",
            },
        )
        surged = await _call_raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "activity_id": "dnd5e.content.srd2014.feature.fighter-action-surge",
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "surge",
            },
        )

        assert surged["status"] == "committed"
        assert surged["result"]["requires_ruling"] is False
        assert surged["result"]["core_effect"]["extra_actions_granted"] == 1
        current = surged["combat"]["combatants"][surged["combat"]["turn_index"]]
        assert current["turn_budget"]["extra_action"] == 1
        assert any(
            item["mechanic_id"] == "dnd5e.core.activity.action_surge"
            for item in surged["result"]["rule_receipts"]
        )
        actor_after = await _call(
            server, "character_get", {"character_id": actor["id"]}
        )
        assert actor_after["sheet"]["content"]["features"][0]["uses"]["value"] == 0

    asyncio.run(exercise())


def test_second_wind_heals_and_pays_bonus_action_atomically(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Second Wind", "edition": "2014", "idempotency_key": "campaign"},
        )
        sheet = default_character_sheet()
        sheet["progression"]["level"] = 2
        sheet["progression"]["classes"] = [
            {"name": "Fighter", "level": 2, "subclass": "", "hit_die": 10}
        ]
        sheet["combat"]["hp"] = {"value": 1, "max": 20, "temp": 0}
        sheet["content"]["features"] = [
            {
                "id": "dnd5e.content.srd2014.feature.fighter-second-wind",
                "name": "Second Wind",
                "source_key": "Fighter",
                "description": "Regain 1d10 + Fighter level hit points.",
                "uses": {
                    "label": "Second Wind",
                    "value": 1,
                    "max": 1,
                    "recovers_on": "short_rest",
                },
                "resource_key": "",
                "activation": {"type": "bonus_action", "cost": 1, "trigger": ""},
                "scaling": [],
                "choices": {"outcome": "roll 1d10 + fighter level"},
            }
        ]
        actor = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Fighter",
                "sheet": sheet,
                "idempotency_key": "actor",
            },
        )
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
        started = await _call_raw(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [actor["id"]],
                "participant_config": [{"actor_id": actor["id"], "initiative": 10}],
                "expected_revision": campaign["revision"],
                "idempotency_key": "start",
            },
        )

        result = await _call_raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "activity_id": "dnd5e.content.srd2014.feature.fighter-second-wind",
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "second-wind",
            },
        )

        assert result["status"] == "committed"
        assert result["result"]["requires_ruling"] is False
        effect = result["result"]["core_effect"]
        assert effect["kind"] == "second_wind"
        assert effect["fighter_level"] == 2
        assert 4 <= effect["after_hp"] <= 13
        current = result["combat"]["combatants"][result["combat"]["turn_index"]]
        assert current["turn_budget"]["bonus_action"] == 0
        actor_after = await _call(server, "character_get", {"character_id": actor["id"]})
        assert actor_after["sheet"]["combat"]["hp"]["value"] == effect["after_hp"]
        assert actor_after["sheet"]["content"]["features"][0]["uses"]["value"] == 0
        assert any(
            item["mechanic_id"] == "dnd5e.core.activity.second_wind"
            for item in result["result"]["rule_receipts"]
        )

    asyncio.run(exercise())


def test_second_wind_heals_and_advances_random_stream_outside_combat(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Noncombat Second Wind",
                "edition": "2014",
                "random_seed": "noncombat-second-wind",
                "idempotency_key": "campaign",
            },
        )
        sheet = default_character_sheet()
        sheet["progression"]["level"] = 1
        sheet["progression"]["classes"] = [
            {"name": "Fighter", "level": 1, "subclass": "", "hit_die": 10}
        ]
        sheet["combat"]["hp"] = {"value": 2, "max": 12, "temp": 0}
        sheet["content"]["features"] = [
            {
                "id": "dnd5e.content.srd2014.feature.fighter-second-wind",
                "name": "Second Wind",
                "source_key": "Fighter",
                "description": "Regain 1d10 + Fighter level hit points.",
                "uses": {
                    "label": "Second Wind",
                    "value": 1,
                    "max": 1,
                    "recovers_on": "short_rest",
                },
                "resource_key": "",
                "activation": {"type": "bonus_action", "cost": 1, "trigger": ""},
                "scaling": [],
                "choices": {"outcome": "roll 1d10 + fighter level"},
            }
        ]
        actor = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Fighter",
                "sheet": sheet,
                "idempotency_key": "actor",
            },
        )
        arguments = {
            "character_id": actor["id"],
            "activity_id": "dnd5e.content.srd2014.feature.fighter-second-wind",
            "expected_revision": actor["revision"],
            "idempotency_key": "second-wind",
        }

        before = await _call(
            server, "campaign_get", {"campaign_id": campaign["id"]}
        )
        stream = server_module.CampaignRandomStream.from_campaign_state(
            campaign["id"],
            before["state"],
            operation="character_action",
            idempotency_key="second-wind",
        )
        with server_module.use_random_stream(stream):
            result = await _call_raw(server, "character_use_activity", arguments)
        assert stream.has_unpersisted_draws is False
        assert await _call_raw(server, "character_use_activity", arguments) == result
        assert result["status"] == "committed"
        effect = result["result"]["core_effect"]
        assert effect["kind"] == "second_wind"
        assert effect["fighter_level"] == 1
        assert 4 <= effect["after_hp"] <= 12
        assert effect["after_hp"] == result["character"]["sheet"]["combat"]["hp"]["value"]
        assert (
            result["character"]["sheet"]["content"]["features"][0]["uses"]["value"]
            == 0
        )
        assert any(
            item["mechanic_id"] == "dnd5e.core.activity.second_wind"
            for item in result["result"]["rule_receipts"]
        )
        current = await _call(
            server, "campaign_get", {"campaign_id": campaign["id"]}
        )
        assert current["state"]["random_stream"]["position"] == 1

    asyncio.run(exercise())


def test_cunning_action_dash_uses_bonus_action_and_doubles_movement(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Cunning Action", "edition": "2014", "idempotency_key": "campaign"},
        )
        sheet = default_character_sheet()
        sheet["progression"]["level"] = 2
        sheet["progression"]["classes"] = [
            {"name": "Rogue", "level": 2, "subclass": "", "hit_die": 8}
        ]
        sheet["content"]["features"] = [
            {
                "id": "dnd5e.content.srd2014.feature.rogue-cunning-action",
                "name": "Cunning Action",
                "source_key": "Rogue",
                "description": "Dash, Disengage, or Hide as a bonus action.",
                "uses": {"label": "", "value": 0, "max": 0, "recovers_on": "none"},
                "resource_key": "",
                "activation": {"type": "bonus_action", "cost": 1, "trigger": ""},
                "scaling": [],
                "choices": {"options": ["Dash", "Disengage", "Hide"]},
            }
        ]
        actor = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Rogue",
                "sheet": sheet,
                "idempotency_key": "actor",
            },
        )
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
        started = await _call_raw(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [actor["id"]],
                "participant_config": [{"actor_id": actor["id"], "initiative": 10}],
                "expected_revision": campaign["revision"],
                "idempotency_key": "start",
            },
        )

        result = await _call_raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "activity_id": "dnd5e.content.srd2014.feature.rogue-cunning-action",
                "declaration": {"action": "dash"},
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "cunning-dash",
            },
        )

        assert result["status"] == "committed"
        assert result["result"]["requires_ruling"] is False
        current = result["combat"]["combatants"][result["combat"]["turn_index"]]
        assert current["turn_budget"]["movement"] == 60
        assert current["turn_budget"]["bonus_action"] == 0
        assert current["turn_budget"]["main_action"] == 1
        assert any(
            item["mechanic_id"] == "dnd5e.core.activity.cunning_action"
            for item in result["result"]["rule_receipts"]
        )

    asyncio.run(exercise())


def test_combat_move_charges_reviewed_difficult_cells_and_records_core_receipt(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Difficult terrain", "edition": "2014", "idempotency_key": "campaign"},
        )
        mover = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Mover",
                "idempotency_key": "mover",
            },
        )
        other = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Other",
                "idempotency_key": "other",
            },
        )
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
        started = await _call_raw(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [mover["id"], other["id"]],
                "participant_config": [
                    {
                        "actor_id": mover["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                        "hidden": True,
                        "visible_to_actor_ids": [mover["id"]],
                    },
                    {
                        "actor_id": other["id"],
                        "initiative": 10,
                        "position": {"x": 4, "y": 0},
                    },
                ],
                "battle_map": {
                    "width_cells": 6,
                    "height_cells": 4,
                    "difficult_cells": [{"x": 1, "y": 0}],
                },
                "expected_revision": campaign["revision"],
                "idempotency_key": "start",
            },
        )

        moved = await _call_raw(
            server,
            "combat_move",
            {
                "campaign_id": campaign["id"],
                "actor_id": mover["id"],
                "distance": 10,
                "destination": {"x": 2, "y": 0},
                "path": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 2, "y": 0}],
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "move",
            },
        )

        current = moved["combat"]["combatants"][moved["combat"]["turn_index"]]
        assert current["turn_budget"]["movement"] == 15
        receipts = await _call(
            server,
            "campaign_rule_receipts",
            {"campaign_id": campaign["id"]},
        )
        assert any(
            item["mechanic_id"] == "dnd5e.core.movement.difficult_terrain"
            for item in receipts
        )
        revealed = await _call_raw(
            server,
            "combat_map_patch",
            {
                "campaign_id": campaign["id"],
                "patches": [
                    {
                        "key": "combatant_visibility",
                        "value": {
                            "actor_id": mover["id"],
                            "hidden": False,
                            "visible_to_actor_ids": None,
                            "reason": "The hidden actor shouted from its new position.",
                        },
                    }
                ],
                "expected_revision": moved["campaign_revision"],
                "idempotency_key": "reveal",
            },
        )
        mover_after = next(
            item
            for item in revealed["combat"]["combatants"]
            if item["actor_id"] == mover["id"]
        )
        assert mover_after["hidden"] is False
        assert mover_after["visible_to_actor_ids"] is None
        assert revealed["campaign_revision"] == moved["campaign_revision"] + 1

    asyncio.run(exercise())
