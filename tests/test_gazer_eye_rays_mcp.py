from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


class _SequenceRng:
    def __init__(self, *values: int) -> None:
        self.values = list(values)

    def randint(self, minimum: int, maximum: int) -> int:
        value = self.values.pop(0)
        assert minimum <= value <= maximum
        return value


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


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


async def _raw(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result


def _gazer_sheet() -> dict:
    sheet = default_character_sheet()
    sheet["edition"] = "2014"
    sheet["traits"]["size"] = "tiny"
    sheet["progression"]["species"] = "aberration"
    sheet["combat"]["hp"] = {"value": 13, "max": 13, "temp": 0}
    sheet["content"]["activities"] = [
        {
            "id": "eye-rays-action",
            "name": "Eye Rays",
            "source_key": "Waterdeep: Dragon Heist p. 204",
            "description": (
                "The gazer shoots two of the following magical eye rays at random "
                "(reroll duplicates), choosing one or two targets it can see within "
                "60 feet of it: 1."
            ),
            "activation": {"type": "action", "cost": 1, "trigger": ""},
        },
        {
            "id": "dazing-ray-action",
            "name": "Dazing Ray",
            "source_key": "Waterdeep: Dragon Heist p. 204",
            "description": (
                "The targeted creature must succeed on a DC 12 Wisdom saving throw "
                "or be charmed until the start of the gazer's next turn. While the "
                "target is charmed in this way, its speed is halved, and it has "
                "disadvantage on attack rolls. 2."
            ),
            "activation": {"type": "action", "cost": 1, "trigger": ""},
        },
        {
            "id": "fear-ray-action",
            "name": "Fear Ray",
            "source_key": "Waterdeep: Dragon Heist p. 204",
            "description": (
                "The targeted creature must succeed on a DC 12 Wisdom saving throw "
                "or be frightened until the start of the gazer's next turn. 3."
            ),
            "activation": {"type": "action", "cost": 1, "trigger": ""},
        },
        {
            "id": "frost-ray-action",
            "name": "Frost Ray",
            "source_key": "Waterdeep: Dragon Heist p. 204",
            "description": (
                "The targeted creature must succeed on a DC 12 Dexterity saving "
                "throw or take 10 (3d6) cold damage. 4."
            ),
            "activation": {"type": "action", "cost": 1, "trigger": ""},
        },
        {
            "id": "telekinetic-ray-action",
            "name": "Telekinetic Ray",
            "source_key": "Waterdeep: Dragon Heist p. 204",
            "description": (
                "If the target is a creature that is Medium or smaller, it must "
                "succeed on a DC 12 Strength saving throw or be moved up to 30 feet "
                "directly away from the gazer."
            ),
            "activation": {"type": "action", "cost": 1, "trigger": ""},
        },
    ]
    return sheet


def test_eye_rays_commit_random_saves_and_expire_at_source_turn_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = server_module.resolve_random_save_effects

    def deterministic(source_actor, target_actors, **kwargs):
        return original(
            source_actor,
            target_actors,
            **kwargs,
            rng=_SequenceRng(1, 2, 1, 1),
        )

    monkeypatch.setattr(
        server_module,
        "resolve_random_save_effects",
        deterministic,
    )

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Gazer Eye Rays",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        gazer = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Gazer",
                "sheet": _gazer_sheet(),
                "idempotency_key": "gazer",
            },
        )
        target_sheet = default_character_sheet()
        target_sheet["combat"]["hp"] = {"value": 30, "max": 30, "temp": 0}
        target = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Target",
                "sheet": target_sheet,
                "idempotency_key": "target",
            },
        )
        campaign = await _call(
            server, "campaign_get", {"campaign_id": campaign["id"]}
        )
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
        started = await _raw(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [gazer["id"], target["id"]],
                "participant_config": [
                    {
                        "actor_id": gazer["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                        "disposition": "hostile",
                    },
                    {
                        "actor_id": target["id"],
                        "initiative": 10,
                        "position": {"x": 2, "y": 0},
                        "disposition": "friendly",
                        "death_saves": True,
                    },
                ],
                "expected_revision": phase["campaign_revision"],
                "idempotency_key": "start",
            },
        )

        with pytest.raises(Exception, match="invalid target count"):
            await _raw(
                server,
                "combat_use_activity",
                {
                    "campaign_id": campaign["id"],
                    "actor_id": gazer["id"],
                    "activity_id": "eye-rays-action",
                    "declaration": {"target_ids": []},
                    "expected_revision": started["campaign_revision"],
                    "idempotency_key": "out-of-range",
                },
            )

        resolved = await _raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": gazer["id"],
                "activity_id": "eye-rays-action",
                "declaration": {"target_ids": [target["id"]]},
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "eye-rays",
            },
        )

        assert resolved["status"] == "committed"
        effect = resolved["result"]["core_effect"]
        assert effect["kind"] == "random_save_effects"
        assert effect["selected_effect_ids"] == ["dazing-ray", "fear-ray"]
        assert [item["outcome"] for item in effect["targets"]] == [
            "condition",
            "condition",
        ]
        assert any(
            item["mechanic_id"] == "dnd5e.core.activity.random_save_effects"
            for item in resolved["result"]["rule_receipts"]
        )
        current = resolved["combat"]["combatants"][
            resolved["combat"]["turn_index"]
        ]
        assert current["actor_id"] == gazer["id"]
        assert current["turn_budget"]["main_action"] == 0
        target_after = await _call(
            server, "character_get", {"character_id": target["id"]}
        )
        assert set(target_after["sheet"]["conditions"]) == {
            "charmed",
            "frightened",
        }

        target_turn = await _raw(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": gazer["id"],
                "expected_revision": resolved["campaign_revision"],
                "idempotency_key": "end-gazer",
            },
        )
        current = target_turn["combat"]["combatants"][
            target_turn["combat"]["turn_index"]
        ]
        assert current["actor_id"] == target["id"]
        assert current["turn_budget"]["movement"] == 15

        source_turn = await _raw(
            server,
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": target["id"],
                "expected_revision": target_turn["campaign_revision"],
                "idempotency_key": "end-target",
            },
        )
        assert len(source_turn["effects_expired"]) == 2
        target_expired = await _call(
            server, "character_get", {"character_id": target["id"]}
        )
        assert target_expired["sheet"]["conditions"] == []
        assert all(
            not item["active"] for item in target_expired["sheet"]["effects"]
        )

        reapplied = await _raw(
            server,
            "combat_use_activity",
            {
                "campaign_id": campaign["id"],
                "actor_id": gazer["id"],
                "activity_id": "eye-rays-action",
                "declaration": {"target_ids": [target["id"]]},
                "expected_revision": source_turn["campaign_revision"],
                "idempotency_key": "eye-rays-before-combat-end",
            },
        )
        assert reapplied["status"] == "committed"
        ended = await _raw(
            server,
            "combat_end",
            {
                "campaign_id": campaign["id"],
                "outcome": {
                    "status": "interrupted",
                    "summary": "The encounter ends before the gazer's next turn.",
                },
                "expected_revision": reapplied["campaign_revision"],
                "idempotency_key": "end-before-source-turn",
            },
        )
        assert len(ended["effects_expired"]) == 2
        target_after_end = await _call(
            server, "character_get", {"character_id": target["id"]}
        )
        assert target_after_end["sheet"]["conditions"] == []
        ended_effects = target_after_end["sheet"]["effects"][-2:]
        assert all(
            not effect["active"] and effect["ended_reason"] == "combat_ended"
            for effect in ended_effects
        )

    asyncio.run(exercise())
