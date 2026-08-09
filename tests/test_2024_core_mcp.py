from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet
from sagasmith_dnd.random_stream import CampaignRandomStream, use_random_stream

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call_raw(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result


async def _call(server, name: str, arguments: dict):
    result = await _call_raw(server, name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def _config(tmp_path: Path, *, bundled_skills: bool = False) -> McpConfig:
    workspace = Path(__file__).resolve().parents[2]
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=(workspace / "SagaSmith-dnd-skills" if bundled_skills else tmp_path / "dnd"),
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )


def test_selection_ready_item_can_be_granted_during_play_with_evidence(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path, bundled_skills=True))
        sheet = default_character_sheet()
        sheet["edition"] = "2024"
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Play reward", "edition": "2024", "idempotency_key": "campaign"},
        )
        character = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Rewarded hero",
                    "sheet": sheet,
                },
                "idempotency_key": "character",
            },
        )
        catalog = await _call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "item",
                    "query": "Longsword",
                },
            },
        )
        longsword = next(item for item in catalog if item["name"] == "Longsword")
        current = await _call(
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
                "expected_revision": current["revision"],
                "idempotency_key": "play",
            },
        )
        granted = await _call(
            server,
            "character_content_apply",
            {
                "character_id": character["id"],
                "artifact_id": longsword["id"],
                "selection": {},
                "grant": {
                    "kind": "story_reward",
                    "reason": "The party received the weapon after completing the scene.",
                    "source_ref": longsword["rule_refs"][0],
                },
                "expected_revision": character["revision"],
                "idempotency_key": "grant",
            },
        )

        assert granted["play_grant"]["kind"] == "story_reward"
        applied = next(
            item
            for item in granted["sheet"]["content"]["selections"]
            if item["artifact_id"] == longsword["id"]
        )
        assert applied["selection"]["inventory_item_id"] in {
            item["id"] for item in granted["sheet"]["inventory"]["items"]
        }

    asyncio.run(exercise())


def test_2024_campaign_uses_only_the_source_linked_2024_catalog(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path, bundled_skills=True))
        campaign_2024 = await _call(
            server,
            "campaign_create",
            {
                "name": "SRD 5.2.1 catalog",
                "edition": "2024",
                "idempotency_key": "campaign-2024",
            },
        )
        fireball = await _call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign_2024["id"],
                    "kind": "spell",
                    "query": "Fireball",
                },
                "principal_id": "system:local",
            },
        )
        exact_fireball = next(item for item in fireball if item["name"] == "Fireball")
        assert exact_fireball["pack_id"] == "dnd5e.content.srd2024"
        assert exact_fireball["id"] == "dnd5e.content.srd2024.spell.fireball"
        assert exact_fireball["rule_refs"][0].startswith("bundled:srd2024/")

        longsword = await _call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign_2024["id"],
                    "kind": "item",
                    "query": "Longsword",
                },
                "principal_id": "system:local",
            },
        )
        assert len(longsword) == 1
        assert longsword[0]["pack_id"] == "dnd5e.content.srd2024"
        assert longsword[0]["mechanic_refs"] == ["dnd5e.core.weapon.mastery"]

        tools = await _call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign_2024["id"],
                    "kind": "item",
                    "query": "Calligrapher's Supplies",
                },
                "principal_id": "system:local",
            },
        )
        assert [item["name"] for item in tools] == ["Calligrapher's Supplies"]

        backgrounds = await _call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign_2024["id"],
                    "kind": "background",
                    "query": "Acolyte",
                },
                "principal_id": "system:local",
            },
        )
        requirements = backgrounds[0]["selection_requirements"]
        assert "equipment_package" in requirements["fields"]
        assert requirements["equipment_package_options"] == ["A", "B"]
        assert requirements["equipment_packages"]["B"] == {
            "items": [],
            "wallet": {"gp": 50},
        }

        campaign_2014 = await _call(
            server,
            "campaign_create",
            {
                "name": "SRD 5.1 catalog",
                "edition": "2014",
                "idempotency_key": "campaign-2014",
            },
        )
        old_fireball = await _call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign_2014["id"],
                    "kind": "spell",
                    "query": "Fireball",
                },
                "principal_id": "system:local",
            },
        )
        assert old_fireball
        assert all(item["pack_id"] != "dnd5e.content.srd2024" for item in old_fireball)

    asyncio.run(exercise())


def test_2024_class_resource_cards_materialize_and_scale_through_public_mcp(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path, bundled_skills=True))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "2024 class resources",
                "edition": "2024",
                "idempotency_key": "campaign",
            },
        )

        async def create_class_actor(
            *, name: str, class_name: str, level: int, hit_die: int, charisma: int = 10
        ) -> dict:
            sheet = default_character_sheet()
            sheet["edition"] = "2024"
            sheet["abilities"]["charisma"]["score"] = charisma
            sheet["progression"].update(
                {
                    "level": level,
                    "classes": [
                        {
                            "name": class_name,
                            "level": level,
                            "subclass": "",
                            "hit_die": hit_die,
                        }
                    ],
                }
            )
            return await _call(
                server,
                "character_create_from",
                {
                    "mode": "direct",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "name": name,
                        "sheet": sheet,
                    },
                    "idempotency_key": f"create-{name}",
                },
            )

        bard = await create_class_actor(
            name="Voice", class_name="Bard", level=10, hit_die=8, charisma=18
        )
        bard = await _call(
            server,
            "character_content_apply",
            {
                "character_id": bard["id"],
                "artifact_id": "dnd5e.content.srd2024.feature.bard-bardic-inspiration",
                "selection": {},
                "expected_revision": bard["revision"],
                "idempotency_key": "bardic-inspiration",
            },
        )
        assert bard["sheet"]["resources"]["bardic_inspiration"] == {
            "label": "Bardic Inspiration",
            "value": 4,
            "max": 4,
            "recovers_on": "short_rest",
            "source_key": "Bard",
            "slot_level": 0,
            "unlimited": False,
        }

        paladin = await create_class_actor(
            name="Beacon", class_name="Paladin", level=11, hit_die=10
        )
        paladin = await _call(
            server,
            "character_content_apply",
            {
                "character_id": paladin["id"],
                "artifact_id": "dnd5e.content.srd2024.feature.paladin-lay-on-hands",
                "selection": {},
                "expected_revision": paladin["revision"],
                "idempotency_key": "lay-on-hands",
            },
        )
        assert paladin["sheet"]["resources"]["lay_on_hands"]["max"] == 55
        paladin = await _call(
            server,
            "character_content_apply",
            {
                "character_id": paladin["id"],
                "artifact_id": "dnd5e.content.srd2024.feature.paladin-channel-divinity",
                "selection": {},
                "expected_revision": paladin["revision"],
                "idempotency_key": "paladin-channel",
            },
        )
        channel = paladin["sheet"]["resources"]["channel_divinity"]
        assert channel["max"] == 3
        assert channel["recovery_amounts"] == {
            "short_rest": 1,
            "long_rest": "all",
        }

        sorcerer = await create_class_actor(name="Ember", class_name="Sorcerer", level=8, hit_die=6)
        sorcerer = await _call(
            server,
            "character_content_apply",
            {
                "character_id": sorcerer["id"],
                "artifact_id": "dnd5e.content.srd2024.feature.sorcerer-font-of-magic",
                "selection": {},
                "expected_revision": sorcerer["revision"],
                "idempotency_key": "font-of-magic",
            },
        )
        assert sorcerer["sheet"]["resources"]["sorcery_points"]["max"] == 8
        sorcerer = await _call(
            server,
            "character_content_apply",
            {
                "character_id": sorcerer["id"],
                "artifact_id": "dnd5e.content.srd2024.feature.sorcerer-metamagic",
                "selection": {
                    "grant_level": 2,
                    "options": ["Careful Spell", "Quickened Spell"],
                },
                "expected_revision": sorcerer["revision"],
                "idempotency_key": "metamagic",
            },
        )
        metamagic = next(
            item for item in sorcerer["sheet"]["content"]["features"] if item["name"] == "Metamagic"
        )
        assert metamagic["choices"]["options"] == [
            "Careful Spell",
            "Quickened Spell",
        ]
        sorcerer = await _call(
            server,
            "character_content_apply",
            {
                "character_id": sorcerer["id"],
                "artifact_id": ("dnd5e.content.srd2024.feature.sorcerer-sorcerous-restoration"),
                "selection": {},
                "expected_revision": sorcerer["revision"],
                "idempotency_key": "sorcerous-restoration",
            },
        )
        sorcerer = await _call(
            server,
            "character_state_change",
            {
                "character_id": sorcerer["id"],
                "action": "resource_set",
                "payload": {"resource": "sorcery_points", "value": 2},
                "expected_revision": sorcerer["revision"],
                "idempotency_key": "spend-sorcery-points",
            },
        )
        preflight = await _call(
            server,
            "character_query",
            {
                "view": "rest",
                "payload": {
                    "character_id": sorcerer["id"],
                    "rest_type": "short_rest",
                    "duration_minutes": 60,
                    "sorcerous_restoration_points": 4,
                },
            },
        )
        assert preflight["ready"] is True
        assert preflight["sorcerous_restoration_points"] == 4
        current_campaign = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
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
                            "character_id": sorcerer["id"],
                            "expected_revision": sorcerer["revision"],
                            "sorcerous_restoration_points": 4,
                        }
                    ],
                },
                "expected_revision": current_campaign["revision"],
                "idempotency_key": "short-rest",
            },
        )
        restoration = rested["recovered"][sorcerer["id"]]["sorcerous_restoration"]
        assert restoration["recovered"] == 4
        assert restoration["feature_uses_remaining"] == 0

    asyncio.run(exercise())


def test_2024_advancement_feat_and_background_choices_commit_atomically(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path, bundled_skills=True))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "2024 structured choices",
                "edition": "2024",
                "idempotency_key": "campaign",
            },
        )

        fighter_sheet = default_character_sheet()
        fighter_sheet["edition"] = "2024"
        fighter_sheet["progression"].update(
            {
                "level": 4,
                "classes": [{"name": "Fighter", "level": 4, "subclass": "", "hit_die": 10}],
            }
        )
        fighter = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Structured Fighter",
                    "sheet": fighter_sheet,
                },
                "idempotency_key": "fighter",
            },
        )
        fighter = await _call(
            server,
            "character_content_apply",
            {
                "character_id": fighter["id"],
                "artifact_id": ("dnd5e.content.srd2024.feature.fighter-ability-score-improvement"),
                "selection": {
                    "grant_level": 4,
                    "feat_choice": {
                        "artifact_id": ("dnd5e.content.srd2024.feat.ability-score-improvement"),
                        "selection": {"ability_score_increases": {"strength": 2}},
                    },
                },
                "expected_revision": fighter["revision"],
                "idempotency_key": "fighter-asi",
            },
        )
        assert fighter["sheet"]["abilities"]["strength"]["score"] == 12
        selected_feat = next(
            item
            for item in fighter["sheet"]["content"]["feats"]
            if item["name"] == "Ability Score Improvement"
        )
        assert selected_feat["choices"]["ability_score_increases"] == {"strength": 2}
        assert selected_feat["choices"]["grant_source"] == "Ability Score Improvement"

        rogue_sheet = default_character_sheet()
        rogue_sheet["edition"] = "2024"
        rogue_sheet["progression"]["classes"] = [
            {"name": "Rogue", "level": 1, "subclass": "", "hit_die": 8}
        ]
        rogue = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Polyglot Rogue",
                    "sheet": rogue_sheet,
                },
                "idempotency_key": "rogue",
            },
        )
        rogue = await _call(
            server,
            "character_content_apply",
            {
                "character_id": rogue["id"],
                "artifact_id": "dnd5e.content.srd2024.feature.rogue-thieves-cant",
                "selection": {"language": "Undercommon"},
                "expected_revision": rogue["revision"],
                "idempotency_key": "thieves-cant",
            },
        )
        assert {"Thieves' Cant", "Undercommon"}.issubset(set(rogue["sheet"]["traits"]["languages"]))
        rogue = await _call(
            server,
            "character_content_apply",
            {
                "character_id": rogue["id"],
                "artifact_id": "dnd5e.content.srd2024.feat.skilled",
                "selection": {
                    "proficiencies": [
                        {"kind": "skill", "name": "Arcana"},
                        {"kind": "skill", "name": "Medicine"},
                        {"kind": "tool", "name": "Herbalism Kit"},
                    ]
                },
                "expected_revision": rogue["revision"],
                "idempotency_key": "skilled",
            },
        )
        assert rogue["sheet"]["skills"]["arcana"]["proficiency"] == "proficient"
        assert "Herbalism Kit" in rogue["sheet"]["traits"]["proficiencies"]["tools"]

        acolyte_sheet = default_character_sheet()
        acolyte_sheet["edition"] = "2024"
        acolyte_sheet["progression"]["classes"] = [
            {"name": "Cleric", "level": 1, "subclass": "", "hit_die": 8}
        ]
        acolyte_sheet["spellcasting"]["ability"] = "wisdom"
        acolyte = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Acolyte",
                    "sheet": acolyte_sheet,
                },
                "idempotency_key": "acolyte",
            },
        )
        acolyte = await _call(
            server,
            "character_content_apply",
            {
                "character_id": acolyte["id"],
                "artifact_id": "dnd5e.content.srd2024.background.acolyte",
                "selection": {
                    "ability_score_increases": {"wisdom": 2, "intelligence": 1},
                    "equipment_package": "A",
                    "origin_feat_selection": {
                        "spellcasting_ability": "wisdom",
                        "cantrip_artifact_ids": [
                            "dnd5e.content.srd2024.spell.guidance",
                            "dnd5e.content.srd2024.spell.sacred-flame",
                        ],
                        "level_1_spell_artifact_id": ("dnd5e.content.srd2024.spell.cure-wounds"),
                    },
                },
                "expected_revision": acolyte["revision"],
                "idempotency_key": "acolyte-background",
            },
        )
        acolyte = acolyte.get("character", acolyte)
        assert acolyte["sheet"]["progression"]["background"] == "Acolyte"
        assert acolyte["sheet"]["abilities"]["wisdom"]["score"] == 12
        assert acolyte["sheet"]["abilities"]["intelligence"]["score"] == 11
        assert acolyte["sheet"]["skills"]["insight"]["proficiency"] == "proficient"
        assert "Calligrapher's Supplies" in acolyte["sheet"]["traits"]["proficiencies"]["tools"]
        assert acolyte["sheet"]["inventory"]["wallet"]["gp"] == 8
        equipment = {item["name"]: item for item in acolyte["sheet"]["inventory"]["items"]}
        assert set(equipment) >= {
            "Calligrapher's Supplies",
            "Book (prayers)",
            "Holy Symbol",
            "Parchment",
            "Robe",
        }
        assert equipment["Parchment"]["quantity"] == 10
        assert set(acolyte["sheet"]["progression"]["background_grants"]["equipment_item_ids"]) == {
            item["id"] for item in equipment.values()
        }
        assert any(
            item["name"] == "Magic Initiate" for item in acolyte["sheet"]["content"]["feats"]
        )
        spell_names = {item["name"] for item in acolyte["sheet"]["content"]["spells"]}
        assert {"Guidance", "Sacred Flame", "Cure Wounds"}.issubset(spell_names)
        cure_wounds = next(
            item for item in acolyte["sheet"]["content"]["spells"] if item["name"] == "Cure Wounds"
        )
        assert cure_wounds["access"]["always_prepared"] is True
        assert (
            acolyte["sheet"]["resources"][
                "magic_initiate:cleric:dnd5e.content.srd2024.spell.cure-wounds"
            ]["max"]
            == 1
        )

    asyncio.run(exercise())


def test_2024_eldritch_invocations_enforce_levels_targets_and_repeat_grants(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path, bundled_skills=True))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "2024 invocations",
                "edition": "2024",
                "idempotency_key": "campaign",
            },
        )
        sheet = default_character_sheet()
        sheet["edition"] = "2024"
        sheet["progression"].update(
            {
                "level": 2,
                "classes": [{"name": "Warlock", "level": 2, "subclass": "", "hit_die": 8}],
            }
        )
        sheet["spellcasting"]["ability"] = "charisma"
        warlock = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Invoker",
                    "sheet": sheet,
                },
                "idempotency_key": "warlock",
            },
        )
        warlock = await _call(
            server,
            "character_content_apply",
            {
                "character_id": warlock["id"],
                "artifact_id": "dnd5e.content.srd2024.spell.eldritch-blast",
                "selection": {"source_class": "Warlock", "method": "known"},
                "expected_revision": warlock["revision"],
                "idempotency_key": "eldritch-blast",
            },
        )
        invocation_id = "dnd5e.content.srd2024.feature.warlock-eldritch-invocations"
        first = await _call(
            server,
            "character_content_apply",
            {
                "character_id": warlock["id"],
                "artifact_id": invocation_id,
                "selection": {
                    "grant_level": 1,
                    "eldritch_invocations": [{"option": "Armor of Shadows"}],
                },
                "expected_revision": warlock["revision"],
                "idempotency_key": "level-1-invocation",
            },
        )
        warlock = first.get("character", first)
        second = await _call(
            server,
            "character_content_apply",
            {
                "character_id": warlock["id"],
                "artifact_id": invocation_id,
                "selection": {
                    "grant_level": 2,
                    "eldritch_invocations": [
                        {
                            "option": "Agonizing Blast",
                            "target_artifact_id": ("dnd5e.content.srd2024.spell.eldritch-blast"),
                        },
                        {"option": "Mask of Many Faces"},
                    ],
                },
                "expected_revision": warlock["revision"],
                "idempotency_key": "level-2-invocations",
            },
        )
        warlock = second.get("character", second)
        invocation_names = {
            item["name"]
            for item in warlock["sheet"]["content"]["features"]
            if item["source_key"] == "Eldritch Invocation"
        }
        assert invocation_names == {
            "Agonizing Blast",
            "Armor of Shadows",
            "Mask of Many Faces",
        }
        at_will_names = {
            item["name"]
            for item in warlock["sheet"]["content"]["spells"]
            if item["access"]["at_will"]
        }
        assert {"Mage Armor", "Disguise Self"}.issubset(at_will_names)

    asyncio.run(exercise())


def test_heroic_inspiration_rerolls_one_exact_recorded_die_atomically(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Heroic Inspiration",
                "edition": "2024",
                "idempotency_key": "campaign",
            },
        )
        sheet = default_character_sheet()
        sheet["edition"] = "2024"
        sheet["combat"]["inspiration"] = True
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Inspired Hero", "sheet": sheet},
                "principal_id": "system:local",
                "idempotency_key": "actor",
            },
        )
        current = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": current["revision"],
                "idempotency_key": "enter-play",
            },
        )
        current = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        checked = await _call_raw(
            server,
            "character_check",
            {
                "campaign_id": campaign["id"],
                "action": "check",
                "payload": {
                    "actor_id": actor["id"],
                    "kind": "check",
                    "ability": "wisdom",
                    "dc": 14,
                    "advantage": True,
                },
                "expected_revision": current["revision"],
                "idempotency_key": "check",
            },
        )
        original = checked["result"]
        reroll_arguments = {
            "campaign_id": campaign["id"],
            "action": "reroll",
            "payload": {
                "actor_id": actor["id"],
                "resolution_id": checked["resolution_id"],
                "roll_index": 0,
                "expected_original_roll": original["rolls"][0],
            },
            "expected_revision": checked["campaign_revision"],
            "idempotency_key": "reroll",
        }
        stream_state = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        stream = CampaignRandomStream.from_campaign_state(
            campaign["id"],
            stream_state["state"],
            operation="character_check",
            idempotency_key="reroll",
        )
        with use_random_stream(stream):
            rerolled = await _call_raw(server, "character_check", reroll_arguments)
            replay = await _call_raw(server, "character_check", reroll_arguments)

        assert replay == rerolled
        assert rerolled["result"]["rolls"][1] == original["rolls"][1]
        assert rerolled["result"]["rolls"][0] == rerolled["heroic_inspiration_reroll"]["new_roll"]
        assert rerolled["result"]["natural"] == max(rerolled["result"]["rolls"])
        assert rerolled["result"]["total"] == (
            rerolled["result"]["natural"] + original["total"] - original["natural"]
        )
        assert rerolled["random_stream_receipt"]["draw_count"] == 1
        refreshed = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": actor["id"]},
                "principal_id": "system:local",
            },
        )
        assert refreshed["sheet"]["combat"]["inspiration"] is False

        after = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        with pytest.raises(Exception, match="identify one check"):
            await _call_raw(
                server,
                "character_check",
                {
                    **reroll_arguments,
                    "payload": {
                        **reroll_arguments["payload"],
                        "resolution_id": "not-the-recorded-resolution",
                    },
                    "expected_revision": after["revision"],
                    "idempotency_key": "wrong-resolution",
                },
            )

    asyncio.run(exercise())
