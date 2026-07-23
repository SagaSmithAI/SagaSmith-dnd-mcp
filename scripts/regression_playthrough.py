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
            "configure-advancement",
            "register-party",
            "start-play",
            "verify-ending",
        ),
        default="status",
    )
    parser.add_argument("--run-id", default="full-playthrough-v1")
    parser.add_argument("--advancement-mode", choices=("xp", "milestone"))
    parser.add_argument("--module-root", type=Path)
    parser.add_argument("--checkpoint-label", default="")
    parser.add_argument("--scene-id")
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
        env["SAGASMITH_DND_MCP_MODULE_IMPORT_ROOTS"] = str(
            args.module_root.expanduser().resolve()
        )
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


def _phase_group(phase: str) -> str:
    return {
        "lobby": "lobby.campaign",
        "play": "play.scene_control",
        "combat": "combat.save",
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
    exclusions = {
        str(item["scene_id"]): item for item in traversal["excluded_scenes"]
    }
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
            await client.load(_phase_group(phase))
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
                    raise RuntimeError(
                        "configure-advancement cannot run during active combat"
                    )
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
                            args.checkpoint_label
                            or f"Formal campaign ending: {args.condition_id}"
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
                return [
                    message
                    for child in nested
                    for message in leaf_messages(child)
                ]
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
