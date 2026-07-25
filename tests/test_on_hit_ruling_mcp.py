from __future__ import annotations

import asyncio
from pathlib import Path

from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp import server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


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


def test_public_on_hit_ruling_applies_and_escapes_web_condition(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original_attack_roll = server_module.roll_attack_action
    original_actor_check = server_module.resolve_actor_check

    def forced_hit(*, plan, rng=None):
        result = original_attack_roll(plan=plan, rng=rng)
        result.update(
            natural=10,
            total=max(int(plan["target_ac"]), int(result.get("total", 0) or 0)),
            armor_class=int(plan["target_ac"]),
            hit=True,
            critical=False,
            fumble=False,
        )
        return result

    def forced_success(*args, **kwargs):
        result = original_actor_check(*args, **kwargs)
        result.update(total=int(kwargs["dc"]), success=True)
        return result

    monkeypatch.setattr(server_module, "roll_attack_action", forced_hit)
    monkeypatch.setattr(server_module, "resolve_actor_check", forced_success)

    async def exercise() -> None:
        server = create_server(_config(tmp_path))

        async def raw(name: str, arguments: dict):
            _, result = await server.call_tool(name, arguments)
            return result

        async def call(name: str, arguments: dict):
            result = await raw(name, arguments)
            return result.get("result", result)

        campaign = await call(
            "campaign_create",
            {
                "name": "Web on-hit ruling",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        spider = await call(
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Giant Spider",
                "character_type": "monster",
                "idempotency_key": "spider",
            },
        )
        target = await call(
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Target",
                "character_type": "pc",
                "idempotency_key": "target",
            },
        )
        web_effect = (
            "The target is restrained by webbing. As an action, the restrained "
            "target can make a DC 12 Strength check, bursting the webbing on a success."
        )
        spider_sheet = default_character_sheet()
        spider_sheet["combat"]["hp"] = {"value": 26, "max": 26, "temp": 0}
        spider_sheet["inventory"]["items"] = [
            {
                "id": "web",
                "name": "Web",
                "kind": "weapon",
                "equipped": True,
                "equipped_slot": "main_hand",
                "mechanics": {
                    "attack_type": "ranged",
                    "attack_ability": "dexterity",
                    "damage_formula": "",
                    "damage_type": "",
                    "on_hit_effect": web_effect,
                    "normal_range_ft": 30,
                    "long_range_ft": 60,
                    "attack_bonus_override": 5,
                    "always_available": True,
                },
            }
        ]
        spider_sheet["inventory"]["equipment_slots"]["main_hand"] = "web"
        target_sheet = default_character_sheet()
        target_sheet["combat"]["hp"] = {"value": 20, "max": 20, "temp": 0}
        for actor, sheet, key in (
            (spider, spider_sheet, "spider-sheet"),
            (target, target_sheet, "target-sheet"),
        ):
            await call(
                "character_sheet_replace",
                {
                    "character_id": actor["id"],
                    "sheet": sheet,
                    "expected_revision": actor["revision"],
                    "idempotency_key": key,
                },
            )
        campaign = await call("campaign_get", {"campaign_id": campaign["id"]})
        started = await raw(
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [spider["id"], target["id"]],
                "participant_config": [
                    {
                        "actor_id": spider["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                    },
                    {
                        "actor_id": target["id"],
                        "initiative": 10,
                        "position": {"x": 2, "y": 0},
                        "death_saves": True,
                    },
                ],
                "expected_revision": campaign["revision"],
                "idempotency_key": "start",
            },
        )
        attacked = await raw(
            "combat_resolve_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": spider["id"],
                "target_id": target["id"],
                "action": {"weapon_id": "web", "attack_mode": "ranged"},
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "web",
            },
        )
        assert attacked["status"] == "pending_ruling"
        choice_id = attacked["result"]["pending_on_hit_ruling_id"]
        ruled = await raw(
            "combat_on_hit_ruling",
            {
                "campaign_id": campaign["id"],
                "target_id": target["id"],
                "choice_id": choice_id,
                "selection": {
                    "id": "apply_condition",
                    "condition": "restrained",
                    "escape_dc": 12,
                    "escape_abilities": ["strength"],
                    "source_excerpt": web_effect,
                },
                "expected_revision": attacked["campaign_revision"],
                "idempotency_key": "rule-web",
            },
        )
        target_after_hit = await call(
            "character_get",
            {"character_id": target["id"]},
        )
        assert ruled["ongoing_effect"]["condition"] == "restrained"
        assert target_after_hit["sheet"]["conditions"] == ["restrained"]
        ended = await raw(
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": spider["id"],
                "expected_revision": ruled["campaign_revision"],
                "idempotency_key": "end-spider",
            },
        )
        escaped = await raw(
            "combat_check",
            {
                "campaign_id": campaign["id"],
                "actor_id": target["id"],
                "kind": "ability",
                "ability": "strength",
                "dc": 12,
                "action": "escape",
                "rule_facts": {"ongoing_effect_id": choice_id},
                "expected_revision": ended["campaign_revision"],
                "idempotency_key": "escape",
            },
        )
        assert escaped["result"]["escaped"] is True
        target_after_escape = await call(
            "character_get",
            {"character_id": target["id"]},
        )
        assert target_after_escape["sheet"]["conditions"] == []
        target_combatant = next(
            item
            for item in escaped["combat"]["combatants"]
            if item["actor_id"] == target["id"]
        )
        assert target_combatant["turn_budget"]["main_action"] == 0
        assert escaped["combat"]["ongoing_effects"][0]["active"] is False

    asyncio.run(exercise())
