from __future__ import annotations

import pytest

from scripts.regression_party import (
    _catalog_source,
    audit_profiles,
    lost_mine_party_profiles,
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
    assert {profile["background"] for profile in profiles} == {"Acolyte"}
    assert all(len(profile["abilities"]) == 6 for profile in profiles)


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
