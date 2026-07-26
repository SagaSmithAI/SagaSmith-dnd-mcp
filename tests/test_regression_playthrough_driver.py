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
from scripts.regression_modules import PRINCIPAL_ID
from scripts.regression_playthrough import (
    _acquire_source_loot,
    _advance_level,
    _advance_scene,
    _advance_time,
    _apply_source_damage,
    _apply_source_effect,
    _attack_source_object,
    _award_experience,
    _branch_from_snapshot,
    _campaign_phase,
    _cast_healing_spell,
    _cast_source_spell,
    _check_identity,
    _check_knowledge_key,
    _checkpoint,
    _claim_party_item_for_character,
    _committed_check_result,
    _committed_contest_result,
    _configure_advancement,
    _configure_ending_conditions,
    _extend_manifest_for_module_revision,
    _index_source,
    _initialize_clock,
    _initialize_source_state,
    _level_spell_choice_counts,
    _long_rest,
    _matching_check_progress,
    _matching_contest_progress,
    _module_refresh_identity,
    _module_refresh_manifest_action,
    _module_refresh_manifest_identity,
    _mutation_key,
    _occurrence_identity,
    _party_member,
    _party_selections,
    _phase_groups,
    _pool_character_currency,
    _preflight_level_completion,
    _prepare_narrative_npc,
    _provision_source_item,
    _query_source,
    _read_scene,
    _record_event,
    _record_outcome,
    _recover_committed_check,
    _recover_committed_contest,
    _recover_stable_party,
    _refresh_module,
    _register_replacement,
    _relock_core,
    _remap_ending_sources_for_module_revision,
    _remove_source_effect,
    _resolve_check,
    _restore_phase_after_failed_refresh,
    _roll_source_table,
    _scene_progress_percent,
    _set_source_exhaustion,
    _short_rest,
    _source_groups,
    _spend_source_currency,
    _spend_source_item,
    _stand_after_source_event,
    _start_play,
    _transfer_source_item_to_party,
    _use_activity,
    _use_shared_consumable,
)
from scripts.regression_rulings import RegressionRulingRequiredError


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


@pytest.mark.parametrize("action", ["checkpoint", "sync"])
def test_explicit_checkpoint_and_sync_require_an_occurrence_id(action: str) -> None:
    args = argparse.Namespace(
        defer_checkpoint=False,
        action=action,
        occurrence_id="",
    )

    with pytest.raises(ValueError, match=rf"{action} requires --occurrence-id"):
        asyncio.run(regression_playthrough._run(args))


def test_scene_resource_actions_support_deferred_checkpoint_batching() -> None:
    assert {
        "advance-level",
        "apply-damage",
        "roll-source",
        "register-replacement",
        "spend-coins",
        "spend-item",
        "use-activity",
        "use-consumable",
    } <= regression_playthrough.DEFERRED_CHECKPOINT_ACTIONS


def test_source_queries_load_the_phase_specific_public_group() -> None:
    assert _source_groups("lobby") == ("lobby.modules",)
    assert _source_groups("play") == ("play.scene",)
    assert _source_groups("combat") == ("combat.observe",)


def test_configure_ending_uses_public_manifest_replace_and_rejects_redefinition() -> None:
    class Client:
        def __init__(self) -> None:
            self.revision = 3
            self.manifest = new_playthrough_manifest(
                run_id="run-1",
                campaign_line_id="line-1",
                module_ids=["module-1"],
                recommended_party_minimum=None,
                recommended_party_maximum=None,
                selected_party_size=None,
                source_refs=[_manifest_source_ref()],
            )
            self.replace_calls: list[dict] = []

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": self.revision}}

        async def domain(self, tool_id: str, arguments: dict):
            assert tool_id == "playthrough_manifest"
            if arguments["action"] == "get":
                return {
                    "manifest": deepcopy(self.manifest),
                    "campaign_revision": self.revision,
                }
            assert arguments["action"] == "replace"
            self.replace_calls.append(deepcopy(arguments))
            self.manifest = deepcopy(arguments["payload"]["manifest"])
            self.revision += 1
            return {
                "manifest": deepcopy(self.manifest),
                "campaign_revision": self.revision,
            }

    condition = {
        "id": "source-victory",
        "label": "The source-defined threat is defeated",
        "source_ref": _manifest_source_ref(),
        "all_of": [
            {
                "kind": "manifest_value",
                "path": "world_state.victory",
                "actor_id": "",
                "fact_key": "",
                "operator": "equals",
                "value": True,
            }
        ],
    }
    client = Client()
    result = asyncio.run(
        _configure_ending_conditions(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            conditions=[condition],
        )
    )

    assert result["manifest"]["ending"]["conditions"] == [condition]
    assert len(client.replace_calls) == 1
    assert client.replace_calls[0]["expected_revision"] == 3

    changed = deepcopy(condition)
    changed["label"] = "Different"
    with pytest.raises(ValueError, match="already exists with different content"):
        asyncio.run(
            _configure_ending_conditions(
                client,
                campaign_id="campaign-1",
                run_id="run-1",
                conditions=[changed],
            )
        )


def test_advance_scene_identity_supports_exact_retry_and_later_revisit() -> None:
    source_excerpt = 'Proceed to "Town."'
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-old",
        "chunk_id": "chunk-transition",
        "page_start": 1,
        "page_end": 1,
        "heading_path": ["Chapter", "Next"],
        "content_sha256": "a" * 64,
    }

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
                requested_scene_id = arguments["payload"]["scene_id"]
                if requested_scene_id == "scene-old":
                    return {
                        "module_id": "module-1",
                        "chapter_id": "chapter-1",
                        "chapter": "Chapter",
                        "scene_id": "scene-old",
                        "title": "Road",
                        "content": source_excerpt,
                    }
                if requested_scene_id == "scene-citation":
                    return {
                        "module_id": "module-1",
                        "chapter_id": "chapter-1",
                        "chapter": "Chapter",
                        "scene_id": "scene-citation",
                        "title": "Sibling source",
                        "content": "The survivors carry the Stone to Town.",
                    }
                assert requested_scene_id == "scene-town"
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

    async def advance(client: Client, occurrence_id: str) -> None:
        await _advance_scene(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id=occurrence_id,
            scene_id="scene-town",
            source_scene_id="scene-old",
            source_excerpt=source_excerpt,
            source_ref=source_ref,
            objective="Return the rescued family.",
            mark_visited=True,
            reachable_scene_ids=[],
            excluded_scenes=[],
        )

    client = Client()
    asyncio.run(advance(client, "town-visit-1"))
    asyncio.run(advance(client, "town-visit-1"))
    first_key, retry_key = [item["idempotency_key"] for item in client.replace_calls]
    assert first_key == retry_key
    assert (
        client.replace_calls[0]["payload"]["manifest"]
        == client.replace_calls[1]["payload"]["manifest"]
    )

    client.manifest["world_state"]["visit_marker"] = 2
    client.manifest["current"]["scene_id"] = "scene-old"
    asyncio.run(advance(client, "town-visit-2"))
    revisit_key = client.replace_calls[2]["idempotency_key"]
    assert revisit_key != first_key
    assert client.manifest["world_state"]["scene_transitions"] == {
        "town-visit-1": {
            "from_scene_id": "scene-old",
            "to_scene_id": "scene-town",
            "source_excerpt": source_excerpt,
            "source_ref": source_ref,
        },
        "town-visit-2": {
            "from_scene_id": "scene-old",
            "to_scene_id": "scene-town",
            "source_excerpt": source_excerpt,
            "source_ref": source_ref,
        },
    }

    citation_ref = {
        **source_ref,
        "scene_id": "scene-citation",
        "chunk_id": "chunk-sibling-transition",
        "content_sha256": "b" * 64,
    }
    client.manifest["current"]["scene_id"] = "scene-old"
    asyncio.run(
        _advance_scene(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="town-visit-sibling-source",
            scene_id="scene-town",
            source_scene_id="scene-citation",
            source_excerpt="The survivors carry the Stone to Town.",
            source_ref=citation_ref,
            objective="Follow the Stone.",
            mark_visited=True,
            reachable_scene_ids=[],
            excluded_scenes=[],
            occurrence_scene_id="scene-old",
        )
    )
    assert client.manifest["world_state"]["scene_transitions"]["town-visit-sibling-source"] == {
        "from_scene_id": "scene-old",
        "to_scene_id": "scene-town",
        "source_excerpt": "The survivors carry the Stone to Town.",
        "source_ref": citation_ref,
    }


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
            if tool_id == "campaign_rules" and arguments["action"] == "get_profile":
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
            if tool_id == "campaign_rules" and arguments["action"] == "core_relock":
                assert arguments["payload"]["expected_core_fingerprint"] == "old-core"
                assert arguments["payload"]["expected_head_snapshot_id"] == "snapshot-1"
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
    assert client.tools.count("campaign_rules") == 2


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


def test_module_refresh_identity_is_retry_stable_and_revision_sensitive(
    tmp_path: Path,
) -> None:
    source = tmp_path / "module.md"
    source.write_text("# First revision", encoding="utf-8")
    first = _module_refresh_identity(
        old_module_id="module-1",
        source_key="campaign",
        source_path=source,
        title="Campaign",
        parser_revision="dnd5e:21",
    )
    assert first == _module_refresh_identity(
        old_module_id="module-1",
        source_key="campaign",
        source_path=source,
        title="Campaign",
        parser_revision="dnd5e:21",
    )

    source.write_text("# Second revision", encoding="utf-8")
    changed_content = _module_refresh_identity(
        old_module_id="module-1",
        source_key="campaign",
        source_path=source,
        title="Campaign",
        parser_revision="dnd5e:21",
    )
    changed_parent = _module_refresh_identity(
        old_module_id="module-2",
        source_key="campaign",
        source_path=source,
        title="Campaign",
        parser_revision="dnd5e:21",
    )
    changed_parser = _module_refresh_identity(
        old_module_id="module-1",
        source_key="campaign",
        source_path=source,
        title="Campaign",
        parser_revision="dnd5e:22",
    )

    assert changed_content != first
    assert changed_parent != changed_content
    assert changed_parser != changed_content


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
                "sheet": {"adventure_state": {"status_tags": ["narrative_only", "source_bound"]}},
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
                    "content": ("Qelline Alderleaf is a pragmatic farmer and can introduce Carp."),
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
            occurrence_id="qelline-alderleaf-introduction",
            initial_phase="play",
            scene_id="scene-1",
            location_key="alderleaf-farm",
            source_excerpt=("Qelline Alderleaf is a pragmatic farmer and can introduce Carp."),
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
    assert result["occurrence_id"] == "qelline-alderleaf-introduction"
    assert result["actor"]["id"] == "npc-1"
    assert result["narrative_npc"]["combat_eligible"] is False
    assert client.manifest["npcs"][0]["actor_id"] == "npc-1"
    assert "combat_statblock=not_imported" in client.manifest["npcs"][0]["notes"]
    assert client.snapshot_calls == (0 if defer_checkpoint else 1)
    if defer_checkpoint:
        assert result["checkpoint"] is None
    else:
        assert result["checkpoint"]["verification"]["valid"] is True


def test_narrative_npc_driver_requires_canonical_anonymous_instance_name() -> None:
    with pytest.raises(ValueError, match="anonymous narrative NPC name"):
        asyncio.run(
            _prepare_narrative_npc(
                object(),
                campaign_id="campaign-1",
                run_id="run-1",
                occurrence_id="anonymous-1",
                initial_phase="play",
                scene_id="scene-1",
                location_key="gate",
                source_excerpt="Two townsfolk wait by the gate.",
                source_ref={},
                name="Invented Mayor",
                role="Anonymous source-counted townsperson.",
                summary="A separately tracked anonymous townsperson.",
                faction="Greenest",
                relationship="rescued civilian",
                source_identity="Townsfolk",
                instance_key="retreat-1",
            )
        )


@pytest.mark.parametrize("defer_checkpoint", [False, True])
def test_shared_consumable_driver_keeps_roll_item_and_healing_in_one_transition(
    defer_checkpoint: bool,
) -> None:
    class Client:
        def __init__(self) -> None:
            self.revision = 10
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
            if tool_id == "memory_change":
                self.continuity_payload = deepcopy(arguments["payload"])
                return {
                    "event": {"id": "event-1"},
                    **({} if defer_checkpoint else {"snapshot": {"slot": 8}}),
                }
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
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert client.tools.count("campaign_change") == 1
    assert result["use"]["roll"]["total"] == 7
    assert result["knowledge_actor_ids"] == ["actor-1", "actor-2"]
    assert ("snapshot" in client.continuity_payload) is not defer_checkpoint


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
            if tool_id == "memory_change":
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


def test_source_loot_driver_rejects_implicit_empty_spellbook() -> None:
    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            raise AssertionError((tool_id, arguments))

    with pytest.raises(ValueError, match="requires explicit mechanics"):
        asyncio.run(
            _acquire_source_loot(
                Client(),
                campaign_id="campaign-1",
                run_id="run-1",
                scene_id="scene-1",
                location_key="treasure-room",
                source_excerpt="The spellbook contains six named spells.",
                source_ref={},
                acquisition_id="spellbook-loot",
                coins={},
                items=[
                    {
                        "id": "recovered-spellbook",
                        "name": "Recovered spellbook",
                        "kind": "spellbook",
                        "quantity": 1,
                    }
                ],
                reason="The party recovered the source-defined spellbook.",
                knowledge_actor_ids=["actor-1"],
            )
        )


def test_source_loot_driver_accepts_explicit_spellbook_contents() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 1,
        "page_end": 1,
        "heading_path": ["Treasure"],
        "content_sha256": "a" * 64,
    }

    class Client:
        def __init__(self) -> None:
            self.revision = 4

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
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "The spellbook contains burning hands.",
                    "spatial": {"locations": [{"key": "treasure-room", "title": "Treasure Room"}]},
                }
            if tool_id == "campaign_change":
                self.revision += 1
                return {
                    "status": "committed",
                    "acquisition_id": "spellbook-loot",
                    "coins": {},
                    "items": arguments["payload"]["items"],
                }
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "memory_change":
                self.revision += 1
                return {"event": {"id": "event-1"}}
            if tool_id == "playthrough_manifest":
                return {"manifest": {"status": "in_progress"}}
            raise AssertionError((tool_id, arguments))

    item = {
        "id": "recovered-spellbook",
        "name": "Recovered spellbook",
        "kind": "spellbook",
        "quantity": 1,
        "mechanics": {
            "edition": "2014",
            "spell_ids": [],
            "unresolved_spell_names": ["Burning Hands"],
            "owner_mark": "The defeated mage",
            "source_scene_id": "scene-1",
            "deciphered": False,
            "copyable": True,
        },
    }
    result = asyncio.run(
        _acquire_source_loot(
            Client(),
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            location_key="treasure-room",
            source_excerpt="spellbook contains burning hands",
            source_ref=source_ref,
            acquisition_id="spellbook-loot",
            coins={},
            items=[item],
            reason="The party recovered the source-defined spellbook.",
            knowledge_actor_ids=["actor-1"],
            defer_checkpoint=True,
        )
    )

    assert result["acquisition"]["items"] == [item]


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


def test_source_item_driver_enriches_an_existing_item_through_public_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "reference-scene",
        "chunk_id": "stone-chunk",
        "page_start": 193,
        "page_end": 193,
        "heading_path": ["Appendix A", "Stone of Golorr"],
        "content_sha256": "b" * 64,
    }
    requested = {
        "id": "stone-of-golorr",
        "name": "Stone of Golorr",
        "kind": "magic_item",
        "source_key": "module-chunk:stone-chunk",
        "attunement": "attuned",
        "charges": {
            "label": "Legend Lore charges",
            "value": 3,
            "max": 3,
            "recovers_on": "dawn",
            "source_key": "module-chunk:stone-chunk",
        },
        "mechanics": {
            "rarity": "artifact",
            "requires_attunement": True,
            "spellcasting": {
                "requires_attunement": True,
                "requires_class_spell_list": False,
                "components_required": False,
                "spells": [
                    {
                        "artifact_id": "dnd5e.content.srd2014.spell.legend-lore",
                        "charge_cost": 1,
                        "casting_time": "10 minutes",
                    }
                ],
            },
        },
    }

    class Client:
        def __init__(self) -> None:
            sheet = default_character_sheet()
            sheet["inventory"]["items"].append(
                {
                    "id": "stone-of-golorr",
                    "name": "Stone of Golorr",
                    "kind": "magic_item",
                    "quantity": 1,
                    "weight_oz": 0,
                    "price_cp": 0,
                    "description": "",
                    "source_key": "module-chunk:stone-chunk",
                    "container_id": None,
                    "equipped": False,
                    "equipped_slot": None,
                    "identified": False,
                    "attunement": "attuned",
                    "condition": "normal",
                    "uses": {
                        "label": "",
                        "value": 0,
                        "max": 0,
                        "recovers_on": "none",
                        "source_key": "",
                        "slot_level": 0,
                    },
                    "charges": deepcopy(requested["charges"]),
                    "mechanics": {
                        "rarity": "artifact",
                        "requires_attunement": True,
                    },
                }
            )
            self.actor = {
                "id": "pip",
                "name": "Pip",
                "campaign_id": "campaign-1",
                "revision": 9,
                "sheet": sheet,
                "derived": {"armor_class": 15},
            }
            self.inventory_arguments: dict | None = None

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "reference-scene",
                    "content": "Wondrous item, artifact (requires attunement)",
                }
            if tool_id == "character_query":
                return deepcopy(self.actor)
            if tool_id == "inventory_change":
                self.inventory_arguments = deepcopy(arguments)
                assert arguments["action"] == "update"
                patch = deepcopy(arguments["payload"]["patch"])
                patch["mechanics"]["spellcasting"]["spells"][0]["card"] = {
                    "id": "dnd5e.content.srd2014.spell.legend-lore",
                    "pack_id": "dnd5e.content.srd2014",
                }
                self.actor["sheet"]["inventory"]["items"][0].update(patch)
                self.actor["revision"] += 1
                return {"character": deepcopy(self.actor)}
            raise AssertionError((tool_id, arguments))

    async def checkpoint(*_args, **_kwargs):
        raise AssertionError("deferred source enrichment must not checkpoint")

    monkeypatch.setattr(regression_playthrough, "_checkpoint", checkpoint)
    client = Client()
    result = asyncio.run(
        _provision_source_item(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            actor_id="pip",
            source_scene_id="reference-scene",
            source_excerpt="requires attunement",
            source_ref=source_ref,
            item=requested,
            equip_slot="",
            reason="Bind the source-defined Legend Lore use.",
            checkpoint_label="",
            defer_checkpoint=True,
        )
    )

    assert client.inventory_arguments is not None
    assert client.inventory_arguments["action"] == "update"
    assert result["add_recovered"] is True
    assert result["update_recovered"] is True
    assert (
        result["item"]["mechanics"]["spellcasting"]["spells"][0]["card"]["id"]
        == "dnd5e.content.srd2014.spell.legend-lore"
    )


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
            occurrence_id="staff-handoff-1",
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
    assert client.transfer_arguments["idempotency_key"] == _mutation_key(
        "run-1",
        "source-item-transfer",
        _occurrence_identity("staff-handoff-1", "transfer-source-item"),
    )
    assert result["transfer"]["item"]["id"] == "staff-of-defense"
    assert checkpoint_calls == (0 if defer_checkpoint else 1)
    if defer_checkpoint:
        assert result["checkpoint"] is None
    else:
        assert result["checkpoint"]["verification"]["valid"] is True


def test_source_item_transfer_driver_uses_atomic_character_to_character_public_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "ambush-chunk",
        "page_start": 63,
        "page_end": 63,
        "heading_path": ["Encounter 1: Alley"],
        "content_sha256": "b" * 64,
    }
    stone = {
        "id": "stone-of-golorr",
        "name": "Stone of Golorr",
        "kind": "magic_item",
        "quantity": 1,
    }

    class Client:
        def __init__(self) -> None:
            source_sheet = default_character_sheet()
            source_sheet["inventory"]["items"].append(deepcopy(stone))
            self.source = {
                "id": "pip",
                "campaign_id": "campaign-1",
                "revision": 7,
                "sheet": source_sheet,
            }
            self.target = {
                "id": "morga",
                "campaign_id": "campaign-1",
                "revision": 3,
                "sheet": default_character_sheet(),
            }
            self.transfer_arguments: dict | None = None

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": 24}}

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "If these creatures obtain the stone, they bring it to Xanathar.",
                    "spatial": {"locations": [{"key": "alley", "title": "Alley"}]},
                }
            if tool_id == "character_query":
                actor_id = arguments["payload"]["character_id"]
                return deepcopy(self.source if actor_id == "pip" else self.target)
            if tool_id == "inventory_transfer":
                self.transfer_arguments = deepcopy(arguments)
                moved = self.source["sheet"]["inventory"]["items"].pop()
                self.target["sheet"]["inventory"]["items"].append(deepcopy(moved))
                return {
                    "source": deepcopy(self.source),
                    "target": deepcopy(self.target),
                    "item": deepcopy(moved),
                }
            raise AssertionError((tool_id, arguments))

    async def checkpoint(*_args, **_kwargs):
        return {"snapshot": {"slot": 51}, "verification": {"valid": True}}

    monkeypatch.setattr(regression_playthrough, "_checkpoint", checkpoint)
    client = Client()
    result = asyncio.run(
        _transfer_source_item_to_party(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="morga-takes-stone",
            scene_id="scene-1",
            location_key="alley",
            source_excerpt="If these creatures obtain the stone, they bring it to Xanathar.",
            source_ref=source_ref,
            character_id="pip",
            recipient_character_id="morga",
            item_id="stone-of-golorr",
            quantity=1,
            reason="Morga takes the Stone from the defeated party.",
            checkpoint_label="Morga takes the Stone",
        )
    )

    assert client.transfer_arguments is not None
    assert client.transfer_arguments["mode"] == "character_to_character"
    assert client.transfer_arguments["payload"] == {
        "source_character_id": "pip",
        "target_character_id": "morga",
        "item_id": "stone-of-golorr",
        "expected_campaign_revision": 24,
        "expected_source_revision": 7,
        "expected_target_revision": 3,
        "quantity": 1,
    }
    assert result["recipient_character_id"] == "morga"
    assert result["transfer"]["item"]["id"] == "stone-of-golorr"


@pytest.mark.parametrize("defer_checkpoint", [False, True])
def test_party_item_claim_driver_uses_atomic_party_to_character_public_tool(
    defer_checkpoint: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "gazer-chunk",
        "page_start": 79,
        "page_end": 79,
        "heading_path": ["Old Tower", "Gazer Attack"],
        "content_sha256": "a" * 64,
    }
    stone = {
        "id": "stone-of-golorr",
        "name": "Stone of Golorr",
        "kind": "magic_item",
        "quantity": 1,
    }

    class Client:
        def __init__(self) -> None:
            sheet = default_character_sheet()
            self.actor = {
                "id": "pip",
                "name": "Pip",
                "campaign_id": "campaign-1",
                "revision": 7,
                "sheet": sheet,
                "derived": {"armor_class": 15},
            }
            self.party = {"inventory": {"items": [deepcopy(stone)]}}
            self.transfer_arguments: dict | None = None

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            if arguments["view"] == "party":
                return {"result": deepcopy(self.party)}
            return {"result": {"id": "campaign-1", "revision": 21}}

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "They use telekinetic rays to steal it.",
                    "spatial": {"locations": [{"key": "upper-level", "title": "Upper Level"}]},
                }
            if tool_id == "character_query":
                return deepcopy(self.actor)
            if tool_id == "inventory_transfer":
                self.transfer_arguments = deepcopy(arguments)
                moved = self.party["inventory"]["items"].pop()
                self.actor["sheet"]["inventory"]["items"].append(deepcopy(moved))
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
        return {"snapshot": {"slot": 14}, "verification": {"valid": True}}

    monkeypatch.setattr(regression_playthrough, "_checkpoint", checkpoint)
    client = Client()
    result = asyncio.run(
        _claim_party_item_for_character(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="stone-bearer-1",
            scene_id="scene-1",
            location_key="upper-level",
            source_excerpt="They use telekinetic rays to steal it.",
            source_ref=source_ref,
            character_id="pip",
            item_id="stone-of-golorr",
            quantity=None,
            reason="The party entrusts the recovered Stone to Pip.",
            checkpoint_label="Pip carries the Stone",
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert client.transfer_arguments is not None
    assert client.transfer_arguments["mode"] == "party_to_character"
    assert client.transfer_arguments["payload"]["expected_campaign_revision"] == 21
    assert client.transfer_arguments["payload"]["expected_character_revision"] == 7
    assert client.transfer_arguments["idempotency_key"] == _mutation_key(
        "run-1",
        "party-item-claim",
        _occurrence_identity("stone-bearer-1", "claim-party-item"),
    )
    assert result["transfer"]["item"]["id"] == "stone-of-golorr"
    assert checkpoint_calls == (0 if defer_checkpoint else 1)
    if defer_checkpoint:
        assert result["checkpoint"] is None
    else:
        assert result["checkpoint"]["verification"]["valid"] is True


@pytest.mark.parametrize("defer_checkpoint", [False, True])
def test_source_effect_application_uses_public_character_transition(
    defer_checkpoint: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "fresco-chunk",
        "page_start": 96,
        "page_end": 96,
        "heading_path": ["Vault", "Enthralling Fresco"],
        "content_sha256": "a" * 64,
    }
    effect = {
        "id": "fresco-charm",
        "name": "Enthralling Fresco",
        "kind": "timed_conditions",
        "source": "module-chunk:fresco-chunk",
        "duration": {"period": "hour", "remaining": 24},
        "changes": [{"path": "conditions", "mode": "add", "value": "charmed"}],
    }

    class Client:
        def __init__(self) -> None:
            self.actor = {
                "id": "thalia",
                "name": "Thalia",
                "campaign_id": "campaign-1",
                "revision": 9,
                "sheet": default_character_sheet(),
                "derived": {"armor_class": 18},
            }
            self.change_arguments: dict | None = None

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "A failed save charms the creature for 24 hours.",
                    "spatial": {"locations": [{"key": "fresco", "title": "Fresco"}]},
                }
            if tool_id == "character_query":
                return deepcopy(self.actor)
            if tool_id == "character_state_change":
                self.change_arguments = deepcopy(arguments)
                self.actor["sheet"]["effects"] = [
                    {
                        **deepcopy(effect),
                        "active": True,
                        "concentration": False,
                        "source_spell_id": "",
                        "description": "",
                    }
                ]
                self.actor["revision"] += 1
                return {
                    "character": deepcopy(self.actor),
                    "effect_id": "fresco-charm",
                }
            raise AssertionError((tool_id, arguments))

    checkpoint_calls = 0

    async def checkpoint(*_args, **_kwargs):
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        return {"snapshot": {"slot": 15}, "verification": {"valid": True}}

    monkeypatch.setattr(regression_playthrough, "_checkpoint", checkpoint)
    client = Client()
    result = asyncio.run(
        _apply_source_effect(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="fresco-charm-thalia",
            scene_id="scene-1",
            location_key="fresco",
            source_excerpt="A failed save charms the creature for 24 hours.",
            source_ref=source_ref,
            character_id="thalia",
            effect=effect,
            reason="Thalia failed the source-defined Wisdom save.",
            checkpoint_label="Fresco charm applied",
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert client.change_arguments == {
        "character_id": "thalia",
        "action": "effect_add",
        "payload": {"effect": effect},
        "expected_revision": 9,
        "idempotency_key": _mutation_key(
            "run-1",
            "source-effect-add",
            _occurrence_identity("fresco-charm-thalia", "apply-source-effect"),
        ),
    }
    assert result["effect"]["id"] == "fresco-charm"
    assert checkpoint_calls == (0 if defer_checkpoint else 1)


@pytest.mark.parametrize("defer_checkpoint", [False, True])
def test_source_effect_removal_uses_public_character_transition(
    defer_checkpoint: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "gazer-chunk",
        "page_start": 79,
        "page_end": 79,
        "heading_path": ["Old Tower", "Gazer Attack"],
        "content_sha256": "a" * 64,
    }

    class Client:
        def __init__(self) -> None:
            sheet = default_character_sheet()
            sheet["conditions"] = ["frightened"]
            sheet["effects"] = [
                {
                    "id": "fear-ray-effect",
                    "name": "Fear Ray",
                    "kind": "timed_conditions",
                    "source": "gazer",
                    "active": True,
                    "duration": {"period": "source_turn_start", "remaining": 1},
                    "changes": [
                        {
                            "path": "conditions",
                            "mode": "add",
                            "value": "frightened",
                        }
                    ],
                }
            ]
            self.actor = {
                "id": "pip",
                "name": "Pip",
                "campaign_id": "campaign-1",
                "revision": 9,
                "sheet": sheet,
                "derived": {"armor_class": 15},
            }
            self.change_arguments: dict | None = None

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "The target is frightened until the next turn.",
                    "spatial": {"locations": [{"key": "upper-level", "title": "Upper Level"}]},
                }
            if tool_id == "character_query":
                return deepcopy(self.actor)
            if tool_id == "character_state_change":
                self.change_arguments = deepcopy(arguments)
                self.actor["sheet"]["effects"] = []
                self.actor["sheet"]["conditions"] = []
                self.actor["revision"] += 1
                return {"character": deepcopy(self.actor)}
            raise AssertionError((tool_id, arguments))

    checkpoint_calls = 0

    async def checkpoint(*_args, **_kwargs):
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        return {"snapshot": {"slot": 15}, "verification": {"valid": True}}

    monkeypatch.setattr(regression_playthrough, "_checkpoint", checkpoint)
    client = Client()
    result = asyncio.run(
        _remove_source_effect(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="fear-cleanup-1",
            scene_id="scene-1",
            location_key="upper-level",
            source_excerpt="The target is frightened until the next turn.",
            source_ref=source_ref,
            character_id="pip",
            effect_id="fear-ray-effect",
            reason="Combat ended before the source's next turn.",
            checkpoint_label="Fear Ray ended",
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert client.change_arguments == {
        "character_id": "pip",
        "action": "effect_remove",
        "payload": {"effect_id": "fear-ray-effect"},
        "expected_revision": 9,
        "idempotency_key": _mutation_key(
            "run-1",
            "source-effect-remove",
            _occurrence_identity("fear-cleanup-1", "remove-source-effect"),
        ),
    }
    assert result["effect"]["id"] == "fear-ray-effect"
    assert checkpoint_calls == (0 if defer_checkpoint else 1)


def test_source_exhaustion_uses_public_character_transition() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "fresco-chunk",
        "page_start": 96,
        "page_end": 96,
        "heading_path": ["Vault", "Enthralling Fresco"],
        "content_sha256": "a" * 64,
    }

    class Client:
        def __init__(self) -> None:
            sheet = default_character_sheet()
            self.actor = {
                "id": "maris",
                "name": "Maris",
                "campaign_id": "campaign-1",
                "revision": 11,
                "sheet": sheet,
                "derived": {"armor_class": 13},
            }
            self.change_arguments: dict | None = None

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "After 24 hours, the creature gains one level of exhaustion.",
                    "spatial": {"locations": [{"key": "fresco", "title": "Fresco"}]},
                }
            if tool_id == "character_query":
                return deepcopy(self.actor)
            if tool_id == "character_state_change":
                self.change_arguments = deepcopy(arguments)
                self.actor["sheet"]["combat"]["exhaustion"] = 1
                self.actor["revision"] += 1
                return {"character": deepcopy(self.actor)}
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _set_source_exhaustion(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="fresco-exhaustion-maris-day-1",
            scene_id="scene-1",
            location_key="fresco",
            source_excerpt="After 24 hours, the creature gains one level of exhaustion.",
            source_ref=source_ref,
            character_id="maris",
            level=1,
            reason="Maris remained charmed for 24 hours.",
            checkpoint_label="",
            defer_checkpoint=True,
        )
    )

    assert client.change_arguments == {
        "character_id": "maris",
        "action": "exhaustion_set",
        "payload": {"value": 1},
        "expected_revision": 11,
        "idempotency_key": _mutation_key(
            "run-1",
            "source-exhaustion-set",
            "fresco-exhaustion-maris-day-1",
        ),
    }
    assert result["before"] == 0
    assert result["after"] == 1


def test_source_object_attack_uses_public_character_action() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "fresco-chunk",
        "page_start": 96,
        "page_end": 96,
        "heading_path": ["Vault", "Enthralling Fresco"],
        "content_sha256": "a" * 64,
    }
    source_object = {
        "id": "fresco-section",
        "name": "Enthralling Fresco Section",
        "scene_id": "scene-1",
        "armor_class": 17,
        "hit_points": 25,
        "damage_immunities": ["poison", "psychic"],
    }

    class Client:
        def __init__(self) -> None:
            self.action_arguments: dict | None = None

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            assert arguments == {
                "view": "get",
                "payload": {"campaign_id": "campaign-1"},
                "principal_id": PRINCIPAL_ID,
            }
            return {"id": "campaign-1", "revision": 12}

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "Each section has AC 17 and 25 hit points.",
                    "spatial": {"locations": [{"key": "fresco", "title": "Fresco"}]},
                }
            if tool_id == "character_query":
                return {
                    "id": "breaker",
                    "campaign_id": "campaign-1",
                    "revision": 7,
                    "sheet": default_character_sheet(),
                }
            if tool_id == "character_action":
                self.action_arguments = deepcopy(arguments)
                return {
                    "status": "committed",
                    "object": {
                        **deepcopy(source_object),
                        "hit_point_maximum": 25,
                        "hit_points": 19,
                        "destroyed": False,
                        "source_ref": deepcopy(source_ref),
                    },
                }
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _attack_source_object(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="fresco-attack-1",
            scene_id="scene-1",
            location_key="fresco",
            source_excerpt="Each section has AC 17 and 25 hit points.",
            source_ref=source_ref,
            character_id="breaker",
            object_state=source_object,
            weapon_id="mace",
            reason="The fresco is within melee reach.",
            advantage=False,
            disadvantage=False,
            checkpoint_label="",
            defer_checkpoint=True,
        )
    )

    assert client.action_arguments == {
        "character_id": "breaker",
        "action": "attack_source_object",
        "payload": {
            "object": source_object,
            "weapon_id": "mace",
            "source_ref": source_ref,
            "reason": "The fresco is within melee reach.",
            "advantage": False,
            "disadvantage": False,
            "expected_campaign_revision": 12,
        },
        "expected_revision": 7,
        "idempotency_key": _mutation_key(
            "run-1",
            "source-object-attack",
            _occurrence_identity("fresco-attack-1", "attack-source-object"),
        ),
    }
    assert result["object"]["hit_points"] == 19


def test_healing_spell_driver_pays_rolls_and_applies_public_healing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "bridge-chunk",
        "page_start": 95,
        "page_end": 96,
        "heading_path": ["Vault", "Bridge"],
        "content_sha256": "a" * 64,
    }
    caster_sheet = default_character_sheet()
    caster_sheet["abilities"]["wisdom"]["score"] = 18
    caster_sheet["spellcasting"]["ability"] = "wisdom"
    caster_sheet["content"]["spells"] = [
        {
            "id": "healing-word",
            "name": "Healing Word",
            "level": 1,
            "resolution": {
                "kind": "healing",
                "targeting": {
                    "mode": "creature",
                    "requires_sight": True,
                    "max_targets": 1,
                    "excluded_creature_types": ["construct", "undead"],
                    "area": None,
                },
                "attack": None,
                "save": None,
                "healing": {
                    "base_dice": "1d4",
                    "per_slot_dice": "1d4",
                    "slot_base_level": 1,
                    "cantrip_dice": {},
                    "add_spellcasting_modifier": True,
                },
            },
        }
    ]
    target_sheet = default_character_sheet()
    target_sheet["combat"]["hp"] = {"value": 0, "max": 20, "temp": 0}
    target_sheet["conditions"] = ["prone", "unconscious"]

    class Client:
        def __init__(self) -> None:
            self.cast_arguments: dict | None = None
            self.roll_arguments: dict | None = None
            self.heal_arguments: dict | None = None

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"id": "campaign-1", "revision": 12, "state": {}}

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "A failed save causes a 60-foot fall.",
                    "spatial": {"locations": [{"key": "bridge", "title": "Bridge"}]},
                }
            if tool_id == "character_query":
                actor_id = arguments["payload"]["character_id"]
                if actor_id == "cleric":
                    return {
                        "id": "cleric",
                        "name": "Cleric",
                        "campaign_id": "campaign-1",
                        "revision": 7,
                        "sheet": deepcopy(caster_sheet),
                    }
                return {
                    "id": "fallen",
                    "name": "Fallen",
                    "campaign_id": "campaign-1",
                    "revision": 4,
                    "sheet": deepcopy(target_sheet),
                }
            if tool_id == "character_action":
                self.cast_arguments = deepcopy(arguments)
                return {"status": "pending_ruling", "result": {"payment": {"cost": 1}}}
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "dnd_dice_roll":
                self.roll_arguments = deepcopy(arguments)
                return {
                    "total": 7,
                    "rolls": [3],
                    "expression": "1d4 + 4",
                    "detail": "1d4[3] +4",
                }
            if tool_id == "character_state_change":
                self.heal_arguments = deepcopy(arguments)
                healed_sheet = deepcopy(target_sheet)
                healed_sheet["combat"]["hp"]["value"] = 7
                healed_sheet["conditions"] = ["prone"]
                return {
                    "character": {
                        "id": "fallen",
                        "revision": 5,
                        "sheet": healed_sheet,
                    }
                }
            if tool_id == "memory_change":
                return {"event": {"id": "event-1"}}
            raise AssertionError((tool_id, arguments))

    async def manifest_mutation(*_args, **_kwargs):
        return {"manifest": {"status": "in_progress"}}

    monkeypatch.setattr(regression_playthrough, "_manifest_mutation", manifest_mutation)
    client = Client()
    result = asyncio.run(
        _cast_healing_spell(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="heal-fallen",
            scene_id="scene-1",
            source_excerpt="A failed save causes a 60-foot fall.",
            source_ref=source_ref,
            location_key="bridge",
            actor_id="cleric",
            target_id="fallen",
            spell_id="healing-word",
            cast_level=1,
            component_ruling=None,
            reason="The cleric restored the fallen ally.",
            knowledge_actor_ids=[],
            defer_checkpoint=True,
        )
    )

    assert client.cast_arguments["action"] == "cast_spell"
    assert client.cast_arguments["payload"] == {
        "spell_id": "healing-word",
        "cast_level": 1,
    }
    assert client.roll_arguments["expression"] == "1d4 + 4"
    assert client.heal_arguments["payload"] == {
        "amount": 7,
        "source_actor_id": "cleric",
        "spell_id": "healing-word",
        "spell_level": 1,
    }
    assert result["roll"]["total"] == 7


def test_healing_spell_driver_returns_precommit_ruling_before_rolling() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "bridge-chunk",
        "page_start": 95,
        "page_end": 96,
        "heading_path": ["Vault", "Bridge"],
        "content_sha256": "a" * 64,
    }
    caster_sheet = default_character_sheet()
    caster_sheet["content"]["spells"] = [
        {
            "id": "healing-word",
            "name": "Healing Word",
            "level": 1,
            "resolution": {
                "kind": "healing",
                "healing": {
                    "base_dice": "1d4",
                    "per_slot_dice": "1d4",
                    "slot_base_level": 1,
                    "add_spellcasting_modifier": False,
                },
            },
        }
    ]

    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "A failed save causes a 60-foot fall.",
                    "spatial": {"locations": [{"key": "bridge", "title": "Bridge"}]},
                }
            if tool_id == "character_query":
                actor_id = arguments["payload"]["character_id"]
                return {
                    "id": actor_id,
                    "name": actor_id.title(),
                    "campaign_id": "campaign-1",
                    "revision": 7,
                    "sheet": deepcopy(
                        caster_sheet
                        if actor_id == "cleric"
                        else default_character_sheet()
                    ),
                }
            if tool_id == "character_action":
                return {
                    "status": "pending_ruling",
                    "default_resolver": "agent",
                    "ruling_kind": "environmental_consequence",
                    "reason": "the active rule pack needs the scene weather",
                    "committed": False,
                    "result": {
                        "status": "pending_ruling",
                        "pending": [{"id": "weather"}],
                    },
                }
            raise AssertionError("a pre-commit ruling must stop before later public writes")

    with pytest.raises(RegressionRulingRequiredError) as raised:
        asyncio.run(
            _cast_healing_spell(
                Client(),
                campaign_id="campaign-1",
                run_id="run-1",
                occurrence_id="heal-fallen",
                scene_id="scene-1",
                source_excerpt="A failed save causes a 60-foot fall.",
                source_ref=source_ref,
                location_key="bridge",
                actor_id="cleric",
                target_id="fallen",
                spell_id="healing-word",
                cast_level=1,
                component_ruling=None,
                reason="The cleric attempted to restore the fallen ally.",
                knowledge_actor_ids=[],
            )
        )

    requirement = raised.value.requirement
    assert requirement["operation"] == "character_action.cast_healing_spell"
    assert requirement["ruling"]["default_resolver"] == "agent"
    assert requirement["ruling"]["committed"] is False


def test_currency_pool_driver_uses_public_atomic_party_transfer() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "source-scene-1",
        "chunk_id": "chunk-1",
        "page_start": 95,
        "page_end": 95,
        "heading_path": ["Vault Keys", "Sunlight"],
        "content_sha256": "a" * 64,
    }

    class Client:
        def __init__(self, existing_progress: dict | None = None) -> None:
            self.campaign_revision = 9
            self.character_revision = 4
            self.wallet_calls: list[dict] = []
            self.progress_arguments: dict = {}
            self.progress_calls: list[dict] = []
            self.existing_progress = deepcopy(existing_progress)
            self.continuity_payload: dict = {}

        async def load(self, *groups: str) -> None:
            assert groups

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.campaign_revision,
                    "state": {"game_phase": "play"},
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                if arguments["view"] == "progress":
                    return (
                        [deepcopy(self.existing_progress)]
                        if self.existing_progress is not None
                        else []
                    )
                scene_id = arguments["payload"]["scene_id"]
                if scene_id == "source-scene-1":
                    return {
                        "module_id": "module-1",
                        "scene_id": scene_id,
                        "content": "Twenty steel mirrors cost 5 gp each.",
                    }
                assert scene_id == "scene-1"
                return {
                    "module_id": "module-1",
                    "scene_id": scene_id,
                    "spatial": {"locations": [{"key": "market", "title": "Market"}]},
                }
            if tool_id == "character_query":
                return {
                    "id": "actor-1",
                    "campaign_id": "campaign-1",
                    "revision": self.character_revision,
                }
            if tool_id == "wallet_change":
                self.wallet_calls.append(deepcopy(arguments))
                expected = arguments["payload"]
                assert expected == {
                    "character_id": "actor-1",
                    "expected_campaign_revision": 9,
                    "expected_character_revision": 4,
                }
                self.campaign_revision += 1
                self.character_revision += 1
                return {
                    "result": {
                        "party": {"inventory": {"wallet": {"gp": 25}}},
                        "character": {
                            "id": "actor-1",
                            "sheet": {"inventory": {"wallet": {"gp": 5}}},
                        },
                    }
                }
            if tool_id == "module_set_progress":
                self.progress_arguments = deepcopy(arguments)
                self.progress_calls.append(deepcopy(arguments))
                self.existing_progress = {
                    "scene_id": "scene-1",
                    "scope_id": "party",
                    "status": "active",
                    "progress": 0,
                    "state_version": (int(arguments.get("expected_state_version", 0)) + 1),
                    "state": deepcopy(arguments["state"]),
                }
                return deepcopy(self.existing_progress)
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "memory_change":
                self.continuity_payload = deepcopy(arguments["payload"])
                self.campaign_revision += 1
                return {"event": {"id": "event-1"}}
            if tool_id == "playthrough_manifest":
                assert arguments["action"] == "sync"
                return {
                    "manifest": {"status": "in_progress"},
                    "campaign_revision": self.campaign_revision,
                }
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _pool_character_currency(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="pool-1",
            scene_id="scene-1",
            source_scene_id="source-scene-1",
            location_key="market",
            source_excerpt="Twenty steel mirrors cost 5 gp each.",
            source_ref=source_ref,
            actor_id="actor-1",
            denomination="gp",
            amount=10,
            reason="The actor pools 10 gp for the source-defined mirrors.",
            defer_checkpoint=True,
        )
    )

    assert len(client.wallet_calls) == 1
    assert client.wallet_calls[-1]["owner"] == "party"
    assert client.wallet_calls[-1]["action"] == "transfer_from_character"
    pool_state = client.progress_arguments["state"]["full_playthrough_currency_pools"]
    assert next(iter(pool_state.values()))["amount"] == 10
    assert "snapshot" not in client.continuity_payload
    assert result["recovered"] is False

    distribution_client = Client()
    distributed = asyncio.run(
        _pool_character_currency(
            distribution_client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="distribution-1",
            scene_id="scene-1",
            source_scene_id="source-scene-1",
            location_key="market",
            source_excerpt="Twenty steel mirrors cost 5 gp each.",
            source_ref=source_ref,
            actor_id="actor-1",
            denomination="gp",
            amount=10,
            reason="The party pays the actor 10 gp from a source-defined reward.",
            defer_checkpoint=True,
            direction="to_character",
        )
    )

    assert len(distribution_client.wallet_calls) == 1
    assert distribution_client.wallet_calls[-1]["owner"] == "party"
    assert distribution_client.wallet_calls[-1]["action"] == "transfer_to_character"
    distribution_state = distribution_client.progress_arguments["state"][
        "full_playthrough_currency_distributions"
    ]
    assert next(iter(distribution_state.values()))["amount"] == 10
    assert distributed["direction"] == "to_character"

    stale_identity = _occurrence_identity("stale-distribution-1", "distribute-coins")
    stale_token = regression_playthrough._token(stale_identity)
    stale_reason = "The party pays the actor 10 gp from a source-defined reward."
    stale_client = Client(
        {
            "scene_id": "scene-1",
            "scope_id": "party",
            "status": "active",
            "progress": 0,
            "state_version": 3,
            "state": {
                "full_playthrough_currency_distributions": {
                    stale_token: {
                        "occurrence_id": stale_identity,
                        "actor_id": "actor-1",
                        "denomination": "gp",
                        "amount": 10,
                        "reason": stale_reason,
                        "source_ref": source_ref,
                        "status": "planned",
                        "expected_campaign_revision": 10,
                        "expected_character_revision": 4,
                    }
                }
            },
        }
    )
    recovered = asyncio.run(
        _pool_character_currency(
            stale_client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="stale-distribution-1",
            scene_id="scene-1",
            source_scene_id="source-scene-1",
            location_key="market",
            source_excerpt="Twenty steel mirrors cost 5 gp each.",
            source_ref=source_ref,
            actor_id="actor-1",
            denomination="gp",
            amount=10,
            reason=stale_reason,
            defer_checkpoint=True,
            direction="to_character",
        )
    )

    assert len(stale_client.progress_calls) == 2
    rebound_plan = stale_client.progress_calls[0]["state"][
        "full_playthrough_currency_distributions"
    ][stale_token]
    assert rebound_plan["expected_campaign_revision"] == 9
    assert stale_client.wallet_calls[-1]["payload"]["expected_campaign_revision"] == 9
    assert recovered["direction"] == "to_character"


def test_currency_pool_driver_recovers_completed_progress_without_double_transfer() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "source-scene-1",
        "chunk_id": "chunk-1",
        "page_start": 95,
        "page_end": 95,
        "heading_path": ["Vault Keys", "Sunlight"],
        "content_sha256": "a" * 64,
    }
    identity = _occurrence_identity("pool-1", "pool-coins")
    existing = {
        "occurrence_id": identity,
        "actor_id": "actor-1",
        "denomination": "gp",
        "amount": 10,
        "reason": "The actor pools 10 gp.",
        "source_ref": source_ref,
        "status": "completed",
        "expected_campaign_revision": 8,
        "expected_character_revision": 3,
    }

    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                if arguments["view"] == "progress":
                    return [
                        {
                            "scene_id": "scene-1",
                            "scope_id": "party",
                            "status": "active",
                            "progress": 0,
                            "state_version": 1,
                            "state": {
                                "full_playthrough_currency_pools": {
                                    regression_playthrough._token(identity): existing
                                }
                            },
                        }
                    ]
                scene_id = arguments["payload"]["scene_id"]
                if scene_id == "source-scene-1":
                    return {
                        "module_id": "module-1",
                        "scene_id": scene_id,
                        "content": "Twenty steel mirrors cost 5 gp each.",
                    }
                return {
                    "module_id": "module-1",
                    "scene_id": scene_id,
                    "spatial": {"locations": [{"key": "market", "title": "Market"}]},
                }
            raise AssertionError((tool_id, arguments))

    result = asyncio.run(
        _pool_character_currency(
            Client(),
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="pool-1",
            scene_id="scene-1",
            source_scene_id="source-scene-1",
            location_key="market",
            source_excerpt="Twenty steel mirrors cost 5 gp each.",
            source_ref=source_ref,
            actor_id="actor-1",
            denomination="gp",
            amount=10,
            reason="The actor pools 10 gp.",
            defer_checkpoint=True,
        )
    )

    assert result["recovered"] is True
    assert result["transfer"] is None


@pytest.mark.parametrize("defer_checkpoint", [False, True])
def test_source_currency_spend_driver_uses_one_public_atomic_campaign_transition(
    defer_checkpoint: bool,
) -> None:
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
            self.continuity_payload: dict = {}

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
            if tool_id == "memory_change":
                self.continuity_payload = deepcopy(arguments["payload"])
                assert len(arguments["payload"]["actor_knowledge"]) == 2
                assert arguments["payload"]["event"]["event_type"] == "currency_spent"
                self.revision += 1
                return {
                    "event": {"id": "event-1"},
                    **({} if defer_checkpoint else {"snapshot": {"slot": 7}}),
                }
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
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert result["spend"]["status"] == "committed"
    assert client.tools.count("campaign_change") == 1
    assert result["knowledge_actor_ids"] == ["actor-1", "actor-2"]
    assert result["scene"]["location_key"] == "inn"
    assert ("snapshot" in client.continuity_payload) is not defer_checkpoint


@pytest.mark.parametrize("defer_checkpoint", [False, True])
def test_source_item_spend_driver_uses_one_public_atomic_campaign_transition(
    defer_checkpoint: bool,
) -> None:
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
            self.continuity_payload: dict = {}

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
            if tool_id == "memory_change":
                self.continuity_payload = deepcopy(arguments["payload"])
                assert len(arguments["payload"]["actor_knowledge"]) == 3
                assert arguments["payload"]["event"]["event_type"] == "item_spent"
                self.revision += 1
                return {
                    "event": {"id": "event-1"},
                    **({} if defer_checkpoint else {"snapshot": {"slot": 7}}),
                }
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
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert result["spend"]["status"] == "committed"
    assert result["spend"]["removed"]["id"] == "severed-head"
    assert client.tools.count("campaign_change") == 1
    assert result["knowledge_actor_ids"] == ["actor-1", "actor-2", "nothic"]
    assert ("snapshot" in client.continuity_payload) is not defer_checkpoint


def test_query_source_searches_and_expands_only_public_mcp_results() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append((tool_id, arguments))
            if tool_id == "module_search":
                return {"result": [{"id": "chunk-1", "content": "A captured character..."}]}
            if tool_id == "playthrough_manifest":
                return {
                    "manifest": {
                        "current": {"module_id": "module-1"},
                    }
                }
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
    assert result["preferred_module_id"] == "module-1"
    assert result["expanded_chunks"][0]["chunk_id"] == "chunk-1"
    assert result["expanded_chunks"][0]["source_ref"]["content_sha256"] == "a" * 64
    assert client.calls == [
        (
            "playthrough_manifest",
            {"campaign_id": "campaign-1", "action": "get"},
        ),
        (
            "module_search",
            {
                "campaign_id": "campaign-1",
                "query": "captured defeated characters",
                "top_k": 4,
                "module_ids": ["module-1"],
            },
        ),
        ("module_expand", {"chunk_id": "chunk-1"}),
    ]


def test_query_source_scopes_search_to_the_current_manifest_module_revision() -> None:
    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_search":
                assert arguments["module_ids"] == ["new-module"]
                return {
                    "result": [
                        {"id": "new-chunk", "source_id": "new-module", "score": 1.0},
                    ]
                }
            if tool_id == "playthrough_manifest":
                return {
                    "manifest": {
                        "current": {"module_id": "new-module"},
                    }
                }
            if tool_id == "module_expand":
                chunk_id = arguments["chunk_id"]
                return {"chunk_id": chunk_id}
            raise AssertionError((tool_id, arguments))

    result = asyncio.run(
        _query_source(
            Client(),
            campaign_id="campaign-1",
            query="level advancement",
            top_k=3,
            expand=True,
        )
    )

    assert [item["id"] for item in result["hits"]] == ["new-chunk"]
    assert [item["chunk_id"] for item in result["expanded_chunks"]] == ["new-chunk"]


def test_query_source_explicit_module_works_before_manifest_initialization() -> None:
    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            assert tool_id == "module_search"
            assert arguments == {
                "campaign_id": "campaign-1",
                "query": "Outline of Episodes",
                "top_k": 5,
                "module_ids": ["module-1"],
            }
            return {"result": []}

    result = asyncio.run(
        _query_source(
            Client(),
            campaign_id="campaign-1",
            query="Outline of Episodes",
            top_k=5,
            expand=False,
            module_id=" module-1 ",
        )
    )

    assert result["preferred_module_id"] == "module-1"
    assert result["hits"] == []


def test_index_source_uses_exact_public_mcp_module_query() -> None:
    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            assert tool_id == "module_query"
            assert arguments == {
                "campaign_id": "campaign-1",
                "view": "index",
                "payload": {"module_id": "module-1"},
            }
            return [
                {
                    "module_id": "module-1",
                    "chapter_id": "chapter-1",
                    "scene_id": "scene-1",
                }
            ]

    result = asyncio.run(
        _index_source(
            Client(),
            campaign_id="campaign-1",
            module_id="module-1",
        )
    )

    assert result["module_id"] == "module-1"
    assert result["scenes"][0]["scene_id"] == "scene-1"


def test_read_scene_uses_exact_public_mcp_scene_query() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append((tool_id, arguments))
            return {
                "scene_id": "scene-1",
                "title": "Triboar Trail",
                "content": "Assume that the party travels twenty-four miles per day.",
            }

    client = Client()
    result = asyncio.run(
        _read_scene(
            client,
            campaign_id="campaign-1",
            scene_id="  scene-1  ",
        )
    )

    assert result["scene_id"] == "scene-1"
    assert client.calls == [
        (
            "module_query",
            {
                "campaign_id": "campaign-1",
                "view": "scene",
                "payload": {"scene_id": "scene-1", "scope_id": "dm"},
            },
        )
    ]


def test_read_scene_rejects_mismatched_public_result() -> None:
    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            return {"scene_id": "scene-other"}

    with pytest.raises(RuntimeError, match="different scene"):
        asyncio.run(
            _read_scene(
                Client(),
                campaign_id="campaign-1",
                scene_id="scene-1",
            )
        )


def test_source_table_roll_is_public_replayable_and_deferred() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 27,
        "page_end": 27,
        "heading_path": ["Triboar Trail", "Wilderness Encounters"],
        "content_sha256": "a" * 64,
    }

    class Client:
        def __init__(self) -> None:
            self.campaign_revision = 10
            self.calls: list[tuple[str, dict]] = []

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.campaign_revision,
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append((tool_id, arguments))
            if tool_id == "module_query" and arguments["view"] == "scene":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": (
                        "Check for encounters once during the day and once at night "
                        "by rolling a d20."
                    ),
                    "locations": [{"key": "triboar-trail"}],
                }
            if tool_id == "module_query" and arguments["view"] == "progress":
                return [
                    {
                        "scene_id": "scene-1",
                        "status": "active",
                        "progress": 25,
                        "state_version": 2,
                        "state": {},
                    }
                ]
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "dnd_dice_roll":
                assert arguments["expression"] == "1d20"
                assert arguments["expected_campaign_revision"] == 10
                self.campaign_revision += 1
                return {
                    "total": 18,
                    "rolls": [18],
                    "random_stream_receipt": {
                        "start_position": 42,
                        "end_position": 43,
                    },
                }
            if tool_id == "module_set_progress":
                stored = arguments["state"]["full_playthrough_rolls"]
                assert next(iter(stored.values()))["result"]["total"] == 18
                assert arguments["expected_state_version"] == 2
                return {"scene_id": "scene-1", "state_version": 3}
            if tool_id == "memory_change":
                event = arguments["payload"]["event"]
                assert event["event_type"] == "source_table_roll"
                assert event["audience_scope"] == "dm"
                assert event["payload"]["result"]["total"] == 18
                assert "snapshot" not in arguments["payload"]
                self.campaign_revision += 1
                return {"event": {"id": "event-1"}, "snapshot": None}
            if tool_id == "playthrough_manifest":
                assert arguments["action"] == "sync"
                return {
                    "manifest": {"status": "in_progress"},
                    "campaign_revision": self.campaign_revision,
                }
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _roll_source_table(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            location_key="triboar-trail",
            source_excerpt=(
                "Check for encounters once during the day and once at night by rolling a d20."
            ),
            source_ref=source_ref,
            roll_id="travel-day-1-daylight",
            expression="1d20",
            reason="Daylight wilderness encounter check.",
            audience_scope="dm",
            defer_checkpoint=True,
        )
    )

    assert result["roll"]["total"] == 18
    assert result["random_stream_receipt"]["end_position"] == 43
    dice_call = next(args for tool, args in client.calls if tool == "dnd_dice_roll")
    assert dice_call["idempotency_key"].startswith("full-playthrough-source-roll-")


def test_stable_party_recovery_uses_one_public_campaign_transition() -> None:
    class Client:
        def __init__(self) -> None:
            self.tools: list[str] = []
            self.keys: dict[str, str] = {}

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": 8,
                    "state": {"world_time": {"day": 1, "elapsed_minutes": 1080}},
                }
            }

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
            if tool_id == "memory_change":
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
            occurrence_id="stable-recovery-after-hideout",
            actor_ids=["actor-1", "actor-2"],
            knowledge_actor_ids=["witness"],
            reason="Both stable adventurers recovered while the party waited.",
        )
    )

    assert result["recovery"]["elapsed_hours"] == 4
    assert client.tools.count("campaign_change") == 1
    identity = _occurrence_identity(
        "stable-recovery-after-hideout",
        "recover-stable",
    )
    assert client.keys == {
        "recovery": _mutation_key("run-1", "stable-recovery", identity),
        "continuity": _mutation_key("run-1", "stable-recovery-continuity", identity),
        "sync": _mutation_key("run-1", "sync", f"stable-recovery-sync:{identity}"),
    }


def test_initialize_clock_commits_one_public_dm_anchor_and_replays_from_state() -> None:
    class Client:
        def __init__(self) -> None:
            self.world_time: dict = {}
            self.calls = 0

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": 8,
                    "state": {"world_time": self.world_time},
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "campaign_change":
                assert arguments["action"] == "clock_set"
                assert arguments["payload"] == {
                    "day": 1,
                    "hour": 18,
                    "minute": 0,
                    "label": "Yawning Portal opening",
                }
                self.calls += 1
                self.world_time = {
                    "schema_version": 1,
                    **arguments["payload"],
                    "elapsed_minutes": 1080,
                }
                return {"status": "committed", "world_time": self.world_time}
            raise AssertionError((tool_id, arguments))

    client = Client()
    first = asyncio.run(
        _initialize_clock(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="waterdeep-opening-clock",
            start_clock={
                "day": 1,
                "hour": 18,
                "label": "Yawning Portal opening",
            },
        )
    )
    replay = asyncio.run(
        _initialize_clock(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="waterdeep-opening-clock",
            start_clock={
                "day": 1,
                "hour": 18,
                "label": "Yawning Portal opening",
            },
        )
    )

    assert first["already_initialized"] is False
    assert replay["already_initialized"] is True
    assert replay["world_time"]["elapsed_minutes"] == 1080
    assert client.calls == 1


def test_occurrence_identity_separates_repeated_equivalent_mutations() -> None:
    first = _occurrence_identity("stable-recovery-1", "recover-stable")

    assert first == _occurrence_identity("stable-recovery-1", "recover-stable")
    assert first != _occurrence_identity("stable-recovery-2", "recover-stable")
    with pytest.raises(ValueError, match="requires --occurrence-id"):
        _occurrence_identity(" ", "recover-stable")
    with pytest.raises(ValueError, match="must not exceed 200"):
        _occurrence_identity("x" * 201, "recover-stable")
    first_checkpoint = _occurrence_identity("scene-visit-1", "checkpoint")
    second_checkpoint = _occurrence_identity("scene-visit-2", "checkpoint")
    assert _mutation_key("run", "snapshot", first_checkpoint) != _mutation_key(
        "run", "snapshot", second_checkpoint
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


def test_module_revision_remaps_exact_ending_source_and_scene_check() -> None:
    source_ref = _manifest_source_ref()
    source_ref.update(
        {
            "asset_sha256": "f" * 64,
            "module_id": "module-v1",
            "scene_id": "ending-v1",
            "chunk_id": "chunk-v1",
            "chunk_content_sha256": "e" * 64,
            "excerpt": "The characters should be 5th level.",
        }
    )
    manifest = new_playthrough_manifest(
        run_id="run-1",
        campaign_line_id="line-1",
        module_ids=["module-v1", "module-v2"],
        recommended_party_minimum=1,
        recommended_party_maximum=1,
        selected_party_size=1,
        source_refs=[],
    )
    manifest["ending"]["conditions"] = [
        {
            "id": "ending",
            "label": "Reach the conclusion",
            "source_ref": source_ref,
            "all_of": [
                {
                    "kind": "manifest_value",
                    "path": "current.scene_id",
                    "actor_id": "",
                    "fact_key": "",
                    "operator": "equals",
                    "value": "ending-v1",
                },
                {
                    "kind": "actor_value",
                    "path": "sheet.progression.level",
                    "actor_id": "predecessor",
                    "fact_key": "",
                    "operator": "at_least",
                    "value": 5,
                },
            ],
        }
    ]
    manifest["party"]["replacements"] = [
        {
            "predecessor_actor_id": "predecessor",
            "replacement_actor_id": "replacement",
            "handoff_event_id": "event-1",
        }
    ]

    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_search":
                assert arguments["module_ids"] == ["module-v2"]
                return [{"id": "chunk-v2"}]
            if tool_id == "module_expand":
                assert arguments == {"chunk_id": "chunk-v2"}
                return {
                    "content": "The characters should be 5th level.",
                    "source_ref": {
                        "module_id": "module-v2",
                        "scene_id": "ending-v2",
                        "chunk_id": "chunk-v2",
                        "page_start": 99,
                        "page_end": 99,
                        "heading_path": ["Conclusion"],
                        "content_sha256": "e" * 64,
                    },
                }
            raise AssertionError((tool_id, arguments))

    updated = asyncio.run(
        _remap_ending_sources_for_module_revision(
            Client(),
            manifest,
            campaign_id="campaign-1",
            new_module_id="module-v2",
            source_asset_sha256="f" * 64,
        )
    )

    condition = updated["ending"]["conditions"][0]
    assert condition["source_ref"]["module_id"] == "module-v2"
    assert condition["source_ref"]["scene_id"] == "ending-v2"
    assert condition["source_ref"]["chunk_id"] == "chunk-v2"
    assert condition["all_of"][0]["value"] == "ending-v2"
    assert condition["all_of"][1]["actor_id"] == "replacement"


def test_module_refresh_validates_ingested_scene_mapping_before_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "module.md"
    source.write_text("# Chapter\n## Cave\nBody.\n", encoding="utf-8")
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
            "reachable_scene_ids": ["scene-v1"],
            "visited_scene_ids": ["scene-v1"],
        },
    }
    events: list[str] = []
    indexes = {
        "module-v1": [{"scene_id": "scene-v1", "stable_key": "chapter-cave"}],
        "module-v2": [
            {
                "scene_id": "scene-v2",
                "stable_key": "chapter-cave",
                "chapter_id": "chapter-v2",
                "chapter": "Chapter",
                "title": "Cave",
            }
        ],
    }

    class Client:
        async def load(self, *group_ids: str) -> None:
            assert group_ids in {
                ("lobby.modules",),
                ("lobby.campaign", "lobby.modules"),
            }

        async def open(self, campaign_id: str) -> None:
            assert campaign_id == "campaign-1"

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                module_id = arguments["payload"]["module_id"]
                events.append(f"index:{module_id}")
                return indexes[module_id]
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "module_import":
                action = arguments["action"]
                events.append(action)
                return {
                    "stage": {"job": {"id": "job-1"}},
                    "inspect": {
                        "preview": {
                            "valid": True,
                            "errors": [],
                            "warnings": [],
                        }
                    },
                    "validate": {"validation": {"valid": True}},
                    "ingest": {"module_id": "module-v2"},
                    "activate": {
                        "activation": {
                            "module_id": "module-v2",
                            "active": True,
                            "replaced_module_ids": ["module-v1"],
                        }
                    },
                }[action]
            raise AssertionError((tool_id, arguments))

    async def manifest_get(client, campaign_id: str):
        return {"manifest": deepcopy(manifest)}

    async def campaign_get(client, campaign_id: str):
        return {
            "revision": 4,
            "state": {
                "game_phase": "lobby",
                "module_imports": {"active": {"module-key": {"module_id": "module-v1"}}},
            },
        }

    async def manifest_mutation(client, **kwargs):
        return {"manifest": deepcopy(kwargs.get("payload", {}).get("manifest", manifest))}

    monkeypatch.setattr(regression_playthrough, "_manifest_get", manifest_get)
    monkeypatch.setattr(regression_playthrough, "_campaign", campaign_get)
    monkeypatch.setattr(regression_playthrough, "_manifest_mutation", manifest_mutation)

    result = asyncio.run(
        _refresh_module(
            Client(),
            campaign_id="campaign-1",
            run_id="run-1",
            initial_phase="lobby",
            source_path=source,
            source_key="module-key",
            title="Module",
            return_phase="lobby",
        )
    )

    assert result["new_module_id"] == "module-v2"
    assert events.index("index:module-v2") < events.index("activate")


def test_module_refresh_rejects_changing_the_logical_source_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "module.md"
    source.write_text("# Chapter\n## Cave\nBody.\n", encoding="utf-8")

    class Client:
        async def load(self, *group_ids: str) -> None:
            assert group_ids == ("lobby.modules",)

        async def domain(self, tool_id: str, arguments: dict):
            assert tool_id == "module_query"
            return [{"scene_id": "scene-v1", "stable_key": "chapter-cave"}]

    async def manifest_get(client, campaign_id: str):
        return {
            "manifest": {
                "current": {"module_id": "module-v1"},
            }
        }

    async def campaign_get(client, campaign_id: str):
        return {
            "revision": 4,
            "state": {
                "module_imports": {"active": {"stable-module-key": {"module_id": "module-v1"}}}
            },
        }

    monkeypatch.setattr(regression_playthrough, "_manifest_get", manifest_get)
    monkeypatch.setattr(regression_playthrough, "_campaign", campaign_get)

    with pytest.raises(ValueError, match="source key must remain stable"):
        asyncio.run(
            _refresh_module(
                Client(),
                campaign_id="campaign-1",
                run_id="run-1",
                initial_phase="lobby",
                source_path=source,
                source_key="versioned-key-v2",
                title="Module",
                return_phase="lobby",
            )
        )


def test_in_place_module_refresh_does_not_duplicate_manifest_module_id() -> None:
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
            "reachable_scene_ids": ["scene-v1"],
            "visited_scene_ids": ["scene-v1"],
        },
    }

    updated = _extend_manifest_for_module_revision(
        manifest,
        old_module_id="module-v1",
        new_module_id="module-v1",
        old_index=[
            {"scene_id": "scene-v1", "stable_key": "cave"},
        ],
        new_index=[
            {
                "scene_id": "scene-v1",
                "stable_key": "cave",
                "chapter_id": "chapter-v1",
                "chapter": "Chapter",
                "title": "Cave",
            },
        ],
    )

    assert updated["module_ids"] == ["module-v1"]
    assert updated["current"]["scene_id"] == "scene-v1"
    assert updated["traversal"]["reachable_scene_ids"] == ["scene-v1"]
    assert manifest["module_ids"] == ["module-v1"]
    assert _module_refresh_manifest_action("module-v1", "module-v1") == "replace"
    assert _module_refresh_manifest_action("module-v1", "module-v2") == "extend_modules"


def test_module_refresh_manifest_identity_tracks_the_exact_manifest_payload() -> None:
    first = _module_refresh_manifest_identity(
        old_module_id="module-v1",
        new_module_id="module-v2",
        refresh_identity="refresh",
        manifest={"current": {"scene_id": "scene-1"}},
    )
    retry = _module_refresh_manifest_identity(
        old_module_id="module-v1",
        new_module_id="module-v2",
        refresh_identity="refresh",
        manifest={"current": {"scene_id": "scene-1"}},
    )
    changed = _module_refresh_manifest_identity(
        old_module_id="module-v1",
        new_module_id="module-v2",
        refresh_identity="refresh",
        manifest={"current": {"scene_id": "scene-2"}},
    )

    assert retry == first
    assert changed != first


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
        "derived": {
            "hit_points": {
                "value": 5,
                "max": 5,
                "temp": 2,
                "base_max": 10,
            }
        },
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
    assert member["hit_points"]["current"] == 5
    assert member["hit_points"]["maximum"] == 5
    assert member["wallet"] == sheet["inventory"]["wallet"]


@pytest.mark.parametrize("defer_checkpoint", [False, True])
def test_replacement_join_preserves_predecessor_and_only_hands_off_explicit_knowledge(
    defer_checkpoint: bool,
) -> None:
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
    manifest["ending"]["conditions"] = [
        {
            "id": "party-level-ending",
            "label": "Active party reaches level 5",
            "source_ref": _manifest_source_ref(),
            "all_of": [
                {
                    "kind": "actor_value",
                    "path": "sheet.progression.level",
                    "actor_id": "predecessor",
                    "fact_key": "",
                    "operator": "at_least",
                    "value": 5,
                },
                {
                    "kind": "actor_value",
                    "path": "sheet.combat.hp.value",
                    "actor_id": "predecessor",
                    "fact_key": "",
                    "operator": "equals",
                    "value": 0,
                },
            ],
        }
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
                return deepcopy(predecessor if actor_id == "predecessor" else replacement)
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
            if tool_id == "memory_change":
                assert "snapshot" not in arguments["payload"]
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
                self.revision += 1
                return {
                    "event": {"id": "event-join"},
                }
            if tool_id == "snapshot_create":
                assert arguments["expected_head_snapshot_id"] == ""
                self.head_snapshot_id = "snapshot-1"
                self.revision += 1
                self.manifest["snapshot_dag"] = {
                    "active_branch_id": "branch-1",
                    "head_snapshot_id": "snapshot-1",
                    "nodes": [
                        {
                            "id": "snapshot-1",
                            "parent_id": "",
                            "branch_id": "branch-1",
                            "slot": 1,
                            "label": arguments["label"],
                            "checksum": "b" * 64,
                            "is_head": True,
                        }
                    ],
                }
                return {"id": "snapshot-1", "slot": 1}
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
            defer_checkpoint=defer_checkpoint,
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
    ending_checks = client.manifest["ending"]["conditions"][0]["all_of"]
    assert ending_checks[0]["actor_id"] == "replacement"
    assert ending_checks[1]["actor_id"] == "predecessor"
    if defer_checkpoint:
        assert result["checkpoint"] is None
        assert client.head_snapshot_id == ""
    else:
        assert result["checkpoint"]["snapshot"]["slot"] == 1


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


@pytest.mark.parametrize("defer_checkpoint", [False, True])
def test_level_advancement_exhausts_public_follow_up_and_restores_play(
    defer_checkpoint: bool,
) -> None:
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
                if arguments["view"] == "advancement":
                    return {
                        "status": "ready",
                        "character_id": "bard-1",
                        "character_revision": self.actor["revision"],
                        "new_level": 2,
                        "follow_up": {
                            "feature_artifacts": [
                                {
                                    "artifact_id": "feature-jack",
                                    "name": "Jack of All Trades",
                                    "selection_requirements": {},
                                    "grant_level": 2,
                                }
                            ],
                            "subclass_options": [],
                            "spell_choices": {
                                "cantrips_to_add": 0,
                                "leveled_spells_to_add": 1,
                            },
                            "prepared_spell_event": None,
                        },
                        "spellcasting": {
                            "preparation_mode": "known",
                            "maximum_spell_level": 1,
                        },
                    }
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
                                    "grant_level": 2,
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
                    assert arguments["selection"] == {"grant_level": 2}
                    self.actor["sheet"]["content"]["features"].append(
                        {
                            "id": artifact_id,
                            "advancement_grants": [{"level": 2}],
                        }
                    )
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
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert client.phase == "play"
    assert result["actor"]["sheet"]["progression"]["level"] == 2
    assert result["applied_features"] == [
        {"artifact_id": "feature-jack", "selection": {"grant_level": 2}}
    ]
    assert result["applied_spells"] == ["spell-heroism"]
    if defer_checkpoint:
        assert result["checkpoint"] is None
        assert "snapshot_create" not in client.calls
    else:
        assert result["checkpoint"]["verification"] == {"valid": True}
        assert client.calls.count("snapshot_create") == 1
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


def test_level_preflight_rejects_missing_feature_choice_without_mutation() -> None:
    sheet = default_character_sheet()
    sheet["progression"].update(
        {
            "level": 1,
            "classes": [
                {
                    "name": "Fighter",
                    "level": 1,
                    "subclass": "",
                    "hit_die": 10,
                }
            ],
        }
    )
    actor = {
        "id": "fighter-1",
        "revision": 4,
        "sheet": sheet,
    }

    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append((tool_id, arguments))
            if tool_id == "character_query":
                return {
                    "status": "ready",
                    "character_id": "fighter-1",
                    "character_revision": 4,
                    "new_level": 2,
                    "follow_up": {
                        "feature_artifacts": [
                            {
                                "artifact_id": "feature-style",
                                "selection_requirements": {
                                    "field": "option",
                                    "options": ["Defense", "Dueling"],
                                },
                            }
                        ],
                        "subclass_options": [],
                        "spell_choices": {
                            "cantrips_to_add": 0,
                            "leveled_spells_to_add": 0,
                        },
                        "prepared_spell_event": None,
                    },
                    "spellcasting": {
                        "preparation_mode": "known",
                        "maximum_spell_level": 0,
                    },
                }
            if tool_id == "rule_pack_query":
                assert arguments["payload"]["kind"] == "feature"
                return [
                    {
                        "id": "feature-style",
                        "name": "Fighting Style",
                        "selection_requirements": {
                            "class_name": "Fighter",
                            "subclass_name": "",
                            "minimum_level": 1,
                            "field": "option",
                            "options": ["Defense", "Dueling"],
                        },
                    }
                ]
            raise AssertionError((tool_id, arguments))

    client = Client()
    with pytest.raises(ValueError, match="requires an explicit option choice"):
        asyncio.run(
            _preflight_level_completion(
                client,
                campaign_id="campaign-1",
                actor=actor,
                class_name="Fighter",
                target_level=2,
                subclass_artifact_id="",
                feature_selections={},
                spell_selections=[],
                prepared_spell_ids=[],
            )
        )

    assert [tool for tool, _ in client.calls] == [
        "character_query",
        "rule_pack_query",
    ]


def test_prepared_caster_spell_hydration_does_not_consume_known_spell_quota() -> None:
    artifact_id = "dnd5e.content.srd2014.spell.aid"
    selections = [
        {
            "artifact_id": artifact_id,
            "source_class": "Cleric",
            "method": "class_prepared",
        }
    ]
    catalog = {
        artifact_id: {
            "selection_requirements": {
                "level": 2,
                "eligible_classes": ["Cleric", "Paladin"],
            }
        }
    }

    assert _level_spell_choice_counts(
        selections,
        spell_by_id=catalog,
        class_name="Cleric",
        preparation_mode="prepared",
        maximum_spell_level=2,
    ) == (0, 0, [artifact_id])

    with pytest.raises(ValueError, match="prepared-caster configuration"):
        _level_spell_choice_counts(
            selections,
            spell_by_id=catalog,
            class_name="Cleric",
            preparation_mode="known",
            maximum_spell_level=2,
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
                return {
                    "campaign_revision": 9,
                    "manifest": {"status": "in_progress"},
                }
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
            checkpoint_id="scene-checkpoint-1",
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
    assert result["post_sync"]["persisted"] is False


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
                return {
                    "campaign_revision": 10,
                    "manifest": {"status": "in_progress"},
                }
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
            if tool_id == "state_revision" and arguments["action"] == "receipt":
                return {
                    "branch_id": None,
                    "request_hash": regression_playthrough._idempotency_request_hash(
                        {
                            "label": "Scene checkpoint",
                            "expected_head_snapshot_id": "snapshot-1",
                        }
                    ),
                    "response": {
                        "id": "snapshot-2",
                        "branch_id": "branch-1",
                        "parent_id": "snapshot-1",
                        "slot": 2,
                        "label": "Scene checkpoint",
                    },
                }
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
            checkpoint_id="scene-checkpoint-1",
        )
    )

    assert result["reused"] is True
    assert result["snapshot"]["id"] == "snapshot-2"
    assert result["verification"] == {"valid": True, "slot": 2}
    assert [name for name, _ in client.calls] == [
        "playthrough_manifest",
        "branch_query",
        "snapshot_create",
        "state_revision",
        "snapshot_query",
        "snapshot_query",
        "playthrough_manifest",
    ]
    assert result["post_sync"]["persisted"] is False


@pytest.mark.parametrize("initial_phase", ["play", "combat"])
def test_failed_route_is_preserved_when_branching_from_verified_snapshot(
    initial_phase: str,
) -> None:
    class Client:
        def __init__(self) -> None:
            self.phase = initial_phase
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

    result_client = Client()
    result = asyncio.run(
        _branch_from_snapshot(
            result_client,
            campaign_id="campaign-1",
            run_id="run-1",
            initial_phase=initial_phase,
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
    assert bool(result["phase_changes"]) is (initial_phase == "play")
    assert (("lobby.campaign",) in result_client.loads) is (initial_phase == "play")


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
    expected_identity = _check_identity("trail-survival-1")

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
                assert arguments["action"] == "check"
                assert arguments["payload"]["kind"] == "ability"
                assert arguments["payload"]["ability"] == "survival"
                assert arguments["payload"]["advantage"] is False
                assert arguments["payload"]["disadvantage"] is True
                assert arguments["idempotency_key"] == _mutation_key(
                    "run-1", "character-check", expected_identity
                )
                self.revision += 1
                return {"status": "committed", "result": {"success": True, "total": 14}}
            if tool_id == "memory_change":
                assert [item["actor_id"] for item in arguments["payload"]["actor_knowledge"]] == [
                    "actor-1",
                    "actor-2",
                ]
                assert all(
                    item["proposition"] == "The trail shows twelve goblins and two captives."
                    for item in arguments["payload"]["actor_knowledge"]
                )
                assert all(
                    item["knowledge_key"] == _check_knowledge_key("run-1", expected_identity)
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
            occurrence_id=expected_identity,
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
    assert _check_knowledge_key("run-1", "trail-survival-1") != _check_knowledge_key(
        "run-1", "trail-survival-2"
    )


def test_check_identity_uses_explicit_occurrence_not_mutable_check_content() -> None:
    assert _check_identity("armory-lock-1") == "armory-lock-1"
    assert _check_identity("armory-lock-1") != _check_identity("armory-lock-2")


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
                occurrence_id="unsupported-check-1",
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
    with pytest.raises(RegressionRulingRequiredError, match="did not commit") as raised:
        _committed_check_result({"status": "pending_ruling"})
    assert raised.value.requirement["ruling"]["default_resolver"] == "agent"


def test_check_recovery_identity_includes_actor_and_roll_mode() -> None:
    source_ref = {"chunk_id": "chunk-1"}
    progress = {
        "current_location_key": "bridge",
        "state": {
            "full_playthrough_check": {
                "run_id": "run-1",
                "occurrence_id": "bridge-stealth-1",
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
        occurrence_id="bridge-stealth-1",
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
        occurrence_id="bridge-stealth-2",
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
        occurrence_id="bridge-stealth-1",
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
        occurrence_id="bridge-stealth-1",
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


def test_ability_contest_accepts_full_and_compact_exposure_shapes() -> None:
    result = {
        "kind": "ability_contest",
        "outcome": "source_wins",
        "winner_actor_id": "bard",
    }

    assert _committed_contest_result({"status": "committed", "result": result}) == result
    assert _committed_contest_result(result) == result
    with pytest.raises(RegressionRulingRequiredError, match="did not commit") as raised:
        _committed_contest_result({"status": "pending_ruling"})
    assert raised.value.requirement["ruling"]["default_resolver"] == "agent"


def test_contest_recovery_identity_binds_both_actors_and_roll_modes() -> None:
    source_ref = {"chunk_id": "chunk-1"}
    progress = {
        "current_location_key": "road",
        "state": {
            "full_playthrough_contest": {
                "run_id": "run-1",
                "occurrence_id": "bluff-group-1",
                "source_actor_id": "bard",
                "target_actor_id": "cultist",
                "source_ability": "deception",
                "target_ability": "insight",
                "source_proficient": True,
                "target_proficient": False,
                "source_advantage": False,
                "source_disadvantage": False,
                "target_advantage": True,
                "target_disadvantage": False,
                "source_ref": source_ref,
            }
        },
    }
    arguments = {
        "run_id": "run-1",
        "occurrence_id": "bluff-group-1",
        "location_key": "road",
        "source_actor_id": "bard",
        "target_actor_id": "cultist",
        "source_ability": "deception",
        "target_ability": "insight",
        "source_proficient": True,
        "target_proficient": False,
        "source_advantage": False,
        "source_disadvantage": False,
        "target_advantage": True,
        "target_disadvantage": False,
        "source_ref": source_ref,
    }

    assert _matching_contest_progress(progress, **arguments)
    assert not _matching_contest_progress(
        progress,
        **{**arguments, "target_actor_id": "different-cultist"},
    )
    assert not _matching_contest_progress(
        progress,
        **{**arguments, "target_advantage": False},
    )


@pytest.mark.parametrize("defer_checkpoint", [False, True])
@pytest.mark.parametrize("force_zero_hp", [False, True])
@pytest.mark.parametrize(("half_damage", "expected_amount"), [(False, 4), (True, 2)])
def test_source_damage_rolls_then_damages_and_knocks_prone_through_public_tools(
    half_damage: bool,
    expected_amount: int,
    force_zero_hp: bool,
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
            self.campaign_revision = 10
            self.character_revision = 3
            self.calls: list[str] = []
            self.keys: list[str] = []

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
            if arguments.get("idempotency_key"):
                self.keys.append(arguments["idempotency_key"])
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
                after_hp = 0 if force_zero_hp else 10 - expected_amount
                sheet["combat"]["hp"] = {
                    "value": after_hp,
                    "max": 10,
                    "temp": 0,
                }
                return {
                    "character": {
                        "id": "actor-1",
                        "revision": self.character_revision,
                        "sheet": sheet,
                    },
                    "result": {"after_hp": after_hp},
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
            if tool_id == "memory_change":
                event = arguments["payload"]["event"]
                assert event["payload"]["amount"] == expected_amount
                assert event["payload"]["damage_roll"]["total"] == 4
                assert event["payload"]["half_damage"] is half_damage
                assert event["payload"]["damage_event_id"] == "chimney-fall-1"
                assert event["payload"]["source_ref"] == source_ref
                checkpoint_deferred = defer_checkpoint and not force_zero_hp
                assert ("snapshot" in arguments["payload"]) is not checkpoint_deferred
                self.campaign_revision += 1
                return {
                    "event": {"id": "event-1"},
                    **({} if checkpoint_deferred else {"snapshot": {"slot": 2}}),
                }
            if tool_id == "playthrough_manifest":
                assert arguments["action"] == "sync"
                return {
                    "manifest": {"status": "in_progress"},
                    "campaign_revision": self.campaign_revision,
                }
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _apply_source_damage(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            location_key="3-kennel",
            source_excerpt="On a result of 5 or less, the character falls.",
            source_ref=source_ref,
            actor_id="actor-1",
            damage_event_id="chimney-fall-1",
            expression="1d6",
            damage_type="bludgeoning",
            reason="falling 10 feet in the chimney",
            half_damage=half_damage,
            knock_prone=True,
            knowledge_actor_ids=["actor-2"],
            defer_checkpoint=defer_checkpoint,
        )
    )

    expected_after_hp = 0 if force_zero_hp else 10 - expected_amount
    assert result["damage"]["result"]["after_hp"] == expected_after_hp
    if force_zero_hp:
        assert result["prone"] is None
        assert result["checkpoint_deferred"] is False
        assert result["continuity"]["snapshot"]["slot"] == 2
    else:
        assert result["prone"]["status"] == "knocked_prone"
        assert result["character"]["sheet"]["conditions"] == ["prone"]
        assert result["checkpoint_deferred"] is defer_checkpoint
        assert ("snapshot" in result["continuity"]) is not defer_checkpoint
    assert result["knowledge_actor_ids"] == ["actor-1", "actor-2"]
    assert _mutation_key("run-1", "source-damage-roll", "chimney-fall-1") in client.keys
    assert _mutation_key("run-1", "source-damage", "chimney-fall-1") in client.keys
    assert _mutation_key("run-1", "source-damage-continuity", "chimney-fall-1") in client.keys


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
            if tool_id == "memory_change":
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
            occurrence_id="scout-stand-after-kennel-fall",
            actor_id="actor-1",
            knowledge_actor_ids=["actor-2"],
            reason="Scout stood after recovering from the source-cited fall.",
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert result["stand"]["status"] == "stood"
    assert result["knowledge_actor_ids"] == ["actor-1", "actor-2"]
    identity = "scout-stand-after-kennel-fall"
    assert client.keys == {
        "stand": _mutation_key("run-1", "source-event-stand", identity),
        "continuity": _mutation_key("run-1", "source-event-stand-continuity", identity),
        "sync": _mutation_key("run-1", "sync", f"source-event-stand-sync:{identity}"),
    }


@pytest.mark.parametrize("defer_checkpoint", [False, True])
def test_source_state_initialization_uses_cited_public_action_without_fake_damage(
    defer_checkpoint: bool,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 40,
        "page_end": 41,
        "heading_path": ["14. KING'S QUARTERS"],
        "content_sha256": "abc",
    }

    class Client:
        def __init__(self) -> None:
            self.revision = 30
            self.keys: dict[str, str] = {}

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": self.revision}}

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "Gundren lies unconscious and stable at 0 hit points.",
                    "locations": [{"key": "14-king-s-uarters"}],
                }
            if tool_id == "character_query":
                return {
                    "id": "gundren",
                    "name": "Gundren Rockseeker",
                    "campaign_id": "campaign-1",
                    "revision": 1,
                }
            if tool_id == "character_state_change":
                assert arguments["action"] == "source_state"
                assert arguments["payload"] == {
                    "state": "stable_unconscious",
                    "source_ref": "module-chunk:chunk-1",
                    "reason": "Gundren begins the scene unconscious and stable.",
                }
                self.keys["source_state"] = arguments["idempotency_key"]
                self.revision += 1
                return {
                    "result": {
                        "status": "initialized",
                        "source_state": "stable_unconscious",
                    }
                }
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "memory_change":
                assert arguments["payload"]["event"]["audience_scope"] == "dm"
                assert arguments["payload"]["event"]["payload"]["source_ref"] == source_ref
                assert ("snapshot" in arguments["payload"]) is not defer_checkpoint
                self.keys["continuity"] = arguments["idempotency_key"]
                self.revision += 1
                return {
                    "event": {"id": "event-1"},
                    **({} if defer_checkpoint else {"snapshot": {"slot": 4}}),
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
        _initialize_source_state(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            source_scene_id="",
            location_key="14-king-s-uarters",
            source_excerpt="Gundren lies unconscious and stable at 0 hit points.",
            source_ref=source_ref,
            occurrence_id="gundren-stable-at-scene-start",
            actor_id="gundren",
            state="stable_unconscious",
            reason="Gundren begins the scene unconscious and stable.",
            knowledge_actor_ids=[],
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert result["state"]["result"]["source_state"] == "stable_unconscious"
    assert result["knowledge_actor_ids"] == []
    identity = "gundren-stable-at-scene-start"
    assert client.keys == {
        "source_state": _mutation_key("run-1", "source-state", identity),
        "continuity": _mutation_key("run-1", "source-state-continuity", identity),
        "sync": _mutation_key("run-1", "sync", f"source-state-sync:{identity}"),
    }


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
                    assert arguments["payload"]["duration_minutes"] == 60
                    assert arguments["payload"]["rest_schedule"] == {
                        "sleep_minutes": 0,
                        "light_activity_minutes": 60,
                        "strenuous_activity_minutes": 0,
                    }
                    if actor_id == "fighter":
                        assert arguments["payload"]["hit_dice_spends"] == [
                            {"key": "fighter:d10", "count": 1}
                        ]
                        assert arguments["payload"]["song_of_rest_source_actor_id"] == "wizard"
                        assert arguments["payload"]["rest_activity_minutes"] == {"meditation": 30}
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
                assert arguments["payload"]["started_elapsed_minutes"] == 840
                assert arguments["payload"]["rest_schedule"] == {
                    "sleep_minutes": 0,
                    "light_activity_minutes": 60,
                    "strenuous_activity_minutes": 0,
                }
                if arguments["character_id"] == "fighter":
                    assert arguments["payload"]["hit_dice_spends"] == [
                        {"key": "fighter:d10", "count": 1}
                    ]
                    assert arguments["payload"]["song_of_rest_source_actor_id"] == "wizard"
                    assert arguments["payload"]["rest_activity_minutes"] == {"meditation": 30}
                else:
                    assert "hit_dice_spends" not in arguments["payload"]
                    assert "rest_activity_minutes" not in arguments["payload"]
                if arguments["character_id"] == "wizard":
                    assert arguments["payload"]["arcane_recovery"] == {"1": 1}
                else:
                    assert "arcane_recovery" not in arguments["payload"]
                self.revision += 1
                return {
                    "status": "committed",
                    "character": {"id": arguments["character_id"]},
                }
            if tool_id == "memory_change":
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
            occurrence_id="hideout-short-rest-1",
            members=[
                {
                    "actor_id": "fighter",
                    "hit_dice_spends": [{"key": "fighter:d10", "count": 1}],
                    "song_of_rest_source_actor_id": "wizard",
                    "rest_activity_minutes": {"meditation": 30},
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
    identity = "hideout-short-rest-1"
    assert client.keys["clock_set"] == [_mutation_key("run-1", "short-rest-clock-set", identity)]
    assert client.keys["clock_advance"] == [
        _mutation_key("run-1", "short-rest-clock-advance", identity)
    ]
    assert client.keys["actor"] == [
        _mutation_key("run-1", "short-rest-actor", f"{identity}:wizard"),
        _mutation_key("run-1", "short-rest-actor", f"{identity}:fighter"),
    ]
    assert client.keys["continuity"] == [_mutation_key("run-1", "short-rest-continuity", identity)]
    assert client.keys["sync"] == [_mutation_key("run-1", "sync", f"short-rest-sync:{identity}")]


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
            if tool_id == "memory_change":
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
                    assert payload["snapshot"]["label"].startswith("Full playthrough time advance:")
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
            occurrence_id="travel-to-phandalin-1",
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


@pytest.mark.parametrize("defer_checkpoint", [False, True])
def test_play_activity_records_structured_effect_and_random_receipt(
    defer_checkpoint: bool,
) -> None:
    receipt = {
        "operation": "character_action",
        "position_before": 10,
        "position_after": 11,
    }

    class Client:
        revision = 8
        keys: list[str] = []

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
            if arguments.get("idempotency_key"):
                self.keys.append(arguments["idempotency_key"])
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
            if tool_id == "memory_change":
                payload = arguments["payload"]["event"]["payload"]
                assert payload["core_effect"]["kind"] == "second_wind"
                assert payload["activity_event_id"] == "second-wind-before-pursuit"
                assert payload["random_stream_receipt"] == receipt
                assert ("snapshot" in arguments["payload"]) is not defer_checkpoint
                self.revision += 1
                return {
                    "event": {"id": "event-1"},
                    **({} if defer_checkpoint else {"snapshot": {"slot": 6}}),
                }
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
            activity_event_id="second-wind-before-pursuit",
            declaration=None,
            reason="The fighter used Second Wind before pursuing the hostage bargain.",
            knowledge_actor_ids=["cleric"],
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert result["action"]["result"]["core_effect"]["after_hp"] == 10
    assert result["knowledge_actor_ids"] == ["fighter", "cleric"]
    assert ("snapshot" in result["continuity"]) is not defer_checkpoint
    assert _mutation_key("run-1", "play-activity", "second-wind-before-pursuit") in Client.keys
    assert (
        _mutation_key("run-1", "play-activity-continuity", "second-wind-before-pursuit")
        in Client.keys
    )


@pytest.mark.parametrize("defer_checkpoint", [False, True])
def test_source_spell_driver_consumes_item_charge_and_preserves_dm_boundary(
    defer_checkpoint: bool,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "stone-reference",
        "chunk_id": "stone-chunk",
        "page_start": 193,
        "page_end": 193,
        "heading_path": ["Appendix A", "Stone of Golorr"],
        "content_sha256": "c" * 64,
    }

    class Client:
        revision = 12

        def actor(self, charges: int) -> dict:
            sheet = default_character_sheet()
            sheet["inventory"]["items"].append(
                {
                    "id": "stone-of-golorr",
                    "name": "Stone of Golorr",
                    "kind": "magic_item",
                    "quantity": 1,
                    "weight_oz": 0,
                    "price_cp": 0,
                    "description": "",
                    "source_key": "module-chunk:stone-chunk",
                    "container_id": None,
                    "equipped": False,
                    "equipped_slot": None,
                    "identified": False,
                    "attunement": "attuned",
                    "condition": "normal",
                    "uses": {},
                    "charges": {
                        "label": "Legend Lore charges",
                        "value": charges,
                        "max": 3,
                        "recovers_on": "dawn",
                        "source_key": "module-chunk:stone-chunk",
                    },
                    "mechanics": {},
                }
            )
            return {
                "id": "pip",
                "name": "Pip",
                "campaign_id": "campaign-1",
                "revision": 7 if charges == 3 else 8,
                "sheet": sheet,
            }

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
                scene_id = arguments["payload"]["scene_id"]
                if scene_id == "occurrence-scene":
                    return {
                        "module_id": "module-1",
                        "scene_id": scene_id,
                        "content": "The party studies the Stone.",
                        "locations": [{"key": "safe-room"}],
                    }
                return {
                    "module_id": "module-1",
                    "scene_id": "stone-reference",
                    "content": (
                        "While holding the stone, you can expend 1 of its charges "
                        "to cast the legend lore spell."
                    ),
                }
            if tool_id == "character_query":
                return self.actor(3)
            if tool_id == "character_action":
                assert arguments["action"] == "cast_spell"
                assert arguments["payload"] == {
                    "spell_id": "dnd5e.content.srd2014.spell.legend-lore",
                    "source_item_id": "stone-of-golorr",
                }
                self.revision += 1
                return {
                    "status": "pending_ruling",
                    "result": {
                        "payment": {
                            "economy": "item_charges",
                            "item_id": "stone-of-golorr",
                            "cost": 1,
                            "level": 5,
                            "ritual": False,
                        }
                    },
                    "character": self.actor(2),
                }
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "memory_change":
                event = arguments["payload"]["event"]
                assert event["event_type"] == "magic_item_spell_cast"
                assert event["payload"]["resolution_status"] == "pending_ruling"
                assert ("snapshot" in arguments["payload"]) is not defer_checkpoint
                self.revision += 1
                return {
                    "event": {"id": "event-1"},
                    **({} if defer_checkpoint else {"snapshot": {"slot": 8}}),
                }
            if tool_id == "playthrough_manifest":
                return {
                    "manifest": {"status": "in_progress"},
                    "campaign_revision": self.revision,
                }
            raise AssertionError((tool_id, arguments))

    result = asyncio.run(
        _cast_source_spell(
            Client(),
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="stone-legend-lore-1",
            scene_id="occurrence-scene",
            source_scene_id="stone-reference",
            location_key="safe-room",
            source_excerpt="expend 1 of its charges to cast the legend lore spell",
            source_ref=source_ref,
            actor_id="pip",
            spell_id="dnd5e.content.srd2014.spell.legend-lore",
            source_item_id="stone-of-golorr",
            cast_level=None,
            component_ruling=None,
            reason="Pip expended one Stone charge; the information awaits DM settlement.",
            knowledge_actor_ids=[],
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert result["cast"]["status"] == "pending_ruling"
    assert result["charges"] == {"before": 3, "after": 2}
    assert result["cast_recovered"] is False
    assert result["knowledge_actor_ids"] == ["pip"]
    assert ("snapshot" in result["continuity"]) is not defer_checkpoint


def test_source_spell_driver_returns_precommit_ruling_without_charge_assumption() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "stone-reference",
        "chunk_id": "stone-chunk",
        "page_start": 193,
        "page_end": 193,
        "heading_path": ["Appendix A", "Stone of Golorr"],
        "content_sha256": "c" * 64,
    }

    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                scene_id = arguments["payload"]["scene_id"]
                return {
                    "module_id": "module-1",
                    "scene_id": scene_id,
                    "content": (
                        "The party studies the Stone."
                        if scene_id == "occurrence-scene"
                        else (
                            "While holding the stone, you can expend 1 of its "
                            "charges to cast the legend lore spell."
                        )
                    ),
                    "locations": (
                        [{"key": "safe-room"}]
                        if scene_id == "occurrence-scene"
                        else []
                    ),
                }
            if tool_id == "character_query":
                sheet = default_character_sheet()
                sheet["inventory"]["items"].append(
                    {
                        "id": "stone-of-golorr",
                        "name": "Stone of Golorr",
                        "kind": "magic_item",
                        "charges": {"value": 3, "max": 3},
                    }
                )
                return {
                    "id": "pip",
                    "name": "Pip",
                    "campaign_id": "campaign-1",
                    "revision": 7,
                    "sheet": sheet,
                }
            if tool_id == "character_action":
                return {
                    "status": "pending_ruling",
                    "default_resolver": "agent",
                    "ruling_kind": "module_specific_procedure",
                    "reason": "the source-defined answer needs Agent adjudication",
                    "committed": False,
                    "result": {"status": "pending_ruling"},
                }
            raise AssertionError("a pre-commit ruling must stop before continuity writes")

    with pytest.raises(RegressionRulingRequiredError) as raised:
        asyncio.run(
            _cast_source_spell(
                Client(),
                campaign_id="campaign-1",
                run_id="run-1",
                occurrence_id="stone-legend-lore-1",
                scene_id="occurrence-scene",
                source_scene_id="stone-reference",
                location_key="safe-room",
                source_excerpt="expend 1 of its charges to cast the legend lore spell",
                source_ref=source_ref,
                actor_id="pip",
                spell_id="dnd5e.content.srd2014.spell.legend-lore",
                source_item_id="stone-of-golorr",
                cast_level=None,
                component_ruling=None,
                reason="Pip attempted to invoke the Stone.",
                knowledge_actor_ids=[],
            )
        )

    assert raised.value.requirement["operation"] == (
        "character_action.cast_source_spell"
    )
    assert raised.value.requirement["ruling"]["ruling_kind"] == (
        "module_specific_procedure"
    )


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
            if tool_id == "memory_change":
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
            occurrence_id="enemy-alerted-1",
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


def test_long_rest_uses_atomic_party_rest_and_unique_occurrence_knowledge() -> None:
    class Client:
        def __init__(self) -> None:
            self.revision = 5
            self.knowledge_keys: list[str] = []
            self.sync_keys: list[str] = []
            self.party_rest_keys: list[str] = []
            self.continuity_keys: list[str] = []
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
                self.party_rest_keys.append(arguments["idempotency_key"])
                assert arguments["payload"]["duration_minutes"] == 480
                assert arguments["payload"]["members"] == [
                    {
                        "character_id": "fighter",
                        "expected_revision": 2,
                        "food_and_drink": True,
                        "rest_activity_minutes": {"meditation": 30},
                        "rest_schedule": {
                            "sleep_minutes": 360,
                            "light_activity_minutes": 120,
                            "strenuous_activity_minutes": 0,
                        },
                    },
                    {
                        "character_id": "cleric",
                        "expected_revision": 2,
                        "food_and_drink": False,
                        "rest_schedule": {
                            "sleep_minutes": 360,
                            "light_activity_minutes": 120,
                            "strenuous_activity_minutes": 0,
                        },
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
            if tool_id == "memory_change":
                self.continuity_keys.append(arguments["idempotency_key"])
                event = arguments["payload"]["event"]
                assert event["event_type"] == "long_rest"
                assert event["payload"]["duration_minutes"] == 480
                self.knowledge_keys.extend(
                    item["knowledge_key"] for item in arguments["payload"]["actor_knowledge"]
                )
                self.revision += 1
                return {"event": {"id": "event-1"}, "snapshot": {"slot": 5}}
            if tool_id == "playthrough_manifest":
                self.sync_keys.append(arguments["idempotency_key"])
                return {
                    "manifest": {"status": "in_progress"},
                    "campaign_revision": self.revision,
                }
            raise AssertionError((tool_id, arguments))

    client = Client()
    shared_reason = "The party completed an uninterrupted long rest."
    result = asyncio.run(
        _long_rest(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="hideout-long-rest-1",
            members=[
                {
                    "actor_id": "fighter",
                    "food_and_drink": True,
                    "rest_activity_minutes": {"meditation": 30},
                },
                {"actor_id": "cleric", "prepared_spell_ids": ["cure-wounds"]},
            ],
            start_clock=None,
            duration_minutes=480,
            reason=shared_reason,
        )
    )

    assert result["member_ids"] == ["fighter", "cleric"]
    assert result["rest"]["world_time"]["day"] == 2
    assert result["continuity"]["snapshot"]["slot"] == 5

    asyncio.run(
        _long_rest(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="hideout-long-rest-2",
            members=[
                {
                    "actor_id": "fighter",
                    "food_and_drink": True,
                    "rest_activity_minutes": {"meditation": 30},
                },
                {"actor_id": "cleric", "prepared_spell_ids": ["cure-wounds"]},
            ],
            start_clock=None,
            duration_minutes=480,
            reason=shared_reason,
        )
    )

    assert len(client.knowledge_keys) == 4
    assert len(set(client.knowledge_keys)) == 4
    assert len(client.sync_keys) == 2
    assert len(set(client.sync_keys)) == 2
    assert len(set(client.party_rest_keys)) == 2
    assert len(set(client.continuity_keys)) == 2


def test_long_rest_recovers_committed_receipt_without_advancing_time_twice() -> None:
    class Client:
        def __init__(self) -> None:
            self.revision = 6
            self.party_rest_calls = 0
            self.receipt_key = ""
            self.world_time = {
                "schema_version": 1,
                "day": 2,
                "hour": 0,
                "minute": 0,
                "elapsed_minutes": 1440,
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
                sheet = default_character_sheet()
                sheet["combat"]["rest_history"] = {
                    "last_rest_type": "long_rest",
                    "last_rest_started_elapsed_minutes": 960,
                    "last_rest_completed_elapsed_minutes": 1440,
                    "last_long_rest_elapsed_minutes": 1440,
                }
                if actor_id == "cleric":
                    sheet["spellcasting"]["preparation"] = {"selected_spell_ids": ["cure-wounds"]}
                return {
                    "id": actor_id,
                    "campaign_id": "campaign-1",
                    "revision": 3,
                    "sheet": sheet,
                }
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "campaign_change":
                self.party_rest_calls += 1
                self.receipt_key = arguments["idempotency_key"]
                raise RuntimeError(
                    f"idempotency key reused with a different request: {self.receipt_key}"
                )
            if tool_id == "state_revision":
                assert arguments == {
                    "campaign_id": "campaign-1",
                    "action": "receipt",
                    "payload": {"idempotency_key": self.receipt_key},
                }
                request_hash = regression_playthrough._idempotency_request_hash(
                    {
                        "members": [
                            {
                                "character_id": "fighter",
                                "expected_revision": 2,
                                "prepared_spell_ids": None,
                                "hit_dice_recovery": None,
                                "rest_activity_minutes": {},
                                "rest_schedule": {
                                    "sleep_minutes": 360,
                                    "light_activity_minutes": 120,
                                    "strenuous_activity_minutes": 0,
                                },
                                "food_and_drink": True,
                            },
                            {
                                "character_id": "cleric",
                                "expected_revision": 2,
                                "prepared_spell_ids": ["cure-wounds"],
                                "hit_dice_recovery": None,
                                "rest_activity_minutes": {},
                                "rest_schedule": {
                                    "sleep_minutes": 360,
                                    "light_activity_minutes": 120,
                                    "strenuous_activity_minutes": 0,
                                },
                                "food_and_drink": False,
                            },
                        ],
                        "duration_minutes": 480,
                        "branch_id": "branch-1",
                    }
                )
                return {
                    "key": self.receipt_key,
                    "replayed": True,
                    "request_hash": request_hash,
                    "branch_id": "branch-1",
                    "entity_revisions": [
                        {
                            "entity_type": "campaign",
                            "entity_id": "campaign-1",
                            "before_revision": 5,
                            "after_revision": 6,
                        },
                        {
                            "entity_type": "character",
                            "entity_id": "fighter",
                            "before_revision": 2,
                            "after_revision": 3,
                        },
                        {
                            "entity_type": "character",
                            "entity_id": "cleric",
                            "before_revision": 2,
                            "after_revision": 3,
                        },
                    ],
                    "response": {
                        "status": "committed",
                        "rest_type": "long_rest",
                        "duration_minutes": 480,
                        "member_ids": ["fighter", "cleric"],
                        "world_time": self.world_time,
                        "campaign_revision": 6,
                        "preparations": {"cleric": {"selected_spell_ids": ["cure-wounds"]}},
                    },
                }
            if tool_id == "memory_change":
                self.revision += 1
                return {"event": {"id": "event-1"}, "snapshot": {"slot": 5}}
            if tool_id == "playthrough_manifest":
                return {
                    "manifest": {"status": "in_progress"},
                    "campaign_revision": self.revision,
                }
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _long_rest(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="recovered-long-rest-1",
            members=[
                {"actor_id": "fighter", "food_and_drink": True},
                {"actor_id": "cleric", "prepared_spell_ids": ["cure-wounds"]},
            ],
            start_clock=None,
            duration_minutes=480,
            reason="The party completed the already-recorded long rest.",
        )
    )

    assert client.party_rest_calls == 1
    assert client.world_time["elapsed_minutes"] == 1440
    assert result["rest_recovered"] is True
    assert result["continuity"]["snapshot"]["slot"] == 5


def test_long_rest_recovery_rejects_a_different_original_request() -> None:
    with pytest.raises(RuntimeError, match="request does not match"):
        regression_playthrough._validate_recovered_long_rest(
            {
                "replayed": True,
                "request_hash": "original-request",
                "response": {},
            },
            campaign={},
            actors=[],
            members=[],
            duration_minutes=480,
            expected_request_hash="different-request",
        )


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


def test_partially_committed_contest_is_recovered_without_reroll() -> None:
    result = {
        "kind": "ability_contest",
        "source_actor_id": "bard",
        "target_actor_id": "cultist",
        "outcome": "tie_no_change",
    }
    campaign = {
        "state": {
            "random_stream": {"last_receipt": {"operation": "character_check"}},
            "resolution_log": [
                {
                    "type": "ability_contest",
                    "source_actor_id": "bard",
                    "target_actor_id": "cultist",
                    "result": result,
                }
            ],
        }
    }

    assert (
        _recover_committed_contest(
            campaign,
            progress_matches=True,
            source_actor_id="bard",
            target_actor_id="cultist",
        )
        == result
    )
    assert (
        _recover_committed_contest(
            campaign,
            progress_matches=False,
            source_actor_id="bard",
            target_actor_id="cultist",
        )
        is None
    )


def test_xp_award_uses_source_ref_and_keeps_dead_participant_share() -> None:
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
                    "sheet": {
                        "conditions": ["dead"] if actor_id == "actor-1" else [],
                    },
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
            occurrence_id="hideout-xp-award-1",
            scene_id="scene-1",
            source_ref=source_ref,
            actor_ids=["actor-1", "actor-2"],
            amount=75,
            reason="Reached the hideout",
        )
    )

    assert [item["new_xp"] for item in result["award"]["awards"]] == [75, 75]


def test_xp_award_idempotency_identity_uses_explicit_occurrence() -> None:
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

    async def award(client: Client, occurrence_id: str) -> None:
        await _award_experience(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id=occurrence_id,
            scene_id="scene-1",
            source_ref=source_ref,
            actor_ids=["actor-1"],
            amount=75,
            reason="Reached the hideout",
        )

    client = Client()
    asyncio.run(award(client, "hideout-award-1"))
    asyncio.run(award(client, "hideout-award-2"))

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
            if tool_id == "memory_change":
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
            occurrence_id="snare-detected-1",
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
    assert client.continuity_payload["event"]["payload"]["source_scene_id"] == ("source-scene-1")
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
            if tool_id == "memory_change":
                assert arguments["payload"]["actor_knowledge"][0]["cause"] == ("told_by")
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
            occurrence_id="yeemik-ransom-demand-1",
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
            if tool_id == "memory_query":
                assert arguments["view"] == "list"
                assert arguments["payload"] == {"include_inactive": False}
                return {
                    "result": [
                        {
                            "fact_key": "quest:hostage:status",
                            "revision_id": "fact-revision-7",
                        }
                    ]
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
            if tool_id == "memory_change":
                self.continuity_payload = deepcopy(arguments["payload"])
                assert "snapshot" not in self.continuity_payload
                assert {item["cause"] for item in self.continuity_payload["actor_knowledge"]} == {
                    "witnessed"
                }
                assert self.continuity_payload["facts"][0]["fact_key"] == ("quest:hostage:status")
                assert self.continuity_payload["facts"][0]["expected_revision_id"] == (
                    "fact-revision-7"
                )
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
    assert client.continuity_payload["event"]["payload"]["source_scene_id"] == ("source-scene-1")
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
            if tool_id == "memory_query":
                return {"result": []}
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
    source_excerpt = "The adventure begins here."
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-opening",
        "page_start": 1,
        "page_end": 1,
        "heading_path": ["Chapter 1", "Opening"],
        "content_sha256": "b" * 64,
    }

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
                    "content": source_excerpt,
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
            source_excerpt=source_excerpt,
            source_ref=source_ref,
            objective="Survive the ambush",
            reachable_scene_ids=["scene-2"],
        )
    )

    assert result["sync"]["campaign_revision"] == 10
    assert client.manifest["status"] == "in_progress"
    assert client.manifest["current"]["scene_id"] == "scene-1"
    assert client.manifest["traversal"]["visited_scene_ids"] == ["scene-1"]
    assert any(name == "game_phase" for name, _ in client.calls)
