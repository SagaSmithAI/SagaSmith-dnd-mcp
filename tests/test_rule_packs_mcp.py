import asyncio
import re
from pathlib import Path

import pytest
from sagasmith_core import Database, RuleProfileService
from sagasmith_core.database import sqlite_database_url
from sagasmith_dnd.character_schema import default_character_sheet
from sagasmith_dnd.core_rule_pack import get_core_rule_pack

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from tests.authoring_helpers import import_and_activate_addon_fixture


def test_mcp_runtime_never_emits_an_unregistered_core_boundary() -> None:
    source_root = Path(__file__).parents[1] / "src" / "sagasmith_dnd_mcp"
    emitted = {
        mechanic_id
        for path in source_root.rglob("*.py")
        for mechanic_id in re.findall(
            r'["\'](dnd5e\.core\.[a-z0-9_.]+)["\']',
            path.read_text(encoding="utf-8"),
        )
    }
    registered = {
        boundary.id
        for edition in ("2014", "2024")
        for boundary in get_core_rule_pack(edition).boundaries
    }

    assert emitted <= registered


@pytest.mark.fresh_database
def test_core_srd_content_catalog_is_structured_and_selectable(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[2]
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=workspace / "SagaSmith-dnd-skills",
        modulegen_skills_dir=workspace / "SagaSmith-module-gen-skills",
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "SRD Catalog", "idempotency_key": "catalog-campaign"},
        )
        await call(
            server,
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "set_profile",
                "payload": {"edition": "2014"},
                "principal_id": "system:local",
                "expected_revision": campaign["revision"],
                "idempotency_key": "catalog-profile",
            },
        )
        spells = await call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "spell",
                    "query": "Fireball",
                },
                "principal_id": "system:local",
            },
        )
        fireball = next(item for item in spells if item["name"] == "Fireball")
        assert fireball["pack_id"] == "dnd5e.content.srd2014"
        assert fireball["rule_refs"]
        assert fireball["selection_requirements"]["eligible_classes"] == [
            "sorcerer",
            "wizard",
        ]
        assert fireball["selection_requirements"]["level"] == 3
        standard_spells = await call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "spell",
                    "query": "Witch Bolt",
                },
                "principal_id": "system:local",
            },
        )
        witch_bolt = next(item for item in standard_spells if item["name"] == "Witch Bolt")
        assert witch_bolt["pack_id"] == "dnd5e.content.standard2014"
        assert witch_bolt["rule_refs"] == ["book:players-handbook-2014:p289"]
        sheet = default_character_sheet()
        sheet["progression"].update(
            {
                "level": 5,
                "classes": [{"name": "Wizard", "level": 5, "subclass": "", "hit_die": 6}],
            }
        )
        sheet["spellcasting"]["preparation"].update(
            {"mode": "spellbook", "max_prepared": 4, "changes_on": "long_rest"}
        )
        sheet["spellcasting"]["spellbook"]["enabled"] = True
        character = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Aria", "sheet": sheet},
                "principal_id": "system:local",
                "idempotency_key": "catalog-character",
            },
        )
        applied = await call(
            server,
            "character_content_apply",
            {
                "character_id": character["id"],
                "artifact_id": fireball["id"],
                "selection": {"source_class": "Wizard", "method": "spellbook"},
                "expected_revision": character["revision"],
                "idempotency_key": "catalog-fireball",
            },
        )
        spell = applied["sheet"]["content"]["spells"][0]
        assert spell["name"] == "Fireball"
        assert spell["definition"]["range"]["kind"] == "distance"
        assert spell["definition"]["range"]["normal_ft"] == 150
        assert spell["grant"]["source_key"] == "wizard"
        assert fireball["id"] in applied["sheet"]["spellcasting"]["spellbook"]["spell_ids"]

        subclasses = await call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "subclass",
                    "query": "Berserker",
                },
                "principal_id": "system:local",
            },
        )
        berserker = next(item for item in subclasses if item["name"] == "Path of the Berserker")
        multiclass_sheet = default_character_sheet()
        multiclass_sheet["progression"].update(
            {
                "level": 8,
                "classes": [
                    {"name": "Wizard", "level": 5, "subclass": "", "hit_die": 6},
                    {"name": "Barbarian", "level": 3, "subclass": "", "hit_die": 12},
                ],
            }
        )
        multiclass = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Multiclass",
                    "sheet": multiclass_sheet,
                },
                "principal_id": "system:local",
                "idempotency_key": "catalog-multiclass",
            },
        )
        spell_source_choice = await call(
            server,
            "character_content_apply",
            {
                "character_id": multiclass["id"],
                "artifact_id": fireball["id"],
                "expected_revision": multiclass["revision"],
                "idempotency_key": "catalog-multiclass-spell-source",
            },
        )
        assert spell_source_choice["status"] == "pending_choice"
        assert spell_source_choice["reason"] == ("multiclass spell selection requires source_class")
        selected = await call(
            server,
            "character_content_apply",
            {
                "character_id": multiclass["id"],
                "artifact_id": berserker["id"],
                "selection": {"target_class_name": "Barbarian"},
                "expected_revision": multiclass["revision"],
                "idempotency_key": "catalog-berserker",
            },
        )
        assert selected["sheet"]["progression"]["classes"][0]["subclass"] == ""
        assert selected["sheet"]["progression"]["classes"][1]["subclass"] == (
            "Path of the Berserker"
        )
        assert (
            selected["sheet"]["content"]["selections"][0]["pack_version"]
            == berserker["pack_version"]
        )

        life_domain = next(
            item
            for item in await call(
                server,
                "character_query",
                {
                    "view": "catalog",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "kind": "subclass",
                        "query": "Life Domain",
                    },
                    "principal_id": "system:local",
                },
            )
            if item["name"] == "Life Domain"
        )
        cleric_sheet = default_character_sheet()
        cleric_sheet["progression"]["classes"] = [
            {"name": "Cleric", "level": 1, "subclass": "", "hit_die": 8}
        ]
        cleric_sheet["spellcasting"]["preparation"].update(
            {"mode": "prepared", "max_prepared": 3, "changes_on": "long_rest"}
        )
        cleric = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Life Cleric",
                    "sheet": cleric_sheet,
                },
                "principal_id": "system:local",
                "idempotency_key": "catalog-life-cleric",
            },
        )
        cleric = await call(
            server,
            "character_content_apply",
            {
                "character_id": cleric["id"],
                "artifact_id": life_domain["id"],
                "selection": {"target_class_name": "Cleric"},
                "expected_revision": cleric["revision"],
                "idempotency_key": "catalog-life-domain",
            },
        )
        domain_spells = {spell["name"]: spell for spell in cleric["sheet"]["content"]["spells"]}
        assert set(domain_spells) == {"Bless", "Cure Wounds"}
        for spell in domain_spells.values():
            assert spell["grant"] == {
                "source_type": "subclass",
                "source_key": "Life Domain",
                "method": "class_prepared",
            }
            assert spell["access"]["always_prepared"] is True
            assert spell["access"]["prepared"] is True
        assert cleric["sheet"]["spellcasting"]["preparation"]["selected_spell_ids"] == []

        bonus_proficiency = next(
            item
            for item in await call(
                server,
                "character_query",
                {
                    "view": "catalog",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "kind": "feature",
                        "query": "Bonus Proficiency",
                    },
                    "principal_id": "system:local",
                },
            )
            if item["name"] == "Bonus Proficiency"
            and item["selection_requirements"]["subclass_name"] == "Life Domain"
        )
        cleric = await call(
            server,
            "character_content_apply",
            {
                "character_id": cleric["id"],
                "artifact_id": bonus_proficiency["id"],
                "expected_revision": cleric["revision"],
                "idempotency_key": "catalog-life-bonus-proficiency",
            },
        )
        assert "heavy armor" in cleric["sheet"]["traits"]["proficiencies"]["armor"]

        disciple_of_life = next(
            item
            for item in await call(
                server,
                "character_query",
                {
                    "view": "catalog",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "kind": "feature",
                        "query": "Disciple of Life",
                    },
                    "principal_id": "system:local",
                },
            )
            if item["name"] == "Disciple of Life"
        )
        cleric = await call(
            server,
            "character_content_apply",
            {
                "character_id": cleric["id"],
                "artifact_id": disciple_of_life["id"],
                "expected_revision": cleric["revision"],
                "idempotency_key": "catalog-disciple-of-life",
            },
        )
        wounded_sheet = default_character_sheet()
        wounded_sheet["combat"]["hp"] = {"value": 1, "max": 20, "temp": 0}
        wounded = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Wounded",
                    "sheet": wounded_sheet,
                },
                "principal_id": "system:local",
                "idempotency_key": "catalog-wounded",
            },
        )
        current_campaign = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        cure_wounds = domain_spells["Cure Wounds"]
        healed_facade = await call(
            server,
            "combat_hp_change",
            {
                "campaign_id": campaign["id"],
                "target_id": wounded["id"],
                "action": "heal",
                "payload": {
                    "amount": 8,
                    "source_actor_id": cleric["id"],
                    "spell_id": cure_wounds["id"],
                    "spell_level": 1,
                },
                "expected_revision": current_campaign["revision"],
                "idempotency_key": "catalog-life-heal",
            },
        )
        healed = healed_facade["result"]
        assert healed["requested_amount"] == 8
        assert healed["bonus_amount"] == 3
        assert healed["after_hp"] == 12
        assert healed["source"]["actor_id"] == cleric["id"]

        bard_sheet = default_character_sheet()
        bard_sheet["progression"].update(
            {
                "level": 3,
                "classes": [
                    {
                        "name": "Bard",
                        "level": 3,
                        "subclass": "College of Lore",
                        "hit_die": 8,
                    }
                ],
            }
        )
        for skill in ("deception", "insight", "perception", "performance"):
            bard_sheet["skills"][skill]["proficiency"] = "proficient"
        bard = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Lore Bard",
                    "sheet": bard_sheet,
                },
                "principal_id": "system:local",
                "idempotency_key": "catalog-lore-bard",
            },
        )
        bard = await call(
            server,
            "character_content_apply",
            {
                "character_id": bard["id"],
                "artifact_id": "dnd5e.content.srd2014.feature.bard-expertise",
                "selection": {"proficiencies": ["deception", "performance"]},
                "expected_revision": bard["revision"],
                "idempotency_key": "catalog-bard-expertise",
            },
        )
        assert bard["sheet"]["skills"]["deception"]["proficiency"] == "expertise"
        assert bard["sheet"]["skills"]["performance"]["proficiency"] == "expertise"
        bard = await call(
            server,
            "character_content_apply",
            {
                "character_id": bard["id"],
                "artifact_id": (
                    "dnd5e.content.srd2014.feature.college-of-lore-bonus-proficiencies"
                ),
                "selection": {"skills": ["arcana", "investigation", "persuasion"]},
                "expected_revision": bard["revision"],
                "idempotency_key": "catalog-lore-proficiencies",
            },
        )
        assert {
            skill: bard["sheet"]["skills"][skill]["proficiency"]
            for skill in ("arcana", "investigation", "persuasion")
        } == {
            "arcana": "proficient",
            "investigation": "proficient",
            "persuasion": "proficient",
        }

        backgrounds = await call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "background",
                    "query": "Acolyte",
                },
                "principal_id": "system:local",
            },
        )
        acolyte = next(item for item in backgrounds if item["name"] == "Acolyte")
        assert acolyte["selection_requirements"]["customizable"] is True
        assert acolyte["selection_requirements"]["customization_fields"] == [
            "custom_name",
            "skills",
            "languages",
            "equipment_item_ids",
        ]
        novice = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Novice"},
                "principal_id": "system:local",
                "idempotency_key": "catalog-novice",
            },
        )
        pending = await call(
            server,
            "character_content_apply",
            {
                "character_id": novice["id"],
                "artifact_id": acolyte["id"],
                "expected_revision": novice["revision"],
                "idempotency_key": "catalog-acolyte-pending",
            },
        )
        assert pending["status"] == "pending_choice"
        with pytest.raises(Exception, match="language choices must be distinct"):
            await call(
                server,
                "character_content_apply",
                {
                    "character_id": novice["id"],
                    "artifact_id": acolyte["id"],
                    "selection": {"languages": ["Elvish", "elvish"]},
                    "expected_revision": novice["revision"],
                    "idempotency_key": "catalog-acolyte-duplicate-languages",
                },
            )
        background = await call(
            server,
            "character_content_apply",
            {
                "character_id": novice["id"],
                "artifact_id": acolyte["id"],
                "selection": {"languages": ["Celestial", "Elvish"]},
                "expected_revision": novice["revision"],
                "idempotency_key": "catalog-acolyte",
            },
        )
        assert background["sheet"]["skills"]["insight"]["proficiency"] == "proficient"
        assert background["sheet"]["traits"]["languages"] == ["Celestial", "Elvish"]

        custom_sheet = default_character_sheet()
        custom_sheet["inventory"]["items"] = [
            {
                "id": "custom-vestments",
                "name": "Vestments",
                "kind": "equipment",
                "quantity": 1,
                "source_key": acolyte["id"],
                "mechanics": {},
            }
        ]
        envoy = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Envoy", "sheet": custom_sheet},
                "principal_id": "system:local",
                "idempotency_key": "catalog-envoy",
            },
        )
        custom_background = await call(
            server,
            "character_content_apply",
            {
                "character_id": envoy["id"],
                "artifact_id": acolyte["id"],
                "selection": {
                    "custom_name": "Temple Envoy",
                    "skills": ["persuasion", "history"],
                    "languages": ["Celestial", "Elvish"],
                    "equipment_item_ids": ["custom-vestments"],
                },
                "expected_revision": envoy["revision"],
                "idempotency_key": "catalog-custom-background",
            },
        )
        custom_sheet = custom_background["sheet"]
        assert custom_sheet["progression"]["background"] == "Temple Envoy"
        assert custom_sheet["progression"]["background_grants"]["feature"] == (
            "Shelter of the Faithful"
        )
        assert custom_sheet["progression"]["background_grants"]["equipment_item_ids"] == [
            "custom-vestments"
        ]
        assert (
            custom_sheet["progression"]["background_grants"]["choices"]["base_background"]
            == "Acolyte"
        )
        assert custom_sheet["progression"]["background_grants"]["choices"]["customized"] is True
        assert custom_sheet["skills"]["persuasion"]["proficiency"] == "proficient"
        assert custom_sheet["skills"]["history"]["proficiency"] == "proficient"

        feats = await call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "feat",
                    "query": "Grappler",
                },
                "principal_id": "system:local",
            },
        )
        grappler = next(item for item in feats if item["name"] == "Grappler")
        assert grappler["selection_requirements"]["prerequisites"] == [
            {"kind": "ability_minimum", "ability": "strength", "minimum": 13}
        ]
        strong_sheet = default_character_sheet()
        strong_sheet["abilities"]["strength"]["score"] = 13
        strong = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Strong", "sheet": strong_sheet},
                "principal_id": "system:local",
                "idempotency_key": "catalog-strong",
            },
        )
        feat_applied = await call(
            server,
            "character_content_apply",
            {
                "character_id": strong["id"],
                "artifact_id": grappler["id"],
                "expected_revision": strong["revision"],
                "idempotency_key": "catalog-grappler",
            },
        )
        assert feat_applied["sheet"]["content"]["feats"][0]["name"] == "Grappler"

        features = await call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "feature",
                    "query": "Sneak Attack",
                },
                "principal_id": "system:local",
            },
        )
        sneak_attack = next(item for item in features if item["name"] == "Sneak Attack")
        assert sneak_attack["selection_requirements"]["class_name"] == "Rogue"
        rogue_sheet = default_character_sheet()
        rogue_sheet["progression"]["classes"] = [
            {"name": "Rogue", "level": 1, "subclass": "", "hit_die": 8}
        ]
        rogue = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Rogue", "sheet": rogue_sheet},
                "principal_id": "system:local",
                "idempotency_key": "catalog-rogue",
            },
        )
        rogue = await call(
            server,
            "character_content_apply",
            {
                "character_id": rogue["id"],
                "artifact_id": sneak_attack["id"],
                "expected_revision": rogue["revision"],
                "idempotency_key": "catalog-sneak-attack",
            },
        )
        assert rogue["sheet"]["content"]["features"][0]["source_key"] == "Rogue"

        species = await call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "species",
                    "query": "Hill Dwarf",
                },
                "principal_id": "system:local",
            },
        )
        hill_dwarf = next(item for item in species if item["name"] == "Hill Dwarf")
        assert hill_dwarf["selection_requirements"]["tool_options"] == [
            "smith's tools",
            "brewer's supplies",
            "mason's tools",
        ]
        assert hill_dwarf["selection_requirements"]["tool_count"] == 1
        dwarf_sheet = default_character_sheet()
        dwarf_sheet["progression"]["species"] = "Dwarf"
        dwarf_sheet["combat"]["hp_progression"] = [
            {"level": 1, "method": "manual", "value": 1, "source": "level 1"}
        ]
        dwarf = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Dwarf", "sheet": dwarf_sheet},
                "principal_id": "system:local",
                "idempotency_key": "catalog-dwarf",
            },
        )
        dwarf = await call(
            server,
            "character_content_apply",
            {
                "character_id": dwarf["id"],
                "artifact_id": hill_dwarf["id"],
                "selection": {"tools": ["smith's tools"]},
                "expected_revision": dwarf["revision"],
                "idempotency_key": "catalog-hill-dwarf",
            },
        )
        assert dwarf["sheet"]["progression"]["species"] == "Hill Dwarf"
        assert dwarf["sheet"]["abilities"]["constitution"]["score"] == 12
        assert dwarf["sheet"]["abilities"]["wisdom"]["score"] == 11
        assert dwarf["sheet"]["traits"]["resistances"] == ["poison"]
        assert dwarf["sheet"]["combat"]["hp"]["max"] == 3
        assert dwarf["sheet"]["combat"]["hp_progression"] == [
            {
                "level": 1,
                "method": "manual",
                "value": 3,
                "source": "level 1",
                "adjustments": [
                    {
                        "kind": "constitution_modifier_change",
                        "amount": 1,
                        "source": "Hill Dwarf: Constitution ability score adjustment",
                        "previous_score": 10,
                        "new_score": 12,
                    },
                    {
                        "kind": "per_level_bonus",
                        "amount": 1,
                        "source": "Hill Dwarf: Dwarven Toughness",
                    },
                ],
            }
        ]
        assert any(
            item["name"] == "Dwarven Toughness" for item in dwarf["sheet"]["content"]["features"]
        )

        half_orc_catalog = await call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "species",
                    "query": "Half-Orc",
                },
                "principal_id": "system:local",
            },
        )
        half_orc_species = next(item for item in half_orc_catalog if item["name"] == "Half-Orc")
        half_orc = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Half-Orc",
                    "sheet": default_character_sheet(),
                },
                "principal_id": "system:local",
                "idempotency_key": "catalog-half-orc-character",
            },
        )
        half_orc = await call(
            server,
            "character_content_apply",
            {
                "character_id": half_orc["id"],
                "artifact_id": half_orc_species["id"],
                "expected_revision": half_orc["revision"],
                "idempotency_key": "catalog-half-orc-species",
            },
        )
        relentless = next(
            feature
            for feature in half_orc["sheet"]["content"]["features"]
            if feature["name"] == "Relentless Endurance"
        )
        assert relentless["mechanic_refs"] == ["dnd5e.core.damage.relentless_endurance"]
        assert relentless["choices"]["source_trait"]["kind"] == ("relentless_endurance")
        assert relentless["uses"]["recovers_on"] == "long_rest"
        assert all(
            "dnd5e.core.damage.relentless_endurance" not in feature["mechanic_refs"]
            for feature in half_orc["sheet"]["content"]["features"]
            if feature["name"] != "Relentless Endurance"
        )

        fire_bolt = next(
            item
            for item in await call(
                server,
                "character_query",
                {
                    "view": "catalog",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "kind": "spell",
                        "query": "Fire Bolt",
                    },
                    "principal_id": "system:local",
                },
            )
            if item["name"] == "Fire Bolt"
        )
        high_elf = next(
            item
            for item in await call(
                server,
                "character_query",
                {
                    "view": "catalog",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "kind": "species",
                        "query": "High Elf",
                    },
                    "principal_id": "system:local",
                },
            )
            if item["name"] == "High Elf"
        )
        assert high_elf["selection_requirements"]["allow_any_language"] is True
        assert high_elf["selection_requirements"]["language_count"] == 1
        elf = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Elf"},
                "principal_id": "system:local",
                "idempotency_key": "catalog-elf",
            },
        )
        elf = await call(
            server,
            "character_content_apply",
            {
                "character_id": elf["id"],
                "artifact_id": high_elf["id"],
                "selection": {
                    "languages": ["Draconic"],
                    "cantrip_artifact_id": fire_bolt["id"],
                },
                "expected_revision": elf["revision"],
                "idempotency_key": "catalog-high-elf",
            },
        )
        assert elf["sheet"]["skills"]["perception"]["proficiency"] == "proficient"
        assert elf["sheet"]["content"]["spells"][0]["grant"] == {
            "source_type": "species",
            "source_key": "High Elf",
            "method": "known",
        }

    asyncio.run(exercise())


def test_finalized_addon_activation_and_explanation(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        with pytest.raises(Exception, match="unsupported D&D core edition"):
            await call(
                server,
                "campaign_create",
                {
                    "name": "Unsupported edition",
                    "edition": "2030",
                    "idempotency_key": "unsupported-edition",
                },
            )
        assert (
            await call(
                server,
                "campaign_query",
                {"view": "list", "payload": {}, "principal_id": "system:local"},
            )
            == []
        )
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Rule packs", "idempotency_key": "campaign-rule-packs"},
        )
        profile = await call(
            server,
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "set_profile",
                "payload": {"edition": "2014"},
                "principal_id": "system:local",
                "expected_revision": campaign["revision"],
                "idempotency_key": "profile-2014",
            },
        )
        assert (
            await call(
                server,
                "campaign_rules",
                {
                    "campaign_id": campaign["id"],
                    "action": "set_profile",
                    "payload": {"edition": "2014"},
                    "principal_id": "system:local",
                    "expected_revision": campaign["revision"],
                    "idempotency_key": "profile-2014",
                },
            )
            == profile
        )
        with pytest.raises(Exception, match="campaign revision conflict"):
            await call(
                server,
                "campaign_rules",
                {
                    "campaign_id": campaign["id"],
                    "action": "set_profile",
                    "payload": {"edition": "2014", "locale": "zh-CN"},
                    "principal_id": "system:local",
                    "expected_revision": campaign["revision"],
                    "idempotency_key": "stale-profile-2014",
                },
            )
        release = await import_and_activate_addon_fixture(
            call,
            server,
            campaign["id"],
            config.home,
            manifest={
                "id": "dnd5e.xgte",
                "version": "1.0.0",
                "title": "Xanathar pilot",
                "namespace": "dnd5e.xgte",
                "system_id": "dnd5e",
                "editions": ["2014"],
                "capabilities": ["activity.after"],
                "tests": [
                    {
                        "name": "recovers pilot resource",
                        "event": "activity.after",
                        "sheet": {"resources": {"pilot": {"value": 0, "max": 1}}},
                        "expect": [{"path": "resources.pilot.value", "equals": 1}],
                    }
                ],
            },
            mechanics=[
                {
                    "id": "dnd5e.xgte.pilot.recover",
                    "event": "activity.after",
                    "operations": [
                        {
                            "op": "resource.recover",
                            "path": "resources.pilot",
                            "amount": 1,
                        }
                    ],
                }
            ],
            artifacts=[
                {
                    "id": "dnd5e.xgte.feature.pilot",
                    "kind": "feature",
                    "card": {
                        "name": "Pilot Feature",
                        "activation": {"type": "action"},
                        "uses": {"value": 1, "max": 1, "recovers_on": "long_rest"},
                    },
                    "rule_refs": ["local:xgte#pilot"],
                    "mechanic_refs": ["dnd5e.xgte.pilot.recover"],
                }
            ],
            expected_revision=profile["campaign_revision"],
            request_key="addon-xgte",
        )
        activated = release["activated"]
        assert (
            await call(
                server,
                "content_pack",
                {
                    "action": "activate",
                    "payload": {
                        "kind": "addon",
                        "campaign_id": campaign["id"],
                        "addon_id": "dnd5e.xgte",
                        "version": "1.0.0",
                    },
                    "principal_id": "system:local",
                    "expected_revision": profile["campaign_revision"],
                    "idempotency_key": "addon-xgte:activate",
                },
            )
            == activated
        )
        with pytest.raises(Exception, match="does not support campaign edition 2024"):
            await call(
                server,
                "campaign_rules",
                {
                    "campaign_id": campaign["id"],
                    "action": "set_profile",
                    "payload": {"edition": "2024"},
                    "principal_id": "system:local",
                    "expected_revision": release["campaign_revision"],
                    "idempotency_key": "reject-profile-2024",
                },
            )
        explained = await call(
            server,
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "explain",
                "payload": {"event": "activity.after"},
                "principal_id": "system:local",
            },
        )
        assert explained["fingerprint"] == activated["effective_ruleset"]["fingerprint"]
        assert explained["core_pack"]["id"] == "dnd5e.core.2014"
        assert any(item["id"] == "dnd5e.core.attack.cover" for item in explained["core_boundaries"])
        assert explained["mechanics"][0]["citations"][0]["source"].startswith(
            "rule-source:fixture.addon-xgte"
        )
        sheet = default_character_sheet()
        sheet["resources"]["pilot"] = {"value": 0, "max": 1, "recovers_on": "none"}
        character = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"name": "Pack User", "campaign_id": campaign["id"], "sheet": sheet},
                "principal_id": "system:local",
                "idempotency_key": "pack-user",
            },
        )
        updated = await call(
            server,
            "character_content_apply",
            {
                "character_id": character["id"],
                "artifact_id": "dnd5e.xgte.feature.pilot",
                "expected_revision": character["revision"],
                "idempotency_key": "add-pilot-feature",
            },
        )
        assert updated["sheet"]["content"]["features"][0]["pack_id"] == "dnd5e.xgte"
        settled = await call(
            server,
            "character_action",
            {
                "character_id": character["id"],
                "action": "use_activity",
                "payload": {"activity_id": "dnd5e.xgte.feature.pilot"},
                "principal_id": "system:local",
                "expected_revision": updated["revision"],
                "idempotency_key": "use-pilot-feature",
            },
        )
        assert settled["status"] == "committed"
        receipts = await call(
            server,
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "receipts",
                "payload": {},
                "principal_id": "system:local",
            },
        )
        assert {item["mechanic_id"] for item in receipts} >= {
            "dnd5e.core.activity.resource_accounting",
            "dnd5e.xgte.pilot.recover",
        }
        assert all(item["mutation_group_id"] for item in receipts)
        assert all(item["ruleset_fingerprint"] == explained["fingerprint"] for item in receipts)

        with pytest.raises(Exception, match="Input should be"):
            await call(
                server,
                "content_pack",
                {
                    "action": "build",
                    "payload": {"campaign_id": campaign["id"], "kind": "addon"},
                },
            )

    asyncio.run(exercise())


def test_rulebook_import_source_bound_pack_and_noncombat_settlement(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    rulebook = import_root / "xanathar-pilot.md"
    rulebook.write_text(
        "# Dungeon Master's Tools\n"
        "## Tool Proficiencies\n"
        "### Tools and Skills Together\n"
        "When both proficiencies apply, use the optional synergy procedure.\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        rule_import_roots=(import_root,),
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Imported rules", "idempotency_key": "import-campaign"},
        )
        with pytest.raises(Exception, match="outside configured import roots"):
            await call(
                server,
                "rulebook_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "start",
                    "payload": {
                        "source_path": str(outside),
                        "source_key": "outside",
                        "title": "Outside",
                        "edition": "2014",
                    },
                    "idempotency_key": "outside:stage",
                },
            )
        staged = await call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(rulebook),
                    "source_key": "xgte-user",
                    "title": "Xanathar User Import",
                    "edition": "2014",
                    "publication_id": "xgte",
                },
                "idempotency_key": "import-xgte:stage",
            },
        )
        job_id = staged["job"]["id"]
        inspection = await call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "idempotency_key": "import-xgte:inspect",
            },
        )
        assert inspection["job"]["state"] in {"extracted", "review_required"}
        imported = await call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "idempotency_key": "import-xgte:ingest",
            },
        )
        replayed = await call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "idempotency_key": "import-xgte:ingest",
            },
        )
        assert replayed == imported
        with pytest.raises(Exception, match="filters.edition must be a non-empty string"):
            await call(
                server,
                "rule_search",
                {
                    "campaign_id": campaign["id"],
                    "query": "Tools and Skills Together",
                    "filters": {"edition": ""},
                },
            )
        with pytest.raises(Exception, match="filters.publications must be a non-empty"):
            await call(
                server,
                "rule_search",
                {
                    "campaign_id": campaign["id"],
                    "query": "Tools and Skills Together",
                    "filters": {"publications": []},
                },
            )
        with pytest.raises(Exception, match="filters.page must be a positive integer"):
            await call(
                server,
                "rule_search",
                {
                    "campaign_id": campaign["id"],
                    "query": "Tools and Skills Together",
                    "filters": {"page": 0},
                },
            )
        with pytest.raises(
            Exception,
            match="publications are outside the current campaign ruleset",
        ):
            await call(
                server,
                "rule_search",
                {
                    "campaign_id": campaign["id"],
                    "query": "Tools and Skills Together",
                    "filters": {"publications": ["guessed-retail-title"]},
                },
            )
        with pytest.raises(
            Exception,
            match="source_keys are outside the current campaign ruleset",
        ):
            await call(
                server,
                "rule_search",
                {
                    "campaign_id": campaign["id"],
                    "query": "Tools and Skills Together",
                    "filters": {"source_keys": ["guessed-source-key"]},
                },
            )
        hits = await call(
            server,
            "rule_search",
            {
                "campaign_id": campaign["id"],
                "query": "Tools and Skills Together",
                "filters": {"edition": "2014"},
                "top_k": 1,
            },
        )
        chunk_id = hits[0]["id"]
        other_campaign = await call(
            server,
            "campaign_create",
            {
                "name": "Unrelated rules",
                "edition": "2014",
                "idempotency_key": "other-campaign",
            },
        )
        assert await call(
            server,
            "rule_search",
            {
                "campaign_id": other_campaign["id"],
                "query": "Tools and Skills Together",
                "filters": {"edition": "2014"},
                "top_k": 1,
            },
        ) == []
        with pytest.raises(Exception, match="outside the current campaign ruleset"):
            await call(
                server,
                "rule_expand",
                {
                    "campaign_id": other_campaign["id"],
                    "chunk_id": chunk_id,
                },
            )
        decisions = [
            {
                "id": candidate["id"],
                "review_status": "rejected",
                "reason": "This test installs only the reviewed settlement mechanic.",
            }
            for candidate in imported["job"]["candidates"]
        ]
        reviewed = imported
        if decisions:
            reviewed = await call(
                server,
                "rulebook_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "edit",
                    "payload": {
                        "job_id": job_id,
                        "operation": "candidates",
                        "decisions": decisions,
                    },
                    "idempotency_key": "xgte-review-candidates",
                },
            )
        finalized = await call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "finalize",
                "payload": {
                    "job_id": job_id,
                    "confirmation": {
                        "confirmed": True,
                        "note": "The Agent reviewed every candidate and the source-bound mechanic.",
                    },
                    "manifest": {
                        "id": "dnd5e.xgte.tool_synergy",
                        "version": "1.0.0",
                        "title": "Tool Synergy",
                        "namespace": "dnd5e.xgte.tool_synergy",
                        "system_id": "dnd5e",
                        "editions": ["2014"],
                        "capabilities": ["check.before"],
                        "tests": [
                            {
                                "name": "both proficiencies activate synergy",
                                "event": "check.before",
                                "facts": {
                                    "skill_proficiency_applies": True,
                                    "tool_proficiency_applies": True,
                                },
                                "expect": [],
                            }
                        ],
                    },
                    "mechanics": [
                        {
                            "id": "dnd5e.xgte.tool_synergy.advantage",
                            "event": "check.before",
                            "predicates": [
                                {
                                    "kind": "fact_equals",
                                    "key": "skill_proficiency_applies",
                                    "value": True,
                                },
                                {
                                    "kind": "fact_equals",
                                    "key": "tool_proficiency_applies",
                                    "value": True,
                                },
                            ],
                            "operations": [{"op": "advantage.add"}],
                            "citations": [{"chunk_id": chunk_id}],
                        }
                    ],
                },
                "expected_revision": reviewed["job"]["revision"],
                "idempotency_key": "xgte-finalize",
            },
        )
        draft = finalized["draft"]
        assert draft["status"] == "validated"
        citation = draft["mechanics"][0]["citations"][0]
        assert citation["source_id"] == imported["source_id"]
        assert citation["source_checksum"] == staged["checksum"]
        assert finalized["stored"]["status"] == "stored"
        stored = await call(
            server,
            "content_pack",
            {
                "action": "get",
                "payload": {
                    "kind": "core_rules",
                    "campaign_id": campaign["id"],
                    "pack_id": "dnd5e.xgte.tool_synergy",
                    "version": "1.0.0",
                },
            },
        )
        assert stored["status"] == "stored"
        listed = await call(
            server,
            "content_pack",
            {
                "action": "list",
                "payload": {
                    "kind": "core_rules",
                    "campaign_id": campaign["id"],
                    "pack_id": "dnd5e.xgte.tool_synergy",
                },
            },
        )
        assert listed[0]["status"] == "stored"
        profile = await call(
            server,
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "set_profile",
                "payload": {"edition": "2014"},
                "principal_id": "system:local",
                "expected_revision": campaign["revision"],
                "idempotency_key": "xgte-profile",
            },
        )
        activated = await call(
            server,
            "content_pack",
            {
                "action": "activate",
                "payload": {
                    "kind": "core_rules",
                    "campaign_id": campaign["id"],
                    "pack_id": "dnd5e.xgte.tool_synergy",
                    "version": "1.0.0",
                },
                "principal_id": "system:local",
                "expected_revision": profile["campaign_revision"],
                "idempotency_key": "xgte-activate",
            },
        )
        character = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Artificer",
                    "sheet": default_character_sheet(),
                },
                "principal_id": "system:local",
                "idempotency_key": "xgte-character",
            },
        )
        current = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        await call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": current["revision"],
                "idempotency_key": "xgte-enter-play",
            },
        )
        current = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        settled = await call(
            server,
            "character_check",
            {
                "campaign_id": campaign["id"],
                "action": "check",
                "payload": {
                    "actor_id": character["id"],
                    "kind": "check",
                    "ability": "intelligence",
                    "dc": 12,
                    "rule_facts": {
                        "skill_proficiency_applies": True,
                        "tool_proficiency_applies": True,
                    },
                },
                "expected_revision": current["revision"],
                "idempotency_key": "xgte-tool-check",
            },
        )
        assert len(settled["rolls"]) == 2
        receipts = await call(
            server,
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "receipts",
                "payload": {},
                "principal_id": "system:local",
            },
        )
        extension = next(
            item for item in receipts if item["mechanic_id"] == "dnd5e.xgte.tool_synergy.advantage"
        )
        assert extension["receipt"]["citations"][0]["chunk_id"] == chunk_id
        assert activated["effective"]["lock"][0]["pack_id"] == "dnd5e.xgte.tool_synergy"

    asyncio.run(exercise())


def test_legacy_campaign_without_core_lock_fails_closed(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Legacy core lock", "idempotency_key": "legacy-core-lock"},
        )
        database = Database(sqlite_database_url(config.database_path))
        try:
            RuleProfileService(database).set(campaign["id"], edition="2014", options={})
        finally:
            database.dispose()
        with pytest.raises(Exception, match="no locked built-in core rule pack"):
            await call(
                server,
                "campaign_rules",
                {
                    "campaign_id": campaign["id"],
                    "action": "explain",
                    "payload": {},
                    "principal_id": "system:local",
                },
            )
        diagnostic = await call(
            server,
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "get_profile",
                "payload": {},
                "principal_id": "system:local",
            },
        )
        assert diagnostic["effective"] is None
        assert "no locked built-in core rule pack" in diagnostic["effective_error"]

    asyncio.run(exercise())


def test_checkpointed_core_relock_preserves_profile_and_adopts_current_runtime(
    tmp_path: Path,
) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Checkpointed relock", "idempotency_key": "relock-campaign"},
        )
        database = Database(sqlite_database_url(config.database_path))
        try:
            RuleProfileService(database).set(
                campaign["id"],
                edition="2014",
                locale="zh-CN",
                publications=["srd-5.1"],
                options={
                    "house_option": "preserved",
                    "_core_rule_pack_lock": {
                        "id": "dnd5e.core.2014",
                        "version": "0.9.0",
                        "fingerprint": "old-core-fingerprint",
                    },
                },
            )
        finally:
            database.dispose()
        changed = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        snapshot = await call(
            server,
            "snapshot_create",
            {
                "campaign_id": campaign["id"],
                "label": "Before Core relock",
                "expected_revision": changed["revision"],
                "expected_head_snapshot_id": "",
                "idempotency_key": "before-relock",
            },
        )
        verification = await call(
            server,
            "snapshot_query",
            {
                "campaign_id": campaign["id"],
                "view": "verify",
                "payload": {"slot": snapshot["slot"]},
            },
        )
        assert verification == {
            "valid": True,
            "captured_campaign_revision": changed["revision"],
        }
        branch = next(
            item
            for item in await call(
                server,
                "branch_query",
                {
                    "campaign_id": campaign["id"],
                    "view": "list",
                    "payload": {},
                    "principal_id": "system:local",
                },
            )
            if item["is_current"]
        )
        relocked = await call(
            server,
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "core_relock",
                "payload": {
                    "expected_core_fingerprint": "old-core-fingerprint",
                    "reason": "Reviewed runtime upgrade during a checkpointed encounter.",
                    "expected_head_snapshot_id": snapshot["id"],
                },
                "branch_id": branch["id"],
                "expected_revision": changed["revision"],
                "idempotency_key": "adopt-current-core",
            },
        )
        assert relocked["previous_core_pack"]["version"] == "0.9.0"
        assert relocked["core_pack"]["id"] == "dnd5e.core.2014"
        assert relocked["core_pack"]["version"] != "0.9.0"
        assert relocked["core_pack"]["fingerprint"] != "old-core-fingerprint"
        assert relocked["profile"]["locale"] == "zh-CN"
        assert list(relocked["profile"]["publications"]) == ["srd-5.1"]
        assert relocked["profile"]["options"]["house_option"] == "preserved"
        assert relocked["checkpoint_snapshot_id"] == snapshot["id"]
        replayed = await call(
            server,
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "core_relock",
                "payload": {
                    "expected_core_fingerprint": "old-core-fingerprint",
                    "reason": "Reviewed runtime upgrade during a checkpointed encounter.",
                    "expected_head_snapshot_id": snapshot["id"],
                },
                "branch_id": branch["id"],
                "expected_revision": changed["revision"],
                "idempotency_key": "adopt-current-core",
            },
        )
        assert replayed == relocked

        lock_view = await call(
            server,
            "snapshot_query",
            {
                "campaign_id": campaign["id"],
                "view": "core",
                "payload": {"slot": snapshot["slot"]},
            },
        )
        assert lock_view["core_pack"]["fingerprint"] == "old-core-fingerprint"
        assert (
            lock_view["available_core_pack"]["fingerprint"] == relocked["core_pack"]["fingerprint"]
        )
        assert lock_view["conversion_required"] is True
        conversion_arguments = {
            "campaign_id": campaign["id"],
            "action": "create_core_upgrade",
            "payload": {
                "slot": snapshot["slot"],
                "name": "converted-old-core",
                "expected_snapshot_core_fingerprint": "old-core-fingerprint",
                "expected_runtime_core_fingerprint": relocked["core_pack"]["fingerprint"],
                "reason": "Explicitly convert the old checkpoint to the reviewed runtime Core.",
            },
            "expected_revision": relocked["campaign_revision"],
            "expected_branch_id": branch["id"],
            "idempotency_key": "convert-old-core-snapshot",
        }
        converted = await call(server, "branch_change", conversion_arguments)
        converted_replay = await call(server, "branch_change", conversion_arguments)
        assert converted_replay == converted
        assert converted["status"] == "converted"
        assert converted["branch"]["name"] == "converted-old-core"
        assert converted["snapshot"]["parent_id"] == snapshot["id"]
        assert converted["previous_core_pack"]["fingerprint"] == "old-core-fingerprint"
        converted_profile = await call(
            server,
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "get_profile",
                "payload": {},
                "principal_id": "system:local",
            },
        )
        assert converted_profile["profile"]["locale"] == "zh-CN"
        assert converted_profile["profile"]["options"]["house_option"] == "preserved"
        assert (
            converted_profile["effective"]["core_pack"]["fingerprint"]
            == relocked["core_pack"]["fingerprint"]
        )

    asyncio.run(exercise())


def test_combat_lifecycle_declares_and_releases_core_mutation_locks(
    tmp_path: Path,
) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Combat Core relock", "idempotency_key": "combat-relock-campaign"},
        )
        actors = [
            await call(
                server,
                "character_create_from",
                {
                    "mode": "direct",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "name": name,
                        "sheet": default_character_sheet(),
                    },
                    "principal_id": "system:local",
                    "idempotency_key": f"combat-relock-{name}",
                },
            )
            for name in ("Hero", "Hostile")
        ]
        campaign = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        play = await call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": campaign["revision"],
                "idempotency_key": "combat-relock-play",
            },
        )
        started = await call(
            server,
            "combat_start",
            {
                "positioning_mode": "grid",
                "battle_map": {"width_cells": 12, "height_cells": 12},
                "campaign_id": campaign["id"],
                "participant_ids": [item["id"] for item in actors],
                "participant_config": [
                    {
                        "actor_id": actors[0]["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                        "disposition": "friendly",
                    },
                    {
                        "actor_id": actors[1]["id"],
                        "initiative": 10,
                        "position": {"x": 1, "y": 0},
                        "disposition": "hostile",
                    },
                ],
                "expected_revision": play["campaign_revision"],
                "idempotency_key": "combat-relock-start",
            },
        )
        database = Database(sqlite_database_url(config.database_path))
        try:
            profiles = RuleProfileService(database)
            profile = profiles.get(campaign["id"])
            assert profile is not None
            with pytest.raises(ValueError, match="rule profile cannot change while locked"):
                profiles.set(
                    campaign["id"],
                    edition=profile.edition,
                    locale=profile.locale,
                    publications=list(profile.publications),
                    options=dict(profile.options),
                    expected_campaign_revision=started["campaign_revision"],
                )
        finally:
            database.dispose()
        changed = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        assert changed["state"]["mutation_locks"] == [
            {
                "id": "dnd5e:combat",
                "domains": [
                    "rule_profile",
                    "rule_pack_activation",
                    "addon_activation",
                ],
                "reason": "active D&D combat",
            }
        ]
        status = await call(
            server,
            "combat_query",
            {"campaign_id": campaign["id"], "view": "status"},
        )
        assert status["active"] is True
        ended = await call(
            server,
            "combat_end",
            {
                "campaign_id": campaign["id"],
                "expected_revision": changed["revision"],
                "idempotency_key": "combat-relock-end",
            },
        )
        after_end = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        assert "mutation_locks" not in after_end["state"]
        database = Database(sqlite_database_url(config.database_path))
        try:
            profiles = RuleProfileService(database)
            profile = profiles.get(campaign["id"])
            assert profile is not None
            updated = profiles.set(
                campaign["id"],
                edition=profile.edition,
                locale=profile.locale,
                publications=list(profile.publications),
                options=dict(profile.options),
                expected_campaign_revision=ended["campaign_revision"],
            )
            assert updated.options == profile.options
        finally:
            database.dispose()

    asyncio.run(exercise())


def test_current_core_relock_is_revision_and_snapshot_noop(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Current core", "idempotency_key": "current-core-campaign"},
        )
        profile = await call(
            server,
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "get_profile",
                "payload": {},
                "principal_id": "system:local",
            },
        )
        assert (
            profile["available_core_pack"]["fingerprint"]
            == (profile["profile"]["options"]["_core_rule_pack_lock"]["fingerprint"])
        )
        snapshot = await call(
            server,
            "snapshot_create",
            {
                "campaign_id": campaign["id"],
                "label": "Existing checkpoint",
                "expected_revision": campaign["revision"],
                "expected_head_snapshot_id": "",
                "idempotency_key": "current-core-checkpoint",
            },
        )
        current = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        branch = next(
            item
            for item in await call(
                server,
                "branch_query",
                {
                    "campaign_id": campaign["id"],
                    "view": "list",
                    "payload": {},
                    "principal_id": "system:local",
                },
            )
            if item["is_current"]
        )
        snapshots_before = await call(
            server,
            "snapshot_query",
            {
                "campaign_id": campaign["id"],
                "view": "list",
                "payload": {},
                "principal_id": "system:local",
            },
        )
        arguments = {
            "campaign_id": campaign["id"],
            "action": "core_relock",
            "payload": {
                "expected_core_fingerprint": profile["available_core_pack"]["fingerprint"],
                "reason": "Prove an already-current Core is a strict no-op.",
                "expected_head_snapshot_id": snapshot["id"],
            },
            "branch_id": branch["id"],
            "expected_revision": current["revision"],
            "idempotency_key": "noop-core-relock",
        }

        result = await call(server, "campaign_rules", arguments)
        replay = await call(server, "campaign_rules", arguments)
        after = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        snapshots_after = await call(
            server,
            "snapshot_query",
            {
                "campaign_id": campaign["id"],
                "view": "list",
                "payload": {},
                "principal_id": "system:local",
            },
        )

        assert replay == result
        assert result["status"] == "current"
        assert result["mutation_applied"] is False
        assert result["campaign_revision"] == current["revision"] == after["revision"]
        assert snapshots_after == snapshots_before

    asyncio.run(exercise())


def test_snapshot_and_branch_checkout_reject_unavailable_core_lock(
    tmp_path: Path,
) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Legacy snapshot", "idempotency_key": "legacy-snapshot"},
        )
        database = Database(sqlite_database_url(config.database_path))
        try:
            RuleProfileService(database).set(campaign["id"], edition="2024", options={})
        finally:
            database.dispose()

        current = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        branch = (
            await call(
                server,
                "branch_query",
                {
                    "campaign_id": campaign["id"],
                    "view": "list",
                    "payload": {},
                    "principal_id": "system:local",
                },
            )
        )[0]
        legacy_snapshot = await call(
            server,
            "snapshot_create",
            {
                "campaign_id": campaign["id"],
                "label": "missing core lock",
                "expected_revision": current["revision"],
                "expected_head_snapshot_id": "",
                "idempotency_key": "legacy-snapshot-create",
            },
        )
        repaired = await call(
            server,
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "set_profile",
                "payload": {"edition": "2024"},
                "principal_id": "system:local",
                "expected_revision": current["revision"],
                "idempotency_key": "repair-core-lock",
            },
        )
        with pytest.raises(Exception, match="cannot be restored without explicit conversion"):
            await call(
                server,
                "branch_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "create",
                    "payload": {
                        "name": "legacy-direct-checkout",
                        "from_snapshot_id": legacy_snapshot["id"],
                        "checkout": True,
                    },
                    "principal_id": "system:local",
                    "expected_revision": repaired["campaign_revision"],
                    "expected_branch_id": branch["id"],
                    "idempotency_key": "reject-legacy-create-checkout",
                },
            )
        assert (
            len(
                await call(
                    server,
                    "branch_query",
                    {
                        "campaign_id": campaign["id"],
                        "view": "list",
                        "payload": {},
                        "principal_id": "system:local",
                    },
                )
            )
            == 1
        )
        legacy_branch_args = {
            "campaign_id": campaign["id"],
            "action": "create",
            "payload": {"name": "legacy-core", "from_snapshot_id": legacy_snapshot["id"]},
            "principal_id": "system:local",
            "expected_revision": repaired["campaign_revision"],
            "expected_branch_id": branch["id"],
            "idempotency_key": "legacy-core-branch",
        }
        legacy_branch = await call(server, "branch_change", legacy_branch_args)
        assert await call(server, "branch_change", legacy_branch_args) == legacy_branch
        assert legacy_branch["campaign_revision"] == repaired["campaign_revision"] + 1

        with pytest.raises(Exception, match="cannot be restored without explicit conversion"):
            await call(
                server,
                "snapshot_restore",
                {
                    "campaign_id": campaign["id"],
                    "slot": legacy_snapshot["slot"],
                    "expected_revision": legacy_branch["campaign_revision"],
                    "expected_branch_id": branch["id"],
                    "idempotency_key": "reject-legacy-restore",
                },
            )
        with pytest.raises(Exception, match="cannot be restored without explicit conversion"):
            await call(
                server,
                "branch_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "checkout",
                    "payload": {"branch_id": legacy_branch["id"]},
                    "principal_id": "system:local",
                    "expected_revision": legacy_branch["campaign_revision"],
                    "expected_branch_id": branch["id"],
                    "idempotency_key": "reject-legacy-checkout",
                },
            )
        after = await call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        current_branch = await call(
            server,
            "branch_query",
            {
                "campaign_id": campaign["id"],
                "view": "list",
                "payload": {},
                "principal_id": "system:local",
            },
        )
        assert after["revision"] == legacy_branch["campaign_revision"]
        assert (
            next(item for item in current_branch if item["id"] == branch["id"])["is_current"]
            is True
        )

    asyncio.run(exercise())
