import asyncio
from pathlib import Path

import pytest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def test_scene_readiness_blocks_missing_combatants_and_reserves(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Participant readiness", "idempotency_key": "campaign"},
        )
        artifact = await _call(
            server,
            "module_write",
            {
                "name": "ambush.md",
                "content": (
                    "# Chapter\n## Ambush\n"
                    "Captain Rusk and two bandits attack the party. "
                    "A tavern guard can be persuaded to join on the next round."
                ),
            },
        )
        await _call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "artifact": artifact["artifact"],
                "idempotency_key": "module",
            },
        )
        scene = next(
            item
            for item in await _call(server, "module_index", {"campaign_id": campaign["id"]})
            if item["title"] == "Ambush"
        )

        actors = {}
        for key, character_type in (
            ("hero", "pc"),
            ("captain", "npc"),
            ("bandit1", "monster"),
            ("bandit2", "monster"),
            ("guard", "npc"),
        ):
            actors[key] = await _call(
                server,
                "character_create",
                {
                    "campaign_id": campaign["id"],
                    "name": key,
                    "character_type": character_type,
                    "idempotency_key": f"actor-{key}",
                },
            )

        def manifest(bandit_ids: list[str]) -> dict:
            return {
                "schema_version": 1,
                "groups": [
                    {
                        "key": "captain-rusk",
                        "label": "Captain Rusk",
                        "role": "combatant",
                        "required_count": 1,
                        "actor_ids": [actors["captain"]["id"]],
                        "source_excerpt": "Captain Rusk and two bandits attack the party.",
                    },
                    {
                        "key": "rusk-bandits",
                        "label": "Rusk's bandits",
                        "role": "combatant",
                        "required_count": 2,
                        "actor_ids": bandit_ids,
                        "source_excerpt": "Captain Rusk and two bandits attack the party.",
                    },
                    {
                        "key": "tavern-guard",
                        "label": "Persuadable tavern guard",
                        "role": "reinforcement",
                        "required_count": 1,
                        "actor_ids": [actors["guard"]["id"]],
                        "source_excerpt": (
                            "A tavern guard can be persuaded to join on the next round."
                        ),
                    },
                ],
            }

        incomplete = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "readiness",
                "payload": {
                    "scene_id": scene["scene_id"],
                    "participant_manifest": manifest([actors["bandit1"]["id"]]),
                },
            },
        )
        assert incomplete["ready"] is False
        assert next(item for item in incomplete["groups"] if item["key"] == "rusk-bandits")[
            "missing_count"
        ] == 1

        complete_manifest = manifest([actors["bandit1"]["id"], actors["bandit2"]["id"]])
        ready = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "readiness",
                "payload": {
                    "scene_id": scene["scene_id"],
                    "participant_manifest": complete_manifest,
                },
            },
        )
        assert ready["ready"] is True
        assert ready["initial_actor_ids"] == [
            actors["captain"]["id"],
            actors["bandit1"]["id"],
            actors["bandit2"]["id"],
        ]
        assert ready["reinforcement_actor_ids"] == [actors["guard"]["id"]]
        assert ready["checksum"]

        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
        phase = await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": campaign["revision"],
                "idempotency_key": "play",
            },
        )
        with pytest.raises(Exception, match="omit manifest combatants"):
            await _call(
                server,
                "combat_start",
                {
                    "campaign_id": campaign["id"],
                    "participant_ids": [
                        actors["hero"]["id"],
                        actors["captain"]["id"],
                        actors["bandit1"]["id"],
                    ],
                    "participant_manifest": complete_manifest,
                    "scene_id": scene["scene_id"],
                    "expected_revision": phase["campaign_revision"],
                    "idempotency_key": "start-missing",
                },
            )

        participant_ids = [
            actors["hero"]["id"],
            actors["captain"]["id"],
            actors["bandit1"]["id"],
            actors["bandit2"]["id"],
        ]
        started = await _call(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": participant_ids,
                "participant_config": [
                    {"actor_id": actor_id, "initiative": 20 - index, "tie_breaker": index}
                    for index, actor_id in enumerate(participant_ids)
                ],
                "participant_manifest": complete_manifest,
                "scene_id": scene["scene_id"],
                "expected_revision": phase["campaign_revision"],
                "idempotency_key": "start-complete",
            },
        )
        assert started["combat"]["participant_manifest"]["checksum"] == ready["checksum"]
        assert actors["guard"]["id"] not in {
            item["actor_id"] for item in started["combat"]["combatants"]
        }

    asyncio.run(exercise())
