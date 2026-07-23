"""Drive a resumable full campaign exclusively through public stdio MCP tools.

The driver never imports the server implementation and never reads the database.
It maintains the snapshot-managed playthrough manifest, verifies scene ownership,
registers already-created legal parties, creates checkpoints, and verifies only
source-declared machine-readable endings.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from scripts.regression_modules import PRINCIPAL_ID, ExposureClient, _token


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--action",
        choices=(
            "status",
            "sync",
            "checkpoint",
            "advance-scene",
            "record-event",
            "resolve-check",
            "apply-damage",
            "stand-up",
            "use-activity",
            "branch-from-snapshot",
            "short-rest",
            "long-rest",
            "award-xp",
            "configure-advancement",
            "refresh-module",
            "register-party",
            "start-play",
            "verify-ending",
        ),
        default="status",
    )
    parser.add_argument("--run-id", default="full-playthrough-v1")
    parser.add_argument("--advancement-mode", choices=("xp", "milestone"))
    parser.add_argument("--module-root", type=Path)
    parser.add_argument("--module-source-path", type=Path)
    parser.add_argument("--module-source-key", default="")
    parser.add_argument("--module-title", default="")
    parser.add_argument("--checkpoint-label", default="")
    parser.add_argument("--scene-id")
    parser.add_argument("--location-key", default="")
    parser.add_argument("--source-excerpt", default="")
    parser.add_argument(
        "--source-ref-json",
        type=json.loads,
        help="Exact module source reference for the playthrough action",
    )
    parser.add_argument("--check-actor-id", default="")
    parser.add_argument(
        "--check-kind",
        choices=("ability", "check", "save", "death_save"),
        default="",
        help="Public character_check kind; use ability with a skill name as --check-ability",
    )
    parser.add_argument("--check-ability", default="")
    parser.add_argument("--check-dc", type=int)
    parser.add_argument("--check-proficient", action="store_true")
    parser.add_argument("--check-advantage", action="store_true")
    parser.add_argument("--check-disadvantage", action="store_true")
    parser.add_argument("--knowledge-actor-id", action="append", default=[])
    parser.add_argument("--success-knowledge", default="")
    parser.add_argument("--failure-knowledge", default="")
    parser.add_argument("--damage-actor-id", default="")
    parser.add_argument("--damage-expression", default="")
    parser.add_argument("--damage-type", default="")
    parser.add_argument("--damage-reason", default="")
    parser.add_argument(
        "--damage-half",
        action="store_true",
        help="Apply half the rolled damage, rounded down, when the cited source requires it",
    )
    parser.add_argument("--damage-knock-prone", action="store_true")
    parser.add_argument("--stand-actor-id", default="")
    parser.add_argument("--stand-reason", default="")
    parser.add_argument("--activity-actor-id", default="")
    parser.add_argument("--activity-id", default="")
    parser.add_argument("--activity-declaration-json", type=json.loads)
    parser.add_argument("--activity-reason", default="")
    parser.add_argument("--snapshot-slot", type=int)
    parser.add_argument("--branch-name", default="")
    parser.add_argument("--rest-member-json", action="append", type=json.loads, default=[])
    parser.add_argument("--rest-start-clock-json", type=json.loads)
    parser.add_argument("--rest-duration-minutes", type=int, default=60)
    parser.add_argument("--rest-reason", default="")
    parser.add_argument("--event-type", default="")
    parser.add_argument("--event-summary", default="")
    parser.add_argument("--event-knowledge", default="")
    parser.add_argument("--event-knowledge-actor-id", action="append", default=[])
    parser.add_argument("--progress-percent", type=int)
    parser.add_argument("--xp-actor-id", action="append", default=[])
    parser.add_argument("--xp-amount", type=int)
    parser.add_argument("--xp-reason", default="")
    parser.add_argument("--objective", default="")
    parser.add_argument("--mark-visited", action="store_true")
    parser.add_argument("--reachable-scene-id", action="append", default=[])
    parser.add_argument(
        "--excluded-scene-json",
        action="append",
        type=json.loads,
        default=[],
        help="JSON object with scene_id, reason, and optional exact source_ref",
    )
    parser.add_argument(
        "--party-member-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "JSON object with actor_id, source=pregen|generated|replacement, "
            "source_asset_path, and optional status"
        ),
    )
    parser.add_argument(
        "--party-report",
        type=Path,
        help="Party-builder JSON report whose manifest_members should be registered",
    )
    parser.add_argument("--condition-id")
    return parser.parse_args()


def _party_selections(args: argparse.Namespace) -> list[dict[str, Any]]:
    selections = deepcopy(list(args.party_member_json))
    if args.party_report is None:
        return selections
    report_path = args.party_report.expanduser().resolve()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report_members = report.get("manifest_members")
    if not isinstance(report_members, list) or not report_members:
        raise ValueError("party report must contain a non-empty manifest_members array")
    if selections:
        raise ValueError("--party-report cannot be combined with --party-member-json")
    return [dict(item) for item in report_members]


def _server_parameters(args: argparse.Namespace) -> StdioServerParameters:
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "SAGASMITH_DND_MCP_HOME": str(args.home.expanduser().resolve()),
            "SAGASMITH_DND_MCP_AUTO_SEED": "1",
        }
    )
    if args.module_root:
        env["SAGASMITH_DND_MCP_MODULE_IMPORT_ROOTS"] = str(args.module_root.expanduser().resolve())
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "sagasmith_dnd_mcp.server"],
        cwd=repo,
        env=env,
    )


def _facade_value(value: Any) -> Any:
    if isinstance(value, dict) and "result" in value:
        return value["result"]
    return value


def _phase_groups(phase: str) -> tuple[str, ...]:
    return {
        "lobby": ("lobby.campaign",),
        "play": ("play.scene_control", "play.scene"),
        "combat": ("combat.save", "combat.observe"),
    }[phase]


def _character_group(phase: str) -> str:
    return {
        "lobby": "lobby.characters",
        "play": "play.characters",
        "combat": "combat.observe",
    }[phase]


def _scene_groups(phase: str) -> tuple[str, ...]:
    if phase == "lobby":
        return ("lobby.modules",)
    if phase == "play":
        return ("play.scene",)
    raise RuntimeError("scene progression cannot advance during active combat")


def _scene_locations(scene: dict[str, Any]) -> list[dict[str, Any]]:
    spatial = scene.get("spatial") if isinstance(scene.get("spatial"), dict) else {}
    values = spatial.get("locations") or scene.get("locations") or []
    return [item for item in values if isinstance(item, dict)]


def _scene_progress_percent(progress: dict[str, Any] | None) -> int:
    if not progress:
        return 0
    value = progress.get("progress", progress.get("percent", 0))
    return int(value or 0)


def _extend_manifest_for_module_revision(
    manifest: dict[str, Any],
    *,
    old_module_id: str,
    new_module_id: str,
    old_index: list[dict[str, Any]],
    new_index: list[dict[str, Any]],
) -> dict[str, Any]:
    value = deepcopy(manifest)
    if old_module_id not in value["module_ids"]:
        raise ValueError("current module revision is not registered in the playthrough manifest")
    old_by_id = {str(item["scene_id"]): item for item in old_index}
    new_by_key = {
        str(item.get("stable_key") or ""): item
        for item in new_index
        if str(item.get("stable_key") or "")
    }
    scene_map: dict[str, dict[str, Any]] = {}
    for scene_id, scene in old_by_id.items():
        stable_key = str(scene.get("stable_key") or "")
        replacement = new_by_key.get(stable_key)
        if replacement is not None:
            scene_map[scene_id] = replacement
    current_scene_id = str(value["current"].get("scene_id") or "")
    replacement = scene_map.get(current_scene_id)
    if replacement is None:
        raise ValueError("current scene has no stable-key match in the new module revision")
    value["module_ids"].append(new_module_id)
    value["current"].update(
        {
            "module_id": new_module_id,
            "chapter_id": str(replacement.get("chapter_id") or ""),
            "chapter_title": str(replacement.get("chapter") or ""),
            "scene_id": str(replacement["scene_id"]),
            "scene_title": str(replacement.get("title") or ""),
        }
    )
    traversal = value["traversal"]
    for field in ("reachable_scene_ids", "visited_scene_ids"):
        scene_ids = list(traversal[field])
        for scene_id in list(scene_ids):
            mapped = scene_map.get(str(scene_id))
            if mapped is not None and str(mapped["scene_id"]) not in scene_ids:
                scene_ids.append(str(mapped["scene_id"]))
        traversal[field] = scene_ids
    return value


def _normalized_source_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _validate_source_ref(
    scene: dict[str, Any],
    source_ref: dict[str, Any] | None,
    *,
    excerpt: str = "",
) -> dict[str, Any]:
    if not isinstance(source_ref, dict):
        raise ValueError("playthrough action requires --source-ref-json")
    required = {
        "module_id",
        "scene_id",
        "chunk_id",
        "page_start",
        "page_end",
        "heading_path",
        "content_sha256",
    }
    missing = sorted(required - set(source_ref))
    if missing:
        raise ValueError(f"source_ref is missing required fields: {', '.join(missing)}")
    if str(source_ref["module_id"]) != str(scene.get("module_id")):
        raise ValueError("source_ref module_id does not match the cited scene")
    if str(source_ref["scene_id"]) != str(scene.get("scene_id")):
        raise ValueError("source_ref scene_id does not match the cited scene")
    if not str(source_ref["chunk_id"]).strip() or not str(source_ref["content_sha256"]).strip():
        raise ValueError("source_ref chunk_id and content_sha256 must not be empty")
    if excerpt and _normalized_source_text(excerpt) not in _normalized_source_text(
        scene.get("content")
    ):
        raise ValueError("source excerpt is not contained in the cited scene")
    return deepcopy(source_ref)


def _campaign_phase(campaign: dict[str, Any]) -> str:
    phase = str(dict(campaign.get("state") or {}).get("game_phase") or "lobby")
    if phase not in {"lobby", "play", "combat"}:
        raise RuntimeError(f"unsupported campaign phase: {phase}")
    return phase


async def _campaign(client: ExposureClient, campaign_id: str) -> dict[str, Any]:
    return _facade_value(
        await client.core(
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign_id},
                "principal_id": PRINCIPAL_ID,
            },
        )
    )


async def _manifest_get(
    client: ExposureClient,
    campaign_id: str,
) -> dict[str, Any]:
    return await client.domain(
        "playthrough_manifest",
        {"campaign_id": campaign_id, "action": "get"},
    )


def _mutation_key(run_id: str, action: str, identity: str) -> str:
    return f"full-playthrough-{action}-{_token(f'{run_id}:{identity}', length=24)}"


def _check_knowledge_key(
    run_id: str,
    scene_id: str,
    kind: str,
    ability: str,
    actor_id: str,
) -> str:
    return (
        f"playthrough.{_token(run_id)}.{_token(scene_id)}."
        f"{_token(kind)}.{_token(ability)}.{_token(actor_id)}"
    )


def _committed_check_result(settled: dict[str, Any]) -> dict[str, Any]:
    """Accept full tool responses and compact dynamic-exposure facades."""

    if settled.get("status") == "committed" and isinstance(settled.get("result"), dict):
        return dict(settled["result"])
    if "success" in settled and ("total" in settled or settled.get("automatic_failure")):
        return dict(settled)
    raise RuntimeError("source-cited character check did not commit")


def _matching_check_progress(
    progress: dict[str, Any] | None,
    *,
    location_key: str,
    actor_id: str,
    kind: str,
    ability: str,
    dc: int,
    advantage: bool,
    disadvantage: bool,
    source_ref: dict[str, Any],
) -> bool:
    if not isinstance(progress, dict):
        return False
    state = dict(progress.get("state") or {})
    check = dict(state.get("full_playthrough_check") or {})
    return bool(
        str(progress.get("current_location_key") or "") == location_key
        and check.get("actor_id") == actor_id
        and check.get("kind") == kind
        and check.get("ability") == ability
        and check.get("dc") == dc
        and bool(check.get("advantage", False)) == advantage
        and bool(check.get("disadvantage", False)) == disadvantage
        and check.get("source_ref") == source_ref
    )


def _recover_committed_check(
    campaign: dict[str, Any],
    *,
    progress_matches: bool,
    actor_id: str,
    kind: str,
    dc: int,
) -> dict[str, Any] | None:
    """Recover a check committed before a driver-side response failure."""

    if not progress_matches:
        return None
    state = dict(campaign.get("state") or {})
    random_stream = dict(state.get("random_stream") or {})
    last_receipt = dict(random_stream.get("last_receipt") or {})
    if last_receipt.get("operation") != "character_check":
        return None
    resolution_log = list(state.get("resolution_log") or [])
    if not resolution_log:
        return None
    latest = dict(resolution_log[-1])
    result = dict(latest.get("result") or {})
    if (
        latest.get("type") != kind
        or latest.get("actor_id") != actor_id
        or result.get("dc") != dc
        or "success" not in result
    ):
        return None
    return result


async def _manifest_mutation(
    client: ExposureClient,
    *,
    campaign_id: str,
    action: str,
    run_id: str,
    identity: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    campaign = await _campaign(client, campaign_id)
    arguments: dict[str, Any] = {
        "campaign_id": campaign_id,
        "action": action,
        "expected_revision": campaign["revision"],
        "idempotency_key": _mutation_key(run_id, action, identity),
    }
    if payload is not None:
        arguments["payload"] = payload
    return await client.domain("playthrough_manifest", arguments)


async def _checkpoint(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    label: str,
) -> dict[str, Any]:
    if not label.strip():
        raise ValueError("checkpoint label must not be empty")
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"checkpoint-sync:{label}",
    )
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    current_branch = next((item for item in branches if item.get("is_current")), None)
    if current_branch is None:
        raise RuntimeError("campaign has no current branch")
    snapshot = await client.domain(
        "snapshot_create",
        {
            "campaign_id": campaign_id,
            "label": label,
            "expected_revision": synced["campaign_revision"],
            "expected_head_snapshot_id": current_branch.get("head_snapshot_id") or "",
            "idempotency_key": _mutation_key(run_id, "snapshot", label),
        },
    )
    verification = await client.domain(
        "snapshot_query",
        {
            "campaign_id": campaign_id,
            "view": "verify",
            "payload": {"slot": snapshot["slot"]},
        },
    )
    if not verification.get("valid"):
        raise RuntimeError(f"checkpoint slot {snapshot['slot']} failed integrity verification")
    return {
        "sync": synced,
        "snapshot": snapshot,
        "verification": verification,
        "manifest": await _manifest_get(client, campaign_id),
    }


def _party_member(actor: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    actor_id = str(actor["id"])
    sheet = dict(actor["sheet"])
    progression = dict(sheet["progression"])
    hp = dict(sheet["combat"]["hp"])
    return {
        "actor_id": actor_id,
        "name": str(actor["name"]),
        "status": str(selection.get("status") or "active"),
        "source": str(selection["source"]),
        "source_asset_path": str(selection.get("source_asset_path") or ""),
        "level": int(progression["level"]),
        "xp": int(progression["xp"]),
        "hit_points": {
            "current": int(hp["value"]),
            "maximum": int(hp["max"]),
            "temporary": int(hp["temp"]),
        },
        "resources": deepcopy(dict(sheet.get("resources") or {})),
        "equipment": sorted(str(item["id"]) for item in sheet["inventory"]["items"]),
        "knowledge_scope_actor_id": actor_id,
    }


async def _register_party(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    selections: list[dict[str, Any]],
) -> dict[str, Any]:
    if not selections:
        raise ValueError("register-party requires at least one --party-member-json")
    selected_ids = [str(item.get("actor_id") or "") for item in selections]
    if any(not item for item in selected_ids) or len(selected_ids) != len(set(selected_ids)):
        raise ValueError("party actor_ids must be non-empty and unique")
    for item in selections:
        if item.get("source") not in {"pregen", "generated", "replacement"}:
            raise ValueError("party member source must be pregen, generated, or replacement")
    current = await _manifest_get(client, campaign_id)
    manifest = deepcopy(current["manifest"])
    selected_size = manifest["party"]["selected_size"]
    if selected_size is None:
        raise RuntimeError("party size still requires explicit DM review")
    if len(selections) != selected_size:
        raise ValueError(
            f"register-party requires exactly the selected maximum of {selected_size} actors"
        )
    members = []
    for selection in selections:
        actor = await client.domain(
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": str(selection["actor_id"])},
            },
        )
        if actor.get("campaign_id") != campaign_id or actor.get("character_type") != "pc":
            raise ValueError("every registered party member must be a PC in this campaign")
        members.append(_party_member(actor, selection))
    members.sort(key=lambda item: (item["source"] != "pregen", item["actor_id"]))
    manifest["party"]["members"] = members
    replaced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="replace",
        run_id=run_id,
        identity="register-party",
        payload={"manifest": manifest},
    )
    return await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity="register-party-sync",
    ) | {"replace": replaced}


async def _advance_scene(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    scene_id: str,
    objective: str,
    mark_visited: bool,
    reachable_scene_ids: list[str],
    excluded_scenes: list[dict[str, Any]],
) -> dict[str, Any]:
    if not scene_id:
        raise ValueError("advance-scene requires --scene-id")
    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    if scene.get("redacted") or str(scene.get("scene_id") or "") != scene_id:
        raise RuntimeError("scene is redacted or does not belong to this campaign")
    current = await _manifest_get(client, campaign_id)
    manifest = deepcopy(current["manifest"])
    module_id = str(scene["module_id"])
    if module_id not in manifest["module_ids"]:
        raise RuntimeError("scene module is not declared by the playthrough manifest")
    manifest["current"] = {
        "module_id": module_id,
        "chapter_id": str(scene.get("chapter_id") or ""),
        "chapter_title": str(scene.get("chapter") or ""),
        "scene_id": scene_id,
        "scene_title": str(scene.get("title") or ""),
        "objective": objective.strip(),
    }
    traversal = manifest["traversal"]
    reachable = list(
        dict.fromkeys(
            [
                *traversal["reachable_scene_ids"],
                scene_id,
                *(str(item) for item in reachable_scene_ids),
            ]
        )
    )
    traversal["reachable_scene_ids"] = reachable
    if mark_visited:
        traversal["visited_scene_ids"] = list(
            dict.fromkeys([*traversal["visited_scene_ids"], scene_id])
        )
    exclusions = {str(item["scene_id"]): item for item in traversal["excluded_scenes"]}
    for item in excluded_scenes:
        excluded_id = str(item.get("scene_id") or "")
        if not excluded_id or not str(item.get("reason") or ""):
            raise ValueError("excluded scenes require scene_id and reason")
        if excluded_id in traversal["visited_scene_ids"]:
            raise ValueError("a visited scene cannot be excluded")
        exclusions[excluded_id] = deepcopy(item)
    traversal["excluded_scenes"] = list(exclusions.values())
    manifest["status"] = (
        "in_progress" if manifest["status"] in {"ready", "in_progress"} else manifest["status"]
    )
    return await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="replace",
        run_id=run_id,
        identity=f"advance-scene:{scene_id}:{mark_visited}",
        payload={"manifest": manifest},
    )


async def _branch_from_snapshot(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    initial_phase: str,
    snapshot_slot: int | None,
    branch_name: str,
    checkpoint_label: str,
) -> dict[str, Any]:
    if initial_phase == "combat":
        raise RuntimeError("branch-from-snapshot cannot run during active combat")
    if snapshot_slot is None or snapshot_slot < 1 or not branch_name.strip():
        raise ValueError(
            "branch-from-snapshot requires a positive --snapshot-slot and --branch-name"
        )
    snapshots = await client.domain(
        "snapshot_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    target = next(
        (item for item in snapshots if int(item.get("slot", 0) or 0) == snapshot_slot),
        None,
    )
    if target is None:
        raise LookupError(f"snapshot slot {snapshot_slot} does not exist")
    verification = await client.domain(
        "snapshot_query",
        {
            "campaign_id": campaign_id,
            "view": "verify",
            "payload": {"slot": snapshot_slot},
        },
    )
    if verification.get("valid") is not True:
        raise RuntimeError(f"snapshot slot {snapshot_slot} failed verification")
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    source_branch = next((item for item in branches if item.get("is_current")), None)
    if source_branch is None:
        raise RuntimeError("campaign has no current branch")
    phase_changes = []
    campaign = await _campaign(client, campaign_id)
    if initial_phase != "lobby":
        phase_changes.append(
            _facade_value(
                await client.core(
                    "game_phase",
                    {
                        "campaign_id": campaign_id,
                        "action": "set",
                        "tool_profile": "lobby",
                        "expected_revision": campaign["revision"],
                        "branch_id": source_branch["id"],
                        "idempotency_key": _mutation_key(
                            run_id,
                            "phase",
                            f"branch-from-snapshot-enter-lobby:{snapshot_slot}",
                        ),
                    },
                )
            )
        )
        await client.open(campaign_id)
        await client.load("lobby.campaign")
    source_checkpoint = await _checkpoint(
        client,
        campaign_id=campaign_id,
        run_id=f"{run_id}-source",
        label=(
            f"Preserve source branch before forking snapshot slot {snapshot_slot}: "
            f"{branch_name.strip()}"
        ),
    )
    campaign = await _campaign(client, campaign_id)
    created = await client.domain(
        "branch_change",
        {
            "campaign_id": campaign_id,
            "action": "create",
            "payload": {
                "name": branch_name.strip(),
                "from_snapshot_id": str(target["id"]),
                "checkout": True,
            },
            "expected_revision": campaign["revision"],
            "expected_branch_id": str(source_branch["id"]),
            "idempotency_key": _mutation_key(
                run_id, "branch-from-snapshot", f"{snapshot_slot}:{branch_name.strip()}"
            ),
        },
    )
    restored_campaign = await _campaign(client, campaign_id)
    restored_phase = _campaign_phase(restored_campaign)
    if restored_phase == "combat":
        raise RuntimeError("selected snapshot unexpectedly restored active combat")
    await client.open(campaign_id)
    await client.load(*_phase_groups(restored_phase))
    checkpoint = await _checkpoint(
        client,
        campaign_id=campaign_id,
        run_id=run_id,
        label=(
            checkpoint_label.strip()
            or f"Branch {branch_name.strip()} restored from snapshot slot {snapshot_slot}"
        ),
    )
    return {
        "source_branch": source_branch,
        "source_head_snapshot_id": source_branch.get("head_snapshot_id"),
        "source_checkpoint": source_checkpoint,
        "target_snapshot": target,
        "target_verification": verification,
        "created_branch": created,
        "phase_changes": phase_changes,
        "restored_phase": restored_phase,
        "checkpoint": checkpoint,
    }


async def _resolve_check(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    actor_id: str,
    kind: str,
    ability: str,
    dc: int | None,
    proficient: bool,
    advantage: bool = False,
    disadvantage: bool = False,
    knowledge_actor_ids: list[str],
    success_knowledge: str,
    failure_knowledge: str,
) -> dict[str, Any]:
    if not all((scene_id, location_key, source_excerpt, actor_id, kind, ability)):
        raise ValueError(
            "resolve-check requires scene, location, excerpt, actor, kind, and ability"
        )
    if dc is None or dc < 0:
        raise ValueError("resolve-check requires a non-negative --check-dc")
    if kind not in {"ability", "check", "save", "death_save"}:
        raise ValueError("resolve-check kind is not supported by character_check")
    if advantage and disadvantage:
        raise ValueError("resolve-check cannot apply advantage and disadvantage together")
    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    exact_ref = _validate_source_ref(scene, source_ref, excerpt=source_excerpt)
    location_keys = {str(item.get("key") or "") for item in _scene_locations(scene)}
    if location_key not in location_keys:
        raise ValueError("resolve-check location is not present in the scene atlas")
    actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": actor_id}},
    )
    if actor.get("campaign_id") != campaign_id:
        raise ValueError("resolve-check actor does not belong to the campaign")
    progress_rows = await client.domain(
        "module_query",
        {"campaign_id": campaign_id, "view": "progress"},
    )
    progress_before = next(
        (item for item in progress_rows if item.get("scene_id") == scene_id),
        None,
    )
    progress_matches = _matching_check_progress(
        progress_before,
        location_key=location_key,
        actor_id=actor_id,
        kind=kind,
        ability=ability,
        dc=dc,
        advantage=advantage,
        disadvantage=disadvantage,
        source_ref=exact_ref,
    )
    if progress_matches:
        progress = deepcopy(progress_before)
    else:
        progress = await client.domain(
            "module_set_progress",
            {
                "campaign_id": campaign_id,
                "scene_id": scene_id,
                "status": "active",
                "progress": max(_scene_progress_percent(progress_before), 50),
                "state": {
                    **deepcopy(dict((progress_before or {}).get("state") or {})),
                    "full_playthrough_check": {
                        "run_id": run_id,
                        "actor_id": actor_id,
                        "kind": kind,
                        "ability": ability,
                        "dc": dc,
                        "advantage": advantage,
                        "disadvantage": disadvantage,
                        "source_ref": exact_ref,
                    },
                },
                "current_location_key": location_key,
                "expected_state_version": int((progress_before or {}).get("state_version", 0) or 0),
                "idempotency_key": _mutation_key(
                    run_id,
                    "scene-progress",
                    f"{scene_id}:{kind}:{ability}:{actor_id}",
                ),
            },
        )
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    campaign = await _campaign(client, campaign_id)
    recovered = _recover_committed_check(
        campaign,
        progress_matches=progress_matches,
        actor_id=actor_id,
        kind=kind,
        dc=dc,
    )
    if recovered is None:
        settled = await client.domain(
            "character_check",
            {
                "campaign_id": campaign_id,
                "actor_id": actor_id,
                "kind": kind,
                "ability": ability,
                "dc": dc,
                "proficient": proficient,
                "advantage": advantage,
                "disadvantage": disadvantage,
                "branch_id": str(branch["id"]),
                "expected_revision": campaign["revision"],
                "idempotency_key": _mutation_key(
                    run_id,
                    "character-check",
                    f"{scene_id}:{kind}:{ability}:{actor_id}",
                ),
            },
        )
        check_result = _committed_check_result(settled)
    else:
        check_result = recovered
    success = bool(check_result.get("success"))
    proposition = (success_knowledge.strip() if success else failure_knowledge.strip()) or (
        f"{actor['name']} {'succeeded' if success else 'failed'} on the "
        f"DC {dc} {ability.title()} ({kind.title()}) check."
    )
    recipients = list(dict.fromkeys([actor_id, *knowledge_actor_ids]))
    campaign = await _campaign(client, campaign_id)
    committed = await client.domain(
        "continuity_commit",
        {
            "campaign_id": campaign_id,
            "payload": {
                "event": {
                    "summary": (
                        f"{actor['name']} {'succeeded' if success else 'failed'} on "
                        f"the source-cited {kind} check at {location_key}."
                    ),
                    "event_type": "ability_check",
                    "audience_scope": "party",
                    "payload": {
                        "scene_id": scene_id,
                        "location_key": location_key,
                        "kind": kind,
                        "ability": ability,
                        "dc": dc,
                        "advantage": advantage,
                        "disadvantage": disadvantage,
                        "success": success,
                        "source_excerpt": source_excerpt,
                        "source_ref": exact_ref,
                    },
                },
                "actor_knowledge": [
                    {
                        "actor_id": recipient,
                        "knowledge_key": _check_knowledge_key(
                            run_id,
                            scene_id,
                            kind,
                            ability,
                            actor_id,
                        ),
                        "proposition": proposition,
                        "disclosure_scope": "owner",
                    }
                    for recipient in recipients
                ],
                "snapshot": {"label": (f"Full playthrough check: {kind} at {location_key}")},
                "branch_id": str(branch["id"]),
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id, "continuity", f"{scene_id}:{kind}:{ability}:{actor_id}"
            ),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"resolve-check-sync:{scene_id}:{kind}:{ability}:{actor_id}",
    )
    return {
        "scene": {
            "scene_id": scene_id,
            "location_key": location_key,
            "source_ref": exact_ref,
        },
        "actor": {"id": actor_id, "name": actor["name"]},
        "progress": progress,
        "check": check_result,
        "check_recovered": recovered is not None,
        "knowledge_actor_ids": recipients,
        "continuity": committed,
        "sync": synced,
    }


async def _record_event(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    event_type: str,
    summary: str,
    knowledge: str,
    knowledge_actor_ids: list[str],
    progress_percent: int | None,
) -> dict[str, Any]:
    if not all((scene_id, location_key, source_excerpt, event_type, summary)):
        raise ValueError("record-event requires scene, location, excerpt, event type, and summary")
    if bool(knowledge.strip()) != bool(knowledge_actor_ids):
        raise ValueError(
            "record-event knowledge text and knowledge actor ids must be provided together"
        )
    if progress_percent is not None and not 0 <= progress_percent <= 100:
        raise ValueError("record-event progress percent must be between 0 and 100")
    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    exact_ref = _validate_source_ref(scene, source_ref, excerpt=source_excerpt)
    location_keys = {str(item.get("key") or "") for item in _scene_locations(scene)}
    if location_key not in location_keys:
        raise ValueError("record-event location is not present in the scene atlas")
    progress_rows = await client.domain(
        "module_query",
        {"campaign_id": campaign_id, "view": "progress"},
    )
    progress_before = next(
        (item for item in progress_rows if item.get("scene_id") == scene_id),
        None,
    )
    state = deepcopy(dict((progress_before or {}).get("state") or {}))
    events = deepcopy(dict(state.get("full_playthrough_events") or {}))
    event_key = _token(run_id, length=24)
    events[event_key] = {
        "event_type": event_type,
        "summary": summary.strip(),
        "source_ref": exact_ref,
    }
    state["full_playthrough_events"] = events
    progress = await client.domain(
        "module_set_progress",
        {
            "campaign_id": campaign_id,
            "scene_id": scene_id,
            "status": "completed" if progress_percent == 100 else "active",
            "progress": (
                progress_percent
                if progress_percent is not None
                else _scene_progress_percent(progress_before)
            ),
            "state": state,
            "current_location_key": location_key,
            "expected_state_version": int((progress_before or {}).get("state_version", 0) or 0),
            "idempotency_key": _mutation_key(
                run_id, "scene-event-progress", f"{scene_id}:{event_type}"
            ),
        },
    )
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    campaign = await _campaign(client, campaign_id)
    committed = await client.domain(
        "continuity_commit",
        {
            "campaign_id": campaign_id,
            "payload": {
                "event": {
                    "summary": summary.strip(),
                    "event_type": event_type,
                    "audience_scope": "party",
                    "payload": {
                        "scene_id": scene_id,
                        "location_key": location_key,
                        "source_excerpt": source_excerpt,
                        "source_ref": exact_ref,
                    },
                },
                "actor_knowledge": [
                    {
                        "actor_id": actor_id,
                        "knowledge_key": (
                            f"playthrough.{_token(run_id)}.{_token(scene_id)}.{_token(event_type)}"
                        ),
                        "proposition": knowledge.strip(),
                        "disclosure_scope": "owner",
                    }
                    for actor_id in list(dict.fromkeys(knowledge_actor_ids))
                ],
                "snapshot": {"label": f"Full playthrough event: {summary.strip()}"},
                "branch_id": str(branch["id"]),
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id, "continuity-event", f"{scene_id}:{event_type}"
            ),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"record-event-sync:{scene_id}:{event_type}",
    )
    return {
        "scene": {
            "scene_id": scene_id,
            "location_key": location_key,
            "source_ref": exact_ref,
        },
        "progress": progress,
        "continuity": committed,
        "knowledge_actor_ids": list(dict.fromkeys(knowledge_actor_ids)),
        "sync": synced,
    }


def _dice_result(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value.get("result") or value)
    total = result.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
        raise RuntimeError("server dice roll did not return a positive integer total")
    return result


async def _apply_source_damage(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    actor_id: str,
    expression: str,
    damage_type: str,
    reason: str,
    half_damage: bool,
    knock_prone: bool,
    knowledge_actor_ids: list[str],
) -> dict[str, Any]:
    if not all(
        (
            scene_id,
            location_key,
            source_excerpt,
            actor_id,
            expression,
            damage_type,
            reason,
        )
    ):
        raise ValueError(
            "apply-damage requires scene, location, excerpt, actor, expression, "
            "damage type, and reason"
        )
    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    exact_ref = _validate_source_ref(scene, source_ref, excerpt=source_excerpt)
    location_keys = {str(item.get("key") or "") for item in _scene_locations(scene)}
    if location_key not in location_keys:
        raise ValueError("apply-damage location is not present in the scene atlas")
    actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": actor_id}},
    )
    if actor.get("campaign_id") != campaign_id:
        raise ValueError("apply-damage actor does not belong to the campaign")
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    campaign = await _campaign(client, campaign_id)
    rolled = await client.domain(
        "dnd_dice_roll",
        {
            "campaign_id": campaign_id,
            "expression": expression,
            "branch_id": str(branch["id"]),
            "expected_campaign_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id, "source-damage-roll", f"{scene_id}:{actor_id}:{expression}"
            ),
        },
    )
    roll_result = _dice_result(rolled)
    rolled_amount = int(roll_result["total"])
    amount = rolled_amount // 2 if half_damage else rolled_amount
    damaged = await client.domain(
        "character_state_change",
        {
            "character_id": actor_id,
            "action": "damage",
            "payload": {
                "parts": [{"amount": amount, "damage_type": damage_type}],
            },
            "expected_revision": actor["revision"],
            "idempotency_key": _mutation_key(
                run_id, "source-damage", f"{scene_id}:{actor_id}:{amount}"
            ),
        },
    )
    character_after = dict(damaged["character"])
    conditions = {
        str(item).casefold()
        for item in dict(character_after.get("sheet") or {}).get("conditions", [])
    }
    hp = int(
        dict(dict(character_after.get("sheet") or {}).get("combat", {}).get("hp") or {}).get(
            "value", 0
        )
        or 0
    )
    prone_result = None
    if knock_prone and hp > 0 and "prone" not in conditions:
        prone_result = await client.domain(
            "character_state_change",
            {
                "character_id": actor_id,
                "action": "knock_prone",
                "expected_revision": character_after["revision"],
                "idempotency_key": _mutation_key(
                    run_id, "source-damage-prone", f"{scene_id}:{actor_id}"
                ),
            },
        )
        character_after = dict(prone_result["character"])
    recipients = list(dict.fromkeys([actor_id, *knowledge_actor_ids]))
    campaign = await _campaign(client, campaign_id)
    committed = await client.domain(
        "continuity_commit",
        {
            "campaign_id": campaign_id,
            "payload": {
                "event": {
                    "summary": (
                            f"{actor['name']} took {amount} {damage_type} damage"
                            f"{f' (half of {rolled_amount})' if half_damage else ''}: "
                            f"{reason.strip()}"
                    ),
                    "event_type": "environmental_damage",
                    "audience_scope": "party",
                    "payload": {
                        "scene_id": scene_id,
                        "location_key": location_key,
                        "actor_id": actor_id,
                        "damage_expression": expression,
                        "damage_roll": roll_result,
                        "damage_type": damage_type,
                        "amount": amount,
                        "half_damage": half_damage,
                        "knock_prone": knock_prone,
                        "reason": reason.strip(),
                        "source_excerpt": source_excerpt,
                        "source_ref": exact_ref,
                    },
                },
                "actor_knowledge": [
                    {
                        "actor_id": recipient,
                        "knowledge_key": (
                            f"playthrough.{_token(run_id)}.{_token(scene_id)}."
                            f"{_token(actor_id)}.environmental_damage"
                        ),
                        "proposition": (
                            f"{actor['name']} took {amount} {damage_type} damage "
                            f"from {reason.strip()}."
                        ),
                        "disclosure_scope": "owner",
                    }
                    for recipient in recipients
                ],
                "snapshot": {
                    "label": (
                        f"Full playthrough environmental damage: {actor['name']} at "
                        f"{location_key}"
                    )
                },
                "branch_id": str(branch["id"]),
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id, "source-damage-continuity", f"{scene_id}:{actor_id}"
            ),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"source-damage-sync:{scene_id}:{actor_id}",
    )
    return {
        "scene": {
            "scene_id": scene_id,
            "location_key": location_key,
            "source_ref": exact_ref,
        },
        "actor": {"id": actor_id, "name": actor["name"]},
        "roll": rolled,
        "damage": damaged,
        "prone": prone_result,
        "character": character_after,
        "knowledge_actor_ids": recipients,
        "continuity": committed,
        "sync": synced,
    }


async def _stand_after_source_event(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    actor_id: str,
    knowledge_actor_ids: list[str],
    reason: str = "",
) -> dict[str, Any]:
    if not all((scene_id, location_key, actor_id)):
        raise ValueError("stand-up requires scene, location, and actor")
    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    if bool(source_excerpt) != bool(source_ref):
        raise ValueError("stand-up source excerpt and source ref must be supplied together")
    exact_ref = (
        _validate_source_ref(scene, source_ref, excerpt=source_excerpt)
        if source_ref is not None
        else None
    )
    if location_key not in {
        str(item.get("key") or "") for item in _scene_locations(scene)
    }:
        raise ValueError("stand-up location is not present in the scene atlas")
    actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": actor_id}},
    )
    if actor.get("campaign_id") != campaign_id:
        raise ValueError("stand-up actor does not belong to the campaign")
    stood = await client.domain(
        "character_state_change",
        {
            "character_id": actor_id,
            "action": "stand",
            "expected_revision": actor["revision"],
            "idempotency_key": _mutation_key(
                run_id, "source-event-stand", f"{scene_id}:{actor_id}"
            ),
        },
    )
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    recipients = list(dict.fromkeys([actor_id, *knowledge_actor_ids]))
    event_summary = reason.strip() or (
        f"{actor['name']} stood after the source-cited Prone result at {location_key}."
        if exact_ref is not None
        else f"{actor['name']} stood from Prone at {location_key}."
    )
    knowledge = reason.strip() or (
        f"{actor['name']} recovered from the source-cited fall and stood at "
        f"{location_key}."
        if exact_ref is not None
        else f"{actor['name']} recovered from Prone and stood at {location_key}."
    )
    campaign = await _campaign(client, campaign_id)
    committed = await client.domain(
        "continuity_commit",
        {
            "campaign_id": campaign_id,
            "payload": {
                "event": {
                    "summary": event_summary,
                    "event_type": "stand",
                    "audience_scope": "party",
                    "payload": {
                        "scene_id": scene_id,
                        "location_key": location_key,
                        "actor_id": actor_id,
                        **(
                            {
                                "source_excerpt": source_excerpt,
                                "source_ref": exact_ref,
                            }
                            if exact_ref is not None
                            else {}
                        ),
                    },
                },
                "actor_knowledge": [
                    {
                        "actor_id": recipient,
                        "knowledge_key": (
                            f"playthrough.{_token(run_id)}.{_token(scene_id)}."
                            f"{_token(actor_id)}.stand"
                        ),
                        "proposition": knowledge,
                        "disclosure_scope": "owner",
                    }
                    for recipient in recipients
                ],
                "snapshot": {
                    "label": f"Full playthrough stand: {actor['name']} at {location_key}"
                },
                "branch_id": str(branch["id"]),
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id, "source-event-stand-continuity", f"{scene_id}:{actor_id}"
            ),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"source-event-stand-sync:{scene_id}:{actor_id}",
    )
    return {
        "scene": {
            "scene_id": scene_id,
            "location_key": location_key,
            "source_ref": exact_ref,
        },
        "actor": {"id": actor_id, "name": actor["name"]},
        "stand": stood,
        "knowledge_actor_ids": recipients,
        "continuity": committed,
        "sync": synced,
    }


async def _short_rest(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    members: list[dict[str, Any]],
    start_clock: dict[str, Any] | None,
    duration_minutes: int,
    reason: str,
) -> dict[str, Any]:
    if duration_minutes < 60:
        raise ValueError("short-rest requires at least 60 minutes")
    if not members or not reason.strip():
        raise ValueError("short-rest requires members and --rest-reason")
    allowed_fields = {"actor_id", "arcane_recovery", "hit_dice_spends"}
    normalized: list[dict[str, Any]] = []
    for index, member in enumerate(members):
        if not isinstance(member, dict):
            raise ValueError(f"rest-member-json[{index}] must be an object")
        unexpected = set(member) - allowed_fields
        actor_id = str(member.get("actor_id") or "")
        arcane_recovery = member.get("arcane_recovery")
        hit_dice_spends = member.get("hit_dice_spends")
        if unexpected or not actor_id or (
            arcane_recovery is not None and not isinstance(arcane_recovery, dict)
        ) or (
            hit_dice_spends is not None and not isinstance(hit_dice_spends, list)
        ):
            raise ValueError(
                "short-rest members accept actor_id, optional arcane_recovery, "
                "and optional hit_dice_spends only"
            )
        normalized_spends: list[dict[str, Any]] = []
        for spend_index, spend in enumerate(hit_dice_spends or []):
            if (
                not isinstance(spend, dict)
                or set(spend) != {"key", "count"}
                or not str(spend.get("key") or "")
                or isinstance(spend.get("count"), bool)
                or not isinstance(spend.get("count"), int)
                or int(spend["count"]) <= 0
            ):
                raise ValueError(
                    f"rest-member-json[{index}].hit_dice_spends[{spend_index}] "
                    "must contain a key and positive integer count"
                )
            normalized_spends.append(
                {"key": str(spend["key"]), "count": int(spend["count"])}
            )
        normalized.append(
            {
                "actor_id": actor_id,
                "arcane_recovery": deepcopy(arcane_recovery or {}),
                "hit_dice_spends": normalized_spends,
            }
        )
    actor_ids = [item["actor_id"] for item in normalized]
    if len(actor_ids) != len(set(actor_ids)):
        raise ValueError("short-rest member actor ids must be unique")
    actors = []
    for actor_id in actor_ids:
        actor = await client.domain(
            "character_query",
            {"view": "get", "payload": {"character_id": actor_id}},
        )
        if actor.get("campaign_id") != campaign_id:
            raise ValueError("every short-rest actor must belong to the campaign")
        actors.append(actor)
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    campaign = await _campaign(client, campaign_id)
    clock_set = None
    if not dict(dict(campaign.get("state") or {}).get("world_time") or {}):
        if not isinstance(start_clock, dict):
            raise ValueError(
                "short-rest requires --rest-start-clock-json when the campaign clock is unset"
            )
        clock_set = await client.domain(
            "campaign_change",
            {
                "campaign_id": campaign_id,
                "action": "clock_set",
                "payload": {
                    "day": start_clock.get("day"),
                    "hour": start_clock.get("hour", 0),
                    "minute": start_clock.get("minute", 0),
                    "label": str(start_clock.get("label") or ""),
                },
                "branch_id": str(branch["id"]),
                "expected_revision": campaign["revision"],
                "idempotency_key": _mutation_key(run_id, "short-rest-clock-set", reason),
            },
        )
    elif start_clock is not None:
        raise ValueError("short-rest start clock must be omitted after the clock is set")
    campaign = await _campaign(client, campaign_id)
    clock_advanced = await client.domain(
        "campaign_change",
        {
            "campaign_id": campaign_id,
            "action": "clock_advance",
            "payload": {"period": "minute", "count": duration_minutes},
            "branch_id": str(branch["id"]),
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id, "short-rest-clock-advance", str(duration_minutes)
            ),
        },
    )
    rested = []
    actor_by_id = {str(actor["id"]): actor for actor in actors}
    for member in normalized:
        actor_id = member["actor_id"]
        payload: dict[str, Any] = {"rest_type": "short_rest"}
        if member["arcane_recovery"]:
            payload["arcane_recovery"] = member["arcane_recovery"]
        if member["hit_dice_spends"]:
            payload["hit_dice_spends"] = member["hit_dice_spends"]
        result = await client.domain(
            "character_state_change",
            {
                "character_id": actor_id,
                "action": "rest",
                "payload": payload,
                "expected_revision": actor_by_id[actor_id]["revision"],
                "idempotency_key": _mutation_key(run_id, "short-rest-actor", actor_id),
            },
        )
        if result.get("status") != "committed":
            raise RuntimeError(f"short rest for {actor_id} did not commit")
        rested.append(result)
    campaign = await _campaign(client, campaign_id)
    committed = await client.domain(
        "continuity_commit",
        {
            "campaign_id": campaign_id,
            "payload": {
                "event": {
                    "summary": reason.strip(),
                    "event_type": "short_rest",
                    "audience_scope": "party",
                    "payload": {
                        "member_ids": actor_ids,
                        "member_choices": normalized,
                        "duration_minutes": duration_minutes,
                        "clock_set": clock_set is not None,
                    },
                },
                "actor_knowledge": [
                    {
                        "actor_id": actor_id,
                        "knowledge_key": (
                            f"playthrough.{_token(run_id)}.{_token(actor_id)}.short_rest"
                        ),
                        "proposition": reason.strip(),
                        "disclosure_scope": "owner",
                    }
                    for actor_id in actor_ids
                ],
                "snapshot": {"label": f"Full playthrough short rest: {reason.strip()}"},
                "branch_id": str(branch["id"]),
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(run_id, "short-rest-continuity", reason),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity="short-rest-sync",
    )
    return {
        "member_ids": actor_ids,
        "clock_set": clock_set,
        "clock_advanced": clock_advanced,
        "rests": rested,
        "continuity": committed,
        "sync": synced,
    }


async def _use_activity(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    scene_id: str,
    location_key: str,
    actor_id: str,
    activity_id: str,
    declaration: dict[str, Any] | None,
    reason: str,
    knowledge_actor_ids: list[str],
) -> dict[str, Any]:
    if not all((scene_id, location_key, actor_id, activity_id, reason.strip())):
        raise ValueError(
            "use-activity requires scene, location, actor, activity id, and reason"
        )
    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    if location_key not in {
        str(item.get("key") or "") for item in _scene_locations(scene)
    }:
        raise ValueError("use-activity location is not present in the scene atlas")
    actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": actor_id}},
    )
    if actor.get("campaign_id") != campaign_id:
        raise ValueError("use-activity actor does not belong to the campaign")
    payload: dict[str, Any] = {"activity_id": activity_id}
    if declaration is not None:
        payload["declaration"] = declaration
    acted = await client.domain(
        "character_action",
        {
            "character_id": actor_id,
            "action": "use_activity",
            "payload": payload,
            "expected_revision": actor["revision"],
            "idempotency_key": _mutation_key(
                run_id, "play-activity", f"{scene_id}:{actor_id}:{activity_id}"
            ),
        },
    )
    if acted.get("status") != "committed":
        raise RuntimeError(
            f"activity {activity_id} did not settle automatically: {acted.get('status')}"
        )
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    recipients = list(dict.fromkeys([actor_id, *knowledge_actor_ids]))
    campaign = await _campaign(client, campaign_id)
    core_effect = dict(dict(acted.get("result") or {}).get("core_effect") or {})
    committed = await client.domain(
        "continuity_commit",
        {
            "campaign_id": campaign_id,
            "payload": {
                "event": {
                    "summary": reason.strip(),
                    "event_type": "character_activity",
                    "audience_scope": "party",
                    "payload": {
                        "scene_id": scene_id,
                        "location_key": location_key,
                        "actor_id": actor_id,
                        "activity_id": activity_id,
                        "declaration": deepcopy(declaration or {}),
                        "core_effect": core_effect,
                        "random_stream_receipt": deepcopy(
                            acted.get("random_stream_receipt")
                        ),
                    },
                },
                "actor_knowledge": [
                    {
                        "actor_id": recipient,
                        "knowledge_key": (
                            f"playthrough.{_token(run_id)}.{_token(scene_id)}."
                            f"{_token(actor_id)}.{_token(activity_id)}"
                        ),
                        "proposition": reason.strip(),
                        "disclosure_scope": "owner",
                    }
                    for recipient in recipients
                ],
                "snapshot": {
                    "label": (
                        f"Full playthrough activity: {actor['name']} used "
                        f"{activity_id} at {location_key}"
                    )
                },
                "branch_id": str(branch["id"]),
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id,
                "play-activity-continuity",
                f"{scene_id}:{actor_id}:{activity_id}",
            ),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"play-activity-sync:{scene_id}:{actor_id}:{activity_id}",
    )
    return {
        "scene_id": scene_id,
        "location_key": location_key,
        "actor": {"id": actor_id, "name": actor["name"]},
        "activity_id": activity_id,
        "action": acted,
        "knowledge_actor_ids": recipients,
        "continuity": committed,
        "sync": synced,
    }


async def _long_rest(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    members: list[dict[str, Any]],
    start_clock: dict[str, Any] | None,
    duration_minutes: int,
    reason: str,
) -> dict[str, Any]:
    if duration_minutes < 480:
        raise ValueError("long-rest requires at least 480 minutes")
    if not members or not reason.strip():
        raise ValueError("long-rest requires members and --rest-reason")
    allowed_fields = {
        "actor_id",
        "prepared_spell_ids",
        "hit_dice_recovery",
        "food_and_drink",
    }
    normalized: list[dict[str, Any]] = []
    actors: list[dict[str, Any]] = []
    for index, member in enumerate(members):
        if not isinstance(member, dict):
            raise ValueError(f"rest-member-json[{index}] must be an object")
        unexpected = set(member) - allowed_fields
        actor_id = str(member.get("actor_id") or "")
        prepared_ids = member.get("prepared_spell_ids")
        hit_dice_recovery = member.get("hit_dice_recovery")
        food_and_drink = member.get("food_and_drink", False)
        if (
            unexpected
            or not actor_id
            or (prepared_ids is not None and not isinstance(prepared_ids, list))
            or (
                hit_dice_recovery is not None
                and not isinstance(hit_dice_recovery, dict)
            )
            or not isinstance(food_and_drink, bool)
        ):
            raise ValueError(
                "long-rest members accept actor_id, optional prepared_spell_ids, "
                "optional hit_dice_recovery, and optional food_and_drink only"
            )
        actor = await client.domain(
            "character_query",
            {"view": "get", "payload": {"character_id": actor_id}},
        )
        if actor.get("campaign_id") != campaign_id:
            raise ValueError("every long-rest actor must belong to the campaign")
        actors.append(actor)
        normalized.append(
            {
                "actor_id": actor_id,
                "prepared_spell_ids": (
                    list(prepared_ids) if prepared_ids is not None else None
                ),
                "hit_dice_recovery": deepcopy(hit_dice_recovery),
                "food_and_drink": food_and_drink,
            }
        )
    actor_ids = [item["actor_id"] for item in normalized]
    if len(actor_ids) != len(set(actor_ids)):
        raise ValueError("long-rest member actor ids must be unique")
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    campaign = await _campaign(client, campaign_id)
    clock_set = None
    if not dict(dict(campaign.get("state") or {}).get("world_time") or {}):
        if not isinstance(start_clock, dict):
            raise ValueError(
                "long-rest requires --rest-start-clock-json when the campaign clock is unset"
            )
        clock_set = await client.domain(
            "campaign_change",
            {
                "campaign_id": campaign_id,
                "action": "clock_set",
                "payload": {
                    "day": start_clock.get("day"),
                    "hour": start_clock.get("hour", 0),
                    "minute": start_clock.get("minute", 0),
                    "label": str(start_clock.get("label") or ""),
                },
                "branch_id": str(branch["id"]),
                "expected_revision": campaign["revision"],
                "idempotency_key": _mutation_key(run_id, "long-rest-clock-set", reason),
            },
        )
    elif start_clock is not None:
        raise ValueError("long-rest start clock must be omitted after the clock is set")
    campaign = await _campaign(client, campaign_id)
    party_members = []
    actor_by_id = {str(actor["id"]): actor for actor in actors}
    for member in normalized:
        party_member: dict[str, Any] = {
            "character_id": member["actor_id"],
            "expected_revision": actor_by_id[member["actor_id"]]["revision"],
            "food_and_drink": member["food_and_drink"],
        }
        if member["prepared_spell_ids"] is not None:
            party_member["prepared_spell_ids"] = member["prepared_spell_ids"]
        if member["hit_dice_recovery"] is not None:
            party_member["hit_dice_recovery"] = member["hit_dice_recovery"]
        party_members.append(party_member)
    rested = await client.domain(
        "campaign_change",
        {
            "campaign_id": campaign_id,
            "action": "party_rest",
            "payload": {
                "members": party_members,
                "duration_minutes": duration_minutes,
            },
            "branch_id": str(branch["id"]),
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(run_id, "long-rest-party", reason),
        },
    )
    if rested.get("status") != "committed":
        raise RuntimeError("long rest did not commit")
    campaign = await _campaign(client, campaign_id)
    committed = await client.domain(
        "continuity_commit",
        {
            "campaign_id": campaign_id,
            "payload": {
                "event": {
                    "summary": reason.strip(),
                    "event_type": "long_rest",
                    "audience_scope": "party",
                    "payload": {
                        "member_ids": actor_ids,
                        "member_choices": normalized,
                        "duration_minutes": duration_minutes,
                        "clock_set": clock_set is not None,
                    },
                },
                "actor_knowledge": [
                    {
                        "actor_id": actor_id,
                        "knowledge_key": (
                            f"playthrough.{_token(run_id)}.{_token(actor_id)}.long_rest"
                        ),
                        "proposition": reason.strip(),
                        "disclosure_scope": "owner",
                    }
                    for actor_id in actor_ids
                ],
                "snapshot": {"label": f"Full playthrough long rest: {reason.strip()}"},
                "branch_id": str(branch["id"]),
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(run_id, "long-rest-continuity", reason),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity="long-rest-sync",
    )
    return {
        "member_ids": actor_ids,
        "clock_set": clock_set,
        "rest": rested,
        "continuity": committed,
        "sync": synced,
    }


async def _award_experience(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    scene_id: str,
    source_ref: dict[str, Any] | None,
    actor_ids: list[str],
    amount: int | None,
    reason: str,
) -> dict[str, Any]:
    if not scene_id or not actor_ids or amount is None or amount <= 0 or not reason.strip():
        raise ValueError("award-xp requires scene, one or more actors, positive amount, and reason")
    if len(actor_ids) != len(set(actor_ids)):
        raise ValueError("award-xp actor ids must be unique")
    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    exact_ref = _validate_source_ref(scene, source_ref)
    actors = []
    for actor_id in actor_ids:
        actor = await client.domain(
            "character_query",
            {"view": "get", "payload": {"character_id": actor_id}},
        )
        if actor.get("campaign_id") != campaign_id:
            raise ValueError("award-xp actor does not belong to the campaign")
        actors.append(actor)
    campaign = await _campaign(client, campaign_id)
    awarded = await client.domain(
        "campaign_change",
        {
            "campaign_id": campaign_id,
            "action": "experience_award",
            "payload": {
                "awards": [
                    {
                        "character_id": actor["id"],
                        "amount": amount,
                        "expected_revision": actor["revision"],
                    }
                    for actor in actors
                ],
                "reason": reason.strip(),
                "source_ref": json.dumps(
                    exact_ref, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(run_id, "experience-award", f"{scene_id}:{amount}"),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"award-xp-sync:{scene_id}:{amount}",
    )
    return {
        "scene_id": scene_id,
        "source_ref": exact_ref,
        "award": awarded,
        "sync": synced,
    }


async def _start_play(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    initial_phase: str,
    scene_id: str,
    objective: str,
    reachable_scene_ids: list[str],
) -> dict[str, Any]:
    current = await _manifest_get(client, campaign_id)
    manifest = deepcopy(current["manifest"])
    ready = None
    phase_change = None
    if initial_phase == "lobby":
        manifest["status"] = "ready"
        ready = await _manifest_mutation(
            client,
            campaign_id=campaign_id,
            action="replace",
            run_id=run_id,
            identity="start-play-ready",
            payload={"manifest": manifest},
        )
        campaign = await _campaign(client, campaign_id)
        branches = await client.domain(
            "branch_query",
            {"campaign_id": campaign_id, "view": "list"},
        )
        branch = next((item for item in branches if item.get("is_current")), None)
        if branch is None:
            raise RuntimeError("campaign has no current branch")
        phase_change = _facade_value(
            await client.core(
                "game_phase",
                {
                    "campaign_id": campaign_id,
                    "action": "set",
                    "tool_profile": "play",
                    "expected_revision": campaign["revision"],
                    "branch_id": branch["id"],
                    "idempotency_key": _mutation_key(
                        run_id,
                        "phase",
                        f"start-play-r{campaign['revision']}",
                    ),
                },
            )
        )
    elif initial_phase != "play":
        raise RuntimeError("start-play cannot run during active combat")
    elif manifest["status"] not in {"ready", "in_progress"}:
        raise RuntimeError("play phase does not have a ready playthrough manifest")
    await client.open(campaign_id)
    await client.load("play.scene", "play.scene_control")
    scene = await _advance_scene(
        client,
        campaign_id=campaign_id,
        run_id=run_id,
        scene_id=scene_id,
        objective=objective,
        mark_visited=True,
        reachable_scene_ids=reachable_scene_ids,
        excluded_scenes=[],
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"start-play-sync:{scene_id}",
    )
    return {
        "ready": ready,
        "phase_change": phase_change,
        "scene": scene,
        "sync": synced,
    }


async def _configure_advancement(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    mode: str,
    initial_phase: str,
) -> dict[str, Any]:
    if mode not in {"xp", "milestone"}:
        raise ValueError("configure-advancement requires --advancement-mode")
    phase_changes: list[dict[str, Any]] = []
    branch_id = ""
    if initial_phase == "play":
        await client.load("play.scene")
        branches = await client.domain(
            "branch_query",
            {"campaign_id": campaign_id, "view": "list"},
        )
        branch = next((item for item in branches if item.get("is_current")), None)
        if branch is None:
            raise RuntimeError("campaign has no current branch")
        branch_id = str(branch["id"])
        campaign = await _campaign(client, campaign_id)
        phase_changes.append(
            _facade_value(
                await client.core(
                    "game_phase",
                    {
                        "campaign_id": campaign_id,
                        "action": "set",
                        "tool_profile": "lobby",
                        "expected_revision": campaign["revision"],
                        "branch_id": branch_id,
                        "idempotency_key": _mutation_key(
                            run_id,
                            "phase",
                            f"advancement-enter-lobby-r{campaign['revision']}",
                        ),
                    },
                )
            )
        )
        await client.open(campaign_id)
        await client.load("lobby.campaign")
    elif initial_phase != "lobby":
        raise RuntimeError("configure-advancement cannot run during active combat")
    campaign = await _campaign(client, campaign_id)
    configured = await client.domain(
        "campaign_change",
        {
            "campaign_id": campaign_id,
            "action": "advancement_configure",
            "payload": {"mode": mode},
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id, "advancement", f"{mode}:r{campaign['revision']}"
            ),
        },
    )
    if initial_phase == "play":
        campaign = await _campaign(client, campaign_id)
        phase_changes.append(
            _facade_value(
                await client.core(
                    "game_phase",
                    {
                        "campaign_id": campaign_id,
                        "action": "set",
                        "tool_profile": "play",
                        "expected_revision": campaign["revision"],
                        "branch_id": branch_id,
                        "idempotency_key": _mutation_key(
                            run_id,
                            "phase",
                            f"advancement-return-play-r{campaign['revision']}",
                        ),
                    },
                )
            )
        )
    return {"configured": configured, "phase_changes": phase_changes}


async def _refresh_module(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    initial_phase: str,
    source_path: Path | None,
    source_key: str,
    title: str,
) -> dict[str, Any]:
    if initial_phase != "play":
        raise RuntimeError("refresh-module requires the play phase")
    if source_path is None:
        raise ValueError("refresh-module requires --module-source-path")
    manifest_result = await _manifest_get(client, campaign_id)
    manifest = manifest_result["manifest"]
    old_module_id = str(manifest["current"].get("module_id") or "")
    if not old_module_id:
        raise ValueError("refresh-module requires a current manifest module")
    old_index = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "index",
            "payload": {"module_id": old_module_id},
        },
    )
    campaign = await _campaign(client, campaign_id)
    active_modules = dict(
        dict(dict(campaign.get("state") or {}).get("module_imports") or {}).get("active") or {}
    )
    if not source_key:
        source_key = next(
            (
                key
                for key, item in active_modules.items()
                if str(dict(item or {}).get("module_id") or "") == old_module_id
            ),
            "",
        )
    if not source_key:
        raise ValueError("refresh-module could not identify the logical module source key")
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    phase_changes = [
        _facade_value(
            await client.core(
                "game_phase",
                {
                    "campaign_id": campaign_id,
                    "action": "set",
                    "tool_profile": "lobby",
                    "expected_revision": campaign["revision"],
                    "branch_id": branch["id"],
                    "idempotency_key": _mutation_key(
                        run_id, "phase", f"refresh-enter-lobby-r{campaign['revision']}"
                    ),
                },
            )
        )
    ]
    await client.open(campaign_id)
    await client.load("lobby.campaign", "lobby.modules")
    staged = await client.domain(
        "module_import",
        {
            "campaign_id": campaign_id,
            "action": "stage",
            "payload": {
                "source_path": str(source_path.expanduser().resolve()),
                "source_key": source_key,
                "title": title.strip() or Path(source_path).stem,
            },
            "idempotency_key": _mutation_key(run_id, "module-refresh-stage", source_key),
        },
    )
    job_id = str(staged["job"]["id"])
    inspected = await client.domain(
        "module_import",
        {
            "campaign_id": campaign_id,
            "action": "inspect",
            "payload": {"job_id": job_id},
            "idempotency_key": _mutation_key(run_id, "module-refresh-inspect", job_id),
        },
    )
    preview = dict(inspected["preview"])
    if not preview.get("valid"):
        raise RuntimeError("; ".join(preview.get("errors") or ["module preview is invalid"]))
    validated = await client.domain(
        "module_import",
        {
            "campaign_id": campaign_id,
            "action": "validate",
            "payload": {"job_id": job_id},
            "idempotency_key": _mutation_key(run_id, "module-refresh-validate", job_id),
        },
    )
    if not validated["validation"]["valid"]:
        raise RuntimeError("module revision validation failed")
    ingested = await client.domain(
        "module_import",
        {
            "campaign_id": campaign_id,
            "action": "ingest",
            "payload": {"job_id": job_id},
            "idempotency_key": _mutation_key(run_id, "module-refresh-ingest", job_id),
        },
    )
    campaign = await _campaign(client, campaign_id)
    activated = await client.domain(
        "module_import",
        {
            "campaign_id": campaign_id,
            "action": "activate",
            "payload": {"job_id": job_id},
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(run_id, "module-refresh-activate", job_id),
        },
    )
    new_module_id = str(activated["activation"]["module_id"])
    new_index = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "index",
            "payload": {"module_id": new_module_id},
        },
    )
    refreshed_manifest = _extend_manifest_for_module_revision(
        manifest,
        old_module_id=old_module_id,
        new_module_id=new_module_id,
        old_index=old_index,
        new_index=new_index,
    )
    extended = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="extend_modules",
        run_id=run_id,
        identity=f"refresh-module-manifest:{old_module_id}:{new_module_id}",
        payload={"manifest": refreshed_manifest},
    )
    campaign = await _campaign(client, campaign_id)
    phase_changes.append(
        _facade_value(
            await client.core(
                "game_phase",
                {
                    "campaign_id": campaign_id,
                    "action": "set",
                    "tool_profile": "play",
                    "expected_revision": campaign["revision"],
                    "branch_id": branch["id"],
                    "idempotency_key": _mutation_key(
                        run_id, "phase", f"refresh-return-play-r{campaign['revision']}"
                    ),
                },
            )
        )
    )
    await client.open(campaign_id)
    await client.load("play.scene", "play.scene_control")
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"refresh-module-sync:{new_module_id}",
    )
    return {
        "old_module_id": old_module_id,
        "new_module_id": new_module_id,
        "source_key": source_key,
        "job_id": job_id,
        "inspection": {
            "parser_profile": preview.get("parser_profile"),
            "parser_version": preview.get("parser_version"),
            "scene_count": preview.get("scene_count"),
            "warnings": list(preview.get("warnings") or []),
        },
        "ingested": {
            "module_id": ingested.get("module_id"),
            "chapter_count": ingested.get("chapter_count"),
            "scene_count": ingested.get("scene_count"),
        },
        "activation": activated["activation"],
        "manifest": extended["manifest"],
        "phase_changes": phase_changes,
        "sync": synced,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    server = _server_parameters(args)
    report: dict[str, Any] = {
        "action": args.action,
        "transport": "stdio",
        "campaign_id": args.campaign_id,
        "home": str(args.home.expanduser().resolve()),
        "run_id": args.run_id,
        "database_access": False,
    }
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            client = ExposureClient(session)
            await client.open(args.campaign_id)
            campaign = await _campaign(client, args.campaign_id)
            phase = _campaign_phase(campaign)
            report["phase"] = phase
            await client.load(*_phase_groups(phase))
            if args.action == "register-party":
                await client.load(_character_group(phase))
                report["result"] = await _register_party(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    selections=_party_selections(args),
                )
            elif args.action == "configure-advancement":
                if phase == "combat":
                    raise RuntimeError("configure-advancement cannot run during active combat")
                if phase == "play":
                    await client.load("play.scene_control")
                report["result"] = await _configure_advancement(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    mode=str(args.advancement_mode or ""),
                    initial_phase=phase,
                )
            elif args.action == "start-play":
                report["result"] = await _start_play(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    initial_phase=phase,
                    scene_id=str(args.scene_id or ""),
                    objective=args.objective,
                    reachable_scene_ids=args.reachable_scene_id,
                )
            elif args.action == "refresh-module":
                report["result"] = await _refresh_module(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    initial_phase=phase,
                    source_path=args.module_source_path,
                    source_key=args.module_source_key,
                    title=args.module_title,
                )
            elif args.action == "advance-scene":
                if phase != "play":
                    raise RuntimeError("advance-scene requires the play phase")
                await client.load(*_scene_groups(phase))
                report["result"] = await _advance_scene(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    scene_id=str(args.scene_id or ""),
                    objective=args.objective,
                    mark_visited=args.mark_visited,
                    reachable_scene_ids=args.reachable_scene_id,
                    excluded_scenes=args.excluded_scene_json,
                )
            elif args.action == "branch-from-snapshot":
                report["result"] = await _branch_from_snapshot(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    initial_phase=phase,
                    snapshot_slot=args.snapshot_slot,
                    branch_name=args.branch_name,
                    checkpoint_label=args.checkpoint_label,
                )
            elif args.action == "resolve-check":
                if phase != "play":
                    raise RuntimeError("resolve-check requires the play phase")
                await client.load("play.characters", "play.resolution")
                report["result"] = await _resolve_check(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    scene_id=str(args.scene_id or ""),
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    actor_id=args.check_actor_id,
                    kind=args.check_kind,
                    ability=args.check_ability,
                    dc=args.check_dc,
                    proficient=args.check_proficient,
                    advantage=args.check_advantage,
                    disadvantage=args.check_disadvantage,
                    knowledge_actor_ids=args.knowledge_actor_id,
                    success_knowledge=args.success_knowledge,
                    failure_knowledge=args.failure_knowledge,
                )
            elif args.action == "record-event":
                if phase != "play":
                    raise RuntimeError("record-event requires the play phase")
                report["result"] = await _record_event(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    scene_id=str(args.scene_id or ""),
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    event_type=args.event_type,
                    summary=args.event_summary,
                    knowledge=args.event_knowledge,
                    knowledge_actor_ids=args.event_knowledge_actor_id,
                    progress_percent=args.progress_percent,
                )
            elif args.action == "apply-damage":
                if phase != "play":
                    raise RuntimeError("apply-damage requires the play phase")
                await client.load("play.characters", "play.resolution")
                report["result"] = await _apply_source_damage(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    scene_id=str(args.scene_id or ""),
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    actor_id=args.damage_actor_id,
                    expression=args.damage_expression,
                    damage_type=args.damage_type,
                    reason=args.damage_reason,
                    half_damage=args.damage_half,
                    knock_prone=args.damage_knock_prone,
                    knowledge_actor_ids=args.knowledge_actor_id,
                )
            elif args.action == "stand-up":
                if phase != "play":
                    raise RuntimeError("stand-up requires the play phase")
                await client.load("play.characters")
                report["result"] = await _stand_after_source_event(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    scene_id=str(args.scene_id or ""),
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    actor_id=args.stand_actor_id,
                    knowledge_actor_ids=args.knowledge_actor_id,
                    reason=args.stand_reason,
                )
            elif args.action == "short-rest":
                if phase != "play":
                    raise RuntimeError("short-rest requires the play phase")
                await client.load("play.characters")
                report["result"] = await _short_rest(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    members=args.rest_member_json,
                    start_clock=args.rest_start_clock_json,
                    duration_minutes=args.rest_duration_minutes,
                    reason=args.rest_reason,
                )
            elif args.action == "use-activity":
                if phase != "play":
                    raise RuntimeError("use-activity requires the play phase")
                await client.load("play.characters")
                report["result"] = await _use_activity(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    scene_id=str(args.scene_id or ""),
                    location_key=args.location_key,
                    actor_id=args.activity_actor_id,
                    activity_id=args.activity_id,
                    declaration=args.activity_declaration_json,
                    reason=args.activity_reason,
                    knowledge_actor_ids=args.knowledge_actor_id,
                )
            elif args.action == "long-rest":
                if phase != "play":
                    raise RuntimeError("long-rest requires the play phase")
                await client.load("play.characters")
                report["result"] = await _long_rest(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    members=args.rest_member_json,
                    start_clock=args.rest_start_clock_json,
                    duration_minutes=args.rest_duration_minutes,
                    reason=args.rest_reason,
                )
            elif args.action == "award-xp":
                if phase != "play":
                    raise RuntimeError("award-xp requires the play phase")
                await client.load("play.characters")
                report["result"] = await _award_experience(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    scene_id=str(args.scene_id or ""),
                    source_ref=args.source_ref_json,
                    actor_ids=args.xp_actor_id,
                    amount=args.xp_amount,
                    reason=args.xp_reason,
                )
            elif args.action == "checkpoint":
                label = args.checkpoint_label or f"Full playthrough checkpoint: {args.run_id}"
                report["result"] = await _checkpoint(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    label=label,
                )
            elif args.action == "verify-ending":
                if phase != "play":
                    raise RuntimeError("verify-ending requires the play phase")
                if not args.condition_id:
                    raise ValueError("verify-ending requires --condition-id")
                ended = await _manifest_mutation(
                    client,
                    campaign_id=args.campaign_id,
                    action="verify_ending",
                    run_id=args.run_id,
                    identity=f"ending:{args.condition_id}",
                    payload={"condition_id": args.condition_id},
                )
                checkpoint = None
                if ended["manifest"]["status"] == "completed":
                    checkpoint = await _checkpoint(
                        client,
                        campaign_id=args.campaign_id,
                        run_id=args.run_id,
                        label=(
                            args.checkpoint_label or f"Formal campaign ending: {args.condition_id}"
                        ),
                    )
                report["result"] = {"ending": ended, "checkpoint": checkpoint}
            elif args.action == "sync":
                report["result"] = await _manifest_mutation(
                    client,
                    campaign_id=args.campaign_id,
                    action="sync",
                    run_id=args.run_id,
                    identity="manual-sync",
                )
            else:
                report["result"] = await _manifest_get(client, args.campaign_id)
    report["passed"] = True
    return report


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")
    args = _arguments()
    try:
        report = asyncio.run(_run(args))
    except Exception as error:

        def leaf_messages(item: BaseException) -> list[str]:
            nested = getattr(item, "exceptions", ())
            if nested:
                return [message for child in nested for message in leaf_messages(child)]
            return [f"{type(item).__name__}: {item}"]

        report = {
            "action": args.action,
            "campaign_id": args.campaign_id,
            "run_id": args.run_id,
            "passed": False,
            "error": "; ".join(leaf_messages(error)),
        }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
