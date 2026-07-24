import asyncio
from pathlib import Path

import pytest
from sagasmith_core import Database, RuleProfileService
from sagasmith_core.database import sqlite_database_url
from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


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
            "campaign_rule_profile_set",
            {
                "campaign_id": campaign["id"],
                "edition": "2014",
                "expected_revision": campaign["revision"],
                "idempotency_key": "catalog-profile",
            },
        )
        spells = await call(
            server,
            "content_catalog_list",
            {"campaign_id": campaign["id"], "kind": "spell", "query": "Fireball"},
        )
        fireball = next(item for item in spells if item["name"] == "Fireball")
        assert fireball["pack_id"] == "dnd5e.content.srd2014"
        assert fireball["rule_refs"]
        assert fireball["selection_requirements"]["eligible_classes"] == [
            "sorcerer",
            "wizard",
        ]
        assert fireball["selection_requirements"]["level"] == 3
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
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Aria",
                "sheet": sheet,
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
            "content_catalog_list",
            {"campaign_id": campaign["id"], "kind": "subclass", "query": "Berserker"},
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
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Multiclass",
                "sheet": multiclass_sheet,
                "idempotency_key": "catalog-multiclass",
            },
        )
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
        assert selected["sheet"]["content"]["selections"][0]["pack_version"] == berserker[
            "pack_version"
        ]

        life_domain = next(
            item
            for item in await call(
                server,
                "content_catalog_list",
                {"campaign_id": campaign["id"], "kind": "subclass", "query": "Life Domain"},
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
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Life Cleric",
                "sheet": cleric_sheet,
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
        domain_spells = {
            spell["name"]: spell
            for spell in cleric["sheet"]["content"]["spells"]
        }
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
                "content_catalog_list",
                {
                    "campaign_id": campaign["id"],
                    "kind": "feature",
                    "query": "Bonus Proficiency",
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
                "content_catalog_list",
                {
                    "campaign_id": campaign["id"],
                    "kind": "feature",
                    "query": "Disciple of Life",
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
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Wounded",
                "sheet": wounded_sheet,
                "idempotency_key": "catalog-wounded",
            },
        )
        current_campaign = await call(
            server, "campaign_get", {"campaign_id": campaign["id"]}
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

        backgrounds = await call(
            server,
            "content_catalog_list",
            {"campaign_id": campaign["id"], "kind": "background", "query": "Acolyte"},
        )
        acolyte = next(item for item in backgrounds if item["name"] == "Acolyte")
        novice = await call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Novice",
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
        assert pending["status"] == "pending_ruling"
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

        feats = await call(
            server,
            "content_catalog_list",
            {"campaign_id": campaign["id"], "kind": "feat", "query": "Grappler"},
        )
        grappler = next(item for item in feats if item["name"] == "Grappler")
        assert grappler["selection_requirements"]["prerequisites"] == [
            {"kind": "ability_minimum", "ability": "strength", "minimum": 13}
        ]
        strong_sheet = default_character_sheet()
        strong_sheet["abilities"]["strength"]["score"] = 13
        strong = await call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Strong",
                "sheet": strong_sheet,
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
            "content_catalog_list",
            {"campaign_id": campaign["id"], "kind": "feature", "query": "Sneak Attack"},
        )
        sneak_attack = next(item for item in features if item["name"] == "Sneak Attack")
        assert sneak_attack["selection_requirements"]["class_name"] == "Rogue"
        rogue_sheet = default_character_sheet()
        rogue_sheet["progression"]["classes"] = [
            {"name": "Rogue", "level": 1, "subclass": "", "hit_die": 8}
        ]
        rogue = await call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Rogue",
                "sheet": rogue_sheet,
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
            "content_catalog_list",
            {"campaign_id": campaign["id"], "kind": "species", "query": "Hill Dwarf"},
        )
        hill_dwarf = next(item for item in species if item["name"] == "Hill Dwarf")
        assert hill_dwarf["selection_requirements"]["tool_options"] == [
            "smith's tools",
            "brewer's supplies",
            "mason's tools",
        ]
        dwarf_sheet = default_character_sheet()
        dwarf_sheet["progression"]["species"] = "Dwarf"
        dwarf_sheet["combat"]["hp_progression"] = [
            {"level": 1, "method": "manual", "value": 1, "source": "level 1"}
        ]
        dwarf = await call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Dwarf",
                "sheet": dwarf_sheet,
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
                "source": (
                    "level 1; Hill Dwarf: Constitution ability score increase; "
                    "Hill Dwarf: Dwarven Toughness"
                ),
            }
        ]
        assert any(
            item["name"] == "Dwarven Toughness"
            for item in dwarf["sheet"]["content"]["features"]
        )

        fire_bolt = next(
            item
            for item in await call(
                server,
                "content_catalog_list",
                {"campaign_id": campaign["id"], "kind": "spell", "query": "Fire Bolt"},
            )
            if item["name"] == "Fire Bolt"
        )
        high_elf = next(
            item
            for item in await call(
                server,
                "content_catalog_list",
                {"campaign_id": campaign["id"], "kind": "species", "query": "High Elf"},
            )
            if item["name"] == "High Elf"
        )
        elf = await call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Elf",
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


def test_rule_pack_authoring_activation_and_explanation(tmp_path: Path) -> None:
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
        assert await call(server, "campaign_list", {}) == []
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Rule packs", "idempotency_key": "campaign-rule-packs"},
        )
        profile = await call(
            server,
            "campaign_rule_profile_set",
            {
                "campaign_id": campaign["id"],
                "edition": "2014",
                "expected_revision": campaign["revision"],
                "idempotency_key": "profile-2014",
            },
        )
        assert (
            await call(
                server,
                "campaign_rule_profile_set",
                {
                    "campaign_id": campaign["id"],
                    "edition": "2014",
                    "expected_revision": campaign["revision"],
                    "idempotency_key": "profile-2014",
                },
            )
            == profile
        )
        with pytest.raises(Exception, match="campaign revision conflict"):
            await call(
                server,
                "campaign_rule_profile_set",
                {
                    "campaign_id": campaign["id"],
                    "edition": "2014",
                    "locale": "zh-CN",
                    "expected_revision": campaign["revision"],
                    "idempotency_key": "stale-profile-2014",
                },
            )
        draft = await call(
            server,
            "rule_pack_draft",
            {
                "manifest": {
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
                "mechanics": [
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
                        "citations": [{"source": "local:xgte", "section": "Pilot"}],
                    }
                ],
                "artifacts": [
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
                "provenance": {"source": "local-private-book"},
            },
        )
        assert draft["status"] == "validated"
        test_report = await call(
            server, "rule_pack_test", {"pack_id": "dnd5e.xgte", "version": "1.0.0"}
        )
        assert test_report["passed"] is True
        await call(server, "rule_pack_install", {"pack_id": "dnd5e.xgte", "version": "1.0.0"})
        activated = await call(
            server,
            "campaign_rule_pack_set",
            {
                "campaign_id": campaign["id"],
                "pack_id": "dnd5e.xgte",
                "version": "1.0.0",
                "expected_revision": profile["campaign_revision"],
                "idempotency_key": "activate-xgte",
            },
        )
        assert (
            await call(
                server,
                "campaign_rule_pack_set",
                {
                    "campaign_id": campaign["id"],
                    "pack_id": "dnd5e.xgte",
                    "version": "1.0.0",
                    "expected_revision": profile["campaign_revision"],
                    "idempotency_key": "activate-xgte",
                },
            )
            == activated
        )
        with pytest.raises(Exception, match="does not support campaign edition 2024"):
            await call(
                server,
                "campaign_rule_profile_set",
                {
                    "campaign_id": campaign["id"],
                    "edition": "2024",
                    "expected_revision": activated["campaign_revision"],
                    "idempotency_key": "reject-profile-2024",
                },
            )
        explained = await call(
            server,
            "campaign_rules_explain",
            {"campaign_id": campaign["id"], "event": "activity.after"},
        )
        assert explained["fingerprint"] == activated["effective"]["fingerprint"]
        assert explained["core_pack"]["id"] == "dnd5e.core.2014"
        assert any(item["id"] == "dnd5e.core.attack.cover" for item in explained["core_boundaries"])
        assert explained["mechanics"][0]["citations"][0]["source"] == "local:xgte"
        sheet = default_character_sheet()
        sheet["resources"]["pilot"] = {"value": 0, "max": 1, "recovers_on": "none"}
        character = await call(
            server,
            "character_create",
            {
                "name": "Pack User",
                "campaign_id": campaign["id"],
                "sheet": sheet,
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
            "character_use_activity",
            {
                "character_id": character["id"],
                "activity_id": "dnd5e.xgte.feature.pilot",
                "expected_revision": updated["revision"],
                "idempotency_key": "use-pilot-feature",
            },
        )
        assert settled["status"] == "committed"
        receipts = await call(
            server,
            "campaign_rule_receipts",
            {"campaign_id": campaign["id"]},
        )
        assert {item["mechanic_id"] for item in receipts} >= {
            "dnd5e.core.activity.resource_accounting",
            "dnd5e.xgte.pilot.recover",
        }
        assert all(item["mutation_group_id"] for item in receipts)
        assert all(item["ruleset_fingerprint"] == explained["fingerprint"] for item in receipts)

        rejected = await call(
            server,
            "rule_pack_draft",
            {
                "manifest": {
                    "id": "dnd5e.unsafe",
                    "version": "1.0.0",
                    "system_id": "dnd5e",
                    "editions": ["2014"],
                },
                "mechanics": [
                    {
                        "id": "dnd5e.unsafe.eval",
                        "event": "rest.after",
                        "operations": [{"op": "python.eval"}],
                        "citations": [{"source": "local:test", "section": "Unsafe"}],
                    }
                ],
            },
        )
        assert rejected["status"] == "rejected"

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
                "rule_document_stage",
                {"campaign_id": campaign["id"], "source_path": str(outside)},
            )
        staged = await call(
            server,
            "rule_document_stage",
            {"campaign_id": campaign["id"], "source_path": str(rulebook)},
        )
        inspection = await call(
            server,
            "rule_document_inspect",
            {"campaign_id": campaign["id"], "artifact": staged["artifact"]},
        )
        assert inspection["sections"] == 3
        imported = await call(
            server,
            "rule_document_import",
            {
                "campaign_id": campaign["id"],
                "artifact": staged["artifact"],
                "source_key": "xgte-user",
                "title": "Xanathar User Import",
                "edition": "2014",
                "publication_id": "xgte",
                "idempotency_key": "import-xgte",
            },
        )
        replayed = await call(
            server,
            "rule_document_import",
            {
                "campaign_id": campaign["id"],
                "artifact": staged["artifact"],
                "source_key": "xgte-user",
                "title": "Xanathar User Import",
                "edition": "2014",
                "publication_id": "xgte",
                "idempotency_key": "import-xgte",
            },
        )
        assert replayed == imported
        hits = await call(
            server,
            "rule_search",
            {"query": "Tools and Skills Together", "edition": "2014", "top_k": 1},
        )
        chunk_id = hits[0]["id"]
        with pytest.raises(Exception, match="source edition"):
            await call(
                server,
                "rule_pack_draft_from_source",
                {
                    "source_id": imported["source_id"],
                    "manifest": {
                        "id": "dnd5e.xgte.wrong-edition",
                        "version": "1.0.0",
                        "namespace": "dnd5e.xgte.wrong-edition",
                        "system_id": "dnd5e",
                        "editions": ["2024"],
                    },
                },
            )
        draft = await call(
            server,
            "rule_pack_draft_from_source",
            {
                "source_id": imported["source_id"],
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
        )
        assert draft["status"] == "validated"
        citation = draft["mechanics"][0]["citations"][0]
        assert citation["source_id"] == imported["source_id"]
        assert citation["source_checksum"] == staged["checksum"]
        with pytest.raises(Exception):
            await call(
                server,
                "rule_pack_draft_from_source",
                {
                    "source_id": "not-the-source",
                    "manifest": {},
                    "mechanics": [],
                },
            )
        await call(
            server,
            "rule_pack_install",
            {"pack_id": "dnd5e.xgte.tool_synergy", "version": "1.0.0"},
        )
        profile = await call(
            server,
            "campaign_rule_profile_set",
            {
                "campaign_id": campaign["id"],
                "edition": "2014",
                "expected_revision": campaign["revision"],
                "idempotency_key": "xgte-profile",
            },
        )
        activated = await call(
            server,
            "campaign_rule_pack_set",
            {
                "campaign_id": campaign["id"],
                "pack_id": "dnd5e.xgte.tool_synergy",
                "version": "1.0.0",
                "expected_revision": profile["campaign_revision"],
                "idempotency_key": "xgte-activate",
            },
        )
        character = await call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Artificer",
                "sheet": default_character_sheet(),
                "idempotency_key": "xgte-character",
            },
        )
        current = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
        settled = await call(
            server,
            "character_check",
            {
                "campaign_id": campaign["id"],
                "actor_id": character["id"],
                "kind": "check",
                "ability": "intelligence",
                "dc": 12,
                "rule_facts": {
                    "skill_proficiency_applies": True,
                    "tool_proficiency_applies": True,
                },
                "expected_revision": current["revision"],
                "idempotency_key": "xgte-tool-check",
            },
        )
        assert len(settled["rolls"]) == 2
        receipts = await call(
            server,
            "campaign_rule_receipts",
            {"campaign_id": campaign["id"]},
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
                "campaign_rules_explain",
                {"campaign_id": campaign["id"]},
            )
        diagnostic = await call(
            server,
            "campaign_rule_profile_get",
            {"campaign_id": campaign["id"]},
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
        changed = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
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
        branch = next(
            item
            for item in await call(
                server, "branch_list", {"campaign_id": campaign["id"]}
            )
            if item["is_current"]
        )
        relocked = await call(
            server,
            "campaign_core_relock",
            {
                "campaign_id": campaign["id"],
                "expected_core_fingerprint": "old-core-fingerprint",
                "reason": "Reviewed runtime upgrade during a checkpointed encounter.",
                "branch_id": branch["id"],
                "expected_revision": changed["revision"],
                "expected_head_snapshot_id": snapshot["id"],
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
            "campaign_core_relock",
            {
                "campaign_id": campaign["id"],
                "expected_core_fingerprint": "old-core-fingerprint",
                "reason": "Reviewed runtime upgrade during a checkpointed encounter.",
                "branch_id": branch["id"],
                "expected_revision": changed["revision"],
                "expected_head_snapshot_id": snapshot["id"],
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
        assert lock_view["available_core_pack"]["fingerprint"] == relocked["core_pack"][
            "fingerprint"
        ]
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
            "campaign_rule_profile_get",
            {"campaign_id": campaign["id"]},
        )
        assert converted_profile["profile"]["locale"] == "zh-CN"
        assert converted_profile["profile"]["options"]["house_option"] == "preserved"
        assert converted_profile["effective"]["core_pack"]["fingerprint"] == relocked["core_pack"][
            "fingerprint"
        ]

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

        current = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
        branch = (await call(server, "branch_list", {"campaign_id": campaign["id"]}))[0]
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
            "campaign_rule_profile_set",
            {
                "campaign_id": campaign["id"],
                "edition": "2024",
                "expected_revision": current["revision"],
                "idempotency_key": "repair-core-lock",
            },
        )
        with pytest.raises(Exception, match="cannot be restored without explicit conversion"):
            await call(
                server,
                "branch_create",
                {
                    "campaign_id": campaign["id"],
                    "name": "legacy-direct-checkout",
                    "from_snapshot_id": legacy_snapshot["id"],
                    "checkout": True,
                    "expected_revision": repaired["campaign_revision"],
                    "expected_branch_id": branch["id"],
                    "idempotency_key": "reject-legacy-create-checkout",
                },
            )
        assert len(await call(server, "branch_list", {"campaign_id": campaign["id"]})) == 1
        legacy_branch = await call(
            server,
            "branch_create",
            {
                "campaign_id": campaign["id"],
                "name": "legacy-core",
                "from_snapshot_id": legacy_snapshot["id"],
                "expected_revision": repaired["campaign_revision"],
                "expected_branch_id": branch["id"],
                "idempotency_key": "legacy-core-branch",
            },
        )

        with pytest.raises(Exception, match="cannot be restored without explicit conversion"):
            await call(
                server,
                "snapshot_restore",
                {
                    "campaign_id": campaign["id"],
                    "slot": legacy_snapshot["slot"],
                    "expected_revision": repaired["campaign_revision"],
                    "expected_branch_id": branch["id"],
                    "idempotency_key": "reject-legacy-restore",
                },
            )
        with pytest.raises(Exception, match="cannot be restored without explicit conversion"):
            await call(
                server,
                "branch_checkout",
                {
                    "campaign_id": campaign["id"],
                    "branch_id": legacy_branch["id"],
                    "expected_revision": repaired["campaign_revision"],
                    "expected_branch_id": branch["id"],
                    "idempotency_key": "reject-legacy-checkout",
                },
            )
        after = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
        current_branch = await call(server, "branch_list", {"campaign_id": campaign["id"]})
        assert after["revision"] == repaired["campaign_revision"]
        assert (
            next(item for item in current_branch if item["id"] == branch["id"])["is_current"]
            is True
        )

    asyncio.run(exercise())
