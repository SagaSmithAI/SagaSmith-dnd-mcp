from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, structured = await server.call_tool(name, arguments)
    value = structured.get("result", structured) if isinstance(structured, dict) else structured
    if isinstance(value, dict) and "action" in value and "result" in value:
        return value["result"]
    return value


def _config(tmp_path: Path) -> McpConfig:
    workspace = Path(__file__).resolve().parents[2]
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=workspace / "SagaSmith-dnd-skills",
        modulegen_skills_dir=workspace / "SagaSmith-module-gen-skills",
        auto_seed_rules=False,
    )


def test_public_rolls_replay_after_restore_and_do_not_pollute_the_parent_branch(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Random stream branches",
                "edition": "2014",
                "random_seed": "fixed-regression-seed",
                "idempotency_key": "campaign",
            },
        )
        campaign_id = campaign["id"]
        initial_branches = await _call(
            server,
            "branch_query",
            {"campaign_id": campaign_id, "view": "list"},
        )
        main_branch_id = next(item["id"] for item in initial_branches if item["is_current"])
        initial_campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign_id}},
        )

        first = await _call(
            server,
            "dnd_dice_roll",
            {
                "campaign_id": campaign_id,
                "expression": "2d6",
                "expected_campaign_revision": initial_campaign["revision"],
                "idempotency_key": "first-roll",
            },
        )
        assert first["random_stream_receipt"]["position_before"] == 0
        assert first["random_stream_receipt"]["position_after"] == 2

        replay = await _call(
            server,
            "dnd_dice_roll",
            {
                "campaign_id": campaign_id,
                "expression": "2d6",
                "expected_campaign_revision": initial_campaign["revision"],
                "idempotency_key": "first-roll",
            },
        )
        assert replay == first
        after_replay = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign_id}},
        )
        assert after_replay["state"]["random_stream"]["position"] == 2
        assert after_replay["revision"] == initial_campaign["revision"] + 1

        checkpoint = await _call(
            server,
            "snapshot_create",
            {
                "campaign_id": campaign_id,
                "label": "Before branch roll",
                "expected_revision": after_replay["revision"],
                "expected_head_snapshot_id": "",
                "idempotency_key": "checkpoint-before",
            },
        )
        branch_roll = await _call(
            server,
            "dnd_dice_roll",
            {
                "campaign_id": campaign_id,
                "expression": "1d20",
                "expected_campaign_revision": after_replay["revision"],
                "idempotency_key": "parent-roll",
            },
        )
        assert branch_roll["random_stream_receipt"]["position_after"] == 3
        after_parent_roll = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign_id}},
        )

        restored = await _call(
            server,
            "snapshot_restore",
            {
                "campaign_id": campaign_id,
                "slot": checkpoint["slot"],
                "expected_revision": after_parent_roll["revision"],
                "expected_branch_id": main_branch_id,
                "idempotency_key": "restore-before-roll",
            },
        )
        restored_campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign_id}},
        )
        assert restored_campaign["state"]["random_stream"]["position"] == 2
        restored_branches = await _call(
            server,
            "branch_query",
            {"campaign_id": campaign_id, "view": "list"},
        )
        restored_branch_id = next(
            item["id"] for item in restored_branches if item["is_current"]
        )
        assert restored_branch_id != main_branch_id

        replayed_branch_roll = await _call(
            server,
            "dnd_dice_roll",
            {
                "campaign_id": campaign_id,
                "expression": "1d20",
                "expected_campaign_revision": restored_campaign["revision"],
                "idempotency_key": "restored-roll",
            },
        )
        assert replayed_branch_roll["rolls"] == branch_roll["rolls"]

        restored_head = restored["id"]
        restored_after_roll = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign_id}},
        )
        await _call(
            server,
            "snapshot_create",
            {
                "campaign_id": campaign_id,
                "label": "Restored branch roll",
                "expected_revision": restored_after_roll["revision"],
                "expected_head_snapshot_id": restored_head,
                "idempotency_key": "checkpoint-restored",
            },
        )
        await _call(
            server,
            "branch_change",
            {
                "campaign_id": campaign_id,
                "action": "checkout",
                "payload": {"branch_id": main_branch_id},
                "expected_revision": restored_after_roll["revision"],
                "expected_branch_id": restored_branch_id,
                "idempotency_key": "checkout-parent",
            },
        )
        parent = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign_id}},
        )
        final_branches = await _call(
            server,
            "branch_query",
            {"campaign_id": campaign_id, "view": "list"},
        )
        assert next(item["id"] for item in final_branches if item["is_current"]) == main_branch_id
        assert parent["state"]["random_stream"]["position"] == 3

    asyncio.run(exercise())


def test_stdio_character_roll_persists_and_replays_its_stream_receipt(tmp_path: Path) -> None:
    async def exercise() -> None:
        env = dict(os.environ)
        env.update(
            {
                "SAGASMITH_DND_MCP_HOME": str(tmp_path / "stdio-home"),
                "SAGASMITH_DND_MCP_AUTO_SEED": "0",
            }
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "sagasmith_dnd_mcp.server"],
            cwd=Path(__file__).parents[1],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                principal_id = "test:random-stream"
                opened = await session.call_tool(
                    "exposure_open",
                    {"principal_id": principal_id},
                )
                exposure_id = json.loads(opened.content[0].text)["exposure_id"]
                await session.call_tool(
                    "exposure_load",
                    {"exposure_id": exposure_id, "group_id": "lobby.bootstrap"},
                )
                created = await session.call_tool(
                    "campaign_create",
                    {
                        "name": "Character stream",
                        "edition": "2014",
                        "random_seed": "character-stream-seed",
                        "principal_id": principal_id,
                        "idempotency_key": "campaign",
                    },
                )
                campaign = json.loads(created.content[0].text)
                campaign_id = campaign["id"]

                opened = await session.call_tool(
                    "exposure_open",
                    {"campaign_id": campaign_id, "principal_id": principal_id},
                )
                exposure_id = json.loads(opened.content[0].text)["exposure_id"]
                await session.call_tool(
                    "exposure_load",
                    {"exposure_id": exposure_id, "group_id": "lobby.characters"},
                )
                actor_result = await session.call_tool(
                    "character_create_from",
                    {
                        "mode": "direct",
                        "payload": {
                            "campaign_id": campaign_id,
                            "name": "Stream Hero",
                            "sheet": default_character_sheet(),
                        },
                        "principal_id": principal_id,
                        "idempotency_key": "actor",
                    },
                )
                actor_payload = json.loads(actor_result.content[0].text)
                actor = actor_payload.get("result", actor_payload)
                arguments = {
                    "character_id": actor["id"],
                    "method": "roll_4d6_drop_lowest",
                    "principal_id": principal_id,
                    "expected_revision": actor["revision"],
                    "idempotency_key": "ability-roll",
                }
                rolled = await session.call_tool("character_ability_apply", arguments)
                payload = json.loads(rolled.content[0].text)
                receipt = payload["random_stream_receipt"]
                assert receipt["position_before"] == 0
                assert receipt["position_after"] == 24
                assert receipt["operation"] == "character_ability_apply"

                replayed = await session.call_tool("character_ability_apply", arguments)
                replay_payload = json.loads(replayed.content[0].text)
                assert replay_payload == payload
                campaign_result = await session.call_tool(
                    "campaign_query",
                    {
                        "view": "get",
                        "payload": {"campaign_id": campaign_id},
                        "principal_id": principal_id,
                    },
                )
                current = json.loads(campaign_result.content[0].text)["result"]
                assert current["state"]["random_stream"]["position"] == 24
                assert (
                    current["state"]["random_stream"]["last_receipt"]
                    == receipt
                )

    asyncio.run(exercise())
