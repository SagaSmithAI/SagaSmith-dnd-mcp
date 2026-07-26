from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from sagasmith_dnd_mcp.tool_profiles import GROUP_BY_ID


class _SequenceRng:
    def __init__(self, *values: int) -> None:
        self.values = list(values)

    def randint(self, minimum: int, maximum: int) -> int:
        value = self.values.pop(0)
        assert minimum <= value <= maximum
        return value


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def _config(tmp_path: Path, import_root: Path) -> McpConfig:
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        module_import_roots=(import_root,),
        auto_seed_rules=False,
    )


def test_chase_tools_are_a_play_phase_group() -> None:
    group = GROUP_BY_ID["play.chase"]
    assert group.phase == "play"
    assert group.tools == {
        "chase_start",
        "chase_query",
        "chase_take_turn",
        "chase_end",
    }


def test_public_chase_uses_exact_module_source_and_no_combat_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_root = tmp_path / "modules"
    import_root.mkdir()
    source_excerpt = (
        "A kenku has the Stone of Golorr and is 60 feet away at the start of the chase."
    )
    source = import_root / "chase.md"
    source.write_text(
        "# Chapter Four\n\n"
        "## Street Chase\n\n"
        "Use the chase rules and the Urban Chase Complications table. "
        f"{source_excerpt}\n\n"
        "### Next Encounter\n\n"
        "When the characters are close, the kenku ducks into an old tower.\n",
        encoding="utf-8",
    )
    original_advance = server_module.advance_chase_turn

    def deterministic_advance(*args, **kwargs):
        kwargs["rng"] = _SequenceRng(20)
        return original_advance(*args, **kwargs)

    monkeypatch.setattr(server_module, "advance_chase_turn", deterministic_advance)

    async def exercise() -> None:
        server = create_server(_config(tmp_path, import_root))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Source-reviewed chase",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        staged = await _call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "stage",
                "payload": {
                    "source_path": str(source),
                    "source_key": "chase-module",
                    "title": "Chase Module",
                },
                "idempotency_key": "stage",
            },
        )
        job_id = staged["job"]["id"]
        for action in ("inspect", "validate", "ingest"):
            await _call(
                server,
                "module_import",
                {
                    "campaign_id": campaign["id"],
                    "action": action,
                    "payload": {"job_id": job_id},
                    "idempotency_key": action,
                },
            )
        current_campaign = await _call(
            server, "campaign_get", {"campaign_id": campaign["id"]}
        )
        await _call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "activate",
                "payload": {"job_id": job_id},
                "expected_revision": current_campaign["revision"],
                "idempotency_key": "activate",
            },
        )
        hits = await _call(
            server,
            "module_search",
            {
                "campaign_id": campaign["id"],
                "query": "kenku Stone Golorr 60 feet chase",
            },
        )
        expanded = await _call(
            server,
            "module_expand",
            {"chunk_id": hits[0]["id"]},
        )
        source_ref = {
            "module_id": expanded["module"]["id"],
            "scene_id": expanded["scene"]["id"],
            "chunk_id": expanded["chunk_id"],
            "page_start": expanded["page_start"],
            "page_end": expanded["page_end"],
            "heading_path": expanded["heading_path"],
            "content_sha256": hashlib.sha256(
                expanded["content"].encode("utf-8")
            ).hexdigest(),
        }

        actor_sheet = default_character_sheet()
        actor_sheet["edition"] = "2014"
        actor_sheet["combat"]["hp"] = {"value": 20, "max": 20, "temp": 0}
        pursuer = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Pursuer",
                "sheet": actor_sheet,
                "idempotency_key": "pursuer",
            },
        )
        quarry = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Kenku",
                "character_type": "npc",
                "sheet": actor_sheet,
                "idempotency_key": "quarry",
            },
        )
        current_campaign = await _call(
            server, "campaign_get", {"campaign_id": campaign["id"]}
        )
        phase = await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": current_campaign["revision"],
                "idempotency_key": "play",
            },
        )
        started = await _call(
            server,
            "chase_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [pursuer["id"], quarry["id"]],
                "quarry_ids": [quarry["id"]],
                "initial_distance_ft": 60,
                "scene_id": expanded["scene"]["id"],
                "source_ref": source_ref,
                "source_excerpt": source_excerpt,
                "participant_config": [
                    {"actor_id": pursuer["id"], "initiative": 20, "tie_breaker": 0},
                    {"actor_id": quarry["id"], "initiative": 10, "tie_breaker": 1},
                ],
                "close_transition": {
                    "distance_ft": 0,
                    "status": "destination_reached",
                    "summary": "The kenku ducks into the old tower.",
                },
                "expected_revision": phase["campaign_revision"],
                "idempotency_key": "chase-start",
            },
        )

        assert started["chase"]["mode"] == "theater_of_the_mind"
        assert "battle_map" not in started["chase"]
        assert started["chase"]["source_ref"]["chunk_id"] == expanded["chunk_id"]
        assert all(
            receipt["mechanic_id"].startswith("dnd5e.core.chase.")
            or receipt["mechanic_id"] == "dnd5e.core.check.jack_of_all_trades"
            for receipt in started["rule_receipts"]
        )

        current_pursuer = await _call(
            server, "character_get", {"character_id": pursuer["id"]}
        )
        turn = await _call(
            server,
            "chase_take_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": pursuer["id"],
                "action": "dash",
                "expected_revision": started["campaign_revision"],
                "expected_actor_revision": current_pursuer["revision"],
                "idempotency_key": "pursuer-turn",
            },
        )

        assert turn["turn"]["moved_ft"] == 60
        assert turn["chase"]["active"] is False
        assert turn["chase"]["outcome"]["status"] == "destination_reached"
        queried = await _call(
            server, "chase_query", {"campaign_id": campaign["id"]}
        )
        assert queried["chase"]["outcome"] == turn["chase"]["outcome"]

    asyncio.run(exercise())
