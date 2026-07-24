import asyncio
from pathlib import Path

import pytest
import sagasmith_dnd.lifecycle as lifecycle_module
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


def _short_rest_schedule() -> dict[str, int]:
    return {
        "sleep_minutes": 0,
        "light_activity_minutes": 60,
        "strenuous_activity_minutes": 0,
    }


async def _advance_short_rest_clock(server, campaign_id: str, key: str) -> int:
    campaign = await _call(
        server,
        "campaign_query",
        {"view": "get", "payload": {"campaign_id": campaign_id}},
    )
    world_time = dict(campaign.get("state", {}).get("world_time") or {})
    if not world_time:
        await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign_id,
                "action": "clock_set",
                "payload": {"day": 1, "hour": 0, "minute": 0, "label": "Rest test"},
                "expected_revision": campaign["revision"],
                "idempotency_key": f"{key}-clock-set",
            },
        )
        campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign_id}},
        )
        world_time = dict(campaign["state"]["world_time"])
    started = int(world_time["elapsed_minutes"])
    await _call(
        server,
        "campaign_change",
        {
            "campaign_id": campaign_id,
            "action": "clock_advance",
            "payload": {"period": "minute", "count": 60},
            "expected_revision": campaign["revision"],
            "idempotency_key": f"{key}-clock-advance",
        },
    )
    return started


def _resting_sheet() -> dict:
    sheet = default_character_sheet()
    sheet["combat"]["hp"] = {"value": 1, "max": 12, "temp": 0}
    sheet["combat"]["hit_dice"] = {
        "fighter:d10": {
            "label": "Fighter d10",
            "value": 2,
            "max": 2,
            "recovers_on": "long_rest",
            "source_key": "Fighter",
            "slot_level": 0,
        }
    }
    return sheet


def _wizard_resting_sheet() -> dict:
    sheet = default_character_sheet()
    sheet["progression"] = {
        "level": 2,
        "classes": [{"name": "Wizard", "level": 2, "hit_die": 6}],
    }
    sheet["combat"]["hp"] = {"value": 7, "max": 12, "temp": 0}
    sheet["spellcasting"]["spell_slots"] = {
        "1": {
            "label": "Level 1 spell slots",
            "value": 0,
            "max": 3,
            "recovers_on": "long_rest",
            "source_key": "Wizard",
            "slot_level": 1,
        }
    }
    sheet["content"]["features"] = [
        {
            "id": "dnd5e.content.srd2014.feature.wizard-arcane-recovery",
            "name": "Arcane Recovery",
            "source_key": "Wizard",
        }
    ]
    return sheet


def _monk_resting_sheet() -> dict:
    sheet = default_character_sheet()
    sheet["combat"]["hp"] = {"value": 8, "max": 8, "temp": 0}
    sheet["resources"]["ki"] = {
        "label": "Ki Points",
        "value": 0,
        "max": 2,
        "recovers_on": "short_rest",
        "recovery_requirements": {
            "activity_minutes": {"meditation": 30},
        },
        "source_key": "Monk",
    }
    return sheet


def test_attunement_requires_a_short_rest_during_play(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Attunement", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Bearer",
                    "sheet": default_character_sheet(),
                },
                "idempotency_key": "actor",
            },
        )
        added = await _call(
            server,
            "inventory_change",
            {
                "owner": "character",
                "action": "add",
                "owner_id": actor["id"],
                "payload": {
                    "item": {
                        "id": "staff",
                        "name": "Staff of Defense",
                        "kind": "magic_item",
                        "source_key": "module:item/staff-of-defense",
                        "attunement": "required",
                        "mechanics": {"ac_bonus": 1},
                    }
                },
                "expected_revision": actor["revision"],
                "idempotency_key": "ring",
            },
        )
        equipped = await _call(
            server,
            "inventory_change",
            {
                "owner": "character",
                "action": "equip",
                "owner_id": actor["id"],
                "payload": {"item_id": "staff", "slot": "main_hand"},
                "expected_revision": added["character"]["revision"],
                "idempotency_key": "equip",
            },
        )
        equipped_actor = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": actor["id"]}},
        )
        assert equipped_actor["revision"] == equipped["revision"]
        current_campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "update",
                "payload": {"state": {**current_campaign["state"], "game_phase": "play"}},
                "expected_revision": current_campaign["revision"],
                "idempotency_key": "play",
            },
        )
        with pytest.raises(Exception, match="cannot be patched"):
            await _call(
                server,
                "inventory_change",
                {
                    "owner": "character",
                    "action": "update",
                    "owner_id": actor["id"],
                    "payload": {
                        "item_id": "staff",
                        "patch": {"attunement": "attuned"},
                    },
                    "expected_revision": equipped_actor["revision"],
                    "idempotency_key": "bypass",
                },
            )

        started = await _advance_short_rest_clock(
            server,
            campaign["id"],
            "attunement",
        )
        rested = await _call(
            server,
            "character_state_change",
            {
                "character_id": actor["id"],
                "action": "rest",
                "payload": {
                    "rest_type": "short_rest",
                    "attune_item_id": "staff",
                    "started_elapsed_minutes": started,
                    "rest_schedule": _short_rest_schedule(),
                },
                "expected_revision": equipped_actor["revision"],
                "idempotency_key": "attune",
            },
        )
        assert rested["result"]["attuned_item_id"] == "staff"
        staff = next(
            item
            for item in rested["character"]["sheet"]["inventory"]["items"]
            if item["id"] == "staff"
        )
        assert staff["attunement"] == "attuned"
        assert rested["character"]["derived"]["armor_class"] == 11

    asyncio.run(exercise())


def test_short_rest_rolls_requested_hit_dice_inside_the_mcp(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Rest dice", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Resting Fighter",
                    "sheet": _resting_sheet(),
                },
                "idempotency_key": "actor",
            },
        )
        started = await _advance_short_rest_clock(server, campaign["id"], "dice")
        arguments = {
            "character_id": actor["id"],
            "action": "rest",
            "payload": {
                "rest_type": "short_rest",
                "started_elapsed_minutes": started,
                "rest_schedule": _short_rest_schedule(),
                "hit_dice_spends": [{"key": "fighter:d10", "count": 1}],
            },
            "expected_revision": actor["revision"],
            "idempotency_key": "rest",
        }

        rested = await _call(server, "character_state_change", arguments)
        replay = await _call(server, "character_state_change", arguments)

        assert rested == replay
        assert len(rested["hit_dice_rolls"]) == 1
        assert rested["hit_dice_rolls"][0]["expression"] == "1d10"
        rolled = rested["hit_dice_rolls"][0]["total"]
        assert rested["result"]["hit_die_healing"] == rolled
        assert rested["character"]["sheet"]["combat"]["hp"]["value"] == 1 + rolled
        assert rested["character"]["sheet"]["combat"]["hit_dice"]["fighter:d10"]["value"] == 1

    asyncio.run(exercise())


def test_short_rest_query_preflights_choices_without_mutation(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Rest preflight", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Resting Fighter",
                    "sheet": _resting_sheet(),
                },
                "idempotency_key": "actor",
            },
        )
        before_campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )

        with pytest.raises(Exception, match="hit die is not recorded"):
            await _call(
                server,
                "character_query",
                {
                    "view": "rest",
                    "payload": {
                        "character_id": actor["id"],
                        "rest_type": "short_rest",
                        "duration_minutes": 60,
                        "rest_schedule": _short_rest_schedule(),
                        "hit_dice_spends": [{"key": "d10", "count": 1}],
                    },
                },
            )
        ready = await _call(
            server,
            "character_query",
            {
                "view": "rest",
                "payload": {
                    "character_id": actor["id"],
                    "rest_type": "short_rest",
                    "duration_minutes": 60,
                    "rest_schedule": _short_rest_schedule(),
                    "hit_dice_spends": [{"key": "fighter:d10", "count": 1}],
                },
            },
        )
        assert ready["ready"] is True
        assert ready["hit_dice_spends"] == [{"key": "fighter:d10", "count": 1}]

        after_campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        after_actor = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": actor["id"]}},
        )
        assert after_campaign["revision"] == before_campaign["revision"]
        assert after_actor["revision"] == actor["revision"]
        assert after_actor["sheet"]["combat"]["hit_dice"]["fighter:d10"]["value"] == 2

    asyncio.run(exercise())


def test_short_rest_recovers_ki_only_after_declared_meditation(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Ki rest", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Resting Monk",
                    "sheet": _monk_resting_sheet(),
                },
                "idempotency_key": "actor",
            },
        )
        first_started = await _advance_short_rest_clock(server, campaign["id"], "ki-first")
        no_meditation = await _call(
            server,
            "character_state_change",
            {
                "character_id": actor["id"],
                "action": "rest",
                "payload": {
                    "rest_type": "short_rest",
                    "started_elapsed_minutes": first_started,
                    "rest_schedule": _short_rest_schedule(),
                },
                "expected_revision": actor["revision"],
                "idempotency_key": "rest-without-meditation",
            },
        )
        assert no_meditation["character"]["sheet"]["resources"]["ki"]["value"] == 0
        assert "ki" in no_meditation["result"]["unmet_recovery_requirements"]

        second_started = await _advance_short_rest_clock(server, campaign["id"], "ki-second")
        rested = await _call(
            server,
            "character_state_change",
            {
                "character_id": actor["id"],
                "action": "rest",
                "payload": {
                    "rest_type": "short_rest",
                    "started_elapsed_minutes": second_started,
                    "rest_schedule": _short_rest_schedule(),
                    "rest_activity_minutes": {"meditation": 30},
                },
                "expected_revision": no_meditation["character"]["revision"],
                "idempotency_key": "rest-with-meditation",
            },
        )
        assert rested["character"]["sheet"]["resources"]["ki"]["value"] == 2
        assert rested["result"]["recovered"]["ki"] == 2

    asyncio.run(exercise())


def test_short_rest_atomically_applies_arcane_recovery_choice(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Arcane rest", "edition": "2014", "idempotency_key": "campaign"},
        )
        await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "clock_set",
                "payload": {"day": 1, "hour": 12, "minute": 0, "label": "Test day"},
                "expected_revision": campaign["revision"],
                "idempotency_key": "clock",
            },
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Resting Wizard",
                    "sheet": _wizard_resting_sheet(),
                },
                "idempotency_key": "actor",
            },
        )
        started = await _advance_short_rest_clock(server, campaign["id"], "arcane")
        arguments = {
            "character_id": actor["id"],
            "action": "rest",
            "payload": {
                "rest_type": "short_rest",
                "started_elapsed_minutes": started,
                "rest_schedule": _short_rest_schedule(),
                "arcane_recovery": {"1": 1},
            },
            "expected_revision": actor["revision"],
            "idempotency_key": "arcane-rest",
        }

        rested = await _call(server, "character_state_change", arguments)
        replay = await _call(server, "character_state_change", arguments)

        assert replay == rested
        assert rested["result"]["arcane_recovery"]["recovered"] == {"1": 1}
        sheet = rested["character"]["sheet"]
        assert sheet["spellcasting"]["spell_slots"]["1"]["value"] == 1
        feature = next(
            item for item in sheet["content"]["features"] if item["name"] == "Arcane Recovery"
        )
        assert feature["uses"]["value"] == 0
        assert feature["uses"]["max"] == 1
        assert feature["uses"]["recovers_on"] == "manual"
        assert feature["choices"]["_arcane_recovery_last_used_day"] == 1

    asyncio.run(exercise())


def test_rest_rejects_stale_revision_before_hit_die_rng(tmp_path: Path, monkeypatch) -> None:
    def unexpected_rolls(_expression, *, rng=None):
        raise AssertionError("hit-die RNG must follow revision validation")

    monkeypatch.setattr(lifecycle_module, "roll", unexpected_rolls)

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Stale rest", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Stale Fighter",
                    "sheet": _resting_sheet(),
                },
                "idempotency_key": "actor",
            },
        )
        started = await _advance_short_rest_clock(server, campaign["id"], "stale")

        with pytest.raises(Exception, match="character revision conflict"):
            await _call(
                server,
                "character_state_change",
                {
                    "character_id": actor["id"],
                    "action": "rest",
                    "payload": {
                        "rest_type": "short_rest",
                        "started_elapsed_minutes": started,
                        "rest_schedule": _short_rest_schedule(),
                        "hit_dice_spends": [{"key": "fighter:d10", "count": 1}],
                    },
                    "expected_revision": actor["revision"] + 1,
                    "idempotency_key": "rest",
                },
            )

    asyncio.run(exercise())


def test_rest_rejects_client_supplied_hit_die_results(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "No forged dice", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Honest Fighter",
                    "sheet": _resting_sheet(),
                },
                "idempotency_key": "actor",
            },
        )
        started = await _advance_short_rest_clock(server, campaign["id"], "forged")

        with pytest.raises(Exception, match="only key and count"):
            await _call(
                server,
                "character_state_change",
                {
                    "character_id": actor["id"],
                    "action": "rest",
                    "payload": {
                        "rest_type": "short_rest",
                        "started_elapsed_minutes": started,
                        "rest_schedule": _short_rest_schedule(),
                        "hit_dice_spends": [{"key": "fighter:d10", "roll": 10}],
                    },
                    "expected_revision": actor["revision"],
                    "idempotency_key": "rest",
                },
            )

    asyncio.run(exercise())
