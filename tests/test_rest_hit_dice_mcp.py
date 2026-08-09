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


async def _ensure_rest_clock(server, campaign_id: str, key: str) -> dict:
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
    return campaign


async def _party_short_rest(
    server,
    campaign_id: str,
    key: str,
    members: list[dict],
) -> dict:
    campaign = await _ensure_rest_clock(server, campaign_id, key)
    result = await _call(
        server,
        "campaign_change",
        {
            "campaign_id": campaign_id,
            "action": "party_rest",
            "payload": {
                "rest_type": "short_rest",
                "duration_minutes": 60,
                "members": members,
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": key,
        },
    )
    primary_id = str(members[0]["character_id"])
    primary = await _call(
        server,
        "character_query",
        {"view": "get", "payload": {"character_id": primary_id}},
    )
    recovery = dict(result["recovered"][primary_id])
    return {
        **result,
        "result": recovery,
        "hit_dice_rolls": list(recovery.get("hit_dice_rolls") or []),
        "character": primary,
    }


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


def _bard_resting_sheet() -> dict:
    sheet = default_character_sheet()
    sheet["edition"] = "2014"
    sheet["progression"] = {
        "level": 3,
        "classes": [{"name": "Bard", "level": 3, "hit_die": 8}],
    }
    sheet["combat"]["hp"] = {"value": 18, "max": 18, "temp": 0}
    sheet["content"]["features"] = [
        {
            "id": "dnd5e.content.srd2014.feature.bard-song-of-rest",
            "name": "Song of Rest",
            "source_key": "Bard",
            "rule_refs": ["bundled:srd2014/02_Classes/Bard.md"],
        }
    ]
    return sheet


def _land_druid_resting_sheet() -> dict:
    sheet = default_character_sheet()
    sheet["edition"] = "2014"
    sheet["progression"] = {
        "level": 4,
        "classes": [
            {
                "name": "Druid",
                "level": 4,
                "subclass": "Circle of the Land",
                "hit_die": 8,
            }
        ],
    }
    sheet["combat"]["hp"] = {"value": 20, "max": 24, "temp": 0}
    sheet["spellcasting"]["spell_slots"] = {
        "2": {
            "label": "Level 2 spell slots",
            "value": 0,
            "max": 3,
            "recovers_on": "long_rest",
            "source_key": "Druid",
            "slot_level": 2,
        }
    }
    sheet["content"]["features"] = [
        {
            "id": (
                "dnd5e.content.srd2014.feature."
                "circle-of-the-land-natural-recovery"
            ),
            "name": "Natural Recovery",
            "source_key": "Circle of the Land",
            "rule_refs": ["bundled:srd2014/02_Classes/Druid.md"],
        }
    ]
    return sheet


def _sorcerer_resting_sheet() -> dict:
    sheet = default_character_sheet()
    sheet["edition"] = "2014"
    sheet["progression"] = {
        "level": 20,
        "classes": [{"name": "Sorcerer", "level": 20, "hit_die": 6}],
    }
    sheet["combat"]["hp"] = {"value": 100, "max": 100, "temp": 0}
    sheet["resources"]["sorcery_points"] = {
        "label": "Sorcery Points",
        "value": 3,
        "max": 20,
        "recovers_on": "long_rest",
        "source_key": "Sorcerer",
    }
    sheet["content"]["features"] = [
        {
            "id": "dnd5e.content.srd2014.feature.sorcerer-sorcerous-restoration",
            "name": "Sorcerous Restoration",
            "source_key": "Sorcerer",
            "rule_refs": ["bundled:srd2014/02_Classes/Sorcerer.md"],
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
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
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

        rest_campaign = await _ensure_rest_clock(
            server,
            campaign["id"],
            "attunement",
        )
        pending_attunement = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "party_rest",
                "payload": {
                    "rest_type": "short_rest",
                    "duration_minutes": 60,
                    "members": [
                        {
                            "character_id": actor["id"],
                            "expected_revision": equipped_actor["revision"],
                            "attune_item_id": "staff",
                        }
                    ],
                },
                "expected_revision": rest_campaign["revision"],
                "idempotency_key": "unreviewed-attunement",
            },
        )
        assert pending_attunement["status"] == "pending_ruling"
        assert pending_attunement["default_resolver"] == "agent"
        assert pending_attunement["ruling_kind"] == "source_or_scene_fact"
        assert pending_attunement["committed"] is False
        rested = await _party_short_rest(
            server,
            campaign["id"],
            "attune",
            [
                {
                    "character_id": actor["id"],
                    "expected_revision": equipped_actor["revision"],
                    "attune_item_id": "staff",
                    "attunement_prerequisite_confirmed": True,
                }
            ],
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
        members = [
            {
                "character_id": actor["id"],
                "expected_revision": actor["revision"],
                "hit_dice_spends": [{"key": "fighter:d10", "count": 1}],
            }
        ]

        rested = await _party_short_rest(server, campaign["id"], "rest", members)
        replay = await _party_short_rest(server, campaign["id"], "rest", members)

        assert rested == replay
        assert len(rested["hit_dice_rolls"]) == 1
        assert rested["hit_dice_rolls"][0]["expression"] == "1d10"
        rolled = rested["hit_dice_rolls"][0]["total"]
        assert rested["result"]["hit_die_healing"] == rolled
        assert rested["character"]["sheet"]["combat"]["hp"]["value"] == 1 + rolled
        assert rested["character"]["sheet"]["combat"]["hit_dice"]["fighter:d10"]["value"] == 1

    asyncio.run(exercise())


def test_short_rest_applies_source_bound_song_of_rest_inside_the_mcp(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Song rest", "edition": "2014", "idempotency_key": "campaign"},
        )
        target_sheet = _resting_sheet()
        target_sheet["combat"]["hp"]["max"] = 30
        target = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Wounded Fighter",
                    "sheet": target_sheet,
                },
                "idempotency_key": "target",
            },
        )
        bard = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Resting Bard",
                    "sheet": _bard_resting_sheet(),
                },
                "idempotency_key": "bard",
            },
        )
        ready = await _call(
            server,
            "character_query",
            {
                "view": "rest",
                "payload": {
                    "character_id": target["id"],
                    "rest_type": "short_rest",
                    "duration_minutes": 60,
                    "hit_dice_spends": [{"key": "fighter:d10", "count": 1}],
                    "song_of_rest_source_actor_id": bard["id"],
                },
            },
        )
        assert ready["ready"] is True
        assert ready["song_of_rest_source_actor_id"] == bard["id"]
        assert ready["song_of_rest_die"] == "1d6"

        with pytest.raises(Exception, match="same party rest"):
            await _party_short_rest(
                server,
                campaign["id"],
                "premature-rest",
                [
                    {
                        "character_id": target["id"],
                        "expected_revision": target["revision"],
                        "hit_dice_spends": [{"key": "fighter:d10", "count": 1}],
                        "song_of_rest_source_actor_id": bard["id"],
                    }
                ],
            )
        rested = await _party_short_rest(
            server,
            campaign["id"],
            "rest",
            [
                {
                    "character_id": target["id"],
                    "expected_revision": target["revision"],
                    "hit_dice_spends": [{"key": "fighter:d10", "count": 1}],
                    "song_of_rest_source_actor_id": bard["id"],
                },
                {
                    "character_id": bard["id"],
                    "expected_revision": bard["revision"],
                },
            ],
        )
        song = rested["result"]["song_of_rest"]
        assert song["die"] == "1d6"
        assert 1 <= song["roll"]["total"] <= 6
        assert song["applied_healing"] == song["rolled_healing"]
        hit_die_total = rested["hit_dice_rolls"][0]["total"]
        assert (
            rested["character"]["sheet"]["combat"]["hp"]["value"]
            == 1 + hit_die_total + song["rolled_healing"]
        )
        mechanic_ids = {
            receipt["mechanic_id"]
            for receipt in rested["rule_receipts"]
        }
        assert "dnd5e.core.rest.song_of_rest" in mechanic_ids

    asyncio.run(exercise())


def test_short_rest_applies_natural_and_sorcerous_recovery_inside_the_mcp(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Class recovery", "edition": "2014", "idempotency_key": "campaign"},
        )
        druid = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Land Druid",
                    "sheet": _land_druid_resting_sheet(),
                },
                "idempotency_key": "druid",
            },
        )
        sorcerer = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Archsorcerer",
                    "sheet": _sorcerer_resting_sheet(),
                },
                "idempotency_key": "sorcerer",
            },
        )
        ready = await _call(
            server,
            "character_query",
            {
                "view": "rest",
                "payload": {
                    "character_id": druid["id"],
                    "rest_type": "short_rest",
                    "duration_minutes": 60,
                    "natural_recovery": {"2": 1},
                    "rest_activity_minutes": {"meditation": 60},
                },
            },
        )
        assert ready["natural_recovery"]["recovered"] == {"2": 1}

        rested = await _party_short_rest(
            server,
            campaign["id"],
            "class-rest",
            [
                {
                    "character_id": druid["id"],
                    "expected_revision": druid["revision"],
                    "natural_recovery": {"2": 1},
                    "rest_activity_minutes": {"meditation": 60},
                },
                {
                    "character_id": sorcerer["id"],
                    "expected_revision": sorcerer["revision"],
                },
            ],
        )
        assert rested["character"]["sheet"]["spellcasting"]["spell_slots"]["2"][
            "value"
        ] == 1
        assert rested["result"]["natural_recovery"]["used_levels"] == 2

        sorcerer_after = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": sorcerer["id"]}},
        )
        sorcerer_recovery = rested["recovered"][sorcerer["id"]]
        assert sorcerer_recovery["sorcerous_restoration"]["recovered"] == 4
        assert (
            sorcerer_after["sheet"]["resources"]["sorcery_points"][
                "value"
            ]
            == 7
        )

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
        no_meditation = await _party_short_rest(
            server,
            campaign["id"],
            "rest-without-meditation",
            [
                {
                    "character_id": actor["id"],
                    "expected_revision": actor["revision"],
                }
            ],
        )
        assert no_meditation["character"]["sheet"]["resources"]["ki"]["value"] == 0
        assert "ki" in no_meditation["result"]["unmet_recovery_requirements"]

        rested = await _party_short_rest(
            server,
            campaign["id"],
            "rest-with-meditation",
            [
                {
                    "character_id": actor["id"],
                    "expected_revision": no_meditation["character"]["revision"],
                    "rest_activity_minutes": {"meditation": 30},
                }
            ],
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
        members = [
            {
                "character_id": actor["id"],
                "expected_revision": actor["revision"],
                "arcane_recovery": {"1": 1},
            }
        ]

        rested = await _party_short_rest(
            server, campaign["id"], "arcane-rest", members
        )
        replay = await _party_short_rest(
            server, campaign["id"], "arcane-rest", members
        )

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
        assert feature["choices"]["_arcane_recovery_last_used_game_day"] == 1

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
        with pytest.raises(Exception, match="character revision conflict"):
            await _party_short_rest(
                server,
                campaign["id"],
                "rest",
                [
                    {
                        "character_id": actor["id"],
                        "expected_revision": actor["revision"] + 1,
                        "hit_dice_spends": [{"key": "fighter:d10", "count": 1}],
                    }
                ],
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
        with pytest.raises(Exception, match="only key and count"):
            await _party_short_rest(
                server,
                campaign["id"],
                "rest",
                [
                    {
                        "character_id": actor["id"],
                        "expected_revision": actor["revision"],
                        "hit_dice_spends": [{"key": "fighter:d10", "roll": 10}],
                    }
                ],
            )

    asyncio.run(exercise())
