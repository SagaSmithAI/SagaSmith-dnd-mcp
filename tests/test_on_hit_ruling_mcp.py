from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet
from sagasmith_dnd.engine import DiceResult

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


def test_public_guiding_bolt_effect_grants_and_consumes_next_attack_advantage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original_attack_roll = server_module.roll_attack_action

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

    monkeypatch.setattr(server_module, "roll_attack_action", forced_hit)

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
                "name": "Guiding Bolt next attack advantage",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        caster = await call(
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Caster",
                "character_type": "pc",
                "idempotency_key": "caster",
            },
        )
        ally = await call(
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Ally",
                "character_type": "pc",
                "idempotency_key": "ally",
            },
        )
        target = await call(
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Target",
                "character_type": "monster",
                "idempotency_key": "target",
            },
        )
        effect = (
            "The next attack against the target before the end of the caster's "
            "next turn has advantage."
        )
        for actor, key in (
            (caster, "caster-sheet"),
            (ally, "ally-sheet"),
            (target, "target-sheet"),
        ):
            sheet = default_character_sheet()
            sheet["combat"]["hp"] = {"value": 20, "max": 20, "temp": 0}
            if actor["id"] == caster["id"]:
                sheet["inventory"]["items"] = [
                    {
                        "id": "guiding-bolt-test",
                        "name": "Guiding Bolt Test",
                        "kind": "weapon",
                        "equipped": True,
                        "equipped_slot": "main_hand",
                        "mechanics": {
                            "attack_type": "ranged",
                            "attack_ability": "spell",
                            "damage_formula": "",
                            "damage_type": "",
                            "on_hit_effect": effect,
                            "normal_range_ft": 120,
                            "long_range_ft": 120,
                            "attack_bonus_override": 99,
                            "always_available": True,
                        },
                    }
                ]
                sheet["inventory"]["equipment_slots"]["main_hand"] = (
                    "guiding-bolt-test"
                )
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
                "participant_ids": [caster["id"], ally["id"], target["id"]],
                "participant_config": [
                    {
                        "actor_id": caster["id"],
                        "initiative": 30,
                        "position": {"x": 0, "y": 0},
                        "disposition": "friendly",
                    },
                    {
                        "actor_id": ally["id"],
                        "initiative": 20,
                        "position": {"x": 1, "y": 0},
                        "disposition": "friendly",
                    },
                    {
                        "actor_id": target["id"],
                        "initiative": 10,
                        "position": {"x": 2, "y": 0},
                        "disposition": "hostile",
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
                "actor_id": caster["id"],
                "target_id": target["id"],
                "action": {
                    "weapon_id": "guiding-bolt-test",
                    "attack_mode": "ranged",
                },
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "guiding-bolt",
            },
        )
        assert attacked["status"] == "pending_ruling"
        pending = next(
            item
            for item in attacked["combat"]["pending"]
            if item["id"] == attacked["result"]["pending_on_hit_ruling_id"]
        )
        assert any(
            candidate["id"] == "next_attack_advantage"
            for candidate in pending["candidates"]
        )
        ruled = await raw(
            "combat_on_hit_ruling",
            {
                "campaign_id": campaign["id"],
                "target_id": target["id"],
                "choice_id": attacked["result"]["pending_on_hit_ruling_id"],
                "selection": {
                    "id": "next_attack_advantage",
                    "source_excerpt": effect,
                },
                "expected_revision": attacked["campaign_revision"],
                "idempotency_key": "rule-guiding-bolt",
            },
        )
        effect_id = ruled["ongoing_effect"]["id"]
        assert ruled["ongoing_effect"]["expires_on_round"] == 2
        ended = await raw(
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": caster["id"],
                "expected_revision": ruled["campaign_revision"],
                "idempotency_key": "end-caster",
            },
        )
        plan = await call(
            "combat_preflight_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": ally["id"],
                "target_id": target["id"],
                "action": {
                    "weapon_id": "unarmed-strike",
                    "attack_mode": "melee",
                },
            },
        )
        assert plan["advantage"] is True
        assert plan["next_attack_advantage_effect_id"] == effect_id
        consumed = await raw(
            "combat_resolve_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": ally["id"],
                "target_id": target["id"],
                "action": {
                    "weapon_id": "unarmed-strike",
                    "attack_mode": "melee",
                },
                "expected_revision": ended["campaign_revision"],
                "idempotency_key": "consume-guiding-bolt",
            },
        )
        assert (
            consumed["result"]["consumed_next_attack_advantage_effect_id"]
            == effect_id
        )
        stored = next(
            item
            for item in consumed["combat"]["ongoing_effects"]
            if item["id"] == effect_id
        )
        assert stored["active"] is False
        assert stored["resolution"]["kind"] == "attack_roll"

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("save_success", "starting_hp"),
    [(True, 20), (False, 4)],
)
def test_public_on_hit_ruling_resolves_spider_bite_poison(
    tmp_path: Path,
    monkeypatch,
    save_success: bool,
    starting_hp: int,
) -> None:
    original_attack_roll = server_module.roll_attack_action
    original_actor_check = server_module.resolve_actor_check
    original_roll = server_module.roll

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

    def forced_save(*args, **kwargs):
        result = original_actor_check(*args, **kwargs)
        result.update(
            natural=15 if save_success else 5,
            total=int(kwargs["dc"]) if save_success else int(kwargs["dc"]) - 1,
            success=save_success,
        )
        return result

    def forced_damage(expression: str, **kwargs):
        if expression.replace(" ", "").casefold() == "2d8":
            return DiceResult(
                total=8,
                rolls=(4, 4),
                expression=expression,
                detail="2d8[4, 4]",
            )
        return original_roll(expression, **kwargs)

    monkeypatch.setattr(server_module, "roll_attack_action", forced_hit)
    monkeypatch.setattr(server_module, "resolve_actor_check", forced_save)
    monkeypatch.setattr(server_module, "roll", forced_damage)

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
                "name": "Spider bite on-hit ruling",
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
        bite_effect = (
            "and the target must make a DC 11 Constitution saving throw, taking "
            "9 (2d8) poison damage on a failed save, or half as much damage on "
            "a successful one. If the poison reduces the target to 0 hit points, "
            "the target is stable but poisoned for 1 hour, and paralyzed while "
            "poisoned in this way."
        )
        spider_sheet = default_character_sheet()
        spider_sheet["combat"]["hp"] = {"value": 26, "max": 26, "temp": 0}
        spider_sheet["inventory"]["items"] = [
            {
                "id": "bite",
                "name": "Bite",
                "kind": "weapon",
                "equipped": True,
                "equipped_slot": "main_hand",
                "mechanics": {
                    "attack_type": "melee",
                    "attack_ability": "strength",
                    "damage_formula": "1",
                    "damage_type": "piercing",
                    "on_hit_effect": bite_effect,
                    "reach_ft": 5,
                    "attack_bonus_override": 5,
                    "always_available": True,
                },
            }
        ]
        spider_sheet["inventory"]["equipment_slots"]["main_hand"] = "bite"
        target_sheet = default_character_sheet()
        target_sheet["combat"]["hp"] = {
            "value": starting_hp,
            "max": starting_hp,
            "temp": 0,
        }
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
                        "position": {"x": 1, "y": 0},
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
                "action": {"weapon_id": "bite", "attack_mode": "melee"},
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "bite",
            },
        )
        assert attacked["status"] == "pending_ruling"
        ruled = await raw(
            "combat_on_hit_ruling",
            {
                "campaign_id": campaign["id"],
                "target_id": target["id"],
                "choice_id": attacked["result"]["pending_on_hit_ruling_id"],
                "selection": {
                    "id": "saving_throw_damage",
                    "save_ability": "constitution",
                    "save_dc": 11,
                    "damage_formula": "2d8",
                    "damage_type": "poison",
                    "half_on_success": True,
                    "zero_hp_effect": {
                        "stable": True,
                        "conditions": ["poisoned", "paralyzed"],
                        "duration": {"period": "hour", "remaining": 1},
                    },
                    "source_excerpt": bite_effect,
                },
                "expected_revision": attacked["campaign_revision"],
                "idempotency_key": "rule-bite",
            },
        )
        target_after = await call(
            "character_get",
            {"character_id": target["id"]},
        )
        assert ruled["result"]["save"]["success"] is save_success
        assert ruled["result"]["damage_roll"]["total"] == 8
        if save_success:
            assert ruled["result"]["damage_amount"] == 4
            assert target_after["sheet"]["combat"]["hp"]["value"] == 15
            assert ruled["result"]["zero_hp_effect"] is None
        else:
            assert ruled["result"]["damage_amount"] == 8
            assert target_after["sheet"]["combat"]["hp"]["value"] == 0
            assert set(target_after["sheet"]["conditions"]) == {
                "paralyzed",
                "poisoned",
                "prone",
                "stable",
                "unconscious",
            }
            assert "dead" not in target_after["sheet"]["conditions"]
            poison_effect = next(
                item
                for item in target_after["sheet"]["effects"]
                if item["kind"] == "timed_conditions"
            )
            assert poison_effect["duration"] == {"period": "hour", "remaining": 1}
            assert ruled["result"]["zero_hp_effect"]["effect_id"] == poison_effect["id"]

    asyncio.run(exercise())
