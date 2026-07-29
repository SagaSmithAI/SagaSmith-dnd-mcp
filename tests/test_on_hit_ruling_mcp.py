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


def test_reviewed_grapple_escape_combines_attack_dc_with_core_checks() -> None:
    effect = (
        "and the target is grappled (escape DC 12). Until this grapple ends, "
        "the target can't breathe."
    )

    checks, rule = server_module.reviewed_on_hit_escape_checks(
        effect,
        condition="grappled",
        escape_dc=12,
        escape_abilities=[],
        escape_checks=["athletics", "acrobatics"],
    )

    assert checks == ["athletics", "acrobatics"]
    assert rule == "core_2014_grapple"
    with pytest.raises(Exception, match="Athletics/Acrobatics"):
        server_module.reviewed_on_hit_escape_checks(
            effect,
            condition="grappled",
            escape_dc=12,
            escape_abilities=[],
            escape_checks=["strength"],
        )


def test_unavailable_sources_release_only_their_owned_grapples() -> None:
    encounter = {
        "ongoing_effects": [
            {
                "id": "ended-grapple",
                "kind": "on_hit_condition",
                "condition": "grappled",
                "source_actor_id": "departed-source",
                "target_id": "target",
                "active": True,
            },
            {
                "id": "remaining-grapple",
                "kind": "on_hit_condition",
                "condition": "grappled",
                "source_actor_id": "present-source",
                "target_id": "target",
                "active": True,
            },
            {
                "id": "independent-web",
                "kind": "on_hit_condition",
                "condition": "restrained",
                "source_actor_id": "departed-source",
                "target_id": "target",
                "active": True,
            },
        ]
    }

    released = server_module.release_unavailable_source_grapples(
        encounter,
        {"departed-source"},
    )

    assert released == {"target": ["ended-grapple"]}
    assert encounter["ongoing_effects"][0]["active"] is False
    assert encounter["ongoing_effects"][0]["resolution"] == {
        "kind": "source_unavailable",
        "source_actor_id": "departed-source",
    }
    assert encounter["ongoing_effects"][1]["active"] is True
    assert encounter["ongoing_effects"][2]["active"] is True
    assert server_module.has_active_owned_condition(
        encounter,
        target_id="target",
        condition="grappled",
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
        garrote_effect = (
            "and the target is grappled (escape DC 12). Until this grapple ends, "
            "the target can't breathe."
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
            },
            {
                "id": "garrote",
                "name": "Web Garrote",
                "kind": "weapon",
                "equipped": True,
                "equipped_slot": "off_hand",
                "mechanics": {
                    "attack_type": "melee",
                    "attack_ability": "dexterity",
                    "damage_formula": "",
                    "damage_type": "",
                    "on_hit_effect": garrote_effect,
                    "reach_ft": 5,
                    "attack_bonus_override": 5,
                    "always_available": True,
                },
            },
        ]
        spider_sheet["inventory"]["equipment_slots"]["main_hand"] = "web"
        spider_sheet["inventory"]["equipment_slots"]["off_hand"] = "garrote"
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
                "action": {"weapon_id": "web", "attack_mode": "ranged"},
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "web",
            },
        )
        assert attacked["status"] == "pending_ruling"
        choice_id = attacked["result"]["pending_on_hit_ruling_id"]
        with pytest.raises(
            Exception,
            match="explicit structured on-hit effect cannot be dismissed",
        ):
            await call(
                "combat_choice",
                {
                    "campaign_id": campaign["id"],
                    "action": "on_hit_ruling",
                    "actor_id": target["id"],
                    "payload": {
                        "choice_id": choice_id,
                        "selection": {
                            "id": "dismiss",
                            "source_excerpt": web_effect,
                        },
                    },
                    "expected_revision": attacked["campaign_revision"],
                    "idempotency_key": "invalid-dismiss-web",
                },
            )
        ruled = await call(
            "combat_choice",
            {
                "campaign_id": campaign["id"],
                "action": "on_hit_ruling",
                "actor_id": target["id"],
                "payload": {
                    "choice_id": choice_id,
                    "selection": {
                        "id": "apply_condition",
                        "condition": "restrained",
                        "escape_dc": 12,
                        "escape_abilities": ["strength"],
                        "source_excerpt": web_effect,
                    },
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
            item for item in escaped["combat"]["combatants"] if item["actor_id"] == target["id"]
        )
        assert target_combatant["turn_budget"]["main_action"] == 0
        assert escaped["combat"]["ongoing_effects"][0]["active"] is False
        round_two = await raw(
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": target["id"],
                "expected_revision": escaped["campaign_revision"],
                "idempotency_key": "end-target",
            },
        )
        garroted = await raw(
            "combat_resolve_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": spider["id"],
                "target_id": target["id"],
                "action": {"weapon_id": "garrote", "attack_mode": "melee"},
                "expected_revision": round_two["campaign_revision"],
                "idempotency_key": "garrote",
            },
        )
        grappled = await call(
            "combat_choice",
            {
                "campaign_id": campaign["id"],
                "action": "on_hit_ruling",
                "actor_id": target["id"],
                "payload": {
                    "choice_id": garroted["result"]["pending_on_hit_ruling_id"],
                    "selection": {
                        "id": "apply_condition",
                        "condition": "grappled",
                        "escape_dc": 12,
                        "escape_checks": ["athletics", "acrobatics"],
                        "source_excerpt": garrote_effect,
                    },
                },
                "expected_revision": garroted["campaign_revision"],
                "idempotency_key": "rule-garrote",
            },
        )
        assert grappled["ongoing_effect"]["escape_rule"] == "core_2014_grapple"
        killed = await call(
            "combat_hp_change",
            {
                "campaign_id": campaign["id"],
                "target_id": spider["id"],
                "action": "damage",
                "payload": {
                    "parts": [{"amount": 100, "damage_type": "bludgeoning"}],
                },
                "expected_revision": grappled["campaign_revision"],
                "idempotency_key": "kill-spider",
            },
        )
        closed = await raw(
            "combat_end",
            {
                "campaign_id": campaign["id"],
                "outcome": {"status": "victory", "summary": "The spider was defeated."},
                "expected_revision": killed["campaign_revision"],
                "idempotency_key": "end-combat",
            },
        )
        target_after_end = await call(
            "character_get",
            {"character_id": target["id"]},
        )
        assert target_after_end["sheet"]["conditions"] == []
        assert closed["released_grapples"] == {
            target["id"]: [garroted["result"]["pending_on_hit_ruling_id"]]
        }
        assert closed["recovered_postcombat_cleanup"] is False

    asyncio.run(exercise())


def test_public_on_hit_ruling_applies_and_ends_source_ongoing_damage(
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
                "name": "Source ongoing damage",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        elemental = await call(
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Fire Elemental",
                "character_type": "monster",
                "idempotency_key": "elemental",
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
        effect = (
            "If the target is a creature or a flammable object, it ignites. "
            "Until a creature takes an action to douse the fire, the target "
            "takes 5 (1d10) fire damage at the start of each of its turns."
        )
        elemental_sheet = default_character_sheet()
        elemental_sheet["combat"]["hp"] = {"value": 102, "max": 102, "temp": 0}
        elemental_sheet["inventory"]["items"] = [
            {
                "id": "touch",
                "name": "Touch",
                "kind": "weapon",
                "equipped": True,
                "equipped_slot": "main_hand",
                "mechanics": {
                    "attack_type": "melee",
                    "attack_ability": "dexterity",
                    "damage_formula": "1d2",
                    "damage_type": "fire",
                    "on_hit_effect": effect,
                    "reach_ft": 5,
                    "attack_bonus_override": 6,
                    "always_available": True,
                },
            }
        ]
        elemental_sheet["inventory"]["equipment_slots"]["main_hand"] = "touch"
        target_sheet = default_character_sheet()
        target_sheet["combat"]["hp"] = {"value": 30, "max": 30, "temp": 0}
        for actor, sheet, key in (
            (elemental, elemental_sheet, "elemental-sheet"),
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
                "participant_ids": [elemental["id"], target["id"]],
                "participant_config": [
                    {
                        "actor_id": elemental["id"],
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
                "actor_id": elemental["id"],
                "target_id": target["id"],
                "action": {"weapon_id": "touch", "attack_mode": "melee"},
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "touch",
            },
        )
        ruled = await call(
            "combat_choice",
            {
                "campaign_id": campaign["id"],
                "action": "on_hit_ruling",
                "actor_id": target["id"],
                "payload": {
                    "choice_id": attacked["result"]["pending_on_hit_ruling_id"],
                    "selection": {
                        "id": "ongoing_damage",
                        "applies": True,
                        "damage_formula": "1d10",
                        "damage_type": "fire",
                        "trigger_timing": "turn_start",
                        "end_action": "improvise",
                        "end_action_description": "douse the fire",
                        "trigger_facts": {
                            "target_id": target["id"],
                            "target_is_creature": True,
                        },
                        "default_resolver": "agent",
                        "ruling_kind": "agent_dm_adjudication",
                        "decision": "The creature target ignites.",
                        "reason": "The target is a creature in the pending attack.",
                        "source_excerpt": effect,
                    },
                },
                "expected_revision": attacked["campaign_revision"],
                "idempotency_key": "rule-ongoing",
            },
        )
        assert ruled["status"] == "committed"
        assert ruled["ongoing_effect"]["kind"] == "source_ongoing_damage"
        started_target_turn = await raw(
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": elemental["id"],
                "expected_revision": ruled["campaign_revision"],
                "idempotency_key": "end-elemental",
            },
        )
        assert started_target_turn["source_turn_start"][0]["kind"] == (
            "source_ongoing_damage"
        )
        target_after_damage = await call(
            "character_get",
            {"character_id": target["id"]},
        )
        hp_after_damage = target_after_damage["sheet"]["combat"]["hp"]["value"]
        assert hp_after_damage < 29

        ended = await raw(
            "combat_common_action",
            {
                "campaign_id": campaign["id"],
                "actor_id": target["id"],
                "action": "improvise",
                "target_id": target["id"],
                "payload": {
                    "end_ongoing_effect_id": ruled["ongoing_effect"]["id"],
                    "end_action_description": "douse the fire",
                    "source_excerpt": effect,
                },
                "expected_revision": started_target_turn["campaign_revision"],
                "idempotency_key": "douse",
            },
        )
        ongoing = next(
            item
            for item in ended["combat"]["ongoing_effects"]
            if item["id"] == ruled["ongoing_effect"]["id"]
        )
        assert ongoing["active"] is False
        target_combatant = next(
            item
            for item in ended["combat"]["combatants"]
            if item["actor_id"] == target["id"]
        )
        assert target_combatant["turn_budget"]["main_action"] == 0

        next_round = await raw(
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": target["id"],
                "expected_revision": ended["campaign_revision"],
                "idempotency_key": "end-target",
            },
        )
        target_turn_again = await raw(
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": elemental["id"],
                "expected_revision": next_round["campaign_revision"],
                "idempotency_key": "end-elemental-round-two",
            },
        )
        assert target_turn_again["source_turn_start"] == []
        target_after_douse = await call(
            "character_get",
            {"character_id": target["id"]},
        )
        assert target_after_douse["sheet"]["combat"]["hp"]["value"] == hp_after_damage

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
                sheet["inventory"]["equipment_slots"]["main_hand"] = "guiding-bolt-test"
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
            candidate["id"] == "next_attack_advantage" for candidate in pending["candidates"]
        )
        ruled = await call(
            "combat_choice",
            {
                "campaign_id": campaign["id"],
                "action": "on_hit_ruling",
                "actor_id": target["id"],
                "payload": {
                    "choice_id": attacked["result"]["pending_on_hit_ruling_id"],
                    "selection": {
                        "id": "next_attack_advantage",
                        "source_excerpt": effect,
                    },
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
        assert consumed["result"]["consumed_next_attack_advantage_effect_id"] == effect_id
        stored = next(
            item for item in consumed["combat"]["ongoing_effects"] if item["id"] == effect_id
        )
        assert stored["active"] is False
        assert stored["resolution"]["kind"] == "attack_roll"

    asyncio.run(exercise())


def test_public_on_hit_ruling_applies_agent_selected_direct_damage(
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
                "name": "Variable direct damage",
                "edition": "2014",
                "random_seed": "variable-direct-damage",
                "idempotency_key": "campaign",
            },
        )
        attacker = await call(
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Acid dragonfang",
                "character_type": "monster",
                "idempotency_key": "attacker",
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
        effect = (
            "22 (5d8) damage of the type to which the dragonfang has "
            "damage resistance."
        )
        attacker_sheet = default_character_sheet()
        attacker_sheet["combat"]["hp"] = {"value": 40, "max": 40, "temp": 0}
        attacker_sheet["traits"]["resistances"] = ["acid"]
        attacker_sheet["inventory"]["items"] = [
            {
                "id": "orb",
                "name": "Orb of Dragon's Breath",
                "kind": "weapon",
                "mechanics": {
                    "attack_type": "ranged",
                    "attack_ability": "spell",
                    "damage_formula": "",
                    "damage_type": "",
                    "on_hit_effect": effect,
                    "normal_range_ft": 90,
                    "long_range_ft": 90,
                    "attack_bonus_override": 5,
                    "always_available": True,
                },
            }
        ]
        target_sheet = default_character_sheet()
        target_sheet["combat"]["hp"] = {"value": 100, "max": 100, "temp": 0}
        for actor, sheet, key in (
            (attacker, attacker_sheet, "attacker-sheet"),
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
                "participant_ids": [attacker["id"], target["id"]],
                "participant_config": [
                    {
                        "actor_id": attacker["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                    },
                    {
                        "actor_id": target["id"],
                        "initiative": 10,
                        "position": {"x": 4, "y": 0},
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
                "actor_id": attacker["id"],
                "target_id": target["id"],
                "action": {"weapon_id": "orb", "attack_mode": "ranged"},
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "attack",
            },
        )
        choice_id = attacked["result"]["pending_on_hit_ruling_id"]
        pending = next(
            item for item in attacked["combat"]["pending"] if item["id"] == choice_id
        )
        assert any(
            candidate["id"] == "direct_damage"
            for candidate in pending["candidates"]
        )
        base_selection = {
            "id": "direct_damage",
            "damage_formula": "5d8",
            "trigger_facts": {"selected_damage_resistance": "acid"},
            "default_resolver": "agent",
            "ruling_kind": "agent_dm_adjudication",
            "decision": "Apply the variable damage as acid.",
            "reason": "The reviewed attacker card records acid resistance.",
            "source_excerpt": effect,
        }
        with pytest.raises(Exception, match="does not match"):
            await call(
                "combat_choice",
                {
                    "campaign_id": campaign["id"],
                    "action": "on_hit_ruling",
                    "actor_id": target["id"],
                    "payload": {
                        "choice_id": choice_id,
                        "selection": {**base_selection, "damage_type": "fire"},
                    },
                    "expected_revision": attacked["campaign_revision"],
                    "idempotency_key": "wrong-type",
                },
            )
        ruled = await call(
            "combat_choice",
            {
                "campaign_id": campaign["id"],
                "action": "on_hit_ruling",
                "actor_id": target["id"],
                "payload": {
                    "choice_id": choice_id,
                    "selection": {**base_selection, "damage_type": "acid"},
                },
                "expected_revision": attacked["campaign_revision"],
                "idempotency_key": "direct-damage",
            },
        )
        settlement = ruled["result"]
        assert settlement["damage_roll"]["expression"] == "5d8"
        assert settlement["damage"]["damage_type"] == "acid"
        assert settlement["damage"]["after_hp"] == 100 - settlement["damage_amount"]
        assert ruled["combat"]["pending"] == []

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("target_size", "applies"),
    [("medium", True), ("small", False)],
)
def test_public_on_hit_ruling_records_agent_conditional_extra_damage(
    tmp_path: Path,
    monkeypatch,
    target_size: str,
    applies: bool,
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
                "name": "Conditional extra damage",
                "edition": "2014",
                "random_seed": f"conditional-extra-{target_size}",
                "idempotency_key": "campaign",
            },
        )
        attacker = await call(
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Reviewed attacker",
                "character_type": "npc",
                "idempotency_key": "attacker",
            },
        )
        target = await call(
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Reviewed target",
                "character_type": "monster",
                "idempotency_key": "target",
            },
        )
        effect = (
            "or 9 (1d6 + 3 plus 1d6) piercing damage if the target "
            "is Medium or larger."
        )
        attacker_sheet = default_character_sheet()
        attacker_sheet["combat"]["hp"] = {"value": 20, "max": 20, "temp": 0}
        attacker_sheet["inventory"]["items"] = [
            {
                "id": "shortsword",
                "name": "Shortsword",
                "kind": "weapon",
                "equipped": True,
                "equipped_slot": "main_hand",
                "mechanics": {
                    "attack_type": "melee",
                    "attack_ability": "dexterity",
                    "damage_formula": "1",
                    "damage_type": "piercing",
                    "on_hit_effect": effect,
                    "reach_ft": 5,
                    "attack_bonus_override": 5,
                    "always_available": True,
                },
            },
        ]
        attacker_sheet["inventory"]["equipment_slots"]["main_hand"] = "shortsword"
        target_sheet = default_character_sheet()
        target_sheet["traits"]["size"] = target_size
        target_sheet["combat"]["hp"] = {"value": 20, "max": 20, "temp": 0}
        for actor, sheet, key in (
            (attacker, attacker_sheet, "attacker-sheet"),
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
                "participant_ids": [attacker["id"], target["id"]],
                "participant_config": [
                    {
                        "actor_id": attacker["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                    },
                    {
                        "actor_id": target["id"],
                        "initiative": 10,
                        "position": {"x": 1, "y": 0},
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
                "actor_id": attacker["id"],
                "target_id": target["id"],
                "action": {"weapon_id": "shortsword", "attack_mode": "melee"},
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "attack",
            },
        )
        assert attacked["status"] == "pending_ruling"
        choice_id = attacked["result"]["pending_on_hit_ruling_id"]
        pending = next(
            item for item in attacked["combat"]["pending"] if item["id"] == choice_id
        )
        assert any(
            candidate["id"] == "conditional_extra_damage"
            for candidate in pending["candidates"]
        )
        with pytest.raises(
            Exception,
            match="explicit structured on-hit effect cannot be dismissed",
        ):
            await call(
                "combat_choice",
                {
                    "campaign_id": campaign["id"],
                    "action": "on_hit_ruling",
                    "actor_id": target["id"],
                    "payload": {
                        "choice_id": choice_id,
                        "selection": {"id": "dismiss", "source_excerpt": effect},
                    },
                    "expected_revision": attacked["campaign_revision"],
                    "idempotency_key": "invalid-dismiss",
                },
            )

        wrong_applies = not applies
        wrong_selection = {
            "id": "conditional_extra_damage",
            "applies": wrong_applies,
            "trigger_facts": {"target_size": target_size},
            "default_resolver": "agent",
            "ruling_kind": "agent_dm_adjudication",
            "decision": "Use the opposite applicability for validation.",
            "reason": "This intentionally conflicts with the actor size.",
            "source_excerpt": effect,
            **(
                {"damage_formula": "1d6", "damage_type": "piercing"}
                if wrong_applies
                else {}
            ),
        }
        with pytest.raises(Exception, match="applicability conflicts"):
            await call(
                "combat_choice",
                {
                    "campaign_id": campaign["id"],
                    "action": "on_hit_ruling",
                    "actor_id": target["id"],
                    "payload": {
                        "choice_id": choice_id,
                        "selection": wrong_selection,
                    },
                    "expected_revision": attacked["campaign_revision"],
                    "idempotency_key": "invalid-applicability",
                },
            )
        unchanged = await call("campaign_get", {"campaign_id": campaign["id"]})
        assert unchanged["revision"] == attacked["campaign_revision"]

        selection = {
            "id": "conditional_extra_damage",
            "applies": applies,
            "trigger_facts": {"target_size": target_size},
            "default_resolver": "agent",
            "ruling_kind": "agent_dm_adjudication",
            "decision": (
                "Apply the printed extra die."
                if applies
                else "Do not apply the printed extra die."
            ),
            "reason": (
                f"The target actor card records size {target_size}, and the source "
                "requires Medium or larger."
            ),
            "source_excerpt": effect,
            **(
                {"damage_formula": "1d6", "damage_type": "piercing"}
                if applies
                else {}
            ),
        }
        arguments = {
            "campaign_id": campaign["id"],
            "action": "on_hit_ruling",
            "actor_id": target["id"],
            "payload": {"choice_id": choice_id, "selection": selection},
            "expected_revision": attacked["campaign_revision"],
            "idempotency_key": "conditional-extra",
        }
        stream = server_module.CampaignRandomStream.from_campaign_state(
            campaign["id"],
            unchanged["state"],
            operation="combat_choice",
            idempotency_key="conditional-extra",
        )
        with server_module.use_random_stream(stream):
            ruled = await call("combat_choice", arguments)
        assert ruled["result"]["applies"] is applies
        assert ruled["result"]["trigger_facts"] == {"target_size": target_size}
        assert ruled["result"]["agent_ruling"]["default_resolver"] == "agent"
        assert stream.has_unpersisted_draws is False

        target_after = await call("character_get", {"character_id": target["id"]})
        if applies:
            extra = ruled["result"]["damage_roll"]["total"]
            assert 1 <= extra <= 6
            assert ruled["result"]["damage_amount"] == extra
            assert target_after["sheet"]["combat"]["hp"]["value"] == 19 - extra
            assert ruled["random_stream_receipt"]["position_before"] == 0
            assert ruled["random_stream_receipt"]["position_after"] == 1
        else:
            assert ruled["result"]["damage_roll"] is None
            assert ruled["result"]["damage_amount"] == 0
            assert target_after["sheet"]["combat"]["hp"]["value"] == 19
            assert "random_stream_receipt" not in ruled

        replayed = await call("combat_choice", arguments)
        assert replayed == ruled
        after_replay = await call("campaign_get", {"campaign_id": campaign["id"]})
        assert after_replay["state"]["random_stream"]["position"] == (1 if applies else 0)
        assert after_replay["revision"] == attacked["campaign_revision"] + 1

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
            "a successful one. If the poison damage reduces the target to 0 hit "
            "points, the target is stable but poisoned for 1 hour, even after "
            "regaining hit points, and is paralyzed while poisoned in this way."
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
        ruled = await call(
            "combat_choice",
            {
                "campaign_id": campaign["id"],
                "action": "on_hit_ruling",
                "actor_id": target["id"],
                "payload": {
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


def test_attack_can_select_and_consume_slaying_ammunition(
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
                "name": "Selected slaying ammunition",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        archer = await call(
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Archer",
                "character_type": "pc",
                "idempotency_key": "archer",
            },
        )
        dragon = await call(
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Dragon",
                "character_type": "monster",
                "idempotency_key": "dragon",
            },
        )
        source_excerpt = (
            "If a creature belonging to the type, race, or group associated with "
            "an arrow of slaying takes damage from the arrow, the creature must "
            "make a DC 17 Constitution saving throw, taking an extra 6d10 piercing "
            "damage on a failed save, or half as much extra damage on a successful one."
        )
        archer_sheet = default_character_sheet()
        archer_sheet["inventory"]["items"] = [
            {"id": "arrows", "name": "Arrows", "kind": "ammunition", "quantity": 20},
            {
                "id": "dragon-slaying-arrows",
                "name": "Arrows of dragon slaying",
                "kind": "ammunition",
                "quantity": 2,
                "mechanics": {
                    "magic": True,
                    "rarity": "very_rare",
                    "slaying": {
                        "target_groups": ["dragon"],
                        "save_ability": "constitution",
                        "save_dc": 17,
                        "damage_formula": "6d10",
                        "damage_type": "piercing",
                        "half_on_success": True,
                        "source_excerpt": source_excerpt,
                        "rule_refs": ["srd2014.magic-items.arrow-of-slaying"],
                    },
                },
            },
            {
                "id": "shortbow",
                "name": "Shortbow",
                "kind": "weapon",
                "equipped": True,
                "equipped_slot": "main_hand",
                "mechanics": {
                    "attack_type": "ranged",
                    "attack_ability": "dexterity",
                    "damage_formula": "1d6",
                    "damage_type": "piercing",
                    "properties": ["ammunition", "two_handed"],
                    "normal_range_ft": 80,
                    "long_range_ft": 320,
                    "ammunition_item_id": "arrows",
                },
            },
        ]
        archer_sheet["inventory"]["equipment_slots"]["main_hand"] = "shortbow"
        dragon_sheet = default_character_sheet()
        dragon_sheet["progression"]["species"] = "Huge dragon"
        dragon_sheet["combat"]["hp"] = {"value": 100, "max": 100, "temp": 0}
        for actor, sheet, key in (
            (archer, archer_sheet, "archer-sheet"),
            (dragon, dragon_sheet, "dragon-sheet"),
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
                "participant_ids": [archer["id"], dragon["id"]],
                "participant_config": [
                    {
                        "actor_id": archer["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                    },
                    {
                        "actor_id": dragon["id"],
                        "initiative": 10,
                        "position": {"x": 4, "y": 0},
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
                "actor_id": archer["id"],
                "target_id": dragon["id"],
                "action": {
                    "weapon_id": "shortbow",
                    "attack_mode": "ranged",
                    "ammunition_item_id": "dragon-slaying-arrows",
                },
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "shot",
            },
        )

        assert attacked["status"] == "pending_ruling"
        assert attacked["result"]["ammunition"]["item_id"] == "dragon-slaying-arrows"
        archer_after = await call("character_get", {"character_id": archer["id"]})
        inventory = {
            item["id"]: item for item in archer_after["sheet"]["inventory"]["items"]
        }
        assert inventory["dragon-slaying-arrows"]["quantity"] == 1
        assert inventory["arrows"]["quantity"] == 20

    asyncio.run(exercise())


def test_public_on_hit_ruling_repeats_save_gated_condition_at_turn_end(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original_attack_roll = server_module.roll_attack_action
    original_actor_check = server_module.resolve_actor_check
    save_results = iter((False, True))
    save_calls: list[bool] = []

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
        success = next(save_results)
        save_calls.append(success)
        result = original_actor_check(*args, **kwargs)
        result.update(
            natural=15 if success else 5,
            total=int(kwargs["dc"]) if success else int(kwargs["dc"]) - 1,
            success=success,
        )
        return result

    monkeypatch.setattr(server_module, "roll_attack_action", forced_hit)
    monkeypatch.setattr(server_module, "resolve_actor_check", forced_save)

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
                "name": "Save-gated on-hit condition",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        ettercap = await call(
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Ettercap",
                "character_type": "monster",
                "idempotency_key": "ettercap",
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
            "The target must succeed on a DC 11 Constitution saving throw or be "
            "poisoned for 1 minute. The creature can repeat the saving throw at "
            "the end of each of its turns, ending the effect on itself on a success."
        )
        ettercap_sheet = default_character_sheet()
        ettercap_sheet["combat"]["hp"] = {"value": 44, "max": 44, "temp": 0}
        ettercap_sheet["inventory"]["items"] = [
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
                    "attack_bonus_override": 4,
                    "always_available": True,
                },
            }
        ]
        ettercap_sheet["inventory"]["equipment_slots"]["main_hand"] = "bite"
        target_sheet = default_character_sheet()
        target_sheet["combat"]["hp"] = {"value": 20, "max": 20, "temp": 0}
        for actor, sheet, key in (
            (ettercap, ettercap_sheet, "ettercap-sheet"),
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
                "participant_ids": [ettercap["id"], target["id"]],
                "participant_config": [
                    {
                        "actor_id": ettercap["id"],
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
                "actor_id": ettercap["id"],
                "target_id": target["id"],
                "action": {"weapon_id": "bite", "attack_mode": "melee"},
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "bite",
            },
        )
        choice_id = attacked["result"]["pending_on_hit_ruling_id"]
        with pytest.raises(
            Exception,
            match="requires saving-throw settlement",
        ):
            await call(
                "combat_choice",
                {
                    "campaign_id": campaign["id"],
                    "action": "on_hit_ruling",
                    "actor_id": target["id"],
                    "payload": {
                        "choice_id": choice_id,
                        "selection": {
                            "id": "apply_condition",
                            "condition": "poisoned",
                            "escape_dc": 11,
                            "escape_abilities": ["constitution"],
                            "source_excerpt": bite_effect,
                        },
                    },
                    "expected_revision": attacked["campaign_revision"],
                    "idempotency_key": "invalid-action-escape",
                },
            )
        ruled = await call(
            "combat_choice",
            {
                "campaign_id": campaign["id"],
                "action": "on_hit_ruling",
                "actor_id": target["id"],
                "payload": {
                    "choice_id": choice_id,
                    "selection": {
                        "id": "saving_throw_condition",
                        "condition": "poisoned",
                        "save_ability": "constitution",
                        "save_dc": 11,
                        "repeat_save_timing": "turn_end",
                        "duration": {"period": "minute", "remaining": 1},
                        "source_excerpt": bite_effect,
                    },
                },
                "expected_revision": attacked["campaign_revision"],
                "idempotency_key": "rule-bite",
            },
        )
        assert ruled["result"]["save"]["success"] is False
        assert ruled["result"]["condition_applied"] is True
        target_after_hit = await call("character_get", {"character_id": target["id"]})
        assert target_after_hit["sheet"]["conditions"] == ["poisoned"]
        timed = next(
            item
            for item in target_after_hit["sheet"]["effects"]
            if item["id"] == ruled["result"]["effect_id"]
        )
        assert timed["duration"] == {"period": "minute", "remaining": 1}

        ended_ettercap = await raw(
            "combat_end_turn",
            {
                "campaign_id": campaign["id"],
                "actor_id": ettercap["id"],
                "expected_revision": ruled["campaign_revision"],
                "idempotency_key": "end-ettercap",
            },
        )
        target_combatant = next(
            item
            for item in ended_ettercap["combat"]["combatants"]
            if item["actor_id"] == target["id"]
        )
        assert target_combatant["turn_budget"]["main_action"] == 1
        end_arguments = {
            "campaign_id": campaign["id"],
            "actor_id": target["id"],
            "expected_revision": ended_ettercap["campaign_revision"],
            "idempotency_key": "end-target",
        }
        ended_target = await raw("combat_end_turn", end_arguments)
        replayed = await raw("combat_end_turn", end_arguments)
        assert replayed == ended_target
        assert save_calls == [False, True]
        assert ended_target["repeat_saves"][0]["condition_ended"] is True
        stored = next(
            item
            for item in ended_target["combat"]["ongoing_effects"]
            if item["id"] == ruled["result"]["effect_id"]
        )
        assert stored["active"] is False
        assert stored["resolution"]["kind"] == "repeat_save_success"
        target_after_save = await call("character_get", {"character_id": target["id"]})
        assert target_after_save["sheet"]["conditions"] == []
        assert all(
            item["id"] != ruled["result"]["effect_id"]
            for item in target_after_save["sheet"]["effects"]
        )

    asyncio.run(exercise())
