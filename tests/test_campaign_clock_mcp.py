import asyncio
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


def test_campaign_clock_and_elapsed_effects_advance_atomically(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Clock", "edition": "2014", "idempotency_key": "campaign"},
        )
        sheet = default_character_sheet()
        sheet["effects"] = [
            {
                "id": "minutes",
                "name": "Minutes",
                "active": True,
                "duration": {"period": "minute", "remaining": 120},
            },
            {
                "id": "hours",
                "name": "Hours",
                "active": True,
                "duration": {"period": "hour", "remaining": 3},
            },
            {
                "id": "days",
                "name": "Days",
                "active": True,
                "duration": {"period": "day", "remaining": 2},
            },
        ]
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Timed Actor",
                    "sheet": sheet,
                },
                "idempotency_key": "actor",
            },
        )
        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        clock = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "clock_set",
                "payload": {"day": 2, "hour": 9, "minute": 30, "label": "Baldur's Gate"},
                "expected_revision": current["revision"],
                "idempotency_key": "clock-set",
            },
        )
        light = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "effect_add",
                "payload": {
                    "effect": {
                        "id": "mace-light",
                        "name": "Light on a mace",
                        "kind": "light",
                        "source_spell_id": "dnd5e.content.srd2014.spell.light",
                        "source_actor_id": actor["id"],
                        "target": {"kind": "object", "id": "mace", "label": "Mace"},
                        "duration": {"period": "hour", "remaining": 1},
                    }
                },
                "expected_revision": clock["campaign_revision"],
                "idempotency_key": "light-add",
            },
        )
        arguments = {
            "campaign_id": campaign["id"],
            "action": "clock_advance",
            "payload": {
                "period": "hour",
                "count": 2,
                "expected_world_time": {
                    "day": 2,
                    "hour": 11,
                    "minute": 30,
                    "elapsed_minutes": 2130,
                },
            },
            "expected_revision": light["campaign_revision"],
            "idempotency_key": "clock-advance",
        }

        advanced = await _call(server, "campaign_change", arguments)
        replay = await _call(server, "campaign_change", arguments)

        assert replay == advanced
        set_receipt = await _call(
            server,
            "state_revision",
            {
                "campaign_id": campaign["id"],
                "action": "receipt",
                "payload": {"idempotency_key": "clock-set"},
            },
        )
        assert set_receipt["response"] == clock
        advance_receipt = await _call(
            server,
            "state_revision",
            {
                "campaign_id": campaign["id"],
                "action": "receipt",
                "payload": {"idempotency_key": "clock-advance"},
            },
        )
        assert advance_receipt["response"] == advanced
        assert advanced["world_time"] == {
            "schema_version": 1,
            "day": 2,
            "hour": 11,
            "minute": 30,
            "elapsed_minutes": 2130,
            "label": "Baldur's Gate",
        }
        assert advanced["world_expired"] == ["mace-light"]
        updated = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": actor["id"]}},
        )
        effects = {item["id"]: item for item in updated["sheet"]["effects"]}
        assert effects["minutes"]["active"] is False
        assert effects["hours"]["duration"]["remaining"] == 1
        assert effects["days"]["duration"]["remaining"] == 2
        persisted = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        assert persisted["state"]["world_time"] == advanced["world_time"]
        assert persisted["state"]["world_effects"][0]["active"] is False

    asyncio.run(exercise())


def test_clock_advance_rejects_a_duration_that_misses_its_expected_target(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Road calendar", "edition": "2014", "idempotency_key": "campaign"},
        )
        clock = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "clock_set",
                "payload": {"day": 45, "hour": 1, "minute": 3},
                "expected_revision": campaign["revision"],
                "idempotency_key": "clock",
            },
        )
        expected = {
            "day": 55,
            "hour": 7,
            "minute": 0,
            "elapsed_minutes": 78180,
        }
        with pytest.raises(Exception, match="require expected_world_time"):
            await _call(
                server,
                "campaign_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "clock_advance",
                    "payload": {"period": "minute", "count": 14757},
                    "expected_revision": clock["campaign_revision"],
                    "idempotency_key": "unbound-road-duration",
                },
            )
        with pytest.raises(Exception, match="does not reach expected_world_time"):
            await _call(
                server,
                "campaign_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "clock_advance",
                    "payload": {
                        "period": "minute",
                        "count": 13197,
                        "expected_world_time": expected,
                    },
                    "expected_revision": clock["campaign_revision"],
                    "idempotency_key": "wrong-road-duration",
                },
            )
        unchanged = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        assert unchanged["revision"] == clock["campaign_revision"]
        assert unchanged["state"]["world_time"]["elapsed_minutes"] == 63423

        advanced = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "clock_advance",
                "payload": {
                    "period": "minute",
                    "count": 14757,
                    "expected_world_time": expected,
                },
                "expected_revision": unchanged["revision"],
                "idempotency_key": "correct-road-duration",
            },
        )
        assert {
            key: advanced["world_time"][key]
            for key in ("day", "hour", "minute", "elapsed_minutes")
        } == expected

    asyncio.run(exercise())


def test_minute_clock_advances_accumulate_for_hour_effects(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Split clock", "edition": "2014", "idempotency_key": "campaign"},
        )
        sheet = default_character_sheet()
        sheet["conditions"] = ["poisoned", "paralyzed", "turned", "invisible"]
        sheet["content"]["spells"] = [
            {"id": "invisibility", "name": "Invisibility", "level": 2}
        ]
        sheet["effects"] = [
            {
                "id": "giant-spider-poison",
                "name": "Giant Spider Poison",
                "kind": "timed_conditions",
                "active": True,
                "duration": {"period": "hour", "remaining": 1},
                "changes": [
                    {
                        "path": "conditions",
                        "mode": "add",
                        "value": ["poisoned", "paralyzed"],
                    }
                ],
            },
            {
                "id": "turn-undead",
                "name": "Turn Undead",
                "kind": "turn_undead",
                "active": True,
                "duration": {"period": "minute", "remaining": 1},
            },
            {
                "id": "invisibility",
                "name": "Invisibility",
                "kind": "concentration",
                "source_spell_id": "invisibility",
                "active": True,
                "concentration": True,
                "duration": {"period": "hour", "remaining": 1},
                "changes": [],
            },
        ]
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Poisoned Actor",
                    "sheet": sheet,
                },
                "idempotency_key": "actor",
            },
        )
        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        clock = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "clock_set",
                "payload": {"day": 1, "hour": 9},
                "expected_revision": current["revision"],
                "idempotency_key": "clock",
            },
        )
        first = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "clock_advance",
                "payload": {
                    "period": "minute",
                    "count": 30,
                    "expected_world_time": {
                        "day": 1,
                        "hour": 9,
                        "minute": 30,
                        "elapsed_minutes": 570,
                    },
                },
                "expected_revision": clock["campaign_revision"],
                "idempotency_key": "first-half-hour",
            },
        )
        halfway = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": actor["id"]}},
        )
        assert halfway["sheet"]["effects"][0]["duration"] == {
            "period": "hour",
            "remaining": 1,
            "elapsed_minutes_remainder": 30,
        }
        assert halfway["sheet"]["effects"][1]["active"] is False
        assert set(halfway["sheet"]["conditions"]) == {
            "invisible",
            "paralyzed",
            "poisoned",
        }

        second = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "clock_advance",
                "payload": {
                    "period": "minute",
                    "count": 30,
                    "expected_world_time": {
                        "day": 1,
                        "hour": 10,
                        "minute": 0,
                        "elapsed_minutes": 600,
                    },
                },
                "expected_revision": first["campaign_revision"],
                "idempotency_key": "second-half-hour",
            },
        )
        assert second["expired"] == {
            actor["id"]: ["giant-spider-poison", "invisibility"]
        }
        finished = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": actor["id"]}},
        )
        assert finished["sheet"]["conditions"] == []
        assert finished["sheet"]["effects"][0]["active"] is False

    asyncio.run(exercise())


def test_campaign_clock_must_be_set_before_time_advance(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Unset Clock", "edition": "2014", "idempotency_key": "campaign"},
        )
        with pytest.raises(Exception, match="set the campaign clock"):
            await _call(
                server,
                "campaign_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "clock_advance",
                    "payload": {
                        "period": "hour",
                        "expected_world_time": {
                            "day": 1,
                            "hour": 1,
                            "minute": 0,
                            "elapsed_minutes": 60,
                        },
                    },
                    "expected_revision": campaign["revision"],
                    "idempotency_key": "advance",
                },
            )

    asyncio.run(exercise())


def test_campaign_clock_cannot_jump_past_timed_effects(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Clock reset", "edition": "2014", "idempotency_key": "campaign"},
        )
        clock = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "clock_set",
                "payload": {"day": 1, "hour": 10},
                "expected_revision": campaign["revision"],
                "idempotency_key": "clock",
            },
        )
        with pytest.raises(Exception, match="use clock_advance"):
            await _call(
                server,
                "campaign_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "clock_set",
                    "payload": {"day": 1, "hour": 12},
                    "expected_revision": clock["campaign_revision"],
                    "idempotency_key": "jump",
                },
            )

    asyncio.run(exercise())


def test_generic_campaign_update_cannot_bypass_the_clock_owner(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Protected clock",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        clock = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "clock_set",
                "payload": {"day": 2, "hour": 7, "minute": 0},
                "expected_revision": campaign["revision"],
                "idempotency_key": "clock",
            },
        )

        with pytest.raises(Exception, match="system-owned state fields: world_time"):
            await _call(
                server,
                "campaign_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "update",
                    "payload": {
                        "state": {
                            "world_time": {
                                "day": 9,
                                "hour": 0,
                                "minute": 0,
                                "elapsed_minutes": 11520,
                            }
                        }
                    },
                    "expected_revision": clock["campaign_revision"],
                    "idempotency_key": "bypass-clock",
                },
            )

        updated = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "update",
                "payload": {"state": {"party": {"notes": "Reviewed party note"}}},
                "expected_revision": clock["campaign_revision"],
                "idempotency_key": "party-note",
            },
        )
        assert updated["state"]["party"]["notes"] == "Reviewed party note"
        assert updated["state"]["world_time"] == clock["world_time"]

    asyncio.run(exercise())


def test_ten_combat_rounds_advance_the_shared_clock_and_all_elapsed_effects(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Combat clock",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        actors = []
        for index, name in enumerate(("First", "Second", "Spectator")):
            sheet = default_character_sheet()
            sheet["effects"] = [
                {
                    "id": f"elapsed-{index}",
                    "name": "One minute effect",
                    "active": True,
                    "duration": {"period": "minute", "remaining": 1},
                }
            ]
            actors.append(
                await _call(
                    server,
                    "character_create_from",
                    {
                        "mode": "direct",
                        "payload": {
                            "campaign_id": campaign["id"],
                            "name": name,
                            "sheet": sheet,
                        },
                        "idempotency_key": f"actor-{index}",
                    },
                )
            )
        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        clock = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "clock_set",
                "payload": {"day": 1, "hour": 10, "minute": 0, "label": "Round test"},
                "expected_revision": current["revision"],
                "idempotency_key": "clock",
            },
        )
        world_effect = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "effect_add",
                "payload": {
                    "effect": {
                        "id": "one-minute-light",
                        "name": "One minute light",
                        "kind": "light",
                        "target": {"kind": "object", "id": "torch"},
                        "duration": {"period": "minute", "remaining": 1},
                    }
                },
                "expected_revision": clock["campaign_revision"],
                "idempotency_key": "world-effect",
            },
        )
        state = await _call(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [actors[0]["id"], actors[1]["id"]],
                "participant_config": [
                    {"actor_id": actors[0]["id"], "initiative": 20},
                    {"actor_id": actors[1]["id"], "initiative": 10},
                ],
                "expected_revision": world_effect["campaign_revision"],
                "idempotency_key": "combat-start",
            },
        )

        final_arguments = None
        for turn in range(20):
            current_combatant = state["combat"]["combatants"][
                state["combat"]["turn_index"]
            ]
            final_arguments = {
                "campaign_id": campaign["id"],
                "actor_id": current_combatant["actor_id"],
                "expected_revision": state["campaign_revision"],
                "idempotency_key": f"turn-{turn}",
            }
            state = await _call(server, "combat_end_turn", final_arguments)

        assert final_arguments is not None
        assert state["world_time"] == {
            "schema_version": 1,
            "day": 1,
            "hour": 10,
            "minute": 1,
            "elapsed_minutes": 601,
            "label": "Round test",
        }
        assert state["world_expired"] == ["one-minute-light"]
        minute_boundary = state
        next_combatant = state["combat"]["combatants"][state["combat"]["turn_index"]]
        state = await _call(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": next_combatant["actor_id"],
                "expected_revision": state["campaign_revision"],
                "idempotency_key": "turn-after-minute",
            },
        )
        assert await _call(server, "combat_end_turn", final_arguments) == minute_boundary

        persisted = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        assert persisted["state"]["world_time"] == minute_boundary["world_time"]
        assert persisted["state"]["world_effects"][0]["active"] is False
        for actor in actors:
            updated = await _call(
                server,
                "character_query",
                {"view": "get", "payload": {"character_id": actor["id"]}},
            )
            assert updated["sheet"]["effects"][0]["active"] is False
        receipt = await _call(
            server,
            "state_revision",
            {
                "campaign_id": campaign["id"],
                "action": "receipt",
                "payload": {"idempotency_key": "turn-19"},
            },
        )
        assert receipt["response"]["world_time"] == minute_boundary["world_time"]
        assert (
            receipt["response"]["combat"]["turn_index"]
            == minute_boundary["combat"]["turn_index"]
        )

    asyncio.run(exercise())
