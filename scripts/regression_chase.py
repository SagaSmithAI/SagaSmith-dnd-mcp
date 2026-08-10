"""Run a source-reviewed D&D chase exclusively through public stdio MCP tools."""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from sagasmith_core.modules import (
    normalize_source_evidence_text as _normalized_source_text,
)

from scripts.regression_modules import (
    ExposureClient,
    _facade_value,
    _token,
    campaign_view,
)
from scripts.regression_playthrough import _checkpoint
from scripts.regression_runtime import regression_server_parameters


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
    parser.add_argument(
        "--agent-start-ruling-json",
        type=json.loads,
        required=True,
        help=(
            "Explicit Agent-as-DM start-distance ruling with initial_distance_ft, "
            "decision, and ruling_reason"
        ),
    )
    parser.add_argument(
        "--agent-turn-policy-json",
        action="append",
        type=json.loads,
        required=True,
        help=(
            "Explicit reusable Agent chase policy for one participant: actor_id, "
            "turn_action, stand_from_prone, complication_choices for every result "
            "1 through 10, decision, and ruling_reason"
        ),
    )
    parser.add_argument(
        "--agent-quarry-visibility-ruling-json",
        type=json.loads,
        required=True,
        help=(
            "Agent-as-DM current-scene visibility ruling with an exact "
            "quarry_visibility boolean map, decision, and ruling_reason"
        ),
    )
    parser.add_argument(
        "--source-speed-adjustment-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Source-authored chase-speed adjustment with actor_id, signed "
            "speed_adjustment_ft, and an exact excerpt contained in --source-excerpt"
        ),
    )
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
    return regression_server_parameters(
        home=args.home,
        auto_seed=True,
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
    return await campaign_view(client, campaign_id)


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


_COMPLICATION_CHOICES = {
    1: {"acrobatics"},
    2: {"athletics", "acrobatics"},
    3: {"strength"},
    4: {"acrobatics", "intelligence"},
    5: {"dexterity"},
    6: {"acrobatics"},
    7: {"athletics", "acrobatics", "intimidation"},
    8: {"athletics", "acrobatics", "intimidation"},
    9: {""},
    10: {"dexterity"},
}


def _bounded_agent_text(value: Any, *, field: str) -> str:
    text = " ".join(str(value or "").split())
    if not 10 <= len(text) <= 500:
        raise ValueError(f"{field} must contain 10 to 500 characters")
    return text


def _agent_start_ruling(
    value: Any,
    *,
    initial_distance_ft: int,
) -> dict[str, Any]:
    """Validate the Agent's explicit theater-of-the-mind start distance."""

    if not isinstance(value, dict):
        raise ValueError("Agent chase start ruling must be an object")
    allowed = {"initial_distance_ft", "decision", "ruling_reason"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            "Agent chase start ruling has unsupported fields: "
            + ", ".join(unknown)
        )
    if (
        isinstance(value.get("initial_distance_ft"), bool)
        or value.get("initial_distance_ft") != initial_distance_ft
        or initial_distance_ft <= 0
    ):
        raise ValueError(
            "Agent chase start ruling must match the positive --initial-distance-ft"
        )
    return {
        "initial_distance_ft": initial_distance_ft,
        "decision": _bounded_agent_text(
            value.get("decision"),
            field="Agent chase start decision",
        ),
        "ruling_reason": _bounded_agent_text(
            value.get("ruling_reason"),
            field="Agent chase start ruling_reason",
        ),
        "default_resolver": "agent",
        "ruling_kind": "source_or_scene_fact",
    }


def _agent_turn_policies(
    values: list[dict[str, Any]],
    *,
    participant_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Require every chase action and complication choice from the Agent."""

    participants = set(participant_ids)
    normalized: dict[str, dict[str, Any]] = {}
    allowed = {
        "actor_id",
        "turn_action",
        "stand_from_prone",
        "complication_choices",
        "decision",
        "ruling_reason",
    }
    expected_numbers = {str(number) for number in _COMPLICATION_CHOICES}
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise ValueError(f"Agent chase turn policy {index} must be an object")
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(
                f"Agent chase turn policy {index} has unsupported fields: "
                + ", ".join(unknown)
            )
        actor_id = str(value.get("actor_id") or "").strip()
        turn_action = (
            str(value.get("turn_action") or "").strip().casefold().replace(" ", "_")
        )
        stand_from_prone = value.get("stand_from_prone")
        choices = value.get("complication_choices")
        if (
            actor_id not in participants
            or actor_id in normalized
            or turn_action not in {"dash", "move", "drop_out"}
            or not isinstance(stand_from_prone, bool)
            or not isinstance(choices, dict)
            or set(choices) != expected_numbers
        ):
            raise ValueError(
                f"Agent chase turn policy {index} requires one unique participant, "
                "an explicit legal turn action, stand_from_prone boolean, and exact "
                "complication choices 1 through 10"
            )
        normalized_choices: dict[str, str] = {}
        for number, allowed_choices in _COMPLICATION_CHOICES.items():
            choice = str(choices[str(number)] or "").strip().casefold()
            if choice not in allowed_choices:
                raise ValueError(
                    f"Agent chase turn policy {index} complication {number} "
                    f"must be one of {sorted(allowed_choices)}"
                )
            normalized_choices[str(number)] = choice
        normalized[actor_id] = {
            "actor_id": actor_id,
            "turn_action": turn_action,
            "stand_from_prone": stand_from_prone,
            "complication_choices": normalized_choices,
            "decision": _bounded_agent_text(
                value.get("decision"),
                field=f"Agent chase turn policy {index} decision",
            ),
            "ruling_reason": _bounded_agent_text(
                value.get("ruling_reason"),
                field=f"Agent chase turn policy {index} ruling_reason",
            ),
            "default_resolver": "agent",
            "ruling_kind": "agent_dm_adjudication",
        }
    if set(normalized) != participants:
        missing = sorted(participants - set(normalized))
        raise ValueError(
            "Agent chase turn policies must cover every participant exactly once; "
            f"missing: {missing}"
        )
    return normalized


def _agent_quarry_visibility_ruling(
    value: Any,
    *,
    quarry_ids: list[str],
) -> dict[str, Any]:
    """Validate the Agent's explicit current-scene quarry visibility facts."""

    if not isinstance(value, dict):
        raise ValueError("Agent quarry visibility ruling must be an object")
    allowed = {"quarry_visibility", "decision", "ruling_reason"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            "Agent quarry visibility ruling has unsupported fields: "
            + ", ".join(unknown)
        )
    visibility = value.get("quarry_visibility")
    if (
        not isinstance(visibility, dict)
        or set(visibility) != set(quarry_ids)
        or any(not isinstance(item, bool) for item in visibility.values())
    ):
        raise ValueError(
            "Agent quarry visibility ruling must map every quarry exactly once "
            "to a boolean"
        )
    return {
        "quarry_visibility": {
            str(identifier): bool(visibility[identifier])
            for identifier in quarry_ids
        },
        "decision": _bounded_agent_text(
            value.get("decision"),
            field="Agent quarry visibility decision",
        ),
        "ruling_reason": _bounded_agent_text(
            value.get("ruling_reason"),
            field="Agent quarry visibility ruling_reason",
        ),
        "default_resolver": "agent",
        "ruling_kind": "source_or_scene_fact",
    }


def _source_speed_adjustments(
    values: list[dict[str, Any]],
    *,
    participant_ids: list[str],
    source_excerpt: str,
) -> list[dict[str, Any]]:
    """Validate source-cited contextual speed changes without actor-name code."""

    participants = set(participant_ids)
    normalized_source = _normalized_source_text(source_excerpt)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed = {"actor_id", "speed_adjustment_ft", "source_excerpt"}
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise ValueError(f"source speed adjustment {index} must be an object")
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(
                f"source speed adjustment {index} has unsupported fields: "
                + ", ".join(unknown)
            )
        actor_id = str(value.get("actor_id") or "").strip()
        adjustment = value.get("speed_adjustment_ft")
        excerpt = " ".join(str(value.get("source_excerpt") or "").split())
        if (
            actor_id not in participants
            or actor_id in seen
            or isinstance(adjustment, bool)
            or not isinstance(adjustment, int)
            or adjustment == 0
            or not -100 <= adjustment <= 100
            or not excerpt
            or _normalized_source_text(excerpt) not in normalized_source
        ):
            raise ValueError(
                f"source speed adjustment {index} requires one unique participant, "
                "a nonzero signed adjustment from -100 to 100, and an exact "
                "encounter-source excerpt"
            )
        seen.add(actor_id)
        normalized.append(
            {
                "actor_id": actor_id,
                "speed_adjustment_ft": adjustment,
                "source_excerpt": excerpt,
            }
        )
    return normalized


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    party_ids = _party_ids(args.party_report)
    quarry_ids = [str(item) for item in args.quarry_actor_id]
    if len(quarry_ids) != len(set(quarry_ids)):
        raise ValueError("quarry actor ids must be unique")
    participant_ids = [*party_ids, *quarry_ids]
    start_ruling = _agent_start_ruling(
        args.agent_start_ruling_json,
        initial_distance_ft=args.initial_distance_ft,
    )
    turn_policies = _agent_turn_policies(
        args.agent_turn_policy_json,
        participant_ids=participant_ids,
    )
    visibility_ruling = _agent_quarry_visibility_ruling(
        args.agent_quarry_visibility_ruling_json,
        quarry_ids=quarry_ids,
    )
    source_speed_adjustments = _source_speed_adjustments(
        args.source_speed_adjustment_json,
        participant_ids=participant_ids,
        source_excerpt=args.source_excerpt,
    )
    async with stdio_client(_server_parameters(args)) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            client = ExposureClient(session)
            opened = await client.open(args.campaign_id)
            if opened.get("phase") != "play":
                raise RuntimeError("chase regression requires the play phase")
            await client.load()
            actors = await _actors(client, args.campaign_id, participant_ids)
            campaign = await _campaign(client, args.campaign_id)
            existing = _facade_value(
                await client.domain(
                    "chase",
                    {"campaign_id": args.campaign_id, "action": "query"},
                )
            )
            chase = dict(existing.get("chase") or {})
            started = None
            if not chase.get("active", False) and not chase.get("outcome"):
                started = _facade_value(
                    await client.domain(
                        "chase",
                        {
                            "campaign_id": args.campaign_id,
                            "action": "start",
                            "payload": {
                                "participant_ids": participant_ids,
                                "quarry_ids": quarry_ids,
                                "initial_distance_ft": args.initial_distance_ft,
                                "scene_id": args.scene_id,
                                "source_ref": args.source_ref_json,
                                "source_excerpt": args.source_excerpt,
                                "name": args.name,
                                "participant_config": source_speed_adjustments,
                                "close_transition": args.close_transition_json,
                            },
                            "expected_revision": campaign["revision"],
                            "idempotency_key": (
                                f"chase-start-{_token(f'{args.run_id}:{args.scene_id}', length=24)}"
                            ),
                        },
                    )
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
                policy = turn_policies[actor_id]
                complication_number = int(pending.get("number", 0) or 0)
                choice = (
                    str(policy["complication_choices"][str(complication_number)])
                    if complication_number
                    else ""
                )
                campaign = await _campaign(client, args.campaign_id)
                settled = _facade_value(
                    await client.domain(
                        "chase",
                        {
                            "campaign_id": args.campaign_id,
                            "action": "take_turn",
                            "payload": {
                                "actor_id": actor_id,
                                "turn_action": policy["turn_action"],
                                "complication_choice": choice,
                                "stand_from_prone": policy["stand_from_prone"],
                                "quarry_visibility": visibility_ruling[
                                    "quarry_visibility"
                                ],
                                "expected_actor_revision": actor["revision"],
                            },
                            "expected_revision": campaign["revision"],
                            "idempotency_key": (
                                "chase-turn-"
                                + _token(
                                    f"{args.run_id}:{chase['id']}:{sequence}:{actor_id}",
                                    length=24,
                                )
                            ),
                        },
                    )
                )
                turns.append(
                    {
                        **deepcopy(settled["turn"]),
                        "agent_turn_policy": deepcopy(policy),
                        "agent_quarry_visibility_ruling": deepcopy(
                            visibility_ruling
                        ),
                    }
                )
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
                "agent_start_ruling": start_ruling,
                "agent_turn_policies": list(turn_policies.values()),
                "agent_quarry_visibility_ruling": visibility_ruling,
                "source_speed_adjustments": source_speed_adjustments,
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
                            dict(actor.get("sheet") or {}).get("combat", {}).get("exhaustion", 0)
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
