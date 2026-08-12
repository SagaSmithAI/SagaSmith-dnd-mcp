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
    _cast_standard_spell,
    _check_identity,
    _check_knowledge_key,
    _checkpoint,
    _claim_party_item_for_character,
    _commit_roll_continuity,
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
    _manifest_recovery_inputs,
    _matching_check_progress,
    _matching_contest_progress,
    _module_progress_remap_rulings,
    _module_refresh_identity,
    _module_refresh_manifest_action,
    _module_refresh_manifest_identity,
    _mutation_key,
    _occurrence_identity,
    _party_member,
    _party_selections,
    _pool_character_currency,
    _preflight_level_completion,
    _prepare_narrative_npc,
    _prepare_segment_continuation,
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
    _revive_character,
    _roll_source_sequence,
    _roll_source_table,
    _scene_progress_percent,
    _scene_progress_write_status,
    _segment_completion_record,
    _set_source_exhaustion,
    _short_rest,
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
        "content_sha256": "b" * 64,
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "excerpt": "The hostage is released.",
    }


def test_scene_progress_writes_keep_current_scene_authoritative() -> None:
    assert _scene_progress_write_status({"status": "current"}, completed=False) == "current"
    assert _scene_progress_write_status({"status": "current"}, completed=True) == "current"
    assert _scene_progress_write_status({"status": "active"}, completed=True) == "completed"
    assert _scene_progress_write_status(None, completed=False) == "active"


def test_manifest_recovery_revalidates_imported_modules_and_reviewed_templates() -> None:
    module_sha = "a" * 64
    player_sha = "b" * 64
    corpus_manifest = {
        "campaign_lines": [
            {
                "id": "storm-kings-thunder",
                "modules": [
                    {
                        "path": "SKT.pdf",
                        "sequence": 1,
                        "sha256": module_sha,
                    }
                ],
                "player_materials": [
                    {
                        "path": "HunterRanger.pdf",
                        "sha256": player_sha,
                        "review_status": "reviewed_not_module_pregen",
                    }
                ],
                "play_requirements": {
                    "recommended_party_size": {
                        "status": "source_confirmed",
                        "minimum": 4,
                        "maximum": 6,
                        "selected": 6,
                    }
                },
            }
        ]
    }
    import_report = {
        "action": "full-campaign-corpus-import",
        "passed": True,
        "campaigns": [
            {
                "campaign_line_id": "storm-kings-thunder",
                "campaign_id": "campaign-1",
                "documents": [
                    {
                        "relative_path": "SKT.pdf",
                        "checksum": module_sha,
                        "module_id": "module-1",
                    },
                    {
                        "relative_path": "HunterRanger.pdf",
                        "checksum": player_sha,
                        "character_document": {
                            "document_kind": "character_sheet",
                            "ready_to_create": False,
                            "missing_fields": ["name", "level"],
                        },
                    },
                ],
            }
        ],
    }

    recovered = _manifest_recovery_inputs(
        corpus_manifest=corpus_manifest,
        import_report=import_report,
        campaign_id="campaign-1",
        campaign_line_id="storm-kings-thunder",
    )

    assert [item["module_id"] for item in recovered["module_documents"]] == ["module-1"]
    assert recovered["review_blocks"] == []


@pytest.fixture(autouse=True)
def _stub_exact_chunk_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep action tests focused while the validator's own tests exercise module_expand."""
    validate = regression_playthrough._validate_source_ref

    async def validate_with_cited_chunk(
        client,
        scene: dict,
        source_ref: dict | None,
        *,
        excerpt: str = "",
    ) -> dict:
        original_domain = client.domain

        async def domain_with_cited_chunk(tool_id: str, arguments: dict):
            if tool_id == "module_expand" and isinstance(source_ref, dict):
                return {
                    "chunk_id": source_ref.get("chunk_id"),
                    "content": scene.get("content"),
                    "content_sha256": source_ref.get("content_sha256"),
                    "source_ref": deepcopy(source_ref),
                }
            return await original_domain(tool_id, arguments)

        client.domain = domain_with_cited_chunk
        try:
            return await validate(
                client,
                scene,
                source_ref,
                excerpt=excerpt,
            )
        finally:
            client.domain = original_domain

    monkeypatch.setattr(
        regression_playthrough,
        "_validate_source_ref",
        validate_with_cited_chunk,
    )


def test_register_party_does_not_block_on_unresolved_party_recommendation() -> None:
    manifest = new_playthrough_manifest(
        run_id="run-1",
        campaign_line_id="unknown-size",
        module_ids=["module-1"],
        recommended_party_minimum=None,
        recommended_party_maximum=None,
        selected_party_size=None,
        source_refs=[_manifest_source_ref()],
        review_blocks=[{"kind": "recommended_party_size"}],
    )

    class Client:
        def __init__(self) -> None:
            self.manifest = manifest
            self.revision = 1

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "playthrough_manifest" and arguments["action"] == "get":
                return {"manifest": deepcopy(self.manifest)}
            if tool_id == "character_query":
                return {
                    "id": "pc-1",
                    "name": "First PC",
                    "campaign_id": "campaign-1",
                    "character_type": "pc",
                    "sheet": default_character_sheet(),
                    "derived": {},
                }
            if tool_id == "playthrough_manifest" and arguments["action"] == "replace":
                self.manifest = deepcopy(arguments["payload"]["manifest"])
                self.revision += 1
                return {"manifest": deepcopy(self.manifest), "campaign_revision": self.revision}
            if tool_id == "playthrough_manifest" and arguments["action"] == "sync":
                return {"manifest": deepcopy(self.manifest), "campaign_revision": self.revision}
            raise AssertionError((tool_id, arguments))

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": self.revision}}

    result = asyncio.run(
        regression_playthrough._register_party(
            Client(),
            campaign_id="campaign-1",
            run_id="run-1",
            selections=[{"actor_id": "pc-1", "source": "generated"}],
        )
    )

    assert result["manifest"]["party"]["members"][0]["actor_id"] == "pc-1"


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


def test_source_roll_modifier_ledger_keeps_independent_lifetimes() -> None:
    modifiers = regression_playthrough._normalize_roll_modifiers(
        [
            {
                "modifier_id": "day-water-table-count",
                "value": 2,
                "kind": "cumulative",
                "lifetime": "persistent",
                "state_key": "day_water_table_bonus",
                "basis": "Two previous daytime table rolls made from the ship on water.",
            },
            {
                "modifier_id": "followed-hunters",
                "value": 1,
                "kind": "limited_use",
                "lifetime": "until_consumed",
                "state_key": "next_daytime_event_bonus",
                "basis": "Following the hunters grants +1 to the next daytime event roll.",
            },
        ],
        expression="1d6+3",
    )

    assert [item["kind"] for item in modifiers] == ["cumulative", "limited_use"]
    assert sum(item["value"] for item in modifiers) == 3


def test_source_roll_modifier_ledger_rejects_merged_state_and_wrong_total() -> None:
    merged = [
        {
            "modifier_id": "cumulative",
            "value": 2,
            "kind": "cumulative",
            "lifetime": "persistent",
            "state_key": "event_bonus",
            "basis": "Prior qualifying rolls.",
        },
        {
            "modifier_id": "limited",
            "value": 1,
            "kind": "limited_use",
            "lifetime": "until_consumed",
            "state_key": "event_bonus",
            "basis": "Next qualifying roll.",
        },
    ]
    with pytest.raises(ValueError, match="must not share one state_key"):
        regression_playthrough._normalize_roll_modifiers(merged, expression="1d6+3")

    with pytest.raises(ValueError, match="ledger total does not match"):
        regression_playthrough._normalize_roll_modifiers(
            [dict(merged[0], state_key="cumulative_bonus")],
            expression="1d6+3",
        )


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
            self.progress: dict | None = None
            self.progress_calls: list[dict] = []

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": self.revision}}

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                if arguments["view"] == "progress":
                    return [deepcopy(self.progress)] if self.progress is not None else []
                if arguments["view"] == "current":
                    if self.progress is None or self.progress["status"] != "current":
                        return None
                    return {
                        "scene_id": self.progress["scene_id"],
                        "progress": {
                            "status": self.progress["status"],
                            "percent": self.progress["progress"],
                        },
                    }
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
            if tool_id == "module_set_progress":
                self.progress_calls.append(deepcopy(arguments))
                self.progress = {
                    "scene_id": arguments["scene_id"],
                    "scope_id": "party",
                    "status": arguments["status"],
                    "progress": arguments["progress"],
                    "state": deepcopy(arguments["state"]),
                    "current_room": "",
                    "current_location_key": arguments.get("current_location_key") or "",
                    "state_version": len(self.progress_calls),
                }
                return deepcopy(self.progress)
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
    assert len(client.progress_calls) == 1
    assert client.progress_calls[0]["status"] == "current"
    assert client.progress_calls[0]["expected_state_version"] == 0

    # Repair playthroughs written by the former driver without replaying the
    # already-committed transition under a changed idempotency payload.
    assert client.progress is not None
    client.progress["status"] = "active"
    asyncio.run(advance(client, "town-visit-1"))
    assert len(client.progress_calls) == 2
    assert client.progress_calls[-1]["status"] == "current"
    assert (
        client.progress_calls[-1]["idempotency_key"] != client.progress_calls[0]["idempotency_key"]
    )

    client.manifest["world_state"]["visit_marker"] = 2
    client.manifest["current"]["scene_id"] = "scene-old"
    asyncio.run(advance(client, "town-visit-2"))
    revisit_key = client.replace_calls[3]["idempotency_key"]
    assert revisit_key != first_key
    assert len(client.progress_calls) == 3
    assert client.progress_calls[2]["expected_state_version"] == 2
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
    transition_ruling = {
        "default_resolver": "agent",
        "ruling_kind": "agent_dm_adjudication",
        "decision": ("The survivors take the established road from the current scene to Town."),
        "reason": ("The cited source establishes the destination but not the descriptive route."),
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
            agent_ruling=transition_ruling,
            occurrence_scene_id="scene-old",
        )
    )
    assert client.manifest["world_state"]["scene_transitions"]["town-visit-sibling-source"] == {
        "from_scene_id": "scene-old",
        "to_scene_id": "scene-town",
        "source_excerpt": "The survivors carry the Stone to Town.",
        "source_ref": citation_ref,
        "agent_ruling": {
            **transition_ruling,
            "committed": True,
        },
    }
    assert len(client.progress_calls) == 4


def test_advance_scene_recovers_when_progress_commits_before_manifest() -> None:
    source_excerpt = "The council summons the heroes back to Waterdeep."
    source_ref = {
        "module_id": "module-2",
        "scene_id": "scene-citation",
        "chunk_id": "chunk-transition",
        "page_start": 20,
        "page_end": 20,
        "heading_path": ["Episode 1", "Starting the Adventure"],
        "content_sha256": "b" * 64,
    }

    class Client:
        def __init__(self) -> None:
            self.revision = 4
            self.failed_once = False
            self.current_scene_id = "scene-old"
            self.progress: dict | None = None
            self.manifest = new_playthrough_manifest(
                run_id="run-1",
                campaign_line_id="line-1",
                module_ids=["module-1", "module-2"],
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
                "objective": "Finish the first volume.",
            }

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"result": {"id": "campaign-1", "revision": self.revision}}

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query" and arguments["view"] == "progress":
                return [deepcopy(self.progress)] if self.progress is not None else []
            if tool_id == "module_query":
                if arguments["view"] == "current":
                    if self.progress is None or self.progress["status"] != "current":
                        return None
                    return {
                        "scene_id": self.progress["scene_id"],
                        "progress": {
                            "status": self.progress["status"],
                            "percent": self.progress["progress"],
                        },
                    }
                requested = arguments["payload"]["scene_id"]
                if requested == "scene-old":
                    return {
                        "module_id": "module-1",
                        "chapter_id": "chapter-1",
                        "chapter": "Chapter",
                        "scene_id": requested,
                        "title": "Old scene",
                        "content": "The first volume ends.",
                    }
                if requested == "scene-citation":
                    return {
                        "module_id": "module-2",
                        "chapter_id": "chapter-2",
                        "chapter": "Episode 1",
                        "scene_id": requested,
                        "title": "Starting the Adventure",
                        "content": source_excerpt,
                    }
                assert requested == "scene-new"
                return {
                    "module_id": "module-2",
                    "chapter_id": "chapter-2",
                    "chapter": "Episode 1",
                    "scene_id": requested,
                    "title": "Back in Waterdeep",
                    "spatial": {
                        "locations": [{"key": "back-in-waterdeep", "title": "Back in Waterdeep"}]
                    },
                }
            if tool_id == "module_set_progress":
                self.progress = {
                    "scene_id": arguments["scene_id"],
                    "status": arguments["status"],
                    "progress": arguments["progress"],
                    "state": deepcopy(arguments["state"]),
                    "current_location_key": arguments["current_location_key"],
                    "state_version": 1,
                }
                self.current_scene_id = arguments["scene_id"]
                return deepcopy(self.progress)
            if tool_id == "playthrough_manifest" and arguments["action"] == "get":
                projected = deepcopy(self.manifest)
                if self.current_scene_id == "scene-new":
                    projected["current"] = {
                        "module_id": "module-2",
                        "chapter_id": "chapter-2",
                        "chapter_title": "Episode 1",
                        "scene_id": "scene-new",
                        "scene_title": "Back in Waterdeep",
                        "objective": projected["current"]["objective"],
                    }
                return {"manifest": projected, "campaign_revision": self.revision}
            if tool_id == "playthrough_manifest" and arguments["action"] == "replace":
                if not self.failed_once:
                    self.failed_once = True
                    raise RuntimeError("response lost after SceneProgress commit")
                self.manifest = deepcopy(arguments["payload"]["manifest"])
                self.revision += 1
                return {
                    "manifest": deepcopy(self.manifest),
                    "campaign_revision": self.revision,
                }
            raise AssertionError((tool_id, arguments))

    async def advance(client: Client) -> dict:
        return await _advance_scene(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="continue-volume-2",
            scene_id="scene-new",
            source_scene_id="scene-citation",
            source_excerpt=source_excerpt,
            source_ref=source_ref,
            objective="Attend the council.",
            mark_visited=True,
            reachable_scene_ids=[],
            excluded_scenes=[],
            occurrence_scene_id="scene-old",
            location_key="back-in-waterdeep",
        )

    client = Client()
    with pytest.raises(RuntimeError, match="response lost"):
        asyncio.run(advance(client))
    result = asyncio.run(advance(client))

    assert result["manifest"]["current"]["scene_id"] == "scene-new"
    assert result["scene_progress"]["state_version"] == 1
    assert (
        result["manifest"]["world_state"]["scene_transitions"]["continue-volume-2"]["from_scene_id"]
        == "scene-old"
    )


def _completed_segment_manifest() -> dict:
    manifest = new_playthrough_manifest(
        run_id="run-1",
        campaign_line_id="line-1",
        module_ids=["module-1", "module-2"],
        recommended_party_minimum=1,
        recommended_party_maximum=1,
        selected_party_size=1,
        source_refs=[_manifest_source_ref()],
    )
    manifest["status"] = "completed"
    manifest["current"] = {
        "module_id": "module-1",
        "chapter_id": "chapter-1",
        "chapter_title": "Volume 1",
        "scene_id": "scene-ending",
        "scene_title": "Victory",
        "objective": "Verify the first volume ending.",
    }
    manifest["party"]["members"] = [
        {
            "actor_id": "pc-1",
            "name": "Hero",
            "status": "active",
            "source": "generated",
            "source_asset_path": "",
            "level": 8,
            "xp": 0,
            "hit_points": {"current": 40, "maximum": 40, "temporary": 0},
            "resources": {},
            "wallet": {"gp": 100},
            "equipment": ["sword"],
            "knowledge_scope_actor_id": "pc-1",
        }
    ]
    condition = {
        "id": "volume-1-victory",
        "label": "Volume 1 completed",
        "source_ref": _manifest_source_ref(),
        "all_of": [
            {
                "kind": "manifest_value",
                "path": "world_state.volume_1_complete",
                "actor_id": "",
                "fact_key": "",
                "operator": "equals",
                "value": True,
            }
        ],
    }
    manifest["world_state"] = {
        "volume_1_complete": True,
        "_canonical": {
            "game_time": {"schema_version": 1, "tick_seconds": 6, "elapsed_ticks": 9000},
            "world_time": {
                "schema_version": 2,
                "tick_seconds": 6,
                "calendar_offset_ticks": 0,
                "day": 1,
                "hour": 15,
                "minute": 0,
                "second": 0,
                "elapsed_minutes": 900,
                "round_remainder": 0,
                "label": "Test",
            },
        },
    }
    manifest["snapshot_dag"] = {
        "active_branch_id": "branch-1",
        "head_snapshot_id": "snapshot-10",
        "nodes": [
            {
                "id": "snapshot-10",
                "parent_id": "snapshot-9",
                "branch_id": "branch-1",
                "slot": 10,
                "label": "Volume 1 formal ending",
                "checksum": "c" * 64,
                "is_head": True,
            }
        ],
    }
    manifest["random_stream"] = {
        "algorithm": "sha256-counter-v1",
        "seed_fingerprint": "seed",
        "position": 123,
    }
    manifest["ending"] = {
        "status": "completed",
        "conditions": [condition],
        "achieved_condition_id": condition["id"],
        "verification": [
            {
                "kind": "manifest_value",
                "path": "world_state.volume_1_complete",
                "operator": "equals",
                "expected": True,
                "actual": True,
                "passed": True,
            }
        ],
    }
    return validate_playthrough_manifest(manifest)


def test_prepare_segment_continuation_archives_terminal_evidence() -> None:
    original = _completed_segment_manifest()

    updated, record, recovered = _prepare_segment_continuation(
        original,
        condition_id="volume-1-victory",
        next_module_id="module-2",
    )

    assert recovered is False
    assert updated["status"] == "in_progress"
    assert updated["ending"] == {
        "status": "pending",
        "conditions": [],
        "achieved_condition_id": "",
        "verification": [],
    }
    assert record["completed_module_id"] == "module-1"
    assert record["next_module_id"] == "module-2"
    assert record["terminal_snapshot"]["id"] == "snapshot-10"
    assert record["random_stream"]["position"] == 123
    assert updated["world_state"]["completed_segments"] == [record]
    assert updated["world_state"]["volume_1_complete"] is True
    assert original["status"] == "completed"

    resumed, resumed_record, resumed_flag = _prepare_segment_continuation(
        updated,
        condition_id="volume-1-victory",
        next_module_id="module-2",
    )
    assert resumed_flag is True
    assert resumed == updated
    assert resumed_record == record


def test_segment_continuation_rejects_missing_terminal_head_or_same_module() -> None:
    manifest = _completed_segment_manifest()
    manifest["snapshot_dag"]["head_snapshot_id"] = "missing"
    with pytest.raises(ValueError, match="verified terminal head"):
        _segment_completion_record(
            manifest,
            condition_id="volume-1-victory",
            next_module_id="module-2",
        )

    manifest = _completed_segment_manifest()
    with pytest.raises(ValueError, match="different next module"):
        _prepare_segment_continuation(
            manifest,
            condition_id="volume-1-victory",
            next_module_id="module-1",
        )


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
                assert arguments["idempotency_key"] == _mutation_key(
                    "run-1",
                    "core-relock",
                    "old-core:snapshot-1",
                )
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


def test_core_relock_driver_skips_current_runtime_without_snapshot_or_sync() -> None:
    class Client:
        def __init__(self) -> None:
            self.tools: list[str] = []

        async def domain(self, tool_id: str, arguments: dict):
            self.tools.append(tool_id)
            assert tool_id == "campaign_rules"
            assert arguments["action"] == "get_profile"
            return {
                "profile": {"options": {"_core_rule_pack_lock": {"fingerprint": "current-core"}}},
                "available_core_pack": {
                    "id": "dnd5e.core.2014",
                    "fingerprint": "current-core",
                },
                "campaign_revision": 20,
            }

    client = Client()
    result = asyncio.run(
        _relock_core(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            reason="Confirm the runtime Core is already current.",
        )
    )

    assert result["status"] == "current"
    assert result["mutation_applied"] is False
    assert client.tools == ["campaign_rules"]


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
                        "effective_game_phase": self.phase,
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
    assert client.loaded[-1] == ()


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
@pytest.mark.parametrize("pre_registered", [False, True])
def test_narrative_npc_driver_round_trips_lobby_and_registers_manifest(
    defer_checkpoint: bool,
    pre_registered: bool,
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
            if pre_registered:
                self.manifest["npcs"] = [
                    {
                        "actor_id": "npc-1",
                        "name": "Qelline Alderleaf",
                        "status": "active",
                        "faction": "Phandalin",
                        "relationship": "later relationship that must be preserved",
                        "notes": (
                            "Narrative-only source-bound actor; "
                            "combat_statblock=not_imported; module=module-1; "
                            "scene=scene-1; chunk=chunk-1; pages=18-18; "
                            f"sha256={'b' * 64}."
                        ),
                    }
                ]
            self.snapshot_calls = 0
            self.manifest_replace_calls = 0

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
                        "effective_game_phase": self.phase,
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
                assert arguments["payload"]["source_ref"] == canonical_source_ref
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
                    self.manifest_replace_calls += 1
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
    assert client.manifest_replace_calls == (0 if pre_registered else 1)
    if pre_registered:
        assert (
            client.manifest["npcs"][0]["relationship"]
            == "later relationship that must be preserved"
        )
    assert client.snapshot_calls == (0 if defer_checkpoint or pre_registered else 1)
    if defer_checkpoint or pre_registered:
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


def test_narrative_npc_driver_strictly_binds_agent_assigned_identity() -> None:
    ruling = {
        "default_resolver": "agent",
        "ruling_kind": "agent_dm_adjudication",
        "decision": "Name the first anonymous townsperson Caldan Voss.",
        "reason": "The source specifies anonymous townsfolk without names.",
        "assigned_name": "Different Name",
        "source_identity": "Townsfolk",
        "instance_key": "retreat-1",
    }
    with pytest.raises(
        ValueError,
        match="identity Agent ruling must match: assigned_name",
    ):
        asyncio.run(
            _prepare_narrative_npc(
                object(),
                campaign_id="campaign-1",
                run_id="run-1",
                occurrence_id="anonymous-agent-name-1",
                initial_phase="play",
                scene_id="scene-1",
                location_key="gate",
                source_excerpt="Two townsfolk wait by the gate.",
                source_ref={},
                name="Caldan Voss",
                role="Anonymous source-counted townsperson.",
                summary="A separately tracked anonymous townsperson.",
                faction="Greenest",
                relationship="rescued civilian",
                source_identity="Townsfolk",
                instance_key="retreat-1",
                identity_agent_ruling=ruling,
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


@pytest.mark.parametrize(
    "mechanics",
    [
        {
            "attack_type": "melee",
            "attack_ability": "dexterity",
            "damage_formula": "1d6",
            "damage_type": "slashing",
        },
        {
            "attack_type": "melee",
            "attack_ability": "dexterity",
            "damage_formula": "1d6",
            "damage_type": "slashing",
            "proficient": "false",
        },
    ],
)
def test_source_loot_driver_requires_explicit_boolean_weapon_proficiency(
    mechanics: dict,
) -> None:
    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            raise AssertionError((tool_id, arguments))

    with pytest.raises(
        ValueError,
        match=r"weapon item 0 requires explicit boolean mechanics\.proficient",
    ):
        asyncio.run(
            _acquire_source_loot(
                Client(),
                campaign_id="campaign-1",
                run_id="run-1",
                scene_id="scene-1",
                location_key="barracks",
                source_excerpt="The defeated cultists carried scimitars.",
                source_ref={},
                acquisition_id="cultist-weapons",
                coins={},
                items=[
                    {
                        "id": "cultist-scimitar",
                        "name": "Cultist scimitar",
                        "kind": "weapon",
                        "quantity": 1,
                        "mechanics": mechanics,
                    }
                ],
                reason="The party recovered an intact scimitar.",
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
        "quantity": 2,
        "source_key": "module-chunk:gazer-chunk",
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
        "quantity": 2,
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
                self.party["inventory"]["items"][0]["quantity"] = 1
                moved = {
                    **deepcopy(stone),
                    "id": "claimed-stone-fragment",
                    "quantity": 1,
                }
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
            quantity=1,
            reason="The party entrusts the recovered Stone to Pip.",
            checkpoint_label="Pip carries the Stone",
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert client.transfer_arguments is not None
    assert client.transfer_arguments["mode"] == "party_to_character"
    assert client.transfer_arguments["payload"]["expected_campaign_revision"] == 21
    assert client.transfer_arguments["payload"]["expected_character_revision"] == 7
    assert client.transfer_arguments["payload"]["quantity"] == 1
    assert client.transfer_arguments["idempotency_key"] == _mutation_key(
        "run-1",
        "party-item-claim",
        _occurrence_identity("stone-bearer-1", "claim-party-item"),
    )
    assert result["item_id"] == "stone-of-golorr"
    assert result["claimed_item_id"] == "claimed-stone-fragment"
    assert checkpoint_calls == (0 if defer_checkpoint else 1)
    if defer_checkpoint:
        assert result["checkpoint"] is None
    else:
        assert result["checkpoint"]["verification"]["valid"] is True


def test_party_item_claim_recovers_committed_partial_split_from_public_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "reward-chunk",
        "page_start": 78,
        "page_end": 78,
        "heading_path": ["Mission to Thay", "Conclusion"],
        "content_sha256": "a" * 64,
    }
    party_item = {
        "id": "protection-bone",
        "name": "Finger Bone of Protection from Undead",
        "kind": "consumable",
        "quantity": 3,
        "description": "Snap it to activate its protection.",
        "source_key": "module-chunk:reward-chunk",
        "uses": {"value": 0, "max": 0},
        "charges": {"value": 0, "max": 0},
        "mechanics": {},
    }
    claimed_item = {
        **deepcopy(party_item),
        "id": "split-protection-bone",
        "quantity": 1,
    }

    class Client:
        def __init__(self) -> None:
            sheet = default_character_sheet()
            sheet["inventory"]["items"].append(deepcopy(claimed_item))
            self.actor = {
                "id": "brynja",
                "name": "Brynja",
                "campaign_id": "campaign-1",
                "revision": 8,
                "sheet": sheet,
                "derived": {"armor_class": 18},
            }
            self.party = {"inventory": {"items": [deepcopy(party_item)]}}
            self.receipt_arguments: dict | None = None

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            assert arguments["view"] == "party"
            return {"result": deepcopy(self.party)}

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "Each character finds a finger bone among their belongings.",
                    "spatial": {"locations": [{"key": "thay", "title": "Thay"}]},
                }
            if tool_id == "character_query":
                return deepcopy(self.actor)
            if tool_id == "state_revision":
                self.receipt_arguments = deepcopy(arguments)
                return {
                    "key": arguments["payload"]["idempotency_key"],
                    "response": {
                        "party": deepcopy(self.party),
                        "character": deepcopy(self.actor),
                        "item": deepcopy(claimed_item),
                    },
                }
            if tool_id == "inventory_transfer":
                raise AssertionError("a committed split must not be submitted again")
            raise AssertionError((tool_id, arguments))

    monkeypatch.setattr(
        regression_playthrough,
        "_checkpoint",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("deferred recovery must not checkpoint")
        ),
    )
    client = Client()
    result = asyncio.run(
        _claim_party_item_for_character(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="brynja-bone",
            scene_id="scene-1",
            location_key="thay",
            source_excerpt="Each character finds a finger bone among their belongings.",
            source_ref=source_ref,
            character_id="brynja",
            item_id="protection-bone",
            quantity=1,
            reason="Brynja takes her source-authored reward.",
            checkpoint_label="",
            defer_checkpoint=True,
        )
    )

    assert client.receipt_arguments == {
        "campaign_id": "campaign-1",
        "action": "receipt",
        "payload": {
            "idempotency_key": _mutation_key(
                "run-1",
                "party-item-claim",
                _occurrence_identity("brynja-bone", "claim-party-item"),
            )
        },
    }
    assert result["recovered"] is True
    assert result["claimed_item_id"] == "split-protection-bone"
    assert result["transfer"]["status"] == "recovered"


@pytest.mark.parametrize("defer_checkpoint", [False, True])
def test_source_effect_application_uses_public_character_transition(
    defer_checkpoint: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "source-scene",
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
                if arguments["payload"]["scene_id"] == "source-scene":
                    return {
                        "module_id": "module-1",
                        "scene_id": "source-scene",
                        "content": "A failed save charms the creature for 24 hours.",
                        "spatial": {"locations": []},
                    }
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "The party examines the fresco.",
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
            source_scene_id="source-scene",
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


def test_standard_spell_driver_pays_resources_and_records_agent_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "village-chunk",
        "page_start": 170,
        "page_end": 170,
        "heading_path": ["Village", "Night"],
        "content_sha256": "a" * 64,
    }
    ruling = {
        "default_resolver": "agent",
        "ruling_kind": "generic_spell_effect",
        "decision": (
            "Invisibility makes the willing rogue unseen for up to one hour; "
            "it permits the infiltration but does not add an unprinted check bonus."
        ),
        "reason": "The standard spell text governs visibility while the Agent selects its use.",
    }

    class Client:
        def __init__(self) -> None:
            self.cast_arguments: dict = {}
            self.continuity_payload: dict = {}

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": 9,
                    "effective_game_phase": "play",
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "At night, the yakfolk retire to their huts.",
                    "locations": [{"key": "village"}],
                }
            if tool_id == "character_query":
                actor_id = arguments["payload"]["character_id"]
                return {
                    "id": actor_id,
                    "name": actor_id.title(),
                    "campaign_id": "campaign-1",
                    "revision": 4,
                    "sheet": default_character_sheet(),
                }
            if tool_id == "character_action":
                self.cast_arguments = deepcopy(arguments)
                return {
                    "status": "pending_ruling",
                    "default_resolver": "agent",
                    "ruling_kind": "generic_spell_effect",
                    "committed": True,
                    "result": {
                        "payment": {
                            "economy": "slots",
                            "level": 2,
                            "cost": 1,
                        }
                    },
                }
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "memory_change":
                self.continuity_payload = deepcopy(arguments["payload"])
                return {"event": {"id": "event-1"}}
            raise AssertionError((tool_id, arguments))

    async def manifest_mutation(*_args, **_kwargs):
        return {"manifest": {"status": "in_progress"}}

    monkeypatch.setattr(regression_playthrough, "_manifest_mutation", manifest_mutation)
    client = Client()
    result = asyncio.run(
        _cast_standard_spell(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="invisibility-1",
            scene_id="scene-1",
            source_scene_id="scene-1",
            location_key="village",
            source_excerpt="At night, the yakfolk retire to their huts.",
            source_ref=source_ref,
            actor_id="bard",
            target_id="rogue",
            spell_id="invisibility",
            cast_level=2,
            component_ruling=None,
            agent_ruling=ruling,
            reason="The bard made the rogue invisible before the infiltration.",
            knowledge_actor_ids=[],
            defer_checkpoint=True,
        )
    )

    assert client.cast_arguments["payload"] == {
        "spell_id": "invisibility",
        "cast_level": 2,
    }
    assert result["agent_ruling"] == {**ruling, "committed": True}
    event = client.continuity_payload["event"]
    assert event["event_type"] == "standard_spell_cast"
    assert event["payload"]["payment"]["economy"] == "slots"
    assert "snapshot" not in client.continuity_payload


def test_standard_spell_driver_stops_at_precommit_ruling() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "village-chunk",
        "page_start": 170,
        "page_end": 170,
        "heading_path": ["Village", "Night"],
        "content_sha256": "a" * 64,
    }

    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "At night, the yakfolk retire to their huts.",
                    "locations": [{"key": "village"}],
                }
            if tool_id == "character_query":
                actor_id = arguments["payload"]["character_id"]
                return {
                    "id": actor_id,
                    "name": actor_id.title(),
                    "campaign_id": "campaign-1",
                    "revision": 4,
                    "sheet": default_character_sheet(),
                }
            if tool_id == "character_action":
                return {
                    "status": "pending_ruling",
                    "default_resolver": "agent",
                    "ruling_kind": "environmental_consequence",
                    "reason": "the target is not confirmed willing",
                    "committed": False,
                    "result": {"pending": [{"id": "target-consent"}]},
                }
            raise AssertionError("pre-commit ruling must stop before continuity writes")

    with pytest.raises(RegressionRulingRequiredError) as raised:
        asyncio.run(
            _cast_standard_spell(
                Client(),
                campaign_id="campaign-1",
                run_id="run-1",
                occurrence_id="invisibility-1",
                scene_id="scene-1",
                source_scene_id="scene-1",
                location_key="village",
                source_excerpt="At night, the yakfolk retire to their huts.",
                source_ref=source_ref,
                actor_id="bard",
                target_id="rogue",
                spell_id="invisibility",
                cast_level=2,
                component_ruling=None,
                agent_ruling=None,
                reason="The bard attempted to make the rogue invisible.",
                knowledge_actor_ids=[],
            )
        )

    assert raised.value.requirement["operation"] == "character_action.cast_spell"


def test_standard_spell_driver_sends_engine_owned_invisibility_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "village-chunk",
        "page_start": 170,
        "page_end": 170,
        "heading_path": ["Village", "Night"],
        "content_sha256": "a" * 64,
    }

    class Client:
        def __init__(self) -> None:
            self.cast_arguments: dict = {}

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": 9,
                    "effective_game_phase": "play",
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "At night, the yakfolk retire to their huts.",
                    "locations": [{"key": "village"}],
                }
            if tool_id == "character_query":
                actor_id = arguments["payload"]["character_id"]
                return {
                    "id": actor_id,
                    "name": actor_id.title(),
                    "campaign_id": "campaign-1",
                    "revision": 4,
                    "sheet": default_character_sheet(),
                }
            if tool_id == "character_action":
                self.cast_arguments = deepcopy(arguments)
                return {
                    "status": "committed",
                    "result": {
                        "payment": {
                            "economy": "slots",
                            "level": 2,
                            "cost": 1,
                        },
                        "automatic_effect": "invisibility",
                        "target_ids": ["rogue"],
                    },
                }
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "memory_change":
                return {"event": {"id": "event-1"}}
            raise AssertionError((tool_id, arguments))

    async def manifest_mutation(*_args, **_kwargs):
        return {"manifest": {"status": "in_progress"}}

    monkeypatch.setattr(
        regression_playthrough,
        "_manifest_mutation",
        manifest_mutation,
    )
    client = Client()
    result = asyncio.run(
        _cast_standard_spell(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="invisibility-engine-1",
            scene_id="scene-1",
            source_scene_id="scene-1",
            location_key="village",
            source_excerpt="At night, the yakfolk retire to their huts.",
            source_ref=source_ref,
            actor_id="bard",
            target_id="rogue",
            spell_id="dnd5e.content.srd2014.spell.invisibility",
            cast_level=2,
            component_ruling=None,
            agent_ruling=None,
            reason="The bard made the rogue invisible before the infiltration.",
            knowledge_actor_ids=[],
            defer_checkpoint=True,
        )
    )

    assert client.cast_arguments["payload"] == {
        "spell_id": "dnd5e.content.srd2014.spell.invisibility",
        "cast_level": 2,
        "target_character_ids": ["rogue"],
    }
    assert result["agent_ruling"] is None


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
            self.continuity_payload: dict | None = None

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
                return {
                    "status": "pending_ruling",
                    "default_resolver": "agent",
                    "ruling_kind": "generic_spell_effect",
                    "result": {"payment": {"cost": 1}},
                }
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
                self.continuity_payload = deepcopy(arguments["payload"])
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
    expected_ruling = {
        "default_resolver": "agent",
        "ruling_kind": "generic_spell_effect",
        "decision": (
            "The Agent selects fallen as the target of healing-word and executes "
            "the spell card's structured healing resolution through public dice "
            "and character-state tools."
        ),
        "reason": "The cleric restored the fallen ally.",
        "committed": True,
    }
    assert result["agent_ruling"] == expected_ruling
    assert client.continuity_payload["event"]["payload"]["agent_ruling"] == expected_ruling


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
                        caster_sheet if actor_id == "cleric" else default_character_sheet()
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
@pytest.mark.parametrize("character_owned", [False, True])
def test_source_item_spend_driver_uses_one_public_atomic_campaign_transition(
    defer_checkpoint: bool,
    character_owned: bool,
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
            if tool_id == "character_query":
                assert character_owned
                assert arguments == {
                    "view": "get",
                    "payload": {"character_id": "actor-1"},
                }
                return {
                    "id": "actor-1",
                    "campaign_id": "campaign-1",
                    "revision": 11,
                }
            if tool_id == "campaign_change":
                assert arguments["action"] == "item_spend"
                assert arguments["payload"]["item_id"] == "severed-head"
                assert arguments["payload"]["quantity"] == 1
                if character_owned:
                    assert arguments["payload"]["character_id"] == "actor-1"
                    assert arguments["payload"]["expected_character_revision"] == 11
                else:
                    assert "character_id" not in arguments["payload"]
                    assert "expected_character_revision" not in arguments["payload"]
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
            character_id="actor-1" if character_owned else "",
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert result["spend"]["status"] == "committed"
    assert result["spend"]["removed"]["id"] == "severed-head"
    assert client.tools.count("campaign_change") == 1
    assert client.tools.count("character_query") == int(character_owned)
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
        def __init__(self, branch_id: str = "branch-1") -> None:
            self.campaign_revision = 10
            self.calls: list[tuple[str, dict]] = []
            self.branch_id = branch_id

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
                return [{"id": self.branch_id, "is_current": True}]
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
    stored_progress = deepcopy(
        next(args["state"] for tool, args in client.calls if tool == "module_set_progress")
    )

    class ResumeClient(Client):
        def __init__(self) -> None:
            super().__init__()
            self.progress_writes = 0

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query" and arguments["view"] == "progress":
                self.calls.append((tool_id, arguments))
                return [
                    {
                        "scene_id": "scene-1",
                        "status": "active",
                        "progress": 25,
                        "state_version": 3,
                        "state": deepcopy(stored_progress),
                    }
                ]
            if tool_id == "module_set_progress":
                self.progress_writes += 1
                raise AssertionError("matching source-roll progress must not be rewritten")
            if tool_id == "memory_change":
                raise RuntimeError("resume reached continuity boundary")
            return await super().domain(tool_id, arguments)

    resume_client = ResumeClient()
    with pytest.raises(RuntimeError, match="resume reached continuity boundary"):
        asyncio.run(
            _roll_source_table(
                resume_client,
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
    assert resume_client.progress_writes == 0

    second_client = Client(branch_id="branch-2")
    asyncio.run(
        _roll_source_table(
            second_client,
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
    second_dice_call = next(args for tool, args in second_client.calls if tool == "dnd_dice_roll")
    assert dice_call["idempotency_key"] == second_dice_call["idempotency_key"]
    for tool_id in ("module_set_progress", "memory_change", "playthrough_manifest"):
        first_key = next(args["idempotency_key"] for tool, args in client.calls if tool == tool_id)
        second_key = next(
            args["idempotency_key"] for tool, args in second_client.calls if tool == tool_id
        )
        assert first_key != second_key


def test_source_roll_sequence_keeps_independent_ids_and_one_final_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    async def fake_roll(client, **arguments):
        calls.append(dict(arguments))
        index = len(calls)
        return {
            "roll": {"total": index, "rolls": [index]},
            "random_stream_receipt": {
                "position_before": index - 1,
                "position_after": index,
            },
            "progress": {"state_version": index},
            "continuity": {
                "event": {"id": f"event-{index}"},
                **({} if arguments["defer_checkpoint"] else {"snapshot": {"slot": 10}}),
            },
            "sync": {"campaign_revision": 20 + index},
        }

    monkeypatch.setattr(
        regression_playthrough,
        "_roll_source_table",
        fake_roll,
    )
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 1,
        "page_end": 1,
        "heading_path": ["Road Events"],
        "content_sha256": "a" * 64,
    }
    result = asyncio.run(
        _roll_source_sequence(
            object(),
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            location_key="road",
            source_excerpt="Roll once for each travel day.",
            source_ref=source_ref,
            roll_id="road-day",
            expression="1d20",
            reason="Daily road-event check.",
            audience_scope="dm",
            count=3,
            defer_checkpoint=False,
        )
    )

    assert [call["roll_id"] for call in calls] == [
        "road-day-001",
        "road-day-002",
        "road-day-003",
    ]
    assert [call["defer_checkpoint"] for call in calls] == [True, True, False]
    assert [item["roll"]["total"] for item in result["rolls"]] == [1, 2, 3]
    assert result["rolls"][-1]["snapshot"] == {"slot": 10}
    assert result["checkpoint_deferred"] is False


@pytest.mark.parametrize("count", [0, 1, 1001])
def test_source_roll_sequence_rejects_invalid_count_before_tools(count: int) -> None:
    with pytest.raises(ValueError, match="between 2 and 1000"):
        asyncio.run(
            _roll_source_sequence(
                object(),
                campaign_id="campaign-1",
                run_id="run-1",
                scene_id="scene-1",
                location_key="road",
                source_excerpt="Roll once for each travel day.",
                source_ref={},
                roll_id="road-day",
                expression="1d20",
                reason="Daily road-event check.",
                audience_scope="dm",
                count=count,
            )
        )


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
                assert arguments["payload"]["resting_members"] == [
                    {
                        "character_id": "actor-3",
                        "expected_revision": 3,
                        "hit_dice_spends": [{"key": "fighter:d10", "count": 1}],
                    }
                ]
                self.keys["recovery"] = arguments["idempotency_key"]
                return {
                    "status": "recovered",
                    "elapsed_hours": 4,
                    "recoveries": {"actor-1": {}, "actor-2": {}},
                    "resting_member_ids": ["actor-3"],
                    "rested": {"actor-3": {"hit_dice_rolls": [{"total": 7}]}},
                    "random_stream_receipt": {"start_position": 10, "end_position": 12},
                }
            if tool_id == "memory_change":
                assert len(arguments["payload"]["actor_knowledge"]) == 4
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
            resting_members=[
                {
                    "actor_id": "actor-3",
                    "hit_dice_spends": [{"key": "fighter:d10", "count": 1}],
                }
            ],
            knowledge_actor_ids=["witness"],
            reason="Both stable adventurers recovered while the party waited.",
        )
    )

    assert result["recovery"]["elapsed_hours"] == 4
    assert result["resting_member_ids"] == ["actor-3"]
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


def test_module_progress_remap_uses_exact_source_scene_signature() -> None:
    rulings = _module_progress_remap_rulings(
        {
            "valid": True,
            "ruling_requirements": [
                {
                    "scope_id": "party",
                    "scene_id": "removed-v1",
                    "default_resolver": "agent",
                    "ruling_kind": "source_or_scene_fact",
                }
            ],
        },
        old_index=[
            {
                "scene_id": "removed-v1",
                "stable_key": "chapter-duplicate-title",
                "chapter": "Episode 2",
                "title": "Episode 2",
                "page_start": 25,
                "page_end": 26,
            }
        ],
        new_index=[
            {
                "scene_id": "replacement-v2",
                "stable_key": "episode-2",
                "chapter": "Episode 2",
                "title": "Episode 2",
                "page_start": 25,
                "page_end": 26,
            },
            {
                "scene_id": "next-v2",
                "stable_key": "episode-2-next",
                "chapter": "Episode 2",
                "title": "Next Scene",
                "page_start": 26,
                "page_end": 28,
            },
        ],
    )

    assert rulings == [
        {
            "from_scene_id": "removed-v1",
            "to_scene_id": "replacement-v2",
            "reason": (
                "The Agent acting as DM maps the removed progress scene to "
                "the candidate scene with the exact same chapter, title, "
                "and source page range."
            ),
        }
    ]


def test_module_progress_remap_requires_review_when_source_signature_is_ambiguous() -> None:
    with pytest.raises(
        regression_playthrough.RegressionRulingRequiredError,
        match="returns to agent",
    ):
        _module_progress_remap_rulings(
            {
                "ruling_requirements": [
                    {
                        "scope_id": "party",
                        "scene_id": "removed-v1",
                        "default_resolver": "agent",
                        "ruling_kind": "source_or_scene_fact",
                    }
                ]
            },
            old_index=[
                {
                    "scene_id": "removed-v1",
                    "chapter": "Episode 2",
                    "title": "Crossroads",
                    "page_start": 25,
                    "page_end": 25,
                }
            ],
            new_index=[
                {
                    "scene_id": "candidate-a",
                    "chapter": "Episode 2",
                    "title": "Crossroads",
                    "page_start": 25,
                    "page_end": 25,
                },
                {
                    "scene_id": "candidate-b",
                    "chapter": "Episode 2",
                    "title": "Crossroads",
                    "page_start": 25,
                    "page_end": 25,
                },
            ],
        )


def test_module_revision_extension_applies_progress_remap_to_traversal() -> None:
    manifest = {
        "module_ids": ["module-v1"],
        "current": {
            "module_id": "module-v1",
            "chapter_id": "chapter-v1",
            "chapter_title": "Chapter",
            "scene_id": "current-v1",
            "scene_title": "Current",
        },
        "traversal": {
            "reachable_scene_ids": ["removed-v1", "current-v1"],
            "visited_scene_ids": ["removed-v1", "current-v1"],
        },
    }
    updated = _extend_manifest_for_module_revision(
        manifest,
        old_module_id="module-v1",
        new_module_id="module-v2",
        old_index=[
            {"scene_id": "removed-v1", "stable_key": "removed"},
            {"scene_id": "current-v1", "stable_key": "current"},
        ],
        new_index=[
            {
                "scene_id": "replacement-v2",
                "stable_key": "replacement",
                "chapter_id": "chapter-v2",
                "chapter": "Chapter",
                "title": "Replacement",
            },
            {
                "scene_id": "current-v2",
                "stable_key": "current",
                "chapter_id": "chapter-v2",
                "chapter": "Chapter",
                "title": "Current",
            },
        ],
        scene_remaps={"removed-v1": "replacement-v2"},
    )

    assert updated["traversal"]["reachable_scene_ids"] == [
        "removed-v1",
        "current-v1",
        "replacement-v2",
        "current-v2",
    ]
    assert updated["traversal"]["visited_scene_ids"] == [
        "removed-v1",
        "current-v1",
        "replacement-v2",
        "current-v2",
    ]


def test_module_revision_remaps_exact_ending_source_and_scene_check() -> None:
    source_ref = _manifest_source_ref()
    source_ref.update(
        {
            "asset_sha256": "f" * 64,
            "module_id": "module-v1",
            "scene_id": "ending-v1",
            "chunk_id": "chunk-v1",
            "content_sha256": "e" * 64,
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
        "draft-module-v2": [
            {
                "scene_id": "draft-scene-v2",
                "stable_key": "chapter-cave",
                "chapter_id": "draft-chapter-v2",
                "chapter": "Chapter",
                "title": "Cave",
            }
        ],
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
            assert group_ids == ()

        async def open(self, campaign_id: str) -> None:
            assert campaign_id == "campaign-1"

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                if arguments["view"] == "list":
                    return [
                        {
                            "id": "module-v1",
                            "source_key": "module-key",
                            "logical_source_key": "module-key",
                            "active": True,
                        }
                    ]
                module_id = arguments["payload"]["module_id"]
                events.append(f"index:{module_id}")
                return indexes[module_id]
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "module_draft":
                action = arguments["action"]
                events.append(action)
                return {
                    "start": {
                        "job": {"id": "job-1"},
                        "module_id": "draft-module-v2",
                        "inspection": {
                            "valid": True,
                            "errors": [],
                            "warnings": [],
                        },
                        "validation": {"valid": True, "ruling_requirements": []},
                    },
                    "finalize": {
                        "artifact": "module-v2.pack",
                    },
                }[action]
            if tool_id == "content_pack":
                action = arguments["action"]
                events.append(action)
                if action == "import":
                    assert arguments["payload"] == {
                        "campaign_id": "campaign-1",
                        "kind": "module",
                        "artifact": "module-v2.pack",
                    }
                    return {"module_id": "module-v2", "activated": False}
                assert action == "activate"
                assert arguments["payload"] == {
                    "campaign_id": "campaign-1",
                    "kind": "module",
                    "module_id": "module-v2",
                }
                return {
                    "activation": {
                        "module_id": "module-v2",
                        "active": True,
                        "replaced_module_ids": ["module-v1"],
                    }
                }
            raise AssertionError((tool_id, arguments))

    async def manifest_get(client, campaign_id: str):
        return {"manifest": deepcopy(manifest)}

    async def campaign_get(client, campaign_id: str):
        return {
            "revision": 4,
            "state": {
                "game_phase": "lobby",
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
            finalization={
                "portable_id": "dnd5e.module.module-key",
                "manifest": {},
                "confirmation": {
                    "confirmed": True,
                    "note": "The Agent reviewed the refresh fixture.",
                },
            },
            return_phase="lobby",
        )
    )

    assert result["new_module_id"] == "module-v2"
    assert result["ingested"]["module_id"] == "draft-module-v2"
    assert result["imported"] == {"module_id": "module-v2", "activated": False}
    assert events.index("index:draft-module-v2") < events.index("import")
    assert events.index("import") < events.index("activate")
    assert events.index("activate") < events.index("index:module-v2")


def test_module_refresh_rejects_changing_the_logical_source_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "module.md"
    source.write_text("# Chapter\n## Cave\nBody.\n", encoding="utf-8")

    class Client:
        async def load(self, *group_ids: str) -> None:
            assert group_ids == ()

        async def domain(self, tool_id: str, arguments: dict):
            assert tool_id == "module_query"
            if arguments["view"] == "list":
                return [
                    {
                        "id": "module-v1",
                        "source_key": "stable-module-key",
                        "logical_source_key": "stable-module-key",
                        "active": True,
                    }
                ]
            return [{"scene_id": "scene-v1", "stable_key": "chapter-cave"}]

    async def manifest_get(client, campaign_id: str):
        return {
            "manifest": {
                "current": {"module_id": "module-v1"},
            }
        }

    monkeypatch.setattr(regression_playthrough, "_manifest_get", manifest_get)

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
                finalization={
                    "portable_id": "dnd5e.module.module-key",
                    "manifest": {},
                    "confirmation": {
                        "confirmed": True,
                        "note": "The Agent reviewed the refresh fixture.",
                    },
                },
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


@pytest.mark.parametrize(
    ("value", "match"),
    [
        (
            {
                "default_resolver": "external_input",
                "ruling_kind": "module_specific_procedure",
                "decision": "Join.",
                "reason": "Reason.",
            },
            "default_resolver must be agent",
        ),
        (
            {
                "default_resolver": "agent",
                "ruling_kind": "player_owned_choice",
                "decision": "Join.",
                "reason": "Reason.",
            },
            "ruling_kind must be module_specific_procedure",
        ),
        (
            {
                "default_resolver": "agent",
                "ruling_kind": "module_specific_procedure",
                "decision": "",
                "reason": "Reason.",
            },
            "decision must contain",
        ),
        (
            {
                "default_resolver": "agent",
                "ruling_kind": "module_specific_procedure",
                "decision": "Join.",
                "reason": "Reason.",
                "payload": {"unvalidated": True},
            },
            "unsupported fields",
        ),
    ],
)
def test_replacement_agent_ruling_is_strictly_bounded(value: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        regression_playthrough._settled_replacement_agent_ruling(value)


@pytest.mark.parametrize(
    ("source_excerpt", "source_ref", "agent_ruling", "match"),
    [
        ("", None, None, "requires exact source evidence or a settled Agent ruling"),
        (
            "Printed arrival.",
            {"chunk_id": "chunk-1"},
            {
                "default_resolver": "agent",
                "ruling_kind": "module_specific_procedure",
                "decision": "The replacement joins.",
                "reason": "The current scene permits an introduction.",
            },
            "either exact source evidence or an Agent ruling, not both",
        ),
    ],
)
def test_replacement_requires_exactly_one_evidence_path(
    source_excerpt: str,
    source_ref: dict | None,
    agent_ruling: dict | None,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        asyncio.run(
            _register_replacement(
                object(),
                campaign_id="campaign-1",
                run_id="run-1",
                predecessor_actor_id="predecessor",
                replacement_actor_id="replacement",
                scene_id="scene-1",
                location_key="inn",
                source_excerpt=source_excerpt,
                source_ref=source_ref,
                agent_ruling=agent_ruling,
                summary="The replacement joins.",
                handoff_knowledge=["The party shares its current objective."],
                witness_actor_ids=["replacement"],
            )
        )


@pytest.mark.parametrize("defer_checkpoint", [False, True])
@pytest.mark.parametrize("evidence_kind", ["source", "agent"])
def test_replacement_join_preserves_predecessor_and_only_hands_off_explicit_knowledge(
    defer_checkpoint: bool,
    evidence_kind: str,
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
                event_payload = arguments["payload"]["event"]["payload"]
                if evidence_kind == "source":
                    assert event_payload["source_ref"] == source_ref
                    assert event_payload["source_excerpt"] == ("The local inn has rooms for rent.")
                    assert event_payload["agent_ruling"] is None
                else:
                    assert event_payload["source_ref"] is None
                    assert event_payload["source_excerpt"] == ""
                    assert event_payload["agent_ruling"] == {
                        "default_resolver": "agent",
                        "ruling_kind": "module_specific_procedure",
                        "decision": "The replacement can join at the inn.",
                        "reason": "The living party members can introduce the new adventurer.",
                        "committed": True,
                    }
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
            source_excerpt=(
                "The local inn has rooms for rent." if evidence_kind == "source" else ""
            ),
            source_ref=source_ref if evidence_kind == "source" else None,
            agent_ruling=(
                {
                    "default_resolver": "agent",
                    "ruling_kind": "module_specific_procedure",
                    "decision": "The replacement can join at the inn.",
                    "reason": ("The living party members can introduce the new adventurer."),
                }
                if evidence_kind == "agent"
                else None
            ),
            summary="New Wizard joined the party at the inn.",
            handoff_knowledge=["Gundren was taken to Cragmaw Castle."],
            witness_actor_ids=["replacement"],
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert result["predecessor"]["retained"] is True
    assert result["predecessor"]["knowledge_count"] == 1
    assert result["replacement"]["knowledge_scope_actor_id"] == "replacement"
    if evidence_kind == "source":
        assert result["scene"]["source_ref"] == source_ref
        assert result["scene"]["agent_ruling"] is None
    else:
        assert result["scene"]["source_ref"] is None
        assert result["scene"]["agent_ruling"]["committed"] is True
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
    with pytest.raises(RuntimeError, match="effective_game_phase"):
        _campaign_phase({"state": {}})
    assert _campaign_phase({"effective_game_phase": "lobby", "state": {}}) == "lobby"
    assert (
        _campaign_phase(
            {
                "effective_game_phase": "combat",
                "state": {"game_phase": "play", "combat": {"active": True}},
            }
        )
        == "combat"
    )
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
                        "effective_game_phase": self.phase,
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
                assert json.loads(arguments["payload"]["source_ref"]) == source_ref
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
            if tool_id == "content_pack":
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


def test_resumed_level_skips_existing_class_prepared_spell_hydration() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-milestone",
        "page_start": 20,
        "page_end": 20,
        "heading_path": ["Rewards"],
        "content_sha256": "abc123",
    }
    sheet = default_character_sheet()
    sheet["progression"].update(
        {
            "level": 3,
            "classes": [
                {
                    "name": "Cleric",
                    "level": 3,
                    "subclass": "Life Domain",
                    "hit_die": 8,
                }
            ],
        }
    )
    sheet["spellcasting"].update(
        {
            "preparation": {"mode": "prepared", "selected_spell_ids": []},
            "spell_slots": {"2": {"max": 2, "value": 2}},
        }
    )
    sheet["content"]["spells"].append({"id": "spell-lesser-restoration"})

    class Client:
        def __init__(self) -> None:
            self.actor = {
                "id": "cleric-1",
                "name": "Mercy",
                "campaign_id": "campaign-1",
                "revision": 8,
                "sheet": deepcopy(sheet),
            }

        async def open(self, campaign_id: str):
            assert campaign_id == "campaign-1"
            return {"exposure_id": "exposure"}

        async def load(self, *_group_ids: str):
            return None

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "The characters reach 3rd level.",
                }
            if tool_id == "character_query":
                return deepcopy(self.actor)
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "character_state_change":
                assert arguments["action"] == "level_advance"
                return {
                    "status": "committed",
                    "character": deepcopy(self.actor),
                    "advancement": {
                        "follow_up": {
                            "feature_artifacts": [],
                            "subclass_options": [],
                            "spell_choices": {
                                "cantrips_to_add": 0,
                                "leveled_spells_to_add": 0,
                            },
                            "prepared_spell_event": None,
                        }
                    },
                }
            if tool_id == "content_pack":
                if arguments["payload"]["content_kind"] == "feature":
                    return []
                return [
                    {
                        "id": "spell-lesser-restoration",
                        "name": "Lesser Restoration",
                        "selection_requirements": {
                            "level": 2,
                            "eligible_classes": ["Cleric"],
                        },
                    }
                ]
            if tool_id == "character_content_apply":
                raise AssertionError("existing class-prepared spell must not be applied twice")
            raise AssertionError((tool_id, arguments))

    result = asyncio.run(
        _advance_level(
            Client(),
            campaign_id="campaign-1",
            run_id="run-1",
            initial_phase="lobby",
            return_phase="lobby",
            scene_id="scene-1",
            source_ref=source_ref,
            actor_id="cleric-1",
            target_level=3,
            class_name="Cleric",
            hp_method="fixed",
            reason="Episode milestone.",
            subclass_artifact_id="",
            feature_selection_values=[],
            spell_selection_values=[
                {
                    "artifact_id": "spell-lesser-restoration",
                    "source_class": "Cleric",
                    "method": "class_prepared",
                }
            ],
            prepared_spell_ids=[],
            checkpoint_label="",
            defer_checkpoint=True,
        )
    )

    assert result["applied_spells"] == ["spell-lesser-restoration"]
    assert result["reused_spells"] == ["spell-lesser-restoration"]


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
            if tool_id == "content_pack":
                assert arguments["payload"] == {
                    "campaign_id": "campaign-1",
                    "kind": "catalog",
                    "content_kind": "feature",
                }
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
    with pytest.raises(
        ValueError,
        match=r"requires an explicit option choice; allowed choices: Defense, Dueling",
    ):
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
        "content_pack",
    ]

    with pytest.raises(
        ValueError,
        match=r"invalid option choice\(s\): Archery; allowed choices: Defense, Dueling",
    ):
        asyncio.run(
            _preflight_level_completion(
                client,
                campaign_id="campaign-1",
                actor=actor,
                class_name="Fighter",
                target_level=2,
                subclass_artifact_id="",
                feature_selections={"feature-style": {"option": "Archery"}},
                spell_selections=[],
                prepared_spell_ids=[],
            )
        )


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


def test_level_preflight_rejects_duplicate_known_spell_before_mutation() -> None:
    spell_id = "dnd5e.content.srd2014.spell.heroism"
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
    sheet["content"]["spells"].append({"id": spell_id})
    actor = {"id": "bard-1", "revision": 3, "sheet": sheet}

    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "character_query":
                return {
                    "status": "ready",
                    "character_id": "bard-1",
                    "character_revision": 3,
                    "new_level": 2,
                    "follow_up": {
                        "feature_artifacts": [],
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
            if tool_id == "content_pack":
                if arguments["payload"]["content_kind"] == "feature":
                    return []
                return [
                    {
                        "id": spell_id,
                        "selection_requirements": {
                            "level": 1,
                            "eligible_classes": ["Bard"],
                        },
                    }
                ]
            raise AssertionError((tool_id, arguments))

    with pytest.raises(
        ValueError,
        match="known or spellbook selections must add new spells; already present",
    ):
        asyncio.run(
            _preflight_level_completion(
                Client(),
                campaign_id="campaign-1",
                actor=actor,
                class_name="Bard",
                target_level=2,
                subclass_artifact_id="",
                feature_selections={},
                spell_selections=[
                    {
                        "artifact_id": spell_id,
                        "source_class": "Bard",
                        "method": "known",
                    }
                ],
                prepared_spell_ids=[],
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
                        "effective_game_phase": self.phase,
                        "state": {"game_phase": ("play" if self.phase == "combat" else self.phase)},
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
    assert all(item == () for item in result_client.loads)


def test_branch_from_snapshot_recovers_after_branch_create_interruption() -> None:
    class Client:
        def __init__(self) -> None:
            self.revision = 30
            self.loads: list[tuple[str, ...]] = []
            self.domain_calls: list[str] = []

        async def open(self, campaign_id: str):
            assert campaign_id == "campaign-1"
            return {"exposure_id": "exposure"}

        async def load(self, *group_ids: str):
            self.loads.append(group_ids)

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.revision,
                    "effective_game_phase": "lobby",
                    "state": {"game_phase": "lobby"},
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            self.domain_calls.append(tool_id)
            if tool_id == "snapshot_query" and arguments["view"] == "list":
                return [
                    {
                        "id": "snapshot-58",
                        "slot": 58,
                        "branch_id": "failed-branch",
                    },
                    {
                        "id": "snapshot-60",
                        "slot": 60,
                        "branch_id": "failed-branch",
                    },
                ]
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
                        "id": "failed-branch",
                        "name": "failed-route",
                        "head_snapshot_id": "snapshot-60",
                        "is_current": False,
                    },
                    {
                        "id": "recovery-branch",
                        "name": "main-after-klarg-defeat",
                        "base_snapshot_id": "snapshot-58",
                        "head_snapshot_id": "snapshot-58",
                        "is_current": True,
                    },
                ]
            if tool_id == "state_revision":
                assert arguments["action"] == "receipt"
                return {
                    "response": {
                        "id": "recovery-branch",
                        "name": "main-after-klarg-defeat",
                        "base_snapshot_id": "snapshot-58",
                        "head_snapshot_id": "snapshot-58",
                        "is_current": True,
                    }
                }
            if tool_id == "playthrough_manifest" and arguments["action"] == "sync":
                self.revision += 1
                return {
                    "manifest": {"status": "in_progress"},
                    "campaign_revision": self.revision,
                }
            if tool_id == "snapshot_create":
                assert arguments["expected_head_snapshot_id"] == "snapshot-58"
                return {
                    "id": "snapshot-61",
                    "slot": 61,
                    "branch_id": "recovery-branch",
                }
            if tool_id == "playthrough_manifest" and arguments["action"] == "get":
                return {"manifest": {"status": "in_progress"}}
            if tool_id == "branch_change":
                raise AssertionError("committed branch creation must not be repeated")
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _branch_from_snapshot(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            initial_phase="lobby",
            snapshot_slot=58,
            branch_name="main-after-klarg-defeat",
            checkpoint_label="Continue from pre-combat state",
        )
    )

    assert result["recovered_after_branch_create_interruption"] is True
    assert result["source_branch"]["id"] == "failed-branch"
    assert result["source_checkpoint"] == {
        "snapshot": {
            "id": "snapshot-60",
            "slot": 60,
            "branch_id": "failed-branch",
        },
        "recovered_existing": True,
    }
    assert result["checkpoint"]["snapshot"]["id"] == "snapshot-61"
    assert "branch_change" not in client.domain_calls
    assert client.loads and all(item == () for item in client.loads)


def test_branch_from_snapshot_recovers_when_target_predates_manifest() -> None:
    class Client:
        async def open(self, campaign_id: str):
            assert campaign_id == "campaign-1"
            return {"exposure_id": "exposure"}

        async def load(self, *group_ids: str):
            return None

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": 31,
                    "effective_game_phase": "lobby",
                    "state": {"game_phase": "lobby"},
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "snapshot_query" and arguments["view"] == "list":
                return [
                    {
                        "id": "snapshot-3",
                        "slot": 3,
                        "branch_id": "main-branch",
                    }
                ]
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
                        "id": "main-branch",
                        "name": "main",
                        "head_snapshot_id": "snapshot-4",
                        "is_current": False,
                    },
                    {
                        "id": "recovery-branch",
                        "name": "manifest-recovery",
                        "base_snapshot_id": "snapshot-3",
                        "head_snapshot_id": "snapshot-3",
                        "is_current": True,
                    },
                ]
            if tool_id == "state_revision":
                return {
                    "response": {
                        "id": "recovery-branch",
                        "name": "manifest-recovery",
                        "base_snapshot_id": "snapshot-3",
                        "head_snapshot_id": "snapshot-3",
                        "is_current": True,
                    }
                }
            if tool_id == "playthrough_manifest":
                raise RuntimeError("campaign has no full-playthrough manifest")
            raise AssertionError((tool_id, arguments))

    result = asyncio.run(
        _branch_from_snapshot(
            Client(),
            campaign_id="campaign-1",
            run_id="run-1",
            initial_phase="lobby",
            snapshot_slot=3,
            branch_name="manifest-recovery",
            checkpoint_label="Recover from pre-manifest snapshot",
        )
    )

    assert result["recovered_after_branch_create_interruption"] is True
    assert result["checkpoint"] == {
        "skipped": True,
        "reason": "The verified target snapshot predates manifest initialization.",
        "required_action": "initialize-manifest",
    }


@pytest.mark.parametrize("defer_checkpoint", [False, True])
@pytest.mark.parametrize("cross_scene", [False, True])
def test_source_cited_check_persists_result_and_explicit_knowledge(
    defer_checkpoint: bool,
    cross_scene: bool,
) -> None:
    source_scene_id = "rules-scene" if cross_scene else "scene-1"
    source_ref = {
        "module_id": "module-1",
        "scene_id": source_scene_id,
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
                requested_scene_id = arguments["payload"]["scene_id"]
                if requested_scene_id == "scene-1" and cross_scene:
                    return {
                        "module_id": "module-1",
                        "scene_id": "scene-1",
                        "content": "The party follows the road through the market.",
                        "locations": [{"key": "ambush"}],
                    }
                assert requested_scene_id == source_scene_id
                return {
                    "module_id": "module-1",
                    "scene_id": source_scene_id,
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
                    "sheet": {
                        "skills": {
                            "survival": {
                                "proficiency": "expertise",
                                "bonus": 0,
                            }
                        }
                    },
                }
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "character_check":
                assert arguments["action"] == "check"
                assert arguments["payload"]["kind"] == "ability"
                assert arguments["payload"]["ability"] == "survival"
                assert arguments["payload"]["bonus"] == -2
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
            source_scene_id=source_scene_id,
            occurrence_id=expected_identity,
            actor_id="actor-1",
            kind="ability",
            ability="survival",
            dc=10,
            proficient=False,
            bonus=-2,
            disadvantage=True,
            knowledge_actor_ids=["actor-2"],
            success_knowledge="The trail shows twelve goblins and two captives.",
            failure_knowledge="The trail's traffic remains unclear.",
            defer_checkpoint=defer_checkpoint,
        )
    )

    assert result["check"] == {"success": True, "total": 14}
    assert result["check_request"] == {
        "actor_id": "actor-1",
        "kind": "ability",
        "ability": "survival",
        "dc": 10,
        "proficient": False,
        "bonus": -2,
        "advantage": False,
        "disadvantage": True,
    }
    assert result["scene"]["source_scene_id"] == source_scene_id
    assert result["knowledge_actor_ids"] == ["actor-1", "actor-2"]
    assert result["sync"]["campaign_revision"] == 7
    assert _check_knowledge_key("run-1", "trail-survival-1") != _check_knowledge_key(
        "run-1", "trail-survival-2"
    )


def test_check_identity_uses_explicit_occurrence_not_mutable_check_content() -> None:
    assert _check_identity("armory-lock-1") == "armory-lock-1"
    assert _check_identity("armory-lock-1") != _check_identity("armory-lock-2")


def test_check_agent_ruling_binds_the_selected_dc() -> None:
    ruling = {
        "default_resolver": "agent",
        "ruling_kind": "agent_dm_adjudication",
        "decision": "Use DC 15 for taking the sword without waking its owner.",
        "reason": "The module establishes the sleeping occupants but prints no DC.",
        "dc": 15,
    }

    assert regression_playthrough._settled_check_agent_ruling(ruling, dc=15) == {
        **ruling,
        "committed": True,
    }
    with pytest.raises(ValueError, match="exactly match"):
        regression_playthrough._settled_check_agent_ruling(ruling, dc=14)


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


def test_named_skill_check_rejects_client_proficiency_override() -> None:
    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query" and arguments["view"] == "scene":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "Make a DC 20 Wisdom (Perception) check.",
                    "locations": [{"key": "village"}],
                }
            if tool_id == "character_query":
                return {
                    "id": "actor-1",
                    "name": "Watcher",
                    "campaign_id": "campaign-1",
                    "sheet": {
                        "skills": {
                            "perception": {
                                "proficiency": "expertise",
                                "bonus": 0,
                            }
                        }
                    },
                }
            raise AssertionError((tool_id, arguments))

    with pytest.raises(ValueError, match="derive proficiency, expertise"):
        asyncio.run(
            _resolve_check(
                Client(),
                campaign_id="campaign-1",
                run_id="run-1",
                scene_id="scene-1",
                location_key="village",
                source_excerpt="Make a DC 20 Wisdom (Perception) check.",
                source_ref={
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "chunk_id": "chunk-1",
                    "page_start": 1,
                    "page_end": 1,
                    "heading_path": ["Village"],
                    "content_sha256": "abc",
                },
                occurrence_id="watch-village",
                actor_id="actor-1",
                kind="ability",
                ability="perception",
                dc=20,
                proficient=True,
                knowledge_actor_ids=[],
                success_knowledge="",
                failure_knowledge="",
            )
        )


def test_character_check_accepts_full_and_compact_exposure_shapes() -> None:
    result = {"success": False, "total": 7, "natural": 4}
    group = {
        "kind": "ability_group_check",
        "success": True,
        "success_count": 5,
        "required_successes": 3,
    }

    assert _committed_check_result({"status": "committed", "result": result}) == result
    assert _committed_check_result(result) == result
    assert _committed_check_result(group) == group
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
                "bonus": 0,
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
        bonus=0,
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
        bonus=0,
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
        bonus=0,
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
        bonus=0,
        advantage=False,
        disadvantage=False,
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
        bonus=2,
        advantage=False,
        disadvantage=True,
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
@pytest.mark.parametrize(
    ("damage_expression", "expects_random_roll"),
    [("1d6", True), ("4", False)],
)
def test_source_damage_rolls_then_damages_and_knocks_prone_through_public_tools(
    damage_expression: str,
    expects_random_roll: bool,
    half_damage: bool,
    expected_amount: int,
    force_zero_hp: bool,
    defer_checkpoint: bool,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "source-scene-1",
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
                scene_id = arguments["payload"]["scene_id"]
                if scene_id == "scene-1":
                    return {
                        "module_id": "module-1",
                        "scene_id": "scene-1",
                        "content": "The party is crossing into the next scene.",
                        "locations": [{"key": "3-kennel"}],
                    }
                assert scene_id == "source-scene-1"
                return {
                    "module_id": "module-1",
                    "scene_id": "source-scene-1",
                    "content": "On a result of 5 or less, the character falls.",
                    "locations": [{"key": "1-entrance"}],
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
                assert expects_random_roll
                assert arguments["expression"] == damage_expression
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
                assert event["payload"]["scene_id"] == "scene-1"
                assert event["payload"]["source_scene_id"] == "source-scene-1"
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
            source_scene_id="source-scene-1",
            location_key="3-kennel",
            source_excerpt="On a result of 5 or less, the character falls.",
            source_ref=source_ref,
            actor_id="actor-1",
            damage_event_id="chimney-fall-1",
            expression=damage_expression,
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
    assert result["scene"]["scene_id"] == "scene-1"
    assert result["scene"]["source_scene_id"] == "source-scene-1"
    assert ("dnd_dice_roll" in client.calls) is expects_random_roll
    assert (
        _mutation_key("run-1", "source-damage-roll", "chimney-fall-1") in client.keys
    ) is expects_random_roll
    if not expects_random_roll:
        assert result["roll"] == {
            "status": "fixed",
            "result": {
                "expression": "4",
                "total": 4,
                "rolls": [],
                "random_draws": 0,
                "resolution": "fixed",
            },
        }
    assert _mutation_key("run-1", "source-damage", "chimney-fall-1") in client.keys
    assert _mutation_key("run-1", "source-damage-continuity", "chimney-fall-1") in client.keys


def test_knowledge_recipient_preflight_covers_every_mutating_driver_action() -> None:
    standard = argparse.Namespace(
        action="apply-damage",
        knowledge_actor_id=["actor-1"],
        event_knowledge_actor_id=["event-actor"],
    )
    event = argparse.Namespace(
        action="record-outcome",
        knowledge_actor_id=["actor-1"],
        event_knowledge_actor_id=["event-actor"],
    )
    unrelated = argparse.Namespace(
        action="status",
        knowledge_actor_id=["actor-1"],
        event_knowledge_actor_id=["event-actor"],
    )

    assert regression_playthrough._knowledge_preflight_actor_ids(standard) == ["actor-1"]
    assert regression_playthrough._knowledge_preflight_actor_ids(event) == ["event-actor"]
    assert regression_playthrough._knowledge_preflight_actor_ids(unrelated) == []
    assert {
        "register-replacement",
        "resolve-check",
        "resolve-group-check",
        "resolve-contest",
        "apply-damage",
        "initialize-source-state",
        "stand-up",
        "use-activity",
        "cast-spell",
        "cast-source-spell",
        "cast-healing-spell",
        "advance-time",
        "recover-stable",
        "acquire-loot",
        "spend-coins",
        "spend-item",
        "use-consumable",
    } == regression_playthrough.KNOWLEDGE_ACTOR_PREFLIGHT_ACTIONS
    assert {
        "record-event",
        "record-outcome",
    } == regression_playthrough.EVENT_KNOWLEDGE_ACTOR_PREFLIGHT_ACTIONS


def test_knowledge_recipient_preflight_rejects_cross_campaign_actor_before_mutation() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append(tool_id)
            assert tool_id == "character_query"
            return {
                "id": arguments["payload"]["character_id"],
                "campaign_id": "different-campaign",
            }

    client = Client()
    with pytest.raises(ValueError, match="does not belong to the campaign"):
        asyncio.run(
            regression_playthrough._validate_campaign_actor_ids(
                client,
                campaign_id="campaign-1",
                actor_ids=["actor-1"],
                operation="apply-damage knowledge recipient",
            )
        )
    assert client.calls == ["character_query"]


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


def test_source_backed_revival_uses_public_character_facade() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 57,
        "page_end": 57,
        "heading_path": ["Second Attack", "Conclusion"],
        "content_sha256": "abc",
    }

    class Client:
        def __init__(self) -> None:
            self.revision = 30
            self.keys: dict[str, str] = {}

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.revision,
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": (
                        "Even if the party is defeated, the characters wake after the "
                        "battle to discover that they are being tended to in a well-guarded "
                        "location and have been restored to life by allies."
                    ),
                    "locations": [{"key": "second-attack"}],
                }
            if tool_id == "character_query":
                actor_id = arguments["payload"]["character_id"]
                return {
                    "id": actor_id,
                    "name": "Brynja",
                    "campaign_id": "campaign-1",
                    "revision": 7,
                }
            if tool_id == "character_state_change":
                assert arguments["action"] == "revive"
                assert arguments["payload"] == {
                    "elapsed_days": 0,
                    "soul_willing": True,
                    "body_intact": True,
                    "source_ref": "module-chunk:chunk-1",
                    "reason": "Allied factions restore Brynja after the cult attack.",
                }
                self.keys["revive"] = arguments["idempotency_key"]
                self.revision += 1
                return {"result": {"status": "revived", "hit_points": 1}}
            if tool_id == "playthrough_manifest":
                self.keys["sync"] = arguments["idempotency_key"]
                return {
                    "manifest": {"status": "in_progress"},
                    "campaign_revision": self.revision,
                }
            raise AssertionError((tool_id, arguments))

    client = Client()
    excerpt = (
        "Even if the party is defeated, the characters wake after the battle to "
        "discover that they are being tended to in a well-guarded location and have "
        "been restored to life by allies."
    )
    result = asyncio.run(
        _revive_character(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            scene_id="scene-1",
            source_scene_id="",
            location_key="second-attack",
            source_excerpt=excerpt,
            source_ref=source_ref,
            occurrence_id="second-attack-revive-brynja",
            actor_id="brynja",
            source_actor_id="",
            elapsed_days=0,
            soul_willing=True,
            body_intact=True,
            reason="Allied factions restore Brynja after the cult attack.",
        )
    )

    assert result["revival"]["result"]["status"] == "revived"
    assert client.keys == {
        "revive": _mutation_key(
            "run-1",
            "revive-character",
            "second-attack-revive-brynja",
        ),
        "sync": _mutation_key(
            "run-1",
            "sync",
            "revive-character-sync:second-attack-revive-brynja",
        ),
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
            if tool_id == "campaign_change" and arguments["action"] == "party_rest":
                self.remember("party_rest", arguments["idempotency_key"])
                assert arguments["payload"] == {
                    "rest_type": "short_rest",
                    "duration_minutes": 60,
                    "members": [
                        {
                            "character_id": "fighter",
                            "expected_revision": 2,
                            "hit_dice_spends": [{"key": "fighter:d10", "count": 1}],
                            "song_of_rest_source_actor_id": "wizard",
                            "rest_activity_minutes": {"meditation": 30},
                        },
                        {
                            "character_id": "wizard",
                            "expected_revision": 2,
                            "arcane_recovery": {"1": 1},
                        },
                    ],
                }
                self.world_time = {
                    **self.world_time,
                    "hour": 15,
                    "elapsed_minutes": 900,
                }
                self.revision += 1
                return {
                    "status": "committed",
                    "rest_type": "short_rest",
                    "duration_minutes": 60,
                    "member_ids": ["fighter", "wizard"],
                    "world_time": self.world_time,
                    "recovered": {
                        "fighter": {"hit_dice_healing": 7},
                        "wizard": {"recovered": {"spell_slot:1": 1}},
                    },
                    "campaign_revision": self.revision,
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
    assert result["rest_recovered"] is False
    identity = "hideout-short-rest-1"
    assert client.keys["clock_set"] == [_mutation_key("run-1", "short-rest-clock-set", identity)]
    assert client.keys["party_rest"] == [_mutation_key("run-1", "short-rest-party", identity)]
    assert client.keys["continuity"] == [_mutation_key("run-1", "short-rest-continuity", identity)]
    assert client.keys["sync"] == [_mutation_key("run-1", "sync", f"short-rest-sync:{identity}")]


def test_short_rest_recovers_the_atomic_random_receipt_without_rerolling() -> None:
    rest_key = _mutation_key("run-1", "short-rest-party", "recovered-short-rest-1")
    response = {
        "status": "committed",
        "rest_type": "short_rest",
        "duration_minutes": 60,
        "member_ids": ["fighter"],
        "game_time": {
            "schema_version": 1,
            "tick_seconds": 6,
            "elapsed_ticks": 600,
        },
        "world_time": {
            "schema_version": 1,
            "day": 1,
            "hour": 1,
            "minute": 0,
            "elapsed_minutes": 60,
            "label": "Camp",
        },
        "recovered": {
            "fighter": {
                "hit_dice_rolls": [
                    {
                        "key": "fighter:d10",
                        "roll": 7,
                    }
                ]
            }
        },
        "preparations": {},
        "campaign_revision": 6,
        "random_stream_receipt": {
            "algorithm": "sha256-counter-v1",
            "position_before": 20,
            "position_after": 21,
            "draw_count": 1,
            "operation": "campaign_change",
            "idempotency_key": rest_key,
        },
    }

    class Client:
        def __init__(self) -> None:
            self.revision = 6
            self.party_rest_calls = 0

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.revision,
                    "state": {
                        "game_phase": "play",
                        "game_time": response["game_time"],
                        "world_time": response["world_time"],
                    },
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "character_query":
                if arguments["view"] == "rest":
                    return {"ready": True}
                sheet = default_character_sheet()
                sheet["combat"]["rest_history"] = {
                    "last_rest_type": "short_rest",
                    "last_rest_started_elapsed_ticks": 0,
                    "last_rest_completed_elapsed_ticks": 600,
                    "last_long_rest_elapsed_ticks": None,
                }
                return {
                    "id": "fighter",
                    "campaign_id": "campaign-1",
                    "revision": 3,
                    "sheet": sheet,
                }
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "campaign_change":
                self.party_rest_calls += 1
                raise RuntimeError(f"idempotency key reused with a different request: {rest_key}")
            if tool_id == "state_revision":
                normalized_request = {
                    "members": [
                        {
                            "character_id": "fighter",
                            "expected_revision": 2,
                            "rest_activity_minutes": {},
                            "hit_dice_spends": [{"key": "fighter:d10", "count": 1}],
                            "arcane_recovery": {},
                            "natural_recovery": {},
                            "song_of_rest_source_actor_id": None,
                            "attune_item_id": None,
                            "attunement_prerequisite_confirmed": None,
                        }
                    ],
                    "duration_minutes": 60,
                    "branch_id": "branch-1",
                    "rest_type": "short_rest",
                }
                return {
                    "key": rest_key,
                    "replayed": True,
                    "request_hash": regression_playthrough._idempotency_request_hash(
                        normalized_request
                    ),
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
                    ],
                    "response": response,
                }
            if tool_id == "memory_change":
                self.revision += 1
                return {"event": {"id": "event-1"}, "snapshot": {"slot": 4}}
            if tool_id == "playthrough_manifest":
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
            occurrence_id="recovered-short-rest-1",
            members=[
                {
                    "actor_id": "fighter",
                    "hit_dice_spends": [{"key": "fighter:d10", "count": 1}],
                }
            ],
            start_clock=None,
            duration_minutes=60,
            reason="The party completed the recorded short rest.",
        )
    )

    assert client.party_rest_calls == 1
    assert result["rest_recovered"] is True
    assert result["party_rest"]["random_stream_receipt"]["position_after"] == 21


@pytest.mark.parametrize("defer_checkpoint", [False, True])
@pytest.mark.parametrize("evidence_mode", ["source", "agent", "source_and_agent"])
def test_time_advance_commits_evidence_clock_knowledge_and_snapshot(
    defer_checkpoint: bool,
    evidence_mode: str,
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
    agent_ruling = {
        "default_resolver": "agent",
        "ruling_kind": "agent_dm_adjudication",
        "decision": "Advance the campaign clock by 13 hours.",
        "reason": "The scene fixes arrival late in the day but not an exact hour.",
        "period": "hour",
        "count": 13,
    }

    class Client:
        def __init__(self) -> None:
            self.revision = 4
            self.game_time = {
                "schema_version": 1,
                "tick_seconds": 6,
                "elapsed_ticks": 16800,
            }
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
                        "game_time": deepcopy(self.game_time),
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
                assert arguments["payload"] == {
                    "period": "hour",
                    "count": 13,
                    "expected_elapsed_ticks": 24600,
                }
                self.world_time = {
                    "day": 2,
                    "hour": 17,
                    "minute": 0,
                    "elapsed_minutes": 2460,
                    "label": "Trail",
                }
                self.game_time = {
                    **self.game_time,
                    "elapsed_ticks": 24600,
                }
                self.revision += 1
                return {
                    "game_time": deepcopy(self.game_time),
                    "world_time": deepcopy(self.world_time),
                    "campaign_revision": self.revision,
                }
            if tool_id == "memory_change":
                assert arguments["expected_revision"] == self.revision
                payload = arguments["payload"]
                event_payload = payload["event"]["payload"]
                if evidence_mode == "agent":
                    assert event_payload["source_ref"] is None
                    assert event_payload["source_excerpt"] == ""
                else:
                    assert event_payload["source_ref"] == source_ref
                if evidence_mode == "source":
                    assert event_payload["agent_ruling"] is None
                else:
                    assert event_payload["agent_ruling"] == {
                        **agent_ruling,
                        "committed": True,
                    }
                assert event_payload["elapsed_minutes"] == 780
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
            source_excerpt=(
                "" if evidence_mode == "agent" else "The characters arrive late in the day."
            ),
            source_ref=None if evidence_mode == "agent" else source_ref,
            period="hour",
            count=13,
            reason="The party traveled with Sildar and arrived late in the day.",
            start_clock=None,
            agent_ruling=None if evidence_mode == "source" else agent_ruling,
            knowledge_actor_ids=["actor-1", "npc-1"],
            defer_checkpoint=defer_checkpoint,
            expected_after_ticks=24600,
            expected_after={
                "day": 2,
                "hour": 17,
                "minute": 0,
                "elapsed_minutes": 2460,
            },
        )
    )

    assert result["after"]["hour"] == 17
    assert result["knowledge_actor_ids"] == ["actor-1", "npc-1"]
    if evidence_mode == "source":
        assert result["agent_ruling"] is None
    else:
        assert result["agent_ruling"]["committed"] is True
    if defer_checkpoint:
        assert "snapshot" not in result["continuity"]
    else:
        assert result["continuity"]["snapshot"]["slot"] == 5


def test_time_advance_recovers_clock_response_without_advancing_twice() -> None:
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
        revision = 10
        clock_calls = 0
        continuity_calls = 0

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.revision,
                    "state": {
                        "game_phase": "play",
                        "game_time": {
                            "schema_version": 1,
                            "tick_seconds": 6,
                            "elapsed_ticks": 24600,
                        },
                        # The first attempt committed this exact clock target,
                        # then lost its response before continuity was written.
                        "world_time": {
                            "day": 2,
                            "hour": 17,
                            "minute": 0,
                            "elapsed_minutes": 2460,
                            "label": "Trail",
                        },
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
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "campaign_change":
                self.clock_calls += 1
                assert arguments["action"] == "clock_advance"
                assert arguments["payload"] == {
                    "period": "hour",
                    "count": 13,
                    "expected_elapsed_ticks": 24600,
                }
                # Public idempotency replays the original response. It does not
                # execute a second 13-hour advance.
                return {
                    "game_time": {
                        "schema_version": 1,
                        "tick_seconds": 6,
                        "elapsed_ticks": 24600,
                    },
                    "world_time": {
                        "day": 2,
                        "hour": 17,
                        "minute": 0,
                        "elapsed_minutes": 2460,
                        "label": "Trail",
                    },
                    "campaign_revision": 10,
                }
            if tool_id == "memory_change":
                self.continuity_calls += 1
                assert arguments["expected_revision"] == 10
                payload = arguments["payload"]["event"]["payload"]
                assert payload["world_time_before"]["elapsed_minutes"] == 1680
                assert payload["world_time_after"]["elapsed_minutes"] == 2460
                self.revision += 1
                return {"event": {"id": "event-1"}, "snapshot": None}
            if tool_id == "playthrough_manifest":
                return {
                    "manifest": {"status": "in_progress"},
                    "campaign_revision": self.revision,
                }
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _advance_time(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="travel-to-phandalin-1",
            scene_id="scene-1",
            source_excerpt="The characters arrive late in the day.",
            source_ref=source_ref,
            period="hour",
            count=13,
            reason="The party traveled and arrived late in the day.",
            start_clock=None,
            agent_ruling=None,
            knowledge_actor_ids=[],
            defer_checkpoint=True,
            expected_after_ticks=24600,
            expected_after={
                "day": 2,
                "hour": 17,
                "minute": 0,
                "elapsed_minutes": 2460,
            },
        )
    )

    assert result["clock_recovery"] is True
    assert result["before"]["elapsed_minutes"] == 1680
    assert result["after"]["elapsed_minutes"] == 2460
    assert client.clock_calls == 1
    assert client.continuity_calls == 1


def test_time_advance_recovery_binds_continuity_to_original_clock_revision() -> None:
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
        revision = 11

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.revision,
                    "state": {
                        "game_phase": "play",
                        "game_time": {
                            "schema_version": 1,
                            "tick_seconds": 6,
                            "elapsed_ticks": 24600,
                        },
                        "world_time": {
                            "day": 2,
                            "hour": 17,
                            "minute": 0,
                            "elapsed_minutes": 2460,
                            "label": "Trail",
                        },
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
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "campaign_change":
                return {
                    "game_time": {
                        "schema_version": 1,
                        "tick_seconds": 6,
                        "elapsed_ticks": 24600,
                    },
                    "world_time": {
                        "day": 2,
                        "hour": 17,
                        "minute": 0,
                        "elapsed_minutes": 2460,
                        "label": "Trail",
                    },
                    "campaign_revision": 10,
                }
            if tool_id == "memory_change":
                assert arguments["expected_revision"] == 10
                raise ValueError("campaign revision conflict: expected 10, found 11")
            raise AssertionError((tool_id, arguments))

    with pytest.raises(ValueError, match="expected 10, found 11"):
        asyncio.run(
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
                reason="The party traveled and arrived late in the day.",
                start_clock=None,
                agent_ruling=None,
                knowledge_actor_ids=[],
                defer_checkpoint=True,
                expected_after_ticks=24600,
                expected_after={
                    "day": 2,
                    "hour": 17,
                    "minute": 0,
                    "elapsed_minutes": 2460,
                },
            )
        )


def test_time_advance_recovers_a_committed_clock_without_a_rich_response() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 14,
        "page_end": 14,
        "heading_path": ["Part 2"],
        "content_sha256": "abc",
    }
    branch_id = "branch-1"
    clock_key = _mutation_key("run-1", "advance-time-clock", "travel-to-phandalin-1")
    expected_clock_request = {
        "period": "hour",
        "count": 13,
        "branch_id": branch_id,
        "expected_elapsed_ticks": 24600,
    }

    class Client:
        revision = 10

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.revision,
                    "state": {
                        "game_phase": "play",
                        "game_time": {
                            "schema_version": 1,
                            "tick_seconds": 6,
                            "elapsed_ticks": 24600,
                        },
                        "world_time": {
                            "day": 2,
                            "hour": 17,
                            "minute": 0,
                            "elapsed_minutes": 2460,
                            "label": "Trail",
                        },
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
            if tool_id == "branch_query":
                return [{"id": branch_id, "is_current": True}]
            if tool_id == "campaign_change":
                return {
                    "status": "committed",
                    "idempotency_replayed": True,
                    "response_recovery": "read_current_state",
                }
            if tool_id == "state_revision":
                assert arguments["payload"] == {
                    "idempotency_key": clock_key,
                    "branch_id": branch_id,
                }
                return {
                    "key": clock_key,
                    "replayed": True,
                    "response": {
                        "status": "committed",
                        "idempotency_replayed": True,
                        "response_recovery": "read_current_state",
                    },
                    "mutation_group_id": "group-1",
                    "request_hash": regression_playthrough._idempotency_request_hash(
                        expected_clock_request
                    ),
                    "branch_id": branch_id,
                    "entity_revisions": [
                        {
                            "entity_type": "campaign",
                            "entity_id": "campaign-1",
                            "before_revision": 9,
                            "after_revision": 10,
                        }
                    ],
                }
            if tool_id == "memory_change":
                assert arguments["expected_revision"] == 10
                self.revision += 1
                return {"event": {"id": "event-1"}}
            if tool_id == "playthrough_manifest":
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
            reason="The party traveled and arrived late in the day.",
            start_clock=None,
            agent_ruling=None,
            knowledge_actor_ids=[],
            defer_checkpoint=True,
            expected_after_ticks=24600,
            expected_after={
                "day": 2,
                "hour": 17,
                "minute": 0,
                "elapsed_minutes": 2460,
            },
        )
    )

    assert result["clock_recovery"] is True
    assert result["clock_receipt_recovered"] is True
    assert result["advance"]["campaign_revision"] == 10
    assert result["after"]["elapsed_minutes"] == 2460


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ([], "JSON object"),
        (
            {
                "default_resolver": "external_input",
                "ruling_kind": "agent_dm_adjudication",
                "decision": "Advance one day.",
                "reason": "Travel estimate.",
                "period": "day",
                "count": 1,
            },
            "default_resolver",
        ),
        (
            {
                "default_resolver": "agent",
                "ruling_kind": "module_specific_procedure",
                "decision": "Advance one day.",
                "reason": "Travel estimate.",
                "period": "day",
                "count": 1,
            },
            "ruling_kind",
        ),
        (
            {
                "default_resolver": "agent",
                "ruling_kind": "agent_dm_adjudication",
                "decision": "",
                "reason": "Travel estimate.",
                "period": "day",
                "count": 1,
            },
            "decision",
        ),
        (
            {
                "default_resolver": "agent",
                "ruling_kind": "agent_dm_adjudication",
                "decision": "Advance one day.",
                "reason": "",
                "period": "day",
                "count": 1,
            },
            "reason",
        ),
        (
            {
                "default_resolver": "agent",
                "ruling_kind": "agent_dm_adjudication",
                "decision": "Advance one day.",
                "reason": "Travel estimate.",
                "period": "hour",
                "count": 1,
            },
            "exactly match",
        ),
        (
            {
                "default_resolver": "agent",
                "ruling_kind": "agent_dm_adjudication",
                "decision": "Advance one day.",
                "reason": "Travel estimate.",
                "period": "day",
                "count": 1,
                "payload": {},
            },
            "unsupported fields",
        ),
    ],
)
def test_time_agent_ruling_is_strictly_bounded(value: object, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        regression_playthrough._settled_time_agent_ruling(
            value,
            period="day",
            count=1,
        )


@pytest.mark.parametrize(
    ("source_excerpt", "source_ref", "match"),
    [
        ("", None, "exact source evidence or a settled Agent"),
        ("Printed duration.", None, "both exact source ref and excerpt"),
        ("", {"scene_id": "scene-1"}, "both exact source ref and excerpt"),
    ],
)
def test_time_advance_rejects_missing_or_partial_evidence_before_public_calls(
    source_excerpt: str,
    source_ref: dict | None,
    match: str,
) -> None:
    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            raise AssertionError((tool_id, arguments))

    with pytest.raises(ValueError, match=match):
        asyncio.run(
            _advance_time(
                Client(),
                campaign_id="campaign-1",
                run_id="run-1",
                occurrence_id="travel-1",
                scene_id="scene-1",
                source_excerpt=source_excerpt,
                source_ref=source_ref,
                period="day",
                count=1,
                reason="The party travels.",
                start_clock=None,
                agent_ruling=None,
                knowledge_actor_ids=[],
                expected_after={
                    "day": 2,
                    "hour": 0,
                    "minute": 0,
                    "elapsed_minutes": 1440,
                },
            )
        )


def test_time_advance_requires_an_exact_target_before_public_calls() -> None:
    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            raise AssertionError((tool_id, arguments))

    with pytest.raises(ValueError, match="requires --time-expected-after-ticks"):
        asyncio.run(
            _advance_time(
                Client(),
                campaign_id="campaign-1",
                run_id="run-1",
                occurrence_id="travel-1",
                scene_id="scene-1",
                source_excerpt="One day passes.",
                source_ref={"scene_id": "scene-1"},
                period="day",
                count=1,
                reason="The party travels.",
                start_clock=None,
                agent_ruling=None,
                knowledge_actor_ids=[],
            )
        )


def test_time_advance_uses_canonical_ticks_without_a_calendar() -> None:
    class Client:
        revision = 1

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.revision,
                    "state": {
                        "game_phase": "play",
                        "game_time": {
                            "schema_version": 1,
                            "tick_seconds": 6,
                            "elapsed_ticks": 0,
                        },
                    },
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {"module_id": "module-1", "scene_id": "scene-1"}
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "campaign_change":
                assert arguments["action"] == "clock_advance"
                assert arguments["payload"] == {
                    "period": "hour",
                    "count": 1,
                    "expected_elapsed_ticks": 600,
                }
                self.revision = 2
                return {
                    "status": "committed",
                    "game_time": {
                        "schema_version": 1,
                        "tick_seconds": 6,
                        "elapsed_ticks": 600,
                    },
                    "world_time": None,
                    "campaign_revision": 2,
                }
            if tool_id == "memory_change":
                assert arguments["expected_revision"] == 2
                event_payload = arguments["payload"]["event"]["payload"]
                assert event_payload["expected_elapsed_ticks"] == 600
                assert event_payload["world_time_before"] == {}
                assert event_payload["world_time_after"] == {}
                self.revision = 3
                return {"event": {"id": "event-1"}}
            if tool_id == "playthrough_manifest":
                self.revision = 4
                return {
                    "manifest": {"status": "in_progress"},
                    "campaign_revision": 4,
                }
            raise AssertionError((tool_id, arguments))

    result = asyncio.run(
        _advance_time(
            Client(),
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="wait-one-hour",
            scene_id="scene-1",
            source_excerpt="",
            source_ref=None,
            period="hour",
            count=1,
            reason="The party waits for one hour.",
            start_clock=None,
            agent_ruling={
                "default_resolver": "agent",
                "ruling_kind": "agent_dm_adjudication",
                "decision": "Advance the campaign clock by one hour.",
                "reason": "The declared wait lasts exactly one hour.",
                "period": "hour",
                "count": 1,
            },
            knowledge_actor_ids=[],
            expected_after_ticks=600,
        )
    )

    assert result["before"] == {}
    assert result["after"] == {}
    assert result["expected_after_ticks"] == 600
    assert result["advance"]["game_time"]["elapsed_ticks"] == 600


def test_narrative_preconditions_require_a_complete_outcome_reference_before_calls() -> None:
    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            raise AssertionError((tool_id, arguments))

    with pytest.raises(ValueError, match="both scene id and outcome id"):
        asyncio.run(
            regression_playthrough._validate_narrative_preconditions(
                Client(),
                campaign_id="campaign-1",
                scene_id="scene-1",
            )
        )


def test_narrative_preconditions_return_public_outcome_and_actor_evidence() -> None:
    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                assert arguments == {
                    "campaign_id": "campaign-1",
                    "view": "progress",
                }
                return [
                    {
                        "scene_id": "scene-1",
                        "state_version": 7,
                        "state": {
                            "full_playthrough_outcomes": {
                                "event-1-resolved": {
                                    "event_type": "road_event_resolved",
                                    "fact_keys": ["world.event.1"],
                                }
                            }
                        },
                    }
                ]
            if tool_id == "character_query":
                assert arguments == {
                    "view": "get",
                    "payload": {"character_id": "npc-1"},
                }
                return {
                    "id": "npc-1",
                    "campaign_id": "campaign-1",
                    "revision": 3,
                    "character_type": "npc",
                }
            raise AssertionError((tool_id, arguments))

    result = asyncio.run(
        regression_playthrough._validate_narrative_preconditions(
            Client(),
            campaign_id="campaign-1",
            scene_id="scene-1",
            outcome_id="event-1-resolved",
            actor_ids=["npc-1"],
        )
    )

    assert result == {
        "outcome": {
            "scene_id": "scene-1",
            "outcome_id": "event-1-resolved",
            "state_version": 7,
            "event_type": "road_event_resolved",
            "fact_keys": ["world.event.1"],
        },
        "actors": [
            {
                "actor_id": "npc-1",
                "revision": 3,
                "character_type": "npc",
            }
        ],
    }


def test_long_rest_rejects_missing_narrative_outcome_before_any_mutation() -> None:
    class Client:
        calls: list[tuple[str, dict]] = []

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append((tool_id, arguments))
            if tool_id == "character_query":
                return {
                    "id": "actor-1",
                    "campaign_id": "campaign-1",
                    "revision": 2,
                    "sheet": default_character_sheet(),
                }
            if tool_id == "module_query":
                return [
                    {
                        "scene_id": "scene-1",
                        "state_version": 6,
                        "state": {"full_playthrough_outcomes": {}},
                    }
                ]
            raise AssertionError((tool_id, arguments))

    client = Client()
    with pytest.raises(ValueError, match="required playthrough outcome is not recorded"):
        asyncio.run(
            _long_rest(
                client,
                campaign_id="campaign-1",
                run_id="run-1",
                occurrence_id="event-1-rest",
                members=[{"actor_id": "actor-1", "food_and_drink": True}],
                start_clock=None,
                duration_minutes=480,
                reason="Rest after resolving event 1.",
                prerequisite_scene_id="scene-1",
                prerequisite_outcome_id="event-1-resolved",
            )
        )
    assert [tool_id for tool_id, _arguments in client.calls] == [
        "character_query",
        "module_query",
    ]


def test_long_rest_rejects_wrong_start_clock_before_campaign_mutation() -> None:
    class Client:
        mutated = False

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": 4,
                    "state": {
                        "game_phase": "play",
                        "game_time": {
                            "schema_version": 1,
                            "tick_seconds": 6,
                            "elapsed_ticks": 14400,
                        },
                        "world_time": {
                            "day": 2,
                            "hour": 0,
                            "minute": 0,
                            "elapsed_minutes": 1440,
                        },
                    },
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "character_query":
                return {
                    "id": "actor-1",
                    "campaign_id": "campaign-1",
                    "revision": 2,
                    "sheet": default_character_sheet(),
                }
            if tool_id == "module_query":
                return [
                    {
                        "scene_id": "scene-1",
                        "state_version": 7,
                        "state": {
                            "full_playthrough_outcomes": {
                                "event-1-resolved": {
                                    "event_type": "road_event_resolved",
                                    "fact_keys": ["world.event.1"],
                                }
                            }
                        },
                    }
                ]
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "campaign_change":
                self.mutated = True
                raise AssertionError("wrong-clock rest must not mutate campaign state")
            raise AssertionError((tool_id, arguments))

    client = Client()
    with pytest.raises(ValueError, match="does not match the required precondition"):
        asyncio.run(
            _long_rest(
                client,
                campaign_id="campaign-1",
                run_id="run-1",
                occurrence_id="event-1-rest",
                members=[{"actor_id": "actor-1", "food_and_drink": True}],
                start_clock=None,
                duration_minutes=480,
                reason="Rest after resolving event 1.",
                prerequisite_scene_id="scene-1",
                prerequisite_outcome_id="event-1-resolved",
                expected_start_clock={
                    "day": 1,
                    "hour": 16,
                    "minute": 0,
                    "elapsed_minutes": 960,
                },
            )
        )
    assert client.mutated is False


def test_rest_world_time_precondition_ignores_labels_but_rejects_wrong_time() -> None:
    campaign = {
        "state": {
            "world_time": {
                "schema_version": 1,
                "day": 3,
                "hour": 17,
                "minute": 0,
                "elapsed_minutes": 3900,
                "label": "Campaign anchor",
            }
        }
    }
    expected = {
        "day": 3,
        "hour": 17,
        "minute": 0,
        "elapsed_minutes": 3900,
    }
    assert regression_playthrough._validate_world_time_precondition(campaign, expected) == expected
    with pytest.raises(ValueError, match="does not match the required precondition"):
        regression_playthrough._validate_world_time_precondition(
            campaign,
            {
                "day": 3,
                "hour": 18,
                "minute": 0,
                "elapsed_minutes": 3960,
            },
        )


def test_time_advance_rejects_wrong_road_calendar_delta_before_clock_write() -> None:
    class Client:
        clock_changes = 0

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": 10,
                    "state": {
                        "game_phase": "play",
                        "game_time": {
                            "schema_version": 1,
                            "tick_seconds": 6,
                            "elapsed_ticks": 634230,
                        },
                        "world_time": {
                            "day": 45,
                            "hour": 1,
                            "minute": 3,
                            "elapsed_minutes": 63423,
                        },
                    },
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_query":
                return {"scene_id": "scene-1", "content": "Routine road journey."}
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "campaign_change":
                self.clock_changes += 1
                raise AssertionError("clock mutation must not be attempted")
            raise AssertionError((tool_id, arguments))

    client = Client()
    with pytest.raises(ValueError, match="does not reach expected tick target"):
        asyncio.run(
            _advance_time(
                client,
                campaign_id="campaign-1",
                run_id="run-1",
                occurrence_id="road-to-travel-day-25",
                scene_id="scene-1",
                source_excerpt="",
                source_ref=None,
                period="minute",
                count=13197,
                reason="Continue to travel day 25 at 7 a.m.",
                start_clock=None,
                agent_ruling={
                    "default_resolver": "agent",
                    "ruling_kind": "agent_dm_adjudication",
                    "decision": "Reach travel day 25 at 7 a.m.",
                    "reason": "The locked road calendar fixes the event time.",
                    "period": "minute",
                    "count": 13197,
                },
                knowledge_actor_ids=[],
                expected_after_ticks=781800,
                expected_after={
                    "day": 55,
                    "hour": 7,
                    "minute": 0,
                    "elapsed_minutes": 78180,
                },
            )
        )
    assert client.clock_changes == 0
    assert regression_playthrough._project_world_time(
        {"elapsed_minutes": 63423},
        14757,
    ) == {
        "day": 55,
        "hour": 7,
        "minute": 0,
        "elapsed_minutes": 78180,
    }


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
                    "locations": ([{"key": "safe-room"}] if scene_id == "occurrence-scene" else []),
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

    assert raised.value.requirement["operation"] == ("character_action.cast_source_spell")
    assert raised.value.requirement["ruling"]["ruling_kind"] == ("module_specific_procedure")


@pytest.mark.parametrize("evidence_mode", ["source", "agent", "source_and_agent"])
def test_dm_event_preserves_evidence_and_keeps_enemy_knowledge_private(
    evidence_mode: str,
) -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 12,
        "page_end": 12,
        "heading_path": ["Developments"],
        "content_sha256": "abc",
    }
    agent_ruling = {
        "default_resolver": "agent",
        "ruling_kind": "module_specific_procedure",
        "decision": "The messenger reaches the leader before the party.",
        "reason": "The current scene state leaves the messenger's timing to the DM.",
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
                event_payload = event["payload"]
                if evidence_mode == "agent":
                    assert event_payload["source_ref"] is None
                    assert event_payload["source_excerpt"] == ""
                else:
                    assert event_payload["source_ref"] == source_ref
                if evidence_mode == "source":
                    assert event_payload["agent_ruling"] is None
                else:
                    assert event_payload["agent_ruling"] == {
                        **agent_ruling,
                        "committed": True,
                    }
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
            source_excerpt=("" if evidence_mode == "agent" else "A messenger warned the leader."),
            source_ref=None if evidence_mode == "agent" else source_ref,
            event_type="enemy_alerted",
            summary="The leader received the warning.",
            knowledge="The party is approaching.",
            knowledge_actor_ids=["enemy"],
            progress_percent=60,
            audience_scope="dm",
            agent_ruling=None if evidence_mode == "source" else agent_ruling,
        )
    )

    assert result["knowledge_actor_ids"] == ["enemy"]
    if evidence_mode == "source":
        assert result["scene"]["agent_ruling"] is None
    else:
        assert result["scene"]["agent_ruling"]["committed"] is True


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
            self.game_time = {
                "schema_version": 1,
                "tick_seconds": 6,
                "elapsed_ticks": 14400,
            }

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.revision,
                    "state": {
                        "game_phase": "play",
                        "game_time": self.game_time,
                        "world_time": self.world_time,
                    },
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
                    "campaign_revision": self.revision,
                }
            if tool_id == "memory_change":
                assert arguments["expected_revision"] == self.revision
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


@pytest.mark.parametrize("rich_response", [True, False])
def test_long_rest_recovers_committed_receipt_without_advancing_time_twice(
    rich_response: bool,
) -> None:
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
            self.game_time = {
                "schema_version": 1,
                "tick_seconds": 6,
                "elapsed_ticks": 14400,
            }

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {
                "result": {
                    "id": "campaign-1",
                    "revision": self.revision,
                    "state": {
                        "game_phase": "play",
                        "game_time": self.game_time,
                        "world_time": self.world_time,
                    },
                }
            }

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "character_query":
                actor_id = arguments["payload"]["character_id"]
                sheet = default_character_sheet()
                sheet["combat"]["rest_history"] = {
                    "last_rest_type": "long_rest",
                    "last_rest_started_elapsed_ticks": 9600,
                    "last_rest_completed_elapsed_ticks": 14400,
                    "last_long_rest_elapsed_ticks": 14400,
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
                                "food_and_drink": True,
                            },
                            {
                                "character_id": "cleric",
                                "expected_revision": 2,
                                "prepared_spell_ids": ["cure-wounds"],
                                "hit_dice_recovery": None,
                                "rest_activity_minutes": {},
                                "food_and_drink": False,
                            },
                        ],
                        "duration_minutes": 480,
                        "branch_id": "branch-1",
                    }
                )
                receipt = {
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
                }
                if rich_response:
                    receipt["response"] = {
                        "status": "committed",
                        "rest_type": "long_rest",
                        "duration_minutes": 480,
                        "member_ids": ["fighter", "cleric"],
                        "game_time": self.game_time,
                        "world_time": self.world_time,
                        "campaign_revision": 6,
                        "preparations": {"cleric": {"selected_spell_ids": ["cure-wounds"]}},
                    }
                else:
                    receipt["response"] = {
                        "status": "committed",
                        "idempotency_replayed": True,
                        "response_recovery": "read_current_state",
                    }
                return receipt
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


def test_roll_continuity_recovers_a_response_lost_commit_receipt() -> None:
    payload = {
        "event": {
            "summary": "The save failed.",
            "event_type": "ability_check",
            "audience_scope": "party",
            "payload": {
                "occurrence_id": "save-1",
                "success": False,
            },
        },
        "actor_knowledge": [
            {
                "actor_id": "actor-1",
                "knowledge_key": "save-1",
                "proposition": "The save failed.",
                "disclosure_scope": "owner",
            }
        ],
        "branch_id": "branch-1",
    }
    response = {
        "event": {
            "id": "event-1",
            "summary": "The save failed.",
            "event_type": "ability_check",
            "audience_scope": "party",
            "payload": {
                "occurrence_id": "save-1",
                "success": False,
                "_sagasmith_skill_manifest": [{"id": "dnd.full"}],
            },
        },
        "facts": [],
        "actor_knowledge": [
            {
                "id": "knowledge-1",
                "actor_id": "actor-1",
                "knowledge_key": "save-1",
                "proposition": "The save failed.",
                "disclosure_scope": "owner",
            }
        ],
        "snapshot": None,
    }

    class Client:
        def __init__(self) -> None:
            self.loaded: list[str] = []

        async def load(self, *group_ids: str):
            self.loaded.extend(group_ids)

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "memory_change":
                raise RuntimeError(
                    "idempotency key reused with a different request: continuity-key"
                )
            if tool_id == "state_revision":
                assert arguments == {
                    "campaign_id": "campaign-1",
                    "action": "receipt",
                    "payload": {"idempotency_key": "continuity-key"},
                }
                return {
                    "replayed": True,
                    "branch_id": "branch-1",
                    "response": response,
                }
            raise AssertionError((tool_id, arguments))

    client = Client()
    recovered = asyncio.run(
        _commit_roll_continuity(
            client,
            campaign_id="campaign-1",
            payload=payload,
            expected_revision=8,
            idempotency_key="continuity-key",
        )
    )

    assert recovered == response
    assert client.loaded == []


def test_roll_continuity_rejects_a_different_receipt_event() -> None:
    with pytest.raises(RuntimeError, match="event does not match"):
        regression_playthrough._validate_recovered_continuity(
            {
                "replayed": True,
                "branch_id": "branch-1",
                "response": {
                    "event": {
                        "summary": "A different event.",
                        "event_type": "ability_check",
                        "audience_scope": "party",
                        "payload": {},
                    },
                    "actor_knowledge": [],
                    "snapshot": None,
                },
            },
            payload={
                "event": {
                    "summary": "Expected event.",
                    "event_type": "ability_check",
                    "audience_scope": "party",
                    "payload": {},
                },
                "actor_knowledge": [],
                "branch_id": "branch-1",
            },
            branch_id="branch-1",
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


def test_record_event_replays_after_later_scene_events_without_resubmitting_old_state() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": "chunk-1",
        "page_start": 7,
        "page_end": 7,
        "heading_path": ["Dungeon", "Crane"],
        "content_sha256": "abc",
    }
    occurrence_id = _occurrence_identity("route-area9", "record-event")
    event_key = regression_playthrough._token(f"run-1:{occurrence_id}", length=24)
    event_record = {
        "occurrence_id": occurrence_id,
        "event_type": "dungeon_traversal",
        "summary": "The party descended the fixed ladder.",
        "source_ref": source_ref,
    }

    class Client:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def core(self, tool_id: str, arguments: dict):
            raise AssertionError(("recovered event must not query a mutation revision", tool_id))

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append(tool_id)
            if tool_id == "module_query" and arguments["view"] == "scene":
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": "A wooden ladder is lashed to the ledge.",
                    "locations": [{"key": "9-crane"}],
                }
            if tool_id == "module_query" and arguments["view"] == "progress":
                return [
                    {
                        "scene_id": "scene-1",
                        "progress": 100,
                        "state_version": 9,
                        "state": {
                            "full_playthrough_events": {
                                event_key: deepcopy(event_record),
                                "later-event": {
                                    "occurrence_id": "later-event",
                                    "event_type": "portal_activated",
                                    "summary": "The party activated the portal.",
                                    "source_ref": source_ref,
                                },
                            }
                        },
                    }
                ]
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "campaign_event":
                assert arguments == {
                    "campaign_id": "campaign-1",
                    "action": "list",
                    "payload": {"limit": 1000, "branch_id": "branch-1"},
                }
                return [
                    {
                        "id": "event-1",
                        "event_type": "dungeon_traversal",
                        "summary": "The party descended the fixed ladder.",
                        "payload": {
                            "scene_id": "scene-1",
                            "source_scene_id": "scene-1",
                            "location_key": "9-crane",
                            "occurrence_id": occurrence_id,
                            "source_excerpt": "A wooden ladder is lashed to the ledge.",
                            "source_ref": source_ref,
                            "agent_ruling": None,
                        },
                    }
                ]
            if tool_id == "playthrough_manifest":
                assert arguments == {
                    "campaign_id": "campaign-1",
                    "action": "get",
                }
                return {
                    "manifest": {"status": "in_progress"},
                    "campaign_revision": 12,
                }
            if tool_id in {"module_set_progress", "memory_change"}:
                raise AssertionError(("recovered event must not mutate", tool_id))
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _record_event(
            client,
            campaign_id="campaign-1",
            run_id="run-1",
            occurrence_id="route-area9",
            scene_id="scene-1",
            location_key="9-crane",
            source_excerpt="A wooden ladder is lashed to the ledge.",
            source_ref=source_ref,
            event_type="dungeon_traversal",
            summary="The party descended the fixed ladder.",
            knowledge="The ladder provides a safe descent.",
            knowledge_actor_ids=["actor-1"],
            progress_percent=None,
            defer_checkpoint=True,
        )
    )

    assert result["recovered"] is True
    assert result["continuity"]["recovered"] is True
    assert result["progress"]["state_version"] == 9
    assert "module_set_progress" not in client.calls
    assert "memory_change" not in client.calls


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
    agent_ruling = {
        "default_resolver": "agent",
        "ruling_kind": "agent_dm_adjudication",
        "decision": "The captor departs after releasing the hostage.",
        "reason": "The source establishes release but leaves the captor's response to the DM.",
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
            self.manifest["world_state"] = {
                "prior_state": True,
                "episode": {
                    "prisoners": {"status": "stopped"},
                    "ritual": {"focused_rounds": 1},
                },
                "replace_list": ["old"],
            }
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
                assert outcomes["hostage-released"]["agent_ruling"] == {
                    **agent_ruling,
                    "committed": True,
                }
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
                assert self.continuity_payload["event"]["payload"]["agent_ruling"] == {
                    **agent_ruling,
                    "committed": True,
                }
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
            world_state={
                "hostage_released": True,
                "episode": {
                    "ritual": {"status": "collapsed"},
                    "mask": {"status": "removed"},
                },
                "replace_list": ["new"],
            },
            objective="Escort the hostage to safety.",
            progress_percent=100,
            source_scene_id="source-scene-1",
            defer_checkpoint=defer_checkpoint,
            agent_ruling=agent_ruling,
        )
    )

    if defer_checkpoint:
        assert result["checkpoint"] is None
    else:
        assert result["checkpoint"]["verification"]["valid"] is True

    assert result["scene"]["source_scene_id"] == "source-scene-1"
    assert result["scene"]["agent_ruling"]["committed"] is True
    assert client.continuity_payload["event"]["payload"]["source_scene_id"] == ("source-scene-1")
    assert client.loaded_groups == [()]
    assert client.replaced_manifest["current"]["objective"] == ("Escort the hostage to safety.")
    assert client.replaced_manifest["world_state"] == {
        "prior_state": True,
        "hostage_released": True,
        "episode": {
            "prisoners": {"status": "stopped"},
            "ritual": {
                "focused_rounds": 1,
                "status": "collapsed",
            },
            "mask": {"status": "removed"},
        },
        "replace_list": ["new"],
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


def test_record_outcome_rejects_unsupported_fact_action_before_tools() -> None:
    with pytest.raises(
        ValueError,
        match="public continuity commit does not support deletion or retraction",
    ):
        asyncio.run(
            _record_outcome(
                object(),
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
                facts=[
                    {
                        "fact_key": "quest:hostage:status",
                        "content": "completed",
                        "action": "retract",
                    }
                ],
                npc_states=[],
                quest_states=[],
                clue_states=[],
                world_state={},
                objective="",
                progress_percent=100,
            )
        )


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


def test_record_outcome_recovers_completed_retry_after_later_campaign_revisions() -> None:
    source_ref = {
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
        "source_ref": source_ref,
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
            self.calls: list[str] = []

        async def load(self, *_group_ids: str) -> None:
            return None

        async def core(self, tool_id: str, arguments: dict):
            raise AssertionError(("completed outcome retry must not mutate", tool_id))

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append(tool_id)
            if tool_id == "playthrough_manifest" and arguments["action"] == "get":
                return {"manifest": deepcopy(self.manifest), "campaign_revision": 12}
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
                        "state_version": 8,
                        "state": {
                            "full_playthrough_outcomes": {"hostage-released": outcome_record},
                            "full_playthrough_events": {"later": {"event_type": "later_event"}},
                        },
                    }
                ]
            if tool_id == "memory_query":
                return {
                    "result": [
                        {
                            "fact_key": "quest:hostage:status",
                            "content": "completed",
                            "revision_id": "fact-revision-1",
                        }
                    ]
                }
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "campaign_event":
                return [
                    {
                        "id": "event-1",
                        "event_type": "hostage_released",
                        "summary": summary,
                        "payload": {
                            "outcome_id": "hostage-released",
                            "scene_id": "scene-1",
                            "source_scene_id": "scene-1",
                            "location_key": "goblin-den",
                            "source_excerpt": "The hostage is released.",
                            "source_ref": source_ref,
                            "agent_ruling": None,
                        },
                    }
                ]
            if tool_id == "snapshot_query":
                assert arguments == {"campaign_id": "campaign-1", "view": "list"}
                return [
                    {
                        "slot": 7,
                        "label": "Full playthrough outcome: hostage-released",
                    }
                ]
            if tool_id in {"module_set_progress", "memory_change"}:
                raise AssertionError(("completed outcome retry must not mutate", tool_id))
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

    assert result["recovered"] is True
    assert result["continuity"]["recovered"] is True
    assert result["checkpoint"]["slot"] == 7
    assert "module_set_progress" not in client.calls
    assert "memory_change" not in client.calls


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
            assert group_ids == ()

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
                return {
                    "manifest": deepcopy(self.manifest),
                    "runtime": {
                        "current_scene": (
                            {
                                "scene_id": "scene-1",
                                "progress": {"status": "current", "percent": 1},
                            }
                            if self.manifest.get("current", {}).get("scene_id") == "scene-1"
                            else None
                        )
                    },
                    "campaign_revision": 10,
                }
            if tool_id == "playthrough_manifest" and arguments["action"] == "replace":
                self.manifest = deepcopy(arguments["payload"]["manifest"])
                return {"manifest": deepcopy(self.manifest), "campaign_revision": 9}
            if tool_id == "playthrough_manifest" and arguments["action"] == "sync":
                return {"manifest": deepcopy(self.manifest), "campaign_revision": 10}
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            if tool_id == "module_query":
                if arguments["view"] == "progress":
                    return []
                if arguments["view"] == "current":
                    return {
                        "scene_id": "scene-1",
                        "progress": {"status": "current", "percent": 1},
                    }
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "chapter_id": "chapter-1",
                    "chapter": "Chapter 1",
                    "title": "Opening",
                    "content": source_excerpt,
                }
            if tool_id == "module_set_progress":
                return {
                    "scene_id": "scene-1",
                    "status": arguments["status"],
                    "progress": 1,
                    "state": deepcopy(arguments["state"]),
                    "current_location_key": "",
                    "state_version": 1,
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
    progress_call = next(
        arguments for name, arguments in client.calls if name == "module_set_progress"
    )
    assert progress_call["status"] == "current"
