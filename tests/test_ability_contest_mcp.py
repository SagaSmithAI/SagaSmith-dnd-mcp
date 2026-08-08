from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def _config(tmp_path: Path) -> McpConfig:
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )


def test_character_check_facade_rejects_attack_kind_before_actor_lookup(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Reject generic attack check",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        current = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": current["revision"],
                "idempotency_key": "enter-play",
            },
        )
        with pytest.raises(
            Exception,
            match=r"payload.kind must be ability, check, save, or death_save",
        ):
            await _call(
                server,
                "character_check",
                {
                    "campaign_id": campaign["id"],
                    "action": "check",
                    "payload": {
                        "actor_id": "missing-actor",
                        "kind": "attack",
                        "ability": "strength",
                    },
                },
            )

    asyncio.run(exercise())


def test_character_check_contest_is_atomic_branch_scoped_and_replayable(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Ability contest",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        source_sheet = default_character_sheet()
        source_sheet["abilities"]["charisma"]["score"] = 16
        source_sheet["skills"]["deception"]["proficiency"] = "expertise"
        target_sheet = default_character_sheet()
        target_sheet["abilities"]["wisdom"]["score"] = 14
        target_sheet["skills"]["insight"]["proficiency"] = "proficient"
        source = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Deceiver",
                    "sheet": source_sheet,
                },
                "principal_id": "system:local",
                "idempotency_key": "source",
            },
        )
        target = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Observer",
                    "sheet": target_sheet,
                },
                "principal_id": "system:local",
                "idempotency_key": "target",
            },
        )
        current = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": current["revision"],
                "idempotency_key": "enter-play",
            },
        )
        current = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        branches = await _call(
            server,
            "branch_query",
            {"campaign_id": campaign["id"], "view": "list"},
        )
        branch_id = next(item["id"] for item in branches if item["is_current"])
        arguments = {
            "campaign_id": campaign["id"],
            "action": "contest",
            "payload": {
                "source_actor_id": source["id"],
                "target_actor_id": target["id"],
                "source_ability": "deception",
                "target_ability": "insight",
                "source_bonus": 2,
                "target_advantage": True,
            },
            "expected_revision": current["revision"],
            "branch_id": branch_id,
            "idempotency_key": "contest",
        }

        with pytest.raises(Exception, match="skill checks derive proficiency and expertise"):
            await _call(
                server,
                "character_check",
                {
                    "campaign_id": campaign["id"],
                    "action": "check",
                    "payload": {
                        "actor_id": source["id"],
                        "kind": "ability",
                        "ability": "deception",
                        "proficient": True,
                    },
                    "expected_revision": current["revision"],
                    "branch_id": branch_id,
                    "idempotency_key": "invalid-check-override",
                },
            )
        with pytest.raises(Exception, match="contest source skill derives"):
            await _call(
                server,
                "character_check",
                {
                    **arguments,
                    "payload": {
                        **arguments["payload"],
                        "source_proficient": True,
                    },
                    "idempotency_key": "invalid-contest-override",
                },
            )
        adjusted_check = await _call(
            server,
            "character_check",
            {
                "campaign_id": campaign["id"],
                "action": "check",
                "payload": {
                    "actor_id": source["id"],
                    "kind": "ability",
                    "ability": "deception",
                    "dc": 10,
                    "bonus": 2,
                },
                "expected_revision": current["revision"],
                "branch_id": branch_id,
                "idempotency_key": "source-modified-skill-check",
            },
        )
        assert adjusted_check["total"] - adjusted_check["natural"] == 9
        current = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        arguments["expected_revision"] = current["revision"]
        settled = await _call(server, "character_check", arguments)
        replay = await _call(server, "character_check", arguments)

        assert replay == settled
        assert settled["kind"] == "ability_contest"
        assert settled["source_actor_id"] == source["id"]
        assert settled["target_actor_id"] == target["id"]
        assert settled["target_check"]["roll_mode"] == "advantage"
        assert len(settled["target_check"]["rolls"]) == 2
        assert "dc" not in settled["source_check"]
        assert "success" not in settled["target_check"]
        assert settled["source_check"]["total"] - settled["source_check"]["natural"] == 9
        source_total = settled["source_check"]["total"]
        target_total = settled["target_check"]["total"]
        assert settled["tie"] is (source_total == target_total)
        assert settled["winner_actor_id"] == (
            ""
            if source_total == target_total
            else source["id"]
            if source_total > target_total
            else target["id"]
        )
        after = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        assert after["revision"] == current["revision"] + 1
        assert after["state"]["resolution_log"][-1]["type"] == "ability_contest"

    asyncio.run(exercise())


def test_character_check_group_is_atomic_branch_scoped_and_replayable(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Group ability check",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        actors = []
        for index in range(6):
            sheet = default_character_sheet()
            sheet["abilities"]["dexterity"]["score"] = 12 + index
            actors.append(
                await _call(
                    server,
                    "character_create_from",
                    {
                        "mode": "direct",
                        "payload": {
                            "campaign_id": campaign["id"],
                            "name": f"Scout {index + 1}",
                            "sheet": sheet,
                        },
                        "principal_id": "system:local",
                        "idempotency_key": f"scout-{index + 1}",
                    },
                )
            )
        current = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": current["revision"],
                "idempotency_key": "enter-play",
            },
        )
        current = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        branches = await _call(
            server,
            "branch_query",
            {"campaign_id": campaign["id"], "view": "list"},
        )
        branch_id = next(item["id"] for item in branches if item["is_current"])
        arguments = {
            "campaign_id": campaign["id"],
            "action": "group",
            "payload": {
                "actor_ids": [actor["id"] for actor in actors],
                "ability": "stealth",
                "dc": 15,
                "advantage": True,
            },
            "expected_revision": current["revision"],
            "branch_id": branch_id,
            "idempotency_key": "group-stealth",
        }

        settled = await _call(server, "character_check", arguments)
        replay = await _call(server, "character_check", arguments)

        assert replay == settled
        assert settled["kind"] == "ability_group_check"
        assert settled["participant_count"] == 6
        assert settled["required_successes"] == 3
        assert settled["success"] is (settled["success_count"] >= 3)
        assert {participant["actor_id"] for participant in settled["participants"]} == {
            actor["id"] for actor in actors
        }
        assert all(
            participant["check"]["roll_mode"] == "advantage"
            for participant in settled["participants"]
        )
        assert [item["mechanic_id"] for item in settled["rule_receipts"]] == [
            "dnd5e.core.check.group"
        ]
        after = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        assert after["revision"] == current["revision"] + 1
        assert after["state"]["resolution_log"][-1]["type"] == "ability_group_check"

        with pytest.raises(Exception, match="must be unique"):
            await _call(
                server,
                "character_check",
                {
                    **arguments,
                    "payload": {
                        **arguments["payload"],
                        "actor_ids": [actors[0]["id"], actors[0]["id"]],
                    },
                    "expected_revision": after["revision"],
                    "idempotency_key": "duplicate-group",
                },
            )

    asyncio.run(exercise())


def test_character_check_contest_rejects_2024_campaigns(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "No 2024 contest fallback",
                "edition": "2024",
                "idempotency_key": "campaign",
            },
        )
        actors = []
        for index in range(2):
            actors.append(
                await _call(
                    server,
                    "character_create_from",
                    {
                        "mode": "direct",
                        "payload": {"campaign_id": campaign["id"], "name": f"Actor {index + 1}"},
                        "principal_id": "system:local",
                        "idempotency_key": f"actor-{index + 1}",
                    },
                )
            )
        current = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": current["revision"],
                "idempotency_key": "enter-play",
            },
        )
        current = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )

        with pytest.raises(Exception, match="2014 rules procedure"):
            await _call(
                server,
                "character_check",
                {
                    "campaign_id": campaign["id"],
                    "action": "contest",
                    "payload": {
                        "source_actor_id": actors[0]["id"],
                        "target_actor_id": actors[1]["id"],
                        "source_ability": "strength",
                        "target_ability": "strength",
                    },
                    "expected_revision": current["revision"],
                    "idempotency_key": "contest",
                },
            )

    asyncio.run(exercise())
