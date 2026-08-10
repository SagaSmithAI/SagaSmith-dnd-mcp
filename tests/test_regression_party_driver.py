from __future__ import annotations

import asyncio

import pytest
from sagasmith_dnd.character_schema import default_character_sheet
from sagasmith_dnd.engine import ability_modifier

from scripts.regression_party import (
    OIL_RULE,
    _apply_artifact,
    _background_starting_items,
    _catalog_source,
    _class_starting_supplements,
    _configure_base_sheet,
    _initialize_prepared_spells,
    _item_weight_oz,
    _pack_contents,
    _spellcasting_audit,
    _switch_phase,
    audit_profiles,
    lost_mine_party_profiles,
    select_profiles,
    storm_kings_party_profiles,
    tomb_of_annihilation_party_profiles,
    tyranny_party_profiles,
    waterdeep_party_profiles,
)
from scripts.regression_rulings import RegressionRulingRequiredError


def test_lost_mine_party_uses_source_maximum_and_diverse_core_models() -> None:
    audit = audit_profiles(lost_mine_party_profiles())

    assert audit["selected_size"] == audit["source_maximum"] == 5
    assert audit["classes_unique"] is True
    assert audit["species_unique"] is True
    assert audit["ability_methods"] == ["manual", "point_buy", "standard_array"]
    assert audit["spell_resource_models"] == ["known", "prepared", "spellbook"]
    assert audit["pregenerated_first"]["official_sheets_present_in_corpus"] is False
    assert "excluded" in audit["pregenerated_first"]["associated_pc_smalls_disposition"]


def test_party_catalog_ruling_returns_structured_control_to_agent() -> None:
    class Client:
        async def domain(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "character_content_apply"
            return {
                "status": "pending_ruling",
                "default_resolver": "agent",
                "ruling_kind": "source_or_scene_fact",
                "reason": "confirm the current scene prerequisite",
                "committed": False,
            }

    with pytest.raises(RegressionRulingRequiredError) as raised:
        asyncio.run(
            _apply_artifact(
                Client(),
                actor={"id": "actor-1", "revision": 3},
                artifact={"id": "feat-1", "name": "Scene Feat"},
                selection={},
                key="apply-feat-1",
            )
        )

    requirement = raised.value.requirement
    assert requirement["operation"] == "character_content_apply.party"
    assert requirement["context"] == {
        "actor_id": "actor-1",
        "artifact_id": "feat-1",
        "artifact_name": "Scene Feat",
    }
    assert requirement["ruling"]["default_resolver"] == "agent"


def test_resumed_replacement_keeps_exact_committed_prepared_spell_setup() -> None:
    class Client:
        async def domain(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "character_query"
            assert arguments == {
                "view": "get",
                "payload": {"character_id": actor["id"]},
            }
            return actor

    actor = {
        "id": "replacement-cleric",
        "revision": 12,
        "sheet": {
            "spellcasting": {
                "preparation": {
                    "selected_spell_ids": ["bless", "cure-wounds"],
                }
            }
        },
    }

    resumed = asyncio.run(
        _initialize_prepared_spells(
            Client(),
            actor=actor,
            prepared_ids=["bless", "cure-wounds"],
            idempotency_key="replacement-cleric-setup",
        )
    )

    assert resumed == actor


def test_resumed_replacement_rejects_different_prepared_spell_setup() -> None:
    class Client:
        async def domain(self, tool_id: str, arguments: dict) -> dict:
            assert tool_id == "character_query"
            return actor

    actor = {
        "id": "replacement-cleric",
        "revision": 12,
        "sheet": {
            "spellcasting": {
                "preparation": {
                    "selected_spell_ids": ["bless"],
                }
            }
        },
    }

    with pytest.raises(RuntimeError, match="different committed prepared-spell list"):
        asyncio.run(
            _initialize_prepared_spells(
                Client(),
                actor=actor,
                prepared_ids=["cure-wounds"],
                idempotency_key="replacement-cleric-setup",
            )
        )


def test_party_profiles_have_source_linked_gear_and_complete_ability_input() -> None:
    profiles = lost_mine_party_profiles()

    assert all(profile["items"] for profile in profiles)
    assert all(item["source_key"] for profile in profiles for item in profile["items"])
    assert {profile["background_base"] for profile in profiles} == {"Acolyte"}
    assert len({profile["background"] for profile in profiles}) == len(profiles)
    assert all(len(profile["background_skills"]) == 2 for profile in profiles)
    assert all(len(_background_starting_items(profile)) == 6 for profile in profiles)
    assert all(_class_starting_supplements(profile) for profile in profiles)
    assert all(len(profile["abilities"]) == 6 for profile in profiles)


@pytest.mark.parametrize(
    ("class_name", "expected_class_list"),
    [("Bard", "bard"), ("Cleric", "cleric"), ("Wizard", "wizard")],
)
def test_base_casters_record_their_spell_class_list(
    class_name: str,
    expected_class_list: str,
) -> None:
    profile = next(item for item in waterdeep_party_profiles() if item["class"] == class_name)
    actor = {"sheet": default_character_sheet()}
    catalog = []
    seen: set[tuple[str, str]] = set()
    for item in [
        *profile["items"],
        *_class_starting_supplements(profile),
        *_background_starting_items(profile),
    ]:
        kind = str(item.get("_source_kind") or "item")
        name = str(item["source_key"])
        if (kind, name) not in seen:
            seen.add((kind, name))
            catalog.append({"id": f"{kind}:{len(catalog)}", "kind": kind, "name": name})

    sheet = _configure_base_sheet(actor, profile, catalog)

    assert sheet["spellcasting"]["class_lists"] == [expected_class_list]
    constitution = int(sheet["abilities"]["constitution"]["score"])
    expected_hp = int(profile["hit_die"]) + ability_modifier(constitution)
    assert sheet["combat"]["hp"] == {
        "value": expected_hp,
        "max": expected_hp,
        "temp": 0,
    }


def test_spellcasting_audit_reports_class_and_species_cantrips_separately() -> None:
    profile = next(item for item in storm_kings_party_profiles() if item["name"] == "Aelar Quill")
    sheet = default_character_sheet()
    sheet["progression"]["level"] = 1
    configured = profile["spellcasting"]
    cantrip_names = [
        profile["species_selection"]["cantrip"],
        *configured["cantrips"],
    ]
    spell_names = list(configured["spells"])

    def card(name: str, *, level: int) -> dict:
        identifier = name.casefold().replace(" ", "-")
        return {
            "id": identifier,
            "name": name,
            "level": level,
            "access": {
                "known": level == 0,
                "prepared": name in configured["prepared"],
                "always_prepared": False,
                "in_spellbook": level > 0,
                "ritual_available": False,
                "at_will": False,
                "at_will_sources": [],
            },
            "ruling_requirements": [
                {
                    "default_resolver": "agent",
                    "ruling_kind": "generic_spell_effect",
                    "source_excerpt": f"{name} uses its source-described effect.",
                }
            ],
        }

    sheet["content"]["spells"] = [
        *[card(name, level=0) for name in cantrip_names],
        *[card(name, level=1) for name in spell_names],
    ]
    spell_ids = [name.casefold().replace(" ", "-") for name in spell_names]
    sheet["spellcasting"]["spellbook"] = {
        "enabled": True,
        "spell_ids": spell_ids,
    }
    sheet["spellcasting"]["preparation"]["selected_spell_ids"] = [
        name.casefold().replace(" ", "-") for name in configured["prepared"]
    ]

    audit = _spellcasting_audit({"sheet": sheet}, profile)

    assert audit["mode"] == "spellbook"
    assert audit["cantrip_spell_names"] == cantrip_names
    assert len(audit["cantrip_spell_ids"]) == 4
    assert audit["spellbook_spell_ids"] == spell_ids
    assert len(audit["prepared_spell_ids"]) == 4
    assert audit["resolution_audit"]["complete"] is True

    sheet["content"]["spells"][0].pop("ruling_requirements")
    with pytest.raises(RuntimeError, match="without a settlement path"):
        _spellcasting_audit({"sheet": sheet}, profile)


def test_spellcasting_audit_rejects_a_missing_level_one_cantrip() -> None:
    profile = next(
        item for item in storm_kings_party_profiles() if item["name"] == "Seraphine Vale"
    )
    sheet = default_character_sheet()
    sheet["progression"]["level"] = 1
    sheet["content"]["spells"] = [
        {
            "id": "vicious-mockery",
            "name": "Vicious Mockery",
            "level": 0,
            "access": {"known": True},
        },
        *[
            {
                "id": name.casefold().replace(" ", "-"),
                "name": name,
                "level": 1,
                "access": {"known": True},
            }
            for name in profile["spellcasting"]["spells"]
        ],
    ]

    with pytest.raises(RuntimeError, match="incomplete cantrip grant"):
        _spellcasting_audit({"sheet": sheet}, profile)


def test_starting_equipment_packs_expand_to_rule_accurate_consumable_items() -> None:
    rogue = next(profile for profile in lost_mine_party_profiles() if profile["class"] == "Rogue")
    bard = next(profile for profile in lost_mine_party_profiles() if profile["class"] == "Bard")
    burglar_items = _pack_contents(rogue, "Burglar's Pack")
    diplomat_items = _pack_contents(bard, "Diplomat's Pack")

    assert all(item["name"] != "Burglar's Pack" for item in burglar_items)
    assert all(item["name"] != "Diplomat's Pack" for item in diplomat_items)
    burglar_oil = next(item for item in burglar_items if item["name"] == "Oil (flask)")
    diplomat_oil = next(item for item in diplomat_items if item["name"] == "Oil (flask)")
    assert burglar_oil["quantity"] == diplomat_oil["quantity"] == 2
    assert burglar_oil["weight_oz"] == diplomat_oil["weight_oz"] == 16
    assert burglar_oil["description"] == OIL_RULE
    assert burglar_oil["mechanics"] == {
        "consumable": True,
        "use_action": "use_object",
        "covered_duration_rounds": 10,
        "trigger_damage_type": "fire",
        "additional_fire_damage": 5,
    }
    assert next(item for item in burglar_items if item["name"] == "Candle")["quantity"] == 5
    assert (
        next(item for item in diplomat_items if item["name"] == "Paper (one sheet)")["quantity"]
        == 5
    )


def test_starting_item_weights_follow_srd_units_including_fractional_ammunition() -> None:
    assert _item_weight_oz("Arrows") == 0.8
    assert _item_weight_oz("Crossbow bolts") == 1.2
    assert _item_weight_oz("Piton") == 4
    assert _item_weight_oz("Chain mail") == 880
    assert _item_weight_oz("Waterskin") == 80
    assert _item_weight_oz("Candle") == 0


def test_waterdeep_party_uses_explicit_dm_review_not_a_fake_module_range() -> None:
    profiles = waterdeep_party_profiles()
    audit = audit_profiles(profiles, campaign_line_id="waterdeep-dragon-heist")

    assert audit["selected_size"] == 4
    assert audit["source_maximum"] is None
    assert audit["party_size_basis"] == {
        "kind": "explicit_dm_review",
        "module_party_size_status": "not_stated_after_text_and_visual_review",
        "core_fallback": "2014 SRD Challenge baseline: party of four adventurers",
        "selected": 4,
        "represented_as_module_recommendation": False,
    }
    assert audit["classes_unique"] is True
    assert audit["species_unique"] is True
    assert audit["ability_methods"] == ["manual", "point_buy", "standard_array"]
    assert audit["spell_resource_models"] == ["known", "prepared", "spellbook"]
    assert audit["backgrounds_unique"] is True
    assert audit["background_customization"] == {
        "base_artifact": "Acolyte",
        "rule": "2014 Core: Customizing a Background",
        "feature_disposition": "retain Shelter of the Faithful",
        "equipment_disposition": "retain the complete Acolyte package",
        "unconfirmed_extensions_used": False,
    }
    assert audit["pregenerated_first"]["official_sheets_present_in_corpus"] is False


def test_tyranny_party_uses_source_four_and_preserves_continuous_party() -> None:
    profiles = tyranny_party_profiles()
    audit = audit_profiles(profiles, campaign_line_id="tyranny-of-dragons")
    seraphine = next(item for item in profiles if item["name"] == "Seraphine Vale")

    assert audit["selected_size"] == audit["source_maximum"] == 4
    assert audit["party_size_basis"] == {
        "kind": "module_source_maximum",
        "source_minimum": 4,
        "source_maximum": 4,
        "selected": 4,
        "starting_level": 1,
        "continuation": "preserve the same party into The Rise of Tiamat",
    }
    assert audit["classes_unique"] is True
    assert audit["species_unique"] is True
    assert audit["ability_methods"] == ["manual", "point_buy", "standard_array"]
    assert audit["spell_resource_models"] == ["known", "prepared", "spellbook"]
    assert audit["pregenerated_first"] == {
        "module_mentions_included_characters": False,
        "official_sheets_present_in_corpus": False,
        "associated_templates_present": 0,
        "disposition": (
            "legally generate all four source-confirmed seats once and "
            "preserve them across both volumes"
        ),
    }
    assert "bardic_inspiration" not in seraphine["resources"]


@pytest.mark.parametrize(
    ("campaign_line_id", "factory", "terrain"),
    [
        ("storm-kings-thunder", storm_kings_party_profiles, "Mountain"),
        (
            "tomb-of-annihilation",
            tomb_of_annihilation_party_profiles,
            "Forest",
        ),
    ],
)
def test_six_character_campaign_parties_use_source_maximum_and_ranger(
    campaign_line_id: str,
    factory,
    terrain: str,
) -> None:
    profiles = factory()
    audit = audit_profiles(profiles, campaign_line_id=campaign_line_id)
    ranger = next(item for item in profiles if item["class"] == "Ranger")

    assert audit["selected_size"] == audit["source_maximum"] == 6
    assert audit["party_size_basis"]["source_minimum"] == 4
    assert audit["party_size_basis"]["starting_level"] == 1
    assert audit["classes_unique"] is True
    assert audit["species_unique"] is True
    assert audit["ability_methods"] == ["manual", "point_buy", "standard_array"]
    assert audit["spell_resource_models"] == ["known", "prepared", "spellbook"]
    if campaign_line_id == "storm-kings-thunder":
        assert audit["pregenerated_first"]["associated_archetype_templates"] == 7
        assert (
            "leave identity, level, all ability scores"
            in audit["pregenerated_first"]["disposition"]
        )
    assert ranger["feature_choices"]["Favored Enemy"]["favored_enemy"] == {
        "creature_type": "Giants",
        "humanoid_races": [],
        "enemy_speaks_language": False,
        "language": "",
    }
    assert "Giant" in ranger["background_languages"]
    assert ranger["feature_choices"]["Natural Explorer"] == {"terrain": terrain}
    assert _class_starting_supplements(ranger)
    assert {item["name"] for item in ranger["items"]} >= {
        "Scale mail",
        "Shortsword",
        "Longbow",
        "Arrows",
        "Quiver",
    }


def test_catalog_source_normalizes_srd_table_markers_but_never_invents_items() -> None:
    catalog = [
        {
            "id": "dnd5e.content.srd2014.item.lute",
            "kind": "item",
            "name": "~ Lute",
        }
    ]

    assert _catalog_source(catalog, "Lute").endswith(".lute")
    with pytest.raises(RuntimeError, match="no source-linked item"):
        _catalog_source(catalog, "Unlisted pack")


def test_one_replacement_reuses_a_legal_profile_without_inheriting_identity() -> None:
    selected, audit = select_profiles(
        lost_mine_party_profiles(),
        profile_name="Aelar Quill",
        actor_name="Mira Emberleaf",
    )

    assert len(selected) == 1
    assert selected[0]["name"] == "Mira Emberleaf"
    assert selected[0]["class"] == "Wizard"
    spellbook = next(item for item in selected[0]["items"] if item["name"] == "Spellbook")
    assert spellbook["mechanics"]["owner_mark"] == "Mira Emberleaf"
    assert audit["source_profile_name"] == "Aelar Quill"
    assert audit["knowledge_inheritance"] == "none"
    source_wizard = next(item for item in lost_mine_party_profiles() if item["class"] == "Wizard")
    assert source_wizard["name"] == "Aelar Quill"
    source_spellbook = next(item for item in source_wizard["items"] if item["name"] == "Spellbook")
    assert source_spellbook["mechanics"]["owner_mark"] == "Aelar Quill"


def test_replacement_phase_switch_uses_public_campaign_and_branch_tools() -> None:
    class Client:
        def __init__(self) -> None:
            self.revision = 9
            self.phase = "play"
            self.loaded: list[tuple[str, ...]] = []

        async def core(self, tool_id: str, arguments: dict):
            if tool_id == "campaign_query":
                return {
                    "result": {
                        "id": "campaign-1",
                        "revision": self.revision,
                        "state": {"game_phase": self.phase},
                    }
                }
            assert tool_id == "game_phase"
            assert arguments["expected_revision"] == 9
            assert arguments["tool_profile"] == "lobby"
            self.phase = "lobby"
            self.revision += 1
            return {"result": {"tool_profile": "lobby", "campaign_revision": 10}}

        async def domain(self, tool_id: str, arguments: dict):
            assert tool_id == "branch_query"
            assert arguments == {"campaign_id": "campaign-1", "view": "list"}
            return [{"id": "branch-1", "is_current": True}]

        async def open(self, campaign_id: str) -> None:
            assert campaign_id == "campaign-1"

        async def load(self, *groups: str) -> None:
            self.loaded.append(groups)

    client = Client()
    result = asyncio.run(
        _switch_phase(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            current_phase="play",
            target_phase="lobby",
            purpose="replacement",
        )
    )

    assert result == {"tool_profile": "lobby", "campaign_revision": 10}
    assert client.loaded[-1] == ()
