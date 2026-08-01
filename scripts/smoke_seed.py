"""Seed a fresh deterministic campaign for an MCP end-to-end smoke run.

Usage:
    python scripts/smoke_seed.py --home C:/path/to/fresh-mcp-home

The command never migrates or rewrites an existing campaign. Point it at a new
MCP home when starting a fresh test run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from sagasmith_core.access import LOCAL_SYSTEM_PRINCIPAL_ID

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    _, result = await server.call_tool(name, arguments)
    if isinstance(result, dict) and "result" in result:
        return result["result"]
    return result


async def seed(home: Path) -> dict[str, Any]:
    os.environ["SAGASMITH_DND_MCP_HOME"] = str(home)
    server = create_server(
        McpConfig(
            home=home,
            database_url=None,
            chroma_url=None,
            chroma_path_override=None,
            dnd_skills_dir=Path(__file__).resolve().parents[2] / "SagaSmith-dnd-skills",
            modulegen_skills_dir=Path(__file__).resolve().parents[2]
            / "SagaSmith-module-gen-skills",
            auto_seed_rules=False,
        )
    )
    campaign = await _call(
        server,
        "campaign_create",
        {
            "name": "MCP Smoke: Split Lantern",
            "principal_id": LOCAL_SYSTEM_PRINCIPAL_ID,
            "idempotency_key": "smoke-campaign-create",
        },
    )
    pc_one = await _call(
        server,
        "character_create_from",
        {
            "mode": "direct",
            "payload": {
                "name": "Aria",
                "campaign_id": campaign["id"],
                "character_type": "pc",
                "player_name": "smoke-player-1",
            },
            "idempotency_key": "smoke-create-aria",
        },
    )
    pc_two = await _call(
        server,
        "character_create_from",
        {
            "mode": "direct",
            "payload": {
                "name": "Bram",
                "campaign_id": campaign["id"],
                "character_type": "pc",
                "player_name": "smoke-player-2",
            },
            "idempotency_key": "smoke-create-bram",
        },
    )
    npc = await _call(
        server,
        "character_create_from",
        {
            "mode": "direct",
            "payload": {
                "name": "The Lantern Warden",
                "campaign_id": campaign["id"],
                "character_type": "npc",
            },
            "idempotency_key": "smoke-create-warden",
        },
    )
    for principal_id, actor in (
        ("discord:smoke-player-1", pc_one),
        ("discord:smoke-player-2", pc_two),
    ):
        await _call(
            server,
            "access_grant",
            {
                "scope": "campaign",
                "campaign_id": campaign["id"],
                "principal_id": principal_id,
                "payload": {"role": "player"},
                "by_principal_id": LOCAL_SYSTEM_PRINCIPAL_ID,
            },
        )
        await _call(
            server,
            "access_grant",
            {
                "scope": "actor",
                "campaign_id": campaign["id"],
                "principal_id": principal_id,
                "payload": {
                    "actor_id": actor["id"],
                    "can_control": True,
                    "can_view_private": True,
                },
                "by_principal_id": LOCAL_SYSTEM_PRINCIPAL_ID,
            },
        )
    event = await _call(
        server,
        "campaign_event",
        {
            "campaign_id": campaign["id"],
            "action": "add",
            "payload": {
                "summary": "Aria and the Warden see the sealed lantern room.",
                "event_type": "scene",
                "audience_scope": "party",
                "known_by_actor_ids": [pc_one["id"], npc["id"]],
                "knowledge_key": "lantern-room-sealed",
                "knowledge_proposition": (
                    "The lantern room is sealed and the brass key is nearby."
                ),
            },
            "principal_id": LOCAL_SYSTEM_PRINCIPAL_ID,
            "idempotency_key": "smoke-lantern-room-event",
        },
    )
    wallet = await _call(
        server,
        "wallet_change",
        {
            "owner": "party",
            "action": "adjust",
            "owner_id": campaign["id"],
            "denomination": "gp",
            "amount": 25,
            "expected_revision": campaign["revision"],
            "idempotency_key": "smoke-initial-wallet",
            "principal_id": LOCAL_SYSTEM_PRINCIPAL_ID,
        },
    )
    snapshot = await _call(
        server,
        "snapshot_create",
        {
            "campaign_id": campaign["id"],
            "label": "smoke-baseline",
            "expected_revision": wallet["campaign"]["revision"],
            "expected_head_snapshot_id": "",
            "idempotency_key": "smoke-baseline-snapshot",
        },
    )
    return {
        "campaign_id": campaign["id"],
        "pc_ids": [pc_one["id"], pc_two["id"]],
        "npc_id": npc["id"],
        "event_id": event["id"],
        "actor_knowledge_ids": event["actor_knowledge_ids"],
        "wallet_revision": wallet["campaign"]["revision"],
        "baseline_snapshot_id": snapshot["id"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(seed(args.home)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
