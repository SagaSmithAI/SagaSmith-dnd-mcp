"""Run a source-defined encounter exclusively through public stdio MCP tools."""

from __future__ import annotations

import argparse
import asyncio
import heapq
import json
import os
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from scripts.regression_modules import PRINCIPAL_ID, ExposureClient, _token
from scripts.regression_playthrough import _checkpoint, _manifest_get, _manifest_mutation
from scripts.regression_rulings import normalize_pending_ruling

GUIDING_BOLT_ID = "dnd5e.content.srd2014.spell.guiding-bolt"
GUIDING_BOLT_ON_HIT = (
    "The next attack against the target before the end of the caster's next turn has advantage."
)
HEALING_WORD_ID = "dnd5e.content.srd2014.spell.healing-word"
MAGIC_MISSILE_ID = "dnd5e.content.srd2014.spell.magic-missile"


class EncounterRulingRequiredError(RuntimeError):
    """Return an unresolved public-tool ruling boundary to the acting Agent."""

    def __init__(
        self,
        ruling: dict[str, Any],
        *,
        operation: str,
        actor_id: str = "",
        target_id: str = "",
        action: dict[str, Any] | None = None,
        retry_hint: str = "",
    ) -> None:
        normalized = normalize_pending_ruling(ruling)
        self.requirement = {
            "operation": operation,
            "actor_id": actor_id,
            "target_id": target_id,
            "action": deepcopy(action or {}),
            "ruling": normalized,
            **({"retry_hint": retry_hint} if retry_hint else {}),
        }
        reason = str(normalized.get("reason") or "Agent adjudication is required")
        resolver = str(normalized["default_resolver"])
        super().__init__(f"{operation} returns to {resolver}: {reason}")


def _require_pending_on_hit_choice_id(
    result: dict[str, Any],
    *,
    operation: str,
    actor_id: str,
    target_id: str,
    action: dict[str, Any],
    retry_hint: str,
) -> str:
    """Reject a pre-commit Agent ruling before treating it as an owned window."""

    choice_id = str(
        dict(result.get("result") or {}).get("pending_on_hit_ruling_id") or ""
    )
    if choice_id:
        return choice_id
    raise EncounterRulingRequiredError(
        result,
        operation=operation,
        actor_id=actor_id,
        target_id=target_id,
        action=action,
        retry_hint=retry_hint,
    )


def _encounter_operation_scope(
    args: argparse.Namespace,
    *,
    branch_id: str,
    party_ids: list[str],
    hostile_ids: list[str],
    additional_hostile_ids: list[str] | None = None,
    reinforcement_hostile_ids: list[str] | None = None,
    combat_id: str = "",
) -> str:
    excluded = {
        "action",
        "checkpoint_label",
        "home",
        "operation_scope",
        "output",
    }
    configuration = {key: value for key, value in vars(args).items() if key not in excluded}
    identity = {
        "branch_id": branch_id,
        "combat_id": combat_id,
        "party_ids": party_ids,
        "hostile_ids": hostile_ids,
        "additional_hostile_ids": list(additional_hostile_ids or []),
        "reinforcement_hostile_ids": list(reinforcement_hostile_ids or []),
        "configuration": configuration,
    }
    return _token(
        json.dumps(
            identity,
            default=str,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        length=32,
    )


def _operation_scope(args: argparse.Namespace) -> str:
    return str(getattr(args, "operation_scope", "") or args.run_id)


def _operation_token(
    args: argparse.Namespace,
    *parts: object,
    length: int = 24,
) -> str:
    identity = ":".join([_operation_scope(args), *(str(part) for part in parts)])
    return _token(identity, length=length)


def _encounter_start_operation_token(request: dict[str, Any]) -> str:
    identity = {key: value for key, value in request.items() if key != "idempotency_key"}
    return "encounter-start-" + _token(
        json.dumps(
            identity,
            default=str,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        length=24,
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--action",
        choices=("start", "status", "auto-run", "finalize"),
        required=True,
    )
    parser.add_argument("--run-id", default="full-playthrough-encounter-v1")
    parser.add_argument("--party-report", type=Path, action="append", required=True)
    parser.add_argument(
        "--agent-party-absence-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Agent-as-DM decision excluding one still-active PC from this encounter; "
            "requires actor_id and ruling_reason and preserves that actor outside combat"
        ),
    )
    parser.add_argument(
        "--party-loadout-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Agent-selected pre-initiative equipment with actor_id, item_id, and "
            "slot; repeat for distinct party equipment slots"
        ),
    )
    parser.add_argument(
        "--ally-report",
        type=Path,
        action="append",
        default=[],
        help=(
            "Prepared source-bound friendly NPC reports; allies join the encounter "
            "without becoming registered party members"
        ),
    )
    parser.add_argument(
        "--ally-actor-id",
        action="append",
        default=[],
        help=(
            "Select an exact actor from the prepared ally reports; repeat as needed. "
            "When omitted, every actor in those reports is selected."
        ),
    )
    parser.add_argument("--hostile-report", type=Path, action="append", default=[])
    parser.add_argument(
        "--hostile-actor-id",
        action="append",
        default=[],
        help=(
            "Select an exact actor from the prepared hostile reports; repeat as "
            "needed. When omitted, every actor in those reports is selected."
        ),
    )
    parser.add_argument(
        "--additional-hostile-report",
        type=Path,
        action="append",
        default=[],
        help="Already-arrived source combatants tracked as a separate manifest group",
    )
    parser.add_argument(
        "--additional-hostile-actor-id",
        action="append",
        default=[],
        help=(
            "Select an exact actor from the additional-hostile reports; repeat as "
            "needed. When omitted, every actor in those reports is selected."
        ),
    )
    parser.add_argument(
        "--reinforcement-hostile-report",
        type=Path,
        action="append",
        default=[],
        help=(
            "Source-cited reinforcements queued through public combat_join after "
            "the encounter starts; they enter at the next round boundary"
        ),
    )
    parser.add_argument(
        "--reinforcement-hostile-actor-id",
        action="append",
        default=[],
        help=(
            "Select an exact actor from the reinforcement reports; repeat as needed. "
            "When omitted, every actor in those reports is selected."
        ),
    )
    parser.add_argument(
        "--required-hostile-weapon-id",
        action="append",
        default=[],
        help=(
            "Require every source hostile to expose this reviewed weapon id. "
            "Repeat for statblocks that must provide multiple attacks."
        ),
    )
    parser.add_argument("--scene-id")
    parser.add_argument("--location-key")
    parser.add_argument("--source-excerpt")
    parser.add_argument("--encounter-name", default="Source-defined encounter")
    parser.add_argument("--hostile-label", default="Source-defined hostiles")
    parser.add_argument(
        "--hostile-source-excerpt",
        default="",
        help=(
            "Exact scene excerpt proving the primary hostile group participates; "
            "defaults to --source-excerpt when the same passage also defines the "
            "complete encounter procedure"
        ),
    )
    parser.add_argument("--additional-hostile-label", default="Additional source hostiles")
    parser.add_argument("--additional-hostile-source-excerpt", default="")
    parser.add_argument("--reinforcement-hostile-label", default="Source reinforcements")
    parser.add_argument("--reinforcement-hostile-source-excerpt", default="")
    parser.add_argument("--surprise-check-report", type=Path)
    parser.add_argument(
        "--party-stealth-check-report",
        type=Path,
        action="append",
        default=[],
        help=(
            "One source-cited Stealth check report per party member against a "
            "shared hostile passive Perception; repeat for the complete party"
        ),
    )
    parser.add_argument("--source-surprised-actor-id", action="append", default=[])
    parser.add_argument(
        "--source-condition-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Encounter-scoped source condition with condition, actor_ids, source_ref, "
            "and exact source_excerpt; repeat for independently cited conditions"
        ),
    )
    parser.add_argument(
        "--source-trait-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Source-bound participant trait with actor_id, kind, feature_id, "
            "and exact source_excerpt; currently supports regeneration"
        ),
    )
    parser.add_argument(
        "--source-target-priority-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Source-cited target priorities with actor_ids, ordered priority_groups, "
            "and an exact source_excerpt; ordering inside each group remains tactical"
        ),
    )
    parser.add_argument(
        "--reinforcement-round",
        type=int,
        default=0,
        help=(
            "Exact future round when every source-cited reinforcement enters; "
            "defaults to the next round"
        ),
    )
    parser.add_argument(
        "--agent-target-priority-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Agent tactical focus-fire decision with party actor_ids, ordered hostile "
            "priority_groups, and ruling_reason; the server still validates every target"
        ),
    )
    parser.add_argument(
        "--no-surprise",
        action="store_true",
        help="Explicitly start with neither side surprised when the cited scene warrants it",
    )
    parser.add_argument(
        "--hostiles-hidden",
        action="store_true",
        help="Keep source-positioned hostiles hidden independently of Surprise",
    )
    parser.add_argument(
        "--source-hidden-actor-id",
        action="append",
        default=[],
        help=(
            "Limit source-positioned hidden status and Stealth rolls to these initial "
            "hostiles; repeat for a mixed visible/hidden encounter"
        ),
    )
    parser.add_argument(
        "--shared-hostile-stealth",
        action="store_true",
        help=(
            "Roll one source-hostile Stealth check for the whole group only when "
            "the cited encounter explicitly says to roll once for all of them"
        ),
    )
    parser.add_argument("--flee-after-defeated", type=int, default=0)
    parser.add_argument(
        "--flee-after-damage",
        type=int,
        default=0,
        help=(
            "Source-defined cumulative damage actually applied to a designated actor "
            "before it attempts to flee"
        ),
    )
    parser.add_argument(
        "--flee-on-critical",
        action="store_true",
        help=(
            "Make the source-designated actor attempt to flee after the server "
            "settles a critical hit against it"
        ),
    )
    parser.add_argument(
        "--flee-actor-id",
        action="append",
        default=[],
        help=(
            "Source-designated actor that attempts to flee after the trigger; "
            "repeat when the source directs every surviving member of a group to flee"
        ),
    )
    parser.add_argument("--flee-trigger-defeated-actor-id", default="")
    parser.add_argument("--flee-on-start-actor-id", default="")
    parser.add_argument("--flee-destination-location-key", default="")
    parser.add_argument("--flee-source-excerpt", default="")
    parser.add_argument(
        "--source-casualty-pool-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Source-authored non-PC casualty cohort with actor_id, pool_key, "
            "initial_count, activity_name, kill_expression, injury_expression, "
            "and exact source_excerpt"
        ),
    )
    parser.add_argument(
        "--source-separation-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Source-authored minimum separation with actor_id, other_actor_ids, "
            "minimum_distance_ft, and exact source_excerpt"
        ),
    )
    parser.add_argument(
        "--agent-position-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Agent-as-DM temporary-map placement with actor_id, x, y, exact "
            "source_excerpt, and ruling_reason; repeat for every overridden participant"
        ),
    )
    parser.add_argument("--truce-after-defeated", type=int, default=0)
    parser.add_argument("--truce-actor-id", default="")
    parser.add_argument("--truce-source-excerpt", default="")
    parser.add_argument(
        "--source-opening-cast-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Source-cited opening cast with actor_id, spell_id, source_item_id, "
            "and source_excerpt; repeat to preserve an authored sequence"
        ),
    )
    parser.add_argument(
        "--source-precombat-cast-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Source-cited out-of-combat cast with actor_id, spell_id, cast_level, "
            "source_excerpt, and optional component_ruling"
        ),
    )
    parser.add_argument(
        "--source-opening-weapon-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Source-cited first attack choice with actor_id, weapon_id, and "
            "source_excerpt; repeat for independently authored openings"
        ),
    )
    parser.add_argument(
        "--source-attack-environment-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Agent-as-DM attack-environment adjudication with actor_id, "
            "direct_sunlight, the exact source_excerpt for a structured Sunlight "
            "Sensitivity trait, and ruling_reason; repeat for each affected participant"
        ),
    )
    parser.add_argument(
        "--source-avoidance-report",
        action="append",
        type=Path,
        default=[],
        help=(
            "Public record-event report proving actor knowledge of marked "
            "hazard cells that voluntary movement must route around"
        ),
    )
    parser.add_argument(
        "--source-on-hit-ruling-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Reviewed attack settlement with actor_id, weapon_id, exact "
            "source_excerpt, and condition/escape terms, "
            "id=saving_throw_condition plus save/repeat/duration terms, or "
            "id=saving_throw_damage plus save/damage/zero-HP terms; use "
            "id=dismiss when the reviewed text is already represented by the "
            "selected attack variant and adds no separate structured effect"
        ),
    )
    parser.add_argument(
        "--source-delayed-action-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Source-cited delayed participation with actor_id, until_round, and "
            "source_excerpt; the actor remains present but takes no earlier turn"
        ),
    )
    parser.add_argument(
        "--source-passive-ally-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Source-cited noncombat behavior with an allied actor_id and exact "
            "source_excerpt; the ally remains targetable but ends each turn "
            "without taking an action"
        ),
    )
    parser.add_argument(
        "--source-random-activity-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Source-bound random saving-throw activity with actor_id, activity_id, "
            "and an exact source_excerpt; the driver invokes the public activity "
            "settlement instead of substituting a weapon attack"
        ),
    )
    parser.add_argument(
        "--source-save-activity-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Source-bound deterministic saving-throw activity with actor_id, "
            "activity_id, target_has_brain, and an exact source_excerpt"
        ),
    )
    parser.add_argument(
        "--source-contest-activity-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Source-bound ability-contest activity with actor_id, activity_id, "
            "target_is_humanoid, and an exact source_excerpt"
        ),
    )
    parser.add_argument(
        "--source-zero-hp-finisher-json",
        type=json.loads,
        default=None,
        help=(
            "Source-authored zero-HP finisher with target_id, eligible actor_ids, "
            "the exact encounter source_excerpt, and the exact 2014 oil_rule_excerpt"
        ),
    )
    parser.add_argument(
        "--source-zero-hp-stabilization-json",
        type=json.loads,
        default=None,
        help=(
            "Module-authored stabilization with eligible PC actor_ids and an exact "
            "scene source_excerpt; no anonymous helper actor is invented"
        ),
    )
    parser.add_argument("--surrender-actor-id", default="")
    parser.add_argument("--surrender-at-hp", type=int, default=0)
    parser.add_argument(
        "--surrender-after-defeated",
        type=int,
        default=0,
        help=(
            "Trigger the source-designated survivor's surrender after this many "
            "source hostiles are defeated; mutually exclusive with --surrender-at-hp"
        ),
    )
    parser.add_argument("--surrender-source-excerpt", default="")
    parser.add_argument(
        "--surrender-no-escape",
        action="store_true",
        help="Confirm the source surrender condition's no-escape predicate",
    )
    parser.add_argument(
        "--knock-out-hostile-id",
        action="append",
        default=[],
        help=(
            "Hostile to capture with the public 2014/2024 melee knockout rule; "
            "repeat for multiple source-required prisoners"
        ),
    )
    parser.add_argument(
        "--required-hostile-count",
        type=int,
        help="Complete source-grounded count for the primary hostile group",
    )
    parser.add_argument(
        "--hostile-count-basis",
        default="",
        help="Exact source or recorded table-roll basis for the required hostile count",
    )
    parser.add_argument("--max-turns", type=int, default=200)
    parser.add_argument("--checkpoint-label", default="Encounter complete")
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
    server_args = ["-m", "sagasmith_dnd_mcp.server"]
    if profile_output := str(env.get("SAGASMITH_SERVER_PROFILE_OUTPUT") or "").strip():
        server_args = ["-m", "cProfile", "-o", profile_output, *server_args]
    return StdioServerParameters(
        command=sys.executable,
        args=server_args,
        cwd=repo,
        env=env,
    )


def _read_report(path: Path) -> dict[str, Any]:
    return json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))


def _party_ids(paths: list[Path]) -> list[str]:
    values: list[str] = []
    for path in paths:
        report = _read_report(path)
        characters = report.get("characters")
        if isinstance(characters, list):
            members = characters
        else:
            result = report.get("result")
            manifest = (
                result.get("manifest") if isinstance(result, dict) else report.get("manifest")
            )
            party = manifest.get("party") if isinstance(manifest, dict) else None
            members = party.get("members") if isinstance(party, dict) else None
            if not isinstance(members, list):
                members = []
            members = [
                item
                for item in members
                if isinstance(item, dict) and item.get("status") == "active"
            ]
        values.extend(str(item.get("actor_id") or "") for item in members if isinstance(item, dict))
    if not values or any(not item for item in values) or len(values) != len(set(values)):
        raise ValueError("party report must contain unique character actor_id values")
    return values


def _prepared_actor_ids(paths: list[Path], *, report_kind: str) -> list[str]:
    values: list[str] = []
    for path in paths:
        report = _read_report(path)
        actors = report.get("actors")
        if isinstance(actors, list) and actors:
            report_values = [str(item.get("id") or "") for item in actors if isinstance(item, dict)]
        else:
            report_values = [
                str(dict(dict(report.get("created") or {}).get("character") or {}).get("id") or "")
            ]
        if not report_values or any(not item for item in report_values):
            raise ValueError(f"{report_kind} report must contain prepared actor id values")
        values.extend(report_values)
    if not values or any(not item for item in values) or len(values) != len(set(values)):
        raise ValueError(f"{report_kind} reports must contain globally unique actor ids")
    return values


def _selected_prepared_actor_ids(
    paths: list[Path],
    requested_actor_ids: list[str],
    *,
    report_kind: str,
) -> list[str]:
    available = _prepared_actor_ids(paths, report_kind=report_kind) if paths else []
    requested = [str(actor_id).strip() for actor_id in requested_actor_ids]
    if not requested:
        return available
    if any(not actor_id for actor_id in requested) or len(requested) != len(set(requested)):
        raise ValueError(f"selected {report_kind} actor ids must be non-empty and unique")
    unknown = sorted(set(requested) - set(available))
    if unknown:
        raise ValueError(
            f"selected {report_kind} actor ids are absent from prepared reports: {unknown}"
        )
    return requested


def _agent_party_absences(
    values: list[dict[str, Any]],
    *,
    reported_party_ids: list[str],
) -> list[dict[str, str]]:
    allowed = {"actor_id", "ruling_reason"}
    absences: list[dict[str, str]] = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("Agent party absence entries must be JSON objects")
        unsupported = set(value) - allowed
        if unsupported:
            raise ValueError(
                "Agent party absence entries contain unsupported fields: "
                + ", ".join(sorted(unsupported))
            )
        actor_id = str(value.get("actor_id") or "").strip()
        ruling_reason = " ".join(str(value.get("ruling_reason") or "").split()).strip()
        if actor_id not in reported_party_ids:
            raise ValueError(
                "Agent party absence requires one actor from the active party reports"
            )
        if len(ruling_reason) < 10:
            raise ValueError("Agent party absence requires a concrete ruling_reason")
        absences.append({"actor_id": actor_id, "ruling_reason": ruling_reason})
    actor_ids = [item["actor_id"] for item in absences]
    if len(actor_ids) != len(set(actor_ids)):
        raise ValueError("Agent party absence actor ids must be unique")
    if len(absences) >= len(reported_party_ids):
        raise ValueError("an encounter requires at least one participating active PC")
    return absences


def _encounter_actor_groups(args: argparse.Namespace) -> dict[str, Any]:
    reported_party_ids = _party_ids(args.party_report)
    agent_party_absences = _agent_party_absences(
        getattr(args, "agent_party_absence_json", []),
        reported_party_ids=reported_party_ids,
    )
    absent_party_ids = {item["actor_id"] for item in agent_party_absences}
    groups = {
        "party_ids": [
            actor_id
            for actor_id in reported_party_ids
            if actor_id not in absent_party_ids
        ],
        "agent_party_absences": agent_party_absences,
        "ally_ids": _selected_prepared_actor_ids(
            args.ally_report,
            getattr(args, "ally_actor_id", []),
            report_kind="ally",
        ),
        "hostile_ids": _selected_prepared_actor_ids(
            args.hostile_report,
            getattr(args, "hostile_actor_id", []),
            report_kind="hostile",
        ),
        "additional_hostile_ids": _selected_prepared_actor_ids(
            args.additional_hostile_report,
            getattr(args, "additional_hostile_actor_id", []),
            report_kind="additional hostile",
        ),
        "reinforcement_hostile_ids": _selected_prepared_actor_ids(
            args.reinforcement_hostile_report,
            getattr(args, "reinforcement_hostile_actor_id", []),
            report_kind="reinforcement hostile",
        ),
    }
    required_hostile_count = getattr(args, "required_hostile_count", None)
    hostile_count_basis = str(getattr(args, "hostile_count_basis", "") or "").strip()
    if required_hostile_count is None:
        if hostile_count_basis:
            raise ValueError("--hostile-count-basis requires --required-hostile-count")
    elif (
        isinstance(required_hostile_count, bool)
        or required_hostile_count <= 0
        or not hostile_count_basis
    ):
        raise ValueError("required hostile count must be positive and include its source basis")
    elif len(groups["hostile_ids"]) != required_hostile_count:
        raise ValueError(
            "prepared primary hostile count does not match the complete "
            f"source-grounded count ({len(groups['hostile_ids'])} != "
            f"{required_hostile_count}): {hostile_count_basis}"
        )
    reinforcement_round = getattr(args, "reinforcement_round", 0)
    if (
        isinstance(reinforcement_round, bool)
        or not isinstance(reinforcement_round, int)
        or reinforcement_round < 0
        or (reinforcement_round and not groups["reinforcement_hostile_ids"])
        or (groups["reinforcement_hostile_ids"] and reinforcement_round == 1)
    ):
        raise ValueError(
            "reinforcement round must be zero/omitted for next-round entry or "
            "at least 2 with prepared source reinforcements"
        )
    actor_sets = [
        (name, set(groups[name]))
        for name in (
            "party_ids",
            "ally_ids",
            "hostile_ids",
            "additional_hostile_ids",
            "reinforcement_hostile_ids",
        )
    ]
    overlaps = [
        (left_name, right_name, sorted(left & right))
        for index, (left_name, left) in enumerate(actor_sets)
        for right_name, right in actor_sets[index + 1 :]
        if left & right
    ]
    if overlaps:
        raise ValueError(f"encounter actor reports must be disjoint: {overlaps}")
    return groups


def _require_live_active_party(
    reported_party_ids: list[str],
    manifest_result: dict[str, Any],
    *,
    agent_party_absences: list[dict[str, str]] | None = None,
) -> list[str]:
    """Reject stale reports that reintroduce departed PCs or omit replacements."""

    manifest = manifest_result.get("manifest")
    if not isinstance(manifest, dict):
        raise RuntimeError("playthrough manifest query returned no manifest")
    party = manifest.get("party")
    members = party.get("members") if isinstance(party, dict) else None
    if not isinstance(members, list):
        raise RuntimeError("playthrough manifest has no party members")
    active_ids = [
        str(item.get("actor_id") or "")
        for item in members
        if isinstance(item, dict) and item.get("status") == "active"
    ]
    if (
        not active_ids
        or any(not actor_id for actor_id in active_ids)
        or len(active_ids) != len(set(active_ids))
    ):
        raise RuntimeError("playthrough manifest active party is invalid")
    absent_ids = {
        str(item.get("actor_id") or "")
        for item in agent_party_absences or []
        if isinstance(item, dict)
    }
    represented_ids = [*reported_party_ids, *absent_ids]
    if set(represented_ids) != set(active_ids) or len(represented_ids) != len(active_ids):
        missing = sorted(set(active_ids) - set(represented_ids))
        unexpected = sorted(set(represented_ids) - set(active_ids))
        raise ValueError(
            "encounter participants and Agent absences do not match the live active party "
            f"(missing={missing}, unexpected={unexpected})"
        )
    return active_ids


def _participant_manifest(
    hostile_ids: list[str],
    *,
    label: str,
    source_excerpt: str,
    additional_hostile_ids: list[str] | None = None,
    additional_label: str = "",
    additional_source_excerpt: str = "",
    reinforcement_hostile_ids: list[str] | None = None,
    reinforcement_label: str = "",
    reinforcement_source_excerpt: str = "",
) -> dict[str, Any]:
    if not source_excerpt.strip():
        raise ValueError("encounter start requires an exact source excerpt")
    additional_ids = list(additional_hostile_ids or [])
    if additional_ids and not additional_source_excerpt.strip():
        raise ValueError("additional source hostiles require an exact source excerpt")
    reinforcement_ids = list(reinforcement_hostile_ids or [])
    if reinforcement_ids and not reinforcement_source_excerpt.strip():
        raise ValueError("source reinforcements require an exact source excerpt")
    groups = [
        {
            "key": "source-hostiles",
            "label": label,
            "role": "combatant",
            "required_count": len(hostile_ids),
            "actor_ids": hostile_ids,
            "source_excerpt": source_excerpt,
        }
    ]
    if additional_ids:
        groups.append(
            {
                "key": "additional-source-hostiles",
                "label": additional_label,
                "role": "combatant",
                "required_count": len(additional_ids),
                "actor_ids": additional_ids,
                "source_excerpt": additional_source_excerpt,
            }
        )
    if reinforcement_ids:
        groups.append(
            {
                "key": "source-reinforcements",
                "label": reinforcement_label,
                "role": "reinforcement",
                "required_count": len(reinforcement_ids),
                "actor_ids": reinforcement_ids,
                "source_excerpt": reinforcement_source_excerpt,
            }
        )
    return {
        "schema_version": 1,
        "groups": groups,
        "notes": "Exact source count; no party-size scaling was applied.",
    }


def _primary_hostile_source_excerpt(args: argparse.Namespace) -> str:
    """Keep participant identity evidence distinct from full procedure evidence."""

    return str(
        getattr(args, "hostile_source_excerpt", "")
        or getattr(args, "source_excerpt", "")
        or ""
    )


def _source_departure_patch(
    actor_id: str,
    *,
    reason: str,
    destination_location_key: str = "",
) -> dict[str, Any]:
    if not actor_id or not reason.strip():
        raise ValueError("source departure requires actor_id and reason")
    return {
        "key": "combatant_departure",
        "value": {
            "actor_id": actor_id,
            "reason": reason.strip(),
            "destination_location_key": destination_location_key.strip(),
        },
    }


def _source_separations(
    declarations: list[dict[str, Any]],
    *,
    participant_ids: list[str],
    hostile_ids: list[str],
    encounter_source_excerpt: str,
) -> dict[str, dict[str, Any]]:
    """Validate source-authored minimum combat-map separations."""

    participants = set(participant_ids)
    hostiles = set(hostile_ids)
    encounter_excerpt = _normalized_source_text(encounter_source_excerpt)
    by_actor: dict[str, dict[str, Any]] = {}
    allowed = {
        "actor_id",
        "other_actor_ids",
        "minimum_distance_ft",
        "source_excerpt",
    }
    for index, declaration in enumerate(declarations):
        if not isinstance(declaration, dict):
            raise ValueError(f"source separation {index} must be an object")
        unknown = set(declaration) - allowed
        if unknown:
            raise ValueError(
                f"source separation {index} has unsupported fields: "
                + ", ".join(sorted(unknown))
            )
        actor_id = str(declaration.get("actor_id") or "").strip()
        other_actor_ids = [
            str(item).strip() for item in declaration.get("other_actor_ids") or []
        ]
        minimum_distance_ft = declaration.get("minimum_distance_ft")
        source_excerpt = str(declaration.get("source_excerpt") or "").strip()
        if (
            actor_id not in hostiles
            or actor_id in by_actor
            or not other_actor_ids
            or any(not item for item in other_actor_ids)
            or len(other_actor_ids) != len(set(other_actor_ids))
            or actor_id in other_actor_ids
            or not set(other_actor_ids) <= participants
            or isinstance(minimum_distance_ft, bool)
            or not isinstance(minimum_distance_ft, int)
            or minimum_distance_ft <= 0
            or minimum_distance_ft % 5
            or not source_excerpt
            or _normalized_source_text(source_excerpt) not in encounter_excerpt
        ):
            raise ValueError(
                f"source separation {index} requires one unique hostile, unique other "
                "participants, a positive five-foot-grid distance, and an exact excerpt"
            )
        distance_match = re.search(
            r"\bwithout moving closer than (?P<distance>\d+) feet from the parapet\b",
            _normalized_source_text(source_excerpt),
        )
        if (
            distance_match is None
            or int(distance_match.group("distance")) != minimum_distance_ft
        ):
            raise ValueError(
                f"source separation {index} distance is not corroborated by the excerpt"
            )
        by_actor[actor_id] = {
            "actor_id": actor_id,
            "other_actor_ids": other_actor_ids,
            "minimum_distance_ft": minimum_distance_ft,
            "source_excerpt": source_excerpt,
        }
    return by_actor


def _agent_positions(
    declarations: list[dict[str, Any]],
    *,
    participant_ids: list[str],
    encounter_source_excerpt: str,
    width_cells: int = 12,
    height_cells: int = 12,
) -> dict[str, dict[str, Any]]:
    """Validate source-cited temporary-map positions chosen by the Agent as DM."""

    participants = set(participant_ids)
    encounter_excerpt = _normalized_source_text(encounter_source_excerpt)
    by_actor: dict[str, dict[str, Any]] = {}
    occupied: set[tuple[int, int]] = set()
    allowed = {"actor_id", "x", "y", "source_excerpt", "ruling_reason"}
    for index, declaration in enumerate(declarations):
        if not isinstance(declaration, dict):
            raise ValueError(f"Agent position {index} must be an object")
        unknown = set(declaration) - allowed
        if unknown:
            raise ValueError(
                f"Agent position {index} has unsupported fields: "
                + ", ".join(sorted(unknown))
            )
        actor_id = str(declaration.get("actor_id") or "").strip()
        x = declaration.get("x")
        y = declaration.get("y")
        source_excerpt = str(declaration.get("source_excerpt") or "").strip()
        ruling_reason = str(declaration.get("ruling_reason") or "").strip()
        if (
            actor_id not in participants
            or actor_id in by_actor
            or isinstance(x, bool)
            or not isinstance(x, int)
            or isinstance(y, bool)
            or not isinstance(y, int)
            or not 0 <= x < width_cells
            or not 0 <= y < height_cells
            or (x, y) in occupied
            or not source_excerpt
            or _normalized_source_text(source_excerpt) not in encounter_excerpt
            or not ruling_reason
        ):
            raise ValueError(
                f"Agent position {index} requires a unique participant and cell, "
                "an exact encounter excerpt, and an explicit ruling reason"
            )
        occupied.add((x, y))
        by_actor[actor_id] = {
            "actor_id": actor_id,
            "position": {"x": x, "y": y},
            "source_excerpt": source_excerpt,
            "ruling_reason": ruling_reason,
        }
    return by_actor


def _apply_agent_positions(
    configs: list[dict[str, Any]],
    positions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply validated Agent positions without altering any other participant facts."""

    values = deepcopy(configs)
    by_actor = {str(item["actor_id"]): item for item in values}
    for actor_id, ruling in positions.items():
        if actor_id not in by_actor:
            raise ValueError("Agent position actor is absent from participant config")
        by_actor[actor_id]["position"] = deepcopy(ruling["position"])
    occupied = [
        (
            int(dict(item.get("position") or {}).get("x", -1)),
            int(dict(item.get("position") or {}).get("y", -1)),
        )
        for item in values
    ]
    if len(occupied) != len(set(occupied)):
        raise ValueError("Agent positions overlap another encounter participant")
    return values


def _apply_source_separations(
    configs: list[dict[str, Any]],
    separations: dict[str, dict[str, Any]],
    *,
    width_cells: int = 12,
    height_cells: int = 12,
) -> list[dict[str, Any]]:
    """Place source-separated actors at the closest valid temporary-map cells."""

    values = deepcopy(configs)
    by_actor = {str(item["actor_id"]): item for item in values}
    for actor_id, separation in separations.items():
        actor = by_actor[actor_id]
        others = [by_actor[item] for item in separation["other_actor_ids"]]
        minimum_cells = int(separation["minimum_distance_ft"]) // 5
        occupied = {
            (int(item["position"]["x"]), int(item["position"]["y"]))
            for item in values
            if item["actor_id"] != actor_id and isinstance(item.get("position"), dict)
        }
        current = dict(actor.get("position") or {"x": 0, "y": 0})
        candidates = [
            {"x": x, "y": y}
            for x in range(width_cells)
            for y in range(height_cells)
            if (x, y) not in occupied
            and all(
                _distance({"x": x, "y": y}, dict(other["position"])) >= minimum_cells
                for other in others
            )
        ]
        if not candidates:
            raise ValueError("source separation does not fit the temporary battle-map bounds")
        candidates.sort(
            key=lambda position: (
                max(_distance(position, dict(other["position"])) for other in others),
                abs(int(position["x"]) - int(current["x"]))
                + abs(int(position["y"]) - int(current["y"])),
                int(position["x"]),
                int(position["y"]),
            )
        )
        actor["position"] = candidates[0]
    return values


def _source_separation_target(
    acting_actor_id: str,
    target_ids: list[str],
    separations: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Return a source separation that forbids approaching a current target."""

    return next(
        (
            separation
            for target_id in target_ids
            if (separation := separations.get(target_id)) is not None
            and acting_actor_id in separation["other_actor_ids"]
        ),
        None,
    )


def _participant_config(
    party_ids: list[str],
    hostile_ids: list[str],
    *,
    ally_ids: list[str] | None = None,
    surprise_by_actor: dict[str, bool],
    hostiles_hidden: bool = True,
    hidden_actor_ids: list[str] | None = None,
    visible_to_actor_ids_by_hostile: dict[str, list[str]] | None = None,
    source_conditions_by_actor: dict[str, list[dict[str, Any]]] | None = None,
    source_traits_by_actor: dict[str, list[dict[str, Any]]] | None = None,
    source_separations: dict[str, dict[str, Any]] | None = None,
    agent_positions: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    allies = list(ally_ids or [])
    if len(party_ids) + len(allies) > 10 or len(hostile_ids) > 10:
        raise ValueError(
            "default encounter layout supports at most 10 friendly actors and 10 hostiles"
        )
    if set(party_ids) & set(allies):
        raise ValueError("PC and allied-NPC participant ids must be disjoint")

    def source_fields(actor_id: str) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        conditions = list(dict(source_conditions_by_actor or {}).get(actor_id) or [])
        traits = list(dict(source_traits_by_actor or {}).get(actor_id) or [])
        if conditions:
            fields["source_conditions"] = conditions
        if traits:
            fields["source_traits"] = traits
        return fields

    configs = [
        {
            "actor_id": actor_id,
            "position": {"x": 1, "y": index + 1},
            "disposition": "friendly",
            "surprised": bool(surprise_by_actor.get(actor_id, False)),
            "death_saves": True,
            **source_fields(actor_id),
        }
        for index, actor_id in enumerate(party_ids)
    ]
    configs.extend(
        {
            "actor_id": actor_id,
            "position": {"x": 0, "y": index + 1},
            "disposition": "friendly",
            "surprised": bool(surprise_by_actor.get(actor_id, False)),
            # NPCs and monsters die at 0 HP unless the DM explicitly elects
            # to use death saves. A prepared allied NPC is not a PC.
            "death_saves": False,
            **source_fields(actor_id),
        }
        for index, actor_id in enumerate(allies)
    )
    hostile_positions = (
        (2, 2),
        (2, 4),
        (7, 2),
        (7, 4),
        (4, 2),
        (4, 4),
        (9, 2),
        (9, 4),
        (6, 6),
        (8, 6),
    )
    selected_hidden_ids = set(hidden_actor_ids or [])
    configs.extend(
        {
            "actor_id": actor_id,
            "position": {"x": hostile_positions[index][0], "y": hostile_positions[index][1]},
            "disposition": "hostile",
            "hidden": (
                (hostiles_hidden or actor_id in selected_hidden_ids)
                and not bool(surprise_by_actor.get(actor_id, False))
            ),
            "visible_to_actor_ids": (
                list(dict(visible_to_actor_ids_by_hostile or {}).get(actor_id) or [])
                if (
                    (hostiles_hidden or actor_id in selected_hidden_ids)
                    and not bool(surprise_by_actor.get(actor_id, False))
                )
                else None
            ),
            "surprised": bool(surprise_by_actor.get(actor_id, False)),
            "death_saves": False,
            **source_fields(actor_id),
        }
        for index, actor_id in enumerate(hostile_ids)
    )
    configs = _apply_agent_positions(configs, dict(agent_positions or {}))
    return _apply_source_separations(configs, dict(source_separations or {}))


def _reinforcement_config(
    actor_id: str,
    index: int,
    *,
    join_round: int = 0,
) -> dict[str, Any]:
    """Place a queued source reinforcement without granting an immediate turn."""

    if not actor_id.strip():
        raise ValueError("source reinforcement actor_id must be non-empty")
    positions = (
        (7, 2),
        (7, 4),
        (9, 2),
        (9, 4),
        (6, 6),
        (8, 6),
        (10, 2),
        (10, 4),
        (6, 7),
        (8, 7),
    )
    if index < 0 or index >= len(positions):
        raise ValueError("default encounter layout supports at most 10 reinforcements")
    if isinstance(join_round, bool) or not isinstance(join_round, int) or join_round < 0:
        raise ValueError("source reinforcement round must be a non-negative integer")
    return {
        "position": {"x": positions[index][0], "y": positions[index][1]},
        "disposition": "hostile",
        "hidden": False,
        "surprised": False,
        "death_saves": False,
        **({"join_round": join_round} if join_round else {}),
    }


def _facade_value(value: Any) -> Any:
    if isinstance(value, dict) and "result" in value:
        return value["result"]
    return value


def _roll_total(value: dict[str, Any]) -> int:
    if "total" in value:
        return int(value["total"])
    return int(dict(value.get("result") or {}).get("total", 0))


def _surprise_from_check_report(
    path: Path,
    *,
    campaign_id: str,
    scene_id: str,
    location_key: str,
    party_ids: list[str],
    hostile_ids: list[str],
) -> tuple[dict[str, bool], dict[str, Any]]:
    report = _read_report(path)
    result = dict(report.get("result") or {})
    scene = dict(result.get("scene") or {})
    actor = dict(result.get("actor") or {})
    check = dict(result.get("check") or {})
    if (
        report.get("passed") is not True
        or report.get("action") != "resolve-check"
        or report.get("campaign_id") != campaign_id
        or scene.get("scene_id") != scene_id
        or scene.get("location_key") != location_key
        or actor.get("id") not in party_ids
        or not isinstance(check.get("success"), bool)
    ):
        raise ValueError("surprise check report does not match this encounter")
    noticed_threat = bool(check["success"])
    surprise = {actor_id: not noticed_threat for actor_id in party_ids}
    surprise.update({actor_id: False for actor_id in hostile_ids})
    return surprise, {
        "mode": "source_cited_party_scout",
        "report_path": str(path.expanduser().resolve()),
        "actor": actor,
        "check": check,
    }


def _source_declared_surprise(
    *,
    party_ids: list[str],
    hostile_ids: list[str],
    surprised_actor_ids: list[str],
    source_excerpt: str,
) -> tuple[dict[str, bool], dict[str, Any]]:
    participants = [*party_ids, *hostile_ids]
    normalized = [str(item).strip() for item in surprised_actor_ids]
    if (
        not normalized
        or any(not item for item in normalized)
        or len(normalized) != len(set(normalized))
        or not set(normalized) <= set(participants)
        or not source_excerpt.strip()
    ):
        raise ValueError(
            "source-declared surprise requires unique participant actor ids "
            "and an exact source excerpt"
        )
    return (
        {actor_id: actor_id in normalized for actor_id in participants},
        {
            "mode": "source_declared_surprise",
            "surprised_actor_ids": normalized,
            "source_excerpt": source_excerpt.strip(),
        },
    )


def _surprise_from_party_stealth_reports(
    paths: list[Path],
    *,
    campaign_id: str,
    scene_id: str,
    location_key: str,
    party_ids: list[str],
    hostile_ids: list[str],
) -> tuple[dict[str, bool], dict[str, Any]]:
    """Resolve a whole party sneaking past one shared passive Perception."""

    if len(paths) != len(party_ids):
        raise ValueError(
            "party Stealth surprise requires exactly one check report per party member"
        )
    checks: list[dict[str, Any]] = []
    seen_actor_ids: set[str] = set()
    dcs: set[int] = set()
    for path in paths:
        report = _read_report(path)
        result = dict(report.get("result") or {})
        scene = dict(result.get("scene") or {})
        actor = dict(result.get("actor") or {})
        check = dict(result.get("check") or {})
        actor_id = str(actor.get("id") or "")
        dc = check.get("dc")
        if (
            report.get("passed") is not True
            or report.get("action") != "resolve-check"
            or report.get("campaign_id") != campaign_id
            or scene.get("scene_id") != scene_id
            or scene.get("location_key") != location_key
            or actor_id not in party_ids
            or actor_id in seen_actor_ids
            or not isinstance(check.get("success"), bool)
            or isinstance(dc, bool)
            or not isinstance(dc, int)
            or dc < 1
        ):
            raise ValueError("party Stealth check report does not match this encounter")
        seen_actor_ids.add(actor_id)
        dcs.add(dc)
        checks.append(
            {
                "report_path": str(path.expanduser().resolve()),
                "actor": actor,
                "check": check,
            }
        )
    if seen_actor_ids != set(party_ids):
        raise ValueError("party Stealth reports must cover every party member exactly once")
    if len(dcs) != 1:
        raise ValueError(
            "party Stealth reports must use one shared hostile passive Perception DC"
        )
    all_hidden = all(bool(item["check"]["success"]) for item in checks)
    surprise = {actor_id: False for actor_id in party_ids}
    surprise.update({actor_id: all_hidden for actor_id in hostile_ids})
    return surprise, {
        "mode": "party_stealth_vs_shared_hostile_passive",
        "passive_perception": next(iter(dcs)),
        "all_party_hidden": all_hidden,
        "checks": checks,
    }


def _source_declared_conditions(
    declarations: list[dict[str, Any]],
    *,
    participant_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    participants = set(participant_ids)
    by_actor: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()
    for declaration in declarations:
        if not isinstance(declaration, dict):
            raise ValueError("source condition declaration must be an object")
        allowed = {"condition", "actor_ids", "source_ref", "source_excerpt"}
        unknown = set(declaration) - allowed
        if unknown:
            raise ValueError(f"unsupported source condition fields: {sorted(unknown)}")
        condition = str(declaration.get("condition") or "").strip().casefold()
        actor_ids = declaration.get("actor_ids")
        source_ref = declaration.get("source_ref")
        source_excerpt = str(declaration.get("source_excerpt") or "").strip()
        if (
            not condition
            or not isinstance(actor_ids, list)
            or not actor_ids
            or any(not str(actor_id).strip() for actor_id in actor_ids)
            or len({str(actor_id) for actor_id in actor_ids}) != len(actor_ids)
            or not isinstance(source_ref, dict)
            or not source_excerpt
        ):
            raise ValueError(
                "source condition requires condition, unique actor_ids, "
                "source_ref, and an exact source_excerpt"
            )
        normalized_actor_ids = [str(actor_id) for actor_id in actor_ids]
        unknown_actors = sorted(set(normalized_actor_ids) - participants)
        if unknown_actors:
            raise ValueError(
                "source condition actor_ids are not encounter participants: "
                + ", ".join(unknown_actors)
            )
        for actor_id in normalized_actor_ids:
            identity = (actor_id, condition)
            if identity in seen:
                raise ValueError(
                    f"duplicate source condition for encounter actor: {actor_id} {condition}"
                )
            seen.add(identity)
            by_actor.setdefault(actor_id, []).append(
                {
                    "condition": condition,
                    "duration": "encounter",
                    "source_ref": source_ref,
                    "source_excerpt": source_excerpt,
                }
            )
    return by_actor


def _source_traits(
    declarations: list[dict[str, Any]],
    *,
    participant_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    participants = set(participant_ids)
    by_actor: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()
    allowed = {"actor_id", "kind", "feature_id", "source_excerpt"}
    for index, declaration in enumerate(declarations):
        if not isinstance(declaration, dict):
            raise ValueError(f"source trait {index} must be an object")
        unknown = set(declaration) - allowed
        if unknown:
            raise ValueError(f"source trait {index} has unsupported fields: {sorted(unknown)}")
        actor_id = str(declaration.get("actor_id") or "").strip()
        kind = str(declaration.get("kind") or "").strip().casefold()
        feature_id = str(declaration.get("feature_id") or "").strip()
        source_excerpt = str(declaration.get("source_excerpt") or "").strip()
        identity = (actor_id, feature_id)
        if (
            actor_id not in participants
            or kind != "regeneration"
            or not feature_id
            or not source_excerpt
            or identity in seen
        ):
            raise ValueError(
                f"source trait {index} requires one participant, regeneration kind, "
                "unique feature_id, and exact source_excerpt"
            )
        seen.add(identity)
        by_actor.setdefault(actor_id, []).append(
            {
                "kind": kind,
                "feature_id": feature_id,
                "source_excerpt": source_excerpt,
            }
        )
    return by_actor


def _source_zero_hp_finisher(
    declaration: dict[str, Any] | None,
    *,
    participant_ids: list[str],
    encounter_source_excerpt: str,
) -> dict[str, Any] | None:
    if declaration is None:
        return None
    if not isinstance(declaration, dict):
        raise ValueError("source zero-HP finisher must be an object")
    allowed = {
        "target_id",
        "actor_ids",
        "source_excerpt",
        "oil_rule_excerpt",
    }
    unknown = set(declaration) - allowed
    if unknown:
        raise ValueError(
            "source zero-HP finisher has unsupported fields: " + ", ".join(sorted(unknown))
        )
    target_id = str(declaration.get("target_id") or "").strip()
    actor_ids = [str(item).strip() for item in declaration.get("actor_ids") or []]
    source_excerpt = str(declaration.get("source_excerpt") or "").strip()
    oil_rule_excerpt = str(declaration.get("oil_rule_excerpt") or "").strip()
    participants = set(participant_ids)
    encounter_excerpt = _normalized_source_text(encounter_source_excerpt)
    damage_match = re.search(
        r"(?i)\btarget takes an additional\s+(\d+)\s+fire damage "
        r"from the burning oil\b",
        oil_rule_excerpt,
    )
    if (
        not target_id
        or target_id not in participants
        or not actor_ids
        or any(not item for item in actor_ids)
        or len(actor_ids) != len(set(actor_ids))
        or not set(actor_ids) <= participants
        or target_id in actor_ids
        or not source_excerpt
        or _normalized_source_text(source_excerpt) not in encounter_excerpt
        or re.search(
            r"(?i)\bdouse the troll with lamp oil and set it on fire when it falls\b",
            source_excerpt,
        )
        is None
        or damage_match is None
        or int(damage_match.group(1)) <= 0
    ):
        raise ValueError(
            "source zero-HP finisher requires one participant target, unique "
            "participant actors, the exact authored lamp-oil instruction, and "
            "the exact positive 2014 Oil damage rule"
        )
    return {
        "id": f"source-zero-hp-finisher:{target_id}",
        "target_id": target_id,
        "actor_ids": actor_ids,
        "source_excerpt": source_excerpt,
        "oil_rule_excerpt": oil_rule_excerpt,
        "fire_damage": int(damage_match.group(1)),
        "oil_duration_rounds": 10,
    }


def _source_zero_hp_stabilization(
    declaration: dict[str, Any] | None,
    *,
    participant_ids: list[str],
) -> dict[str, Any] | None:
    if declaration is None:
        return None
    if not isinstance(declaration, dict):
        raise ValueError("source zero-HP stabilization must be an object")
    allowed = {"actor_ids", "source_excerpt"}
    unknown = set(declaration) - allowed
    if unknown:
        raise ValueError(
            "source zero-HP stabilization has unsupported fields: " + ", ".join(sorted(unknown))
        )
    actor_ids = [str(item).strip() for item in declaration.get("actor_ids") or []]
    source_excerpt = str(declaration.get("source_excerpt") or "").strip()
    if (
        not actor_ids
        or any(not item for item in actor_ids)
        or len(actor_ids) != len(set(actor_ids))
        or not set(actor_ids) <= set(participant_ids)
        or not source_excerpt
        or re.search(
            r"(?i)\bcharacters are reduced to 0 hit points\b.*\bemployees "
            r"of the yawning portal step forward to stabilize them\b",
            _normalized_source_text(source_excerpt),
        )
        is None
    ):
        raise ValueError(
            "source zero-HP stabilization requires unique participant PCs and "
            "the exact authored Yawning Portal employee instruction"
        )
    return {
        "actor_ids": actor_ids,
        "source_excerpt": source_excerpt,
    }


def _normalized_source_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _source_target_priorities(
    declarations: list[dict[str, Any]],
    *,
    participant_ids: list[str],
    encounter_source_excerpt: str,
) -> dict[str, dict[str, Any]]:
    participants = set(participant_ids)
    encounter_excerpt = _normalized_source_text(encounter_source_excerpt)
    by_actor: dict[str, dict[str, Any]] = {}
    allowed = {"actor_ids", "priority_groups", "source_excerpt"}
    for index, declaration in enumerate(declarations):
        if not isinstance(declaration, dict):
            raise ValueError(f"source target priority {index} must be an object")
        unknown = set(declaration) - allowed
        if unknown:
            raise ValueError(
                f"source target priority {index} has unsupported fields: {sorted(unknown)}"
            )
        actor_ids = [str(item).strip() for item in declaration.get("actor_ids") or []]
        raw_groups = declaration.get("priority_groups")
        source_excerpt = str(declaration.get("source_excerpt") or "").strip()
        if (
            not actor_ids
            or any(not item for item in actor_ids)
            or len(actor_ids) != len(set(actor_ids))
            or not set(actor_ids) <= participants
            or not isinstance(raw_groups, list)
            or not raw_groups
            or not source_excerpt
        ):
            raise ValueError(
                "source target priority requires unique participant actor_ids, "
                "non-empty priority_groups, and an exact source_excerpt"
            )
        priority_groups: list[list[str]] = []
        for raw_group in raw_groups:
            if not isinstance(raw_group, list):
                raise ValueError("source target priority groups must be actor-id lists")
            group = [str(item).strip() for item in raw_group]
            if (
                not group
                or any(not item for item in group)
                or len(group) != len(set(group))
                or not set(group) <= participants
            ):
                raise ValueError(
                    "source target priority groups must contain unique participant ids"
                )
            priority_groups.append(group)
        target_ids = [item for group in priority_groups for item in group]
        if (
            len(target_ids) != len(set(target_ids))
            or set(actor_ids) & set(target_ids)
            or set(actor_ids) & set(by_actor)
        ):
            raise ValueError(
                "source target priority actors and targets must be disjoint and "
                "each acting participant may be declared only once"
            )
        normalized_declaration_excerpt = _normalized_source_text(source_excerpt)
        if not encounter_excerpt or normalized_declaration_excerpt not in encounter_excerpt:
            raise ValueError(
                "source target priority excerpt is not contained in the encounter source"
            )
        value = {
            "actor_ids": actor_ids,
            "priority_groups": priority_groups,
            "source_excerpt": source_excerpt,
        }
        for actor_id in actor_ids:
            by_actor[actor_id] = value
    return by_actor


def _agent_target_priorities(
    declarations: list[dict[str, Any]],
    *,
    party_ids: list[str],
    hostile_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Validate explicit Agent tactics without pretending they are module facts."""

    party = set(party_ids)
    hostiles = set(hostile_ids)
    by_actor: dict[str, dict[str, Any]] = {}
    allowed = {"actor_ids", "priority_groups", "ruling_reason"}
    for index, declaration in enumerate(declarations):
        if not isinstance(declaration, dict):
            raise ValueError(f"Agent target priority {index} must be an object")
        unknown = set(declaration) - allowed
        if unknown:
            raise ValueError(
                f"Agent target priority {index} has unsupported fields: {sorted(unknown)}"
            )
        actor_ids = [str(item).strip() for item in declaration.get("actor_ids") or []]
        raw_groups = declaration.get("priority_groups")
        ruling_reason = " ".join(
            str(declaration.get("ruling_reason") or "").split()
        )
        if (
            not actor_ids
            or any(not item for item in actor_ids)
            or len(actor_ids) != len(set(actor_ids))
            or not set(actor_ids) <= party
            or not isinstance(raw_groups, list)
            or not raw_groups
            or not ruling_reason
            or len(ruling_reason) > 500
        ):
            raise ValueError(
                "Agent target priority requires unique party actor_ids, non-empty "
                "hostile priority_groups, and a 1-to-500-character ruling_reason"
            )
        priority_groups: list[list[str]] = []
        for raw_group in raw_groups:
            if not isinstance(raw_group, list):
                raise ValueError("Agent target priority groups must be actor-id lists")
            group = [str(item).strip() for item in raw_group]
            if (
                not group
                or any(not item for item in group)
                or len(group) != len(set(group))
                or not set(group) <= hostiles
            ):
                raise ValueError(
                    "Agent target priority groups must contain unique encounter hostiles"
                )
            priority_groups.append(group)
        target_ids = [item for group in priority_groups for item in group]
        if len(target_ids) != len(set(target_ids)) or set(actor_ids) & set(by_actor):
            raise ValueError(
                "Agent priority targets must be unique and each party actor may be "
                "declared only once"
            )
        value = {
            "actor_ids": actor_ids,
            "priority_groups": priority_groups,
            "ruling_reason": ruling_reason,
            "default_resolver": "agent",
            "ruling_kind": "tactical_decision",
        }
        for actor_id in actor_ids:
            by_actor[actor_id] = value
    return by_actor


def _prioritize_targets(
    actor_id: str,
    target_ids: list[str],
    priorities_by_actor: dict[str, dict[str, Any]],
) -> list[str]:
    prioritized = list(target_ids)
    declaration = priorities_by_actor.get(actor_id)
    if declaration is None:
        return prioritized
    rank = {
        target_id: group_index
        for group_index, group in enumerate(declaration["priority_groups"])
        for target_id in group
    }
    fallback_rank = len(declaration["priority_groups"])
    prioritized.sort(key=lambda target_id: rank.get(target_id, fallback_rank))
    return prioritized


def _surprise_from_hostile_stealth_totals(
    *,
    party_ids: list[str],
    hostile_ids: list[str],
    passive_perception: dict[str, int],
    stealth_totals: dict[str, int],
) -> dict[str, bool]:
    if set(passive_perception) != set(party_ids):
        raise ValueError("passive Perception must be available for every party member")
    if set(stealth_totals) != set(hostile_ids):
        raise ValueError("Stealth totals must be available for every source hostile")
    surprise = {
        actor_id: all(
            int(passive_perception[actor_id]) < int(stealth_totals[hostile_id])
            for hostile_id in hostile_ids
        )
        for actor_id in party_ids
    }
    surprise.update({actor_id: False for actor_id in hostile_ids})
    return surprise


def _source_opening_casts(
    values: list[dict[str, Any]],
    *,
    participant_ids: list[str],
) -> list[dict[str, Any]]:
    allowed = {
        "actor_id",
        "spell_id",
        "source_item_id",
        "source_excerpt",
        "declaration",
    }
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise ValueError(f"source opening cast {index} must be an object")
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                f"source opening cast {index} has unsupported fields: {', '.join(sorted(unknown))}"
            )
        cast = {
            key: str(raw.get(key) or "").strip()
            for key in ("actor_id", "spell_id", "source_item_id", "source_excerpt")
        }
        if (
            not all(cast.values())
            or cast["actor_id"] not in participant_ids
            or (
                "declaration" in raw
                and raw["declaration"] is not None
                and not isinstance(raw["declaration"], dict)
            )
        ):
            raise ValueError(
                f"source opening cast {index} requires a participant actor, spell, "
                "source item, exact excerpt, and optional object declaration"
            )
        cast["declaration"] = dict(raw.get("declaration") or {})
        cast["sequence"] = index + 1
        normalized.append(cast)
    return normalized


def _source_precombat_casts(
    values: list[dict[str, Any]],
    *,
    participant_ids: list[str],
) -> list[dict[str, Any]]:
    allowed = {
        "actor_id",
        "spell_id",
        "cast_level",
        "source_excerpt",
        "component_ruling",
    }
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise ValueError(f"source precombat cast {index} must be an object")
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                f"source precombat cast {index} has unsupported fields: "
                f"{', '.join(sorted(unknown))}"
            )
        actor_id = str(raw.get("actor_id") or "").strip()
        spell_id = str(raw.get("spell_id") or "").strip()
        source_excerpt = str(raw.get("source_excerpt") or "").strip()
        cast_level = raw.get("cast_level")
        component_ruling = raw.get("component_ruling")
        identity = (actor_id, spell_id)
        if (
            actor_id not in participant_ids
            or not spell_id
            or not source_excerpt
            or isinstance(cast_level, bool)
            or not isinstance(cast_level, int)
            or cast_level < 0
            or cast_level > 9
            or (component_ruling is not None and not isinstance(component_ruling, dict))
            or identity in seen
        ):
            raise ValueError(
                f"source precombat cast {index} requires a unique participant spell, "
                "legal cast level, exact excerpt, and optional component ruling"
            )
        seen.add(identity)
        normalized.append(
            {
                "sequence": index + 1,
                "actor_id": actor_id,
                "spell_id": spell_id,
                "cast_level": cast_level,
                "source_excerpt": source_excerpt,
                "component_ruling": dict(component_ruling or {}),
            }
        )
    return normalized


def _source_opening_weapons(
    values: list[dict[str, Any]],
    *,
    participant_ids: list[str],
) -> dict[str, dict[str, str]]:
    normalized: dict[str, dict[str, str]] = {}
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise ValueError(f"source opening weapon {index} must be an object")
        unknown = set(raw) - {"actor_id", "weapon_id", "source_excerpt"}
        if unknown:
            raise ValueError(
                f"source opening weapon {index} has unsupported fields: "
                f"{', '.join(sorted(unknown))}"
            )
        value = {
            key: str(raw.get(key) or "").strip()
            for key in ("actor_id", "weapon_id", "source_excerpt")
        }
        if (
            not all(value.values())
            or value["actor_id"] not in participant_ids
            or value["actor_id"] in normalized
        ):
            raise ValueError(
                f"source opening weapon {index} requires one participant actor, "
                "weapon id, and exact excerpt"
            )
        normalized[value["actor_id"]] = value
    return normalized


def _source_attack_environments(
    values: list[dict[str, Any]],
    *,
    participant_ids: list[str],
    actors: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise ValueError(f"source attack environment {index} must be an object")
        unknown = set(raw) - {
            "actor_id",
            "direct_sunlight",
            "source_excerpt",
            "ruling_reason",
        }
        if unknown:
            raise ValueError(
                f"source attack environment {index} has unsupported fields: "
                f"{', '.join(sorted(unknown))}"
            )
        actor_id = str(raw.get("actor_id") or "").strip()
        source_excerpt = " ".join(str(raw.get("source_excerpt") or "").split())
        ruling_reason = " ".join(str(raw.get("ruling_reason") or "").split())
        if (
            actor_id not in participant_ids
            or actor_id in normalized
            or not isinstance(raw.get("direct_sunlight"), bool)
            or not source_excerpt
            or not ruling_reason
        ):
            raise ValueError(
                f"source attack environment {index} requires one unique "
                "participant, an Agent-adjudicated direct_sunlight fact, an exact "
                "trait excerpt, and a ruling reason"
            )
        if actors is not None:
            actor = actors.get(actor_id)
            feature = next(
                (
                    item
                    for item in (
                        dict(dict(actor or {}).get("sheet") or {})
                        .get("content", {})
                        .get("features", [])
                    )
                    if str(
                        dict(dict(item.get("choices") or {}).get("source_trait") or {}).get("kind")
                        or ""
                    )
                    == "sunlight_sensitivity"
                ),
                None,
            )
            description = " ".join(str(dict(feature or {}).get("description") or "").split())
            if feature is None or source_excerpt not in description:
                raise ValueError(
                    f"source attack environment {index} must match the "
                    "structured Sunlight Sensitivity on its actor card"
                )
        normalized[actor_id] = {
            "actor_id": actor_id,
            "direct_sunlight": bool(raw["direct_sunlight"]),
            "source_excerpt": source_excerpt,
            "ruling_reason": ruling_reason,
        }
    return normalized


def _source_avoidances(
    paths: list[Path],
    *,
    campaign_id: str,
    scene_id: str,
    participant_ids: list[str],
) -> tuple[dict[str, set[str]], list[dict[str, Any]]]:
    participant_set = set(participant_ids)
    avoided_by_actor: dict[str, set[str]] = {}
    evidence: list[dict[str, Any]] = []
    cell_pattern = re.compile(r"(?<!\d)(\d+),(\d+)(?!\d)")
    for index, path in enumerate(paths):
        report = _read_report(path)
        continuity = dict(dict(report.get("result") or {}).get("continuity") or {})
        event = dict(continuity.get("event") or {})
        payload = dict(event.get("payload") or {})
        knowledge = list(continuity.get("actor_knowledge") or [])
        summary = str(event.get("summary") or "")
        source_excerpt = str(payload.get("source_excerpt") or "").strip()
        cells = {f"{int(x)},{int(y)}" for x, y in cell_pattern.findall(summary)}
        if (
            report.get("campaign_id") != campaign_id
            or report.get("passed") is not True
            or event.get("event_type") not in {"trap_detected", "trap_locations_shared"}
            or str(payload.get("scene_id") or "") != scene_id
            or not str(event.get("id") or "")
            or not source_excerpt
            or not cells
        ):
            raise ValueError(
                f"source avoidance report {index} must be a passed public "
                "trap knowledge event for this campaign and scene with marked cells"
            )
        actor_ids: list[str] = []
        for item in knowledge:
            actor_id = str(dict(item).get("actor_id") or "")
            proposition = str(dict(item).get("proposition") or "")
            proposition_cells = {f"{int(x)},{int(y)}" for x, y in cell_pattern.findall(proposition)}
            if (
                actor_id not in participant_set
                or not cells <= proposition_cells
                or "avoid" not in proposition.casefold()
            ):
                raise ValueError(
                    f"source avoidance report {index} contains knowledge that "
                    "does not prove a participant knows and avoids every marked cell"
                )
            avoided_by_actor.setdefault(actor_id, set()).update(cells)
            actor_ids.append(actor_id)
        if not actor_ids or len(actor_ids) != len(set(actor_ids)):
            raise ValueError(f"source avoidance report {index} must contain unique actor knowledge")
        evidence.append(
            {
                "report_path": str(path.expanduser().resolve()),
                "event_id": str(event["id"]),
                "actor_ids": actor_ids,
                "avoided_cells": sorted(cells),
                "source_excerpt": source_excerpt,
                "source_ref": deepcopy(payload.get("source_ref")),
            }
        )
    return avoided_by_actor, evidence


def _source_on_hit_rulings(
    values: list[dict[str, Any]],
    *,
    participant_ids: list[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    allowed = {
        "actor_id",
        "weapon_id",
        "id",
        "condition",
        "escape_dc",
        "escape_abilities",
        "save_ability",
        "save_dc",
        "repeat_save_timing",
        "duration",
        "damage_formula",
        "damage_type",
        "half_on_success",
        "zero_hp_effect",
        "target_has_limbs",
        "source_excerpt",
    }
    normalized: dict[tuple[str, str], dict[str, Any]] = {}
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise ValueError(f"source on-hit ruling {index} must be an object")
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                f"source on-hit ruling {index} has unsupported fields: {', '.join(sorted(unknown))}"
            )
        actor_id = str(raw.get("actor_id") or "").strip()
        weapon_id = str(raw.get("weapon_id") or "").strip()
        selection_id = str(raw.get("id") or "").strip().casefold()
        if not selection_id:
            if raw.get("save_ability") is not None:
                selection_id = (
                    "saving_throw_condition"
                    if raw.get("condition") is not None
                    and raw.get("damage_formula") is None
                    else "saving_throw_damage"
                )
            else:
                selection_id = "apply_condition"
        if selection_id not in {
            "apply_condition",
            "saving_throw_condition",
            "saving_throw_damage",
            "attachment",
            "critical_followup",
            "dismiss",
        }:
            raise ValueError(f"source on-hit ruling {index} has unsupported id {selection_id!r}")
        source_excerpt = str(raw.get("source_excerpt") or "").strip()
        identity = (actor_id, weapon_id)
        if (
            actor_id not in participant_ids
            or not weapon_id
            or not source_excerpt
            or identity in normalized
        ):
            raise ValueError(
                f"source on-hit ruling {index} requires one participant weapon and exact excerpt"
            )
        if selection_id == "critical_followup":
            critical_fields = {
                "condition",
                "escape_dc",
                "escape_abilities",
                "save_ability",
                "save_dc",
                "repeat_save_timing",
                "duration",
                "damage_formula",
                "damage_type",
                "half_on_success",
                "zero_hp_effect",
            }
            if any(raw.get(field) is not None for field in critical_fields) or not isinstance(
                raw.get("target_has_limbs"), bool
            ):
                raise ValueError(
                    f"source on-hit ruling {index} critical_followup accepts only "
                    "actor, weapon, id, target_has_limbs, and exact excerpt"
                )
            normalized[identity] = {
                "actor_id": actor_id,
                "weapon_id": weapon_id,
                "id": selection_id,
                "target_has_limbs": bool(raw["target_has_limbs"]),
                "source_excerpt": source_excerpt,
            }
            continue
        if raw.get("target_has_limbs") is not None:
            raise ValueError(
                f"source on-hit ruling {index} target_has_limbs is only valid for critical_followup"
            )
        if selection_id == "dismiss":
            settlement_fields = {
                "condition",
                "escape_dc",
                "escape_abilities",
                "save_ability",
                "save_dc",
                "repeat_save_timing",
                "duration",
                "damage_formula",
                "damage_type",
                "half_on_success",
                "zero_hp_effect",
            }
            if any(raw.get(field) is not None for field in settlement_fields):
                raise ValueError(
                    f"source on-hit ruling {index} dismiss accepts only "
                    "actor, weapon, id, and exact excerpt"
                )
            normalized[identity] = {
                "actor_id": actor_id,
                "weapon_id": weapon_id,
                "id": selection_id,
                "source_excerpt": source_excerpt,
            }
            continue
        if selection_id == "attachment":
            attachment_fields = {
                "condition",
                "escape_dc",
                "escape_abilities",
                "save_ability",
                "save_dc",
                "repeat_save_timing",
                "duration",
                "damage_formula",
                "damage_type",
                "half_on_success",
                "zero_hp_effect",
            }
            if any(raw.get(field) is not None for field in attachment_fields):
                raise ValueError(
                    f"source on-hit ruling {index} attachment accepts only "
                    "actor, weapon, id, and exact excerpt"
                )
            normalized[identity] = {
                "actor_id": actor_id,
                "weapon_id": weapon_id,
                "id": selection_id,
                "source_excerpt": source_excerpt,
            }
            continue
        if selection_id == "saving_throw_condition":
            incompatible_fields = {
                "escape_dc",
                "escape_abilities",
                "damage_formula",
                "damage_type",
                "half_on_success",
                "zero_hp_effect",
            }
            if any(raw.get(field) is not None for field in incompatible_fields):
                raise ValueError(
                    f"source on-hit ruling {index} mixes save-condition with "
                    "escape or damage terms"
                )
            condition = str(raw.get("condition") or "").strip().casefold()
            save_ability = str(raw.get("save_ability") or "").strip().casefold()
            save_dc = raw.get("save_dc")
            repeat_save_timing = str(
                raw.get("repeat_save_timing") or ""
            ).strip().casefold()
            raw_duration = raw.get("duration")
            if not isinstance(raw_duration, dict):
                raise ValueError(
                    f"source on-hit ruling {index} requires a structured duration"
                )
            duration = dict(raw_duration)
            duration_period = str(duration.get("period") or "").strip().casefold()
            duration_remaining = duration.get("remaining")
            if (
                set(duration) - {"period", "remaining"}
                or not condition
                or not save_ability
                or isinstance(save_dc, bool)
                or not isinstance(save_dc, int)
                or not 1 <= save_dc <= 40
                or repeat_save_timing != "turn_end"
                or duration_period not in {"round", "minute", "hour", "day"}
                or isinstance(duration_remaining, bool)
                or not isinstance(duration_remaining, int)
                or duration_remaining < 1
            ):
                raise ValueError(
                    f"source on-hit ruling {index} requires reviewed condition, "
                    "save, turn-end repeat, duration, and exact excerpt terms"
                )
            normalized[identity] = {
                "actor_id": actor_id,
                "weapon_id": weapon_id,
                "id": selection_id,
                "condition": condition,
                "save_ability": save_ability,
                "save_dc": save_dc,
                "repeat_save_timing": repeat_save_timing,
                "duration": {
                    "period": duration_period,
                    "remaining": duration_remaining,
                },
                "source_excerpt": source_excerpt,
            }
            continue
        if selection_id == "saving_throw_damage":
            condition_fields = {
                "condition",
                "escape_dc",
                "escape_abilities",
                "repeat_save_timing",
                "duration",
            }
            if any(raw.get(field) is not None for field in condition_fields):
                raise ValueError(
                    f"source on-hit ruling {index} mixes condition and save-damage terms"
                )
            save_ability = str(raw.get("save_ability") or "").strip().casefold()
            save_dc = raw.get("save_dc")
            damage_formula = str(raw.get("damage_formula") or "").strip()
            damage_type = str(raw.get("damage_type") or "").strip().casefold()
            half_on_success = raw.get("half_on_success")
            zero_hp_effect = raw.get("zero_hp_effect")
            if (
                not save_ability
                or isinstance(save_dc, bool)
                or not isinstance(save_dc, int)
                or not 1 <= save_dc <= 40
                or not damage_formula
                or not damage_type
                or not isinstance(half_on_success, bool)
                or (zero_hp_effect is not None and not isinstance(zero_hp_effect, dict))
            ):
                raise ValueError(
                    f"source on-hit ruling {index} requires reviewed save, "
                    "damage, success, and optional zero-HP terms"
                )
            normalized[identity] = {
                "actor_id": actor_id,
                "weapon_id": weapon_id,
                "id": selection_id,
                "save_ability": save_ability,
                "save_dc": save_dc,
                "damage_formula": damage_formula,
                "damage_type": damage_type,
                "half_on_success": half_on_success,
                "source_excerpt": source_excerpt,
                **(
                    {"zero_hp_effect": deepcopy(zero_hp_effect)}
                    if zero_hp_effect is not None
                    else {}
                ),
            }
            continue
        condition_incompatible_fields = {
            "save_ability",
            "save_dc",
            "repeat_save_timing",
            "duration",
            "damage_formula",
            "damage_type",
            "half_on_success",
            "zero_hp_effect",
        }
        if any(raw.get(field) is not None for field in condition_incompatible_fields):
            raise ValueError(
                f"source on-hit ruling {index} mixes action escape with "
                "saving-throw or damage terms"
            )
        condition = str(raw.get("condition") or "").strip().casefold()
        escape_dc = raw.get("escape_dc")
        escape_abilities = [
            str(item).strip().casefold()
            for item in raw.get("escape_abilities") or []
            if str(item).strip()
        ]
        if (
            not condition
            or isinstance(escape_dc, bool)
            or not isinstance(escape_dc, int)
            or not 1 <= escape_dc <= 40
            or not escape_abilities
            or len(escape_abilities) != len(set(escape_abilities))
        ):
            raise ValueError(
                f"source on-hit ruling {index} requires one participant weapon, "
                "condition, escape terms, and exact excerpt"
            )
        normalized[identity] = {
            "actor_id": actor_id,
            "weapon_id": weapon_id,
            "id": selection_id,
            "condition": condition,
            "escape_dc": escape_dc,
            "escape_abilities": escape_abilities,
            "source_excerpt": source_excerpt,
        }
    return normalized


def _source_delayed_actions(
    values: list[dict[str, Any]],
    *,
    participant_ids: list[str],
) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise ValueError(f"source delayed action {index} must be an object")
        unknown = set(raw) - {"actor_id", "until_round", "source_excerpt"}
        if unknown:
            raise ValueError(
                f"source delayed action {index} has unsupported fields: "
                f"{', '.join(sorted(unknown))}"
            )
        actor_id = str(raw.get("actor_id") or "").strip()
        until_round = raw.get("until_round")
        source_excerpt = str(raw.get("source_excerpt") or "").strip()
        if (
            actor_id not in participant_ids
            or actor_id in normalized
            or isinstance(until_round, bool)
            or not isinstance(until_round, int)
            or until_round < 2
            or not source_excerpt
        ):
            raise ValueError(
                f"source delayed action {index} requires one participant, round 2 "
                "or later, and an exact excerpt"
            )
        normalized[actor_id] = {
            "actor_id": actor_id,
            "until_round": until_round,
            "source_excerpt": source_excerpt,
        }
    return normalized


def _source_passive_allies(
    values: list[dict[str, Any]],
    *,
    ally_ids: list[str],
) -> dict[str, dict[str, str]]:
    normalized: dict[str, dict[str, str]] = {}
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise ValueError(f"source passive ally {index} must be an object")
        unknown = set(raw) - {"actor_id", "source_excerpt"}
        if unknown:
            raise ValueError(
                f"source passive ally {index} has unsupported fields: {', '.join(sorted(unknown))}"
            )
        actor_id = str(raw.get("actor_id") or "").strip()
        source_excerpt = str(raw.get("source_excerpt") or "").strip()
        if actor_id not in ally_ids or actor_id in normalized or not source_excerpt:
            raise ValueError(
                f"source passive ally {index} requires one unique allied actor and an exact excerpt"
            )
        normalized[actor_id] = {
            "actor_id": actor_id,
            "source_excerpt": source_excerpt,
        }
    return normalized


def _source_random_activities(
    values: list[dict[str, Any]],
    *,
    participant_ids: list[str],
    actors: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, str]]:
    normalized: dict[str, dict[str, str]] = {}
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise ValueError(f"source random activity {index} must be an object")
        unknown = set(raw) - {"actor_id", "activity_id", "source_excerpt"}
        if unknown:
            raise ValueError(
                f"source random activity {index} has unsupported fields: "
                f"{', '.join(sorted(unknown))}"
            )
        actor_id = str(raw.get("actor_id") or "").strip()
        activity_id = str(raw.get("activity_id") or "").strip()
        source_excerpt = " ".join(str(raw.get("source_excerpt") or "").split())
        if (
            actor_id not in participant_ids
            or actor_id in normalized
            or not activity_id
            or not source_excerpt
        ):
            raise ValueError(
                f"source random activity {index} requires one unique participant, "
                "an activity_id, and an exact excerpt"
            )
        if actors is not None:
            actor = actors.get(actor_id)
            activities = (
                dict(dict(actor or {}).get("sheet") or {}).get("content", {}).get("activities", [])
            )
            activity = next(
                (item for item in activities if str(item.get("id") or "") == activity_id),
                None,
            )
            description = " ".join(str(dict(activity or {}).get("description") or "").split())
            if activity is None or source_excerpt not in description:
                raise ValueError(
                    f"source random activity {index} must match its actor card and "
                    "contain the exact excerpt"
                )
        normalized[actor_id] = {
            "actor_id": actor_id,
            "activity_id": activity_id,
            "source_excerpt": source_excerpt,
        }
    return normalized


def _source_save_activities(
    values: list[dict[str, Any]],
    *,
    participant_ids: list[str],
    actors: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise ValueError(f"source save activity {index} must be an object")
        unknown = set(raw) - {
            "actor_id",
            "activity_id",
            "target_has_brain",
            "source_excerpt",
        }
        if unknown:
            raise ValueError(
                f"source save activity {index} has unsupported fields: {', '.join(sorted(unknown))}"
            )
        actor_id = str(raw.get("actor_id") or "").strip()
        activity_id = str(raw.get("activity_id") or "").strip()
        source_excerpt = " ".join(str(raw.get("source_excerpt") or "").split())
        if (
            actor_id not in participant_ids
            or actor_id in normalized
            or not activity_id
            or raw.get("target_has_brain") is not True
            or not source_excerpt
        ):
            raise ValueError(
                f"source save activity {index} requires one unique participant, "
                "an activity_id, a true target_has_brain ruling, and an exact excerpt"
            )
        if actors is not None:
            actor = actors.get(actor_id)
            activity = next(
                (
                    item
                    for item in (
                        dict(dict(actor or {}).get("sheet") or {})
                        .get("content", {})
                        .get("activities", [])
                    )
                    if str(item.get("id") or "") == activity_id
                ),
                None,
            )
            spec = dict(dict(activity or {}).get("choices", {}).get("source_save_effect") or {})
            description = " ".join(str(dict(activity or {}).get("description") or "").split())
            if (
                activity is None
                or not spec
                or spec.get("target_requirement") != "has_brain"
                or source_excerpt not in description
            ):
                raise ValueError(
                    f"source save activity {index} must match its structured "
                    "actor card and contain the exact excerpt"
                )
        normalized[actor_id] = {
            "actor_id": actor_id,
            "activity_id": activity_id,
            "target_has_brain": True,
            "source_excerpt": source_excerpt,
        }
    return normalized


def _source_contest_activities(
    values: list[dict[str, Any]],
    *,
    participant_ids: list[str],
    actors: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise ValueError(f"source contest activity {index} must be an object")
        unknown = set(raw) - {
            "actor_id",
            "activity_id",
            "target_is_humanoid",
            "source_excerpt",
        }
        if unknown:
            raise ValueError(
                f"source contest activity {index} has unsupported fields: "
                f"{', '.join(sorted(unknown))}"
            )
        actor_id = str(raw.get("actor_id") or "").strip()
        activity_id = str(raw.get("activity_id") or "").strip()
        source_excerpt = " ".join(str(raw.get("source_excerpt") or "").split())
        if (
            actor_id not in participant_ids
            or actor_id in normalized
            or not activity_id
            or raw.get("target_is_humanoid") is not True
            or not source_excerpt
        ):
            raise ValueError(
                f"source contest activity {index} requires one unique "
                "participant, an activity_id, a true target_is_humanoid ruling, "
                "and an exact excerpt"
            )
        if actors is not None:
            actor = actors.get(actor_id)
            activity = next(
                (
                    item
                    for item in (
                        dict(dict(actor or {}).get("sheet") or {})
                        .get("content", {})
                        .get("activities", [])
                    )
                    if str(item.get("id") or "") == activity_id
                ),
                None,
            )
            spec = dict(dict(activity or {}).get("choices", {}).get("source_contest_effect") or {})
            description = " ".join(str(dict(activity or {}).get("description") or "").split())
            if (
                activity is None
                or spec.get("kind") != "intellect_devourer_body_thief_2014"
                or spec.get("target_requirements") != ["incapacitated", "humanoid"]
                or source_excerpt not in description
            ):
                raise ValueError(
                    f"source contest activity {index} must match its structured "
                    "actor card and contain the exact excerpt"
                )
        normalized[actor_id] = {
            "actor_id": actor_id,
            "activity_id": activity_id,
            "target_is_humanoid": True,
            "source_excerpt": source_excerpt,
        }
    return normalized


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


async def _roll_hostile_stealth(
    client: ExposureClient,
    args: argparse.Namespace,
    *,
    branch_id: str,
    actors: dict[str, dict[str, Any]],
    party_ids: list[str],
    hostile_ids: list[str],
) -> tuple[dict[str, bool], dict[str, int], dict[str, Any], int]:
    passive_perception = {
        actor_id: int(dict(actors[actor_id].get("derived") or {}).get("passive_perception", 10))
        for actor_id in party_ids
    }
    stealth_profiles = {
        actor_id: {
            "bonus": int(
                dict(dict(actors[actor_id].get("derived") or {}).get("skills") or {}).get(
                    "stealth", 0
                )
            ),
            "disadvantage": bool(
                dict(actors[actor_id].get("derived") or {}).get("stealth_disadvantage", False)
            ),
        }
        for actor_id in hostile_ids
    }
    if (
        args.shared_hostile_stealth
        and len({(item["bonus"], item["disadvantage"]) for item in stealth_profiles.values()}) != 1
    ):
        raise ValueError("one shared hostile Stealth roll requires identical Stealth profiles")

    roll_actor_ids = hostile_ids[:1] if args.shared_hostile_stealth else hostile_ids
    rolls: list[dict[str, Any]] = []
    stealth_totals: dict[str, int] = {}
    for actor_id in roll_actor_ids:
        campaign = await _campaign(client, args.campaign_id)
        settled = await client.domain(
            "character_check",
            {
                "campaign_id": args.campaign_id,
                "action": "check",
                "payload": {
                    "actor_id": actor_id,
                    "kind": "ability",
                    "ability": "stealth",
                    "dc": 0,
                    "proficient": False,
                    "bonus": 0,
                    "advantage": False,
                    "disadvantage": False,
                },
                "branch_id": branch_id,
                "expected_revision": campaign["revision"],
                "idempotency_key": (
                    "encounter-stealth-"
                    + _operation_token(
                        args,
                        args.scene_id,
                        actor_id,
                    )
                ),
            },
        )
        result = dict(settled.get("result") or {})
        total = result.get("total")
        if isinstance(total, bool) or not isinstance(total, int):
            raise RuntimeError(f"hostile Stealth check for {actor_id} has no integer total")
        stealth_totals[actor_id] = total
        rolls.append(
            {
                "actor_id": actor_id,
                "actor_name": actors[actor_id].get("name"),
                "derived_stealth_bonus": stealth_profiles[actor_id]["bonus"],
                "derived_stealth_disadvantage": stealth_profiles[actor_id]["disadvantage"],
                "result": result,
                "random_stream_receipt": settled.get("random_stream_receipt"),
            }
        )
    if args.shared_hostile_stealth:
        shared_total = stealth_totals[roll_actor_ids[0]]
        stealth_totals = {actor_id: shared_total for actor_id in hostile_ids}

    surprise = _surprise_from_hostile_stealth_totals(
        party_ids=party_ids,
        hostile_ids=hostile_ids,
        passive_perception=passive_perception,
        stealth_totals=stealth_totals,
    )
    campaign = await _campaign(client, args.campaign_id)
    return (
        surprise,
        passive_perception,
        {
            "mode": (
                "source_shared_hostile_stealth"
                if args.shared_hostile_stealth
                else "individual_hostile_stealth"
            ),
            "rolls": rolls,
            "stealth_totals": stealth_totals,
        },
        int(campaign["revision"]),
    )


async def _current_branch(client: ExposureClient, campaign_id: str) -> dict[str, Any]:
    values = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in values if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    return branch


async def _characters(
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
    actors = {
        str(item.get("id") or ""): item
        for item in values
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    if set(actors) != set(actor_ids):
        raise RuntimeError("batch character query did not return every requested actor")
    return actors


def _character_summary(actor: dict[str, Any]) -> dict[str, Any]:
    derived = dict(actor.get("derived") or {})
    sheet = dict(actor.get("sheet") or {})
    return {
        "id": actor["id"],
        "name": actor["name"],
        "hp": dict(derived.get("hit_points") or {}),
        "conditions": list(sheet.get("conditions") or []),
        "resources": deepcopy(dict(sheet.get("resources") or {})),
        "spell_slots": deepcopy(
            dict(dict(sheet.get("spellcasting") or {}).get("spell_slots") or {})
        ),
        "prepared_spell_ids": list(
            dict(derived.get("spellcasting") or {}).get("prepared_spell_ids") or []
        ),
        "weapons": [
            {
                "item_id": item.get("item_id"),
                "name": item.get("name"),
                "attack_type": item.get("attack_type"),
                "range_ft": item.get("range_ft"),
                "on_hit_effect": item.get("on_hit_effect"),
            }
            for item in dict(derived.get("inventory") or {}).get("weapon_attacks", [])
        ],
    }


def _party_loadouts(
    declarations: list[dict[str, Any]],
    *,
    party_ids: list[str],
    actors: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    """Validate Agent-selected pre-initiative equipment against owned items."""

    party = set(party_ids)
    normalized: list[dict[str, str]] = []
    selected_slots: set[tuple[str, str]] = set()
    allowed = {"actor_id", "item_id", "slot"}
    for index, declaration in enumerate(declarations):
        if not isinstance(declaration, dict):
            raise ValueError(f"party loadout {index} must be an object")
        unknown = set(declaration) - allowed
        if unknown:
            raise ValueError(
                f"party loadout {index} has unsupported fields: "
                + ", ".join(sorted(unknown))
            )
        actor_id = str(declaration.get("actor_id") or "").strip()
        item_id = str(declaration.get("item_id") or "").strip()
        slot = str(declaration.get("slot") or "").strip()
        if actor_id not in party or not item_id or not slot:
            raise ValueError(
                f"party loadout {index} requires one party actor, item_id, and slot"
            )
        slot_key = (actor_id, slot)
        if slot_key in selected_slots:
            raise ValueError(
                f"party loadout {index} duplicates {actor_id!r} slot {slot!r}"
            )
        actor = actors.get(actor_id)
        items = list(
            dict(dict(actor or {}).get("sheet") or {})
            .get("inventory", {})
            .get("items", [])
        )
        item = next(
            (
                value
                for value in items
                if isinstance(value, dict)
                and str(value.get("id") or "").strip() == item_id
            ),
            None,
        )
        if item is None:
            raise ValueError(
                f"party loadout {index} item {item_id!r} is not owned by {actor_id!r}"
            )
        if slot in {"main_hand", "off_hand"} and str(item.get("kind") or "") != "weapon":
            raise ValueError(
                f"party loadout {index} cannot equip non-weapon {item_id!r} in {slot}"
            )
        normalized.append(
            {
                "actor_id": actor_id,
                "item_id": item_id,
                "slot": slot,
            }
        )
        selected_slots.add(slot_key)
    return normalized


async def _apply_party_loadouts(
    client: ExposureClient,
    args: argparse.Namespace,
    *,
    party_ids: list[str],
    actors: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Equip reviewed owned items through the public pre-combat inventory facade."""

    loadouts = _party_loadouts(
        list(getattr(args, "party_loadout_json", []) or []),
        party_ids=party_ids,
        actors=actors,
    )
    results: list[dict[str, Any]] = []
    current_actors = dict(actors)
    for loadout in loadouts:
        actor = current_actors[loadout["actor_id"]]
        item = next(
            value
            for value in dict(actor["sheet"]["inventory"]).get("items", [])
            if str(value.get("id") or "") == loadout["item_id"]
        )
        if (
            item.get("equipped") is True
            and str(item.get("equipped_slot") or "") == loadout["slot"]
        ):
            results.append({**loadout, "status": "already_equipped"})
            continue
        equipped = await client.domain(
            "inventory_change",
            {
                "owner": "character",
                "action": "equip",
                "owner_id": loadout["actor_id"],
                "payload": {
                    "item_id": loadout["item_id"],
                    "slot": loadout["slot"],
                },
                "expected_revision": actor["revision"],
                "idempotency_key": (
                    "encounter-party-loadout-"
                    + _operation_token(
                        args,
                        loadout["actor_id"],
                        loadout["slot"],
                        loadout["item_id"],
                    )
                ),
            },
        )
        results.append({**loadout, "status": "equipped", "result": equipped})
        current_actors.update(
            await _characters(
                client,
                args.campaign_id,
                [loadout["actor_id"]],
            )
        )
    return results, current_actors


def _validate_hostile_attacks(
    actor_id: str,
    attacks: list[dict[str, Any]],
    *,
    required_weapon_ids: list[str],
) -> None:
    attack_ids = {str(item.get("item_id") or "") for item in attacks}
    if not attack_ids - {""}:
        raise RuntimeError(f"source hostile {actor_id} has no executable weapon attack")
    missing = set(required_weapon_ids) - attack_ids
    if missing:
        raise RuntimeError(
            f"source hostile {actor_id} lacks required reviewed attacks: "
            f"{', '.join(sorted(missing))}"
        )
    if "shortbow" in required_weapon_ids:
        shortbow = next(item for item in attacks if item.get("item_id") == "shortbow")
        if dict(shortbow.get("range_ft") or {}) != {"normal": 80, "long": 320}:
            raise RuntimeError(f"source hostile {actor_id} has an invalid Shortbow range")
        if str(shortbow.get("on_hit_effect") or ""):
            raise RuntimeError(f"source hostile {actor_id} has unresolved trailing action prose")


def _preferred_hostile_weapon_id(
    actor: dict[str, Any],
    *,
    hostile_index: int,
) -> str:
    weapons = list(
        dict(dict(actor.get("derived") or {}).get("inventory") or {}).get("weapon_attacks", [])
    )
    attack_ids = {str(item.get("item_id") or "") for item in weapons}
    if hostile_index >= 2 and "shortbow" in attack_ids:
        return "shortbow"
    if "scimitar" in attack_ids:
        return "scimitar"
    melee = next(
        (str(item.get("item_id") or "") for item in weapons if item.get("attack_type") == "melee"),
        "",
    )
    return melee or (str(weapons[0].get("item_id") or "") if weapons else "")


def _preferred_multiattack_option_id(
    actor: dict[str, Any],
    *,
    preferred_weapon_id: str,
) -> str:
    options = [
        item
        for item in dict(actor.get("derived") or {}).get("multiattack_options", [])
        if isinstance(item, dict)
        and str(item.get("id") or "")
        and (
            sum(
                int(attack.get("count", 0) or 0)
                for attack in item.get("attacks", [])
                if isinstance(attack, dict)
            )
            + sum(
                int(activity.get("count", 0) or 0)
                for activity in item.get("activities", [])
                if isinstance(activity, dict)
            )
        )
        >= 2
    ]
    if not options:
        return ""
    if preferred_weapon_id:
        matching = [
            option
            for option in options
            if any(
                str(attack.get("weapon_id") or "") == preferred_weapon_id
                for attack in option.get("attacks", [])
                if isinstance(attack, dict)
            )
        ]
        if matching:
            return str(matching[0]["id"])
    return str(options[0]["id"])


def _has_multiattack_followup(combat: dict[str, Any], actor_id: str) -> bool:
    combatant = next(
        (
            item
            for item in combat.get("combatants", [])
            if isinstance(item, dict) and str(item.get("actor_id") or "") == actor_id
        ),
        None,
    )
    if combatant is None:
        return False
    budget = dict(combatant.get("turn_budget") or {})
    flags = dict(combatant.get("turn_flags") or {})
    return int(budget.get("attack_budget", 0) or 0) > 0 and bool(flags.get("multiattack"))


async def _start(
    client: ExposureClient,
    args: argparse.Namespace,
    party_ids: list[str],
    hostile_ids: list[str],
    additional_hostile_ids: list[str],
    reinforcement_hostile_ids: list[str],
) -> dict[str, Any]:
    if not args.scene_id or not args.location_key:
        raise ValueError("encounter start requires --scene-id and --location-key")
    opened_play = await client.open(args.campaign_id)
    await client.load(
        "play.scene",
        "play.scene_control",
        "play.characters",
        "play.resolution",
        "play.combat_control",
    )
    campaign = await _campaign(client, args.campaign_id)
    phase = str(dict(campaign.get("state") or {}).get("game_phase") or "")
    if phase != "play":
        raise RuntimeError("encounter start requires the play phase")
    branch = await _current_branch(client, args.campaign_id)
    args.operation_scope = _encounter_operation_scope(
        args,
        branch_id=str(branch["id"]),
        party_ids=party_ids,
        hostile_ids=hostile_ids,
        additional_hostile_ids=additional_hostile_ids,
        reinforcement_hostile_ids=reinforcement_hostile_ids,
    )
    ally_ids = _selected_prepared_actor_ids(
        args.ally_report,
        getattr(args, "ally_actor_id", []),
        report_kind="ally",
    )
    ally_id_set = set(ally_ids)
    pc_ids = [actor_id for actor_id in party_ids if actor_id not in ally_id_set]
    if len(pc_ids) + len(ally_ids) != len(party_ids):
        raise ValueError("friendly participant reports contain duplicate actor ids")
    _require_live_active_party(
        pc_ids,
        await _manifest_get(client, args.campaign_id),
        agent_party_absences=getattr(args, "agent_party_absence_json", []),
    )
    initial_hostile_ids = [*hostile_ids, *additional_hostile_ids]
    all_hostile_ids = [*initial_hostile_ids, *reinforcement_hostile_ids]
    source_target_priorities = _source_target_priorities(
        args.source_target_priority_json,
        participant_ids=[*party_ids, *all_hostile_ids],
        encounter_source_excerpt=str(args.source_excerpt or ""),
    )
    agent_target_priorities = _agent_target_priorities(
        getattr(args, "agent_target_priority_json", []),
        party_ids=pc_ids,
        hostile_ids=all_hostile_ids,
    )
    if set(source_target_priorities) & set(agent_target_priorities):
        raise ValueError(
            "the same actor cannot have both source-authored and Agent tactical "
            "target priorities"
        )
    source_conditions_by_actor = _source_declared_conditions(
        args.source_condition_json,
        participant_ids=[*party_ids, *initial_hostile_ids],
    )
    source_traits_by_actor = _source_traits(
        args.source_trait_json,
        participant_ids=[*party_ids, *initial_hostile_ids],
    )
    source_zero_hp_finisher = _source_zero_hp_finisher(
        args.source_zero_hp_finisher_json,
        participant_ids=[*party_ids, *initial_hostile_ids],
        encounter_source_excerpt=str(args.source_excerpt or ""),
    )
    if source_zero_hp_finisher is not None and set(source_zero_hp_finisher["actor_ids"]) & set(
        ally_ids
    ):
        raise ValueError("source zero-HP finisher actor_ids must be PCs, not allied NPCs")
    source_zero_hp_stabilization = _source_zero_hp_stabilization(
        args.source_zero_hp_stabilization_json,
        participant_ids=pc_ids,
    )
    actors = await _characters(
        client,
        args.campaign_id,
        [*party_ids, *all_hostile_ids],
    )
    opening_weapons = _source_opening_weapons(
        args.source_opening_weapon_json,
        participant_ids=[*party_ids, *all_hostile_ids],
    )
    on_hit_rulings = _source_on_hit_rulings(
        args.source_on_hit_ruling_json,
        participant_ids=[*party_ids, *all_hostile_ids],
    )
    delayed_actions = _source_delayed_actions(
        args.source_delayed_action_json,
        participant_ids=initial_hostile_ids,
    )
    passive_allies = _source_passive_allies(
        args.source_passive_ally_json,
        ally_ids=ally_ids,
    )
    random_activities = _source_random_activities(
        args.source_random_activity_json,
        participant_ids=[*party_ids, *all_hostile_ids],
        actors=actors,
    )
    save_activities = _source_save_activities(
        args.source_save_activity_json,
        participant_ids=[*party_ids, *all_hostile_ids],
        actors=actors,
    )
    contest_activities = _source_contest_activities(
        args.source_contest_activity_json,
        participant_ids=[*party_ids, *all_hostile_ids],
        actors=actors,
    )
    attack_environments = _source_attack_environments(
        args.source_attack_environment_json,
        participant_ids=[*party_ids, *all_hostile_ids],
        actors=actors,
    )
    source_casualty_pools = _source_casualty_pools(
        args.source_casualty_pool_json,
        hostile_ids=initial_hostile_ids,
        actors=actors,
        encounter_source_excerpt=str(args.source_excerpt or ""),
    )
    source_separations = _source_separations(
        args.source_separation_json,
        participant_ids=[*party_ids, *initial_hostile_ids],
        hostile_ids=initial_hostile_ids,
        encounter_source_excerpt=str(args.source_excerpt or ""),
    )
    agent_positions = _agent_positions(
        args.agent_position_json,
        participant_ids=[*party_ids, *initial_hostile_ids],
        encounter_source_excerpt=str(args.source_excerpt or ""),
    )
    _, source_avoidance_evidence = _source_avoidances(
        args.source_avoidance_report,
        campaign_id=args.campaign_id,
        scene_id=args.scene_id,
        participant_ids=[*party_ids, *all_hostile_ids],
    )
    precombat_loadouts, actors = await _apply_party_loadouts(
        client,
        args,
        party_ids=pc_ids,
        actors=actors,
    )
    for actor_id in set(all_hostile_ids) | {
        ruling_actor_id for ruling_actor_id, _ in on_hit_rulings
    }:
        attacks = list(
            dict(dict(actors[actor_id].get("derived") or {}).get("inventory") or {}).get(
                "weapon_attacks", []
            )
        )
        if actor_id in all_hostile_ids:
            _validate_hostile_attacks(
                actor_id,
                attacks,
                required_weapon_ids=args.required_hostile_weapon_id,
            )
        attack_ids = {str(item.get("item_id") or "") for item in attacks}
        opening = opening_weapons.get(actor_id)
        if opening and opening["weapon_id"] not in attack_ids:
            raise RuntimeError(
                f"source opening weapon {opening['weapon_id']} is absent from {actor_id}"
            )
        for ruling_actor_id, ruling_weapon_id in on_hit_rulings:
            if ruling_actor_id == actor_id and ruling_weapon_id not in attack_ids:
                raise RuntimeError(
                    f"source on-hit weapon {ruling_weapon_id} is absent from {actor_id}"
                )
    selected_hidden_ids = [str(item).strip() for item in args.source_hidden_actor_id]
    if (
        any(not item for item in selected_hidden_ids)
        or len(selected_hidden_ids) != len(set(selected_hidden_ids))
        or not set(selected_hidden_ids) <= set(initial_hostile_ids)
        or (selected_hidden_ids and args.hostiles_hidden)
    ):
        raise ValueError(
            "source hidden actor ids must be unique initial hostiles and cannot be "
            "combined with the all-hostiles --hostiles-hidden flag"
        )
    precombat_casts = _source_precombat_casts(
        args.source_precombat_cast_json,
        participant_ids=[*party_ids, *initial_hostile_ids],
    )
    precombat_cast_results: list[dict[str, Any]] = []
    for cast in precombat_casts:
        actor = actors[cast["actor_id"]]
        cast_payload: dict[str, Any] = {
            "spell_id": cast["spell_id"],
            "cast_level": cast["cast_level"],
        }
        if cast["component_ruling"]:
            cast_payload["component_ruling"] = cast["component_ruling"]
        settled = await client.domain(
            "character_action",
            {
                "character_id": cast["actor_id"],
                "action": "cast_spell",
                "payload": cast_payload,
                "expected_revision": actor["revision"],
                "idempotency_key": (
                    "encounter-source-precombat-cast-"
                    + _operation_token(
                        args,
                        cast["sequence"],
                        cast["actor_id"],
                        cast["spell_id"],
                    )
                ),
            },
        )
        if (
            settled.get("status") == "pending_ruling"
            and not dict(settled.get("result") or {}).get("payment")
        ):
            raise EncounterRulingRequiredError(
                settled,
                operation="character_action.precombat_spell",
                actor_id=str(cast["actor_id"]),
                action={
                    "spell_id": str(cast["spell_id"]),
                    "cast_level": int(cast["cast_level"]),
                },
                retry_hint=(
                    "Resolve the typed pre-commit ruling and retry before "
                    "starting the encounter."
                ),
            )
        if settled.get("status") not in {"committed", "pending_ruling"}:
            raise RuntimeError(
                "source precombat spell did not pay canonical resources and "
                "start its structured duration"
            )
        precombat_cast_results.append(
            {
                **cast,
                "result": settled,
            }
        )
        actors.update(
            await _characters(
                client,
                args.campaign_id,
                [cast["actor_id"]],
            )
        )
    campaign = await _campaign(client, args.campaign_id)
    passive_perception: dict[str, int] = {}
    visible_to_actor_ids_by_hostile: dict[str, list[str]] = {}
    surprise_modes = sum(
        (
            bool(args.no_surprise),
            args.surprise_check_report is not None,
            bool(args.party_stealth_check_report),
            bool(args.source_surprised_actor_id),
        )
    )
    if surprise_modes > 1:
        raise ValueError(
            "--no-surprise, --surprise-check-report, "
            "--party-stealth-check-report, and --source-surprised-actor-id "
            "are mutually exclusive"
        )
    if args.no_surprise:
        surprise = {actor_id: False for actor_id in [*party_ids, *initial_hostile_ids]}
        surprise_basis = {
            "mode": "source_scene_no_surprise",
            "source_excerpt": str(args.source_excerpt or ""),
        }
        expected_revision = campaign["revision"]
        if selected_hidden_ids:
            (
                _ignored_surprise,
                passive_perception,
                hidden_basis,
                expected_revision,
            ) = await _roll_hostile_stealth(
                client,
                args,
                branch_id=str(branch["id"]),
                actors=actors,
                party_ids=party_ids,
                hostile_ids=selected_hidden_ids,
            )
            visible_to_actor_ids_by_hostile = {
                hostile_id: [
                    actor_id
                    for actor_id in party_ids
                    if passive_perception[actor_id]
                    >= int(dict(hidden_basis["stealth_totals"])[hostile_id])
                ]
                for hostile_id in selected_hidden_ids
            }
            surprise_basis["hidden_positioning"] = hidden_basis
    elif args.surprise_check_report is not None:
        surprise, surprise_basis = _surprise_from_check_report(
            args.surprise_check_report,
            campaign_id=args.campaign_id,
            scene_id=args.scene_id,
            location_key=args.location_key,
            party_ids=party_ids,
            hostile_ids=initial_hostile_ids,
        )
        expected_revision = campaign["revision"]
        if selected_hidden_ids:
            scout_success = bool(dict(surprise_basis.get("check") or {}).get("success"))
            visible_to_actor_ids_by_hostile = {
                hostile_id: list(party_ids) if scout_success else []
                for hostile_id in selected_hidden_ids
            }
    elif args.party_stealth_check_report:
        surprise, surprise_basis = _surprise_from_party_stealth_reports(
            args.party_stealth_check_report,
            campaign_id=args.campaign_id,
            scene_id=args.scene_id,
            location_key=args.location_key,
            party_ids=party_ids,
            hostile_ids=initial_hostile_ids,
        )
        expected_revision = campaign["revision"]
    elif args.source_surprised_actor_id:
        surprise, surprise_basis = _source_declared_surprise(
            party_ids=party_ids,
            hostile_ids=initial_hostile_ids,
            surprised_actor_ids=args.source_surprised_actor_id,
            source_excerpt=str(args.source_excerpt or ""),
        )
        expected_revision = campaign["revision"]
    else:
        (
            surprise,
            passive_perception,
            surprise_basis,
            expected_revision,
        ) = await _roll_hostile_stealth(
            client,
            args,
            branch_id=str(branch["id"]),
            actors=actors,
            party_ids=party_ids,
            hostile_ids=selected_hidden_ids or initial_hostile_ids,
        )
        surprise.update(
            {actor_id: False for actor_id in initial_hostile_ids if actor_id not in surprise}
        )
        visible_to_actor_ids_by_hostile = {
            hostile_id: [
                actor_id
                for actor_id in party_ids
                if passive_perception[actor_id]
                >= int(dict(surprise_basis["stealth_totals"])[hostile_id])
            ]
            for hostile_id in (selected_hidden_ids or initial_hostile_ids)
        }
    start_request = {
        "campaign_id": args.campaign_id,
        "participant_ids": [*party_ids, *initial_hostile_ids],
        "participant_config": _participant_config(
            pc_ids,
            initial_hostile_ids,
            ally_ids=ally_ids,
            surprise_by_actor=surprise,
            hostiles_hidden=(
                args.hostiles_hidden
                or surprise_basis.get("mode")
                in {"source_shared_hostile_stealth", "individual_hostile_stealth"}
            ),
            hidden_actor_ids=selected_hidden_ids,
            visible_to_actor_ids_by_hostile=visible_to_actor_ids_by_hostile,
            source_conditions_by_actor=source_conditions_by_actor,
            source_traits_by_actor=source_traits_by_actor,
            source_separations=source_separations,
            agent_positions=agent_positions,
        ),
        "participant_manifest": _participant_manifest(
            hostile_ids,
            label=args.hostile_label,
            source_excerpt=_primary_hostile_source_excerpt(args),
            additional_hostile_ids=additional_hostile_ids,
            additional_label=args.additional_hostile_label,
            additional_source_excerpt=str(args.additional_hostile_source_excerpt or ""),
            reinforcement_hostile_ids=reinforcement_hostile_ids,
            reinforcement_label=args.reinforcement_hostile_label,
            reinforcement_source_excerpt=str(args.reinforcement_hostile_source_excerpt or ""),
        ),
        "name": args.encounter_name,
        "scene_id": args.scene_id,
        "battle_map": {"location_key": args.location_key},
        "ruleset": "2014",
        "branch_id": branch["id"],
        "expected_revision": expected_revision,
    }
    start_request["idempotency_key"] = _encounter_start_operation_token(start_request)
    started = await client.domain("combat_start", start_request)
    opened_combat = await client.open(args.campaign_id)
    await client.load(
        "combat.observe",
        "combat.actions",
        "combat.turn",
        "combat.control",
        "combat.save",
        "combat.map",
    )
    reinforcement_queue: list[dict[str, Any]] = []
    for index, actor_id in enumerate(reinforcement_hostile_ids):
        campaign = await _campaign(client, args.campaign_id)
        reinforcement_queue.append(
            await client.domain(
                "combat_join",
                {
                    "campaign_id": args.campaign_id,
                    "actor_id": actor_id,
                    "participant_config": _reinforcement_config(
                        actor_id,
                        index,
                        join_round=int(args.reinforcement_round or 0),
                    ),
                    "branch_id": branch["id"],
                    "expected_revision": campaign["revision"],
                    "idempotency_key": (
                        "encounter-queue-reinforcement-" + _operation_token(args, actor_id)
                    ),
                },
            )
        )
    status = await client.domain(
        "combat_query",
        {"campaign_id": args.campaign_id, "view": "status"},
    )
    return {
        "play_exposure": opened_play,
        "surprise_basis": surprise_basis,
        "passive_perception": passive_perception,
        "visible_to_actor_ids_by_hostile": visible_to_actor_ids_by_hostile,
        "surprise": surprise,
        "source_conditions_by_actor": source_conditions_by_actor,
        "source_traits_by_actor": source_traits_by_actor,
        "source_zero_hp_finisher": source_zero_hp_finisher,
        "source_zero_hp_stabilization": source_zero_hp_stabilization,
        "source_target_priorities": list(
            {
                tuple(value["actor_ids"]): value
                for value in source_target_priorities.values()
            }.values()
        ),
        "agent_target_priorities": list(
            {
                tuple(value["actor_ids"]): value
                for value in agent_target_priorities.values()
            }.values()
        ),
        "source_precombat_casts": precombat_cast_results,
        "source_opening_weapons": list(opening_weapons.values()),
        "source_on_hit_rulings": list(on_hit_rulings.values()),
        "source_delayed_actions": list(delayed_actions.values()),
        "source_passive_allies": list(passive_allies.values()),
        "source_random_activities": list(random_activities.values()),
        "source_save_activities": list(save_activities.values()),
        "source_contest_activities": list(contest_activities.values()),
        "source_attack_environments": list(attack_environments.values()),
        "source_casualty_pools": list(source_casualty_pools.values()),
        "source_separations": list(source_separations.values()),
        "agent_positions": list(agent_positions.values()),
        "source_avoidances": source_avoidance_evidence,
        "precombat_loadouts": precombat_loadouts,
        "source_opening_casts": _source_opening_casts(
            args.source_opening_cast_json,
            participant_ids=[*party_ids, *all_hostile_ids],
        ),
        "start": started,
        "reinforcement_queue": reinforcement_queue,
        "combat_exposure": opened_combat,
        "combat": status,
        "actors": [_character_summary(actors[item]) for item in actors],
    }


def _hit_points(actor: dict[str, Any]) -> int:
    return int(
        dict(dict(actor.get("sheet") or {}).get("combat") or {}).get("hp", {}).get("value", 0) or 0
    )


def _conditions(actor: dict[str, Any]) -> set[str]:
    return {str(item).casefold() for item in dict(actor.get("sheet") or {}).get("conditions", [])}


def _should_stand(actor: dict[str, Any], available_actions: set[str]) -> bool:
    return _hit_points(actor) > 0 and "prone" in _conditions(actor) and "move" in available_actions


def _choose_party_spell(
    actor_id: str,
    *,
    party_ids: list[str],
    actors: dict[str, dict[str, Any]],
    living_targets: list[str],
    leveled_spell_available: bool = True,
) -> tuple[str, str, int] | None:
    """Choose a prepared/known supported spell and the lowest legal available slot."""

    if not leveled_spell_available:
        return None
    actor = actors[actor_id]
    sheet = dict(actor.get("sheet") or {})
    spellcasting = dict(sheet.get("spellcasting") or {})
    preparation = dict(spellcasting.get("preparation") or {})
    spell_cards = [
        item
        for item in dict(sheet.get("content") or {}).get("spells", [])
        if isinstance(item, dict) and str(item.get("id") or "")
    ]
    selected_ids = {str(item) for item in preparation.get("selected_spell_ids", []) if str(item)}
    derived_prepared_ids = {
        str(item)
        for item in dict(dict(actor.get("derived") or {}).get("spellcasting") or {}).get(
            "prepared_spell_ids", []
        )
        if str(item)
    }
    known_ids = {
        str(item["id"])
        for item in spell_cards
        if dict(item.get("access") or {}).get("known") is True
        or dict(item.get("access") or {}).get("prepared") is True
    }
    if preparation:
        spells = selected_ids | derived_prepared_ids | known_ids
    else:
        spells = {str(item["id"]) for item in spell_cards}
    available_slot_levels = sorted(
        int(level)
        for level, slot in dict(spellcasting.get("spell_slots") or {}).items()
        if str(level).isdigit() and int(level) >= 1 and int(dict(slot).get("value", 0) or 0) > 0
    )
    if not available_slot_levels:
        return None
    cast_level = available_slot_levels[0]
    if actor_id in party_ids:
        downed_allies = [
            ally_id
            for ally_id in party_ids
            if ally_id != actor_id
            and _hit_points(actors[ally_id]) == 0
            and "dead" not in _conditions(actors[ally_id])
        ]
        downed_allies.sort(key=lambda item: "stable" in _conditions(actors[item]))
        if HEALING_WORD_ID in spells and downed_allies:
            return HEALING_WORD_ID, downed_allies[0], cast_level
    if MAGIC_MISSILE_ID in spells and living_targets:
        return MAGIC_MISSILE_ID, living_targets[0], cast_level
    if GUIDING_BOLT_ID in spells and living_targets:
        return GUIDING_BOLT_ID, living_targets[0], cast_level
    return None


def _distance(left: dict[str, Any], right: dict[str, Any]) -> int:
    return max(abs(int(left["x"]) - int(right["x"])), abs(int(left["y"]) - int(right["y"])))


def _observable_target_ids(
    combat: dict[str, Any],
    *,
    observer_id: str,
    target_ids: list[str],
) -> list[str]:
    combatants = {
        str(item.get("actor_id") or ""): item
        for item in combat.get("combatants", [])
        if isinstance(item, dict)
    }
    observable = []
    for target_id in target_ids:
        target = combatants.get(target_id)
        if target is None or target.get("inside_host"):
            continue
        visible_to = target.get("visible_to_actor_ids")
        if not target.get("hidden") or (isinstance(visible_to, list) and observer_id in visible_to):
            observable.append(target_id)
    return observable


def _body_thief_sides(
    combat: dict[str, Any],
    *,
    party_ids: list[str],
    hostile_ids: list[str],
) -> dict[str, Any]:
    """Map a Body Thief host to its controller without erasing either actor."""

    combatants = {
        str(item.get("actor_id") or ""): item
        for item in combat.get("combatants", [])
        if isinstance(item, dict)
    }
    controlled_hosts = {
        actor_id: str(item.get("controlled_by_actor_id") or "")
        for actor_id, item in combatants.items()
        if actor_id in party_ids
        and str(item.get("controlled_by_actor_id") or "") in hostile_ids
        and item.get("body_thief_host")
    }
    inside_sources = {
        actor_id
        for actor_id, item in combatants.items()
        if actor_id in hostile_ids and item.get("inside_host")
    }
    effective_party_ids = [actor_id for actor_id in party_ids if actor_id not in controlled_hosts]
    attackable_hostile_ids = [
        actor_id for actor_id in hostile_ids if actor_id not in inside_sources
    ] + list(controlled_hosts)
    hostile_turn_actor_ids = set(attackable_hostile_ids)
    return {
        "controlled_hosts": controlled_hosts,
        "inside_sources": inside_sources,
        "effective_party_ids": effective_party_ids,
        "attackable_hostile_ids": attackable_hostile_ids,
        "hostile_turn_actor_ids": hostile_turn_actor_ids,
    }


def _body_thief_target_ids(
    combat: dict[str, Any],
    *,
    actors: dict[str, dict[str, Any]],
    source_actor_id: str,
    party_ids: list[str],
    range_ft: int,
) -> list[str]:
    """Return living incapacitated targets, including creatures at 0 HP."""

    combatants = {
        str(item.get("actor_id") or ""): item
        for item in combat.get("combatants", [])
        if isinstance(item, dict)
    }
    source = combatants.get(source_actor_id)
    if source is None:
        return []
    source_position = dict(source.get("position") or {"x": 0, "y": 0})
    eligible = [
        target_id
        for target_id in party_ids
        if target_id in actors
        and target_id in combatants
        and "dead" not in _conditions(actors[target_id])
        and _conditions(actors[target_id])
        & {
            "incapacitated",
            "paralyzed",
            "petrified",
            "stunned",
            "unconscious",
        }
        and _distance(
            source_position,
            dict(combatants[target_id].get("position") or {"x": 0, "y": 0}),
        )
        * 5
        <= range_ft
    ]
    eligible.sort(
        key=lambda target_id: _distance(
            source_position,
            dict(combatants[target_id].get("position") or {"x": 0, "y": 0}),
        )
    )
    return eligible


def _has_action_budget(combat: dict[str, Any], actor_id: str) -> bool:
    combatant = next(
        (
            item
            for item in combat.get("combatants", [])
            if isinstance(item, dict) and str(item.get("actor_id") or "") == actor_id
        ),
        None,
    )
    if combatant is None:
        return False
    budget = dict(combatant.get("turn_budget") or {})
    return int(budget.get("main_action", 0) or 0) > 0 or int(budget.get("extra_action", 0) or 0) > 0


def _wound_priority(actor: dict[str, Any]) -> tuple[bool, float]:
    hp = dict(dict(actor.get("sheet") or {}).get("combat") or {}).get("hp", {})
    current = max(0, int(dict(hp).get("value", 0) or 0))
    maximum = max(1, int(dict(hp).get("max", current) or current or 1))
    return current >= maximum, current / maximum


def _choose_destination(
    combat: dict[str, Any],
    actor_id: str,
    target_id: str,
    *,
    avoided_cells: set[str] | None = None,
) -> tuple[dict[str, int], int, list[dict[str, int]]] | None:
    combatants = list(combat.get("combatants") or [])
    acting = next(item for item in combatants if item.get("actor_id") == actor_id)
    target = next(item for item in combatants if item.get("actor_id") == target_id)
    origin = dict(acting.get("position") or {})
    goal = dict(target.get("position") or {})
    if set(origin) != {"x", "y"} or set(goal) != {"x", "y"}:
        return None
    conditions = {str(item).casefold() for item in acting.get("conditions", [])}
    if conditions & {
        "dead",
        "unconscious",
        "stunned",
        "paralyzed",
        "petrified",
        "restrained",
        "grappled",
        "prone",
    } or bool(acting.get("surprised")):
        return None
    budget_cells = int(dict(acting.get("turn_budget") or {}).get("movement", 0) or 0) // 5
    if budget_cells <= 0:
        return None

    def _source_details(
        source_id: str,
    ) -> tuple[bool, dict[str, Any] | None] | None:
        source = next(
            (item for item in combatants if str(item.get("actor_id") or "") == source_id),
            None,
        )
        if source is None:
            return None
        visible_to = source.get("visible_to_actor_ids")
        if isinstance(visible_to, list):
            visible = actor_id in {str(item) for item in visible_to}
        else:
            source_conditions = {str(item).casefold() for item in source.get("conditions", [])}
            visible = not source.get("hidden", False) and "invisible" not in source_conditions
        position = dict(source.get("position") or {})
        return visible, position if set(position) == {"x", "y"} else None

    fear_source_positions: list[dict[str, Any]] = []
    if "frightened" in conditions:
        raw_fear_sources = dict(acting.get("condition_sources") or {}).get("frightened")
        if not isinstance(raw_fear_sources, list) or not raw_fear_sources:
            return None
        for source_id in raw_fear_sources:
            source_details = _source_details(str(source_id))
            if source_details is None:
                return None
            visible, source_position = source_details
            if visible and source_position is None:
                return None
            if visible:
                fear_source_positions.append(source_position)

    turn_source_position = None
    if "turned" in conditions:
        turn_source_id = str(dict(acting.get("turned") or {}).get("source_actor_id") or "")
        if not turn_source_id:
            return None
        turn_source_details = _source_details(turn_source_id)
        if turn_source_details is None or turn_source_details[1] is None:
            return None
        turn_source_position = turn_source_details[1]

    occupied = {
        (
            int(dict(item.get("position") or {}).get("x", -1)),
            int(dict(item.get("position") or {}).get("y", -1)),
        )
        for item in combatants
        if item.get("actor_id") != actor_id
        and "dead" not in {str(value).casefold() for value in item.get("conditions", [])}
        and isinstance(item.get("position"), dict)
    }
    battle_map = dict(combat.get("battle_map") or {})
    bounds = dict(battle_map.get("bounds") or {})
    width = int(bounds.get("width_cells", 0) or 0)
    height = int(bounds.get("height_cells", 0) or 0)
    if width <= 0 or height <= 0:
        return None
    blocked_cells = {
        *set(battle_map.get("blocked_cells") or []),
        *set(avoided_cells or set()),
    }
    difficult_cells = set(battle_map.get("difficult_cells") or [])
    origin_cell = (int(origin["x"]), int(origin["y"]))
    goal_cell = (int(goal["x"]), int(goal["y"]))
    budget_ft = budget_cells * 5
    costs: dict[tuple[int, int], int] = {origin_cell: 0}
    steps_by_cell: dict[tuple[int, int], int] = {origin_cell: 0}
    previous: dict[tuple[int, int], tuple[int, int]] = {}
    queue: list[tuple[int, int, int, int]] = [(0, 0, origin_cell[0], origin_cell[1])]
    while queue:
        cost, steps, x, y = heapq.heappop(queue)
        current_cell = (x, y)
        if cost != costs.get(current_cell) or steps != steps_by_cell.get(current_cell):
            continue
        for delta_x in (-1, 0, 1):
            for delta_y in (-1, 0, 1):
                if delta_x == 0 and delta_y == 0:
                    continue
                neighbor = (x + delta_x, y + delta_y)
                if (
                    not 0 <= neighbor[0] < width
                    or not 0 <= neighbor[1] < height
                    or neighbor in occupied
                    or neighbor == goal_cell
                    or f"{neighbor[0]},{neighbor[1]}" in blocked_cells
                ):
                    continue
                current_position = {"x": x, "y": y}
                neighbor_position = {
                    "x": neighbor[0],
                    "y": neighbor[1],
                }
                if any(
                    _distance(neighbor_position, source_position)
                    < _distance(current_position, source_position)
                    for source_position in fear_source_positions
                ):
                    continue
                next_steps = steps + 1
                next_cost = cost + (10 if f"{neighbor[0]},{neighbor[1]}" in difficult_cells else 5)
                if next_cost > budget_ft:
                    continue
                previous_best = (
                    costs.get(neighbor, budget_ft + 1),
                    steps_by_cell.get(neighbor, budget_cells + 1),
                )
                if (next_cost, next_steps) >= previous_best:
                    continue
                costs[neighbor] = next_cost
                steps_by_cell[neighbor] = next_steps
                previous[neighbor] = current_cell
                heapq.heappush(
                    queue,
                    (next_cost, next_steps, neighbor[0], neighbor[1]),
                )
    origin_target_distance = _distance(origin, goal)
    candidates: list[tuple[int, int, int, int, int]] = []
    for (x, y), cost in costs.items():
        if (x, y) == origin_cell:
            continue
        destination = {"x": x, "y": y}
        target_distance = _distance(destination, goal)
        if target_distance >= origin_target_distance:
            continue
        if turn_source_position is not None and _distance(
            destination, turn_source_position
        ) <= _distance(origin, turn_source_position):
            continue
        candidates.append(
            (
                target_distance,
                cost,
                steps_by_cell[(x, y)],
                x,
                y,
            )
        )
    if not candidates:
        return None
    _, _, steps, x, y = min(candidates)
    selected = (x, y)
    reverse_path = [selected]
    while reverse_path[-1] != origin_cell:
        reverse_path.append(previous[reverse_path[-1]])
    route = [{"x": point[0], "y": point[1]} for point in reversed(reverse_path[:-1])]
    return {"x": x, "y": y}, steps * 5, route


def _current_actor_id(combat: dict[str, Any]) -> str:
    combatants = list(combat.get("combatants") or [])
    if not combatants:
        raise RuntimeError("combat has no participants")
    return str(combatants[int(combat.get("turn_index", 0)) % len(combatants)]["actor_id"])


def _has_blocking_pending(combat: dict[str, Any]) -> bool:
    return any(
        item.get("status", "pending") == "pending"
        for item in combat.get("pending", [])
        if isinstance(item, dict)
    )


def _defense_selection(pending: dict[str, Any]) -> dict[str, Any]:
    """Choose a defense only when it prevents the triggering hit or missiles."""

    trigger = str(pending.get("trigger") or "")
    candidates = [
        item
        for item in pending.get("candidates", [])
        if isinstance(item, dict)
        and str(item.get("id") or "") not in {"", "decline", "skip", "pass"}
    ]
    selected = next(
        (
            item
            for item in candidates
            if trigger == "magic_missile_targeted"
            or (trigger == "attack_hit_defense" and item.get("projected_hit") is False)
        ),
        None,
    )
    if selected is None:
        return {"id": "decline"}
    selection: dict[str, Any] = {"id": str(selected["id"])}
    cast_levels = sorted(
        int(level)
        for level in selected.get("cast_levels", [])
        if isinstance(level, int) and not isinstance(level, bool) and level > 0
    )
    if cast_levels:
        selection["cast_level"] = cast_levels[0]
    return selection


def _source_outcome(
    *,
    defeated_hostiles: int,
    fled_hostiles: int = 0,
    hostile_count: int,
    unresolved_party: bool,
    party_down: bool,
) -> tuple[str, str] | None:
    if unresolved_party:
        return None
    if hostile_count > 0 and defeated_hostiles + fled_hostiles >= hostile_count:
        if fled_hostiles:
            return (
                "victory",
                f"{defeated_hostiles} source-defined hostiles were defeated and "
                f"{fled_hostiles} followed a source instruction to flee.",
            )
        return (
            "victory",
            f"All {hostile_count} source-defined hostiles were defeated.",
        )
    if party_down:
        return (
            "defeat",
            "The party was defeated. Combat ended with resolved unconscious or dead "
            "characters; their later treatment requires explicit source support or "
            "Agent-as-DM adjudication.",
        )
    return None


def _postcombat_stabilization_target(
    *,
    actor_id: str,
    party_ids: list[str],
    actors: dict[str, dict[str, Any]],
    defeated_hostiles: int,
    fled_hostiles: int,
    hostile_count: int,
) -> str | None:
    """Choose a dying ally only after every source hostile is resolved."""

    actor = actors[actor_id]
    if (
        actor_id not in party_ids
        or _hit_points(actor) <= 0
        or _conditions(actor)
        & {"dead", "unconscious", "stunned", "incapacitated", "paralyzed", "petrified"}
        or hostile_count <= 0
        or defeated_hostiles + fled_hostiles < hostile_count
    ):
        return None
    return next(
        (
            ally_id
            for ally_id in party_ids
            if ally_id != actor_id
            and _hit_points(actors[ally_id]) == 0
            and not _conditions(actors[ally_id]) & {"dead", "stable"}
        ),
        None,
    )


def _source_flee_ready(
    *,
    acting_actor_id: str,
    flee_actor_ids: set[str],
    defeated_hostile_ids: list[str],
    flee_after_defeated: int,
    trigger_defeated_actor_id: str,
    damage_taken_by_actor: dict[str, int] | None = None,
    flee_after_damage: int = 0,
    critical_hit_actor_ids: set[str] | None = None,
    flee_on_critical: bool = False,
) -> bool:
    """Return whether the source-designated actor must now attempt to leave."""

    if acting_actor_id not in flee_actor_ids:
        return False
    if trigger_defeated_actor_id:
        if trigger_defeated_actor_id in defeated_hostile_ids:
            return True
    if flee_after_defeated > 0 and len(defeated_hostile_ids) >= flee_after_defeated:
        return True
    if flee_after_damage > 0 and int(
        dict(damage_taken_by_actor or {}).get(acting_actor_id, 0) or 0
    ) >= flee_after_damage:
        return True
    return flee_on_critical and acting_actor_id in set(critical_hit_actor_ids or set())


def _validate_source_flee_configuration(
    args: argparse.Namespace,
    *,
    hostile_ids: list[str],
) -> set[str]:
    """Validate source retreat triggers without widening encounter authority."""

    source_flee_ids = {
        *(str(actor_id) for actor_id in args.flee_actor_id),
        str(args.flee_trigger_defeated_actor_id or ""),
        str(args.flee_on_start_actor_id or ""),
    } - {""}
    triggered_flee_configured = bool(
        args.flee_actor_id
        or args.flee_trigger_defeated_actor_id
        or args.flee_after_defeated
        or args.flee_after_damage
        or args.flee_on_critical
    )
    defeated_flee_triggers = int(bool(args.flee_trigger_defeated_actor_id)) + int(
        bool(args.flee_after_defeated)
    )
    has_triggered_flee_condition = bool(
        defeated_flee_triggers
        or args.flee_after_damage
        or args.flee_on_critical
    )
    if triggered_flee_configured and (
        not args.flee_actor_id
        or not has_triggered_flee_condition
        or defeated_flee_triggers > 1
    ):
        raise ValueError(
            "source-specific triggered flee requires --flee-actor-id, at least one "
            "damage, critical-hit, or defeat trigger, and no more than one defeat trigger"
        )
    if args.flee_after_defeated < 0 or args.flee_after_damage < 0:
        raise ValueError("source flee thresholds must not be negative")
    if source_flee_ids and (
        not source_flee_ids <= set(hostile_ids) or not str(args.flee_source_excerpt or "").strip()
    ):
        raise ValueError(
            "source-specific flee actors must be encounter hostiles and require "
            "--flee-source-excerpt"
        )
    if source_flee_ids and _normalized_source_text(args.flee_source_excerpt) not in (
        _normalized_source_text(args.source_excerpt)
    ):
        raise ValueError("source-specific flee excerpt must be contained in --source-excerpt")
    if args.flee_actor_id and (
        args.flee_trigger_defeated_actor_id in args.flee_actor_id or args.flee_on_start_actor_id
    ):
        raise ValueError(
            "triggered and on-start source departures are mutually exclusive, and "
            "triggered actors must be distinct"
        )
    return source_flee_ids


def _record_source_flee_damage(
    response: dict[str, Any] | None,
    *,
    flee_actor_ids: set[str],
    damage_taken_by_actor: dict[str, int],
    critical_hit_actor_ids: set[str],
) -> list[dict[str, Any]]:
    """Record server-settled damage and critical-hit facts used by retreat rules."""

    result = dict(dict(response or {}).get("result") or {})
    observations: list[dict[str, Any]] = []

    def record(target_id: str, applied_amount: int, *, critical_hit: bool) -> None:
        if target_id not in flee_actor_ids:
            return
        if applied_amount < 0:
            raise RuntimeError("server settlement returned negative applied damage")
        damage_taken_by_actor[target_id] = (
            int(damage_taken_by_actor.get(target_id, 0) or 0) + applied_amount
        )
        if critical_hit:
            critical_hit_actor_ids.add(target_id)
        if applied_amount > 0 or critical_hit:
            observations.append(
                {
                    "target_id": target_id,
                    "applied_damage": applied_amount,
                    "cumulative_applied_damage": damage_taken_by_actor[target_id],
                    "critical_hit": critical_hit,
                }
            )

    direct_target_id = str(result.get("target_id") or "")
    if direct_target_id:
        damage = dict(result.get("damage") or {})
        record(
            direct_target_id,
            int(damage.get("applied_amount", 0) or 0),
            critical_hit=bool(result.get("hit")) and bool(result.get("critical")),
        )
    if str(result.get("kind") or "") == "magic_missile":
        for target in result.get("targets", []):
            if not isinstance(target, dict):
                continue
            record(
                str(target.get("target_id") or ""),
                sum(
                    int(dict(dart).get("applied_amount", 0) or 0)
                    for dart in target.get("dart_results", [])
                    if isinstance(dart, dict)
                ),
                critical_hit=False,
            )
    return observations


def _source_flee_damage_history(
    combat: dict[str, Any],
    *,
    flee_actor_ids: set[str],
) -> tuple[dict[str, int], set[str]]:
    """Recover retreat damage and critical facts from the bounded combat log."""

    damage_taken_by_actor = {actor_id: 0 for actor_id in flee_actor_ids}
    critical_hit_actor_ids: set[str] = set()
    for event in combat.get("log", []):
        if not isinstance(event, dict):
            continue
        _record_source_flee_damage(
            {"result": event.get("result")},
            flee_actor_ids=flee_actor_ids,
            damage_taken_by_actor=damage_taken_by_actor,
            critical_hit_actor_ids=critical_hit_actor_ids,
        )
    return damage_taken_by_actor, critical_hit_actor_ids


def _source_casualty_pools(
    declarations: list[dict[str, Any]],
    *,
    hostile_ids: list[str],
    actors: dict[str, dict[str, Any]],
    encounter_source_excerpt: str,
) -> dict[str, dict[str, Any]]:
    """Validate source-authored non-PC casualty cohorts and recharge mechanics."""

    allowed = {
        "actor_id",
        "pool_key",
        "initial_count",
        "activity_name",
        "kill_expression",
        "injury_expression",
        "source_excerpt",
    }
    number_words = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "thirteen": 13,
        "fourteen": 14,
        "fifteen": 15,
        "sixteen": 16,
        "seventeen": 17,
        "eighteen": 18,
        "nineteen": 19,
        "twenty": 20,
    }
    encounter_excerpt = _normalized_source_text(encounter_source_excerpt)
    by_actor: dict[str, dict[str, Any]] = {}
    pool_keys: set[str] = set()
    for index, declaration in enumerate(declarations):
        if not isinstance(declaration, dict):
            raise ValueError(f"source casualty pool {index} must be an object")
        unknown = set(declaration) - allowed
        if unknown:
            raise ValueError(
                f"source casualty pool {index} has unsupported fields: "
                + ", ".join(sorted(unknown))
            )
        actor_id = str(declaration.get("actor_id") or "").strip()
        pool_key = str(declaration.get("pool_key") or "").strip()
        activity_name = str(declaration.get("activity_name") or "").strip()
        kill_expression = str(declaration.get("kill_expression") or "").strip().casefold()
        injury_expression = str(declaration.get("injury_expression") or "").strip().casefold()
        source_excerpt = str(declaration.get("source_excerpt") or "").strip()
        initial_count = declaration.get("initial_count")
        if (
            actor_id not in hostile_ids
            or actor_id in by_actor
            or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", pool_key)
            or pool_key in pool_keys
            or isinstance(initial_count, bool)
            or not isinstance(initial_count, int)
            or initial_count <= 0
            or not activity_name
            or re.fullmatch(r"\d+d\d+(?:[+-]\d+)?", kill_expression) is None
            or re.fullmatch(r"\d+d\d+(?:[+-]\d+)?", injury_expression) is None
            or not source_excerpt
            or _normalized_source_text(source_excerpt) not in encounter_excerpt
        ):
            raise ValueError(
                f"source casualty pool {index} requires one unique hostile actor and "
                "pool key, a positive initial count, dice expressions, an activity, "
                "and an exact contained source excerpt"
            )
        normalized_excerpt = _normalized_source_text(source_excerpt)
        count_match = re.search(
            r"\bthere are (?P<count>\d+|"
            + "|".join(number_words)
            + r") npc defenders\b",
            normalized_excerpt,
        )
        parsed_count = None
        if count_match is not None:
            raw_count = str(count_match.group("count"))
            parsed_count = int(raw_count) if raw_count.isdigit() else number_words[raw_count]
        casualty_pattern = re.compile(
            rf"\bkills {re.escape(kill_expression)} npc defenders and "
            rf"injures {re.escape(injury_expression)} more\b"
        )
        if (
            parsed_count != initial_count
            or casualty_pattern.search(normalized_excerpt) is None
            or re.search(
                r"\buntil (?:his|its|the dragon(?:'s)?) breath weapon recharges\b",
                normalized_excerpt,
            )
            is None
        ):
            raise ValueError(
                f"source casualty pool {index} does not match the authored initial "
                "count, casualty dice, and recharge instruction"
            )
        actor = actors.get(actor_id)
        activities = (
            list(
                dict(dict(actor or {}).get("sheet") or {})
                .get("content", {})
                .get("activities", [])
            )
            if actor is not None
            else []
        )
        activity = next(
            (
                item
                for item in activities
                if isinstance(item, dict)
                and str(item.get("name") or "").strip().casefold() == activity_name.casefold()
            ),
            None,
        )
        manual_ruling = dict(
            dict(activity.get("choices") or {}).get("manual_ruling") or {}
        ) if activity is not None else {}
        recharge_match = re.search(
            r"(?i)\brecharge\s+(?P<minimum>[1-6])"
            r"(?:\s*[-\u2013]\s*(?P<maximum>[1-6]))?",
            activity_name,
        )
        if (
            activity is None
            or not str(activity.get("id") or "").strip()
            or recharge_match is None
            or manual_ruling.get("kind") != "descriptive_activity"
            or _normalized_source_text(str(manual_ruling.get("source_excerpt") or ""))
            != _normalized_source_text(str(activity.get("description") or ""))
        ):
            raise ValueError(
                f"source casualty pool {index} activity is not a reviewed rechargeable "
                "descriptive activity on the hostile actor card"
            )
        recharge_minimum = int(recharge_match.group("minimum"))
        recharge_maximum = int(recharge_match.group("maximum") or recharge_minimum)
        if recharge_minimum > recharge_maximum:
            raise ValueError(f"source casualty pool {index} has an invalid recharge range")
        value = {
            "actor_id": actor_id,
            "pool_key": pool_key,
            "initial_count": initial_count,
            "activity_id": str(activity.get("id") or ""),
            "activity_name": activity_name,
            "activity_source_key": str(activity.get("source_key") or ""),
            "activity_rule_refs": deepcopy(list(activity.get("rule_refs") or [])),
            "kill_expression": kill_expression,
            "injury_expression": injury_expression,
            "recharge_expression": "1d6",
            "recharge_minimum": recharge_minimum,
            "recharge_maximum": recharge_maximum,
            "source_excerpt": source_excerpt,
        }
        by_actor[actor_id] = value
        pool_keys.add(pool_key)
    return by_actor


def _apply_source_casualty_rolls(
    state: dict[str, Any] | None,
    *,
    declaration: dict[str, Any],
    combat_id: str,
    round_number: int,
    recharge_roll: int | None,
    kill_roll: int | None,
    injury_roll: int | None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Apply one idempotent cohort recharge/breath event to manifest world state."""

    value = deepcopy(dict(state or {}))
    if value:
        immutable = {
            "actor_id": declaration["actor_id"],
            "initial_count": declaration["initial_count"],
            "activity_name": declaration["activity_name"],
            "kill_expression": declaration["kill_expression"],
            "injury_expression": declaration["injury_expression"],
        }
        if any(value.get(key) != expected for key, expected in immutable.items()):
            raise RuntimeError("source casualty pool state conflicts with its declaration")
    else:
        value = {
            "actor_id": declaration["actor_id"],
            "initial_count": declaration["initial_count"],
            "activity_name": declaration["activity_name"],
            "kill_expression": declaration["kill_expression"],
            "injury_expression": declaration["injury_expression"],
            "killed": 0,
            "injured": 0,
            "able": declaration["initial_count"],
            "attacks": 0,
            "events": [],
        }
    event_id = f"{combat_id}:{round_number}:{declaration['actor_id']}"
    existing = next(
        (
            item
            for item in value.get("events", [])
            if isinstance(item, dict) and str(item.get("id") or "") == event_id
        ),
        None,
    )
    if existing is not None:
        return value, deepcopy(existing), True
    recharged = recharge_roll is None or (
        declaration["recharge_minimum"]
        <= recharge_roll
        <= declaration["recharge_maximum"]
    )
    event = {
        "id": event_id,
        "round": round_number,
        "recharge_roll": recharge_roll,
        "recharged": recharged,
        "kill_roll": None,
        "injury_roll": None,
        "killed": 0,
        "injured": 0,
        "able_before": int(value["able"]),
        "able_after": int(value["able"]),
    }
    if recharged:
        if kill_roll is None or injury_roll is None:
            raise ValueError("a recharged casualty activity requires both casualty rolls")
        available = int(value["able"])
        killed = min(max(0, kill_roll), available)
        injured = min(max(0, injury_roll), available - killed)
        value["killed"] = int(value["killed"]) + killed
        value["injured"] = int(value["injured"]) + injured
        value["able"] = available - killed - injured
        value["attacks"] = int(value["attacks"]) + 1
        event.update(
            kill_roll=kill_roll,
            injury_roll=injury_roll,
            killed=killed,
            injured=injured,
            able_after=int(value["able"]),
        )
    value["events"] = [*list(value.get("events") or []), event]
    return value, event, False


def _source_truce_outcome(
    *,
    defeated_hostiles: int,
    truce_after_defeated: int,
    truce_actor_alive: bool,
    unresolved_party: bool,
) -> tuple[str, str] | None:
    if (
        truce_after_defeated > 0
        and defeated_hostiles >= truce_after_defeated
        and truce_actor_alive
        and not unresolved_party
    ):
        return (
            "truce",
            f"After {defeated_hostiles} source-defined hostiles were defeated, "
            "the source-designated leader invoked the hostage truce.",
        )
    return None


def _source_surrender_outcome(
    *,
    actor_hit_points: int,
    surrender_at_hp: int,
    defeated_hostiles: int = 0,
    surrender_after_defeated: int = 0,
    actor_alive: bool,
    no_escape: bool,
    unresolved_party: bool,
) -> tuple[str, str] | None:
    threshold_met = surrender_at_hp > 0 and 0 < actor_hit_points <= surrender_at_hp
    casualties_met = surrender_after_defeated > 0 and defeated_hostiles >= surrender_after_defeated
    if (threshold_met or casualties_met) and actor_alive and no_escape and not unresolved_party:
        if casualties_met:
            return (
                "surrender",
                f"After {defeated_hostiles} source-defined hostiles were defeated, "
                "the source-designated survivor surrendered with no avenue of escape.",
            )
        return (
            "surrender",
            f"The source-designated hostile surrendered at {actor_hit_points} hit points "
            f"(threshold {surrender_at_hp}) with no avenue of escape.",
        )
    return None


def _source_zero_hp_finisher_stage(
    combat: dict[str, Any],
    finisher: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    events = [
        item
        for item in combat.get("log", [])
        if isinstance(item, dict)
        and item.get("type") == "common_action"
        and str(dict(item.get("payload") or {}).get("source_finisher_id") or "")
        == str(finisher["id"])
    ]
    ignited = next(
        (
            item
            for item in reversed(events)
            if dict(item.get("payload") or {}).get("stage") == "ignite"
        ),
        None,
    )
    if ignited is not None:
        return None, ignited
    doused = next(
        (
            item
            for item in reversed(events)
            if dict(item.get("payload") or {}).get("stage") == "douse"
        ),
        None,
    )
    if doused is None:
        return "douse", None
    doused_round = int(dict(doused.get("payload") or {}).get("round", 0) or 0)
    current_round = int(combat.get("round", 1) or 1)
    if current_round - doused_round >= int(finisher["oil_duration_rounds"]):
        return "douse", doused
    return "ignite", doused


async def _resolve_pending(
    client: ExposureClient,
    args: argparse.Namespace,
    branch_id: str,
    combat: dict[str, Any],
) -> dict[str, Any] | None:
    pending = next(
        (item for item in combat.get("pending", []) if item.get("status", "pending") == "pending"),
        None,
    )
    if pending is None:
        return None
    campaign = await _campaign(client, args.campaign_id)
    actor_id = str(pending.get("actor_id") or "")
    identity = f"{pending.get('id')}:{campaign['revision']}"
    if (
        pending.get("trigger") == "attack_on_hit_effect"
        and str(pending.get("effect") or "").strip().casefold() == GUIDING_BOLT_ON_HIT.casefold()
    ):
        return _facade_value(
            await client.domain(
                "combat_choice",
                {
                    "campaign_id": args.campaign_id,
                    "action": "on_hit_ruling",
                    "actor_id": str(pending.get("target_id") or actor_id),
                    "payload": {
                        "choice_id": str(pending["id"]),
                        "selection": {
                            "id": "next_attack_advantage",
                            "source_excerpt": GUIDING_BOLT_ON_HIT,
                        },
                    },
                    "branch_id": branch_id,
                    "expected_revision": campaign["revision"],
                    "idempotency_key": (
                        "encounter-guiding-bolt-on-hit-" + _token(identity, length=24)
                    ),
                },
            )
        )
    if pending.get("trigger") in {
        "attack_on_hit_effect",
        "critical_body_part_loss",
    }:
        declarations = list(args.source_on_hit_ruling_json or [])
        declared_actor_ids = sorted(
            {
                str(item.get("actor_id") or "").strip()
                for item in declarations
                if isinstance(item, dict) and str(item.get("actor_id") or "").strip()
            }
        )
        rulings = _source_on_hit_rulings(
            declarations,
            participant_ids=declared_actor_ids,
        )
        ruling = rulings.get(
            (
                str(pending.get("attacker_id") or ""),
                str(pending.get("weapon_id") or ""),
            )
        )
        if ruling is None:
            raise EncounterRulingRequiredError(
                {
                    "status": "pending_ruling",
                    "default_resolver": "agent",
                    "ruling_kind": "agent_dm_adjudication",
                    "reason": (
                        "the interrupted attack retains an unresolved reviewed "
                        "on-hit effect"
                    ),
                    "committed": True,
                    "retry_contract": {
                        "resolver": "agent",
                        "reuse_current_revision": True,
                        "use_public_tools_only": True,
                    },
                },
                operation="combat_choice.on_hit_ruling",
                actor_id=str(pending.get("attacker_id") or ""),
                target_id=str(pending.get("target_id") or actor_id),
                action={
                    "choice_id": str(pending.get("id") or ""),
                    "weapon_id": str(pending.get("weapon_id") or ""),
                },
                retry_hint=(
                    "Inspect the reviewed attack card and retry with one typed "
                    "--source-on-hit-ruling-json settlement."
                ),
            )
        return _facade_value(
            await client.domain(
                "combat_choice",
                {
                    "campaign_id": args.campaign_id,
                    "action": "on_hit_ruling",
                    "actor_id": str(pending.get("target_id") or actor_id),
                    "payload": {
                        "choice_id": str(pending["id"]),
                        "selection": {
                            key: value
                            for key, value in ruling.items()
                            if key not in {"actor_id", "weapon_id"}
                        },
                    },
                    "branch_id": branch_id,
                    "expected_revision": campaign["revision"],
                    "idempotency_key": (
                        "encounter-source-on-hit-resume-" + _token(identity, length=24)
                    ),
                },
            )
        )
    if pending.get("kind") == "concentration":
        return await client.domain(
            "combat_concentration_check",
            {
                "campaign_id": args.campaign_id,
                "target_id": actor_id,
                "dc": int(pending["dc"]),
                "effect_ids": list(pending.get("effect_ids") or []),
                "branch_id": branch_id,
                "expected_revision": campaign["revision"],
                "idempotency_key": f"encounter-concentration-{_token(identity, length=24)}",
            },
        )
    action = (
        "resolve_defense"
        if pending.get("trigger") in {"attack_hit_defense", "magic_missile_targeted"}
        else "resolve"
    )
    return await client.domain(
        "combat_choice",
        {
            "campaign_id": args.campaign_id,
            "actor_id": actor_id,
            "action": action,
            "payload": {
                "choice_id": pending["id"],
                "selection": _defense_selection(pending),
            },
            "branch_id": branch_id,
            "expected_revision": campaign["revision"],
            "idempotency_key": f"encounter-choice-{_token(identity, length=24)}",
        },
    )


async def _preflight_attack(
    client: ExposureClient,
    args: argparse.Namespace,
    actor: dict[str, Any],
    target_ids: list[str],
    *,
    preferred_weapon_id: str = "",
    multiattack_option_id: str = "",
    action_context: dict[str, Any] | None = None,
    knock_out_target_ids: set[str] | None = None,
    agent_rulings: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    knock_out_targets = set(knock_out_target_ids or set())
    weapons = list(
        dict(dict(actor.get("derived") or {}).get("inventory") or {}).get("weapon_attacks", [])
    )
    weapons.sort(key=lambda item: item.get("item_id") != preferred_weapon_id)
    for target_id in target_ids:
        for weapon in weapons or [{"item_id": "unarmed-strike", "attack_type": "melee"}]:
            attack_modes = [str(weapon.get("attack_type") or "melee")]
            properties = {
                str(item).strip().casefold() for item in weapon.get("properties", [])
            }
            thrown_range = dict(
                weapon.get("thrown_range_ft")
                or weapon.get("range_ft")
                or {}
            )
            if (
                "thrown" in properties
                and int(thrown_range.get("normal", 0) or 0) > 0
                and "ranged" not in attack_modes
            ):
                attack_modes.append("ranged")
            for attack_mode in attack_modes:
                if target_id in knock_out_targets and attack_mode != "melee":
                    continue
                action = {
                    "weapon_id": weapon.get("item_id"),
                    "attack_mode": attack_mode,
                }
                if target_id in knock_out_targets:
                    action["knock_out"] = True
                if action_context:
                    action["context"] = dict(action_context)
                if multiattack_option_id:
                    action["multiattack_option_id"] = multiattack_option_id
                try:
                    plan = await client.domain(
                        "combat_preflight_attack",
                        {
                            "campaign_id": args.campaign_id,
                            "actor_id": actor["id"],
                            "target_id": target_id,
                            "action": action,
                        },
                    )
                except RuntimeError:
                    continue
                if plan.get("status") == "pending_ruling":
                    missing = {
                        str(item)
                        for item in plan.get("missing", [])
                        if str(item)
                    }
                    if (
                        str(plan.get("default_resolver") or "") == "agent"
                        and any(item.startswith("weapon.targeting:") for item in missing)
                    ):
                        if agent_rulings is not None:
                            agent_rulings.append(
                                {
                                    "operation": "combat_preflight_attack",
                                    "actor_id": str(actor["id"]),
                                    "target_id": target_id,
                                    "action": action,
                                    "decision": "decline_optional_attack",
                                    "reason": (
                                        "The current two-dimensional temporary map has "
                                        "no vertical-position fact satisfying the "
                                        "source-defined target restriction."
                                    ),
                                    "ruling": plan,
                                }
                            )
                        continue
                    raise EncounterRulingRequiredError(
                        plan,
                        operation="combat_preflight_attack",
                        actor_id=str(actor["id"]),
                        target_id=target_id,
                        action=action,
                        retry_hint=(
                            "Inspect the typed missing facts and retry at the current "
                            "revision. For direct_sunlight, provide "
                            "--source-attack-environment-json with the Agent's ruling."
                        ),
                    )
                return target_id, action, plan
    return None


async def _end_turn(
    client: ExposureClient,
    args: argparse.Namespace,
    branch_id: str,
    actor_id: str,
    sequence: int,
) -> dict[str, Any]:
    campaign = await _campaign(client, args.campaign_id)
    return await client.domain(
        "combat_end_turn",
        {
            "campaign_id": args.campaign_id,
            "actor_id": actor_id,
            "branch_id": branch_id,
            "expected_revision": campaign["revision"],
            "idempotency_key": (
                "encounter-end-turn-"
                + _operation_token(
                    args,
                    sequence,
                    campaign["revision"],
                )
            ),
        },
    )


async def _settle_source_casualty_pool_turn(
    client: ExposureClient,
    args: argparse.Namespace,
    *,
    branch_id: str,
    combat: dict[str, Any],
    declaration: dict[str, Any],
) -> dict[str, Any]:
    """Pay and persist one source-authored non-PC casualty-pool turn."""

    actor_id = str(declaration["actor_id"])
    combat_id = str(combat["id"])
    round_number = int(combat.get("round", 1) or 1)
    event_id = f"{combat_id}:{round_number}:{actor_id}"
    manifest_result = await _manifest_get(client, args.campaign_id)
    manifest = deepcopy(dict(manifest_result["manifest"]))
    world_state = deepcopy(dict(manifest.get("world_state") or {}))
    pools = deepcopy(dict(world_state.get("source_casualty_pools") or {}))
    prior_state = deepcopy(dict(pools.get(declaration["pool_key"]) or {}))
    existing = next(
        (
            item
            for item in prior_state.get("events", [])
            if isinstance(item, dict) and str(item.get("id") or "") == event_id
        ),
        None,
    )
    if existing is not None:
        return {
            "status": "replayed_from_manifest",
            "event": deepcopy(existing),
            "pool": prior_state,
        }

    async def roll_source(expression: str, purpose: str) -> dict[str, Any]:
        campaign = await _campaign(client, args.campaign_id)
        return await client.domain(
            "dnd_dice_roll",
            {
                "campaign_id": args.campaign_id,
                "expression": expression,
                "branch_id": branch_id,
                "expected_campaign_revision": campaign["revision"],
                "idempotency_key": (
                    "encounter-source-casualty-roll-"
                    + _operation_token(
                        args,
                        combat_id,
                        round_number,
                        actor_id,
                        purpose,
                    )
                ),
            },
        )

    attack_count = int(prior_state.get("attacks", 0) or 0)
    recharge_result = None
    recharge_roll = None
    if attack_count > 0:
        recharge_result = await roll_source(declaration["recharge_expression"], "recharge")
        recharge_roll = _roll_total(recharge_result)
    recharged = recharge_roll is None or (
        declaration["recharge_minimum"]
        <= recharge_roll
        <= declaration["recharge_maximum"]
    )

    action_result = None
    kill_result = None
    injury_result = None
    kill_roll = None
    injury_roll = None
    if recharged:
        campaign = await _campaign(client, args.campaign_id)
        action_result = await client.domain(
            "combat_use_activity",
            {
                "campaign_id": args.campaign_id,
                "actor_id": actor_id,
                "activity_id": declaration["activity_id"],
                "declaration": {
                    "kind": "source_casualty_pool_activity",
                    "pool_key": declaration["pool_key"],
                    "source_excerpt": declaration["source_excerpt"],
                },
                "branch_id": branch_id,
                "expected_revision": campaign["revision"],
                "idempotency_key": (
                    "encounter-source-casualty-action-"
                    + _operation_token(
                        args,
                        combat_id,
                        round_number,
                        actor_id,
                    )
                ),
            },
        )
        activity_result = dict(action_result.get("result") or {})
        if (
            action_result.get("status") == "pending_ruling"
            and not activity_result.get("payment")
        ):
            raise EncounterRulingRequiredError(
                action_result,
                operation="combat_use_activity.source_casualty_pool",
                actor_id=actor_id,
                action={
                    "activity_id": declaration["activity_id"],
                    "pool_key": declaration["pool_key"],
                },
                retry_hint=(
                    "Resolve the typed pre-commit ruling and retry before "
                    "rolling source casualty dice."
                ),
            )
        if (
            action_result.get("status") != "pending_ruling"
            or str(activity_result.get("activity_id") or "") != declaration["activity_id"]
        ):
            raise RuntimeError(
                "source casualty activity must pay its reviewed descriptive card "
                "and retain the Agent-as-DM ruling boundary"
            )
        kill_result = await roll_source(declaration["kill_expression"], "killed")
        injury_result = await roll_source(declaration["injury_expression"], "injured")
        kill_roll = _roll_total(kill_result)
        injury_roll = _roll_total(injury_result)

    latest_manifest_result = await _manifest_get(client, args.campaign_id)
    manifest = deepcopy(dict(latest_manifest_result["manifest"]))
    world_state = deepcopy(dict(manifest.get("world_state") or {}))
    pools = deepcopy(dict(world_state.get("source_casualty_pools") or {}))
    latest_prior_state = deepcopy(dict(pools.get(declaration["pool_key"]) or {}))
    if latest_prior_state != prior_state:
        raise RuntimeError("source casualty pool changed concurrently during settlement")
    next_state, event, replayed = _apply_source_casualty_rolls(
        latest_prior_state,
        declaration=declaration,
        combat_id=combat_id,
        round_number=round_number,
        recharge_roll=recharge_roll,
        kill_roll=kill_roll,
        injury_roll=injury_roll,
    )
    if replayed:
        raise RuntimeError("source casualty event appeared concurrently during settlement")
    pools[declaration["pool_key"]] = next_state
    world_state["source_casualty_pools"] = pools
    manifest["world_state"] = world_state
    replaced = await _manifest_mutation(
        client,
        campaign_id=args.campaign_id,
        action="replace",
        run_id=args.run_id,
        identity=f"source-casualty:{event_id}",
        payload={"manifest": manifest},
    )
    return {
        "status": "committed",
        "event": event,
        "pool": next_state,
        "recharge": recharge_result,
        "action": action_result,
        "killed_roll": kill_result,
        "injured_roll": injury_result,
        "manifest": replaced,
    }


async def _auto_run(
    client: ExposureClient,
    args: argparse.Namespace,
    party_ids: list[str],
    hostile_ids: list[str],
) -> dict[str, Any]:
    opened_combat = await client.open(args.campaign_id)
    await client.load(
        "combat.observe",
        "combat.actions",
        "combat.turn",
        "combat.control",
        "combat.save",
        "combat.map",
    )
    campaign = await _campaign(client, args.campaign_id)
    if str(dict(campaign.get("state") or {}).get("game_phase") or "") != "combat":
        raise RuntimeError("auto-run requires an active combat")
    branch = await _current_branch(client, args.campaign_id)
    _validate_source_flee_configuration(
        args,
        hostile_ids=hostile_ids,
    )
    if bool(args.truce_after_defeated) != bool(args.truce_actor_id):
        raise ValueError("source truce requires both --truce-after-defeated and --truce-actor-id")
    if args.truce_after_defeated < 0:
        raise ValueError("--truce-after-defeated must not be negative")
    if args.truce_actor_id and (
        args.truce_actor_id not in hostile_ids or not str(args.truce_source_excerpt or "").strip()
    ):
        raise ValueError(
            "source truce actor must be an encounter hostile and require --truce-source-excerpt"
        )
    knock_out_hostile_ids = {
        str(actor_id).strip() for actor_id in args.knock_out_hostile_id if str(actor_id).strip()
    }
    if len(knock_out_hostile_ids) != len(args.knock_out_hostile_id) or not (
        knock_out_hostile_ids <= set(hostile_ids)
    ):
        raise ValueError("knockout targets must be distinct encounter hostiles")
    opening_casts = _source_opening_casts(
        args.source_opening_cast_json,
        participant_ids=[*party_ids, *hostile_ids],
    )
    opening_weapons = _source_opening_weapons(
        args.source_opening_weapon_json,
        participant_ids=[*party_ids, *hostile_ids],
    )
    on_hit_rulings = _source_on_hit_rulings(
        args.source_on_hit_ruling_json,
        participant_ids=[*party_ids, *hostile_ids],
    )
    _source_traits(
        args.source_trait_json,
        participant_ids=[*party_ids, *hostile_ids],
    )
    delayed_actions = _source_delayed_actions(
        args.source_delayed_action_json,
        participant_ids=hostile_ids,
    )
    source_target_priorities = _source_target_priorities(
        args.source_target_priority_json,
        participant_ids=[*party_ids, *hostile_ids],
        encounter_source_excerpt=str(args.source_excerpt or ""),
    )
    agent_target_priorities = _agent_target_priorities(
        getattr(args, "agent_target_priority_json", []),
        party_ids=party_ids,
        hostile_ids=hostile_ids,
    )
    if set(source_target_priorities) & set(agent_target_priorities):
        raise ValueError(
            "the same actor cannot have both source-authored and Agent tactical "
            "target priorities"
        )
    target_priorities = {**source_target_priorities, **agent_target_priorities}
    ally_ids = _selected_prepared_actor_ids(
        args.ally_report,
        getattr(args, "ally_actor_id", []),
        report_kind="ally",
    )
    passive_allies = _source_passive_allies(
        args.source_passive_ally_json,
        ally_ids=ally_ids,
    )
    source_zero_hp_finisher = _source_zero_hp_finisher(
        args.source_zero_hp_finisher_json,
        participant_ids=[*party_ids, *hostile_ids],
        encounter_source_excerpt=str(args.source_excerpt or ""),
    )
    if source_zero_hp_finisher is not None and set(source_zero_hp_finisher["actor_ids"]) & set(
        ally_ids
    ):
        raise ValueError("source zero-HP finisher actor_ids must be PCs, not allied NPCs")
    source_zero_hp_stabilization = _source_zero_hp_stabilization(
        args.source_zero_hp_stabilization_json,
        participant_ids=[actor_id for actor_id in party_ids if actor_id not in set(ally_ids)],
    )
    surrender_configured = bool(
        args.surrender_actor_id
        or args.surrender_at_hp
        or args.surrender_after_defeated
        or args.surrender_source_excerpt
        or args.surrender_no_escape
    )
    if args.surrender_at_hp < 0 or args.surrender_after_defeated < 0:
        raise ValueError("source surrender thresholds must not be negative")
    if surrender_configured and (
        args.surrender_actor_id not in hostile_ids
        or bool(args.surrender_at_hp) == bool(args.surrender_after_defeated)
        or not str(args.surrender_source_excerpt or "").strip()
        or not args.surrender_no_escape
    ):
        raise ValueError(
            "source surrender requires a hostile actor, exactly one positive HP or "
            "defeated-hostile threshold, an exact source excerpt, and "
            "--surrender-no-escape"
        )
    initial_combat = await client.domain(
        "combat_query",
        {"campaign_id": args.campaign_id, "view": "status"},
    )
    args.operation_scope = _encounter_operation_scope(
        args,
        branch_id=str(branch["id"]),
        combat_id=str(initial_combat["id"]),
        party_ids=party_ids,
        hostile_ids=hostile_ids,
    )
    initial_actors = await _characters(
        client,
        args.campaign_id,
        [*party_ids, *hostile_ids],
    )
    source_casualty_pools = _source_casualty_pools(
        args.source_casualty_pool_json,
        hostile_ids=hostile_ids,
        actors=initial_actors,
        encounter_source_excerpt=str(args.source_excerpt or ""),
    )
    source_separations = _source_separations(
        args.source_separation_json,
        participant_ids=[*party_ids, *hostile_ids],
        hostile_ids=hostile_ids,
        encounter_source_excerpt=str(args.source_excerpt or ""),
    )
    random_activities = _source_random_activities(
        args.source_random_activity_json,
        participant_ids=[*party_ids, *hostile_ids],
        actors=initial_actors,
    )
    save_activities = _source_save_activities(
        args.source_save_activity_json,
        participant_ids=[*party_ids, *hostile_ids],
        actors=initial_actors,
    )
    contest_activities = _source_contest_activities(
        args.source_contest_activity_json,
        participant_ids=[*party_ids, *hostile_ids],
        actors=initial_actors,
    )
    attack_environments = _source_attack_environments(
        args.source_attack_environment_json,
        participant_ids=[*party_ids, *hostile_ids],
        actors=initial_actors,
    )
    avoided_cells_by_actor, source_avoidance_evidence = _source_avoidances(
        args.source_avoidance_report,
        campaign_id=args.campaign_id,
        scene_id=args.scene_id,
        participant_ids=[*party_ids, *hostile_ids],
    )
    revealed_surprised = [
        str(item["actor_id"])
        for item in initial_combat.get("combatants", [])
        if item.get("actor_id") in hostile_ids and item.get("surprised") and item.get("hidden")
    ]
    visibility_patch = None
    if revealed_surprised:
        campaign = await _campaign(client, args.campaign_id)
        visibility_patch = await client.domain(
            "combat_map_patch",
            {
                "campaign_id": args.campaign_id,
                "patches": [
                    {
                        "key": "combatant_visibility",
                        "value": {
                            "actor_id": actor_id,
                            "hidden": False,
                            "reason": (
                                "The source-cited successful scout check surprised this "
                                "lookout, so the party located it before initiative."
                            ),
                        },
                    }
                    for actor_id in revealed_surprised
                ],
                "branch_id": branch["id"],
                "expected_revision": campaign["revision"],
                "idempotency_key": (f"encounter-reveal-surprised-{_operation_token(args)}"),
            },
    )
    turns: list[dict[str, Any]] = []
    agent_preflight_rulings: list[dict[str, Any]] = []
    completed_opening_casts: set[int] = set()
    completed_opening_weapon_actor_ids: set[str] = set()
    fled_hostile_ids: set[str] = set()
    damage_taken_by_flee_actor, critical_hit_flee_actor_ids = _source_flee_damage_history(
        initial_combat,
        flee_actor_ids=set(args.flee_actor_id),
    )
    if args.flee_on_start_actor_id:
        campaign = await _campaign(client, args.campaign_id)
        escaped = await client.domain(
            "combat_map_patch",
            {
                "campaign_id": args.campaign_id,
                "patches": [
                    _source_departure_patch(
                        args.flee_on_start_actor_id,
                        reason=str(args.flee_source_excerpt),
                        destination_location_key=args.flee_destination_location_key,
                    )
                ],
                "branch_id": branch["id"],
                "expected_revision": campaign["revision"],
                "idempotency_key": (
                    "encounter-source-start-flee-"
                    f"{_operation_token(args, args.flee_on_start_actor_id)}"
                ),
            },
        )
        fled_hostile_ids.add(args.flee_on_start_actor_id)
        turns.append(
            {
                "sequence": 0,
                "kind": "source_flee",
                "actor_id": args.flee_on_start_actor_id,
                "trigger": "combat_start",
                "source_excerpt": str(args.flee_source_excerpt).strip(),
                "destination_location_key": args.flee_destination_location_key,
                "map_patch": escaped,
            }
        )
    outcome_status = ""
    outcome_summary = ""
    for sequence in range(1, args.max_turns + 1):
        combat = await client.domain(
            "combat_query",
            {"campaign_id": args.campaign_id, "view": "status"},
        )
        actors = await _characters(
            client,
            args.campaign_id,
            [*party_ids, *hostile_ids],
        )
        combatants_by_actor = {
            str(item.get("actor_id") or ""): item
            for item in combat.get("combatants", [])
            if isinstance(item, dict)
        }
        body_thief_sides = _body_thief_sides(
            combat,
            party_ids=party_ids,
            hostile_ids=hostile_ids,
        )
        effective_party_ids = list(body_thief_sides["effective_party_ids"])
        attackable_hostile_ids = list(body_thief_sides["attackable_hostile_ids"])
        hostile_turn_actor_ids = set(body_thief_sides["hostile_turn_actor_ids"])
        defeated_hostiles = [
            actor_id
            for actor_id in hostile_ids
            if "dead" in _conditions(actors[actor_id])
            or (
                _hit_points(actors[actor_id]) <= 0
                and not bool(
                    dict(combatants_by_actor.get(actor_id) or {}).get("zero_hp_recovery", False)
                )
            )
        ]
        unresolved_party = [
            actor_id
            for actor_id in effective_party_ids
            if _hit_points(actors[actor_id]) == 0
            and not _conditions(actors[actor_id]) & {"dead", "stable"}
        ]
        party_down = all(_hit_points(actors[actor_id]) <= 0 for actor_id in effective_party_ids)
        outcome = (
            _source_surrender_outcome(
                actor_hit_points=_hit_points(actors[args.surrender_actor_id]),
                surrender_at_hp=args.surrender_at_hp,
                defeated_hostiles=len(defeated_hostiles),
                surrender_after_defeated=args.surrender_after_defeated,
                actor_alive=("dead" not in _conditions(actors[args.surrender_actor_id])),
                no_escape=args.surrender_no_escape,
                unresolved_party=bool(unresolved_party),
            )
            if surrender_configured
            else None
        )
        if outcome is None:
            outcome = _source_truce_outcome(
                defeated_hostiles=len(defeated_hostiles),
                truce_after_defeated=args.truce_after_defeated,
                truce_actor_alive=bool(
                    args.truce_actor_id
                    and _hit_points(actors[args.truce_actor_id]) > 0
                    and "dead" not in _conditions(actors[args.truce_actor_id])
                ),
                unresolved_party=bool(unresolved_party),
            )
        if outcome is None:
            outcome = _source_outcome(
                defeated_hostiles=len(defeated_hostiles),
                fled_hostiles=len(fled_hostile_ids),
                hostile_count=len(hostile_ids),
                unresolved_party=bool(unresolved_party),
                party_down=party_down,
            )
        if outcome is not None:
            outcome_status, outcome_summary = outcome
            break
        pending_result = await _resolve_pending(
            client,
            args,
            str(branch["id"]),
            combat,
        )
        if pending_result is not None:
            source_flee_observations = _record_source_flee_damage(
                pending_result,
                flee_actor_ids=set(args.flee_actor_id),
                damage_taken_by_actor=damage_taken_by_flee_actor,
                critical_hit_actor_ids=critical_hit_flee_actor_ids,
            )
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "pending_resolution",
                    "result": pending_result,
                    "source_flee_observations": source_flee_observations,
                }
            )
            continue
        stabilization_target_id = (
            next(
                (
                    actor_id
                    for actor_id in source_zero_hp_stabilization["actor_ids"]
                    if _hit_points(actors[actor_id]) == 0
                    and not _conditions(actors[actor_id]) & {"dead", "stable"}
                ),
                None,
            )
            if source_zero_hp_stabilization is not None
            else None
        )
        if stabilization_target_id is not None:
            campaign = await _campaign(client, args.campaign_id)
            stabilized = await client.domain(
                "combat_hp_change",
                {
                    "campaign_id": args.campaign_id,
                    "target_id": stabilization_target_id,
                    "action": "stabilize",
                    "payload": {"source_excerpt": source_zero_hp_stabilization["source_excerpt"]},
                    "branch_id": branch["id"],
                    "expected_revision": campaign["revision"],
                    "idempotency_key": (
                        "encounter-source-stabilize-"
                        + _operation_token(args, stabilization_target_id)
                    ),
                },
            )
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "source_zero_hp_stabilization",
                    "target_id": stabilization_target_id,
                    "source_excerpt": source_zero_hp_stabilization["source_excerpt"],
                    "result": stabilized,
                }
            )
            continue
        actor_id = _current_actor_id(combat)
        actor = actors[actor_id]
        actor_conditions = _conditions(actor)
        if (
            _source_flee_ready(
                acting_actor_id=actor_id,
                flee_actor_ids=set(args.flee_actor_id),
                defeated_hostile_ids=defeated_hostiles,
                flee_after_defeated=args.flee_after_defeated,
                trigger_defeated_actor_id=str(args.flee_trigger_defeated_actor_id or ""),
                damage_taken_by_actor=damage_taken_by_flee_actor,
                flee_after_damage=args.flee_after_damage,
                critical_hit_actor_ids=critical_hit_flee_actor_ids,
                flee_on_critical=args.flee_on_critical,
            )
            and _hit_points(actor) > 0
            and actor_id not in fled_hostile_ids
        ):
            campaign = await _campaign(client, args.campaign_id)
            escaped = await client.domain(
                "combat_map_patch",
                {
                    "campaign_id": args.campaign_id,
                    "patches": [
                        {
                            **_source_departure_patch(
                                actor_id,
                                reason=str(args.flee_source_excerpt),
                                destination_location_key=(args.flee_destination_location_key),
                            ),
                        }
                    ],
                    "branch_id": branch["id"],
                    "expected_revision": campaign["revision"],
                    "idempotency_key": (
                        f"encounter-source-flee-{_operation_token(args, actor_id)}"
                    ),
                },
            )
            fled_hostile_ids.add(actor_id)
            ended_turn = await _end_turn(
                client,
                args,
                str(branch["id"]),
                actor_id,
                sequence,
            )
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "source_flee",
                    "actor_id": actor_id,
                    "trigger_actor_id": (args.flee_trigger_defeated_actor_id or None),
                    "trigger_defeated_count": (args.flee_after_defeated or None),
                    "trigger_damage_taken": (
                        damage_taken_by_flee_actor.get(actor_id)
                        if args.flee_after_damage
                        else None
                    ),
                    "trigger_damage_threshold": (args.flee_after_damage or None),
                    "trigger_critical_hit": (
                        actor_id in critical_hit_flee_actor_ids
                        if args.flee_on_critical
                        else None
                    ),
                    "source_excerpt": str(args.flee_source_excerpt).strip(),
                    "map_patch": escaped,
                    "end_turn": ended_turn,
                }
            )
            continue
        source_casualty_pool = source_casualty_pools.get(actor_id)
        if (
            source_casualty_pool is not None
            and _hit_points(actor) > 0
            and not actor_conditions
            & {"dead", "unconscious", "stunned", "incapacitated", "paralyzed", "petrified"}
        ):
            settled_pool = await _settle_source_casualty_pool_turn(
                client,
                args,
                branch_id=str(branch["id"]),
                combat=combat,
                declaration=source_casualty_pool,
            )
            ended_turn = await _end_turn(
                client,
                args,
                str(branch["id"]),
                actor_id,
                sequence,
            )
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "source_casualty_pool",
                    "actor_id": actor_id,
                    "source_excerpt": source_casualty_pool["source_excerpt"],
                    "result": settled_pool,
                    "end_turn": ended_turn,
                }
            )
            continue
        delayed = delayed_actions.get(actor_id)
        round_number = int(combat.get("round", 1) or 1)
        if (
            delayed is not None
            and round_number < int(delayed["until_round"])
            and _hit_points(actor) > 0
        ):
            ended_turn = await _end_turn(
                client,
                args,
                str(branch["id"]),
                actor_id,
                sequence,
            )
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "source_delayed_action",
                    "actor_id": actor_id,
                    "round": round_number,
                    "until_round": delayed["until_round"],
                    "source_excerpt": delayed["source_excerpt"],
                    "result": ended_turn,
                }
            )
            continue
        passive_ally = passive_allies.get(actor_id)
        if passive_ally is not None and _hit_points(actor) > 0:
            ended_turn = await _end_turn(
                client,
                args,
                str(branch["id"]),
                actor_id,
                sequence,
            )
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "source_passive_ally",
                    "actor_id": actor_id,
                    "round": round_number,
                    "source_excerpt": passive_ally["source_excerpt"],
                    "result": ended_turn,
                }
            )
            continue
        if (
            _hit_points(actor) == 0
            and actor_id in effective_party_ids
            and not actor_conditions
            & {
                "dead",
                "stable",
            }
        ):
            campaign = await _campaign(client, args.campaign_id)
            saved = await client.domain(
                "combat_check",
                {
                    "campaign_id": args.campaign_id,
                    "actor_id": actor_id,
                    "kind": "death_save",
                    "branch_id": branch["id"],
                    "expected_revision": campaign["revision"],
                    "idempotency_key": (
                        "encounter-death-save-"
                        + _operation_token(
                            args,
                            sequence,
                            campaign["revision"],
                        )
                    ),
                },
            )
            turns.append({"sequence": sequence, "kind": "death_save", "result": saved})
            await _end_turn(client, args, str(branch["id"]), actor_id, sequence)
            continue
        available = await client.domain(
            "combat_query",
            {
                "campaign_id": args.campaign_id,
                "view": "available_actions",
                "actor_id": actor_id,
            },
        )
        available_actions = set(available.get("actions") or [])
        if actor_id in body_thief_sides["inside_sources"]:
            ended_turn = await _end_turn(
                client,
                args,
                str(branch["id"]),
                actor_id,
                sequence,
            )
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "body_thief_source_inside_host",
                    "actor_id": actor_id,
                    "host_actor_id": dict(
                        combatants_by_actor[actor_id].get("inside_host") or {}
                    ).get("host_actor_id"),
                    "result": ended_turn,
                }
            )
            continue
        if _should_stand(actor, available_actions):
            campaign = await _campaign(client, args.campaign_id)
            stood = await client.domain(
                "combat_movement",
                {
                    "campaign_id": args.campaign_id,
                    "actor_id": actor_id,
                    "action": "stand",
                    "branch_id": branch["id"],
                    "expected_revision": campaign["revision"],
                    "idempotency_key": (
                        f"encounter-stand-{_operation_token(args, sequence, actor_id)}"
                    ),
                },
            )
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "stand",
                    "actor_id": actor_id,
                    "result": stood,
                }
            )
            continue
        contest_activity = contest_activities.get(actor_id)
        if (
            contest_activity is not None
            and _hit_points(actor) > 0
            and actor_id not in body_thief_sides["inside_sources"]
            and _has_action_budget(combat, actor_id)
        ):
            combatants = {str(item["actor_id"]): item for item in combat["combatants"]}
            activity_card = next(
                item
                for item in dict(actor.get("sheet") or {}).get("content", {}).get("activities", [])
                if str(item.get("id") or "") == contest_activity["activity_id"]
            )
            contest_range_ft = int(
                dict(activity_card.get("choices") or {})
                .get("source_contest_effect", {})
                .get("range_ft", 0)
                or 0
            )
            eligible_targets = _body_thief_target_ids(
                combat,
                actors=actors,
                source_actor_id=actor_id,
                party_ids=effective_party_ids,
                range_ft=contest_range_ft,
            )
            eligible_targets = _prioritize_targets(
                actor_id,
                eligible_targets,
                target_priorities,
            )
            if eligible_targets:
                target_id = eligible_targets[0]
                campaign = await _campaign(client, args.campaign_id)
                settled_activity = await client.domain(
                    "combat_use_activity",
                    {
                        "campaign_id": args.campaign_id,
                        "actor_id": actor_id,
                        "activity_id": contest_activity["activity_id"],
                        "declaration": {
                            "target_id": target_id,
                            "target_is_humanoid": contest_activity["target_is_humanoid"],
                        },
                        "branch_id": branch["id"],
                        "expected_revision": campaign["revision"],
                        "idempotency_key": (
                            "encounter-source-contest-activity-"
                            + _operation_token(
                                args,
                                sequence,
                                actor_id,
                                contest_activity["activity_id"],
                            )
                        ),
                    },
                )
                if settled_activity.get("status") != "committed":
                    raise RuntimeError(
                        "source ability-contest activity did not commit "
                        "through structured settlement"
                    )
                core_effect = dict(
                    dict(settled_activity.get("result") or {}).get("core_effect") or {}
                )
                if core_effect.get("success") and (
                    core_effect.get("knowledge_transfer") != "all_target_knowledge"
                    or int(core_effect.get("knowledge_transfer_count", -1)) < 0
                ):
                    raise RuntimeError(
                        "Body Thief did not attest its complete ActorKnowledge transfer"
                    )
                turn_entry = {
                    "sequence": sequence,
                    "kind": "source_contest_activity",
                    "actor_id": actor_id,
                    "activity_id": contest_activity["activity_id"],
                    "target_id": target_id,
                    "source_excerpt": contest_activity["source_excerpt"],
                    "result": settled_activity,
                }
                if _has_blocking_pending(dict(settled_activity.get("combat") or {})):
                    turns.append(turn_entry)
                    continue
                turn_entry["end_turn"] = await _end_turn(
                    client,
                    args,
                    str(branch["id"]),
                    actor_id,
                    sequence,
                )
                turns.append(turn_entry)
                continue
        save_activity = save_activities.get(actor_id)
        if save_activity is not None and _hit_points(actor) > 0:
            combatant = next(
                item
                for item in combat.get("combatants", [])
                if str(item.get("actor_id") or "") == actor_id
            )
            active_multiattack = bool(dict(combatant.get("turn_flags") or {}).get("multiattack"))
            mixed_options = [
                option
                for option in dict(actor.get("derived") or {}).get("multiattack_options", [])
                if any(
                    str(item.get("activity_id") or "") == save_activity["activity_id"]
                    for item in option.get("activities", [])
                    if isinstance(item, dict)
                )
            ]
            if active_multiattack or not mixed_options:
                opponents = (
                    [
                        hostile_id
                        for hostile_id in attackable_hostile_ids
                        if hostile_id not in fled_hostile_ids
                    ]
                    if actor_id in effective_party_ids
                    else effective_party_ids
                )
                living_targets = [
                    target_id
                    for target_id in opponents
                    if _hit_points(actors[target_id]) > 0
                    and int(
                        dict(actors[target_id].get("derived") or {})
                        .get("ability_scores", {})
                        .get("intelligence", 10)
                    )
                    > 0
                ]
                living_targets = _observable_target_ids(
                    combat,
                    observer_id=actor_id,
                    target_ids=living_targets,
                )
                combatants = {str(item["actor_id"]): item for item in combat["combatants"]}
                activity_card = next(
                    item
                    for item in dict(actor.get("sheet") or {})
                    .get("content", {})
                    .get("activities", [])
                    if str(item.get("id") or "") == save_activity["activity_id"]
                )
                save_range_ft = int(
                    dict(activity_card.get("choices") or {})
                    .get("source_save_effect", {})
                    .get("range_ft", 0)
                    or 0
                )
                living_targets = [
                    target_id
                    for target_id in living_targets
                    if _distance(
                        dict(combatants[actor_id].get("position") or {"x": 0, "y": 0}),
                        dict(combatants[target_id].get("position") or {"x": 0, "y": 0}),
                    )
                    * 5
                    <= save_range_ft
                ]
                living_targets.sort(
                    key=lambda target_id: _distance(
                        dict(combatants[actor_id].get("position") or {"x": 0, "y": 0}),
                        dict(combatants[target_id].get("position") or {"x": 0, "y": 0}),
                    )
                )
                living_targets = _prioritize_targets(
                    actor_id,
                    living_targets,
                    target_priorities,
                )
                if not living_targets:
                    ended_turn = await _end_turn(
                        client,
                        args,
                        str(branch["id"]),
                        actor_id,
                        sequence,
                    )
                    turns.append(
                        {
                            "sequence": sequence,
                            "kind": "source_save_activity_no_target",
                            "actor_id": actor_id,
                            "source_excerpt": save_activity["source_excerpt"],
                            "result": ended_turn,
                        }
                    )
                    continue
                campaign = await _campaign(client, args.campaign_id)
                settled_activity = await client.domain(
                    "combat_use_activity",
                    {
                        "campaign_id": args.campaign_id,
                        "actor_id": actor_id,
                        "activity_id": save_activity["activity_id"],
                        "declaration": {
                            "target_id": living_targets[0],
                            "target_has_brain": save_activity["target_has_brain"],
                        },
                        "branch_id": branch["id"],
                        "expected_revision": campaign["revision"],
                        "idempotency_key": (
                            "encounter-source-save-activity-"
                            + _operation_token(
                                args,
                                sequence,
                                actor_id,
                                save_activity["activity_id"],
                            )
                        ),
                    },
                )
                if settled_activity.get("status") != "committed":
                    raise RuntimeError(
                        "source saving-throw activity did not commit through structured settlement"
                    )
                turn_entry = {
                    "sequence": sequence,
                    "kind": "source_save_activity",
                    "actor_id": actor_id,
                    "activity_id": save_activity["activity_id"],
                    "target_id": living_targets[0],
                    "source_excerpt": save_activity["source_excerpt"],
                    "result": settled_activity,
                }
                if _has_blocking_pending(dict(settled_activity.get("combat") or {})):
                    turns.append(turn_entry)
                    continue
                turn_entry["end_turn"] = await _end_turn(
                    client,
                    args,
                    str(branch["id"]),
                    actor_id,
                    sequence,
                )
                turns.append(turn_entry)
                continue
        random_activity = random_activities.get(actor_id)
        if random_activity is not None and _hit_points(actor) > 0:
            opponents = (
                [
                    hostile_id
                    for hostile_id in attackable_hostile_ids
                    if hostile_id not in fled_hostile_ids
                ]
                if actor_id in effective_party_ids
                else effective_party_ids
            )
            living_targets = [
                target_id for target_id in opponents if _hit_points(actors[target_id]) > 0
            ]
            living_targets = _observable_target_ids(
                combat,
                observer_id=actor_id,
                target_ids=living_targets,
            )
            combatants = {str(item["actor_id"]): item for item in combat["combatants"]}
            living_targets.sort(
                key=lambda target_id: _distance(
                    dict(combatants[actor_id].get("position") or {"x": 0, "y": 0}),
                    dict(combatants[target_id].get("position") or {"x": 0, "y": 0}),
                )
            )
            living_targets = _prioritize_targets(
                actor_id,
                living_targets,
                target_priorities,
            )[:2]
            if not living_targets:
                ended_turn = await _end_turn(
                    client,
                    args,
                    str(branch["id"]),
                    actor_id,
                    sequence,
                )
                turns.append(
                    {
                        "sequence": sequence,
                        "kind": "source_random_activity_no_target",
                        "actor_id": actor_id,
                        "source_excerpt": random_activity["source_excerpt"],
                        "result": ended_turn,
                    }
                )
                continue
            campaign = await _campaign(client, args.campaign_id)
            settled_activity = await client.domain(
                "combat_use_activity",
                {
                    "campaign_id": args.campaign_id,
                    "actor_id": actor_id,
                    "activity_id": random_activity["activity_id"],
                    "declaration": {"target_ids": living_targets},
                    "branch_id": branch["id"],
                    "expected_revision": campaign["revision"],
                    "idempotency_key": (
                        "encounter-random-activity-"
                        + _operation_token(
                            args,
                            sequence,
                            actor_id,
                            random_activity["activity_id"],
                        )
                    ),
                },
            )
            if settled_activity.get("status") != "committed":
                raise RuntimeError(
                    "source random activity did not commit through structured settlement"
                )
            turn_entry = {
                "sequence": sequence,
                "kind": "source_random_activity",
                "actor_id": actor_id,
                "activity_id": random_activity["activity_id"],
                "target_ids": living_targets,
                "source_excerpt": random_activity["source_excerpt"],
                "result": settled_activity,
            }
            if _has_blocking_pending(dict(settled_activity.get("combat") or {})):
                turns.append(turn_entry)
                continue
            turn_entry["end_turn"] = await _end_turn(
                client,
                args,
                str(branch["id"]),
                actor_id,
                sequence,
            )
            turns.append(turn_entry)
            continue
        stabilization_target_id = _postcombat_stabilization_target(
            actor_id=actor_id,
            party_ids=effective_party_ids,
            actors=actors,
            defeated_hostiles=len(defeated_hostiles),
            fled_hostiles=len(fled_hostile_ids),
            hostile_count=len(hostile_ids),
        )
        if stabilization_target_id is not None:
            combatants = {
                str(item.get("actor_id") or ""): item
                for item in combat.get("combatants", [])
                if isinstance(item, dict)
            }
            actor_position = dict(combatants[actor_id].get("position") or {})
            target_position = dict(combatants[stabilization_target_id].get("position") or {})
            distance_ft = (
                _distance(actor_position, target_position) * 5
                if set(actor_position) == {"x", "y"} and set(target_position) == {"x", "y"}
                else 0
            )
            moved = None
            if distance_ft > 5:
                destination = _choose_destination(
                    combat,
                    actor_id,
                    stabilization_target_id,
                    avoided_cells=avoided_cells_by_actor.get(actor_id, set()),
                )
                if destination is None:
                    await _end_turn(
                        client,
                        args,
                        str(branch["id"]),
                        actor_id,
                        sequence,
                    )
                    continue
                campaign = await _campaign(client, args.campaign_id)
                moved = await client.domain(
                    "combat_movement",
                    {
                        "campaign_id": args.campaign_id,
                        "actor_id": actor_id,
                        "action": "move",
                        "payload": {
                            "distance": destination[1],
                            "destination": destination[0],
                            "path": destination[2],
                        },
                        "branch_id": branch["id"],
                        "expected_revision": campaign["revision"],
                        "idempotency_key": (
                            "encounter-stabilize-move-" + _operation_token(args, sequence, actor_id)
                        ),
                    },
                )
                if _has_blocking_pending(dict(moved.get("combat") or {})):
                    turns.append(
                        {
                            "sequence": sequence,
                            "kind": "stabilize_move",
                            "actor_id": actor_id,
                            "target_id": stabilization_target_id,
                            "planned_path": destination[2],
                            "avoided_cells": sorted(avoided_cells_by_actor.get(actor_id, set())),
                            "result": moved,
                        }
                    )
                    continue
            campaign = await _campaign(client, args.campaign_id)
            stabilized = await client.domain(
                "combat_check",
                {
                    "campaign_id": args.campaign_id,
                    "actor_id": actor_id,
                    "target_id": stabilization_target_id,
                    "kind": "stabilize",
                    "ability": "wisdom",
                    "branch_id": branch["id"],
                    "expected_revision": campaign["revision"],
                    "idempotency_key": (
                        "encounter-stabilize-"
                        + _operation_token(
                            args,
                            sequence,
                            actor_id,
                            stabilization_target_id,
                        )
                    ),
                },
            )
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "stabilize",
                    "actor_id": actor_id,
                    "target_id": stabilization_target_id,
                    "move": moved,
                    "result": stabilized,
                }
            )
            await _end_turn(
                client,
                args,
                str(branch["id"]),
                actor_id,
                sequence,
            )
            continue
        escape_effect = next(
            (
                item
                for item in combat.get("ongoing_effects", [])
                if isinstance(item, dict)
                and item.get("active", True)
                and str(item.get("target_id") or "") == actor_id
                and str(item.get("condition") or "").casefold() in actor_conditions
                and isinstance(item.get("escape_abilities"), list)
                and bool(item.get("escape_abilities"))
                and isinstance(item.get("escape_dc"), int)
                and not isinstance(item.get("escape_dc"), bool)
            ),
            None,
        )
        if escape_effect is not None and "escape" in available_actions:
            ability = str(list(escape_effect["escape_abilities"])[0])
            dc = int(escape_effect["escape_dc"])
            campaign = await _campaign(client, args.campaign_id)
            escaped = await client.domain(
                "combat_check",
                {
                    "campaign_id": args.campaign_id,
                    "actor_id": actor_id,
                    "kind": "ability",
                    "ability": ability,
                    "dc": dc,
                    "action": "escape",
                    "rule_facts": {
                        "ongoing_effect_id": str(escape_effect["id"]),
                    },
                    "branch_id": branch["id"],
                    "expected_revision": campaign["revision"],
                    "idempotency_key": (
                        "encounter-effect-escape-"
                        + _operation_token(
                            args,
                            sequence,
                            actor_id,
                            escape_effect["id"],
                        )
                    ),
                },
            )
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "effect_escape",
                    "actor_id": actor_id,
                    "ongoing_effect_id": escape_effect["id"],
                    "condition": escape_effect["condition"],
                    "ability": ability,
                    "dc": dc,
                    "result": escaped,
                }
            )
            await _end_turn(
                client,
                args,
                str(branch["id"]),
                actor_id,
                sequence,
            )
            continue
        if source_zero_hp_finisher is not None:
            finisher_target_id = str(source_zero_hp_finisher["target_id"])
            other_hostiles_defeated = all(
                hostile_id == finisher_target_id
                or hostile_id in defeated_hostiles
                or hostile_id in fled_hostile_ids
                for hostile_id in hostile_ids
            )
            finisher_target = actors[finisher_target_id]
            if (
                actor_id in source_zero_hp_finisher["actor_ids"]
                and _hit_points(finisher_target) == 0
                and "dead" not in _conditions(finisher_target)
                and other_hostiles_defeated
                and "use_object" in available_actions
            ):
                stage, prior_event = _source_zero_hp_finisher_stage(
                    combat,
                    source_zero_hp_finisher,
                )
                if stage is not None:
                    round_number = int(combat.get("round", 1) or 1)
                    campaign = await _campaign(client, args.campaign_id)
                    source_action = await client.domain(
                        "combat_common_action",
                        {
                            "campaign_id": args.campaign_id,
                            "actor_id": actor_id,
                            "action": "use_object",
                            "target_id": finisher_target_id,
                            "payload": {
                                "source_finisher_id": source_zero_hp_finisher["id"],
                                "stage": stage,
                                "round": round_number,
                                "object": ("lamp oil" if stage == "douse" else "burning lamp oil"),
                                "source_excerpt": source_zero_hp_finisher["source_excerpt"],
                                "oil_rule_excerpt": source_zero_hp_finisher["oil_rule_excerpt"],
                            },
                            "branch_id": branch["id"],
                            "expected_revision": campaign["revision"],
                            "idempotency_key": (
                                "encounter-source-finisher-action-"
                                + _operation_token(
                                    args,
                                    stage,
                                    round_number,
                                    actor_id,
                                    finisher_target_id,
                                )
                            ),
                        },
                    )
                    fire_damage = None
                    if stage == "ignite":
                        campaign = await _campaign(client, args.campaign_id)
                        fire_damage = await client.domain(
                            "combat_hp_change",
                            {
                                "campaign_id": args.campaign_id,
                                "target_id": finisher_target_id,
                                "action": "damage",
                                "payload": {
                                    "parts": [
                                        {
                                            "amount": int(source_zero_hp_finisher["fire_damage"]),
                                            "damage_type": "fire",
                                            "source": "burning_oil",
                                        }
                                    ]
                                },
                                "branch_id": branch["id"],
                                "expected_revision": campaign["revision"],
                                "idempotency_key": (
                                    "encounter-source-finisher-fire-"
                                    + _operation_token(
                                        args,
                                        round_number,
                                        actor_id,
                                        finisher_target_id,
                                    )
                                ),
                            },
                        )
                    ended_turn = await _end_turn(
                        client,
                        args,
                        str(branch["id"]),
                        actor_id,
                        sequence,
                    )
                    turns.append(
                        {
                            "sequence": sequence,
                            "kind": f"source_zero_hp_finisher_{stage}",
                            "actor_id": actor_id,
                            "target_id": finisher_target_id,
                            "round": round_number,
                            "source_excerpt": source_zero_hp_finisher["source_excerpt"],
                            "oil_rule_excerpt": source_zero_hp_finisher["oil_rule_excerpt"],
                            "prior_event": prior_event,
                            "action": source_action,
                            "fire_damage": fire_damage,
                            "end_turn": ended_turn,
                        }
                    )
                    continue
        opening_cast = next(
            (
                item
                for item in opening_casts
                if int(item["sequence"]) not in completed_opening_casts
                and item["actor_id"] == actor_id
            ),
            None,
        )
        if opening_cast is not None and "cast" in available_actions:
            campaign = await _campaign(client, args.campaign_id)
            cast_arguments: dict[str, Any] = {
                "campaign_id": args.campaign_id,
                "actor_id": actor_id,
                "spell_id": opening_cast["spell_id"],
                "source_item_id": opening_cast["source_item_id"],
                "branch_id": branch["id"],
                "expected_revision": campaign["revision"],
                "idempotency_key": (
                    "encounter-source-opening-cast-"
                    + _operation_token(
                        args,
                        opening_cast["sequence"],
                        actor_id,
                        opening_cast["spell_id"],
                    )
                ),
            }
            if opening_cast["declaration"]:
                cast_arguments["declaration"] = opening_cast["declaration"]
            cast = await client.domain("combat_cast_spell", cast_arguments)
            if cast.get("status") != "committed":
                raise RuntimeError(
                    "source opening item spell did not commit through structured settlement"
                )
            completed_opening_casts.add(int(opening_cast["sequence"]))
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "source_opening_item_spell",
                    "actor_id": actor_id,
                    "spell_id": opening_cast["spell_id"],
                    "source_item_id": opening_cast["source_item_id"],
                    "source_excerpt": opening_cast["source_excerpt"],
                    "result": cast,
                }
            )
            await _end_turn(client, args, str(branch["id"]), actor_id, sequence)
            continue
        if (
            actor_id in fled_hostile_ids
            or party_down
            or _hit_points(actor) <= 0
            or "attack" not in available_actions
        ):
            ended_turn = await _end_turn(
                client,
                args,
                str(branch["id"]),
                actor_id,
                sequence,
            )
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "end_turn",
                    "actor_id": actor_id,
                    "result": ended_turn,
                }
            )
            continue
        opponents = (
            [
                hostile_id
                for hostile_id in attackable_hostile_ids
                if hostile_id not in fled_hostile_ids
            ]
            if actor_id in effective_party_ids
            else effective_party_ids
        )
        living_targets = [
            target_id for target_id in opponents if _hit_points(actors[target_id]) > 0
        ]
        combatants = {str(item["actor_id"]): item for item in combat["combatants"]}
        if actor_id in effective_party_ids:
            living_targets = _observable_target_ids(
                combat,
                observer_id=actor_id,
                target_ids=living_targets,
            )
        living_targets.sort(
            key=lambda item: (
                *(
                    _wound_priority(actors[item])
                    if actor_id in effective_party_ids
                    else (False, 0.0)
                ),
                _distance(
                    dict(combatants[actor_id].get("position") or {"x": 0, "y": 0}),
                    dict(combatants[item].get("position") or {"x": 0, "y": 0}),
                ),
            )
        )
        living_targets = _prioritize_targets(
            actor_id,
            living_targets,
            target_priorities,
        )
        if actor_id in effective_party_ids and knock_out_hostile_ids:
            living_targets.sort(key=lambda target_id: target_id in knock_out_hostile_ids)
        spell_targets = [
            target_id for target_id in living_targets if target_id not in knock_out_hostile_ids
        ]
        spell_choice = _choose_party_spell(
            actor_id,
            party_ids=effective_party_ids,
            actors=actors,
            living_targets=spell_targets,
            leveled_spell_available=not bool(
                dict(combatants[actor_id].get("turn_flags") or {}).get("cast_declared")
            ),
        )
        if spell_choice is not None:
            spell_id, spell_target_id, cast_level = spell_choice
            campaign = await _campaign(client, args.campaign_id)
            cast_arguments: dict[str, Any] = {
                "campaign_id": args.campaign_id,
                "actor_id": actor_id,
                "spell_id": spell_id,
                "cast_level": cast_level,
                "branch_id": branch["id"],
                "expected_revision": campaign["revision"],
                "idempotency_key": (
                    f"encounter-spell-{_operation_token(args, sequence, spell_id)}"
                ),
            }
            if spell_id == MAGIC_MISSILE_ID:
                cast_arguments["target_allocations"] = [
                    {"target_id": spell_target_id, "darts": cast_level + 2}
                ]
            elif spell_id == HEALING_WORD_ID:
                cast_arguments["declaration"] = {"target_id": spell_target_id}
            cast = await client.domain("combat_cast_spell", cast_arguments)
            spell_result: dict[str, Any] = {"cast": cast}
            source_flee_observations = _record_source_flee_damage(
                cast,
                flee_actor_ids=set(args.flee_actor_id),
                damage_taken_by_actor=damage_taken_by_flee_actor,
                critical_hit_actor_ids=critical_hit_flee_actor_ids,
            )
            pending_reaction = cast.get("status") == "pending_reaction"
            if spell_id == GUIDING_BOLT_ID:
                if cast.get("status") != "pending_resolution":
                    raise RuntimeError(
                        "Guiding Bolt did not open a source-bound spell attack resolution"
                    )
                campaign = await _campaign(client, args.campaign_id)
                settled = await client.domain(
                    "combat_resolve_attack",
                    {
                        "campaign_id": args.campaign_id,
                        "actor_id": actor_id,
                        "target_id": spell_target_id,
                        "action": {"spell_resolution_id": str(cast["result"]["resolution_id"])},
                        "branch_id": branch["id"],
                        "expected_revision": campaign["revision"],
                        "idempotency_key": (
                            f"encounter-guiding-bolt-{_operation_token(args, sequence)}"
                        ),
                    },
                )
                spell_result["settlement"] = settled
                source_flee_observations.extend(
                    _record_source_flee_damage(
                        settled,
                        flee_actor_ids=set(args.flee_actor_id),
                        damage_taken_by_actor=damage_taken_by_flee_actor,
                        critical_hit_actor_ids=critical_hit_flee_actor_ids,
                    )
                )
                pending_reaction = settled.get("status") == "pending_reaction"
                if settled.get("status") == "pending_ruling":
                    choice_id = _require_pending_on_hit_choice_id(
                        settled,
                        operation="combat_resolve_attack.guiding_bolt",
                        actor_id=actor_id,
                        target_id=spell_target_id,
                        action={
                            "spell_id": GUIDING_BOLT_ID,
                            "spell_resolution_id": str(
                                cast["result"]["resolution_id"]
                            ),
                        },
                        retry_hint=(
                            "Resolve the typed pre-commit ruling and retry "
                            "at the current revision."
                        ),
                    )
                    campaign = await _campaign(client, args.campaign_id)
                    ruling = _facade_value(
                        await client.domain(
                            "combat_choice",
                            {
                                "campaign_id": args.campaign_id,
                                "action": "on_hit_ruling",
                                "actor_id": spell_target_id,
                                "payload": {
                                    "choice_id": choice_id,
                                    "selection": {
                                        "id": "next_attack_advantage",
                                        "source_excerpt": GUIDING_BOLT_ON_HIT,
                                    },
                                },
                                "branch_id": branch["id"],
                                "expected_revision": campaign["revision"],
                                "idempotency_key": (
                                    "encounter-guiding-bolt-on-hit-"
                                    + _operation_token(
                                        args,
                                        sequence,
                                        choice_id,
                                    )
                                ),
                            },
                        )
                    )
                    spell_result["on_hit_ruling"] = ruling
                elif settled.get("status") not in {
                    "committed",
                    "pending_reaction",
                }:
                    raise RuntimeError(
                        "Guiding Bolt spell attack did not commit or open a supported "
                        "reaction or on-hit ruling"
                    )
            elif cast.get("status") not in {"committed", "pending_reaction"}:
                raise RuntimeError(f"{spell_id} did not commit through structured spell settlement")
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "spell",
                    "actor_id": actor_id,
                    "spell_id": spell_id,
                    "cast_level": cast_level,
                    "target_id": spell_target_id,
                    "result": spell_result,
                    "source_flee_observations": source_flee_observations,
                }
            )
            if pending_reaction:
                continue
            if _has_action_budget(dict(cast.get("combat") or {}), actor_id):
                # The server budget is authoritative: a bonus-action spell such as
                # Healing Word leaves a main action available. The cast-declared
                # guard above still prevents a second leveled spell this turn.
                continue
            await _end_turn(client, args, str(branch["id"]), actor_id, sequence)
            continue
        if actor_id in effective_party_ids and not living_targets:
            if "dodge" in available_actions:
                campaign = await _campaign(client, args.campaign_id)
                dodged = await client.domain(
                    "combat_common_action",
                    {
                        "campaign_id": args.campaign_id,
                        "actor_id": actor_id,
                        "action": "dodge",
                        "branch_id": branch["id"],
                        "expected_revision": campaign["revision"],
                        "idempotency_key": (
                            f"encounter-unseen-dodge-{_operation_token(args, sequence)}"
                        ),
                    },
                )
                turns.append(
                    {
                        "sequence": sequence,
                        "kind": "dodge_unseen",
                        "actor_id": actor_id,
                        "result": dodged,
                    }
                )
            await _end_turn(client, args, str(branch["id"]), actor_id, sequence)
            continue
        source_opening_weapon = opening_weapons.get(actor_id)
        preferred_weapon_id = ""
        if source_opening_weapon is not None and actor_id not in completed_opening_weapon_actor_ids:
            preferred_weapon_id = source_opening_weapon["weapon_id"]
        elif actor_id in hostile_turn_actor_ids:
            tactical_source_id = str(body_thief_sides["controlled_hosts"].get(actor_id, actor_id))
            preferred_weapon_id = _preferred_hostile_weapon_id(
                actor,
                hostile_index=hostile_ids.index(tactical_source_id),
            )
        active_multiattack = bool(
            dict(combatants[actor_id].get("turn_flags") or {}).get("multiattack")
        )
        multiattack_option_id = (
            _preferred_multiattack_option_id(
                actor,
                preferred_weapon_id=preferred_weapon_id,
            )
            if not active_multiattack
            else ""
        )
        attack_context = (
            {"direct_sunlight": attack_environments[actor_id]["direct_sunlight"]}
            if actor_id in attack_environments
            else None
        )
        plan = await _preflight_attack(
            client,
            args,
            actor,
            living_targets,
            preferred_weapon_id=preferred_weapon_id,
            multiattack_option_id=multiattack_option_id,
            action_context=attack_context,
            knock_out_target_ids=(
                knock_out_hostile_ids if actor_id in effective_party_ids else None
            ),
            agent_rulings=agent_preflight_rulings,
        )
        source_separation_target = _source_separation_target(
            actor_id,
            living_targets,
            source_separations,
        )
        if plan is None and living_targets and source_separation_target is None:
            destination = _choose_destination(
                combat,
                actor_id,
                living_targets[0],
                avoided_cells=avoided_cells_by_actor.get(actor_id, set()),
            )
            if destination is not None:
                campaign = await _campaign(client, args.campaign_id)
                moved = await client.domain(
                    "combat_movement",
                    {
                        "campaign_id": args.campaign_id,
                        "actor_id": actor_id,
                        "action": "move",
                        "payload": {
                            "distance": destination[1],
                            "destination": destination[0],
                            "path": destination[2],
                        },
                        "branch_id": branch["id"],
                        "expected_revision": campaign["revision"],
                        "idempotency_key": (f"encounter-move-{_operation_token(args, sequence)}"),
                    },
                )
                turns.append(
                    {
                        "sequence": sequence,
                        "kind": "move",
                        "actor_id": actor_id,
                        "planned_path": destination[2],
                        "avoided_cells": sorted(avoided_cells_by_actor.get(actor_id, set())),
                        "result": moved,
                    }
                )
                if _has_blocking_pending(dict(moved.get("combat") or {})):
                    continue
                plan = await _preflight_attack(
                    client,
                    args,
                    actor,
                    living_targets,
                    preferred_weapon_id=preferred_weapon_id,
                    multiattack_option_id=multiattack_option_id,
                    action_context=attack_context,
                    knock_out_target_ids=(
                        knock_out_hostile_ids if actor_id in effective_party_ids else None
                    ),
                    agent_rulings=agent_preflight_rulings,
                )
        if plan is None and source_separation_target is not None:
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "source_separation_no_legal_attack",
                    "actor_id": actor_id,
                    "target_id": source_separation_target["actor_id"],
                    "minimum_distance_ft": source_separation_target["minimum_distance_ft"],
                    "source_excerpt": source_separation_target["source_excerpt"],
                }
            )
        if plan is not None:
            target_id, action, preflight = plan
            campaign = await _campaign(client, args.campaign_id)
            resolved = await client.domain(
                "combat_resolve_attack",
                {
                    "campaign_id": args.campaign_id,
                    "actor_id": actor_id,
                    "target_id": target_id,
                    "action": action,
                    "branch_id": branch["id"],
                    "expected_revision": campaign["revision"],
                    "idempotency_key": (
                        "encounter-attack-"
                        + _operation_token(
                            args,
                            sequence,
                            campaign["revision"],
                        )
                    ),
                },
            )
            selected_weapon_id = str(action.get("weapon_id") or "")
            if (
                source_opening_weapon is not None
                and selected_weapon_id == source_opening_weapon["weapon_id"]
            ):
                completed_opening_weapon_actor_ids.add(actor_id)
            source_flee_observations = _record_source_flee_damage(
                resolved,
                flee_actor_ids=set(args.flee_actor_id),
                damage_taken_by_actor=damage_taken_by_flee_actor,
                critical_hit_actor_ids=critical_hit_flee_actor_ids,
            )
            on_hit_settlement = None
            if resolved.get("status") == "pending_ruling":
                choice_id = _require_pending_on_hit_choice_id(
                    resolved,
                    operation="combat_resolve_attack",
                    actor_id=actor_id,
                    target_id=target_id,
                    action=action,
                    retry_hint=(
                        "Inspect the typed missing facts, adjudicate them as the "
                        "Agent, and retry at the current revision."
                    ),
                )
                ruling = on_hit_rulings.get((actor_id, selected_weapon_id))
                if ruling is None:
                    raise EncounterRulingRequiredError(
                        resolved,
                        operation="combat_choice.on_hit_ruling",
                        actor_id=actor_id,
                        target_id=target_id,
                        action=action,
                        retry_hint=(
                            "Inspect the reviewed attack card and retry with one typed "
                            "--source-on-hit-ruling-json settlement."
                        ),
                    )
                campaign = await _campaign(client, args.campaign_id)
                on_hit_settlement = _facade_value(
                    await client.domain(
                        "combat_choice",
                        {
                            "campaign_id": args.campaign_id,
                            "action": "on_hit_ruling",
                            "actor_id": target_id,
                            "payload": {
                                "choice_id": choice_id,
                                "selection": {
                                    key: value
                                    for key, value in ruling.items()
                                    if key not in {"actor_id", "weapon_id"}
                                },
                            },
                            "branch_id": branch["id"],
                            "expected_revision": campaign["revision"],
                            "idempotency_key": (
                                "encounter-on-hit-ruling-"
                                + _operation_token(args, sequence, choice_id)
                            ),
                        },
                    )
                )
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "attack",
                    "actor_id": actor_id,
                    "target_id": target_id,
                    "preflight": preflight,
                    "result": resolved,
                    "source_opening_weapon": (
                        source_opening_weapon
                        if source_opening_weapon is not None
                        and selected_weapon_id == source_opening_weapon["weapon_id"]
                        else None
                    ),
                    "source_flee_observations": source_flee_observations,
                    "on_hit_settlement": on_hit_settlement,
                }
            )
            settlement_combat = (
                dict(on_hit_settlement.get("combat") or {})
                if on_hit_settlement is not None
                else dict(resolved.get("combat") or {})
            )
            if _has_blocking_pending(settlement_combat):
                continue
            if _has_multiattack_followup(
                dict(resolved.get("combat") or {}),
                actor_id,
            ):
                continue
        await _end_turn(client, args, str(branch["id"]), actor_id, sequence)
    else:
        raise RuntimeError(f"combat did not reach a source outcome in {args.max_turns} turns")
    campaign = await _campaign(client, args.campaign_id)
    ended = await client.domain(
        "combat_end",
        {
            "campaign_id": args.campaign_id,
            "outcome": {"status": outcome_status, "summary": outcome_summary},
            "branch_id": branch["id"],
            "expected_revision": campaign["revision"],
            "idempotency_key": (f"encounter-end-{_operation_token(args, outcome_status)}"),
        },
    )
    opened_play = await client.open(args.campaign_id)
    await client.load("play.scene", "play.scene_control", "play.characters")
    checkpoint = await _checkpoint(
        client,
        campaign_id=args.campaign_id,
        run_id=args.run_id,
        label=args.checkpoint_label,
        checkpoint_id=f"encounter:{str(ended['combat']['id'])}",
    )
    final_actor_ids = [*party_ids, *hostile_ids]
    final_actor_values = await _characters(client, args.campaign_id, final_actor_ids)
    for captured_actor_id in knock_out_hostile_ids:
        captured_actor = final_actor_values[captured_actor_id]
        captured_conditions = _conditions(captured_actor)
        if (
            _hit_points(captured_actor) > 1
            or "unconscious" not in captured_conditions
            or "dead" in captured_conditions
        ):
            raise RuntimeError("designated knockout hostile was not captured unconscious and alive")
    final_actors = [
        _character_summary(final_actor_values[actor_id]) for actor_id in final_actor_ids
    ]
    return {
        "combat_exposure": opened_combat,
        "visibility_patch": visibility_patch,
        "turns": turns,
        "fled_hostile_ids": sorted(fled_hostile_ids),
        "source_flee_damage_taken": dict(sorted(damage_taken_by_flee_actor.items())),
        "source_flee_critical_hit_actor_ids": sorted(critical_hit_flee_actor_ids),
        "source_casualty_pools": list(source_casualty_pools.values()),
        "source_separations": list(source_separations.values()),
        "truce": (
            {
                "actor_id": args.truce_actor_id,
                "after_defeated": args.truce_after_defeated,
                "source_excerpt": str(args.truce_source_excerpt or "").strip(),
            }
            if args.truce_actor_id
            else None
        ),
        "source_opening_casts": opening_casts,
        "completed_opening_cast_sequences": sorted(completed_opening_casts),
        "source_opening_weapons": list(opening_weapons.values()),
        "completed_opening_weapon_actor_ids": sorted(completed_opening_weapon_actor_ids),
        "source_on_hit_rulings": list(on_hit_rulings.values()),
        "source_delayed_actions": list(delayed_actions.values()),
        "source_passive_allies": list(passive_allies.values()),
        "source_random_activities": list(random_activities.values()),
        "source_save_activities": list(save_activities.values()),
        "source_contest_activities": list(contest_activities.values()),
        "source_attack_environments": list(attack_environments.values()),
        "agent_preflight_rulings": agent_preflight_rulings,
        "source_avoidances": source_avoidance_evidence,
        "source_zero_hp_finisher": source_zero_hp_finisher,
        "source_zero_hp_stabilization": source_zero_hp_stabilization,
        "source_target_priorities": list(
            {
                tuple(value["actor_ids"]): value
                for value in source_target_priorities.values()
            }.values()
        ),
        "agent_target_priorities": list(
            {
                tuple(value["actor_ids"]): value
                for value in agent_target_priorities.values()
            }.values()
        ),
        "surrender": (
            {
                "actor_id": args.surrender_actor_id,
                "at_or_below_hit_points": args.surrender_at_hp,
                "after_defeated": args.surrender_after_defeated,
                "no_escape": args.surrender_no_escape,
                "source_excerpt": str(args.surrender_source_excerpt or "").strip(),
            }
            if surrender_configured
            else None
        ),
        "knocked_out_hostile_ids": sorted(knock_out_hostile_ids),
        "outcome": ended,
        "play_exposure": opened_play,
        "checkpoint": checkpoint,
        "actors": final_actors,
    }


async def _finalize_ended_encounter(
    client: ExposureClient,
    args: argparse.Namespace,
    actor_ids: list[str],
) -> dict[str, Any]:
    opened = await client.open(args.campaign_id)
    if str(opened.get("phase") or "") != "play":
        raise RuntimeError("encounter finalization requires the Play phase")
    await client.load("play.scene", "play.scene_control", "play.characters")
    campaign = await _campaign(client, args.campaign_id)
    combat = dict(dict(campaign.get("state") or {}).get("combat") or {})
    outcome = dict(combat.get("outcome") or {})
    if (
        not combat
        or combat.get("active", True)
        or outcome.get("status")
        not in {
            "victory",
            "defeat",
            "surrender",
            "truce",
            "withdrawal",
            "interrupted",
        }
    ):
        raise RuntimeError("campaign does not retain a completed encounter with a source outcome")
    if args.scene_id and str(combat.get("scene_id") or "") != str(args.scene_id):
        raise RuntimeError("completed encounter scene does not match --scene-id")
    checkpoint = await _checkpoint(
        client,
        campaign_id=args.campaign_id,
        run_id=args.run_id,
        label=args.checkpoint_label,
        checkpoint_id=f"encounter:{str(combat['id'])}",
    )
    actor_values = await _characters(client, args.campaign_id, actor_ids)
    return {
        "play_exposure": opened,
        "recovered_after_postcombat_interruption": True,
        "combat": combat,
        "outcome": outcome,
        "checkpoint": checkpoint,
        "actors": [_character_summary(actor_values[actor_id]) for actor_id in actor_ids],
    }


async def _start_or_resume_auto_run(
    client: ExposureClient,
    args: argparse.Namespace,
    party_ids: list[str],
    hostile_ids: list[str],
    additional_hostile_ids: list[str],
    reinforcement_hostile_ids: list[str],
) -> dict[str, Any]:
    opened = await client.open(args.campaign_id)
    phase = str(opened.get("phase") or "")
    started: dict[str, Any] | None = None
    if phase == "play":
        started = await _start(
            client,
            args,
            party_ids,
            hostile_ids,
            additional_hostile_ids,
            reinforcement_hostile_ids,
        )
    elif phase != "combat":
        raise RuntimeError(
            "auto-run requires the play phase or an active combat; "
            f"campaign is in {phase or 'an unknown phase'}"
        )
    completed = await _auto_run(
        client,
        args,
        party_ids,
        [
            *hostile_ids,
            *additional_hostile_ids,
            *reinforcement_hostile_ids,
        ],
    )
    if started is not None:
        completed["auto_start"] = started
    return completed


async def _status(
    client: ExposureClient,
    *,
    campaign_id: str,
    actor_ids: list[str],
) -> dict[str, Any]:
    opened = await client.open(campaign_id)
    phase = str(opened.get("phase") or "")
    combat = None
    if phase == "combat":
        await client.load("combat.observe")
        combat = await client.domain(
            "combat_query",
            {"campaign_id": campaign_id, "view": "status"},
        )
    elif phase == "play":
        await client.load("play.characters")
        campaign = await _campaign(client, campaign_id)
        retained_combat = dict(dict(campaign.get("state") or {}).get("combat") or {})
        combat = retained_combat or None
    else:
        raise RuntimeError(
            "encounter status requires the play phase or an active combat; "
            f"campaign is in {phase or 'an unknown phase'}"
        )
    actor_values = await _characters(client, campaign_id, actor_ids)
    return {
        "exposure": opened,
        "phase": phase,
        "combat": combat,
        "actors": [_character_summary(actor_values[actor_id]) for actor_id in actor_ids],
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    actor_groups = _encounter_actor_groups(args)
    party_ids = actor_groups["party_ids"]
    agent_party_absences = actor_groups["agent_party_absences"]
    ally_ids = actor_groups["ally_ids"]
    friendly_ids = [*party_ids, *ally_ids]
    hostile_ids = actor_groups["hostile_ids"]
    additional_hostile_ids = actor_groups["additional_hostile_ids"]
    reinforcement_hostile_ids = actor_groups["reinforcement_hostile_ids"]
    all_hostile_ids = [
        *hostile_ids,
        *additional_hostile_ids,
        *reinforcement_hostile_ids,
    ]
    report: dict[str, Any] = {
        "action": args.action,
        "transport": "stdio",
        "campaign_id": args.campaign_id,
        "run_id": args.run_id,
        "party_ids": party_ids,
        "agent_party_absences": agent_party_absences,
        "ally_ids": ally_ids,
        "friendly_ids": friendly_ids,
        "hostile_ids": hostile_ids,
        "additional_hostile_ids": additional_hostile_ids,
        "reinforcement_hostile_ids": reinforcement_hostile_ids,
    }
    async with stdio_client(_server_parameters(args)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            client = ExposureClient(session)
            if args.action == "start":
                report["result"] = await _start(
                    client,
                    args,
                    friendly_ids,
                    hostile_ids,
                    additional_hostile_ids,
                    reinforcement_hostile_ids,
                )
            elif args.action == "auto-run":
                report["result"] = await _start_or_resume_auto_run(
                    client,
                    args,
                    friendly_ids,
                    hostile_ids,
                    additional_hostile_ids,
                    reinforcement_hostile_ids,
                )
            elif args.action == "finalize":
                report["result"] = await _finalize_ended_encounter(
                    client,
                    args,
                    [*friendly_ids, *all_hostile_ids],
                )
            else:
                actor_ids = [*friendly_ids, *all_hostile_ids]
                report["result"] = await _status(
                    client,
                    campaign_id=args.campaign_id,
                    actor_ids=actor_ids,
                )
    report["passed"] = True
    return report


def _leaf_messages(error: BaseException) -> list[str]:
    nested = getattr(error, "exceptions", ())
    if nested:
        return [message for child in nested for message in _leaf_messages(child)]
    return [f"{type(error).__name__}: {error}"]


def _leaf_ruling_requirements(error: BaseException) -> list[dict[str, Any]]:
    nested = getattr(error, "exceptions", ())
    if nested:
        return [
            requirement
            for child in nested
            for requirement in _leaf_ruling_requirements(child)
        ]
    if isinstance(error, EncounterRulingRequiredError):
        return [deepcopy(error.requirement)]
    return []


def main() -> int:
    args = _arguments()
    try:
        report = asyncio.run(_run(args))
    except Exception as error:
        ruling_requirements = _leaf_ruling_requirements(error)
        report = {
            "action": args.action,
            "campaign_id": args.campaign_id,
            "run_id": args.run_id,
            "passed": False,
            "error": "; ".join(_leaf_messages(error)),
            **(
                {
                    "status": "pending_ruling",
                    "default_resolver": (
                        "agent"
                        if all(
                            str(dict(item.get("ruling") or {}).get("default_resolver") or "agent")
                            == "agent"
                            for item in ruling_requirements
                        )
                        else "external_input"
                    ),
                    "ruling_requirements": ruling_requirements,
                }
                if ruling_requirements
                else {}
            ),
        }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
