"""Run a source-defined encounter exclusively through public stdio MCP tools."""

from __future__ import annotations

import argparse
import asyncio
import heapq
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from sagasmith_core.modules import (
    normalize_source_evidence_text as _normalized_source_text,
)
from sagasmith_dnd.conditions import (
    DEATH_SAVE_SETTLED_CONDITIONS,
    INCAPACITATING_STATE_IDS,
)
from sagasmith_dnd.spell_resolution import effective_spell_resolution
from sagasmith_dnd.vocabulary import (
    ATTACK_MODES,
    COMBAT_OUTCOME_STATUSES,
    WEAPON_HAND_SLOTS,
)

from scripts.regression_lock import campaign_operation_lock
from scripts.regression_modules import (
    ExposureClient,
    _facade_value,
    _token,
    campaign_view,
)
from scripts.regression_playthrough import _checkpoint, _manifest_get
from scripts.regression_rulings import normalize_pending_ruling
from scripts.regression_runtime import (
    exception_leaf_messages,
    regression_server_parameters,
)

GUIDING_BOLT_ID = "dnd5e.content.srd2014.spell.guiding-bolt"
HEALING_WORD_ID = "dnd5e.content.srd2014.spell.healing-word"
HYPNOTIC_PATTERN_ID = "dnd5e.content.srd2014.spell.hypnotic-pattern"
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


def _require_committed_encounter_start(result: dict[str, Any]) -> dict[str, Any]:
    """Enter Combat exposure only after combat_start actually committed."""

    if result.get("status") == "pending_ruling":
        raise EncounterRulingRequiredError(
            result,
            operation="combat_start",
            retry_hint=(
                "Supply a source-grounded temporary-map ruling or omit an "
                "unindexed location key so the canonical default map can be "
                "compiled, then retry the same public encounter start."
            ),
        )
    combat = dict(result.get("combat") or {})
    if not combat.get("active"):
        raise RuntimeError(
            "combat_start returned without an active committed encounter"
        )
    return combat


def _encounter_battle_map_request(location_key: str | None) -> dict[str, Any]:
    """Use indexed spatial evidence when available, otherwise the canonical default grid."""

    normalized = str(location_key or "").strip()
    return {"location_key": normalized} if normalized else {}


def _encounter_operation_scope(
    args: argparse.Namespace,
    *,
    branch_id: str,
    party_ids: list[str],
    hostile_ids: list[str],
    additional_hostile_ids: list[str] | None = None,
    reinforcement_hostile_ids: list[str] | None = None,
    reinforcement_ally_ids: list[str] | None = None,
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
        "reinforcement_ally_ids": list(reinforcement_ally_ids or []),
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


def _movement_operation_token(
    args: argparse.Namespace,
    *,
    sequence: int,
    actor_id: str,
    target_id: str,
    destination: tuple[dict[str, int], int, list[dict[str, int]]],
) -> str:
    """Identify one semantic movement request across process recovery."""

    position, distance, path = destination
    identity = {
        "operation_scope": _operation_scope(args),
        "sequence": sequence,
        "actor_id": actor_id,
        "target_id": target_id,
        "distance": distance,
        "destination": position,
        "path": path,
    }
    return _token(
        json.dumps(
            identity,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        length=24,
    )


def _agent_turn_transaction_token(
    args: argparse.Namespace,
    *,
    branch_id: str,
    application_id: str,
    parts: tuple[object, ...] = (),
) -> str:
    """Identify one Agent settlement independently of driver-local encounter flags."""

    identity = {
        "campaign_id": str(args.campaign_id),
        "run_id": str(args.run_id),
        "branch_id": str(branch_id),
        "application_id": str(application_id),
        "parts": [str(part) for part in parts],
    }
    return _token(
        json.dumps(
            identity,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        length=24,
    )


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
        "--reinforcement-ally-report",
        type=Path,
        action="append",
        default=[],
        help=(
            "Source-cited friendly reinforcements queued through public "
            "combat_join without registering them as party members"
        ),
    )
    parser.add_argument(
        "--reinforcement-ally-actor-id",
        action="append",
        default=[],
        help=(
            "Select an exact friendly reinforcement from the prepared reports; "
            "repeat as needed. When omitted, every actor is selected."
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
    parser.add_argument(
        "--reinforcement-ally-label",
        default="Source friendly reinforcements",
    )
    parser.add_argument("--reinforcement-ally-source-excerpt", default="")
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
        "--source-surprise-report",
        type=Path,
        help=(
            "Passed public record-event or record-outcome report containing the exact "
            "source_ref and source_excerpt that grant --source-surprised-actor-id; use "
            "when the surprise grant is cited in a different scene from the encounter"
        ),
    )
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
        "--agent-reinforcement-trigger-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Agent-as-DM interpretation of a source-semantic reinforcement "
            "condition with actor_ids, trigger_round, exact source_excerpt, "
            "decision, and ruling_reason. The actors still enter through public "
            "combat_join at that future round."
        ),
    )
    parser.add_argument(
        "--agent-target-priority-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Agent tactical target decision with same-side actor_ids, every opposing "
            "participant exactly once in ordered priority_groups, decision, and "
            "ruling_reason; the server still validates every attempted target"
        ),
    )
    parser.add_argument(
        "--agent-weapon-priority-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Agent tactical weapon policy with actor_id, ordered choices "
            "(weapon_id, attack_mode, optional multiattack_option_id), decision, "
            "and ruling_reason. Auto-run stops at the Agent boundary when a turn "
            "needs an attack and no source opening or Agent policy exists."
        ),
    )
    parser.add_argument(
        "--agent-spell-priority-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Agent tactical spell policy with actor_id, ordered choices "
            "(spell_id, target_policy, cast_level_policy=lowest_available), "
            "decision, and ruling_reason. Supported target policies are "
            "downed_ally, prioritized_opponent, and "
            "maximize_opponents_without_allies."
        ),
    )
    parser.add_argument(
        "--agent-common-action-priority-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Agent tactical fallback with actor_id, ordered choices containing "
            "action=dodge, decision, and ruling_reason. This lets a participant "
            "legally spend its turn when the reviewed scene makes every recorded "
            "attack or spell inappropriate."
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
        "--flee-at-hp",
        type=int,
        default=0,
        help=(
            "Source-defined current hit-point threshold at or below which every "
            "designated actor attempts to flee"
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
        "--linked-flee-actor-id",
        action="append",
        default=[],
        help=(
            "Source-designated hostile that retreats after another hostile has "
            "already fled; repeat for every linked survivor"
        ),
    )
    parser.add_argument("--linked-flee-trigger-actor-id", default="")
    parser.add_argument("--linked-flee-destination-location-key", default="")
    parser.add_argument("--linked-flee-source-excerpt", default="")
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
        "--source-ammunition-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Source-provenanced ammunition selection with actor_id, weapon_id, "
            "and ammunition_item_id; repeat for distinct actor/weapon pairs"
        ),
    )
    parser.add_argument(
        "--content-solution-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Agent-authored reusable solution for one custom source card: actor_id, "
            "source_card_id, source_card_kind, resolution_plan, compile_ruling, "
            "bindings, and execution_ruling. The driver persists it through "
            "content_solution and executes it through combat_choice(execute_plan). "
            "Optional activations schedule action-triggered cards with round plus "
            "per-use bindings/execution_ruling and optional spell cast_level; repeat "
            "for distinct source cards."
        ),
    )
    parser.add_argument(
        "--agent-attack-context-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Source-bound Agent-as-DM attack context with actor_id, optional "
            "target_id, attack_mode, exact source_ref and source_excerpt, decision, "
            "ruling_reason, and either an unambiguous advantage/disadvantage result "
            "or target-relative cover (half, three_quarters, or total); repeat for "
            "distinct actor, target, or attack-mode relationships"
        ),
    )
    parser.add_argument(
        "--agent-casting-perception-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Explicit Agent-as-DM hidden-casting perception decision with caster_id, "
            "one observation per affected observer (observer_id, perceived, reason), "
            "decision, and ruling_reason. The driver never infers perception from "
            "missing scene facts."
        ),
    )
    parser.add_argument(
        "--agent-target-reaction-context-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Source-bound Agent-as-DM target reaction with actor_id for the reacting "
            "target, attack_mode, exact source_ref and source_excerpt, exactly one "
            "true advantage or disadvantage result, decision, and ruling_reason; "
            "the driver opens and resolves a public reaction window before applying "
            "the modifier to that triggering attack"
        ),
    )
    parser.add_argument(
        "--agent-turn-ruling-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Agent-as-DM settlement for one source-cited scene procedure: actor_id, "
            "procedure_id, round, source_ref, exact procedure source excerpt, exact "
            "encounter_source_excerpt, decision, and ruling_reason. Custom actor-card "
            "activities and spells use --content-solution-json instead. Scene "
            "procedures pay a normal improvised action and may provide check_ability, "
            "check_dc, and the edition-legal check_action for one action-bound "
            "server-rolled check (2014 uses improvise for a source-authored "
            "persuasion action; 2024 uses influence). "
            "Optional target_id plus save_ability/save_dc settle a server-rolled save; "
            "success_outcome/failure_outcome record either roll's meaning. "
            "A successful procedure check may provide success_combat_outcome to end "
            "the encounter with the Agent-selected, source-grounded result. "
            "A failed save may include forced_target_id to direct the target's next "
            "attack without inventing a creature-specific rule."
        ),
    )
    parser.add_argument(
        "--agent-object-interaction-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Agent-as-DM free object interaction that ends one exact "
            "encounter-source condition: actor_id, round, object_description, "
            "interaction=remove, condition, source_ref, exact source_excerpt, "
            "decision, and ruling_reason"
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
            "Hostile eligible for capture with the public 2014/2024 melee knockout "
            "rule; repeat to constrain a minimum objective to selected hostiles, or "
            "omit --minimum-hostile-knockouts to require every selected hostile"
        ),
    )
    parser.add_argument(
        "--minimum-hostile-knockouts",
        type=int,
        default=None,
        help=(
            "Agent-selected minimum number of hostiles that must finish alive and "
            "unconscious; zero keeps the selected nonlethal preference without making "
            "capture a hard encounter-success condition; when no eligible hostile ids "
            "are supplied, every encounter hostile is eligible"
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
    return regression_server_parameters(
        home=args.home,
        auto_seed=True,
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
        "reinforcement_ally_ids": _selected_prepared_actor_ids(
            getattr(args, "reinforcement_ally_report", []),
            getattr(args, "reinforcement_ally_actor_id", []),
            report_kind="reinforcement ally",
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
        or (
            reinforcement_round
            and not (
                groups["reinforcement_hostile_ids"]
                or groups["reinforcement_ally_ids"]
            )
        )
        or (
            (
                groups["reinforcement_hostile_ids"]
                or groups["reinforcement_ally_ids"]
            )
            and reinforcement_round == 1
        )
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
            "reinforcement_ally_ids",
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
    reinforcement_ally_ids: list[str] | None = None,
    reinforcement_ally_label: str = "",
    reinforcement_ally_source_excerpt: str = "",
) -> dict[str, Any]:
    if not source_excerpt.strip():
        raise ValueError("encounter start requires an exact source excerpt")
    additional_ids = list(additional_hostile_ids or [])
    if additional_ids and not additional_source_excerpt.strip():
        raise ValueError("additional source hostiles require an exact source excerpt")
    reinforcement_ids = list(reinforcement_hostile_ids or [])
    if reinforcement_ids and not reinforcement_source_excerpt.strip():
        raise ValueError("source reinforcements require an exact source excerpt")
    reinforcement_friend_ids = list(reinforcement_ally_ids or [])
    if reinforcement_friend_ids and not reinforcement_ally_source_excerpt.strip():
        raise ValueError(
            "source friendly reinforcements require an exact source excerpt"
        )
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
    if reinforcement_friend_ids:
        groups.append(
            {
                "key": "source-friendly-reinforcements",
                "label": reinforcement_ally_label,
                "role": "reinforcement",
                "required_count": len(reinforcement_friend_ids),
                "actor_ids": reinforcement_friend_ids,
                "source_excerpt": reinforcement_ally_source_excerpt,
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


async def _require_encounter_preflight(
    client: ExposureClient,
    *,
    campaign_id: str,
    scene_id: str,
    participant_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Fail before mutation when required participants are missing or invalid."""

    preflight = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "preflight",
            "payload": {
                "scene_id": scene_id,
                "participant_manifest": participant_manifest,
            },
        },
    )
    if not isinstance(preflight, dict):
        raise TypeError("module_query(view='preflight') must return an object")
    if preflight.get("ready") is not True:
        failed_groups = [
            {
                "key": str(group.get("key") or ""),
                "missing_count": int(group.get("missing_count", 0) or 0),
                "invalid_count": int(group.get("invalid_count", 0) or 0),
                "invalid_actor_ids": [
                    str(item) for item in group.get("invalid_actor_ids") or []
                ],
                "hard_blockers": {
                    str(actor.get("id") or ""): list(
                        dict(actor.get("combat_card") or {}).get(
                            "hard_blockers"
                        )
                        or []
                    )
                    for actor in group.get("actors") or []
                    if isinstance(actor, dict)
                    and dict(actor.get("combat_card") or {}).get(
                        "hard_blockers"
                    )
                },
                "issues": list(group.get("issues") or []),
            }
            for group in preflight.get("groups", [])
            if isinstance(group, dict)
            and (
                int(group.get("missing_count", 0) or 0) > 0
                or int(group.get("invalid_count", 0) or 0) > 0
                or bool(group.get("issues"))
            )
        ]
        raise RuntimeError(
            "encounter participant preflight failed before mutation "
            f"(groups={failed_groups})"
        )
    return preflight


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
    source_separations: dict[str, dict[str, Any]] | None = None,
    agent_positions: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    allies = list(ally_ids or [])
    preferred_hostile_positions = (
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
        (10, 6),
        (2, 7),
        (4, 7),
        (6, 7),
        (8, 7),
        (10, 7),
        (2, 9),
        (4, 9),
        (6, 9),
        (8, 9),
        (10, 9),
    )
    hostile_positions = [
        *preferred_hostile_positions,
        *[
            (x, y)
            for y in range(1, 12)
            for x in range(2, 12)
            if (x, y) not in set(preferred_hostile_positions)
        ],
    ]
    if len(party_ids) > 11 or len(allies) > 11 or len(hostile_ids) > len(
        hostile_positions
    ):
        raise ValueError(
            "default encounter layout supports at most 11 PCs, 11 allied NPCs, "
            f"and {len(hostile_positions)} hostiles"
        )
    if set(party_ids) & set(allies):
        raise ValueError("PC and allied-NPC participant ids must be disjoint")

    def source_fields(actor_id: str) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        conditions = list(dict(source_conditions_by_actor or {}).get(actor_id) or [])
        if conditions:
            fields["source_conditions"] = conditions
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
    disposition: str = "hostile",
    join_round: int = 0,
    tie_breaker: int | None = None,
    source_conditions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Place a queued source reinforcement without granting an immediate turn."""

    if not actor_id.strip():
        raise ValueError("source reinforcement actor_id must be non-empty")
    if disposition not in {"friendly", "hostile"}:
        raise ValueError(
            "source reinforcement disposition must be friendly or hostile"
        )
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
    if tie_breaker is not None and (
        isinstance(tie_breaker, bool)
        or not isinstance(tie_breaker, int)
        or tie_breaker < 0
    ):
        raise ValueError("Agent reinforcement tie breaker must be a non-negative integer")
    return {
        "position": {"x": positions[index][0], "y": positions[index][1]},
        "disposition": disposition,
        "hidden": False,
        "surprised": False,
        "death_saves": False,
        **(
            {"source_conditions": deepcopy(source_conditions)}
            if source_conditions
            else {}
        ),
        **({"tie_breaker": tie_breaker} if tie_breaker is not None else {}),
        **({"join_round": join_round} if join_round else {}),
    }


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
    source_evidence: dict[str, Any] | None = None,
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
    basis = {
        "mode": "source_declared_surprise",
        "surprised_actor_ids": normalized,
        "source_excerpt": source_excerpt.strip(),
    }
    if source_evidence is not None:
        basis["source_evidence"] = deepcopy(source_evidence)
    return {actor_id: actor_id in normalized for actor_id in participants}, basis


def _source_surprise_evidence_from_report(
    path: Path,
    *,
    campaign_id: str,
) -> dict[str, Any]:
    """Read exact surprise evidence already committed through public play tools."""

    report = _read_report(path)
    result = dict(report.get("result") or {})
    scene = dict(result.get("scene") or {})
    continuity = dict(result.get("continuity") or {})
    event = dict(continuity.get("event") or {})
    payload = dict(event.get("payload") or {})
    source_ref = scene.get("source_ref")
    source_excerpt = str(payload.get("source_excerpt") or "").strip()
    source_scene_id = str(payload.get("source_scene_id") or payload.get("scene_id") or "")
    required_source_ref_fields = {
        "module_id",
        "scene_id",
        "chunk_id",
        "page_start",
        "page_end",
        "heading_path",
        "content_sha256",
    }
    if (
        report.get("passed") is not True
        or report.get("action") not in {"record-event", "record-outcome"}
        or report.get("campaign_id") != campaign_id
        or not isinstance(source_ref, dict)
        or set(source_ref) != required_source_ref_fields
        or payload.get("source_ref") != source_ref
        or not source_excerpt
        or not source_scene_id
        or str(source_ref.get("scene_id") or "") != source_scene_id
        or not str(event.get("event_type") or "")
        or not str(event.get("summary") or "")
    ):
        raise ValueError(
            "source surprise report must be a passed public source-bound "
            "record-event or record-outcome for this campaign"
        )
    return {
        "report_path": str(path.expanduser().resolve()),
        "action": str(report["action"]),
        "event_id": str(event.get("id") or ""),
        "event_type": str(event["event_type"]),
        "summary": str(event["summary"]),
        "source_ref": deepcopy(source_ref),
        "source_excerpt": source_excerpt,
    }


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
    participants = party | hostiles
    by_actor: dict[str, dict[str, Any]] = {}
    allowed = {
        "actor_ids",
        "priority_groups",
        "decision",
        "ruling_reason",
    }
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
        decision = " ".join(str(declaration.get("decision") or "").split())
        ruling_reason = " ".join(
            str(declaration.get("ruling_reason") or "").split()
        )
        actors_are_party = bool(set(actor_ids)) and set(actor_ids) <= party
        actors_are_hostile = bool(set(actor_ids)) and set(actor_ids) <= hostiles
        expected_targets = hostiles if actors_are_party else party
        if (
            not actor_ids
            or any(not item for item in actor_ids)
            or len(actor_ids) != len(set(actor_ids))
            or not set(actor_ids) <= participants
            or actors_are_party == actors_are_hostile
            or not isinstance(raw_groups, list)
            or not raw_groups
            or not 10 <= len(decision) <= 500
            or not 10 <= len(ruling_reason) <= 500
        ):
            raise ValueError(
                "Agent target priority requires unique same-side actor_ids, "
                "non-empty opposing priority_groups, decision, and ruling_reason"
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
                or not set(group) <= expected_targets
            ):
                raise ValueError(
                    "Agent target priority groups must contain unique opposing "
                    "encounter participants"
                )
            priority_groups.append(group)
        target_ids = [item for group in priority_groups for item in group]
        if (
            len(target_ids) != len(set(target_ids))
            or set(target_ids) != expected_targets
            or set(actor_ids) & set(by_actor)
        ):
            raise ValueError(
                "Agent priority targets must enumerate every opponent exactly once "
                "and each acting participant may be declared only once"
            )
        value = {
            "actor_ids": actor_ids,
            "priority_groups": priority_groups,
            "decision": decision,
            "ruling_reason": ruling_reason,
            "default_resolver": "agent",
            "ruling_kind": "agent_dm_adjudication",
        }
        for actor_id in actor_ids:
            by_actor[actor_id] = value
    return by_actor


def _validate_agent_target_refinements(
    source_priorities: dict[str, dict[str, Any]],
    agent_priorities: dict[str, dict[str, Any]],
) -> None:
    """Keep Agent ordering within any source-authored priority constraints."""

    for actor_id in set(source_priorities) & set(agent_priorities):
        source = source_priorities[actor_id]
        agent = agent_priorities[actor_id]
        source_rank = {
            target_id: group_index
            for group_index, group in enumerate(source["priority_groups"])
            for target_id in group
        }
        fallback_rank = len(source["priority_groups"])
        ordered_targets = [
            target_id
            for group in agent["priority_groups"]
            for target_id in group
        ]
        ranks = [
            source_rank.get(target_id, fallback_rank)
            for target_id in ordered_targets
        ]
        if ranks != sorted(ranks):
            raise ValueError(
                f"Agent target priority for {actor_id} contradicts the "
                "source-authored target order"
            )


def _agent_reinforcement_triggers(
    declarations: list[dict[str, Any]],
    *,
    reinforcement_ids: list[str],
    reinforcement_round: int,
    encounter_source_excerpt: str,
) -> list[dict[str, Any]]:
    """Validate Agent interpretations of source-semantic arrival conditions."""

    available = set(reinforcement_ids)
    encounter_excerpt = _normalized_source_text(encounter_source_excerpt)
    allowed = {
        "actor_ids",
        "trigger_round",
        "source_excerpt",
        "decision",
        "ruling_reason",
    }
    normalized: list[dict[str, Any]] = []
    used_actor_ids: set[str] = set()
    for index, declaration in enumerate(declarations):
        if not isinstance(declaration, dict):
            raise ValueError(
                f"Agent reinforcement trigger {index} must be an object"
            )
        unknown = set(declaration) - allowed
        actor_ids = [
            str(item).strip()
            for item in declaration.get("actor_ids") or []
        ]
        trigger_round = declaration.get("trigger_round")
        source_excerpt = " ".join(
            str(declaration.get("source_excerpt") or "").split()
        ).strip()
        decision = " ".join(str(declaration.get("decision") or "").split())
        ruling_reason = " ".join(
            str(declaration.get("ruling_reason") or "").split()
        )
        if (
            unknown
            or not actor_ids
            or any(not item for item in actor_ids)
            or len(actor_ids) != len(set(actor_ids))
            or not set(actor_ids) <= available
            or used_actor_ids & set(actor_ids)
            or isinstance(trigger_round, bool)
            or not isinstance(trigger_round, int)
            or trigger_round < 2
            or trigger_round != reinforcement_round
            or len(source_excerpt) < 8
            or _normalized_source_text(source_excerpt) not in encounter_excerpt
            or not 10 <= len(decision) <= 500
            or not 10 <= len(ruling_reason) <= 1000
        ):
            raise ValueError(
                f"Agent reinforcement trigger {index} requires unique prepared "
                "reinforcements, the configured future round, an exact encounter "
                "excerpt, decision, and ruling_reason"
            )
        used_actor_ids.update(actor_ids)
        normalized.append(
            {
                "actor_ids": actor_ids,
                "trigger_round": trigger_round,
                "source_excerpt": source_excerpt,
                "agent_ruling": {
                    "default_resolver": "agent",
                    "ruling_kind": "agent_dm_adjudication",
                    "decision": decision,
                    "reason": ruling_reason,
                },
            }
        )
    if normalized and used_actor_ids != available:
        raise ValueError(
            "Agent reinforcement triggers must cover every configured "
            "reinforcement exactly once"
        )
    return normalized


def _weapon_attack_modes(weapon: dict[str, Any]) -> set[str]:
    modes = {str(weapon.get("attack_type") or "melee").strip().casefold()}
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
    ):
        modes.add("ranged")
    return modes & ATTACK_MODES


def _actor_weapon_attacks(actor: dict[str, Any]) -> list[dict[str, Any]]:
    """Return persisted attacks plus the universal standard unarmed strike."""
    weapons = [
        dict(item)
        for item in (
            dict(dict(actor.get("derived") or {}).get("inventory") or {}).get(
                "weapon_attacks", []
            )
        )
        if isinstance(item, dict) and str(item.get("item_id") or "")
    ]
    if not any(
        str(item.get("item_id") or "") == "unarmed-strike"
        for item in weapons
    ):
        weapons.append(
            {
                "item_id": "unarmed-strike",
                "attack_type": "melee",
                "properties": [],
            }
        )
    return weapons


def _agent_weapon_priorities(
    declarations: list[dict[str, Any]],
    *,
    participant_ids: list[str],
    actors: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Validate reusable weapon choices authored by the acting Agent."""

    participants = set(participant_ids)
    normalized: dict[str, dict[str, Any]] = {}
    allowed = {"actor_id", "choices", "decision", "ruling_reason"}
    choice_allowed = {
        "weapon_id",
        "attack_mode",
        "multiattack_option_id",
    }
    for index, declaration in enumerate(declarations):
        if not isinstance(declaration, dict):
            raise ValueError(f"Agent weapon priority {index} must be an object")
        unknown = set(declaration) - allowed
        if unknown:
            raise ValueError(
                f"Agent weapon priority {index} has unsupported fields: "
                f"{', '.join(sorted(unknown))}"
            )
        actor_id = str(declaration.get("actor_id") or "").strip()
        raw_choices = declaration.get("choices")
        decision = " ".join(str(declaration.get("decision") or "").split())
        ruling_reason = " ".join(
            str(declaration.get("ruling_reason") or "").split()
        )
        if (
            actor_id not in participants
            or actor_id in normalized
            or not isinstance(raw_choices, list)
            or not raw_choices
            or not 10 <= len(decision) <= 500
            or not 10 <= len(ruling_reason) <= 500
        ):
            raise ValueError(
                f"Agent weapon priority {index} requires one unique participant, "
                "ordered choices, decision, and ruling_reason"
            )
        actor = actors.get(actor_id)
        weapons = {
            str(item.get("item_id") or ""): dict(item)
            for item in _actor_weapon_attacks(dict(actor or {}))
        }
        multiattacks = {
            str(item.get("id") or ""): dict(item)
            for item in dict(dict(actor or {}).get("derived") or {}).get(
                "multiattack_options", []
            )
            if isinstance(item, dict) and str(item.get("id") or "")
        }
        choices: list[dict[str, str]] = []
        identities: set[tuple[str, str, str]] = set()
        for choice_index, raw_choice in enumerate(raw_choices):
            if not isinstance(raw_choice, dict):
                raise ValueError(
                    f"Agent weapon priority choice {index}:{choice_index} "
                    "must be an object"
                )
            choice_unknown = set(raw_choice) - choice_allowed
            weapon_id = str(raw_choice.get("weapon_id") or "").strip()
            attack_mode = (
                str(raw_choice.get("attack_mode") or "").strip().casefold()
            )
            multiattack_option_id = str(
                raw_choice.get("multiattack_option_id") or ""
            ).strip()
            identity = (weapon_id, attack_mode, multiattack_option_id)
            weapon = weapons.get(weapon_id)
            option = (
                multiattacks.get(multiattack_option_id)
                if multiattack_option_id
                else None
            )
            option_attacks = [
                dict(item)
                for item in dict(option or {}).get("attacks", [])
                if isinstance(item, dict)
            ]
            first_option_attack = option_attacks[0] if option_attacks else {}
            if (
                choice_unknown
                or weapon is None
                or attack_mode not in _weapon_attack_modes(weapon)
                or identity in identities
                or (
                    multiattack_option_id
                    and (
                        option is None
                        or str(first_option_attack.get("weapon_id") or "")
                        != weapon_id
                        or str(first_option_attack.get("attack_mode") or "melee")
                        != attack_mode
                    )
                )
            ):
                raise ValueError(
                    f"Agent weapon priority choice {index}:{choice_index} must "
                    "name one existing weapon and legal attack mode; an optional "
                    "Multiattack must exist and begin with that exact attack"
                )
            choices.append(
                {
                    "weapon_id": weapon_id,
                    "attack_mode": attack_mode,
                    **(
                        {"multiattack_option_id": multiattack_option_id}
                        if multiattack_option_id
                        else {}
                    ),
                }
            )
            identities.add(identity)
        normalized[actor_id] = {
            "actor_id": actor_id,
            "choices": choices,
            "agent_ruling": {
                "default_resolver": "agent",
                "ruling_kind": "agent_dm_adjudication",
                "decision": decision,
                "reason": ruling_reason,
            },
        }
    return normalized


def _safe_single_target_spell_declaration(
    spell: dict[str, Any],
    *,
    target_id: str,
) -> dict[str, str] | None:
    """Return a complete declaration only when no unrecorded cover fact is needed."""

    resolution = dict(spell.get("resolution") or {})
    targeting = dict(resolution.get("targeting") or {})
    save = dict(resolution.get("save") or {})
    if (
        resolution.get("kind") != "saving_throw"
        or targeting.get("mode") != "creature"
        or int(targeting.get("max_targets", 1) or 1) != 1
        or (
            str(save.get("ability") or "") == "dexterity"
            and not bool(save.get("ignores_cover"))
        )
    ):
        return None
    return {"target_id": target_id}


def _agent_spell_priorities(
    declarations: list[dict[str, Any]],
    *,
    participant_ids: list[str],
    actors: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Validate explicit spell and targeting policies from the acting Agent."""

    participants = set(participant_ids)
    normalized: dict[str, dict[str, Any]] = {}
    allowed = {"actor_id", "choices", "decision", "ruling_reason"}
    choice_allowed = {"spell_id", "target_policy", "cast_level_policy"}
    target_policies = {
        "downed_ally",
        "prioritized_opponent",
        "maximize_opponents_without_allies",
    }
    for index, declaration in enumerate(declarations):
        if not isinstance(declaration, dict):
            raise ValueError(f"Agent spell priority {index} must be an object")
        unknown = set(declaration) - allowed
        if unknown:
            raise ValueError(
                f"Agent spell priority {index} has unsupported fields: "
                f"{', '.join(sorted(unknown))}"
            )
        actor_id = str(declaration.get("actor_id") or "").strip()
        raw_choices = declaration.get("choices")
        decision = " ".join(str(declaration.get("decision") or "").split())
        ruling_reason = " ".join(
            str(declaration.get("ruling_reason") or "").split()
        )
        if (
            actor_id not in participants
            or actor_id in normalized
            or not isinstance(raw_choices, list)
            or not raw_choices
            or not 10 <= len(decision) <= 500
            or not 10 <= len(ruling_reason) <= 500
        ):
            raise ValueError(
                f"Agent spell priority {index} requires one unique participant, "
                "ordered choices, decision, and ruling_reason"
            )
        actor = actors.get(actor_id)
        spell_cards = {
            str(item.get("id") or ""): dict(item)
            for item in (
                dict(dict(actor or {}).get("sheet") or {})
                .get("content", {})
                .get("spells", [])
            )
            if isinstance(item, dict) and str(item.get("id") or "")
        }
        choices: list[dict[str, str]] = []
        identities: set[tuple[str, str]] = set()
        for choice_index, raw_choice in enumerate(raw_choices):
            if not isinstance(raw_choice, dict):
                raise ValueError(
                    f"Agent spell priority choice {index}:{choice_index} "
                    "must be an object"
                )
            choice_unknown = set(raw_choice) - choice_allowed
            spell_id = str(raw_choice.get("spell_id") or "").strip()
            target_policy = (
                str(raw_choice.get("target_policy") or "").strip().casefold()
            )
            cast_level_policy = (
                str(
                    raw_choice.get("cast_level_policy")
                    or "lowest_available"
                )
                .strip()
                .casefold()
            )
            spell = spell_cards.get(spell_id)
            resolution = dict(effective_spell_resolution(dict(spell or {})) or {})
            targeting = dict(resolution.get("targeting") or {})
            area_targeting = targeting.get("mode") == "area"
            safe_single_target_save = (
                _safe_single_target_spell_declaration(
                    {
                        **dict(spell or {}),
                        **({"resolution": resolution} if resolution else {}),
                    },
                    target_id="validation-target",
                )
                is not None
            )
            identity = (spell_id, target_policy)
            compatible = (
                target_policy == "downed_ally"
                and (
                    spell_id == HEALING_WORD_ID
                    or resolution.get("kind") == "healing"
                )
            ) or (
                target_policy == "prioritized_opponent"
                and (
                    spell_id in {MAGIC_MISSILE_ID, GUIDING_BOLT_ID}
                    or safe_single_target_save
                )
            ) or (
                target_policy == "maximize_opponents_without_allies"
                and (
                    spell_id == HYPNOTIC_PATTERN_ID
                    or (
                        resolution.get("kind") == "saving_throw"
                        and area_targeting
                    )
                )
            )
            if (
                choice_unknown
                or spell is None
                or target_policy not in target_policies
                or cast_level_policy != "lowest_available"
                or not compatible
                or identity in identities
            ):
                raise ValueError(
                    f"Agent spell priority choice {index}:{choice_index} must "
                    "name one hydrated supported spell, a compatible explicit "
                    "target policy, and lowest_available cast-level policy"
                )
            choices.append(
                {
                    "spell_id": spell_id,
                    "target_policy": target_policy,
                    "cast_level_policy": cast_level_policy,
                }
            )
            identities.add(identity)
        normalized[actor_id] = {
            "actor_id": actor_id,
            "choices": choices,
            "agent_ruling": {
                "default_resolver": "agent",
                "ruling_kind": "agent_dm_adjudication",
                "decision": decision,
                "reason": ruling_reason,
            },
        }
    return normalized


def _agent_common_action_priorities(
    declarations: list[dict[str, Any]],
    *,
    participant_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Validate explicit, source-neutral Agent fallback actions."""

    participants = set(participant_ids)
    normalized: dict[str, dict[str, Any]] = {}
    allowed = {"actor_id", "choices", "decision", "ruling_reason"}
    choice_allowed = {"action"}
    for index, declaration in enumerate(declarations):
        if not isinstance(declaration, dict):
            raise ValueError(f"Agent common-action priority {index} must be an object")
        unknown = set(declaration) - allowed
        actor_id = str(declaration.get("actor_id") or "").strip()
        raw_choices = declaration.get("choices")
        decision = " ".join(str(declaration.get("decision") or "").split())
        ruling_reason = " ".join(
            str(declaration.get("ruling_reason") or "").split()
        )
        if (
            unknown
            or actor_id not in participants
            or actor_id in normalized
            or not isinstance(raw_choices, list)
            or not raw_choices
            or not 10 <= len(decision) <= 500
            or not 10 <= len(ruling_reason) <= 500
        ):
            raise ValueError(
                f"Agent common-action priority {index} requires one unique "
                "participant, ordered choices, decision, and ruling_reason"
            )
        choices: list[dict[str, str]] = []
        seen: set[str] = set()
        for choice_index, raw_choice in enumerate(raw_choices):
            if not isinstance(raw_choice, dict):
                raise ValueError(
                    f"Agent common-action priority choice {index}:{choice_index} "
                    "must be an object"
                )
            action = str(raw_choice.get("action") or "").strip().casefold()
            if (
                set(raw_choice) - choice_allowed
                or action != "dodge"
                or action in seen
            ):
                raise ValueError(
                    f"Agent common-action priority choice {index}:{choice_index} "
                    "must name the safe fallback action dodge exactly once"
                )
            choices.append({"action": action})
            seen.add(action)
        normalized[actor_id] = {
            "actor_id": actor_id,
            "choices": choices,
            "agent_ruling": {
                "default_resolver": "agent",
                "ruling_kind": "agent_dm_adjudication",
                "decision": decision,
                "reason": ruling_reason,
            },
        }
    return normalized


def _prioritize_targets(
    actor_id: str,
    target_ids: list[str],
    priorities_by_actor: dict[str, dict[str, Any]],
) -> list[str]:
    prioritized = list(target_ids)
    declaration = priorities_by_actor.get(actor_id)
    if declaration is None:
        return prioritized
    if declaration.get("default_resolver") == "agent":
        rank = {
            target_id: order
            for order, target_id in enumerate(
                target_id
                for group in declaration["priority_groups"]
                for target_id in group
            )
        }
        fallback_rank = sum(
            len(group) for group in declaration["priority_groups"]
        )
    else:
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
        "target_actor_ids",
        "willing_target_ids",
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
        target_actor_ids = raw.get("target_actor_ids")
        willing_target_ids = raw.get("willing_target_ids")
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
            or (
                target_actor_ids is not None
                and (
                    not isinstance(target_actor_ids, list)
                    or any(
                        not isinstance(item, str)
                        or not item.strip()
                        or item.strip() not in participant_ids
                        for item in target_actor_ids
                    )
                    or len(target_actor_ids)
                    != len({item.strip() for item in target_actor_ids})
                )
            )
            or (
                willing_target_ids is not None
                and (
                    not isinstance(willing_target_ids, list)
                    or any(
                        not isinstance(item, str) or not item.strip()
                        for item in willing_target_ids
                    )
                    or len(willing_target_ids)
                    != len({item.strip() for item in willing_target_ids})
                )
            )
            or (
                {item.strip() for item in (target_actor_ids or [])}
                != {item.strip() for item in (willing_target_ids or [])}
            )
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
                "target_actor_ids": [
                    item.strip() for item in (target_actor_ids or [])
                ],
                "willing_target_ids": [
                    item.strip() for item in (willing_target_ids or [])
                ],
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


def _source_ammunition_selections(
    values: list[dict[str, Any]],
    *,
    participant_ids: list[str],
    actors: dict[str, dict[str, Any]],
) -> dict[tuple[str, str], dict[str, str]]:
    normalized: dict[tuple[str, str], dict[str, str]] = {}
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise ValueError(f"source ammunition selection {index} must be an object")
        unknown = set(raw) - {"actor_id", "weapon_id", "ammunition_item_id"}
        if unknown:
            raise ValueError(
                f"source ammunition selection {index} has unsupported fields: "
                f"{', '.join(sorted(unknown))}"
            )
        value = {
            key: str(raw.get(key) or "").strip()
            for key in ("actor_id", "weapon_id", "ammunition_item_id")
        }
        identity = (value["actor_id"], value["weapon_id"])
        actor = actors.get(value["actor_id"])
        attacks = list(
            dict(dict(actor or {}).get("derived") or {})
            .get("inventory", {})
            .get("weapon_attacks", [])
        )
        weapon = next(
            (
                item
                for item in attacks
                if str(item.get("item_id") or "") == value["weapon_id"]
            ),
            None,
        )
        ammunition = next(
            (
                item
                for item in (
                    dict(dict(actor or {}).get("sheet") or {})
                    .get("inventory", {})
                    .get("items", [])
                )
                if str(item.get("id") or "") == value["ammunition_item_id"]
            ),
            None,
        )
        properties = {
            str(item).strip().casefold()
            for item in dict(weapon or {}).get("properties", [])
        }
        if (
            not all(value.values())
            or value["actor_id"] not in participant_ids
            or identity in normalized
            or weapon is None
            or "ammunition" not in properties
            or not isinstance(ammunition, dict)
            or ammunition.get("kind") != "ammunition"
            or int(ammunition.get("quantity", 0) or 0) < 1
            or not str(ammunition.get("source_key") or "").strip()
        ):
            raise ValueError(
                f"source ammunition selection {index} requires one unique "
                "participant/weapon pair, an ammunition weapon, and a remaining "
                "source-provenanced ammunition stack on that actor"
            )
        normalized[identity] = value
    return normalized


def _content_solutions(
    values: list[dict[str, Any]],
    *,
    participant_ids: list[str],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Validate one generic Agent-authored solution per portable source card."""

    normalized: dict[tuple[str, str, str], dict[str, Any]] = {}
    allowed_kinds = {
        "activity",
        "feature",
        "item",
        "monster_action",
        "spell",
        "trait",
    }
    allowed_fields = {
        "actor_id",
        "source_card_id",
        "source_card_kind",
        "resolution_plan",
        "compile_ruling",
        "bindings",
        "execution_ruling",
        "activations",
    }
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise ValueError(f"content solution {index} must be an object")
        unknown = set(raw) - allowed_fields
        actor_id = str(raw.get("actor_id") or "").strip()
        source_card_id = str(raw.get("source_card_id") or "").strip()
        source_card_kind = str(raw.get("source_card_kind") or "").strip()
        identity = (actor_id, source_card_id, source_card_kind)
        if unknown:
            raise ValueError(
                f"content solution {index} has unsupported fields: "
                f"{', '.join(sorted(unknown))}"
            )
        if (
            actor_id not in participant_ids
            or not source_card_id
            or source_card_kind not in allowed_kinds
            or identity in normalized
            or not isinstance(raw.get("resolution_plan"), dict)
            or not isinstance(raw.get("compile_ruling"), dict)
            or not isinstance(raw.get("bindings"), dict)
            or not isinstance(raw.get("execution_ruling"), dict)
        ):
            raise ValueError(
                f"content solution {index} requires one unique participant/source-card "
                "identity plus resolution_plan, compile_ruling, bindings, and "
                "execution_ruling objects"
            )
        raw_activations = raw.get("activations", [])
        if not isinstance(raw_activations, list):
            raise ValueError(f"content solution {index} activations must be a list")
        activations: list[dict[str, Any]] = []
        seen_rounds: set[int] = set()
        for activation_index, raw_activation in enumerate(raw_activations):
            if not isinstance(raw_activation, dict):
                raise ValueError(
                    f"content solution {index} activation {activation_index} must be an object"
                )
            unknown_activation_fields = set(raw_activation) - {
                "round",
                "cast_level",
                "bindings",
                "execution_ruling",
            }
            round_number = raw_activation.get("round")
            if (
                unknown_activation_fields
                or isinstance(round_number, bool)
                or not isinstance(round_number, int)
                or round_number < 1
                or round_number in seen_rounds
                or source_card_kind not in {
                    "activity",
                    "feature",
                    "monster_action",
                    "spell",
                    "trait",
                }
                or str(dict(raw.get("resolution_plan") or {}).get("trigger") or "")
                != "action"
            ):
                raise ValueError(
                    f"content solution {index} activation {activation_index} requires "
                    "one unique positive round on an action-triggered activity, feature, "
                    "monster_action, spell, or trait plan"
                )
            cast_level = raw_activation.get("cast_level")
            if cast_level is not None and (
                source_card_kind != "spell"
                or isinstance(cast_level, bool)
                or not isinstance(cast_level, int)
                or cast_level < 0
            ):
                raise ValueError(
                    f"content solution {index} activation {activation_index} cast_level "
                    "is allowed only as a non-negative integer for a spell"
                )
            activation_bindings = raw_activation.get("bindings", raw["bindings"])
            activation_ruling = raw_activation.get(
                "execution_ruling",
                raw["execution_ruling"],
            )
            if not isinstance(activation_bindings, dict) or not isinstance(
                activation_ruling,
                dict,
            ):
                raise ValueError(
                    f"content solution {index} activation {activation_index} bindings "
                    "and execution_ruling must be objects"
                )
            seen_rounds.add(round_number)
            activations.append(
                {
                    "round": round_number,
                    **({"cast_level": cast_level} if cast_level is not None else {}),
                    "bindings": deepcopy(activation_bindings),
                    "execution_ruling": deepcopy(activation_ruling),
                }
            )
        normalized[identity] = {
            "actor_id": actor_id,
            "source_card_id": source_card_id,
            "source_card_kind": source_card_kind,
            "resolution_plan": deepcopy(raw["resolution_plan"]),
            "compile_ruling": deepcopy(raw["compile_ruling"]),
            "bindings": deepcopy(raw["bindings"]),
            "execution_ruling": deepcopy(raw["execution_ruling"]),
            **({"activations": activations} if activations else {}),
        }
    scheduled_rounds: dict[tuple[str, int], tuple[str, str, str]] = {}
    for identity, solution in normalized.items():
        for activation in solution.get("activations", []):
            schedule_key = (str(solution["actor_id"]), int(activation["round"]))
            prior = scheduled_rounds.get(schedule_key)
            if prior is not None:
                raise ValueError(
                    "content solutions schedule multiple source-card actions for actor "
                    f"{schedule_key[0]} in round {schedule_key[1]}: {prior} and {identity}"
                )
            scheduled_rounds[schedule_key] = identity
    return normalized




def _agent_attack_contexts(
    values: list[dict[str, Any]],
    *,
    participant_ids: list[str],
    scene_id: str,
    encounter_source_excerpt: str,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Validate generic source-bound Agent rulings for attack-roll context."""

    normalized: dict[tuple[str, str, str], dict[str, Any]] = {}
    compact_encounter = " ".join(encounter_source_excerpt.split()).casefold()
    allowed = {
        "actor_id",
        "target_id",
        "attack_mode",
        "advantage",
        "disadvantage",
        "cover",
        "source_ref",
        "source_excerpt",
        "decision",
        "ruling_reason",
    }
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise ValueError(f"Agent attack context {index} must be an object")
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                f"Agent attack context {index} has unsupported fields: "
                f"{', '.join(sorted(unknown))}"
            )
        actor_id = str(raw.get("actor_id") or "").strip()
        target_id = str(raw.get("target_id") or "").strip()
        attack_mode = str(raw.get("attack_mode") or "").strip().casefold()
        source_ref = raw.get("source_ref")
        source_excerpt = " ".join(str(raw.get("source_excerpt") or "").split())
        decision = " ".join(str(raw.get("decision") or "").split())
        ruling_reason = " ".join(str(raw.get("ruling_reason") or "").split())
        advantage = raw.get("advantage")
        disadvantage = raw.get("disadvantage")
        cover = str(raw.get("cover") or "").strip().casefold().replace("-", "_")
        advantage_declared = "advantage" in raw or "disadvantage" in raw
        valid_advantage = (
            advantage_declared
            and isinstance(advantage, bool)
            and isinstance(disadvantage, bool)
            and advantage != disadvantage
        )
        valid_cover = cover in {"half", "three_quarters", "total"}
        identity = (actor_id, target_id, attack_mode)
        if (
            actor_id not in participant_ids
            or (
                target_id
                and (target_id not in participant_ids or target_id == actor_id)
            )
            or attack_mode not in ATTACK_MODES
            or identity in normalized
            or (advantage_declared and not valid_advantage)
            or (not valid_advantage and not valid_cover)
            or (bool(raw.get("cover")) and not valid_cover)
            or (valid_cover and not target_id)
            or not isinstance(source_ref, dict)
            or any(
                not str(source_ref.get(key) or "").strip()
                for key in ("module_id", "scene_id", "chunk_id", "content_sha256")
            )
            or str(source_ref.get("scene_id")) != scene_id
            or not source_excerpt
            or source_excerpt.casefold() not in compact_encounter
            or len(decision) < 10
            or len(ruling_reason) < 10
        ):
            raise ValueError(
                f"Agent attack context {index} requires one acting participant, "
                "an optional distinct target, one attack mode, an unambiguous "
                "advantage state and/or target-relative rules cover, a source_ref "
                "for the current scene, an exact encounter excerpt, and concrete "
                "Agent reasoning"
            )
        application_id = (
            "attack-context-"
            + _token(
                json.dumps(
                    {
                        "actor_id": actor_id,
                        "target_id": target_id,
                        "attack_mode": attack_mode,
                        "advantage": advantage if valid_advantage else None,
                        "disadvantage": disadvantage if valid_advantage else None,
                        "cover": cover,
                        "source_ref": source_ref,
                        "source_excerpt": source_excerpt,
                        "decision": decision,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                length=24,
            )
        )
        source_key = f"agent-ruling:{application_id}"
        agent_ruling = {
            "application_id": application_id,
            "default_resolver": "agent",
            "ruling_kind": "source_or_scene_fact",
            "decision": decision,
            "reason": ruling_reason,
            "source_ref": deepcopy(source_ref),
            "source_excerpt": source_excerpt,
        }
        context: dict[str, Any] = {"agent_ruling": deepcopy(agent_ruling)}
        if valid_advantage:
            context.update(
                {
                    "advantage": advantage,
                    "disadvantage": disadvantage,
                }
            )
            if advantage:
                context["advantage_sources"] = [source_key]
            else:
                context["disadvantage_sources"] = [source_key]
        if valid_cover:
            context["cover"] = {"degree": cover}
        normalized[identity] = {
            "application_id": application_id,
            "actor_id": actor_id,
            "target_id": target_id,
            "attack_mode": attack_mode,
            "cover": cover,
            "context": context,
            "agent_ruling": agent_ruling,
        }
    return normalized


def _agent_target_reaction_contexts(
    values: list[dict[str, Any]],
    *,
    participant_ids: list[str],
    scene_id: str,
    encounter_source_excerpt: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Validate Agent rulings that a targeted actor may take as a reaction."""

    normalized: dict[tuple[str, str], dict[str, Any]] = {}
    compact_encounter = " ".join(encounter_source_excerpt.split()).casefold()
    allowed = {
        "actor_id",
        "attack_mode",
        "advantage",
        "disadvantage",
        "source_ref",
        "source_excerpt",
        "decision",
        "ruling_reason",
    }
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise ValueError(f"Agent target reaction context {index} must be an object")
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                f"Agent target reaction context {index} has unsupported fields: "
                f"{', '.join(sorted(unknown))}"
            )
        actor_id = str(raw.get("actor_id") or "").strip()
        attack_mode = str(raw.get("attack_mode") or "").strip().casefold()
        source_ref = raw.get("source_ref")
        source_excerpt = " ".join(str(raw.get("source_excerpt") or "").split())
        decision = " ".join(str(raw.get("decision") or "").split())
        ruling_reason = " ".join(str(raw.get("ruling_reason") or "").split())
        advantage = raw.get("advantage")
        disadvantage = raw.get("disadvantage")
        identity = (actor_id, attack_mode)
        if (
            actor_id not in participant_ids
            or attack_mode not in ATTACK_MODES
            or identity in normalized
            or not isinstance(advantage, bool)
            or not isinstance(disadvantage, bool)
            or advantage == disadvantage
            or not isinstance(source_ref, dict)
            or any(
                not str(source_ref.get(key) or "").strip()
                for key in ("module_id", "scene_id", "chunk_id", "content_sha256")
            )
            or str(source_ref.get("scene_id")) != scene_id
            or not source_excerpt
            or source_excerpt.casefold() not in compact_encounter
            or len(decision) < 10
            or len(ruling_reason) < 10
        ):
            raise ValueError(
                f"Agent target reaction context {index} requires one reacting "
                "participant and triggering attack mode, exactly one true advantage "
                "state, a source_ref for the current scene, an exact encounter "
                "excerpt, and concrete Agent reasoning"
            )
        application_id = (
            "target-reaction-context-"
            + _token(
                json.dumps(
                    {
                        "actor_id": actor_id,
                        "attack_mode": attack_mode,
                        "source_ref": source_ref,
                        "source_excerpt": source_excerpt,
                        "decision": decision,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                length=24,
            )
        )
        source_key = f"agent-ruling:{application_id}"
        context: dict[str, Any] = {}
        if advantage:
            context.update(
                {
                    "advantage": True,
                    "advantage_sources": [source_key],
                }
            )
        else:
            context.update(
                {
                    "disadvantage": True,
                    "disadvantage_sources": [source_key],
                }
            )
        normalized[identity] = {
            "application_id": application_id,
            "actor_id": actor_id,
            "attack_mode": attack_mode,
            "context": context,
            "agent_ruling": {
                "default_resolver": "agent",
                "ruling_kind": "agent_dm_adjudication",
                "decision": decision,
                "reason": ruling_reason,
                "source_ref": deepcopy(source_ref),
                "source_excerpt": source_excerpt,
            },
        }
    return normalized


def _agent_casting_perception_rulings(
    declarations: list[dict[str, Any]],
    *,
    participant_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Validate explicit hidden-casting perception decisions from the DM Agent."""

    participants = set(participant_ids)
    normalized: dict[str, dict[str, Any]] = {}
    allowed = {
        "caster_id",
        "observations",
        "decision",
        "ruling_reason",
    }
    observation_allowed = {"observer_id", "perceived", "reason"}
    for index, declaration in enumerate(declarations):
        if not isinstance(declaration, dict):
            raise ValueError(
                f"Agent casting-perception ruling {index} must be an object"
            )
        unknown = set(declaration) - allowed
        if unknown:
            raise ValueError(
                f"Agent casting-perception ruling {index} has unsupported fields: "
                f"{', '.join(sorted(unknown))}"
            )
        caster_id = str(declaration.get("caster_id") or "").strip()
        raw_observations = declaration.get("observations")
        decision = " ".join(str(declaration.get("decision") or "").split())
        ruling_reason = " ".join(
            str(declaration.get("ruling_reason") or "").split()
        )
        if (
            caster_id not in participants
            or caster_id in normalized
            or not isinstance(raw_observations, list)
            or not raw_observations
            or not 10 <= len(decision) <= 500
            or not 10 <= len(ruling_reason) <= 500
        ):
            raise ValueError(
                f"Agent casting-perception ruling {index} requires one unique "
                "participant caster, observations, decision, and ruling_reason"
            )
        observations: list[dict[str, Any]] = []
        observer_ids: set[str] = set()
        for observation_index, raw_observation in enumerate(raw_observations):
            if not isinstance(raw_observation, dict):
                raise ValueError(
                    "Agent casting-perception observation "
                    f"{index}:{observation_index} must be an object"
                )
            observation_unknown = set(raw_observation) - observation_allowed
            observer_id = str(raw_observation.get("observer_id") or "").strip()
            perceived = raw_observation.get("perceived")
            reason = " ".join(str(raw_observation.get("reason") or "").split())
            if (
                observation_unknown
                or observer_id not in participants
                or observer_id == caster_id
                or observer_id in observer_ids
                or not isinstance(perceived, bool)
                or not 10 <= len(reason) <= 500
            ):
                raise ValueError(
                    "Agent casting-perception observation "
                    f"{index}:{observation_index} requires one distinct participant "
                    "observer, a boolean perceived decision, and a bounded reason"
                )
            observations.append(
                {
                    "observer_id": observer_id,
                    "perceived": perceived,
                    "reason": reason,
                }
            )
            observer_ids.add(observer_id)
        normalized[caster_id] = {
            "caster_id": caster_id,
            "component_ruling": {"casting_perception": observations},
            "agent_ruling": {
                "default_resolver": "agent",
                "ruling_kind": "agent_dm_adjudication",
                "decision": decision,
                "reason": ruling_reason,
            },
        }
    return normalized


def _agent_turn_rulings(
    values: list[dict[str, Any]],
    *,
    participant_ids: list[str],
    actors: dict[str, dict[str, Any]],
    scene_id: str,
    encounter_source_excerpt: str,
    ruleset: str = "2014",
) -> dict[tuple[str, int], dict[str, Any]]:
    """Validate source-cited scene procedures settled by Agent-as-DM reasoning.

    Actor-card activities, spells, traits, and monster actions deliberately do
    not enter this path; they compile and persist an exact-card content solution.
    """

    del actors
    check_actions_by_ruleset = {
        "2014": {"escape", "hide", "improvise", "search", "use_object"},
        "2024": {"escape", "hide", "influence", "search", "study", "utilize"},
    }
    normalized_ruleset = str(ruleset).strip()
    if normalized_ruleset not in check_actions_by_ruleset:
        raise ValueError(f"unsupported encounter ruleset: {ruleset}")
    allowed = {
        "actor_id",
        "procedure_id",
        "round",
        "source_ref",
        "procedure_source_excerpt",
        "encounter_source_excerpt",
        "decision",
        "ruling_reason",
        "target_id",
        "target_ids",
        "save_ability",
        "save_dc",
        "save_advantage",
        "save_disadvantage",
        "check_ability",
        "check_dc",
        "check_action",
        "check_advantage",
        "check_disadvantage",
        "success_outcome",
        "failure_outcome",
        "success_combat_outcome",
        "forced_target_id",
        "ends_if_source_incapacitated",
        "damage_expression",
        "damage_type",
        "half_on_success",
    }
    ability_labels = {
        "strength": ("strength", "str"),
        "dexterity": ("dexterity", "dex"),
        "constitution": ("constitution", "con"),
        "intelligence": ("intelligence", "int"),
        "wisdom": ("wisdom", "wis"),
        "charisma": ("charisma", "cha"),
    }
    participant_set = set(participant_ids)
    compact_scene = _normalized_source_text(encounter_source_excerpt)
    normalized: dict[tuple[str, int], dict[str, Any]] = {}
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise ValueError(f"Agent turn ruling {index} must be an object")
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                f"Agent turn ruling {index} has unsupported fields: "
                f"{', '.join(sorted(unknown))}"
            )
        actor_id = str(raw.get("actor_id") or "").strip()
        procedure_id = str(raw.get("procedure_id") or "").strip()
        round_number = int(raw.get("round", 0) or 0)
        source_ref = raw.get("source_ref")
        procedure_excerpt = " ".join(
            str(raw.get("procedure_source_excerpt") or "").split()
        )
        encounter_excerpt = " ".join(
            str(raw.get("encounter_source_excerpt") or "").split()
        )
        decision = " ".join(str(raw.get("decision") or "").split())
        reason = " ".join(str(raw.get("ruling_reason") or "").split())
        target_id = str(raw.get("target_id") or "").strip()
        raw_target_ids = raw.get("target_ids")
        target_ids = (
            [str(item).strip() for item in raw_target_ids]
            if isinstance(raw_target_ids, list)
            else []
        )
        save_ability = str(raw.get("save_ability") or "").strip().casefold()
        save_dc = int(raw.get("save_dc", 0) or 0)
        save_advantage = raw.get("save_advantage", False)
        save_disadvantage = raw.get("save_disadvantage", False)
        check_ability = str(raw.get("check_ability") or "").strip().casefold()
        check_dc = int(raw.get("check_dc", 0) or 0)
        check_action = str(raw.get("check_action") or "").strip().casefold().replace("-", "_")
        check_advantage = raw.get("check_advantage", False)
        check_disadvantage = raw.get("check_disadvantage", False)
        success_outcome = " ".join(str(raw.get("success_outcome") or "").split())
        failure_outcome = " ".join(str(raw.get("failure_outcome") or "").split())
        forced_target_id = str(raw.get("forced_target_id") or "").strip()
        ends_if_source_incapacitated = raw.get("ends_if_source_incapacitated", False)
        damage_expression = "".join(str(raw.get("damage_expression") or "").split()).casefold()
        damage_type = str(raw.get("damage_type") or "").strip().casefold()
        half_on_success = raw.get("half_on_success")
        raw_combat_outcome = raw.get("success_combat_outcome")
        combat_outcome = (
            {
                "status": str(dict(raw_combat_outcome).get("status") or "").strip().casefold(),
                "summary": " ".join(
                    str(dict(raw_combat_outcome).get("summary") or "").split()
                ),
            }
            if isinstance(raw_combat_outcome, dict)
            else None
        )
        save_target_ids = target_ids or ([target_id] if target_id else [])
        has_save = bool(save_target_ids or save_ability or save_dc)
        has_check = bool(check_ability or check_dc or check_action)
        has_damage = bool(damage_expression or damage_type or half_on_success is not None)
        identity = (actor_id, round_number)
        invalid = (
            actor_id not in participant_set
            or round_number <= 0
            or identity in normalized
            or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", procedure_id)
            or not isinstance(source_ref, dict)
            or any(
                not str(source_ref.get(key) or "").strip()
                for key in ("module_id", "scene_id", "chunk_id", "content_sha256")
            )
            or str(source_ref.get("scene_id")) != scene_id
            or not procedure_excerpt
            or _normalized_source_text(procedure_excerpt)
            not in _normalized_source_text(encounter_excerpt)
            or not encounter_excerpt
            or _normalized_source_text(encounter_excerpt) not in compact_scene
            or len(decision) < 10
            or len(reason) < 10
            or raw_target_ids is not None
            and not isinstance(raw_target_ids, list)
            or bool(target_id) and bool(target_ids)
            or len(target_ids) != len(set(target_ids))
            or any(item not in participant_set or item == actor_id for item in target_ids)
            or has_save and has_check
            or not all(
                isinstance(item, bool)
                for item in (
                    save_advantage,
                    save_disadvantage,
                    check_advantage,
                    check_disadvantage,
                    ends_if_source_incapacitated,
                )
            )
            or save_advantage and save_disadvantage
            or check_advantage and check_disadvantage
        )
        if has_save:
            invalid = invalid or (
                not save_target_ids
                or any(item not in participant_set or item == actor_id for item in save_target_ids)
                or save_ability not in ability_labels
                or not 1 <= save_dc <= 40
                or not success_outcome
                or not failure_outcome
            )
        if has_check:
            if check_action not in check_actions_by_ruleset[normalized_ruleset]:
                raise ValueError(
                    f"Agent turn ruling {index} check_action={check_action!r} is "
                    f"not a legal {normalized_ruleset} action primitive"
                )
            invalid = invalid or (
                not check_ability
                or not 1 <= check_dc <= 40
                or not success_outcome
                or not failure_outcome
                or bool(target_id)
                or bool(target_ids)
            )
        if raw_combat_outcome is not None:
            invalid = invalid or (
                not has_check
                or not isinstance(raw_combat_outcome, dict)
                or set(raw_combat_outcome) != {"status", "summary"}
                or combat_outcome is None
                or combat_outcome["status"] not in COMBAT_OUTCOME_STATUSES
                or not combat_outcome["summary"]
                or len(combat_outcome["summary"]) > 2000
            )
        if not has_save and not has_check:
            invalid = invalid or bool(success_outcome or failure_outcome or forced_target_id)
        invalid = invalid or bool(forced_target_id and forced_target_id not in participant_set)
        if has_damage:
            invalid = invalid or (
                not target_ids
                or not has_save
                or re.fullmatch(r"[1-9]\d*d[1-9]\d*(?:[+-]\d+)?", damage_expression) is None
                or not damage_type
                or not isinstance(half_on_success, bool)
                or bool(forced_target_id)
            )
        if invalid:
            raise ValueError(
                f"Agent turn ruling {index} requires one source-cited scene procedure, "
                "a current-scene source_ref and exact excerpts, concrete Agent "
                "reasoning, and a complete optional server check or save contract"
            )
        if has_check:
            printed_check = re.search(
                rf"(?is)\bDC\s*{check_dc}\b[^.]*\b{re.escape(check_ability)}\b[^.]*\bcheck\b",
                procedure_excerpt,
            )
            if (
                printed_check is None
                or check_advantage and re.search(r"(?i)\badvantage\b", procedure_excerpt) is None
                or check_disadvantage
                and re.search(r"(?i)\bdisadvantage\b", procedure_excerpt) is None
            ):
                raise ValueError(
                    f"Agent turn ruling {index} check must match the source-cited procedure"
                )
        if has_save:
            labels = "|".join(re.escape(item) for item in ability_labels[save_ability])
            if re.search(
                rf"(?i)\bDC\s*{save_dc}\s+(?:{labels})\s+saving throw\b",
                procedure_excerpt,
            ) is None:
                raise ValueError(
                    f"Agent turn ruling {index} save must match the source-cited procedure"
                )
        if has_damage:
            printed_half = re.search(
                r"(?i)\bhalf\s+(?:as\s+much|the)\s+damage\b.*\b(?:successful|success)\b",
                procedure_excerpt,
            )
            if (
                damage_expression not in "".join(procedure_excerpt.split()).casefold()
                or re.search(rf"\b{re.escape(damage_type)}\b", procedure_excerpt.casefold()) is None
                or bool(printed_half) != half_on_success
            ):
                raise ValueError(
                    f"Agent turn ruling {index} damage must match the source-cited procedure"
                )
        application_id = "turn-ruling-" + _token(
            json.dumps(
                {
                    "actor_id": actor_id,
                    "procedure_id": procedure_id,
                    "round": round_number,
                    "source_ref": source_ref,
                    "procedure_source_excerpt": procedure_excerpt,
                    "encounter_source_excerpt": encounter_excerpt,
                    "decision": decision,
                    "target_id": target_id,
                    "target_ids": target_ids,
                    "check_ability": check_ability,
                    "check_dc": check_dc,
                    "check_action": check_action,
                    "damage_expression": damage_expression,
                    "damage_type": damage_type,
                    "half_on_success": half_on_success,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            length=24,
        )
        normalized[identity] = {
            "application_id": application_id,
            "actor_id": actor_id,
            "feature_id": "",
            "activity_id": "",
            "spell_id": "",
            "procedure_id": procedure_id,
            "mechanic_source_excerpt": procedure_excerpt,
            "spell_payment_economies": [],
            "concentration_required": False,
            "round": round_number,
            "target_id": target_id,
            "target_ids": target_ids,
            "check": {
                "ability": check_ability,
                "dc": check_dc,
                "action": check_action,
                "advantage": check_advantage,
                "disadvantage": check_disadvantage,
                "success_outcome": success_outcome,
                "failure_outcome": failure_outcome,
                "success_combat_outcome": deepcopy(combat_outcome),
            }
            if has_check
            else None,
            "save": {
                "ability": save_ability,
                "dc": save_dc,
                "advantage": save_advantage,
                "disadvantage": save_disadvantage,
                "success_outcome": success_outcome,
                "failure_outcome": failure_outcome,
                "forced_target_id": forced_target_id,
                "ends_if_source_incapacitated": ends_if_source_incapacitated,
                "damage": {
                    "expression": damage_expression,
                    "damage_type": damage_type,
                    "half_on_success": half_on_success,
                }
                if has_damage
                else None,
            }
            if has_save
            else None,
            "agent_ruling": {
                "application_id": application_id,
                "default_resolver": "agent",
                "ruling_kind": "agent_dm_adjudication",
                "decision": decision,
                "reason": reason,
                "source_ref": deepcopy(source_ref),
                "actor_source_excerpt": "",
                "procedure_source_excerpt": procedure_excerpt,
                "encounter_source_excerpt": encounter_excerpt,
            },
        }
    return normalized






def _agent_object_interactions(
    values: list[dict[str, Any]],
    *,
    participant_ids: list[str],
    source_conditions: list[dict[str, Any]],
) -> dict[tuple[str, int], dict[str, Any]]:
    """Validate Agent decisions that remove a source condition with a free interaction."""

    participants = set(participant_ids)
    allowed = {
        "actor_id",
        "round",
        "object_description",
        "interaction",
        "condition",
        "source_ref",
        "source_excerpt",
        "decision",
        "ruling_reason",
    }
    normalized: dict[tuple[str, int], dict[str, Any]] = {}
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise ValueError(f"Agent object interaction {index} must be an object")
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                f"Agent object interaction {index} has unsupported fields: "
                + ", ".join(sorted(unknown))
            )
        actor_id = str(raw.get("actor_id") or "").strip()
        round_number = raw.get("round")
        object_description = " ".join(
            str(raw.get("object_description") or "").split()
        )
        interaction = str(raw.get("interaction") or "").strip().casefold()
        condition = str(raw.get("condition") or "").strip().casefold()
        source_ref = raw.get("source_ref")
        source_excerpt = _normalized_source_text(
            str(raw.get("source_excerpt") or "")
        )
        decision = " ".join(str(raw.get("decision") or "").split())
        ruling_reason = " ".join(str(raw.get("ruling_reason") or "").split())
        identity = (actor_id, round_number if isinstance(round_number, int) else 0)
        if (
            actor_id not in participants
            or isinstance(round_number, bool)
            or not isinstance(round_number, int)
            or round_number < 1
            or not object_description
            or interaction != "remove"
            or not condition
            or not isinstance(source_ref, dict)
            or not source_excerpt
            or not decision
            or len(decision) > 1_000
            or not ruling_reason
            or len(ruling_reason) > 500
            or identity in normalized
        ):
            raise ValueError(
                f"Agent object interaction {index} requires one participant, "
                "positive round, removal description, exact source evidence, "
                "and bounded Agent reasoning"
            )
        source_condition = next(
            (
                item
                for item in source_conditions
                if isinstance(item, dict)
                and str(item.get("actor_id") or "") == actor_id
                and str(item.get("condition") or "").casefold() == condition
                and item.get("source_ref") == source_ref
                and _normalized_source_text(
                    str(item.get("source_excerpt") or "")
                )
                == source_excerpt
            ),
            None,
        )
        if source_condition is None:
            raise ValueError(
                f"Agent object interaction {index} does not match an exact "
                "encounter-source condition for the actor"
            )
        normalized[identity] = {
            "actor_id": actor_id,
            "round": round_number,
            "object_description": object_description,
            "interaction": "remove",
            "condition": condition,
            "source_ref": deepcopy(source_ref),
            "source_excerpt": str(source_condition["source_excerpt"]),
            "agent_ruling": {
                "default_resolver": "agent",
                "ruling_kind": "agent_dm_adjudication",
                "decision": decision,
                "reason": ruling_reason,
            },
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
            or event.get("event_type")
            not in {
                "movement_hazard_marked",
                "trap_detected",
                "trap_locations_shared",
            }
            or str(payload.get("scene_id") or "") != scene_id
            or not str(event.get("id") or "")
            or not source_excerpt
            or not cells
        ):
            raise ValueError(
                f"source avoidance report {index} must be a passed public "
                "hazard-knowledge event for this campaign and scene with marked cells"
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








async def _campaign(client: ExposureClient, campaign_id: str) -> dict[str, Any]:
    return await campaign_view(client, campaign_id)


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
        "agent_ruling_features": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "description": item.get("description"),
                "manual_ruling": deepcopy(
                    dict(dict(item.get("choices") or {}).get("manual_ruling") or {})
                ),
            }
            for item in dict(sheet.get("content") or {}).get("features", [])
            if isinstance(item, dict)
            and dict(dict(item.get("choices") or {}).get("manual_ruling") or {}).get(
                "default_resolver"
            )
            == "agent"
        ],
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
        if slot in WEAPON_HAND_SLOTS and str(item.get("kind") or "") != "weapon":
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
    reinforcement_ally_ids: list[str],
) -> dict[str, Any]:
    if not args.scene_id:
        raise ValueError("encounter start requires --scene-id")
    opened_play = await client.open(args.campaign_id)
    await client.load()
    campaign = await _campaign(client, args.campaign_id)
    phase = str(campaign.get("effective_game_phase") or "")
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
        reinforcement_ally_ids=reinforcement_ally_ids,
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
    all_party_ids = [*party_ids, *reinforcement_ally_ids]
    all_participant_ids = [*all_party_ids, *all_hostile_ids]
    _validate_source_flee_configuration(
        args,
        hostile_ids=all_hostile_ids,
    )
    agent_reinforcement_triggers = _agent_reinforcement_triggers(
        getattr(args, "agent_reinforcement_trigger_json", []),
        reinforcement_ids=[
            *reinforcement_hostile_ids,
            *reinforcement_ally_ids,
        ],
        reinforcement_round=int(args.reinforcement_round or 0),
        encounter_source_excerpt=str(args.source_excerpt or ""),
    )
    source_target_priorities = _source_target_priorities(
        args.source_target_priority_json,
        participant_ids=all_participant_ids,
        encounter_source_excerpt=str(args.source_excerpt or ""),
    )
    agent_target_priorities = _agent_target_priorities(
        getattr(args, "agent_target_priority_json", []),
        party_ids=all_party_ids,
        hostile_ids=all_hostile_ids,
    )
    _validate_agent_target_refinements(
        source_target_priorities,
        agent_target_priorities,
    )
    source_conditions_by_actor = _source_declared_conditions(
        args.source_condition_json,
        participant_ids=all_participant_ids,
    )
    actors = await _characters(
        client,
        args.campaign_id,
        all_participant_ids,
    )
    agent_weapon_priorities = _agent_weapon_priorities(
        getattr(args, "agent_weapon_priority_json", []),
        participant_ids=all_participant_ids,
        actors=actors,
    )
    agent_spell_priorities = _agent_spell_priorities(
        getattr(args, "agent_spell_priority_json", []),
        participant_ids=all_participant_ids,
        actors=actors,
    )
    agent_common_action_priorities = _agent_common_action_priorities(
        getattr(args, "agent_common_action_priority_json", []),
        participant_ids=all_participant_ids,
    )
    opening_weapons = _source_opening_weapons(
        args.source_opening_weapon_json,
        participant_ids=all_participant_ids,
    )
    delayed_actions = _source_delayed_actions(
        args.source_delayed_action_json,
        participant_ids=initial_hostile_ids,
    )
    passive_allies = _source_passive_allies(
        args.source_passive_ally_json,
        ally_ids=ally_ids,
    )
    content_solutions = _content_solutions(
        getattr(args, "content_solution_json", []),
        participant_ids=all_participant_ids,
    )
    agent_attack_contexts = _agent_attack_contexts(
        args.agent_attack_context_json,
        participant_ids=all_participant_ids,
        scene_id=str(args.scene_id or ""),
        encounter_source_excerpt=str(args.source_excerpt or ""),
    )
    agent_casting_perception_rulings = _agent_casting_perception_rulings(
        getattr(args, "agent_casting_perception_json", []),
        participant_ids=all_participant_ids,
    )
    agent_target_reaction_contexts = _agent_target_reaction_contexts(
        getattr(args, "agent_target_reaction_context_json", []),
        participant_ids=all_participant_ids,
        scene_id=str(args.scene_id or ""),
        encounter_source_excerpt=str(args.source_excerpt or ""),
    )
    agent_turn_rulings = _agent_turn_rulings(
        getattr(args, "agent_turn_ruling_json", []),
        participant_ids=all_participant_ids,
        actors=actors,
        scene_id=str(args.scene_id or ""),
        encounter_source_excerpt=str(args.source_excerpt or ""),
        ruleset="2014",
    )
    agent_object_interactions = _agent_object_interactions(
        getattr(args, "agent_object_interaction_json", []),
        participant_ids=all_participant_ids,
        source_conditions=[
            {"actor_id": actor_id, **deepcopy(item)}
            for actor_id, conditions in source_conditions_by_actor.items()
            for item in conditions
        ],
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
        participant_ids=all_participant_ids,
    )
    participant_manifest = _participant_manifest(
        hostile_ids,
        label=args.hostile_label,
        source_excerpt=_primary_hostile_source_excerpt(args),
        additional_hostile_ids=additional_hostile_ids,
        additional_label=args.additional_hostile_label,
        additional_source_excerpt=str(args.additional_hostile_source_excerpt or ""),
        reinforcement_hostile_ids=reinforcement_hostile_ids,
        reinforcement_label=args.reinforcement_hostile_label,
        reinforcement_source_excerpt=str(args.reinforcement_hostile_source_excerpt or ""),
        reinforcement_ally_ids=reinforcement_ally_ids,
        reinforcement_ally_label=str(
            getattr(args, "reinforcement_ally_label", "")
        ),
        reinforcement_ally_source_excerpt=str(
            getattr(args, "reinforcement_ally_source_excerpt", "") or ""
        ),
    )
    encounter_preflight = await _require_encounter_preflight(
        client,
        campaign_id=args.campaign_id,
        scene_id=args.scene_id,
        participant_manifest=participant_manifest,
    )
    source_ammunition_selections = _source_ammunition_selections(
        args.source_ammunition_json,
        participant_ids=all_participant_ids,
        actors=actors,
    )
    for actor_id in set(all_hostile_ids) | {
        actor_id for actor_id, _ in source_ammunition_selections
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
    precombat_loadouts, actors = await _apply_party_loadouts(
        client,
        args,
        party_ids=pc_ids,
        actors=actors,
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
        if cast["target_actor_ids"]:
            cast_payload["target_character_ids"] = cast[
                "target_actor_ids"
            ]
            cast_payload["willing_target_ids"] = cast[
                "willing_target_ids"
            ]
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
    source_surprise_report = getattr(args, "source_surprise_report", None)
    if source_surprise_report is not None and not args.source_surprised_actor_id:
        raise ValueError(
            "--source-surprise-report requires --source-surprised-actor-id"
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
        source_surprise_evidence = (
            _source_surprise_evidence_from_report(
                source_surprise_report,
                campaign_id=args.campaign_id,
            )
            if source_surprise_report is not None
            else None
        )
        surprise, surprise_basis = _source_declared_surprise(
            party_ids=party_ids,
            hostile_ids=initial_hostile_ids,
            surprised_actor_ids=args.source_surprised_actor_id,
            source_excerpt=(
                str(source_surprise_evidence["source_excerpt"])
                if source_surprise_evidence is not None
                else str(args.source_excerpt or "")
            ),
            source_evidence=source_surprise_evidence,
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
            source_separations=source_separations,
            agent_positions=agent_positions,
        ),
        "participant_manifest": participant_manifest,
        "name": args.encounter_name,
        "scene_id": args.scene_id,
        "battle_map": _encounter_battle_map_request(args.location_key),
        "ruleset": "2014",
        "branch_id": branch["id"],
        "expected_revision": expected_revision,
    }
    start_request["idempotency_key"] = _encounter_start_operation_token(start_request)
    started = await client.domain("combat_start", start_request)
    _require_committed_encounter_start(started)
    started["participant_preflight"] = encounter_preflight
    opened_combat = await client.open(args.campaign_id)
    await client.load()
    reinforcement_queue: list[dict[str, Any]] = []
    agent_reinforcement_initiative_rulings: list[dict[str, Any]] = []
    reinforcements = [
        *((actor_id, "hostile") for actor_id in reinforcement_hostile_ids),
        *((actor_id, "friendly") for actor_id in reinforcement_ally_ids),
    ]
    for index, (actor_id, disposition) in enumerate(reinforcements):
        campaign = await _campaign(client, args.campaign_id)
        tie_breaker = len(party_ids) + len(initial_hostile_ids) + index
        agent_reinforcement_initiative_rulings.append(
            {
                "actor_id": actor_id,
                "tie_breaker": tie_breaker,
                "ruling_reason": (
                    "The Agent places a late-arriving reinforcement after every already "
                    "ordered participant with the same rolled initiative; this "
                    "preselects only the DM-owned tie and does not replace the "
                    "server initiative roll."
                ),
            }
        )
        reinforcement_queue.append(
            await client.domain(
                "combat_join",
                {
                    "campaign_id": args.campaign_id,
                    "actor_id": actor_id,
                    "participant_config": _reinforcement_config(
                        actor_id,
                        index,
                        disposition=disposition,
                        join_round=int(args.reinforcement_round or 0),
                        tie_breaker=tie_breaker,
                        source_conditions=source_conditions_by_actor.get(actor_id),
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
        "agent_weapon_priorities": list(agent_weapon_priorities.values()),
        "agent_spell_priorities": list(agent_spell_priorities.values()),
        "agent_common_action_priorities": list(
            agent_common_action_priorities.values()
        ),
        "source_precombat_casts": precombat_cast_results,
        "source_opening_weapons": list(opening_weapons.values()),
        "source_ammunition_selections": list(source_ammunition_selections.values()),
        "source_delayed_actions": list(delayed_actions.values()),
        "source_passive_allies": list(passive_allies.values()),
        "content_solutions": list(content_solutions.values()),
        "agent_attack_contexts": list(agent_attack_contexts.values()),
        "agent_casting_perception_rulings": list(
            agent_casting_perception_rulings.values()
        ),
        "agent_target_reaction_contexts": list(
            agent_target_reaction_contexts.values()
        ),
        "agent_turn_rulings": list(agent_turn_rulings.values()),
        "agent_object_interactions": list(agent_object_interactions.values()),
        "source_separations": list(source_separations.values()),
        "agent_positions": list(agent_positions.values()),
        "source_avoidances": source_avoidance_evidence,
        "precombat_loadouts": precombat_loadouts,
        "source_opening_casts": _source_opening_casts(
            args.source_opening_cast_json,
            participant_ids=[*party_ids, *all_hostile_ids],
        ),
        "agent_reinforcement_initiative_rulings": (
            agent_reinforcement_initiative_rulings
        ),
        "agent_reinforcement_triggers": agent_reinforcement_triggers,
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


def _knockout_objective(
    args: argparse.Namespace,
    *,
    hostile_ids: list[str],
) -> tuple[set[str], int | None]:
    requested_values = list(getattr(args, "knock_out_hostile_id", []) or [])
    requested = {
        str(actor_id).strip() for actor_id in requested_values if str(actor_id).strip()
    }
    if len(requested) != len(requested_values) or not requested <= set(hostile_ids):
        raise ValueError("knockout targets must be distinct encounter hostiles")
    minimum = getattr(args, "minimum_hostile_knockouts", None)
    if minimum is None:
        return requested, None
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
        raise ValueError("--minimum-hostile-knockouts must be a non-negative integer")
    candidates = requested or set(hostile_ids)
    if minimum > len(candidates):
        raise ValueError(
            "--minimum-hostile-knockouts cannot exceed the eligible hostile count"
        )
    return candidates, minimum


def _captured_hostile_ids(
    actors: dict[str, dict[str, Any]],
    *,
    candidate_ids: set[str],
) -> set[str]:
    captured: set[str] = set()
    for actor_id in candidate_ids:
        actor = actors[actor_id]
        conditions = _conditions(actor)
        if (
            _hit_points(actor) == 0
            and "unconscious" in conditions
            and "dead" not in conditions
        ):
            captured.add(actor_id)
    return captured


def _should_stand(actor: dict[str, Any], available_actions: set[str]) -> bool:
    return _hit_points(actor) > 0 and "prone" in _conditions(actor) and "move" in available_actions


def _area_spell_declaration(
    spell: dict[str, Any],
    *,
    actor_id: str,
    party_ids: list[str],
    living_targets: list[str],
    actors: dict[str, dict[str, Any]],
    combat: dict[str, Any],
) -> dict[str, Any] | None:
    """Choose a complete, observable area that hits multiple foes and no allies."""

    spell_id = str(spell.get("id") or "")
    resolution = dict(effective_spell_resolution(spell) or {})
    targeting = dict(resolution.get("targeting") or {})
    area = dict(targeting.get("area") or {})
    hypnotic_pattern = spell_id == HYPNOTIC_PATTERN_ID
    if not hypnotic_pattern and (
        resolution.get("kind") != "saving_throw"
        or targeting.get("mode") != "area"
        or not dict(resolution.get("save") or {}).get("damage")
    ):
        return None
    shape = str(area.get("shape") or "")
    radius_ft = int(area.get("radius_ft", 0) or 0)
    length_ft = int(area.get("length_ft", 0) or 0)
    width_ft = int(area.get("width_ft", 0) or 0)
    range_ft = int(
        dict(dict(spell.get("definition") or {}).get("range") or {}).get(
            "normal_ft", 0
        )
        or 0
    )
    if (
        not hypnotic_pattern
        and shape == "sphere"
        and (radius_ft <= 0 or range_ft <= 0)
    ):
        return None
    if (
        not hypnotic_pattern
        and shape == "line"
        and (length_ft <= 0 or width_ft <= 0)
    ):
        return None
    if not hypnotic_pattern and shape not in {"sphere", "line"}:
        return None
    combatants = {
        str(item.get("actor_id") or ""): dict(item)
        for item in combat.get("combatants", [])
        if isinstance(item, dict) and str(item.get("actor_id") or "")
    }
    caster = combatants.get(actor_id)
    caster_position = dict((caster or {}).get("position") or {})
    if set(caster_position) < {"x", "y"}:
        return None
    battle_map = dict(combat.get("battle_map") or {})
    bounds = dict(battle_map.get("bounds") or {})
    width = int(bounds.get("width_cells", 0) or 0)
    height = int(bounds.get("height_cells", 0) or 0)
    if width <= 0 or height <= 0:
        return None
    cell_ft = int(dict(battle_map.get("grid") or {}).get("cell_ft", 5) or 5)
    if cell_ft <= 0:
        return None
    nondead_combatants = {
        target_id: item
        for target_id, item in combatants.items()
        if "dead" not in {
            str(condition).casefold() for condition in item.get("conditions", [])
        }
    }
    observable_ids = {
        target_id
        for target_id, item in nondead_combatants.items()
        if not item.get("hidden")
        or actor_id in set(item.get("visible_to_actor_ids") or [])
        or target_id == actor_id
    }
    caster_disposition = str((caster or {}).get("disposition") or "")
    if caster_disposition in {"friendly", "hostile"}:
        friendly_ids = {
            target_id
            for target_id, item in nondead_combatants.items()
            if str(item.get("disposition") or "") == caster_disposition
        }
        hostile_ids = set(nondead_combatants) - friendly_ids
    elif actor_id in party_ids:
        friendly_ids = set(party_ids)
        hostile_ids = set(nondead_combatants) - friendly_ids
    else:
        hostile_ids = set(party_ids)
        friendly_ids = set(nondead_combatants) - hostile_ids
    active_hostile_ids = set(living_targets)
    if hypnotic_pattern:
        if 30 % cell_ft:
            return None
        cube_cells = 30 // cell_ft
        if width < cube_cells or height < cube_cells:
            return None
        best_cube: tuple[int, int, int, dict[str, Any]] | None = None
        for minimum_y in range(height - cube_cells + 1):
            for minimum_x in range(width - cube_cells + 1):
                maximum_x = minimum_x + cube_cells - 1
                maximum_y = minimum_y + cube_cells - 1
                affected = {
                    target_id
                    for target_id, item in nondead_combatants.items()
                    if isinstance(item.get("position"), dict)
                    and minimum_x <= int(item["position"]["x"]) <= maximum_x
                    and minimum_y <= int(item["position"]["y"]) <= maximum_y
                }
                if not affected or not affected <= observable_ids:
                    continue
                affected_hostiles = affected & hostile_ids
                if (
                    len(affected_hostiles & active_hostile_ids) < 2
                    or affected & friendly_ids
                ):
                    continue
                boundary = [
                    {"x": x, "y": y}
                    for y in range(minimum_y, maximum_y + 1)
                    for x in range(minimum_x, maximum_x + 1)
                    if x in {minimum_x, maximum_x}
                    or y in {minimum_y, maximum_y}
                ]
                in_range = [
                    position
                    for position in boundary
                    if max(
                        abs(int(caster_position["x"]) - position["x"]),
                        abs(int(caster_position["y"]) - position["y"]),
                    )
                    * cell_ft
                    <= range_ft
                ]
                if not in_range:
                    continue
                origin = min(
                    in_range,
                    key=lambda position: (
                        max(
                            abs(int(caster_position["x"]) - position["x"]),
                            abs(int(caster_position["y"]) - position["y"]),
                        ),
                        position["x"],
                        position["y"],
                    ),
                )
                declaration = {
                    "origin": origin,
                    "cube": {
                        "min": {"x": minimum_x, "y": minimum_y},
                        "max": {"x": maximum_x, "y": maximum_y},
                    },
                    "_target_ids": sorted(affected),
                }
                candidate = (
                    len(affected_hostiles),
                    -minimum_x,
                    -minimum_y,
                    declaration,
                )
                if best_cube is None or candidate[:3] > best_cube[:3]:
                    best_cube = candidate
        return deepcopy(best_cube[3]) if best_cube is not None else None

    if shape == "line":
        start_x = float(caster_position["x"])
        start_y = float(caster_position["y"])
        best_line: tuple[int, int, int, dict[str, Any]] | None = None
        for origin_y in range(height):
            for origin_x in range(width):
                direction_x = origin_x - start_x
                direction_y = origin_y - start_y
                direction_length = (direction_x**2 + direction_y**2) ** 0.5
                if direction_length == 0:
                    continue
                affected: set[str] = set()
                for target_id, item in nondead_combatants.items():
                    position = item.get("position")
                    if not isinstance(position, dict):
                        continue
                    target_x = float(position["x"]) - start_x
                    target_y = float(position["y"]) - start_y
                    projection_cells = (
                        target_x * direction_x + target_y * direction_y
                    ) / direction_length
                    perpendicular_cells = abs(
                        target_x * direction_y - target_y * direction_x
                    ) / direction_length
                    if (
                        0 < projection_cells * cell_ft <= length_ft
                        and perpendicular_cells * cell_ft <= width_ft / 2
                    ):
                        affected.add(target_id)
                if not affected or not affected <= observable_ids:
                    continue
                affected_hostiles = affected & hostile_ids
                if (
                    len(affected_hostiles & active_hostile_ids) < 2
                    or affected & friendly_ids
                ):
                    continue
                declaration = {
                    "origin": {"x": origin_x, "y": origin_y},
                    "target_contexts": [
                        {"target_id": target_id, "cover": "none"}
                        for target_id in sorted(affected)
                    ],
                }
                candidate = (
                    len(affected_hostiles),
                    -origin_x,
                    -origin_y,
                    declaration,
                )
                if best_line is None or candidate[:3] > best_line[:3]:
                    best_line = candidate
        return deepcopy(best_line[3]) if best_line is not None else None

    best: tuple[int, int, int, dict[str, Any]] | None = None
    for y in range(height):
        for x in range(width):
            if (
                max(
                    abs(float(caster_position["x"]) - x),
                    abs(float(caster_position["y"]) - y),
                )
                * 5
                > range_ft
            ):
                continue
            affected = {
                target_id
                for target_id, item in nondead_combatants.items()
                if isinstance(item.get("position"), dict)
                and max(
                    abs(float(item["position"]["x"]) - x),
                    abs(float(item["position"]["y"]) - y),
                )
                * 5
                <= radius_ft
            }
            # Requiring the complete affected set to be observable prevents the
            # targeting declaration from leaking hidden actor knowledge.
            if not affected or not affected <= observable_ids:
                continue
            affected_hostiles = affected & hostile_ids
            affected_friendlies = affected & friendly_ids
            if (
                len(affected_hostiles & active_hostile_ids) < 2
                or affected_friendlies
            ):
                continue
            declaration = {
                "origin": {"x": x, "y": y},
                "target_contexts": [
                    {"target_id": target_id, "cover": "none"}
                    for target_id in sorted(affected)
                ],
            }
            candidate = (len(affected_hostiles), x, y, declaration)
            if best is None or candidate[:1] > best[:1]:
                best = candidate
    return deepcopy(best[3]) if best is not None else None


def _choose_agent_spell(
    actor_id: str,
    *,
    party_ids: list[str],
    actors: dict[str, dict[str, Any]],
    living_targets: list[str],
    spell_choices: list[dict[str, str]],
    leveled_spell_available: bool = True,
    combat: dict[str, Any] | None = None,
) -> tuple[str, str, int] | tuple[str, str, int, dict[str, Any]] | None:
    """Apply one explicit Agent spell policy to current structured state."""

    if not spell_choices:
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
    allied_ids = (
        list(party_ids)
        if actor_id in party_ids
        else [item for item in actors if item not in set(party_ids)]
    )
    for choice in spell_choices:
        spell_id = str(choice.get("spell_id") or "")
        target_policy = str(choice.get("target_policy") or "")
        if spell_id not in spells:
            continue
        spell = next(
            (
                item
                for item in spell_cards
                if str(item.get("id") or "") == spell_id
            ),
            None,
        )
        spell_level = int(dict(spell or {}).get("level", 1) or 0)
        cast_level = (
            0
            if spell_level == 0
            else next(
                (
                    level
                    for level in available_slot_levels
                    if leveled_spell_available and level >= spell_level
                ),
                None,
            )
        )
        if cast_level is None:
            continue
        if target_policy == "downed_ally":
            downed_allies = [
                ally_id
                for ally_id in allied_ids
                if ally_id != actor_id
                and _hit_points(actors[ally_id]) == 0
                and "dead" not in _conditions(actors[ally_id])
            ]
            downed_allies.sort(
                key=lambda item: "stable" in _conditions(actors[item])
            )
            if downed_allies:
                return spell_id, downed_allies[0], cast_level
            continue
        if target_policy == "prioritized_opponent":
            if living_targets:
                return spell_id, living_targets[0], cast_level
            continue
        if (
            target_policy == "maximize_opponents_without_allies"
            and living_targets
            and combat is not None
            and spell is not None
        ):
            declaration = _area_spell_declaration(
                spell,
                actor_id=actor_id,
                party_ids=allied_ids,
                living_targets=living_targets,
                actors=actors,
                combat=combat,
            )
            if declaration is not None:
                affected_ids = [
                    str(target_id)
                    for target_id in declaration.pop("_target_ids", [])
                ] or [
                    str(item["target_id"])
                    for item in declaration.get("target_contexts", [])
                ]
                return spell_id, affected_ids[0], cast_level, declaration
    return None


def _area_spell_target_ids(
    declaration: dict[str, Any],
    cast: dict[str, Any],
) -> list[str]:
    """Read area targets from the declaration or authoritative cast result."""

    declared = [
        str(item.get("target_id") or "")
        for item in declaration.get("target_contexts", [])
        if isinstance(item, dict)
    ]
    if declared and all(declared):
        return declared
    settled = [
        str(item.get("target_id") or "")
        for item in dict(cast.get("result") or {}).get("targets", [])
        if isinstance(item, dict)
    ]
    if settled and all(settled):
        return settled
    raise RuntimeError(
        "committed area spell did not report its complete affected target set"
    )


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
        if target is None:
            continue
        visible_to = target.get("visible_to_actor_ids")
        if not target.get("hidden") or (isinstance(visible_to, list) and observer_id in visible_to):
            observable.append(target_id)
    return observable






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
    _, cost, _, x, y = min(candidates)
    selected = (x, y)
    reverse_path = [selected]
    while reverse_path[-1] != origin_cell:
        reverse_path.append(previous[reverse_path[-1]])
    route = [{"x": point[0], "y": point[1]} for point in reversed(reverse_path[:-1])]
    return {"x": x, "y": y}, cost, route


def _destination_within_range(
    destination: dict[str, int],
    target_position: dict[str, int],
    *,
    range_ft: int,
) -> bool:
    if (
        range_ft < 0
        or set(destination) != {"x", "y"}
        or set(target_position) != {"x", "y"}
    ):
        return False
    return _distance(destination, target_position) * 5 <= range_ft


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


def _pending_window(
    combat: dict[str, Any],
) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in combat.get("pending", [])
            if isinstance(item, dict)
            and item.get("status", "pending") == "pending"
        ),
        None,
    )


def _pending_resolution_made_progress(
    pending: dict[str, Any],
    combat_after: dict[str, Any],
) -> bool:
    pending_id = str(pending.get("id") or "")
    return bool(pending_id) and all(
        not isinstance(item, dict)
        or str(item.get("id") or "") != pending_id
        or item.get("status", "pending") != "pending"
        for item in combat_after.get("pending", [])
    )


def _spell_cast_blocks_turn_progress(
    cast: dict[str, Any],
    *,
    pending_reaction: bool,
) -> bool:
    """Keep the current turn open until every spell-created window settles."""

    return pending_reaction or _has_blocking_pending(
        dict(cast.get("combat") or {})
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


def _source_outcome_allows_checkpoint(status: str) -> bool:
    """Never label a source encounter defeat with a caller's success checkpoint."""

    return str(status).strip().casefold() != "defeat"


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
        or _conditions(actor) & INCAPACITATING_STATE_IDS
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
            and not _conditions(actors[ally_id]) & DEATH_SAVE_SETTLED_CONDITIONS
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
    actor: dict[str, Any] | None = None,
    flee_at_hp: int = 0,
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
    if flee_at_hp > 0 and actor is not None and _hit_points(actor) <= flee_at_hp:
        return True
    return flee_on_critical and acting_actor_id in set(critical_hit_actor_ids or set())


def _ready_immediate_source_flee_actor_ids(
    *,
    flee_actor_ids: set[str],
    actors: dict[str, dict[str, Any]],
    already_fled_actor_ids: set[str],
    damage_taken_by_actor: dict[str, int],
    flee_after_damage: int,
    critical_hit_actor_ids: set[str],
    flee_on_critical: bool,
    flee_at_hp: int = 0,
) -> list[str]:
    """Select living actors whose source retreat resolves at damage settlement."""

    return sorted(
        actor_id
        for actor_id in flee_actor_ids
        if actor_id not in already_fled_actor_ids
        and actor_id in actors
        and _hit_points(actors[actor_id]) > 0
        and (
            (
                flee_after_damage > 0
                and int(damage_taken_by_actor.get(actor_id, 0) or 0)
                >= flee_after_damage
            )
            or (flee_at_hp > 0 and _hit_points(actors[actor_id]) <= flee_at_hp)
            or (flee_on_critical and actor_id in critical_hit_actor_ids)
        )
    )


def _ready_linked_source_flee_actor_ids(
    *,
    linked_flee_actor_ids: set[str],
    trigger_fled_actor_id: str,
    fled_hostile_ids: set[str],
    actors: dict[str, dict[str, Any]],
    active_combatant_ids: set[str],
) -> list[str]:
    """Select active survivors whose cited leader has already fled."""

    if not trigger_fled_actor_id or trigger_fled_actor_id not in fled_hostile_ids:
        return []
    return sorted(
        actor_id
        for actor_id in linked_flee_actor_ids
        if actor_id not in fled_hostile_ids
        and actor_id in active_combatant_ids
        and actor_id in actors
        and _hit_points(actors[actor_id]) > 0
        and "dead" not in _conditions(actors[actor_id])
    )


def _validate_source_flee_configuration(
    args: argparse.Namespace,
    *,
    hostile_ids: list[str],
) -> set[str]:
    """Validate source retreat triggers without widening encounter authority."""

    linked_flee_actor_ids = {
        str(actor_id) for actor_id in getattr(args, "linked_flee_actor_id", [])
    } - {""}
    linked_trigger_actor_id = str(
        getattr(args, "linked_flee_trigger_actor_id", "") or ""
    )
    linked_source_excerpt = str(
        getattr(args, "linked_flee_source_excerpt", "") or ""
    ).strip()
    flee_at_hp = int(getattr(args, "flee_at_hp", 0) or 0)
    source_flee_ids = {
        *(str(actor_id) for actor_id in args.flee_actor_id),
        str(args.flee_trigger_defeated_actor_id or ""),
        str(args.flee_on_start_actor_id or ""),
        *linked_flee_actor_ids,
        linked_trigger_actor_id,
    } - {""}
    triggered_flee_configured = bool(
        args.flee_actor_id
        or args.flee_trigger_defeated_actor_id
        or args.flee_after_defeated
        or args.flee_after_damage
        or flee_at_hp
        or args.flee_on_critical
    )
    defeated_flee_triggers = int(bool(args.flee_trigger_defeated_actor_id)) + int(
        bool(args.flee_after_defeated)
    )
    has_triggered_flee_condition = bool(
        defeated_flee_triggers
        or args.flee_after_damage
        or flee_at_hp
        or args.flee_on_critical
    )
    if triggered_flee_configured and (
        not args.flee_actor_id
        or not has_triggered_flee_condition
        or defeated_flee_triggers > 1
    ):
        raise ValueError(
            "source-specific triggered flee requires --flee-actor-id, at least one "
            "HP, damage, critical-hit, or defeat trigger, and no more than one "
            "defeat trigger"
        )
    if (
        args.flee_after_defeated < 0
        or args.flee_after_damage < 0
        or flee_at_hp < 0
    ):
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
    linked_configured = bool(linked_flee_actor_ids or linked_trigger_actor_id)
    if linked_configured and (
        not linked_flee_actor_ids
        or not linked_trigger_actor_id
        or linked_trigger_actor_id in linked_flee_actor_ids
        or not linked_source_excerpt
    ):
        raise ValueError(
            "linked source flee requires distinct linked actors and trigger actor "
            "plus --linked-flee-source-excerpt"
        )
    if linked_source_excerpt and _normalized_source_text(linked_source_excerpt) not in (
        _normalized_source_text(args.source_excerpt)
    ):
        raise ValueError(
            "linked source flee excerpt must be contained in --source-excerpt"
        )
    return {str(actor_id) for actor_id in args.flee_actor_id} - {""}


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


def _completed_source_opening_weapon_actor_ids(
    combat: dict[str, Any],
    declarations: dict[str, dict[str, str]],
) -> set[str]:
    """Recover source-required opening attacks from the public combat log."""

    completed: set[str] = set()
    for event in combat.get("log", []):
        if not isinstance(event, dict) or event.get("type") != "attack":
            continue
        result = dict(event.get("result") or {})
        attacker_id = str(result.get("attacker_id") or "")
        declaration = declarations.get(attacker_id)
        if declaration is None:
            continue
        if str(result.get("weapon_id") or "") == declaration["weapon_id"]:
            completed.add(attacker_id)
    return completed


def _required_source_opening_weapon(
    declarations: dict[str, dict[str, str]],
    *,
    actor_id: str,
    completed_actor_ids: set[str],
) -> dict[str, str] | None:
    """Return an opening constraint only until that actor has made the attack."""

    if actor_id in completed_actor_ids:
        return None
    return declarations.get(actor_id)










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


async def _compile_content_solution(
    client: ExposureClient,
    args: argparse.Namespace,
    solution: dict[str, Any],
) -> dict[str, Any]:
    """Persist one Agent-authored source-card plan through the public MCP tool."""

    query_arguments = {
        "campaign_id": args.campaign_id,
        "action": "query",
        "actor_id": str(solution["actor_id"]),
        "source_card_id": str(solution["source_card_id"]),
        "source_card_kind": str(solution["source_card_kind"]),
    }
    current = await client.domain("content_solution", query_arguments)
    if current.get("status") == "compiled":
        plan = dict(current.get("resolution_plan") or {})
        return {
            "status": "compiled",
            "resolution_plan_contract": {
                "plan_id": str(plan.get("id") or ""),
                "plan_fingerprint": str(plan.get("fingerprint") or ""),
            },
        }
    actors = await _characters(
        client,
        args.campaign_id,
        [str(solution["actor_id"])],
    )
    actor = actors[str(solution["actor_id"])]
    compiled = _facade_value(
        await client.domain(
            "content_solution",
            {
                **query_arguments,
                "action": "compile",
                "payload": {
                    "resolution_plan": deepcopy(solution["resolution_plan"]),
                    "agent_ruling": deepcopy(solution["compile_ruling"]),
                },
                "expected_revision": int(actor["revision"]),
                "idempotency_key": (
                    "encounter-content-solution-"
                    + _token(
                        str(solution["actor_id"]),
                        str(solution["source_card_kind"]),
                        str(solution["source_card_id"]),
                        length=24,
                    )
                ),
            },
        )
    )
    if compiled.get("status") != "compiled":
        raise RuntimeError("content_solution did not persist the Agent-authored plan")
    return compiled


async def _execute_pending_content_solution(
    client: ExposureClient,
    args: argparse.Namespace,
    *,
    branch_id: str,
    pending: dict[str, Any],
    solutions: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Compile once, then execute one paid custom-card event through generic primitives."""

    source_actor_id = str(pending.get("attacker_id") or pending.get("actor_id") or "")
    source_card_id = str(pending.get("source_card_id") or pending.get("weapon_id") or "")
    source_card_kind = str(pending.get("source_card_kind") or "item")
    identity = (source_actor_id, source_card_id, source_card_kind)
    solution = solutions.get(identity)
    if solution is None:
        raise EncounterRulingRequiredError(
            {
                "status": "pending_ruling",
                "default_resolver": "agent",
                "ruling_kind": "agent_dm_adjudication",
                "reason": (
                    "the paid custom-card event has no persisted source-bound resolution plan"
                ),
                "committed": True,
                "missing": ["content_solution"],
                "semantic_solution": {
                    "required_action": "compile_and_persist_source_bound_resolution",
                    "source_card_id": source_card_id,
                    "source_card_kind": source_card_kind,
                },
                "retry_contract": {
                    "resolver": "agent",
                    "reuse_current_revision": True,
                    "use_public_tools_only": True,
                },
            },
            operation="content_solution.compile_then_combat_choice.execute_plan",
            actor_id=source_actor_id,
            target_id=str(pending.get("target_id") or ""),
            action={
                "choice_id": str(pending.get("id") or ""),
                "source_card_id": source_card_id,
                "source_card_kind": source_card_kind,
            },
            retry_hint=(
                "Have the Agent read the exact source card and active scene, then retry "
                "with one generic --content-solution-json declaration."
            ),
        )
    compiled = await _compile_content_solution(client, args, solution)
    contract = dict(compiled.get("resolution_plan_contract") or {})
    if not str(contract.get("plan_id") or "") or not str(
        contract.get("plan_fingerprint") or ""
    ):
        raise RuntimeError("compiled content solution returned no executable plan contract")
    application_id = str(pending.get("id") or "")
    execution_ruling = deepcopy(solution["execution_ruling"])
    execution_ruling["application_id"] = application_id
    commitment = {
        "application_id": application_id,
        "plan_id": str(contract["plan_id"]),
        "plan_fingerprint": str(contract["plan_fingerprint"]),
        "source_card_id": source_card_id,
        "source_card_kind": source_card_kind,
        "bindings": deepcopy(solution["bindings"]),
        "agent_ruling": execution_ruling,
    }
    campaign = await _campaign(client, args.campaign_id)
    return _facade_value(
        await client.domain(
            "combat_choice",
            {
                "campaign_id": args.campaign_id,
                "action": "execute_plan",
                "actor_id": source_actor_id,
                "payload": {"commitment": commitment},
                "branch_id": branch_id,
                "expected_revision": campaign["revision"],
                "idempotency_key": (
                    "encounter-execute-content-plan-"
                    + _token(application_id, str(contract["plan_fingerprint"]), length=24)
                ),
            },
        )
    )


def _scheduled_content_solution(
    solutions: dict[tuple[str, str, str], dict[str, Any]],
    *,
    actor_id: str,
    round_number: int,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return the one Agent-scheduled portable-card activation for this turn."""

    matches = [
        (solution, activation)
        for solution in solutions.values()
        if str(solution["actor_id"]) == actor_id
        for activation in solution.get("activations", [])
        if int(activation["round"]) == round_number
    ]
    if len(matches) > 1:
        raise RuntimeError(
            "multiple content solutions reached the same actor/round action boundary"
        )
    return matches[0] if matches else None


async def _activate_content_solution(
    client: ExposureClient,
    args: argparse.Namespace,
    *,
    branch_id: str,
    solution: dict[str, Any],
    activation: dict[str, Any],
    component_ruling: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pay and execute one Agent-scheduled custom-card action through public tools."""

    compiled = await _compile_content_solution(client, args, solution)
    contract = dict(compiled.get("resolution_plan_contract") or {})
    plan_id = str(contract.get("plan_id") or "")
    plan_fingerprint = str(contract.get("plan_fingerprint") or "")
    if not plan_id or not plan_fingerprint:
        raise RuntimeError("compiled content solution returned no executable plan contract")
    actor_id = str(solution["actor_id"])
    source_card_id = str(solution["source_card_id"])
    source_card_kind = str(solution["source_card_kind"])
    round_number = int(activation["round"])
    application_id = (
        "encounter-content-action-"
        + _operation_token(
            args,
            branch_id,
            actor_id,
            source_card_kind,
            source_card_id,
            round_number,
        )
    )
    execution_ruling = deepcopy(activation["execution_ruling"])
    execution_ruling["application_id"] = application_id
    commitment = {
        "application_id": application_id,
        "plan_id": plan_id,
        "plan_fingerprint": plan_fingerprint,
        "source_card_id": source_card_id,
        "source_card_kind": source_card_kind,
        "bindings": deepcopy(activation["bindings"]),
        "agent_ruling": execution_ruling,
    }
    campaign = await _campaign(client, args.campaign_id)
    payment_arguments: dict[str, Any] = {
        "campaign_id": args.campaign_id,
        "actor_id": actor_id,
        "branch_id": branch_id,
        "expected_revision": campaign["revision"],
        "idempotency_key": (
            "encounter-content-action-payment-"
            + _token(application_id, length=24)
        ),
    }
    if source_card_kind == "spell":
        payment_tool = "combat_cast_spell"
        payment_arguments["spell_id"] = source_card_id
        if "cast_level" in activation:
            payment_arguments["cast_level"] = int(activation["cast_level"])
        if component_ruling:
            payment_arguments["component_ruling"] = deepcopy(component_ruling)
    else:
        payment_tool = "combat_use_activity"
        payment_arguments["activity_id"] = source_card_id
    payment_arguments["declaration"] = {
        "agent_resolution_commitment": commitment,
    }
    payment = _facade_value(await client.domain(payment_tool, payment_arguments))
    if payment.get("status") != "pending_ruling":
        raise RuntimeError(
            "planned custom-card action did not reach its paid semantic-plan boundary"
        )
    payment_result = dict(payment.get("result") or {})
    if source_card_kind == "spell":
        paid_commitment = dict(
            dict(payment_result.get("semantic_plan") or {}).get("commitment")
            or commitment
        )
    else:
        paid_commitment = dict(
            dict(payment_result.get("declaration") or {}).get(
                "agent_resolution_commitment"
            )
            or commitment
        )
    paid_revision = payment.get("campaign_revision")
    if not isinstance(paid_revision, int):
        raise RuntimeError("planned custom-card action returned no campaign revision")
    settlement = _facade_value(
        await client.domain(
            "combat_choice",
            {
                "campaign_id": args.campaign_id,
                "action": "execute_plan",
                "actor_id": actor_id,
                "payload": {"commitment": paid_commitment},
                "branch_id": branch_id,
                "expected_revision": paid_revision,
                "idempotency_key": (
                    "encounter-content-action-settlement-"
                    + _token(f"{application_id}:{plan_fingerprint}", length=24)
                ),
            },
        )
    )
    if settlement.get("status") != "committed":
        raise RuntimeError("planned custom-card action did not execute its generic plan")
    return {
        "application_id": application_id,
        "source_card_id": source_card_id,
        "source_card_kind": source_card_kind,
        "activation": deepcopy(activation),
        "payment": payment,
        "settlement": settlement,
    }


async def _resolve_pending(
    client: ExposureClient,
    args: argparse.Namespace,
    branch_id: str,
    combat: dict[str, Any],
) -> dict[str, Any] | None:
    pending = _pending_window(combat)
    if pending is None:
        return None
    campaign = await _campaign(client, args.campaign_id)
    actor_id = str(pending.get("actor_id") or "")
    identity = f"{pending.get('id')}:{campaign['revision']}"
    if pending.get("trigger") in {"attack_on_hit_effect", "attack_semantic_plan"}:
        participant_ids = [
            str(item.get("actor_id") or "")
            for item in combat.get("combatants", [])
            if isinstance(item, dict) and str(item.get("actor_id") or "")
        ]
        return await _execute_pending_content_solution(
            client,
            args,
            branch_id=branch_id,
            pending=pending,
            solutions=_content_solutions(
                getattr(args, "content_solution_json", []),
                participant_ids=participant_ids,
            ),
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


def _reaction_available_actor_ids(combat: dict[str, Any]) -> set[str]:
    """Return combatants that still own their reaction for the current round."""

    return {
        str(item.get("actor_id") or "")
        for item in combat.get("combatants", [])
        if int(dict(item.get("turn_budget") or {}).get("reaction", 0) or 0) > 0
    }


async def _consume_agent_target_reaction(
    client: ExposureClient,
    args: argparse.Namespace,
    *,
    branch_id: str,
    context: dict[str, Any],
    attacker_id: str,
    sequence: int,
) -> dict[str, Any]:
    """Open and accept a source-bound Agent reaction through public combat tools."""

    actor_id = str(context["actor_id"])
    application_id = str(context["application_id"])
    ruling = dict(context["agent_ruling"])
    selection = {
        "id": application_id,
        "decision": ruling["decision"],
        "source_ref": deepcopy(ruling["source_ref"]),
        "source_excerpt": ruling["source_excerpt"],
    }
    campaign = await _campaign(client, args.campaign_id)
    opened = _facade_value(
        await client.domain(
            "combat_choice",
            {
                "campaign_id": args.campaign_id,
                "action": "open",
                "actor_id": actor_id,
                "payload": {
                    "event": (
                        f"{actor_id} is targeted by a {context['attack_mode']} attack "
                        f"from {attacker_id}; the Agent elects the source-bound reaction."
                    ),
                    "kind": "reaction",
                    "candidates": [selection],
                },
                "branch_id": branch_id,
                "expected_revision": campaign["revision"],
                "idempotency_key": (
                    "encounter-target-reaction-open-"
                    + _operation_token(
                        args,
                        sequence,
                        application_id,
                        campaign["revision"],
                    )
                ),
            },
        )
    )
    choice_id = str(dict(opened.get("choice") or {}).get("id") or "")
    if not choice_id:
        raise RuntimeError("public target reaction window did not return a choice id")
    campaign = await _campaign(client, args.campaign_id)
    resolved = _facade_value(
        await client.domain(
            "combat_choice",
            {
                "campaign_id": args.campaign_id,
                "action": "resolve",
                "actor_id": actor_id,
                "payload": {
                    "choice_id": choice_id,
                    "selection": selection,
                },
                "branch_id": branch_id,
                "expected_revision": campaign["revision"],
                "idempotency_key": (
                    "encounter-target-reaction-resolve-"
                    + _operation_token(
                        args,
                        sequence,
                        application_id,
                        campaign["revision"],
                    )
                ),
            },
        )
    )
    return {
        "actor_id": actor_id,
        "attacker_id": attacker_id,
        "application_id": application_id,
        "agent_ruling": deepcopy(ruling),
        "open": opened,
        "resolve": resolved,
    }


async def _settle_agent_turn_ruling(
    client: ExposureClient,
    args: argparse.Namespace,
    *,
    branch_id: str,
    ruling: dict[str, Any],
) -> dict[str, Any]:
    """Pay one descriptive action and persist the Agent's source-bound outcome."""

    actor_id = str(ruling["actor_id"])
    target_id = str(ruling.get("target_id") or "")
    target_ids = [
        str(item)
        for item in ruling.get("target_ids") or ([target_id] if target_id else [])
    ]
    save_contract = dict(ruling.get("save") or {})
    check_contract = dict(ruling.get("check") or {})
    damage_contract = dict(save_contract.get("damage") or {})
    source_card_kind = "scene_procedure"
    source_card_id = str(ruling.get("procedure_id") or "")
    mechanic_source_excerpt = " ".join(
        str(ruling.get("mechanic_source_excerpt") or "").split()
    )
    scene_agent_ruling = (
        {
            "application_id": str(ruling["application_id"]),
            "default_resolver": "agent",
            "ruling_kind": "agent_dm_adjudication",
            "decision": " ".join(
                str(ruling["agent_ruling"]["decision"]).split()
            ),
            "reason": " ".join(
                str(ruling["agent_ruling"]["reason"]).split()
            ),
            "source_ref": deepcopy(ruling["agent_ruling"]["source_ref"]),
            "source_excerpt": str(
                ruling["agent_ruling"]["encounter_source_excerpt"]
            ),
        }
        if damage_contract
        else None
    )
    save_damage_commitment = (
        {
            "application_id": str(ruling["application_id"]),
            "source_card_id": source_card_id,
            "source_card_kind": source_card_kind,
            "target_ids": target_ids,
            "save_ability": str(save_contract["ability"]).strip().casefold(),
            "save_dc": int(save_contract["dc"]),
            "save_advantage": bool(save_contract["advantage"]),
            "save_disadvantage": bool(save_contract["disadvantage"]),
            "damage_expression": "".join(
                str(damage_contract["expression"]).split()
            ).casefold(),
            "damage_type": str(
                damage_contract["damage_type"]
            ).strip().casefold(),
            "half_on_success": bool(
                damage_contract["half_on_success"]
            ),
            "mechanic_source_excerpt": mechanic_source_excerpt,
            "agent_ruling": deepcopy(scene_agent_ruling),
        }
        if damage_contract
        else None
    )
    async def pay_action(
        tool_id: str,
        arguments: dict[str, Any],
        *,
        idempotency_prefix: str,
    ) -> dict[str, Any]:
        """Pay a ruling action under its durable application identity."""

        application_id = str(ruling["application_id"])
        stable_key = idempotency_prefix + _agent_turn_transaction_token(
            args,
            branch_id=branch_id,
            application_id=application_id,
            parts=("action", tool_id),
        )
        return await client.domain(
            tool_id,
            {**arguments, "idempotency_key": stable_key},
        )

    campaign = await _campaign(client, args.campaign_id)
    if check_contract:
        action_result = await client.domain(
            "combat_check",
            {
                "campaign_id": args.campaign_id,
                "actor_id": actor_id,
                "kind": "check",
                "ability": str(check_contract["ability"]),
                "action": str(check_contract["action"]),
                "dc": int(check_contract["dc"]),
                "advantage": bool(check_contract["advantage"]),
                "disadvantage": bool(check_contract["disadvantage"]),
                "rule_facts": {
                    "source_ref": deepcopy(
                        ruling["agent_ruling"]["source_ref"]
                    ),
                    "agent_ruling_id": str(ruling["application_id"]),
                },
                "branch_id": branch_id,
                "expected_revision": campaign["revision"],
                "idempotency_key": (
                    "encounter-agent-turn-check-"
                    + _agent_turn_transaction_token(
                        args,
                        branch_id=branch_id,
                        application_id=str(ruling["application_id"]),
                        parts=("check",),
                    )
                ),
            },
        )
        check_value = dict(action_result.get("result") or {})
        if (
            action_result.get("status") != "committed"
            or check_value.get("kind") != "ability"
            or str(check_value.get("skill") or check_value.get("ability") or "")
            .strip()
            .casefold()
            != str(check_contract["ability"]).strip().casefold()
            or int(check_value.get("dc", 0) or 0) != int(check_contract["dc"])
        ):
            raise RuntimeError(
                "source-cited procedure check did not settle as the expected "
                "action-bound server ability check"
            )
    else:
        action_result = await pay_action(
            "combat_common_action",
            {
                "campaign_id": args.campaign_id,
                "actor_id": actor_id,
                "action": "improvise",
                "target_id": target_id or (target_ids[0] if target_ids else None),
                "payload": {
                    "kind": "agent_dm_adjudication",
                    "procedure_id": str(ruling.get("procedure_id") or ""),
                    "application_id": str(ruling["application_id"]),
                    "decision": str(ruling["agent_ruling"]["decision"]),
                    **(
                        {
                            "agent_ruling_commitment": (
                                save_damage_commitment
                            )
                        }
                        if save_damage_commitment is not None
                        else {}
                    ),
                },
                "branch_id": branch_id,
                "expected_revision": campaign["revision"],
            },
            idempotency_prefix="encounter-agent-turn-feature-",
        )
        if action_result.get("status") != "committed":
            raise RuntimeError("Agent-adjudicated feature did not pay its combat action")

    save_result = None
    save_results: list[dict[str, Any]] = []
    save_success = None
    outcome = str(ruling["agent_ruling"]["decision"])
    damage_roll = None
    damage_results: list[dict[str, Any]] = []
    atomic_save_damage = None
    check_result = action_result if check_contract else None
    check_success = (
        bool(dict(action_result.get("result") or {}).get("success"))
        if check_contract
        else None
    )
    combat_outcome = (
        deepcopy(check_contract.get("success_combat_outcome"))
        if check_contract and check_success
        else None
    )
    if check_contract:
        outcome = str(
            check_contract["success_outcome"]
            if check_success
            else check_contract["failure_outcome"]
        )
    if save_contract:
        if damage_contract:
            campaign = await _campaign(client, args.campaign_id)
            atomic_save_damage = _facade_value(
                await client.domain(
                    "combat_hp_change",
                    {
                        "campaign_id": args.campaign_id,
                        "target_id": target_ids[0],
                        "action": "save_damage",
                        "payload": {
                            "target_ids": target_ids,
                            "source_actor_id": actor_id,
                            "source_card_id": source_card_id,
                            "source_card_kind": source_card_kind,
                            "save_ability": str(save_contract["ability"]),
                            "save_dc": int(save_contract["dc"]),
                            "save_advantage": bool(
                                save_contract["advantage"]
                            ),
                            "save_disadvantage": bool(
                                save_contract["disadvantage"]
                            ),
                            "damage_expression": str(
                                damage_contract["expression"]
                            ),
                            "damage_type": str(
                                damage_contract["damage_type"]
                            ),
                            "half_on_success": bool(
                                damage_contract["half_on_success"]
                            ),
                            "mechanic_source_excerpt": (
                                mechanic_source_excerpt
                            ),
                            "agent_ruling": scene_agent_ruling,
                        },
                        "branch_id": branch_id,
                        "expected_revision": campaign["revision"],
                        "idempotency_key": (
                            "encounter-agent-turn-save-damage-"
                            + _agent_turn_transaction_token(
                                args,
                                branch_id=branch_id,
                                application_id=str(
                                    ruling["application_id"]
                                ),
                                parts=("save_damage", *target_ids),
                            )
                        ),
                    },
                )
            )
            atomic_result = dict(atomic_save_damage.get("result") or {})
            damage_roll = deepcopy(atomic_result.get("damage_roll"))
            for target_result in atomic_result.get("targets", []):
                save_target_id = str(target_result["target_id"])
                current_success = bool(target_result["success"])
                current_outcome = str(
                    save_contract["success_outcome"]
                    if current_success
                    else save_contract["failure_outcome"]
                )
                current_save = {
                    "status": "committed",
                    "result": deepcopy(target_result["save"]),
                    "combat": deepcopy(atomic_save_damage.get("combat")),
                    "atomic_save_damage": True,
                }
                save_results.append(
                    {
                        "target_id": save_target_id,
                        "result": current_save,
                        "success": current_success,
                        "outcome": current_outcome,
                    }
                )
                damage_results.append(
                    {
                        "target_id": save_target_id,
                        "rolled": _roll_total(dict(damage_roll or {})),
                        "applied_amount": int(
                            target_result.get("damage_amount", 0) or 0
                        ),
                        "result": {
                            "status": "committed",
                            "result": deepcopy(
                                target_result.get("damage")
                            ),
                            "atomic_save_damage": True,
                        },
                    }
                )
        for save_target_id in (
            [] if atomic_save_damage is not None else target_ids
        ):
            campaign = await _campaign(client, args.campaign_id)
            current_save = await client.domain(
                "combat_check",
                {
                    "campaign_id": args.campaign_id,
                    "actor_id": save_target_id,
                    "kind": "save",
                    "ability": str(save_contract["ability"]),
                    "dc": int(save_contract["dc"]),
                    "advantage": bool(save_contract["advantage"]),
                    "disadvantage": bool(save_contract["disadvantage"]),
                    "rule_facts": {
                        "source_ref": deepcopy(
                            ruling["agent_ruling"]["source_ref"]
                        ),
                        "agent_ruling_id": str(ruling["application_id"]),
                    },
                    "branch_id": branch_id,
                    "expected_revision": campaign["revision"],
                    "idempotency_key": (
                        "encounter-agent-turn-save-"
                        + _agent_turn_transaction_token(
                            args,
                            branch_id=branch_id,
                            application_id=str(ruling["application_id"]),
                            parts=("save", save_target_id),
                        )
                    ),
                },
            )
            current_success = bool(
                dict(current_save.get("result") or {}).get("success")
            )
            current_outcome = str(
                save_contract["success_outcome"]
                if current_success
                else save_contract["failure_outcome"]
            )
            save_results.append(
                {
                    "target_id": save_target_id,
                    "result": current_save,
                    "success": current_success,
                    "outcome": current_outcome,
                }
            )
        if len(save_results) == 1:
            save_result = save_results[0]["result"]
            save_success = bool(save_results[0]["success"])
            outcome = str(save_results[0]["outcome"])
        elif save_results:
            outcome = "; ".join(
                f"{item['target_id']}: {item['outcome']}"
                for item in save_results
            )

    receipt = {
        "kind": "agent_turn_ruling",
        "application_id": str(ruling["application_id"]),
        "actor_id": actor_id,
        "feature_id": str(ruling.get("feature_id") or ""),
        "activity_id": str(ruling.get("activity_id") or ""),
        "spell_id": str(ruling.get("spell_id") or ""),
        "procedure_id": str(ruling.get("procedure_id") or ""),
        "round": int(ruling["round"]),
        "target_id": target_id,
        "target_ids": target_ids,
        "agent_ruling": deepcopy(ruling["agent_ruling"]),
        "action_result": action_result,
        "check_result": check_result,
        "check_success": check_success,
        "combat_outcome": combat_outcome,
        "save_result": save_result,
        "save_results": save_results,
        "save_success": save_success,
        "outcome": outcome,
        "damage_roll": damage_roll,
        "damage_results": damage_results,
        "forced_target_id": (
            str(save_contract.get("forced_target_id") or "")
            if save_contract and save_success is False
            else ""
        ),
        "ends_if_source_incapacitated": bool(
            save_contract.get("ends_if_source_incapacitated", False)
        ),
    }
    campaign = await _campaign(client, args.campaign_id)
    receipt["world_patch"] = await client.domain(
        "combat_map_patch",
        {
            "campaign_id": args.campaign_id,
            "patches": [
                {
                    "key": f"agent_turn_ruling:{ruling['application_id']}",
                    "value": {
                        key: deepcopy(value)
                        for key, value in receipt.items()
                        if key
                        not in {
                            "action_result",
                            "check_result",
                            "save_result",
                            "save_results",
                            "damage_roll",
                            "damage_results",
                            "world_patch",
                        }
                    },
                }
            ],
            "branch_id": branch_id,
            "expected_revision": campaign["revision"],
            "idempotency_key": (
                "encounter-agent-turn-patch-"
                + _agent_turn_transaction_token(
                    args,
                    branch_id=branch_id,
                    application_id=str(ruling["application_id"]),
                    parts=("receipt",),
                )
            ),
        },
    )
    return receipt


def _pending_agent_forced_targets(combat: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Recover unconsumed Agent-directed attacks from temporary-map receipts."""

    patches = list(dict(combat.get("battle_map") or {}).get("world_patches") or [])
    consumed = {
        str(item.get("key") or "").split(":", 1)[1]
        for item in patches
        if isinstance(item, dict)
        and str(item.get("key") or "").startswith("agent_forced_target_consumed:")
    }
    pending: dict[str, dict[str, Any]] = {}
    for item in patches:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        if not key.startswith("agent_turn_ruling:"):
            continue
        value = dict(item.get("value") or {})
        application_id = str(value.get("application_id") or "")
        target_actor_id = str(value.get("target_id") or "")
        forced_target_id = str(value.get("forced_target_id") or "")
        if (
            application_id
            and application_id not in consumed
            and target_actor_id
            and forced_target_id
        ):
            pending[target_actor_id] = {
                "application_id": application_id,
                "target_id": forced_target_id,
                "source_actor_id": str(value.get("actor_id") or ""),
                "ends_if_source_incapacitated": bool(
                    value.get("ends_if_source_incapacitated", False)
                ),
            }
    return pending


def _completed_agent_turn_combat_outcome(
    combat: dict[str, Any],
) -> dict[str, str] | None:
    """Recover a successful procedure outcome after a driver interruption."""

    outcomes: list[dict[str, str]] = []
    for item in dict(combat.get("battle_map") or {}).get("world_patches", []):
        if (
            not isinstance(item, dict)
            or not str(item.get("key") or "").startswith("agent_turn_ruling:")
        ):
            continue
        value = dict(item.get("value") or {})
        outcome = dict(value.get("combat_outcome") or {})
        if not outcome:
            continue
        normalized = {
            "status": str(outcome.get("status") or "").strip().casefold(),
            "summary": " ".join(str(outcome.get("summary") or "").split()),
        }
        if (
            value.get("check_success") is not True
            or normalized["status"] not in COMBAT_OUTCOME_STATUSES
            or not normalized["summary"]
        ):
            raise RuntimeError(
                "persisted Agent procedure combat outcome is incomplete or "
                "not backed by a successful server check"
            )
        outcomes.append(normalized)
    distinct = {(item["status"], item["summary"]) for item in outcomes}
    if len(distinct) > 1:
        raise RuntimeError(
            "encounter contains conflicting successful Agent procedure outcomes"
        )
    return outcomes[0] if outcomes else None


async def _consume_agent_forced_target(
    client: ExposureClient,
    args: argparse.Namespace,
    *,
    branch_id: str,
    actor_id: str,
    target_id: str,
    forced_targets: dict[str, dict[str, Any]],
    reason: str = (
        "The Agent-adjudicated suggested course was completed by the "
        "source-directed attack."
    ),
) -> dict[str, Any] | None:
    declaration = forced_targets.get(actor_id)
    if declaration is None or declaration["target_id"] != target_id:
        return None
    campaign = await _campaign(client, args.campaign_id)
    application_id = str(declaration["application_id"])
    consumed = await client.domain(
        "combat_map_patch",
        {
            "campaign_id": args.campaign_id,
            "patches": [
                {
                    "key": f"agent_forced_target_consumed:{application_id}",
                    "value": {
                        "application_id": application_id,
                        "actor_id": actor_id,
                        "target_id": target_id,
                        "reason": reason,
                    },
                }
            ],
            "branch_id": branch_id,
            "expected_revision": campaign["revision"],
            "idempotency_key": (
                "encounter-agent-forced-target-consumed-"
                + _operation_token(args, application_id)
            ),
        },
    )
    forced_targets.pop(actor_id, None)
    return consumed


async def _preflight_attack(
    client: ExposureClient,
    args: argparse.Namespace,
    actor: dict[str, Any],
    target_ids: list[str],
    *,
    preferred_weapon_id: str = "",
    multiattack_option_id: str = "",
    agent_weapon_choices: list[dict[str, str]] | None = None,
    action_context: dict[str, Any] | None = None,
    agent_attack_contexts: dict[tuple[str, str, str], dict[str, Any]] | None = None,
    agent_target_reaction_contexts: (
        dict[tuple[str, str], dict[str, Any]] | None
    ) = None,
    reaction_available_actor_ids: set[str] | None = None,
    knock_out_target_ids: set[str] | None = None,
    agent_rulings: list[dict[str, Any]] | None = None,
    source_ammunition_selections: (
        dict[tuple[str, str], dict[str, str]] | None
    ) = None,
    require_preferred_weapon: bool = False,
    preflight_rejections: list[dict[str, str]] | None = None,
    round_number: int = 1,
    combat: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    knock_out_targets = set(knock_out_target_ids or set())
    weapons = _actor_weapon_attacks(actor)
    weapons.sort(key=lambda item: item.get("item_id") != preferred_weapon_id)
    if require_preferred_weapon:
        weapons = [
            weapon
            for weapon in weapons
            if str(weapon.get("item_id") or "") == preferred_weapon_id
        ]
        if not weapons:
            raise RuntimeError(
                f"required source opening weapon {preferred_weapon_id!r} is absent "
                f"from {actor['id']}"
            )
    weapon_by_id = {
        str(weapon.get("item_id") or ""): weapon for weapon in weapons
    }
    attack_candidates: list[tuple[dict[str, Any], str, str]] = []
    if agent_weapon_choices:
        for choice in agent_weapon_choices:
            weapon = weapon_by_id.get(str(choice.get("weapon_id") or ""))
            if weapon is not None:
                attack_candidates.append(
                    (
                        weapon,
                        str(choice.get("attack_mode") or ""),
                        str(choice.get("multiattack_option_id") or ""),
                    )
                )
    else:
        for weapon in weapons:
            for attack_mode in sorted(_weapon_attack_modes(weapon)):
                attack_candidates.append(
                    (weapon, attack_mode, multiattack_option_id)
                )
    for weapon, attack_mode, candidate_multiattack_option_id in attack_candidates:
        for target_id in target_ids:
                if target_id in knock_out_targets and attack_mode != "melee":
                    continue
                action = {
                    "weapon_id": weapon.get("item_id"),
                    "attack_mode": attack_mode,
                }
                ammunition_selection = dict(
                    (source_ammunition_selections or {}).get(
                        (
                            str(actor["id"]),
                            str(weapon.get("item_id") or ""),
                        )
                    )
                    or {}
                )
                if ammunition_selection:
                    ammunition = next(
                        (
                            item
                            for item in (
                                dict(actor.get("sheet") or {})
                                .get("inventory", {})
                                .get("items", [])
                            )
                            if str(item.get("id") or "")
                            == ammunition_selection["ammunition_item_id"]
                        ),
                        None,
                    )
                    if (
                        isinstance(ammunition, dict)
                        and int(ammunition.get("quantity", 0) or 0) > 0
                    ):
                        action["ammunition_item_id"] = ammunition_selection[
                            "ammunition_item_id"
                        ]
                if target_id in knock_out_targets:
                    action["knock_out"] = True
                context = dict(action_context or {})
                agent_context = dict(
                    (agent_attack_contexts or {}).get(
                        (str(actor["id"]), target_id, attack_mode)
                    )
                    or (agent_attack_contexts or {}).get(
                        (str(actor["id"]), "", attack_mode)
                    )
                    or {}
                )
                if agent_context:
                    context.update(dict(agent_context["context"]))
                target_reaction_context = dict(
                    (agent_target_reaction_contexts or {}).get(
                        (target_id, attack_mode)
                    )
                    or {}
                )
                if (
                    target_reaction_context
                    and target_id in set(reaction_available_actor_ids or set())
                ):
                    context.update(dict(target_reaction_context["context"]))
                if context:
                    action["context"] = context
                if candidate_multiattack_option_id:
                    action["multiattack_option_id"] = (
                        candidate_multiattack_option_id
                    )
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
                except RuntimeError as error:
                    if preflight_rejections is not None:
                        preflight_rejections.append(
                            {
                                "actor_id": str(actor["id"]),
                                "target_id": target_id,
                                "weapon_id": str(weapon.get("item_id") or ""),
                                "attack_mode": attack_mode,
                                "error": str(error),
                            }
                        )
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
                            "Inspect the typed missing facts, let the Agent decide only "
                            "the documented scene facts, and retry at the current revision."
                        ),
                    )
                return target_id, action, plan
    if (
        multiattack_option_id
        and not agent_weapon_choices
        and not require_preferred_weapon
    ):
        # A creature may always take one ordinary Attack instead of selecting its
        # Multiattack action.  Keep the source-preferred option first, but do not
        # move into a hazard merely because that option is illegal at the current
        # range while one ordinary ranged attack is legal.
        return await _preflight_attack(
            client,
            args,
            actor,
            target_ids,
            preferred_weapon_id=preferred_weapon_id,
            multiattack_option_id="",
            agent_weapon_choices=None,
            action_context=action_context,
            agent_attack_contexts=agent_attack_contexts,
            agent_target_reaction_contexts=agent_target_reaction_contexts,
            reaction_available_actor_ids=reaction_available_actor_ids,
            knock_out_target_ids=knock_out_target_ids,
            agent_rulings=agent_rulings,
            source_ammunition_selections=source_ammunition_selections,
            require_preferred_weapon=False,
            preflight_rejections=preflight_rejections,
            round_number=round_number,
            combat=combat,
        )
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




async def _auto_run(
    client: ExposureClient,
    args: argparse.Namespace,
    party_ids: list[str],
    hostile_ids: list[str],
) -> dict[str, Any]:
    opened_combat = await client.open(args.campaign_id)
    await client.load()
    campaign = await _campaign(client, args.campaign_id)
    if not bool(
        dict(dict(campaign.get("state") or {}).get("combat") or {}).get(
            "active", False
        )
    ):
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
    knock_out_hostile_ids, minimum_hostile_knockouts = _knockout_objective(
        args,
        hostile_ids=hostile_ids,
    )
    opening_casts = _source_opening_casts(
        args.source_opening_cast_json,
        participant_ids=[*party_ids, *hostile_ids],
    )
    opening_weapons = _source_opening_weapons(
        args.source_opening_weapon_json,
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
    _validate_agent_target_refinements(
        source_target_priorities,
        agent_target_priorities,
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
    agent_weapon_priorities = _agent_weapon_priorities(
        getattr(args, "agent_weapon_priority_json", []),
        participant_ids=[*party_ids, *hostile_ids],
        actors=initial_actors,
    )
    agent_spell_priorities = _agent_spell_priorities(
        getattr(args, "agent_spell_priority_json", []),
        participant_ids=[*party_ids, *hostile_ids],
        actors=initial_actors,
    )
    agent_common_action_priorities = _agent_common_action_priorities(
        getattr(args, "agent_common_action_priority_json", []),
        participant_ids=[*party_ids, *hostile_ids],
    )
    source_ammunition_selections = _source_ammunition_selections(
        args.source_ammunition_json,
        participant_ids=[*party_ids, *hostile_ids],
        actors=initial_actors,
    )
    source_separations = _source_separations(
        args.source_separation_json,
        participant_ids=[*party_ids, *hostile_ids],
        hostile_ids=hostile_ids,
        encounter_source_excerpt=str(args.source_excerpt or ""),
    )
    content_solutions = _content_solutions(
        getattr(args, "content_solution_json", []),
        participant_ids=[*party_ids, *hostile_ids],
    )
    agent_attack_contexts = _agent_attack_contexts(
        args.agent_attack_context_json,
        participant_ids=[*party_ids, *hostile_ids],
        scene_id=str(args.scene_id or ""),
        encounter_source_excerpt=str(args.source_excerpt or ""),
    )
    agent_casting_perception_rulings = _agent_casting_perception_rulings(
        getattr(args, "agent_casting_perception_json", []),
        participant_ids=[*party_ids, *hostile_ids],
    )
    agent_target_reaction_contexts = _agent_target_reaction_contexts(
        getattr(args, "agent_target_reaction_context_json", []),
        participant_ids=[*party_ids, *hostile_ids],
        scene_id=str(args.scene_id or ""),
        encounter_source_excerpt=str(args.source_excerpt or ""),
    )
    agent_turn_rulings = _agent_turn_rulings(
        getattr(args, "agent_turn_ruling_json", []),
        participant_ids=[*party_ids, *hostile_ids],
        actors=initial_actors,
        scene_id=str(args.scene_id or ""),
        encounter_source_excerpt=str(args.source_excerpt or ""),
        ruleset=str(initial_combat.get("ruleset") or "2014"),
    )
    agent_object_interactions = _agent_object_interactions(
        getattr(args, "agent_object_interaction_json", []),
        participant_ids=[*party_ids, *hostile_ids],
        source_conditions=[
            deepcopy(item)
            for item in initial_combat.get("source_conditions", [])
            if isinstance(item, dict)
        ],
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
    agent_forced_targets = _pending_agent_forced_targets(initial_combat)
    completed_agent_turn_ruling_ids = {
        str(item.get("key") or "").split(":", 1)[1]
        for item in dict(initial_combat.get("battle_map") or {}).get(
            "world_patches", []
        )
        if isinstance(item, dict)
        and str(item.get("key") or "").startswith("agent_turn_ruling:")
    }
    completed_opening_casts: set[int] = set()
    completed_opening_weapon_actor_ids = _completed_source_opening_weapon_actor_ids(
        initial_combat,
        opening_weapons,
    )
    fled_hostile_ids: set[str] = set()
    linked_flee_actor_ids = {
        str(actor_id) for actor_id in getattr(args, "linked_flee_actor_id", [])
    }
    linked_flee_trigger_actor_id = str(
        getattr(args, "linked_flee_trigger_actor_id", "") or ""
    )
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
        recovered_agent_outcome = _completed_agent_turn_combat_outcome(combat)
        if recovered_agent_outcome is not None:
            outcome_status = recovered_agent_outcome["status"]
            outcome_summary = recovered_agent_outcome["summary"]
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "recovered_agent_turn_combat_outcome",
                    "outcome": deepcopy(recovered_agent_outcome),
                }
            )
            break
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
        potential_party_ids = list(party_ids)
        effective_party_ids = list(party_ids)
        attackable_hostile_ids = list(hostile_ids)
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
        ready_flee_actor_ids = _ready_immediate_source_flee_actor_ids(
            flee_actor_ids=set(args.flee_actor_id),
            actors=actors,
            already_fled_actor_ids=fled_hostile_ids,
            damage_taken_by_actor=damage_taken_by_flee_actor,
            flee_after_damage=args.flee_after_damage,
            critical_hit_actor_ids=critical_hit_flee_actor_ids,
            flee_on_critical=args.flee_on_critical,
            flee_at_hp=args.flee_at_hp,
        )
        for fleeing_actor_id in ready_flee_actor_ids:
            campaign = await _campaign(client, args.campaign_id)
            escaped = await client.domain(
                "combat_map_patch",
                {
                    "campaign_id": args.campaign_id,
                    "patches": [
                        _source_departure_patch(
                            fleeing_actor_id,
                            reason=str(args.flee_source_excerpt),
                            destination_location_key=args.flee_destination_location_key,
                        )
                    ],
                    "branch_id": branch["id"],
                    "expected_revision": campaign["revision"],
                    "idempotency_key": (
                        "encounter-source-immediate-flee-"
                        + _operation_token(args, fleeing_actor_id)
                    ),
                },
            )
            fled_hostile_ids.add(fleeing_actor_id)
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "source_flee",
                    "actor_id": fleeing_actor_id,
                    "trigger": "resolved_source_threshold",
                    "trigger_actor_id": (args.flee_trigger_defeated_actor_id or None),
                    "trigger_defeated_count": (args.flee_after_defeated or None),
                    "trigger_damage_taken": (
                        damage_taken_by_flee_actor.get(fleeing_actor_id)
                        if args.flee_after_damage
                        else None
                    ),
                    "trigger_damage_threshold": (args.flee_after_damage or None),
                    "trigger_current_hp": _hit_points(actors[fleeing_actor_id]),
                    "trigger_hp_threshold": (args.flee_at_hp or None),
                    "trigger_critical_hit": (
                        fleeing_actor_id in critical_hit_flee_actor_ids
                        if args.flee_on_critical
                        else None
                    ),
                    "source_excerpt": str(args.flee_source_excerpt).strip(),
                    "map_patch": escaped,
                }
            )
        ready_linked_flee_actor_ids = _ready_linked_source_flee_actor_ids(
            linked_flee_actor_ids=linked_flee_actor_ids,
            trigger_fled_actor_id=linked_flee_trigger_actor_id,
            fled_hostile_ids=fled_hostile_ids,
            actors=actors,
            active_combatant_ids=set(combatants_by_actor),
        )
        if ready_linked_flee_actor_ids:
            campaign = await _campaign(client, args.campaign_id)
            linked_escape = await client.domain(
                "combat_map_patch",
                {
                    "campaign_id": args.campaign_id,
                    "patches": [
                        _source_departure_patch(
                            actor_id,
                            reason=str(args.linked_flee_source_excerpt),
                            destination_location_key=(
                                args.linked_flee_destination_location_key
                            ),
                        )
                        for actor_id in ready_linked_flee_actor_ids
                    ],
                    "branch_id": branch["id"],
                    "expected_revision": campaign["revision"],
                    "idempotency_key": (
                        "encounter-source-linked-flee-"
                        + _operation_token(args, *ready_linked_flee_actor_ids)
                    ),
                },
            )
            fled_hostile_ids.update(ready_linked_flee_actor_ids)
            turns.extend(
                {
                    "sequence": sequence,
                    "kind": "source_flee",
                    "actor_id": actor_id,
                    "trigger": "source_actor_fled",
                    "trigger_actor_id": linked_flee_trigger_actor_id,
                    "source_excerpt": str(args.linked_flee_source_excerpt).strip(),
                    "map_patch": linked_escape,
                }
                for actor_id in ready_linked_flee_actor_ids
            )
        unresolved_party = [
            actor_id
            for actor_id in effective_party_ids
            if _hit_points(actors[actor_id]) == 0
            and not _conditions(actors[actor_id]) & DEATH_SAVE_SETTLED_CONDITIONS
        ]
        party_down = bool(potential_party_ids) and all(
            _hit_points(actors[actor_id]) <= 0
            for actor_id in potential_party_ids
        )
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
        pending_before = _pending_window(combat)
        pending_result = await _resolve_pending(
            client,
            args,
            str(branch["id"]),
            combat,
        )
        if pending_result is not None:
            if pending_result.get("status") == "pending_ruling":
                raise EncounterRulingRequiredError(
                    pending_result,
                    operation="combat.pending.resolve",
                    actor_id=str(dict(pending_before or {}).get("actor_id") or ""),
                    action={
                        "choice_id": str(
                            dict(pending_before or {}).get("id") or ""
                        ),
                        "kind": str(
                            dict(pending_before or {}).get("kind") or ""
                        ),
                        "trigger": str(
                            dict(pending_before or {}).get("trigger") or ""
                        ),
                    },
                    retry_hint=(
                        "Supply the requested Agent source-or-scene fact and retry "
                        "the same public encounter run."
                    ),
                )
            combat_after_pending = await client.domain(
                "combat_query",
                {"campaign_id": args.campaign_id, "view": "status"},
            )
            if pending_before is not None and not _pending_resolution_made_progress(
                pending_before,
                combat_after_pending,
            ):
                raise RuntimeError(
                    "public pending-window settlement returned without advancing "
                    f"{pending_before.get('id')}; refusing to spin until max-turns"
                )
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
                actor=actor,
                flee_at_hp=args.flee_at_hp,
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
                    "trigger_current_hp": _hit_points(actor),
                    "trigger_hp_threshold": (args.flee_at_hp or None),
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
        agent_object_interaction = agent_object_interactions.get(
            (actor_id, round_number)
        )
        active_object_condition = (
            next(
                (
                    item
                    for item in combat.get("source_conditions", [])
                    if isinstance(item, dict)
                    and item.get("active", True)
                    and str(item.get("actor_id") or "") == actor_id
                    and str(item.get("condition") or "").casefold()
                    == str(agent_object_interaction["condition"]).casefold()
                    and item.get("source_ref")
                    == agent_object_interaction["source_ref"]
                    and _normalized_source_text(
                        str(item.get("source_excerpt") or "")
                    )
                    == _normalized_source_text(
                        str(agent_object_interaction["source_excerpt"])
                    )
                ),
                None,
            )
            if agent_object_interaction is not None
            else None
        )
        if (
            agent_object_interaction is not None
            and active_object_condition is not None
            and agent_object_interaction["condition"] in actor_conditions
            and _hit_points(actor) > 0
            and not actor_conditions & INCAPACITATING_STATE_IDS
        ):
            campaign = await _campaign(client, args.campaign_id)
            interacted = await client.domain(
                "combat_common_action",
                {
                    "campaign_id": args.campaign_id,
                    "actor_id": actor_id,
                    "action": "interact_object",
                    "payload": {
                        "object_description": agent_object_interaction[
                            "object_description"
                        ],
                        "interaction": agent_object_interaction["interaction"],
                        "remove_source_condition": agent_object_interaction[
                            "condition"
                        ],
                        "source_ref": agent_object_interaction["source_ref"],
                        "source_excerpt": agent_object_interaction[
                            "source_excerpt"
                        ],
                        "agent_ruling": agent_object_interaction["agent_ruling"],
                    },
                    "branch_id": branch["id"],
                    "expected_revision": campaign["revision"],
                    "idempotency_key": (
                        "encounter-agent-object-interaction-"
                        + _operation_token(
                            args,
                            actor_id,
                            round_number,
                            agent_object_interaction["condition"],
                        )
                    ),
                },
            )
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "agent_object_interaction",
                    "actor_id": actor_id,
                    "round": round_number,
                    "declaration": deepcopy(agent_object_interaction),
                    "result": interacted,
                }
            )
            continue
        scheduled_content = _scheduled_content_solution(
            content_solutions,
            actor_id=actor_id,
            round_number=round_number,
        )
        if (
            scheduled_content is not None
            and _hit_points(actor) > 0
            and not actor_conditions & INCAPACITATING_STATE_IDS
        ):
            solution, activation = scheduled_content
            casting_perception = dict(
                agent_casting_perception_rulings.get(actor_id) or {}
            )
            activated = await _activate_content_solution(
                client,
                args,
                branch_id=str(branch["id"]),
                solution=solution,
                activation=activation,
                component_ruling=(
                    deepcopy(casting_perception["component_ruling"])
                    if casting_perception
                    else None
                ),
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
                    "kind": "content_solution_action",
                    "actor_id": actor_id,
                    "round": round_number,
                    "result": activated,
                    "end_turn": ended_turn,
                }
            )
            continue
        agent_turn_ruling = agent_turn_rulings.get((actor_id, round_number))
        if (
            agent_turn_ruling is not None
            and agent_turn_ruling["application_id"]
            not in completed_agent_turn_ruling_ids
            and _hit_points(actor) > 0
            and not actor_conditions & INCAPACITATING_STATE_IDS
        ):
            settled_ruling = await _settle_agent_turn_ruling(
                client,
                args,
                branch_id=str(branch["id"]),
                ruling=agent_turn_ruling,
            )
            completed_agent_turn_ruling_ids.add(
                str(agent_turn_ruling["application_id"])
            )
            if settled_ruling.get("forced_target_id"):
                agent_forced_targets[str(settled_ruling["target_id"])] = {
                    "application_id": str(settled_ruling["application_id"]),
                    "target_id": str(settled_ruling["forced_target_id"]),
                    "source_actor_id": actor_id,
                    "ends_if_source_incapacitated": bool(
                        settled_ruling["ends_if_source_incapacitated"]
                    ),
                }
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
                    "kind": "agent_turn_ruling",
                    "actor_id": actor_id,
                    "round": round_number,
                    "result": settled_ruling,
                    "end_turn": ended_turn,
                }
            )
            if settled_ruling.get("combat_outcome"):
                outcome_status = str(
                    settled_ruling["combat_outcome"]["status"]
                )
                outcome_summary = str(
                    settled_ruling["combat_outcome"]["summary"]
                )
                break
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
            and not actor_conditions & DEATH_SAVE_SETTLED_CONDITIONS
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
                if not _destination_within_range(
                    destination[0],
                    target_position,
                    range_ft=5,
                ):
                    turns.append(
                        {
                            "sequence": sequence,
                            "kind": "stabilize_approach",
                            "actor_id": actor_id,
                            "target_id": stabilization_target_id,
                            "planned_path": destination[2],
                            "avoided_cells": sorted(
                                avoided_cells_by_actor.get(actor_id, set())
                            ),
                            "move": moved,
                            "end_turn": await _end_turn(
                                client,
                                args,
                                str(branch["id"]),
                                actor_id,
                                sequence,
                            ),
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
        forced_target = agent_forced_targets.get(actor_id)
        if (
            forced_target is not None
            and forced_target.get("ends_if_source_incapacitated")
        ):
            source_actor = actors.get(str(forced_target.get("source_actor_id") or ""))
            if source_actor is None or _hit_points(source_actor) <= 0 or (
                _conditions(source_actor) & INCAPACITATING_STATE_IDS
            ):
                expired = await _consume_agent_forced_target(
                    client,
                    args,
                    branch_id=str(branch["id"]),
                    actor_id=actor_id,
                    target_id=str(forced_target["target_id"]),
                    forced_targets=agent_forced_targets,
                    reason=(
                        "The source-bound effect ended before the directed attack "
                        "because its source became unable to sustain it."
                    ),
                )
                turns.append(
                    {
                        "sequence": sequence,
                        "kind": "agent_forced_target_expired",
                        "actor_id": actor_id,
                        "result": expired,
                    }
                )
                forced_target = None
        if (
            forced_target is not None
            and _hit_points(actors[forced_target["target_id"]]) > 0
        ):
            opponents = [forced_target["target_id"]]
        else:
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
        if len(living_targets) > 1 and actor_id not in target_priorities:
            raise EncounterRulingRequiredError(
                {
                    "status": "pending_ruling",
                    "default_resolver": "agent",
                    "ruling_kind": "agent_dm_adjudication",
                    "reason": (
                        "the current actor has multiple observable legal opponents "
                        "but no explicit Agent or source-authored target order"
                    ),
                    "committed": False,
                    "missing": ["agent_target_priority"],
                    "retry_contract": {
                        "resolver": "agent",
                        "reuse_current_revision": True,
                        "use_public_tools_only": True,
                    },
                },
                operation="encounter.auto_run.choose_target",
                actor_id=actor_id,
                target_id="",
                action={"candidate_target_ids": living_targets},
                retry_hint=(
                    "Retry with --agent-target-priority-json enumerating every "
                    "opponent in the Agent's exact order."
                ),
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
        spell_choice = _choose_agent_spell(
            actor_id,
            party_ids=effective_party_ids,
            actors=actors,
            living_targets=spell_targets,
            spell_choices=list(
                dict(agent_spell_priorities.get(actor_id) or {}).get(
                    "choices", []
                )
            ),
            combat=combat,
            leveled_spell_available=not bool(
                dict(combatants[actor_id].get("turn_flags") or {}).get("cast_declared")
            ),
        )
        if spell_choice is not None:
            spell_id, spell_target_id, cast_level = spell_choice[:3]
            area_declaration = (
                deepcopy(spell_choice[3]) if len(spell_choice) == 4 else None
            )
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
            elif area_declaration is not None:
                cast_arguments["declaration"] = area_declaration
            else:
                spell_card = next(
                    (
                        item
                        for item in dict(
                            dict(actors[actor_id].get("sheet") or {}).get(
                                "content"
                            )
                            or {}
                        ).get("spells", [])
                        if isinstance(item, dict)
                        and str(item.get("id") or "") == spell_id
                    ),
                    None,
                )
                single_target_declaration = (
                    _safe_single_target_spell_declaration(
                        dict(spell_card or {}),
                        target_id=spell_target_id,
                    )
                )
                if single_target_declaration is not None:
                    cast_arguments["declaration"] = single_target_declaration
            casting_perception_decision = dict(
                agent_casting_perception_rulings.get(actor_id) or {}
            )
            if casting_perception_decision:
                cast_arguments["component_ruling"] = deepcopy(
                    casting_perception_decision["component_ruling"]
                )
            cast = await client.domain("combat_cast_spell", cast_arguments)
            spell_result: dict[str, Any] = {"cast": cast}
            if casting_perception_decision:
                spell_result["agent_casting_perception_ruling"] = (
                    casting_perception_decision
                )
            if cast.get("status") == "pending_ruling":
                raise EncounterRulingRequiredError(
                    cast,
                    operation="combat_cast_spell",
                    actor_id=actor_id,
                    target_id=spell_target_id,
                    action={
                        "spell_id": spell_id,
                        "cast_level": cast_level,
                    },
                    retry_hint=(
                        "Inspect the active scene and retry with an explicit "
                        "--agent-casting-perception-json observer matrix."
                    ),
                )
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
                if settled.get("status") not in {
                    "committed",
                    "pending_reaction",
                }:
                    raise RuntimeError(
                        "Guiding Bolt spell attack did not commit or open a supported reaction"
                    )
            elif cast.get("status") not in {"committed", "pending_reaction"}:
                raise RuntimeError(f"{spell_id} did not commit through structured spell settlement")
            forced_target_consumption = await _consume_agent_forced_target(
                client,
                args,
                branch_id=str(branch["id"]),
                actor_id=actor_id,
                target_id=spell_target_id,
                forced_targets=agent_forced_targets,
            )
            if forced_target_consumption is not None:
                spell_result["agent_forced_target_consumption"] = (
                    forced_target_consumption
                )
            area_target_ids = (
                _area_spell_target_ids(area_declaration, cast)
                if area_declaration is not None
                else []
            )
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "spell",
                    "actor_id": actor_id,
                    "spell_id": spell_id,
                    "cast_level": cast_level,
                    "target_id": spell_target_id,
                    **(
                        {
                            "target_ids": area_target_ids,
                            "area_declaration": deepcopy(area_declaration),
                        }
                        if area_declaration is not None
                        else {}
                    ),
                    "result": spell_result,
                    "source_flee_observations": source_flee_observations,
                }
            )
            if _spell_cast_blocks_turn_progress(
                cast,
                pending_reaction=pending_reaction,
            ):
                # Damage from one spell can open one or more server-owned
                # concentration windows. They must settle before the caster
                # takes another action or ends the turn.
                continue
            if _has_action_budget(dict(cast.get("combat") or {}), actor_id):
                # The server budget is authoritative: a bonus-action spell such as
                # Healing Word leaves a main action available. The cast-declared
                # guard above still prevents a second leveled spell this turn.
                continue
            await _end_turn(client, args, str(branch["id"]), actor_id, sequence)
            continue
        agent_common_action_priority = dict(
            agent_common_action_priorities.get(actor_id) or {}
        )
        common_action = next(
            (
                str(choice["action"])
                for choice in agent_common_action_priority.get("choices", [])
                if str(choice["action"]) in available_actions
            ),
            "",
        )
        if common_action:
            campaign = await _campaign(client, args.campaign_id)
            common_result = await client.domain(
                "combat_common_action",
                {
                    "campaign_id": args.campaign_id,
                    "actor_id": actor_id,
                    "action": common_action,
                    "branch_id": branch["id"],
                    "expected_revision": campaign["revision"],
                    "idempotency_key": (
                        "encounter-agent-common-action-"
                        + _operation_token(
                            args,
                            sequence,
                            actor_id,
                            common_action,
                        )
                    ),
                },
            )
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "agent_common_action",
                    "actor_id": actor_id,
                    "action": common_action,
                    "agent_ruling": deepcopy(
                        agent_common_action_priority["agent_ruling"]
                    ),
                    "result": common_result,
                }
            )
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
        required_source_opening_weapon = _required_source_opening_weapon(
            opening_weapons,
            actor_id=actor_id,
            completed_actor_ids=completed_opening_weapon_actor_ids,
        )
        agent_weapon_priority = dict(
            agent_weapon_priorities.get(actor_id) or {}
        )
        agent_weapon_choices = list(agent_weapon_priority.get("choices") or [])
        if required_source_opening_weapon is None and not agent_weapon_choices:
            raise EncounterRulingRequiredError(
                {
                    "status": "pending_ruling",
                    "default_resolver": "agent",
                    "ruling_kind": "agent_dm_adjudication",
                    "reason": (
                        "the current actor has no explicit Agent weapon policy "
                        "after all higher-priority source actions and spell policies"
                    ),
                    "committed": False,
                    "missing": ["agent_weapon_priority"],
                    "retry_contract": {
                        "resolver": "agent",
                        "reuse_current_revision": True,
                        "use_public_tools_only": True,
                    },
                },
                operation="encounter.auto_run.choose_action",
                actor_id=actor_id,
                target_id=(living_targets[0] if living_targets else ""),
                action={"round": int(combat.get("round", 1) or 1)},
                retry_hint=(
                    "Retry with --agent-weapon-priority-json containing ordered "
                    "weapon/mode choices and the Agent's decision and reasoning."
                ),
            )
        preferred_weapon_id = ""
        if required_source_opening_weapon is not None:
            preferred_weapon_id = required_source_opening_weapon["weapon_id"]
        active_multiattack = bool(
            dict(combatants[actor_id].get("turn_flags") or {}).get("multiattack")
        )
        active_agent_weapon_choices = (
            [
                {
                    **choice,
                    **(
                        {"multiattack_option_id": ""}
                        if active_multiattack
                        else {}
                    ),
                }
                for choice in agent_weapon_choices
            ]
            if required_source_opening_weapon is None
            else []
        )
        reaction_available_ids = _reaction_available_actor_ids(combat)
        preflight_rejections: list[dict[str, str]] = []
        plan = await _preflight_attack(
            client,
            args,
            actor,
            living_targets,
            preferred_weapon_id=preferred_weapon_id,
            multiattack_option_id="",
            agent_weapon_choices=active_agent_weapon_choices,
            action_context=None,
            agent_attack_contexts=agent_attack_contexts,
            agent_target_reaction_contexts=agent_target_reaction_contexts,
            reaction_available_actor_ids=reaction_available_ids,
            knock_out_target_ids=(
                knock_out_hostile_ids if actor_id in effective_party_ids else None
            ),
            agent_rulings=agent_preflight_rulings,
            source_ammunition_selections=source_ammunition_selections,
            require_preferred_weapon=required_source_opening_weapon is not None,
            preflight_rejections=preflight_rejections,
            round_number=int(combat.get("round", 1) or 1),
            combat=combat,
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
                        "idempotency_key": (
                            "encounter-move-"
                            + _movement_operation_token(
                                args,
                                sequence=sequence,
                                actor_id=actor_id,
                                target_id=living_targets[0],
                                destination=destination,
                            )
                        ),
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
                movement_combat = dict(moved.get("combat") or combat)
                reaction_available_ids = _reaction_available_actor_ids(
                    movement_combat
                )
                plan = await _preflight_attack(
                    client,
                    args,
                    actor,
                    living_targets,
                    preferred_weapon_id=preferred_weapon_id,
                    multiattack_option_id="",
                    agent_weapon_choices=active_agent_weapon_choices,
                    action_context=None,
                    agent_attack_contexts=agent_attack_contexts,
                    agent_target_reaction_contexts=(
                        agent_target_reaction_contexts
                    ),
                    reaction_available_actor_ids=reaction_available_ids,
                    knock_out_target_ids=(
                        knock_out_hostile_ids if actor_id in effective_party_ids else None
                    ),
                    agent_rulings=agent_preflight_rulings,
                    source_ammunition_selections=source_ammunition_selections,
                    require_preferred_weapon=required_source_opening_weapon is not None,
                    preflight_rejections=preflight_rejections,
                    round_number=int(combat.get("round", 1) or 1),
                    combat=movement_combat,
                )
        if plan is None and required_source_opening_weapon is not None:
            raise RuntimeError(
                "source opening weapon has no legal target after movement "
                f"(actor_id={actor_id}, "
                f"weapon_id={required_source_opening_weapon['weapon_id']}, "
                f"rejections={preflight_rejections})"
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
            target_reaction_context = dict(
                agent_target_reaction_contexts.get(
                    (target_id, str(action.get("attack_mode") or "melee"))
                )
                or {}
            )
            if (
                target_reaction_context
                and target_id in reaction_available_ids
            ):
                reaction_result = await _consume_agent_target_reaction(
                    client,
                    args,
                    branch_id=str(branch["id"]),
                    context=target_reaction_context,
                    attacker_id=actor_id,
                    sequence=sequence,
                )
                turns.append(
                    {
                        "sequence": sequence,
                        "kind": "agent_target_reaction",
                        "actor_id": target_id,
                        "attacker_id": actor_id,
                        "result": reaction_result,
                    }
                )
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
            content_solution_settlement = None
            if resolved.get("status") == "pending_ruling":
                pending_combat = dict(resolved.get("combat") or {})
                pending = _pending_window(pending_combat)
                if pending is None:
                    raise EncounterRulingRequiredError(
                        resolved,
                        operation="combat_resolve_attack",
                        actor_id=actor_id,
                        target_id=target_id,
                        action=action,
                        retry_hint=(
                            "Inspect the typed missing facts and let the Agent author "
                            "a source-bound solution when the card is custom."
                        ),
                    )
                content_solution_settlement = await _resolve_pending(
                    client,
                    args,
                    str(branch["id"]),
                    pending_combat,
                )
            forced_target_consumption = await _consume_agent_forced_target(
                client,
                args,
                branch_id=str(branch["id"]),
                actor_id=actor_id,
                target_id=target_id,
                forced_targets=agent_forced_targets,
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
                    "content_solution_settlement": content_solution_settlement,
                    "agent_forced_target_consumption": forced_target_consumption,
                }
            )
            settlement_combat = (
                dict(content_solution_settlement.get("combat") or {})
                if content_solution_settlement is not None
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
    await client.load()
    checkpoint = None
    if _source_outcome_allows_checkpoint(outcome_status):
        checkpoint = await _checkpoint(
            client,
            campaign_id=args.campaign_id,
            run_id=args.run_id,
            label=args.checkpoint_label,
            checkpoint_id=f"encounter:{str(ended['combat']['id'])}",
        )
    final_actor_ids = [*party_ids, *hostile_ids]
    final_actor_values = await _characters(client, args.campaign_id, final_actor_ids)
    captured_hostile_ids = _captured_hostile_ids(
        final_actor_values,
        candidate_ids=knock_out_hostile_ids,
    )
    if minimum_hostile_knockouts is None:
        if captured_hostile_ids != knock_out_hostile_ids:
            raise RuntimeError("designated knockout hostile was not captured unconscious and alive")
    elif len(captured_hostile_ids) < minimum_hostile_knockouts:
        raise RuntimeError(
            "encounter did not satisfy the Agent-selected minimum hostile knockout objective"
        )
    final_actors = [
        _character_summary(final_actor_values[actor_id]) for actor_id in final_actor_ids
    ]
    return {
        "combat_exposure": opened_combat,
        "visibility_patch": visibility_patch,
        "turns": turns,
        "fled_hostile_ids": sorted(fled_hostile_ids),
        "source_flee_damage_taken": dict(sorted(damage_taken_by_flee_actor.items())),
        "source_flee_hp_threshold": (args.flee_at_hp or None),
        "source_flee_critical_hit_actor_ids": sorted(critical_hit_flee_actor_ids),
        "linked_source_flee": {
            "actor_ids": sorted(linked_flee_actor_ids),
            "trigger_actor_id": linked_flee_trigger_actor_id or None,
            "source_excerpt": str(
                getattr(args, "linked_flee_source_excerpt", "") or ""
            ).strip(),
        },
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
        "agent_weapon_priorities": list(agent_weapon_priorities.values()),
        "agent_spell_priorities": list(agent_spell_priorities.values()),
        "agent_common_action_priorities": list(
            agent_common_action_priorities.values()
        ),
        "source_ammunition_selections": list(source_ammunition_selections.values()),
        "source_delayed_actions": list(delayed_actions.values()),
        "source_passive_allies": list(passive_allies.values()),
        "content_solutions": list(content_solutions.values()),
        "agent_attack_contexts": list(agent_attack_contexts.values()),
        "agent_casting_perception_rulings": list(
            agent_casting_perception_rulings.values()
        ),
        "agent_target_reaction_contexts": list(
            agent_target_reaction_contexts.values()
        ),
        "agent_turn_rulings": list(agent_turn_rulings.values()),
        "agent_object_interactions": list(agent_object_interactions.values()),
        "pending_agent_forced_targets": deepcopy(agent_forced_targets),
        "agent_preflight_rulings": agent_preflight_rulings,
        "source_avoidances": source_avoidance_evidence,
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
        "knock_out_candidate_ids": sorted(knock_out_hostile_ids),
        "minimum_hostile_knockouts": minimum_hostile_knockouts,
        "knocked_out_hostile_ids": sorted(captured_hostile_ids),
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
    await client.load()
    campaign = await _campaign(client, args.campaign_id)
    combat = dict(dict(campaign.get("state") or {}).get("combat") or {})
    outcome = dict(combat.get("outcome") or {})
    if (
        not combat
        or combat.get("active", True)
        or outcome.get("status")
        not in COMBAT_OUTCOME_STATUSES
    ):
        raise RuntimeError("campaign does not retain a completed encounter with a source outcome")
    if args.scene_id and str(combat.get("scene_id") or "") != str(args.scene_id):
        raise RuntimeError("completed encounter scene does not match --scene-id")
    requested_name = str(getattr(args, "encounter_name", "") or "").strip()
    if requested_name and str(combat.get("name") or "") != requested_name:
        raise RuntimeError("completed encounter name does not match --encounter-name")
    if _retained_combat_actor_ids(combat) != set(actor_ids):
        raise RuntimeError(
            "completed encounter does not match the requested encounter participants"
        )
    checkpoint = None
    if _source_outcome_allows_checkpoint(str(outcome.get("status") or "")):
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


def _retained_combat_actor_ids(combat: dict[str, Any]) -> set[str]:
    return {
        str(item.get("actor_id") or "").strip()
        for collection in (
            combat.get("combatants") or [],
            combat.get("reinforcements") or [],
        )
        for item in collection
        if isinstance(item, dict) and str(item.get("actor_id") or "").strip()
    }


def _retained_combat_matches_requested_encounter(
    combat: dict[str, Any],
    args: argparse.Namespace,
    *,
    party_ids: list[str],
    hostile_ids: list[str],
    additional_hostile_ids: list[str],
    reinforcement_hostile_ids: list[str],
    reinforcement_ally_ids: list[str],
) -> bool:
    """Only resume/finalize the exact encounter selected by this invocation."""

    requested_actor_ids = {
        *party_ids,
        *hostile_ids,
        *additional_hostile_ids,
        *reinforcement_hostile_ids,
        *reinforcement_ally_ids,
    }
    retained_actor_ids = _retained_combat_actor_ids(combat)
    if retained_actor_ids != requested_actor_ids:
        return False
    requested_scene_id = str(getattr(args, "scene_id", "") or "").strip()
    if requested_scene_id and str(combat.get("scene_id") or "") != requested_scene_id:
        return False
    requested_name = str(getattr(args, "encounter_name", "") or "").strip()
    if requested_name and str(combat.get("name") or "") != requested_name:
        return False
    manifest = dict(combat.get("participant_manifest") or {})
    if manifest:
        retained_source_ids = {
            str(item).strip()
            for key in ("initial_actor_ids", "reinforcement_actor_ids")
            for item in manifest.get(key) or []
            if str(item).strip()
        }
        requested_source_ids = {
            *hostile_ids,
            *additional_hostile_ids,
            *reinforcement_hostile_ids,
            *reinforcement_ally_ids,
        }
        if retained_source_ids != requested_source_ids:
            return False
    return True


def _missing_source_reinforcement_ids(
    combat: dict[str, Any],
    *,
    scene_id: str,
    reinforcement_ids: list[str],
) -> list[str]:
    """Return only source reinforcements absent from a matching live encounter."""

    if not combat.get("active"):
        raise RuntimeError("reinforcement recovery requires an active combat")
    if scene_id and str(combat.get("scene_id") or "") != scene_id:
        raise RuntimeError("active combat scene does not match reinforcement recovery scene")
    manifest = dict(combat.get("participant_manifest") or {})
    declared_ids = [
        str(item)
        for item in manifest.get("reinforcement_actor_ids") or []
        if str(item)
    ]
    if declared_ids != reinforcement_ids:
        raise RuntimeError(
            "active combat reinforcement manifest does not match configured source actors"
        )
    existing_ids = {
        str(item.get("actor_id") or "")
        for item in [
            *list(combat.get("combatants") or []),
            *list(combat.get("reinforcements") or []),
        ]
        if isinstance(item, dict)
    }
    return [
        actor_id
        for actor_id in reinforcement_ids
        if actor_id not in existing_ids
    ]


async def _resume_source_reinforcements(
    client: ExposureClient,
    args: argparse.Namespace,
    *,
    party_ids: list[str],
    initial_hostile_ids: list[str],
    reinforcement_hostile_ids: list[str],
    reinforcement_ally_ids: list[str],
) -> list[dict[str, Any]]:
    """Idempotently complete a source reinforcement queue after partial startup."""

    reinforcement_ids = [
        *reinforcement_hostile_ids,
        *reinforcement_ally_ids,
    ]
    _agent_reinforcement_triggers(
        getattr(args, "agent_reinforcement_trigger_json", []),
        reinforcement_ids=reinforcement_ids,
        reinforcement_round=int(args.reinforcement_round or 0),
        encounter_source_excerpt=str(args.source_excerpt or ""),
    )
    if not reinforcement_ids:
        return []
    await client.load()
    combat = await client.domain(
        "combat_query",
        {"campaign_id": args.campaign_id, "view": "status"},
    )
    missing_ids = _missing_source_reinforcement_ids(
        combat,
        scene_id=str(args.scene_id or ""),
        reinforcement_ids=reinforcement_ids,
    )
    if not missing_ids:
        return []
    all_hostile_ids = [*initial_hostile_ids, *reinforcement_hostile_ids]
    all_party_ids = [*party_ids, *reinforcement_ally_ids]
    all_participant_ids = [*all_party_ids, *all_hostile_ids]
    source_conditions_by_actor = _source_declared_conditions(
        args.source_condition_json,
        participant_ids=all_participant_ids,
    )
    branch = await _current_branch(client, args.campaign_id)
    recovered: list[dict[str, Any]] = []
    for actor_id in missing_ids:
        index = reinforcement_ids.index(actor_id)
        campaign = await _campaign(client, args.campaign_id)
        tie_breaker = len(party_ids) + len(initial_hostile_ids) + index
        disposition = (
            "hostile"
            if actor_id in set(reinforcement_hostile_ids)
            else "friendly"
        )
        recovered.append(
            await client.domain(
                "combat_join",
                {
                    "campaign_id": args.campaign_id,
                    "actor_id": actor_id,
                    "participant_config": _reinforcement_config(
                        actor_id,
                        index,
                        disposition=disposition,
                        join_round=int(args.reinforcement_round or 0),
                        tie_breaker=tie_breaker,
                        source_conditions=source_conditions_by_actor.get(actor_id),
                    ),
                    "branch_id": branch["id"],
                    "expected_revision": campaign["revision"],
                    "idempotency_key": (
                        "encounter-queue-reinforcement-"
                        + _operation_token(args, actor_id)
                    ),
                },
            )
        )
    return recovered


async def _start_or_resume_auto_run(
    client: ExposureClient,
    args: argparse.Namespace,
    party_ids: list[str],
    hostile_ids: list[str],
    additional_hostile_ids: list[str],
    reinforcement_hostile_ids: list[str],
    reinforcement_ally_ids: list[str],
) -> dict[str, Any]:
    opened = await client.open(args.campaign_id)
    phase = str(opened.get("phase") or "")
    started: dict[str, Any] | None = None
    recovered_reinforcement_queue: list[dict[str, Any]] = []
    if phase == "play":
        campaign = await _campaign(client, args.campaign_id)
        retained_combat = dict(
            dict(campaign.get("state") or {}).get("combat") or {}
        )
        retained_outcome = dict(retained_combat.get("outcome") or {})
        if (
            retained_combat
            and retained_combat.get("active") is False
            and retained_outcome.get("status") in COMBAT_OUTCOME_STATUSES
            and _retained_combat_matches_requested_encounter(
                retained_combat,
                args,
                party_ids=party_ids,
                hostile_ids=hostile_ids,
                additional_hostile_ids=additional_hostile_ids,
                reinforcement_hostile_ids=reinforcement_hostile_ids,
                reinforcement_ally_ids=reinforcement_ally_ids,
            )
        ):
            return await _finalize_ended_encounter(
                client,
                args,
                [
                    *party_ids,
                    *reinforcement_ally_ids,
                    *hostile_ids,
                    *additional_hostile_ids,
                    *reinforcement_hostile_ids,
                ],
            )
        started = await _start(
            client,
            args,
            party_ids,
            hostile_ids,
            additional_hostile_ids,
            reinforcement_hostile_ids,
            reinforcement_ally_ids,
        )
    elif phase == "combat":
        campaign = await _campaign(client, args.campaign_id)
        retained_combat = dict(
            dict(campaign.get("state") or {}).get("combat") or {}
        )
        if not _retained_combat_matches_requested_encounter(
            retained_combat,
            args,
            party_ids=party_ids,
            hostile_ids=hostile_ids,
            additional_hostile_ids=additional_hostile_ids,
            reinforcement_hostile_ids=reinforcement_hostile_ids,
            reinforcement_ally_ids=reinforcement_ally_ids,
        ):
            raise RuntimeError(
                "active combat does not match the requested encounter participants"
            )
        recovered_reinforcement_queue = await _resume_source_reinforcements(
            client,
            args,
            party_ids=party_ids,
            initial_hostile_ids=[*hostile_ids, *additional_hostile_ids],
            reinforcement_hostile_ids=reinforcement_hostile_ids,
            reinforcement_ally_ids=reinforcement_ally_ids,
        )
    elif phase != "combat":
        raise RuntimeError(
            "auto-run requires the play phase or an active combat; "
            f"campaign is in {phase or 'an unknown phase'}"
        )
    completed = await _auto_run(
        client,
        args,
        [*party_ids, *reinforcement_ally_ids],
        [
            *hostile_ids,
            *additional_hostile_ids,
            *reinforcement_hostile_ids,
        ],
    )
    if started is not None:
        completed["auto_start"] = started
    if recovered_reinforcement_queue:
        completed["recovered_reinforcement_queue"] = recovered_reinforcement_queue
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
        await client.load()
        combat = await client.domain(
            "combat_query",
            {"campaign_id": campaign_id, "view": "status"},
        )
    elif phase == "play":
        await client.load()
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
    reinforcement_ally_ids = actor_groups["reinforcement_ally_ids"]
    all_friendly_ids = [*friendly_ids, *reinforcement_ally_ids]
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
        "reinforcement_ally_ids": reinforcement_ally_ids,
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
                    reinforcement_ally_ids,
                )
            elif args.action == "auto-run":
                report["result"] = await _start_or_resume_auto_run(
                    client,
                    args,
                    friendly_ids,
                    hostile_ids,
                    additional_hostile_ids,
                    reinforcement_hostile_ids,
                    reinforcement_ally_ids,
                )
            elif args.action == "finalize":
                report["result"] = await _finalize_ended_encounter(
                    client,
                    args,
                    [*all_friendly_ids, *all_hostile_ids],
                )
            else:
                actor_ids = [*all_friendly_ids, *all_hostile_ids]
                report["result"] = await _status(
                    client,
                    campaign_id=args.campaign_id,
                    actor_ids=actor_ids,
                )
    report["passed"] = True
    return report


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
        with campaign_operation_lock(args.home, args.campaign_id):
            report = asyncio.run(_run(args))
    except Exception as error:
        ruling_requirements = _leaf_ruling_requirements(error)
        report = {
            "action": args.action,
            "campaign_id": args.campaign_id,
            "run_id": args.run_id,
            "passed": False,
            "error": "; ".join(exception_leaf_messages(error)),
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
