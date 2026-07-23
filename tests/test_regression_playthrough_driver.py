from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

from scripts.regression_playthrough import (
    _acquire_source_loot,
    _apply_source_damage,
    _award_experience,
    _branch_from_snapshot,
    _campaign_phase,
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
    _query_source,
    _record_event,
    _recover_committed_check,
    _recover_stable_party,
    _resolve_check,
    _scene_progress_percent,
    _short_rest,
    _stand_after_source_event,
    _start_play,
    _use_activity,
    _use_shared_consumable,
)


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
        "scene_id": "scene-1",
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
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "The chest contains 60 cp and a jade frog.",
                    "spatial": {
                        "locations": [
                            {"key": "treasure-room", "title": "Treasure Room"}
                        ]
                    },
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
            source_excerpt="contains 60 cp and a jade frog",
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
        )
    )

    assert result["acquisition"]["status"] == "committed"
    assert client.tools.count("campaign_change") == 1
    assert result["knowledge_actor_ids"] == ["actor-1", "actor-2"]


def test_query_source_searches_and_expands_only_public_mcp_results() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append((tool_id, arguments))
            if tool_id == "module_search":
                return {
                    "result": [
                        {"id": "chunk-1", "content": "A captured character..."}
                    ]
                }
            if tool_id == "module_expand":
                return {
                    "chunk_id": "chunk-1",
                    "content": "A captured character is taken to the eating cave.",
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
                return {
                    "status": "recovered",
                    "elapsed_hours": 4,
                    "recoveries": {"actor-1": {}, "actor-2": {}},
                    "random_stream_receipt": {"start_position": 10, "end_position": 12},
                }
            if tool_id == "continuity_commit":
                assert len(arguments["payload"]["actor_knowledge"]) == 3
                return {"event": {"id": "event-1"}, "snapshot": {"slot": 7}}
            if tool_id == "playthrough_manifest":
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
                return [
                    {"id": "snapshot-58", "slot": 58, "branch_id": "failed-branch"}
                ]
            if tool_id == "snapshot_query" and arguments["view"] == "verify":
                return {"valid": True}
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


def test_source_cited_check_persists_result_and_explicit_knowledge() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 7,
        "page_end": 7,
        "heading_path": ["Goblin Trail"],
        "content_sha256": "abc",
    }

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
                    == _check_knowledge_key("run-1", "scene-1", "ability", "survival", "actor-1")
                    for item in arguments["payload"]["actor_knowledge"]
                )
                assert arguments["payload"]["event"]["payload"]["source_ref"] == source_ref
                self.revision += 1
                return {"event": {"id": "event-1"}, "snapshot": {"slot": 3}}
            if tool_id == "playthrough_manifest":
                assert arguments["action"] == "sync"
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
        )
    )

    assert result["check"] == {"success": True, "total": 14}
    assert result["knowledge_actor_ids"] == ["actor-1", "actor-2"]
    assert result["sync"]["campaign_revision"] == 7
    assert _check_knowledge_key(
        "run-1", "scene-1", "ability", "survival", "actor-1"
    ) != _check_knowledge_key("run-1", "scene-1", "ability", "perception", "actor-1")


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
                "actor_id": "fighter",
                "kind": "ability",
                "ability": "stealth",
                "dc": 9,
                "advantage": False,
                "disadvantage": True,
                "source_ref": source_ref,
            }
        },
    }

    assert _matching_check_progress(
        progress,
        location_key="bridge",
        actor_id="fighter",
        kind="ability",
        ability="stealth",
        dc=9,
        advantage=False,
        disadvantage=True,
        source_ref=source_ref,
    )
    assert not _matching_check_progress(
        progress,
        location_key="bridge",
        actor_id="rogue",
        kind="ability",
        ability="stealth",
        dc=9,
        advantage=False,
        disadvantage=True,
        source_ref=source_ref,
    )
    assert not _matching_check_progress(
        progress,
        location_key="bridge",
        actor_id="fighter",
        kind="ability",
        ability="stealth",
        dc=9,
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
            if (
                tool_id == "character_state_change"
                and arguments["action"] == "knock_prone"
            ):
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


def test_source_event_stand_uses_validated_public_character_action() -> None:
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
        revision = 20

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
                self.revision += 1
                return {"status": "stood", "character": {"revision": 5}}
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "continuity_commit":
                assert arguments["payload"]["event"]["payload"]["source_ref"] == source_ref
                self.revision += 1
                return {"event": {"id": "event-1"}, "snapshot": {"slot": 3}}
            if tool_id == "playthrough_manifest":
                return {
                    "manifest": {"status": "in_progress"},
                    "campaign_revision": self.revision,
                }
            raise AssertionError((tool_id, arguments))

    result = asyncio.run(
        _stand_after_source_event(
            Client(),
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            location_key="3-kennel",
            source_excerpt="The character lands prone at the base of the shaft.",
            source_ref=source_ref,
            actor_id="actor-1",
            knowledge_actor_ids=["actor-2"],
        )
    )

    assert result["stand"]["status"] == "stood"
    assert result["knowledge_actor_ids"] == ["actor-1", "actor-2"]


def test_short_rest_advances_clock_and_applies_only_explicit_resource_choices() -> None:
    class Client:
        def __init__(self) -> None:
            self.revision = 5
            self.world_time: dict = {}

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
            if tool_id == "campaign_change" and arguments["action"] == "clock_set":
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
                assert arguments["payload"] == {"period": "minute", "count": 60}
                self.world_time = {
                    **self.world_time,
                    "hour": 15,
                    "elapsed_minutes": 900,
                }
                self.revision += 1
                return {"world_time": self.world_time}
            if tool_id == "character_state_change":
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
                assert arguments["payload"]["event"]["payload"]["duration_minutes"] == 60
                self.revision += 1
                return {"event": {"id": "event-1"}, "snapshot": {"slot": 4}}
            if tool_id == "playthrough_manifest":
                return {
                    "manifest": {"status": "in_progress"},
                    "campaign_revision": self.revision,
                }
            raise AssertionError((tool_id, arguments))

    result = asyncio.run(
        _short_rest(
            Client(),
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
                assert arguments["payload"] == {
                    "activity_id": "fighter-second-wind"
                }
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


def test_source_cited_automatic_event_does_not_roll() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 7,
        "page_end": 7,
        "heading_path": ["Goblin Trail"],
        "content_sha256": "abc",
    }

    class Client:
        def __init__(self) -> None:
            self.tools: list[str] = []

        async def core(self, tool_id: str, arguments: dict):
            self.tools.append(tool_id)
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": 4}}

        async def domain(self, tool_id: str, arguments: dict):
            self.tools.append(tool_id)
            if tool_id == "module_query" and arguments["view"] == "scene":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "The lead character spots the snare automatically.",
                    "locations": [{"key": "ambush"}],
                }
            if tool_id == "module_query" and arguments["view"] == "progress":
                return []
            if tool_id == "module_set_progress":
                return {"state_version": 1}
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "continuity_commit":
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
        )
    )

    assert result["knowledge_actor_ids"] == ["actor-1", "actor-2"]
    assert "character_check" not in client.tools
    assert "dnd_dice_roll" not in client.tools


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
