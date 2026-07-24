from __future__ import annotations

import argparse
import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet
from sagasmith_dnd.playthrough import (
    new_playthrough_manifest,
    validate_playthrough_manifest,
)

import scripts.regression_playthrough as regression_playthrough
from scripts.regression_playthrough import (
    _acquire_source_loot,
    _advance_level,
    _advance_scene,
    _advance_time,
    _apply_source_damage,
    _award_experience,
    _branch_from_snapshot,
    _campaign_phase,
    _check_identity,
    _check_knowledge_key,
    _checkpoint,
    _committed_check_result,
    _configure_advancement,
    _extend_manifest_for_module_revision,
    _long_rest,
    _matching_check_progress,
    _mutation_key,
    _party_member,
    _party_selections,
    _phase_groups,
    _prepare_narrative_npc,
    _provision_source_item,
    _query_source,
    _record_event,
    _record_outcome,
    _recover_committed_check,
    _recover_stable_party,
    _register_replacement,
    _relock_core,
    _resolve_check,
    _restore_phase_after_failed_refresh,
    _scene_progress_percent,
    _short_rest,
    _short_rest_identity,
    _spend_source_currency,
    _spend_source_item,
    _stable_recovery_identity,
    _stand_after_source_event,
    _stand_identity,
    _start_play,
    _transfer_source_item_to_party,
    _use_activity,
    _use_shared_consumable,
)


def _manifest_source_ref() -> dict:
    return {
        "purpose": "test",
        "asset_path": "module.pdf",
        "asset_sha256": "a" * 64,
        "page_start": 10,
        "page_end": 11,
        "heading_path": ["Goblin Den"],
        "chunk_content_sha256": "b" * 64,
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "excerpt": "The hostage is released.",
    }


def test_playthrough_parser_accepts_deferred_scene_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "regression_playthrough.py",
            "--home",
            str(tmp_path),
            "--campaign-id",
            "campaign",
            "--output",
            str(tmp_path / "report.json"),
            "--defer-checkpoint",
        ],
    )

    assert regression_playthrough._arguments().defer_checkpoint is True


def test_playthrough_rejects_deferred_checkpoint_for_key_rest() -> None:
    args = argparse.Namespace(defer_checkpoint=True, action="long-rest")

    with pytest.raises(ValueError, match="unsupported for long-rest"):
        asyncio.run(regression_playthrough._run(args))


def test_advance_scene_identity_supports_exact_retry_and_later_revisit() -> None:
    class Client:
        def __init__(self) -> None:
            self.revision = 1
            self.manifest = new_playthrough_manifest(
                run_id="run-1",
                campaign_line_id="line-1",
                module_ids=["module-1"],
                recommended_party_minimum=None,
                recommended_party_maximum=None,
                selected_party_size=None,
                source_refs=[_manifest_source_ref()],
            )
            self.manifest["current"] = {
                "module_id": "module-1",
                "chapter_id": "chapter-1",
                "chapter_title": "Chapter",
                "scene_id": "scene-old",
                "scene_title": "Old scene",
                "objective": "Leave.",
            }
            self.replace_calls: list[dict] = []

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": self.revision}}

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                assert arguments["payload"]["scene_id"] == "scene-town"
                return {
                    "module_id": "module-1",
                    "chapter_id": "chapter-1",
                    "chapter": "Chapter",
                    "scene_id": "scene-town",
                    "title": "Town",
                }
            if tool_id == "playthrough_manifest" and arguments["action"] == "get":
                return {
                    "manifest": deepcopy(self.manifest),
                    "campaign_revision": self.revision,
                }
            if tool_id == "playthrough_manifest" and arguments["action"] == "replace":
                self.replace_calls.append(deepcopy(arguments))
                self.manifest = deepcopy(arguments["payload"]["manifest"])
                self.revision += 1
                return {
                    "manifest": deepcopy(self.manifest),
                    "campaign_revision": self.revision,
                }
            raise AssertionError((tool_id, arguments))

    async def advance(client: Client) -> None:
        await _advance_scene(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-town",
            objective="Return the rescued family.",
            mark_visited=True,
            reachable_scene_ids=[],
            excluded_scenes=[],
        )

    client = Client()
    asyncio.run(advance(client))
    asyncio.run(advance(client))
    first_key, retry_key = [
        item["idempotency_key"] for item in client.replace_calls
    ]
    assert first_key == retry_key
    assert (
        client.replace_calls[0]["payload"]["manifest"]
        == client.replace_calls[1]["payload"]["manifest"]
    )

    client.manifest["world_state"]["visit_marker"] = 2
    asyncio.run(advance(client))
    revisit_key = client.replace_calls[2]["idempotency_key"]
    assert revisit_key != first_key


def test_core_relock_driver_requires_current_checkpoint_and_public_profile() -> None:
    class Client:
        def __init__(self) -> None:
            self.revision = 20
            self.tools: list[str] = []

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": self.revision}}

        async def domain(self, tool_id: str, arguments: dict):
            self.tools.append(tool_id)
            if tool_id == "campaign_rules":
                return {
                    "profile": {"options": {"_core_rule_pack_lock": {"fingerprint": "old-core"}}}
                }
            if tool_id == "branch_query":
                return [
                    {
                        "id": "branch-1",
                        "is_current": True,
                        "head_snapshot_id": "snapshot-1",
                    }
                ]
            if tool_id == "campaign_core_relock":
                assert arguments["expected_core_fingerprint"] == "old-core"
                assert arguments["expected_head_snapshot_id"] == "snapshot-1"
                self.revision += 1
                return {
                    "status": "relocked",
                    "core_pack": {"fingerprint": "new-core"},
                }
            if tool_id == "playthrough_manifest":
                return {"manifest": {"status": "in_progress"}, "campaign_revision": 22}
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _relock_core(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            reason="Adopt the checkpointed consumable rule boundary.",
        )
    )

    assert result["checkpoint_snapshot_id"] == "snapshot-1"
    assert result["relock"]["core_pack"]["fingerprint"] == "new-core"
    assert client.tools.count("campaign_core_relock") == 1


def test_failed_module_refresh_restores_its_entry_phase() -> None:
    class Client:
        def __init__(self) -> None:
            self.phase = "lobby"
            self.revision = 12
            self.loaded: list[tuple[str, ...]] = []

        async def open(self, campaign_id: str) -> None:
            assert campaign_id == "campaign-1"

        async def load(self, *groups: str) -> None:
            self.loaded.append(groups)

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
            assert arguments["tool_profile"] == "play"
            assert arguments["expected_revision"] == 12
            self.phase = "play"
            self.revision += 1
            return {"result": {"tool_profile": "play", "campaign_revision": self.revision}}

        async def domain(self, tool_id: str, arguments: dict):
            assert tool_id == "branch_query"
            assert arguments == {"campaign_id": "campaign-1", "view": "list"}
            return [{"id": "branch-1", "is_current": True}]

    client = Client()
    result = asyncio.run(
        _restore_phase_after_failed_refresh(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            original_phase="play",
        )
    )

    assert result == {"tool_profile": "play", "campaign_revision": 13}
    assert client.phase == "play"
    assert client.loaded[-1] == ("play.scene_control", "play.scene")


@pytest.mark.parametrize("defer_checkpoint", [False, True])
def test_narrative_npc_driver_round_trips_lobby_and_registers_manifest(
    defer_checkpoint: bool,
) -> None:
    source_ref = {
        "purpose": "Create a source-bound narrative NPC",
        "asset_path": "module.pdf",
        "asset_sha256": "b" * 64,
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 18,
        "page_end": 18,
        "heading_path": ["Part 2", "Alderleaf Farm"],
        "content_sha256": "b" * 64,
    }

    class Client:
        def __init__(self) -> None:
            self.phase = "play"
            self.revision = 20
            self.loaded: list[tuple[str, ...]] = []
            self.manifest = new_playthrough_manifest(
                run_id="run-1",
                campaign_line_id="line-1",
                module_ids=["module-1"],
                recommended_party_minimum=None,
                recommended_party_maximum=None,
                selected_party_size=None,
                source_refs=[_manifest_source_ref()],
            )
            self.actor = {
                "id": "npc-1",
                "campaign_id": "campaign-1",
                "character_type": "npc",
                "name": "Qelline Alderleaf",
                "sheet": {
                    "adventure_state": {
                        "status_tags": ["narrative_only", "source_bound"]
                    }
                },
            }
            self.snapshot_calls = 0

        async def open(self, campaign_id: str) -> None:
            assert campaign_id == "campaign-1"

        async def load(self, *groups: str) -> None:
            self.loaded.append(groups)

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
            assert arguments["tool_profile"] in {"lobby", "play"}
            self.phase = arguments["tool_profile"]
            self.revision += 1
            return {
                "result": {
                    "tool_profile": self.phase,
                    "campaign_revision": self.revision,
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": (
                        "Qelline Alderleaf is a pragmatic farmer and can introduce Carp."
                    ),
                    "spatial": {"locations": [{"key": "alderleaf-farm"}]},
                }
            if tool_id == "branch_query":
                return [
                    {
                        "id": "branch-1",
                        "is_current": True,
                        "head_snapshot_id": "snapshot-old",
                    }
                ]
            if tool_id == "character_create_from":
                assert self.phase == "lobby"
                assert arguments["mode"] == "narrative_npc"
                assert arguments["payload"]["source_ref"] == source_ref
                canonical_source_ref = {
                    key: deepcopy(source_ref[key])
                    for key in (
                        "module_id",
                        "scene_id",
                        "chunk_id",
                        "page_start",
                        "page_end",
                        "heading_path",
                        "content_sha256",
                    )
                }
                return {
                    "character": deepcopy(self.actor),
                    "narrative_npc": {
                        "combat_eligible": False,
                        "combat_statblock": "not_imported",
                        "source_ref": canonical_source_ref,
                    },
                }
            if tool_id == "character_query":
                assert self.phase == "play"
                return deepcopy(self.actor)
            if tool_id == "playthrough_manifest":
                action = arguments["action"]
                if action == "get":
                    return {
                        "manifest": deepcopy(self.manifest),
                        "campaign_revision": self.revision,
                    }
                if action == "replace":
                    self.manifest = deepcopy(arguments["payload"]["manifest"])
                self.revision += 1
                return {
                    "manifest": deepcopy(self.manifest),
                    "campaign_revision": self.revision,
                }
            if tool_id == "snapshot_create":
                self.snapshot_calls += 1
                assert arguments["label"] == "Narrative NPC prepared: Qelline Alderleaf"
                self.revision += 1
                self.manifest["snapshot_dag"] = {
                    "active_branch_id": "branch-1",
                    "head_snapshot_id": "snapshot-new",
                    "nodes": [
                        {
                            "id": "snapshot-new",
                            "parent_id": "snapshot-old",
                            "branch_id": "branch-1",
                            "slot": 7,
                            "label": arguments["label"],
                            "checksum": "a" * 64,
                            "is_head": True,
                        }
                    ],
                }
                return {"id": "snapshot-new", "slot": 7}
            if tool_id == "snapshot_query":
                return {"valid": True, "slot": 7}
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _prepare_narrative_npc(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            initial_phase="play",
            scene_id="scene-1",
            location_key="alderleaf-farm",
            source_excerpt=(
                "Qelline Alderleaf is a pragmatic farmer and can introduce Carp."
            ),
            source_ref=source_ref,
            name="Qelline Alderleaf",
            role="Pragmatic farmer and local guide.",
            summary="Qelline hosts the party and can introduce her son Carp.",
            faction="Phandalin",
            relationship="helpful host",
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert client.phase == "play"
    assert result["actor"]["id"] == "npc-1"
    assert result["narrative_npc"]["combat_eligible"] is False
    assert client.manifest["npcs"][0]["actor_id"] == "npc-1"
    assert "combat_statblock=not_imported" in client.manifest["npcs"][0]["notes"]
    assert client.snapshot_calls == (0 if defer_checkpoint else 1)
    if defer_checkpoint:
        assert result["checkpoint"] is None
    else:
        assert result["checkpoint"]["verification"]["valid"] is True


def test_shared_consumable_driver_keeps_roll_item_and_healing_in_one_transition() -> None:
    class Client:
        def __init__(self) -> None:
            self.revision = 10
            self.tools: list[str] = []

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.revision,
                    "state": {"game_phase": "play"},
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            self.tools.append(tool_id)
            if tool_id == "module_query":
                return {
                    "scene_id": "scene-1",
                    "spatial": {"locations": [{"key": "room-1"}]},
                }
            if tool_id == "character_query":
                return {
                    "id": "actor-1",
                    "name": "Actor One",
                    "campaign_id": "campaign-1",
                    "revision": 3,
                }
            if tool_id == "campaign_change":
                assert arguments["action"] == "consumable_use"
                assert arguments["payload"]["expected_character_revision"] == 3
                self.revision += 1
                return {
                    "status": "committed",
                    "formula": "2d4+2",
                    "roll": {"total": 7},
                    "healing": {"before_hp": 1, "after_hp": 8},
                }
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "continuity_commit":
                return {"event": {"id": "event-1"}, "snapshot": {"slot": 8}}
            if tool_id == "playthrough_manifest":
                return {"manifest": {"status": "in_progress"}, "campaign_revision": 12}
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _use_shared_consumable(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            location_key="room-1",
            use_id="potion-use-1",
            item_id="healing-potions",
            target_character_id="actor-1",
            reason="Actor One drank a healing potion.",
            knowledge_actor_ids=["actor-2"],
        )
    )

    assert client.tools.count("campaign_change") == 1
    assert result["use"]["roll"]["total"] == 7
    assert result["knowledge_actor_ids"] == ["actor-1", "actor-2"]


def test_source_loot_driver_uses_one_public_atomic_campaign_transition() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "source-scene-1",
        "chunk_id": "chunk-1",
        "page_start": 1,
        "page_end": 1,
        "heading_path": ["Chapter One", "Treasure Room"],
        "content_sha256": "a" * 64,
    }

    class Client:
        def __init__(self) -> None:
            self.revision = 4
            self.tools: list[str] = []
            self.continuity_payload: dict = {}

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.revision,
                    "state": {"game_phase": "play"},
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            self.tools.append(tool_id)
            if tool_id == "module_query":
                if arguments["payload"]["scene_id"] == "source-scene-1":
                    return {
                        "module_id": "module-1",
                        "scene_id": "source-scene-1",
                        "content": "The patron promises a payment of 60 cp and a jade frog.",
                    }
                assert arguments["payload"]["scene_id"] == "scene-1"
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "spatial": {"locations": [{"key": "treasure-room", "title": "Treasure Room"}]},
                }
            if tool_id == "campaign_change":
                assert arguments["action"] == "loot_acquire"
                assert arguments["payload"]["coins"] == {"cp": 60}
                self.revision += 1
                return {
                    "status": "committed",
                    "acquisition_id": "chapter-one-chest",
                    "coins": {"cp": 60},
                    "items": [{"id": "jade-frog"}],
                }
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "continuity_commit":
                self.continuity_payload = deepcopy(arguments["payload"])
                assert len(arguments["payload"]["actor_knowledge"]) == 2
                return {"event": {"id": "event-1"}, "snapshot": {"slot": 7}}
            if tool_id == "playthrough_manifest":
                return {"manifest": {"status": "in_progress"}, "campaign_revision": 6}
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _acquire_source_loot(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            location_key="treasure-room",
            source_excerpt="payment of 60 cp and a jade frog",
            source_ref=source_ref,
            acquisition_id="chapter-one-chest",
            coins={"cp": 60},
            items=[
                {
                    "id": "jade-frog",
                    "name": "Jade frog",
                    "kind": "loot",
                    "quantity": 1,
                }
            ],
            reason="The party recovered the treasure.",
            knowledge_actor_ids=["actor-1", "actor-2"],
            source_scene_id="source-scene-1",
            defer_checkpoint=True,
        )
    )

    assert result["acquisition"]["status"] == "committed"
    assert client.tools.count("campaign_change") == 1
    assert result["knowledge_actor_ids"] == ["actor-1", "actor-2"]
    assert result["scene"]["source_scene_id"] == "source-scene-1"
    assert "snapshot" not in client.continuity_payload


@pytest.mark.parametrize("defer_checkpoint", [False, True])
def test_source_item_driver_validates_provenance_hydrates_and_equips(
    defer_checkpoint: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "reference-scene",
        "chunk_id": "staff-chunk",
        "page_start": 53,
        "page_end": 53,
        "heading_path": ["Appendix A", "Staff of Defense"],
        "content_sha256": "a" * 64,
    }
    item = {
        "id": "staff-of-defense",
        "name": "Staff of Defense",
        "kind": "magic_item",
        "source_key": "module-chunk:staff-chunk",
        "attunement": "attuned",
        "charges": {
            "label": "Staff charges",
            "value": 10,
            "max": 10,
            "recovers_on": "dawn",
            "source_key": "module-chunk:staff-chunk",
        },
        "mechanics": {
            "ac_bonus": 1,
            "spellcasting": {
                "requires_attunement": True,
                "requires_class_spell_list": True,
                "components_required": False,
                "spells": [
                    {
                        "artifact_id": "dnd5e.content.srd2014.spell.mage-armor",
                        "charge_cost": 1,
                        "casting_time": "1 action",
                    }
                ],
            },
        },
    }

    class Client:
        def __init__(self) -> None:
            sheet = default_character_sheet()
            sheet["spellcasting"]["class_lists"] = ["wizard"]
            self.actor = {
                "id": "iarno",
                "name": "Iarno Albrek",
                "campaign_id": "campaign-1",
                "revision": 3,
                "sheet": sheet,
                "derived": {"armor_class": 12},
            }
            self.inventory_actions: list[str] = []

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "reference-scene",
                    "content": "The staff has 10 charges and can cast mage armor.",
                }
            if tool_id == "character_query":
                return deepcopy(self.actor)
            if tool_id == "inventory_change":
                action = arguments["action"]
                self.inventory_actions.append(action)
                if action == "add":
                    hydrated = deepcopy(arguments["payload"]["item"])
                    hydrated["mechanics"]["spellcasting"]["spells"][0]["card"] = {
                        "id": "dnd5e.content.srd2014.spell.mage-armor",
                        "pack_id": "dnd5e.content.srd2014",
                        "rule_refs": ["srd2014.spells.mage-armor"],
                    }
                    self.actor["sheet"]["inventory"]["items"].append(hydrated)
                else:
                    assert action == "equip"
                    equipped = self.actor["sheet"]["inventory"]["items"][0]
                    equipped["equipped"] = True
                    equipped["equipped_slot"] = arguments["payload"]["slot"]
                    self.actor["derived"]["armor_class"] = 13
                self.actor["revision"] += 1
                return {"character": deepcopy(self.actor)}
            raise AssertionError((tool_id, arguments))

    checkpoint_calls = 0

    async def checkpoint(*_args, **_kwargs):
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        return {"snapshot": {"slot": 12}, "verification": {"valid": True}}

    monkeypatch.setattr(regression_playthrough, "_checkpoint", checkpoint)
    client = Client()
    result = asyncio.run(
        _provision_source_item(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            actor_id="iarno",
            source_scene_id="reference-scene",
            source_excerpt="staff has 10 charges",
            source_ref=source_ref,
            item=item,
            equip_slot="main_hand",
            reason="Iarno wields the source-declared staff.",
            checkpoint_label="Area 12 staff ready",
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert client.inventory_actions == ["add", "equip"]
    assert result["actor"]["class_lists"] == ["wizard"]
    assert result["actor"]["armor_class"] == 13
    assert result["item"]["equipped_slot"] == "main_hand"
    assert result["item"]["mechanics"]["spellcasting"]["spells"][0]["card"]["rule_refs"]
    assert checkpoint_calls == (0 if defer_checkpoint else 1)
    if defer_checkpoint:
        assert result["checkpoint"] is None
    else:
        assert result["checkpoint"]["verification"]["valid"] is True


@pytest.mark.parametrize("defer_checkpoint", [False, True])
def test_source_item_transfer_driver_uses_atomic_character_to_party_public_tool(
    defer_checkpoint: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "treasure-chunk",
        "page_start": 26,
        "page_end": 26,
        "heading_path": ["Redbrand Hideout", "Treasure"],
        "content_sha256": "a" * 64,
    }
    staff = {
        "id": "staff-of-defense",
        "name": "Staff of Defense",
        "kind": "magic_item",
        "quantity": 1,
    }

    class Client:
        def __init__(self) -> None:
            sheet = default_character_sheet()
            sheet["inventory"]["items"].append(deepcopy(staff))
            self.actor = {
                "id": "iarno",
                "name": "Iarno",
                "campaign_id": "campaign-1",
                "revision": 4,
                "sheet": sheet,
                "derived": {"armor_class": 13},
            }
            self.party = {"inventory": {"items": []}}
            self.transfer_arguments: dict | None = None

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            if arguments["view"] == "party":
                return {"result": deepcopy(self.party)}
            return {"result": {"id": "campaign-1", "revision": 20}}

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "Iarno also wields a staff of defense.",
                    "spatial": {
                        "locations": [{"key": "iarno-quarters", "title": "Iarno's Quarters"}]
                    },
                }
            if tool_id == "character_query":
                return deepcopy(self.actor)
            if tool_id == "inventory_transfer":
                self.transfer_arguments = deepcopy(arguments)
                moved = self.actor["sheet"]["inventory"]["items"].pop()
                self.party["inventory"]["items"].append(deepcopy(moved))
                self.actor["revision"] += 1
                return {
                    "party": deepcopy(self.party),
                    "character": deepcopy(self.actor),
                    "item": moved,
                }
            raise AssertionError((tool_id, arguments))

    checkpoint_calls = 0

    async def checkpoint(*_args, **_kwargs):
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        return {"snapshot": {"slot": 13}, "verification": {"valid": True}}

    monkeypatch.setattr(regression_playthrough, "_checkpoint", checkpoint)
    client = Client()
    result = asyncio.run(
        _transfer_source_item_to_party(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            location_key="iarno-quarters",
            source_excerpt="Iarno also wields a staff of defense.",
            source_ref=source_ref,
            character_id="iarno",
            item_id="staff-of-defense",
            quantity=None,
            reason="The party secured the surrendered mage's staff.",
            checkpoint_label="Staff secured",
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert client.transfer_arguments is not None
    assert client.transfer_arguments["mode"] == "character_to_party"
    assert client.transfer_arguments["payload"]["expected_campaign_revision"] == 20
    assert client.transfer_arguments["payload"]["expected_character_revision"] == 4
    assert result["transfer"]["item"]["id"] == "staff-of-defense"
    assert checkpoint_calls == (0 if defer_checkpoint else 1)
    if defer_checkpoint:
        assert result["checkpoint"] is None
    else:
        assert result["checkpoint"]["verification"]["valid"] is True


def test_source_currency_spend_driver_uses_one_public_atomic_campaign_transition() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 15,
        "page_end": 15,
        "heading_path": ["Town", "Inn"],
        "content_sha256": "a" * 64,
    }

    class Client:
        def __init__(self) -> None:
            self.revision = 4
            self.tools: list[str] = []

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.revision,
                    "state": {"game_phase": "play", "currency_spends": []},
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            self.tools.append(tool_id)
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "This modest inn has six rooms for rent.",
                    "spatial": {"locations": [{"key": "inn", "title": "Inn"}]},
                }
            if tool_id == "campaign_change":
                assert arguments["action"] == "currency_spend"
                assert arguments["payload"]["coins"] == {"sp": 25}
                self.revision += 1
                return {
                    "status": "committed",
                    "spend_id": "lodging",
                    "coins": {"sp": 25},
                }
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "continuity_commit":
                assert len(arguments["payload"]["actor_knowledge"]) == 2
                assert arguments["payload"]["event"]["event_type"] == "currency_spent"
                self.revision += 1
                return {"event": {"id": "event-1"}, "snapshot": {"slot": 7}}
            if tool_id == "playthrough_manifest":
                return {"manifest": {"status": "in_progress"}, "campaign_revision": 7}
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _spend_source_currency(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            location_key="inn",
            source_excerpt="This modest inn has six rooms for rent.",
            source_ref=source_ref,
            spend_id="lodging",
            coins={"sp": 25},
            reason="The five PCs paid 5 sp each for one modest inn stay.",
            rule_ref="srd2014.expenses.food-drink-lodging.modest-inn",
            knowledge_actor_ids=["actor-1", "actor-2"],
        )
    )

    assert result["spend"]["status"] == "committed"
    assert client.tools.count("campaign_change") == 1
    assert result["knowledge_actor_ids"] == ["actor-1", "actor-2"]
    assert result["scene"]["location_key"] == "inn"


def test_source_item_spend_driver_uses_one_public_atomic_campaign_transition() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 23,
        "page_end": 23,
        "heading_path": ["Hideout", "Crevasse"],
        "content_sha256": "a" * 64,
    }

    class Client:
        def __init__(self) -> None:
            self.revision = 4
            self.tools: list[str] = []

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.revision,
                    "state": {"game_phase": "play", "item_spends": []},
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            self.tools.append(tool_id)
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "The nothic might betray the gang for a promise of food.",
                    "spatial": {"locations": [{"key": "crevasse", "title": "Crevasse"}]},
                }
            if tool_id == "campaign_change":
                assert arguments["action"] == "item_spend"
                assert arguments["payload"]["item_id"] == "severed-head"
                assert arguments["payload"]["quantity"] == 1
                self.revision += 1
                return {
                    "status": "committed",
                    "spend_id": "feed-nothic",
                    "item_id": "severed-head",
                    "quantity": 1,
                    "removed": {"id": "severed-head", "quantity": 1},
                }
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "continuity_commit":
                assert len(arguments["payload"]["actor_knowledge"]) == 3
                assert arguments["payload"]["event"]["event_type"] == "item_spent"
                self.revision += 1
                return {"event": {"id": "event-1"}, "snapshot": {"slot": 7}}
            if tool_id == "playthrough_manifest":
                return {"manifest": {"status": "in_progress"}, "campaign_revision": 7}
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _spend_source_item(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            location_key="crevasse",
            source_excerpt="betray the gang for a promise of food",
            source_ref=source_ref,
            spend_id="feed-nothic",
            item_id="severed-head",
            quantity=1,
            reason="The party surrendered the severed head to secure the nothic's truce.",
            knowledge_actor_ids=["actor-1", "actor-2", "nothic"],
        )
    )

    assert result["spend"]["status"] == "committed"
    assert result["spend"]["removed"]["id"] == "severed-head"
    assert client.tools.count("campaign_change") == 1
    assert result["knowledge_actor_ids"] == ["actor-1", "actor-2", "nothic"]


def test_query_source_searches_and_expands_only_public_mcp_results() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append((tool_id, arguments))
            if tool_id == "module_search":
                return {"result": [{"id": "chunk-1", "content": "A captured character..."}]}
            if tool_id == "module_expand":
                return {
                    "chunk_id": "chunk-1",
                    "content": "A captured character is taken to the eating cave.",
                    "content_sha256": "a" * 64,
                    "source_ref": {
                        "module_id": "module-1",
                        "scene_id": "scene-1",
                        "chunk_id": "chunk-1",
                        "page_start": 8,
                        "page_end": 8,
                        "heading_path": ["Eating Cave"],
                        "content_sha256": "a" * 64,
                    },
                }
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _query_source(
            client,
            campaign_id="campaign-1",
            query="  captured defeated characters  ",
            top_k=4,
            expand=True,
        )
    )

    assert result["query"] == "captured defeated characters"
    assert result["expanded_chunks"][0]["chunk_id"] == "chunk-1"
    assert result["expanded_chunks"][0]["source_ref"]["content_sha256"] == "a" * 64
    assert client.calls == [
        (
            "module_search",
            {
                "campaign_id": "campaign-1",
                "query": "captured defeated characters",
                "top_k": 4,
            },
        ),
        ("module_expand", {"chunk_id": "chunk-1"}),
    ]


def test_stable_party_recovery_uses_one_public_campaign_transition() -> None:
    class Client:
        def __init__(self) -> None:
            self.tools: list[str] = []
            self.keys: dict[str, str] = {}

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": 8}}

        async def domain(self, tool_id: str, arguments: dict):
            self.tools.append(tool_id)
            if tool_id == "character_query":
                actor_id = arguments["payload"]["character_id"]
                return {
                    "id": actor_id,
                    "name": actor_id,
                    "campaign_id": "campaign-1",
                    "revision": 3,
                }
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "campaign_change":
                assert arguments["action"] == "stable_recovery"
                assert len(arguments["payload"]["members"]) == 2
                self.keys["recovery"] = arguments["idempotency_key"]
                return {
                    "status": "recovered",
                    "elapsed_hours": 4,
                    "recoveries": {"actor-1": {}, "actor-2": {}},
                    "random_stream_receipt": {"start_position": 10, "end_position": 12},
                }
            if tool_id == "continuity_commit":
                assert len(arguments["payload"]["actor_knowledge"]) == 3
                self.keys["continuity"] = arguments["idempotency_key"]
                return {"event": {"id": "event-1"}, "snapshot": {"slot": 7}}
            if tool_id == "playthrough_manifest":
                self.keys["sync"] = arguments["idempotency_key"]
                return {"manifest": {"status": "in_progress"}, "campaign_revision": 10}
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _recover_stable_party(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            actor_ids=["actor-1", "actor-2"],
            knowledge_actor_ids=["witness"],
            reason="Both stable adventurers recovered while the party waited.",
        )
    )

    assert result["recovery"]["elapsed_hours"] == 4
    assert client.tools.count("campaign_change") == 1
    identity = _stable_recovery_identity(
        ["actor-1", "actor-2"],
        "Both stable adventurers recovered while the party waited.",
    )
    assert client.keys == {
        "recovery": _mutation_key("run-1", "stable-recovery", identity),
        "continuity": _mutation_key(
            "run-1", "stable-recovery-continuity", identity
        ),
        "sync": _mutation_key(
            "run-1", "sync", f"stable-recovery-sync:{identity}"
        ),
    }


def test_stable_recovery_identity_separates_later_occurrence_for_same_actor() -> None:
    first = _stable_recovery_identity(
        ["actor-1"],
        "Actor One recovered after the first battle.",
    )

    assert first == _stable_recovery_identity(
        ["actor-1"],
        "Actor One recovered after the first battle.",
    )
    assert first != _stable_recovery_identity(
        ["actor-1"],
        "Actor One recovered after the later battle.",
    )


def test_module_revision_extension_remaps_current_and_traversed_scenes() -> None:
    manifest = {
        "module_ids": ["module-v1"],
        "current": {
            "module_id": "module-v1",
            "chapter_id": "chapter-v1",
            "chapter_title": "Chapter",
            "scene_id": "scene-v1",
            "scene_title": "Cave",
        },
        "traversal": {
            "reachable_scene_ids": ["opening-v1", "scene-v1"],
            "visited_scene_ids": ["opening-v1", "scene-v1"],
        },
    }
    updated = _extend_manifest_for_module_revision(
        manifest,
        old_module_id="module-v1",
        new_module_id="module-v2",
        old_index=[
            {"scene_id": "opening-v1", "stable_key": "opening"},
            {"scene_id": "scene-v1", "stable_key": "cave"},
        ],
        new_index=[
            {
                "scene_id": "opening-v2",
                "stable_key": "opening",
                "chapter_id": "chapter-v2",
                "chapter": "Chapter",
                "title": "Opening",
            },
            {
                "scene_id": "scene-v2",
                "stable_key": "cave",
                "chapter_id": "chapter-v2",
                "chapter": "Chapter",
                "title": "Cave",
            },
        ],
    )

    assert updated["module_ids"] == ["module-v1", "module-v2"]
    assert updated["current"]["module_id"] == "module-v2"
    assert updated["current"]["scene_id"] == "scene-v2"
    assert updated["traversal"]["visited_scene_ids"] == [
        "opening-v1",
        "scene-v1",
        "opening-v2",
        "scene-v2",
    ]
    assert manifest["module_ids"] == ["module-v1"]


def test_scene_progress_percent_accepts_query_and_mutation_shapes() -> None:
    assert _scene_progress_percent({"percent": 65}) == 65
    assert _scene_progress_percent({"progress": 70}) == 70
    assert _scene_progress_percent(None) == 0


def test_party_projection_keeps_knowledge_bound_to_the_new_actor() -> None:
    sheet = default_character_sheet()
    sheet["progression"]["xp"] = 300
    sheet["combat"]["hp"] = {"value": 7, "max": 10, "temp": 2}
    actor = {
        "id": "replacement-actor",
        "name": "Replacement",
        "sheet": sheet,
    }

    member = _party_member(
        actor,
        {
            "source": "replacement",
            "source_asset_path": "",
        },
    )

    assert member["actor_id"] == "replacement-actor"
    assert member["knowledge_scope_actor_id"] == "replacement-actor"
    assert member["xp"] == 300
    assert member["hit_points"]["current"] == 7


def test_replacement_join_preserves_predecessor_and_only_hands_off_explicit_knowledge() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 15,
        "page_end": 15,
        "heading_path": ["Town", "Inn"],
        "content_sha256": "abc",
    }
    predecessor_sheet = default_character_sheet()
    predecessor_sheet["combat"]["hp"] = {"value": 0, "max": 8, "temp": 0}
    replacement_sheet = default_character_sheet()
    replacement_sheet["combat"]["hp"] = {"value": 8, "max": 8, "temp": 0}
    predecessor = {
        "id": "predecessor",
        "name": "Fallen Wizard",
        "campaign_id": "campaign-1",
        "character_type": "pc",
        "sheet": predecessor_sheet,
        "derived": {"hit_points": {"conditions": ["dead"]}},
    }
    replacement = {
        "id": "replacement",
        "name": "New Wizard",
        "campaign_id": "campaign-1",
        "character_type": "pc",
        "sheet": replacement_sheet,
        "derived": {"hit_points": {"conditions": []}},
    }
    manifest = new_playthrough_manifest(
        run_id="run-1",
        campaign_line_id="line-1",
        module_ids=["module-1"],
        recommended_party_minimum=1,
        recommended_party_maximum=1,
        selected_party_size=1,
        source_refs=[],
    )
    manifest["status"] = "in_progress"
    manifest["current"] = {
        "module_id": "module-1",
        "chapter_id": "chapter-1",
        "chapter_title": "Town",
        "scene_id": "scene-1",
        "scene_title": "Town",
        "objective": "Recruit a replacement.",
    }
    manifest["party"]["members"] = [
        _party_member(
            predecessor,
            {"source": "generated", "source_asset_path": "", "status": "dead"},
        )
    ]

    class Client:
        def __init__(self) -> None:
            self.revision = 10
            self.manifest = validate_playthrough_manifest(manifest)
            self.knowledge = {
                "predecessor": [{"id": "old-knowledge", "knowledge_key": "old.fact"}],
                "replacement": [],
            }
            self.head_snapshot_id = ""

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.revision,
                    "state": {"game_phase": "play"},
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "playthrough_manifest":
                action = arguments["action"]
                if action == "get":
                    return {
                        "manifest": deepcopy(self.manifest),
                        "campaign_revision": self.revision,
                    }
                if action == "replace":
                    self.manifest = deepcopy(arguments["payload"]["manifest"])
                    self.revision += 1
                elif action == "sync":
                    self.revision += 1
                return {
                    "manifest": deepcopy(self.manifest),
                    "campaign_revision": self.revision,
                }
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "The local inn has rooms for rent.",
                    "spatial": {"locations": [{"key": "inn"}]},
                }
            if tool_id == "character_query":
                actor_id = arguments["payload"]["character_id"]
                return deepcopy(
                    predecessor if actor_id == "predecessor" else replacement
                )
            if tool_id == "branch_query":
                return [
                    {
                        "id": "branch-1",
                        "is_current": True,
                        "head_snapshot_id": self.head_snapshot_id,
                    }
                ]
            if tool_id == "actor_knowledge_query":
                return deepcopy(self.knowledge[arguments["actor_id"]])
            if tool_id == "continuity_commit":
                rows = arguments["payload"]["actor_knowledge"]
                assert [item["actor_id"] for item in rows] == [
                    "replacement",
                    "replacement",
                ]
                self.knowledge["replacement"] = [
                    {
                        "id": f"knowledge-{index}",
                        "knowledge_key": item["knowledge_key"],
                    }
                    for index, item in enumerate(rows)
                ]
                self.head_snapshot_id = "snapshot-1"
                self.revision += 1
                return {
                    "event": {"id": "event-join"},
                    "snapshot": {"id": "snapshot-1", "slot": 1},
                }
            if tool_id == "snapshot_create":
                assert arguments["expected_head_snapshot_id"] == "snapshot-1"
                self.head_snapshot_id = "snapshot-2"
                self.revision += 1
                self.manifest["snapshot_dag"] = {
                    "active_branch_id": "branch-1",
                    "head_snapshot_id": "snapshot-2",
                    "nodes": [
                        {
                            "id": "snapshot-2",
                            "parent_id": "snapshot-1",
                            "branch_id": "branch-1",
                            "slot": 2,
                            "label": arguments["label"],
                            "checksum": "b" * 64,
                            "is_head": True,
                        }
                    ],
                }
                return {"id": "snapshot-2", "slot": 2}
            if tool_id == "snapshot_query":
                return {"valid": True}
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _register_replacement(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            predecessor_actor_id="predecessor",
            replacement_actor_id="replacement",
            scene_id="scene-1",
            location_key="inn",
            source_excerpt="The local inn has rooms for rent.",
            source_ref=source_ref,
            summary="New Wizard joined the party at the inn.",
            handoff_knowledge=["Gundren was taken to Cragmaw Castle."],
            witness_actor_ids=["replacement"],
        )
    )

    assert result["predecessor"]["retained"] is True
    assert result["predecessor"]["knowledge_count"] == 1
    assert result["replacement"]["knowledge_scope_actor_id"] == "replacement"
    assert client.manifest["party"]["members"][0]["actor_id"] == "replacement"
    assert client.manifest["party"]["replacements"] == [
        {
            "predecessor_actor_id": "predecessor",
            "replacement_actor_id": "replacement",
            "handoff_event_id": "event-join",
        }
    ]
    assert result["checkpoint"]["snapshot"]["slot"] == 2


def test_phase_and_idempotency_namespaces_are_stable() -> None:
    assert _campaign_phase({"state": {}}) == "lobby"
    assert _campaign_phase({"state": {"game_phase": "combat"}}) == "combat"
    assert _phase_groups("lobby") == ("lobby.campaign",)
    assert _phase_groups("play") == ("play.scene_control", "play.scene")
    assert _phase_groups("combat") == ("combat.save", "combat.observe")
    assert _mutation_key("run", "snapshot", "scene-1") == _mutation_key(
        "run", "snapshot", "scene-1"
    )
    assert _mutation_key("run", "snapshot", "scene-1") != _mutation_key(
        "run", "snapshot", "scene-2"
    )


def test_party_report_supplies_exact_manifest_members(tmp_path) -> None:
    report_path = tmp_path / "party.json"
    members = [
        {
            "actor_id": "actor-1",
            "source": "generated",
            "source_asset_path": "",
            "status": "active",
        }
    ]
    report_path.write_text(json.dumps({"manifest_members": members}), encoding="utf-8")
    args = argparse.Namespace(party_member_json=[], party_report=report_path)

    assert _party_selections(args) == members


def test_advancement_configuration_uses_public_campaign_change() -> None:
    class Client:
        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": 7}}

        async def domain(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_change"
            assert arguments["action"] == "advancement_configure"
            assert arguments["payload"] == {"mode": "xp"}
            assert arguments["expected_revision"] == 7
            return {"advancement": {"mode": "xp"}}

    result = asyncio.run(
        _configure_advancement(
            Client(),
            campaign_id="campaign-1",
            run_id="run-1",
            mode="xp",
            initial_phase="lobby",
        )
    )

    assert result["configured"]["advancement"]["mode"] == "xp"
    assert result["phase_changes"] == []


def test_level_advancement_exhausts_public_follow_up_and_restores_play() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-xp",
        "page_start": 12,
        "page_end": 13,
        "heading_path": ["Experience Points"],
        "content_sha256": "abc123",
    }
    sheet = default_character_sheet()
    sheet["progression"].update(
        {
            "level": 1,
            "classes": [
                {
                    "name": "Bard",
                    "level": 1,
                    "subclass": "",
                    "hit_die": 8,
                }
            ],
        }
    )

    class Client:
        def __init__(self) -> None:
            self.phase = "play"
            self.campaign_revision = 10
            self.actor = {
                "id": "bard-1",
                "name": "Song",
                "campaign_id": "campaign-1",
                "revision": 3,
                "sheet": deepcopy(sheet),
            }
            self.calls: list[str] = []

        async def open(self, campaign_id: str):
            assert campaign_id == "campaign-1"
            return {"exposure_id": "exposure"}

        async def load(self, *_group_ids: str):
            return None

        async def core(self, tool_id: str, arguments: dict):
            self.calls.append(tool_id)
            if tool_id == "campaign_query":
                return {
                    "result": {
                        "id": "campaign-1",
                        "revision": self.campaign_revision,
                        "state": {"game_phase": self.phase},
                    }
                }
            if tool_id == "game_phase":
                self.phase = arguments["tool_profile"]
                self.campaign_revision += 1
                return {"result": {"tool_profile": self.phase}}
            raise AssertionError((tool_id, arguments))

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append(tool_id)
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "The characters divide XP evenly.",
                }
            if tool_id == "character_query":
                return deepcopy(self.actor)
            if tool_id == "branch_query":
                return [
                    {
                        "id": "branch-1",
                        "is_current": True,
                        "head_snapshot_id": "snapshot-1",
                    }
                ]
            if tool_id == "character_state_change":
                assert self.phase == "lobby"
                assert arguments["action"] == "level_advance"
                assert arguments["payload"]["source_ref"].endswith("sha256:abc123")
                self.actor["sheet"]["progression"]["level"] = 2
                self.actor["sheet"]["progression"]["classes"][0]["level"] = 2
                self.actor["revision"] += 1
                return {
                    "status": "committed",
                    "character": deepcopy(self.actor),
                    "advancement": {
                        "follow_up": {
                            "feature_artifacts": [
                                {
                                    "artifact_id": "feature-jack",
                                    "name": "Jack of All Trades",
                                    "selection_requirements": {},
                                }
                            ],
                            "subclass_options": [],
                            "spell_choices": {
                                "cantrips_to_add": 0,
                                "leveled_spells_to_add": 1,
                            },
                            "prepared_spell_event": None,
                        }
                    },
                }
            if tool_id == "rule_pack_query":
                kind = arguments["payload"]["kind"]
                if kind == "feature":
                    return [
                        {
                            "id": "feature-jack",
                            "name": "Jack of All Trades",
                            "selection_requirements": {
                                "class_name": "Bard",
                                "subclass_name": "",
                                "minimum_level": 2,
                            },
                        }
                    ]
                return [
                    {
                        "id": "spell-heroism",
                        "name": "Heroism",
                        "selection_requirements": {
                            "level": 1,
                            "eligible_classes": ["Bard"],
                        },
                    }
                ]
            if tool_id == "character_content_apply":
                artifact_id = arguments["artifact_id"]
                if artifact_id == "feature-jack":
                    self.actor["sheet"]["content"]["features"].append({"id": artifact_id})
                else:
                    assert arguments["selection"] == {
                        "source_class": "Bard",
                        "method": "known",
                    }
                    self.actor["sheet"]["content"]["spells"].append({"id": artifact_id})
                self.actor["revision"] += 1
                return deepcopy(self.actor)
            if tool_id == "playthrough_manifest" and arguments["action"] == "sync":
                self.campaign_revision += 1
                return {
                    "campaign_revision": self.campaign_revision,
                    "manifest": {"status": "in_progress"},
                }
            if tool_id == "snapshot_create":
                return {"id": "snapshot-2", "slot": 2}
            if tool_id == "snapshot_query":
                return {"valid": True}
            if tool_id == "playthrough_manifest" and arguments["action"] == "get":
                return {
                    "manifest": {
                        "status": "in_progress",
                        "snapshot_dag": {
                            "active_branch_id": "branch-1",
                            "head_snapshot_id": "snapshot-2",
                            "nodes": [
                                {
                                    "id": "snapshot-2",
                                    "branch_id": "branch-1",
                                }
                            ],
                        },
                    }
                }
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _advance_level(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            initial_phase="play",
            return_phase="play",
            scene_id="scene-1",
            source_ref=source_ref,
            actor_id="bard-1",
            target_level=2,
            class_name="Bard",
            hp_method="fixed",
            reason="earned the module's opening XP threshold",
            subclass_artifact_id="",
            feature_selection_values=[],
            spell_selection_values=[
                {
                    "artifact_id": "spell-heroism",
                    "source_class": "Bard",
                    "method": "known",
                }
            ],
            prepared_spell_ids=[],
            checkpoint_label="Bard reaches level 2",
        )
    )

    assert client.phase == "play"
    assert result["actor"]["sheet"]["progression"]["level"] == 2
    assert result["applied_features"] == [{"artifact_id": "feature-jack", "selection": {}}]
    assert result["applied_spells"] == ["spell-heroism"]
    assert result["checkpoint"]["verification"] == {"valid": True}
    assert client.calls.count("game_phase") == 2
    assert "character_state_change" in client.calls
    assert "character_content_apply" in client.calls


def test_level_advancement_rejects_malformed_choices_before_public_mutation() -> None:
    class Client:
        async def load(self, *_group_ids: str):
            raise AssertionError("malformed choices must fail before loading tools")

    with pytest.raises(ValueError, match="only artifact_id and selection"):
        asyncio.run(
            _advance_level(
                Client(),
                campaign_id="campaign-1",
                run_id="run-1",
                initial_phase="play",
                return_phase="play",
                scene_id="scene-1",
                source_ref=_manifest_source_ref(),
                actor_id="actor-1",
                target_level=2,
                class_name="Fighter",
                hp_method="fixed",
                reason="earned enough XP",
                subclass_artifact_id="",
                feature_selection_values=[
                    {
                        "artifact_id": "feature-1",
                        "selection": {},
                        "unexpected": True,
                    }
                ],
                spell_selection_values=[],
                prepared_spell_ids=[],
                checkpoint_label="",
            )
        )


def test_checkpoint_uses_only_public_manifest_branch_and_snapshot_tools() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": 8}}

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append((tool_id, arguments))
            if tool_id == "playthrough_manifest" and arguments["action"] == "sync":
                return {"campaign_revision": 9, "manifest": {"status": "in_progress"}}
            if tool_id == "branch_query":
                return [
                    {
                        "id": "branch-1",
                        "is_current": True,
                        "head_snapshot_id": "snapshot-1",
                    }
                ]
            if tool_id == "snapshot_create":
                return {"id": "snapshot-2", "slot": 2}
            if tool_id == "snapshot_query":
                return {"valid": True}
            if tool_id == "playthrough_manifest" and arguments["action"] == "get":
                return {"manifest": {"status": "in_progress"}}
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _checkpoint(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            label="Scene checkpoint",
        )
    )

    assert result["verification"] == {"valid": True}
    assert result["snapshot"]["id"] == "snapshot-2"
    assert [name for name, _ in client.calls] == [
        "playthrough_manifest",
        "branch_query",
        "snapshot_create",
        "snapshot_query",
        "playthrough_manifest",
    ]


def test_checkpoint_recovers_verified_same_branch_snapshot_after_retry_revision_change() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": 9}}

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append((tool_id, arguments))
            if tool_id == "playthrough_manifest" and arguments["action"] == "sync":
                return {"campaign_revision": 10, "manifest": {"status": "in_progress"}}
            if tool_id == "branch_query":
                return [
                    {
                        "id": "branch-1",
                        "is_current": True,
                        "head_snapshot_id": "snapshot-2",
                    }
                ]
            if tool_id == "snapshot_create":
                raise RuntimeError(
                    "idempotency key reused with a different request: checkpoint-key"
                )
            if tool_id == "snapshot_query" and arguments["view"] == "list":
                return [
                    {
                        "id": "snapshot-2",
                        "branch_id": "branch-1",
                        "slot": 2,
                        "label": "Scene checkpoint",
                    }
                ]
            if tool_id == "snapshot_query" and arguments["view"] == "verify":
                return {"valid": True, "slot": 2}
            if tool_id == "playthrough_manifest" and arguments["action"] == "get":
                return {
                    "manifest": {
                        "status": "in_progress",
                        "snapshot_dag": {
                            "active_branch_id": "branch-1",
                            "head_snapshot_id": "snapshot-2",
                            "nodes": [
                                {
                                    "id": "snapshot-2",
                                    "branch_id": "branch-1",
                                }
                            ],
                        },
                    }
                }
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _checkpoint(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            label="Scene checkpoint",
        )
    )

    assert result["reused"] is True
    assert result["snapshot"]["id"] == "snapshot-2"
    assert result["verification"] == {"valid": True, "slot": 2}
    assert [name for name, _ in client.calls] == [
        "playthrough_manifest",
        "branch_query",
        "snapshot_create",
        "snapshot_query",
        "snapshot_query",
        "playthrough_manifest",
    ]


def test_failed_route_is_preserved_when_branching_from_verified_snapshot() -> None:
    class Client:
        def __init__(self) -> None:
            self.phase = "play"
            self.revision = 30
            self.current_branch = "failed-branch"
            self.source_saved = False
            self.loads: list[tuple[str, ...]] = []

        async def open(self, campaign_id: str):
            assert campaign_id == "campaign-1"
            return {"exposure_id": "exposure"}

        async def load(self, *group_ids: str):
            self.loads.append(group_ids)

        async def core(self, tool_id: str, arguments: dict):
            if tool_id == "campaign_query":
                return {
                    "result": {
                        "id": "campaign-1",
                        "revision": self.revision,
                        "state": {"game_phase": self.phase},
                    }
                }
            if tool_id == "game_phase":
                assert arguments["tool_profile"] == "lobby"
                assert arguments["branch_id"] == "failed-branch"
                self.phase = "lobby"
                self.revision += 1
                return {"result": {"game_phase": "lobby"}}
            raise AssertionError((tool_id, arguments))

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "snapshot_query" and arguments["view"] == "list":
                return [{"id": "snapshot-58", "slot": 58, "branch_id": "failed-branch"}]
            if tool_id == "snapshot_query" and arguments["view"] == "verify":
                return {"valid": True}
            if tool_id == "snapshot_query" and arguments["view"] == "core":
                return {
                    "core_pack": {"fingerprint": "current"},
                    "available_core_pack": {"fingerprint": "current"},
                    "conversion_required": False,
                }
            if tool_id == "branch_query":
                return [
                    {
                        "id": self.current_branch,
                        "is_current": True,
                        "head_snapshot_id": (
                            ("snapshot-60" if self.source_saved else "snapshot-59")
                            if self.current_branch == "failed-branch"
                            else "snapshot-58"
                        ),
                    }
                ]
            if tool_id == "branch_change":
                assert arguments["payload"] == {
                    "name": "main-after-klarg-defeat",
                    "from_snapshot_id": "snapshot-58",
                    "checkout": True,
                }
                assert arguments["expected_branch_id"] == "failed-branch"
                self.current_branch = "recovery-branch"
                self.phase = "play"
                return {
                    "id": "recovery-branch",
                    "head_snapshot_id": "snapshot-58",
                    "snapshot": {"id": "snapshot-58", "slot": 58},
                }
            if tool_id == "playthrough_manifest" and arguments["action"] == "sync":
                return {"manifest": {"status": "in_progress"}, "campaign_revision": 31}
            if tool_id == "snapshot_create":
                if self.current_branch == "failed-branch":
                    assert arguments["expected_head_snapshot_id"] == "snapshot-59"
                    self.source_saved = True
                    return {"id": "snapshot-60", "slot": 60}
                assert arguments["expected_head_snapshot_id"] == "snapshot-58"
                return {"id": "snapshot-61", "slot": 61}
            if tool_id == "playthrough_manifest" and arguments["action"] == "get":
                return {"manifest": {"status": "in_progress"}}
            raise AssertionError((tool_id, arguments))

    result = asyncio.run(
        _branch_from_snapshot(
            Client(),
            campaign_id="campaign-1",
            run_id="run-1",
            initial_phase="play",
            snapshot_slot=58,
            branch_name="main-after-klarg-defeat",
            checkpoint_label="Continue from pre-combat state",
        )
    )

    assert result["source_branch"]["id"] == "failed-branch"
    assert result["source_head_snapshot_id"] == "snapshot-59"
    assert result["source_checkpoint"]["snapshot"]["slot"] == 60
    assert result["created_branch"]["id"] == "recovery-branch"
    assert result["checkpoint"]["snapshot"]["slot"] == 61


@pytest.mark.parametrize("defer_checkpoint", [False, True])
def test_source_cited_check_persists_result_and_explicit_knowledge(
    defer_checkpoint: bool,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 7,
        "page_end": 7,
        "heading_path": ["Goblin Trail"],
        "content_sha256": "abc",
    }
    expected_identity = _check_identity(
        scene_id="scene-1",
        location_key="ambush",
        kind="ability",
        ability="survival",
        actor_id="actor-1",
        dc=10,
        proficient=True,
        advantage=False,
        disadvantage=True,
        source_ref=source_ref,
    )

    class Client:
        def __init__(self) -> None:
            self.revision = 4

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": self.revision}}

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query" and arguments["view"] == "scene":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "A DC 10 Wisdom (Survival) check reveals the trail.",
                    "locations": [{"key": "ambush"}],
                }
            if tool_id == "module_query" and arguments["view"] == "progress":
                return []
            if tool_id == "module_set_progress":
                assert arguments["idempotency_key"] == _mutation_key(
                    "run-1", "scene-progress", expected_identity
                )
                return {"state_version": 1}
            if tool_id == "character_query":
                return {
                    "id": arguments["payload"]["character_id"],
                    "name": "Scout",
                    "campaign_id": "campaign-1",
                    "revision": 2,
                }
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "character_check":
                assert arguments["kind"] == "ability"
                assert arguments["ability"] == "survival"
                assert arguments["advantage"] is False
                assert arguments["disadvantage"] is True
                assert arguments["idempotency_key"] == _mutation_key(
                    "run-1", "character-check", expected_identity
                )
                self.revision += 1
                return {"status": "committed", "result": {"success": True, "total": 14}}
            if tool_id == "continuity_commit":
                assert [item["actor_id"] for item in arguments["payload"]["actor_knowledge"]] == [
                    "actor-1",
                    "actor-2",
                ]
                assert all(
                    item["proposition"] == "The trail shows twelve goblins and two captives."
                    for item in arguments["payload"]["actor_knowledge"]
                )
                assert all(
                    item["knowledge_key"]
                    == _check_knowledge_key(
                        "run-1",
                        "scene-1",
                        "ambush",
                        "ability",
                        "survival",
                        "actor-1",
                        10,
                        True,
                        False,
                        True,
                        source_ref,
                    )
                    for item in arguments["payload"]["actor_knowledge"]
                )
                assert arguments["payload"]["event"]["payload"]["source_ref"] == source_ref
                assert arguments["idempotency_key"] == _mutation_key(
                    "run-1", "continuity", expected_identity
                )
                if defer_checkpoint:
                    assert "snapshot" not in arguments["payload"]
                else:
                    assert arguments["payload"]["snapshot"]["label"].startswith(
                        "Full playthrough check:"
                    )
                self.revision += 1
                return {
                    "event": {"id": "event-1"},
                    **({} if defer_checkpoint else {"snapshot": {"slot": 3}}),
                }
            if tool_id == "playthrough_manifest":
                assert arguments["action"] == "sync"
                assert arguments["idempotency_key"] == _mutation_key(
                    "run-1", "sync", f"resolve-check-sync:{expected_identity}"
                )
                return {"manifest": {"status": "in_progress"}, "campaign_revision": 7}
            raise AssertionError((tool_id, arguments))

    result = asyncio.run(
        _resolve_check(
            Client(),
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            location_key="ambush",
            source_excerpt="A DC 10 Wisdom (Survival) check reveals the trail.",
            source_ref=source_ref,
            actor_id="actor-1",
            kind="ability",
            ability="survival",
            dc=10,
            proficient=True,
            disadvantage=True,
            knowledge_actor_ids=["actor-2"],
            success_knowledge="The trail shows twelve goblins and two captives.",
            failure_knowledge="The trail's traffic remains unclear.",
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert result["check"] == {"success": True, "total": 14}
    assert result["knowledge_actor_ids"] == ["actor-1", "actor-2"]
    assert result["sync"]["campaign_revision"] == 7
    assert _check_knowledge_key(
        "run-1",
        "scene-1",
        "ambush",
        "ability",
        "survival",
        "actor-1",
        10,
        True,
        False,
        True,
        source_ref,
    ) != _check_knowledge_key(
        "run-1",
        "scene-1",
        "ambush",
        "ability",
        "perception",
        "actor-1",
        10,
        True,
        False,
        True,
        source_ref,
    )


def test_check_identity_separates_same_scene_checks_by_location_dc_and_source() -> None:
    base = {
        "scene_id": "scene-1",
        "location_key": "6-armory",
        "kind": "ability",
        "ability": "dexterity",
        "actor_id": "rogue-1",
        "dc": 10,
        "proficient": True,
        "advantage": False,
        "disadvantage": False,
        "source_ref": {"chunk_id": "armory-lock"},
    }
    identity = _check_identity(**base)

    assert identity != _check_identity(**{**base, "location_key": "5-slave-pens"})
    assert identity != _check_identity(**{**base, "dc": 22})
    assert identity != _check_identity(
        **{**base, "source_ref": {"chunk_id": "slave-pens-lock"}}
    )
    assert identity != _check_identity(**{**base, "proficient": False})


def test_source_cited_check_rejects_unsupported_kind_before_tools() -> None:
    with pytest.raises(ValueError, match="not supported"):
        asyncio.run(
            _resolve_check(
                object(),
                campaign_id="campaign-1",
                run_id="run-1",
                scene_id="scene-1",
                location_key="ambush",
                source_excerpt="Source",
                source_ref={},
                actor_id="actor-1",
                kind="survival",
                ability="wisdom",
                dc=10,
                proficient=True,
                knowledge_actor_ids=[],
                success_knowledge="",
                failure_knowledge="",
            )
        )


def test_character_check_accepts_full_and_compact_exposure_shapes() -> None:
    result = {"success": False, "total": 7, "natural": 4}

    assert _committed_check_result({"status": "committed", "result": result}) == result
    assert _committed_check_result(result) == result
    with pytest.raises(RuntimeError, match="did not commit"):
        _committed_check_result({"status": "pending_ruling"})


def test_check_recovery_identity_includes_actor_and_roll_mode() -> None:
    source_ref = {"chunk_id": "chunk-1"}
    progress = {
        "current_location_key": "bridge",
        "state": {
            "full_playthrough_check": {
                "run_id": "run-1",
                "actor_id": "fighter",
                "kind": "ability",
                "ability": "stealth",
                "dc": 9,
                "proficient": True,
                "advantage": False,
                "disadvantage": True,
                "source_ref": source_ref,
            }
        },
    }

    assert _matching_check_progress(
        progress,
        run_id="run-1",
        location_key="bridge",
        actor_id="fighter",
        kind="ability",
        ability="stealth",
        dc=9,
        proficient=True,
        advantage=False,
        disadvantage=True,
        source_ref=source_ref,
    )
    assert not _matching_check_progress(
        progress,
        run_id="run-1",
        location_key="bridge",
        actor_id="rogue",
        kind="ability",
        ability="stealth",
        dc=9,
        proficient=True,
        advantage=False,
        disadvantage=True,
        source_ref=source_ref,
    )
    assert not _matching_check_progress(
        progress,
        run_id="run-1",
        location_key="bridge",
        actor_id="fighter",
        kind="ability",
        ability="stealth",
        dc=9,
        proficient=True,
        advantage=False,
        disadvantage=False,
        source_ref=source_ref,
    )


@pytest.mark.parametrize(("half_damage", "expected_amount"), [(False, 4), (True, 2)])
def test_source_damage_rolls_then_damages_and_knocks_prone_through_public_tools(
    half_damage: bool,
    expected_amount: int,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 8,
        "page_end": 9,
        "heading_path": ["3. KENNEL"],
        "content_sha256": "abc",
    }

    class Client:
        def __init__(self) -> None:
            self.campaign_revision = 10
            self.character_revision = 3
            self.calls: list[str] = []

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.campaign_revision,
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append(tool_id)
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "On a result of 5 or less, the character falls.",
                    "locations": [{"key": "3-kennel"}],
                }
            if tool_id == "character_query":
                return {
                    "id": "actor-1",
                    "name": "Scout",
                    "campaign_id": "campaign-1",
                    "revision": self.character_revision,
                }
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "dnd_dice_roll":
                assert arguments["expression"] == "1d6"
                assert arguments["expected_campaign_revision"] == 10
                self.campaign_revision += 1
                return {"status": "committed", "result": {"total": 4, "rolls": [4]}}
            if tool_id == "character_state_change" and arguments["action"] == "damage":
                assert arguments["payload"] == {
                    "parts": [{"amount": expected_amount, "damage_type": "bludgeoning"}]
                }
                assert arguments["expected_revision"] == 3
                self.campaign_revision += 1
                self.character_revision += 1
                sheet = default_character_sheet()
                sheet["combat"]["hp"] = {
                    "value": 10 - expected_amount,
                    "max": 10,
                    "temp": 0,
                }
                return {
                    "character": {
                        "id": "actor-1",
                        "revision": self.character_revision,
                        "sheet": sheet,
                    },
                    "result": {"after_hp": 10 - expected_amount},
                }
            if tool_id == "character_state_change" and arguments["action"] == "knock_prone":
                assert arguments["expected_revision"] == 4
                self.campaign_revision += 1
                self.character_revision += 1
                sheet = default_character_sheet()
                sheet["combat"]["hp"] = {
                    "value": 10 - expected_amount,
                    "max": 10,
                    "temp": 0,
                }
                sheet["conditions"] = ["prone"]
                return {
                    "character": {
                        "id": "actor-1",
                        "revision": self.character_revision,
                        "sheet": sheet,
                    },
                    "status": "knocked_prone",
                }
            if tool_id == "continuity_commit":
                event = arguments["payload"]["event"]
                assert event["payload"]["amount"] == expected_amount
                assert event["payload"]["damage_roll"]["total"] == 4
                assert event["payload"]["half_damage"] is half_damage
                assert event["payload"]["source_ref"] == source_ref
                self.campaign_revision += 1
                return {"event": {"id": "event-1"}, "snapshot": {"slot": 2}}
            if tool_id == "playthrough_manifest":
                assert arguments["action"] == "sync"
                return {
                    "manifest": {"status": "in_progress"},
                    "campaign_revision": self.campaign_revision,
                }
            raise AssertionError((tool_id, arguments))

    result = asyncio.run(
        _apply_source_damage(
            Client(),
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            location_key="3-kennel",
            source_excerpt="On a result of 5 or less, the character falls.",
            source_ref=source_ref,
            actor_id="actor-1",
            expression="1d6",
            damage_type="bludgeoning",
            reason="falling 10 feet in the chimney",
            half_damage=half_damage,
            knock_prone=True,
            knowledge_actor_ids=["actor-2"],
        )
    )

    assert result["damage"]["result"]["after_hp"] == 10 - expected_amount
    assert result["prone"]["status"] == "knocked_prone"
    assert result["character"]["sheet"]["conditions"] == ["prone"]
    assert result["knowledge_actor_ids"] == ["actor-1", "actor-2"]


@pytest.mark.parametrize("defer_checkpoint", [False, True])
def test_source_event_stand_uses_validated_public_character_action(
    defer_checkpoint: bool,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 8,
        "page_end": 9,
        "heading_path": ["3. KENNEL"],
        "content_sha256": "abc",
    }

    class Client:
        def __init__(self) -> None:
            self.revision = 20
            self.keys: dict[str, str] = {}

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": self.revision}}

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "The character lands prone at the base of the shaft.",
                    "locations": [{"key": "3-kennel"}],
                }
            if tool_id == "character_query":
                return {
                    "id": "actor-1",
                    "name": "Scout",
                    "campaign_id": "campaign-1",
                    "revision": 4,
                }
            if tool_id == "character_state_change":
                assert arguments["action"] == "stand"
                assert arguments["expected_revision"] == 4
                self.keys["stand"] = arguments["idempotency_key"]
                self.revision += 1
                return {"status": "stood", "character": {"revision": 5}}
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "continuity_commit":
                assert arguments["payload"]["event"]["payload"]["source_ref"] == source_ref
                assert ("snapshot" in arguments["payload"]) is not defer_checkpoint
                self.keys["continuity"] = arguments["idempotency_key"]
                self.revision += 1
                return {
                    "event": {"id": "event-1"},
                    **({} if defer_checkpoint else {"snapshot": {"slot": 3}}),
                }
            if tool_id == "playthrough_manifest":
                self.keys["sync"] = arguments["idempotency_key"]
                return {
                    "manifest": {"status": "in_progress"},
                    "campaign_revision": self.revision,
                }
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _stand_after_source_event(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            location_key="3-kennel",
            source_excerpt="The character lands prone at the base of the shaft.",
            source_ref=source_ref,
            actor_id="actor-1",
            knowledge_actor_ids=["actor-2"],
            reason="Scout stood after recovering from the source-cited fall.",
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert result["stand"]["status"] == "stood"
    assert result["knowledge_actor_ids"] == ["actor-1", "actor-2"]
    identity = _stand_identity(
        scene_id="scene-1",
        location_key="3-kennel",
        actor_id="actor-1",
        reason="Scout stood after recovering from the source-cited fall.",
        source_ref=source_ref,
    )
    assert client.keys == {
        "stand": _mutation_key("run-1", "source-event-stand", identity),
        "continuity": _mutation_key(
            "run-1", "source-event-stand-continuity", identity
        ),
        "sync": _mutation_key(
            "run-1", "sync", f"source-event-stand-sync:{identity}"
        ),
    }


def test_stand_identity_separates_later_occurrence_for_same_actor_and_scene() -> None:
    first = _stand_identity(
        scene_id="scene-1",
        location_key="room-1",
        actor_id="actor-1",
        reason="Actor One stood after the first fall.",
        source_ref=None,
    )

    assert first == _stand_identity(
        scene_id="scene-1",
        location_key="room-1",
        actor_id="actor-1",
        reason="Actor One stood after the first fall.",
        source_ref=None,
    )
    assert first != _stand_identity(
        scene_id="scene-1",
        location_key="room-2",
        actor_id="actor-1",
        reason="Actor One stood after a later fall.",
        source_ref=None,
    )


def test_short_rest_advances_clock_and_applies_only_explicit_resource_choices() -> None:
    class Client:
        def __init__(self) -> None:
            self.revision = 5
            self.world_time: dict = {}
            self.keys: dict[str, list[str]] = {}

        def remember(self, kind: str, key: str) -> None:
            self.keys.setdefault(kind, []).append(key)

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.revision,
                    "state": {"game_phase": "play", "world_time": self.world_time},
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "character_query":
                actor_id = arguments["payload"]["character_id"]
                if arguments["view"] == "rest":
                    if actor_id == "fighter":
                        assert arguments["payload"]["hit_dice_spends"] == [
                            {"key": "fighter:d10", "count": 1}
                        ]
                    if actor_id == "wizard":
                        assert arguments["payload"]["arcane_recovery"] == {"1": 1}
                    return {"ready": True, "character_id": actor_id}
                return {
                    "id": actor_id,
                    "campaign_id": "campaign-1",
                    "revision": 2,
                }
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "campaign_change" and arguments["action"] == "clock_set":
                self.remember("clock_set", arguments["idempotency_key"])
                assert arguments["payload"]["day"] == 1
                self.world_time = {
                    "day": 1,
                    "hour": 14,
                    "minute": 0,
                    "elapsed_minutes": 840,
                    "label": "Hideout",
                }
                self.revision += 1
                return {"world_time": self.world_time}
            if tool_id == "campaign_change" and arguments["action"] == "clock_advance":
                self.remember("clock_advance", arguments["idempotency_key"])
                assert arguments["payload"] == {"period": "minute", "count": 60}
                self.world_time = {
                    **self.world_time,
                    "hour": 15,
                    "elapsed_minutes": 900,
                }
                self.revision += 1
                return {"world_time": self.world_time}
            if tool_id == "character_state_change":
                self.remember("actor", arguments["idempotency_key"])
                assert arguments["action"] == "rest"
                if arguments["character_id"] == "fighter":
                    assert arguments["payload"]["hit_dice_spends"] == [
                        {"key": "fighter:d10", "count": 1}
                    ]
                else:
                    assert "hit_dice_spends" not in arguments["payload"]
                if arguments["character_id"] == "wizard":
                    assert arguments["payload"]["arcane_recovery"] == {"1": 1}
                else:
                    assert "arcane_recovery" not in arguments["payload"]
                self.revision += 1
                return {
                    "status": "committed",
                    "character": {"id": arguments["character_id"]},
                }
            if tool_id == "continuity_commit":
                self.remember("continuity", arguments["idempotency_key"])
                assert arguments["payload"]["event"]["payload"]["duration_minutes"] == 60
                self.revision += 1
                return {"event": {"id": "event-1"}, "snapshot": {"slot": 4}}
            if tool_id == "playthrough_manifest":
                self.remember("sync", arguments["idempotency_key"])
                return {
                    "manifest": {"status": "in_progress"},
                    "campaign_revision": self.revision,
                }
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _short_rest(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            members=[
                {
                    "actor_id": "fighter",
                    "hit_dice_spends": [{"key": "fighter:d10", "count": 1}],
                },
                {"actor_id": "wizard", "arcane_recovery": {"1": 1}},
            ],
            start_clock={"day": 1, "hour": 14, "label": "Hideout"},
            duration_minutes=60,
            reason="The party regrouped outside the flooded passage.",
        )
    )

    assert result["member_ids"] == ["fighter", "wizard"]
    assert result["clock_advanced"]["world_time"]["hour"] == 15
    assert len(result["rests"]) == 2
    normalized = [
        {
            "actor_id": "fighter",
            "arcane_recovery": {},
            "hit_dice_spends": [{"key": "fighter:d10", "count": 1}],
        },
        {
            "actor_id": "wizard",
            "arcane_recovery": {"1": 1},
            "hit_dice_spends": [],
        },
    ]
    identity = _short_rest_identity(
        normalized,
        duration_minutes=60,
        reason="The party regrouped outside the flooded passage.",
    )
    assert client.keys["clock_set"] == [
        _mutation_key("run-1", "short-rest-clock-set", identity)
    ]
    assert client.keys["clock_advance"] == [
        _mutation_key("run-1", "short-rest-clock-advance", identity)
    ]
    assert client.keys["actor"] == [
        _mutation_key("run-1", "short-rest-actor", f"{identity}:fighter"),
        _mutation_key("run-1", "short-rest-actor", f"{identity}:wizard"),
    ]
    assert client.keys["continuity"] == [
        _mutation_key("run-1", "short-rest-continuity", identity)
    ]
    assert client.keys["sync"] == [
        _mutation_key("run-1", "sync", f"short-rest-sync:{identity}")
    ]


def test_short_rest_identity_separates_later_rest_choices() -> None:
    members = [
        {
            "actor_id": "fighter",
            "arcane_recovery": {},
            "hit_dice_spends": [{"key": "d10", "count": 1}],
        }
    ]
    first = _short_rest_identity(
        members,
        duration_minutes=60,
        reason="First rest.",
    )
    assert first == _short_rest_identity(
        deepcopy(members),
        duration_minutes=60,
        reason="First rest.",
    )
    assert first != _short_rest_identity(
        members,
        duration_minutes=60,
        reason="Later rest.",
    )
    changed = deepcopy(members)
    changed[0]["hit_dice_spends"][0]["count"] = 2
    assert first != _short_rest_identity(
        changed,
        duration_minutes=60,
        reason="First rest.",
    )


@pytest.mark.parametrize("defer_checkpoint", [False, True])
def test_source_bound_time_advance_commits_clock_knowledge_and_snapshot(
    defer_checkpoint: bool,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 14,
        "page_end": 14,
        "heading_path": ["Part 2"],
        "content_sha256": "abc",
    }

    class Client:
        def __init__(self) -> None:
            self.revision = 4
            self.world_time = {
                "day": 2,
                "hour": 4,
                "minute": 0,
                "elapsed_minutes": 1680,
                "label": "Trail",
            }

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.revision,
                    "state": {
                        "game_phase": "play",
                        "world_time": deepcopy(self.world_time),
                    },
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "The characters arrive late in the day.",
                }
            if tool_id == "character_query":
                return {
                    "id": arguments["payload"]["character_id"],
                    "campaign_id": "campaign-1",
                }
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "campaign_change":
                assert arguments["action"] == "clock_advance"
                assert arguments["payload"] == {"period": "hour", "count": 13}
                self.world_time = {
                    "day": 2,
                    "hour": 17,
                    "minute": 0,
                    "elapsed_minutes": 2460,
                    "label": "Trail",
                }
                self.revision += 1
                return {"world_time": deepcopy(self.world_time)}
            if tool_id == "continuity_commit":
                payload = arguments["payload"]
                assert payload["event"]["payload"]["source_ref"] == source_ref
                assert payload["event"]["payload"]["elapsed_minutes"] == 780
                assert [item["actor_id"] for item in payload["actor_knowledge"]] == [
                    "actor-1",
                    "npc-1",
                ]
                if defer_checkpoint:
                    assert "snapshot" not in payload
                else:
                    assert payload["snapshot"]["label"].startswith(
                        "Full playthrough time advance:"
                    )
                self.revision += 1
                return {
                    "event": {"id": "event-1"},
                    **({} if defer_checkpoint else {"snapshot": {"slot": 5}}),
                }
            if tool_id == "playthrough_manifest":
                assert arguments["action"] == "sync"
                self.revision += 1
                return {
                    "manifest": {"status": "in_progress"},
                    "campaign_revision": self.revision,
                }
            raise AssertionError((tool_id, arguments))

    result = asyncio.run(
        _advance_time(
            Client(),
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            source_excerpt="The characters arrive late in the day.",
            source_ref=source_ref,
            period="hour",
            count=13,
            reason="The party traveled with Sildar and arrived late in the day.",
            start_clock=None,
            knowledge_actor_ids=["actor-1", "npc-1"],
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert result["after"]["hour"] == 17
    assert result["knowledge_actor_ids"] == ["actor-1", "npc-1"]
    if defer_checkpoint:
        assert "snapshot" not in result["continuity"]
    else:
        assert result["continuity"]["snapshot"]["slot"] == 5


def test_play_activity_records_structured_effect_and_random_receipt() -> None:
    receipt = {
        "operation": "character_action",
        "position_before": 10,
        "position_after": 11,
    }

    class Client:
        revision = 8

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.revision,
                    "state": {"game_phase": "play"},
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "scene_id": "scene-1",
                    "locations": [{"key": "6-goblin-den"}],
                }
            if tool_id == "character_query":
                return {
                    "id": "fighter",
                    "name": "Fighter",
                    "campaign_id": "campaign-1",
                    "revision": 3,
                }
            if tool_id == "character_action":
                assert arguments["action"] == "use_activity"
                assert arguments["payload"] == {"activity_id": "fighter-second-wind"}
                self.revision += 1
                return {
                    "status": "committed",
                    "result": {
                        "core_effect": {
                            "kind": "second_wind",
                            "before_hp": 2,
                            "after_hp": 10,
                        }
                    },
                    "random_stream_receipt": receipt,
                }
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "continuity_commit":
                payload = arguments["payload"]["event"]["payload"]
                assert payload["core_effect"]["kind"] == "second_wind"
                assert payload["random_stream_receipt"] == receipt
                self.revision += 1
                return {"event": {"id": "event-1"}, "snapshot": {"slot": 6}}
            if tool_id == "playthrough_manifest":
                return {
                    "manifest": {"status": "in_progress"},
                    "campaign_revision": self.revision,
                }
            raise AssertionError((tool_id, arguments))

    result = asyncio.run(
        _use_activity(
            Client(),
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            location_key="6-goblin-den",
            actor_id="fighter",
            activity_id="fighter-second-wind",
            declaration=None,
            reason="The fighter used Second Wind before pursuing the hostage bargain.",
            knowledge_actor_ids=["cleric"],
        )
    )

    assert result["action"]["result"]["core_effect"]["after_hp"] == 10
    assert result["knowledge_actor_ids"] == ["fighter", "cleric"]


def test_dm_event_keeps_enemy_knowledge_out_of_party_event_stream() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 12,
        "page_end": 12,
        "heading_path": ["Developments"],
        "content_sha256": "abc",
    }

    class Client:
        revision = 7

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": self.revision}}

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query" and arguments["view"] == "scene":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "A messenger warned the leader.",
                    "locations": [{"key": "8-cave"}],
                }
            if tool_id == "module_query" and arguments["view"] == "progress":
                return []
            if tool_id == "module_set_progress":
                self.revision += 1
                return {"scene_id": "scene-1", "state_version": 1}
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "continuity_commit":
                event = arguments["payload"]["event"]
                assert event["audience_scope"] == "dm"
                knowledge = arguments["payload"]["actor_knowledge"]
                assert [item["actor_id"] for item in knowledge] == ["enemy"]
                self.revision += 1
                return {"event": {"id": "event-1"}, "snapshot": {"slot": 7}}
            if tool_id == "playthrough_manifest":
                return {
                    "manifest": {"status": "in_progress"},
                    "campaign_revision": self.revision,
                }
            raise AssertionError((tool_id, arguments))

    result = asyncio.run(
        _record_event(
            Client(),
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            location_key="8-cave",
            source_excerpt="A messenger warned the leader.",
            source_ref=source_ref,
            event_type="enemy_alerted",
            summary="The leader received the warning.",
            knowledge="The party is approaching.",
            knowledge_actor_ids=["enemy"],
            progress_percent=60,
            audience_scope="dm",
        )
    )

    assert result["knowledge_actor_ids"] == ["enemy"]


def test_long_rest_uses_atomic_party_rest_and_records_checkpoint() -> None:
    class Client:
        def __init__(self) -> None:
            self.revision = 5
            self.world_time = {
                "day": 1,
                "hour": 16,
                "minute": 0,
                "elapsed_minutes": 960,
                "label": "Cragmaw Hideout",
            }

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.revision,
                    "state": {"game_phase": "play", "world_time": self.world_time},
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "character_query":
                actor_id = arguments["payload"]["character_id"]
                return {
                    "id": actor_id,
                    "campaign_id": "campaign-1",
                    "revision": 2,
                }
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "campaign_change":
                assert arguments["action"] == "party_rest"
                assert arguments["payload"]["duration_minutes"] == 480
                assert arguments["payload"]["members"] == [
                    {
                        "character_id": "fighter",
                        "expected_revision": 2,
                        "food_and_drink": True,
                    },
                    {
                        "character_id": "cleric",
                        "expected_revision": 2,
                        "food_and_drink": False,
                        "prepared_spell_ids": ["cure-wounds"],
                    },
                ]
                self.world_time = {
                    **self.world_time,
                    "day": 2,
                    "hour": 0,
                    "elapsed_minutes": 1440,
                }
                self.revision += 1
                return {
                    "status": "committed",
                    "world_time": self.world_time,
                    "member_ids": ["fighter", "cleric"],
                }
            if tool_id == "continuity_commit":
                event = arguments["payload"]["event"]
                assert event["event_type"] == "long_rest"
                assert event["payload"]["duration_minutes"] == 480
                self.revision += 1
                return {"event": {"id": "event-1"}, "snapshot": {"slot": 5}}
            if tool_id == "playthrough_manifest":
                return {
                    "manifest": {"status": "in_progress"},
                    "campaign_revision": self.revision,
                }
            raise AssertionError((tool_id, arguments))

    result = asyncio.run(
        _long_rest(
            Client(),
            campaign_id="campaign-1",
            run_id="run-1",
            members=[
                {"actor_id": "fighter", "food_and_drink": True},
                {"actor_id": "cleric", "prepared_spell_ids": ["cure-wounds"]},
            ],
            start_clock=None,
            duration_minutes=480,
            reason="The party withdrew and completed an uninterrupted long rest.",
        )
    )

    assert result["member_ids"] == ["fighter", "cleric"]
    assert result["rest"]["world_time"]["day"] == 2
    assert result["continuity"]["snapshot"]["slot"] == 5


def test_partially_committed_check_is_recovered_without_reroll() -> None:
    result = {"success": False, "total": 7, "dc": 10}
    campaign = {
        "state": {
            "random_stream": {"last_receipt": {"operation": "character_check"}},
            "resolution_log": [{"type": "ability", "actor_id": "actor-1", "result": result}],
        }
    }

    assert (
        _recover_committed_check(
            campaign,
            progress_matches=True,
            actor_id="actor-1",
            kind="ability",
            dc=10,
        )
        == result
    )
    assert (
        _recover_committed_check(
            campaign,
            progress_matches=False,
            actor_id="actor-1",
            kind="ability",
            dc=10,
        )
        is None
    )


def test_xp_award_uses_source_ref_and_all_exact_recipients() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 7,
        "page_end": 7,
        "heading_path": ["Awarding Experience Points"],
        "content_sha256": "abc",
    }

    class Client:
        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": 4}}

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "Award each character 75 XP.",
                }
            if tool_id == "character_query":
                actor_id = arguments["payload"]["character_id"]
                return {
                    "id": actor_id,
                    "campaign_id": "campaign-1",
                    "revision": 2,
                }
            if tool_id == "campaign_change":
                assert arguments["action"] == "experience_award"
                assert [item["character_id"] for item in arguments["payload"]["awards"]] == [
                    "actor-1",
                    "actor-2",
                ]
                assert all(item["amount"] == 75 for item in arguments["payload"]["awards"])
                assert json.loads(arguments["payload"]["source_ref"]) == source_ref
                return {"awards": [{"new_xp": 75}, {"new_xp": 75}]}
            if tool_id == "playthrough_manifest":
                return {"manifest": {"status": "in_progress"}, "campaign_revision": 5}
            raise AssertionError((tool_id, arguments))

    result = asyncio.run(
        _award_experience(
            Client(),
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            source_ref=source_ref,
            actor_ids=["actor-1", "actor-2"],
            amount=75,
            reason="Reached the hideout",
        )
    )

    assert [item["new_xp"] for item in result["award"]["awards"]] == [75, 75]


def test_xp_award_idempotency_identity_includes_exact_recipient_set() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 7,
        "page_end": 7,
        "heading_path": ["Awarding Experience Points"],
        "content_sha256": "abc",
    }

    class Client:
        def __init__(self) -> None:
            self.award_keys: list[str] = []
            self.sync_keys: list[str] = []

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": 4}}

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "Award each character 75 XP.",
                }
            if tool_id == "character_query":
                actor_id = arguments["payload"]["character_id"]
                return {
                    "id": actor_id,
                    "campaign_id": "campaign-1",
                    "revision": 2,
                }
            if tool_id == "campaign_change":
                self.award_keys.append(arguments["idempotency_key"])
                return {"awards": [{"new_xp": 75}]}
            if tool_id == "playthrough_manifest":
                self.sync_keys.append(arguments["idempotency_key"])
                return {"manifest": {"status": "in_progress"}, "campaign_revision": 5}
            raise AssertionError((tool_id, arguments))

    async def award(client: Client, actor_id: str) -> None:
        await _award_experience(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            source_ref=source_ref,
            actor_ids=[actor_id],
            amount=75,
            reason="Reached the hideout",
        )

    client = Client()
    asyncio.run(award(client, "actor-1"))
    asyncio.run(award(client, "actor-2"))

    assert len(set(client.award_keys)) == 2
    assert len(set(client.sync_keys)) == 2


def test_source_cited_automatic_event_does_not_roll() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "source-scene-1",
        "chunk_id": "chunk-1",
        "page_start": 7,
        "page_end": 7,
        "heading_path": ["Goblin Trail"],
        "content_sha256": "abc",
    }

    class Client:
        def __init__(self) -> None:
            self.tools: list[str] = []
            self.continuity_payload: dict = {}

        async def core(self, tool_id: str, arguments: dict):
            self.tools.append(tool_id)
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": 4}}

        async def domain(self, tool_id: str, arguments: dict):
            self.tools.append(tool_id)
            if tool_id == "module_query" and arguments["view"] == "scene":
                if arguments["payload"]["scene_id"] == "source-scene-1":
                    return {
                        "module_id": "module-1",
                        "scene_id": "source-scene-1",
                        "content": "The lead character spots the snare automatically.",
                    }
                assert arguments["payload"]["scene_id"] == "scene-1"
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "locations": [{"key": "ambush"}],
                }
            if tool_id == "module_query" and arguments["view"] == "progress":
                return []
            if tool_id == "module_set_progress":
                return {"state_version": 1}
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "continuity_commit":
                self.continuity_payload = deepcopy(arguments["payload"])
                assert len(arguments["payload"]["actor_knowledge"]) == 2
                return {"event": {"id": "event-1"}, "snapshot": {"slot": 4}}
            if tool_id == "playthrough_manifest":
                return {"manifest": {"status": "in_progress"}, "campaign_revision": 5}
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _record_event(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            location_key="ambush",
            source_excerpt="The lead character spots the snare automatically.",
            source_ref=source_ref,
            event_type="trap_detected",
            summary="Dorn automatically spotted the snare.",
            knowledge="The party knows the snare's location.",
            knowledge_actor_ids=["actor-1", "actor-2"],
            progress_percent=65,
            source_scene_id="source-scene-1",
            defer_checkpoint=True,
        )
    )

    assert result["knowledge_actor_ids"] == ["actor-1", "actor-2"]
    assert result["scene"]["scene_id"] == "scene-1"
    assert result["scene"]["source_scene_id"] == "source-scene-1"
    assert client.continuity_payload["event"]["payload"]["source_scene_id"] == (
        "source-scene-1"
    )
    assert "character_check" not in client.tools
    assert "dnd_dice_roll" not in client.tools
    assert "snapshot" not in client.continuity_payload


def test_record_event_preserves_prior_scene_events_in_same_run() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 7,
        "page_end": 7,
        "heading_path": ["Goblin Den"],
        "content_sha256": "abc",
    }
    prior_events = {
        "prior-event-key": {
            "event_type": "hostage_truce",
            "summary": "Yeemik seized Sildar.",
            "source_ref": source_ref,
        }
    }

    class Client:
        def __init__(self) -> None:
            self.saved_events: dict = {}

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": 4}}

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query" and arguments["view"] == "scene":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "Yeemik demands a rich ransom.",
                    "locations": [{"key": "goblin-den"}],
                }
            if tool_id == "module_query" and arguments["view"] == "progress":
                return [
                    {
                        "scene_id": "scene-1",
                        "progress": 60,
                        "state_version": 3,
                        "state": {"full_playthrough_events": deepcopy(prior_events)},
                    }
                ]
            if tool_id == "module_set_progress":
                self.saved_events = deepcopy(arguments["state"]["full_playthrough_events"])
                return {"scene_id": "scene-1", "state_version": 4}
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "continuity_commit":
                assert arguments["payload"]["actor_knowledge"][0]["cause"] == (
                    "told_by"
                )
                return {"event": {"id": "event-2"}, "snapshot": {"slot": 5}}
            if tool_id == "playthrough_manifest":
                return {"manifest": {"status": "in_progress"}, "campaign_revision": 5}
            raise AssertionError((tool_id, arguments))

    client = Client()
    asyncio.run(
        _record_event(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            location_key="goblin-den",
            source_excerpt="Yeemik demands a rich ransom.",
            source_ref=source_ref,
            event_type="ransom_demand",
            summary="Yeemik demanded an additional ransom.",
            knowledge="Yeemik has broken the spirit of the bargain.",
            knowledge_actor_ids=["actor-1"],
            progress_percent=70,
            knowledge_cause="told_by",
        )
    )

    assert client.saved_events["prior-event-key"] == prior_events["prior-event-key"]
    assert len(client.saved_events) == 2
    assert {value["event_type"] for value in client.saved_events.values()} == {
        "hostage_truce",
        "ransom_demand",
    }


@pytest.mark.parametrize("defer_checkpoint", [False, True])
def test_record_outcome_commits_facts_then_syncs_manifest_and_checkpoint(
    defer_checkpoint: bool,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "source-scene-1",
        "chunk_id": "chunk-1",
        "page_start": 10,
        "page_end": 11,
        "heading_path": ["Goblin Den"],
        "content_sha256": "abc",
    }

    class Client:
        def __init__(self) -> None:
            self.revision = 10
            self.loaded_groups: list[tuple[str, ...]] = []
            self.manifest = new_playthrough_manifest(
                run_id="run-1",
                campaign_line_id="line-1",
                module_ids=["module-1"],
                recommended_party_minimum=None,
                recommended_party_maximum=None,
                selected_party_size=None,
                source_refs=[_manifest_source_ref()],
            )
            self.manifest["current"]["objective"] = "Rescue the hostage."
            self.manifest["npcs"] = [
                {
                    "actor_id": "npc-1",
                    "name": "Hostage",
                    "status": "missing",
                }
            ]
            self.manifest["world_state"] = {"prior_state": True}
            self.replaced_manifest: dict = {}
            self.continuity_payload: dict = {}

        async def load(self, *group_ids: str) -> None:
            self.loaded_groups.append(group_ids)

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": self.revision}}

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query" and arguments["view"] == "scene":
                if arguments["payload"]["scene_id"] == "source-scene-1":
                    return {
                        "module_id": "module-1",
                        "scene_id": "source-scene-1",
                        "content": "The hostage is released.",
                    }
                assert arguments["payload"]["scene_id"] == "scene-1"
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "locations": [{"key": "goblin-den"}],
                }
            if tool_id == "module_query" and arguments["view"] == "progress":
                return [
                    {
                        "scene_id": "scene-1",
                        "progress": 80,
                        "state_version": 2,
                        "state": {"full_playthrough_outcomes": {"prior": {"event_type": "prior"}}},
                    }
                ]
            if tool_id == "character_query":
                actor_id = arguments["payload"]["character_id"]
                return {
                    "id": actor_id,
                    "campaign_id": "campaign-1",
                    "name": actor_id,
                }
            if tool_id == "module_set_progress":
                outcomes = arguments["state"]["full_playthrough_outcomes"]
                assert set(outcomes) == {"prior", "hostage-released"}
                assert arguments["status"] == "completed"
                return {"scene_id": "scene-1", "state_version": 3}
            if tool_id == "branch_query":
                return [
                    {
                        "id": "branch-1",
                        "is_current": True,
                        "head_snapshot_id": "snapshot-old",
                    }
                ]
            if tool_id == "continuity_commit":
                self.continuity_payload = deepcopy(arguments["payload"])
                assert "snapshot" not in self.continuity_payload
                assert {
                    item["cause"] for item in self.continuity_payload["actor_knowledge"]
                } == {"witnessed"}
                assert self.continuity_payload["facts"][0]["fact_key"] == ("quest:hostage:status")
                self.revision += 1
                return {
                    "event": {"id": "event-1"},
                    "facts": [{"fact_key": "quest:hostage:status"}],
                }
            if tool_id == "playthrough_manifest" and arguments["action"] == "get":
                return {
                    "manifest": deepcopy(self.manifest),
                    "campaign_revision": self.revision,
                }
            if tool_id == "playthrough_manifest" and arguments["action"] == "replace":
                self.replaced_manifest = deepcopy(arguments["payload"]["manifest"])
                self.manifest = deepcopy(self.replaced_manifest)
                self.revision += 1
                return {
                    "manifest": deepcopy(self.manifest),
                    "campaign_revision": self.revision,
                }
            if tool_id == "playthrough_manifest" and arguments["action"] == "sync":
                self.revision += 1
                return {
                    "manifest": deepcopy(self.manifest),
                    "campaign_revision": self.revision,
                }
            if tool_id == "snapshot_create":
                assert arguments["label"] == ("Full playthrough outcome: hostage-released")
                self.revision += 1
                self.manifest["snapshot_dag"] = {
                    "active_branch_id": "branch-1",
                    "head_snapshot_id": "snapshot-new",
                    "nodes": [
                        {
                            "id": "snapshot-new",
                            "parent_id": "snapshot-old",
                            "branch_id": "branch-1",
                            "slot": 7,
                            "label": arguments["label"],
                            "checksum": "c" * 64,
                            "is_head": True,
                        }
                    ],
                }
                return {"id": "snapshot-new", "slot": 7}
            if tool_id == "snapshot_query":
                return {"valid": True, "slot": 7}
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _record_outcome(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            outcome_id="hostage-released",
            scene_id="scene-1",
            location_key="goblin-den",
            source_excerpt="The hostage is released.",
            source_ref=source_ref,
            event_type="hostage_released",
            summary="The hostage was released and the captor departed.",
            knowledge="The hostage is free.",
            knowledge_actor_ids=["pc-1", "npc-1"],
            facts=[
                {
                    "fact_key": "quest:hostage:status",
                    "content": "completed",
                }
            ],
            npc_states=[
                {
                    "actor_id": "npc-1",
                    "name": "Hostage",
                    "status": "active",
                    "relationship": "rescued ally",
                },
                {
                    "actor_id": "npc-2",
                    "name": "Captor",
                    "status": "departed",
                    "relationship": "hostile",
                },
            ],
            quest_states=[
                {
                    "id": "rescue-hostage",
                    "title": "Rescue the hostage",
                    "status": "completed",
                    "source_ref": _manifest_source_ref(),
                    "outcome": "Released alive.",
                }
            ],
            clue_states=[],
            world_state={"hostage_released": True},
            objective="Escort the hostage to safety.",
            progress_percent=100,
            source_scene_id="source-scene-1",
            defer_checkpoint=defer_checkpoint,
        )
    )

    if defer_checkpoint:
        assert result["checkpoint"] is None
    else:
        assert result["checkpoint"]["verification"]["valid"] is True
    assert result["scene"]["source_scene_id"] == "source-scene-1"
    assert client.continuity_payload["event"]["payload"]["source_scene_id"] == (
        "source-scene-1"
    )
    assert client.loaded_groups == [("play.characters",)]
    assert client.replaced_manifest["current"]["objective"] == ("Escort the hostage to safety.")
    assert client.replaced_manifest["world_state"] == {
        "prior_state": True,
        "hostage_released": True,
    }
    assert client.replaced_manifest["npcs"][0]["status"] == "active"
    assert client.replaced_manifest["npcs"][1]["actor_id"] == "npc-2"
    assert client.replaced_manifest["quests"][0]["status"] == "completed"


def test_record_outcome_rejects_invalid_manifest_rows_before_mutation() -> None:
    class Client:
        def __init__(self) -> None:
            self.loaded = False
            self.calls: list[tuple[str, str]] = []
            self.manifest = new_playthrough_manifest(
                run_id="run-1",
                campaign_line_id="line-1",
                module_ids=["module-1"],
                recommended_party_minimum=None,
                recommended_party_maximum=None,
                selected_party_size=None,
                source_refs=[_manifest_source_ref()],
            )

        async def load(self, *_group_ids: str) -> None:
            self.loaded = True

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append((tool_id, str(arguments.get("action") or "")))
            if tool_id == "playthrough_manifest" and arguments["action"] == "get":
                return {"manifest": deepcopy(self.manifest), "campaign_revision": 1}
            raise AssertionError((tool_id, arguments))

    client = Client()
    with pytest.raises(ValueError, match="unsupported fields: objective"):
        asyncio.run(
            _record_outcome(
                client,
                campaign_id="campaign-1",
                run_id="run-1",
                outcome_id="hostage-released",
                scene_id="scene-1",
                location_key="goblin-den",
                source_excerpt="The hostage is released.",
                source_ref={},
                event_type="hostage_released",
                summary="The hostage was released.",
                knowledge="",
                knowledge_actor_ids=[],
                facts=[{"fact_key": "quest:hostage:status", "content": "completed"}],
                npc_states=[],
                quest_states=[
                    {
                        "id": "rescue-hostage",
                        "title": "Rescue the hostage",
                        "status": "completed",
                        "source_ref": _manifest_source_ref(),
                        "outcome": "Released alive.",
                        "objective": "This field is not in the manifest schema.",
                    }
                ],
                clue_states=[],
                world_state={},
                objective="",
                progress_percent=100,
            )
        )

    assert client.calls == [("playthrough_manifest", "get")]
    assert client.loaded is False


def test_record_outcome_resumes_after_matching_progress_was_already_saved() -> None:
    compact_source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 10,
        "page_end": 11,
        "heading_path": ["Goblin Den"],
        "content_sha256": "abc",
    }
    summary = "The hostage was released."
    outcome_record = {
        "event_type": "hostage_released",
        "summary": summary,
        "source_ref": compact_source_ref,
        "fact_keys": ["quest:hostage:status"],
    }

    class Client:
        def __init__(self) -> None:
            self.manifest = new_playthrough_manifest(
                run_id="run-1",
                campaign_line_id="line-1",
                module_ids=["module-1"],
                recommended_party_minimum=None,
                recommended_party_maximum=None,
                selected_party_size=None,
                source_refs=[_manifest_source_ref()],
            )
            self.progress_writes = 0

        async def load(self, *_group_ids: str) -> None:
            return None

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "playthrough_manifest" and arguments["action"] == "get":
                return {"manifest": deepcopy(self.manifest), "campaign_revision": 1}
            if tool_id == "module_query" and arguments["view"] == "scene":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "The hostage is released.",
                    "locations": [{"key": "goblin-den"}],
                }
            if tool_id == "module_query" and arguments["view"] == "progress":
                return [
                    {
                        "scene_id": "scene-1",
                        "progress": 100,
                        "state_version": 3,
                        "state": {
                            "full_playthrough_outcomes": {"hostage-released": outcome_record}
                        },
                    }
                ]
            if tool_id == "module_set_progress":
                self.progress_writes += 1
                raise AssertionError("matching progress must be resumed without rewriting")
            if tool_id == "branch_query":
                raise RuntimeError("resume reached continuity boundary")
            raise AssertionError((tool_id, arguments))

    client = Client()
    with pytest.raises(RuntimeError, match="resume reached continuity boundary"):
        asyncio.run(
            _record_outcome(
                client,
                campaign_id="campaign-1",
                run_id="run-1",
                outcome_id="hostage-released",
                scene_id="scene-1",
                location_key="goblin-den",
                source_excerpt="The hostage is released.",
                source_ref=compact_source_ref,
                event_type="hostage_released",
                summary=summary,
                knowledge="",
                knowledge_actor_ids=[],
                facts=[{"fact_key": "quest:hostage:status", "content": "completed"}],
                npc_states=[],
                quest_states=[],
                clue_states=[],
                world_state={},
                objective="",
                progress_percent=100,
            )
        )

    assert client.progress_writes == 0


def test_start_play_uses_public_quality_gate_phase_and_scene_tools() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []
            self.manifest = {
                "status": "lobby",
                "module_ids": ["module-1"],
                "current": {},
                "traversal": {
                    "reachable_scene_ids": [],
                    "visited_scene_ids": [],
                    "excluded_scenes": [],
                },
            }

        async def open(self, campaign_id: str) -> None:
            assert campaign_id == "campaign-1"

        async def load(self, *group_ids: str) -> None:
            assert group_ids == ("play.scene", "play.scene_control")

        async def core(self, tool_id: str, arguments: dict):
            self.calls.append((tool_id, arguments))
            if tool_id == "campaign_query":
                return {"result": {"id": "campaign-1", "revision": 8}}
            if tool_id == "game_phase":
                return {"result": {"tool_profile": "play"}}
            raise AssertionError(tool_id)

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append((tool_id, arguments))
            if tool_id == "playthrough_manifest" and arguments["action"] == "get":
                return {"manifest": deepcopy(self.manifest)}
            if tool_id == "playthrough_manifest" and arguments["action"] == "replace":
                self.manifest = deepcopy(arguments["payload"]["manifest"])
                return {"manifest": deepcopy(self.manifest), "campaign_revision": 9}
            if tool_id == "playthrough_manifest" and arguments["action"] == "sync":
                return {"manifest": deepcopy(self.manifest), "campaign_revision": 10}
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "chapter_id": "chapter-1",
                    "chapter": "Chapter 1",
                    "title": "Opening",
                }
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _start_play(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            initial_phase="lobby",
            scene_id="scene-1",
            objective="Survive the ambush",
            reachable_scene_ids=["scene-2"],
        )
    )

    assert result["sync"]["campaign_revision"] == 10
    assert client.manifest["status"] == "in_progress"
    assert client.manifest["current"]["scene_id"] == "scene-1"
    assert client.manifest["traversal"]["visited_scene_ids"] == ["scene-1"]
    assert any(name == "game_phase" for name, _ in client.calls)
