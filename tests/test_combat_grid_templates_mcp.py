from __future__ import annotations

import asyncio
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from PIL import Image
from sagasmith_core.content_pack import ARCHIVE_DESCRIPTOR, loads_content_archive
from sagasmith_core.integrity import canonical_json

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from tests.authoring_helpers import finalize_and_activate_module


def _config(tmp_path: Path) -> McpConfig:
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        module_import_roots=(tmp_path,),
        auto_seed_rules=False,
    )


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def test_finalized_combat_grid_template_starts_isolated_encounter_map(tmp_path: Path) -> None:
    async def exercise() -> None:
        config = _config(tmp_path)
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Template grid", "edition": "2014", "idempotency_key": "campaign"},
        )
        mover = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Mover"},
                "idempotency_key": "mover",
            },
        )
        threat = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Threat"},
                "idempotency_key": "threat",
            },
        )
        started = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "name": "keep.md",
                    "content": "# Keep\n## Layout\n#### A1. Gate\nA 30 by 20 foot gatehouse.",
                    "source_key": "keep",
                    "title": "Keep",
                },
                "idempotency_key": "draft",
            },
        )
        scene = next(
            item
            for item in await _call(
                server,
                "module_query",
                {
                    "campaign_id": campaign["id"],
                    "view": "index",
                    "payload": {"module_id": started["module_id"]},
                },
            )
            if item["title"] == "Layout"
        )
        image_path = tmp_path / "gate-map.png"
        Image.new("RGB", (8, 8), "#808080").save(image_path)
        await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "job_id": started["job"]["id"],
                    "operation": "asset",
                    "source_path": str(image_path),
                    "asset_kind": "map_image",
                    "scene_id": scene["scene_id"],
                    "location_key": "a1-gate",
                    "title": "Gate map evidence",
                    "metadata": {"content_asset_key": "gate-map"},
                },
                "idempotency_key": "attach-gate-map",
            },
        )
        evidence = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "evidence",
                "payload": {"job_id": started["job"]["id"], "kind": "chunks"},
            },
        )
        source_ref = deepcopy(evidence[0]["source_ref"])
        template = {
            "schema_version": 1,
            "id": "gate-ambush",
            "title": "Gate ambush",
            "location_key": "a1-gate",
            "grid": {"kind": "square", "cell_ft": 5},
            "bounds": {"width_cells": 6, "height_cells": 4},
            "blocked_cells": [{"x": 3, "y": 1}],
            "difficult_cells": [{"x": 2, "y": 1}],
            "deployment_zones": [
                {"id": "party", "cells": [{"x": 0, "y": 3}]},
                {"id": "hostile", "cells": [{"x": 5, "y": 0}]},
            ],
            "map_asset_key": "gate-map",
            "source_refs": [source_ref],
        }
        edit_arguments = {
            "campaign_id": campaign["id"],
            "action": "edit",
            "payload": {
                "job_id": started["job"]["id"],
                "operation": "combat_grid",
                "change": "upsert",
                "scene_id": scene["scene_id"],
                "template": template,
                "note": "Reviewed the source-bound gate map.",
            },
            "expected_revision": started["job"]["revision"],
            "idempotency_key": "grid-upsert",
        }
        edited = await _call(server, "module_draft", edit_arguments)
        assert await _call(server, "module_draft", edit_arguments) == edited
        assert edited["combat_grid_templates"][0]["id"] == "gate-ambush"
        with pytest.raises(ToolError, match="revision conflict"):
            await _call(
                server,
                "module_draft",
                {
                    **edit_arguments,
                    "idempotency_key": "grid-stale",
                },
            )
        removed = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "job_id": started["job"]["id"],
                    "operation": "combat_grid",
                    "change": "remove",
                    "scene_id": scene["scene_id"],
                    "template_id": "gate-ambush",
                    "source_refs": [source_ref],
                    "note": "Exercise the evidence-bound removal path.",
                },
                "expected_revision": edited["job"]["revision"],
                "idempotency_key": "grid-remove",
            },
        )
        assert removed["combat_grid_templates"] == []
        assert await _call(server, "module_draft", edit_arguments) == edited
        edited = await _call(
            server,
            "module_draft",
            {
                **edit_arguments,
                "expected_revision": removed["job"]["revision"],
                "idempotency_key": "grid-reinsert",
            },
        )
        finalized = await finalize_and_activate_module(
            _call,
            server,
            campaign["id"],
            {**started, "job": edited["job"]},
            source_key="keep",
            title="Keep",
            portable_id="dnd5e.module.template-grid",
        )
        imported_module_id = finalized["imported"]["module_id"]
        package_before = await _call(
            server,
            "content_pack",
            {
                "action": "export",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "module",
                    "module_id": imported_module_id,
                    "include_package": True,
                },
            },
        )
        packaged_template = next(
            template
            for packaged_scene in package_before["package"]["content"]["scene_atlas"]
            for template in dict(packaged_scene["metadata"]["profile_data"]).get(
                "combat_grid_templates", []
            )
        )
        assert packaged_template["source_refs"][0]["chunk_key"]
        assert packaged_template["map_asset_key"] == "gate-map"
        with pytest.raises(ToolError, match="immutable"):
            await _call(
                server,
                "module_draft",
                {
                    **edit_arguments,
                    "expected_revision": edited["job"]["revision"],
                    "idempotency_key": "edit-finalized-grid",
                },
            )
        server = create_server(config)
        imported_scene = next(
            item
            for item in await _call(
                server,
                "module_query",
                {
                    "campaign_id": campaign["id"],
                    "view": "index",
                    "payload": {"module_id": imported_module_id},
                },
            )
            if item["title"] == "Layout"
        )
        campaign_state = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        base_start = {
            "campaign_id": campaign["id"],
            "scene_id": imported_scene["scene_id"],
            "positioning_mode": "grid",
            "battle_map_template_id": "gate-ambush",
            "participant_ids": [mover["id"], threat["id"]],
            "expected_revision": campaign_state["revision"],
        }
        with pytest.raises(ToolError, match="mutually exclusive"):
            await _call(
                server,
                "combat_start",
                {
                    **base_start,
                    "battle_map": {"width_cells": 6, "height_cells": 4},
                    "participant_config": [],
                    "idempotency_key": "two-map-authorities",
                },
            )
        for request_key, invalid_position, expected_message in (
            ("blocked-start", {"x": 3, "y": 1}, "blocked"),
            ("outside-start", {"x": 6, "y": 0}, "outside"),
        ):
            with pytest.raises(ToolError, match=expected_message):
                await _call(
                    server,
                    "combat_start",
                    {
                        **base_start,
                        "participant_config": [
                            {
                                "actor_id": mover["id"],
                                "position": invalid_position,
                                "deployment_zone_id": "party",
                            },
                            {
                                "actor_id": threat["id"],
                                "position": {"x": 5, "y": 0},
                                "deployment_zone_id": "hostile",
                            },
                        ],
                        "idempotency_key": request_key,
                    },
                )
        combat = await _call(
            server,
            "combat_start",
            {
                **base_start,
                "participant_config": [
                    {
                        "actor_id": mover["id"],
                        "position": {"x": 0, "y": 3},
                        "deployment_zone_id": "party",
                        "initiative": 20,
                    },
                    {
                        "actor_id": threat["id"],
                        "position": {"x": 5, "y": 0},
                        "deployment_zone_id": "hostile",
                        "initiative": 10,
                    },
                ],
                "idempotency_key": "combat-start",
            },
        )
        battle_map = combat["combat"]["battle_map"]
        assert battle_map["authority_receipt"]["kind"] == "content_pack_template"
        assert battle_map["source"]["battle_map_template_id"] == "gate-ambush"
        main_branch = next(
            item
            for item in await _call(
                server,
                "branch_query",
                {
                    "campaign_id": campaign["id"],
                    "view": "list",
                    "payload": {},
                },
            )
            if item["is_current"]
        )
        baseline = await _call(
            server,
            "snapshot_create",
            {
                "campaign_id": campaign["id"],
                "label": "Template map baseline",
                "expected_revision": combat["campaign_revision"],
                "expected_head_snapshot_id": "",
                "idempotency_key": "snapshot-baseline",
            },
        )
        patched = await _call(
            server,
            "combat_map_patch",
            {
                "campaign_id": campaign["id"],
                "patches": [{"key": "gate_open", "value": True}],
                "expected_revision": combat["campaign_revision"],
                "idempotency_key": "patch",
            },
        )
        assert patched["battle_map"]["map_revision"] == 2
        history = await _call(
            server,
            "state_revision",
            {
                "campaign_id": campaign["id"],
                "action": "history",
                "payload": {},
            },
        )
        await _call(
            server,
            "state_revision",
            {
                "campaign_id": campaign["id"],
                "action": "undo",
                "payload": {"expected_history_sequence": history[0]["sequence"]},
                "idempotency_key": "undo-map-patch",
            },
        )
        undone = await _call(
            server,
            "combat_query",
            {"campaign_id": campaign["id"], "view": "status"},
        )
        assert undone["battle_map"]["map_revision"] == 1
        undone_history = await _call(
            server,
            "state_revision",
            {
                "campaign_id": campaign["id"],
                "action": "history",
                "payload": {},
            },
        )
        redo_cursor = next(item["sequence"] for item in undone_history if item["applied"])
        await _call(
            server,
            "state_revision",
            {
                "campaign_id": campaign["id"],
                "action": "redo",
                "payload": {"expected_history_sequence": redo_cursor},
                "idempotency_key": "redo-map-patch",
            },
        )
        redone = await _call(
            server,
            "combat_query",
            {"campaign_id": campaign["id"], "view": "status"},
        )
        assert redone["battle_map"]["map_revision"] == 2
        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        patched_snapshot = await _call(
            server,
            "snapshot_create",
            {
                "campaign_id": campaign["id"],
                "label": "Patched template map",
                "expected_revision": current["revision"],
                "expected_head_snapshot_id": baseline["id"],
                "idempotency_key": "snapshot-patched",
            },
        )
        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        await _call(
            server,
            "branch_change",
            {
                "campaign_id": campaign["id"],
                "action": "create",
                "payload": {
                    "name": "unpatched-map",
                    "from_snapshot_id": baseline["id"],
                    "checkout": True,
                },
                "expected_revision": current["revision"],
                "expected_branch_id": main_branch["id"],
                "idempotency_key": "branch-unpatched",
            },
        )
        branched = await _call(
            server,
            "combat_query",
            {"campaign_id": campaign["id"], "view": "status"},
        )
        assert branched["battle_map"]["map_revision"] == 1
        assert patched_snapshot["id"] != baseline["id"]
        package_after, package_blobs = loads_content_archive(
            (config.content_packages_dir / package_before["artifact"]).read_bytes()
        )
        assert package_after["checksum"] == package_before["package"]["checksum"]
        assert packaged_template == next(
            template
            for packaged_scene in package_after["content"]["scene_atlas"]
            for template in dict(packaged_scene["metadata"]["profile_data"]).get(
                "combat_grid_templates", []
            )
        )
        map_asset = next(
            asset for asset in package_after["assets"] if asset["asset_key"] == "gate-map"
        )
        corrupt_output = BytesIO()
        with ZipFile(corrupt_output, "w") as archive:
            archive.writestr(ARCHIVE_DESCRIPTOR, canonical_json(package_after))
            for checksum, content in package_blobs.items():
                archive.writestr(
                    f"blobs/sha256/{checksum}",
                    content + b"corrupt" if checksum == map_asset["checksum"] else content,
                    compress_type=ZIP_STORED,
                )
        corrupt_artifact = "corrupt-map.sagasmith-pack"
        (config.content_packages_dir / corrupt_artifact).write_bytes(corrupt_output.getvalue())
        corrupt_target = await _call(
            server,
            "campaign_create",
            {"name": "Corrupt target", "edition": "2014", "idempotency_key": "corrupt"},
        )
        with pytest.raises(ToolError, match="archive blob mismatch"):
            await _call(
                server,
                "content_pack",
                {
                    "action": "import",
                    "payload": {
                        "campaign_id": corrupt_target["id"],
                        "kind": "module",
                        "artifact": corrupt_artifact,
                    },
                    "idempotency_key": "corrupt-import",
                },
            )
        target = await _call(
            server,
            "campaign_create",
            {"name": "Template target", "edition": "2014", "idempotency_key": "target"},
        )
        target_import = await _call(
            server,
            "content_pack",
            {
                "action": "import",
                "payload": {
                    "campaign_id": target["id"],
                    "kind": "module",
                    "artifact": package_before["artifact"],
                },
                "idempotency_key": "target-import",
            },
        )
        target_state = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": target["id"]}},
        )
        await _call(
            server,
            "content_pack",
            {
                "action": "activate",
                "payload": {
                    "campaign_id": target["id"],
                    "kind": "module",
                    "module_id": target_import["module_id"],
                },
                "expected_revision": target_state["revision"],
                "idempotency_key": "target-activate",
            },
        )
        target_mover = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": target["id"], "name": "Target mover"},
                "idempotency_key": "target-mover",
            },
        )
        target_threat = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": target["id"], "name": "Target threat"},
                "idempotency_key": "target-threat",
            },
        )
        target_scene = next(
            item
            for item in await _call(
                server,
                "module_query",
                {
                    "campaign_id": target["id"],
                    "view": "index",
                    "payload": {"module_id": target_import["module_id"]},
                },
            )
            if item["title"] == "Layout"
        )
        target_state = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": target["id"]}},
        )
        target_combat = await _call(
            server,
            "combat_start",
            {
                "campaign_id": target["id"],
                "scene_id": target_scene["scene_id"],
                "positioning_mode": "grid",
                "battle_map_template_id": "gate-ambush",
                "participant_ids": [target_mover["id"], target_threat["id"]],
                "participant_config": [
                    {
                        "actor_id": target_mover["id"],
                        "position": {"x": 0, "y": 3},
                        "deployment_zone_id": "party",
                    },
                    {
                        "actor_id": target_threat["id"],
                        "position": {"x": 5, "y": 0},
                        "deployment_zone_id": "hostile",
                    },
                ],
                "expected_revision": target_state["revision"],
                "idempotency_key": "target-combat",
            },
        )
        target_map = target_combat["combat"]["battle_map"]
        assert target_map["id"] != battle_map["id"]
        assert target_map["authority_receipt"]["package_checksum"] == package_after["checksum"]
        override_campaign = await _call(
            server,
            "campaign_create",
            {"name": "Override receipt", "edition": "2014", "idempotency_key": "override"},
        )
        override_actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": override_campaign["id"], "name": "Override actor"},
                "idempotency_key": "override-actor",
            },
        )
        override_state = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": override_campaign["id"]}},
        )
        override_combat = await _call(
            server,
            "combat_start",
            {
                "campaign_id": override_campaign["id"],
                "positioning_mode": "grid",
                "battle_map": {"width_cells": 4, "height_cells": 4},
                "battle_map_override_reason": "The DM established bounded open terrain.",
                "participant_ids": [override_actor["id"]],
                "participant_config": [
                    {"actor_id": override_actor["id"], "position": {"x": 1, "y": 1}}
                ],
                "expected_revision": override_state["revision"],
                "idempotency_key": "override-combat",
            },
        )
        override_receipt = override_combat["combat"]["battle_map"]["authority_receipt"]
        assert override_receipt["kind"] == "dm_override"
        assert override_receipt["reason"] == "The DM established bounded open terrain."

    asyncio.run(exercise())
