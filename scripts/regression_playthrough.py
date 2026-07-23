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
from sagasmith_dnd.playthrough import validate_playthrough_manifest

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
            "record-outcome",
            "resolve-check",
            "apply-damage",
            "stand-up",
            "use-activity",
            "branch-from-snapshot",
            "advance-time",
            "short-rest",
            "long-rest",
            "recover-stable",
            "acquire-loot",
            "spend-coins",
            "spend-item",
            "use-consumable",
            "award-xp",
            "advance-level",
            "configure-advancement",
            "relock-core",
            "refresh-module",
            "query-source",
            "register-party",
            "register-replacement",
            "prepare-narrative-npc",
            "start-play",
            "verify-ending",
        ),
        default="status",
    )
    parser.add_argument("--run-id", default="full-playthrough-v1")
    parser.add_argument("--advancement-mode", choices=("xp", "milestone"))
    parser.add_argument("--core-relock-reason", default="")
    parser.add_argument("--module-root", type=Path)
    parser.add_argument("--module-source-path", type=Path)
    parser.add_argument("--module-source-key", default="")
    parser.add_argument("--module-title", default="")
    parser.add_argument(
        "--refresh-return-phase",
        choices=("lobby", "play"),
        default="",
        help="Phase to expose after a successful module refresh; defaults to the entry phase",
    )
    parser.add_argument("--source-query", default="")
    parser.add_argument("--source-top-k", type=int, default=8)
    parser.add_argument(
        "--source-expand",
        action="store_true",
        help="Expand every module-search hit into its complete indexed chunk",
    )
    parser.add_argument("--checkpoint-label", default="")
    parser.add_argument("--scene-id")
    parser.add_argument(
        "--source-scene-id",
        default="",
        help=(
            "Scene containing the cited source when it differs from the scene where "
            "the action occurs"
        ),
    )
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
    parser.add_argument("--time-period", choices=("minute", "hour", "day"))
    parser.add_argument("--time-count", type=int)
    parser.add_argument("--time-reason", default="")
    parser.add_argument("--time-start-clock-json", type=json.loads)
    parser.add_argument("--rest-member-json", action="append", type=json.loads, default=[])
    parser.add_argument("--rest-start-clock-json", type=json.loads)
    parser.add_argument("--rest-duration-minutes", type=int, default=60)
    parser.add_argument("--rest-reason", default="")
    parser.add_argument("--recovery-actor-id", action="append", default=[])
    parser.add_argument("--loot-acquisition-id", default="")
    parser.add_argument("--loot-coins-json", type=json.loads, default={})
    parser.add_argument("--loot-item-json", action="append", type=json.loads, default=[])
    parser.add_argument("--loot-reason", default="")
    parser.add_argument("--spend-id", default="")
    parser.add_argument("--spend-coins-json", type=json.loads, default={})
    parser.add_argument("--spend-item-id", default="")
    parser.add_argument("--spend-item-quantity", type=int, default=1)
    parser.add_argument("--spend-reason", default="")
    parser.add_argument("--spend-rule-ref", default="")
    parser.add_argument("--consumable-use-id", default="")
    parser.add_argument("--consumable-item-id", default="")
    parser.add_argument("--consumable-target-id", default="")
    parser.add_argument("--consumable-reason", default="")
    parser.add_argument("--event-type", default="")
    parser.add_argument(
        "--event-audience-scope",
        choices=("party", "dm"),
        default="party",
    )
    parser.add_argument("--event-summary", default="")
    parser.add_argument("--event-knowledge", default="")
    parser.add_argument("--event-knowledge-actor-id", action="append", default=[])
    parser.add_argument("--replacement-predecessor-id", default="")
    parser.add_argument("--replacement-actor-id", default="")
    parser.add_argument("--replacement-knowledge", action="append", default=[])
    parser.add_argument("--narrative-npc-name", default="")
    parser.add_argument("--narrative-npc-role", default="")
    parser.add_argument("--narrative-npc-summary", default="")
    parser.add_argument("--narrative-npc-faction", default="")
    parser.add_argument("--narrative-npc-relationship", default="")
    parser.add_argument("--outcome-id", default="")
    parser.add_argument("--fact-json", action="append", type=json.loads, default=[])
    parser.add_argument("--npc-state-json", action="append", type=json.loads, default=[])
    parser.add_argument("--quest-state-json", action="append", type=json.loads, default=[])
    parser.add_argument("--clue-state-json", action="append", type=json.loads, default=[])
    parser.add_argument("--world-state-json", type=json.loads, default={})
    parser.add_argument("--progress-percent", type=int)
    parser.add_argument("--xp-actor-id", action="append", default=[])
    parser.add_argument("--xp-amount", type=int)
    parser.add_argument("--xp-reason", default="")
    parser.add_argument("--level-actor-id", default="")
    parser.add_argument("--level-target", type=int)
    parser.add_argument("--level-class-name", default="")
    parser.add_argument("--level-hp-method", choices=("fixed", "rolled"))
    parser.add_argument("--level-reason", default="")
    parser.add_argument(
        "--level-return-phase",
        choices=("lobby", "play"),
        help="Explicit phase to restore after the lobby-only level transaction",
    )
    parser.add_argument("--level-subclass-artifact-id", default="")
    parser.add_argument(
        "--level-feature-selection-json",
        action="append",
        type=json.loads,
        default=[],
        help="JSON object with artifact_id and a selection object",
    )
    parser.add_argument(
        "--level-spell-json",
        action="append",
        type=json.loads,
        default=[],
        help="JSON object with artifact_id, source_class, and method",
    )
    parser.add_argument("--level-prepared-spell-id", action="append", default=[])
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


async def _query_source(
    client: ExposureClient,
    *,
    campaign_id: str,
    query: str,
    top_k: int,
    expand: bool,
) -> dict[str, Any]:
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("query-source requires --source-query")
    if top_k < 1 or top_k > 50:
        raise ValueError("--source-top-k must be between 1 and 50")
    search_result = await client.domain(
        "module_search",
        {
            "campaign_id": campaign_id,
            "query": normalized_query,
            "top_k": top_k,
        },
    )
    hits = (
        search_result.get("result")
        if isinstance(search_result, dict) and isinstance(search_result.get("result"), list)
        else search_result
    )
    if not isinstance(hits, list) or any(not isinstance(hit, dict) for hit in hits):
        raise RuntimeError("module_search returned an invalid result collection")
    expanded = []
    if expand:
        for hit in hits:
            chunk_id = str(hit.get("chunk_id") or hit.get("id") or "")
            if not chunk_id:
                raise RuntimeError("module_search returned a hit without a chunk identifier")
            expanded.append(
                await client.domain(
                    "module_expand",
                    {"chunk_id": chunk_id},
                )
            )
    return {
        "query": normalized_query,
        "top_k": top_k,
        "hits": hits,
        "expanded_chunks": expanded,
    }


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


async def _register_replacement(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    predecessor_actor_id: str,
    replacement_actor_id: str,
    scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    summary: str,
    handoff_knowledge: list[str],
    witness_actor_ids: list[str],
) -> dict[str, Any]:
    predecessor_id = predecessor_actor_id.strip()
    replacement_id = replacement_actor_id.strip()
    normalized_summary = summary.strip()
    handoff = [item.strip() for item in handoff_knowledge if item.strip()]
    witnesses = list(dict.fromkeys(witness_actor_ids))
    if not all(
        (
            predecessor_id,
            replacement_id,
            scene_id,
            location_key,
            source_excerpt.strip(),
            normalized_summary,
        )
    ):
        raise ValueError(
            "register-replacement requires predecessor, replacement, scene, "
            "location, source excerpt, and summary"
        )
    if predecessor_id == replacement_id:
        raise ValueError("replacement actor must differ from predecessor")
    if not handoff or len(handoff) != len(set(handoff)):
        raise ValueError("register-replacement requires unique explicit handoff knowledge")
    if not witnesses or len(witnesses) != len(witness_actor_ids):
        raise ValueError("register-replacement requires unique witnesses")
    if predecessor_id in witnesses:
        raise ValueError("a dead or departed predecessor cannot witness replacement joining")
    if replacement_id not in witnesses:
        raise ValueError("replacement actor must witness their own joining event")

    current = await _manifest_get(client, campaign_id)
    manifest = deepcopy(dict(current["manifest"]))
    if str(dict(manifest["current"]).get("scene_id") or "") != scene_id:
        raise ValueError("replacement must join in the manifest's current scene")
    members = list(manifest["party"]["members"])
    predecessor_index = next(
        (
            index
            for index, member in enumerate(members)
            if str(member.get("actor_id") or "") == predecessor_id
        ),
        None,
    )
    if predecessor_index is None:
        raise ValueError("predecessor is not an active manifest party slot")
    predecessor_member = dict(members[predecessor_index])
    if predecessor_member.get("status") not in {"dead", "departed"}:
        raise ValueError("predecessor must be dead or departed before replacement")
    if any(str(member.get("actor_id") or "") == replacement_id for member in members):
        raise ValueError("replacement actor already occupies a party slot")
    if any(
        replacement_id
        in {
            str(item.get("predecessor_actor_id") or ""),
            str(item.get("replacement_actor_id") or ""),
        }
        for item in manifest["party"]["replacements"]
    ):
        raise ValueError("replacement actor is already present in replacement history")

    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    exact_ref = _validate_source_ref(scene, source_ref, excerpt=source_excerpt)
    if location_key not in {str(item.get("key") or "") for item in _scene_locations(scene)}:
        raise ValueError("replacement location is not present in the scene atlas")

    predecessor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": predecessor_id}},
    )
    replacement = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": replacement_id}},
    )
    for label, actor in (("predecessor", predecessor), ("replacement", replacement)):
        if actor.get("campaign_id") != campaign_id or actor.get("character_type") != "pc":
            raise ValueError(f"{label} must be a PC in this campaign")
    replacement_hp = dict(dict(replacement["sheet"])["combat"]["hp"])
    replacement_derived_hp = dict(
        dict(replacement.get("derived") or {}).get("hit_points") or {}
    )
    replacement_conditions = {
        str(item).casefold()
        for item in list(replacement_derived_hp.get("conditions") or [])
    }
    if int(replacement_hp.get("value", 0) or 0) <= 0 or "dead" in replacement_conditions:
        raise ValueError("replacement must be a living PC")
    for actor_id in witnesses:
        actor = await client.domain(
            "character_query",
            {"view": "get", "payload": {"character_id": actor_id}},
        )
        if actor.get("campaign_id") != campaign_id:
            raise ValueError("every replacement witness must belong to the campaign")

    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    branch_id = str(branch["id"])
    predecessor_knowledge_before = list(
        await client.domain(
            "actor_knowledge_query",
            {
                "campaign_id": campaign_id,
                "actor_id": predecessor_id,
                "view": "list",
                "payload": {"branch_id": branch_id},
            },
        )
        or []
    )
    replacement_knowledge_before = list(
        await client.domain(
            "actor_knowledge_query",
            {
                "campaign_id": campaign_id,
                "actor_id": replacement_id,
                "view": "list",
                "payload": {"branch_id": branch_id},
            },
        )
        or []
    )
    if replacement_knowledge_before:
        raise ValueError("new replacement must begin with independent empty ActorKnowledge")

    knowledge_prefix = (
        f"playthrough.{_token(run_id)}.replacement.{_token(replacement_id)}"
    )
    join_key = f"{knowledge_prefix}.joined"
    handoff_rows = [
        {
            "actor_id": replacement_id,
            "knowledge_key": f"{knowledge_prefix}.handoff.{index + 1}.{_token(proposition)}",
            "proposition": proposition,
            "cause": "told_by",
            "disclosure_scope": "owner",
        }
        for index, proposition in enumerate(handoff)
    ]
    actor_knowledge = [
        {
            "actor_id": actor_id,
            "knowledge_key": join_key,
            "proposition": normalized_summary,
            "cause": "witnessed",
            "disclosure_scope": "owner",
        }
        for actor_id in witnesses
    ] + handoff_rows

    replacement_member = _party_member(
        replacement,
        {
            "source": "replacement",
            "source_asset_path": "",
            "status": "active",
        },
    )
    prospective = deepcopy(manifest)
    prospective["party"]["members"][predecessor_index] = replacement_member
    prospective["party"]["replacements"].append(
        {
            "predecessor_actor_id": predecessor_id,
            "replacement_actor_id": replacement_id,
            "handoff_event_id": f"pending:{_token(run_id + replacement_id)}",
        }
    )
    validate_playthrough_manifest(prospective)

    campaign = await _campaign(client, campaign_id)
    committed = await client.domain(
        "continuity_commit",
        {
            "campaign_id": campaign_id,
            "payload": {
                "event": {
                    "summary": normalized_summary,
                    "event_type": "replacement_joined",
                    "audience_scope": "party",
                    "payload": {
                        "scene_id": scene_id,
                        "location_key": location_key,
                        "predecessor_actor_id": predecessor_id,
                        "replacement_actor_id": replacement_id,
                        "handoff_knowledge": handoff,
                        "source_excerpt": source_excerpt.strip(),
                        "source_ref": exact_ref,
                    },
                },
                "actor_knowledge": actor_knowledge,
                "snapshot": {
                    "label": (
                        f"Replacement handoff: {replacement['name']} succeeds "
                        f"{predecessor['name']}"
                    )
                },
                "branch_id": branch_id,
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id,
                "replacement-continuity",
                f"{predecessor_id}:{replacement_id}",
            ),
        },
    )
    handoff_event_id = str(dict(committed["event"])["id"])
    prospective["party"]["replacements"][-1]["handoff_event_id"] = handoff_event_id
    prospective = validate_playthrough_manifest(prospective)
    replaced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="replace",
        run_id=run_id,
        identity=f"replacement-manifest:{predecessor_id}:{replacement_id}",
        payload={"manifest": prospective},
    )
    checkpoint = await _checkpoint(
        client,
        campaign_id=campaign_id,
        run_id=run_id,
        label=(
            f"Full playthrough replacement: {replacement['name']} succeeds "
            f"{predecessor['name']}"
        ),
    )

    replacement_knowledge_after = list(
        await client.domain(
            "actor_knowledge_query",
            {
                "campaign_id": campaign_id,
                "actor_id": replacement_id,
                "view": "list",
                "payload": {"branch_id": branch_id},
            },
        )
        or []
    )
    expected_keys = {join_key, *(row["knowledge_key"] for row in handoff_rows)}
    actual_keys = {
        str(item.get("knowledge_key") or "") for item in replacement_knowledge_after
    }
    if actual_keys != expected_keys:
        raise RuntimeError("replacement ActorKnowledge does not match explicit handoff")
    predecessor_knowledge_after = list(
        await client.domain(
            "actor_knowledge_query",
            {
                "campaign_id": campaign_id,
                "actor_id": predecessor_id,
                "view": "list",
                "payload": {"branch_id": branch_id},
            },
        )
        or []
    )
    before_ids = {str(item.get("id") or "") for item in predecessor_knowledge_before}
    after_ids = {str(item.get("id") or "") for item in predecessor_knowledge_after}
    if before_ids != after_ids:
        raise RuntimeError("predecessor ActorKnowledge changed during replacement")
    retained_predecessor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": predecessor_id}},
    )
    if str(retained_predecessor.get("id") or "") != predecessor_id:
        raise RuntimeError("predecessor actor was not retained")
    return {
        "scene": {
            "scene_id": scene_id,
            "location_key": location_key,
            "source_ref": exact_ref,
        },
        "predecessor": {
            "actor_id": predecessor_id,
            "name": predecessor["name"],
            "status": predecessor_member["status"],
            "retained": True,
            "knowledge_count": len(predecessor_knowledge_after),
        },
        "replacement": replacement_member,
        "handoff_knowledge": handoff,
        "witness_actor_ids": witnesses,
        "continuity": committed,
        "manifest_replace": replaced,
        "checkpoint": checkpoint,
    }


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
    audience_scope: str = "party",
) -> dict[str, Any]:
    if not all((scene_id, location_key, source_excerpt, event_type, summary)):
        raise ValueError("record-event requires scene, location, excerpt, event type, and summary")
    if bool(knowledge.strip()) != bool(knowledge_actor_ids):
        raise ValueError(
            "record-event knowledge text and knowledge actor ids must be provided together"
        )
    if progress_percent is not None and not 0 <= progress_percent <= 100:
        raise ValueError("record-event progress percent must be between 0 and 100")
    if audience_scope not in {"party", "dm"}:
        raise ValueError("record-event audience scope must be party or dm")
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
    event_identity = f"{scene_id}:{event_type}:{summary.strip()}"
    event_key = _token(f"{run_id}:{event_identity}", length=24)
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
            "idempotency_key": _mutation_key(run_id, "scene-event-progress", event_identity),
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
                    "audience_scope": audience_scope,
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
                        "knowledge_key": (f"playthrough.{_token(run_id)}.{_token(event_identity)}"),
                        "proposition": knowledge.strip(),
                        "disclosure_scope": "owner",
                    }
                    for actor_id in list(dict.fromkeys(knowledge_actor_ids))
                ],
                "snapshot": {"label": f"Full playthrough event: {summary.strip()}"},
                "branch_id": str(branch["id"]),
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(run_id, "continuity-event", event_identity),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"record-event-sync:{event_identity}",
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


def _upsert_manifest_rows(
    existing: list[dict[str, Any]],
    updates: list[dict[str, Any]],
    *,
    key: str,
) -> list[dict[str, Any]]:
    rows = [deepcopy(dict(item)) for item in existing]
    index = {str(item.get(key) or ""): position for position, item in enumerate(rows)}
    for raw in updates:
        if not isinstance(raw, dict):
            raise ValueError(f"manifest {key} updates must be objects")
        item = deepcopy(raw)
        identity = str(item.get(key) or "").strip()
        if not identity:
            raise ValueError(f"manifest {key} updates require {key}")
        if identity in index:
            rows[index[identity]] = item
        else:
            index[identity] = len(rows)
            rows.append(item)
    return rows


async def _prepare_narrative_npc(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    initial_phase: str,
    scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    name: str,
    role: str,
    summary: str,
    faction: str,
    relationship: str,
) -> dict[str, Any]:
    normalized_name = name.strip()
    normalized_role = role.strip()
    normalized_summary = summary.strip()
    if initial_phase != "play":
        raise RuntimeError("prepare-narrative-npc requires the play phase")
    if not all(
        (
            scene_id,
            location_key,
            source_excerpt,
            normalized_name,
            normalized_role,
            normalized_summary,
        )
    ):
        raise ValueError(
            "prepare-narrative-npc requires scene, location, excerpt, name, role, and summary"
        )

    await client.load("play.scene", "play.scene_control", "play.characters")
    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    exact_ref = _validate_source_ref(scene, source_ref, excerpt=source_excerpt)
    if location_key not in {str(item.get("key") or "") for item in _scene_locations(scene)}:
        raise ValueError("narrative NPC location is not present in the scene atlas")
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    branch_id = str(branch["id"])

    campaign = await _campaign(client, campaign_id)
    entered_lobby = _facade_value(
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
                    f"narrative-npc-{normalized_name}-enter-lobby-r{campaign['revision']}",
                ),
            },
        )
    )
    await client.open(campaign_id)
    await client.load("lobby.campaign", "lobby.characters")
    created = _facade_value(
        await client.domain(
            "character_create_from",
            {
                "mode": "narrative_npc",
                "payload": {
                    "campaign_id": campaign_id,
                    "name": normalized_name,
                    "role": normalized_role,
                    "summary": normalized_summary,
                    "source_ref": exact_ref,
                    "source_excerpt": source_excerpt,
                },
                "idempotency_key": _mutation_key(
                    run_id,
                    "narrative-npc",
                    (
                        f"{normalized_name}:{exact_ref['module_id']}:"
                        f"{exact_ref['chunk_id']}"
                    ),
                ),
            },
        )
    )
    actor = dict(created.get("character") or {})
    provenance = dict(created.get("narrative_npc") or {})
    canonical_source_ref = {
        key: deepcopy(exact_ref[key])
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
    if (
        actor.get("campaign_id") != campaign_id
        or actor.get("character_type") != "npc"
        or actor.get("name") != normalized_name
        or provenance.get("combat_eligible") is not False
        or provenance.get("combat_statblock") != "not_imported"
        or dict(provenance.get("source_ref") or {}) != canonical_source_ref
    ):
        raise RuntimeError("source-bound narrative NPC creation verification failed")
    status_tags = set(
        dict(dict(actor.get("sheet") or {}).get("adventure_state") or {}).get(
            "status_tags"
        )
        or []
    )
    if not {"narrative_only", "source_bound"}.issubset(status_tags):
        raise RuntimeError("narrative NPC actor is missing its noncombat provenance tags")

    campaign = await _campaign(client, campaign_id)
    returned_play = _facade_value(
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
                    f"narrative-npc-{actor['id']}-return-play-r{campaign['revision']}",
                ),
            },
        )
    )
    await client.open(campaign_id)
    await client.load("play.scene", "play.scene_control", "play.characters")
    verified_actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": str(actor["id"])}},
    )
    if verified_actor.get("campaign_id") != campaign_id:
        raise RuntimeError("narrative NPC disappeared after returning to play")

    current_manifest = await _manifest_get(client, campaign_id)
    manifest = deepcopy(dict(current_manifest["manifest"]))
    source_note = (
        "Narrative-only source-bound actor; combat_statblock=not_imported; "
        f"module={exact_ref['module_id']}; scene={exact_ref['scene_id']}; "
        f"chunk={exact_ref['chunk_id']}; pages={exact_ref['page_start']}-"
        f"{exact_ref['page_end']}; sha256={exact_ref['content_sha256']}."
    )
    manifest["npcs"] = _upsert_manifest_rows(
        list(manifest.get("npcs") or []),
        [
            {
                "actor_id": str(actor["id"]),
                "name": normalized_name,
                "status": "active",
                "faction": faction.strip(),
                "relationship": relationship.strip(),
                "notes": source_note,
            }
        ],
        key="actor_id",
    )
    manifest = validate_playthrough_manifest(manifest)
    replaced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="replace",
        run_id=run_id,
        identity=f"narrative-npc-register:{actor['id']}",
        payload={"manifest": manifest},
    )
    checkpoint = await _checkpoint(
        client,
        campaign_id=campaign_id,
        run_id=run_id,
        label=f"Narrative NPC prepared: {normalized_name}",
    )
    return {
        "actor": verified_actor,
        "narrative_npc": provenance,
        "scene": {
            "scene_id": scene_id,
            "location_key": location_key,
            "source_ref": exact_ref,
        },
        "phase_changes": [entered_lobby, returned_play],
        "manifest_replace": replaced,
        "checkpoint": checkpoint,
    }


async def _record_outcome(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    outcome_id: str,
    scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    event_type: str,
    summary: str,
    knowledge: str,
    knowledge_actor_ids: list[str],
    facts: list[dict[str, Any]],
    npc_states: list[dict[str, Any]],
    quest_states: list[dict[str, Any]],
    clue_states: list[dict[str, Any]],
    world_state: dict[str, Any],
    objective: str,
    progress_percent: int | None,
    audience_scope: str = "party",
) -> dict[str, Any]:
    if not all(
        (
            outcome_id.strip(),
            scene_id,
            location_key,
            source_excerpt,
            event_type,
            summary.strip(),
        )
    ):
        raise ValueError(
            "record-outcome requires outcome id, scene, location, excerpt, event type, and summary"
        )
    if bool(knowledge.strip()) != bool(knowledge_actor_ids):
        raise ValueError(
            "record-outcome knowledge text and knowledge actor ids must be provided together"
        )
    if not facts:
        raise ValueError("record-outcome requires at least one stable fact")
    if progress_percent is not None and not 0 <= progress_percent <= 100:
        raise ValueError("record-outcome progress percent must be between 0 and 100")
    if audience_scope not in {"party", "dm"}:
        raise ValueError("record-outcome audience scope must be party or dm")
    if not isinstance(world_state, dict):
        raise ValueError("record-outcome world state must be an object")
    normalized_facts = []
    for index, raw in enumerate(facts):
        if not isinstance(raw, dict):
            raise ValueError(f"fact-json[{index}] must be an object")
        fact = deepcopy(raw)
        if (
            not str(fact.get("fact_key") or "").strip()
            or not str(fact.get("content") or "").strip()
        ):
            raise ValueError(f"fact-json[{index}] requires fact_key and content")
        normalized_facts.append(fact)

    current_manifest = await _manifest_get(client, campaign_id)
    manifest = deepcopy(dict(current_manifest["manifest"]))
    manifest["npcs"] = _upsert_manifest_rows(
        list(manifest.get("npcs") or []), npc_states, key="actor_id"
    )
    manifest["quests"] = _upsert_manifest_rows(
        list(manifest.get("quests") or []), quest_states, key="id"
    )
    manifest["clues"] = _upsert_manifest_rows(
        list(manifest.get("clues") or []), clue_states, key="id"
    )
    manifest["world_state"] = {
        **deepcopy(dict(manifest.get("world_state") or {})),
        **deepcopy(world_state),
    }
    if objective.strip():
        manifest["current"]["objective"] = objective.strip()
    manifest = validate_playthrough_manifest(manifest)

    await client.load("play.characters")
    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    exact_ref = _validate_source_ref(scene, source_ref, excerpt=source_excerpt)
    if location_key not in {str(item.get("key") or "") for item in _scene_locations(scene)}:
        raise ValueError("record-outcome location is not present in the scene atlas")

    recipients = list(dict.fromkeys(knowledge_actor_ids))
    for actor_id in recipients:
        actor = await client.domain(
            "character_query",
            {"view": "get", "payload": {"character_id": actor_id}},
        )
        if actor.get("campaign_id") != campaign_id:
            raise ValueError("every record-outcome witness must belong to the campaign")
    for item in npc_states:
        if not isinstance(item, dict) or not str(item.get("actor_id") or "").strip():
            raise ValueError("npc-state-json entries require actor_id")
        actor = await client.domain(
            "character_query",
            {"view": "get", "payload": {"character_id": str(item["actor_id"])}},
        )
        if actor.get("campaign_id") != campaign_id:
            raise ValueError("every tracked outcome NPC must belong to the campaign")

    progress_rows = await client.domain(
        "module_query",
        {"campaign_id": campaign_id, "view": "progress"},
    )
    progress_before = next(
        (item for item in progress_rows if item.get("scene_id") == scene_id),
        None,
    )
    state = deepcopy(dict((progress_before or {}).get("state") or {}))
    outcomes = deepcopy(dict(state.get("full_playthrough_outcomes") or {}))
    outcome_record = {
        "event_type": event_type,
        "summary": summary.strip(),
        "source_ref": exact_ref,
        "fact_keys": [str(item["fact_key"]) for item in normalized_facts],
    }
    existing_outcome = outcomes.get(outcome_id.strip())
    if existing_outcome is not None:
        if existing_outcome != outcome_record:
            raise ValueError("record-outcome id already exists with different scene outcome data")
        progress = progress_before
    else:
        outcomes[outcome_id.strip()] = outcome_record
        state["full_playthrough_outcomes"] = outcomes
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
                "idempotency_key": _mutation_key(run_id, "scene-outcome-progress", outcome_id),
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
                    "audience_scope": audience_scope,
                    "payload": {
                        "outcome_id": outcome_id.strip(),
                        "scene_id": scene_id,
                        "location_key": location_key,
                        "source_excerpt": source_excerpt,
                        "source_ref": exact_ref,
                    },
                },
                "facts": normalized_facts,
                "actor_knowledge": [
                    {
                        "actor_id": actor_id,
                        "knowledge_key": (
                            f"playthrough.{_token(run_id)}.outcome.{_token(outcome_id.strip())}"
                        ),
                        "proposition": knowledge.strip(),
                        "disclosure_scope": "owner",
                    }
                    for actor_id in recipients
                ],
                "branch_id": str(branch["id"]),
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(run_id, "continuity-outcome", outcome_id),
        },
    )

    replaced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="replace",
        run_id=run_id,
        identity=f"record-outcome-replace:{outcome_id}",
        payload={"manifest": manifest},
    )
    checkpoint = await _checkpoint(
        client,
        campaign_id=campaign_id,
        run_id=run_id,
        label=f"Full playthrough outcome: {outcome_id.strip()}",
    )
    return {
        "outcome_id": outcome_id.strip(),
        "scene": {
            "scene_id": scene_id,
            "location_key": location_key,
            "source_ref": exact_ref,
        },
        "progress": progress,
        "continuity": committed,
        "knowledge_actor_ids": recipients,
        "manifest_replace": replaced,
        "checkpoint": checkpoint,
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
                        f"Full playthrough environmental damage: {actor['name']} at {location_key}"
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
    if location_key not in {str(item.get("key") or "") for item in _scene_locations(scene)}:
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
        f"{actor['name']} recovered from the source-cited fall and stood at {location_key}."
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
                "snapshot": {"label": f"Full playthrough stand: {actor['name']} at {location_key}"},
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
        if (
            unexpected
            or not actor_id
            or (arcane_recovery is not None and not isinstance(arcane_recovery, dict))
            or (hit_dice_spends is not None and not isinstance(hit_dice_spends, list))
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
            normalized_spends.append({"key": str(spend["key"]), "count": int(spend["count"])})
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
        raise ValueError("use-activity requires scene, location, actor, activity id, and reason")
    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    if location_key not in {str(item.get("key") or "") for item in _scene_locations(scene)}:
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
                        "random_stream_receipt": deepcopy(acted.get("random_stream_receipt")),
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
            or (hit_dice_recovery is not None and not isinstance(hit_dice_recovery, dict))
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
                "prepared_spell_ids": (list(prepared_ids) if prepared_ids is not None else None),
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


async def _advance_time(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    scene_id: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    period: str,
    count: int | None,
    reason: str,
    start_clock: dict[str, Any] | None,
    knowledge_actor_ids: list[str],
) -> dict[str, Any]:
    normalized_reason = reason.strip()
    if (
        not scene_id
        or period not in {"minute", "hour", "day"}
        or count is None
        or count <= 0
        or not normalized_reason
    ):
        raise ValueError(
            "advance-time requires scene, positive count, period, reason, and exact source"
        )
    if len(knowledge_actor_ids) != len(set(knowledge_actor_ids)):
        raise ValueError("advance-time knowledge actor ids must be unique")
    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    exact_ref = _validate_source_ref(scene, source_ref, excerpt=source_excerpt)
    actors = []
    for actor_id in knowledge_actor_ids:
        actor = await client.domain(
            "character_query",
            {"view": "get", "payload": {"character_id": actor_id}},
        )
        if actor.get("campaign_id") != campaign_id:
            raise ValueError("advance-time witness does not belong to the campaign")
        actors.append(actor)
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    branch_id = str(branch["id"])
    identity = f"{scene_id}:{period}:{count}:{normalized_reason}"
    campaign = await _campaign(client, campaign_id)
    before = deepcopy(dict(dict(campaign.get("state") or {}).get("world_time") or {}))
    clock_set = None
    if not before:
        if not isinstance(start_clock, dict):
            raise ValueError(
                "advance-time requires --time-start-clock-json when the clock is unset"
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
                "branch_id": branch_id,
                "expected_revision": campaign["revision"],
                "idempotency_key": _mutation_key(run_id, "advance-time-clock-set", identity),
            },
        )
        before = deepcopy(dict(clock_set.get("world_time") or {}))
    elif start_clock is not None:
        raise ValueError("advance-time start clock must be omitted after the clock is set")
    campaign = await _campaign(client, campaign_id)
    advanced = await client.domain(
        "campaign_change",
        {
            "campaign_id": campaign_id,
            "action": "clock_advance",
            "payload": {"period": period, "count": count},
            "branch_id": branch_id,
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(run_id, "advance-time-clock", identity),
        },
    )
    after = deepcopy(dict(advanced.get("world_time") or {}))
    expected_minutes = count * {"minute": 1, "hour": 60, "day": 1440}[period]
    if (
        not before
        or not after
        or int(after.get("elapsed_minutes", 0) or 0) - int(before.get("elapsed_minutes", 0) or 0)
        != expected_minutes
    ):
        raise RuntimeError("campaign clock did not advance by the requested duration")
    campaign = await _campaign(client, campaign_id)
    committed = await client.domain(
        "continuity_commit",
        {
            "campaign_id": campaign_id,
            "payload": {
                "event": {
                    "summary": normalized_reason,
                    "event_type": "time_advanced",
                    "audience_scope": "party",
                    "payload": {
                        "scene_id": scene_id,
                        "period": period,
                        "count": count,
                        "elapsed_minutes": expected_minutes,
                        "world_time_before": before,
                        "world_time_after": after,
                        "source_excerpt": source_excerpt,
                        "source_ref": exact_ref,
                    },
                },
                "actor_knowledge": [
                    {
                        "actor_id": str(actor["id"]),
                        "knowledge_key": (
                            f"playthrough.{_token(run_id)}.{_token(scene_id)}."
                            f"time.{_token(identity)}"
                        ),
                        "proposition": normalized_reason,
                        "disclosure_scope": "owner",
                    }
                    for actor in actors
                ],
                "snapshot": {"label": f"Full playthrough time advance: {normalized_reason}"},
                "branch_id": branch_id,
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(run_id, "advance-time-continuity", identity),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"advance-time-sync:{identity}",
    )
    return {
        "scene_id": scene_id,
        "source_ref": exact_ref,
        "clock_set": clock_set,
        "before": before,
        "advance": advanced,
        "after": after,
        "knowledge_actor_ids": [str(actor["id"]) for actor in actors],
        "continuity": committed,
        "sync": synced,
    }


async def _recover_stable_party(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    actor_ids: list[str],
    knowledge_actor_ids: list[str],
    reason: str,
) -> dict[str, Any]:
    member_ids = list(dict.fromkeys(actor_ids))
    if not member_ids or len(member_ids) != len(actor_ids) or not reason.strip():
        raise ValueError("recover-stable requires unique actor ids and a non-empty --rest-reason")
    actors = []
    for actor_id in member_ids:
        actor = await client.domain(
            "character_query",
            {"view": "get", "payload": {"character_id": actor_id}},
        )
        if actor.get("campaign_id") != campaign_id:
            raise ValueError("every stable recovery actor must belong to the campaign")
        actors.append(actor)
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    campaign = await _campaign(client, campaign_id)
    recovered = await client.domain(
        "campaign_change",
        {
            "campaign_id": campaign_id,
            "action": "stable_recovery",
            "payload": {
                "members": [
                    {
                        "character_id": actor["id"],
                        "expected_revision": actor["revision"],
                    }
                    for actor in actors
                ]
            },
            "expected_revision": campaign["revision"],
            "branch_id": branch["id"],
            "idempotency_key": _mutation_key(run_id, "stable-recovery", ":".join(member_ids)),
        },
    )
    if recovered.get("status") != "recovered":
        raise RuntimeError("party stable recovery did not commit")
    recipients = list(dict.fromkeys([*member_ids, *knowledge_actor_ids]))
    campaign = await _campaign(client, campaign_id)
    committed = await client.domain(
        "continuity_commit",
        {
            "campaign_id": campaign_id,
            "payload": {
                "event": {
                    "summary": reason.strip(),
                    "event_type": "stable_recovery",
                    "audience_scope": "party",
                    "payload": {
                        "member_ids": member_ids,
                        "elapsed_hours": recovered["elapsed_hours"],
                        "recoveries": deepcopy(recovered["recoveries"]),
                        "random_stream_receipt": deepcopy(recovered.get("random_stream_receipt")),
                    },
                },
                "actor_knowledge": [
                    {
                        "actor_id": actor_id,
                        "knowledge_key": (
                            f"playthrough.{_token(run_id)}.stable_recovery."
                            f"{_token(':'.join(member_ids))}"
                        ),
                        "proposition": reason.strip(),
                        "disclosure_scope": "owner",
                    }
                    for actor_id in recipients
                ],
                "snapshot": {"label": f"Full playthrough stable recovery: {reason.strip()}"},
                "branch_id": str(branch["id"]),
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id, "stable-recovery-continuity", ":".join(member_ids)
            ),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"stable-recovery-sync:{':'.join(member_ids)}",
    )
    return {
        "member_ids": member_ids,
        "knowledge_actor_ids": recipients,
        "recovery": recovered,
        "continuity": committed,
        "sync": synced,
    }


async def _acquire_source_loot(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    acquisition_id: str,
    coins: dict[str, Any],
    items: list[dict[str, Any]],
    reason: str,
    knowledge_actor_ids: list[str],
    source_scene_id: str = "",
) -> dict[str, Any]:
    normalized_acquisition_id = acquisition_id.strip()
    normalized_reason = reason.strip()
    cited_scene_id = source_scene_id.strip() or scene_id
    recipients = list(dict.fromkeys(knowledge_actor_ids))
    if not all(
        (
            scene_id,
            location_key,
            source_excerpt.strip(),
            normalized_acquisition_id,
            normalized_reason,
        )
    ):
        raise ValueError(
            "acquire-loot requires scene, location, excerpt, acquisition id, and reason"
        )
    if not isinstance(coins, dict) or not isinstance(items, list):
        raise ValueError("acquire-loot coins must be an object and items must be an array")
    if not coins and not items:
        raise ValueError("acquire-loot requires coins or items")
    if not recipients or len(recipients) != len(knowledge_actor_ids):
        raise ValueError("acquire-loot requires unique actor knowledge recipients")

    source_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": cited_scene_id},
        },
    )
    exact_ref = _validate_source_ref(source_scene, source_ref, excerpt=source_excerpt)
    occurrence_scene = (
        source_scene
        if cited_scene_id == scene_id
        else await client.domain(
            "module_query",
            {
                "campaign_id": campaign_id,
                "view": "scene",
                "payload": {"scene_id": scene_id},
            },
        )
    )
    if location_key not in {
        str(item.get("key") or "") for item in _scene_locations(occurrence_scene)
    }:
        raise ValueError("acquire-loot location is not present in the occurrence scene atlas")
    serialized_source_ref = json.dumps(
        exact_ref,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    campaign = await _campaign(client, campaign_id)
    prior = next(
        (
            dict(item)
            for item in list(dict(campaign.get("state") or {}).get("loot_acquisitions") or [])
            if isinstance(item, dict) and str(item.get("id") or "") == normalized_acquisition_id
        ),
        None,
    )
    recovered = prior is not None
    if prior is not None:
        expected = {
            "id": normalized_acquisition_id,
            "reason": normalized_reason,
            "source_ref": serialized_source_ref,
            "coins": deepcopy(coins),
        }
        if any(prior.get(key) != value for key, value in expected.items()):
            raise RuntimeError("existing loot acquisition does not match this request")
        requested_item_ids = [str(item.get("id") or "") for item in items]
        if [str(item.get("id") or "") for item in prior.get("items", [])] != (requested_item_ids):
            raise RuntimeError("existing loot acquisition items do not match this request")
        acquired: dict[str, Any] = {
            "status": "recovered",
            "acquisition_id": normalized_acquisition_id,
            "coins": deepcopy(prior["coins"]),
            "items": deepcopy(prior["items"]),
            "reason": normalized_reason,
            "source_ref": serialized_source_ref,
        }
    else:
        acquired = await client.domain(
            "campaign_change",
            {
                "campaign_id": campaign_id,
                "action": "loot_acquire",
                "payload": {
                    "acquisition_id": normalized_acquisition_id,
                    "coins": deepcopy(coins),
                    "items": deepcopy(items),
                    "reason": normalized_reason,
                    "source_ref": serialized_source_ref,
                },
                "expected_revision": campaign["revision"],
                "idempotency_key": _mutation_key(run_id, "loot-acquire", normalized_acquisition_id),
            },
        )
        if acquired.get("status") != "committed":
            raise RuntimeError("source-bound loot acquisition did not commit")

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
                    "summary": normalized_reason,
                    "event_type": "loot_acquired",
                    "audience_scope": "party",
                    "payload": {
                        "scene_id": scene_id,
                        "location_key": location_key,
                        "acquisition_id": normalized_acquisition_id,
                        "coins": deepcopy(coins),
                        "item_ids": [str(item.get("id") or "") for item in items],
                        "source_excerpt": source_excerpt.strip(),
                        "source_ref": exact_ref,
                    },
                },
                "actor_knowledge": [
                    {
                        "actor_id": actor_id,
                        "knowledge_key": (
                            f"playthrough.{_token(run_id)}.loot.{_token(normalized_acquisition_id)}"
                        ),
                        "proposition": normalized_reason,
                        "disclosure_scope": "owner",
                    }
                    for actor_id in recipients
                ],
                "snapshot": {"label": f"Full playthrough loot: {normalized_acquisition_id}"},
                "branch_id": str(branch["id"]),
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(run_id, "loot-continuity", normalized_acquisition_id),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"loot-sync:{normalized_acquisition_id}",
    )
    return {
        "scene": {
            "scene_id": scene_id,
            "location_key": location_key,
            "source_scene_id": cited_scene_id,
            "source_ref": exact_ref,
        },
        "acquisition": acquired,
        "acquisition_recovered": recovered,
        "knowledge_actor_ids": recipients,
        "continuity": committed,
        "sync": synced,
    }


async def _spend_source_currency(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    spend_id: str,
    coins: dict[str, Any],
    reason: str,
    rule_ref: str,
    knowledge_actor_ids: list[str],
    source_scene_id: str = "",
) -> dict[str, Any]:
    normalized_spend_id = spend_id.strip()
    normalized_reason = reason.strip()
    normalized_rule_ref = rule_ref.strip()
    cited_scene_id = source_scene_id.strip() or scene_id
    recipients = list(dict.fromkeys(knowledge_actor_ids))
    if not all(
        (
            scene_id,
            location_key,
            source_excerpt.strip(),
            normalized_spend_id,
            normalized_reason,
            normalized_rule_ref,
        )
    ):
        raise ValueError(
            "spend-coins requires scene, location, excerpt, spend id, reason, and rule ref"
        )
    if not isinstance(coins, dict) or not coins:
        raise ValueError("spend-coins requires a nonempty coin object")
    if not recipients or len(recipients) != len(knowledge_actor_ids):
        raise ValueError("spend-coins requires unique actor knowledge recipients")

    source_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": cited_scene_id},
        },
    )
    exact_ref = _validate_source_ref(source_scene, source_ref, excerpt=source_excerpt)
    occurrence_scene = (
        source_scene
        if cited_scene_id == scene_id
        else await client.domain(
            "module_query",
            {
                "campaign_id": campaign_id,
                "view": "scene",
                "payload": {"scene_id": scene_id},
            },
        )
    )
    if location_key not in {
        str(item.get("key") or "") for item in _scene_locations(occurrence_scene)
    }:
        raise ValueError("spend-coins location is not present in the occurrence scene atlas")
    serialized_source_ref = json.dumps(
        exact_ref,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    campaign = await _campaign(client, campaign_id)
    prior = next(
        (
            dict(item)
            for item in list(dict(campaign.get("state") or {}).get("currency_spends") or [])
            if isinstance(item, dict) and str(item.get("id") or "") == normalized_spend_id
        ),
        None,
    )
    recovered = prior is not None
    if prior is not None:
        expected = {
            "id": normalized_spend_id,
            "reason": normalized_reason,
            "source_ref": serialized_source_ref,
            "rule_ref": normalized_rule_ref,
            "coins": deepcopy(coins),
        }
        if any(prior.get(key) != value for key, value in expected.items()):
            raise RuntimeError("existing currency spend does not match this request")
        spent: dict[str, Any] = {
            "status": "recovered",
            "spend_id": normalized_spend_id,
            "coins": deepcopy(prior["coins"]),
            "reason": normalized_reason,
            "source_ref": serialized_source_ref,
            "rule_ref": normalized_rule_ref,
        }
    else:
        spent = await client.domain(
            "campaign_change",
            {
                "campaign_id": campaign_id,
                "action": "currency_spend",
                "payload": {
                    "spend_id": normalized_spend_id,
                    "coins": deepcopy(coins),
                    "reason": normalized_reason,
                    "source_ref": serialized_source_ref,
                    "rule_ref": normalized_rule_ref,
                },
                "expected_revision": campaign["revision"],
                "idempotency_key": _mutation_key(
                    run_id, "currency-spend", normalized_spend_id
                ),
            },
        )
        if spent.get("status") != "committed":
            raise RuntimeError("source-bound currency spend did not commit")

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
                    "summary": normalized_reason,
                    "event_type": "currency_spent",
                    "audience_scope": "party",
                    "payload": {
                        "scene_id": scene_id,
                        "location_key": location_key,
                        "spend_id": normalized_spend_id,
                        "coins": deepcopy(coins),
                        "source_excerpt": source_excerpt.strip(),
                        "source_ref": exact_ref,
                        "rule_ref": normalized_rule_ref,
                    },
                },
                "actor_knowledge": [
                    {
                        "actor_id": actor_id,
                        "knowledge_key": (
                            f"playthrough.{_token(run_id)}.spend.{_token(normalized_spend_id)}"
                        ),
                        "proposition": normalized_reason,
                        "disclosure_scope": "owner",
                    }
                    for actor_id in recipients
                ],
                "snapshot": {
                    "label": f"Full playthrough currency spend: {normalized_spend_id}"
                },
                "branch_id": str(branch["id"]),
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id, "currency-spend-continuity", normalized_spend_id
            ),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"currency-spend-sync:{normalized_spend_id}",
    )
    return {
        "scene": {
            "scene_id": scene_id,
            "location_key": location_key,
            "source_scene_id": cited_scene_id,
            "source_ref": exact_ref,
        },
        "spend": spent,
        "spend_recovered": recovered,
        "knowledge_actor_ids": recipients,
        "continuity": committed,
        "sync": synced,
    }


async def _spend_source_item(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    spend_id: str,
    item_id: str,
    quantity: int,
    reason: str,
    knowledge_actor_ids: list[str],
    source_scene_id: str = "",
) -> dict[str, Any]:
    normalized_spend_id = spend_id.strip()
    normalized_item_id = item_id.strip()
    normalized_reason = reason.strip()
    cited_scene_id = source_scene_id.strip() or scene_id
    recipients = list(dict.fromkeys(knowledge_actor_ids))
    if not all(
        (
            scene_id,
            location_key,
            source_excerpt.strip(),
            normalized_spend_id,
            normalized_item_id,
            normalized_reason,
        )
    ):
        raise ValueError(
            "spend-item requires scene, location, excerpt, spend id, item id, and reason"
        )
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError("spend-item requires a positive item quantity")
    if not recipients or len(recipients) != len(knowledge_actor_ids):
        raise ValueError("spend-item requires unique actor knowledge recipients")

    source_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": cited_scene_id},
        },
    )
    exact_ref = _validate_source_ref(source_scene, source_ref, excerpt=source_excerpt)
    occurrence_scene = (
        source_scene
        if cited_scene_id == scene_id
        else await client.domain(
            "module_query",
            {
                "campaign_id": campaign_id,
                "view": "scene",
                "payload": {"scene_id": scene_id},
            },
        )
    )
    if location_key not in {
        str(item.get("key") or "") for item in _scene_locations(occurrence_scene)
    }:
        raise ValueError("spend-item location is not present in the occurrence scene atlas")
    serialized_source_ref = json.dumps(
        exact_ref,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    campaign = await _campaign(client, campaign_id)
    prior = next(
        (
            dict(item)
            for item in list(dict(campaign.get("state") or {}).get("item_spends") or [])
            if isinstance(item, dict) and str(item.get("id") or "") == normalized_spend_id
        ),
        None,
    )
    recovered = prior is not None
    if prior is not None:
        expected = {
            "id": normalized_spend_id,
            "item_id": normalized_item_id,
            "quantity": quantity,
            "reason": normalized_reason,
            "source_ref": serialized_source_ref,
        }
        if any(prior.get(key) != value for key, value in expected.items()):
            raise RuntimeError("existing item spend does not match this request")
        spent: dict[str, Any] = {
            "status": "recovered",
            "spend_id": normalized_spend_id,
            "item_id": normalized_item_id,
            "quantity": quantity,
            "removed": deepcopy(prior.get("removed") or {}),
            "reason": normalized_reason,
            "source_ref": serialized_source_ref,
        }
    else:
        spent = await client.domain(
            "campaign_change",
            {
                "campaign_id": campaign_id,
                "action": "item_spend",
                "payload": {
                    "spend_id": normalized_spend_id,
                    "item_id": normalized_item_id,
                    "quantity": quantity,
                    "reason": normalized_reason,
                    "source_ref": serialized_source_ref,
                },
                "expected_revision": campaign["revision"],
                "idempotency_key": _mutation_key(
                    run_id, "item-spend", normalized_spend_id
                ),
            },
        )
        if spent.get("status") != "committed":
            raise RuntimeError("source-bound item spend did not commit")

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
                    "summary": normalized_reason,
                    "event_type": "item_spent",
                    "audience_scope": "party",
                    "payload": {
                        "scene_id": scene_id,
                        "location_key": location_key,
                        "spend_id": normalized_spend_id,
                        "item_id": normalized_item_id,
                        "quantity": quantity,
                        "removed": deepcopy(spent.get("removed") or {}),
                        "source_excerpt": source_excerpt.strip(),
                        "source_ref": exact_ref,
                    },
                },
                "actor_knowledge": [
                    {
                        "actor_id": actor_id,
                        "knowledge_key": (
                            f"playthrough.{_token(run_id)}.item-spend."
                            f"{_token(normalized_spend_id)}"
                        ),
                        "proposition": normalized_reason,
                        "disclosure_scope": "owner",
                    }
                    for actor_id in recipients
                ],
                "snapshot": {
                    "label": f"Full playthrough item spend: {normalized_spend_id}"
                },
                "branch_id": str(branch["id"]),
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id, "item-spend-continuity", normalized_spend_id
            ),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"item-spend-sync:{normalized_spend_id}",
    )
    return {
        "scene": {
            "scene_id": scene_id,
            "location_key": location_key,
            "source_scene_id": cited_scene_id,
            "source_ref": exact_ref,
        },
        "spend": spent,
        "spend_recovered": recovered,
        "knowledge_actor_ids": recipients,
        "continuity": committed,
        "sync": synced,
    }


async def _use_shared_consumable(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    scene_id: str,
    location_key: str,
    use_id: str,
    item_id: str,
    target_character_id: str,
    reason: str,
    knowledge_actor_ids: list[str],
) -> dict[str, Any]:
    normalized_use_id = use_id.strip()
    normalized_reason = reason.strip()
    if not all(
        (
            scene_id,
            location_key,
            normalized_use_id,
            item_id.strip(),
            target_character_id.strip(),
            normalized_reason,
        )
    ):
        raise ValueError(
            "use-consumable requires scene, location, use id, item id, target, and reason"
        )
    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    if location_key not in {str(item.get("key") or "") for item in _scene_locations(scene)}:
        raise ValueError("use-consumable location is not present in the scene atlas")
    target = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": target_character_id}},
    )
    if target.get("campaign_id") != campaign_id:
        raise ValueError("use-consumable target does not belong to the campaign")

    campaign = await _campaign(client, campaign_id)
    prior = next(
        (
            dict(item)
            for item in list(dict(campaign.get("state") or {}).get("consumable_uses") or [])
            if isinstance(item, dict) and str(item.get("id") or "") == normalized_use_id
        ),
        None,
    )
    recovered = prior is not None
    if prior is not None:
        if (
            str(dict(prior.get("item") or {}).get("id") or "") != item_id
            or str(prior.get("target_character_id") or "") != target_character_id
            or str(prior.get("reason") or "") != normalized_reason
        ):
            raise RuntimeError("existing consumable use does not match this request")
        used: dict[str, Any] = {
            "status": "recovered",
            "use_id": normalized_use_id,
            "item": deepcopy(prior["item"]),
            "target_character_id": target_character_id,
            "reason": normalized_reason,
            "formula": prior["formula"],
            "roll": deepcopy(prior["roll"]),
            "healing": deepcopy(prior["healing"]),
        }
    else:
        used = await client.domain(
            "campaign_change",
            {
                "campaign_id": campaign_id,
                "action": "consumable_use",
                "payload": {
                    "use_id": normalized_use_id,
                    "item_id": item_id,
                    "target_character_id": target_character_id,
                    "expected_character_revision": target["revision"],
                    "reason": normalized_reason,
                },
                "expected_revision": campaign["revision"],
                "idempotency_key": _mutation_key(run_id, "consumable-use", normalized_use_id),
            },
        )
        if used.get("status") != "committed":
            raise RuntimeError("shared consumable use did not commit")

    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    recipients = list(dict.fromkeys([target_character_id, *knowledge_actor_ids]))
    campaign = await _campaign(client, campaign_id)
    committed = await client.domain(
        "continuity_commit",
        {
            "campaign_id": campaign_id,
            "payload": {
                "event": {
                    "summary": normalized_reason,
                    "event_type": "consumable_used",
                    "audience_scope": "party",
                    "payload": {
                        "scene_id": scene_id,
                        "location_key": location_key,
                        "use_id": normalized_use_id,
                        "item_id": item_id,
                        "target_character_id": target_character_id,
                        "formula": used["formula"],
                        "roll": deepcopy(used["roll"]),
                        "healing": deepcopy(used["healing"]),
                    },
                },
                "actor_knowledge": [
                    {
                        "actor_id": actor_id,
                        "knowledge_key": (
                            f"playthrough.{_token(run_id)}.consumable.{_token(normalized_use_id)}"
                        ),
                        "proposition": normalized_reason,
                        "disclosure_scope": "owner",
                    }
                    for actor_id in recipients
                ],
                "snapshot": {"label": f"Full playthrough consumable: {normalized_use_id}"},
                "branch_id": str(branch["id"]),
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(run_id, "consumable-continuity", normalized_use_id),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"consumable-sync:{normalized_use_id}",
    )
    return {
        "scene": {"scene_id": scene_id, "location_key": location_key},
        "target": {"id": target_character_id, "name": target["name"]},
        "use": used,
        "use_recovered": recovered,
        "knowledge_actor_ids": recipients,
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
    recipient_identity = ",".join(sorted(actor_ids))
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
            "idempotency_key": _mutation_key(
                run_id,
                "experience-award",
                f"{scene_id}:{amount}:{recipient_identity}",
            ),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"award-xp-sync:{scene_id}:{amount}:{recipient_identity}",
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


def _level_audit_source(source_ref: dict[str, Any]) -> str:
    return (
        f"module:{source_ref['module_id']}:scene:{source_ref['scene_id']}:"
        f"chunk:{source_ref['chunk_id']}:pages:{source_ref['page_start']}-"
        f"{source_ref['page_end']}:sha256:{source_ref['content_sha256']}"
    )


def _level_feature_selections(
    values: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in values:
        if not isinstance(item, dict) or set(item) != {"artifact_id", "selection"}:
            raise ValueError(
                "every level feature selection must contain only artifact_id and selection"
            )
        artifact_id = str(item.get("artifact_id") or "").strip()
        selection = item.get("selection")
        if not artifact_id or not isinstance(selection, dict):
            raise ValueError("level feature artifact_id and selection object are required")
        if artifact_id in result:
            raise ValueError("level feature selection artifact ids must be unique")
        result[artifact_id] = deepcopy(selection)
    return result


def _level_spell_selections(values: list[dict[str, Any]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    artifact_ids: set[str] = set()
    allowed_methods = {"known", "spellbook", "class_prepared"}
    for item in values:
        if not isinstance(item, dict) or set(item) != {
            "artifact_id",
            "source_class",
            "method",
        }:
            raise ValueError(
                "every level spell selection must contain only artifact_id, "
                "source_class, and method"
            )
        selection = {key: str(item.get(key) or "").strip() for key in item}
        artifact_id = selection["artifact_id"]
        if not artifact_id or not selection["source_class"]:
            raise ValueError("level spell artifact_id and source_class are required")
        if selection["method"] not in allowed_methods:
            raise ValueError("level spell method must be known, spellbook, or class_prepared")
        if artifact_id in artifact_ids:
            raise ValueError("level spell artifact ids must be unique")
        artifact_ids.add(artifact_id)
        result.append(selection)
    return result


async def _advance_level(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    initial_phase: str,
    return_phase: str,
    scene_id: str,
    source_ref: dict[str, Any] | None,
    actor_id: str,
    target_level: int | None,
    class_name: str,
    hp_method: str,
    reason: str,
    subclass_artifact_id: str,
    feature_selection_values: list[dict[str, Any]],
    spell_selection_values: list[dict[str, Any]],
    prepared_spell_ids: list[str],
    checkpoint_label: str,
) -> dict[str, Any]:
    normalized_class = class_name.strip()
    normalized_reason = reason.strip()
    if (
        not actor_id
        or target_level is None
        or not normalized_class
        or hp_method not in {"fixed", "rolled"}
        or not normalized_reason
        or return_phase not in {"lobby", "play"}
        or not scene_id
    ):
        raise ValueError(
            "advance-level requires actor, target level, class, HP method, reason, "
            "return phase, scene, and exact source reference"
        )
    if target_level < 2 or target_level > 20:
        raise ValueError("level target must be between 2 and 20")
    if initial_phase == "combat":
        raise RuntimeError("advance-level cannot run during active combat")
    if len(prepared_spell_ids) != len(set(prepared_spell_ids)):
        raise ValueError("prepared spell ids must be unique")
    feature_selections = _level_feature_selections(feature_selection_values)
    spell_selections = _level_spell_selections(spell_selection_values)
    if any(
        item["source_class"].casefold() != normalized_class.casefold() for item in spell_selections
    ):
        raise ValueError("every level spell source_class must match the advanced class")

    await client.load(*_scene_groups(initial_phase), _character_group(initial_phase))
    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    exact_ref = _validate_source_ref(scene, source_ref)
    audit_source = _level_audit_source(exact_ref)
    if len(audit_source) + len(normalized_reason) + 2 > 300:
        raise ValueError("level source reference and reason exceed the audited 300-character limit")
    actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": actor_id}},
    )
    if actor.get("campaign_id") != campaign_id:
        raise ValueError("advance-level actor does not belong to the campaign")
    progression = dict(dict(actor.get("sheet") or {}).get("progression") or {})
    current_level = int(progression.get("level", 0) or 0)
    classes = list(progression.get("classes") or [])
    if len(classes) != 1 or str(classes[0].get("name") or "").casefold() != (
        normalized_class.casefold()
    ):
        raise ValueError("advance-level currently requires the actor's single existing class")
    if current_level not in {target_level - 1, target_level}:
        raise ValueError("advance-level can apply or resume exactly one target level at a time")

    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    branch_id = str(branch["id"])
    phase_changes: list[dict[str, Any]] = []
    if initial_phase == "play":
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
                            (
                                f"level-{actor_id}-{target_level}-enter-lobby-"
                                f"r{campaign['revision']}"
                            ),
                        ),
                    },
                )
            )
        )
    await client.open(campaign_id)
    await client.load("lobby.campaign", "lobby.characters", "lobby.rules")
    actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": actor_id}},
    )
    advanced = _facade_value(
        await client.domain(
            "character_state_change",
            {
                "character_id": actor_id,
                "action": "level_advance",
                "payload": {
                    "class_name": normalized_class,
                    "hp_method": hp_method,
                    "reason": normalized_reason,
                    "source_ref": audit_source,
                },
                "expected_revision": actor["revision"],
                "idempotency_key": _mutation_key(
                    run_id, "level-advance", f"{actor_id}:level-{target_level}"
                ),
            },
        )
    )
    if advanced.get("status") != "committed":
        raise RuntimeError("character level advancement did not commit")
    actor = dict(advanced["character"])
    follow_up = dict(dict(advanced["advancement"]).get("follow_up") or {})

    subclass_options = list(follow_up.get("subclass_options") or [])
    selected_subclass: dict[str, Any] | None = None
    if subclass_options:
        if not subclass_artifact_id:
            raise ValueError("level advancement requires an explicit subclass artifact")
        selected_subclass = next(
            (
                item
                for item in subclass_options
                if str(item.get("artifact_id") or "") == subclass_artifact_id
            ),
            None,
        )
        if selected_subclass is None:
            raise ValueError("selected subclass is not offered by this level advancement")
        applied = _facade_value(
            await client.domain(
                "character_content_apply",
                {
                    "character_id": actor_id,
                    "artifact_id": subclass_artifact_id,
                    "selection": {"target_class_name": normalized_class},
                    "expected_revision": actor["revision"],
                    "idempotency_key": _mutation_key(
                        run_id,
                        "level-subclass",
                        f"{actor_id}:level-{target_level}:{subclass_artifact_id}",
                    ),
                },
            )
        )
        if applied.get("status") == "pending_ruling":
            raise RuntimeError(f"subclass selection needs DM review: {applied['reason']}")
        actor = dict(applied.get("character") or applied)
    elif subclass_artifact_id:
        raise ValueError("this level advancement does not offer a subclass selection")

    feature_catalog = list(
        _facade_value(
            await client.domain(
                "rule_pack_query",
                {
                    "view": "content_catalog",
                    "payload": {"campaign_id": campaign_id, "kind": "feature"},
                },
            )
        )
    )
    existing_feature_ids = {
        str(item.get("id") or "")
        for item in dict(actor["sheet"].get("content") or {}).get("features", [])
    }
    actor_class = next(
        item
        for item in actor["sheet"]["progression"]["classes"]
        if str(item.get("name") or "").casefold() == normalized_class.casefold()
    )
    actor_subclass = str(actor_class.get("subclass") or "")
    required_features: dict[str, dict[str, Any]] = {
        str(item["artifact_id"]): dict(item) for item in follow_up.get("feature_artifacts") or []
    }
    for item in feature_catalog:
        requirements = dict(item.get("selection_requirements") or {})
        artifact_id = str(item.get("id") or "")
        if (
            artifact_id
            and artifact_id not in existing_feature_ids
            and str(requirements.get("class_name") or "").casefold() == normalized_class.casefold()
            and int(requirements.get("minimum_level", 1) or 1) <= target_level
            and (
                not str(requirements.get("subclass_name") or "")
                or str(requirements.get("subclass_name") or "").casefold()
                == actor_subclass.casefold()
            )
        ):
            required_features.setdefault(
                artifact_id,
                {
                    "artifact_id": artifact_id,
                    "name": str(item.get("name") or artifact_id),
                    "selection_requirements": requirements,
                },
            )
    unknown_feature_selections = set(feature_selections) - set(required_features)
    if unknown_feature_selections:
        raise ValueError(
            "feature selections were supplied for artifacts not required at this level: "
            + ", ".join(sorted(unknown_feature_selections))
        )
    applied_features: list[dict[str, Any]] = []
    for artifact_id, feature in sorted(required_features.items()):
        requirements = dict(feature.get("selection_requirements") or {})
        selection = feature_selections.get(artifact_id, {})
        choice_field = str(requirements.get("field") or "")
        if choice_field and choice_field not in selection:
            raise ValueError(
                f"level feature {artifact_id} requires an explicit {choice_field} choice"
            )
        applied = _facade_value(
            await client.domain(
                "character_content_apply",
                {
                    "character_id": actor_id,
                    "artifact_id": artifact_id,
                    "selection": selection,
                    "expected_revision": actor["revision"],
                    "idempotency_key": _mutation_key(
                        run_id,
                        "level-feature",
                        f"{actor_id}:level-{target_level}:{artifact_id}",
                    ),
                },
            )
        )
        if applied.get("status") == "pending_ruling":
            raise RuntimeError(f"level feature needs DM review: {artifact_id}: {applied['reason']}")
        actor = dict(applied.get("character") or applied)
        applied_features.append({"artifact_id": artifact_id, "selection": deepcopy(selection)})

    spell_catalog = list(
        _facade_value(
            await client.domain(
                "rule_pack_query",
                {
                    "view": "content_catalog",
                    "payload": {"campaign_id": campaign_id, "kind": "spell"},
                },
            )
        )
    )
    spell_by_id = {str(item["id"]): item for item in spell_catalog}
    spell_choices = dict(follow_up.get("spell_choices") or {})
    required_cantrips = int(spell_choices.get("cantrips_to_add", 0) or 0)
    required_leveled = int(spell_choices.get("leveled_spells_to_add", 0) or 0)
    selected_cantrips = 0
    selected_leveled = 0
    for selection in spell_selections:
        artifact = spell_by_id.get(selection["artifact_id"])
        if artifact is None:
            raise ValueError(
                f"selected level spell is not in the active catalog: {selection['artifact_id']}"
            )
        requirements = dict(artifact.get("selection_requirements") or {})
        eligible_classes = {
            str(item).casefold() for item in requirements.get("eligible_classes") or []
        }
        if normalized_class.casefold() not in eligible_classes:
            raise ValueError("selected level spell is not eligible for the advanced class")
        spell_level = int(requirements.get("level", 0) or 0)
        if spell_level == 0:
            selected_cantrips += 1
            if selection["method"] != "known":
                raise ValueError("selected cantrips must use the known method")
        else:
            selected_leveled += 1
    if (selected_cantrips, selected_leveled) != (
        required_cantrips,
        required_leveled,
    ):
        raise ValueError(
            "level spell selections do not satisfy the reported cantrip and leveled-spell "
            f"choices: expected {required_cantrips}/{required_leveled}, got "
            f"{selected_cantrips}/{selected_leveled}"
        )
    applied_spells: list[str] = []
    for selection in spell_selections:
        artifact_id = selection["artifact_id"]
        applied = _facade_value(
            await client.domain(
                "character_content_apply",
                {
                    "character_id": actor_id,
                    "artifact_id": artifact_id,
                    "selection": {
                        "source_class": selection["source_class"],
                        "method": selection["method"],
                    },
                    "expected_revision": actor["revision"],
                    "idempotency_key": _mutation_key(
                        run_id,
                        "level-spell",
                        f"{actor_id}:level-{target_level}:{artifact_id}",
                    ),
                },
            )
        )
        if applied.get("status") == "pending_ruling":
            raise RuntimeError(f"level spell needs DM review: {artifact_id}: {applied['reason']}")
        actor = dict(applied.get("character") or applied)
        applied_spells.append(artifact_id)

    prepared_event = str(follow_up.get("prepared_spell_event") or "")
    prepared = None
    if prepared_event:
        if not prepared_spell_ids:
            raise ValueError(
                "prepared or spellbook advancement requires an explicit complete "
                "prepared-spell list"
            )
        prepared = _facade_value(
            await client.domain(
                "character_spell_prepare",
                {
                    "character_id": actor_id,
                    "mode": "replace_all",
                    "payload": {
                        "spell_ids": prepared_spell_ids,
                        "event": prepared_event,
                    },
                    "expected_revision": actor["revision"],
                    "idempotency_key": _mutation_key(
                        run_id,
                        "level-prepare",
                        f"{actor_id}:level-{target_level}",
                    ),
                },
            )
        )
        actor = dict(prepared.get("character") or prepared)
    elif prepared_spell_ids:
        raise ValueError("this level advancement does not allow a prepared-spell event")

    verified_actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": actor_id}},
    )
    verified_sheet = dict(verified_actor["sheet"])
    if int(dict(verified_sheet["progression"]).get("level", 0) or 0) != target_level:
        raise RuntimeError("level advancement verification found the wrong actor level")
    verified_features = {
        str(item.get("id") or "")
        for item in dict(verified_sheet.get("content") or {}).get("features", [])
    }
    if not set(required_features).issubset(verified_features):
        raise RuntimeError("level advancement verification found missing feature artifacts")
    verified_spells = {
        str(item.get("id") or "")
        for item in dict(verified_sheet.get("content") or {}).get("spells", [])
    }
    if not set(applied_spells).issubset(verified_spells):
        raise RuntimeError("level advancement verification found missing spell artifacts")
    if prepared_event:
        actual_prepared = set(
            dict(verified_sheet["spellcasting"]["preparation"]).get("selected_spell_ids", [])
        )
        if actual_prepared != set(prepared_spell_ids):
            raise RuntimeError("level advancement verification found the wrong prepared spells")
    if selected_subclass is not None:
        verified_class = next(
            item
            for item in verified_sheet["progression"]["classes"]
            if str(item.get("name") or "").casefold() == normalized_class.casefold()
        )
        if str(verified_class.get("subclass") or "") != str(selected_subclass["name"]):
            raise RuntimeError("level advancement verification found the wrong subclass")

    if return_phase == "play":
        campaign = await _campaign(client, campaign_id)
        if _campaign_phase(campaign) != "play":
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
                                (
                                    f"level-{actor_id}-{target_level}-return-play-"
                                    f"r{campaign['revision']}"
                                ),
                            ),
                        },
                    )
                )
            )
            await client.open(campaign_id)
            await client.load("play.scene", "play.scene_control")
    label = checkpoint_label.strip() or (
        f"Level {target_level} advancement: {verified_actor['name']}"
    )
    checkpoint = await _checkpoint(
        client,
        campaign_id=campaign_id,
        run_id=run_id,
        label=label,
    )
    return {
        "actor": verified_actor,
        "target_level": target_level,
        "source_ref": exact_ref,
        "audit_source": audit_source,
        "advancement": advanced["advancement"],
        "selected_subclass": selected_subclass,
        "applied_features": applied_features,
        "applied_spells": applied_spells,
        "prepared": prepared,
        "phase_changes": phase_changes,
        "return_phase": return_phase,
        "checkpoint": checkpoint,
    }


async def _relock_core(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    reason: str,
) -> dict[str, Any]:
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise ValueError("relock-core requires --core-relock-reason")
    profile = await client.domain(
        "campaign_rules",
        {
            "campaign_id": campaign_id,
            "action": "get_profile",
        },
    )
    profile_data = dict(profile.get("profile") or profile)
    lock = dict(dict(profile_data.get("options") or {}).get("_core_rule_pack_lock") or {})
    previous_fingerprint = str(lock.get("fingerprint") or "")
    if not previous_fingerprint:
        raise RuntimeError("campaign rule profile has no Core fingerprint lock")
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None or not branch.get("head_snapshot_id"):
        raise RuntimeError("Core relock requires a current branch head snapshot")
    campaign = await _campaign(client, campaign_id)
    relocked = await client.domain(
        "campaign_core_relock",
        {
            "campaign_id": campaign_id,
            "expected_core_fingerprint": previous_fingerprint,
            "reason": normalized_reason,
            "branch_id": str(branch["id"]),
            "expected_revision": campaign["revision"],
            "expected_head_snapshot_id": str(branch["head_snapshot_id"]),
            "idempotency_key": _mutation_key(run_id, "core-relock", previous_fingerprint),
        },
    )
    if relocked.get("status") != "relocked":
        raise RuntimeError("Core relock did not commit")
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"core-relock-sync:{previous_fingerprint}",
    )
    return {
        "reason": normalized_reason,
        "previous_core_fingerprint": previous_fingerprint,
        "checkpoint_snapshot_id": str(branch["head_snapshot_id"]),
        "relock": relocked,
        "sync": synced,
    }


async def _refresh_module(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    initial_phase: str,
    source_path: Path | None,
    source_key: str,
    title: str,
    return_phase: str = "",
) -> dict[str, Any]:
    if initial_phase not in {"lobby", "play"}:
        raise RuntimeError("refresh-module cannot run during active combat")
    target_phase = return_phase.strip() or initial_phase
    if target_phase not in {"lobby", "play"}:
        raise ValueError("refresh-module return phase must be lobby or play")
    if source_path is None:
        raise ValueError("refresh-module requires --module-source-path")
    manifest_result = await _manifest_get(client, campaign_id)
    manifest = manifest_result["manifest"]
    old_module_id = str(manifest["current"].get("module_id") or "")
    if not old_module_id:
        raise ValueError("refresh-module requires a current manifest module")
    if initial_phase == "lobby":
        await client.load("lobby.modules")
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
    phase_changes: list[dict[str, Any]] = []
    if initial_phase == "play":
        phase_changes.append(
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
        )
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
    if target_phase == "play":
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
        "return_phase": target_phase,
        "sync": synced,
    }


async def _restore_phase_after_failed_refresh(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    original_phase: str,
) -> dict[str, Any] | None:
    """Restore the entry exposure when a refresh fails after entering Lobby."""
    return await _restore_phase_after_failed_lobby_action(
        client,
        campaign_id=campaign_id,
        run_id=run_id,
        original_phase=original_phase,
        identity="refresh",
    )


async def _restore_phase_after_failed_lobby_action(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    original_phase: str,
    identity: str,
) -> dict[str, Any] | None:
    """Restore the entry exposure after a resumable Lobby-only action fails."""
    if original_phase not in {"lobby", "play"}:
        return None
    campaign = await _campaign(client, campaign_id)
    current_phase = _campaign_phase(campaign)
    if current_phase == original_phase:
        return None
    if current_phase not in {"lobby", "play"}:
        raise RuntimeError("failed module refresh left the campaign in combat")
    await client.open(campaign_id)
    await client.load(*_phase_groups(current_phase))
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch for phase recovery")
    restored = _facade_value(
        await client.core(
            "game_phase",
            {
                "campaign_id": campaign_id,
                "action": "set",
                "tool_profile": original_phase,
                "expected_revision": campaign["revision"],
                "branch_id": branch["id"],
                "idempotency_key": _mutation_key(
                    run_id,
                    "phase",
                    (
                        f"{_token(identity)}-failure-restore-{original_phase}-"
                        f"r{campaign['revision']}"
                    ),
                ),
            },
        )
    )
    await client.open(campaign_id)
    await client.load(*_phase_groups(original_phase))
    return restored


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
            elif args.action == "register-replacement":
                if phase != "play":
                    raise RuntimeError("register-replacement requires the play phase")
                await client.load("play.characters")
                report["result"] = await _register_replacement(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    predecessor_actor_id=args.replacement_predecessor_id,
                    replacement_actor_id=args.replacement_actor_id,
                    scene_id=str(args.scene_id or ""),
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    summary=args.event_summary,
                    handoff_knowledge=args.replacement_knowledge,
                    witness_actor_ids=args.knowledge_actor_id,
                )
            elif args.action == "prepare-narrative-npc":
                try:
                    report["result"] = await _prepare_narrative_npc(
                        client,
                        campaign_id=args.campaign_id,
                        run_id=args.run_id,
                        initial_phase=phase,
                        scene_id=str(args.scene_id or ""),
                        location_key=args.location_key,
                        source_excerpt=args.source_excerpt,
                        source_ref=args.source_ref_json,
                        name=args.narrative_npc_name,
                        role=args.narrative_npc_role,
                        summary=args.narrative_npc_summary,
                        faction=args.narrative_npc_faction,
                        relationship=args.narrative_npc_relationship,
                    )
                except Exception:
                    await _restore_phase_after_failed_lobby_action(
                        client,
                        campaign_id=args.campaign_id,
                        run_id=args.run_id,
                        original_phase=phase,
                        identity=f"narrative-npc-{args.narrative_npc_name}",
                    )
                    raise
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
            elif args.action == "relock-core":
                if phase == "combat":
                    raise RuntimeError("relock-core cannot run during active combat")
                report["result"] = await _relock_core(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    reason=args.core_relock_reason,
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
                try:
                    report["result"] = await _refresh_module(
                        client,
                        campaign_id=args.campaign_id,
                        run_id=args.run_id,
                        initial_phase=phase,
                        source_path=args.module_source_path,
                        source_key=args.module_source_key,
                        title=args.module_title,
                        return_phase=args.refresh_return_phase,
                    )
                except Exception:
                    await _restore_phase_after_failed_refresh(
                        client,
                        campaign_id=args.campaign_id,
                        run_id=args.run_id,
                        original_phase=phase,
                    )
                    raise
            elif args.action == "query-source":
                report["result"] = await _query_source(
                    client,
                    campaign_id=args.campaign_id,
                    query=args.source_query,
                    top_k=args.source_top_k,
                    expand=args.source_expand,
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
            elif args.action == "advance-time":
                if phase != "play":
                    raise RuntimeError("advance-time requires the play phase")
                await client.load("play.characters")
                report["result"] = await _advance_time(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    scene_id=str(args.scene_id or ""),
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    period=str(args.time_period or ""),
                    count=args.time_count,
                    reason=args.time_reason,
                    start_clock=args.time_start_clock_json,
                    knowledge_actor_ids=args.knowledge_actor_id,
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
                    audience_scope=args.event_audience_scope,
                )
            elif args.action == "record-outcome":
                if phase != "play":
                    raise RuntimeError("record-outcome requires the play phase")
                report["result"] = await _record_outcome(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    outcome_id=args.outcome_id,
                    scene_id=str(args.scene_id or ""),
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    event_type=args.event_type,
                    summary=args.event_summary,
                    knowledge=args.event_knowledge,
                    knowledge_actor_ids=args.event_knowledge_actor_id,
                    facts=args.fact_json,
                    npc_states=args.npc_state_json,
                    quest_states=args.quest_state_json,
                    clue_states=args.clue_state_json,
                    world_state=args.world_state_json,
                    objective=args.objective,
                    progress_percent=args.progress_percent,
                    audience_scope=args.event_audience_scope,
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
            elif args.action == "recover-stable":
                if phase != "play":
                    raise RuntimeError("recover-stable requires the play phase")
                await client.load("play.characters")
                report["result"] = await _recover_stable_party(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    actor_ids=args.recovery_actor_id,
                    knowledge_actor_ids=args.knowledge_actor_id,
                    reason=args.rest_reason,
                )
            elif args.action == "acquire-loot":
                if phase != "play":
                    raise RuntimeError("acquire-loot requires the play phase")
                report["result"] = await _acquire_source_loot(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    scene_id=str(args.scene_id or ""),
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    acquisition_id=args.loot_acquisition_id,
                    coins=args.loot_coins_json,
                    items=args.loot_item_json,
                    reason=args.loot_reason,
                    knowledge_actor_ids=args.knowledge_actor_id,
                    source_scene_id=args.source_scene_id,
                )
            elif args.action == "spend-coins":
                if phase != "play":
                    raise RuntimeError("spend-coins requires the play phase")
                report["result"] = await _spend_source_currency(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    scene_id=str(args.scene_id or ""),
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    spend_id=args.spend_id,
                    coins=args.spend_coins_json,
                    reason=args.spend_reason,
                    rule_ref=args.spend_rule_ref,
                    knowledge_actor_ids=args.knowledge_actor_id,
                    source_scene_id=args.source_scene_id,
                )
            elif args.action == "spend-item":
                if phase != "play":
                    raise RuntimeError("spend-item requires the play phase")
                report["result"] = await _spend_source_item(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    scene_id=str(args.scene_id or ""),
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    spend_id=args.spend_id,
                    item_id=args.spend_item_id,
                    quantity=args.spend_item_quantity,
                    reason=args.spend_reason,
                    knowledge_actor_ids=args.knowledge_actor_id,
                    source_scene_id=args.source_scene_id,
                )
            elif args.action == "use-consumable":
                if phase != "play":
                    raise RuntimeError("use-consumable requires the play phase")
                await client.load("play.characters")
                report["result"] = await _use_shared_consumable(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    scene_id=str(args.scene_id or ""),
                    location_key=args.location_key,
                    use_id=args.consumable_use_id,
                    item_id=args.consumable_item_id,
                    target_character_id=args.consumable_target_id,
                    reason=args.consumable_reason,
                    knowledge_actor_ids=args.knowledge_actor_id,
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
            elif args.action == "advance-level":
                report["result"] = await _advance_level(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    initial_phase=phase,
                    return_phase=str(args.level_return_phase or ""),
                    scene_id=str(args.scene_id or ""),
                    source_ref=args.source_ref_json,
                    actor_id=args.level_actor_id,
                    target_level=args.level_target,
                    class_name=args.level_class_name,
                    hp_method=str(args.level_hp_method or ""),
                    reason=args.level_reason,
                    subclass_artifact_id=args.level_subclass_artifact_id,
                    feature_selection_values=args.level_feature_selection_json,
                    spell_selection_values=args.level_spell_json,
                    prepared_spell_ids=args.level_prepared_spell_id,
                    checkpoint_label=args.checkpoint_label,
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
