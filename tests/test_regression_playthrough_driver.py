from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

from scripts.regression_playthrough import (
    _award_experience,
    _campaign_phase,
    _check_knowledge_key,
    _checkpoint,
    _configure_advancement,
    _mutation_key,
    _party_member,
    _party_selections,
    _phase_groups,
    _resolve_check,
    _start_play,
)


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
