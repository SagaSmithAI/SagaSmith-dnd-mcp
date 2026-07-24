import asyncio
from pathlib import Path

from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


async def _raw_call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result


def test_combat_join_queues_actor_until_next_round(tmp_path: Path) -> None:
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
            {"name": "Reinforcements", "edition": "2014", "idempotency_key": "join-campaign"},
        )
        actors = []
        for index, name in enumerate(("Fast", "Slow", "Ally")):
            sheet = default_character_sheet()
            if name == "Fast":
                sheet["abilities"]["charisma"]["score"] = 18
                sheet["skills"]["persuasion"]["proficiency"] = "proficient"
            actor = await _call(
                server,
                "character_create",
                {
                    "campaign_id": campaign["id"],
                    "name": name,
                    "sheet": sheet,
                    "idempotency_key": f"join-actor-{index}",
                },
            )
            actors.append(actor)
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
        phase = await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": campaign["revision"],
                "idempotency_key": "join-play",
            },
        )
        started = await _call(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [actors[0]["id"], actors[1]["id"]],
                "participant_config": [
                    {"actor_id": actors[0]["id"], "initiative": 20, "tie_breaker": 0},
                    {"actor_id": actors[1]["id"], "initiative": 10, "tie_breaker": 1},
                ],
                "expected_revision": phase["campaign_revision"],
                "idempotency_key": "join-start",
            },
        )
        persuaded = await _raw_call(
            server,
            "combat_check",
            {
                "campaign_id": campaign["id"],
                "actor_id": actors[0]["id"],
                "kind": "check",
                "ability": "persuasion",
                "action": "improvise",
                "dc": 1,
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "join-persuade",
            },
        )
        assert persuaded["result"]["skill"] == "persuasion"
        assert persuaded["result"]["success"] is True
        fast_combatant = next(
            item
            for item in persuaded["combat"]["combatants"]
            if item["actor_id"] == actors[0]["id"]
        )
        assert fast_combatant["turn_budget"]["main_action"] == 0

        joined = await _call(
            server,
            "combat_join",
            {
                "campaign_id": campaign["id"],
                "actor_id": actors[2]["id"],
                "participant_config": {
                    "initiative": 15,
                    "tie_breaker": 2,
                    "disposition": "friendly",
                },
                "expected_revision": persuaded["campaign_revision"],
                "idempotency_key": "join-queue",
            },
        )
        assert joined["queued"]["actor_id"] == actors[2]["id"]
        assert joined["queued"]["join_round"] == 2
        assert actors[2]["id"] not in {
            item["actor_id"] for item in joined["combat"]["combatants"]
        }

        first_end = await _call(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": actors[0]["id"],
                "expected_revision": joined["campaign_revision"],
                "idempotency_key": "join-fast-end",
            },
        )
        second_end = await _call(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": actors[1]["id"],
                "expected_revision": first_end["campaign_revision"],
                "idempotency_key": "join-slow-end",
            },
        )
        assert second_end["combat"]["round"] == 2
        assert [item["actor_id"] for item in second_end["combat"]["combatants"]] == [
            actors[0]["id"],
            actors[2]["id"],
            actors[1]["id"],
        ]
        assert second_end["combat"]["reinforcements"] == []
        ended = await _call(
            server,
            "combat_end",
            {
                "campaign_id": campaign["id"],
                "outcome": {
                    "status": "victory",
                    "summary": "The queued ally entered and the opposition withdrew.",
                },
                "expected_revision": second_end["campaign_revision"],
                "idempotency_key": "join-combat-end",
            },
        )
        assert ended["outcome"]["status"] == "victory"
        assert ended["combat"]["outcome"] == ended["outcome"]
        assert ended["combat"]["snapshot_role"] == "historical_final_encounter"
        assert ended["combat"]["combatant_state_is_current"] is False
        assert ended["combat"]["current_character_state_source"] == "character_query"

        status = await _call(
            server,
            "combat_query",
            {"campaign_id": campaign["id"], "view": "status"},
        )
        assert status["snapshot_role"] == "historical_final_encounter"
        assert status["combatant_state_is_current"] is False

    asyncio.run(exercise())


def test_combat_end_accepts_source_surrender_outcome(tmp_path: Path) -> None:
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
            {
                "name": "Source surrender",
                "edition": "2014",
                "idempotency_key": "surrender-campaign",
            },
        )
        actor = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Source Hostile",
                "sheet": default_character_sheet(),
                "idempotency_key": "surrender-actor",
            },
        )
        campaign = await _call(server, "campaign_get", {"campaign_id": campaign["id"]})
        phase = await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": campaign["revision"],
                "idempotency_key": "surrender-play",
            },
        )
        started = await _call(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [actor["id"]],
                "expected_revision": phase["campaign_revision"],
                "idempotency_key": "surrender-start",
            },
        )
        ended = await _call(
            server,
            "combat_end",
            {
                "campaign_id": campaign["id"],
                "outcome": {
                    "status": "surrender",
                    "summary": (
                        "The source-designated hostile surrendered at the authored "
                        "hit-point threshold."
                    ),
                },
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "surrender-end",
            },
        )

        assert ended["outcome"]["status"] == "surrender"
        assert ended["combat"]["outcome"] == ended["outcome"]

    asyncio.run(exercise())
