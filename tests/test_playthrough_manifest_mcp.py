from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

from sagasmith_dnd.character_schema import default_character_sheet
from sagasmith_dnd.playthrough import new_playthrough_manifest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server

SOURCE_REF = {
    "purpose": "campaign_setup",
    "asset_path": "Campaign.md",
    "asset_sha256": "a" * 64,
    "page_start": 1,
    "page_end": 1,
    "heading_path": ["Chapter One"],
    "chunk_content_sha256": "b" * 64,
}


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
            "module_import",
            {
                "campaign_id": campaign_id,
                "action": "stage",
                "payload": {
                    "name": "Campaign.md",
                    "content": "# Chapter One\n\n## Opening\n\nThe party begins.\n",
                    "source_key": "campaign",
                    "title": "Campaign",
                },
                "idempotency_key": "stage",
            },
        )
        job_id = staged["job"]["id"]
        await _call(
            server,
            "module_import",
            {
                "campaign_id": campaign_id,
                "action": "inspect",
                "payload": {"job_id": job_id},
                "idempotency_key": "inspect",
            },
        )
        await _call(
            server,
            "module_import",
            {
                "campaign_id": campaign_id,
                "action": "validate",
                "payload": {"job_id": job_id},
                "idempotency_key": "validate",
            },
        )
        ingested = await _call(
            server,
            "module_import",
            {
                "campaign_id": campaign_id,
                "action": "ingest",
                "payload": {"job_id": job_id},
                "idempotency_key": "ingest",
            },
        )
        before_activate = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign_id}},
        )
        activated = await _call(
            server,
            "module_import",
            {
                "campaign_id": campaign_id,
                "action": "activate",
                "payload": {"job_id": job_id},
                "expected_revision": before_activate["revision"],
                "idempotency_key": "activate",
            },
        )
        module_id = activated["activation"]["module_id"]
        assert module_id == ingested["module_id"]
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
            source_refs=[SOURCE_REF],
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

        revision_staged = await _call(
            server,
            "module_import",
            {
                "campaign_id": campaign_id,
                "action": "stage",
                "payload": {
                    "name": "Campaign-v2.md",
                    "content": (
                        "# Chapter One\n\n## Opening\n\nThe party begins.\n"
                        "#### 1. Revised Room\n\nThe indexed room is explicit.\n"
                    ),
                    "source_key": "campaign",
                    "title": "Campaign",
                },
                "idempotency_key": "revision-stage",
            },
        )
        revision_job_id = revision_staged["job"]["id"]
        for action in ("inspect", "validate", "ingest"):
            await _call(
                server,
                "module_import",
                {
                    "campaign_id": campaign_id,
                    "action": action,
                    "payload": {"job_id": revision_job_id},
                    "idempotency_key": f"revision-{action}",
                },
            )
        before_revision_activate = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign_id}},
        )
        revision_activated = await _call(
            server,
            "module_import",
            {
                "campaign_id": campaign_id,
                "action": "activate",
                "payload": {"job_id": revision_job_id},
                "expected_revision": before_revision_activate["revision"],
                "idempotency_key": "revision-activate",
            },
        )
        revision_module_id = revision_activated["activation"]["module_id"]
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
        updated_manifest = deepcopy(initialized["manifest"])
        updated_manifest["party"]["members"] = [
            {
                "actor_id": actor["id"],
                "name": actor["name"],
                "status": "active",
                "source": "pregen",
                "source_asset_path": "Pregenerated-Hero.pdf",
                "level": 1,
                "xp": 0,
                "hit_points": {"current": 1, "maximum": 1},
                "resources": {},
                "equipment": [],
                "knowledge_scope_actor_id": actor["id"],
            }
        ]
        updated_manifest["world_state"]["victory"] = True
        updated_manifest["status"] = "in_progress"
        updated_manifest["current"] = {
            "module_id": module_id,
            "chapter_id": str(opening_scene.get("chapter_id") or ""),
            "chapter_title": str(opening_scene.get("chapter") or ""),
            "scene_id": str(opening_scene["scene_id"]),
            "scene_title": str(opening_scene.get("title") or ""),
            "objective": "Complete the source-defined ending.",
        }
        updated_manifest["ending"]["conditions"] = [
            {
                "id": "victory",
                "label": "The campaign threat is defeated",
                "source_ref": SOURCE_REF,
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
        ]
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
        synced = await _call(
            server,
            "playthrough_manifest",
            {
                "campaign_id": campaign_id,
                "action": "sync",
                "expected_revision": replaced["campaign_revision"],
                "idempotency_key": "manifest-sync",
            },
        )
        assert synced["manifest"]["status"] == "in_progress"
        synced_member = synced["manifest"]["party"]["members"][0]
        assert synced_member["name"] == "Pregenerated Hero"
        assert synced_member["resources"]["character"]["second_wind"]["value"] == 0
        assert synced_member["resources"]["spell_slots"]["1"]["value"] == 1
        assert synced_member["resources"]["hit_dice"]["d8"]["value"] == 1
        assert synced_member["resources"]["death_saves"]["successes"] == 1
        assert synced["runtime"]["world_state"]["combat_active"] is False
        assert synced["manifest"]["random_stream"]["position"] == 0

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
