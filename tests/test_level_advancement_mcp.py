import asyncio
from pathlib import Path

import pytest
import sagasmith_dnd.progression as progression_module
from sagasmith_dnd.character_schema import default_character_sheet
from sagasmith_dnd.core_content import PACK_VERSION as CORE_CONTENT_PACK_VERSION
from sagasmith_dnd.core_content import build_srd2014_content
from sagasmith_dnd.engine import roll as engine_roll

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import (
    SUPPORTED_FEATURE_MECHANICAL_GRANTS,
    SUPPORTED_FEATURE_OPTION_PREREQUISITE_FIELDS,
    SUPPORTED_FEATURE_SELECTION_KINDS,
    SUPPORTED_FEATURE_SELECTION_REQUIREMENT_FIELDS,
    create_server,
)

CORE_ADVANCEMENT_RULE_REF = "bundled:srd2014/03_Characterization/Beyond_1st_Level.md"


class _SequenceRng:
    def __init__(self, *values: int) -> None:
        self.values = list(values)

    def randint(self, minimum: int, maximum: int) -> int:
        value = self.values.pop(0)
        assert minimum <= value <= maximum
        return value


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def test_all_declared_srd_feature_contracts_are_fail_closed_or_supported() -> None:
    workspace = Path(__file__).resolve().parents[2]
    _manifest, artifacts = build_srd2014_content(workspace / "SagaSmith-dnd-skills")
    feature_cards = [
        dict(artifact.get("card") or {})
        for artifact in artifacts
        if artifact.get("kind") == "feature"
    ]
    declared_grants = {
        key for card in feature_cards for key in dict(card.get("mechanical_grants") or {})
    }
    declared_kinds = {
        str(requirements.get("kind") or "")
        for card in feature_cards
        for requirements in [
            dict(card.get("selection_requirements") or {}),
            *[
                dict(item)
                for item in dict(card.get("selection_requirements_by_level") or {}).values()
            ],
        ]
    }
    declared_requirement_fields = {
        field
        for card in feature_cards
        for requirements in [
            dict(card.get("selection_requirements") or {}),
            *[
                dict(item)
                for item in dict(card.get("selection_requirements_by_level") or {}).values()
            ],
        ]
        for field in requirements
    }
    declared_prerequisite_fields = {
        field
        for card in feature_cards
        for requirements in [
            dict(card.get("selection_requirements") or {}),
            *[
                dict(item)
                for item in dict(card.get("selection_requirements_by_level") or {}).values()
            ],
        ]
        for prerequisite in dict(requirements.get("option_prerequisites") or {}).values()
        for field in dict(prerequisite)
    }

    assert declared_grants <= SUPPORTED_FEATURE_MECHANICAL_GRANTS
    assert declared_kinds <= SUPPORTED_FEATURE_SELECTION_KINDS
    assert declared_requirement_fields <= SUPPORTED_FEATURE_SELECTION_REQUIREMENT_FIELDS
    assert declared_prerequisite_fields <= SUPPORTED_FEATURE_OPTION_PREREQUISITE_FIELDS


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
    sheet["combat"]["hp_progression"] = [
        {
            "level": 1,
            "method": "manual",
            "value": 12,
            "source": "Cleric level 1; Hill Dwarf: Dwarven Toughness",
        }
    ]
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
            "pack_version": CORE_CONTENT_PACK_VERSION,
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
            "pack_version": CORE_CONTENT_PACK_VERSION,
            "rule_refs": ["bundled:srd2014/01_Races/Races_Each/Dwarf.md"],
            "mechanic_refs": [],
        }
    ]
    return sheet


def _fighter_sheet() -> dict:
    sheet = default_character_sheet()
    sheet["progression"]["level"] = 3
    sheet["progression"]["classes"] = [
        {"name": "Fighter", "level": 3, "subclass": "Champion", "hit_die": 10}
    ]
    sheet["abilities"]["strength"]["score"] = 15
    sheet["abilities"]["constitution"]["score"] = 13
    sheet["combat"]["hp"] = {"value": 20, "max": 24, "temp": 0}
    sheet["combat"]["hit_dice"] = {
        "d10": {
            "label": "d10",
            "value": 3,
            "max": 3,
            "recovers_on": "long_rest",
            "source_key": "Fighter",
        }
    }
    sheet["combat"]["hp_progression"] = [
        {
            "level": level,
            "method": "manual" if level == 1 else "fixed",
            "value": 8,
            "source": f"Fighter level {level}",
        }
        for level in range(1, 4)
    ]
    return sheet


def _bard_sheet_with_shadow_resource() -> dict:
    sheet = default_character_sheet()
    sheet["progression"]["level"] = 10
    sheet["progression"]["classes"] = [
        {"name": "Bard", "level": 10, "subclass": "College of Lore", "hit_die": 8}
    ]
    sheet["abilities"]["charisma"]["score"] = 20
    sheet["abilities"]["constitution"]["score"] = 14
    sheet["combat"]["hp"] = {"value": 70, "max": 70, "temp": 0}
    sheet["combat"]["hit_dice"] = {
        "d8": {
            "label": "d8",
            "value": 10,
            "max": 10,
            "recovers_on": "long_rest",
            "source_key": "Bard",
        }
    }
    sheet["resources"] = {
        "bardic_inspiration": {
            "label": "Bardic Inspiration",
            "value": 3,
            "max": 3,
            "recovers_on": "long_rest",
            "source_key": "Bard",
        }
    }
    sheet["content"]["features"] = [
        {
            "id": "dnd5e.content.srd2014.feature.bard-bardic-inspiration",
            "name": "Bardic Inspiration",
            "source_key": "Bard",
            "uses": {
                "label": "Bardic Inspiration",
                "value": 3,
                "max": 3,
                "recovers_on": "long_rest",
                "source_key": "Bard",
            },
            "resource_key": "",
            "activation": {"type": "bonus_action", "cost": 1},
            "resource_scaling": {
                "target": "uses",
                "label": "Bardic Inspiration",
                "class_name": "Bard",
                "maximum_by_level": {},
                "maximum_formula": {
                    "kind": "ability_modifier",
                    "ability": "charisma",
                    "minimum": 1,
                    "multiplier": 1,
                    "offset": 0,
                },
                "recovers_on": "long_rest",
                "recovery_by_level": {"5": "short_rest"},
                "unlimited_at_level": 0,
            },
            "pack_id": "dnd5e.content.srd2014",
            "pack_version": CORE_CONTENT_PACK_VERSION,
            "rule_refs": ["bundled:srd2014/02_Classes/Bard.md"],
            "mechanic_refs": [],
        }
    ]
    return sheet


def _land_druid_sheet() -> dict:
    sheet = default_character_sheet()
    sheet["progression"]["level"] = 2
    sheet["progression"]["classes"] = [
        {
            "name": "Druid",
            "level": 2,
            "subclass": "Circle of the Land",
            "hit_die": 8,
        }
    ]
    sheet["abilities"]["constitution"]["score"] = 14
    sheet["abilities"]["wisdom"]["score"] = 16
    sheet["combat"]["hp"] = {"value": 17, "max": 17, "temp": 0}
    sheet["combat"]["hit_dice"] = {
        "d8": {
            "label": "d8",
            "value": 2,
            "max": 2,
            "recovers_on": "long_rest",
            "source_key": "Druid",
        }
    }
    sheet["combat"]["hp_progression"] = [
        {
            "level": level,
            "method": "manual" if level == 1 else "fixed",
            "value": 9 if level == 1 else 8,
            "source": f"Druid level {level}",
        }
        for level in range(1, 3)
    ]
    sheet["spellcasting"]["ability"] = "wisdom"
    sheet["spellcasting"]["preparation"].update(
        {
            "mode": "prepared",
            "max_prepared": 5,
            "changes_on": "long_rest",
        }
    )
    sheet["content"]["selections"] = [
        {
            "artifact_id": "dnd5e.content.srd2014.subclass.circle-of-the-land",
            "kind": "subclass",
            "name": "Circle of the Land",
            "pack_id": "dnd5e.content.srd2014",
            "pack_version": CORE_CONTENT_PACK_VERSION,
            "rule_refs": ["bundled:srd2014/02_Classes/Druid.md"],
            "mechanic_refs": [],
            "selection": {"target_class_name": "Druid"},
        }
    ]
    sheet["content"]["features"] = [
        {
            "id": "dnd5e.content.srd2014.feature.circle-of-the-land-circle-spells",
            "name": "Circle Spells",
            "source_key": "Circle of the Land",
            "description": "Circle spells are always prepared.",
            "choices": {"option": "Coast"},
            "pack_id": "dnd5e.content.srd2014",
            "pack_version": CORE_CONTENT_PACK_VERSION,
            "rule_refs": ["bundled:srd2014/02_Classes/Druid.md"],
            "mechanic_refs": [],
        }
    ]
    return sheet


def test_lobby_resource_sync_preserves_independent_top_level_counter_via_public_facade(
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
            {
                "name": "Resource synchronization",
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
                    "name": "Legacy Bard",
                    "sheet": _bard_sheet_with_shadow_resource(),
                },
                "idempotency_key": "actor",
            },
        )
        arguments = {
            "character_id": actor["id"],
            "action": "resource_sync",
            "payload": {"reason": "Synchronize the Bardic Inspiration card uses."},
            "expected_revision": actor["revision"],
            "idempotency_key": "resource-sync",
        }

        synchronized = await _call(server, "character_state_change", arguments)
        replay = await _call(server, "character_state_change", arguments)

        assert replay == synchronized
        assert synchronized["status"] == "committed"
        sheet = synchronized["character"]["sheet"]
        assert sheet["resources"]["bardic_inspiration"]["value"] == 3
        bardic = next(
            item
            for item in sheet["content"]["features"]
            if item["id"] == "dnd5e.content.srd2014.feature.bard-bardic-inspiration"
        )
        assert bardic["uses"] == {
            "label": "Bardic Inspiration",
            "value": 5,
            "max": 5,
            "recovers_on": "short_rest",
            "source_key": "Bard",
            "slot_level": 0,
            "unlimited": False,
        }

    asyncio.run(exercise())


def test_source_choice_repeats_and_off_list_oath_spells_are_enforced(
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
            {
                "name": "Source Choices",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )

        paladin_sheet = default_character_sheet()
        paladin_sheet["progression"].update(
            {
                "level": 3,
                "classes": [
                    {
                        "name": "Paladin",
                        "level": 3,
                        "subclass": "",
                        "hit_die": 10,
                    }
                ],
            }
        )
        paladin = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Oathkeeper",
                    "sheet": paladin_sheet,
                },
                "idempotency_key": "paladin",
            },
        )
        oath = await _call(
            server,
            "character_content_apply",
            {
                "character_id": paladin["id"],
                "artifact_id": "dnd5e.content.srd2014.subclass.oath-of-devotion",
                "selection": {"target_class_name": "Paladin"},
                "expected_revision": paladin["revision"],
                "idempotency_key": "oath",
            },
        )
        sanctuary = next(
            spell for spell in oath["sheet"]["content"]["spells"] if spell["name"] == "Sanctuary"
        )
        assert sanctuary["access"]["always_prepared"] is True
        assert sanctuary["grant"]["source_key"] == "Oath of Devotion"

        sorcerer_sheet = default_character_sheet()
        sorcerer_sheet["progression"].update(
            {
                "level": 10,
                "classes": [
                    {
                        "name": "Sorcerer",
                        "level": 10,
                        "subclass": "Draconic Bloodline",
                        "hit_die": 6,
                    }
                ],
            }
        )
        metamagic_id = "dnd5e.content.srd2014.feature.sorcerer-metamagic"
        sorcerer_sheet["content"]["features"] = [
            {
                "id": metamagic_id,
                "name": "Metamagic",
                "source_key": "Sorcerer",
                "description": "Selected Metamagic options.",
                "choices": {"options": ["Careful Spell", "Distant Spell"]},
                "advancement_grants": [
                    {
                        "level": 3,
                        "choices": {"options": ["Careful Spell", "Distant Spell"]},
                        "pack_id": "dnd5e.content.srd2014",
                        "pack_version": CORE_CONTENT_PACK_VERSION,
                        "rule_refs": ["bundled:srd2014/02_Classes/Sorcerer.md"],
                    }
                ],
                "pack_id": "dnd5e.content.srd2014",
                "pack_version": CORE_CONTENT_PACK_VERSION,
                "rule_refs": ["bundled:srd2014/02_Classes/Sorcerer.md"],
                "mechanic_refs": [],
            }
        ]
        sorcerer = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Ember",
                    "sheet": sorcerer_sheet,
                },
                "idempotency_key": "sorcerer",
            },
        )
        with pytest.raises(Exception, match="already selected"):
            await _call(
                server,
                "character_content_apply",
                {
                    "character_id": sorcerer["id"],
                    "artifact_id": metamagic_id,
                    "selection": {
                        "grant_level": 10,
                        "options": ["Careful Spell"],
                    },
                    "expected_revision": sorcerer["revision"],
                    "idempotency_key": "repeat-existing-metamagic",
                },
            )
        repeated = await _call(
            server,
            "character_content_apply",
            {
                "character_id": sorcerer["id"],
                "artifact_id": metamagic_id,
                "selection": {
                    "grant_level": 10,
                    "options": ["Quickened Spell"],
                },
                "expected_revision": sorcerer["revision"],
                "idempotency_key": "new-metamagic",
            },
        )
        metamagic = next(
            item for item in repeated["sheet"]["content"]["features"] if item["id"] == metamagic_id
        )
        assert [item["level"] for item in metamagic["advancement_grants"]] == [
            3,
            10,
        ]
        assert metamagic["advancement_grants"][-1]["choices"] == {"options": ["Quickened Spell"]}

    asyncio.run(exercise())


def test_feature_granted_spells_and_invocation_prerequisites_are_settled(
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
            {
                "name": "Feature Spell Grants",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )

        bard_sheet = default_character_sheet()
        bard_sheet["progression"].update(
            {
                "level": 10,
                "classes": [
                    {
                        "name": "Bard",
                        "level": 10,
                        "subclass": "College of Lore",
                        "hit_die": 8,
                    }
                ],
            }
        )
        bard_sheet["spellcasting"]["preparation"]["mode"] = "known"
        bard_sheet["spellcasting"]["spell_slots"]["5"] = {
            "label": "Level 5 spell slots",
            "value": 2,
            "max": 2,
            "recovers_on": "long_rest",
            "source_key": "Bard",
            "slot_level": 5,
        }
        bard = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Song",
                    "sheet": bard_sheet,
                },
                "idempotency_key": "bard",
            },
        )
        secrets = await _call(
            server,
            "character_content_apply",
            {
                "character_id": bard["id"],
                "artifact_id": "dnd5e.content.srd2014.feature.bard-magical-secrets",
                "selection": {
                    "spell_artifact_ids": [
                        "dnd5e.content.srd2014.spell.fireball",
                        "dnd5e.content.srd2014.spell.eldritch-blast",
                    ]
                },
                "expected_revision": bard["revision"],
                "idempotency_key": "magical-secrets",
            },
        )
        assert {item["artifact_id"] for item in secrets["feature_spell_grants"]} == {
            "dnd5e.content.srd2014.spell.fireball",
            "dnd5e.content.srd2014.spell.eldritch-blast",
        }
        secrets_character = secrets
        assert {
            spell["grant"]["source_key"]
            for spell in secrets_character["sheet"]["content"]["spells"]
        } == {"Bard"}

        warlock_sheet = default_character_sheet()
        warlock_sheet["progression"].update(
            {
                "level": 11,
                "classes": [
                    {
                        "name": "Warlock",
                        "level": 11,
                        "subclass": "The Fiend",
                        "hit_die": 8,
                    }
                ],
            }
        )
        warlock_sheet["spellcasting"]["preparation"]["mode"] = "known"
        warlock = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Ash",
                    "sheet": warlock_sheet,
                },
                "idempotency_key": "warlock",
            },
        )
        arcanum = await _call(
            server,
            "character_content_apply",
            {
                "character_id": warlock["id"],
                "artifact_id": "dnd5e.content.srd2014.feature.warlock-mystic-arcanum",
                "selection": {
                    "grant_level": 11,
                    "spell_artifact_ids": ["dnd5e.content.srd2014.spell.mass-suggestion"],
                },
                "expected_revision": warlock["revision"],
                "idempotency_key": "arcanum",
            },
        )
        arcanum_character = arcanum
        assert (
            arcanum_character["sheet"]["resources"][
                "mystic_arcanum:dnd5e.content.srd2014.spell.mass-suggestion"
            ]["value"]
            == 1
        )
        arcanum_spell = next(
            spell
            for spell in arcanum_character["sheet"]["content"]["spells"]
            if spell["name"] == "Mass Suggestion"
        )
        assert arcanum_spell["grant"]["method"] == "mystic_arcanum"

        novice_sheet = default_character_sheet()
        novice_sheet["progression"].update(
            {
                "level": 2,
                "classes": [
                    {
                        "name": "Warlock",
                        "level": 2,
                        "subclass": "The Fiend",
                        "hit_die": 8,
                    }
                ],
            }
        )
        novice_sheet["spellcasting"]["preparation"]["mode"] = "known"
        novice = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Nox",
                    "sheet": novice_sheet,
                },
                "idempotency_key": "novice",
            },
        )
        eldritch_blast = await _call(
            server,
            "character_content_apply",
            {
                "character_id": novice["id"],
                "artifact_id": "dnd5e.content.srd2014.spell.eldritch-blast",
                "selection": {"source_class": "Warlock", "method": "known"},
                "expected_revision": novice["revision"],
                "idempotency_key": "eldritch-blast",
            },
        )
        with pytest.raises(Exception, match="level prerequisite"):
            await _call(
                server,
                "character_content_apply",
                {
                    "character_id": novice["id"],
                    "artifact_id": ("dnd5e.content.srd2014.feature.warlock-eldritch-invocations"),
                    "selection": {
                        "grant_level": 2,
                        "options": ["Ascendant Step", "Agonizing Blast"],
                    },
                    "expected_revision": eldritch_blast["revision"],
                    "idempotency_key": "invalid-invocations",
                },
            )
        invocations = await _call(
            server,
            "character_content_apply",
            {
                "character_id": novice["id"],
                "artifact_id": ("dnd5e.content.srd2014.feature.warlock-eldritch-invocations"),
                "selection": {
                    "grant_level": 2,
                    "options": ["Agonizing Blast", "Armor of Shadows"],
                },
                "expected_revision": eldritch_blast["revision"],
                "idempotency_key": "invocations",
            },
        )
        invocation_character = invocations
        mage_armor = next(
            spell
            for spell in invocation_character["sheet"]["content"]["spells"]
            if spell["name"] == "Mage Armor"
        )
        assert mage_armor["access"]["at_will"] is True
        assert mage_armor["access"]["known"] is False
        assert mage_armor["access"]["prepared"] is False
        assert mage_armor["grant"]["method"] == "eldritch_invocation"

    asyncio.run(exercise())


def test_favored_enemy_language_is_conditional_and_materialized(
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
            {
                "name": "Favored Enemy",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )

        async def create_ranger(name: str, key: str) -> dict:
            sheet = default_character_sheet()
            sheet["progression"].update(
                {
                    "level": 1,
                    "classes": [
                        {
                            "name": "Ranger",
                            "level": 1,
                            "subclass": "",
                            "hit_die": 10,
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
                    "idempotency_key": key,
                },
            )

        feature_id = "dnd5e.content.srd2014.feature.ranger-favored-enemy"
        ooze_hunter = await create_ranger("Ooze Hunter", "ooze-hunter")
        with pytest.raises(Exception, match="explicitly record"):
            await _call(
                server,
                "character_content_apply",
                {
                    "character_id": ooze_hunter["id"],
                    "artifact_id": feature_id,
                    "selection": {
                        "favored_enemy": {
                            "creature_type": "Oozes",
                            "humanoid_races": [],
                        }
                    },
                    "expected_revision": ooze_hunter["revision"],
                    "idempotency_key": "missing-language-fact",
                },
            )
        no_language = await _call(
            server,
            "character_content_apply",
            {
                "character_id": ooze_hunter["id"],
                "artifact_id": feature_id,
                "selection": {
                    "favored_enemy": {
                        "creature_type": "Oozes",
                        "humanoid_races": [],
                        "enemy_speaks_language": False,
                        "language": "",
                    }
                },
                "expected_revision": ooze_hunter["revision"],
                "idempotency_key": "oozes",
            },
        )
        assert no_language["sheet"]["traits"]["languages"] == []

        beast_hunter = await create_ranger("Beast Hunter", "beast-hunter")
        learned = await _call(
            server,
            "character_content_apply",
            {
                "character_id": beast_hunter["id"],
                "artifact_id": feature_id,
                "selection": {
                    "favored_enemy": {
                        "creature_type": "Beasts",
                        "humanoid_races": [],
                        "enemy_speaks_language": True,
                        "language": "Sylvan",
                    }
                },
                "expected_revision": beast_hunter["revision"],
                "idempotency_key": "beasts",
            },
        )
        assert learned["sheet"]["traits"]["languages"] == ["Sylvan"]

    asyncio.run(exercise())


def test_draconic_resilience_applies_retroactive_max_hp_and_unarmored_ac(
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
            {
                "name": "Draconic Resilience",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        sheet = default_character_sheet()
        sheet["progression"].update(
            {
                "level": 1,
                "classes": [
                    {
                        "name": "Sorcerer",
                        "level": 1,
                        "subclass": "Draconic Bloodline",
                        "hit_die": 6,
                    }
                ],
            }
        )
        sheet["combat"]["hp"] = {"value": 6, "max": 6, "temp": 0}
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Scale",
                    "sheet": sheet,
                },
                "idempotency_key": "actor",
            },
        )
        ancestry = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": ("dnd5e.content.srd2014.feature.draconic-bloodline-dragon-ancestor"),
                "selection": {"option": "Red"},
                "expected_revision": actor["revision"],
                "idempotency_key": "ancestor",
            },
        )
        assert ancestry["sheet"]["traits"]["languages"] == ["Draconic"]

        applied = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": (
                    "dnd5e.content.srd2014.feature.draconic-bloodline-draconic-resilience"
                ),
                "selection": {"initial_setup_full_hp": True},
                "expected_revision": ancestry["revision"],
                "idempotency_key": "resilience",
            },
        )

        assert applied["sheet"]["combat"]["hp"] == {
            "value": 7,
            "max": 7,
            "temp": 0,
        }
        assert applied["derived"]["armor_class"] == 13
        assert applied["derived"]["armor_class_breakdown"]["mode"] == ("unarmored_formula")
        assert applied["sheet"]["effects"][0]["source"].endswith(
            "draconic-bloodline-draconic-resilience"
        )

    asyncio.run(exercise())


def test_multiclass_channel_divinity_uses_the_shared_cleric_capacity(
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
            {
                "name": "Shared Channel Divinity",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        sheet = default_character_sheet()
        sheet["progression"].update(
            {
                "level": 9,
                "classes": [
                    {
                        "name": "Cleric",
                        "level": 6,
                        "subclass": "Life Domain",
                        "hit_die": 8,
                    },
                    {
                        "name": "Paladin",
                        "level": 3,
                        "subclass": "Oath of Devotion",
                        "hit_die": 10,
                    },
                ],
            }
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Shared Faith",
                    "sheet": sheet,
                },
                "idempotency_key": "actor",
            },
        )
        cleric = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": ("dnd5e.content.srd2014.feature.cleric-channel-divinity"),
                "selection": {},
                "expected_revision": actor["revision"],
                "idempotency_key": "cleric-channel",
            },
        )
        assert cleric["sheet"]["resources"]["channel_divinity"]["max"] == 2

        paladin = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": ("dnd5e.content.srd2014.feature.oath-of-devotion-channel-divinity"),
                "selection": {},
                "expected_revision": cleric["revision"],
                "idempotency_key": "paladin-channel",
            },
        )
        assert paladin["sheet"]["resources"]["channel_divinity"]["max"] == 2
        assert [
            item["name"]
            for item in paladin["sheet"]["content"]["features"]
            if item["name"] == "Channel Divinity"
        ] == ["Channel Divinity", "Channel Divinity"]

    asyncio.run(exercise())


def test_extra_attack_materializes_the_fighter_level_count_through_mcp(
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
            {
                "name": "Fighter Extra Attack",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        sheet = default_character_sheet()
        sheet["progression"].update(
            {
                "level": 11,
                "classes": [
                    {
                        "name": "Fighter",
                        "level": 11,
                        "subclass": "Champion",
                        "hit_die": 10,
                    }
                ],
            }
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Three Strikes",
                    "sheet": sheet,
                },
                "idempotency_key": "actor",
            },
        )
        applied = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": ("dnd5e.content.srd2014.feature.fighter-extra-attack"),
                "selection": {},
                "expected_revision": actor["revision"],
                "idempotency_key": "extra-attack",
            },
        )

        assert applied["sheet"]["combat"]["attacks_per_action"] == 3
        assert applied["derived"]["attacks_per_action"] == 3

    asyncio.run(exercise())


def test_multiclass_character_cannot_gain_unarmored_defense_twice(
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
            {
                "name": "One Unarmored Defense",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        sheet = default_character_sheet()
        sheet["progression"].update(
            {
                "level": 2,
                "classes": [
                    {
                        "name": "Barbarian",
                        "level": 1,
                        "subclass": "",
                        "hit_die": 12,
                    },
                    {
                        "name": "Monk",
                        "level": 1,
                        "subclass": "",
                        "hit_die": 8,
                    },
                ],
            }
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Disciplined Rager",
                    "sheet": sheet,
                },
                "idempotency_key": "actor",
            },
        )
        barbarian = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": ("dnd5e.content.srd2014.feature.barbarian-unarmored-defense"),
                "selection": {},
                "expected_revision": actor["revision"],
                "idempotency_key": "barbarian-defense",
            },
        )
        with pytest.raises(Exception, match="cannot gain it again"):
            await _call(
                server,
                "character_content_apply",
                {
                    "character_id": actor["id"],
                    "artifact_id": ("dnd5e.content.srd2014.feature.monk-unarmored-defense"),
                    "selection": {},
                    "expected_revision": barbarian["revision"],
                    "idempotency_key": "monk-defense",
                },
            )

    asyncio.run(exercise())


def test_fighting_style_uniqueness_ignores_unrelated_option_choices(
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
            {
                "name": "Scoped Fighting Styles",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        sheet = default_character_sheet()
        sheet["progression"].update(
            {
                "level": 3,
                "classes": [
                    {
                        "name": "Fighter",
                        "level": 1,
                        "subclass": "",
                        "hit_die": 10,
                    },
                    {
                        "name": "Ranger",
                        "level": 2,
                        "subclass": "",
                        "hit_die": 10,
                    },
                ],
            }
        )
        sheet["content"]["features"].append(
            {
                "id": "unrelated-option",
                "name": "Unrelated Option",
                "choices": {"option": "Defense"},
            }
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Versatile Warrior",
                    "sheet": sheet,
                },
                "idempotency_key": "actor",
            },
        )
        fighter = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": ("dnd5e.content.srd2014.feature.fighter-fighting-style"),
                "selection": {"option": "Defense"},
                "expected_revision": actor["revision"],
                "idempotency_key": "fighter-style",
            },
        )
        selected = next(
            item
            for item in fighter["sheet"]["content"]["features"]
            if item["id"].endswith("fighter-fighting-style")
        )
        assert selected["choices"] == {"option": "Defense"}

        with pytest.raises(Exception, match="already selected"):
            await _call(
                server,
                "character_content_apply",
                {
                    "character_id": actor["id"],
                    "artifact_id": ("dnd5e.content.srd2014.feature.ranger-fighting-style"),
                    "selection": {"option": "Defense"},
                    "expected_revision": fighter["revision"],
                    "idempotency_key": "duplicate-style",
                },
            )

    asyncio.run(exercise())


def test_monk_unarmored_defense_materializes_its_wisdom_formula(
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
            {
                "name": "Unarmored Defense",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        sheet = default_character_sheet()
        sheet["progression"].update(
            {
                "level": 1,
                "classes": [
                    {
                        "name": "Monk",
                        "level": 1,
                        "subclass": "",
                        "hit_die": 8,
                    }
                ],
            }
        )
        sheet["abilities"]["dexterity"]["score"] = 14
        sheet["abilities"]["wisdom"]["score"] = 16
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Still Water",
                    "sheet": sheet,
                },
                "idempotency_key": "actor",
            },
        )

        applied = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": ("dnd5e.content.srd2014.feature.monk-unarmored-defense"),
                "selection": {},
                "expected_revision": actor["revision"],
                "idempotency_key": "unarmored-defense",
            },
        )

        assert applied["derived"]["armor_class"] == 15
        assert applied["derived"]["armor_class_breakdown"]["ability_bonus"] == {
            "ability": "wisdom",
            "bonus": 3,
        }
        assert applied["sheet"]["effects"][0]["changes"][0]["value"] == {
            "base": 10,
            "ability": "wisdom",
            "allows_shield": False,
            "includes_dexterity": True,
        }

    asyncio.run(exercise())


def test_paladin_channel_divinity_is_publicly_materialized_and_rest_bound(
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
            {
                "name": "Paladin Channel Divinity",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        sheet = default_character_sheet()
        sheet["progression"].update(
            {
                "level": 3,
                "classes": [
                    {
                        "name": "Paladin",
                        "level": 3,
                        "subclass": "Oath of Devotion",
                        "hit_die": 10,
                    }
                ],
            }
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Dawn",
                    "sheet": sheet,
                },
                "idempotency_key": "actor",
            },
        )

        applied = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": ("dnd5e.content.srd2014.feature.oath-of-devotion-channel-divinity"),
                "selection": {},
                "expected_revision": actor["revision"],
                "idempotency_key": "channel-divinity",
            },
        )

        assert applied["sheet"]["resources"]["channel_divinity"] == {
            "label": "Channel Divinity",
            "value": 1,
            "max": 1,
            "recovers_on": "short_rest",
            "source_key": "Paladin",
            "slot_level": 0,
            "unlimited": False,
        }
        feature = next(
            item
            for item in applied["sheet"]["content"]["features"]
            if item["id"].endswith("oath-of-devotion-channel-divinity")
        )
        assert feature["resource_key"] == "channel_divinity"

    asyncio.run(exercise())


def test_signature_spells_are_always_prepared_and_use_explicit_free_resources(
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
            {
                "name": "Signature Spells",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        sheet = default_character_sheet()
        sheet["progression"].update(
            {
                "level": 20,
                "classes": [
                    {
                        "name": "Wizard",
                        "level": 20,
                        "subclass": "School of Evocation",
                        "hit_die": 6,
                    }
                ],
            }
        )
        sheet["spellcasting"].update(
            {
                "ability": "intelligence",
                "class_lists": ["wizard"],
                "spell_slots": {
                    "3": {
                        "label": "3rd-level slots",
                        "value": 1,
                        "max": 3,
                        "unlimited": False,
                        "recovers_on": "long_rest",
                        "source_key": "Wizard",
                        "slot_level": 3,
                    }
                },
            }
        )
        sheet["spellcasting"]["preparation"].update(
            {
                "mode": "spellbook",
                "max_prepared": 25,
                "changes_on": "long_rest",
            }
        )
        sheet["spellcasting"]["spellbook"]["enabled"] = True
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Archmage",
                    "sheet": sheet,
                },
                "idempotency_key": "actor",
            },
        )
        spell_ids = [
            "dnd5e.content.srd2014.spell.fireball",
            "dnd5e.content.srd2014.spell.counterspell",
        ]
        current = actor
        for index, spell_id in enumerate(spell_ids):
            current = await _call(
                server,
                "character_content_apply",
                {
                    "character_id": actor["id"],
                    "artifact_id": spell_id,
                    "selection": {
                        "method": "spellbook",
                        "source_class": "Wizard",
                    },
                    "expected_revision": current["revision"],
                    "idempotency_key": f"spell-{index}",
                },
            )

        applied = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": ("dnd5e.content.srd2014.feature.wizard-signature-spells"),
                "selection": {"spell_artifact_ids": spell_ids},
                "expected_revision": current["revision"],
                "idempotency_key": "signature",
            },
        )
        applied_character = applied
        by_id = {spell["id"]: spell for spell in applied_character["sheet"]["content"]["spells"]}
        for spell_id in spell_ids:
            assert by_id[spell_id]["access"]["always_prepared"] is True
            assert by_id[spell_id]["access"]["prepared"] is True
            assert (
                applied_character["sheet"]["resources"][f"signature_spell:{spell_id}"]["value"] == 1
            )

        cast = await _call(
            server,
            "character_action",
            {
                "character_id": actor["id"],
                "action": "cast_spell",
                "payload": {"spell_id": spell_ids[0], "cast_level": 3, "signature_free_cast": True},
                "principal_id": "system:local",
                "expected_revision": applied_character["revision"],
                "idempotency_key": "free-fireball",
            },
        )
        assert cast["result"]["payment"]["economy"] == "signature_spell"
        updated = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": actor["id"]}},
        )
        assert updated["sheet"]["spellcasting"]["spell_slots"]["3"]["value"] == 1
        assert updated["sheet"]["resources"][f"signature_spell:{spell_ids[0]}"]["value"] == 0

        with pytest.raises(Exception, match="free use is unavailable"):
            await _call(
                server,
                "character_action",
                {
                    "character_id": actor["id"],
                    "action": "cast_spell",
                    "payload": {
                        "spell_id": spell_ids[0],
                        "cast_level": 3,
                        "signature_free_cast": True,
                    },
                    "principal_id": "system:local",
                    "expected_revision": updated["revision"],
                    "idempotency_key": "free-fireball-again",
                },
            )

    asyncio.run(exercise())


def test_spell_mastery_preserves_existing_preparation_state(
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
            {
                "name": "Spell Mastery",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        spell_ids = [
            "dnd5e.content.srd2014.spell.shield",
            "dnd5e.content.srd2014.spell.misty-step",
            "dnd5e.content.srd2014.spell.magic-missile",
            "dnd5e.content.srd2014.spell.invisibility",
        ]
        sheet = default_character_sheet()
        sheet["progression"].update(
            {
                "level": 18,
                "classes": [
                    {
                        "name": "Wizard",
                        "level": 18,
                        "subclass": "School of Evocation",
                        "hit_die": 6,
                    }
                ],
            }
        )
        sheet["spellcasting"]["preparation"].update(
            {
                "mode": "spellbook",
                "max_prepared": 23,
                "changes_on": "long_rest",
                "selected_spell_ids": [spell_ids[0], spell_ids[2]],
            }
        )
        sheet["spellcasting"]["spellbook"].update({"enabled": True, "spell_ids": spell_ids})
        sheet["content"]["spells"] = [
            {
                "id": spell_ids[0],
                "name": "Shield",
                "level": 1,
                "grant": {
                    "source_type": "class",
                    "source_key": "wizard",
                    "method": "spellbook",
                },
                "access": {
                    "known": False,
                    "prepared": True,
                    "in_spellbook": True,
                },
            },
            {
                "id": spell_ids[1],
                "name": "Misty Step",
                "level": 2,
                "grant": {
                    "source_type": "class",
                    "source_key": "wizard",
                    "method": "spellbook",
                },
                "access": {
                    "known": False,
                    "prepared": False,
                    "in_spellbook": True,
                },
            },
            {
                "id": spell_ids[2],
                "name": "Magic Missile",
                "level": 1,
                "grant": {
                    "source_type": "class",
                    "source_key": "wizard",
                    "method": "spellbook",
                },
                "access": {
                    "known": False,
                    "prepared": True,
                    "in_spellbook": True,
                },
            },
            {
                "id": spell_ids[3],
                "name": "Invisibility",
                "level": 2,
                "grant": {
                    "source_type": "class",
                    "source_key": "wizard",
                    "method": "spellbook",
                },
                "access": {
                    "known": False,
                    "prepared": False,
                    "in_spellbook": True,
                },
            },
        ]
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Master",
                    "sheet": sheet,
                },
                "idempotency_key": "actor",
            },
        )
        mastery_catalog = await _call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "feature",
                    "query": "dnd5e.content.srd2014.feature.wizard-spell-mastery",
                    "include_context": True,
                },
            },
        )
        mastery_source_ref = mastery_catalog[0]["runtime_context"]["rule_refs"][0]

        applied = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": ("dnd5e.content.srd2014.feature.wizard-spell-mastery"),
                "selection": {"spell_artifact_ids": spell_ids[:2]},
                "expected_revision": actor["revision"],
                "idempotency_key": "mastery",
            },
        )
        by_id = {spell["id"]: spell for spell in applied["sheet"]["content"]["spells"]}
        assert by_id[spell_ids[0]]["access"]["prepared"] is True
        assert by_id[spell_ids[1]]["access"]["prepared"] is False
        assert all(by_id[spell_id]["access"]["at_will"] for spell_id in spell_ids[:2])
        assert all(
            by_id[spell_id]["access"]["at_will_sources"] == ["spell_mastery:Wizard"]
            for spell_id in spell_ids[:2]
        )
        assert all(by_id[spell_id]["access"]["known"] is False for spell_id in spell_ids)

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
        clock = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "clock_set",
                "payload": {
                    "day": 1,
                    "hour": 0,
                    "minute": 0,
                    "label": "Study",
                },
                "expected_revision": phase["campaign_revision"],
                "idempotency_key": "clock",
            },
        )
        with pytest.raises(Exception, match="required 480 minutes"):
            await _call(
                server,
                "character_content_apply",
                {
                    "character_id": actor["id"],
                    "artifact_id": ("dnd5e.content.srd2014.feature.wizard-spell-mastery"),
                    "grant": {
                        "kind": "training",
                        "reason": "The wizard is retraining Spell Mastery.",
                        "source_ref": mastery_source_ref,
                    },
                    "selection": {
                        "spell_artifact_ids": spell_ids[2:],
                        "replace_existing": True,
                        "study_started_elapsed_ticks": 0,
                    },
                    "expected_revision": applied["revision"],
                    "idempotency_key": "too-early",
                },
            )
        advanced = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "clock_advance",
                "payload": {
                    "period": "minute",
                    "count": 480,
                    "expected_elapsed_ticks": 4800,
                },
                "expected_revision": clock["campaign_revision"],
                "idempotency_key": "study",
            },
        )
        replaced = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": ("dnd5e.content.srd2014.feature.wizard-spell-mastery"),
                "grant": {
                    "kind": "training",
                    "reason": "The wizard completed Spell Mastery retraining.",
                    "source_ref": mastery_source_ref,
                },
                "selection": {
                    "spell_artifact_ids": spell_ids[2:],
                    "replace_existing": True,
                    "study_started_elapsed_ticks": 0,
                },
                "expected_revision": applied["revision"],
                "idempotency_key": "replace",
            },
        )
        assert advanced["world_time"]["elapsed_minutes"] == 480
        by_id = {spell["id"]: spell for spell in replaced["sheet"]["content"]["spells"]}
        assert all(by_id[spell_id]["access"]["at_will"] is False for spell_id in spell_ids[:2])
        assert all(by_id[spell_id]["access"]["at_will"] is True for spell_id in spell_ids[2:])
        mastery = next(
            item
            for item in replaced["sheet"]["content"]["features"]
            if item["id"].endswith("wizard-spell-mastery")
        )
        assert mastery["choices"]["spell_artifact_ids"] == spell_ids[2:]
        assert mastery["choices"]["_replacement_history"] == [
            {
                "previous_spell_artifact_ids": spell_ids[:2],
                "study_started_elapsed_ticks": 0,
                "study_completed_elapsed_ticks": 4800,
                "study_minutes": 480,
            }
        ]

    asyncio.run(exercise())


def test_level_plan_is_read_only_and_orders_feature_resource_dependencies(
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
            {
                "name": "Read-only advancement plan",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        sheet = default_character_sheet()
        sheet["progression"].update(
            {
                "level": 1,
                "classes": [
                    {
                        "name": "Cleric",
                        "level": 1,
                        "subclass": "Life Domain",
                        "hit_die": 8,
                    }
                ],
            }
        )
        cleric = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Mercy",
                    "sheet": sheet,
                },
                "idempotency_key": "cleric",
            },
        )

        plan = await _call(
            server,
            "character_query",
            {
                "view": "advancement",
                "payload": {
                    "character_id": cleric["id"],
                    "class_name": "Cleric",
                },
            },
        )
        feature_ids = [item["artifact_id"] for item in plan["follow_up"]["feature_artifacts"]]
        channel_id = "dnd5e.content.srd2014.feature.cleric-channel-divinity"
        preserve_id = "dnd5e.content.srd2014.feature.life-domain-channel-divinity-preserve-life"
        assert feature_ids.index(channel_id) < feature_ids.index(preserve_id)
        assert plan["new_level"] == 2

        unchanged = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": cleric["id"]}},
        )
        assert unchanged["revision"] == cleric["revision"]
        assert unchanged["sheet"]["progression"]["level"] == 1
        assert unchanged["sheet"]["resources"] == cleric["sheet"]["resources"]

    asyncio.run(exercise())


def test_land_druid_bonus_cantrip_and_non_list_circle_spells_are_materialized(
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
            {
                "name": "Circle Spell Advancement",
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
                    "name": "Rowan",
                    "sheet": _land_druid_sheet(),
                },
                "idempotency_key": "actor",
            },
        )
        bonus = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": ("dnd5e.content.srd2014.feature.circle-of-the-land-bonus-cantrip"),
                "selection": {"spell_artifact_id": "dnd5e.content.srd2014.spell.guidance"},
                "expected_revision": actor["revision"],
                "idempotency_key": "bonus-cantrip",
            },
        )
        guidance = next(
            spell
            for spell in bonus["sheet"]["content"]["spells"]
            if spell["id"] == "dnd5e.content.srd2014.spell.guidance"
        )
        assert guidance["grant"] == {
            "source_type": "subclass",
            "source_key": "Circle of the Land",
            "method": "known",
        }
        assert guidance["access"]["known"] is True

        advanced = await _call(
            server,
            "character_state_change",
            {
                "character_id": actor["id"],
                "action": "level_advance",
                "payload": {
                    "class_name": "Druid",
                    "hp_method": "fixed",
                    "reason": "milestone",
                    "source_ref": CORE_ADVANCEMENT_RULE_REF,
                },
                "expected_revision": bonus["revision"],
                "idempotency_key": "level-3",
            },
        )
        unlocked = {item["name"] for item in advanced["advancement"]["subclass_spell_grants"]}
        assert unlocked == {"Mirror Image", "Misty Step"}
        spells = {
            spell["name"]: spell for spell in advanced["character"]["sheet"]["content"]["spells"]
        }
        for name in unlocked:
            assert spells[name]["access"]["always_prepared"] is True
            assert spells[name]["grant"]["source_key"] == "Circle of the Land"

    asyncio.run(exercise())


def test_ability_score_improvement_is_applied_and_repeats_at_later_unlocks(
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
            {"name": "Repeated ASI", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Ari",
                    "sheet": _fighter_sheet(),
                },
                "idempotency_key": "actor",
            },
        )

        level_four = await _call(
            server,
            "character_state_change",
            {
                "character_id": actor["id"],
                "action": "level_advance",
                "payload": {
                    "class_name": "Fighter",
                    "hp_method": "fixed",
                    "reason": "milestone",
                    "source_ref": CORE_ADVANCEMENT_RULE_REF,
                },
                "expected_revision": actor["revision"],
                "idempotency_key": "level-4",
            },
        )
        asi_id = "dnd5e.content.srd2014.feature.fighter-ability-score-improvement"
        first_offer = next(
            item
            for item in level_four["advancement"]["follow_up"]["feature_artifacts"]
            if item["artifact_id"] == asi_id
        )
        assert first_offer["grant_level"] == 4
        first_asi = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": asi_id,
                "selection": {
                    "grant_level": 4,
                    "ability_score_increases": {"strength": 1, "constitution": 1},
                },
                "expected_revision": level_four["character"]["revision"],
                "idempotency_key": "asi-4",
            },
        )
        assert first_asi["sheet"]["abilities"]["strength"]["score"] == 16
        assert first_asi["sheet"]["abilities"]["constitution"]["score"] == 14
        assert first_asi["sheet"]["combat"]["hp"] == {"value": 20, "max": 35, "temp": 0}
        feature = next(
            item for item in first_asi["sheet"]["content"]["features"] if item["id"] == asi_id
        )
        assert [item["level"] for item in feature["advancement_grants"]] == [4]

        current = first_asi
        for level in (5, 6):
            current = await _call(
                server,
                "character_state_change",
                {
                    "character_id": actor["id"],
                    "action": "level_advance",
                    "payload": {
                        "class_name": "Fighter",
                        "hp_method": "fixed",
                        "reason": "milestone",
                        "source_ref": CORE_ADVANCEMENT_RULE_REF,
                    },
                    "expected_revision": current["revision"]
                    if "revision" in current
                    else current["character"]["revision"],
                    "idempotency_key": f"level-{level}",
                },
            )
        repeat_offer = next(
            item
            for item in current["advancement"]["follow_up"]["feature_artifacts"]
            if item["artifact_id"] == asi_id
        )
        assert repeat_offer["grant_level"] == 6
        second_asi = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": asi_id,
                "selection": {
                    "grant_level": 6,
                    "ability_score_increases": {"strength": 2},
                },
                "expected_revision": current["character"]["revision"],
                "idempotency_key": "asi-6",
            },
        )
        assert second_asi["sheet"]["abilities"]["strength"]["score"] == 18
        feature = next(
            item for item in second_asi["sheet"]["content"]["features"] if item["id"] == asi_id
        )
        assert [item["level"] for item in feature["advancement_grants"]] == [4, 6]

        with pytest.raises(Exception, match="already recorded"):
            await _call(
                server,
                "character_content_apply",
                {
                    "character_id": actor["id"],
                    "artifact_id": asi_id,
                    "selection": {
                        "grant_level": 6,
                        "ability_score_increases": {"dexterity": 2},
                    },
                    "expected_revision": second_asi["revision"],
                    "idempotency_key": "duplicate-asi-6",
                },
            )

    asyncio.run(exercise())


def test_prepared_spell_limit_tracks_spellcasting_ability_score_improvement(
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
            {
                "name": "Prepared Limit ASI",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        sheet = _cleric_sheet()
        sheet["progression"]["level"] = 3
        sheet["progression"]["classes"][0]["level"] = 3
        sheet["abilities"]["wisdom"]["score"] = 17
        sheet["combat"]["hp"] = {"value": 28, "max": 28, "temp": 0}
        sheet["combat"]["hit_dice"]["d8"].update(value=3, max=3)
        sheet["combat"]["hp_progression"] = [
            {
                "level": level,
                "method": "manual" if level == 1 else "fixed",
                "value": 12 if level == 1 else 8,
                "source": f"Cleric level {level}",
            }
            for level in range(1, 4)
        ]
        sheet["spellcasting"]["preparation"]["max_prepared"] = 6
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Prepared Cleric",
                    "sheet": sheet,
                },
                "idempotency_key": "actor",
            },
        )
        level_four = await _call(
            server,
            "character_state_change",
            {
                "character_id": actor["id"],
                "action": "level_advance",
                "payload": {
                    "class_name": "Cleric",
                    "hp_method": "fixed",
                    "reason": "milestone",
                    "source_ref": CORE_ADVANCEMENT_RULE_REF,
                },
                "expected_revision": actor["revision"],
                "idempotency_key": "level-4",
            },
        )
        assert level_four["character"]["sheet"]["spellcasting"]["preparation"]["max_prepared"] == 7

        applied = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": ("dnd5e.content.srd2014.feature.cleric-ability-score-improvement"),
                "selection": {
                    "grant_level": 4,
                    "ability_score_increases": {"wisdom": 1, "constitution": 1},
                },
                "expected_revision": level_four["character"]["revision"],
                "idempotency_key": "asi-4",
            },
        )

        assert applied["sheet"]["abilities"]["wisdom"]["score"] == 18
        assert applied["sheet"]["spellcasting"]["preparation"]["max_prepared"] == 8

        stale_sheet = applied["sheet"]
        stale_sheet["spellcasting"]["preparation"]["max_prepared"] = 7
        stale_actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Legacy Prepared Cleric",
                    "sheet": stale_sheet,
                },
                "idempotency_key": "stale-actor",
            },
        )
        repaired = await _call(
            server,
            "character_state_change",
            {
                "character_id": stale_actor["id"],
                "action": "resource_sync",
                "payload": {
                    "reason": (
                        "Recompute a stored prepared-spell limit after a prior "
                        "spellcasting ability increase."
                    )
                },
                "expected_revision": stale_actor["revision"],
                "idempotency_key": "repair-prepared-limit",
            },
        )
        assert repaired["character"]["sheet"]["spellcasting"]["preparation"]["max_prepared"] == 8
        assert repaired["changes"][-1] == {
            "target": "spellcasting.preparation.max_prepared",
            "old_value": 7,
            "new_value": 8,
            "class_limits": {"cleric": 8},
        }

    asyncio.run(exercise())


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
        plan = await _call(
            server,
            "character_query",
            {
                "view": "advancement",
                "payload": {"character_id": actor["id"], "class_name": "Cleric"},
            },
        )
        assert plan["follow_up"]["prepared_spell_event"] is None
        advancement_source_ref = CORE_ADVANCEMENT_RULE_REF
        arguments = {
            "character_id": actor["id"],
            "action": "level_advance",
            "payload": {
                "class_name": "Cleric",
                "hp_method": "fixed",
                "reason": "survived the opening encounter",
                "source_ref": advancement_source_ref,
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
        assert [item["value"] for item in sheet["combat"]["hp_progression"]] == [12, 9]
        assert sum(item["value"] for item in sheet["combat"]["hp_progression"]) == 21
        hp_gain = sheet["combat"]["hp_progression"][-1]
        assert hp_gain["source"] == "Cleric level 2"
        assert hp_gain["source_ref"] == advancement_source_ref
        assert hp_gain["reason"] == "survived the opening encounter"
        assert sheet["spellcasting"]["spell_slots"]["1"]["value"] == 1
        assert sheet["spellcasting"]["spell_slots"]["1"]["max"] == 3
        assert advanced["advancement"]["hp_bonus_sources"][0]["amount"] == 1
        assert advanced["advancement"]["follow_up"]["prepared_spell_event"] is None
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
                    "dnd5e.content.srd2014.feature.life-domain-channel-divinity-preserve-life"
                ),
                "expected_revision": channel["revision"],
                "idempotency_key": "preserve",
            },
        )
        with pytest.raises(Exception, match="above half"):
            await _call(
                server,
                "character_action",
                {
                    "character_id": actor["id"],
                    "action": "use_activity",
                    "payload": {
                        "activity_id": (
                            "dnd5e.content.srd2014.feature."
                            "life-domain-channel-divinity-preserve-life"
                        ),
                        "declaration": {
                            "allocations": [
                                {
                                    "target_id": actor["id"],
                                    "amount": 4,
                                    "expected_revision": preserve["revision"],
                                    "within_30_ft": True,
                                }
                            ]
                        },
                    },
                    "expected_revision": preserve["revision"],
                    "idempotency_key": "invalid-preserve",
                },
            )
        unchanged = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": actor["id"]}},
        )
        assert unchanged["revision"] == preserve["revision"]
        assert unchanged["sheet"]["resources"]["channel_divinity"]["value"] == 1
        used = await _call(
            server,
            "character_action",
            {
                "character_id": actor["id"],
                "action": "use_activity",
                "payload": {
                    "activity_id": (
                        "dnd5e.content.srd2014.feature.life-domain-channel-divinity-preserve-life"
                    ),
                    "declaration": {
                        "allocations": [
                            {
                                "target_id": actor["id"],
                                "amount": 3,
                                "expected_revision": preserve["revision"],
                                "within_30_ft": True,
                            }
                        ]
                    },
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
        assert used["character"]["sheet"]["combat"]["hp"]["value"] == 10

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

        current_character = used["character"]
        for level in range(3, 7):
            current_character = (
                await _call(
                    server,
                    "character_state_change",
                    {
                        "character_id": actor["id"],
                        "action": "level_advance",
                        "payload": {
                            "class_name": "Cleric",
                            "hp_method": "fixed",
                            "reason": f"resource scaling regression level {level}",
                            "source_ref": CORE_ADVANCEMENT_RULE_REF,
                        },
                        "expected_revision": current_character["revision"],
                        "idempotency_key": f"cleric-level-{level}",
                    },
                )
            )["character"]
        channel_resource = current_character["sheet"]["resources"]["channel_divinity"]
        assert channel_resource["max"] == 2
        # One use was spent before leveling. Gaining one point of capacity grants
        # only that new point; level advancement is not a hidden rest.
        assert channel_resource["value"] == 1
        channel_feature = next(
            item
            for item in current_character["sheet"]["content"]["features"]
            if item["id"] == "dnd5e.content.srd2014.feature.cleric-channel-divinity"
        )
        assert channel_feature["resource_scaling"]["maximum_by_level"]["18"] == 3

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
                        "source_ref": CORE_ADVANCEMENT_RULE_REF,
                    },
                    "expected_revision": unresolvable["revision"],
                    "idempotency_key": "unresolvable-level",
                },
            )

    asyncio.run(exercise())


def test_level_advance_materializes_new_always_prepared_domain_spells(
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
            {
                "name": "Domain Spell Advancement",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        sheet = _cleric_sheet()
        sheet["progression"]["classes"][0]["subclass"] = ""
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Mara",
                    "sheet": sheet,
                },
                "idempotency_key": "actor",
            },
        )
        selected = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": "dnd5e.content.srd2014.subclass.life-domain",
                "selection": {"target_class_name": "Cleric"},
                "expected_revision": actor["revision"],
                "idempotency_key": "life-domain",
            },
        )
        level_two = await _call(
            server,
            "character_state_change",
            {
                "character_id": actor["id"],
                "action": "level_advance",
                "payload": {
                    "class_name": "Cleric",
                    "hp_method": "fixed",
                    "reason": "module milestone",
                    "source_ref": CORE_ADVANCEMENT_RULE_REF,
                },
                "expected_revision": selected["revision"],
                "idempotency_key": "level-2",
            },
        )
        level_three = await _call(
            server,
            "character_state_change",
            {
                "character_id": actor["id"],
                "action": "level_advance",
                "payload": {
                    "class_name": "Cleric",
                    "hp_method": "fixed",
                    "reason": "module milestone",
                    "source_ref": CORE_ADVANCEMENT_RULE_REF,
                },
                "expected_revision": level_two["character"]["revision"],
                "idempotency_key": "level-3",
            },
        )

        unlocked = {item["name"] for item in level_three["advancement"]["subclass_spell_grants"]}
        assert unlocked == {"Lesser Restoration", "Spiritual Weapon"}
        spells = {
            spell["name"]: spell for spell in level_three["character"]["sheet"]["content"]["spells"]
        }
        assert set(spells) == {
            "Bless",
            "Cure Wounds",
            "Lesser Restoration",
            "Spiritual Weapon",
        }
        for name in unlocked:
            assert spells[name]["grant"] == {
                "source_type": "subclass",
                "source_key": "Life Domain",
                "method": "class_prepared",
            }
            assert spells[name]["access"]["always_prepared"] is True
            assert spells[name]["access"]["prepared"] is True

    asyncio.run(exercise())


def test_rolled_level_hp_is_engine_owned_idempotent_and_revision_safe(
    tmp_path: Path, monkeypatch
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
    calls: list[str] = []

    def fixed_roll(expression: str, *, rng=None):
        calls.append(expression)
        return engine_roll(expression, rng=_SequenceRng(3))

    monkeypatch.setattr(progression_module, "roll", fixed_roll)

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Rolled level", "edition": "2014", "idempotency_key": "campaign"},
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
                "hp_method": "rolled",
                "reason": "milestone",
                "source_ref": CORE_ADVANCEMENT_RULE_REF,
            },
            "expected_revision": actor["revision"],
            "idempotency_key": "rolled-level",
        }

        advanced = await _call(server, "character_state_change", arguments)
        replay = await _call(server, "character_state_change", arguments)

        assert replay == advanced
        assert calls == ["1d8"]
        assert advanced["advancement"]["hit_points"]["roll"]["total"] == 3
        assert advanced["character"]["sheet"]["combat"]["hp"]["max"] == 19

        stale = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Stale Mara",
                    "sheet": _cleric_sheet(),
                },
                "idempotency_key": "stale-actor",
            },
        )
        with pytest.raises(Exception, match="character revision conflict"):
            await _call(
                server,
                "character_state_change",
                {
                    **arguments,
                    "character_id": stale["id"],
                    "expected_revision": stale["revision"] + 1,
                    "idempotency_key": "stale-level",
                },
            )
        assert calls == ["1d8"]

        with pytest.raises(Exception, match="unexpected fields:.*hp_roll"):
            await _call(
                server,
                "character_state_change",
                {
                    **arguments,
                    "character_id": stale["id"],
                    "payload": {**arguments["payload"], "hp_roll": 8},
                    "expected_revision": stale["revision"],
                    "idempotency_key": "forged-level",
                },
            )
        assert calls == ["1d8"]

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
                        "source_ref": CORE_ADVANCEMENT_RULE_REF,
                    },
                    "expected_revision": actor["revision"],
                    "idempotency_key": "level",
                },
            )

    asyncio.run(exercise())


def test_xp_mode_awards_atomically_and_enforces_level_threshold(tmp_path: Path) -> None:
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
            {
                "name": "XP campaign",
                "edition": "2014",
                "advancement_mode": "xp",
                "idempotency_key": "campaign",
            },
        )
        assert campaign["settings"]["advancement"] == {"mode": "xp"}
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

        exact_source_ref = CORE_ADVANCEMENT_RULE_REF
        first_arguments = {
            "campaign_id": campaign["id"],
            "action": "experience_award",
            "payload": {
                "awards": [
                    {
                        "character_id": actor["id"],
                        "amount": 299,
                        "expected_revision": actor["revision"],
                    }
                ],
                "reason": "resolved the first threat",
                "source_ref": exact_source_ref,
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": "xp-299",
        }
        first = await _call(server, "campaign_change", first_arguments)
        assert first["awards"][0]["new_xp"] == 299
        assert first["awards"][0]["advancement"]["eligible"] is False
        assert first["awards"][0]["character"]["sheet"]["progression"]["level"] == 1
        assert first["source_ref"] == exact_source_ref
        assert await _call(server, "campaign_change", first_arguments) == first

        with pytest.raises(Exception, match="XP threshold"):
            await _call(
                server,
                "character_state_change",
                {
                    "character_id": actor["id"],
                    "action": "level_advance",
                    "payload": {
                        "class_name": "Cleric",
                        "hp_method": "fixed",
                        "reason": "premature",
                        "source_ref": CORE_ADVANCEMENT_RULE_REF,
                    },
                    "expected_revision": first["awards"][0]["character"]["revision"],
                    "idempotency_key": "premature-level",
                },
            )

        second = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "experience_award",
                "payload": {
                    "awards": [
                        {
                            "character_id": actor["id"],
                            "amount": 1,
                            "expected_revision": first["awards"][0]["character"]["revision"],
                        }
                    ],
                    "reason": "completed the objective",
                    "source_ref": CORE_ADVANCEMENT_RULE_REF,
                },
                "expected_revision": first["campaign"]["revision"],
                "idempotency_key": "xp-1",
            },
        )
        recipient = second["awards"][0]
        assert recipient["new_xp"] == 300
        assert recipient["advancement"]["eligible"] is True
        assert recipient["character"]["sheet"]["progression"]["level"] == 1

        advanced = await _call(
            server,
            "character_state_change",
            {
                "character_id": actor["id"],
                "action": "level_advance",
                "payload": {
                    "class_name": "Cleric",
                    "hp_method": "fixed",
                    "reason": "reached 300 XP",
                    "source_ref": CORE_ADVANCEMENT_RULE_REF,
                },
                "expected_revision": recipient["character"]["revision"],
                "idempotency_key": "level-2",
            },
        )
        assert advanced["character"]["sheet"]["progression"]["level"] == 2
        assert advanced["character"]["sheet"]["progression"]["xp"] == 300
        assert advanced["advancement"]["mode"] == "xp"
        assert advanced["advancement"]["experience_before"]["eligible"] is True
        assert advanced["advancement"]["experience_after"]["eligible"] is False
        assert len(second["campaign"]["state"]["advancement"]["xp_awards"]) == 2

    asyncio.run(exercise())
