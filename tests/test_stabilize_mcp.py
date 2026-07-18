import asyncio
import random
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


@pytest.mark.parametrize(("seed", "expected_success"), [(0, True), (2, False)])
def test_medicine_stabilization_pays_action_and_commits_target_atomically(
    tmp_path: Path, monkeypatch, seed: int, expected_success: bool
) -> None:
    original_check = server_module.resolve_actor_check

    def deterministic_check(actor, **kwargs):
        return original_check(actor, **kwargs, rng=random.Random(seed))

    monkeypatch.setattr(server_module, "resolve_actor_check", deterministic_check)
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
            {"name": "Stabilize", "edition": "2014", "idempotency_key": "stabilize-campaign"},
        )
        helper_sheet = default_character_sheet()
        helper_sheet["abilities"]["wisdom"]["score"] = 16
        helper_sheet["skills"]["medicine"]["proficiency"] = "proficient"
        helper = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Helper",
                "sheet": helper_sheet,
                "idempotency_key": "stabilize-helper",
            },
        )
        target_sheet = default_character_sheet()
        target_sheet["combat"]["hp"] = {"value": 0, "max": 12, "temp": 0}
        target_sheet["combat"]["death_saves"] = {"successes": 1, "failures": 2}
        target_sheet["conditions"] = ["prone", "unconscious"]
        target = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Target",
                "sheet": target_sheet,
                "idempotency_key": "stabilize-target",
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
                "idempotency_key": "stabilize-play",
            },
        )
        started = await _call(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [helper["id"], target["id"]],
                "participant_config": [
                    {
                        "actor_id": helper["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                        "disposition": "friendly",
                    },
                    {
                        "actor_id": target["id"],
                        "initiative": 10,
                        "position": {"x": 1, "y": 0},
                        "disposition": "friendly",
                    },
                ],
                "expected_revision": phase["campaign_revision"],
                "idempotency_key": "stabilize-start",
            },
        )
        result = await _call(
            server,
            "combat_check",
            {
                "campaign_id": campaign["id"],
                "actor_id": helper["id"],
                "target_id": target["id"],
                "kind": "stabilize",
                "ability": "wisdom",
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "stabilize-medicine",
            },
        )

        assert result["success"] is expected_success
        assert result["stabilized"] is expected_success
        assert result["skill"] == "medicine"
        status = await _call(
            server,
            "combat_query",
            {"campaign_id": campaign["id"], "view": "status"},
        )
        helper_combatant = next(
            item for item in status["combatants"] if item["actor_id"] == helper["id"]
        )
        assert helper_combatant["turn_budget"]["main_action"] == 0
        target_after = await _call(
            server, "character_get", {"character_id": target["id"]}
        )
        assert target_after["sheet"]["combat"]["hp"]["value"] == 0
        if expected_success:
            assert target_after["sheet"]["combat"]["death_saves"] == {
                "successes": 0,
                "failures": 0,
            }
            assert set(target_after["sheet"]["conditions"]) == {
                "prone",
                "stable",
                "unconscious",
            }
        else:
            assert target_after["sheet"]["combat"]["death_saves"] == {
                "successes": 1,
                "failures": 2,
            }
            assert set(target_after["sheet"]["conditions"]) == {
                "prone",
                "unconscious",
            }

    asyncio.run(exercise())
