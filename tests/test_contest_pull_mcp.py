from __future__ import annotations

import asyncio
from pathlib import Path

from sagasmith_dnd import combat_engine as combat_engine_module
from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp import server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


def test_public_harpoon_attack_settles_contest_and_moves_target_atomically(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original_attack_roll = server_module.roll_attack_action

    def forced_hit(*, plan, rng=None):
        result = original_attack_roll(plan=plan, rng=rng)
        result.update(
            natural=10,
            total=max(int(plan["target_ac"]), 20),
            armor_class=int(plan["target_ac"]),
            hit=True,
            critical=False,
            fumble=False,
        )
        return result

    def forced_source_win(
        source_actor,
        target_actor,
        *,
        source_ability,
        target_ability,
        **_kwargs,
    ):
        return {
            "kind": "ability_contest",
            "source_actor_id": source_actor["id"],
            "target_actor_id": target_actor["id"],
            "source_ability": source_ability,
            "target_ability": target_ability,
            "source_check": {"natural": 15, "total": 19},
            "target_check": {"natural": 10, "total": 10},
            "tie": False,
            "winner_actor_id": source_actor["id"],
            "outcome": "source_wins",
        }

    monkeypatch.setattr(server_module, "roll_attack_action", forced_hit)
    monkeypatch.setattr(
        combat_engine_module,
        "resolve_actor_contest",
        forced_source_win,
    )

    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {
                "name": "Harpoon contest pull",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        merrow_sheet = default_character_sheet()
        merrow_sheet["abilities"]["strength"]["score"] = 18
        merrow_sheet["inventory"]["items"] = [
            {
                "id": "harpoon",
                "name": "Harpoon",
                "kind": "weapon",
                "mechanics": {
                    "attack_type": "melee",
                    "attack_ability": "strength",
                    "attack_bonus_override": 6,
                    "damage_formula": "2d6",
                    "damage_type": "piercing",
                    "damage_bonus_override": 4,
                    "always_available": True,
                    "properties": ["thrown"],
                    "thrown_normal_range_ft": 20,
                    "thrown_long_range_ft": 60,
                    "on_hit_resolution": {
                        "kind": "contest_pull",
                        "trigger": "weapon_hit",
                        "required_target_kind": "creature",
                        "maximum_target_size": "huge",
                        "source_ability": "strength",
                        "target_ability": "strength",
                        "ties": "no_movement",
                        "maximum_distance_ft": 20,
                        "direction": "toward_source",
                        "automatic": True,
                        "source_excerpt": (
                            "If the target is a Huge or smaller creature, it "
                            "must succeed on a Strength contest against the "
                            "merrow or be pulled up to 20 feet toward the merrow."
                        ),
                    },
                },
            }
        ]
        merrow = await call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Merrow",
                "character_type": "monster",
                "sheet": merrow_sheet,
                "idempotency_key": "merrow",
            },
        )
        target_sheet = default_character_sheet()
        target_sheet["abilities"]["strength"]["score"] = 10
        target_sheet["combat"]["hp"] = {"value": 100, "max": 100, "temp": 0}
        target_sheet["combat"]["ac"]["override"] = 10
        target = await call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Target",
                "character_type": "pc",
                "sheet": target_sheet,
                "idempotency_key": "target",
            },
        )
        current = await call(
            server,
            "campaign_get",
            {"campaign_id": campaign["id"]},
        )
        phase = await call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": current["revision"],
                "idempotency_key": "play",
            },
        )
        started = await call(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [merrow["id"], target["id"]],
                "participant_config": [
                    {
                        "actor_id": merrow["id"],
                        "initiative": 20,
                        "position": {"x": 1, "y": 1},
                        "disposition": "hostile",
                        "death_saves": False,
                    },
                    {
                        "actor_id": target["id"],
                        "initiative": 10,
                        "position": {"x": 5, "y": 1},
                        "disposition": "friendly",
                        "death_saves": True,
                    },
                ],
                "expected_revision": phase["campaign_revision"],
                "idempotency_key": "start",
            },
        )

        _, attacked = await server.call_tool(
            "combat_resolve_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": merrow["id"],
                "target_id": target["id"],
                "action": {
                    "weapon_id": "harpoon",
                    "attack_mode": "ranged",
                },
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "harpoon",
            },
        )

        settlement = attacked["result"]["structured_on_hit"]
        assert settlement["contest"]["outcome"] == "source_wins"
        assert settlement["forced_movement"]["settlement"][
            "moved_distance_ft"
        ] == 15
        assert settlement["forced_movement"]["settlement"]["destination"] == {
            "x": 2,
            "y": 1,
        }
        target_combatant = next(
            item
            for item in attacked["combat"]["combatants"]
            if item["actor_id"] == target["id"]
        )
        assert target_combatant["position"] == {"x": 2, "y": 1}
        assert attacked["status"] == "committed"
        assert not any(
            item.get("trigger") == "attack_on_hit_effect"
            for item in attacked["combat"]["pending"]
        )
        target_after = await call(
            server,
            "character_get",
            {"character_id": target["id"]},
        )
        assert target_after["sheet"]["combat"]["hp"]["value"] < 100

    asyncio.run(exercise())
