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
        choices=("audit", "relock-core"),
        default="audit",
        help="Read-only audit or checkpointed adoption of the current built-in Core",
    )
    parser.add_argument(
        "--run-id",
        default="campaign-regression-v1",
        help="Stable idempotency namespace for mutating actions",
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
    spells = (sheet.get("content") or {}).get("spells") or []
    source_bound = any(
        item.get("source_key") or item.get("rule_refs") or item.get("mechanic_refs")
        for item in [*attacks, *spells]
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
            latest_snapshot = max(snapshots, key=lambda item: int(item.get("slot") or 0))
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
            token = "".join(character if character.isalnum() else "-" for character in args.run_id)
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


def main() -> int:
    args = _arguments()
    operation = _audit if args.action == "audit" else _relock_core
    report = asyncio.run(operation(args))
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
