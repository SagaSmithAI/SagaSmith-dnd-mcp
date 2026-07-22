"""Audit a real campaign exclusively through a phase-scoped stdio MCP session.

This harness deliberately avoids importing server repositories or reading the
database.  It exercises the same progressive exposure contract available to an
external Agent and writes a compact, reviewable report.
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

PRINCIPAL_ID = "system:local"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True, help="Existing D&D MCP home")
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--action",
        choices=(
            "audit",
            "relock-core",
            "prepare-statblock",
            "prepare-core-wizard",
            "structured-combat",
        ),
        default="audit",
        help="Read-only audit or checkpointed adoption of the current built-in Core",
    )
    parser.add_argument(
        "--run-id",
        default="campaign-regression-v1",
        help="Stable idempotency namespace for mutating actions",
    )
    parser.add_argument("--review-id", help="Reviewed module statblock for prepare-statblock")
    parser.add_argument(
        "--candidate-id",
        help="Review-ready text candidate to review and create during prepare-statblock",
    )
    parser.add_argument(
        "--actor-name",
        default="Structured regression actor",
        help="Canonical actor name for actor preparation actions",
    )
    parser.add_argument(
        "--ability-method",
        choices=("manual", "standard_array"),
        default="standard_array",
        help="Ability-score input method for prepare-core-wizard",
    )
    parser.add_argument(
        "--ability-assignments",
        type=json.loads,
        default={
            "strength": 8,
            "dexterity": 13,
            "constitution": 14,
            "intelligence": 15,
            "wisdom": 12,
            "charisma": 10,
        },
        help="JSON object containing all six ability assignments",
    )
    parser.add_argument(
        "--isolate-branch",
        action="store_true",
        help="Create the reviewed actor on a disposable branch and restore the source branch",
    )
    parser.add_argument("--caster-id", help="Source-bound spellcaster for structured-combat")
    parser.add_argument(
        "--target-id",
        action="append",
        default=[],
        help="One target per spell attack; repeat for structured-combat",
    )
    parser.add_argument(
        "--support-actor-id", help="Optional second source-grounded hostile combatant"
    )
    parser.add_argument("--scene-id", help="Encounter scene for structured-combat")
    parser.add_argument("--location-key", help="Exact scene-atlas location for structured-combat")
    parser.add_argument(
        "--source-excerpt",
        help="Exact encounter-scene text supporting the hostile manifest",
    )
    parser.add_argument(
        "--resume-source-branch-id",
        help="Resume an already-forked regression branch and later return to this source branch",
    )
    parser.add_argument(
        "--spell-id",
        default="dnd5e.content.srd2014.spell.scorching-ray",
    )
    parser.add_argument(
        "--module-root",
        type=Path,
        help="Optional allowlisted module root passed to the MCP server",
    )
    return parser.parse_args()


def _server_parameters(args: argparse.Namespace) -> StdioServerParameters:
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.update(
        {
            "SAGASMITH_DND_MCP_HOME": str(args.home.expanduser().resolve()),
            "SAGASMITH_DND_MCP_AUTO_SEED": "0",
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


def _idempotency_token(run_id: str) -> str:
    return "".join(character if character.isalnum() else "-" for character in run_id)


def _phase_transition_key(token: str, action: str, campaign: dict[str, Any]) -> str:
    """Make transient phase writes retryable without replaying a stale state change."""

    return f"{token}-{action}-r{campaign['revision']}"


def _decode(result: Any) -> Any:
    texts = [item.text for item in result.content if getattr(item, "text", None)]
    message = "\n".join(texts)
    if result.isError:
        raise RuntimeError(message or "MCP tool call failed")
    if not message:
        return result.structuredContent
    return json.loads(message)


def _facade_value(payload: Any) -> Any:
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return payload


class CampaignMcp:
    def __init__(self, session: ClientSession, campaign_id: str) -> None:
        self.session = session
        self.campaign_id = campaign_id
        self.exposure_id = ""

    async def core(self, tool_id: str, arguments: dict[str, Any]) -> Any:
        return _decode(await self.session.call_tool(tool_id, arguments))

    async def open(self) -> dict[str, Any]:
        opened = await self.core(
            "exposure_open",
            {"campaign_id": self.campaign_id, "principal_id": PRINCIPAL_ID},
        )
        self.exposure_id = str(opened["exposure_id"])
        return opened

    async def load(self, *group_ids: str) -> dict[str, Any]:
        status: dict[str, Any] = {}
        for group_id in group_ids:
            status = await self.core(
                "exposure_load",
                {"exposure_id": self.exposure_id, "group_id": group_id},
            )
        return status

    async def domain(self, tool_id: str, arguments: dict[str, Any]) -> Any:
        wrapped = await self.core(
            "exposure_call",
            {
                "exposure_id": self.exposure_id,
                "tool_id": tool_id,
                "arguments": arguments,
            },
        )
        return wrapped["result"]


def _phase_groups(phase: str) -> tuple[str, ...]:
    if phase == "lobby":
        return (
            "lobby.campaign",
            "lobby.rules",
            "lobby.modules",
            "lobby.characters",
            "lobby.memory",
            "lobby.memory_control",
        )
    if phase == "combat":
        return ("combat.observe", "combat.save", "combat.maintenance")
    return ("play.scene", "play.scene_control", "play.characters")


def _module_summary(module: dict[str, Any]) -> dict[str, Any]:
    return {
        key: module.get(key)
        for key in ("id", "title", "revision", "status", "source_key", "checksum")
        if key in module
    }


def _character_summary(character: dict[str, Any]) -> dict[str, Any]:
    sheet = character.get("sheet") if isinstance(character.get("sheet"), dict) else {}
    derived = character.get("derived") if isinstance(character.get("derived"), dict) else {}
    inventory = derived.get("inventory") if isinstance(derived.get("inventory"), dict) else {}
    attacks = inventory.get("weapon_attacks") or []
    spellcasting = (
        derived.get("spellcasting") if isinstance(derived.get("spellcasting"), dict) else {}
    )
    sheet_inventory = sheet.get("inventory") if isinstance(sheet.get("inventory"), dict) else {}
    source_items = [item for item in (sheet_inventory.get("items") or []) if isinstance(item, dict)]
    for collection in (sheet.get("content") or {}).values():
        if isinstance(collection, list):
            source_items.extend(item for item in collection if isinstance(item, dict))
    source_bound = any(
        item.get("source_key") or item.get("rule_refs") or item.get("mechanic_refs")
        for item in source_items
        if isinstance(item, dict)
    )
    return {
        "id": character.get("id"),
        "name": character.get("name"),
        "character_type": character.get("character_type"),
        "revision": character.get("revision"),
        "hp": derived.get("hit_points"),
        "armor_class": derived.get("armor_class"),
        "spell_count": len(spellcasting.get("prepared_spell_ids") or []),
        "attack_count": len(attacks),
        "source_bound": bool(source_bound),
    }


def _review_summary(review: dict[str, Any]) -> dict[str, Any]:
    content = str(review.get("normalized_content") or "")
    return {
        "id": review.get("id"),
        "scene_id": review.get("scene_id"),
        "content_key": review.get("content_key"),
        "content_kind": review.get("content_kind"),
        "checksum": review.get("checksum"),
        "content_preview": content[:160].replace("\n", " "),
        "evidence": review.get("evidence"),
    }


def _spell_card_summary(card: dict[str, Any]) -> dict[str, Any]:
    definition = dict(card.get("definition") or {})
    resolution = dict(card.get("resolution") or {})
    attack = dict(resolution.get("attack") or {})
    settlement_range = attack.get("range_ft_override")
    definition_range = dict(definition.get("range") or {})
    display_range = definition_range.get("normal_ft")
    display_long_range = definition_range.get("long_ft")
    return {
        "id": card.get("id"),
        "name": card.get("name"),
        "level": card.get("level"),
        "grant": card.get("grant"),
        "pack_id": card.get("pack_id"),
        "pack_version": card.get("pack_version"),
        "rule_refs": card.get("rule_refs"),
        "mechanic_refs": card.get("mechanic_refs"),
        "definition": {
            "casting_time": definition.get("casting_time"),
            "range": definition.get("range"),
            "components": definition.get("components"),
            "effect_preview": str(definition.get("effect") or "")[:500],
        },
        "notes": card.get("notes"),
        "resolution": resolution or None,
        "display_settlement_range_consistent": (
            settlement_range is None
            or (display_range == settlement_range and display_long_range in {None, 0})
        ),
    }


def _current_scene_summary(current: Any) -> Any:
    if not isinstance(current, dict):
        return current
    content = str(current.get("content") or "")
    return {
        key: current.get(key)
        for key in (
            "campaign_id",
            "scope_id",
            "module_id",
            "scene_id",
            "stable_key",
            "title",
            "page_start",
            "page_end",
            "progress",
            "spatial",
        )
        if key in current
    } | {"content_characters": len(content)}


async def _audit(args: argparse.Namespace) -> dict[str, Any]:
    params = _server_parameters(args)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            client = CampaignMcp(session, args.campaign_id)
            initial_tools = sorted(tool.name for tool in (await session.list_tools()).tools)
            capabilities = await client.core("server_capabilities", {})
            storage = await client.core("storage_status", {})
            phase_payload = await client.core(
                "game_phase",
                {"campaign_id": args.campaign_id, "action": "get"},
            )
            phase = str(_facade_value(phase_payload)["tool_profile"])
            campaign_payload = await client.core(
                "campaign_query",
                {
                    "view": "get",
                    "payload": {"campaign_id": args.campaign_id},
                    "principal_id": PRINCIPAL_ID,
                },
            )
            campaign = _facade_value(campaign_payload)
            opened = await client.open()
            groups = _phase_groups(phase)
            exposure = await client.load(*groups)
            visible_tools = sorted(tool.name for tool in (await session.list_tools()).tools)

            rules = _facade_value(
                await client.domain(
                    "campaign_rules",
                    {"campaign_id": args.campaign_id, "action": "get_profile"},
                )
            )
            branches = _facade_value(
                await client.domain(
                    "branch_query", {"campaign_id": args.campaign_id, "view": "list"}
                )
            )
            snapshots = _facade_value(
                await client.domain(
                    "snapshot_query", {"campaign_id": args.campaign_id, "view": "list"}
                )
            )
            latest_snapshot = (
                max(snapshots, key=lambda item: int(item.get("slot") or 0)) if snapshots else None
            )
            snapshot_verification = None
            snapshot_lineage = None
            if latest_snapshot is not None:
                snapshot_verification = _facade_value(
                    await client.domain(
                        "snapshot_query",
                        {
                            "campaign_id": args.campaign_id,
                            "view": "verify",
                            "payload": {"slot": latest_snapshot["slot"]},
                        },
                    )
                )
                snapshot_lineage = _facade_value(
                    await client.domain(
                        "snapshot_query",
                        {
                            "campaign_id": args.campaign_id,
                            "view": "lineage",
                            "payload": {"slot": latest_snapshot["slot"]},
                        },
                    )
                )

            modules = _facade_value(
                await client.domain(
                    "module_query", {"campaign_id": args.campaign_id, "view": "list"}
                )
            )
            module_reports: list[dict[str, Any]] = []
            for module in modules:
                module_id = str(module["id"])
                index = _facade_value(
                    await client.domain(
                        "module_query",
                        {
                            "campaign_id": args.campaign_id,
                            "view": "index",
                            "payload": {"module_id": module_id},
                        },
                    )
                )
                assets = _facade_value(
                    await client.domain(
                        "module_query",
                        {
                            "campaign_id": args.campaign_id,
                            "view": "assets",
                            "payload": {"module_id": module_id},
                        },
                    )
                )
                reviews = _facade_value(
                    await client.domain(
                        "module_query",
                        {
                            "campaign_id": args.campaign_id,
                            "view": "content",
                            "payload": {
                                "module_id": module_id,
                                "content_kind": "dnd5e_2014_statblock",
                            },
                        },
                    )
                )
                candidates = _facade_value(
                    await client.domain(
                        "module_query",
                        {
                            "campaign_id": args.campaign_id,
                            "view": "candidates",
                            "payload": {"module_id": module_id},
                        },
                    )
                )
                scenes = index.get("scenes", index) if isinstance(index, dict) else index
                module_reports.append(
                    {
                        **_module_summary(module),
                        "scene_count": len(scenes or []),
                        "asset_count": len(assets or []),
                        "asset_media_types": sorted(
                            {str(item.get("media_type") or "unknown") for item in assets or []}
                        ),
                        "content_reviews": [_review_summary(item) for item in reviews or []],
                        "statblock_candidates": {
                            "count": len(candidates or []),
                            "review_ready": sum(
                                item.get("execution_state") == "review_ready"
                                for item in candidates or []
                            ),
                            "blocked": sum(
                                item.get("execution_state") == "blocked"
                                for item in candidates or []
                            ),
                            "items": [
                                {
                                    key: item.get(key)
                                    for key in (
                                        "id",
                                        "name",
                                        "page_start",
                                        "page_end",
                                        "execution_state",
                                        "review_error",
                                        "validation",
                                    )
                                }
                                for item in candidates or []
                            ],
                        },
                    }
                )
            current_scene = _facade_value(
                await client.domain(
                    "module_query", {"campaign_id": args.campaign_id, "view": "current"}
                )
            )
            characters = _facade_value(
                await client.domain(
                    "character_query",
                    {"view": "list", "payload": {"campaign_id": args.campaign_id}},
                )
            )
            knowledge: list[dict[str, Any]] = []
            for character in characters:
                actor_id = str(character["id"])
                actor_items = _facade_value(
                    await client.domain(
                        "actor_knowledge_query",
                        {
                            "campaign_id": args.campaign_id,
                            "actor_id": actor_id,
                            "view": "list",
                        },
                    )
                )
                actor_context = _facade_value(
                    await client.domain(
                        "continuity_context",
                        {
                            "campaign_id": args.campaign_id,
                            "actor_id": actor_id,
                            "query": "D13 Flennis combat",
                            "audience": "dm",
                            "limit": 4,
                        },
                    )
                )
                knowledge.append(
                    {
                        "actor_id": actor_id,
                        "actor_name": character.get("name"),
                        "knowledge_count": len(actor_items or []),
                        "context_knowledge_count": len(actor_context.get("actor_knowledge") or []),
                    }
                )
            memory = _facade_value(
                await client.domain(
                    "memory_query", {"campaign_id": args.campaign_id, "view": "list"}
                )
            )
            combat = None
            if phase == "combat":
                combat = _facade_value(
                    await client.domain(
                        "combat_query", {"campaign_id": args.campaign_id, "view": "status"}
                    )
                )

            return {
                "transport": "stdio",
                "server_home": str(args.home.expanduser().resolve()),
                "campaign_id": args.campaign_id,
                "initial_tool_count": len(initial_tools),
                "initial_tools": initial_tools,
                "phase": phase,
                "loaded_groups": list(groups),
                "visible_tool_count": len(visible_tools),
                "visible_tools": visible_tools,
                "exposure": {
                    "phase": exposure.get("phase"),
                    "loaded_groups": exposure.get("loaded_groups"),
                },
                "native_dynamic_tools": opened.get("native_dynamic_tools"),
                "capabilities": {
                    "contract_version": capabilities.get("contract_version"),
                    "transport": capabilities.get("transport"),
                    "features": capabilities.get("features"),
                },
                "storage": storage,
                "campaign": campaign,
                "rules": rules,
                "branches": branches,
                "snapshots": {
                    "count": len(snapshots),
                    "latest": latest_snapshot,
                    "latest_verification": snapshot_verification,
                    "latest_lineage": snapshot_lineage,
                },
                "modules": module_reports,
                "current_scene": _current_scene_summary(current_scene),
                "characters": [_character_summary(item) for item in characters],
                "actor_knowledge": knowledge,
                "memory_count": len(memory or []),
                "combat": combat,
            }


async def _relock_core(args: argparse.Namespace) -> dict[str, Any]:
    """Adopt the current Core only between two verified branch checkpoints."""

    async with stdio_client(_server_parameters(args)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            client = CampaignMcp(session, args.campaign_id)
            phase_payload = await client.core(
                "game_phase", {"campaign_id": args.campaign_id, "action": "get"}
            )
            phase = str(_facade_value(phase_payload)["tool_profile"])
            await client.open()
            await client.load(*_phase_groups(phase))

            rules_before = _facade_value(
                await client.domain(
                    "campaign_rules",
                    {"campaign_id": args.campaign_id, "action": "get_profile"},
                )
            )
            profile = dict(rules_before.get("profile") or {})
            old_lock = dict(dict(profile.get("options") or {}).get("_core_rule_pack_lock") or {})
            old_fingerprint = str(old_lock.get("fingerprint") or "")
            if not old_fingerprint:
                raise RuntimeError("campaign profile has no built-in Core fingerprint")
            branches_before = _facade_value(
                await client.domain(
                    "branch_query", {"campaign_id": args.campaign_id, "view": "list"}
                )
            )
            current_branch = next(
                (item for item in branches_before if item.get("is_current")), None
            )
            if current_branch is None:
                raise RuntimeError("campaign has no current branch")
            campaign_before = _facade_value(
                await client.core(
                    "campaign_query",
                    {
                        "view": "get",
                        "payload": {"campaign_id": args.campaign_id},
                        "principal_id": PRINCIPAL_ID,
                    },
                )
            )
            token = _idempotency_token(args.run_id)
            pre_snapshot = await client.domain(
                "snapshot_create",
                {
                    "campaign_id": args.campaign_id,
                    "label": "Before explicit built-in Core relock",
                    "expected_revision": campaign_before["revision"],
                    "expected_head_snapshot_id": current_branch.get("head_snapshot_id") or "",
                    "idempotency_key": f"{token}-pre-core-relock",
                },
            )
            campaign_at_relock = _facade_value(
                await client.core(
                    "campaign_query",
                    {
                        "view": "get",
                        "payload": {"campaign_id": args.campaign_id},
                        "principal_id": PRINCIPAL_ID,
                    },
                )
            )
            relocked = await client.domain(
                "campaign_core_relock",
                {
                    "campaign_id": args.campaign_id,
                    "expected_core_fingerprint": old_fingerprint,
                    "reason": (
                        "Adopt the current tested built-in D&D 5e Core before the "
                        "Avernus structured-spell regression; no character data migration."
                    ),
                    "branch_id": current_branch["id"],
                    "expected_revision": campaign_at_relock["revision"],
                    "expected_head_snapshot_id": pre_snapshot["id"],
                    "idempotency_key": f"{token}-core-relock",
                },
            )
            rules_after = _facade_value(
                await client.domain(
                    "campaign_rules",
                    {"campaign_id": args.campaign_id, "action": "get_profile"},
                )
            )
            campaign_after_relock = _facade_value(
                await client.core(
                    "campaign_query",
                    {
                        "view": "get",
                        "payload": {"campaign_id": args.campaign_id},
                        "principal_id": PRINCIPAL_ID,
                    },
                )
            )
            post_snapshot = await client.domain(
                "snapshot_create",
                {
                    "campaign_id": args.campaign_id,
                    "label": "After explicit built-in Core relock",
                    "expected_revision": campaign_after_relock["revision"],
                    "expected_head_snapshot_id": pre_snapshot["id"],
                    "idempotency_key": f"{token}-post-core-relock",
                },
            )
            pre_verified = _facade_value(
                await client.domain(
                    "snapshot_query",
                    {
                        "campaign_id": args.campaign_id,
                        "view": "verify",
                        "payload": {"slot": pre_snapshot["slot"]},
                    },
                )
            )
            post_verified = _facade_value(
                await client.domain(
                    "snapshot_query",
                    {
                        "campaign_id": args.campaign_id,
                        "view": "verify",
                        "payload": {"slot": post_snapshot["slot"]},
                    },
                )
            )
            return {
                "action": "relock-core",
                "transport": "stdio",
                "campaign_id": args.campaign_id,
                "phase": phase,
                "branch_id": current_branch["id"],
                "rules_before": rules_before,
                "pre_snapshot": pre_snapshot,
                "pre_snapshot_verification": pre_verified,
                "relock": relocked,
                "rules_after": rules_after,
                "post_snapshot": post_snapshot,
                "post_snapshot_verification": post_verified,
            }


async def _prepare_statblock(args: argparse.Namespace) -> dict[str, Any]:
    """Create a fresh source-bound actor in lobby, optionally on a disposable branch."""

    if bool(args.review_id) == bool(args.candidate_id):
        raise ValueError("prepare-statblock requires exactly one of --review-id or --candidate-id")
    token = _idempotency_token(args.run_id)
    async with stdio_client(_server_parameters(args)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            client = CampaignMcp(session, args.campaign_id)
            phase_payload = await client.core(
                "game_phase", {"campaign_id": args.campaign_id, "action": "get"}
            )
            initial_phase = str(_facade_value(phase_payload)["tool_profile"])
            if initial_phase == "combat":
                raise RuntimeError("prepare-statblock cannot run during active combat")
            await client.open()
            await client.load(*_phase_groups(initial_phase))
            branches = _facade_value(
                await client.domain(
                    "branch_query", {"campaign_id": args.campaign_id, "view": "list"}
                )
            )
            current_branch = next((item for item in branches if item.get("is_current")), None)
            if current_branch is None:
                raise RuntimeError("campaign has no current branch")
            source_branch = current_branch
            source_checkpoint: dict[str, Any] | None = None
            isolation: dict[str, Any] | None = None

            phase_changes: list[dict[str, Any]] = []
            if initial_phase != "lobby":
                campaign = _facade_value(
                    await client.core(
                        "campaign_query",
                        {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                    )
                )
                changed = _facade_value(
                    await client.core(
                        "game_phase",
                        {
                            "campaign_id": args.campaign_id,
                            "action": "set",
                            "tool_profile": "lobby",
                            "expected_revision": campaign["revision"],
                            "branch_id": current_branch["id"],
                            "idempotency_key": _phase_transition_key(
                                token, "enter-lobby", campaign
                            ),
                        },
                    )
                )
                phase_changes.append(changed)
            await client.open()
            await client.load("lobby.campaign", "lobby.rules", "lobby.modules", "lobby.characters")
            if args.isolate_branch:
                campaign_lobby = _facade_value(
                    await client.core(
                        "campaign_query",
                        {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                    )
                )
                source_checkpoint = await client.domain(
                    "snapshot_create",
                    {
                        "campaign_id": args.campaign_id,
                        "label": "Before isolated reviewed-statblock regression",
                        "expected_revision": campaign_lobby["revision"],
                        "expected_head_snapshot_id": source_branch.get("head_snapshot_id") or "",
                        "idempotency_key": f"{token}-source-checkpoint",
                    },
                )
                current_branch = _facade_value(
                    await client.domain(
                        "branch_change",
                        {
                            "campaign_id": args.campaign_id,
                            "action": "create",
                            "payload": {
                                "name": f"reviewed-statblock-{token}",
                                "from_snapshot_id": source_checkpoint["id"],
                                "checkout": True,
                            },
                            "expected_revision": campaign_lobby["revision"],
                            "expected_branch_id": source_branch["id"],
                            "idempotency_key": f"{token}-branch-create",
                        },
                    )
                )
                await client.open()
                await client.load(
                    "lobby.campaign", "lobby.rules", "lobby.modules", "lobby.characters"
                )
            rules = _facade_value(
                await client.domain(
                    "campaign_rules",
                    {"campaign_id": args.campaign_id, "action": "get_profile"},
                )
            )
            if rules.get("effective_error"):
                raise RuntimeError(str(rules["effective_error"]))
            candidate: dict[str, Any] | None = None
            if args.candidate_id:
                module_sources = _facade_value(
                    await client.domain(
                        "module_query",
                        {"campaign_id": args.campaign_id, "view": "list"},
                    )
                )
                matches: list[dict[str, Any]] = []
                for module in module_sources:
                    module_candidates = _facade_value(
                        await client.domain(
                            "module_query",
                            {
                                "campaign_id": args.campaign_id,
                                "view": "candidates",
                                "payload": {"module_id": module["id"]},
                            },
                        )
                    )
                    matches.extend(
                        item for item in module_candidates if item.get("id") == args.candidate_id
                    )
                if len(matches) != 1:
                    raise RuntimeError(
                        f"candidate id must resolve exactly once; found {len(matches)}"
                    )
                candidate = matches[0]
                if candidate.get("execution_state") != "review_ready":
                    raise RuntimeError(
                        str(candidate.get("review_error") or "candidate is not review-ready")
                    )
                reviewed = _facade_value(
                    await client.domain(
                        "module_content_review",
                        {
                            "campaign_id": args.campaign_id,
                            "module_id": candidate["module_id"],
                            "scene_id": candidate["scene_id"],
                            "content_key": (
                                f"{_idempotency_token(str(candidate['name'])).lower()}-"
                                f"{str(candidate['id']).split(':')[-1][:10]}"
                            ),
                            "normalized_content": candidate["normalized_content"],
                            "source_chunk_ids": candidate["source_chunk_ids"],
                            "observation": (
                                "Regression DM reviewed the normalized statblock against "
                                "every cited module text chunk."
                            ),
                            "idempotency_key": f"{token}-review-candidate",
                        },
                    )
                )
                review = dict(reviewed["review"])
            else:
                review = _facade_value(
                    await client.domain(
                        "module_query",
                        {
                            "campaign_id": args.campaign_id,
                            "view": "content",
                            "payload": {"review_id": args.review_id},
                        },
                    )
                )
            if review.get("content_kind") != "dnd5e_2014_statblock":
                raise RuntimeError("review is not a D&D 2014 statblock")
            created = _facade_value(
                await client.domain(
                    "character_create_from",
                    {
                        "mode": "module_statblock",
                        "payload": {
                            "campaign_id": args.campaign_id,
                            "review_id": review["id"],
                            "name": args.actor_name,
                            "character_type": "monster",
                        },
                        "idempotency_key": f"{token}-create-statblock",
                    },
                )
            )
            actor_id = str(created["character"]["id"])
            actor = _facade_value(
                await client.domain(
                    "character_query",
                    {"view": "get", "payload": {"character_id": actor_id}},
                )
            )
            spell_cards = [
                _spell_card_summary(card)
                for card in (actor.get("sheet", {}).get("content", {}).get("spells") or [])
            ]
            if not all(item["display_settlement_range_consistent"] for item in spell_cards):
                raise RuntimeError("source-bound spell display and settlement ranges disagree")

            campaign_in_lobby = _facade_value(
                await client.core(
                    "campaign_query",
                    {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                )
            )
            returned_to_play = _facade_value(
                await client.core(
                    "game_phase",
                    {
                        "campaign_id": args.campaign_id,
                        "action": "set",
                        "tool_profile": "play",
                        "expected_revision": campaign_in_lobby["revision"],
                        "branch_id": current_branch["id"],
                        "idempotency_key": _phase_transition_key(
                            token, "return-play", campaign_in_lobby
                        ),
                    },
                )
            )
            phase_changes.append(returned_to_play)
            await client.open()
            await client.load("play.scene", "play.scene_control", "play.characters")
            branches_after = _facade_value(
                await client.domain(
                    "branch_query", {"campaign_id": args.campaign_id, "view": "list"}
                )
            )
            branch_after = next(item for item in branches_after if item.get("is_current"))
            campaign_after = _facade_value(
                await client.core(
                    "campaign_query",
                    {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                )
            )
            snapshot_label = f"Prepared source-bound actor: {args.actor_name}"
            snapshots = _facade_value(
                await client.domain(
                    "snapshot_query",
                    {"campaign_id": args.campaign_id, "view": "list"},
                )
            )
            snapshot = next(
                (
                    item
                    for item in snapshots
                    if item.get("id") == branch_after.get("head_snapshot_id")
                    and item.get("label") == snapshot_label
                ),
                None,
            )
            if snapshot is None:
                snapshot = await client.domain(
                    "snapshot_create",
                    {
                        "campaign_id": args.campaign_id,
                        "label": snapshot_label,
                        "expected_revision": campaign_after["revision"],
                        "expected_head_snapshot_id": (branch_after.get("head_snapshot_id") or ""),
                        "idempotency_key": (
                            f"{token}-prepared-actor-snapshot-r{campaign_after['revision']}"
                        ),
                    },
                )
            verified = _facade_value(
                await client.domain(
                    "snapshot_query",
                    {
                        "campaign_id": args.campaign_id,
                        "view": "verify",
                        "payload": {"slot": snapshot["slot"]},
                    },
                )
            )
            if args.isolate_branch:
                campaign_working_play = _facade_value(
                    await client.core(
                        "campaign_query",
                        {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                    )
                )
                entered_lobby = _facade_value(
                    await client.core(
                        "game_phase",
                        {
                            "campaign_id": args.campaign_id,
                            "action": "set",
                            "tool_profile": "lobby",
                            "expected_revision": campaign_working_play["revision"],
                            "branch_id": current_branch["id"],
                            "idempotency_key": _phase_transition_key(
                                token, "close-enter-lobby", campaign_working_play
                            ),
                        },
                    )
                )
                phase_changes.append(entered_lobby)
                await client.open()
                await client.load("lobby.campaign", "lobby.characters")
                campaign_working_lobby = _facade_value(
                    await client.core(
                        "campaign_query",
                        {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                    )
                )
                branch_snapshot = await client.domain(
                    "snapshot_create",
                    {
                        "campaign_id": args.campaign_id,
                        "label": f"Closed isolated reviewed actor: {args.actor_name}",
                        "expected_revision": campaign_working_lobby["revision"],
                        "expected_head_snapshot_id": snapshot["id"],
                        "idempotency_key": f"{token}-closed-branch-snapshot",
                    },
                )
                checkout = _facade_value(
                    await client.domain(
                        "branch_change",
                        {
                            "campaign_id": args.campaign_id,
                            "action": "checkout",
                            "payload": {"branch_id": source_branch["id"]},
                            "expected_revision": campaign_working_lobby["revision"],
                            "expected_branch_id": current_branch["id"],
                            "idempotency_key": f"{token}-return-source",
                        },
                    )
                )
                campaign_source_lobby = _facade_value(
                    await client.core(
                        "campaign_query",
                        {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                    )
                )
                source_play = _facade_value(
                    await client.core(
                        "game_phase",
                        {
                            "campaign_id": args.campaign_id,
                            "action": "set",
                            "tool_profile": "play",
                            "expected_revision": campaign_source_lobby["revision"],
                            "branch_id": source_branch["id"],
                            "idempotency_key": _phase_transition_key(
                                token, "source-return-play", campaign_source_lobby
                            ),
                        },
                    )
                )
                phase_changes.append(source_play)
                await client.open()
                await client.load("play.scene", "play.scene_control", "play.characters")
                source_characters = _facade_value(
                    await client.domain(
                        "character_query",
                        {"view": "list", "payload": {"campaign_id": args.campaign_id}},
                    )
                )
                actor_absent = actor_id not in {
                    str(item.get("id")) for item in source_characters or []
                }
                if not actor_absent:
                    raise RuntimeError("isolated reviewed actor leaked into the source branch")
                source_campaign = _facade_value(
                    await client.core(
                        "campaign_query",
                        {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                    )
                )
                source_snapshot = await client.domain(
                    "snapshot_create",
                    {
                        "campaign_id": args.campaign_id,
                        "label": "Returned after isolated reviewed-statblock regression",
                        "expected_revision": source_campaign["revision"],
                        "expected_head_snapshot_id": source_checkpoint["id"],
                        "idempotency_key": f"{token}-source-final-snapshot",
                    },
                )
                source_verified = _facade_value(
                    await client.domain(
                        "snapshot_query",
                        {
                            "campaign_id": args.campaign_id,
                            "view": "verify",
                            "payload": {"slot": source_snapshot["slot"]},
                        },
                    )
                )
                isolation = {
                    "source_branch_id": source_branch["id"],
                    "regression_branch_id": current_branch["id"],
                    "source_checkpoint": source_checkpoint,
                    "branch_snapshot": branch_snapshot,
                    "checkout": checkout,
                    "actor_absent_from_source": actor_absent,
                    "source_snapshot": source_snapshot,
                    "source_snapshot_verification": source_verified,
                }
            return {
                "action": "prepare-statblock",
                "transport": "stdio",
                "campaign_id": args.campaign_id,
                "branch_id": current_branch["id"],
                "initial_phase": initial_phase,
                "phase_changes": phase_changes,
                "review": _review_summary(review),
                "candidate": (
                    {
                        key: candidate.get(key)
                        for key in (
                            "id",
                            "name",
                            "module_id",
                            "scene_id",
                            "page_start",
                            "page_end",
                            "execution_state",
                            "validation",
                        )
                    }
                    if candidate is not None
                    else None
                ),
                "created": {
                    "statblock": created.get("statblock"),
                    "spell_warnings": created.get("spell_warnings"),
                    "character": _character_summary(actor),
                    "spell_display_consistent": True,
                    "spell_cards": spell_cards,
                },
                "snapshot": snapshot,
                "snapshot_verification": verified,
                "isolation": isolation,
            }


async def _prepare_core_wizard(args: argparse.Namespace) -> dict[str, Any]:
    """Build one complete level-3 Wizard through public lobby tools and Core content."""

    assignments = args.ability_assignments
    if not isinstance(assignments, dict):
        raise ValueError("--ability-assignments must decode to a JSON object")
    token = _idempotency_token(args.run_id)
    async with stdio_client(_server_parameters(args)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            client = CampaignMcp(session, args.campaign_id)
            phase_payload = await client.core(
                "game_phase", {"campaign_id": args.campaign_id, "action": "get"}
            )
            initial_phase = str(_facade_value(phase_payload)["tool_profile"])
            if initial_phase == "combat":
                raise RuntimeError("prepare-core-wizard cannot run during active combat")
            await client.open()
            await client.load(*_phase_groups(initial_phase))
            branches = _facade_value(
                await client.domain(
                    "branch_query", {"campaign_id": args.campaign_id, "view": "list"}
                )
            )
            current_branch = next((item for item in branches if item.get("is_current")), None)
            if current_branch is None:
                raise RuntimeError("campaign has no current branch")
            if initial_phase != "lobby":
                campaign = _facade_value(
                    await client.core(
                        "campaign_query",
                        {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                    )
                )
                await client.core(
                    "game_phase",
                    {
                        "campaign_id": args.campaign_id,
                        "action": "set",
                        "tool_profile": "lobby",
                        "expected_revision": campaign["revision"],
                        "branch_id": current_branch["id"],
                        "idempotency_key": _phase_transition_key(
                            token, "wizard-enter-lobby", campaign
                        ),
                    },
                )
            await client.open()
            await client.load("lobby.campaign", "lobby.rules", "lobby.characters")

            built = _facade_value(
                await client.domain(
                    "character_create_from",
                    {
                        "mode": "build",
                        "payload": {
                            "campaign_id": args.campaign_id,
                            "name": args.actor_name,
                            "summary": (
                                "Regression PC built from the active D&D 5e 2014 Core "
                                "content catalog."
                            ),
                        },
                        "idempotency_key": f"{token}-build-wizard",
                    },
                )
            )
            actor = dict(built["instance"])
            ability = _facade_value(
                await client.domain(
                    "character_ability_apply",
                    {
                        "character_id": actor["id"],
                        "method": args.ability_method,
                        "assignments": assignments,
                        "expected_revision": actor["revision"],
                        "idempotency_key": f"{token}-wizard-abilities",
                    },
                )
            )
            actor = dict(ability["character"])
            sheet = deepcopy(actor["sheet"])
            sheet["progression"]["level"] = 1
            sheet["progression"]["classes"] = [
                {"name": "Wizard", "level": 1, "subclass": "", "hit_die": 6}
            ]
            sheet["abilities"]["intelligence"]["save_proficient"] = True
            sheet["abilities"]["wisdom"]["save_proficient"] = True
            sheet["skills"]["arcana"]["proficiency"] = "proficient"
            sheet["skills"]["investigation"]["proficiency"] = "proficient"
            constitution = int(sheet["abilities"]["constitution"]["score"])
            intelligence = int(sheet["abilities"]["intelligence"]["score"])
            constitution_modifier = (constitution - 10) // 2
            intelligence_modifier = (intelligence - 10) // 2
            level_one_hp = 6 + constitution_modifier
            sheet["combat"]["hp"] = {
                "value": level_one_hp,
                "max": level_one_hp,
                "temp": 0,
            }
            sheet["combat"]["hit_dice"] = {
                "d6": {
                    "label": "d6",
                    "value": 1,
                    "max": 1,
                    "recovers_on": "long_rest",
                    "source_key": "Wizard",
                }
            }
            sheet["combat"]["hp_progression"] = [
                {
                    "level": 1,
                    "method": "fixed",
                    "value": level_one_hp,
                    "source": "dnd5e.core.2014 Wizard level 1",
                }
            ]
            sheet["traits"]["proficiencies"]["weapons"] = [
                "daggers",
                "darts",
                "slings",
                "quarterstaffs",
                "light crossbows",
            ]
            sheet["spellcasting"]["ability"] = "intelligence"
            sheet["spellcasting"]["spell_slots"] = {
                "1": {
                    "label": "Level 1 spell slots",
                    "value": 2,
                    "max": 2,
                    "recovers_on": "long_rest",
                    "source_key": "Wizard",
                    "slot_level": 1,
                }
            }
            sheet["spellcasting"]["preparation"] = {
                "mode": "spellbook",
                "max_prepared": max(1, 1 + intelligence_modifier),
                "changes_on": "long_rest",
                "selected_spell_ids": [],
            }
            sheet["spellcasting"]["ritual_casting"] = True
            sheet["spellcasting"]["spellbook"] = {"enabled": True, "spell_ids": []}
            actor = _facade_value(
                await client.domain(
                    "character_sheet_replace",
                    {
                        "character_id": actor["id"],
                        "sheet": sheet,
                        "expected_revision": actor["revision"],
                        "idempotency_key": f"{token}-wizard-level-one-sheet",
                    },
                )
            )

            async def catalog(kind: str, query: str = "") -> list[dict[str, Any]]:
                return list(
                    _facade_value(
                        await client.domain(
                            "rule_pack_query",
                            {
                                "view": "content_catalog",
                                "payload": {
                                    "campaign_id": args.campaign_id,
                                    "kind": kind,
                                    "query": query,
                                },
                            },
                        )
                    )
                )

            async def apply_artifact(
                current: dict[str, Any],
                artifact: dict[str, Any],
                suffix: str,
                selection: dict[str, Any] | None = None,
            ) -> dict[str, Any]:
                return dict(
                    _facade_value(
                        await client.domain(
                            "character_content_apply",
                            {
                                "character_id": current["id"],
                                "artifact_id": artifact["id"],
                                "selection": selection,
                                "expected_revision": current["revision"],
                                "idempotency_key": f"{token}-{suffix}",
                            },
                        )
                    )
                )

            human = next(
                item for item in await catalog("species", "Human") if item["name"] == "Human"
            )
            human_languages = int(human["selection_requirements"].get("language_count", 0) or 0)
            actor = await apply_artifact(
                actor,
                human,
                "wizard-species-human",
                {"languages": ["Elvish"][:human_languages]} if human_languages else {},
            )
            acolyte = next(
                item for item in await catalog("background", "Acolyte") if item["name"] == "Acolyte"
            )
            background_languages = int(
                acolyte["selection_requirements"].get("language_count", 0) or 0
            )
            actor = await apply_artifact(
                actor,
                acolyte,
                "wizard-background-acolyte",
                {"languages": ["Celestial", "Draconic"][:background_languages]},
            )

            advancements: list[dict[str, Any]] = []
            applied_features: list[str] = []
            selected_subclass: dict[str, Any] | None = None
            for new_level in (2, 3):
                await client.domain(
                    "campaign_event",
                    {
                        "campaign_id": args.campaign_id,
                        "action": "add",
                        "payload": {
                            "summary": (
                                f"Prepared {args.actor_name} as a level {new_level} "
                                "Core regression Wizard."
                            ),
                            "event_type": "regression_setup",
                            "audience_scope": "dm",
                        },
                        "idempotency_key": f"{token}-wizard-level-{new_level}-event",
                    },
                )
                advanced = _facade_value(
                    await client.domain(
                        "character_state_change",
                        {
                            "character_id": actor["id"],
                            "action": "level_advance",
                            "payload": {
                                "class_name": "Wizard",
                                "hp_method": "fixed",
                                "reason": "prepare a rules-complete campaign regression PC",
                                "source_ref": "dnd5e.core.2014",
                            },
                            "expected_revision": actor["revision"],
                            "idempotency_key": f"{token}-wizard-level-{new_level}",
                        },
                    )
                )
                actor = dict(advanced["character"])
                follow_up = dict(advanced["advancement"]["follow_up"])
                for feature in follow_up.get("feature_artifacts") or []:
                    actor = await apply_artifact(
                        actor,
                        {"id": feature["artifact_id"]},
                        f"wizard-feature-{_idempotency_token(str(feature['artifact_id']))}",
                    )
                    applied_features.append(str(feature["artifact_id"]))
                if selected_subclass is None and follow_up.get("subclass_options"):
                    selected_subclass = sorted(
                        follow_up["subclass_options"],
                        key=lambda item: (str(item.get("name")), str(item.get("artifact_id"))),
                    )[0]
                    actor = await apply_artifact(
                        actor,
                        {"id": selected_subclass["artifact_id"]},
                        "wizard-subclass",
                        {"target_class_name": "Wizard"},
                    )
                advancements.append(
                    {
                        "level": new_level,
                        "hit_points": advanced["advancement"]["hit_points"],
                        "spell_choices": advanced["advancement"]["spell_choices"],
                        "follow_up": follow_up,
                    }
                )

            spells = await catalog("spell")
            wizard_spells = [
                item
                for item in spells
                if "wizard"
                in {
                    str(value).casefold()
                    for value in item["selection_requirements"].get("eligible_classes", [])
                }
                and int(item["selection_requirements"].get("level", 0) or 0) <= 2
            ]
            cantrips = sorted(
                (
                    item
                    for item in wizard_spells
                    if int(item["selection_requirements"].get("level", 0) or 0) == 0
                ),
                key=lambda item: (item["name"], item["id"]),
            )[:3]
            leveled = sorted(
                (
                    item
                    for item in wizard_spells
                    if int(item["selection_requirements"].get("level", 0) or 0) in {1, 2}
                ),
                key=lambda item: (
                    int(item["selection_requirements"].get("level", 0) or 0),
                    item["name"],
                    item["id"],
                ),
            )
            scorching_ray = next((item for item in leveled if item["id"] == args.spell_id), None)
            if scorching_ray is None:
                raise RuntimeError(
                    "the active Core catalog does not expose the requested Wizard spell"
                )
            spellbook = [
                scorching_ray,
                *[item for item in leveled if item["id"] != args.spell_id][:9],
            ]
            if len(cantrips) != 3 or len(spellbook) != 10:
                raise RuntimeError(
                    "the active Core catalog cannot complete a level-3 Wizard spell loadout"
                )
            applied_spells: list[str] = []
            for spell in [*cantrips, *spellbook]:
                level = int(spell["selection_requirements"].get("level", 0) or 0)
                actor = await apply_artifact(
                    actor,
                    spell,
                    f"wizard-spell-{_idempotency_token(str(spell['id']))}",
                    {
                        "source_class": "Wizard",
                        "method": "known" if level == 0 else "spellbook",
                    },
                )
                applied_spells.append(str(spell["id"]))
            max_prepared = int(actor["sheet"]["spellcasting"]["preparation"]["max_prepared"])
            prepared_ids = [
                args.spell_id,
                *[item["id"] for item in spellbook if item["id"] != args.spell_id],
            ][:max_prepared]
            prepared = _facade_value(
                await client.domain(
                    "character_spell_prepare",
                    {
                        "character_id": actor["id"],
                        "mode": "replace_all",
                        "payload": {"spell_ids": prepared_ids, "event": "level_up"},
                        "expected_revision": actor["revision"],
                        "idempotency_key": f"{token}-wizard-prepared-spells",
                    },
                )
            )
            actor = dict(prepared["character"] if "character" in prepared else prepared)
            campaign_before_rest = _facade_value(
                await client.core(
                    "campaign_query",
                    {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                )
            )
            if not dict(campaign_before_rest.get("state") or {}).get("world_time"):
                clock = _facade_value(
                    await client.domain(
                        "campaign_change",
                        {
                            "campaign_id": args.campaign_id,
                            "action": "clock_set",
                            "payload": {
                                "day": 1,
                                "hour": 0,
                                "minute": 0,
                                "label": "Campaign regression setup",
                            },
                            "expected_revision": campaign_before_rest["revision"],
                            "idempotency_key": f"{token}-wizard-clock",
                        },
                    )
                )
                rest_campaign_revision = int(clock["campaign_revision"])
            else:
                rest_campaign_revision = int(campaign_before_rest["revision"])
            rested = _facade_value(
                await client.domain(
                    "campaign_change",
                    {
                        "campaign_id": args.campaign_id,
                        "action": "party_rest",
                        "payload": {
                            "members": [
                                {
                                    "character_id": actor["id"],
                                    "expected_revision": actor["revision"],
                                    "prepared_spell_ids": prepared_ids,
                                    "food_and_drink": True,
                                }
                            ]
                        },
                        "expected_revision": rest_campaign_revision,
                        "idempotency_key": f"{token}-wizard-ready-long-rest",
                    },
                )
            )
            if actor["id"] not in set(rested["member_ids"]):
                raise RuntimeError("party rest did not settle the prepared Wizard")
            actor = dict(
                _facade_value(
                    await client.domain(
                        "character_query",
                        {"view": "get", "payload": {"character_id": actor["id"]}},
                    )
                )
            )

            branches_after = _facade_value(
                await client.domain(
                    "branch_query", {"campaign_id": args.campaign_id, "view": "list"}
                )
            )
            branch_after = next(item for item in branches_after if item.get("is_current"))
            campaign_after = _facade_value(
                await client.core(
                    "campaign_query",
                    {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                )
            )
            snapshot_label = f"Prepared Core Wizard: {args.actor_name}"
            snapshots = _facade_value(
                await client.domain(
                    "snapshot_query", {"campaign_id": args.campaign_id, "view": "list"}
                )
            )
            snapshot = next(
                (
                    item
                    for item in snapshots
                    if item.get("id") == branch_after.get("head_snapshot_id")
                    and item.get("label") == snapshot_label
                ),
                None,
            )
            if snapshot is None:
                snapshot = await client.domain(
                    "snapshot_create",
                    {
                        "campaign_id": args.campaign_id,
                        "label": snapshot_label,
                        "expected_revision": campaign_after["revision"],
                        "expected_head_snapshot_id": branch_after.get("head_snapshot_id") or "",
                        "idempotency_key": (
                            f"{token}-wizard-snapshot-r{campaign_after['revision']}"
                        ),
                    },
                )
            verified = _facade_value(
                await client.domain(
                    "snapshot_query",
                    {
                        "campaign_id": args.campaign_id,
                        "view": "verify",
                        "payload": {"slot": snapshot["slot"]},
                    },
                )
            )
            campaign_lobby = _facade_value(
                await client.core(
                    "campaign_query",
                    {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                )
            )
            phase_change = _facade_value(
                await client.core(
                    "game_phase",
                    {
                        "campaign_id": args.campaign_id,
                        "action": "set",
                        "tool_profile": "play",
                        "expected_revision": campaign_lobby["revision"],
                        "branch_id": branch_after["id"],
                        "idempotency_key": _phase_transition_key(
                            token, "wizard-return-play", campaign_lobby
                        ),
                    },
                )
            )
            return {
                "action": "prepare-core-wizard",
                "transport": "stdio",
                "campaign_id": args.campaign_id,
                "initial_phase": initial_phase,
                "ability_generation": {
                    "method": args.ability_method,
                    "assignments": assignments,
                    "status": ability.get("status"),
                },
                "species": human["id"],
                "background": acolyte["id"],
                "advancements": advancements,
                "selected_subclass": selected_subclass,
                "applied_features": applied_features,
                "applied_spells": applied_spells,
                "prepared_spell_ids": prepared_ids,
                "character": _character_summary(actor),
                "snapshot": snapshot,
                "snapshot_verification": verified,
                "phase_change": phase_change,
            }


async def _structured_combat(args: argparse.Namespace) -> dict[str, Any]:
    """Run structured spell combat on a disposable branch and restore the source branch."""

    if not all(
        (args.caster_id, args.scene_id, args.location_key, args.source_excerpt, args.target_id)
    ):
        raise ValueError(
            "--caster-id, --scene-id, --location-key, --source-excerpt, and at least "
            "one --target-id are required"
        )
    token = _idempotency_token(args.run_id)
    async with stdio_client(_server_parameters(args)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            client = CampaignMcp(session, args.campaign_id)
            initial_phase_payload = await client.core(
                "game_phase", {"campaign_id": args.campaign_id, "action": "get"}
            )
            initial_phase = str(_facade_value(initial_phase_payload)["tool_profile"])
            if initial_phase == "combat":
                raise RuntimeError("campaign already has active combat")
            await client.open()
            await client.load(*_phase_groups(initial_phase))
            initial_branches = _facade_value(
                await client.domain(
                    "branch_query", {"campaign_id": args.campaign_id, "view": "list"}
                )
            )
            current_branch = next(
                (item for item in initial_branches if item.get("is_current")), None
            )
            if current_branch is None:
                raise RuntimeError("campaign has no current branch")
            if args.resume_source_branch_id:
                source_branch = next(
                    (
                        item
                        for item in initial_branches
                        if item.get("id") == args.resume_source_branch_id
                    ),
                    None,
                )
                if source_branch is None:
                    raise RuntimeError("resume source branch does not exist")
                if current_branch["id"] == source_branch["id"]:
                    raise RuntimeError("resume requires the disposable branch to be current")
                regression_branch = current_branch
                regression_branch_id = str(current_branch["id"])
                source_checkpoint = {
                    "id": current_branch.get("base_snapshot_id"),
                    "resumed": True,
                }
            else:
                source_branch = current_branch
            source_actor_ids = [args.caster_id, *args.target_id]
            if args.support_actor_id:
                source_actor_ids.append(args.support_actor_id)
            source_actors = {
                actor_id: _facade_value(
                    await client.domain(
                        "character_query",
                        {"view": "get", "payload": {"character_id": actor_id}},
                    )
                )
                for actor_id in source_actor_ids
            }
            hp_before = {
                actor_id: _character_summary(actor)["hp"]
                for actor_id, actor in source_actors.items()
            }

            if not args.resume_source_branch_id:
                campaign = _facade_value(
                    await client.core(
                        "campaign_query",
                        {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                    )
                )
                if initial_phase != "lobby":
                    await client.core(
                        "game_phase",
                        {
                            "campaign_id": args.campaign_id,
                            "action": "set",
                            "tool_profile": "lobby",
                            "expected_revision": campaign["revision"],
                            "branch_id": source_branch["id"],
                            "idempotency_key": _phase_transition_key(
                                token, "source-enter-lobby", campaign
                            ),
                        },
                    )
                await client.open()
                await client.load("lobby.campaign", "lobby.characters")
                campaign_lobby = _facade_value(
                    await client.core(
                        "campaign_query",
                        {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                    )
                )
                source_checkpoint = await client.domain(
                    "snapshot_create",
                    {
                        "campaign_id": args.campaign_id,
                        "label": "Campaign structured-combat regression source",
                        "expected_revision": campaign_lobby["revision"],
                        "expected_head_snapshot_id": source_branch.get("head_snapshot_id") or "",
                        "idempotency_key": f"{token}-source-checkpoint",
                    },
                )
                regression_branch = _facade_value(
                    await client.domain(
                        "branch_change",
                        {
                            "campaign_id": args.campaign_id,
                            "action": "create",
                            "payload": {
                                "name": f"regression-{token}",
                                "from_snapshot_id": source_checkpoint["id"],
                                "checkout": True,
                            },
                            "expected_revision": campaign_lobby["revision"],
                            "expected_branch_id": source_branch["id"],
                            "idempotency_key": f"{token}-branch-create",
                        },
                    )
                )
                regression_branch_id = str(regression_branch["id"])
                campaign_on_branch = _facade_value(
                    await client.core(
                        "campaign_query",
                        {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                    )
                )
                await client.core(
                    "game_phase",
                    {
                        "campaign_id": args.campaign_id,
                        "action": "set",
                        "tool_profile": "play",
                        "expected_revision": campaign_on_branch["revision"],
                        "branch_id": regression_branch_id,
                        "idempotency_key": _phase_transition_key(
                            token, "branch-enter-play", campaign_on_branch
                        ),
                    },
                )
            await client.open()
            await client.load(
                "play.scene",
                "play.scene_control",
                "play.characters",
                "play.combat_control",
            )

            hostile_ids = list(args.target_id)
            if args.support_actor_id:
                hostile_ids.append(args.support_actor_id)
            manifest = {
                "schema_version": 1,
                "groups": [
                    {
                        "key": "source-grounded-hostiles",
                        "label": "Reviewed scene hostiles",
                        "role": "combatant",
                        "required_count": len(hostile_ids),
                        "actor_ids": hostile_ids,
                        "source_excerpt": args.source_excerpt,
                    }
                ],
                "notes": "Temporary branch regression; party actors are additional participants.",
            }
            readiness = _facade_value(
                await client.domain(
                    "module_query",
                    {
                        "campaign_id": args.campaign_id,
                        "view": "readiness",
                        "payload": {
                            "scene_id": args.scene_id,
                            "participant_manifest": manifest,
                        },
                    },
                )
            )
            if not readiness.get("ready"):
                raise RuntimeError("source-grounded participant manifest is not combat-ready")

            participant_ids = [args.caster_id, *args.target_id]
            if args.support_actor_id:
                participant_ids.append(args.support_actor_id)
            participant_config = [
                {
                    "actor_id": args.caster_id,
                    "initiative": 30,
                    "tie_breaker": 0,
                    "position": {"x": 2, "y": 2},
                    "disposition": "friendly",
                }
            ]
            participant_config.extend(
                {
                    "actor_id": target_id,
                    "initiative": 20 - index,
                    "tie_breaker": index,
                    "position": {"x": 7, "y": 2 + index},
                    "disposition": "hostile",
                }
                for index, target_id in enumerate(args.target_id, start=1)
            )
            if args.support_actor_id:
                participant_config.append(
                    {
                        "actor_id": args.support_actor_id,
                        "initiative": 10,
                        "tie_breaker": len(participant_config),
                        "position": {"x": 3, "y": 2},
                        "disposition": "hostile",
                    }
                )
            campaign_before_combat = _facade_value(
                await client.core(
                    "campaign_query",
                    {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                )
            )
            started = await client.domain(
                "combat_start",
                {
                    "campaign_id": args.campaign_id,
                    "participant_ids": participant_ids,
                    "participant_config": participant_config,
                    "participant_manifest": manifest,
                    "name": f"{args.location_key} structured spell regression",
                    "scene_id": args.scene_id,
                    "scope_id": "party",
                    "battle_map": {"location_key": args.location_key},
                    "ruleset": "2014",
                    "branch_id": regression_branch_id,
                    "expected_revision": campaign_before_combat["revision"],
                    "idempotency_key": f"{token}-combat-start",
                },
            )
            await client.open()
            await client.load(
                "combat.observe",
                "combat.actions",
                "combat.turn",
                "combat.control",
                "combat.save",
                "combat.map",
            )
            combat_tools = sorted(tool.name for tool in (await session.list_tools()).tools)
            cast = await client.domain(
                "combat_cast_spell",
                {
                    "campaign_id": args.campaign_id,
                    "actor_id": args.caster_id,
                    "spell_id": args.spell_id,
                    "cast_level": 2,
                    "branch_id": regression_branch_id,
                    "expected_revision": started["campaign_revision"],
                    "idempotency_key": f"{token}-spell-cast",
                },
            )
            if cast.get("status") != "pending_resolution":
                raise RuntimeError(f"spell cast did not open a resolution: {cast.get('status')}")
            resolution_id = str(cast["result"]["resolution_id"])
            attack_results: list[dict[str, Any]] = []
            current_revision = int(cast["campaign_revision"])
            attack_count = int(cast["result"]["attack_count"])
            for index in range(attack_count):
                target_id = args.target_id[index % len(args.target_id)]
                settled = await client.domain(
                    "combat_resolve_attack",
                    {
                        "campaign_id": args.campaign_id,
                        "actor_id": args.caster_id,
                        "target_id": target_id,
                        "action": {"spell_resolution_id": resolution_id},
                        "branch_id": regression_branch_id,
                        "expected_revision": current_revision,
                        "idempotency_key": f"{token}-spell-attack-{index + 1}",
                    },
                )
                if settled.get("status") != "committed":
                    raise RuntimeError(
                        f"spell attack {index + 1} did not commit: {settled.get('status')}"
                    )
                current_revision = int(settled["campaign_revision"])
                result = dict(settled.get("result") or {})
                attack_results.append(
                    {
                        "target_id": target_id,
                        "hit": result.get("hit"),
                        "critical": result.get("critical"),
                        "damage": result.get("damage"),
                        "remaining_attacks": dict(result.get("spell_resolution") or {}).get(
                            "remaining_attacks"
                        ),
                    }
                )
            combat_status = _facade_value(
                await client.domain(
                    "combat_query", {"campaign_id": args.campaign_id, "view": "status"}
                )
            )
            battle_map = dict(combat_status.get("battle_map") or {})
            ended = await client.domain(
                "combat_end",
                {
                    "campaign_id": args.campaign_id,
                    "outcome": {
                        "status": "truce",
                        "summary": (
                            "Regression encounter ended after every structured spell attack "
                            "was settled; story continuity remains on the source branch."
                        ),
                    },
                    "branch_id": regression_branch_id,
                    "expected_revision": current_revision,
                    "idempotency_key": f"{token}-combat-end",
                },
            )

            await client.open()
            await client.load("play.scene", "play.scene_control", "play.characters")
            caster_name = str(source_actors[args.caster_id].get("name") or args.caster_id)
            witness_name = str(source_actors[args.target_id[0]].get("name") or args.target_id[0])
            witness_key = f"regression.{token}.witnessed-spell"
            caster_key = f"regression.{token}.observed-outcomes"
            witness_event = _facade_value(
                await client.domain(
                    "campaign_event",
                    {
                        "campaign_id": args.campaign_id,
                        "action": "add",
                        "payload": {
                            "summary": f"{witness_name} saw {caster_name} cast the spell.",
                            "event_type": "regression",
                            "audience_scope": "actor",
                            "branch_id": regression_branch_id,
                            "known_by_actor_ids": [args.target_id[0]],
                            "knowledge_key": witness_key,
                            "knowledge_proposition": (
                                f"{caster_name} cast Scorching Ray during this encounter."
                            ),
                            "knowledge_disclosure_scope": "owner",
                        },
                        "idempotency_key": f"{token}-witness-event",
                    },
                )
            )
            caster_event = _facade_value(
                await client.domain(
                    "campaign_event",
                    {
                        "campaign_id": args.campaign_id,
                        "action": "add",
                        "payload": {
                            "summary": f"{caster_name} observed the spell attack outcomes.",
                            "event_type": "regression",
                            "audience_scope": "actor",
                            "branch_id": regression_branch_id,
                            "known_by_actor_ids": [args.caster_id],
                            "knowledge_key": caster_key,
                            "knowledge_proposition": (
                                f"{caster_name} observed the resolved outcomes of every "
                                "Scorching Ray attack."
                            ),
                            "knowledge_disclosure_scope": "owner",
                        },
                        "idempotency_key": f"{token}-caster-event",
                    },
                )
            )
            branch_memory = _facade_value(
                await client.domain(
                    "memory_change",
                    {
                        "campaign_id": args.campaign_id,
                        "content": (
                            "Structured spell regression completed on the disposable "
                            f"{args.location_key} branch."
                        ),
                        "kind": "regression",
                        "subject": f"{args.location_key} structured combat",
                        "metadata": {"spell_id": args.spell_id},
                        "branch_id": regression_branch_id,
                        "idempotency_key": f"{token}-branch-memory",
                    },
                )
            )
            witness_knowledge = _facade_value(
                await client.domain(
                    "actor_knowledge_query",
                    {
                        "campaign_id": args.campaign_id,
                        "actor_id": args.target_id[0],
                        "view": "list",
                        "payload": {"branch_id": regression_branch_id},
                    },
                )
            )
            caster_knowledge = _facade_value(
                await client.domain(
                    "actor_knowledge_query",
                    {
                        "campaign_id": args.campaign_id,
                        "actor_id": args.caster_id,
                        "view": "list",
                        "payload": {"branch_id": regression_branch_id},
                    },
                )
            )
            campaign_after_events = _facade_value(
                await client.core(
                    "campaign_query",
                    {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                )
            )
            branches_after_combat = _facade_value(
                await client.domain(
                    "branch_query", {"campaign_id": args.campaign_id, "view": "list"}
                )
            )
            branch_after_combat = next(
                item for item in branches_after_combat if item.get("is_current")
            )
            play_snapshot = await client.domain(
                "snapshot_create",
                {
                    "campaign_id": args.campaign_id,
                    "label": "Structured spell and actor-knowledge regression complete",
                    "expected_revision": campaign_after_events["revision"],
                    "expected_head_snapshot_id": (
                        branch_after_combat.get("head_snapshot_id") or ""
                    ),
                    "idempotency_key": f"{token}-regression-play-snapshot",
                },
            )
            campaign_before_lobby = _facade_value(
                await client.core(
                    "campaign_query",
                    {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                )
            )
            await client.core(
                "game_phase",
                {
                    "campaign_id": args.campaign_id,
                    "action": "set",
                    "tool_profile": "lobby",
                    "expected_revision": campaign_before_lobby["revision"],
                    "branch_id": regression_branch_id,
                    "idempotency_key": _phase_transition_key(
                        token, "regression-enter-lobby", campaign_before_lobby
                    ),
                },
            )
            await client.open()
            await client.load("lobby.campaign", "lobby.characters")
            campaign_regression_lobby = _facade_value(
                await client.core(
                    "campaign_query",
                    {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                )
            )
            regression_lobby_snapshot = await client.domain(
                "snapshot_create",
                {
                    "campaign_id": args.campaign_id,
                    "label": "Closed disposable structured-combat branch",
                    "expected_revision": campaign_regression_lobby["revision"],
                    "expected_head_snapshot_id": play_snapshot["id"],
                    "idempotency_key": f"{token}-regression-lobby-snapshot",
                },
            )
            checked_out = _facade_value(
                await client.domain(
                    "branch_change",
                    {
                        "campaign_id": args.campaign_id,
                        "action": "checkout",
                        "payload": {"branch_id": source_branch["id"]},
                        "expected_revision": campaign_regression_lobby["revision"],
                        "expected_branch_id": regression_branch_id,
                        "idempotency_key": f"{token}-return-source-branch",
                    },
                )
            )
            campaign_source_lobby = _facade_value(
                await client.core(
                    "campaign_query",
                    {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                )
            )
            await client.core(
                "game_phase",
                {
                    "campaign_id": args.campaign_id,
                    "action": "set",
                    "tool_profile": "play",
                    "expected_revision": campaign_source_lobby["revision"],
                    "branch_id": source_branch["id"],
                    "idempotency_key": _phase_transition_key(
                        token, "source-return-play", campaign_source_lobby
                    ),
                },
            )
            await client.open()
            await client.load("play.scene", "play.scene_control", "play.characters")
            campaign_final = _facade_value(
                await client.core(
                    "campaign_query",
                    {"view": "get", "payload": {"campaign_id": args.campaign_id}},
                )
            )
            final_branches = _facade_value(
                await client.domain(
                    "branch_query", {"campaign_id": args.campaign_id, "view": "list"}
                )
            )
            final_source_branch = next(item for item in final_branches if item.get("is_current"))
            final_snapshot = await client.domain(
                "snapshot_create",
                {
                    "campaign_id": args.campaign_id,
                    "label": "Returned to source branch after isolated combat regression",
                    "expected_revision": campaign_final["revision"],
                    "expected_head_snapshot_id": (
                        final_source_branch.get("head_snapshot_id") or ""
                    ),
                    "idempotency_key": f"{token}-source-final-snapshot",
                },
            )
            final_verified = _facade_value(
                await client.domain(
                    "snapshot_query",
                    {
                        "campaign_id": args.campaign_id,
                        "view": "verify",
                        "payload": {"slot": final_snapshot["slot"]},
                    },
                )
            )
            final_actors = {
                actor_id: _facade_value(
                    await client.domain(
                        "character_query",
                        {"view": "get", "payload": {"character_id": actor_id}},
                    )
                )
                for actor_id in source_actor_ids
            }
            hp_after = {
                actor_id: _character_summary(actor)["hp"]
                for actor_id, actor in final_actors.items()
            }
            source_witness_knowledge = _facade_value(
                await client.domain(
                    "actor_knowledge_query",
                    {
                        "campaign_id": args.campaign_id,
                        "actor_id": args.target_id[0],
                        "view": "list",
                        "payload": {"branch_id": source_branch["id"]},
                    },
                )
            )
            source_caster_knowledge = _facade_value(
                await client.domain(
                    "actor_knowledge_query",
                    {
                        "campaign_id": args.campaign_id,
                        "actor_id": args.caster_id,
                        "view": "list",
                        "payload": {"branch_id": source_branch["id"]},
                    },
                )
            )
            comparison = _facade_value(
                await client.domain(
                    "branch_query",
                    {
                        "campaign_id": args.campaign_id,
                        "view": "compare",
                        "payload": {
                            "left_branch_id": source_branch["id"],
                            "right_branch_id": regression_branch_id,
                        },
                    },
                )
            )
            source_witness_keys = {
                str(item.get("knowledge_key")) for item in source_witness_knowledge
            }
            source_caster_keys = {
                str(item.get("knowledge_key")) for item in source_caster_knowledge
            }
            return {
                "action": "structured-combat",
                "transport": "stdio",
                "campaign_id": args.campaign_id,
                "source_branch_id": source_branch["id"],
                "regression_branch_id": regression_branch_id,
                "source_checkpoint": source_checkpoint,
                "readiness": readiness,
                "combat": {
                    "start": {
                        "campaign_revision": started.get("campaign_revision"),
                        "map": {
                            "id": battle_map.get("id"),
                            "lifecycle": battle_map.get("lifecycle"),
                            "source": battle_map.get("source"),
                            "bounds": battle_map.get("bounds"),
                        },
                    },
                    "visible_tool_count": len(combat_tools),
                    "lobby_tools_hidden": all(
                        item not in combat_tools
                        for item in ("character_create_from", "module_import", "rule_import")
                    ),
                    "cast": cast.get("result"),
                    "attacks": attack_results,
                    "end": {
                        "ended": ended.get("ended"),
                        "outcome": ended.get("outcome"),
                        "campaign_revision": ended.get("campaign_revision"),
                    },
                },
                "branch_events": {
                    "witness_event_id": witness_event.get("id"),
                    "caster_event_id": caster_event.get("id"),
                    "memory_id": branch_memory.get("id"),
                    "witness_has_key": witness_key
                    in {str(item.get("knowledge_key")) for item in witness_knowledge},
                    "caster_has_key": caster_key
                    in {str(item.get("knowledge_key")) for item in caster_knowledge},
                },
                "regression_snapshots": [play_snapshot, regression_lobby_snapshot],
                "checkout": checked_out,
                "final_snapshot": final_snapshot,
                "final_snapshot_verification": final_verified,
                "source_branch_isolation": {
                    "hp_restored": hp_after == hp_before,
                    "hp_before": hp_before,
                    "hp_after": hp_after,
                    "witness_key_absent": witness_key not in source_witness_keys,
                    "caster_key_absent": caster_key not in source_caster_keys,
                },
                "branch_comparison": comparison,
            }


def main() -> int:
    args = _arguments()
    operation = {
        "audit": _audit,
        "relock-core": _relock_core,
        "prepare-statblock": _prepare_statblock,
        "prepare-core-wizard": _prepare_core_wizard,
        "structured-combat": _structured_combat,
    }[args.action]
    report = asyncio.run(operation(args))
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
