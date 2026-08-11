from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet
from sagasmith_dnd.playthrough import new_playthrough_manifest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from tests.authoring_helpers import finalize_and_activate_module


async def _call(server, name: str, arguments: dict):
    _, structured = await server.call_tool(name, arguments)
    value = structured.get("result", structured) if isinstance(structured, dict) else structured
    if isinstance(value, dict) and "action" in value and "result" in value:
        return value["result"]
    return value


def _config(tmp_path: Path) -> McpConfig:
    workspace = Path(__file__).resolve().parents[2]
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=workspace / "SagaSmith-dnd-skills",
        modulegen_skills_dir=workspace / "SagaSmith-module-gen-skills",
        auto_seed_rules=False,
    )


def test_manifest_syncs_canonical_state_and_verifies_source_defined_ending(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Full playthrough",
                "edition": "2014",
                "random_seed": "playthrough-seed",
                "idempotency_key": "campaign",
            },
        )
        campaign_id = campaign["id"]
        staged = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign_id,
                "action": "start",
                "payload": {
                    "name": "Campaign.md",
                    "content": (
                        "<!-- page: 1 -->\n"
                        "# Chapter One\n\n## Opening\n\n"
                        "The party begins. A storm gathers. The gate opens.\n"
                    ),
                    "source_key": "campaign",
                    "title": "Campaign",
                },
                "idempotency_key": "stage",
            },
        )
        activation = await finalize_and_activate_module(
            _call,
            server,
            campaign_id,
            staged,
            source_key="campaign",
            title="Campaign",
            portable_id="dnd5e.module.playthrough-campaign",
        )
        module_id = activation["activated"]["activation"]["module_id"]
        module_index = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign_id,
                "view": "index",
                "payload": {"module_id": module_id},
            },
        )
        opening_scene = module_index[0]
        hits = await _call(
            server,
            "module_search",
            {
                "campaign_id": campaign_id,
                "query": "The party begins.",
                "module_ids": [module_id],
            },
        )
        expanded = await _call(
            server,
            "module_expand",
            {"chunk_id": hits[0]["id"]},
        )
        assets = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign_id,
                "view": "assets",
                "payload": {"module_id": module_id},
            },
        )
        source_asset = next(
            item for item in assets if not str(item.get("asset_key") or "").endswith(".normalized")
        )
        source_ref = {
            "purpose": "campaign_setup",
            "asset_path": Path(source_asset["source_path"]).name,
            "asset_sha256": source_asset["checksum"],
            **expanded["source_ref"],
            "excerpt": "The party begins. The gate opens.",
        }
        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign_id}},
        )
        manifest = new_playthrough_manifest(
            run_id="run-1",
            campaign_line_id="line-1",
            module_ids=[module_id],
            recommended_party_minimum=1,
            recommended_party_maximum=1,
            selected_party_size=1,
            source_refs=[source_ref],
        )
        invalid_chunk_manifest = deepcopy(manifest)
        invalid_chunk_manifest["source_refs"][0]["content_sha256"] = "0" * 64
        with pytest.raises(Exception, match="content_sha256"):
            await _call(
                server,
                "playthrough_manifest",
                {
                    "campaign_id": campaign_id,
                    "action": "initialize",
                    "payload": {"manifest": invalid_chunk_manifest},
                    "expected_revision": current["revision"],
                    "idempotency_key": "invalid-manifest-chunk",
                },
            )
        portable_chunk_manifest = deepcopy(manifest)
        portable_chunk_manifest["source_refs"][0]["chunk_id"] = (
            "campaign/scene/opening/chunk/0-portable-key"
        )
        with pytest.raises(
            Exception,
            match=(
                "source_refs\\[0\\]: source_ref chunk_id is not an active runtime "
                "chunk; after Pack activation call module_search"
            ),
        ):
            await _call(
                server,
                "playthrough_manifest",
                {
                    "campaign_id": campaign_id,
                    "action": "initialize",
                    "payload": {"manifest": portable_chunk_manifest},
                    "expected_revision": current["revision"],
                    "idempotency_key": "portable-manifest-chunk",
                },
            )
        invalid_asset_manifest = deepcopy(manifest)
        invalid_asset_manifest["source_refs"][0]["asset_sha256"] = "0" * 64
        with pytest.raises(Exception, match="source asset"):
            await _call(
                server,
                "playthrough_manifest",
                {
                    "campaign_id": campaign_id,
                    "action": "initialize",
                    "payload": {"manifest": invalid_asset_manifest},
                    "expected_revision": current["revision"],
                    "idempotency_key": "invalid-manifest-asset",
                },
            )
        initialized = await _call(
            server,
            "playthrough_manifest",
            {
                "campaign_id": campaign_id,
                "action": "initialize",
                "payload": {"manifest": manifest},
                "expected_revision": current["revision"],
                "idempotency_key": "manifest-init",
            },
        )
        replay = await _call(
            server,
            "playthrough_manifest",
            {
                "campaign_id": campaign_id,
                "action": "initialize",
                "payload": {"manifest": manifest},
                "expected_revision": current["revision"],
                "idempotency_key": "manifest-init",
            },
        )
        assert replay == initialized
        invalid_runtime_ref = {
            key: source_ref[key]
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
        invalid_runtime_ref["content_sha256"] = "0" * 64
        with pytest.raises(Exception, match="content_sha256"):
            await _call(
                server,
                "module_set_progress",
                {
                    "campaign_id": campaign_id,
                    "scene_id": opening_scene["scene_id"],
                    "status": "active",
                    "state": {"source_ref": invalid_runtime_ref},
                    "expected_state_version": 0,
                    "idempotency_key": "invalid-progress-source",
                },
            )
        with pytest.raises(Exception, match="content_sha256"):
            await _call(
                server,
                "campaign_event",
                {
                    "campaign_id": campaign_id,
                    "action": "add",
                    "payload": {
                        "summary": "Invalid source evidence must not enter the event log.",
                        "event_type": "audit",
                        "payload": {"source_ref": invalid_runtime_ref},
                    },
                    "idempotency_key": "invalid-event-source",
                },
            )
        with pytest.raises(Exception, match="content_sha256"):
            await _call(
                server,
                "memory_change",
                {
                    "campaign_id": campaign_id,
                    "action": "commit",
                    "payload": {
                        "event": {
                            "summary": "Invalid source evidence must not enter continuity.",
                            "event_type": "audit",
                            "audience_scope": "dm",
                            "payload": {"source_ref": invalid_runtime_ref},
                        }
                    },
                    "idempotency_key": "invalid-continuity-source",
                },
            )

        revision_staged = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign_id,
                "action": "start",
                "payload": {
                    "name": "Campaign-v2.md",
                    "content": (
                        "<!-- page: 1 -->\n"
                        "# Chapter One\n\n## Opening\n\n"
                        "The party begins. A storm gathers. The gate opens.\n"
                        "#### 1. Revised Room\n\nThe indexed room is explicit.\n"
                    ),
                    "source_key": "campaign",
                    "title": "Campaign",
                },
                "idempotency_key": "revision-stage",
            },
        )
        revision_activation = await finalize_and_activate_module(
            _call,
            server,
            campaign_id,
            revision_staged,
            source_key="campaign",
            title="Campaign v2",
            portable_id="dnd5e.module.playthrough-campaign-v2",
            request_key="campaign-v2",
        )
        revision_module_id = revision_activation["activated"]["activation"]["module_id"]
        extended_manifest = deepcopy(initialized["manifest"])
        extended_manifest["module_ids"].append(revision_module_id)
        before_extend = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign_id}},
        )
        initialized = await _call(
            server,
            "playthrough_manifest",
            {
                "campaign_id": campaign_id,
                "action": "extend_modules",
                "payload": {"manifest": extended_manifest},
                "expected_revision": before_extend["revision"],
                "idempotency_key": "manifest-extend-modules",
            },
        )
        assert initialized["manifest"]["module_ids"] == [module_id, revision_module_id]
        revision_index = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign_id,
                "view": "index",
                "payload": {"module_id": revision_module_id},
            },
        )
        current_revision_scene = revision_index[-1]
        await _call(
            server,
            "module_set_progress",
            {
                "campaign_id": campaign_id,
                "scene_id": current_revision_scene["scene_id"],
                "scope_id": "party",
                "status": "current",
                "progress": 10,
                "expected_state_version": 0,
                "idempotency_key": "revision-scene-current",
            },
        )

        actor_sheet = default_character_sheet()
        actor_sheet["resources"]["second_wind"] = {
            "label": "Second Wind",
            "value": 0,
            "max": 1,
            "recovers_on": "short_rest",
        }
        actor_sheet["combat"]["hit_dice"]["d8"] = {
            "label": "Hit Die",
            "value": 1,
            "max": 1,
            "recovers_on": "long_rest",
        }
        actor_sheet["combat"]["death_saves"]["successes"] = 1
        actor_sheet["combat"]["hp"] = {"value": 30, "max": 38, "temp": 0}
        actor_sheet["combat"]["exhaustion"] = 4
        actor_sheet["spellcasting"]["spell_slots"]["1"] = {
            "label": "1st-level spell slots",
            "value": 1,
            "max": 2,
            "recovers_on": "long_rest",
        }
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign_id,
                    "name": "Pregenerated Hero",
                    "sheet": actor_sheet,
                },
                "idempotency_key": "actor",
            },
        )
        with pytest.raises(Exception, match="source_ref"):
            await _call(
                server,
                "character_state_change",
                {
                    "character_id": actor["id"],
                    "action": "level_advance",
                    "payload": {
                        "class_name": "Fighter",
                        "hp_method": "fixed",
                        "reason": "A weak citation must not pass a full playthrough.",
                        "source_ref": "module:chapter-one",
                    },
                    "expected_revision": actor["revision"],
                    "idempotency_key": "weak-level-source",
                },
            )
        before_play = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign_id}},
        )
        play = await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign_id,
                "action": "set",
                "tool_profile": "play",
                "expected_revision": before_play["revision"],
                "idempotency_key": "play",
            },
        )
        started = await _call(
            server,
            "combat_start",
            {
                "positioning_mode": "agent",
                "campaign_id": campaign_id,
                "participant_ids": [actor["id"]],
                "participant_config": [
                    {
                        "actor_id": actor["id"],
                        "initiative": 10,
                        "tie_breaker": 0,
                    }
                ],
                "expected_revision": play["campaign_revision"],
                "idempotency_key": "historical-combat-start",
            },
        )
        during_combat = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign_id}},
        )
        assert during_combat["state"]["game_phase"] == "play"
        assert during_combat["effective_game_phase"] == "combat"
        await _call(
            server,
            "combat_end",
            {
                "campaign_id": campaign_id,
                "outcome": {
                    "status": "victory",
                    "summary": "The test encounter is complete.",
                },
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "historical-combat-end",
            },
        )
        updated_manifest = deepcopy(initialized["manifest"])
        updated_manifest["party"]["members"] = [
            {
                "actor_id": actor["id"],
                "name": actor["name"],
                "status": "dead",
                "source": "pregen",
                "source_asset_path": "Pregenerated-Hero.pdf",
                "level": 1,
                "xp": 0,
                "hit_points": {"current": 1, "maximum": 1},
                "resources": {},
                "equipment": [],
                "wallet": {},
                "knowledge_scope_actor_id": actor["id"],
            }
        ]
        updated_manifest["world_state"]["victory"] = True
        updated_manifest["snapshot_dag"]["active_branch_id"] = "stale-branch"
        updated_manifest["snapshot_dag"]["head_snapshot_id"] = "stale-snapshot"
        updated_manifest["random_stream"]["position"] = 999
        updated_manifest["status"] = "in_progress"
        updated_manifest["current"] = {
            "module_id": module_id,
            "chapter_id": str(opening_scene.get("chapter_id") or ""),
            "chapter_title": str(opening_scene.get("chapter") or ""),
            "scene_id": str(opening_scene["scene_id"]),
            "scene_title": str(opening_scene.get("title") or ""),
            "objective": "Complete the source-defined ending.",
        }
        ending_condition = {
            "id": "victory",
            "label": "The campaign threat is defeated",
            "source_ref": source_ref,
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
        before_replace = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign_id}},
        )
        replaced = await _call(
            server,
            "playthrough_manifest",
            {
                "campaign_id": campaign_id,
                "action": "replace",
                "payload": {"manifest": updated_manifest},
                "expected_revision": before_replace["revision"],
                "idempotency_key": "manifest-party",
            },
        )
        assert replaced["manifest"]["snapshot_dag"]["active_branch_id"] != "stale-branch"
        assert replaced["manifest"]["snapshot_dag"]["head_snapshot_id"] == ""
        assert replaced["manifest"]["random_stream"]["position"] == 0
        assert replaced["manifest"]["party"]["members"][0]["status"] == "active"
        assert replaced["manifest"]["current"]["scene_id"] == current_revision_scene["scene_id"]
        configured = await _call(
            server,
            "playthrough_manifest",
            {
                "campaign_id": campaign_id,
                "action": "configure_ending",
                "payload": {"condition": ending_condition},
                "expected_revision": replaced["campaign_revision"],
                "idempotency_key": "configure-ending",
            },
        )
        assert configured["manifest"]["ending"]["conditions"] == [ending_condition]
        corrected_condition = deepcopy(ending_condition)
        corrected_condition["label"] = "The source-defined campaign threat is defeated"
        corrected = await _call(
            server,
            "playthrough_manifest",
            {
                "campaign_id": campaign_id,
                "action": "configure_ending",
                "payload": {"condition": corrected_condition},
                "expected_revision": configured["campaign_revision"],
                "idempotency_key": "correct-ending",
            },
        )
        assert corrected["manifest"]["ending"]["conditions"] == [corrected_condition]
        synced = await _call(
            server,
            "playthrough_manifest",
            {
                "campaign_id": campaign_id,
                "action": "sync",
                "expected_revision": corrected["campaign_revision"],
                "idempotency_key": "manifest-sync",
            },
        )
        assert synced["manifest"]["status"] == "in_progress"
        assert synced["runtime"]["current_scene"]["scene_id"] == current_revision_scene["scene_id"]
        synced_member = synced["manifest"]["party"]["members"][0]
        assert synced_member["name"] == "Pregenerated Hero"
        assert synced_member["resources"]["character"]["second_wind"]["value"] == 0
        assert synced_member["resources"]["spell_slots"]["1"]["value"] == 1
        assert synced_member["resources"]["hit_dice"]["d8"]["value"] == 1
        assert synced_member["resources"]["death_saves"]["successes"] == 1
        assert synced_member["hit_points"]["current"] == 19
        assert synced_member["hit_points"]["maximum"] == 19
        assert synced_member["resources"]["exhaustion"] == 4
        assert synced_member["wallet"] == synced["runtime"]["party_members"][0]["wallet"]
        assert synced["runtime"]["world_state"]["combat_active"] is False
        assert synced["manifest"]["random_stream"]["position"] == 0
        persisted = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign_id}},
        )
        assert persisted["state"]["playthrough_manifest"]["snapshot_dag"]["nodes"] == []
        assert (
            persisted["state"]["playthrough_manifest"]["snapshot_dag"]["active_branch_id"]
            == replaced["manifest"]["snapshot_dag"]["active_branch_id"]
        )
        assert persisted["state"]["playthrough_manifest"]["snapshot_dag"]["head_snapshot_id"] == ""
        assert persisted["state"]["playthrough_manifest"]["random_stream"]["position"] == 0

        branches = await _call(
            server,
            "branch_query",
            {"campaign_id": campaign_id, "view": "list"},
        )
        active_branch = next(item for item in branches if item["is_current"])
        snapshot = await _call(
            server,
            "snapshot_create",
            {
                "campaign_id": campaign_id,
                "label": "Opening checkpoint",
                "expected_revision": synced["campaign_revision"],
                "expected_head_snapshot_id": active_branch["head_snapshot_id"] or "",
                "idempotency_key": "opening-checkpoint",
            },
        )
        inspected = await _call(
            server,
            "playthrough_manifest",
            {"campaign_id": campaign_id, "action": "get"},
        )
        assert snapshot["id"] in {
            item["id"] for item in inspected["runtime"]["snapshot_dag"]["nodes"]
        }
        assert snapshot["id"] in {
            item["id"] for item in inspected["manifest"]["snapshot_dag"]["nodes"]
        }
        assert inspected["manifest"]["snapshot_dag"]["head_snapshot_id"] == snapshot["id"]

        ended = await _call(
            server,
            "playthrough_manifest",
            {
                "campaign_id": campaign_id,
                "action": "verify_ending",
                "payload": {"condition_id": "victory"},
                "expected_revision": inspected["campaign_revision"],
                "idempotency_key": "verify-ending",
            },
        )
        assert ended["manifest"]["status"] == "completed"
        assert ended["manifest"]["ending"]["achieved_condition_id"] == "victory"
        assert all(item["passed"] for item in ended["manifest"]["ending"]["verification"])

    asyncio.run(exercise())
