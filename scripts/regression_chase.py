"""Run a source-reviewed D&D chase exclusively through public stdio MCP tools."""

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

from scripts.regression_modules import PRINCIPAL_ID, ExposureClient, _facade_value, _token
from scripts.regression_playthrough import _checkpoint


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--party-report", type=Path, required=True)
    parser.add_argument("--quarry-actor-id", action="append", required=True)
    parser.add_argument("--scene-id", required=True)
    parser.add_argument("--source-ref-json", type=json.loads, required=True)
    parser.add_argument("--source-excerpt", required=True)
    parser.add_argument("--name", default="Chase")
    parser.add_argument("--initial-distance-ft", type=int, required=True)
    parser.add_argument("--close-transition-json", type=json.loads)
    parser.add_argument("--max-turns", type=int, default=100)
    parser.add_argument("--checkpoint-label", required=True)
    parser.add_argument(
        "--defer-checkpoint",
        action="store_true",
        help=(
            "Commit the chase without creating its terminal snapshot so the caller "
            "can batch the source outcome and scene transition into one checkpoint."
        ),
    )
    return parser.parse_args()


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
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "sagasmith_dnd_mcp.server"],
        cwd=repo,
        env=env,
    )


def _party_ids(path: Path) -> list[str]:
    report = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    values = report.get("manifest_members")
    if not isinstance(values, list):
        result = report.get("result")
        manifest = result.get("manifest") if isinstance(result, dict) else None
        party = manifest.get("party") if isinstance(manifest, dict) else None
        values = party.get("members") if isinstance(party, dict) else None
    if not isinstance(values, list):
        raise ValueError("party report has no manifest members")
    actor_ids = [
        str(item.get("actor_id") or "")
        for item in values
        if isinstance(item, dict) and str(item.get("status") or "active") == "active"
    ]
    if not actor_ids or any(not item for item in actor_ids):
        raise ValueError("party report contains no active actor ids")
    if len(actor_ids) != len(set(actor_ids)):
        raise ValueError("party report actor ids must be unique")
    return actor_ids


async def _finalize_chase_checkpoint(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    label: str,
    chase_id: str,
    defer_checkpoint: bool,
) -> dict[str, Any] | None:
    if defer_checkpoint:
        return None
    return await _checkpoint(
        client,
        campaign_id=campaign_id,
        run_id=run_id,
        label=label,
        checkpoint_id=f"chase:{chase_id}",
    )


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


async def _actors(
    client: ExposureClient,
    campaign_id: str,
    actor_ids: list[str],
) -> dict[str, dict[str, Any]]:
    values = await client.domain(
        "character_query",
        {
            "view": "batch",
            "payload": {
                "campaign_id": campaign_id,
                "character_ids": actor_ids,
            },
        },
    )
    result = {
        str(item.get("id") or ""): item
        for item in values
        if isinstance(item, dict) and item.get("id")
    }
    if set(result) != set(actor_ids):
        raise RuntimeError("character query did not return every chase participant")
    return result


def _complication_choice(number: int, actor: dict[str, Any]) -> str:
    derived = dict(actor.get("derived") or {})
    skill_values = dict(derived.get("skills") or {})
    ability_values = dict(derived.get("ability_modifiers") or {})
    choices = {
        1: ("acrobatics",),
        2: ("athletics", "acrobatics"),
        3: ("strength",),
        4: ("acrobatics", "intelligence"),
        5: ("dexterity",),
        6: ("acrobatics",),
        7: ("athletics", "acrobatics", "intimidation"),
        8: ("athletics", "acrobatics", "intimidation"),
        9: ("",),
        10: ("dexterity",),
    }.get(number, ("",))
    return max(
        choices,
        key=lambda item: int(
            skill_values.get(item, ability_values.get(item, 0)) or 0
        ),
    )


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    party_ids = _party_ids(args.party_report)
    quarry_ids = [str(item) for item in args.quarry_actor_id]
    if len(quarry_ids) != len(set(quarry_ids)):
        raise ValueError("quarry actor ids must be unique")
    participant_ids = [*party_ids, *quarry_ids]
    async with stdio_client(_server_parameters(args)) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            client = ExposureClient(session)
            opened = await client.open(args.campaign_id)
            if opened.get("phase") != "play":
                raise RuntimeError("chase regression requires the play phase")
            await client.load(
                "play.chase",
                "play.characters",
                "play.scene",
                "play.scene_control",
            )
            actors = await _actors(client, args.campaign_id, participant_ids)
            campaign = await _campaign(client, args.campaign_id)
            existing = await client.domain(
                "chase_query",
                {"campaign_id": args.campaign_id},
            )
            chase = dict(existing.get("chase") or {})
            started = None
            if not chase.get("active", False) and not chase.get("outcome"):
                started = await client.domain(
                    "chase_start",
                    {
                        "campaign_id": args.campaign_id,
                        "participant_ids": participant_ids,
                        "quarry_ids": quarry_ids,
                        "initial_distance_ft": args.initial_distance_ft,
                        "scene_id": args.scene_id,
                        "source_ref": args.source_ref_json,
                        "source_excerpt": args.source_excerpt,
                        "name": args.name,
                        "close_transition": args.close_transition_json,
                        "expected_revision": campaign["revision"],
                        "idempotency_key": (
                            f"chase-start-{_token(f'{args.run_id}:{args.scene_id}', length=24)}"
                        ),
                    },
                )
                chase = dict(started["chase"])
            turns = []
            for sequence in range(args.max_turns):
                if not chase.get("active", False):
                    break
                current = dict(chase["participants"][int(chase["turn_index"])])
                actor_id = str(current["actor_id"])
                actors = await _actors(client, args.campaign_id, participant_ids)
                actor = actors[actor_id]
                pending = dict(chase.get("pending_complication") or {})
                choice = _complication_choice(int(pending.get("number", 0) or 0), actor)
                campaign = await _campaign(client, args.campaign_id)
                settled = await client.domain(
                    "chase_take_turn",
                    {
                        "campaign_id": args.campaign_id,
                        "actor_id": actor_id,
                        "action": "dash",
                        "complication_choice": choice,
                        "quarry_visibility": {
                            identifier: True for identifier in quarry_ids
                        },
                        "expected_revision": campaign["revision"],
                        "expected_actor_revision": actor["revision"],
                        "idempotency_key": (
                            "chase-turn-"
                            + _token(
                                f"{args.run_id}:{chase['id']}:{sequence}:{actor_id}",
                                length=24,
                            )
                        ),
                    },
                )
                turns.append(deepcopy(settled["turn"]))
                chase = dict(settled["chase"])
            if chase.get("active", False):
                raise RuntimeError("chase exceeded max-turns without a source outcome")
            if not chase.get("outcome"):
                raise RuntimeError("chase ended without an outcome")
            checkpoint = await _finalize_chase_checkpoint(
                client,
                campaign_id=args.campaign_id,
                run_id=args.run_id,
                label=args.checkpoint_label,
                chase_id=str(chase["id"]),
                defer_checkpoint=args.defer_checkpoint,
            )
            final_actors = await _actors(client, args.campaign_id, participant_ids)
            return {
                "action": "auto-run",
                "transport": "stdio",
                "database_access": False,
                "campaign_id": args.campaign_id,
                "run_id": args.run_id,
                "source_ref": args.source_ref_json,
                "started": started,
                "turns": turns,
                "chase": chase,
                "actors": [
                    {
                        "id": actor["id"],
                        "name": actor["name"],
                        "revision": actor["revision"],
                        "hit_points": deepcopy(
                            dict(actor.get("derived") or {}).get("hit_points") or {}
                        ),
                        "exhaustion": int(
                            dict(actor.get("sheet") or {})
                            .get("combat", {})
                            .get("exhaustion", 0)
                            or 0
                        ),
                    }
                    for actor in final_actors.values()
                ],
                "checkpoint": checkpoint,
                "passed": True,
            }


def main() -> None:
    args = _arguments()
    try:
        report = asyncio.run(_run(args))
    except Exception as error:
        report = {
            "action": "auto-run",
            "transport": "stdio",
            "database_access": False,
            "campaign_id": args.campaign_id,
            "run_id": args.run_id,
            "passed": False,
            "error": str(error),
        }
    args.output.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    args.output.expanduser().resolve().write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
