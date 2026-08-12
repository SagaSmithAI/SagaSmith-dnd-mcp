"""Drive a resumable full campaign exclusively through public stdio MCP tools.

The driver never imports the server implementation and never reads the database.
It maintains the snapshot-managed playthrough manifest, verifies scene ownership,
registers already-created legal parties, creates checkpoints, and verifies only
source-declared machine-readable endings.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from sagasmith_core.continuity_commit import FACT_KEY_WRITE_ACTIONS
from sagasmith_core.documents import file_sha256
from sagasmith_core.idempotency import request_hash
from sagasmith_core.modules import (
    EXACT_MODULE_SOURCE_FIELD_ORDER,
    EXACT_MODULE_SOURCE_FIELDS,
)
from sagasmith_core.modules import (
    normalize_source_evidence_text as _normalized_source_text,
)
from sagasmith_dnd.character_schema import effective_ability_modifier
from sagasmith_dnd.combat_engine import (
    ACTOR_CHECK_KINDS,
    damage_amount_after_reduction,
)
from sagasmith_dnd.game_time import (
    CALENDAR_MINUTE_FIELDS,
    NARRATIVE_GAME_TIME_PERIODS,
    TICKS_PER_MINUTE,
    advance_calendar_minutes_from_elapsed,
    calendar_minute_point,
    calendar_minute_point_from_elapsed,
    game_time_ticks,
    validate_calendar_minute_point,
)
from sagasmith_dnd.lifecycle import allows_trance_rest, minimum_rest_minutes
from sagasmith_dnd.module_profile import DndModuleProfile
from sagasmith_dnd.playthrough import (
    PARTY_MEMBER_SOURCES,
    PLAYTHROUGH_SOURCE_FIELDS,
    validate_playthrough_manifest,
    validate_source_defined_ending_condition,
)
from sagasmith_dnd.spell_resolution import scaled_roll_expression
from sagasmith_dnd.standard_spell_ids import (
    CORE_FLY_SPELL_ID,
    CORE_INVISIBILITY_SPELL_ID,
)
from sagasmith_dnd.vocabulary import (
    ADVANCEMENT_MODES,
    CAMPAIGN_GAME_PHASES,
    DENOMINATIONS,
    EFFECTIVE_GAME_PHASES,
)

from scripts.regression_full_campaigns import (
    _initialize_playthrough_manifest,
    _line_review_blocks,
    _load_and_verify_manifest,
)
from scripts.regression_lock import campaign_operation_lock
from scripts.regression_modules import (
    PRINCIPAL_ID,
    ExposureClient,
    _facade_value,
    _token,
    campaign_view,
)
from scripts.regression_rulings import (
    RegressionRulingRequiredError,
    normalize_pending_ruling,
    raise_for_pending_ruling,
    ruling_failure_fields,
)
from scripts.regression_runtime import (
    exception_leaf_messages,
    regression_server_parameters,
    required_core_relock_reason,
)

DEFERRED_CHECKPOINT_ACTIONS = frozenset(
    {
        "advance-time",
        "apply-damage",
        "roll-source",
        "resolve-check",
        "resolve-group-check",
        "resolve-contest",
        "initialize-source-state",
        "stand-up",
        "use-activity",
        "cast-spell",
        "cast-source-spell",
        "cast-healing-spell",
        "revive-character",
        "record-event",
        "record-outcome",
        "register-replacement",
        "prepare-narrative-npc",
        "provision-source-item",
        "pool-coins",
        "distribute-coins",
        "transfer-source-item",
        "claim-party-item",
        "apply-source-effect",
        "remove-source-effect",
        "set-source-exhaustion",
        "attack-source-object",
        "acquire-loot",
        "spend-coins",
        "spend-item",
        "use-consumable",
        "advance-level",
        "sync-character-resources",
    }
)

KNOWLEDGE_ACTOR_PREFLIGHT_ACTIONS = frozenset(
    {
        "register-replacement",
        "resolve-check",
        "resolve-group-check",
        "resolve-contest",
        "apply-damage",
        "initialize-source-state",
        "stand-up",
        "use-activity",
        "cast-spell",
        "cast-source-spell",
        "cast-healing-spell",
        "advance-time",
        "recover-stable",
        "acquire-loot",
        "spend-coins",
        "spend-item",
        "use-consumable",
    }
)
EVENT_KNOWLEDGE_ACTOR_PREFLIGHT_ACTIONS = frozenset(
    {
        "record-event",
        "record-outcome",
    }
)


def _arguments() -> argparse.Namespace:
    repo = Path(__file__).resolve().parents[1]
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
            "continue-segment",
            "advance-scene",
            "record-event",
            "record-outcome",
            "resolve-check",
            "resolve-group-check",
            "resolve-contest",
            "initialize-source-state",
            "apply-damage",
            "stand-up",
            "use-activity",
            "cast-spell",
            "cast-source-spell",
            "cast-healing-spell",
            "revive-character",
            "branch-from-snapshot",
            "initialize-clock",
            "advance-time",
            "short-rest",
            "long-rest",
            "recover-stable",
            "provision-source-item",
            "pool-coins",
            "distribute-coins",
            "transfer-source-item",
            "claim-party-item",
            "apply-source-effect",
            "remove-source-effect",
            "set-source-exhaustion",
            "attack-source-object",
            "acquire-loot",
            "spend-coins",
            "spend-item",
            "use-consumable",
            "award-xp",
            "advance-level",
            "sync-character-resources",
            "configure-advancement",
            "relock-core",
            "refresh-module",
            "query-source",
            "index-source",
            "read-scene",
            "roll-source",
            "register-party",
            "initialize-manifest",
            "register-replacement",
            "prepare-narrative-npc",
            "configure-ending",
            "start-play",
            "verify-ending",
        ),
        default="status",
    )
    parser.add_argument("--run-id", default="full-playthrough-v1")
    parser.add_argument(
        "--corpus-root",
        type=Path,
        help="Root of the verified campaign corpus used by initialize-manifest",
    )
    parser.add_argument(
        "--corpus-manifest",
        type=Path,
        default=repo / "fixtures" / "full_campaign_corpus.json",
        help="Verified corpus metadata used by initialize-manifest",
    )
    parser.add_argument(
        "--campaign-import-report",
        type=Path,
        help=(
            "Prior public full-campaign import report whose module IDs are "
            "revalidated before initialize-manifest"
        ),
    )
    parser.add_argument("--campaign-line-id", default="")
    parser.add_argument("--advancement-mode", choices=tuple(sorted(ADVANCEMENT_MODES)))
    parser.add_argument("--core-relock-reason", default="")
    parser.add_argument("--module-root", type=Path)
    parser.add_argument("--module-source-path", type=Path)
    parser.add_argument("--module-source-key", default="")
    parser.add_argument("--module-title", default="")
    parser.add_argument("--module-id", default="")
    parser.add_argument(
        "--module-finalization-json",
        type=json.loads,
        help=(
            "Agent-reviewed module finalization containing portable_id, manifest, "
            "confirmation, and optional version/catalogs/dependencies/metadata/narrative"
        ),
    )
    parser.add_argument(
        "--module-progress-remap-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Agent-reviewed module revision progress remap with from_scene_id, "
            "to_scene_id, and reason; repeat for each removed progressed scene"
        ),
    )
    parser.add_argument(
        "--refresh-return-phase",
        choices=tuple(sorted(CAMPAIGN_GAME_PHASES)),
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
    parser.add_argument(
        "--occurrence-id",
        default="",
        help=(
            "Stable unique id for one occurrence of a repeatable mutation; "
            "reuse it only when retrying that exact occurrence"
        ),
    )
    parser.add_argument(
        "--defer-checkpoint",
        action="store_true",
        help=(
            "For supported scene-batch actions, persist without an action-local "
            "snapshot; use the public checkpoint action after the complete scene batch"
        ),
    )
    parser.add_argument("--scene-id")
    parser.add_argument(
        "--source-scene-id",
        default="",
        help=(
            "Scene containing the cited source when it differs from the scene where "
            "the action occurs"
        ),
    )
    parser.add_argument(
        "--occurrence-scene-id",
        default="",
        help=(
            "Current scene where a transition begins when its cited source text "
            "is indexed under a different scene"
        ),
    )
    parser.add_argument("--location-key", default="")
    parser.add_argument("--source-excerpt", default="")
    parser.add_argument(
        "--source-ref-json",
        type=json.loads,
        help="Exact module source reference for the playthrough action",
    )
    parser.add_argument(
        "--scene-agent-ruling-json",
        type=json.loads,
        help=(
            "Committed Agent DM adjudication for a descriptive transition "
            "not fully specified by the cited module text"
        ),
    )
    parser.add_argument("--check-actor-id", default="")
    parser.add_argument(
        "--group-check-actor-id",
        action="append",
        default=[],
        help="Participant in a standard group ability check; repeat for every member",
    )
    parser.add_argument(
        "--check-kind",
        choices=tuple(sorted(ACTOR_CHECK_KINDS)),
        default="",
        help="Public character_check kind; use ability with a skill name as --check-ability",
    )
    parser.add_argument("--check-ability", default="")
    parser.add_argument("--check-dc", type=int)
    parser.add_argument(
        "--check-bonus",
        type=int,
        default=0,
        help="Source-authored external bonus or penalty applied by character_check",
    )
    parser.add_argument(
        "--check-proficient",
        action="store_true",
        help=(
            "Apply proficiency only to a raw ability check; named skills derive "
            "none/half/proficient/expertise from the actor card"
        ),
    )
    parser.add_argument("--check-advantage", action="store_true")
    parser.add_argument("--check-disadvantage", action="store_true")
    parser.add_argument(
        "--check-agent-ruling-json",
        type=json.loads,
        help=(
            "Settled Agent-as-DM DC selection for a source-bound descriptive "
            "situation whose module text requires a check but prints no DC."
        ),
    )
    parser.add_argument("--knowledge-actor-id", action="append", default=[])
    parser.add_argument("--success-knowledge", default="")
    parser.add_argument("--failure-knowledge", default="")
    parser.add_argument("--contest-source-actor-id", default="")
    parser.add_argument("--contest-target-actor-id", default="")
    parser.add_argument("--contest-source-ability", default="")
    parser.add_argument("--contest-target-ability", default="")
    parser.add_argument("--contest-source-proficient", action="store_true")
    parser.add_argument("--contest-target-proficient", action="store_true")
    parser.add_argument("--contest-source-advantage", action="store_true")
    parser.add_argument("--contest-source-disadvantage", action="store_true")
    parser.add_argument("--contest-target-advantage", action="store_true")
    parser.add_argument("--contest-target-disadvantage", action="store_true")
    parser.add_argument("--source-win-knowledge", default="")
    parser.add_argument("--target-win-knowledge", default="")
    parser.add_argument("--tie-knowledge", default="")
    parser.add_argument("--damage-actor-id", default="")
    parser.add_argument(
        "--damage-event-id",
        default="",
        help="Stable occurrence id for one source-authored environmental damage event",
    )
    parser.add_argument("--damage-expression", default="")
    parser.add_argument("--damage-type", default="")
    parser.add_argument("--damage-reason", default="")
    parser.add_argument("--roll-id", default="")
    parser.add_argument("--roll-expression", default="")
    parser.add_argument("--roll-reason", default="")
    parser.add_argument(
        "--roll-modifier-json",
        action="append",
        type=json.loads,
        default=[],
        help=(
            "Independent external modifier ledger entry with modifier_id, value, "
            "kind, lifetime, state_key, and basis; repeat once per source"
        ),
    )
    parser.add_argument(
        "--roll-count",
        type=int,
        default=1,
        help=(
            "Resolve this many independently identified source rolls in one MCP "
            "process; each roll still uses the public server dice tool"
        ),
    )
    parser.add_argument(
        "--damage-half",
        action="store_true",
        help="Apply half the rolled damage, rounded down, when the cited source requires it",
    )
    parser.add_argument("--damage-knock-prone", action="store_true")
    parser.add_argument("--stand-actor-id", default="")
    parser.add_argument("--stand-reason", default="")
    parser.add_argument("--source-state-actor-id", default="")
    parser.add_argument(
        "--source-state",
        choices=("stable_unconscious",),
        default="",
    )
    parser.add_argument("--source-state-reason", default="")
    parser.add_argument("--activity-actor-id", default="")
    parser.add_argument("--activity-id", default="")
    parser.add_argument(
        "--activity-event-id",
        default="",
        help="Stable occurrence id for one use of the selected activity",
    )
    parser.add_argument("--activity-declaration-json", type=json.loads)
    parser.add_argument("--activity-reason", default="")
    parser.add_argument("--spell-actor-id", default="")
    parser.add_argument("--spell-id", default="")
    parser.add_argument("--spell-source-item-id", default="")
    parser.add_argument("--spell-target-id", default="")
    parser.add_argument("--spell-cast-level", type=int)
    parser.add_argument("--spell-component-ruling-json", type=json.loads)
    parser.add_argument(
        "--spell-agent-ruling-json",
        type=json.loads,
        help=(
            "Settled Agent interpretation of a paid standard spell's descriptive "
            "effect. Required only when the engine returns a post-commit "
            "generic_spell_effect ruling."
        ),
    )
    parser.add_argument("--spell-reason", default="")
    parser.add_argument("--revive-actor-id", default="")
    parser.add_argument("--revive-source-actor-id", default="")
    parser.add_argument("--revive-elapsed-days", type=int, default=0)
    parser.add_argument("--revive-soul-willing", action="store_true")
    parser.add_argument("--revive-body-intact", action="store_true")
    parser.add_argument("--revive-reason", default="")
    parser.add_argument("--snapshot-slot", type=int)
    parser.add_argument("--branch-name", default="")
    parser.add_argument(
        "--core-conversion-reason",
        default="",
        help="Explicit reviewed reason for converting an old-Core snapshot on the new branch",
    )
    parser.add_argument("--time-period", choices=("minute", "hour", "day"))
    parser.add_argument("--time-count", type=int)
    parser.add_argument("--time-reason", default="")
    parser.add_argument("--time-start-clock-json", type=json.loads)
    parser.add_argument(
        "--time-expected-after-json",
        type=json.loads,
        help=(
            "Optional anchored-calendar day/hour/minute/elapsed_minutes target "
            "for additional advance-time projection verification"
        ),
    )
    parser.add_argument(
        "--time-expected-after-ticks",
        type=int,
        help=(
            "Canonical state.game_time.elapsed_ticks target for advance-time; "
            "required for every advance-time operation"
        ),
    )
    parser.add_argument(
        "--time-agent-ruling-json",
        type=json.loads,
        help=(
            "Settled Agent-as-DM duration ruling. Required when exact module text "
            "does not establish the elapsed interval; may accompany a source "
            "reference when the Agent converts narrative timing into an exact count."
        ),
    )
    parser.add_argument(
        "--prerequisite-scene-id",
        default="",
        help=(
            "Scene whose public progress must contain --prerequisite-outcome-id "
            "before advance-time or a rest may mutate campaign state"
        ),
    )
    parser.add_argument(
        "--prerequisite-outcome-id",
        default="",
        help=(
            "Previously recorded full-playthrough outcome required before "
            "advance-time or a rest may mutate campaign state"
        ),
    )
    parser.add_argument(
        "--prerequisite-actor-id",
        action="append",
        default=[],
        help=(
            "Campaign actor that must already exist before advance-time or a rest "
            "may mutate campaign state; repeat for multiple narrative prerequisites"
        ),
    )
    parser.add_argument("--rest-member-json", action="append", type=json.loads, default=[])
    parser.add_argument("--rest-start-clock-json", type=json.loads)
    parser.add_argument(
        "--rest-expected-start-clock-json",
        type=json.loads,
        help=(
            "Exact current world clock required before short-rest or long-rest; "
            "validated through public campaign state before any rest mutation"
        ),
    )
    parser.add_argument("--rest-duration-minutes", type=int, default=60)
    parser.add_argument("--rest-reason", default="")
    parser.add_argument("--recovery-actor-id", action="append", default=[])
    parser.add_argument("--item-actor-id", default="")
    parser.add_argument("--item-json", type=json.loads)
    parser.add_argument("--item-equip-slot", default="")
    parser.add_argument("--item-reason", default="")
    parser.add_argument("--transfer-character-id", default="")
    parser.add_argument("--transfer-recipient-character-id", default="")
    parser.add_argument("--transfer-item-id", default="")
    parser.add_argument("--transfer-item-quantity", type=int)
    parser.add_argument("--transfer-reason", default="")
    parser.add_argument("--pool-actor-id", default="")
    parser.add_argument("--pool-denomination", default="")
    parser.add_argument("--pool-amount", type=int)
    parser.add_argument("--pool-reason", default="")
    parser.add_argument("--effect-character-id", default="")
    parser.add_argument("--effect-id", default="")
    parser.add_argument("--effect-json", type=json.loads)
    parser.add_argument("--effect-reason", default="")
    parser.add_argument("--exhaustion-level", type=int)
    parser.add_argument("--object-json", type=json.loads)
    parser.add_argument("--object-weapon-id", default="")
    parser.add_argument("--object-reason", default="")
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
    parser.add_argument(
        "--event-knowledge-cause",
        choices=("witnessed", "told_by"),
        default="witnessed",
        help="How the event knowledge recipients learned the proposition",
    )
    parser.add_argument(
        "--event-agent-ruling-json",
        type=json.loads,
        help=(
            "Settled Agent-as-DM adjudication recorded with record-event or "
            "record-outcome. It may replace absent source evidence or accompany "
            "source text whose module-specific consequence needs a DM decision."
        ),
    )
    parser.add_argument("--replacement-predecessor-id", default="")
    parser.add_argument("--replacement-actor-id", default="")
    parser.add_argument("--replacement-knowledge", action="append", default=[])
    parser.add_argument(
        "--replacement-agent-ruling-json",
        type=json.loads,
        help=(
            "Settled Agent-as-DM ruling for a source-independent replacement arrival; "
            "mutually exclusive with --source-ref-json"
        ),
    )
    parser.add_argument("--narrative-npc-name", default="")
    parser.add_argument("--narrative-npc-role", default="")
    parser.add_argument("--narrative-npc-summary", default="")
    parser.add_argument("--narrative-npc-faction", default="")
    parser.add_argument("--narrative-npc-relationship", default="")
    parser.add_argument("--narrative-npc-source-identity", default="")
    parser.add_argument("--narrative-npc-instance-key", default="")
    parser.add_argument(
        "--narrative-npc-identity-agent-ruling-json",
        type=json.loads,
        help=(
            "Settled Agent-as-DM naming decision for one source-backed anonymous "
            "NPC instance. The source identity and instance key remain mandatory."
        ),
    )
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
        choices=tuple(sorted(CAMPAIGN_GAME_PHASES)),
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
    parser.add_argument("--resource-sync-actor-id", default="")
    parser.add_argument("--resource-sync-reason", default="")
    parser.add_argument(
        "--resource-sync-return-phase",
        choices=tuple(sorted(CAMPAIGN_GAME_PHASES)),
        help="Explicit phase to restore after lobby-only class resource synchronization",
    )
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
    parser.add_argument(
        "--ending-condition-json",
        action="append",
        type=json.loads,
        default=[],
        help="Source-backed machine-verifiable ending condition to add to the manifest",
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
    module_id: str = "",
) -> dict[str, Any]:
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("query-source requires --source-query")
    if top_k < 1 or top_k > 50:
        raise ValueError("--source-top-k must be between 1 and 50")
    preferred_module_id = module_id.strip()
    if not preferred_module_id:
        manifest_result = await client.domain(
            "playthrough_manifest",
            {"campaign_id": campaign_id, "action": "get"},
        )
        preferred_module_id = str(
            dict(dict(manifest_result.get("manifest") or {}).get("current") or {}).get("module_id")
            or ""
        )
    search_arguments: dict[str, Any] = {
        "campaign_id": campaign_id,
        "query": normalized_query,
        "top_k": top_k,
    }
    if preferred_module_id:
        search_arguments["module_ids"] = [preferred_module_id]
    search_result = await client.domain(
        "module_search",
        search_arguments,
    )
    hits = (
        search_result.get("result")
        if isinstance(search_result, dict) and isinstance(search_result.get("result"), list)
        else search_result
    )
    if not isinstance(hits, list) or any(not isinstance(hit, dict) for hit in hits):
        raise RuntimeError("module_search returned an invalid result collection")
    if preferred_module_id and any(
        str(
            hit.get("source_id")
            or dict(hit.get("metadata") or {}).get("module_id")
            or preferred_module_id
        )
        != preferred_module_id
        for hit in hits
    ):
        raise RuntimeError("module_search returned a hit outside the current manifest module")
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
        "preferred_module_id": preferred_module_id,
        "hits": hits,
        "expanded_chunks": expanded,
    }


async def _read_scene(
    client: ExposureClient,
    *,
    campaign_id: str,
    scene_id: str,
) -> dict[str, Any]:
    normalized_scene_id = scene_id.strip()
    if not normalized_scene_id:
        raise ValueError("read-scene requires --scene-id")
    result = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {
                "scene_id": normalized_scene_id,
                "scope_id": "dm",
            },
        },
    )
    if not isinstance(result, dict):
        raise RuntimeError("module_query returned an invalid scene")
    returned_scene_id = str(result.get("scene_id") or result.get("id") or "")
    if returned_scene_id != normalized_scene_id:
        raise RuntimeError("module_query returned a different scene")
    return result


async def _index_source(
    client: ExposureClient,
    *,
    campaign_id: str,
    module_id: str,
) -> dict[str, Any]:
    normalized_module_id = module_id.strip()
    if not normalized_module_id:
        raise ValueError("index-source requires --module-id")
    result = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "index",
            "payload": {"module_id": normalized_module_id},
        },
    )
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise RuntimeError("module_query returned an invalid module index")
    returned_module_ids = {str(item.get("module_id") or "") for item in result}
    if returned_module_ids - {normalized_module_id}:
        raise RuntimeError("module_query returned a different module index")
    return {"module_id": normalized_module_id, "scenes": result}


def _server_parameters(args: argparse.Namespace) -> StdioServerParameters:
    return regression_server_parameters(
        home=args.home,
        auto_seed=True,
        module_root=args.module_root,
    )


def _scene_locations(scene: dict[str, Any]) -> list[dict[str, Any]]:
    spatial = scene.get("spatial") if isinstance(scene.get("spatial"), dict) else {}
    values = spatial.get("locations") or scene.get("locations") or []
    return [item for item in values if isinstance(item, dict)]


def _scene_progress_percent(progress: dict[str, Any] | None) -> int:
    if not progress:
        return 0
    value = progress.get("progress", progress.get("percent", 0))
    return int(value or 0)


def _scene_progress_write_status(
    progress: dict[str, Any] | None,
    *,
    completed: bool = False,
) -> str:
    """Preserve the authoritative current-scene selector while updating progress."""

    current = str((progress or {}).get("status") or "")
    if current == "current":
        return "current"
    if completed:
        return "completed"
    return current or "active"


def _scene_revision_signature(scene: dict[str, Any]) -> tuple[str, str, int, int]:
    return (
        " ".join(str(scene.get("chapter") or "").casefold().split()),
        " ".join(str(scene.get("title") or "").casefold().split()),
        int(scene.get("page_start", 0) or 0),
        int(scene.get("page_end", 0) or 0),
    )


def _module_progress_remap_rulings(
    validation: dict[str, Any],
    *,
    old_index: list[dict[str, Any]],
    new_index: list[dict[str, Any]],
    supplied: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    requirements = [
        dict(item)
        for item in list(validation.get("ruling_requirements") or [])
        if isinstance(item, dict)
    ]
    required_scene_ids = {
        str(item.get("scene_id") or "").strip()
        for item in requirements
        if str(item.get("scene_id") or "").strip()
    }
    old_by_id = {str(item.get("scene_id") or ""): item for item in old_index}
    new_by_id = {str(item.get("scene_id") or ""): item for item in new_index}
    normalized_supplied: dict[str, dict[str, str]] = {}
    for index, raw in enumerate(supplied or []):
        if not isinstance(raw, dict):
            raise ValueError(f"module-progress-remap-json[{index}] must be an object")
        unknown = set(raw) - {"from_scene_id", "to_scene_id", "reason"}
        if unknown:
            raise ValueError(
                f"module-progress-remap-json[{index}] has unsupported fields: "
                + ", ".join(sorted(unknown))
            )
        source_scene_id = str(raw.get("from_scene_id") or "").strip()
        target_scene_id = str(raw.get("to_scene_id") or "").strip()
        reason = str(raw.get("reason") or "").strip()
        if (
            source_scene_id not in required_scene_ids
            or source_scene_id not in old_by_id
            or target_scene_id not in new_by_id
        ):
            raise ValueError(
                f"module-progress-remap-json[{index}] must map one required old scene "
                "to a scene in the candidate revision"
            )
        if not reason or len(reason) > 1000:
            raise ValueError(
                f"module-progress-remap-json[{index}].reason must contain 1 to 1000 characters"
            )
        if source_scene_id in normalized_supplied:
            raise ValueError("module progress remaps contain duplicate from_scene_id values")
        normalized_supplied[source_scene_id] = {
            "from_scene_id": source_scene_id,
            "to_scene_id": target_scene_id,
            "reason": reason,
        }
    unexpected_requirements = [
        item
        for item in requirements
        if (
            not str(item.get("scene_id") or "").strip()
            or item.get("default_resolver") != "agent"
            or item.get("ruling_kind") != "source_or_scene_fact"
        )
    ]
    if unexpected_requirements:
        raise RegressionRulingRequiredError(
            {
                "status": "pending_ruling",
                "ruling_kind": "missing_or_conflicting_source_review",
                "reason": "module revision validation returned a non-scene remap requirement",
                "ruling_requirements": unexpected_requirements,
            },
            operation="content_pack.activate",
            retry_hint="Resolve the reported review requirement before retrying the refresh.",
        )
    candidates_by_signature: dict[tuple[str, str, int, int], list[dict[str, Any]]] = {}
    for scene in new_index:
        candidates_by_signature.setdefault(_scene_revision_signature(scene), []).append(scene)
    rulings: list[dict[str, str]] = []
    for source_scene_id in sorted(required_scene_ids):
        supplied_ruling = normalized_supplied.get(source_scene_id)
        if supplied_ruling is not None:
            rulings.append(supplied_ruling)
            continue
        source_scene = old_by_id.get(source_scene_id)
        matches = (
            candidates_by_signature.get(_scene_revision_signature(source_scene), [])
            if source_scene is not None
            else []
        )
        if len(matches) != 1:
            raise RegressionRulingRequiredError(
                {
                    "status": "pending_ruling",
                    "ruling_kind": "source_or_scene_fact",
                    "reason": (
                        "a removed progressed scene has no unique exact "
                        "chapter/title/page match in the candidate module revision"
                    ),
                    "scene_id": source_scene_id,
                    "source_scene": deepcopy(source_scene),
                    "candidate_scenes": [deepcopy(item) for item in matches],
                },
                operation="content_pack.activate",
                context={"from_scene_id": source_scene_id},
                retry_hint=(
                    "Inspect the candidate index and retry with --module-progress-remap-json."
                ),
            )
        target = matches[0]
        rulings.append(
            {
                "from_scene_id": source_scene_id,
                "to_scene_id": str(target["scene_id"]),
                "reason": (
                    "The Agent acting as DM maps the removed progress scene to "
                    "the candidate scene with the exact same chapter, title, "
                    "and source page range."
                ),
            }
        )
    return rulings


def _extend_manifest_for_module_revision(
    manifest: dict[str, Any],
    *,
    old_module_id: str,
    new_module_id: str,
    old_index: list[dict[str, Any]],
    new_index: list[dict[str, Any]],
    scene_remaps: dict[str, str] | None = None,
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
    new_by_id = {str(item["scene_id"]): item for item in new_index}
    for source_scene_id, target_scene_id in dict(scene_remaps or {}).items():
        if source_scene_id not in old_by_id or target_scene_id not in new_by_id:
            raise ValueError("module revision scene remap references an unknown scene")
        scene_map[source_scene_id] = new_by_id[target_scene_id]
    current_scene_id = str(value["current"].get("scene_id") or "")
    replacement = scene_map.get(current_scene_id)
    if replacement is None:
        raise ValueError("current scene has no stable-key match in the new module revision")
    if new_module_id != old_module_id:
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


def _remap_replacement_level_endings(
    manifest: dict[str, Any],
    *,
    predecessor_actor_id: str,
    replacement_actor_id: str,
) -> None:
    """Keep party-level endings bound to the active party slot.

    This deliberately remaps only level checks. Other actor-value endings can
    describe the predecessor's own fate and must remain bound to that actor.
    """

    for condition in list(dict(manifest.get("ending") or {}).get("conditions") or []):
        for check in list(dict(condition).get("all_of") or []):
            if (
                str(dict(check).get("kind") or "") == "actor_value"
                and str(dict(check).get("path") or "") == "sheet.progression.level"
                and str(dict(check).get("actor_id") or "") == predecessor_actor_id
            ):
                check["actor_id"] = replacement_actor_id


async def _remap_ending_sources_for_module_revision(
    client: ExposureClient,
    manifest: dict[str, Any],
    *,
    campaign_id: str,
    new_module_id: str,
    source_asset_sha256: str,
) -> dict[str, Any]:
    """Resolve ending citations against the newly ingested module revision."""

    value = deepcopy(manifest)
    module_ids = {str(item) for item in list(value.get("module_ids") or [])}
    for condition in list(dict(value.get("ending") or {}).get("conditions") or []):
        source_ref = dict(dict(condition).get("source_ref") or {})
        if (
            str(source_ref.get("asset_sha256") or "").casefold() != source_asset_sha256.casefold()
            or str(source_ref.get("module_id") or "") == new_module_id
            or str(source_ref.get("module_id") or "") not in module_ids
        ):
            continue
        excerpt = str(source_ref.get("excerpt") or "").strip()
        content_sha256 = str(source_ref.get("content_sha256") or "").casefold()
        if not excerpt or not content_sha256:
            raise ValueError(
                "module refresh cannot remap an ending citation without its excerpt "
                "and chunk content hash"
            )
        search_result = await client.domain(
            "module_search",
            {
                "campaign_id": campaign_id,
                "query": excerpt,
                "top_k": 50,
                "module_ids": [new_module_id],
            },
        )
        hits = (
            search_result.get("result")
            if isinstance(search_result, dict) and isinstance(search_result.get("result"), list)
            else search_result
        )
        if not isinstance(hits, list):
            raise RuntimeError("module_search returned an invalid result collection")
        matches: list[dict[str, Any]] = []
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            chunk_id = str(hit.get("chunk_id") or hit.get("id") or "")
            if not chunk_id:
                continue
            expanded = await client.domain("module_expand", {"chunk_id": chunk_id})
            exact_ref = dict(dict(expanded).get("source_ref") or {})
            if (
                str(exact_ref.get("module_id") or "") == new_module_id
                and str(exact_ref.get("content_sha256") or "").casefold() == content_sha256
                and _normalized_source_text(excerpt)
                in _normalized_source_text(dict(expanded).get("content"))
            ):
                matches.append(exact_ref)
        if len(matches) != 1:
            raise ValueError(
                "module refresh must resolve each ending citation to exactly one "
                "content-hash-matched chunk in the new revision"
            )
        exact_ref = matches[0]
        previous_scene_id = str(source_ref.get("scene_id") or "")
        replacement_scene_id = str(exact_ref["scene_id"])
        condition["source_ref"] = {
            **source_ref,
            "page_start": int(exact_ref["page_start"]),
            "page_end": int(exact_ref["page_end"]),
            "heading_path": list(exact_ref["heading_path"]),
            "content_sha256": str(exact_ref["content_sha256"]).casefold(),
            "module_id": new_module_id,
            "scene_id": replacement_scene_id,
            "chunk_id": str(exact_ref["chunk_id"]),
        }
        for check in list(dict(condition).get("all_of") or []):
            if (
                str(dict(check).get("kind") or "") == "manifest_value"
                and str(dict(check).get("path") or "") == "current.scene_id"
                and str(dict(check).get("value") or "") == previous_scene_id
            ):
                check["value"] = replacement_scene_id
    for replacement in list(dict(value.get("party") or {}).get("replacements") or []):
        _remap_replacement_level_endings(
            value,
            predecessor_actor_id=str(dict(replacement).get("predecessor_actor_id") or ""),
            replacement_actor_id=str(dict(replacement).get("replacement_actor_id") or ""),
        )
    return value


def _module_refresh_manifest_action(old_module_id: str, new_module_id: str) -> str:
    return "replace" if new_module_id == old_module_id else "extend_modules"


def _module_refresh_manifest_identity(
    *,
    old_module_id: str,
    new_module_id: str,
    refresh_identity: str,
    manifest: dict[str, Any],
) -> str:
    request_hash = _idempotency_request_hash(manifest)[:24]
    return (
        f"refresh-module-manifest:{old_module_id}:{new_module_id}:{refresh_identity}:{request_hash}"
    )


async def _validate_source_ref(
    client: ExposureClient,
    scene: dict[str, Any],
    source_ref: dict[str, Any] | None,
    *,
    excerpt: str = "",
) -> dict[str, Any]:
    if not isinstance(source_ref, dict):
        raise ValueError("playthrough action requires --source-ref-json")
    required = EXACT_MODULE_SOURCE_FIELDS
    unknown = sorted(set(source_ref) - (required | PLAYTHROUGH_SOURCE_FIELDS))
    if unknown:
        raise ValueError(f"source_ref has unsupported fields: {', '.join(unknown)}")
    missing = sorted(required - set(source_ref))
    if missing:
        raise ValueError(f"source_ref is missing required fields: {', '.join(missing)}")
    if str(source_ref["module_id"]) != str(scene.get("module_id")):
        raise ValueError("source_ref module_id does not match the cited scene")
    if str(source_ref["scene_id"]) != str(scene.get("scene_id")):
        raise ValueError("source_ref scene_id does not match the cited scene")
    if not str(source_ref["chunk_id"]).strip() or not str(source_ref["content_sha256"]).strip():
        raise ValueError("source_ref chunk_id and content_sha256 must not be empty")
    expanded = await client.domain(
        "module_expand",
        {"chunk_id": str(source_ref["chunk_id"])},
    )
    expanded_ref = expanded.get("source_ref")
    if not isinstance(expanded_ref, dict):
        raise RuntimeError("module_expand returned no exact source_ref")
    cited = {key: deepcopy(source_ref[key]) for key in EXACT_MODULE_SOURCE_FIELD_ORDER}
    resolved = {key: deepcopy(expanded_ref.get(key)) for key in EXACT_MODULE_SOURCE_FIELD_ORDER}
    if resolved != cited:
        raise ValueError("source_ref does not match the cited chunk's exact source metadata")
    if str(expanded.get("chunk_id") or "") != str(source_ref["chunk_id"]):
        raise ValueError("module_expand returned a different cited chunk")
    if str(expanded.get("content_sha256") or "") != str(source_ref["content_sha256"]):
        raise ValueError("module_expand returned a different cited chunk digest")
    if excerpt and _normalized_source_text(excerpt) not in _normalized_source_text(
        expanded.get("content")
    ):
        raise ValueError("source excerpt is not contained in the cited chunk")
    return cited


def _campaign_phase(campaign: dict[str, Any]) -> str:
    phase = str(campaign.get("effective_game_phase") or "")
    if phase not in EFFECTIVE_GAME_PHASES:
        raise RuntimeError(f"campaign view has no valid effective_game_phase: {phase!r}")
    return phase


async def _campaign(client: ExposureClient, campaign_id: str) -> dict[str, Any]:
    return await campaign_view(client, campaign_id)


def _knowledge_preflight_actor_ids(args: argparse.Namespace) -> list[str]:
    if args.action in KNOWLEDGE_ACTOR_PREFLIGHT_ACTIONS:
        return list(args.knowledge_actor_id)
    if args.action in EVENT_KNOWLEDGE_ACTOR_PREFLIGHT_ACTIONS:
        return list(args.event_knowledge_actor_id)
    return []


async def _validate_campaign_actor_ids(
    client: ExposureClient,
    *,
    campaign_id: str,
    actor_ids: list[str],
    operation: str,
) -> list[dict[str, Any]]:
    normalized_ids = list(dict.fromkeys(str(actor_id).strip() for actor_id in actor_ids))
    if any(not actor_id for actor_id in normalized_ids):
        raise ValueError(f"{operation} actor ids must not be empty")
    actors = []
    for actor_id in normalized_ids:
        actor = await client.domain(
            "character_query",
            {"view": "get", "payload": {"character_id": actor_id}},
        )
        if actor.get("campaign_id") != campaign_id:
            raise ValueError(f"{operation} actor does not belong to the campaign: {actor_id}")
        actors.append(actor)
    return actors


async def _manifest_get(
    client: ExposureClient,
    campaign_id: str,
) -> dict[str, Any]:
    return await client.domain(
        "playthrough_manifest",
        {"campaign_id": campaign_id, "action": "get"},
    )


async def _validate_narrative_preconditions(
    client: ExposureClient,
    *,
    campaign_id: str,
    scene_id: str = "",
    outcome_id: str = "",
    actor_ids: list[str] | None = None,
) -> dict[str, Any]:
    normalized_scene_id = scene_id.strip()
    normalized_outcome_id = outcome_id.strip()
    normalized_actor_ids = [str(actor_id).strip() for actor_id in (actor_ids or [])]
    if bool(normalized_scene_id) != bool(normalized_outcome_id):
        raise ValueError("narrative outcome precondition requires both scene id and outcome id")
    if any(not actor_id for actor_id in normalized_actor_ids):
        raise ValueError("narrative actor precondition ids must not be empty")
    if len(normalized_actor_ids) != len(set(normalized_actor_ids)):
        raise ValueError("narrative actor precondition ids must be unique")

    outcome_evidence = None
    if normalized_outcome_id:
        progress_rows = await client.domain(
            "module_query",
            {"campaign_id": campaign_id, "view": "progress"},
        )
        if not isinstance(progress_rows, list) or any(
            not isinstance(item, dict) for item in progress_rows
        ):
            raise RuntimeError("module progress precondition query returned invalid rows")
        progress = next(
            (
                item
                for item in progress_rows
                if str(item.get("scene_id") or "") == normalized_scene_id
            ),
            None,
        )
        outcomes = dict(
            dict((progress or {}).get("state") or {}).get("full_playthrough_outcomes") or {}
        )
        outcome = outcomes.get(normalized_outcome_id)
        if not isinstance(outcome, dict):
            raise ValueError(
                "required playthrough outcome is not recorded in the current branch: "
                f"{normalized_outcome_id}"
            )
        outcome_evidence = {
            "scene_id": normalized_scene_id,
            "outcome_id": normalized_outcome_id,
            "state_version": int((progress or {}).get("state_version", 0) or 0),
            "event_type": str(outcome.get("event_type") or ""),
            "fact_keys": [str(item) for item in list(outcome.get("fact_keys") or [])],
        }

    actor_evidence = []
    for actor_id in normalized_actor_ids:
        actor = await client.domain(
            "character_query",
            {"view": "get", "payload": {"character_id": actor_id}},
        )
        if actor.get("campaign_id") != campaign_id:
            raise ValueError(
                f"narrative actor precondition does not belong to the campaign: {actor_id}"
            )
        actor_evidence.append(
            {
                "actor_id": actor_id,
                "revision": actor.get("revision"),
                "character_type": str(actor.get("character_type") or ""),
            }
        )
    return {
        "outcome": outcome_evidence,
        "actors": actor_evidence,
    }


def _validate_world_time_precondition(
    campaign: dict[str, Any],
    expected: Any,
) -> dict[str, int] | None:
    normalized_expected = _normalize_expected_calendar_time(expected)
    if normalized_expected is None:
        return None
    world_time = dict(dict(campaign.get("state") or {}).get("world_time") or {})
    actual = {key: world_time.get(key) for key in CALENDAR_MINUTE_FIELDS}
    if actual != normalized_expected:
        raise ValueError(
            "campaign world time does not match the required precondition: "
            f"expected day {normalized_expected['day']} "
            f"{normalized_expected['hour']:02}:{normalized_expected['minute']:02}, "
            f"elapsed {normalized_expected['elapsed_minutes']}; "
            f"actual {actual}"
        )
    return normalized_expected


def _mutation_key(run_id: str, action: str, identity: str) -> str:
    return f"full-playthrough-{action}-{_token(f'{run_id}:{identity}', length=24)}"


def _idempotency_request_hash(payload: dict[str, Any]) -> str:
    return request_hash(payload)


def _validate_recovered_continuity(
    receipt: dict[str, Any],
    *,
    payload: dict[str, Any],
    branch_id: str,
) -> dict[str, Any]:
    response = receipt.get("response")
    if receipt.get("replayed") is not True or not isinstance(response, dict):
        raise RuntimeError("continuity recovery receipt has no response")
    receipt_branch_id = str(receipt.get("branch_id") or "")
    if receipt_branch_id and receipt_branch_id != branch_id:
        raise RuntimeError(
            f"continuity recovery receipt is from another branch: {receipt_branch_id}"
        )
    if payload.get("facts"):
        raise RuntimeError("continuity recovery with fact writes requires explicit review")
    expected_event = dict(payload.get("event") or {})
    recovered_event = dict(response.get("event") or {})
    recovered_payload = dict(recovered_event.get("payload") or {})
    recovered_payload.pop("_sagasmith_skill_manifest", None)
    if (
        recovered_event.get("summary") != expected_event.get("summary")
        or recovered_event.get("event_type") != expected_event.get("event_type")
        or recovered_event.get("audience_scope") != expected_event.get("audience_scope")
        or recovered_payload != dict(expected_event.get("payload") or {})
    ):
        raise RuntimeError("continuity recovery receipt event does not match")
    expected_knowledge = [
        {
            "actor_id": str(item.get("actor_id") or ""),
            "knowledge_key": str(item.get("knowledge_key") or ""),
            "proposition": str(item.get("proposition") or ""),
            "disclosure_scope": str(item.get("disclosure_scope") or ""),
        }
        for item in list(payload.get("actor_knowledge") or [])
    ]
    recovered_knowledge = [
        {
            "actor_id": str(item.get("actor_id") or ""),
            "knowledge_key": str(item.get("knowledge_key") or ""),
            "proposition": str(item.get("proposition") or ""),
            "disclosure_scope": str(item.get("disclosure_scope") or ""),
        }
        for item in list(response.get("actor_knowledge") or [])
    ]
    if recovered_knowledge != expected_knowledge:
        raise RuntimeError("continuity recovery receipt actor knowledge does not match")
    expected_snapshot = payload.get("snapshot")
    recovered_snapshot = response.get("snapshot")
    if expected_snapshot is None:
        if recovered_snapshot is not None:
            raise RuntimeError("continuity recovery receipt has an unexpected snapshot")
    elif not isinstance(recovered_snapshot, dict) or recovered_snapshot.get("label") != dict(
        expected_snapshot
    ).get("label"):
        raise RuntimeError("continuity recovery receipt snapshot does not match")
    return deepcopy(response)


async def _commit_roll_continuity(
    client: ExposureClient,
    *,
    campaign_id: str,
    payload: dict[str, Any],
    expected_revision: int,
    idempotency_key: str,
) -> dict[str, Any]:
    try:
        return await client.domain(
            "memory_change",
            {
                "campaign_id": campaign_id,
                "action": "commit",
                "payload": payload,
                "expected_revision": expected_revision,
                "idempotency_key": idempotency_key,
            },
        )
    except Exception as exc:
        if "idempotency key reused with a different request" not in str(exc):
            raise
        await client.load()
        receipt = await client.domain(
            "state_revision",
            {
                "campaign_id": campaign_id,
                "action": "receipt",
                "payload": {"idempotency_key": idempotency_key},
            },
        )
        if not str(receipt.get("branch_id") or ""):
            await client.load()
            visible_events = await client.domain(
                "campaign_event",
                {
                    "campaign_id": campaign_id,
                    "action": "list",
                    "payload": {
                        "limit": 500,
                        "branch_id": str(payload.get("branch_id") or ""),
                    },
                },
            )
            response_event_id = str(
                dict(dict(receipt.get("response") or {}).get("event") or {}).get("id") or ""
            )
            if not response_event_id or response_event_id not in {
                str(item.get("id") or "") for item in list(visible_events or [])
            }:
                raise RuntimeError(
                    "continuity recovery receipt event is not visible on the current branch"
                )
        return _validate_recovered_continuity(
            receipt,
            payload=payload,
            branch_id=str(payload.get("branch_id") or ""),
        )


def _module_refresh_identity(
    *,
    old_module_id: str,
    source_key: str,
    source_path: Path,
    title: str,
    parser_revision: str,
    source_sha256: str = "",
) -> str:
    serialized = json.dumps(
        {
            "old_module_id": old_module_id,
            "source_key": source_key,
            "source_sha256": source_sha256 or file_sha256(source_path),
            "title": title,
            "parser_revision": parser_revision,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _token(serialized, length=24)


def _occurrence_identity(value: str, action: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{action} requires --occurrence-id")
    if len(normalized) > 200:
        raise ValueError("occurrence id must not exceed 200 characters")
    return normalized


def _validate_recovered_long_rest(
    receipt: dict[str, Any],
    *,
    campaign: dict[str, Any],
    actors: list[dict[str, Any]],
    members: list[dict[str, Any]],
    duration_minutes: int,
    expected_request_hash: str,
) -> dict[str, Any]:
    response = receipt.get("response")
    if receipt.get("replayed") is not True or not isinstance(response, dict):
        raise RuntimeError("long-rest recovery receipt has no response")
    if receipt.get("request_hash") != expected_request_hash:
        raise RuntimeError("long-rest recovery receipt request does not match")
    actor_ids = [str(item["actor_id"]) for item in members]
    current_clock = dict(dict(campaign.get("state") or {}).get("world_time") or {})
    current_game_time = dict(dict(campaign.get("state") or {}).get("game_time") or {})
    if (
        response.get("idempotency_replayed") is True
        and response.get("response_recovery") == "read_current_state"
    ):
        actor_by_id = {str(actor.get("id")): actor for actor in actors}
        preparations: dict[str, dict[str, list[str]]] = {}
        for member in members:
            if member.get("prepared_spell_ids") is None:
                continue
            actor_id = str(member["actor_id"])
            actor = actor_by_id.get(actor_id)
            if actor is None:
                raise RuntimeError("long-rest recovery is missing a requested actor")
            spellcasting = dict(dict(actor.get("sheet") or {}).get("spellcasting") or {})
            preparation = dict(spellcasting.get("preparation") or {})
            preparations[actor_id] = {
                "selected_spell_ids": list(preparation.get("selected_spell_ids") or [])
            }
        response = {
            **response,
            "status": "committed",
            "rest_type": "long_rest",
            "duration_minutes": duration_minutes,
            "member_ids": actor_ids,
            "campaign_revision": campaign.get("revision"),
            "game_time": current_game_time,
            "world_time": current_clock,
            "preparations": preparations,
        }
    if (
        response.get("status") != "committed"
        or response.get("rest_type") != "long_rest"
        or response.get("duration_minutes") != duration_minutes
        or response.get("member_ids") != actor_ids
    ):
        raise RuntimeError("long-rest recovery receipt does not match the requested rest")
    campaign_revision = campaign.get("revision")
    if response.get("campaign_revision") != campaign_revision:
        raise RuntimeError("long-rest recovery receipt is not the current campaign mutation")
    if dict(response.get("world_time") or {}) != current_clock:
        raise RuntimeError("long-rest recovery receipt does not match the calendar projection")
    if response.get("game_time") != current_game_time:
        raise RuntimeError("long-rest recovery receipt does not match the campaign timeline")
    completed_elapsed = current_game_time.get("elapsed_ticks")
    if isinstance(completed_elapsed, bool) or not isinstance(completed_elapsed, int):
        raise RuntimeError("long-rest recovery requires the campaign game timeline")
    started_elapsed = completed_elapsed - duration_minutes * 10
    actor_by_id = {str(actor.get("id")): actor for actor in actors}
    receipt_preparations = dict(response.get("preparations") or {})
    for member in members:
        actor_id = str(member["actor_id"])
        actor = actor_by_id.get(actor_id)
        if actor is None:
            raise RuntimeError("long-rest recovery is missing a requested actor")
        combat = dict(dict(actor.get("sheet") or {}).get("combat") or {})
        history = dict(combat.get("rest_history") or {})
        if history != {
            "last_rest_type": "long_rest",
            "last_rest_started_elapsed_ticks": started_elapsed,
            "last_rest_completed_elapsed_ticks": completed_elapsed,
            "last_long_rest_elapsed_ticks": completed_elapsed,
        }:
            raise RuntimeError(f"long-rest recovery history does not match for actor {actor_id}")
        prepared_ids = member.get("prepared_spell_ids")
        if prepared_ids is not None:
            spellcasting = dict(dict(actor.get("sheet") or {}).get("spellcasting") or {})
            preparation = dict(spellcasting.get("preparation") or {})
            receipt_preparation = dict(receipt_preparations.get(actor_id) or {})
            if receipt_preparation.get("selected_spell_ids") != preparation.get(
                "selected_spell_ids"
            ):
                raise RuntimeError(
                    f"long-rest recovery preparations do not match for actor {actor_id}"
                )
    return deepcopy(response)


def _validate_recovered_short_rest(
    receipt: dict[str, Any],
    *,
    campaign: dict[str, Any],
    actors: list[dict[str, Any]],
    members: list[dict[str, Any]],
    duration_minutes: int,
    expected_request_hash: str,
) -> dict[str, Any]:
    response = receipt.get("response")
    if receipt.get("replayed") is not True or not isinstance(response, dict):
        raise RuntimeError("short-rest recovery receipt has no response")
    if receipt.get("request_hash") != expected_request_hash:
        raise RuntimeError("short-rest recovery receipt request does not match")
    if response.get("response_recovery") == "read_current_state":
        raise RuntimeError("random-capable short-rest recovery requires its atomic exact response")
    actor_ids = [str(item["actor_id"]) for item in members]
    if (
        response.get("status") != "committed"
        or response.get("rest_type") != "short_rest"
        or response.get("duration_minutes") != duration_minutes
        or response.get("member_ids") != actor_ids
    ):
        raise RuntimeError("short-rest recovery receipt does not match the requested rest")
    if response.get("campaign_revision") != campaign.get("revision"):
        raise RuntimeError("short-rest recovery receipt is not the current campaign mutation")
    current_clock = dict(dict(campaign.get("state") or {}).get("world_time") or {})
    current_game_time = dict(dict(campaign.get("state") or {}).get("game_time") or {})
    if dict(response.get("world_time") or {}) != current_clock:
        raise RuntimeError("short-rest recovery receipt does not match the calendar projection")
    if response.get("game_time") != current_game_time:
        raise RuntimeError("short-rest recovery receipt does not match the campaign timeline")
    completed_elapsed = current_game_time.get("elapsed_ticks")
    if isinstance(completed_elapsed, bool) or not isinstance(completed_elapsed, int):
        raise RuntimeError("short-rest recovery requires the campaign game timeline")
    started_elapsed = completed_elapsed - duration_minutes * 10
    actor_by_id = {str(actor.get("id")): actor for actor in actors}
    recovered_by_id = dict(response.get("recovered") or {})
    requested_draws = 0
    for member in members:
        actor_id = str(member["actor_id"])
        actor = actor_by_id.get(actor_id)
        if actor is None:
            raise RuntimeError("short-rest recovery is missing a requested actor")
        history = dict(
            dict(dict(actor.get("sheet") or {}).get("combat") or {}).get("rest_history") or {}
        )
        if (
            history.get("last_rest_type") != "short_rest"
            or history.get("last_rest_started_elapsed_ticks") != started_elapsed
            or history.get("last_rest_completed_elapsed_ticks") != completed_elapsed
        ):
            raise RuntimeError(f"short-rest recovery history does not match for actor {actor_id}")
        recovered = recovered_by_id.get(actor_id)
        if not isinstance(recovered, dict):
            raise RuntimeError(
                f"short-rest recovery has no exact member result for actor {actor_id}"
            )
        requested_actor_draws = sum(
            int(item["count"]) for item in list(member.get("hit_dice_spends") or [])
        )
        requested_draws += requested_actor_draws
        if len(list(recovered.get("hit_dice_rolls") or [])) != requested_actor_draws:
            raise RuntimeError(
                f"short-rest recovery has incomplete Hit Dice rolls for actor {actor_id}"
            )
        attune_item_id = str(member.get("attune_item_id") or "")
        if attune_item_id:
            inventory = list(
                dict(dict(actor.get("sheet") or {}).get("inventory") or {}).get("items") or []
            )
            item = next(
                (
                    value
                    for value in inventory
                    if str(dict(value).get("id") or "") == attune_item_id
                ),
                None,
            )
            if (
                item is None
                or dict(item).get("attunement") != "attuned"
                or recovered.get("attuned_item_id") != attune_item_id
            ):
                raise RuntimeError(
                    f"short-rest recovery attunement does not match for actor {actor_id}"
                )
    if requested_draws:
        random_receipt = response.get("random_stream_receipt")
        if (
            not isinstance(random_receipt, dict)
            or random_receipt.get("idempotency_key") != receipt.get("key")
            or int(random_receipt.get("draw_count", 0) or 0) < requested_draws
        ):
            raise RuntimeError("short-rest recovery has no complete random-stream receipt")
    return deepcopy(response)


def _check_knowledge_key(
    run_id: str,
    occurrence_id: str,
) -> str:
    return (
        f"playthrough.{_token(run_id)}.check."
        f"{_token(_occurrence_identity(occurrence_id, 'resolve-check'), length=32)}"
    )


def _check_identity(occurrence_id: str) -> str:
    return _occurrence_identity(occurrence_id, "resolve-check")


def _committed_check_result(settled: dict[str, Any]) -> dict[str, Any]:
    """Accept full tool responses and compact dynamic-exposure facades."""

    raise_for_pending_ruling(
        {
            **settled,
            "reason": str(settled.get("reason") or "source-cited character check did not commit"),
        },
        operation="character_check",
    )
    if settled.get("status") == "committed" and isinstance(settled.get("result"), dict):
        return dict(settled["result"])
    if "success" in settled and (
        "total" in settled
        or settled.get("automatic_failure")
        or settled.get("kind") == "ability_group_check"
    ):
        return dict(settled)
    raise RuntimeError("source-cited character check did not commit")


def _actor_card_has_named_skill(actor: dict[str, Any], ability: str) -> bool:
    normalized = str(ability).strip().casefold().replace(" ", "_")
    sheet = dict(actor.get("sheet") or {})
    return normalized in dict(sheet.get("skills") or {})


def _matching_check_progress(
    progress: dict[str, Any] | None,
    *,
    run_id: str,
    occurrence_id: str,
    location_key: str,
    actor_id: str,
    kind: str,
    ability: str,
    dc: int,
    proficient: bool,
    bonus: int,
    advantage: bool,
    disadvantage: bool,
    source_ref: dict[str, Any],
    agent_ruling: dict[str, Any] | None = None,
) -> bool:
    if not isinstance(progress, dict):
        return False
    state = dict(progress.get("state") or {})
    check = dict(state.get("full_playthrough_check") or {})
    return bool(
        str(progress.get("current_location_key") or "") == location_key
        and check.get("run_id") == run_id
        and check.get("occurrence_id") == occurrence_id
        and check.get("actor_id") == actor_id
        and check.get("kind") == kind
        and check.get("ability") == ability
        and check.get("dc") == dc
        and bool(check.get("proficient", False)) == proficient
        and int(check.get("bonus", 0) or 0) == bonus
        and bool(check.get("advantage", False)) == advantage
        and bool(check.get("disadvantage", False)) == disadvantage
        and check.get("source_ref") == source_ref
        and check.get("agent_ruling") == agent_ruling
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


def _contest_identity(occurrence_id: str) -> str:
    return _occurrence_identity(occurrence_id, "resolve-contest")


def _contest_knowledge_key(run_id: str, occurrence_id: str) -> str:
    return (
        f"playthrough.{_token(run_id)}.contest."
        f"{_token(_contest_identity(occurrence_id), length=32)}"
    )


def _committed_contest_result(settled: dict[str, Any]) -> dict[str, Any]:
    """Accept full tool responses and compact dynamic-exposure facades."""

    raise_for_pending_ruling(
        {
            **settled,
            "reason": str(settled.get("reason") or "source-cited ability contest did not commit"),
        },
        operation="character_check.contest",
    )
    if settled.get("status") == "committed" and isinstance(settled.get("result"), dict):
        return dict(settled["result"])
    if settled.get("kind") == "ability_contest" and "outcome" in settled:
        return dict(settled)
    raise RuntimeError("source-cited ability contest did not commit")


def _matching_contest_progress(
    progress: dict[str, Any] | None,
    *,
    run_id: str,
    occurrence_id: str,
    location_key: str,
    source_actor_id: str,
    target_actor_id: str,
    source_ability: str,
    target_ability: str,
    source_proficient: bool,
    target_proficient: bool,
    source_advantage: bool,
    source_disadvantage: bool,
    target_advantage: bool,
    target_disadvantage: bool,
    source_ref: dict[str, Any],
) -> bool:
    if not isinstance(progress, dict):
        return False
    state = dict(progress.get("state") or {})
    contest = dict(state.get("full_playthrough_contest") or {})
    return bool(
        str(progress.get("current_location_key") or "") == location_key
        and contest.get("run_id") == run_id
        and contest.get("occurrence_id") == occurrence_id
        and contest.get("source_actor_id") == source_actor_id
        and contest.get("target_actor_id") == target_actor_id
        and contest.get("source_ability") == source_ability
        and contest.get("target_ability") == target_ability
        and bool(contest.get("source_proficient", False)) == source_proficient
        and bool(contest.get("target_proficient", False)) == target_proficient
        and bool(contest.get("source_advantage", False)) == source_advantage
        and bool(contest.get("source_disadvantage", False)) == source_disadvantage
        and bool(contest.get("target_advantage", False)) == target_advantage
        and bool(contest.get("target_disadvantage", False)) == target_disadvantage
        and contest.get("source_ref") == source_ref
    )


def _recover_committed_contest(
    campaign: dict[str, Any],
    *,
    progress_matches: bool,
    source_actor_id: str,
    target_actor_id: str,
) -> dict[str, Any] | None:
    """Recover a contest committed before a driver-side response failure."""

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
        latest.get("type") != "ability_contest"
        or latest.get("source_actor_id") != source_actor_id
        or latest.get("target_actor_id") != target_actor_id
        or result.get("kind") != "ability_contest"
        or "outcome" not in result
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
    checkpoint_id: str,
) -> dict[str, Any]:
    if not label.strip():
        raise ValueError("checkpoint label must not be empty")
    checkpoint_identity = _occurrence_identity(checkpoint_id, "checkpoint")
    snapshot_key = _mutation_key(run_id, "snapshot", checkpoint_identity)
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"checkpoint-sync:{checkpoint_identity}",
    )
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    current_branch = next((item for item in branches if item.get("is_current")), None)
    if current_branch is None:
        raise RuntimeError("campaign has no current branch")
    try:
        snapshot = await client.domain(
            "snapshot_create",
            {
                "campaign_id": campaign_id,
                "label": label,
                "expected_revision": synced["campaign_revision"],
                "expected_head_snapshot_id": current_branch.get("head_snapshot_id") or "",
                "idempotency_key": snapshot_key,
            },
        )
        reused = False
    except Exception as error:
        if "idempotency key reused with a different request" not in str(error):
            raise
        receipt = await client.domain(
            "state_revision",
            {
                "campaign_id": campaign_id,
                "action": "receipt",
                "payload": {"idempotency_key": snapshot_key},
            },
        )
        recovered = dict(receipt.get("response") or {})
        receipt_branch_id = str(receipt.get("branch_id") or recovered.get("branch_id") or "")
        if receipt_branch_id != str(current_branch["id"]):
            raise RuntimeError("checkpoint recovery receipt is from another branch")
        expected_request_hash = _idempotency_request_hash(
            {
                "label": label,
                "expected_head_snapshot_id": str(recovered.get("parent_id") or ""),
            }
        )
        if (
            str(receipt.get("request_hash") or "") != expected_request_hash
            or str(recovered.get("label") or "") != label
            or str(recovered.get("branch_id") or "") != str(current_branch["id"])
            or str(recovered.get("id") or "") != str(current_branch.get("head_snapshot_id") or "")
        ):
            raise RuntimeError("checkpoint recovery receipt does not match the current branch head")
        snapshots = await client.domain(
            "snapshot_query",
            {"campaign_id": campaign_id, "view": "list"},
        )
        snapshot = next(
            (
                item
                for item in snapshots
                if str(item.get("id") or "") == str(recovered["id"])
                and str(item.get("branch_id") or "") == str(current_branch["id"])
            ),
            None,
        )
        if snapshot is None or int(snapshot.get("slot", 0) or 0) != int(
            recovered.get("slot", 0) or 0
        ):
            raise RuntimeError("checkpoint recovery receipt snapshot is unavailable")
        reused = True
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
    # Manifest reads project the authoritative Snapshot DAG from Core tables. A
    # second persisted sync here would only copy the new head id into campaign
    # state after the snapshot was created, immediately making the branch dirty
    # again and forcing another checkpoint before a safe branch checkout.
    manifest_view = await _manifest_get(client, campaign_id)
    manifest = dict(manifest_view.get("manifest") or {})
    if "snapshot_dag" in manifest:
        snapshot_dag = dict(manifest["snapshot_dag"])
        nodes = list(snapshot_dag.get("nodes") or [])
        projected = next(
            (item for item in nodes if str(item.get("id") or "") == str(snapshot["id"])),
            None,
        )
        if (
            projected is None
            or str(projected.get("branch_id") or "") != str(current_branch["id"])
            or str(snapshot_dag.get("active_branch_id") or "") != str(current_branch["id"])
            or str(snapshot_dag.get("head_snapshot_id") or "") != str(snapshot["id"])
        ):
            raise RuntimeError("checkpoint is missing from the current manifest Snapshot DAG")
    return {
        "sync": synced,
        "snapshot": snapshot,
        "verification": verification,
        "post_sync": {
            "persisted": False,
            "reason": "Snapshot DAG is projected on public manifest reads",
            "campaign_revision": manifest_view.get("campaign_revision"),
        },
        "reused": reused,
        "manifest": manifest_view,
    }


def _party_member(actor: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    actor_id = str(actor["id"])
    sheet = dict(actor["sheet"])
    progression = dict(sheet["progression"])
    hp = dict(sheet["combat"]["hp"])
    effective_hp = dict(dict(actor.get("derived") or {}).get("hit_points") or {})
    return {
        "actor_id": actor_id,
        "name": str(actor["name"]),
        "status": str(selection.get("status") or "active"),
        "source": str(selection["source"]),
        "source_asset_path": str(selection.get("source_asset_path") or ""),
        "level": int(progression["level"]),
        "xp": int(progression["xp"]),
        "hit_points": {
            "current": int(effective_hp.get("value", hp["value"])),
            "maximum": int(effective_hp.get("max", hp["max"])),
            "temporary": int(hp["temp"]),
        },
        "resources": deepcopy(dict(sheet.get("resources") or {})),
        "wallet": deepcopy(dict(sheet["inventory"]["wallet"])),
        "equipment": sorted(str(item["id"]) for item in sheet["inventory"]["items"]),
        "knowledge_scope_actor_id": actor_id,
    }


def _manifest_recovery_inputs(
    *,
    corpus_manifest: dict[str, Any],
    import_report: dict[str, Any],
    campaign_id: str,
    campaign_line_id: str,
) -> dict[str, Any]:
    """Recover audited initialization inputs without treating a report as runtime state."""

    if (
        import_report.get("action") != "full-campaign-corpus-import"
        or import_report.get("passed") is not True
    ):
        raise ValueError("campaign import report must be a successful public corpus import")
    line = next(
        (
            deepcopy(item)
            for item in corpus_manifest.get("campaign_lines") or []
            if str(item.get("id") or "") == campaign_line_id
        ),
        None,
    )
    if line is None:
        raise ValueError(f"campaign line is absent from the corpus manifest: {campaign_line_id}")
    imported = next(
        (
            deepcopy(item)
            for item in import_report.get("campaigns") or []
            if str(item.get("campaign_line_id") or "") == campaign_line_id
            and str(item.get("campaign_id") or "") == campaign_id
        ),
        None,
    )
    if imported is None:
        raise ValueError("campaign import report does not identify this campaign and line")
    documents = [
        deepcopy(item) for item in imported.get("documents") or [] if isinstance(item, dict)
    ]
    documents_by_path = {str(item.get("relative_path") or ""): item for item in documents}
    if "" in documents_by_path or len(documents_by_path) != len(documents):
        raise ValueError("campaign import report document paths must be non-empty and unique")

    module_documents: list[dict[str, Any]] = []
    for entry in sorted(line.get("modules") or [], key=lambda item: int(item["sequence"])):
        relative_path = str(entry["path"])
        document = documents_by_path.get(relative_path)
        if (
            document is None
            or str(document.get("checksum") or "").casefold()
            != str(entry.get("sha256") or "").casefold()
            or not str(document.get("module_id") or "")
        ):
            raise ValueError(
                f"campaign import report lacks the verified module document: {relative_path}"
            )
        module_documents.append(document)

    player_documents: list[dict[str, Any]] = []
    for entry in line.get("player_materials") or []:
        relative_path = str(entry["path"])
        document = documents_by_path.get(relative_path)
        if (
            document is None
            or str(document.get("checksum") or "").casefold()
            != str(entry.get("sha256") or "").casefold()
        ):
            raise ValueError(
                f"campaign import report lacks the verified player material: {relative_path}"
            )
        document["declared_player_material"] = deepcopy(entry)
        player_documents.append(document)

    return {
        "line": line,
        "module_documents": module_documents,
        "review_blocks": _line_review_blocks(line, player_documents),
    }


async def _initialize_manifest_from_import_report(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    campaign_line_id: str,
    corpus_root: Path | None,
    corpus_manifest_path: Path,
    import_report_path: Path | None,
) -> dict[str, Any]:
    """Initialize a missing manifest for a previously verified public import."""

    if not campaign_line_id.strip():
        raise ValueError("initialize-manifest requires --campaign-line-id")
    if corpus_root is None or import_report_path is None:
        raise ValueError("initialize-manifest requires --corpus-root and --campaign-import-report")
    resolved_root = corpus_root.expanduser().resolve()
    resolved_manifest = corpus_manifest_path.expanduser().resolve()
    resolved_report = import_report_path.expanduser().resolve()
    if not resolved_report.is_file():
        raise FileNotFoundError(resolved_report)
    corpus_manifest = _load_and_verify_manifest(resolved_manifest, resolved_root)
    import_report = json.loads(resolved_report.read_text(encoding="utf-8"))
    recovered = _manifest_recovery_inputs(
        corpus_manifest=corpus_manifest,
        import_report=import_report,
        campaign_id=campaign_id,
        campaign_line_id=campaign_line_id.strip(),
    )

    active_modules = await client.domain(
        "module_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    if not isinstance(active_modules, list) or any(
        not isinstance(item, dict) for item in active_modules
    ):
        raise RuntimeError("module_query(list) returned an invalid collection")
    actual_module_ids = {str(item.get("id") or "") for item in active_modules}
    expected_module_ids = {str(item["module_id"]) for item in recovered["module_documents"]}
    if actual_module_ids != expected_module_ids:
        raise RuntimeError(
            "active campaign modules differ from the verified import report: "
            f"expected {sorted(expected_module_ids)}, found {sorted(actual_module_ids)}"
        )
    initialized = await _initialize_playthrough_manifest(
        client,
        line=recovered["line"],
        module_documents=recovered["module_documents"],
        campaign_id=campaign_id,
        run_id=run_id,
        review_blocks=recovered["review_blocks"],
    )
    return {
        "initialization": initialized,
        "campaign_line_id": campaign_line_id.strip(),
        "module_ids": [str(item["module_id"]) for item in recovered["module_documents"]],
        "review_blocks": deepcopy(recovered["review_blocks"]),
        "corpus_manifest": str(resolved_manifest),
        "campaign_import_report": str(resolved_report),
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
        if item.get("source") not in PARTY_MEMBER_SOURCES:
            raise ValueError("party member source must be pregen, generated, or replacement")
    current = await _manifest_get(client, campaign_id)
    manifest = deepcopy(current["manifest"])
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


def _settled_agent_ruling(
    value: Any,
    *,
    label: str,
    ruling_kinds: frozenset[str],
    extra_fields: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{label} Agent ruling must be a JSON object")
    allowed = {
        "default_resolver",
        "ruling_kind",
        "decision",
        "reason",
        *extra_fields,
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{label} Agent ruling contains unsupported fields: " + ", ".join(unknown))
    if value.get("default_resolver") != "agent":
        raise ValueError(f"{label} Agent ruling default_resolver must be agent")
    ruling_kind = str(value.get("ruling_kind") or "")
    if ruling_kind not in ruling_kinds:
        expected = (
            next(iter(ruling_kinds)) if len(ruling_kinds) == 1 else ", ".join(sorted(ruling_kinds))
        )
        raise ValueError(f"{label} Agent ruling ruling_kind must be {expected}")
    decision = str(value.get("decision") or "").strip()
    reason = str(value.get("reason") or "").strip()
    if not decision or len(decision) > 1_000:
        raise ValueError(f"{label} Agent ruling decision must contain 1 to 1000 characters")
    if not reason or len(reason) > 500:
        raise ValueError(f"{label} Agent ruling reason must contain 1 to 500 characters")
    return {
        "default_resolver": "agent",
        "ruling_kind": ruling_kind,
        "decision": decision,
        "reason": reason,
        **{field: deepcopy(value.get(field)) for field in extra_fields},
        "committed": True,
    }


def _settled_replacement_agent_ruling(value: Any) -> dict[str, Any] | None:
    return _settled_agent_ruling(
        value,
        label="replacement",
        ruling_kinds=frozenset({"module_specific_procedure"}),
    )


def _settled_time_agent_ruling(
    value: Any,
    *,
    period: str,
    count: int | None,
) -> dict[str, Any] | None:
    normalized = _settled_agent_ruling(
        value,
        label="time",
        ruling_kinds=frozenset({"agent_dm_adjudication"}),
        extra_fields=frozenset({"period", "count"}),
    )
    if normalized is None:
        return None
    if normalized.get("period") != period or normalized.get("count") != count:
        raise ValueError(
            "time Agent ruling period and count must exactly match the requested advance"
        )
    return normalized


def _normalize_expected_calendar_time(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    return validate_calendar_minute_point(value, field="expected world time")


def _project_world_time(clock: dict[str, Any], elapsed_minutes: int) -> dict[str, int]:
    return advance_calendar_minutes_from_elapsed(
        clock.get("elapsed_minutes"),
        elapsed_minutes,
    )


def _settled_event_agent_ruling(value: Any) -> dict[str, Any] | None:
    return _settled_agent_ruling(
        value,
        label="event",
        ruling_kinds=frozenset(
            {
                "agent_dm_adjudication",
                "module_specific_procedure",
            }
        ),
    )


def _settled_check_agent_ruling(
    value: Any,
    *,
    dc: int | None,
) -> dict[str, Any] | None:
    normalized = _settled_agent_ruling(
        value,
        label="check",
        ruling_kinds=frozenset({"agent_dm_adjudication"}),
        extra_fields=frozenset({"dc"}),
    )
    if normalized is None:
        return None
    if normalized.get("dc") != dc:
        raise ValueError("check Agent ruling DC must exactly match --check-dc")
    return normalized


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
    agent_ruling: dict[str, Any] | None,
    summary: str,
    handoff_knowledge: list[str],
    witness_actor_ids: list[str],
    defer_checkpoint: bool = False,
) -> dict[str, Any]:
    predecessor_id = predecessor_actor_id.strip()
    replacement_id = replacement_actor_id.strip()
    normalized_summary = summary.strip()
    handoff = [item.strip() for item in handoff_knowledge if item.strip()]
    witnesses = list(dict.fromkeys(witness_actor_ids))
    normalized_agent_ruling = _settled_replacement_agent_ruling(agent_ruling)
    if not all(
        (
            predecessor_id,
            replacement_id,
            scene_id,
            location_key,
            normalized_summary,
        )
    ):
        raise ValueError(
            "register-replacement requires predecessor, replacement, scene, location, and summary"
        )
    has_source_evidence = source_ref is not None or bool(source_excerpt.strip())
    if normalized_agent_ruling is not None and has_source_evidence:
        raise ValueError(
            "replacement arrival must use either exact source evidence or an Agent ruling, not both"
        )
    if normalized_agent_ruling is None and (source_ref is None or not source_excerpt.strip()):
        raise ValueError(
            "replacement arrival requires exact source evidence or a settled Agent ruling"
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
    exact_ref = (
        await _validate_source_ref(client, scene, source_ref, excerpt=source_excerpt)
        if normalized_agent_ruling is None
        else None
    )
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
    replacement_derived_hp = dict(dict(replacement.get("derived") or {}).get("hit_points") or {})
    replacement_conditions = {
        str(item).casefold() for item in list(replacement_derived_hp.get("conditions") or [])
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

    knowledge_prefix = f"playthrough.{_token(run_id)}.replacement.{_token(replacement_id)}"
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
    _remap_replacement_level_endings(
        prospective,
        predecessor_actor_id=predecessor_id,
        replacement_actor_id=replacement_id,
    )
    validate_playthrough_manifest(prospective)

    campaign = await _campaign(client, campaign_id)
    committed = await client.domain(
        "memory_change",
        {
            "campaign_id": campaign_id,
            "action": "commit",
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
                        "source_excerpt": (
                            source_excerpt.strip() if normalized_agent_ruling is None else ""
                        ),
                        "source_ref": exact_ref,
                        "agent_ruling": normalized_agent_ruling,
                    },
                },
                "actor_knowledge": actor_knowledge,
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
    checkpoint = None
    if not defer_checkpoint:
        checkpoint = await _checkpoint(
            client,
            campaign_id=campaign_id,
            run_id=run_id,
            label=(
                "Full playthrough replacement: "
                f"{replacement['name']} succeeds {predecessor['name']}"
            ),
            checkpoint_id=f"replacement:{predecessor_id}:{replacement_id}",
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
    actual_keys = {str(item.get("knowledge_key") or "") for item in replacement_knowledge_after}
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
            "agent_ruling": normalized_agent_ruling,
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
    occurrence_id: str,
    scene_id: str,
    source_scene_id: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    objective: str,
    mark_visited: bool,
    reachable_scene_ids: list[str],
    excluded_scenes: list[dict[str, Any]],
    agent_ruling: dict[str, Any] | None = None,
    occurrence_scene_id: str = "",
    location_key: str = "",
) -> dict[str, Any]:
    scene_identity = _occurrence_identity(occurrence_id, "advance-scene")
    if not all((scene_id, source_scene_id, source_excerpt)):
        raise ValueError(
            "advance-scene requires --scene-id, --source-scene-id, "
            "--source-excerpt, and --source-ref-json"
        )
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
    transition_from_scene_id = occurrence_scene_id.strip() or source_scene_id
    occurrence_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": transition_from_scene_id},
        },
    )
    if (
        occurrence_scene.get("redacted")
        or str(occurrence_scene.get("scene_id") or "") != transition_from_scene_id
    ):
        raise RuntimeError("transition occurrence scene is redacted or invalid")
    source_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": source_scene_id},
        },
    )
    exact_ref = await _validate_source_ref(
        client,
        source_scene,
        source_ref,
        excerpt=source_excerpt,
    )
    normalized_agent_ruling = _settled_agent_ruling(
        agent_ruling,
        label="scene transition",
        ruling_kinds=frozenset({"agent_dm_adjudication"}),
    )
    scene_locations = {
        str(item.get("key") or "") for item in _scene_locations(scene) if item.get("key")
    }
    selected_location_key = location_key.strip()
    if selected_location_key and selected_location_key not in scene_locations:
        raise ValueError("advance-scene location is not present in the target Scene Atlas")
    progress_rows = await client.domain(
        "module_query",
        {"campaign_id": campaign_id, "view": "progress"},
    )
    progress_before = next(
        (item for item in progress_rows if item.get("scene_id") == scene_id),
        None,
    )
    current_scene_id = str(dict(manifest.get("current") or {}).get("scene_id") or "")
    transitions = deepcopy(
        dict(dict(manifest.get("world_state") or {}).get("scene_transitions") or {})
    )
    transition_record = {
        "from_scene_id": transition_from_scene_id,
        "to_scene_id": scene_id,
        "source_excerpt": source_excerpt,
        "source_ref": exact_ref,
        **(
            {"agent_ruling": normalized_agent_ruling} if normalized_agent_ruling is not None else {}
        ),
    }
    existing_transition = transitions.get(scene_identity)
    progress_state = deepcopy(dict((progress_before or {}).get("state") or {}))
    progress_entries = deepcopy(dict(progress_state.get("full_playthrough_scene_entries") or {}))
    existing_progress_transition = progress_entries.get(scene_identity)
    exact_retry = existing_transition == transition_record and current_scene_id == scene_id
    interrupted_retry = (
        existing_progress_transition == transition_record and current_scene_id == scene_id
    )
    initial_scene_selection = (
        not current_scene_id and transition_from_scene_id == scene_id and not transitions
    )
    if (
        current_scene_id != transition_from_scene_id
        and not exact_retry
        and not interrupted_retry
        and not initial_scene_selection
    ):
        raise ValueError(
            "advance-scene occurrence scene must be the manifest current scene "
            "or the exact initial scene"
        )
    if existing_transition is not None and existing_transition != transition_record:
        raise ValueError(
            "advance-scene occurrence id already exists with different transition evidence"
        )
    if (
        existing_progress_transition is not None
        and existing_progress_transition != transition_record
    ):
        raise ValueError(
            "advance-scene occurrence id already exists in SceneProgress with "
            "different transition evidence"
        )
    needs_current_repair = (
        existing_progress_transition == transition_record
        and str((progress_before or {}).get("status") or "") != "current"
    )
    if existing_progress_transition == transition_record and not needs_current_repair:
        progress_result = deepcopy(progress_before)
    else:
        progress_entries[scene_identity] = transition_record
        current_progress = _scene_progress_percent(progress_before)
        current_status = str((progress_before or {}).get("status") or "")
        progress_result = await client.domain(
            "module_set_progress",
            {
                "campaign_id": campaign_id,
                "scene_id": scene_id,
                "status": "current",
                "progress": (100 if current_status == "completed" else max(current_progress, 1)),
                "state": {
                    **progress_state,
                    "full_playthrough_scene_entries": progress_entries,
                },
                "current_location_key": (
                    selected_location_key
                    or str((progress_before or {}).get("current_location_key") or "")
                    or None
                ),
                "expected_state_version": int((progress_before or {}).get("state_version", 0) or 0),
                "idempotency_key": _mutation_key(
                    run_id,
                    (
                        "scene-transition-current-repair"
                        if needs_current_repair
                        else "scene-transition-progress"
                    ),
                    scene_identity,
                ),
            },
        )
    current_runtime_scene = _facade_value(
        await client.domain(
            "module_query",
            {"campaign_id": campaign_id, "view": "current"},
        )
    )
    if (
        not isinstance(current_runtime_scene, dict)
        or str(current_runtime_scene.get("scene_id") or "") != scene_id
        or str(dict(current_runtime_scene.get("progress") or {}).get("status") or "") != "current"
    ):
        raise RuntimeError(
            "module current-scene projection did not converge with the scene transition"
        )
    transitions[scene_identity] = transition_record
    manifest["world_state"] = {
        **deepcopy(dict(manifest.get("world_state") or {})),
        "scene_transitions": transitions,
    }
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
    replaced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="replace",
        run_id=run_id,
        identity=f"advance-scene:{scene_identity}",
        payload={"manifest": manifest},
    )
    observed = await _manifest_get(client, campaign_id)
    observed_current = dict(dict(observed.get("runtime") or {}).get("current_scene") or {})
    if observed_current and str(observed_current.get("scene_id") or "") != scene_id:
        raise RuntimeError(
            "live playthrough projection diverged from the authoritative current scene"
        )
    return {
        **observed,
        "mutation_receipt": replaced,
        "scene_progress": progress_result,
        "current_scene": current_runtime_scene,
    }


def _segment_completion_record(
    manifest: dict[str, Any],
    *,
    condition_id: str,
    next_module_id: str,
) -> dict[str, Any]:
    ending = deepcopy(dict(manifest.get("ending") or {}))
    normalized_condition_id = condition_id.strip()
    if (
        manifest.get("status") != "completed"
        or ending.get("status") != "completed"
        or str(ending.get("achieved_condition_id") or "") != normalized_condition_id
        or not list(ending.get("verification") or [])
        or any(item.get("passed") is not True for item in ending["verification"])
    ):
        raise ValueError(
            "continue-segment requires the exact completed ending with only passing "
            "verification results"
        )
    current = deepcopy(dict(manifest.get("current") or {}))
    completed_module_id = str(current.get("module_id") or "")
    if (
        not completed_module_id
        or not next_module_id
        or next_module_id == completed_module_id
        or next_module_id not in set(manifest.get("module_ids") or [])
    ):
        raise ValueError(
            "continue-segment requires a different next module already declared by "
            "the campaign-line manifest"
        )
    dag = deepcopy(dict(manifest.get("snapshot_dag") or {}))
    head_snapshot_id = str(dag.get("head_snapshot_id") or "")
    head = next(
        (
            deepcopy(item)
            for item in list(dag.get("nodes") or [])
            if str(item.get("id") or "") == head_snapshot_id
        ),
        None,
    )
    if (
        head is None
        or head.get("is_head") is not True
        or str(head.get("branch_id") or "") != str(dag.get("active_branch_id") or "")
    ):
        raise ValueError("continue-segment requires a verified terminal head on the active branch")
    world_state = deepcopy(dict(manifest.get("world_state") or {}))
    canonical = deepcopy(dict(world_state.get("_canonical") or {}))
    condition = next(
        (
            deepcopy(item)
            for item in list(ending.get("conditions") or [])
            if str(item.get("id") or "") == normalized_condition_id
        ),
        None,
    )
    if condition is None:
        raise ValueError("completed segment condition is missing from the ending manifest")
    return {
        "condition_id": normalized_condition_id,
        "completed_module_id": completed_module_id,
        "next_module_id": next_module_id,
        "terminal_scene": current,
        "ending": ending,
        "condition": condition,
        "terminal_snapshot": {
            key: deepcopy(head.get(key))
            for key in ("id", "parent_id", "branch_id", "slot", "label", "checksum")
        },
        "random_stream": deepcopy(dict(manifest.get("random_stream") or {})),
        "game_time": deepcopy(dict(canonical.get("game_time") or {})),
        "world_time": deepcopy(dict(canonical.get("world_time") or {})),
    }


def _prepare_segment_continuation(
    manifest: dict[str, Any],
    *,
    condition_id: str,
    next_module_id: str,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Archive one verified volume ending and reopen its continuous campaign line."""

    updated = deepcopy(manifest)
    world_state = deepcopy(dict(updated.get("world_state") or {}))
    raw_history = world_state.get("completed_segments") or []
    if not isinstance(raw_history, list) or any(not isinstance(item, dict) for item in raw_history):
        raise ValueError("world_state.completed_segments must be a list of objects")
    history = [deepcopy(item) for item in raw_history]
    normalized_condition_id = condition_id.strip()
    existing = next(
        (
            item
            for item in history
            if str(item.get("condition_id") or "") == normalized_condition_id
        ),
        None,
    )
    if updated.get("status") == "completed":
        if existing is not None:
            raise ValueError("completed segment cannot already be archived before continuation")
        record = _segment_completion_record(
            updated,
            condition_id=normalized_condition_id,
            next_module_id=next_module_id,
        )
        history.append(record)
        world_state["completed_segments"] = history
        updated["world_state"] = world_state
        updated["status"] = "in_progress"
        updated["ending"] = {
            "status": "pending",
            "conditions": [],
            "achieved_condition_id": "",
            "verification": [],
        }
        return validate_playthrough_manifest(updated), record, False
    if (
        updated.get("status") != "in_progress"
        or existing is None
        or str(existing.get("next_module_id") or "") != next_module_id
        or str(dict(updated.get("ending") or {}).get("status") or "") != "pending"
        or str(dict(updated.get("ending") or {}).get("achieved_condition_id") or "")
    ):
        raise ValueError(
            "continue-segment can resume only its exact archived in-progress transition"
        )
    return validate_playthrough_manifest(updated), deepcopy(existing), True


def _party_continuity_projection(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            key: deepcopy(member.get(key))
            for key in (
                "actor_id",
                "status",
                "level",
                "xp",
                "hit_points",
                "resources",
                "wallet",
                "equipment",
                "knowledge_scope_actor_id",
            )
        }
        for member in list(dict(manifest.get("party") or {}).get("members") or [])
    ]


async def _continue_completed_segment(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    condition_id: str,
    occurrence_id: str,
    scene_id: str,
    source_scene_id: str,
    occurrence_scene_id: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    objective: str,
    location_key: str,
    reachable_scene_ids: list[str],
    checkpoint_label: str,
) -> dict[str, Any]:
    transition_identity = _occurrence_identity(occurrence_id, "continue-segment")
    target_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    next_module_id = str(target_scene.get("module_id") or "")
    current = await _manifest_get(client, campaign_id)
    before_manifest = deepcopy(dict(current["manifest"]))
    before_party = _party_continuity_projection(before_manifest)
    before_random_stream = deepcopy(dict(before_manifest.get("random_stream") or {}))
    prepared, segment_record, recovered = _prepare_segment_continuation(
        before_manifest,
        condition_id=condition_id,
        next_module_id=next_module_id,
    )
    archived = None
    if not recovered:
        archived = await _manifest_mutation(
            client,
            campaign_id=campaign_id,
            action="replace",
            run_id=run_id,
            identity=(
                f"continue-segment-archive:{transition_identity}:{condition_id}:{next_module_id}"
            ),
            payload={"manifest": prepared},
        )
    transition = await _advance_scene(
        client,
        campaign_id=campaign_id,
        run_id=run_id,
        occurrence_id=transition_identity,
        scene_id=scene_id,
        source_scene_id=source_scene_id,
        source_excerpt=source_excerpt,
        source_ref=source_ref,
        objective=objective,
        mark_visited=True,
        reachable_scene_ids=reachable_scene_ids,
        excluded_scenes=[],
        occurrence_scene_id=occurrence_scene_id,
        location_key=location_key,
    )
    checkpoint = await _checkpoint(
        client,
        campaign_id=campaign_id,
        run_id=run_id,
        label=(
            checkpoint_label.strip()
            or f"Campaign segment continuation: {condition_id} to {target_scene.get('title')}"
        ),
        checkpoint_id=f"continue-segment:{transition_identity}",
    )
    final = await _manifest_get(client, campaign_id)
    final_manifest = deepcopy(dict(final["manifest"]))
    if (
        final_manifest.get("status") != "in_progress"
        or str(dict(final_manifest.get("ending") or {}).get("status") or "") != "pending"
        or str(dict(final_manifest.get("current") or {}).get("scene_id") or "") != scene_id
        or str(dict(final_manifest.get("current") or {}).get("module_id") or "") != next_module_id
        or _party_continuity_projection(final_manifest) != before_party
        or dict(final_manifest.get("random_stream") or {}) != before_random_stream
        or segment_record
        not in list(dict(final_manifest.get("world_state") or {}).get("completed_segments") or [])
        or str(dict(final_manifest.get("snapshot_dag") or {}).get("head_snapshot_id") or "")
        != str(dict(checkpoint.get("snapshot") or {}).get("id") or "")
    ):
        raise RuntimeError("campaign segment continuation failed its continuity audit")
    return {
        "segment": segment_record,
        "archive": archived,
        "archive_recovered": recovered,
        "transition": transition,
        "checkpoint": checkpoint,
        "manifest": final_manifest,
    }


async def _branch_from_snapshot(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    initial_phase: str,
    snapshot_slot: int | None,
    branch_name: str,
    checkpoint_label: str,
    core_conversion_reason: str = "",
) -> dict[str, Any]:
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
    core_lock = await client.domain(
        "snapshot_query",
        {
            "campaign_id": campaign_id,
            "view": "core",
            "payload": {"slot": snapshot_slot},
        },
    )
    conversion_required = bool(core_lock.get("conversion_required"))
    if conversion_required and not core_conversion_reason.strip():
        raise RuntimeError(
            "snapshot uses an unavailable built-in Core; rerun with "
            "--core-conversion-reason after reviewing the runtime upgrade"
        )
    if core_conversion_reason.strip() and not conversion_required:
        raise RuntimeError("snapshot Core conversion was requested but is not required")

    async def restored_branch_checkpoint() -> dict[str, Any]:
        try:
            return await _checkpoint(
                client,
                campaign_id=campaign_id,
                run_id=run_id,
                label=(
                    checkpoint_label.strip()
                    or (f"Branch {branch_name.strip()} restored from snapshot slot {snapshot_slot}")
                ),
                checkpoint_id=(f"branch-restored:{snapshot_slot}:{branch_name.strip()}"),
            )
        except Exception as error:
            if "campaign has no full-playthrough manifest" not in str(error):
                raise
            return {
                "skipped": True,
                "reason": "The verified target snapshot predates manifest initialization.",
                "required_action": "initialize-manifest",
            }

    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    source_branch = next((item for item in branches if item.get("is_current")), None)
    if source_branch is None:
        raise RuntimeError("campaign has no current branch")
    branch_identity = f"{snapshot_slot}:{branch_name.strip()}"
    branch_key = _mutation_key(run_id, "branch-from-snapshot", branch_identity)
    if str(source_branch.get("name") or "") == branch_name.strip() and str(
        source_branch.get("base_snapshot_id") or ""
    ) == str(target["id"]):
        receipt = await client.domain(
            "state_revision",
            {
                "campaign_id": campaign_id,
                "action": "receipt",
                "payload": {"idempotency_key": branch_key},
            },
        )
        recovered_branch = dict(receipt.get("response") or {})
        if (
            str(recovered_branch.get("id") or "") != str(source_branch["id"])
            or str(recovered_branch.get("name") or "") != branch_name.strip()
            or str(recovered_branch.get("base_snapshot_id") or "") != str(target["id"])
            or not bool(recovered_branch.get("is_current"))
        ):
            raise RuntimeError(
                "branch-from-snapshot recovery receipt does not match the current restored branch"
            )
        original_source_branch = next(
            (
                item
                for item in branches
                if str(item.get("id") or "") == str(target.get("branch_id") or "")
            ),
            None,
        )
        restored_campaign = await _campaign(client, campaign_id)
        restored_phase = _campaign_phase(restored_campaign)
        await client.open(campaign_id)
        await client.load()
        checkpoint = await restored_branch_checkpoint()
        source_head_snapshot = next(
            (
                item
                for item in snapshots
                if original_source_branch is not None
                and str(item.get("id") or "")
                == str(original_source_branch.get("head_snapshot_id") or "")
            ),
            None,
        )
        return {
            "source_branch": original_source_branch,
            "source_head_snapshot_id": (
                original_source_branch.get("head_snapshot_id")
                if original_source_branch is not None
                else None
            ),
            "source_checkpoint": {
                "snapshot": source_head_snapshot,
                "recovered_existing": True,
            },
            "target_snapshot": target,
            "target_verification": verification,
            "target_core_lock": core_lock,
            "created_branch": recovered_branch,
            "phase_changes": [],
            "restored_phase": restored_phase,
            "checkpoint": checkpoint,
            "recovered_after_branch_create_interruption": True,
        }
    phase_changes = []
    campaign = await _campaign(client, campaign_id)
    if initial_phase not in {"lobby", "combat"}:
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
        await client.load()
    source_checkpoint = await _checkpoint(
        client,
        campaign_id=campaign_id,
        run_id=f"{run_id}-source",
        label=(
            f"Preserve source branch before forking snapshot slot {snapshot_slot}: "
            f"{branch_name.strip()}"
        ),
        checkpoint_id=f"branch-source:{snapshot_slot}:{branch_name.strip()}",
    )
    campaign = await _campaign(client, campaign_id)
    branch_action = "create_core_upgrade" if conversion_required else "create"
    branch_payload = (
        {
            "slot": snapshot_slot,
            "name": branch_name.strip(),
            "expected_snapshot_core_fingerprint": str(
                dict(core_lock.get("core_pack") or {}).get("fingerprint") or ""
            ),
            "expected_runtime_core_fingerprint": str(
                dict(core_lock.get("available_core_pack") or {}).get("fingerprint") or ""
            ),
            "reason": core_conversion_reason.strip(),
        }
        if conversion_required
        else {
            "name": branch_name.strip(),
            "from_snapshot_id": str(target["id"]),
            "checkout": True,
        }
    )
    created = await client.domain(
        "branch_change",
        {
            "campaign_id": campaign_id,
            "action": branch_action,
            "payload": branch_payload,
            "expected_revision": campaign["revision"],
            "expected_branch_id": str(source_branch["id"]),
            "idempotency_key": _mutation_key(run_id, "branch-from-snapshot", branch_identity),
        },
    )
    restored_campaign = await _campaign(client, campaign_id)
    restored_phase = _campaign_phase(restored_campaign)
    if restored_phase == "combat":
        raise RuntimeError("selected snapshot unexpectedly restored active combat")
    await client.open(campaign_id)
    await client.load()
    checkpoint = await restored_branch_checkpoint()
    return {
        "source_branch": source_branch,
        "source_head_snapshot_id": source_branch.get("head_snapshot_id"),
        "source_checkpoint": source_checkpoint,
        "target_snapshot": target,
        "target_verification": verification,
        "target_core_lock": core_lock,
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
    occurrence_id: str,
    actor_id: str,
    kind: str,
    ability: str,
    dc: int | None,
    proficient: bool,
    bonus: int = 0,
    advantage: bool = False,
    disadvantage: bool = False,
    knowledge_actor_ids: list[str],
    success_knowledge: str,
    failure_knowledge: str,
    agent_ruling: dict[str, Any] | None = None,
    source_scene_id: str = "",
    defer_checkpoint: bool = False,
) -> dict[str, Any]:
    check_identity = _check_identity(occurrence_id)
    if not all((scene_id, location_key, source_excerpt, actor_id, kind, ability)):
        raise ValueError(
            "resolve-check requires scene, location, excerpt, actor, kind, and ability"
        )
    if dc is None or dc < 0:
        raise ValueError("resolve-check requires a non-negative --check-dc")
    if kind not in ACTOR_CHECK_KINDS:
        raise ValueError("resolve-check kind is not supported by character_check")
    if advantage and disadvantage:
        raise ValueError("resolve-check cannot apply advantage and disadvantage together")
    normalized_agent_ruling = _settled_check_agent_ruling(agent_ruling, dc=dc)
    occurrence_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    cited_scene_id = source_scene_id.strip() or scene_id
    cited_scene = occurrence_scene
    if cited_scene_id != scene_id:
        cited_scene = await client.domain(
            "module_query",
            {
                "campaign_id": campaign_id,
                "view": "scene",
                "payload": {"scene_id": cited_scene_id},
            },
        )
    exact_ref = await _validate_source_ref(
        client,
        cited_scene,
        source_ref,
        excerpt=source_excerpt,
    )
    location_keys = {str(item.get("key") or "") for item in _scene_locations(occurrence_scene)}
    if location_key not in location_keys:
        raise ValueError("resolve-check location is not present in the scene atlas")
    actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": actor_id}},
    )
    if actor.get("campaign_id") != campaign_id:
        raise ValueError("resolve-check actor does not belong to the campaign")
    if _actor_card_has_named_skill(actor, ability) and proficient:
        raise ValueError(
            "resolve-check named skills derive proficiency, expertise, and bonuses "
            "from the actor card; omit --check-proficient"
        )
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
        run_id=run_id,
        occurrence_id=check_identity,
        location_key=location_key,
        actor_id=actor_id,
        kind=kind,
        ability=ability,
        dc=dc,
        proficient=proficient,
        bonus=bonus,
        advantage=advantage,
        disadvantage=disadvantage,
        source_ref=exact_ref,
        agent_ruling=normalized_agent_ruling,
    )
    if progress_matches:
        progress = deepcopy(progress_before)
    else:
        progress = await client.domain(
            "module_set_progress",
            {
                "campaign_id": campaign_id,
                "scene_id": scene_id,
                "status": _scene_progress_write_status(progress_before),
                "progress": max(_scene_progress_percent(progress_before), 50),
                "state": {
                    **deepcopy(dict((progress_before or {}).get("state") or {})),
                    "full_playthrough_check": {
                        "run_id": run_id,
                        "occurrence_id": check_identity,
                        "actor_id": actor_id,
                        "kind": kind,
                        "ability": ability,
                        "dc": dc,
                        "proficient": proficient,
                        "bonus": bonus,
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
                    check_identity,
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
                "action": "check",
                "payload": {
                    "actor_id": actor_id,
                    "kind": kind,
                    "ability": ability,
                    "dc": dc,
                    "proficient": proficient,
                    "bonus": bonus,
                    "advantage": advantage,
                    "disadvantage": disadvantage,
                },
                "branch_id": str(branch["id"]),
                "expected_revision": campaign["revision"],
                "idempotency_key": _mutation_key(
                    run_id,
                    "character-check",
                    check_identity,
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
    continuity_payload = {
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
                "occurrence_id": check_identity,
                "kind": kind,
                "ability": ability,
                "dc": dc,
                "bonus": bonus,
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
                "knowledge_key": _check_knowledge_key(run_id, check_identity),
                "proposition": proposition,
                "disclosure_scope": "owner",
            }
            for recipient in recipients
        ],
        "branch_id": str(branch["id"]),
    }
    if not defer_checkpoint:
        continuity_payload["snapshot"] = {
            "label": f"Full playthrough check: {kind} at {location_key}"
        }
    committed = await _commit_roll_continuity(
        client,
        campaign_id=campaign_id,
        payload=continuity_payload,
        expected_revision=campaign["revision"],
        idempotency_key=_mutation_key(run_id, "continuity", check_identity),
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"resolve-check-sync:{check_identity}",
    )
    return {
        "scene": {
            "scene_id": scene_id,
            "source_scene_id": cited_scene_id,
            "location_key": location_key,
            "source_ref": exact_ref,
        },
        "actor": {"id": actor_id, "name": actor["name"]},
        "occurrence_id": check_identity,
        "progress": progress,
        "check_request": {
            "actor_id": actor_id,
            "kind": kind,
            "ability": ability,
            "dc": dc,
            "proficient": proficient,
            "bonus": bonus,
            "advantage": advantage,
            "disadvantage": disadvantage,
        },
        "check": check_result,
        "check_recovered": recovered is not None,
        "agent_ruling": normalized_agent_ruling,
        "knowledge_actor_ids": recipients,
        "continuity": committed,
        "sync": synced,
    }


async def _resolve_group_check(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    occurrence_id: str,
    actor_ids: list[str],
    ability: str,
    dc: int | None,
    proficient: bool,
    bonus: int,
    advantage: bool,
    disadvantage: bool,
    knowledge_actor_ids: list[str],
    success_knowledge: str,
    failure_knowledge: str,
    source_scene_id: str = "",
    defer_checkpoint: bool = False,
) -> dict[str, Any]:
    group_identity = _occurrence_identity(occurrence_id, "resolve-group-check")
    normalized_actor_ids = list(dict.fromkeys(str(actor_id).strip() for actor_id in actor_ids))
    if not all((scene_id, location_key, source_excerpt, ability)):
        raise ValueError("resolve-group-check requires scene, location, excerpt, and ability")
    if len(normalized_actor_ids) < 2 or any(not actor_id for actor_id in normalized_actor_ids):
        raise ValueError(
            "resolve-group-check requires at least two non-empty --group-check-actor-id"
        )
    if len(normalized_actor_ids) != len(actor_ids):
        raise ValueError("resolve-group-check actor ids must be unique")
    if dc is None or dc < 0:
        raise ValueError("resolve-group-check requires a non-negative --check-dc")
    if advantage and disadvantage:
        raise ValueError("resolve-group-check cannot apply advantage and disadvantage together")
    occurrence_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    cited_scene_id = source_scene_id.strip() or scene_id
    cited_scene = occurrence_scene
    if cited_scene_id != scene_id:
        cited_scene = await client.domain(
            "module_query",
            {
                "campaign_id": campaign_id,
                "view": "scene",
                "payload": {"scene_id": cited_scene_id},
            },
        )
    exact_ref = await _validate_source_ref(
        client,
        cited_scene,
        source_ref,
        excerpt=source_excerpt,
    )
    location_keys = {str(item.get("key") or "") for item in _scene_locations(occurrence_scene)}
    if location_key not in location_keys:
        raise ValueError("resolve-group-check location is not present in the scene atlas")
    actors = await _validate_campaign_actor_ids(
        client,
        campaign_id=campaign_id,
        actor_ids=normalized_actor_ids,
        operation="resolve-group-check participant",
    )
    if proficient and any(_actor_card_has_named_skill(actor, ability) for actor in actors):
        raise ValueError(
            "resolve-group-check named skills derive proficiency and expertise "
            "from each actor card; omit --check-proficient"
        )
    progress_rows = await client.domain(
        "module_query",
        {"campaign_id": campaign_id, "view": "progress"},
    )
    progress_before = next(
        (item for item in progress_rows if item.get("scene_id") == scene_id),
        None,
    )
    stored_group = dict(
        dict((progress_before or {}).get("state") or {}).get("full_playthrough_group_check") or {}
    )
    progress_matches = bool(
        str((progress_before or {}).get("current_location_key") or "") == location_key
        and stored_group.get("run_id") == run_id
        and stored_group.get("occurrence_id") == group_identity
        and stored_group.get("actor_ids") == normalized_actor_ids
        and stored_group.get("ability") == ability
        and stored_group.get("dc") == dc
        and bool(stored_group.get("proficient", False)) == proficient
        and int(stored_group.get("bonus", 0) or 0) == bonus
        and bool(stored_group.get("advantage", False)) == advantage
        and bool(stored_group.get("disadvantage", False)) == disadvantage
        and stored_group.get("source_ref") == exact_ref
    )
    if progress_matches:
        progress = deepcopy(progress_before)
    else:
        progress = await client.domain(
            "module_set_progress",
            {
                "campaign_id": campaign_id,
                "scene_id": scene_id,
                "status": _scene_progress_write_status(progress_before),
                "progress": max(_scene_progress_percent(progress_before), 50),
                "state": {
                    **deepcopy(dict((progress_before or {}).get("state") or {})),
                    "full_playthrough_group_check": {
                        "run_id": run_id,
                        "occurrence_id": group_identity,
                        "actor_ids": normalized_actor_ids,
                        "ability": ability,
                        "dc": dc,
                        "proficient": proficient,
                        "bonus": bonus,
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
                    group_identity,
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
    settled = await client.domain(
        "character_check",
        {
            "campaign_id": campaign_id,
            "action": "group",
            "payload": {
                "actor_ids": normalized_actor_ids,
                "ability": ability,
                "dc": dc,
                "proficient": proficient,
                "bonus": bonus,
                "advantage": advantage,
                "disadvantage": disadvantage,
            },
            "branch_id": str(branch["id"]),
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id,
                "character-group-check",
                group_identity,
            ),
        },
    )
    check_result = _committed_check_result(settled)
    success = bool(check_result["success"])
    proposition = (success_knowledge.strip() if success else failure_knowledge.strip()) or (
        f"The group {'succeeded' if success else 'failed'} on the DC {dc} "
        f"{ability.title()} group ability check."
    )
    recipients = list(dict.fromkeys([*normalized_actor_ids, *knowledge_actor_ids]))
    campaign = await _campaign(client, campaign_id)
    continuity_payload = {
        "event": {
            "summary": (
                f"The group {'succeeded' if success else 'failed'} on the "
                f"source-cited {ability} group check at {location_key}."
            ),
            "event_type": "ability_group_check",
            "audience_scope": "party",
            "payload": {
                "scene_id": scene_id,
                "location_key": location_key,
                "occurrence_id": group_identity,
                "actor_ids": normalized_actor_ids,
                "ability": ability,
                "dc": dc,
                "bonus": bonus,
                "advantage": advantage,
                "disadvantage": disadvantage,
                "success": success,
                "success_count": check_result["success_count"],
                "required_successes": check_result["required_successes"],
                "source_excerpt": source_excerpt,
                "source_ref": exact_ref,
            },
        },
        "actor_knowledge": [
            {
                "actor_id": recipient,
                "knowledge_key": (
                    f"playthrough.{_token(run_id)}.group-check.{_token(group_identity, length=32)}"
                ),
                "proposition": proposition,
                "disclosure_scope": "owner",
            }
            for recipient in recipients
        ],
        "branch_id": str(branch["id"]),
    }
    if not defer_checkpoint:
        continuity_payload["snapshot"] = {
            "label": f"Full playthrough group check: {ability} at {location_key}"
        }
    committed = await _commit_roll_continuity(
        client,
        campaign_id=campaign_id,
        payload=continuity_payload,
        expected_revision=campaign["revision"],
        idempotency_key=_mutation_key(
            run_id,
            "continuity",
            group_identity,
        ),
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"resolve-group-check-sync:{group_identity}",
    )
    return {
        "scene": {
            "scene_id": scene_id,
            "source_scene_id": cited_scene_id,
            "location_key": location_key,
            "source_ref": exact_ref,
        },
        "actors": [{"id": actor["id"], "name": actor["name"]} for actor in actors],
        "occurrence_id": group_identity,
        "progress": progress,
        "check": check_result,
        "knowledge_actor_ids": recipients,
        "continuity": committed,
        "sync": synced,
    }


async def _resolve_contest(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    occurrence_id: str,
    source_actor_id: str,
    target_actor_id: str,
    source_ability: str,
    target_ability: str,
    source_proficient: bool,
    target_proficient: bool,
    source_advantage: bool,
    source_disadvantage: bool,
    target_advantage: bool,
    target_disadvantage: bool,
    knowledge_actor_ids: list[str],
    source_win_knowledge: str,
    target_win_knowledge: str,
    tie_knowledge: str,
    source_scene_id: str = "",
    defer_checkpoint: bool = False,
) -> dict[str, Any]:
    contest_identity = _contest_identity(occurrence_id)
    if not all(
        (
            scene_id,
            location_key,
            source_excerpt,
            source_actor_id,
            target_actor_id,
            source_ability,
            target_ability,
        )
    ):
        raise ValueError(
            "resolve-contest requires scene, location, excerpt, both actors, and both abilities"
        )
    if source_actor_id == target_actor_id:
        raise ValueError("resolve-contest requires two different actors")
    if source_advantage and source_disadvantage:
        raise ValueError("resolve-contest source cannot have advantage and disadvantage together")
    if target_advantage and target_disadvantage:
        raise ValueError("resolve-contest target cannot have advantage and disadvantage together")
    occurrence_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    cited_scene_id = source_scene_id.strip() or scene_id
    cited_scene = occurrence_scene
    if cited_scene_id != scene_id:
        cited_scene = await client.domain(
            "module_query",
            {
                "campaign_id": campaign_id,
                "view": "scene",
                "payload": {"scene_id": cited_scene_id},
            },
        )
    exact_ref = await _validate_source_ref(
        client,
        cited_scene,
        source_ref,
        excerpt=source_excerpt,
    )
    location_keys = {str(item.get("key") or "") for item in _scene_locations(occurrence_scene)}
    if location_key not in location_keys:
        raise ValueError("resolve-contest location is not present in the scene atlas")
    source_actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": source_actor_id}},
    )
    target_actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": target_actor_id}},
    )
    for label, actor in (("source", source_actor), ("target", target_actor)):
        if actor.get("campaign_id") != campaign_id:
            raise ValueError(f"resolve-contest {label} actor does not belong to campaign")
    if _actor_card_has_named_skill(source_actor, source_ability) and source_proficient:
        raise ValueError(
            "resolve-contest source named skill derives proficiency, expertise, "
            "and bonuses from the actor card"
        )
    if _actor_card_has_named_skill(target_actor, target_ability) and target_proficient:
        raise ValueError(
            "resolve-contest target named skill derives proficiency, expertise, "
            "and bonuses from the actor card"
        )
    progress_rows = await client.domain(
        "module_query",
        {"campaign_id": campaign_id, "view": "progress"},
    )
    progress_before = next(
        (item for item in progress_rows if item.get("scene_id") == scene_id),
        None,
    )
    progress_matches = _matching_contest_progress(
        progress_before,
        run_id=run_id,
        occurrence_id=contest_identity,
        location_key=location_key,
        source_actor_id=source_actor_id,
        target_actor_id=target_actor_id,
        source_ability=source_ability,
        target_ability=target_ability,
        source_proficient=source_proficient,
        target_proficient=target_proficient,
        source_advantage=source_advantage,
        source_disadvantage=source_disadvantage,
        target_advantage=target_advantage,
        target_disadvantage=target_disadvantage,
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
                "status": _scene_progress_write_status(progress_before),
                "progress": max(_scene_progress_percent(progress_before), 50),
                "state": {
                    **deepcopy(dict((progress_before or {}).get("state") or {})),
                    "full_playthrough_contest": {
                        "run_id": run_id,
                        "occurrence_id": contest_identity,
                        "source_actor_id": source_actor_id,
                        "target_actor_id": target_actor_id,
                        "source_ability": source_ability,
                        "target_ability": target_ability,
                        "source_proficient": source_proficient,
                        "target_proficient": target_proficient,
                        "source_advantage": source_advantage,
                        "source_disadvantage": source_disadvantage,
                        "target_advantage": target_advantage,
                        "target_disadvantage": target_disadvantage,
                        "source_ref": exact_ref,
                    },
                },
                "current_location_key": location_key,
                "expected_state_version": int((progress_before or {}).get("state_version", 0) or 0),
                "idempotency_key": _mutation_key(
                    run_id,
                    "scene-progress",
                    contest_identity,
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
    recovered = _recover_committed_contest(
        campaign,
        progress_matches=progress_matches,
        source_actor_id=source_actor_id,
        target_actor_id=target_actor_id,
    )
    if recovered is None:
        settled = await client.domain(
            "character_check",
            {
                "campaign_id": campaign_id,
                "action": "contest",
                "payload": {
                    "source_actor_id": source_actor_id,
                    "target_actor_id": target_actor_id,
                    "source_ability": source_ability,
                    "target_ability": target_ability,
                    "source_proficient": source_proficient,
                    "target_proficient": target_proficient,
                    "source_advantage": source_advantage,
                    "source_disadvantage": source_disadvantage,
                    "target_advantage": target_advantage,
                    "target_disadvantage": target_disadvantage,
                },
                "branch_id": str(branch["id"]),
                "expected_revision": campaign["revision"],
                "idempotency_key": _mutation_key(
                    run_id,
                    "character-contest",
                    contest_identity,
                ),
            },
        )
        contest_result = _committed_contest_result(settled)
    else:
        contest_result = recovered
    outcome = str(contest_result["outcome"])
    proposition = {
        "source_wins": source_win_knowledge.strip(),
        "target_wins": target_win_knowledge.strip(),
        "tie_no_change": tie_knowledge.strip(),
    }.get(outcome, "")
    if not proposition:
        proposition = (
            f"{source_actor['name']} and {target_actor['name']} resolved the "
            f"{source_ability.title()} versus {target_ability.title()} contest: "
            f"{outcome.replace('_', ' ')}."
        )
    recipients = list(dict.fromkeys([source_actor_id, target_actor_id, *knowledge_actor_ids]))
    campaign = await _campaign(client, campaign_id)
    continuity_payload = {
        "event": {
            "summary": (
                f"{source_actor['name']} and {target_actor['name']} resolved a "
                f"source-cited ability contest at {location_key}: "
                f"{outcome.replace('_', ' ')}."
            ),
            "event_type": "ability_contest",
            "audience_scope": "party",
            "payload": {
                "scene_id": scene_id,
                "location_key": location_key,
                "occurrence_id": contest_identity,
                "source_actor_id": source_actor_id,
                "target_actor_id": target_actor_id,
                "source_ability": source_ability,
                "target_ability": target_ability,
                "source_advantage": source_advantage,
                "source_disadvantage": source_disadvantage,
                "target_advantage": target_advantage,
                "target_disadvantage": target_disadvantage,
                "outcome": outcome,
                "winner_actor_id": contest_result.get("winner_actor_id", ""),
                "source_excerpt": source_excerpt,
                "source_ref": exact_ref,
            },
        },
        "actor_knowledge": [
            {
                "actor_id": recipient,
                "knowledge_key": _contest_knowledge_key(
                    run_id,
                    contest_identity,
                ),
                "proposition": proposition,
                "disclosure_scope": "owner",
            }
            for recipient in recipients
        ],
        "branch_id": str(branch["id"]),
    }
    if not defer_checkpoint:
        continuity_payload["snapshot"] = {
            "label": f"Full playthrough ability contest at {location_key}"
        }
    committed = await _commit_roll_continuity(
        client,
        campaign_id=campaign_id,
        payload=continuity_payload,
        expected_revision=campaign["revision"],
        idempotency_key=_mutation_key(
            run_id,
            "continuity",
            contest_identity,
        ),
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"resolve-contest-sync:{contest_identity}",
    )
    return {
        "scene": {
            "scene_id": scene_id,
            "source_scene_id": cited_scene_id,
            "location_key": location_key,
            "source_ref": exact_ref,
        },
        "source_actor": {
            "id": source_actor_id,
            "name": source_actor["name"],
        },
        "target_actor": {
            "id": target_actor_id,
            "name": target_actor["name"],
        },
        "occurrence_id": contest_identity,
        "progress": progress,
        "contest": contest_result,
        "contest_recovered": recovered is not None,
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
    occurrence_id: str,
    event_type: str,
    summary: str,
    knowledge: str,
    knowledge_actor_ids: list[str],
    progress_percent: int | None,
    audience_scope: str = "party",
    source_scene_id: str = "",
    defer_checkpoint: bool = False,
    knowledge_cause: str = "witnessed",
    agent_ruling: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event_identity = _occurrence_identity(occurrence_id, "record-event")
    if not all((scene_id, location_key, event_type, summary)):
        raise ValueError("record-event requires scene, location, event type, and summary")
    normalized_agent_ruling = _settled_event_agent_ruling(agent_ruling)
    has_source_ref = source_ref is not None
    has_source_excerpt = bool(source_excerpt.strip())
    if has_source_ref != has_source_excerpt:
        raise ValueError("record-event source evidence requires both exact source ref and excerpt")
    if not has_source_ref and normalized_agent_ruling is None:
        raise ValueError("record-event requires exact source evidence or a settled Agent ruling")
    if not has_source_ref and source_scene_id.strip():
        raise ValueError("record-event source scene requires exact source evidence")
    if bool(knowledge.strip()) != bool(knowledge_actor_ids):
        raise ValueError(
            "record-event knowledge text and knowledge actor ids must be provided together"
        )
    if progress_percent is not None and not 0 <= progress_percent <= 100:
        raise ValueError("record-event progress percent must be between 0 and 100")
    if audience_scope not in {"party", "dm"}:
        raise ValueError("record-event audience scope must be party or dm")
    if knowledge_cause not in {"witnessed", "told_by"}:
        raise ValueError("record-event knowledge cause must be witnessed or told_by")
    occurrence_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    cited_scene_id = source_scene_id.strip() or scene_id
    source_scene = occurrence_scene
    if has_source_ref and cited_scene_id != scene_id:
        source_scene = await client.domain(
            "module_query",
            {
                "campaign_id": campaign_id,
                "view": "scene",
                "payload": {"scene_id": cited_scene_id},
            },
        )
    exact_ref = (
        await _validate_source_ref(client, source_scene, source_ref, excerpt=source_excerpt)
        if has_source_ref
        else None
    )
    location_keys = {str(item.get("key") or "") for item in _scene_locations(occurrence_scene)}
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
    event_key = _token(f"{run_id}:{event_identity}", length=24)
    event_record = {
        "occurrence_id": event_identity,
        "event_type": event_type,
        "summary": summary.strip(),
        "source_ref": exact_ref,
        **(
            {"agent_ruling": normalized_agent_ruling} if normalized_agent_ruling is not None else {}
        ),
    }
    existing_event = events.get(event_key)
    if existing_event is not None and existing_event != event_record:
        raise RuntimeError("record-event occurrence already exists with different evidence")
    progress_recovered = existing_event == event_record
    if progress_recovered:
        progress = deepcopy(progress_before or {})
    else:
        events[event_key] = event_record
        state["full_playthrough_events"] = events
        progress = await client.domain(
            "module_set_progress",
            {
                "campaign_id": campaign_id,
                "scene_id": scene_id,
                "status": _scene_progress_write_status(
                    progress_before,
                    completed=progress_percent == 100,
                ),
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
    recovered_continuity = None
    if progress_recovered:
        event_rows = await client.domain(
            "campaign_event",
            {
                "campaign_id": campaign_id,
                "action": "list",
                "payload": {"limit": 1000, "branch_id": str(branch["id"])},
            },
        )
        recovered_continuity = next(
            (
                item
                for item in event_rows
                if str(item.get("event_type") or "") == event_type
                and str(item.get("summary") or "") == summary.strip()
                and str(dict(item.get("payload") or {}).get("occurrence_id") or "")
                == event_identity
                and str(dict(item.get("payload") or {}).get("scene_id") or "") == scene_id
                and str(dict(item.get("payload") or {}).get("location_key") or "") == location_key
                and dict(item.get("payload") or {}).get("source_ref") == exact_ref
                and dict(item.get("payload") or {}).get("agent_ruling") == normalized_agent_ruling
            ),
            None,
        )
        if recovered_continuity is not None:
            current_manifest = await _manifest_get(client, campaign_id)
            return {
                "scene": {
                    "scene_id": scene_id,
                    "source_scene_id": cited_scene_id,
                    "location_key": location_key,
                    "source_ref": exact_ref,
                    "agent_ruling": normalized_agent_ruling,
                },
                "progress": progress,
                "occurrence_id": event_identity,
                "continuity": {
                    "event": recovered_continuity,
                    "recovered": True,
                },
                "knowledge_actor_ids": list(dict.fromkeys(knowledge_actor_ids)),
                "sync": current_manifest,
                "recovered": True,
            }
    campaign = await _campaign(client, campaign_id)
    continuity_payload = {
        "event": {
            "summary": summary.strip(),
            "event_type": event_type,
            "audience_scope": audience_scope,
            "payload": {
                "scene_id": scene_id,
                "source_scene_id": cited_scene_id,
                "location_key": location_key,
                "occurrence_id": event_identity,
                "source_excerpt": source_excerpt.strip() if has_source_ref else "",
                "source_ref": exact_ref,
                "agent_ruling": normalized_agent_ruling,
            },
        },
        "actor_knowledge": [
            {
                "actor_id": actor_id,
                "knowledge_key": (f"playthrough.{_token(run_id)}.{_token(event_identity)}"),
                "proposition": knowledge.strip(),
                "cause": knowledge_cause,
                "disclosure_scope": "owner",
            }
            for actor_id in list(dict.fromkeys(knowledge_actor_ids))
        ],
        "branch_id": str(branch["id"]),
    }
    if not defer_checkpoint:
        continuity_payload["snapshot"] = {"label": f"Full playthrough event: {summary.strip()}"}
    committed = await client.domain(
        "memory_change",
        {
            "campaign_id": campaign_id,
            "action": "commit",
            "payload": continuity_payload,
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
            "source_scene_id": cited_scene_id,
            "location_key": location_key,
            "source_ref": exact_ref,
            "agent_ruling": normalized_agent_ruling,
        },
        "progress": progress,
        "occurrence_id": event_identity,
        "continuity": committed,
        "knowledge_actor_ids": list(dict.fromkeys(knowledge_actor_ids)),
        "sync": synced,
        "recovered": progress_recovered,
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


def _merge_manifest_objects(
    existing: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Recursively apply an additive manifest object patch.

    Nested objects retain siblings that are absent from the patch. Lists and
    scalar values are complete values and replace the existing value.
    """

    merged = deepcopy(existing)
    for key, value in patch.items():
        current = merged.get(key)
        if isinstance(value, dict):
            merged[key] = _merge_manifest_objects(
                current if isinstance(current, dict) else {},
                value,
            )
        else:
            merged[key] = deepcopy(value)
    return merged


async def _prepare_narrative_npc(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    occurrence_id: str,
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
    source_identity: str = "",
    instance_key: str = "",
    identity_agent_ruling: dict[str, Any] | None = None,
    defer_checkpoint: bool = False,
) -> dict[str, Any]:
    npc_identity = _occurrence_identity(occurrence_id, "prepare-narrative-npc")
    normalized_name = name.strip()
    normalized_role = role.strip()
    normalized_summary = summary.strip()
    normalized_source_identity = source_identity.strip() or normalized_name
    normalized_instance_key = instance_key.strip()
    normalized_identity_agent_ruling = _settled_agent_ruling(
        identity_agent_ruling,
        label="narrative NPC identity",
        ruling_kinds=frozenset({"agent_dm_adjudication"}),
        extra_fields=frozenset({"assigned_name", "source_identity", "instance_key"}),
    )
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
    if normalized_identity_agent_ruling is not None:
        if not normalized_instance_key:
            raise ValueError("Agent-named narrative NPCs require a source instance key")
        expected_identity_fields = {
            "assigned_name": normalized_name,
            "source_identity": normalized_source_identity,
            "instance_key": normalized_instance_key,
        }
        mismatched = sorted(
            field
            for field, expected in expected_identity_fields.items()
            if normalized_identity_agent_ruling.get(field) != expected
        )
        if mismatched:
            raise ValueError(
                "narrative NPC identity Agent ruling must match: " + ", ".join(mismatched)
            )
    elif normalized_instance_key and normalized_name != (
        f"{normalized_source_identity} [{normalized_instance_key}]"
    ):
        raise ValueError(
            "anonymous narrative NPC name must equal '<source identity> [<instance key>]'"
        )
    if not normalized_instance_key and normalized_source_identity != normalized_name:
        raise ValueError(
            "narrative NPC source identity may differ from name only with an instance key"
        )

    await client.load()
    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    exact_ref = await _validate_source_ref(client, scene, source_ref, excerpt=source_excerpt)
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
    await client.load()
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
                    **(
                        {
                            "source_identity": normalized_source_identity,
                            "instance_key": normalized_instance_key,
                        }
                        if normalized_instance_key
                        else {}
                    ),
                    **(
                        {"identity_agent_ruling": (normalized_identity_agent_ruling)}
                        if normalized_identity_agent_ruling is not None
                        else {}
                    ),
                },
                "idempotency_key": _mutation_key(
                    run_id,
                    "narrative-npc",
                    npc_identity,
                ),
            },
        )
    )
    actor = dict(created.get("character") or {})
    provenance = dict(created.get("narrative_npc") or {})
    canonical_source_ref = {
        key: deepcopy(exact_ref[key]) for key in EXACT_MODULE_SOURCE_FIELD_ORDER
    }
    if (
        actor.get("campaign_id") != campaign_id
        or actor.get("character_type") != "npc"
        or actor.get("name") != normalized_name
        or provenance.get("combat_eligible") is not False
        or provenance.get("combat_statblock") != "not_imported"
        or dict(provenance.get("source_ref") or {}) != canonical_source_ref
        or (
            normalized_identity_agent_ruling is not None
            and dict(provenance.get("identity_agent_ruling") or {})
            != normalized_identity_agent_ruling
        )
    ):
        raise RuntimeError("source-bound narrative NPC creation verification failed")
    status_tags = set(
        dict(dict(actor.get("sheet") or {}).get("adventure_state") or {}).get("status_tags") or []
    )
    required_status_tags = {"narrative_only", "source_bound"}
    if normalized_identity_agent_ruling is not None:
        required_status_tags.add("agent_named_source_instance")
    if not required_status_tags.issubset(status_tags):
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
    await client.load()
    verified_actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": str(actor["id"])}},
    )
    if verified_actor.get("campaign_id") != campaign_id:
        raise RuntimeError("narrative NPC disappeared after returning to play")

    current_manifest = await _manifest_get(client, campaign_id)
    manifest = deepcopy(dict(current_manifest["manifest"]))
    existing_manifest_npc = next(
        (
            item
            for item in list(manifest.get("npcs") or [])
            if str(item.get("actor_id") or "") == str(actor["id"])
        ),
        None,
    )
    if (
        existing_manifest_npc is not None
        and str(existing_manifest_npc.get("name") or "") != normalized_name
    ):
        raise RuntimeError("registered narrative NPC manifest name does not match its actor")
    source_note = (
        "Narrative-only source-bound actor; combat_statblock=not_imported; "
        f"module={exact_ref['module_id']}; scene={exact_ref['scene_id']}; "
        f"chunk={exact_ref['chunk_id']}; pages={exact_ref['page_start']}-"
        f"{exact_ref['page_end']}; sha256={exact_ref['content_sha256']}."
    )
    if existing_manifest_npc is None:
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
    else:
        validate_playthrough_manifest(manifest)
        replaced = current_manifest
    checkpoint = (
        None
        if defer_checkpoint or existing_manifest_npc is not None
        else await _checkpoint(
            client,
            campaign_id=campaign_id,
            run_id=run_id,
            label=f"Narrative NPC prepared: {normalized_name}",
            checkpoint_id=f"narrative-npc:{actor['id']}",
        )
    )
    return {
        "occurrence_id": npc_identity,
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
    source_scene_id: str = "",
    defer_checkpoint: bool = False,
    knowledge_cause: str = "witnessed",
    agent_ruling: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not all(
        (
            outcome_id.strip(),
            scene_id,
            location_key,
            event_type,
            summary.strip(),
        )
    ):
        raise ValueError(
            "record-outcome requires outcome id, scene, location, event type, and summary"
        )
    normalized_agent_ruling = _settled_event_agent_ruling(agent_ruling)
    has_source_ref = source_ref is not None
    has_source_excerpt = bool(source_excerpt.strip())
    if has_source_ref != has_source_excerpt:
        raise ValueError(
            "record-outcome source evidence requires both exact source ref and excerpt"
        )
    if not has_source_ref and normalized_agent_ruling is None:
        raise ValueError("record-outcome requires exact source evidence or a settled Agent ruling")
    if not has_source_ref and source_scene_id.strip():
        raise ValueError("record-outcome source scene requires exact source evidence")
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
    if knowledge_cause not in {"witnessed", "told_by"}:
        raise ValueError("record-outcome knowledge cause must be witnessed or told_by")
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
        action = str(fact.get("action", "upsert"))
        if action not in FACT_KEY_WRITE_ACTIONS:
            raise ValueError(
                f"fact-json[{index}].action must be add or upsert; "
                "the public continuity commit does not support deletion or retraction"
            )
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
    manifest["world_state"] = _merge_manifest_objects(
        dict(manifest.get("world_state") or {}),
        world_state,
    )
    if objective.strip():
        manifest["current"]["objective"] = objective.strip()
    manifest = validate_playthrough_manifest(manifest)

    await client.load()
    occurrence_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    cited_scene_id = source_scene_id.strip() or scene_id
    source_scene = occurrence_scene
    if has_source_ref and cited_scene_id != scene_id:
        source_scene = await client.domain(
            "module_query",
            {
                "campaign_id": campaign_id,
                "view": "scene",
                "payload": {"scene_id": cited_scene_id},
            },
        )
    exact_ref = (
        await _validate_source_ref(client, source_scene, source_ref, excerpt=source_excerpt)
        if has_source_ref
        else None
    )
    if location_key not in {
        str(item.get("key") or "") for item in _scene_locations(occurrence_scene)
    }:
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

    current_fact_rows = _facade_value(
        await client.domain(
            "memory_query",
            {
                "campaign_id": campaign_id,
                "view": "list",
                "payload": {"include_inactive": False},
            },
        )
    )
    if not isinstance(current_fact_rows, list) or any(
        not isinstance(item, dict) for item in current_fact_rows
    ):
        raise RuntimeError("memory_query returned an invalid fact collection")
    current_facts = {
        str(item.get("fact_key") or ""): item
        for item in current_fact_rows
        if str(item.get("fact_key") or "")
    }
    for fact in normalized_facts:
        if str(fact.get("action", "upsert")) != "upsert":
            continue
        current_fact = current_facts.get(str(fact["fact_key"]))
        if current_fact is None:
            continue
        current_revision_id = str(current_fact.get("revision_id") or "")
        if not current_revision_id:
            raise RuntimeError("memory_query returned an existing fact without revision_id")
        supplied_revision_id = str(fact.get("expected_revision_id") or "")
        if supplied_revision_id and supplied_revision_id != current_revision_id:
            raise ValueError(
                "fact expected_revision_id is stale: "
                f"{fact['fact_key']} expected {supplied_revision_id}, "
                f"current {current_revision_id}"
            )
        fact["expected_revision_id"] = current_revision_id

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
        **(
            {"agent_ruling": normalized_agent_ruling} if normalized_agent_ruling is not None else {}
        ),
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
                "status": _scene_progress_write_status(
                    progress_before,
                    completed=progress_percent == 100,
                ),
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
    continuity_payload = {
        "event": {
            "summary": summary.strip(),
            "event_type": event_type,
            "audience_scope": audience_scope,
            "payload": {
                "outcome_id": outcome_id.strip(),
                "scene_id": scene_id,
                "source_scene_id": cited_scene_id,
                "location_key": location_key,
                "source_excerpt": (source_excerpt.strip() if has_source_ref else ""),
                "source_ref": exact_ref,
                "agent_ruling": normalized_agent_ruling,
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
                "cause": knowledge_cause,
                "disclosure_scope": "owner",
            }
            for actor_id in recipients
        ],
        "branch_id": str(branch["id"]),
    }
    recovered_continuity = None
    recovered_checkpoint = None
    if existing_outcome is not None:
        event_rows = await client.domain(
            "campaign_event",
            {
                "campaign_id": campaign_id,
                "action": "list",
                "payload": {"limit": 1000, "branch_id": str(branch["id"])},
            },
        )
        recovered_continuity = next(
            (
                item
                for item in event_rows
                if str(item.get("event_type") or "") == event_type
                and str(item.get("summary") or "") == summary.strip()
                and str(dict(item.get("payload") or {}).get("outcome_id") or "")
                == outcome_id.strip()
                and str(dict(item.get("payload") or {}).get("scene_id") or "") == scene_id
                and str(dict(item.get("payload") or {}).get("location_key") or "") == location_key
                and dict(item.get("payload") or {}).get("source_ref") == exact_ref
                and dict(item.get("payload") or {}).get("agent_ruling") == normalized_agent_ruling
            ),
            None,
        )
        if recovered_continuity is not None and not defer_checkpoint:
            snapshots = await client.domain(
                "snapshot_query",
                {"campaign_id": campaign_id, "view": "list"},
            )
            recovered_checkpoint = next(
                (
                    item
                    for item in snapshots
                    if str(item.get("label") or "")
                    == f"Full playthrough outcome: {outcome_id.strip()}"
                ),
                None,
            )
        if recovered_continuity is not None and (
            defer_checkpoint or recovered_checkpoint is not None
        ):
            return {
                "outcome_id": outcome_id.strip(),
                "scene": {
                    "scene_id": scene_id,
                    "source_scene_id": cited_scene_id,
                    "location_key": location_key,
                    "source_ref": exact_ref,
                    "agent_ruling": normalized_agent_ruling,
                },
                "progress": progress,
                "continuity": {
                    "event": recovered_continuity,
                    "recovered": True,
                },
                "knowledge_actor_ids": recipients,
                "manifest_replace": current_manifest,
                "checkpoint": recovered_checkpoint,
                "recovered": True,
            }
    if recovered_continuity is None:
        campaign = await _campaign(client, campaign_id)
        committed = await client.domain(
            "memory_change",
            {
                "campaign_id": campaign_id,
                "action": "commit",
                "payload": continuity_payload,
                "expected_revision": campaign["revision"],
                "idempotency_key": _mutation_key(run_id, "continuity-outcome", outcome_id),
            },
        )
    else:
        committed = {"event": recovered_continuity, "recovered": True}

    replaced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="replace",
        run_id=run_id,
        identity=f"record-outcome-replace:{outcome_id}",
        payload={"manifest": manifest},
    )
    checkpoint = (
        None
        if defer_checkpoint
        else await _checkpoint(
            client,
            campaign_id=campaign_id,
            run_id=run_id,
            label=f"Full playthrough outcome: {outcome_id.strip()}",
            checkpoint_id=f"outcome:{outcome_id.strip()}",
        )
    )
    return {
        "outcome_id": outcome_id.strip(),
        "scene": {
            "scene_id": scene_id,
            "source_scene_id": cited_scene_id,
            "location_key": location_key,
            "source_ref": exact_ref,
            "agent_ruling": normalized_agent_ruling,
        },
        "progress": progress,
        "continuity": committed,
        "knowledge_actor_ids": recipients,
        "manifest_replace": replaced,
        "checkpoint": checkpoint,
        "recovered": existing_outcome is not None,
    }


def _dice_result(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value.get("result") or value)
    total = result.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
        raise RuntimeError("server dice roll did not return a positive integer total")
    return result


def _normalize_roll_modifiers(
    modifiers: list[dict[str, Any]],
    *,
    expression: str,
) -> list[dict[str, Any]]:
    if not modifiers:
        return []
    normalized: list[dict[str, Any]] = []
    modifier_ids: set[str] = set()
    state_keys: set[str] = set()
    for index, raw in enumerate(modifiers):
        if not isinstance(raw, dict):
            raise ValueError(f"roll-modifier-json[{index}] must be an object")
        modifier_id = str(raw.get("modifier_id") or "").strip()
        state_key = str(raw.get("state_key") or "").strip()
        basis = str(raw.get("basis") or "").strip()
        kind = str(raw.get("kind") or "").strip()
        lifetime = str(raw.get("lifetime") or "").strip()
        value = raw.get("value")
        if not modifier_id or not state_key or not basis:
            raise ValueError(
                f"roll-modifier-json[{index}] requires modifier_id, state_key, and basis"
            )
        if kind not in {"cumulative", "limited_use", "static", "penalty"}:
            raise ValueError(
                f"roll-modifier-json[{index}] kind must be cumulative, limited_use, "
                "static, or penalty"
            )
        if lifetime not in {"roll", "scene", "until_consumed", "persistent"}:
            raise ValueError(
                f"roll-modifier-json[{index}] lifetime must be roll, scene, "
                "until_consumed, or persistent"
            )
        if isinstance(value, bool) or not isinstance(value, int) or value == 0:
            raise ValueError(f"roll-modifier-json[{index}] value must be a nonzero integer")
        if modifier_id in modifier_ids:
            raise ValueError(f"duplicate roll modifier id: {modifier_id}")
        if state_key in state_keys:
            raise ValueError(
                f"independent roll modifiers must not share one state_key: {state_key}"
            )
        modifier_ids.add(modifier_id)
        state_keys.add(state_key)
        normalized.append(
            {
                "modifier_id": modifier_id,
                "value": value,
                "kind": kind,
                "lifetime": lifetime,
                "state_key": state_key,
                "basis": basis,
            }
        )
    flat_match = re.search(r"([+-]\d+)\s*$", expression)
    expression_modifier = int(flat_match.group(1)) if flat_match else 0
    ledger_total = sum(item["value"] for item in normalized)
    if ledger_total != expression_modifier:
        raise ValueError(
            "roll modifier ledger total does not match the expression's trailing "
            f"modifier: ledger {ledger_total}, expression {expression_modifier}"
        )
    return normalized


async def _roll_source_table(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    roll_id: str,
    expression: str,
    reason: str,
    audience_scope: str,
    defer_checkpoint: bool,
    modifiers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized_roll_id = roll_id.strip()
    normalized_expression = expression.strip()
    normalized_reason = reason.strip()
    if not all(
        (
            scene_id,
            location_key,
            source_excerpt,
            normalized_roll_id,
            normalized_expression,
            normalized_reason,
        )
    ):
        raise ValueError(
            "roll-source requires scene, location, excerpt, roll id, expression, and reason"
        )
    if audience_scope not in {"party", "dm"}:
        raise ValueError("roll-source audience scope must be party or dm")
    normalized_modifiers = _normalize_roll_modifiers(
        list(modifiers or []),
        expression=normalized_expression,
    )
    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    exact_ref = await _validate_source_ref(client, scene, source_ref, excerpt=source_excerpt)
    location_keys = {str(item.get("key") or "") for item in _scene_locations(scene)}
    if location_key not in location_keys:
        raise ValueError("roll-source location is not present in the scene atlas")
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    branch_id = str(branch["id"])
    roll_identity = json.dumps(
        {
            "scene_id": scene_id,
            "location_key": location_key,
            "roll_id": normalized_roll_id,
            "expression": normalized_expression,
            "modifiers": normalized_modifiers,
            "source_ref": exact_ref,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    branch_roll_identity = json.dumps(
        {
            "branch_id": branch_id,
            "roll_identity": roll_identity,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    campaign = await _campaign(client, campaign_id)
    rolled = await client.domain(
        "dnd_dice_roll",
        {
            "campaign_id": campaign_id,
            "expression": normalized_expression,
            "branch_id": branch_id,
            "expected_campaign_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id,
                "source-roll",
                roll_identity,
            ),
        },
    )
    roll_result = _dice_result(rolled)
    random_receipt = dict(
        rolled.get("random_stream_receipt") or roll_result.get("random_stream_receipt") or {}
    )
    progress_rows = await client.domain(
        "module_query",
        {"campaign_id": campaign_id, "view": "progress"},
    )
    progress_before = next(
        (item for item in progress_rows if item.get("scene_id") == scene_id),
        None,
    )
    state = deepcopy(dict((progress_before or {}).get("state") or {}))
    rolls = deepcopy(dict(state.get("full_playthrough_rolls") or {}))
    roll_key = _token(f"{run_id}:{branch_roll_identity}", length=24)
    roll_record = {
        "roll_id": normalized_roll_id,
        "expression": normalized_expression,
        "modifiers": normalized_modifiers,
        "reason": normalized_reason,
        "result": roll_result,
        "random_stream_receipt": random_receipt,
        "source_ref": exact_ref,
    }
    existing_roll = rolls.get(roll_key)
    if existing_roll is not None and existing_roll != roll_record:
        raise RuntimeError("stored source roll conflicts with the replayed server receipt")
    if existing_roll == roll_record:
        progress = deepcopy(progress_before or {})
    else:
        rolls[roll_key] = roll_record
        state["full_playthrough_rolls"] = rolls
        progress = await client.domain(
            "module_set_progress",
            {
                "campaign_id": campaign_id,
                "scene_id": scene_id,
                "status": str((progress_before or {}).get("status") or "active"),
                "progress": _scene_progress_percent(progress_before),
                "state": state,
                "current_location_key": location_key,
                "expected_state_version": int((progress_before or {}).get("state_version", 0) or 0),
                "idempotency_key": _mutation_key(
                    run_id,
                    "source-roll-progress",
                    branch_roll_identity,
                ),
            },
        )
    event_summary = (
        f"{normalized_reason} Server roll {normalized_expression} = {roll_result['total']}."
    )
    campaign = await _campaign(client, campaign_id)
    continuity_payload: dict[str, Any] = {
        "event": {
            "summary": event_summary,
            "event_type": "source_table_roll",
            "audience_scope": audience_scope,
            "payload": {
                "scene_id": scene_id,
                "location_key": location_key,
                "roll_id": normalized_roll_id,
                "expression": normalized_expression,
                "modifiers": normalized_modifiers,
                "result": roll_result,
                "random_stream_receipt": random_receipt,
                "source_excerpt": source_excerpt,
                "source_ref": exact_ref,
            },
        },
        "branch_id": branch_id,
    }
    if not defer_checkpoint:
        continuity_payload["snapshot"] = {
            "label": f"Full playthrough source roll: {normalized_roll_id}"
        }
    committed = await client.domain(
        "memory_change",
        {
            "campaign_id": campaign_id,
            "action": "commit",
            "payload": continuity_payload,
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id,
                "source-roll-continuity",
                branch_roll_identity,
            ),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"source-roll-sync:{branch_roll_identity}",
    )
    return {
        "scene": {
            "scene_id": scene_id,
            "location_key": location_key,
            "source_ref": exact_ref,
        },
        "roll_id": normalized_roll_id,
        "roll": roll_result,
        "random_stream_receipt": random_receipt,
        "progress": progress,
        "continuity": committed,
        "sync": synced,
    }


async def _roll_source_sequence(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    roll_id: str,
    expression: str,
    reason: str,
    audience_scope: str,
    count: int,
    defer_checkpoint: bool = False,
    modifiers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if count < 2 or count > 1000:
        raise ValueError("roll-source sequence count must be between 2 and 1000")
    results: list[dict[str, Any]] = []
    for index in range(1, count + 1):
        item_roll_id = f"{roll_id}-{index:03d}"
        item = await _roll_source_table(
            client,
            campaign_id=campaign_id,
            run_id=run_id,
            scene_id=scene_id,
            location_key=location_key,
            source_excerpt=source_excerpt,
            source_ref=source_ref,
            roll_id=item_roll_id,
            expression=expression,
            reason=f"{reason} ({index}/{count})",
            audience_scope=audience_scope,
            defer_checkpoint=defer_checkpoint or index < count,
            modifiers=modifiers,
        )
        results.append(
            {
                "roll_id": item_roll_id,
                "roll": item["roll"],
                "random_stream_receipt": item["random_stream_receipt"],
                "progress_state_version": item["progress"].get("state_version"),
                "continuity_event": item["continuity"].get("event"),
                "snapshot": item["continuity"].get("snapshot"),
                "campaign_revision": item["sync"].get("campaign_revision"),
            }
        )
    return {
        "scene": {
            "scene_id": scene_id,
            "location_key": location_key,
            "source_ref": results[0]["roll"].get("source_ref") or source_ref,
        },
        "roll_count": count,
        "rolls": results,
        "checkpoint_deferred": defer_checkpoint,
    }


async def _apply_source_damage(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    scene_id: str,
    source_scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    actor_id: str,
    damage_event_id: str,
    expression: str,
    damage_type: str,
    reason: str,
    half_damage: bool,
    knock_prone: bool,
    knowledge_actor_ids: list[str],
    defer_checkpoint: bool = False,
) -> dict[str, Any]:
    normalized_event_id = damage_event_id.strip()
    if not all(
        (
            scene_id,
            location_key,
            source_excerpt,
            actor_id,
            normalized_event_id,
            expression,
            damage_type,
            reason,
        )
    ):
        raise ValueError(
            "apply-damage requires scene, location, excerpt, actor, damage event id, "
            "expression, damage type, and reason"
        )
    if len(normalized_event_id) > 200:
        raise ValueError("damage event id must not exceed 200 characters")
    current_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    cited_scene_id = source_scene_id or scene_id
    cited_scene = (
        current_scene
        if cited_scene_id == scene_id
        else await client.domain(
            "module_query",
            {
                "campaign_id": campaign_id,
                "view": "scene",
                "payload": {"scene_id": cited_scene_id},
            },
        )
    )
    exact_ref = await _validate_source_ref(client, cited_scene, source_ref, excerpt=source_excerpt)
    location_keys = {str(item.get("key") or "") for item in _scene_locations(current_scene)}
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
    normalized_expression = expression.strip()
    fixed_damage = re.fullmatch(r"\+?(\d+)", normalized_expression)
    if fixed_damage is not None:
        fixed_amount = int(fixed_damage.group(1))
        rolled = {
            "status": "fixed",
            "result": {
                "expression": normalized_expression,
                "total": fixed_amount,
                "rolls": [],
                "random_draws": 0,
                "resolution": "fixed",
            },
        }
    else:
        rolled = await client.domain(
            "dnd_dice_roll",
            {
                "campaign_id": campaign_id,
                "expression": normalized_expression,
                "branch_id": str(branch["id"]),
                "expected_campaign_revision": campaign["revision"],
                "idempotency_key": _mutation_key(
                    run_id,
                    "source-damage-roll",
                    normalized_event_id,
                ),
            },
        )
    roll_result = _dice_result(rolled)
    rolled_amount = int(roll_result["total"])
    amount = damage_amount_after_reduction(
        rolled_amount,
        "half" if half_damage else "full",
    )
    damaged = await client.domain(
        "character_state_change",
        {
            "character_id": actor_id,
            "action": "damage",
            "payload": {
                "parts": [{"amount": amount, "damage_type": damage_type}],
            },
            "expected_revision": actor["revision"],
            "idempotency_key": _mutation_key(run_id, "source-damage", normalized_event_id),
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
                    run_id, "source-damage-prone", normalized_event_id
                ),
            },
        )
        character_after = dict(prone_result["character"])
    recipients = list(dict.fromkeys([actor_id, *knowledge_actor_ids]))
    campaign = await _campaign(client, campaign_id)
    checkpoint_deferred = bool(defer_checkpoint and hp > 0)
    continuity_payload: dict[str, Any] = {
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
                "source_scene_id": cited_scene_id,
                "location_key": location_key,
                "damage_event_id": normalized_event_id,
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
                    f"{_token(actor_id)}.environmental_damage."
                    f"{_token(normalized_event_id)}"
                ),
                "proposition": (
                    f"{actor['name']} took {amount} {damage_type} damage from {reason.strip()}."
                ),
                "disclosure_scope": "owner",
            }
            for recipient in recipients
        ],
        "branch_id": str(branch["id"]),
    }
    if not checkpoint_deferred:
        continuity_payload["snapshot"] = {
            "label": (f"Full playthrough environmental damage: {actor['name']} at {location_key}")
        }
    committed = await client.domain(
        "memory_change",
        {
            "campaign_id": campaign_id,
            "action": "commit",
            "payload": continuity_payload,
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id, "source-damage-continuity", normalized_event_id
            ),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"source-damage-sync:{normalized_event_id}",
    )
    return {
        "scene": {
            "scene_id": scene_id,
            "source_scene_id": cited_scene_id,
            "location_key": location_key,
            "source_ref": exact_ref,
        },
        "actor": {"id": actor_id, "name": actor["name"]},
        "damage_event_id": normalized_event_id,
        "roll": rolled,
        "damage": damaged,
        "prone": prone_result,
        "character": character_after,
        "knowledge_actor_ids": recipients,
        "checkpoint_deferred": checkpoint_deferred,
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
    occurrence_id: str,
    actor_id: str,
    knowledge_actor_ids: list[str],
    reason: str = "",
    defer_checkpoint: bool = False,
) -> dict[str, Any]:
    stand_identity = _occurrence_identity(occurrence_id, "stand-up")
    if not all((scene_id, location_key, actor_id, reason.strip())):
        raise ValueError("stand-up requires scene, location, actor, and reason")
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
        await _validate_source_ref(client, scene, source_ref, excerpt=source_excerpt)
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
            "idempotency_key": _mutation_key(run_id, "source-event-stand", stand_identity),
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
    event_summary = reason.strip()
    knowledge = reason.strip()
    campaign = await _campaign(client, campaign_id)
    continuity_payload = {
        "event": {
            "summary": event_summary,
            "event_type": "stand",
            "audience_scope": "party",
            "payload": {
                "scene_id": scene_id,
                "location_key": location_key,
                "occurrence_id": stand_identity,
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
                "knowledge_key": (f"playthrough.{_token(run_id)}.stand.{_token(stand_identity)}"),
                "proposition": knowledge,
                "disclosure_scope": "owner",
            }
            for recipient in recipients
        ],
        "branch_id": str(branch["id"]),
    }
    if not defer_checkpoint:
        continuity_payload["snapshot"] = {
            "label": f"Full playthrough stand: {actor['name']} at {location_key}"
        }
    committed = await client.domain(
        "memory_change",
        {
            "campaign_id": campaign_id,
            "action": "commit",
            "payload": continuity_payload,
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id, "source-event-stand-continuity", stand_identity
            ),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"source-event-stand-sync:{stand_identity}",
    )
    return {
        "scene": {
            "scene_id": scene_id,
            "location_key": location_key,
            "source_ref": exact_ref,
        },
        "actor": {"id": actor_id, "name": actor["name"]},
        "occurrence_id": stand_identity,
        "stand": stood,
        "knowledge_actor_ids": recipients,
        "continuity": committed,
        "sync": synced,
    }


async def _initialize_source_state(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    scene_id: str,
    source_scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    occurrence_id: str,
    actor_id: str,
    state: str,
    reason: str,
    knowledge_actor_ids: list[str],
    defer_checkpoint: bool,
) -> dict[str, Any]:
    identity = _occurrence_identity(occurrence_id, "initialize-source-state")
    if not all(
        (
            scene_id,
            location_key,
            source_excerpt,
            actor_id,
            state,
            reason.strip(),
        )
    ):
        raise ValueError(
            "initialize-source-state requires scene, location, source, actor, state, and reason"
        )
    current_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    cited_scene_id = source_scene_id or scene_id
    cited_scene = (
        current_scene
        if cited_scene_id == scene_id
        else await client.domain(
            "module_query",
            {
                "campaign_id": campaign_id,
                "view": "scene",
                "payload": {"scene_id": cited_scene_id},
            },
        )
    )
    exact_ref = await _validate_source_ref(client, cited_scene, source_ref, excerpt=source_excerpt)
    if location_key not in {str(item.get("key") or "") for item in _scene_locations(current_scene)}:
        raise ValueError("source-state location is not present in the current scene atlas")
    actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": actor_id}},
    )
    if actor.get("campaign_id") != campaign_id:
        raise ValueError("source-state actor does not belong to the campaign")
    initialized = await client.domain(
        "character_state_change",
        {
            "character_id": actor_id,
            "action": "source_state",
            "payload": {
                "state": state,
                "source_ref": f"module-chunk:{exact_ref['chunk_id']}",
                "reason": reason.strip(),
            },
            "expected_revision": actor["revision"],
            "idempotency_key": _mutation_key(run_id, "source-state", identity),
        },
    )
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    recipients = list(dict.fromkeys(knowledge_actor_ids))
    campaign = await _campaign(client, campaign_id)
    continuity_payload: dict[str, Any] = {
        "event": {
            "summary": reason.strip(),
            "event_type": "source_state_initialized",
            "audience_scope": "dm",
            "payload": {
                "scene_id": scene_id,
                "source_scene_id": cited_scene_id,
                "location_key": location_key,
                "occurrence_id": identity,
                "actor_id": actor_id,
                "state": state,
                "source_excerpt": source_excerpt,
                "source_ref": exact_ref,
            },
        },
        "actor_knowledge": [
            {
                "actor_id": recipient,
                "knowledge_key": (f"playthrough.{_token(run_id)}.source_state.{_token(identity)}"),
                "proposition": reason.strip(),
                "disclosure_scope": "owner",
            }
            for recipient in recipients
        ],
        "branch_id": str(branch["id"]),
    }
    if not defer_checkpoint:
        continuity_payload["snapshot"] = {
            "label": f"Full playthrough source state: {actor['name']} at {location_key}"
        }
    committed = await client.domain(
        "memory_change",
        {
            "campaign_id": campaign_id,
            "action": "commit",
            "payload": continuity_payload,
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(run_id, "source-state-continuity", identity),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"source-state-sync:{identity}",
    )
    return {
        "scene": {
            "scene_id": scene_id,
            "source_scene_id": cited_scene_id,
            "location_key": location_key,
            "source_ref": exact_ref,
        },
        "actor": {"id": actor_id, "name": actor["name"]},
        "occurrence_id": identity,
        "state": initialized,
        "knowledge_actor_ids": recipients,
        "continuity": committed,
        "sync": synced,
    }


async def _revive_character(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    scene_id: str,
    source_scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    occurrence_id: str,
    actor_id: str,
    source_actor_id: str,
    elapsed_days: int,
    soul_willing: bool,
    body_intact: bool,
    reason: str,
) -> dict[str, Any]:
    identity = _occurrence_identity(occurrence_id, "revive-character")
    if not all((scene_id, location_key, source_excerpt, actor_id, reason.strip())):
        raise ValueError("revive-character requires scene, location, source, actor, and reason")
    current_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    cited_scene_id = source_scene_id or scene_id
    cited_scene = (
        current_scene
        if cited_scene_id == scene_id
        else await client.domain(
            "module_query",
            {
                "campaign_id": campaign_id,
                "view": "scene",
                "payload": {"scene_id": cited_scene_id},
            },
        )
    )
    exact_ref = await _validate_source_ref(
        client,
        cited_scene,
        source_ref,
        excerpt=source_excerpt,
    )
    if location_key not in {str(item.get("key") or "") for item in _scene_locations(current_scene)}:
        raise ValueError("revival location is not present in the current scene atlas")
    actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": actor_id}},
    )
    if actor.get("campaign_id") != campaign_id:
        raise ValueError("revival actor does not belong to the campaign")
    normalized_source_actor_id = source_actor_id.strip()
    if normalized_source_actor_id:
        provider = await client.domain(
            "character_query",
            {"view": "get", "payload": {"character_id": normalized_source_actor_id}},
        )
        if provider.get("campaign_id") != campaign_id:
            raise ValueError("revival source actor does not belong to the campaign")
    revival_payload: dict[str, Any] = {
        "elapsed_days": elapsed_days,
        "soul_willing": soul_willing,
        "body_intact": body_intact,
        "source_ref": f"module-chunk:{exact_ref['chunk_id']}",
        "reason": reason.strip(),
    }
    if normalized_source_actor_id:
        revival_payload["source_actor_id"] = normalized_source_actor_id
    revived = await client.domain(
        "character_state_change",
        {
            "character_id": actor_id,
            "action": "revive",
            "payload": revival_payload,
            "expected_revision": actor["revision"],
            "idempotency_key": _mutation_key(run_id, "revive-character", identity),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"revive-character-sync:{identity}",
    )
    return {
        "scene": {
            "scene_id": scene_id,
            "source_scene_id": cited_scene_id,
            "location_key": location_key,
            "source_ref": exact_ref,
        },
        "actor": {"id": actor_id, "name": actor["name"]},
        "occurrence_id": identity,
        "revival": revived,
        "sync": synced,
    }


async def _short_rest(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    occurrence_id: str,
    members: list[dict[str, Any]],
    start_clock: dict[str, Any] | None,
    duration_minutes: int,
    reason: str,
    prerequisite_scene_id: str = "",
    prerequisite_outcome_id: str = "",
    prerequisite_actor_ids: list[str] | None = None,
    expected_start_clock: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rest_identity = _occurrence_identity(occurrence_id, "short-rest")
    minimum_duration = minimum_rest_minutes("short_rest")
    if duration_minutes < minimum_duration:
        raise ValueError(f"short-rest requires at least {minimum_duration} minutes")
    if not members or not reason.strip():
        raise ValueError("short-rest requires members and --rest-reason")
    allowed_fields = {
        "actor_id",
        "arcane_recovery",
        "natural_recovery",
        "song_of_rest_source_actor_id",
        "attune_item_id",
        "attunement_prerequisite_confirmed",
        "hit_dice_spends",
        "rest_activity_minutes",
    }
    normalized: list[dict[str, Any]] = []
    for index, member in enumerate(members):
        if not isinstance(member, dict):
            raise ValueError(f"rest-member-json[{index}] must be an object")
        unexpected = set(member) - allowed_fields
        actor_id = str(member.get("actor_id") or "")
        arcane_recovery = member.get("arcane_recovery")
        natural_recovery = member.get("natural_recovery")
        song_of_rest_source_actor_id = (
            str(member.get("song_of_rest_source_actor_id") or "").strip() or None
        )
        attune_item_id = str(member.get("attune_item_id") or "").strip() or None
        attunement_prerequisite_confirmed = member.get("attunement_prerequisite_confirmed")
        hit_dice_spends = member.get("hit_dice_spends")
        rest_activity_minutes = member.get("rest_activity_minutes")
        if (
            unexpected
            or not actor_id
            or (arcane_recovery is not None and not isinstance(arcane_recovery, dict))
            or (natural_recovery is not None and not isinstance(natural_recovery, dict))
            or (
                attunement_prerequisite_confirmed is not None
                and not isinstance(attunement_prerequisite_confirmed, bool)
            )
            or (hit_dice_spends is not None and not isinstance(hit_dice_spends, list))
            or (rest_activity_minutes is not None and not isinstance(rest_activity_minutes, dict))
        ):
            raise ValueError(
                "short-rest members accept actor_id, optional arcane_recovery, "
                "optional natural_recovery, "
                "optional song_of_rest_source_actor_id, "
                "optional attune_item_id with attunement_prerequisite_confirmed, "
                "optional hit_dice_spends and optional rest_activity_minutes only"
            )
        if attune_item_id and attunement_prerequisite_confirmed is not True:
            raise ValueError(
                "short-rest attunement requires attunement_prerequisite_confirmed=true"
            )
        if not attune_item_id and attunement_prerequisite_confirmed is not None:
            raise ValueError("attunement_prerequisite_confirmed requires attune_item_id")
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
                "natural_recovery": deepcopy(natural_recovery or {}),
                "song_of_rest_source_actor_id": song_of_rest_source_actor_id,
                "hit_dice_spends": normalized_spends,
                "rest_activity_minutes": deepcopy(rest_activity_minutes or {}),
                **({"attune_item_id": attune_item_id} if attune_item_id is not None else {}),
                **(
                    {"attunement_prerequisite_confirmed": True}
                    if attune_item_id is not None
                    else {}
                ),
            }
        )
    actor_ids = [item["actor_id"] for item in normalized]
    if len(actor_ids) != len(set(actor_ids)):
        raise ValueError("short-rest member actor ids must be unique")
    song_source_ids = {
        str(item["song_of_rest_source_actor_id"])
        for item in normalized
        if item["song_of_rest_source_actor_id"] is not None
    }
    if song_source_ids - set(actor_ids):
        raise ValueError("every Song of Rest source must participate in the same short rest")
    if any(
        item["song_of_rest_source_actor_id"] is not None and not item["hit_dice_spends"]
        for item in normalized
    ):
        raise ValueError("Song of Rest applies only to members who spend one or more Hit Dice")
    preconditions = await _validate_narrative_preconditions(
        client,
        campaign_id=campaign_id,
        scene_id=prerequisite_scene_id,
        outcome_id=prerequisite_outcome_id,
        actor_ids=prerequisite_actor_ids,
    )
    actors = []
    for actor_id in actor_ids:
        actor = await client.domain(
            "character_query",
            {"view": "get", "payload": {"character_id": actor_id}},
        )
        if actor.get("campaign_id") != campaign_id:
            raise ValueError("every short-rest actor must belong to the campaign")
        actors.append(actor)
    member_by_id = {item["actor_id"]: item for item in normalized}
    for actor in actors:
        member = member_by_id[str(actor["id"])]
        preflight = await client.domain(
            "character_query",
            {
                "view": "rest",
                "payload": {
                    "character_id": str(actor["id"]),
                    "rest_type": "short_rest",
                    "hit_dice_spends": member["hit_dice_spends"],
                    "arcane_recovery": member["arcane_recovery"],
                    "natural_recovery": member["natural_recovery"],
                    "song_of_rest_source_actor_id": member["song_of_rest_source_actor_id"],
                    "attune_item_id": member.get("attune_item_id"),
                    "attunement_prerequisite_confirmed": member.get(
                        "attunement_prerequisite_confirmed"
                    ),
                    "rest_activity_minutes": member["rest_activity_minutes"],
                    "duration_minutes": duration_minutes,
                },
            },
        )
        if preflight.get("ready") is not True:
            raise RuntimeError(
                f"short rest preflight for {actor['id']} has unresolved rule choices"
            )
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    campaign = await _campaign(client, campaign_id)
    required_start_clock = _validate_world_time_precondition(
        campaign,
        expected_start_clock,
    )
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
                "idempotency_key": _mutation_key(run_id, "short-rest-clock-set", rest_identity),
            },
        )
    elif start_clock is not None:
        raise ValueError("short-rest start clock must be omitted after the clock is set")
    campaign = await _campaign(client, campaign_id)
    actor_by_id = {str(actor["id"]): actor for actor in actors}
    party_members: list[dict[str, Any]] = []
    for member in normalized:
        party_member: dict[str, Any] = {
            "character_id": member["actor_id"],
            "expected_revision": actor_by_id[member["actor_id"]]["revision"],
        }
        if member["arcane_recovery"]:
            party_member["arcane_recovery"] = member["arcane_recovery"]
        if member["natural_recovery"]:
            party_member["natural_recovery"] = member["natural_recovery"]
        if member["song_of_rest_source_actor_id"] is not None:
            party_member["song_of_rest_source_actor_id"] = member["song_of_rest_source_actor_id"]
        if member.get("attune_item_id"):
            party_member["attune_item_id"] = member["attune_item_id"]
            party_member["attunement_prerequisite_confirmed"] = True
        if member["hit_dice_spends"]:
            party_member["hit_dice_spends"] = member["hit_dice_spends"]
        if member["rest_activity_minutes"]:
            party_member["rest_activity_minutes"] = member["rest_activity_minutes"]
        party_members.append(party_member)
    party_rest_key = _mutation_key(run_id, "short-rest-party", rest_identity)
    rest_recovered = False
    try:
        rested = await client.domain(
            "campaign_change",
            {
                "campaign_id": campaign_id,
                "action": "party_rest",
                "payload": {
                    "rest_type": "short_rest",
                    "members": party_members,
                    "duration_minutes": duration_minutes,
                },
                "branch_id": str(branch["id"]),
                "expected_revision": campaign["revision"],
                "idempotency_key": party_rest_key,
            },
        )
    except Exception as exc:
        if "idempotency key reused with a different request" not in str(exc):
            raise
        receipt = await client.domain(
            "state_revision",
            {
                "campaign_id": campaign_id,
                "action": "receipt",
                "payload": {"idempotency_key": party_rest_key},
            },
        )
        receipt_branch_id = str(receipt.get("branch_id") or "")
        if receipt_branch_id != str(branch["id"]):
            raise RuntimeError("short-rest recovery receipt is from another branch")
        revision_rows = list(receipt.get("entity_revisions") or [])
        if not all(isinstance(item, dict) for item in revision_rows):
            raise RuntimeError("short-rest recovery receipt has invalid revision evidence")
        revision_by_entity = {
            (str(item.get("entity_type") or ""), str(item.get("entity_id") or "")): item
            for item in revision_rows
        }
        if len(revision_by_entity) != len(revision_rows):
            raise RuntimeError("short-rest recovery receipt repeats revision evidence")
        expected_entities = {("campaign", campaign_id)} | {
            ("character", actor_id) for actor_id in actor_ids
        }
        if not expected_entities.issubset(revision_by_entity) or any(
            entity_type not in {"campaign", "character"}
            for entity_type, _entity_id in revision_by_entity
        ):
            raise RuntimeError("short-rest recovery receipt has unexpected revision evidence")
        campaign_revision_row = revision_by_entity[("campaign", campaign_id)]
        if (
            campaign_revision_row.get("after_revision") != campaign.get("revision")
            or campaign_revision_row.get("before_revision") != campaign.get("revision") - 1
        ):
            raise RuntimeError("short-rest recovery campaign revisions do not match")
        for (entity_type, entity_id), revision_row in revision_by_entity.items():
            if entity_type != "character":
                continue
            current_actor = actor_by_id.get(entity_id)
            if current_actor is None:
                current_actor = await client.domain(
                    "character_query",
                    {"view": "get", "payload": {"character_id": entity_id}},
                )
            current_revision = current_actor.get("revision")
            if (
                current_actor.get("campaign_id") != campaign_id
                or isinstance(current_revision, bool)
                or not isinstance(current_revision, int)
                or revision_row.get("after_revision") != current_revision
                or revision_row.get("before_revision") != current_revision - 1
            ):
                raise RuntimeError("short-rest recovery actor revisions do not match")
        recovery_members = []
        for member in normalized:
            revision_row = revision_by_entity[("character", member["actor_id"])]
            recovery_members.append(
                {
                    "character_id": member["actor_id"],
                    "expected_revision": revision_row["before_revision"],
                    "rest_activity_minutes": member["rest_activity_minutes"],
                    "hit_dice_spends": member["hit_dice_spends"],
                    "arcane_recovery": member["arcane_recovery"],
                    "natural_recovery": member["natural_recovery"],
                    "song_of_rest_source_actor_id": member["song_of_rest_source_actor_id"],
                    "attune_item_id": member.get("attune_item_id"),
                    "attunement_prerequisite_confirmed": (
                        True if member.get("attune_item_id") else None
                    ),
                }
            )
        expected_request_hash = _idempotency_request_hash(
            {
                "members": recovery_members,
                "duration_minutes": duration_minutes,
                "branch_id": receipt_branch_id,
                "rest_type": "short_rest",
            }
        )
        rested = _validate_recovered_short_rest(
            receipt,
            campaign=campaign,
            actors=actors,
            members=normalized,
            duration_minutes=duration_minutes,
            expected_request_hash=expected_request_hash,
        )
        rest_recovered = True
    if (
        rested.get("status") != "committed"
        or rested.get("rest_type") != "short_rest"
        or rested.get("duration_minutes") != duration_minutes
        or rested.get("member_ids") != actor_ids
    ):
        raise RuntimeError("atomic short rest did not settle the requested party")
    campaign = await _campaign(client, campaign_id)
    committed = await client.domain(
        "memory_change",
        {
            "campaign_id": campaign_id,
            "action": "commit",
            "payload": {
                "event": {
                    "summary": reason.strip(),
                    "event_type": "short_rest",
                    "audience_scope": "party",
                    "payload": {
                        "member_ids": actor_ids,
                        "occurrence_id": rest_identity,
                        "member_choices": normalized,
                        "duration_minutes": duration_minutes,
                        "clock_set": clock_set is not None,
                    },
                },
                "actor_knowledge": [
                    {
                        "actor_id": actor_id,
                        "knowledge_key": (
                            f"playthrough.{_token(run_id)}.{_token(actor_id)}."
                            f"short_rest.{rest_identity}"
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
            "idempotency_key": _mutation_key(run_id, "short-rest-continuity", rest_identity),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"short-rest-sync:{rest_identity}",
    )
    return {
        "occurrence_id": rest_identity,
        "member_ids": actor_ids,
        "preconditions": {
            **preconditions,
            "world_time": required_start_clock,
        },
        "clock_set": clock_set,
        "rest_recovered": rest_recovered,
        "clock_advanced": rested,
        "rests": [
            dict(dict(rested.get("recovered") or {}).get(actor_id) or {}) for actor_id in actor_ids
        ],
        "party_rest": rested,
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
    activity_event_id: str,
    declaration: dict[str, Any] | None,
    reason: str,
    knowledge_actor_ids: list[str],
    defer_checkpoint: bool = False,
) -> dict[str, Any]:
    normalized_event_id = activity_event_id.strip()
    if not all(
        (
            scene_id,
            location_key,
            actor_id,
            activity_id,
            normalized_event_id,
            reason.strip(),
        )
    ):
        raise ValueError(
            "use-activity requires scene, location, actor, activity id, "
            "activity event id, and reason"
        )
    if len(normalized_event_id) > 200:
        raise ValueError("activity event id must not exceed 200 characters")
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
            "idempotency_key": _mutation_key(run_id, "play-activity", normalized_event_id),
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
    continuity_payload = {
        "event": {
            "summary": reason.strip(),
            "event_type": "character_activity",
            "audience_scope": "party",
            "payload": {
                "scene_id": scene_id,
                "location_key": location_key,
                "activity_event_id": normalized_event_id,
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
                    f"{_token(actor_id)}.{_token(activity_id)}."
                    f"{_token(normalized_event_id)}"
                ),
                "proposition": reason.strip(),
                "disclosure_scope": "owner",
            }
            for recipient in recipients
        ],
        "branch_id": str(branch["id"]),
    }
    if not defer_checkpoint:
        continuity_payload["snapshot"] = {
            "label": (
                f"Full playthrough activity: {actor['name']} used {activity_id} at {location_key}"
            )
        }
    committed = await client.domain(
        "memory_change",
        {
            "campaign_id": campaign_id,
            "action": "commit",
            "payload": continuity_payload,
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id,
                "play-activity-continuity",
                normalized_event_id,
            ),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"play-activity-sync:{normalized_event_id}",
    )
    return {
        "scene_id": scene_id,
        "location_key": location_key,
        "actor": {"id": actor_id, "name": actor["name"]},
        "activity_id": activity_id,
        "activity_event_id": normalized_event_id,
        "action": acted,
        "knowledge_actor_ids": recipients,
        "continuity": committed,
        "sync": synced,
    }


async def _cast_standard_spell(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    occurrence_id: str,
    scene_id: str,
    source_scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    actor_id: str,
    target_id: str,
    spell_id: str,
    cast_level: int | None,
    component_ruling: dict[str, Any] | None,
    agent_ruling: dict[str, Any] | None,
    reason: str,
    knowledge_actor_ids: list[str],
    defer_checkpoint: bool = False,
) -> dict[str, Any]:
    cast_identity = _occurrence_identity(occurrence_id, "cast-spell")
    normalized_actor_id = actor_id.strip()
    normalized_target_id = target_id.strip()
    normalized_spell_id = spell_id.strip()
    normalized_reason = reason.strip()
    if not all(
        (
            scene_id,
            source_scene_id,
            location_key,
            source_excerpt.strip(),
            normalized_actor_id,
            normalized_spell_id,
            normalized_reason,
        )
    ):
        raise ValueError(
            "cast-spell requires occurrence and source scenes, location, excerpt, "
            "actor, spell, and reason"
        )
    if cast_level is not None and cast_level < 0:
        raise ValueError("spell cast level must be non-negative")
    occurrence_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    cited_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": source_scene_id},
        },
    )
    exact_ref = await _validate_source_ref(
        client,
        cited_scene,
        source_ref,
        excerpt=source_excerpt,
    )
    if location_key not in {
        str(item.get("key") or "") for item in _scene_locations(occurrence_scene)
    }:
        raise ValueError("cast-spell location is not present in the occurrence scene")
    actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": normalized_actor_id}},
    )
    if actor.get("campaign_id") != campaign_id:
        raise ValueError("cast-spell actor does not belong to the campaign")
    if normalized_target_id:
        target = await client.domain(
            "character_query",
            {"view": "get", "payload": {"character_id": normalized_target_id}},
        )
        if target.get("campaign_id") != campaign_id:
            raise ValueError("cast-spell target does not belong to the campaign")
    payload: dict[str, Any] = {"spell_id": normalized_spell_id}
    if cast_level is not None:
        payload["cast_level"] = cast_level
    if component_ruling is not None:
        payload["component_ruling"] = deepcopy(component_ruling)
    if normalized_spell_id == CORE_INVISIBILITY_SPELL_ID:
        if not normalized_target_id:
            raise ValueError("the engine-settled Invisibility spell requires a target")
        payload["target_character_ids"] = [normalized_target_id]
    elif normalized_spell_id == CORE_FLY_SPELL_ID:
        if not normalized_target_id:
            raise ValueError("the engine-settled Fly spell requires a target")
        payload["target_character_ids"] = [normalized_target_id]
        payload["willing_target_ids"] = [normalized_target_id]
    acted = await client.domain(
        "character_action",
        {
            "character_id": normalized_actor_id,
            "action": "cast_spell",
            "payload": payload,
            "expected_revision": actor["revision"],
            "idempotency_key": _mutation_key(
                run_id,
                "standard-spell-cast",
                cast_identity,
            ),
        },
    )
    payment = dict(dict(acted.get("result") or {}).get("payment") or {})
    if acted.get("status") == "pending_ruling" and not payment:
        raise_for_pending_ruling(
            acted,
            operation="character_action.cast_spell",
            context={
                "actor_id": normalized_actor_id,
                "target_id": normalized_target_id,
                "spell_id": normalized_spell_id,
            },
            retry_hint=(
                "Resolve the typed pre-commit ruling and retry at the current character revision."
            ),
        )
    if acted.get("status") not in {"committed", "pending_ruling"} or not payment:
        raise RuntimeError("standard spell did not consume its canonical resource")
    normalized_agent_ruling = _settled_agent_ruling(
        agent_ruling,
        label="standard spell",
        ruling_kinds=frozenset({"generic_spell_effect"}),
    )
    if acted.get("status") == "pending_ruling":
        pending = normalize_pending_ruling(acted)
        if pending["ruling_kind"] != "generic_spell_effect":
            raise RuntimeError(
                "a paid standard spell returned an unsupported post-commit ruling kind"
            )
        if normalized_agent_ruling is None:
            raise ValueError("a paid descriptive standard spell requires --spell-agent-ruling-json")
    elif normalized_agent_ruling is not None:
        raise ValueError(
            "spell Agent ruling must be omitted when the engine fully commits the effect"
        )
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    recipients = list(
        dict.fromkeys(
            [
                normalized_actor_id,
                *([normalized_target_id] if normalized_target_id else []),
                *knowledge_actor_ids,
            ]
        )
    )
    campaign = await _campaign(client, campaign_id)
    continuity_payload = {
        "event": {
            "summary": normalized_reason,
            "event_type": "standard_spell_cast",
            "audience_scope": "party",
            "payload": {
                "scene_id": scene_id,
                "source_scene_id": source_scene_id,
                "location_key": location_key,
                "occurrence_id": cast_identity,
                "actor_id": normalized_actor_id,
                "target_id": normalized_target_id,
                "spell_id": normalized_spell_id,
                "payment": payment,
                "resolution_status": acted["status"],
                "source_excerpt": source_excerpt,
                "source_ref": exact_ref,
                "agent_ruling": normalized_agent_ruling,
            },
        },
        "actor_knowledge": [
            {
                "actor_id": recipient,
                "knowledge_key": (
                    f"playthrough.{_token(run_id)}.{_token(scene_id)}.spell.{_token(cast_identity)}"
                ),
                "proposition": normalized_reason,
                "disclosure_scope": "owner",
            }
            for recipient in recipients
        ],
        "branch_id": str(branch["id"]),
    }
    if not defer_checkpoint:
        continuity_payload["snapshot"] = {
            "label": (
                f"Full playthrough standard spell: {actor['name']} cast {normalized_spell_id}"
            )
        }
    committed = await client.domain(
        "memory_change",
        {
            "campaign_id": campaign_id,
            "action": "commit",
            "payload": continuity_payload,
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id,
                "standard-spell-continuity",
                cast_identity,
            ),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"standard-spell-sync:{cast_identity}",
    )
    return {
        "scene": {
            "scene_id": scene_id,
            "source_scene_id": source_scene_id,
            "location_key": location_key,
            "source_ref": exact_ref,
        },
        "actor": {"id": normalized_actor_id, "name": actor["name"]},
        "target_id": normalized_target_id,
        "occurrence_id": cast_identity,
        "spell_id": normalized_spell_id,
        "cast": acted,
        "agent_ruling": normalized_agent_ruling,
        "knowledge_actor_ids": recipients,
        "continuity": committed,
        "sync": synced,
    }


async def _cast_source_spell(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    occurrence_id: str,
    scene_id: str,
    source_scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    actor_id: str,
    spell_id: str,
    source_item_id: str,
    cast_level: int | None,
    component_ruling: dict[str, Any] | None,
    reason: str,
    knowledge_actor_ids: list[str],
    defer_checkpoint: bool = False,
) -> dict[str, Any]:
    cast_identity = _occurrence_identity(occurrence_id, "cast-source-spell")
    normalized_actor_id = actor_id.strip()
    normalized_spell_id = spell_id.strip()
    normalized_item_id = source_item_id.strip()
    normalized_reason = reason.strip()
    if not all(
        (
            scene_id,
            source_scene_id,
            location_key,
            source_excerpt.strip(),
            normalized_actor_id,
            normalized_spell_id,
            normalized_item_id,
            normalized_reason,
        )
    ):
        raise ValueError(
            "cast-source-spell requires occurrence and source scenes, location, "
            "excerpt, actor, spell, source item, and reason"
        )
    if cast_level is not None and cast_level < 0:
        raise ValueError("spell cast level must be non-negative")
    occurrence_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    cited_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": source_scene_id},
        },
    )
    exact_ref = await _validate_source_ref(
        client,
        cited_scene,
        source_ref,
        excerpt=source_excerpt,
    )
    if location_key not in {
        str(item.get("key") or "") for item in _scene_locations(occurrence_scene)
    }:
        raise ValueError("cast-source-spell location is not present in the occurrence scene")
    actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": normalized_actor_id}},
    )
    if actor.get("campaign_id") != campaign_id:
        raise ValueError("cast-source-spell actor does not belong to the campaign")
    source_item = next(
        (
            dict(item)
            for item in actor["sheet"]["inventory"]["items"]
            if str(item.get("id") or "") == normalized_item_id
        ),
        None,
    )
    if source_item is None:
        raise ValueError("cast-source-spell actor does not carry the source item")
    before_charges = int(dict(source_item.get("charges") or {}).get("value", 0) or 0)
    payload: dict[str, Any] = {
        "spell_id": normalized_spell_id,
        "source_item_id": normalized_item_id,
    }
    if cast_level is not None:
        payload["cast_level"] = cast_level
    if component_ruling is not None:
        payload["component_ruling"] = deepcopy(component_ruling)
    acted = await client.domain(
        "character_action",
        {
            "character_id": normalized_actor_id,
            "action": "cast_spell",
            "payload": payload,
            "expected_revision": actor["revision"],
            "idempotency_key": _mutation_key(
                run_id,
                "source-spell-cast",
                cast_identity,
            ),
        },
    )
    payment = dict(dict(acted.get("result") or {}).get("payment") or {})
    if acted.get("status") == "pending_ruling" and not payment:
        raise_for_pending_ruling(
            acted,
            operation="character_action.cast_source_spell",
            context={
                "actor_id": normalized_actor_id,
                "spell_id": normalized_spell_id,
                "source_item_id": normalized_item_id,
            },
            retry_hint=(
                "Resolve the typed pre-commit ruling and retry at the current character revision."
            ),
        )
    if acted.get("status") not in {"committed", "pending_ruling"}:
        raise RuntimeError(
            f"source spell cast did not consume its canonical resources: {acted.get('status')}"
        )
    if (
        payment.get("economy") != "item_charges"
        or str(payment.get("item_id") or "") != normalized_item_id
        or isinstance(payment.get("cost"), bool)
        or not isinstance(payment.get("cost"), int)
        or int(payment["cost"]) <= 0
    ):
        raise RuntimeError("source spell cast returned invalid item-charge payment")
    character_after = dict(acted.get("character") or {})
    item_after = next(
        (
            dict(item)
            for item in dict(character_after.get("sheet") or {})
            .get("inventory", {})
            .get("items", [])
            if str(item.get("id") or "") == normalized_item_id
        ),
        None,
    )
    if item_after is None:
        raise RuntimeError("source spell cast removed the source item unexpectedly")
    after_charges = int(dict(item_after.get("charges") or {}).get("value", 0) or 0)
    cast_recovered = after_charges == before_charges
    if not cast_recovered and after_charges != before_charges - int(payment["cost"]):
        raise RuntimeError("source spell cast charge balance does not match its payment")

    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    recipients = list(dict.fromkeys([normalized_actor_id, *knowledge_actor_ids]))
    campaign = await _campaign(client, campaign_id)
    continuity_payload = {
        "event": {
            "summary": normalized_reason,
            "event_type": "magic_item_spell_cast",
            "audience_scope": "party",
            "payload": {
                "scene_id": scene_id,
                "source_scene_id": source_scene_id,
                "location_key": location_key,
                "occurrence_id": cast_identity,
                "actor_id": normalized_actor_id,
                "spell_id": normalized_spell_id,
                "source_item_id": normalized_item_id,
                "payment": payment,
                "resolution_status": acted["status"],
                "source_excerpt": source_excerpt,
                "source_ref": exact_ref,
            },
        },
        "actor_knowledge": [
            {
                "actor_id": recipient,
                "knowledge_key": (
                    f"playthrough.{_token(run_id)}.{_token(scene_id)}."
                    f"source_spell.{_token(cast_identity)}"
                ),
                "proposition": normalized_reason,
                "disclosure_scope": "owner",
            }
            for recipient in recipients
        ],
        "branch_id": str(branch["id"]),
    }
    if not defer_checkpoint:
        continuity_payload["snapshot"] = {
            "label": (f"Full playthrough source spell: {actor['name']} cast {normalized_spell_id}")
        }
    committed = await client.domain(
        "memory_change",
        {
            "campaign_id": campaign_id,
            "action": "commit",
            "payload": continuity_payload,
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id,
                "source-spell-continuity",
                cast_identity,
            ),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"source-spell-sync:{cast_identity}",
    )
    return {
        "scene": {
            "scene_id": scene_id,
            "source_scene_id": source_scene_id,
            "location_key": location_key,
            "source_ref": exact_ref,
        },
        "actor": {"id": normalized_actor_id, "name": actor["name"]},
        "occurrence_id": cast_identity,
        "spell_id": normalized_spell_id,
        "source_item_id": normalized_item_id,
        "cast": acted,
        "cast_recovered": cast_recovered,
        "charges": {"before": before_charges, "after": after_charges},
        "knowledge_actor_ids": recipients,
        "continuity": committed,
        "sync": synced,
    }


async def _cast_healing_spell(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    occurrence_id: str,
    scene_id: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    location_key: str,
    actor_id: str,
    target_id: str,
    spell_id: str,
    cast_level: int | None,
    component_ruling: dict[str, Any] | None,
    reason: str,
    knowledge_actor_ids: list[str],
    defer_checkpoint: bool = False,
) -> dict[str, Any]:
    cast_identity = _occurrence_identity(occurrence_id, "cast-healing-spell")
    normalized_actor_id = actor_id.strip()
    normalized_target_id = target_id.strip()
    normalized_spell_id = spell_id.strip()
    normalized_reason = reason.strip()
    if not all(
        (
            scene_id,
            location_key,
            source_excerpt.strip(),
            normalized_actor_id,
            normalized_target_id,
            normalized_spell_id,
            normalized_reason,
        )
    ):
        raise ValueError(
            "cast-healing-spell requires scene, location, source, caster, target, spell, and reason"
        )
    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    exact_ref = await _validate_source_ref(client, scene, source_ref, excerpt=source_excerpt)
    if location_key not in {str(item.get("key") or "") for item in _scene_locations(scene)}:
        raise ValueError("cast-healing-spell location is not present in the scene atlas")
    actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": normalized_actor_id}},
    )
    target = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": normalized_target_id}},
    )
    if actor.get("campaign_id") != campaign_id or target.get("campaign_id") != campaign_id:
        raise ValueError("cast-healing-spell actors must belong to the campaign")
    spell = next(
        (
            dict(item)
            for item in dict(actor.get("sheet") or {}).get("content", {}).get("spells", [])
            if str(item.get("id") or "") == normalized_spell_id
        ),
        None,
    )
    resolution = dict((spell or {}).get("resolution") or {})
    healing = dict(resolution.get("healing") or {})
    if resolution.get("kind") != "healing" or not healing:
        raise ValueError("cast-healing-spell requires a structured healing spell card")
    spell_level = int((spell or {}).get("level", 0) or 0)
    paid_level = spell_level if cast_level is None else cast_level
    if paid_level < max(1, spell_level):
        raise ValueError("cast-healing-spell cast level is below the spell level")
    modifier = 0
    if healing.get("add_spellcasting_modifier"):
        derived = dict(actor.get("derived") or {})
        ability = str(
            dict(derived.get("spellcasting") or {}).get("ability")
            or dict(actor["sheet"].get("spellcasting") or {}).get("ability")
            or ""
        )
        derived_modifiers = dict(derived.get("ability_modifiers") or {})
        modifier = (
            int(derived_modifiers[ability])
            if ability in derived_modifiers
            else effective_ability_modifier(actor["sheet"], ability)
        )
    healing_expression = scaled_roll_expression(
        healing,
        cast_level=paid_level,
        actor_level=int(actor["sheet"].get("progression", {}).get("level", 1) or 1),
        flat_modifier=modifier,
    )

    cast_payload: dict[str, Any] = {
        "spell_id": normalized_spell_id,
        "cast_level": paid_level,
    }
    if component_ruling is not None:
        cast_payload["component_ruling"] = deepcopy(component_ruling)
    cast = await client.domain(
        "character_action",
        {
            "character_id": normalized_actor_id,
            "action": "cast_spell",
            "payload": cast_payload,
            "expected_revision": actor["revision"],
            "idempotency_key": _mutation_key(
                run_id,
                "healing-spell-cast",
                cast_identity,
            ),
        },
    )
    if cast.get("status") == "pending_ruling" and not dict(cast.get("result") or {}).get("payment"):
        raise_for_pending_ruling(
            cast,
            operation="character_action.cast_healing_spell",
            context={
                "actor_id": normalized_actor_id,
                "target_id": normalized_target_id,
                "spell_id": normalized_spell_id,
            },
            retry_hint=(
                "Resolve the typed pre-commit ruling and retry before rolling or applying healing."
            ),
        )
    if cast.get("status") not in {"committed", "pending_ruling"}:
        raise RuntimeError("healing spell did not consume its canonical resource")
    agent_ruling = None
    if cast.get("status") == "pending_ruling":
        normalized_ruling = normalize_pending_ruling(cast)
        if normalized_ruling["ruling_kind"] != "generic_spell_effect":
            raise RuntimeError(
                "a paid healing spell returned an unsupported post-commit ruling kind"
            )
        agent_ruling = _settled_agent_ruling(
            {
                "default_resolver": "agent",
                "ruling_kind": "generic_spell_effect",
                "decision": (
                    f"The Agent selects {normalized_target_id} as the target of "
                    f"{normalized_spell_id} and executes the spell card's structured "
                    "healing resolution through public dice and character-state tools."
                ),
                "reason": normalized_reason,
            },
            label="healing spell",
            ruling_kinds=frozenset({"generic_spell_effect"}),
        )
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
            "expression": healing_expression,
            "branch_id": str(branch["id"]),
            "expected_campaign_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id,
                "healing-spell-roll",
                cast_identity,
            ),
        },
    )
    roll_result = _dice_result(rolled)
    target_after_roll = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": normalized_target_id}},
    )
    healed = await client.domain(
        "character_state_change",
        {
            "character_id": normalized_target_id,
            "action": "heal",
            "payload": {
                "amount": int(roll_result["total"]),
                "source_actor_id": normalized_actor_id,
                "spell_id": normalized_spell_id,
                "spell_level": paid_level,
            },
            "expected_revision": target_after_roll["revision"],
            "idempotency_key": _mutation_key(
                run_id,
                "healing-spell-apply",
                cast_identity,
            ),
        },
    )
    recipients = list(
        dict.fromkeys([normalized_actor_id, normalized_target_id, *knowledge_actor_ids])
    )
    campaign = await _campaign(client, campaign_id)
    continuity_payload = {
        "event": {
            "summary": normalized_reason,
            "event_type": "healing_spell_cast",
            "audience_scope": "party",
            "payload": {
                "scene_id": scene_id,
                "location_key": location_key,
                "occurrence_id": cast_identity,
                "actor_id": normalized_actor_id,
                "target_id": normalized_target_id,
                "spell_id": normalized_spell_id,
                "cast_level": paid_level,
                "healing_expression": healing_expression,
                "healing_roll": roll_result,
                "source_excerpt": source_excerpt,
                "source_ref": exact_ref,
                **({"agent_ruling": agent_ruling} if agent_ruling is not None else {}),
            },
        },
        "actor_knowledge": [
            {
                "actor_id": recipient,
                "knowledge_key": (
                    f"playthrough.{_token(run_id)}.healing_spell.{_token(cast_identity)}"
                ),
                "proposition": normalized_reason,
                "disclosure_scope": "owner",
            }
            for recipient in recipients
        ],
        "branch_id": str(branch["id"]),
    }
    if not defer_checkpoint:
        continuity_payload["snapshot"] = {
            "label": f"Full playthrough healing spell: {normalized_spell_id}"
        }
    committed = await client.domain(
        "memory_change",
        {
            "campaign_id": campaign_id,
            "action": "commit",
            "payload": continuity_payload,
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(
                run_id,
                "healing-spell-continuity",
                cast_identity,
            ),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"healing-spell-sync:{cast_identity}",
    )
    return {
        "scene_id": scene_id,
        "location_key": location_key,
        "source_ref": exact_ref,
        "occurrence_id": cast_identity,
        "actor_id": normalized_actor_id,
        "target_id": normalized_target_id,
        "spell_id": normalized_spell_id,
        "cast_level": paid_level,
        "cast": cast,
        "agent_ruling": agent_ruling,
        "healing_expression": healing_expression,
        "roll": roll_result,
        "healing": healed,
        "knowledge_actor_ids": recipients,
        "continuity": committed,
        "sync": synced,
    }


async def _long_rest(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    occurrence_id: str,
    members: list[dict[str, Any]],
    start_clock: dict[str, Any] | None,
    duration_minutes: int,
    reason: str,
    prerequisite_scene_id: str = "",
    prerequisite_outcome_id: str = "",
    prerequisite_actor_ids: list[str] | None = None,
    expected_start_clock: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rest_identity = _occurrence_identity(occurrence_id, "long-rest")
    trance_minimum = minimum_rest_minutes("long_rest", allows_trance=True)
    standard_minimum = minimum_rest_minutes("long_rest")
    if duration_minutes < trance_minimum:
        raise ValueError(
            f"long-rest requires at least {standard_minimum} minutes, or "
            f"{trance_minimum} minutes for "
            "members with the source-granted Trance feature"
        )
    if not members or not reason.strip():
        raise ValueError("long-rest requires members and --rest-reason")
    allowed_fields = {
        "actor_id",
        "prepared_spell_ids",
        "hit_dice_recovery",
        "rest_activity_minutes",
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
        rest_activity_minutes = member.get("rest_activity_minutes")
        if (
            unexpected
            or not actor_id
            or (prepared_ids is not None and not isinstance(prepared_ids, list))
            or (hit_dice_recovery is not None and not isinstance(hit_dice_recovery, dict))
            or not isinstance(food_and_drink, bool)
            or (rest_activity_minutes is not None and not isinstance(rest_activity_minutes, dict))
        ):
            raise ValueError(
                "long-rest members accept actor_id, optional prepared_spell_ids, "
                "optional hit_dice_recovery, optional rest_activity_minutes, "
                "and optional food_and_drink only"
            )
        actor = await client.domain(
            "character_query",
            {"view": "get", "payload": {"character_id": actor_id}},
        )
        if actor.get("campaign_id") != campaign_id:
            raise ValueError("every long-rest actor must belong to the campaign")
        actors.append(actor)
        uses_trance = duration_minutes < standard_minimum and allows_trance_rest(actor["sheet"])
        if duration_minutes < standard_minimum and not uses_trance:
            raise ValueError(
                f"long-rest member {actor_id} requires at least {standard_minimum} minutes"
            )
        normalized.append(
            {
                "actor_id": actor_id,
                "prepared_spell_ids": (list(prepared_ids) if prepared_ids is not None else None),
                "hit_dice_recovery": deepcopy(hit_dice_recovery),
                "rest_activity_minutes": deepcopy(rest_activity_minutes or {}),
                "food_and_drink": food_and_drink,
            }
        )
    actor_ids = [item["actor_id"] for item in normalized]
    if len(actor_ids) != len(set(actor_ids)):
        raise ValueError("long-rest member actor ids must be unique")
    preconditions = await _validate_narrative_preconditions(
        client,
        campaign_id=campaign_id,
        scene_id=prerequisite_scene_id,
        outcome_id=prerequisite_outcome_id,
        actor_ids=prerequisite_actor_ids,
    )
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    campaign = await _campaign(client, campaign_id)
    required_start_clock = _validate_world_time_precondition(
        campaign,
        expected_start_clock,
    )
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
                "idempotency_key": _mutation_key(run_id, "long-rest-clock-set", rest_identity),
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
        if member["rest_activity_minutes"]:
            party_member["rest_activity_minutes"] = member["rest_activity_minutes"]
        if member["prepared_spell_ids"] is not None:
            party_member["prepared_spell_ids"] = member["prepared_spell_ids"]
        if member["hit_dice_recovery"] is not None:
            party_member["hit_dice_recovery"] = member["hit_dice_recovery"]
        party_members.append(party_member)
    party_rest_key = _mutation_key(run_id, "long-rest-party", rest_identity)
    rest_recovered = False
    try:
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
                "idempotency_key": party_rest_key,
            },
        )
    except Exception as exc:
        if "idempotency key reused with a different request" not in str(exc):
            raise
        receipt = await client.domain(
            "state_revision",
            {
                "campaign_id": campaign_id,
                "action": "receipt",
                "payload": {"idempotency_key": party_rest_key},
            },
        )
        receipt_branch_id = str(receipt.get("branch_id") or "")
        if receipt_branch_id != str(branch["id"]):
            raise RuntimeError("long-rest recovery receipt is from another branch")
        revision_rows = list(receipt.get("entity_revisions") or [])
        if not all(isinstance(item, dict) for item in revision_rows):
            raise RuntimeError("long-rest recovery receipt has invalid revision evidence")
        revision_by_entity = {
            (str(item.get("entity_type") or ""), str(item.get("entity_id") or "")): item
            for item in revision_rows
        }
        if len(revision_by_entity) != len(revision_rows):
            raise RuntimeError("long-rest recovery receipt repeats revision evidence")
        expected_entities = {("campaign", campaign_id)} | {
            ("character", actor_id) for actor_id in actor_ids
        }
        if not expected_entities.issubset(revision_by_entity) or any(
            entity_type not in {"campaign", "character"}
            for entity_type, _entity_id in revision_by_entity
        ):
            raise RuntimeError("long-rest recovery receipt has unexpected revision evidence")
        campaign_revision_row = revision_by_entity[("campaign", campaign_id)]
        if (
            campaign_revision_row.get("after_revision") != campaign.get("revision")
            or campaign_revision_row.get("before_revision") != campaign.get("revision") - 1
        ):
            raise RuntimeError("long-rest recovery campaign revisions do not match")
        for (entity_type, entity_id), revision_row in revision_by_entity.items():
            if entity_type != "character" or entity_id in actor_by_id:
                continue
            current_actor = await client.domain(
                "character_query",
                {"view": "get", "payload": {"character_id": entity_id}},
            )
            current_revision = current_actor.get("revision")
            if (
                current_actor.get("campaign_id") != campaign_id
                or isinstance(current_revision, bool)
                or not isinstance(current_revision, int)
                or revision_row.get("after_revision") != current_revision
                or revision_row.get("before_revision") != current_revision - 1
            ):
                raise RuntimeError("long-rest recovery incidental actor revisions do not match")
        recovery_members = []
        for member in normalized:
            actor = actor_by_id[member["actor_id"]]
            current_revision = actor.get("revision")
            revision_row = revision_by_entity[("character", member["actor_id"])]
            if (
                isinstance(current_revision, bool)
                or not isinstance(current_revision, int)
                or revision_row.get("after_revision") != current_revision
                or revision_row.get("before_revision") != current_revision - 1
            ):
                raise RuntimeError("long-rest recovery actor revisions do not match")
            recovery_members.append(
                {
                    "character_id": member["actor_id"],
                    "expected_revision": revision_row["before_revision"],
                    "prepared_spell_ids": member["prepared_spell_ids"],
                    "hit_dice_recovery": member["hit_dice_recovery"],
                    "rest_activity_minutes": member["rest_activity_minutes"],
                    "food_and_drink": member["food_and_drink"],
                }
            )
        expected_request_hash = _idempotency_request_hash(
            {
                "members": recovery_members,
                "duration_minutes": duration_minutes,
                "branch_id": receipt_branch_id,
            }
        )
        rested = _validate_recovered_long_rest(
            receipt,
            campaign=campaign,
            actors=actors,
            members=normalized,
            duration_minutes=duration_minutes,
            expected_request_hash=expected_request_hash,
        )
        rest_recovered = True
    if rested.get("status") != "committed":
        raise RuntimeError("long rest did not commit")
    rested_revision = rested.get("campaign_revision")
    if isinstance(rested_revision, bool) or not isinstance(rested_revision, int):
        raise RuntimeError("long rest response has no integer campaign revision")
    committed = await client.domain(
        "memory_change",
        {
            "campaign_id": campaign_id,
            "action": "commit",
            "payload": {
                "event": {
                    "summary": reason.strip(),
                    "event_type": "long_rest",
                    "audience_scope": "party",
                    "payload": {
                        "member_ids": actor_ids,
                        "occurrence_id": rest_identity,
                        "member_choices": normalized,
                        "duration_minutes": duration_minutes,
                        "clock_set": clock_set is not None,
                    },
                },
                "actor_knowledge": [
                    {
                        "actor_id": actor_id,
                        "knowledge_key": (
                            f"playthrough.{_token(run_id)}.{_token(actor_id)}."
                            f"long_rest.{rest_identity}"
                        ),
                        "proposition": reason.strip(),
                        "disclosure_scope": "owner",
                    }
                    for actor_id in actor_ids
                ],
                "snapshot": {"label": f"Full playthrough long rest: {reason.strip()}"},
                "branch_id": str(branch["id"]),
            },
            # Do not attach a rest event after an unrelated intervening write.
            # A prior identical continuity commit still replays before the
            # revision check.
            "expected_revision": rested_revision,
            "idempotency_key": _mutation_key(run_id, "long-rest-continuity", rest_identity),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"long-rest-sync:{rest_identity}",
    )
    return {
        "occurrence_id": rest_identity,
        "member_ids": actor_ids,
        "preconditions": {
            **preconditions,
            "world_time": required_start_clock,
        },
        "clock_set": clock_set,
        "rest": rested,
        "rest_recovered": rest_recovered,
        "continuity": committed,
        "sync": synced,
    }


async def _advance_time(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    occurrence_id: str,
    scene_id: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    period: str,
    count: int | None,
    reason: str,
    start_clock: dict[str, Any] | None,
    agent_ruling: dict[str, Any] | None,
    knowledge_actor_ids: list[str],
    defer_checkpoint: bool = False,
    expected_after: dict[str, Any] | None = None,
    expected_after_ticks: int | None = None,
    prerequisite_scene_id: str = "",
    prerequisite_outcome_id: str = "",
    prerequisite_actor_ids: list[str] | None = None,
) -> dict[str, Any]:
    identity = _occurrence_identity(occurrence_id, "advance-time")
    normalized_reason = reason.strip()
    if (
        not scene_id
        or period not in NARRATIVE_GAME_TIME_PERIODS
        or count is None
        or count <= 0
        or not normalized_reason
    ):
        raise ValueError("advance-time requires scene, positive count, period, and reason")
    normalized_expected_after = _normalize_expected_calendar_time(expected_after)
    if expected_after_ticks is not None and (
        isinstance(expected_after_ticks, bool)
        or not isinstance(expected_after_ticks, int)
        or expected_after_ticks < 0
    ):
        raise ValueError("advance-time expected tick target must be nonnegative")
    if normalized_expected_after is None and expected_after_ticks is None:
        raise ValueError(
            "advance-time requires --time-expected-after-ticks or "
            "--time-expected-after-json so every elapsed interval is bound to "
            "one machine-verifiable destination"
        )
    normalized_agent_ruling = _settled_time_agent_ruling(
        agent_ruling,
        period=period,
        count=count,
    )
    has_source_ref = source_ref is not None
    has_source_excerpt = bool(source_excerpt.strip())
    if has_source_ref != has_source_excerpt:
        raise ValueError("advance-time source evidence requires both exact source ref and excerpt")
    if not has_source_ref and normalized_agent_ruling is None:
        raise ValueError(
            "advance-time requires exact source evidence or a settled Agent duration ruling"
        )
    if len(knowledge_actor_ids) != len(set(knowledge_actor_ids)):
        raise ValueError("advance-time knowledge actor ids must be unique")
    preconditions = await _validate_narrative_preconditions(
        client,
        campaign_id=campaign_id,
        scene_id=prerequisite_scene_id,
        outcome_id=prerequisite_outcome_id,
        actor_ids=prerequisite_actor_ids,
    )
    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    exact_ref = (
        await _validate_source_ref(client, scene, source_ref, excerpt=source_excerpt)
        if has_source_ref
        else None
    )
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
    campaign = await _campaign(client, campaign_id)
    campaign_state = dict(campaign.get("state") or {})
    before = deepcopy(dict(campaign_state.get("world_time") or {}))
    before_game_time = deepcopy(dict(campaign_state.get("game_time") or {}))
    expected_tick_delta = game_time_ticks(period, count)
    expected_minutes = expected_tick_delta // TICKS_PER_MINUTE
    current_ticks = before_game_time.get("elapsed_ticks")
    if isinstance(current_ticks, bool) or not isinstance(current_ticks, int):
        raise RuntimeError("campaign has no canonical game-time position")
    projected_before = before
    if not projected_before and isinstance(start_clock, dict):
        projected_before = calendar_minute_point(
            day=start_clock.get("day"),
            hour=start_clock.get("hour", 0),
            minute=start_clock.get("minute", 0),
        )
    if normalized_expected_after is not None and not projected_before:
        raise ValueError(
            "advance-time calendar target requires an existing or supplied start clock"
        )
    if expected_after_ticks is None:
        raise ValueError("advance-time requires the canonical expected tick target")
    expected_target_ticks = expected_after_ticks
    clock_recovery = current_ticks == expected_target_ticks
    if not clock_recovery and current_ticks + expected_tick_delta != expected_target_ticks:
        raise ValueError(
            "advance-time duration does not reach expected tick target: "
            f"computed {current_ticks + expected_tick_delta}, "
            f"expected {expected_target_ticks}"
        )
    if clock_recovery:
        before_game_time = {
            **before_game_time,
            "elapsed_ticks": current_ticks - expected_tick_delta,
        }
        if before_game_time["elapsed_ticks"] < 0:
            raise RuntimeError("advance-time recovery predates canonical game-time zero")
        if normalized_expected_after is not None:
            original_elapsed = normalized_expected_after["elapsed_minutes"] - expected_minutes
            if original_elapsed < 0:
                raise ValueError("advance-time recovery target predates the requested duration")
            recovered_point = calendar_minute_point_from_elapsed(original_elapsed)
            before = {
                "schema_version": int(projected_before.get("schema_version", 1) or 1),
                **recovered_point,
                "label": str(projected_before.get("label") or ""),
            }
    elif normalized_expected_after is not None:
        projected_after = _project_world_time(projected_before, expected_minutes)
        if projected_after != normalized_expected_after:
            raise ValueError(
                "advance-time duration does not reach expected target: "
                f"computed day {projected_after['day']} "
                f"{projected_after['hour']:02}:{projected_after['minute']:02}, "
                f"elapsed {projected_after['elapsed_minutes']}"
            )
    clock_set = None
    if not before and isinstance(start_clock, dict):
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
    elif before and start_clock is not None:
        raise ValueError("advance-time start clock must be omitted after the clock is set")
    campaign = await _campaign(client, campaign_id)
    advance_payload: dict[str, Any] = {
        "period": period,
        "count": count,
    }
    if expected_after_ticks is not None:
        advance_payload["expected_elapsed_ticks"] = expected_target_ticks
    advanced = await client.domain(
        "campaign_change",
        {
            "campaign_id": campaign_id,
            "action": "clock_advance",
            "payload": advance_payload,
            "branch_id": branch_id,
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(run_id, "advance-time-clock", identity),
        },
    )
    clock_receipt_recovered = False
    if (
        advanced.get("idempotency_replayed") is True
        and advanced.get("response_recovery") == "read_current_state"
    ):
        clock_key = _mutation_key(run_id, "advance-time-clock", identity)
        receipt = await client.domain(
            "state_revision",
            {
                "campaign_id": campaign_id,
                "action": "receipt",
                "payload": {
                    "idempotency_key": clock_key,
                    "branch_id": branch_id,
                },
            },
        )
        if str(receipt.get("branch_id") or "") != branch_id:
            raise RuntimeError("clock recovery receipt is from another branch")
        expected_request_hash = _idempotency_request_hash(
            {**advance_payload, "branch_id": branch_id}
        )
        if receipt.get("request_hash") != expected_request_hash:
            raise RuntimeError("clock recovery receipt request does not match")
        campaign_revisions = [
            item
            for item in list(receipt.get("entity_revisions") or [])
            if isinstance(item, dict)
            and item.get("entity_type") == "campaign"
            and item.get("entity_id") == campaign_id
        ]
        if len(campaign_revisions) != 1:
            raise RuntimeError("clock recovery receipt has no unique campaign revision")
        revision_row = campaign_revisions[0]
        before_revision = revision_row.get("before_revision")
        after_revision = revision_row.get("after_revision")
        if (
            isinstance(before_revision, bool)
            or not isinstance(before_revision, int)
            or isinstance(after_revision, bool)
            or not isinstance(after_revision, int)
            or after_revision != before_revision + 1
        ):
            raise RuntimeError("clock recovery receipt campaign revisions are invalid")
        current_clock = deepcopy(dict(dict(campaign.get("state") or {}).get("world_time") or {}))
        current_game_time = deepcopy(dict(dict(campaign.get("state") or {}).get("game_time") or {}))
        if current_game_time.get("elapsed_ticks") != expected_target_ticks:
            raise RuntimeError("clock recovery current game time does not match the exact target")
        if (
            normalized_expected_after is not None
            and {key: current_clock.get(key) for key in CALENDAR_MINUTE_FIELDS}
            != normalized_expected_after
        ):
            raise RuntimeError("clock recovery current calendar does not match the exact target")
        advanced = {
            **advanced,
            "game_time": current_game_time,
            "world_time": current_clock or None,
            "campaign_revision": after_revision,
            "recovery_receipt": receipt,
        }
        clock_receipt_recovered = True
    advanced_revision = advanced.get("campaign_revision")
    if isinstance(advanced_revision, bool) or not isinstance(advanced_revision, int):
        raise RuntimeError("campaign clock response has no integer campaign revision")
    after = deepcopy(dict(advanced.get("world_time") or {}))
    after_game_time = deepcopy(dict(advanced.get("game_time") or {}))
    if bool(before) != bool(after):
        raise RuntimeError("campaign calendar anchor changed during time advancement")
    if before and (
        int(after.get("elapsed_minutes", 0) or 0) - int(before.get("elapsed_minutes", 0) or 0)
        != expected_minutes
    ):
        raise RuntimeError("campaign calendar did not advance by the requested duration")
    before_ticks = before_game_time.get("elapsed_ticks")
    after_ticks = after_game_time.get("elapsed_ticks")
    if (
        isinstance(before_ticks, bool)
        or not isinstance(before_ticks, int)
        or isinstance(after_ticks, bool)
        or not isinstance(after_ticks, int)
        or after_ticks - before_ticks != expected_tick_delta
        or after_ticks != expected_target_ticks
    ):
        raise RuntimeError("canonical game time did not advance by the requested duration")
    if (
        normalized_expected_after is not None
        and {key: int(after.get(key, -1)) for key in CALENDAR_MINUTE_FIELDS}
        != normalized_expected_after
    ):
        raise RuntimeError("campaign calendar response does not match expected target")
    continuity_payload = {
        "event": {
            "summary": normalized_reason,
            "event_type": "time_advanced",
            "audience_scope": "party",
            "payload": {
                "scene_id": scene_id,
                "occurrence_id": identity,
                "period": period,
                "count": count,
                "elapsed_minutes": expected_minutes,
                "elapsed_ticks": expected_tick_delta,
                "expected_elapsed_ticks": expected_target_ticks,
                "expected_calendar_time": normalized_expected_after,
                "game_time_before": before_game_time,
                "game_time_after": after_game_time,
                "world_time_before": before,
                "world_time_after": after,
                "source_excerpt": source_excerpt.strip() if has_source_ref else "",
                "source_ref": exact_ref,
                "agent_ruling": normalized_agent_ruling,
            },
        },
        "actor_knowledge": [
            {
                "actor_id": str(actor["id"]),
                "knowledge_key": (
                    f"playthrough.{_token(run_id)}.{_token(scene_id)}.time.{_token(identity)}"
                ),
                "proposition": normalized_reason,
                "disclosure_scope": "owner",
            }
            for actor in actors
        ],
        "branch_id": branch_id,
    }
    if not defer_checkpoint:
        continuity_payload["snapshot"] = {
            "label": f"Full playthrough time advance: {normalized_reason}"
        }
    committed = await client.domain(
        "memory_change",
        {
            "campaign_id": campaign_id,
            "action": "commit",
            "payload": continuity_payload,
            # Bind continuity to the exact clock mutation. On a response-lost
            # retry the clock action replays its original revision; a missing
            # continuity write can proceed only if no intervening mutation
            # changed the campaign. A previously committed continuity request
            # still replays before this revision guard.
            "expected_revision": advanced_revision,
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
        "occurrence_id": identity,
        "scene_id": scene_id,
        "source_ref": exact_ref,
        "agent_ruling": normalized_agent_ruling,
        "preconditions": preconditions,
        "clock_recovery": clock_recovery,
        "clock_receipt_recovered": clock_receipt_recovered,
        "clock_set": clock_set,
        "before": before,
        "advance": advanced,
        "after": after,
        "expected_after_ticks": expected_target_ticks,
        "expected_after": normalized_expected_after,
        "knowledge_actor_ids": [str(actor["id"]) for actor in actors],
        "continuity": committed,
        "sync": synced,
    }


async def _initialize_clock(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    occurrence_id: str,
    start_clock: dict[str, Any] | None,
) -> dict[str, Any]:
    identity = _occurrence_identity(occurrence_id, "initialize-clock")
    if not isinstance(start_clock, dict):
        raise ValueError("initialize-clock requires --time-start-clock-json")
    day = start_clock.get("day")
    hour = start_clock.get("hour", 0)
    minute = start_clock.get("minute", 0)
    label = str(start_clock.get("label") or "").strip()
    if (
        isinstance(day, bool)
        or not isinstance(day, int)
        or day < 1
        or isinstance(hour, bool)
        or not isinstance(hour, int)
        or not 0 <= hour <= 23
        or isinstance(minute, bool)
        or not isinstance(minute, int)
        or not 0 <= minute <= 59
        or not label
    ):
        raise ValueError(
            "initialize-clock requires a positive day, hour 0-23, minute 0-59, "
            "and a non-empty DM anchor label"
        )
    campaign = await _campaign(client, campaign_id)
    existing = dict(dict(campaign.get("state") or {}).get("world_time") or {})
    requested_point = calendar_minute_point(day=day, hour=hour, minute=minute)
    if existing:
        if (
            int(existing.get("elapsed_minutes", -1)) != requested_point["elapsed_minutes"]
            or str(existing.get("label") or "") != label
        ):
            raise ValueError("campaign clock is already initialized to a different DM anchor")
        return {
            "occurrence_id": identity,
            "already_initialized": True,
            "world_time": existing,
            "clock_set": None,
        }
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    clock_set = await client.domain(
        "campaign_change",
        {
            "campaign_id": campaign_id,
            "action": "clock_set",
            "payload": {
                "day": day,
                "hour": hour,
                "minute": minute,
                "label": label,
            },
            "branch_id": str(branch["id"]),
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(run_id, "initialize-clock", identity),
        },
    )
    return {
        "occurrence_id": identity,
        "already_initialized": False,
        "world_time": deepcopy(clock_set["world_time"]),
        "clock_set": clock_set,
    }


async def _recover_stable_party(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    occurrence_id: str,
    actor_ids: list[str],
    resting_members: list[dict[str, Any]] | None,
    knowledge_actor_ids: list[str],
    reason: str,
    expected_start_clock: dict[str, Any] | None = None,
) -> dict[str, Any]:
    recovery_identity = _occurrence_identity(occurrence_id, "recover-stable")
    member_ids = list(dict.fromkeys(actor_ids))
    if not member_ids or len(member_ids) != len(actor_ids) or not reason.strip():
        raise ValueError("recover-stable requires unique actor ids and a non-empty --rest-reason")
    resting_members = list(resting_members or [])
    allowed_resting_fields = {
        "actor_id",
        "arcane_recovery",
        "natural_recovery",
        "song_of_rest_source_actor_id",
        "attune_item_id",
        "attunement_prerequisite_confirmed",
        "hit_dice_spends",
        "rest_activity_minutes",
    }
    normalized_resting: list[dict[str, Any]] = []
    for index, member in enumerate(resting_members):
        if not isinstance(member, dict):
            raise ValueError(f"rest-member-json[{index}] must be an object")
        unexpected = sorted(set(member) - allowed_resting_fields)
        actor_id = str(member.get("actor_id") or "").strip()
        if unexpected or not actor_id:
            raise ValueError(
                "recover-stable rest members accept actor_id and short-rest "
                f"choice fields only; invalid entry {index}"
            )
        normalized_resting.append(
            {
                "actor_id": actor_id,
                **{
                    key: deepcopy(value)
                    for key, value in member.items()
                    if key != "actor_id" and value not in (None, {}, [])
                },
            }
        )
    resting_ids = [item["actor_id"] for item in normalized_resting]
    if len(resting_ids) != len(set(resting_ids)):
        raise ValueError("recover-stable concurrent rest actor ids must be unique")
    if set(member_ids) & set(resting_ids):
        raise ValueError("recover-stable actors cannot also be concurrent short-rest members")
    actors = []
    for actor_id in [*member_ids, *resting_ids]:
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
    required_start_clock = _validate_world_time_precondition(
        campaign,
        expected_start_clock,
    )
    actor_by_id = {str(actor["id"]): actor for actor in actors}
    concurrent_rest_payload = []
    for member in normalized_resting:
        actor_id = member["actor_id"]
        concurrent_rest_payload.append(
            {
                "character_id": actor_id,
                "expected_revision": actor_by_id[actor_id]["revision"],
                **{key: deepcopy(value) for key, value in member.items() if key != "actor_id"},
            }
        )
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
                    if str(actor["id"]) in set(member_ids)
                ],
                "resting_members": concurrent_rest_payload,
            },
            "expected_revision": campaign["revision"],
            "branch_id": branch["id"],
            "idempotency_key": _mutation_key(run_id, "stable-recovery", recovery_identity),
        },
    )
    if recovered.get("status") != "recovered":
        raise RuntimeError("party stable recovery did not commit")
    if list(recovered.get("resting_member_ids") or []) != resting_ids:
        raise RuntimeError("party stable recovery omitted concurrent short-rest members")
    recipients = list(dict.fromkeys([*member_ids, *resting_ids, *knowledge_actor_ids]))
    campaign = await _campaign(client, campaign_id)
    committed = await client.domain(
        "memory_change",
        {
            "campaign_id": campaign_id,
            "action": "commit",
            "payload": {
                "event": {
                    "summary": reason.strip(),
                    "event_type": "stable_recovery",
                    "audience_scope": "party",
                    "payload": {
                        "member_ids": member_ids,
                        "resting_member_ids": resting_ids,
                        "occurrence_id": recovery_identity,
                        "elapsed_hours": recovered["elapsed_hours"],
                        "recoveries": deepcopy(recovered["recoveries"]),
                        "rested": deepcopy(recovered.get("rested") or {}),
                        "expected_start_clock": required_start_clock,
                        "random_stream_receipt": deepcopy(recovered.get("random_stream_receipt")),
                    },
                },
                "actor_knowledge": [
                    {
                        "actor_id": actor_id,
                        "knowledge_key": (
                            f"playthrough.{_token(run_id)}.stable_recovery."
                            f"{_token(recovery_identity)}"
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
                run_id, "stable-recovery-continuity", recovery_identity
            ),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"stable-recovery-sync:{recovery_identity}",
    )
    return {
        "occurrence_id": recovery_identity,
        "member_ids": member_ids,
        "resting_member_ids": resting_ids,
        "knowledge_actor_ids": recipients,
        "recovery": recovered,
        "continuity": committed,
        "sync": synced,
    }


def _assert_source_item_shape(
    actual: Any,
    requested: Any,
    *,
    field: str = "item",
) -> None:
    """Require every requested source field while allowing server hydration fields."""
    if isinstance(requested, dict):
        if not isinstance(actual, dict):
            raise RuntimeError(f"existing {field} does not match the requested source item")
        for key, value in requested.items():
            if key not in actual:
                raise RuntimeError(f"existing {field} is missing requested field {key}")
            _assert_source_item_shape(actual[key], value, field=f"{field}.{key}")
        return
    if isinstance(requested, list):
        if not isinstance(actual, list) or len(actual) != len(requested):
            raise RuntimeError(f"existing {field} does not match the requested source item")
        for index, value in enumerate(requested):
            _assert_source_item_shape(actual[index], value, field=f"{field}[{index}]")
        return
    if actual != requested:
        raise RuntimeError(f"existing {field} does not match the requested source item")


async def _provision_source_item(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    actor_id: str,
    source_scene_id: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    item: dict[str, Any] | None,
    equip_slot: str,
    reason: str,
    checkpoint_label: str,
    defer_checkpoint: bool = False,
) -> dict[str, Any]:
    normalized_actor_id = actor_id.strip()
    normalized_scene_id = source_scene_id.strip()
    normalized_excerpt = source_excerpt.strip()
    normalized_reason = reason.strip()
    requested_item = deepcopy(item) if isinstance(item, dict) else {}
    item_id = str(requested_item.get("id") or "").strip()
    if not all(
        (
            normalized_actor_id,
            normalized_scene_id,
            normalized_excerpt,
            normalized_reason,
            item_id,
            str(requested_item.get("name") or "").strip(),
            str(requested_item.get("kind") or "").strip(),
        )
    ):
        raise ValueError(
            "provision-source-item requires actor, source scene, excerpt, reason, "
            "and an item with id, name, and kind"
        )

    source_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": normalized_scene_id},
        },
    )
    exact_ref = await _validate_source_ref(
        client,
        source_scene,
        source_ref,
        excerpt=normalized_excerpt,
    )
    expected_source_key = f"module-chunk:{exact_ref['chunk_id']}"
    if str(requested_item.get("source_key") or "") != expected_source_key:
        raise ValueError("source item source_key must be module-chunk:<source_ref.chunk_id>")
    charges = requested_item.get("charges")
    if isinstance(charges, dict) and charges:
        if str(charges.get("source_key") or "") != expected_source_key:
            raise ValueError("source item charges.source_key must match the cited module chunk")

    actor = dict(
        _facade_value(
            await client.domain(
                "character_query",
                {"view": "get", "payload": {"character_id": normalized_actor_id}},
            )
        )
    )
    if str(actor.get("campaign_id") or "") not in {"", campaign_id}:
        raise ValueError("source item actor does not belong to the campaign")
    existing = next(
        (
            dict(entry)
            for entry in actor["sheet"]["inventory"]["items"]
            if str(entry.get("id") or "") == item_id
        ),
        None,
    )
    recovered_add = existing is not None
    recovered_update = False
    if existing is None:
        added = _facade_value(
            await client.domain(
                "inventory_change",
                {
                    "owner": "character",
                    "action": "add",
                    "owner_id": normalized_actor_id,
                    "payload": {"item": requested_item},
                    "expected_revision": actor["revision"],
                    "idempotency_key": _mutation_key(
                        run_id,
                        "source-item-add",
                        f"{normalized_actor_id}:{item_id}",
                    ),
                },
            )
        )
        actor = dict(added.get("character") or added)
        existing = next(
            dict(entry)
            for entry in actor["sheet"]["inventory"]["items"]
            if str(entry.get("id") or "") == item_id
        )
    else:
        patch: dict[str, Any] = {}
        for key, requested_value in requested_item.items():
            if key == "id":
                continue
            try:
                _assert_source_item_shape(
                    existing.get(key),
                    requested_value,
                    field=f"item.{key}",
                )
            except RuntimeError:
                patch[key] = deepcopy(requested_value)
        if patch:
            updated = _facade_value(
                await client.domain(
                    "inventory_change",
                    {
                        "owner": "character",
                        "action": "update",
                        "owner_id": normalized_actor_id,
                        "payload": {"item_id": item_id, "patch": patch},
                        "expected_revision": actor["revision"],
                        "idempotency_key": _mutation_key(
                            run_id,
                            "source-item-enrich",
                            (
                                f"{normalized_actor_id}:{item_id}:"
                                f"{_token(json.dumps(requested_item, sort_keys=True))}"
                            ),
                        ),
                    },
                )
            )
            actor = dict(updated.get("character") or updated)
            existing = next(
                dict(entry)
                for entry in actor["sheet"]["inventory"]["items"]
                if str(entry.get("id") or "") == item_id
            )
            recovered_update = True
    _assert_source_item_shape(existing, requested_item)

    normalized_slot = equip_slot.strip()
    recovered_equip = bool(
        normalized_slot
        and existing.get("equipped")
        and str(existing.get("equipped_slot") or "") == normalized_slot
    )
    if normalized_slot and not recovered_equip:
        equipped = _facade_value(
            await client.domain(
                "inventory_change",
                {
                    "owner": "character",
                    "action": "equip",
                    "owner_id": normalized_actor_id,
                    "payload": {"item_id": item_id, "slot": normalized_slot},
                    "expected_revision": actor["revision"],
                    "idempotency_key": _mutation_key(
                        run_id,
                        "source-item-equip",
                        f"{normalized_actor_id}:{item_id}:{normalized_slot}",
                    ),
                },
            )
        )
        actor = dict(equipped.get("character") or equipped)
        existing = next(
            dict(entry)
            for entry in actor["sheet"]["inventory"]["items"]
            if str(entry.get("id") or "") == item_id
        )

    checkpoint = (
        None
        if defer_checkpoint
        else await _checkpoint(
            client,
            campaign_id=campaign_id,
            run_id=run_id,
            label=(
                checkpoint_label.strip()
                or f"Full playthrough source item: {requested_item['name']} — {normalized_reason}"
            ),
            checkpoint_id=f"source-item:{normalized_actor_id}:{item_id}",
        )
    )
    return {
        "actor": {
            "id": str(actor["id"]),
            "name": str(actor["name"]),
            "revision": int(actor["revision"]),
            "armor_class": int(actor["derived"]["armor_class"]),
            "class_lists": list(actor["sheet"]["spellcasting"].get("class_lists") or []),
        },
        "item": existing,
        "source_ref": exact_ref,
        "source_excerpt": normalized_excerpt,
        "reason": normalized_reason,
        "add_recovered": recovered_add,
        "update_recovered": recovered_update,
        "equip_recovered": recovered_equip,
        "checkpoint": checkpoint,
    }


async def _transfer_source_item_to_party(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    occurrence_id: str,
    scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    character_id: str,
    item_id: str,
    quantity: int | None,
    reason: str,
    checkpoint_label: str,
    defer_checkpoint: bool = False,
    recipient_character_id: str = "",
) -> dict[str, Any]:
    transfer_identity = _occurrence_identity(occurrence_id, "transfer-source-item")
    normalized_character_id = character_id.strip()
    normalized_recipient_id = recipient_character_id.strip()
    normalized_item_id = item_id.strip()
    normalized_reason = reason.strip()
    if not all(
        (
            scene_id,
            location_key,
            source_excerpt.strip(),
            normalized_character_id,
            normalized_item_id,
            normalized_reason,
        )
    ):
        raise ValueError(
            "transfer-source-item requires scene, location, excerpt, character, item, and reason"
        )
    if quantity is not None and quantity <= 0:
        raise ValueError("transfer-source-item quantity must be positive")
    if normalized_recipient_id == normalized_character_id:
        raise ValueError("source and recipient characters must differ")

    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    exact_ref = await _validate_source_ref(client, scene, source_ref, excerpt=source_excerpt)
    if location_key not in {str(item.get("key") or "") for item in _scene_locations(scene)}:
        raise ValueError("transfer-source-item location is not present in the scene atlas")

    actor = dict(
        _facade_value(
            await client.domain(
                "character_query",
                {"view": "get", "payload": {"character_id": normalized_character_id}},
            )
        )
    )
    party = dict(
        _facade_value(
            await client.core(
                "campaign_query",
                {
                    "view": "party",
                    "payload": {"campaign_id": campaign_id},
                    "principal_id": PRINCIPAL_ID,
                },
            )
        )
    )
    actor_item = next(
        (
            dict(item)
            for item in actor["sheet"]["inventory"]["items"]
            if str(item.get("id") or "") == normalized_item_id
        ),
        None,
    )
    recipient = (
        dict(
            _facade_value(
                await client.domain(
                    "character_query",
                    {"view": "get", "payload": {"character_id": normalized_recipient_id}},
                )
            )
        )
        if normalized_recipient_id
        else None
    )
    if recipient is not None and recipient.get("campaign_id") != campaign_id:
        raise ValueError("source item recipient must belong to the campaign")
    recipient_items = (
        recipient["sheet"]["inventory"]["items"]
        if recipient is not None
        else party["inventory"]["items"]
    )
    recipient_item = next(
        (dict(item) for item in recipient_items if str(item.get("id") or "") == normalized_item_id),
        None,
    )
    recovered = actor_item is None and recipient_item is not None
    if actor_item is None and not recovered:
        raise ValueError("source character does not carry the requested item")
    if actor_item is not None and recipient_item is not None:
        raise RuntimeError("source item id already exists in both source and recipient inventories")

    if recovered:
        transferred: dict[str, Any] = {
            "source": actor,
            "recipient": recipient or party,
            "item": recipient_item,
            "status": "recovered",
        }
    else:
        campaign = await _campaign(client, campaign_id)
        if recipient is not None:
            mode = "character_to_character"
            payload: dict[str, Any] = {
                "source_character_id": normalized_character_id,
                "target_character_id": normalized_recipient_id,
                "item_id": normalized_item_id,
                "expected_campaign_revision": campaign["revision"],
                "expected_source_revision": actor["revision"],
                "expected_target_revision": recipient["revision"],
            }
        else:
            mode = "character_to_party"
            payload = {
                "campaign_id": campaign_id,
                "character_id": normalized_character_id,
                "item_id": normalized_item_id,
                "expected_campaign_revision": campaign["revision"],
                "expected_character_revision": actor["revision"],
            }
        if quantity is not None:
            payload["quantity"] = quantity
        transferred = dict(
            _facade_value(
                await client.domain(
                    "inventory_transfer",
                    {
                        "mode": mode,
                        "payload": payload,
                        "idempotency_key": _mutation_key(
                            run_id,
                            "source-item-transfer",
                            transfer_identity,
                        ),
                    },
                )
            )
        )
        if str(dict(transferred.get("item") or {}).get("id") or "") != normalized_item_id:
            raise RuntimeError("source item transfer returned a different item")

    checkpoint = (
        None
        if defer_checkpoint
        else await _checkpoint(
            client,
            campaign_id=campaign_id,
            run_id=run_id,
            label=(
                checkpoint_label.strip()
                or f"Full playthrough source item transferred: {normalized_item_id}"
            ),
            checkpoint_id=(f"source-item-transfer:{transfer_identity}"),
        )
    )
    return {
        "character_id": normalized_character_id,
        "recipient_character_id": normalized_recipient_id or None,
        "item_id": normalized_item_id,
        "quantity": quantity,
        "occurrence_id": transfer_identity,
        "reason": normalized_reason,
        "source_ref": exact_ref,
        "transfer": transferred,
        "recovered": recovered,
        "checkpoint": checkpoint,
    }


async def _claim_party_item_for_character(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    occurrence_id: str,
    scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    character_id: str,
    item_id: str,
    quantity: int | None,
    reason: str,
    checkpoint_label: str,
    defer_checkpoint: bool = False,
) -> dict[str, Any]:
    claim_identity = _occurrence_identity(occurrence_id, "claim-party-item")
    normalized_character_id = character_id.strip()
    normalized_item_id = item_id.strip()
    normalized_reason = reason.strip()
    if not all(
        (
            scene_id,
            location_key,
            source_excerpt.strip(),
            normalized_character_id,
            normalized_item_id,
            normalized_reason,
        )
    ):
        raise ValueError(
            "claim-party-item requires scene, location, excerpt, character, item, and reason"
        )
    if quantity is not None and quantity <= 0:
        raise ValueError("claim-party-item quantity must be positive")

    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    exact_ref = await _validate_source_ref(client, scene, source_ref, excerpt=source_excerpt)
    if location_key not in {str(item.get("key") or "") for item in _scene_locations(scene)}:
        raise ValueError("claim-party-item location is not present in the scene atlas")

    actor = dict(
        _facade_value(
            await client.domain(
                "character_query",
                {"view": "get", "payload": {"character_id": normalized_character_id}},
            )
        )
    )
    party = dict(
        _facade_value(
            await client.core(
                "campaign_query",
                {
                    "view": "party",
                    "payload": {"campaign_id": campaign_id},
                    "principal_id": PRINCIPAL_ID,
                },
            )
        )
    )
    actor_item = next(
        (
            dict(item)
            for item in actor["sheet"]["inventory"]["items"]
            if str(item.get("id") or "") == normalized_item_id
        ),
        None,
    )
    party_item = next(
        (
            dict(item)
            for item in party["inventory"]["items"]
            if str(item.get("id") or "") == normalized_item_id
        ),
        None,
    )
    claim_key = _mutation_key(run_id, "party-item-claim", claim_identity)
    expected_quantity = (
        quantity
        if quantity is not None
        else int(dict(party_item or actor_item or {}).get("quantity", 0) or 0)
    )
    recovery_candidates: list[dict[str, Any]] = []
    if (
        party_item is not None
        and actor_item is None
        and expected_quantity > 0
        and str(party_item.get("source_key") or "")
    ):
        semantic_fields = (
            "name",
            "kind",
            "source_key",
            "description",
            "mechanics",
            "uses",
            "charges",
        )
        recovery_candidates = [
            dict(item)
            for item in actor["sheet"]["inventory"]["items"]
            if (
                str(item.get("id") or "") != normalized_item_id
                and int(item.get("quantity", 0) or 0) == expected_quantity
                and all(item.get(field) == party_item.get(field) for field in semantic_fields)
            )
        ]

    recovered = party_item is None and actor_item is not None
    recovered_receipt: dict[str, Any] | None = None
    if recovery_candidates:
        try:
            recovered_receipt = dict(
                _facade_value(
                    await client.domain(
                        "state_revision",
                        {
                            "campaign_id": campaign_id,
                            "action": "receipt",
                            "payload": {"idempotency_key": claim_key},
                        },
                    )
                )
            )
        except Exception as error:
            missing_receipt = any(
                message.startswith("idempotency receipt not found:")
                for message in exception_leaf_messages(error)
            )
            if not missing_receipt:
                raise
        if recovered_receipt is not None:
            receipt_response = dict(recovered_receipt.get("response") or {})
            receipt_item_id = str(dict(receipt_response.get("item") or {}).get("id") or "")
            actor_item = next(
                (
                    item
                    for item in recovery_candidates
                    if str(item.get("id") or "") == receipt_item_id
                ),
                None,
            )
            if actor_item is None:
                raise RuntimeError(
                    "party item claim receipt does not match the current character inventory"
                )
            recovered = True
    if party_item is None and not recovered:
        raise ValueError("party inventory does not carry the requested item")
    if actor_item is not None and party_item is not None:
        if not recovered:
            raise RuntimeError("claimed item id already exists in both inventories")

    if recovered:
        transferred: dict[str, Any] = dict(
            dict(recovered_receipt or {}).get("response")
            or {
                "party": party,
                "character": actor,
                "item": actor_item,
            }
        )
        transferred["status"] = "recovered"
    else:
        campaign = await _campaign(client, campaign_id)
        payload: dict[str, Any] = {
            "campaign_id": campaign_id,
            "character_id": normalized_character_id,
            "item_id": normalized_item_id,
            "expected_campaign_revision": campaign["revision"],
            "expected_character_revision": actor["revision"],
        }
        if quantity is not None:
            payload["quantity"] = quantity
        transferred = dict(
            _facade_value(
                await client.domain(
                    "inventory_transfer",
                    {
                        "mode": "party_to_character",
                        "payload": payload,
                        "idempotency_key": claim_key,
                    },
                )
            )
        )
        claimed_item = dict(transferred.get("item") or {})
        if (
            not str(claimed_item.get("id") or "")
            or int(claimed_item.get("quantity", 0) or 0) != expected_quantity
            or any(
                claimed_item.get(field) != party_item.get(field)
                for field in ("name", "kind", "source_key")
                if field in party_item
            )
        ):
            raise RuntimeError("party item claim returned a different item")

    checkpoint = (
        None
        if defer_checkpoint
        else await _checkpoint(
            client,
            campaign_id=campaign_id,
            run_id=run_id,
            label=(
                checkpoint_label.strip()
                or f"Full playthrough party item claimed: {normalized_item_id}"
            ),
            checkpoint_id=f"party-item-claim:{claim_identity}",
        )
    )
    return {
        "character_id": normalized_character_id,
        "item_id": normalized_item_id,
        "claimed_item_id": str(dict(transferred.get("item") or {}).get("id") or ""),
        "quantity": quantity,
        "occurrence_id": claim_identity,
        "reason": normalized_reason,
        "source_ref": exact_ref,
        "transfer": transferred,
        "recovered": recovered,
        "checkpoint": checkpoint,
    }


async def _pool_character_currency(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    occurrence_id: str,
    scene_id: str,
    source_scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    actor_id: str,
    denomination: str,
    amount: int | None,
    reason: str,
    defer_checkpoint: bool = False,
    direction: str = "from_character",
) -> dict[str, Any]:
    if direction not in {"from_character", "to_character"}:
        raise ValueError("currency transfer direction must be from_character or to_character")
    distributing = direction == "to_character"
    action_name = "distribute-coins" if distributing else "pool-coins"
    transfer_action = "transfer_to_character" if distributing else "transfer_from_character"
    state_key = (
        "full_playthrough_currency_distributions"
        if distributing
        else "full_playthrough_currency_pools"
    )
    event_type = "currency_distributed" if distributing else "currency_pooled"
    identity_label = "currency-distribution" if distributing else "currency-pool"
    pool_identity = _occurrence_identity(occurrence_id, action_name)
    normalized_actor_id = actor_id.strip()
    normalized_denomination = denomination.strip().lower()
    normalized_reason = reason.strip()
    cited_scene_id = source_scene_id.strip() or scene_id
    if not all(
        (
            scene_id,
            location_key,
            source_excerpt.strip(),
            normalized_actor_id,
            normalized_denomination,
            normalized_reason,
        )
    ):
        raise ValueError(
            f"{action_name} requires scene, location, excerpt, actor, denomination, and reason"
        )
    if normalized_denomination not in DENOMINATIONS:
        raise ValueError(f"{action_name} denomination must be cp, sp, ep, gp, or pp")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ValueError(f"{action_name} amount must be a positive integer")

    occurrence_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    source_scene = (
        occurrence_scene
        if cited_scene_id == scene_id
        else await client.domain(
            "module_query",
            {
                "campaign_id": campaign_id,
                "view": "scene",
                "payload": {"scene_id": cited_scene_id},
            },
        )
    )
    exact_ref = await _validate_source_ref(client, source_scene, source_ref, excerpt=source_excerpt)
    if location_key not in {
        str(item.get("key") or "") for item in _scene_locations(occurrence_scene)
    }:
        raise ValueError(f"{action_name} location is not present in the occurrence scene atlas")

    progress_rows = await client.domain(
        "module_query",
        {"campaign_id": campaign_id, "view": "progress"},
    )
    progress_before = next(
        (
            item
            for item in progress_rows
            if str(item.get("scene_id") or "") == scene_id
            and str(item.get("scope_id") or "") == "party"
        ),
        None,
    )
    state_before = deepcopy(dict((progress_before or {}).get("state") or {}))
    pools_before = deepcopy(dict(state_before.get(state_key) or {}))
    identity_token = _token(pool_identity)
    pool_details = {
        "occurrence_id": pool_identity,
        "actor_id": normalized_actor_id,
        "denomination": normalized_denomination,
        "amount": amount,
        "reason": normalized_reason,
        "source_ref": exact_ref,
    }
    existing_pool = pools_before.get(identity_token)
    if existing_pool is not None:
        if not isinstance(existing_pool, dict) or any(
            existing_pool.get(key) != value for key, value in pool_details.items()
        ):
            raise ValueError(f"{action_name} occurrence id already exists with different details")
        if str(existing_pool.get("status") or "") == "completed":
            return {
                "scene": {
                    "scene_id": scene_id,
                    "source_scene_id": cited_scene_id,
                    "location_key": location_key,
                    "source_ref": exact_ref,
                },
                "occurrence_id": pool_identity,
                "actor_id": normalized_actor_id,
                "denomination": normalized_denomination,
                "amount": amount,
                "reason": normalized_reason,
                "transfer": None,
                "progress": progress_before,
                "continuity": None,
                "sync": None,
                "recovered": True,
            }
        if (
            str(existing_pool.get("status") or "") != "planned"
            or not isinstance(existing_pool.get("expected_campaign_revision"), int)
            or not isinstance(existing_pool.get("expected_character_revision"), int)
        ):
            raise RuntimeError(f"{action_name} progress contains an invalid planned transfer")

    actor = dict(
        _facade_value(
            await client.domain(
                "character_query",
                {"view": "get", "payload": {"character_id": normalized_actor_id}},
            )
        )
    )
    if str(actor.get("campaign_id") or "") != campaign_id:
        raise ValueError(f"{action_name} actor must belong to the campaign")
    campaign = await _campaign(client, campaign_id)
    idempotency_key = _mutation_key(run_id, identity_label, pool_identity)

    if existing_pool is None:
        planned_pool = {
            **pool_details,
            "status": "planned",
            "expected_campaign_revision": int(campaign["revision"]),
            "expected_character_revision": int(actor["revision"]),
        }
        planned_state = deepcopy(state_before)
        planned_pools = deepcopy(pools_before)
        planned_pools[identity_token] = planned_pool
        planned_state[state_key] = planned_pools
        progress_planned = await client.domain(
            "module_set_progress",
            {
                "campaign_id": campaign_id,
                "scene_id": scene_id,
                "status": str((progress_before or {}).get("status") or "active"),
                "progress": _scene_progress_percent(progress_before),
                "state": planned_state,
                "current_location_key": location_key,
                "expected_state_version": int((progress_before or {}).get("state_version", 0) or 0),
                "idempotency_key": _mutation_key(
                    run_id, f"{identity_label}-progress-plan", pool_identity
                ),
            },
        )
    else:
        planned_pool = deepcopy(existing_pool)
        progress_planned = progress_before
        if int(planned_pool["expected_campaign_revision"]) == int(campaign["revision"]) + 1 and int(
            planned_pool["expected_character_revision"]
        ) == int(actor["revision"]):
            # Scene progress has its own state_version and does not mutate the
            # campaign revision. Recover plans written by the former +1
            # assumption before retrying the public atomic wallet transfer.
            planned_pool["expected_campaign_revision"] = int(campaign["revision"])
            planned_state = deepcopy(state_before)
            planned_pools = deepcopy(pools_before)
            planned_pools[identity_token] = planned_pool
            planned_state[state_key] = planned_pools
            progress_planned = await client.domain(
                "module_set_progress",
                {
                    "campaign_id": campaign_id,
                    "scene_id": scene_id,
                    "status": str((progress_before or {}).get("status") or "active"),
                    "progress": _scene_progress_percent(progress_before),
                    "state": planned_state,
                    "current_location_key": location_key,
                    "expected_state_version": int(
                        (progress_before or {}).get("state_version", 0) or 0
                    ),
                    "idempotency_key": _mutation_key(
                        run_id,
                        f"{identity_label}-progress-rebase",
                        (f"{pool_identity}:c{campaign['revision']}:a{actor['revision']}"),
                    ),
                },
            )

    transferred = dict(
        _facade_value(
            await client.domain(
                "wallet_change",
                {
                    "owner": "party",
                    "action": transfer_action,
                    "owner_id": campaign_id,
                    "denomination": normalized_denomination,
                    "amount": amount,
                    "payload": {
                        "character_id": normalized_actor_id,
                        "expected_campaign_revision": planned_pool["expected_campaign_revision"],
                        "expected_character_revision": planned_pool["expected_character_revision"],
                    },
                    "idempotency_key": idempotency_key,
                },
            )
        )
    )

    completed_state = deepcopy(dict((progress_planned or {}).get("state") or {}))
    completed_pools = deepcopy(dict(completed_state.get(state_key) or {}))
    completed_pools[identity_token] = {**planned_pool, "status": "completed"}
    completed_state[state_key] = completed_pools
    progress = await client.domain(
        "module_set_progress",
        {
            "campaign_id": campaign_id,
            "scene_id": scene_id,
            "status": str((progress_planned or {}).get("status") or "active"),
            "progress": _scene_progress_percent(progress_planned),
            "state": completed_state,
            "current_location_key": location_key,
            "expected_state_version": int((progress_planned or {}).get("state_version", 0) or 0),
            "idempotency_key": _mutation_key(
                run_id, f"{identity_label}-progress-complete", pool_identity
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
    continuity_payload: dict[str, Any] = {
        "event": {
            "summary": normalized_reason,
            "event_type": event_type,
            "audience_scope": "party",
            "payload": {
                "scene_id": scene_id,
                "source_scene_id": cited_scene_id,
                "location_key": location_key,
                "occurrence_id": pool_identity,
                "actor_id": normalized_actor_id,
                "denomination": normalized_denomination,
                "amount": amount,
                "source_excerpt": source_excerpt.strip(),
                "source_ref": exact_ref,
            },
        },
        "actor_knowledge": [
            {
                "actor_id": normalized_actor_id,
                "knowledge_key": (
                    f"playthrough.{_token(run_id)}.{_token(identity_label)}.{identity_token}"
                ),
                "proposition": normalized_reason,
                "disclosure_scope": "owner",
            }
        ],
        "branch_id": str(branch["id"]),
    }
    if not defer_checkpoint:
        continuity_payload["snapshot"] = {
            "label": (
                f"Full playthrough currency "
                f"{'distributed' if distributing else 'pooled'}: {normalized_reason}"
            )
        }
    committed = await client.domain(
        "memory_change",
        {
            "campaign_id": campaign_id,
            "action": "commit",
            "payload": continuity_payload,
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(run_id, f"{identity_label}-continuity", pool_identity),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"{identity_label}-sync:{pool_identity}",
    )
    return {
        "scene": {
            "scene_id": scene_id,
            "source_scene_id": cited_scene_id,
            "location_key": location_key,
            "source_ref": exact_ref,
        },
        "occurrence_id": pool_identity,
        "actor_id": normalized_actor_id,
        "denomination": normalized_denomination,
        "amount": amount,
        "direction": direction,
        "reason": normalized_reason,
        "transfer": transferred,
        "progress": progress,
        "continuity": committed,
        "sync": synced,
        "recovered": existing_pool is not None,
    }


async def _apply_source_effect(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    occurrence_id: str,
    scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    character_id: str,
    effect: dict[str, Any] | None,
    reason: str,
    checkpoint_label: str,
    source_scene_id: str = "",
    defer_checkpoint: bool = False,
) -> dict[str, Any]:
    application_identity = _occurrence_identity(occurrence_id, "apply-source-effect")
    normalized_character_id = character_id.strip()
    normalized_reason = reason.strip()
    requested_effect = deepcopy(effect) if isinstance(effect, dict) else {}
    effect_id = str(requested_effect.get("id") or "").strip()
    if not all(
        (
            scene_id,
            location_key,
            source_excerpt.strip(),
            normalized_character_id,
            effect_id,
            normalized_reason,
        )
    ):
        raise ValueError(
            "apply-source-effect requires scene, location, excerpt, character, "
            "an effect with id, and reason"
        )

    cited_scene_id = source_scene_id.strip() or scene_id
    source_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": cited_scene_id},
        },
    )
    exact_ref = await _validate_source_ref(client, source_scene, source_ref, excerpt=source_excerpt)
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
        raise ValueError("apply-source-effect location is not present in the scene atlas")
    expected_source = f"module-chunk:{exact_ref['chunk_id']}"
    if str(requested_effect.get("source") or "") != expected_source:
        raise ValueError("source effect source must be module-chunk:<source_ref.chunk_id>")

    actor = dict(
        _facade_value(
            await client.domain(
                "character_query",
                {"view": "get", "payload": {"character_id": normalized_character_id}},
            )
        )
    )
    if str(actor.get("campaign_id") or "") != campaign_id:
        raise ValueError("apply-source-effect actor does not belong to the campaign")
    existing = next(
        (
            dict(item)
            for item in dict(actor.get("sheet") or {}).get("effects", [])
            if str(item.get("id") or "") == effect_id
        ),
        None,
    )
    recovered = existing is not None
    if recovered:
        if any(
            existing.get(field) != requested_effect.get(field)
            for field in ("id", "name", "kind", "source", "duration", "changes")
        ):
            raise ValueError("apply-source-effect id already exists with different effect data")
        applied: dict[str, Any] = {
            "character": actor,
            "effect_id": effect_id,
            "status": "recovered",
        }
    else:
        applied = dict(
            _facade_value(
                await client.domain(
                    "character_state_change",
                    {
                        "character_id": normalized_character_id,
                        "action": "effect_add",
                        "payload": {"effect": requested_effect},
                        "expected_revision": actor["revision"],
                        "idempotency_key": _mutation_key(
                            run_id,
                            "source-effect-add",
                            application_identity,
                        ),
                    },
                )
            )
        )
        actor_after = dict(applied.get("character") or applied)
        added = next(
            (
                dict(item)
                for item in dict(actor_after.get("sheet") or {}).get("effects", [])
                if str(item.get("id") or "") == effect_id
            ),
            None,
        )
        if added is None:
            raise RuntimeError("source effect application did not add the requested effect")
        existing = added

    checkpoint = (
        None
        if defer_checkpoint
        else await _checkpoint(
            client,
            campaign_id=campaign_id,
            run_id=run_id,
            label=(
                checkpoint_label.strip() or f"Full playthrough source effect applied: {effect_id}"
            ),
            checkpoint_id=f"source-effect-add:{application_identity}",
        )
    )
    return {
        "character_id": normalized_character_id,
        "effect_id": effect_id,
        "occurrence_id": application_identity,
        "reason": normalized_reason,
        "scene_id": scene_id,
        "source_scene_id": cited_scene_id,
        "location_key": location_key,
        "source_ref": exact_ref,
        "effect": existing,
        "application": applied,
        "recovered": recovered,
        "checkpoint": checkpoint,
    }


async def _remove_source_effect(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    occurrence_id: str,
    scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    character_id: str,
    effect_id: str,
    reason: str,
    checkpoint_label: str,
    source_scene_id: str = "",
    defer_checkpoint: bool = False,
) -> dict[str, Any]:
    removal_identity = _occurrence_identity(occurrence_id, "remove-source-effect")
    normalized_character_id = character_id.strip()
    normalized_effect_id = effect_id.strip()
    normalized_reason = reason.strip()
    if not all(
        (
            scene_id,
            location_key,
            source_excerpt.strip(),
            normalized_character_id,
            normalized_effect_id,
            normalized_reason,
        )
    ):
        raise ValueError(
            "remove-source-effect requires scene, location, excerpt, character, effect, and reason"
        )

    cited_scene_id = source_scene_id.strip() or scene_id
    source_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": cited_scene_id},
        },
    )
    exact_ref = await _validate_source_ref(client, source_scene, source_ref, excerpt=source_excerpt)
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
        raise ValueError("remove-source-effect location is not present in the scene atlas")

    actor = dict(
        _facade_value(
            await client.domain(
                "character_query",
                {"view": "get", "payload": {"character_id": normalized_character_id}},
            )
        )
    )
    if str(actor.get("campaign_id") or "") != campaign_id:
        raise ValueError("remove-source-effect actor does not belong to the campaign")
    effect = next(
        (
            dict(item)
            for item in dict(actor.get("sheet") or {}).get("effects", [])
            if str(item.get("id") or "") == normalized_effect_id
        ),
        None,
    )
    recovered = effect is None
    if recovered:
        removed: dict[str, Any] = {
            "character": actor,
            "status": "recovered",
        }
    else:
        removed = dict(
            _facade_value(
                await client.domain(
                    "character_state_change",
                    {
                        "character_id": normalized_character_id,
                        "action": "effect_remove",
                        "payload": {"effect_id": normalized_effect_id},
                        "expected_revision": actor["revision"],
                        "idempotency_key": _mutation_key(
                            run_id,
                            "source-effect-remove",
                            removal_identity,
                        ),
                    },
                )
            )
        )
        removed_character = dict(removed.get("character") or removed)
        remaining_ids = {
            str(item.get("id") or "")
            for item in dict(removed_character.get("sheet") or {}).get("effects", [])
        }
        if normalized_effect_id in remaining_ids:
            raise RuntimeError("source effect removal did not remove the requested effect")

    checkpoint = (
        None
        if defer_checkpoint
        else await _checkpoint(
            client,
            campaign_id=campaign_id,
            run_id=run_id,
            label=(
                checkpoint_label.strip()
                or f"Full playthrough source effect removed: {normalized_effect_id}"
            ),
            checkpoint_id=f"source-effect-remove:{removal_identity}",
        )
    )
    return {
        "character_id": normalized_character_id,
        "effect_id": normalized_effect_id,
        "occurrence_id": removal_identity,
        "reason": normalized_reason,
        "scene_id": scene_id,
        "source_scene_id": cited_scene_id,
        "location_key": location_key,
        "source_ref": exact_ref,
        "effect": effect,
        "removal": removed,
        "recovered": recovered,
        "checkpoint": checkpoint,
    }


async def _set_source_exhaustion(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    occurrence_id: str,
    scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    character_id: str,
    level: int | None,
    reason: str,
    checkpoint_label: str,
    defer_checkpoint: bool = False,
) -> dict[str, Any]:
    exhaustion_identity = _occurrence_identity(occurrence_id, "set-source-exhaustion")
    normalized_character_id = character_id.strip()
    normalized_reason = reason.strip()
    if not all(
        (
            scene_id,
            location_key,
            source_excerpt.strip(),
            normalized_character_id,
            normalized_reason,
        )
    ):
        raise ValueError(
            "set-source-exhaustion requires scene, location, excerpt, character, level, and reason"
        )
    if level is None or not 0 <= level <= 6:
        raise ValueError("set-source-exhaustion level must be between 0 and 6")

    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    exact_ref = await _validate_source_ref(client, scene, source_ref, excerpt=source_excerpt)
    if location_key not in {str(item.get("key") or "") for item in _scene_locations(scene)}:
        raise ValueError("set-source-exhaustion location is not present in the scene atlas")
    actor = dict(
        _facade_value(
            await client.domain(
                "character_query",
                {"view": "get", "payload": {"character_id": normalized_character_id}},
            )
        )
    )
    if str(actor.get("campaign_id") or "") != campaign_id:
        raise ValueError("set-source-exhaustion actor does not belong to the campaign")
    before = int(dict(actor["sheet"].get("combat") or {}).get("exhaustion", 0) or 0)
    recovered = before == level
    if recovered:
        changed: dict[str, Any] = {
            "character": actor,
            "status": "recovered",
        }
    else:
        changed = dict(
            _facade_value(
                await client.domain(
                    "character_state_change",
                    {
                        "character_id": normalized_character_id,
                        "action": "exhaustion_set",
                        "payload": {"value": level},
                        "expected_revision": actor["revision"],
                        "idempotency_key": _mutation_key(
                            run_id,
                            "source-exhaustion-set",
                            exhaustion_identity,
                        ),
                    },
                )
            )
        )
        actor_after = dict(changed.get("character") or changed)
        after = int(dict(actor_after["sheet"].get("combat") or {}).get("exhaustion", 0) or 0)
        if after != level:
            raise RuntimeError("source exhaustion update did not set the requested level")

    checkpoint = (
        None
        if defer_checkpoint
        else await _checkpoint(
            client,
            campaign_id=campaign_id,
            run_id=run_id,
            label=(
                checkpoint_label.strip()
                or f"Full playthrough source exhaustion: {normalized_character_id}={level}"
            ),
            checkpoint_id=f"source-exhaustion:{exhaustion_identity}",
        )
    )
    return {
        "character_id": normalized_character_id,
        "occurrence_id": exhaustion_identity,
        "before": before,
        "after": level,
        "reason": normalized_reason,
        "source_ref": exact_ref,
        "change": changed,
        "recovered": recovered,
        "checkpoint": checkpoint,
    }


async def _attack_source_object(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    occurrence_id: str,
    scene_id: str,
    location_key: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
    character_id: str,
    object_state: dict[str, Any] | None,
    weapon_id: str,
    reason: str,
    advantage: bool,
    disadvantage: bool,
    checkpoint_label: str,
    defer_checkpoint: bool = False,
) -> dict[str, Any]:
    attack_identity = _occurrence_identity(occurrence_id, "attack-source-object")
    normalized_character_id = character_id.strip()
    normalized_weapon_id = weapon_id.strip()
    normalized_reason = reason.strip()
    requested_object = deepcopy(object_state) if isinstance(object_state, dict) else {}
    if not all(
        (
            scene_id,
            location_key,
            source_excerpt.strip(),
            normalized_character_id,
            normalized_weapon_id,
            str(requested_object.get("id") or "").strip(),
            normalized_reason,
        )
    ):
        raise ValueError(
            "attack-source-object requires scene, location, excerpt, character, "
            "object, weapon, and reason"
        )
    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    exact_ref = await _validate_source_ref(client, scene, source_ref, excerpt=source_excerpt)
    if location_key not in {str(item.get("key") or "") for item in _scene_locations(scene)}:
        raise ValueError("attack-source-object location is not present in the scene atlas")
    if str(requested_object.get("scene_id") or "") != scene_id:
        raise ValueError("attack-source-object object scene_id must match the cited scene")
    actor = dict(
        _facade_value(
            await client.domain(
                "character_query",
                {"view": "get", "payload": {"character_id": normalized_character_id}},
            )
        )
    )
    if str(actor.get("campaign_id") or "") != campaign_id:
        raise ValueError("attack-source-object actor does not belong to the campaign")
    campaign = await _campaign(client, campaign_id)
    attacked = dict(
        _facade_value(
            await client.domain(
                "character_action",
                {
                    "character_id": normalized_character_id,
                    "action": "attack_source_object",
                    "payload": {
                        "object": requested_object,
                        "weapon_id": normalized_weapon_id,
                        "source_ref": exact_ref,
                        "reason": normalized_reason,
                        "advantage": advantage,
                        "disadvantage": disadvantage,
                        "expected_campaign_revision": campaign["revision"],
                    },
                    "expected_revision": actor["revision"],
                    "idempotency_key": _mutation_key(
                        run_id,
                        "source-object-attack",
                        attack_identity,
                    ),
                },
            )
        )
    )
    if attacked.get("status") != "committed":
        raise RuntimeError("source object attack did not commit")
    settled_object = dict(attacked.get("object") or {})
    if str(settled_object.get("id") or "") != str(requested_object["id"]):
        raise RuntimeError("source object attack returned the wrong object")
    checkpoint = (
        None
        if defer_checkpoint
        else await _checkpoint(
            client,
            campaign_id=campaign_id,
            run_id=run_id,
            label=(
                checkpoint_label.strip()
                or f"Full playthrough source object attack: {requested_object['id']}"
            ),
            checkpoint_id=f"source-object-attack:{attack_identity}",
        )
    )
    return {
        "character_id": normalized_character_id,
        "weapon_id": normalized_weapon_id,
        "occurrence_id": attack_identity,
        "reason": normalized_reason,
        "source_ref": exact_ref,
        "attack": attacked,
        "object": settled_object,
        "checkpoint": checkpoint,
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
    defer_checkpoint: bool = False,
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
    item_ids = [str(item.get("id") or "").strip() for item in items]
    if any(not item_id for item_id in item_ids) or len(item_ids) != len(set(item_ids)):
        raise ValueError("acquire-loot items require unique non-empty ids")
    for index, item in enumerate(items):
        item_kind = str(item.get("kind") or "").strip()
        if item_kind == "weapon":
            mechanics = item.get("mechanics")
            if not isinstance(mechanics, dict) or not isinstance(mechanics.get("proficient"), bool):
                raise ValueError(
                    f"acquire-loot weapon item {index} requires explicit boolean "
                    "mechanics.proficient"
                )
        if item_kind != "spellbook":
            continue
        mechanics = item.get("mechanics")
        if not isinstance(mechanics, dict):
            raise ValueError(f"acquire-loot spellbook item {index} requires explicit mechanics")
        required_spellbook_fields = {
            "spell_ids",
            "unresolved_spell_names",
            "source_scene_id",
        }
        missing = sorted(required_spellbook_fields - set(mechanics))
        if missing:
            raise ValueError(f"acquire-loot spellbook item {index} mechanics is missing {missing}")
        if not isinstance(mechanics["spell_ids"], list) or not isinstance(
            mechanics["unresolved_spell_names"], list
        ):
            raise ValueError(f"acquire-loot spellbook item {index} spell contents must be arrays")
        if not str(mechanics["source_scene_id"] or "").strip():
            raise ValueError(f"acquire-loot spellbook item {index} requires a source scene id")

    source_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": cited_scene_id},
        },
    )
    exact_ref = await _validate_source_ref(client, source_scene, source_ref, excerpt=source_excerpt)
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
    continuity_payload = {
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
        "branch_id": str(branch["id"]),
    }
    if not defer_checkpoint:
        continuity_payload["snapshot"] = {
            "label": f"Full playthrough loot: {normalized_acquisition_id}"
        }
    committed = await client.domain(
        "memory_change",
        {
            "campaign_id": campaign_id,
            "action": "commit",
            "payload": continuity_payload,
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
    defer_checkpoint: bool = False,
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
    exact_ref = await _validate_source_ref(client, source_scene, source_ref, excerpt=source_excerpt)
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
                "idempotency_key": _mutation_key(run_id, "currency-spend", normalized_spend_id),
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
    continuity_payload = {
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
        "branch_id": str(branch["id"]),
    }
    if not defer_checkpoint:
        continuity_payload["snapshot"] = {
            "label": f"Full playthrough currency spend: {normalized_spend_id}"
        }
    committed = await client.domain(
        "memory_change",
        {
            "campaign_id": campaign_id,
            "action": "commit",
            "payload": continuity_payload,
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
    character_id: str = "",
    source_scene_id: str = "",
    defer_checkpoint: bool = False,
) -> dict[str, Any]:
    normalized_spend_id = spend_id.strip()
    normalized_item_id = item_id.strip()
    normalized_reason = reason.strip()
    normalized_character_id = character_id.strip()
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
    if normalized_character_id and normalized_character_id not in recipients:
        raise ValueError("a character item owner must be one of the knowledge recipients")

    source_scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": cited_scene_id},
        },
    )
    exact_ref = await _validate_source_ref(client, source_scene, source_ref, excerpt=source_excerpt)
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
    character = None
    if normalized_character_id:
        character = await client.domain(
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": normalized_character_id},
            },
        )
        if character.get("campaign_id") != campaign_id:
            raise ValueError("spend-item character owner does not belong to the campaign")
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
            "character_id": normalized_character_id or None,
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
            "character_id": normalized_character_id or None,
            "owner": deepcopy(prior.get("owner") or {}),
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
                    **(
                        {
                            "character_id": normalized_character_id,
                            "expected_character_revision": int(character["revision"]),
                        }
                        if character is not None
                        else {}
                    ),
                },
                "expected_revision": campaign["revision"],
                "idempotency_key": _mutation_key(run_id, "item-spend", normalized_spend_id),
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
    continuity_payload = {
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
                **({"character_id": normalized_character_id} if normalized_character_id else {}),
                "removed": deepcopy(spent.get("removed") or {}),
                "source_excerpt": source_excerpt.strip(),
                "source_ref": exact_ref,
            },
        },
        "actor_knowledge": [
            {
                "actor_id": actor_id,
                "knowledge_key": (
                    f"playthrough.{_token(run_id)}.item-spend.{_token(normalized_spend_id)}"
                ),
                "proposition": normalized_reason,
                "disclosure_scope": "owner",
            }
            for actor_id in recipients
        ],
        "branch_id": str(branch["id"]),
    }
    if not defer_checkpoint:
        continuity_payload["snapshot"] = {
            "label": f"Full playthrough item spend: {normalized_spend_id}"
        }
    committed = await client.domain(
        "memory_change",
        {
            "campaign_id": campaign_id,
            "action": "commit",
            "payload": continuity_payload,
            "expected_revision": campaign["revision"],
            "idempotency_key": _mutation_key(run_id, "item-spend-continuity", normalized_spend_id),
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
    defer_checkpoint: bool = False,
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
    continuity_payload = {
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
        "branch_id": str(branch["id"]),
    }
    if not defer_checkpoint:
        continuity_payload["snapshot"] = {
            "label": f"Full playthrough consumable: {normalized_use_id}"
        }
    committed = await client.domain(
        "memory_change",
        {
            "campaign_id": campaign_id,
            "action": "commit",
            "payload": continuity_payload,
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
    occurrence_id: str,
    scene_id: str,
    source_ref: dict[str, Any] | None,
    actor_ids: list[str],
    amount: int | None,
    reason: str,
) -> dict[str, Any]:
    award_identity = _occurrence_identity(occurrence_id, "award-xp")
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
    exact_ref = await _validate_source_ref(client, scene, source_ref)
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
            "idempotency_key": _mutation_key(
                run_id,
                "experience-award",
                award_identity,
            ),
        },
    )
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"award-xp-sync:{award_identity}",
    )
    return {
        "occurrence_id": award_identity,
        "scene_id": scene_id,
        "source_ref": exact_ref,
        "award": awarded,
        "sync": synced,
    }


async def _configure_ending_conditions(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    conditions: list[dict[str, Any]],
) -> dict[str, Any]:
    if not conditions:
        raise ValueError("configure-ending requires at least one --ending-condition-json")
    current = await _manifest_get(client, campaign_id)
    manifest = deepcopy(dict(current["manifest"]))
    if manifest["status"] == "completed":
        raise RuntimeError("completed playthrough ending conditions cannot be changed")

    existing = {
        str(item["id"]): deepcopy(item)
        for item in list(dict(manifest["ending"]).get("conditions") or [])
    }
    for index, raw in enumerate(conditions):
        if not isinstance(raw, dict):
            raise ValueError(f"ending-condition-json[{index}] must be an object")
        condition = validate_source_defined_ending_condition(raw)
        condition_id = str(condition.get("id") or "").strip()
        if not condition_id:
            raise ValueError(f"ending-condition-json[{index}] requires id")
        previous = existing.get(condition_id)
        if previous is not None and previous != condition:
            raise ValueError(
                f"ending condition {condition_id} already exists with different content"
            )
        existing[condition_id] = condition

    manifest["ending"]["conditions"] = list(existing.values())
    manifest = validate_playthrough_manifest(manifest)
    identity_source = json.dumps(
        conditions,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    identity = hashlib.sha256(identity_source.encode("utf-8")).hexdigest()[:24]
    return await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="replace",
        run_id=run_id,
        identity=f"configure-ending:{identity}",
        payload={"manifest": manifest},
    )


async def _start_play(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    initial_phase: str,
    scene_id: str,
    source_excerpt: str,
    source_ref: dict[str, Any] | None,
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
    await client.load()
    scene = await _advance_scene(
        client,
        campaign_id=campaign_id,
        run_id=run_id,
        occurrence_id=f"start-play:{scene_id}",
        scene_id=scene_id,
        source_scene_id=scene_id,
        source_excerpt=source_excerpt,
        source_ref=source_ref,
        objective=objective,
        mark_visited=True,
        reachable_scene_ids=reachable_scene_ids,
        excluded_scenes=[],
    )
    sync_receipt = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"start-play-sync:{scene_id}",
    )
    synced = await _manifest_get(client, campaign_id)
    return {
        "ready": ready,
        "phase_change": phase_change,
        "scene": scene,
        "sync": synced,
        "sync_receipt": sync_receipt,
    }


async def _configure_advancement(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    mode: str,
    initial_phase: str,
) -> dict[str, Any]:
    if mode not in ADVANCEMENT_MODES:
        raise ValueError("configure-advancement requires --advancement-mode")
    phase_changes: list[dict[str, Any]] = []
    branch_id = ""
    if initial_phase == "play":
        await client.load()
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
        await client.load()
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
    return json.dumps(
        source_ref,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
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


def _level_spell_choice_counts(
    spell_selections: list[dict[str, str]],
    *,
    spell_by_id: dict[str, dict[str, Any]],
    class_name: str,
    preparation_mode: str,
    maximum_spell_level: int,
) -> tuple[int, int, list[str]]:
    """Count advancement choices while auditing prepared-caster catalog hydration."""
    selected_cantrips = 0
    selected_leveled = 0
    prepared_additions: list[str] = []
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
        if class_name.casefold() not in eligible_classes:
            raise ValueError("selected level spell is not eligible for the advanced class")
        spell_level = int(requirements.get("level", 0) or 0)
        method = selection["method"]
        if method == "class_prepared":
            if preparation_mode != "prepared":
                raise ValueError(
                    "class_prepared level spells require a prepared-caster configuration"
                )
            if spell_level < 1:
                raise ValueError("prepared-caster cantrips must use the known method")
            if spell_level > maximum_spell_level:
                raise ValueError(
                    "class_prepared level spell exceeds the actor's available spell slots"
                )
            # This lazily materializes legal class-list access on the actor
            # card. It does not prepare the spell; 2014 preparation waits for
            # the next completed long rest.
            prepared_additions.append(selection["artifact_id"])
            continue
        if spell_level == 0:
            selected_cantrips += 1
            if method != "known":
                raise ValueError("selected cantrips must use the known method")
        else:
            selected_leveled += 1
    return selected_cantrips, selected_leveled, prepared_additions


def _required_level_features(
    *,
    follow_up: dict[str, Any],
    feature_catalog: list[dict[str, Any]],
    actor_sheet: dict[str, Any],
    class_name: str,
    subclass_name: str,
    target_level: int,
) -> dict[str, dict[str, Any]]:
    """Preserve the server's dependency order while adding catch-up features."""
    required: dict[str, dict[str, Any]] = {
        str(item["artifact_id"]): dict(item) for item in follow_up.get("feature_artifacts") or []
    }
    existing_feature_ids = {
        str(item.get("id") or "")
        for item in dict(actor_sheet.get("content") or {}).get("features", [])
    }
    for item in feature_catalog:
        catalog_requirements = dict(item.get("selection_requirements") or {})
        by_level = dict(catalog_requirements.get("selection_requirements_by_level") or {})
        level_requirements = dict(by_level.get(str(target_level)) or catalog_requirements)
        artifact_id = str(item.get("id") or "")
        repeatable_levels = {
            int(value) for value in catalog_requirements.get("repeatable_selection_levels", [])
        }
        repeat_due = target_level in repeatable_levels
        if (
            artifact_id
            and (artifact_id not in existing_feature_ids or repeat_due)
            and str(catalog_requirements.get("class_name") or "").casefold()
            == class_name.casefold()
            and int(catalog_requirements.get("minimum_level", 1) or 1) <= target_level
            and (
                not str(catalog_requirements.get("subclass_name") or "")
                or str(catalog_requirements.get("subclass_name") or "").casefold()
                == subclass_name.casefold()
            )
        ):
            required.setdefault(
                artifact_id,
                {
                    "artifact_id": artifact_id,
                    "name": str(item.get("name") or artifact_id),
                    "selection_requirements": level_requirements,
                    "grant_level": target_level if repeat_due else None,
                },
            )
    return required


def _validate_level_feature_completion(
    required_features: dict[str, dict[str, Any]],
    feature_selections: dict[str, dict[str, Any]],
) -> None:
    unknown = set(feature_selections) - set(required_features)
    if unknown:
        raise ValueError(
            "feature selections were supplied for artifacts not required at this level: "
            + ", ".join(sorted(unknown))
        )
    for artifact_id, feature in required_features.items():
        requirements = dict(feature.get("selection_requirements") or {})
        choice_field = str(requirements.get("field") or "")
        selection = feature_selections.get(artifact_id, {})
        if choice_field and choice_field not in selection:
            allowed = [str(item) for item in requirements.get("options") or []]
            suffix = f"; allowed choices: {', '.join(allowed)}" if allowed else ""
            raise ValueError(
                f"level feature {artifact_id} requires an explicit {choice_field} choice{suffix}"
            )
        allowed = [str(item) for item in requirements.get("options") or []]
        if not choice_field or not allowed:
            continue
        selected = selection[choice_field]
        selected_values = (
            [str(item) for item in selected] if isinstance(selected, list) else [str(selected)]
        )
        invalid = [item for item in selected_values if item not in allowed]
        if invalid:
            raise ValueError(
                f"level feature {artifact_id} has invalid {choice_field} "
                f"choice(s): {', '.join(invalid)}; allowed choices: " + ", ".join(allowed)
            )
        expected_count = int(requirements.get("count", 1) or 1)
        if len(selected_values) != expected_count:
            raise ValueError(
                f"level feature {artifact_id} requires exactly {expected_count} "
                f"{choice_field} choice(s); got {len(selected_values)}"
            )


async def _preflight_level_completion(
    client: ExposureClient,
    *,
    campaign_id: str,
    actor: dict[str, Any],
    class_name: str,
    target_level: int,
    subclass_artifact_id: str,
    feature_selections: dict[str, dict[str, Any]],
    spell_selections: list[dict[str, str]],
    prepared_spell_ids: list[str],
) -> dict[str, Any]:
    plan = await client.domain(
        "character_query",
        {
            "view": "advancement",
            "payload": {
                "character_id": actor["id"],
                "class_name": class_name,
            },
        },
    )
    if (
        plan.get("status") != "ready"
        or int(plan.get("character_revision", -1)) != int(actor["revision"])
        or int(plan.get("new_level", 0) or 0) != target_level
    ):
        raise ValueError("level advancement plan is not ready for the requested target")
    follow_up = dict(plan.get("follow_up") or {})
    subclass_options = list(follow_up.get("subclass_options") or [])
    selected_subclass = None
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
    elif subclass_artifact_id:
        raise ValueError("this level advancement does not offer a subclass selection")

    feature_catalog = list(
        _facade_value(
            await client.domain(
                "content_pack",
                {
                    "action": "list",
                    "payload": {
                        "campaign_id": campaign_id,
                        "kind": "catalog",
                        "content_kind": "feature",
                    },
                },
            )
        )
    )
    actor_class = next(
        item
        for item in actor["sheet"]["progression"]["classes"]
        if str(item.get("name") or "").casefold() == class_name.casefold()
    )
    planned_subclass = str(
        (selected_subclass or {}).get("name") or actor_class.get("subclass") or ""
    )
    required_features = _required_level_features(
        follow_up=follow_up,
        feature_catalog=feature_catalog,
        actor_sheet=actor["sheet"],
        class_name=class_name,
        subclass_name=planned_subclass,
        target_level=target_level,
    )
    _validate_level_feature_completion(required_features, feature_selections)

    spell_catalog = list(
        _facade_value(
            await client.domain(
                "content_pack",
                {
                    "action": "list",
                    "payload": {
                        "campaign_id": campaign_id,
                        "kind": "catalog",
                        "content_kind": "spell",
                    },
                },
            )
        )
    )
    spell_by_id = {str(item["id"]): item for item in spell_catalog}
    spell_choices = dict(follow_up.get("spell_choices") or {})
    prepared_event = str(follow_up.get("prepared_spell_event") or "")
    spellcasting_plan = dict(plan.get("spellcasting") or {})
    selected_cantrips, selected_leveled, prepared_additions = _level_spell_choice_counts(
        spell_selections,
        spell_by_id=spell_by_id,
        class_name=class_name,
        preparation_mode=str(spellcasting_plan.get("preparation_mode") or "known"),
        maximum_spell_level=int(spellcasting_plan.get("maximum_spell_level", 0) or 0),
    )
    existing_spell_ids = {
        str(item.get("id") or "")
        for item in dict(actor["sheet"].get("content") or {}).get("spells", [])
    }
    duplicate_new_spells = sorted(
        {
            selection["artifact_id"]
            for selection in spell_selections
            if selection["method"] != "class_prepared"
            and selection["artifact_id"] in existing_spell_ids
        }
    )
    if duplicate_new_spells:
        raise ValueError(
            "level known or spellbook selections must add new spells; "
            "already present: " + ", ".join(duplicate_new_spells)
        )
    required_counts = (
        int(spell_choices.get("cantrips_to_add", 0) or 0),
        int(spell_choices.get("leveled_spells_to_add", 0) or 0),
    )
    if (selected_cantrips, selected_leveled) != required_counts:
        raise ValueError(
            "level spell selections do not satisfy the reported cantrip and "
            "leveled-spell choices: expected "
            f"{required_counts[0]}/{required_counts[1]}, got "
            f"{selected_cantrips}/{selected_leveled}"
        )
    if prepared_event and not prepared_spell_ids:
        raise ValueError(
            "prepared or spellbook advancement requires an explicit complete prepared-spell list"
        )
    if not prepared_event and prepared_spell_ids:
        raise ValueError("this level advancement does not allow a prepared-spell event")
    return {
        "plan": plan,
        "follow_up": follow_up,
        "selected_subclass": selected_subclass,
        "feature_catalog": feature_catalog,
        "required_features": required_features,
        "spell_catalog": spell_catalog,
        "prepared_spell_additions": prepared_additions,
    }


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
    defer_checkpoint: bool = False,
) -> dict[str, Any]:
    normalized_class = class_name.strip()
    normalized_reason = reason.strip()
    if (
        not actor_id
        or target_level is None
        or not normalized_class
        or hp_method not in {"fixed", "rolled"}
        or not normalized_reason
        or return_phase not in CAMPAIGN_GAME_PHASES
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

    await client.load()
    scene = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "scene",
            "payload": {"scene_id": scene_id},
        },
    )
    exact_ref = await _validate_source_ref(client, scene, source_ref)
    audit_source = _level_audit_source(exact_ref)
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
    await client.load()
    actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": actor_id}},
    )
    actor_level_before_commit = int(
        dict(dict(actor.get("sheet") or {}).get("progression") or {}).get("level", 0) or 0
    )
    preflight = None
    if actor_level_before_commit == target_level - 1:
        preflight = await _preflight_level_completion(
            client,
            campaign_id=campaign_id,
            actor=actor,
            class_name=normalized_class,
            target_level=target_level,
            subclass_artifact_id=subclass_artifact_id,
            feature_selections=feature_selections,
            spell_selections=spell_selections,
            prepared_spell_ids=prepared_spell_ids,
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
    if preflight is not None and follow_up != preflight["follow_up"]:
        raise RuntimeError("level advancement follow-up changed after its revision-bound plan")

    subclass_options = list(follow_up.get("subclass_options") or [])
    selected_subclass: dict[str, Any] | None = (
        deepcopy(preflight["selected_subclass"])
        if preflight is not None and preflight["selected_subclass"] is not None
        else None
    )
    if subclass_options:
        if selected_subclass is None:
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
        raise_for_pending_ruling(
            applied,
            operation="character_content_apply.subclass",
            context={
                "actor_id": actor_id,
                "artifact_id": subclass_artifact_id,
                "target_level": target_level,
            },
        )
        actor = dict(applied.get("character") or applied)
    elif subclass_artifact_id:
        raise ValueError("this level advancement does not offer a subclass selection")

    feature_catalog = (
        list(preflight["feature_catalog"])
        if preflight is not None
        else list(
            _facade_value(
                await client.domain(
                    "content_pack",
                    {
                        "action": "list",
                        "payload": {
                            "campaign_id": campaign_id,
                            "kind": "catalog",
                            "content_kind": "feature",
                        },
                    },
                )
            )
        )
    )
    actor_class = next(
        item
        for item in actor["sheet"]["progression"]["classes"]
        if str(item.get("name") or "").casefold() == normalized_class.casefold()
    )
    actor_subclass = str(actor_class.get("subclass") or "")
    required_features = (
        deepcopy(preflight["required_features"])
        if preflight is not None
        else _required_level_features(
            follow_up=follow_up,
            feature_catalog=feature_catalog,
            actor_sheet=actor["sheet"],
            class_name=normalized_class,
            subclass_name=actor_subclass,
            target_level=target_level,
        )
    )
    _validate_level_feature_completion(required_features, feature_selections)
    applied_features: list[dict[str, Any]] = []
    feature_spell_grants: list[dict[str, Any]] = []
    for artifact_id, feature in required_features.items():
        requirements = dict(feature.get("selection_requirements") or {})
        selection = deepcopy(feature_selections.get(artifact_id, {}))
        choice_field = str(requirements.get("field") or "")
        if choice_field and choice_field not in selection:
            raise ValueError(
                f"level feature {artifact_id} requires an explicit {choice_field} choice"
            )
        grant_level = int(feature.get("grant_level", 0) or 0)
        if grant_level:
            selection["grant_level"] = grant_level
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
        raise_for_pending_ruling(
            applied,
            operation="character_content_apply.level_feature",
            context={
                "actor_id": actor_id,
                "artifact_id": artifact_id,
                "target_level": target_level,
            },
        )
        feature_spell_grants.extend(deepcopy(list(applied.get("feature_spell_grants") or [])))
        actor = dict(applied.get("character") or applied)
        applied_features.append({"artifact_id": artifact_id, "selection": deepcopy(selection)})

    spell_catalog = (
        list(preflight["spell_catalog"])
        if preflight is not None
        else list(
            _facade_value(
                await client.domain(
                    "content_pack",
                    {
                        "action": "list",
                        "payload": {
                            "campaign_id": campaign_id,
                            "kind": "catalog",
                            "content_kind": "spell",
                        },
                    },
                )
            )
        )
    )
    spell_by_id = {str(item["id"]): item for item in spell_catalog}
    spell_choices = dict(follow_up.get("spell_choices") or {})
    required_cantrips = int(spell_choices.get("cantrips_to_add", 0) or 0)
    required_leveled = int(spell_choices.get("leveled_spells_to_add", 0) or 0)
    prepared_event = str(follow_up.get("prepared_spell_event") or "")
    spellcasting = dict(actor["sheet"].get("spellcasting") or {})
    preparation_mode = str(dict(spellcasting.get("preparation") or {}).get("mode") or "known")
    maximum_spell_level = max(
        (
            int(level)
            for level, resource in dict(spellcasting.get("spell_slots") or {}).items()
            if int(dict(resource).get("max", 0) or 0) > 0
        ),
        default=0,
    )
    selected_cantrips, selected_leveled, prepared_additions = _level_spell_choice_counts(
        spell_selections,
        spell_by_id=spell_by_id,
        class_name=normalized_class,
        preparation_mode=preparation_mode,
        maximum_spell_level=maximum_spell_level,
    )
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
    reused_spells: list[str] = []
    for selection in spell_selections:
        artifact_id = selection["artifact_id"]
        actor_spell_ids = {
            str(item.get("id") or "")
            for item in dict(actor["sheet"].get("content") or {}).get("spells", [])
        }
        if selection["method"] == "class_prepared" and artifact_id in actor_spell_ids:
            applied_spells.append(artifact_id)
            reused_spells.append(artifact_id)
            continue
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
        raise_for_pending_ruling(
            applied,
            operation="character_content_apply.level_spell",
            context={
                "actor_id": actor_id,
                "artifact_id": artifact_id,
                "target_level": target_level,
            },
        )
        actor = dict(applied.get("character") or applied)
        applied_spells.append(artifact_id)

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
    verified_feature_records = {
        str(item.get("id") or ""): item
        for item in dict(verified_sheet.get("content") or {}).get("features", [])
    }
    for artifact_id, feature in required_features.items():
        grant_level = int(feature.get("grant_level", 0) or 0)
        if grant_level and not any(
            int(item.get("level", 0) or 0) == grant_level
            for item in verified_feature_records[artifact_id].get("advancement_grants", [])
        ):
            raise RuntimeError(
                "level advancement verification found a missing repeatable feature grant"
            )
    verified_spells = {
        str(item.get("id") or "")
        for item in dict(verified_sheet.get("content") or {}).get("spells", [])
    }
    if not set(applied_spells).issubset(verified_spells):
        raise RuntimeError("level advancement verification found missing spell artifacts")
    if not {str(item.get("artifact_id") or "") for item in feature_spell_grants}.issubset(
        verified_spells
    ):
        raise RuntimeError("level advancement verification found missing feature-granted spells")
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
            await client.load()
    checkpoint = None
    if not defer_checkpoint:
        label = checkpoint_label.strip() or (
            f"Level {target_level} advancement: {verified_actor['name']}"
        )
        checkpoint = await _checkpoint(
            client,
            campaign_id=campaign_id,
            run_id=run_id,
            label=label,
            checkpoint_id=f"level:{actor_id}:{target_level}",
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
        "reused_spells": reused_spells,
        "feature_spell_grants": feature_spell_grants,
        "advancement_plan": (deepcopy(preflight["plan"]) if preflight is not None else None),
        "prepared_spell_additions": prepared_additions,
        "prepared": prepared,
        "phase_changes": phase_changes,
        "return_phase": return_phase,
        "checkpoint": checkpoint,
    }


async def _sync_character_resources(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    initial_phase: str,
    return_phase: str,
    actor_id: str,
    reason: str,
    checkpoint_label: str,
    defer_checkpoint: bool = False,
) -> dict[str, Any]:
    normalized_reason = reason.strip()
    if not actor_id or not normalized_reason or return_phase not in CAMPAIGN_GAME_PHASES:
        raise ValueError("sync-character-resources requires actor, reason, and return phase")
    if initial_phase == "combat":
        raise RuntimeError("sync-character-resources cannot run during active combat")

    await client.load()
    actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": actor_id}},
    )
    if actor.get("campaign_id") != campaign_id:
        raise ValueError("sync-character-resources actor does not belong to the campaign")
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
                            (f"resource-sync-{actor_id}-enter-lobby-r{campaign['revision']}"),
                        ),
                    },
                )
            )
        )
    await client.open(campaign_id)
    await client.load()
    actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": actor_id}},
    )
    synchronized = _facade_value(
        await client.domain(
            "character_state_change",
            {
                "character_id": actor_id,
                "action": "resource_sync",
                "payload": {"reason": normalized_reason},
                "expected_revision": actor["revision"],
                "idempotency_key": _mutation_key(run_id, "resource-sync", actor_id),
            },
        )
    )
    verified_actor = await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": actor_id}},
    )
    if dict(synchronized.get("character") or {}).get("revision") != verified_actor.get("revision"):
        raise RuntimeError("sync-character-resources verification found a different actor revision")

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
                                (f"resource-sync-{actor_id}-return-play-r{campaign['revision']}"),
                            ),
                        },
                    )
                )
            )
            await client.open(campaign_id)
            await client.load()
    checkpoint = None
    if not defer_checkpoint:
        checkpoint = await _checkpoint(
            client,
            campaign_id=campaign_id,
            run_id=run_id,
            label=(
                checkpoint_label.strip()
                or f"Class resource synchronization: {verified_actor['name']}"
            ),
            checkpoint_id=f"resource-sync:{actor_id}",
        )
    return {
        "actor": verified_actor,
        "changes": list(synchronized.get("changes") or []),
        "reason": normalized_reason,
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
    normalized_reason = required_core_relock_reason(reason)
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
    available_core = dict(profile.get("available_core_pack") or {})
    if available_core.get("fingerprint") == previous_fingerprint:
        return {
            "reason": normalized_reason,
            "previous_core_fingerprint": previous_fingerprint,
            "status": "current",
            "core_pack": available_core,
            "mutation_applied": False,
        }
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None or not branch.get("head_snapshot_id"):
        raise RuntimeError("Core relock requires a current branch head snapshot")
    campaign = await _campaign(client, campaign_id)
    relocked = _facade_value(
        await client.domain(
            "campaign_rules",
            {
                "campaign_id": campaign_id,
                "action": "core_relock",
                "payload": {
                    "expected_core_fingerprint": previous_fingerprint,
                    "reason": normalized_reason,
                    "expected_head_snapshot_id": str(branch["head_snapshot_id"]),
                },
                "branch_id": str(branch["id"]),
                "expected_revision": campaign["revision"],
                "idempotency_key": _mutation_key(
                    run_id,
                    "core-relock",
                    f"{previous_fingerprint}:{branch['head_snapshot_id']}",
                ),
            },
        )
    )
    if relocked.get("status") not in {"current", "relocked"}:
        raise RuntimeError("Core relock did not commit")
    if relocked.get("status") == "current":
        return {
            "reason": normalized_reason,
            "previous_core_fingerprint": previous_fingerprint,
            "checkpoint_snapshot_id": str(branch["head_snapshot_id"]),
            "relock": relocked,
            "mutation_applied": False,
        }
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=(f"core-relock-sync:{previous_fingerprint}:{branch['head_snapshot_id']}"),
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
    finalization: dict[str, Any] | None,
    return_phase: str = "",
    progress_remaps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if initial_phase not in CAMPAIGN_GAME_PHASES:
        raise RuntimeError("refresh-module cannot run during active combat")
    target_phase = return_phase.strip() or initial_phase
    if target_phase not in CAMPAIGN_GAME_PHASES:
        raise ValueError("refresh-module return phase must be lobby or play")
    if source_path is None:
        raise ValueError("refresh-module requires --module-source-path")
    if not isinstance(finalization, dict):
        raise ValueError("refresh-module requires --module-finalization-json")
    allowed_finalization = {
        "catalogs",
        "confirmation",
        "dependencies",
        "manifest",
        "metadata",
        "narrative",
        "portable_id",
        "version",
    }
    unsupported_finalization = sorted(set(finalization) - allowed_finalization)
    if unsupported_finalization:
        raise ValueError(
            "module-finalization-json has unsupported fields: "
            + ", ".join(unsupported_finalization)
        )
    for required_field in ("portable_id", "manifest", "confirmation"):
        value = finalization.get(required_field)
        if value is None or value == "":
            raise ValueError(f"module-finalization-json requires {required_field}")
    manifest_result = await _manifest_get(client, campaign_id)
    manifest = manifest_result["manifest"]
    old_module_id = str(manifest["current"].get("module_id") or "")
    if not old_module_id:
        raise ValueError("refresh-module requires a current manifest module")
    if initial_phase == "lobby":
        await client.load()
    old_index = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "index",
            "payload": {"module_id": old_module_id},
        },
    )
    imported_modules = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "list",
            "payload": {},
        },
    )
    current_module = next(
        (
            dict(item)
            for item in imported_modules
            if str(dict(item).get("id") or "") == old_module_id
        ),
        None,
    )
    if current_module is None:
        raise ValueError("refresh-module current manifest module is not an active imported module")
    current_source_key = str(
        current_module.get("logical_source_key") or current_module.get("source_key") or ""
    )
    if source_key and current_source_key and source_key != current_source_key:
        raise ValueError(
            "refresh-module source key must remain stable across parser revisions: "
            f"current {current_source_key}, requested {source_key}"
        )
    if not source_key:
        source_key = current_source_key
    if not source_key:
        raise ValueError("refresh-module could not identify the logical module source key")
    campaign = await _campaign(client, campaign_id)
    resolved_source_path = source_path.expanduser().resolve()
    resolved_title = title.strip() or resolved_source_path.stem
    source_asset_sha256 = file_sha256(resolved_source_path)
    refresh_identity = _module_refresh_identity(
        old_module_id=old_module_id,
        source_key=source_key,
        source_path=resolved_source_path,
        title=resolved_title,
        parser_revision=f"{DndModuleProfile.name}:{DndModuleProfile.version}",
        source_sha256=source_asset_sha256,
    )
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
    await client.load()
    staged = await client.domain(
        "module_draft",
        {
            "campaign_id": campaign_id,
            "action": "start",
            "payload": {
                "source_path": str(resolved_source_path),
                "source_key": source_key,
                "title": resolved_title,
            },
            "idempotency_key": _mutation_key(run_id, "module-refresh-stage", refresh_identity),
        },
    )
    job_id = str(staged["job"]["id"])
    preview = dict(staged["inspection"])
    if not preview.get("valid"):
        raise RuntimeError("; ".join(preview.get("errors") or ["module preview is invalid"]))
    validation = dict(staged["validation"])
    if not validation.get("valid"):
        raise RuntimeError("module revision validation failed")
    draft_module_id = str(staged.get("module_id") or "")
    if not draft_module_id:
        raise RuntimeError("module revision ingestion returned no module id")
    draft_index = await client.domain(
        "module_query",
        {
            "campaign_id": campaign_id,
            "view": "index",
            "payload": {"module_id": draft_module_id},
        },
    )
    progress_remap_rulings = _module_progress_remap_rulings(
        validation,
        old_index=old_index,
        new_index=draft_index,
        supplied=progress_remaps,
    )
    draft_scene_keys = {
        str(item["scene_id"]): str(item.get("stable_key") or "") for item in draft_index
    }
    activation_remaps = [
        {
            "from_scene_id": item["from_scene_id"],
            "to_scene_key": draft_scene_keys[item["to_scene_id"]],
            "reason": item["reason"],
        }
        for item in progress_remap_rulings
    ]
    finalized = await client.domain(
        "module_draft",
        {
            "campaign_id": campaign_id,
            "action": "finalize",
            "payload": {
                "job_id": job_id,
                **deepcopy(finalization),
            },
            "idempotency_key": _mutation_key(run_id, "module-refresh-finalize", job_id),
        },
    )
    imported = await client.domain(
        "content_pack",
        {
            "action": "import",
            "payload": {
                "campaign_id": campaign_id,
                "kind": "module",
                "artifact": finalized["artifact"],
            },
            "idempotency_key": _mutation_key(run_id, "module-refresh-import", job_id),
        },
    )
    imported_module_id = str(imported.get("module_id") or "")
    if not imported_module_id:
        raise RuntimeError("module Pack import returned no module id")
    campaign = await _campaign(client, campaign_id)
    activated = await client.domain(
        "content_pack",
        {
            "action": "activate",
            "payload": {
                "campaign_id": campaign_id,
                "kind": "module",
                "module_id": imported_module_id,
                **({"progress_remaps": activation_remaps} if activation_remaps else {}),
            },
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
    final_by_key = {
        str(item.get("stable_key") or ""): str(item["scene_id"])
        for item in new_index
        if str(item.get("stable_key") or "")
    }
    progress_remap_targets = {
        item["from_scene_id"]: final_by_key[draft_scene_keys[item["to_scene_id"]]]
        for item in progress_remap_rulings
    }
    refreshed_manifest = _extend_manifest_for_module_revision(
        manifest,
        old_module_id=old_module_id,
        new_module_id=new_module_id,
        old_index=old_index,
        new_index=new_index,
        scene_remaps=progress_remap_targets,
    )
    refreshed_manifest = await _remap_ending_sources_for_module_revision(
        client,
        refreshed_manifest,
        campaign_id=campaign_id,
        new_module_id=new_module_id,
        source_asset_sha256=source_asset_sha256,
    )
    extended = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action=_module_refresh_manifest_action(old_module_id, new_module_id),
        run_id=run_id,
        identity=_module_refresh_manifest_identity(
            old_module_id=old_module_id,
            new_module_id=new_module_id,
            refresh_identity=refresh_identity,
            manifest=refreshed_manifest,
        ),
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
        await client.load()
    synced = await _manifest_mutation(
        client,
        campaign_id=campaign_id,
        action="sync",
        run_id=run_id,
        identity=f"refresh-module-sync:{new_module_id}:{refresh_identity}",
    )
    return {
        "old_module_id": old_module_id,
        "new_module_id": new_module_id,
        "source_key": source_key,
        "refresh_identity": refresh_identity,
        "job_id": job_id,
        "inspection": {
            "parser_profile": preview.get("parser_profile"),
            "parser_version": preview.get("parser_version"),
            "scene_count": preview.get("scene_count"),
            "warnings": list(preview.get("warnings") or []),
        },
        "ingested": {
            "module_id": draft_module_id,
            "chapter_count": staged.get("chapter_count"),
            "scene_count": staged.get("scene_count"),
        },
        "imported": {
            "module_id": imported_module_id,
            "activated": bool(imported.get("activated", False)),
        },
        "activation": activated["activation"],
        "progress_remap_rulings": progress_remap_rulings,
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
    if original_phase not in CAMPAIGN_GAME_PHASES:
        return None
    campaign = await _campaign(client, campaign_id)
    current_phase = _campaign_phase(campaign)
    if current_phase == original_phase:
        return None
    if current_phase not in CAMPAIGN_GAME_PHASES:
        raise RuntimeError("failed module refresh left the campaign in combat")
    await client.open(campaign_id)
    await client.load()
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
    await client.load()
    return restored


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.defer_checkpoint and args.action not in DEFERRED_CHECKPOINT_ACTIONS:
        supported = ", ".join(sorted(DEFERRED_CHECKPOINT_ACTIONS))
        raise ValueError(
            f"--defer-checkpoint is unsupported for {args.action}; supported: {supported}"
        )
    if args.action in {
        "advance-scene",
        "checkpoint",
        "sync",
        "transfer-source-item",
        "claim-party-item",
        "remove-source-effect",
    }:
        _occurrence_identity(args.occurrence_id, args.action)
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
            await client.load()
            knowledge_actor_ids = _knowledge_preflight_actor_ids(args)
            if knowledge_actor_ids:
                await client.load()
                await _validate_campaign_actor_ids(
                    client,
                    campaign_id=args.campaign_id,
                    actor_ids=knowledge_actor_ids,
                    operation=f"{args.action} knowledge recipient",
                )
            if args.action == "initialize-manifest":
                if phase != "lobby":
                    raise RuntimeError("initialize-manifest requires the lobby phase")
                await client.load()
                report["result"] = await _initialize_manifest_from_import_report(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    campaign_line_id=args.campaign_line_id,
                    corpus_root=args.corpus_root,
                    corpus_manifest_path=args.corpus_manifest,
                    import_report_path=args.campaign_import_report,
                )
            elif args.action == "register-party":
                await client.load()
                report["result"] = await _register_party(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    selections=_party_selections(args),
                )
            elif args.action == "register-replacement":
                if phase != "play":
                    raise RuntimeError("register-replacement requires the play phase")
                await client.load()
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
                    agent_ruling=args.replacement_agent_ruling_json,
                    summary=args.event_summary,
                    handoff_knowledge=args.replacement_knowledge,
                    witness_actor_ids=args.knowledge_actor_id,
                    defer_checkpoint=args.defer_checkpoint,
                )
            elif args.action == "prepare-narrative-npc":
                try:
                    report["result"] = await _prepare_narrative_npc(
                        client,
                        campaign_id=args.campaign_id,
                        run_id=args.run_id,
                        occurrence_id=args.occurrence_id,
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
                        source_identity=args.narrative_npc_source_identity,
                        instance_key=args.narrative_npc_instance_key,
                        identity_agent_ruling=(args.narrative_npc_identity_agent_ruling_json),
                        defer_checkpoint=args.defer_checkpoint,
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
                    await client.load()
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
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
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
                        finalization=args.module_finalization_json,
                        return_phase=args.refresh_return_phase,
                        progress_remaps=args.module_progress_remap_json,
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
                await client.load()
                report["result"] = await _query_source(
                    client,
                    campaign_id=args.campaign_id,
                    query=args.source_query,
                    top_k=args.source_top_k,
                    expand=args.source_expand,
                    module_id=args.module_id,
                )
            elif args.action == "index-source":
                await client.load()
                report["result"] = await _index_source(
                    client,
                    campaign_id=args.campaign_id,
                    module_id=args.module_id,
                )
            elif args.action == "read-scene":
                await client.load()
                report["result"] = await _read_scene(
                    client,
                    campaign_id=args.campaign_id,
                    scene_id=str(args.scene_id or ""),
                )
            elif args.action == "continue-segment":
                if phase != "play":
                    raise RuntimeError("continue-segment requires the play phase")
                if not args.condition_id:
                    raise ValueError("continue-segment requires --condition-id")
                await client.load()
                report["result"] = await _continue_completed_segment(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    condition_id=args.condition_id,
                    occurrence_id=args.occurrence_id,
                    scene_id=str(args.scene_id or ""),
                    source_scene_id=args.source_scene_id,
                    occurrence_scene_id=args.occurrence_scene_id,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    objective=args.objective,
                    location_key=args.location_key,
                    reachable_scene_ids=args.reachable_scene_id,
                    checkpoint_label=args.checkpoint_label,
                )
            elif args.action == "advance-scene":
                if phase != "play":
                    raise RuntimeError("advance-scene requires the play phase")
                await client.load()
                report["result"] = await _advance_scene(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    occurrence_id=args.occurrence_id,
                    scene_id=str(args.scene_id or ""),
                    source_scene_id=args.source_scene_id,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    objective=args.objective,
                    mark_visited=args.mark_visited,
                    reachable_scene_ids=args.reachable_scene_id,
                    excluded_scenes=args.excluded_scene_json,
                    agent_ruling=args.scene_agent_ruling_json,
                    occurrence_scene_id=args.occurrence_scene_id,
                    location_key=args.location_key,
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
                    core_conversion_reason=args.core_conversion_reason,
                )
            elif args.action == "advance-time":
                if phase != "play":
                    raise RuntimeError("advance-time requires the play phase")
                await client.load()
                report["result"] = await _advance_time(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    occurrence_id=args.occurrence_id,
                    scene_id=str(args.scene_id or ""),
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    period=str(args.time_period or ""),
                    count=args.time_count,
                    reason=args.time_reason,
                    start_clock=args.time_start_clock_json,
                    agent_ruling=args.time_agent_ruling_json,
                    knowledge_actor_ids=args.knowledge_actor_id,
                    defer_checkpoint=args.defer_checkpoint,
                    expected_after=args.time_expected_after_json,
                    expected_after_ticks=args.time_expected_after_ticks,
                    prerequisite_scene_id=args.prerequisite_scene_id,
                    prerequisite_outcome_id=args.prerequisite_outcome_id,
                    prerequisite_actor_ids=args.prerequisite_actor_id,
                )
            elif args.action == "initialize-clock":
                if phase != "play":
                    raise RuntimeError("initialize-clock requires the play phase")
                await client.load()
                report["result"] = await _initialize_clock(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    occurrence_id=args.occurrence_id,
                    start_clock=args.time_start_clock_json,
                )
            elif args.action == "roll-source":
                if phase != "play":
                    raise RuntimeError("roll-source requires the play phase")
                await client.load()
                roll_arguments = {
                    "campaign_id": args.campaign_id,
                    "run_id": args.run_id,
                    "scene_id": str(args.scene_id or ""),
                    "location_key": args.location_key,
                    "source_excerpt": args.source_excerpt,
                    "source_ref": args.source_ref_json,
                    "roll_id": args.roll_id,
                    "expression": args.roll_expression,
                    "reason": args.roll_reason,
                    "audience_scope": args.event_audience_scope,
                    "defer_checkpoint": args.defer_checkpoint,
                    "modifiers": args.roll_modifier_json,
                }
                if args.roll_count == 1:
                    report["result"] = await _roll_source_table(
                        client,
                        **roll_arguments,
                    )
                else:
                    report["result"] = await _roll_source_sequence(
                        client,
                        **roll_arguments,
                        count=args.roll_count,
                    )
            elif args.action == "resolve-check":
                if phase != "play":
                    raise RuntimeError("resolve-check requires the play phase")
                await client.load()
                report["result"] = await _resolve_check(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    scene_id=str(args.scene_id or ""),
                    source_scene_id=args.source_scene_id,
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    occurrence_id=args.occurrence_id,
                    actor_id=args.check_actor_id,
                    kind=args.check_kind,
                    ability=args.check_ability,
                    dc=args.check_dc,
                    proficient=args.check_proficient,
                    bonus=args.check_bonus,
                    advantage=args.check_advantage,
                    disadvantage=args.check_disadvantage,
                    knowledge_actor_ids=args.knowledge_actor_id,
                    success_knowledge=args.success_knowledge,
                    failure_knowledge=args.failure_knowledge,
                    agent_ruling=args.check_agent_ruling_json,
                    defer_checkpoint=args.defer_checkpoint,
                )
            elif args.action == "resolve-group-check":
                if phase != "play":
                    raise RuntimeError("resolve-group-check requires the play phase")
                await client.load()
                report["result"] = await _resolve_group_check(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    scene_id=str(args.scene_id or ""),
                    source_scene_id=args.source_scene_id,
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    occurrence_id=args.occurrence_id,
                    actor_ids=args.group_check_actor_id,
                    ability=args.check_ability,
                    dc=args.check_dc,
                    proficient=args.check_proficient,
                    bonus=args.check_bonus,
                    advantage=args.check_advantage,
                    disadvantage=args.check_disadvantage,
                    knowledge_actor_ids=args.knowledge_actor_id,
                    success_knowledge=args.success_knowledge,
                    failure_knowledge=args.failure_knowledge,
                    defer_checkpoint=args.defer_checkpoint,
                )
            elif args.action == "resolve-contest":
                if phase != "play":
                    raise RuntimeError("resolve-contest requires the play phase")
                await client.load()
                report["result"] = await _resolve_contest(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    scene_id=str(args.scene_id or ""),
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    source_scene_id=args.source_scene_id,
                    occurrence_id=args.occurrence_id,
                    source_actor_id=args.contest_source_actor_id,
                    target_actor_id=args.contest_target_actor_id,
                    source_ability=args.contest_source_ability,
                    target_ability=args.contest_target_ability,
                    source_proficient=args.contest_source_proficient,
                    target_proficient=args.contest_target_proficient,
                    source_advantage=args.contest_source_advantage,
                    source_disadvantage=args.contest_source_disadvantage,
                    target_advantage=args.contest_target_advantage,
                    target_disadvantage=args.contest_target_disadvantage,
                    knowledge_actor_ids=args.knowledge_actor_id,
                    source_win_knowledge=args.source_win_knowledge,
                    target_win_knowledge=args.target_win_knowledge,
                    tie_knowledge=args.tie_knowledge,
                    defer_checkpoint=args.defer_checkpoint,
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
                    occurrence_id=args.occurrence_id,
                    event_type=args.event_type,
                    summary=args.event_summary,
                    knowledge=args.event_knowledge,
                    knowledge_actor_ids=args.event_knowledge_actor_id,
                    progress_percent=args.progress_percent,
                    audience_scope=args.event_audience_scope,
                    source_scene_id=args.source_scene_id,
                    defer_checkpoint=args.defer_checkpoint,
                    knowledge_cause=args.event_knowledge_cause,
                    agent_ruling=args.event_agent_ruling_json,
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
                    source_scene_id=args.source_scene_id,
                    defer_checkpoint=args.defer_checkpoint,
                    knowledge_cause=args.event_knowledge_cause,
                    agent_ruling=args.event_agent_ruling_json,
                )
            elif args.action == "apply-damage":
                if phase != "play":
                    raise RuntimeError("apply-damage requires the play phase")
                await client.load()
                report["result"] = await _apply_source_damage(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    scene_id=str(args.scene_id or ""),
                    source_scene_id=args.source_scene_id,
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    actor_id=args.damage_actor_id,
                    damage_event_id=args.damage_event_id,
                    expression=args.damage_expression,
                    damage_type=args.damage_type,
                    reason=args.damage_reason,
                    half_damage=args.damage_half,
                    knock_prone=args.damage_knock_prone,
                    knowledge_actor_ids=args.knowledge_actor_id,
                    defer_checkpoint=args.defer_checkpoint,
                )
            elif args.action == "initialize-source-state":
                if phase != "play":
                    raise RuntimeError("initialize-source-state requires the play phase")
                await client.load()
                report["result"] = await _initialize_source_state(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    scene_id=str(args.scene_id or ""),
                    source_scene_id=args.source_scene_id,
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    occurrence_id=args.occurrence_id,
                    actor_id=args.source_state_actor_id,
                    state=args.source_state,
                    reason=args.source_state_reason,
                    knowledge_actor_ids=args.knowledge_actor_id,
                    defer_checkpoint=args.defer_checkpoint,
                )
            elif args.action == "stand-up":
                if phase != "play":
                    raise RuntimeError("stand-up requires the play phase")
                await client.load()
                report["result"] = await _stand_after_source_event(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    scene_id=str(args.scene_id or ""),
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    occurrence_id=args.occurrence_id,
                    actor_id=args.stand_actor_id,
                    knowledge_actor_ids=args.knowledge_actor_id,
                    reason=args.stand_reason,
                    defer_checkpoint=args.defer_checkpoint,
                )
            elif args.action == "short-rest":
                if phase != "play":
                    raise RuntimeError("short-rest requires the play phase")
                await client.load()
                report["result"] = await _short_rest(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    occurrence_id=args.occurrence_id,
                    members=args.rest_member_json,
                    start_clock=args.rest_start_clock_json,
                    duration_minutes=args.rest_duration_minutes,
                    reason=args.rest_reason,
                    prerequisite_scene_id=args.prerequisite_scene_id,
                    prerequisite_outcome_id=args.prerequisite_outcome_id,
                    prerequisite_actor_ids=args.prerequisite_actor_id,
                    expected_start_clock=args.rest_expected_start_clock_json,
                )
            elif args.action == "use-activity":
                if phase != "play":
                    raise RuntimeError("use-activity requires the play phase")
                await client.load()
                report["result"] = await _use_activity(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    scene_id=str(args.scene_id or ""),
                    location_key=args.location_key,
                    actor_id=args.activity_actor_id,
                    activity_id=args.activity_id,
                    activity_event_id=args.activity_event_id,
                    declaration=args.activity_declaration_json,
                    reason=args.activity_reason,
                    knowledge_actor_ids=args.knowledge_actor_id,
                    defer_checkpoint=args.defer_checkpoint,
                )
            elif args.action == "cast-spell":
                if phase != "play":
                    raise RuntimeError("cast-spell requires the play phase")
                await client.load()
                report["result"] = await _cast_standard_spell(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    occurrence_id=args.occurrence_id,
                    scene_id=str(args.scene_id or ""),
                    source_scene_id=args.source_scene_id,
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    actor_id=args.spell_actor_id,
                    target_id=args.spell_target_id,
                    spell_id=args.spell_id,
                    cast_level=args.spell_cast_level,
                    component_ruling=args.spell_component_ruling_json,
                    agent_ruling=args.spell_agent_ruling_json,
                    reason=args.spell_reason,
                    knowledge_actor_ids=args.knowledge_actor_id,
                    defer_checkpoint=args.defer_checkpoint,
                )
            elif args.action == "cast-source-spell":
                if phase != "play":
                    raise RuntimeError("cast-source-spell requires the play phase")
                await client.load()
                report["result"] = await _cast_source_spell(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    occurrence_id=args.occurrence_id,
                    scene_id=str(args.scene_id or ""),
                    source_scene_id=args.source_scene_id,
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    actor_id=args.spell_actor_id,
                    spell_id=args.spell_id,
                    source_item_id=args.spell_source_item_id,
                    cast_level=args.spell_cast_level,
                    component_ruling=args.spell_component_ruling_json,
                    reason=args.spell_reason,
                    knowledge_actor_ids=args.knowledge_actor_id,
                    defer_checkpoint=args.defer_checkpoint,
                )
            elif args.action == "cast-healing-spell":
                if phase != "play":
                    raise RuntimeError("cast-healing-spell requires the play phase")
                await client.load()
                report["result"] = await _cast_healing_spell(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    occurrence_id=args.occurrence_id,
                    scene_id=str(args.scene_id or ""),
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    location_key=args.location_key,
                    actor_id=args.spell_actor_id,
                    target_id=args.spell_target_id,
                    spell_id=args.spell_id,
                    cast_level=args.spell_cast_level,
                    component_ruling=args.spell_component_ruling_json,
                    reason=args.spell_reason,
                    knowledge_actor_ids=args.knowledge_actor_id,
                    defer_checkpoint=args.defer_checkpoint,
                )
            elif args.action == "revive-character":
                if phase != "play":
                    raise RuntimeError("revive-character requires the play phase")
                await client.load()
                report["result"] = await _revive_character(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    occurrence_id=args.occurrence_id,
                    scene_id=str(args.scene_id or ""),
                    source_scene_id=args.source_scene_id,
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    actor_id=args.revive_actor_id,
                    source_actor_id=args.revive_source_actor_id,
                    elapsed_days=args.revive_elapsed_days,
                    soul_willing=args.revive_soul_willing,
                    body_intact=args.revive_body_intact,
                    reason=args.revive_reason,
                )
            elif args.action == "long-rest":
                if phase != "play":
                    raise RuntimeError("long-rest requires the play phase")
                await client.load()
                report["result"] = await _long_rest(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    occurrence_id=args.occurrence_id,
                    members=args.rest_member_json,
                    start_clock=args.rest_start_clock_json,
                    duration_minutes=args.rest_duration_minutes,
                    reason=args.rest_reason,
                    prerequisite_scene_id=args.prerequisite_scene_id,
                    prerequisite_outcome_id=args.prerequisite_outcome_id,
                    prerequisite_actor_ids=args.prerequisite_actor_id,
                    expected_start_clock=args.rest_expected_start_clock_json,
                )
            elif args.action == "recover-stable":
                if phase != "play":
                    raise RuntimeError("recover-stable requires the play phase")
                await client.load()
                report["result"] = await _recover_stable_party(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    occurrence_id=args.occurrence_id,
                    actor_ids=args.recovery_actor_id,
                    resting_members=args.rest_member_json,
                    knowledge_actor_ids=args.knowledge_actor_id,
                    reason=args.rest_reason,
                    expected_start_clock=args.rest_expected_start_clock_json,
                )
            elif args.action == "provision-source-item":
                if phase != "play":
                    raise RuntimeError("provision-source-item requires the play phase")
                await client.load()
                report["result"] = await _provision_source_item(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    actor_id=args.item_actor_id,
                    source_scene_id=args.source_scene_id,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    item=args.item_json,
                    equip_slot=args.item_equip_slot,
                    reason=args.item_reason,
                    checkpoint_label=args.checkpoint_label,
                    defer_checkpoint=args.defer_checkpoint,
                )
            elif args.action == "transfer-source-item":
                if phase != "play":
                    raise RuntimeError("transfer-source-item requires the play phase")
                await client.load()
                report["result"] = await _transfer_source_item_to_party(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    occurrence_id=args.occurrence_id,
                    scene_id=str(args.scene_id or ""),
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    character_id=args.transfer_character_id,
                    item_id=args.transfer_item_id,
                    quantity=args.transfer_item_quantity,
                    reason=args.transfer_reason,
                    checkpoint_label=args.checkpoint_label,
                    defer_checkpoint=args.defer_checkpoint,
                    recipient_character_id=args.transfer_recipient_character_id,
                )
            elif args.action == "claim-party-item":
                if phase != "play":
                    raise RuntimeError("claim-party-item requires the play phase")
                await client.load()
                report["result"] = await _claim_party_item_for_character(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    occurrence_id=args.occurrence_id,
                    scene_id=str(args.scene_id or ""),
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    character_id=args.transfer_character_id,
                    item_id=args.transfer_item_id,
                    quantity=args.transfer_item_quantity,
                    reason=args.transfer_reason,
                    checkpoint_label=args.checkpoint_label,
                    defer_checkpoint=args.defer_checkpoint,
                )
            elif args.action == "pool-coins":
                if phase != "play":
                    raise RuntimeError("pool-coins requires the play phase")
                await client.load()
                report["result"] = await _pool_character_currency(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    occurrence_id=args.occurrence_id,
                    scene_id=str(args.scene_id or ""),
                    source_scene_id=args.source_scene_id,
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    actor_id=args.pool_actor_id,
                    denomination=args.pool_denomination,
                    amount=args.pool_amount,
                    reason=args.pool_reason,
                    defer_checkpoint=args.defer_checkpoint,
                )
            elif args.action == "distribute-coins":
                if phase != "play":
                    raise RuntimeError("distribute-coins requires the play phase")
                await client.load()
                report["result"] = await _pool_character_currency(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    occurrence_id=args.occurrence_id,
                    scene_id=str(args.scene_id or ""),
                    source_scene_id=args.source_scene_id,
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    actor_id=args.pool_actor_id,
                    denomination=args.pool_denomination,
                    amount=args.pool_amount,
                    reason=args.pool_reason,
                    defer_checkpoint=args.defer_checkpoint,
                    direction="to_character",
                )
            elif args.action == "apply-source-effect":
                if phase != "play":
                    raise RuntimeError("apply-source-effect requires the play phase")
                await client.load()
                report["result"] = await _apply_source_effect(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    occurrence_id=args.occurrence_id,
                    scene_id=str(args.scene_id or ""),
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    character_id=args.effect_character_id,
                    effect=args.effect_json,
                    reason=args.effect_reason,
                    checkpoint_label=args.checkpoint_label,
                    source_scene_id=args.source_scene_id,
                    defer_checkpoint=args.defer_checkpoint,
                )
            elif args.action == "remove-source-effect":
                if phase != "play":
                    raise RuntimeError("remove-source-effect requires the play phase")
                await client.load()
                report["result"] = await _remove_source_effect(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    occurrence_id=args.occurrence_id,
                    scene_id=str(args.scene_id or ""),
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    character_id=args.effect_character_id,
                    effect_id=args.effect_id,
                    reason=args.effect_reason,
                    checkpoint_label=args.checkpoint_label,
                    source_scene_id=args.source_scene_id,
                    defer_checkpoint=args.defer_checkpoint,
                )
            elif args.action == "set-source-exhaustion":
                if phase != "play":
                    raise RuntimeError("set-source-exhaustion requires the play phase")
                await client.load()
                report["result"] = await _set_source_exhaustion(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    occurrence_id=args.occurrence_id,
                    scene_id=str(args.scene_id or ""),
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    character_id=args.effect_character_id,
                    level=args.exhaustion_level,
                    reason=args.effect_reason,
                    checkpoint_label=args.checkpoint_label,
                    defer_checkpoint=args.defer_checkpoint,
                )
            elif args.action == "attack-source-object":
                if phase != "play":
                    raise RuntimeError("attack-source-object requires the play phase")
                await client.load()
                report["result"] = await _attack_source_object(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    occurrence_id=args.occurrence_id,
                    scene_id=str(args.scene_id or ""),
                    location_key=args.location_key,
                    source_excerpt=args.source_excerpt,
                    source_ref=args.source_ref_json,
                    character_id=args.check_actor_id,
                    object_state=args.object_json,
                    weapon_id=args.object_weapon_id,
                    reason=args.object_reason,
                    advantage=args.check_advantage,
                    disadvantage=args.check_disadvantage,
                    checkpoint_label=args.checkpoint_label,
                    defer_checkpoint=args.defer_checkpoint,
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
                    defer_checkpoint=args.defer_checkpoint,
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
                    defer_checkpoint=args.defer_checkpoint,
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
                    character_id=args.item_actor_id,
                    source_scene_id=args.source_scene_id,
                    defer_checkpoint=args.defer_checkpoint,
                )
            elif args.action == "use-consumable":
                if phase != "play":
                    raise RuntimeError("use-consumable requires the play phase")
                await client.load()
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
                    defer_checkpoint=args.defer_checkpoint,
                )
            elif args.action == "award-xp":
                if phase != "play":
                    raise RuntimeError("award-xp requires the play phase")
                await client.load()
                report["result"] = await _award_experience(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    occurrence_id=args.occurrence_id,
                    scene_id=str(args.scene_id or ""),
                    source_ref=args.source_ref_json,
                    actor_ids=args.xp_actor_id,
                    amount=args.xp_amount,
                    reason=args.xp_reason,
                )
            elif args.action == "advance-level":
                try:
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
                        defer_checkpoint=args.defer_checkpoint,
                    )
                except Exception:
                    await _restore_phase_after_failed_lobby_action(
                        client,
                        campaign_id=args.campaign_id,
                        run_id=args.run_id,
                        original_phase=phase,
                        identity=f"level:{args.level_actor_id}:{args.level_target}",
                    )
                    raise
            elif args.action == "sync-character-resources":
                try:
                    report["result"] = await _sync_character_resources(
                        client,
                        campaign_id=args.campaign_id,
                        run_id=args.run_id,
                        initial_phase=phase,
                        return_phase=str(args.resource_sync_return_phase or ""),
                        actor_id=args.resource_sync_actor_id,
                        reason=args.resource_sync_reason,
                        checkpoint_label=args.checkpoint_label,
                        defer_checkpoint=args.defer_checkpoint,
                    )
                except Exception:
                    await _restore_phase_after_failed_lobby_action(
                        client,
                        campaign_id=args.campaign_id,
                        run_id=args.run_id,
                        original_phase=phase,
                        identity=f"resource-sync:{args.resource_sync_actor_id}",
                    )
                    raise
            elif args.action == "checkpoint":
                label = args.checkpoint_label or f"Full playthrough checkpoint: {args.run_id}"
                report["result"] = await _checkpoint(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    label=label,
                    checkpoint_id=args.occurrence_id,
                )
            elif args.action == "configure-ending":
                if phase == "combat":
                    raise RuntimeError("configure-ending cannot run during active combat")
                report["result"] = await _configure_ending_conditions(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    conditions=args.ending_condition_json,
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
                        checkpoint_id=f"ending:{args.condition_id}",
                    )
                report["result"] = {"ending": ended, "checkpoint": checkpoint}
            elif args.action == "sync":
                sync_identity = _occurrence_identity(args.occurrence_id, "sync")
                report["result"] = await _manifest_mutation(
                    client,
                    campaign_id=args.campaign_id,
                    action="sync",
                    run_id=args.run_id,
                    identity=f"manual-sync:{sync_identity}",
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
        with campaign_operation_lock(args.home, args.campaign_id):
            report = asyncio.run(_run(args))
    except Exception as error:
        report = {
            "action": args.action,
            "campaign_id": args.campaign_id,
            "run_id": args.run_id,
            "passed": False,
            "error": "; ".join(exception_leaf_messages(error)),
            **ruling_failure_fields(error),
        }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
