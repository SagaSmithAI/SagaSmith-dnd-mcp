from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

from scripts.regression_party import (
    OIL_RULE,
    _background_starting_items,
    _catalog_source,
    _class_starting_supplements,
    _configure_base_sheet,
    _item_weight_oz,
    _pack_contents,
    _repair_existing_party_equipment,
    _source_linked_starting_items,
    _switch_phase,
    audit_profiles,
    lost_mine_party_profiles,
    select_profiles,
    tyranny_party_profiles,
    waterdeep_party_profiles,
)


def test_lost_mine_party_uses_source_maximum_and_diverse_core_models() -> None:
    audit = audit_profiles(lost_mine_party_profiles())

    assert audit["selected_size"] == audit["source_maximum"] == 5
    assert audit["classes_unique"] is True
    assert audit["species_unique"] is True
    assert audit["ability_methods"] == ["manual", "point_buy", "standard_array"]
    assert audit["spell_resource_models"] == ["known", "prepared", "spellbook"]
    assert audit["pregenerated_first"]["official_sheets_present_in_corpus"] is False
    assert "excluded" in audit["pregenerated_first"]["associated_pc_smalls_disposition"]


def test_party_profiles_have_source_linked_gear_and_complete_ability_input() -> None:
    profiles = lost_mine_party_profiles()

    assert all(profile["items"] for profile in profiles)
    assert all(item["source_key"] for profile in profiles for item in profile["items"])
    assert {profile["background_base"] for profile in profiles} == {"Acolyte"}
    assert len({profile["background"] for profile in profiles}) == len(profiles)
    assert all(
        len(profile["background_skills"]) == 2
        for profile in profiles
    )
    assert all(
        len(_background_starting_items(profile)) == 6
        for profile in profiles
    )
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
    profile = next(
        item
        for item in waterdeep_party_profiles()
        if item["class"] == class_name
    )
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
            catalog.append(
                {"id": f"{kind}:{len(catalog)}", "kind": kind, "name": name}
            )

    sheet = _configure_base_sheet(actor, profile, catalog)

    assert sheet["spellcasting"]["class_lists"] == [expected_class_list]


def test_starting_equipment_packs_expand_to_rule_accurate_consumable_items() -> None:
    rogue = next(
        profile for profile in lost_mine_party_profiles() if profile["class"] == "Rogue"
    )
    bard = next(
        profile for profile in lost_mine_party_profiles() if profile["class"] == "Bard"
    )
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
    assert next(
        item for item in burglar_items if item["name"] == "Candle"
    )["quantity"] == 5
    assert next(
        item for item in diplomat_items if item["name"] == "Paper (one sheet)"
    )["quantity"] == 5


def test_starting_item_weights_follow_srd_units_including_fractional_ammunition() -> None:
    assert _item_weight_oz("Arrows") == 0.8
    assert _item_weight_oz("Crossbow bolts") == 1.2
    assert _item_weight_oz("Piton") == 4
    assert _item_weight_oz("Chain mail") == 880
    assert _item_weight_oz("Waterskin") == 80
    assert _item_weight_oz("Candle") == 0


def test_existing_opaque_pack_is_repaired_only_through_public_inventory_calls(
    tmp_path,
) -> None:
    profile = next(
        item for item in waterdeep_party_profiles() if item["class"] == "Rogue"
    )
    raw_items = [
        *profile["items"],
        *_class_starting_supplements(profile),
        *_background_starting_items(profile),
    ]
    catalog: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_items:
        kind = str(item.get("_source_kind") or "item")
        name = str(item["source_key"])
        if (kind, name) in seen:
            continue
        seen.add((kind, name))
        catalog.append(
            {"id": f"{kind}:{len(catalog)}", "kind": kind, "name": name}
        )
    desired = _source_linked_starting_items(profile, catalog)
    pack_ids = {
        item["id"] for item in _pack_contents(profile, "Burglar's Pack")
    }
    initial_items = [
        {**deepcopy(item), "weight_oz": 0}
        for item in desired
        if item["id"] not in pack_ids
    ]
    initial_items.append(
        {
            "id": "pip-underbough-burglars-pack",
            "name": "Burglar's Pack",
            "kind": "equipment",
            "quantity": 1,
            "weight_oz": 0,
        }
    )

    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []
            self.actor = {
                "id": "pip",
                "campaign_id": "campaign",
                "name": profile["name"],
                "revision": 1,
                "sheet": {"inventory": {"items": initial_items}},
                "derived": {
                    "inventory": {
                        "total_weight_oz": 0,
                        "encumbrance": {"carried_weight_oz": 0},
                    }
                },
            }

        def _refresh(self) -> None:
            total = sum(
                item.get("weight_oz", 0) * item.get("quantity", 1)
                for item in self.actor["sheet"]["inventory"]["items"]
            )
            self.actor["revision"] += 1
            self.actor["derived"]["inventory"] = {
                "total_weight_oz": total,
                "encumbrance": {"carried_weight_oz": total},
            }

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append((tool_id, deepcopy(arguments)))
            if tool_id == "character_query":
                return deepcopy(self.actor)
            assert tool_id == "inventory_change"
            assert arguments["owner"] == "character"
            assert arguments["expected_revision"] == self.actor["revision"]
            payload = arguments["payload"]
            if arguments["action"] == "remove":
                self.actor["sheet"]["inventory"]["items"] = [
                    item
                    for item in self.actor["sheet"]["inventory"]["items"]
                    if item["id"] != payload["item_id"]
                ]
            elif arguments["action"] == "add":
                self.actor["sheet"]["inventory"]["items"].append(
                    deepcopy(payload["item"])
                )
            else:
                item = next(
                    item
                    for item in self.actor["sheet"]["inventory"]["items"]
                    if item["id"] == payload["item_id"]
                )
                item.update(deepcopy(payload["patch"]))
            self._refresh()
            return deepcopy(self.actor)

    report_path = tmp_path / "party.json"
    report_path.write_text(
        json.dumps(
            {
                "campaign_id": "campaign",
                "characters": [{"actor_id": "pip", "name": profile["name"]}],
            }
        ),
        encoding="utf-8",
    )
    client = Client()
    repaired, changes = asyncio.run(
        _repair_existing_party_equipment(
            client,
            campaign_id="campaign",
            run_id="run",
            profiles=[profile],
            catalog=catalog,
            report_path=report_path,
        )
    )

    final_ids = {
        item["id"] for item in client.actor["sheet"]["inventory"]["items"]
    }
    assert "pip-underbough-burglars-pack" not in final_ids
    assert pack_ids <= final_ids
    assert repaired[0]["total_weight_oz"] > 0
    assert changes[0]["changes"][0]["action"] == "remove_opaque_pack"
    assert all(tool_id in {"character_query", "inventory_change"} for tool_id, _ in client.calls)


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
    assert audit["source_profile_name"] == "Aelar Quill"
    assert audit["knowledge_inheritance"] == "none"
    assert next(
        item for item in lost_mine_party_profiles() if item["class"] == "Wizard"
    )["name"] == "Aelar Quill"


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
    assert client.loaded[-1] == ("lobby.campaign", "lobby.rules", "lobby.characters")
