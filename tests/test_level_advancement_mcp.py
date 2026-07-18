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


def _cleric_sheet() -> dict:
    sheet = default_character_sheet()
    sheet["progression"]["classes"] = [
        {"name": "Cleric", "level": 1, "subclass": "Life Domain", "hit_die": 8}
    ]
    sheet["progression"]["species"] = "Hill Dwarf"
    sheet["abilities"]["constitution"]["score"] = 16
    sheet["abilities"]["wisdom"]["score"] = 14
    sheet["combat"]["hp"] = {"value": 7, "max": 12, "temp": 0}
    sheet["combat"]["hit_dice"] = {
        "d8": {
            "label": "d8",
            "value": 1,
            "max": 1,
            "recovers_on": "long_rest",
            "source_key": "Cleric",
        }
    }
    sheet["spellcasting"]["ability"] = "wisdom"
    sheet["spellcasting"]["spell_slots"] = {
        "1": {
            "label": "Level 1 spell slots",
            "value": 0,
            "max": 2,
            "recovers_on": "long_rest",
            "source_key": "Cleric",
            "slot_level": 1,
        }
    }
    sheet["spellcasting"]["preparation"] = {
        "mode": "prepared",
        "max_prepared": 3,
        "changes_on": "long_rest",
        "selected_spell_ids": [],
    }
    sheet["content"]["selections"] = [
        {
            "artifact_id": "dnd5e.content.srd2014.species.hill-dwarf",
            "kind": "species",
            "name": "Hill Dwarf",
            "pack_id": "dnd5e.content.srd2014",
            "pack_version": "1.5.0",
            "rule_refs": ["bundled:srd2014/01_Races/Races_Each/Dwarf.md"],
            "mechanic_refs": [],
            "selection": {},
        }
    ]
    sheet["content"]["features"] = [
        {
            "id": "dnd5e.content.srd2014.species-feature.hill-dwarf-dwarven-toughness",
            "name": "Dwarven Toughness",
            "source_key": "Hill Dwarf",
            "description": "Maximum hit points increase again whenever the actor gains a level.",
            "pack_id": "dnd5e.content.srd2014",
            "pack_version": "1.5.0",
            "rule_refs": ["bundled:srd2014/01_Races/Races_Each/Dwarf.md"],
            "mechanic_refs": [],
        }
    ]
    return sheet


def test_lobby_level_advance_is_source_bound_and_reports_catalog_follow_up(
    tmp_path: Path,
) -> None:
    workspace = Path(__file__).resolve().parents[2]
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=workspace / "SagaSmith-dnd-skills",
        modulegen_skills_dir=workspace / "SagaSmith-module-gen-skills",
        auto_seed_rules=True,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Level Up", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Mara",
                    "sheet": _cleric_sheet(),
                },
                "idempotency_key": "actor",
            },
        )
        arguments = {
            "character_id": actor["id"],
            "action": "level_advance",
            "payload": {
                "class_name": "Cleric",
                "hp_method": "fixed",
                "reason": "survived the opening encounter",
                "source_ref": "module:chapter-1",
            },
            "expected_revision": actor["revision"],
            "idempotency_key": "level-2",
        }

        advanced = await _call(server, "character_state_change", arguments)
        replay = await _call(server, "character_state_change", arguments)

        assert replay == advanced
        assert advanced["status"] == "committed"
        sheet = advanced["character"]["sheet"]
        assert sheet["progression"]["level"] == 2
        assert sheet["combat"]["hp"] == {"value": 7, "max": 21, "temp": 0}
        assert sheet["spellcasting"]["spell_slots"]["1"]["value"] == 1
        assert sheet["spellcasting"]["spell_slots"]["1"]["max"] == 3
        assert advanced["advancement"]["hp_bonus_sources"][0]["amount"] == 1
        feature_ids = {
            item["artifact_id"]
            for item in advanced["advancement"]["follow_up"]["feature_artifacts"]
        }
        assert "dnd5e.content.srd2014.feature.cleric-channel-divinity" in feature_ids
        assert (
            "dnd5e.content.srd2014.feature.life-domain-channel-divinity-preserve-life"
            in feature_ids
        )

        channel = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": "dnd5e.content.srd2014.feature.cleric-channel-divinity",
                "expected_revision": advanced["character"]["revision"],
                "idempotency_key": "channel",
            },
        )
        assert channel["sheet"]["resources"]["channel_divinity"]["value"] == 1
        preserve = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": (
                    "dnd5e.content.srd2014.feature."
                    "life-domain-channel-divinity-preserve-life"
                ),
                "expected_revision": channel["revision"],
                "idempotency_key": "preserve",
            },
        )
        used = await _call(
            server,
            "character_action",
            {
                "character_id": actor["id"],
                "action": "use_activity",
                "payload": {
                    "activity_id": (
                        "dnd5e.content.srd2014.feature."
                        "life-domain-channel-divinity-preserve-life"
                    )
                },
                "expected_revision": preserve["revision"],
                "idempotency_key": "use-preserve",
            },
        )
        assert used["result"]["payment"] == {
            "kind": "resource",
            "key": "channel_divinity",
            "amount": 1,
        }
        assert used["character"]["sheet"]["resources"]["channel_divinity"]["value"] == 0

        receipts = await _call(
            server,
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "receipts",
                "payload": {"mechanic_id": "dnd5e.core.progression.hp_hit_dice"},
            },
        )
        assert len(receipts) == 1
        assert receipts[0]["event"] == "character.level.advance"

        unresolvable_sheet = _cleric_sheet()
        unresolvable_sheet["content"]["selections"][0]["pack_version"] = "9.9.9"
        unresolvable = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Unresolvable provenance",
                    "sheet": unresolvable_sheet,
                },
                "idempotency_key": "unresolvable-actor",
            },
        )
        with pytest.raises(Exception, match="recorded content pack is unavailable"):
            await _call(
                server,
                "character_state_change",
                {
                    "character_id": unresolvable["id"],
                    "action": "level_advance",
                    "payload": {
                        "class_name": "Cleric",
                        "hp_method": "fixed",
                        "reason": "milestone",
                        "source_ref": "module:test",
                    },
                    "expected_revision": unresolvable["revision"],
                    "idempotency_key": "unresolvable-level",
                },
            )

    asyncio.run(exercise())


def test_level_advance_is_rejected_outside_lobby(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Play Phase", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Cleric",
                    "sheet": _cleric_sheet(),
                },
                "idempotency_key": "actor",
            },
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
        assert phase["tool_profile"] == "play"
        with pytest.raises(Exception, match="switch to lobby"):
            await _call(
                server,
                "character_state_change",
                {
                    "character_id": actor["id"],
                    "action": "level_advance",
                    "payload": {
                        "class_name": "Cleric",
                        "hp_method": "fixed",
                        "reason": "milestone",
                        "source_ref": "module:test",
                    },
                    "expected_revision": actor["revision"],
                    "idempotency_key": "level",
                },
            )

    asyncio.run(exercise())
