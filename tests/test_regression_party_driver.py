from __future__ import annotations

import asyncio

import pytest

from scripts.regression_party import (
    _catalog_source,
    _switch_phase,
    audit_profiles,
    lost_mine_party_profiles,
    select_profiles,
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
