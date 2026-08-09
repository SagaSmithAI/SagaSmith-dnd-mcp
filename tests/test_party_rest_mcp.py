import asyncio
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet
from sagasmith_dnd.random_stream import CampaignRandomStream, use_random_stream

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


def _spent_sheet() -> dict:
    sheet = default_character_sheet()
    sheet["combat"]["hp"] = {"value": 1, "max": 12, "temp": 3}
    sheet["combat"]["hit_dice"] = {
        "fighter:d10": {
            "label": "Fighter d10",
            "value": 0,
            "max": 2,
            "recovers_on": "long_rest",
            "source_key": "Fighter",
            "slot_level": 0,
        }
    }
    sheet["effects"] = [
        {
            "id": "hours",
            "name": "Expires while resting",
            "active": True,
            "duration": {"period": "hour", "remaining": 5},
        }
    ]
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


def test_party_long_rest_advances_once_and_settles_members_atomically(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Party rest", "edition": "2014", "idempotency_key": "campaign"},
        )
        first = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "First",
                    "sheet": _spent_sheet(),
                },
                "idempotency_key": "first",
            },
        )
        second = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Second",
                    "sheet": _spent_sheet(),
                },
                "idempotency_key": "second",
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
                "payload": {"day": 1, "hour": 21, "minute": 0, "label": "Baldur's Gate"},
                "expected_revision": current["revision"],
                "idempotency_key": "clock",
            },
        )
        with pytest.raises(Exception, match="literal_error"):
            await _call(
                server,
                "character_state_change",
                {
                    "character_id": first["id"],
                    "action": "rest",
                    "payload": {"rest_type": "short_rest"},
                    "expected_revision": first["revision"],
                    "idempotency_key": "unsafe-individual-short-rest",
                },
            )
        before_party_rest = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        assert before_party_rest["revision"] == clock["campaign_revision"]
        assert before_party_rest["state"]["world_time"] == clock["world_time"]
        arguments = {
            "campaign_id": campaign["id"],
            "action": "party_rest",
            "payload": {
                "members": [
                    {
                        "character_id": first["id"],
                        "expected_revision": first["revision"],
                        "rest_activity_minutes": {"meditation": 30},
                    },
                    {
                        "character_id": second["id"],
                        "expected_revision": second["revision"],
                    },
                ]
            },
            "expected_revision": clock["campaign_revision"],
            "idempotency_key": "long-rest",
        }

        rested = await _call(server, "campaign_change", arguments)
        assert await _call(server, "campaign_change", arguments) == rested
        assert rested["world_time"] == {
            "schema_version": 2,
            "tick_seconds": 6,
            "calendar_offset_ticks": 12600,
            "day": 2,
            "hour": 5,
            "minute": 0,
            "second": 0,
            "elapsed_minutes": 1740,
            "round_remainder": 0,
            "label": "Baldur's Gate",
        }
        assert set(rested["member_ids"]) == {first["id"], second["id"]}
        assert rested["expired"] == {first["id"]: ["hours"], second["id"]: ["hours"]}
        receipt = await _call(
            server,
            "state_revision",
            {
                "campaign_id": campaign["id"],
                "action": "receipt",
                "payload": {"idempotency_key": "long-rest"},
            },
        )
        assert receipt["key"] == "long-rest"
        assert receipt["replayed"] is True
        assert len(receipt["request_hash"]) == 64
        assert receipt["branch_id"]
        assert len(receipt["entity_revisions"]) == 3
        assert receipt["response"] == rested

        updated = []
        for index, actor in enumerate((first, second)):
            current_actor = await _call(
                server,
                "character_query",
                {"view": "get", "payload": {"character_id": actor["id"]}},
            )
            updated.append(current_actor)
            assert current_actor["sheet"]["combat"]["hp"] == {
                "value": 12,
                "max": 12,
                "temp": 0,
            }
            assert current_actor["sheet"]["combat"]["hit_dice"]["fighter:d10"]["value"] == 1
            assert current_actor["sheet"]["combat"]["rest_history"] == {
                "last_rest_type": "long_rest",
                "last_rest_started_elapsed_ticks": 0,
                "last_rest_completed_elapsed_ticks": 4800,
                "last_long_rest_elapsed_ticks": 4800,
            }
            assert current_actor["sheet"]["effects"][0]["active"] is False
            assert current_actor["sheet"]["resources"]["ki"]["value"] == (2 if index == 0 else 0)

        with pytest.raises(Exception, match="in 24 hours"):
            await _call(
                server,
                "campaign_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "party_rest",
                    "payload": {
                        "members": [
                            {
                                "character_id": updated[0]["id"],
                                "expected_revision": updated[0]["revision"],
                            }
                        ]
                    },
                    "expected_revision": rested["campaign_revision"],
                    "idempotency_key": "too-soon",
                },
            )
        unchanged = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        assert unchanged["state"]["world_time"]["elapsed_minutes"] == 1740
        assert unchanged["revision"] == rested["campaign_revision"]

    asyncio.run(exercise())


def test_party_short_rest_advances_and_settles_every_member_atomically(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Atomic short rest", "edition": "2014", "idempotency_key": "campaign"},
        )
        first_sheet = _spent_sheet()
        first_sheet["combat"]["hit_dice"]["fighter:d10"]["value"] = 1
        first = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Meditating monk",
                    "sheet": first_sheet,
                },
                "idempotency_key": "first",
            },
        )
        second = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Watching fighter",
                    "sheet": _spent_sheet(),
                },
                "idempotency_key": "second",
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
                "payload": {"day": 3, "hour": 10, "minute": 0, "label": "Roadside"},
                "expected_revision": current["revision"],
                "idempotency_key": "clock",
            },
        )
        arguments = {
            "campaign_id": campaign["id"],
            "action": "party_rest",
            "payload": {
                "rest_type": "short_rest",
                "duration_minutes": 60,
                "members": [
                    {
                        "character_id": first["id"],
                        "expected_revision": first["revision"],
                        "hit_dice_spends": [{"key": "fighter:d10", "count": 1}],
                        "rest_activity_minutes": {"meditation": 30},
                    },
                    {
                        "character_id": second["id"],
                        "expected_revision": second["revision"],
                    },
                ],
            },
            "expected_revision": clock["campaign_revision"],
            "idempotency_key": "short-rest",
        }

        stream = CampaignRandomStream.from_campaign_state(
            campaign["id"],
            current["state"],
            operation="campaign_change",
            idempotency_key="short-rest",
        )
        with use_random_stream(stream):
            rested = await _call(server, "campaign_change", arguments)
        assert await _call(server, "campaign_change", arguments) == rested
        assert rested["rest_type"] == "short_rest"
        assert len(rested["recovered"][first["id"]]["hit_dice_rolls"]) == 1
        assert rested["random_stream_receipt"]["draw_count"] == 1
        assert rested["world_time"] == {
            "schema_version": 2,
            "tick_seconds": 6,
            "calendar_offset_ticks": 34800,
            "day": 3,
            "hour": 11,
            "minute": 0,
            "second": 0,
            "elapsed_minutes": 3540,
            "round_remainder": 0,
            "label": "Roadside",
        }
        updated = []
        for actor in (first, second):
            current_actor = await _call(
                server,
                "character_query",
                {"view": "get", "payload": {"character_id": actor["id"]}},
            )
            updated.append(current_actor)
            assert current_actor["sheet"]["combat"]["rest_history"] == {
                "last_rest_type": "short_rest",
                "last_rest_started_elapsed_ticks": 0,
                "last_rest_completed_elapsed_ticks": 600,
                "last_long_rest_elapsed_ticks": None,
            }
        assert updated[0]["sheet"]["resources"]["ki"]["value"] == 2
        assert updated[1]["sheet"]["resources"]["ki"]["value"] == 0
        receipt = await _call(
            server,
            "state_revision",
            {
                "campaign_id": campaign["id"],
                "action": "receipt",
                "payload": {"idempotency_key": "short-rest"},
            },
        )
        assert receipt["response"] == rested

        before_failure = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        with pytest.raises(Exception, match="at least 60"):
            await _call(
                server,
                "campaign_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "party_rest",
                    "payload": {
                        "rest_type": "short_rest",
                        "duration_minutes": 30,
                        "members": [
                            {
                                "character_id": updated[0]["id"],
                                "expected_revision": updated[0]["revision"],
                            },
                            {
                                "character_id": updated[1]["id"],
                                "expected_revision": updated[1]["revision"],
                            },
                        ],
                    },
                    "expected_revision": before_failure["revision"],
                    "idempotency_key": "invalid-short-rest",
                },
            )
        after_failure = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        assert after_failure["revision"] == before_failure["revision"]
        assert after_failure["state"]["world_time"]["elapsed_minutes"] == 3540

    asyncio.run(exercise())


def test_party_rest_uses_game_time_without_requiring_a_calendar(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Unanchored rest",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Traveler",
                    "sheet": _spent_sheet(),
                },
                "idempotency_key": "actor",
            },
        )
        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        rested = await _call(
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
                            "expected_revision": actor["revision"],
                        }
                    ],
                },
                "expected_revision": current["revision"],
                "idempotency_key": "rest",
            },
        )
        assert rested["world_time"] is None
        assert rested["game_time"]["elapsed_ticks"] == 600
        updated = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": actor["id"]}},
        )
        assert updated["sheet"]["combat"]["rest_history"] == {
            "last_rest_type": "short_rest",
            "last_rest_started_elapsed_ticks": 0,
            "last_rest_completed_elapsed_ticks": 600,
            "last_long_rest_elapsed_ticks": None,
        }

    asyncio.run(exercise())


def test_party_long_rest_honors_source_granted_elf_trance(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Trance rest", "edition": "2014", "idempotency_key": "campaign"},
        )
        elf_sheet = _spent_sheet()
        elf_sheet["content"]["features"] = [
            {
                "id": "dnd5e.content.srd2014.species-feature.elf-trance",
                "name": "Trance",
                "source_key": "Elf",
                "description": "Four hours of trance grants the benefit of eight hours of sleep.",
            }
        ]
        elf = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Elf",
                    "sheet": elf_sheet,
                },
                "idempotency_key": "elf",
            },
        )
        human = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Human",
                    "sheet": _spent_sheet(),
                },
                "idempotency_key": "human",
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
                "payload": {"day": 1, "hour": 0, "minute": 0},
                "expected_revision": current["revision"],
                "idempotency_key": "clock",
            },
        )
        with pytest.raises(Exception, match="at least 480"):
            await _call(
                server,
                "campaign_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "party_rest",
                    "payload": {
                        "duration_minutes": 240,
                        "members": [
                            {
                                "character_id": human["id"],
                                "expected_revision": human["revision"],
                            }
                        ],
                    },
                    "expected_revision": clock["campaign_revision"],
                    "idempotency_key": "human-shortcut",
                },
            )

        rested = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "party_rest",
                "payload": {
                    "duration_minutes": 240,
                    "members": [
                        {
                            "character_id": elf["id"],
                            "expected_revision": elf["revision"],
                        }
                    ],
                },
                "expected_revision": clock["campaign_revision"],
                "idempotency_key": "elf-trance",
            },
        )
        assert rested["duration_minutes"] == 240
        assert rested["world_time"]["elapsed_minutes"] == 240
        updated = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": elf["id"]}},
        )
        assert updated["sheet"]["combat"]["hp"]["value"] == 12
        assert updated["sheet"]["combat"]["rest_history"]["last_long_rest_elapsed_ticks"] == 2400

    asyncio.run(exercise())


def test_party_long_rest_accounts_for_2014_preparation_time(tmp_path: Path) -> None:
    async def exercise() -> None:
        workspace = Path(__file__).resolve().parents[2]
        config = McpConfig(
            home=tmp_path / "home",
            database_url=None,
            chroma_url=None,
            chroma_path_override=None,
            dnd_skills_dir=workspace / "SagaSmith-dnd-skills",
            modulegen_skills_dir=tmp_path / "modulegen",
            auto_seed_rules=False,
        )
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Prepared spell timing",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        sheet = default_character_sheet()
        sheet["progression"] = {
            "level": 3,
            "classes": [{"name": "Cleric", "level": 3, "hit_die": 8}],
        }
        sheet["abilities"]["wisdom"]["score"] = 16
        sheet["spellcasting"]["preparation"] = {
            "mode": "prepared",
            "max_prepared": 6,
            "changes_on": "long_rest",
            "selected_spell_ids": ["dnd5e.content.srd2014.spell.bless"],
        }
        sheet["content"]["spells"] = [
            {
                "id": "dnd5e.content.srd2014.spell.bless",
                "name": "Bless",
                "level": 1,
                "grant": {
                    "source_type": "class",
                    "source_key": "cleric",
                    "method": "class_prepared",
                },
                "access": {"prepared": True},
            },
        ]
        cleric = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Cleric",
                    "sheet": sheet,
                },
                "idempotency_key": "cleric",
            },
        )
        prepared_setup = await _call(
            server,
            "character_spell_prepare",
            {
                "character_id": cleric["id"],
                "mode": "replace_all",
                "payload": {
                    "spell_ids": [
                        "dnd5e.content.srd2014.spell.bless",
                        "dnd5e.content.srd2014.spell.aid",
                    ],
                    "event": "setup",
                },
                "principal_id": "system:local",
                "expected_revision": cleric["revision"],
                "idempotency_key": "prepared-setup",
            },
        )
        assert prepared_setup["preparation"]["materialized_spell_ids"] == [
            "dnd5e.content.srd2014.spell.aid"
        ]
        cleric = prepared_setup["character"]
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
                "payload": {"day": 1, "hour": 8, "minute": 0},
                "expected_revision": current["revision"],
                "idempotency_key": "clock",
            },
        )

        rested = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "party_rest",
                "payload": {
                    "members": [
                        {
                            "character_id": cleric["id"],
                            "expected_revision": cleric["revision"],
                            "prepared_spell_ids": [
                                "dnd5e.content.srd2014.spell.bless",
                                "dnd5e.content.srd2014.spell.lesser-restoration",
                            ],
                        }
                    ]
                },
                "expected_revision": clock["campaign_revision"],
                "idempotency_key": "prepared-rest",
            },
        )
        assert rested["preparations"][cleric["id"]]["preparation_minutes"] == 3
        assert rested["preparations"][cleric["id"]]["materialized_spell_ids"] == [
            "dnd5e.content.srd2014.spell.lesser-restoration"
        ]
        updated = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": cleric["id"]}},
        )
        assert updated["sheet"]["spellcasting"]["preparation"]["selected_spell_ids"] == [
            "dnd5e.content.srd2014.spell.bless",
            "dnd5e.content.srd2014.spell.lesser-restoration",
        ]
        lesser_restoration = next(
            item
            for item in updated["sheet"]["content"]["spells"]
            if item["id"] == "dnd5e.content.srd2014.spell.lesser-restoration"
        )
        assert lesser_restoration["grant"] == {
            "source_type": "class",
            "source_key": "cleric",
            "method": "class_prepared",
        }
        assert lesser_restoration["pack_id"] == "dnd5e.content.srd2014"

        multiclass_sheet = default_character_sheet()
        multiclass_sheet["progression"] = {
            "level": 6,
            "classes": [
                {"name": "Cleric", "level": 3, "hit_die": 8},
                {"name": "Druid", "level": 3, "hit_die": 8},
            ],
        }
        multiclass_sheet["spellcasting"]["preparation"] = {
            "mode": "prepared",
            "max_prepared": 8,
            "changes_on": "long_rest",
            "selected_spell_ids": [],
        }
        multiclass = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Multiclass prepared caster",
                    "sheet": multiclass_sheet,
                },
                "idempotency_key": "multiclass",
            },
        )
        with pytest.raises(Exception, match="unambiguous source class"):
            await _call(
                server,
                "character_spell_prepare",
                {
                    "character_id": multiclass["id"],
                    "mode": "replace_all",
                    "payload": {
                        "spell_ids": ["dnd5e.content.srd2014.spell.cure-wounds"],
                        "event": "setup",
                    },
                    "principal_id": "system:local",
                    "expected_revision": multiclass["revision"],
                    "idempotency_key": "ambiguous-multiclass-preparation",
                },
            )

    asyncio.run(exercise())
