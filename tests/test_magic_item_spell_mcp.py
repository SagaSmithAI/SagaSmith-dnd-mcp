import asyncio
from pathlib import Path

from sagasmith_dnd.character_schema import default_character_sheet
from sagasmith_dnd.spells import CORE_MAGE_ARMOR_SPELL_ID, CORE_SHIELD_SPELL_ID

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    called = await server.call_tool(name, arguments)
    if isinstance(called, tuple):
        _, result = called
        return result.get("result", result) if isinstance(result, dict) else result
    return called


async def _raw(server, name: str, arguments: dict):
    called = await server.call_tool(name, arguments)
    if isinstance(called, tuple):
        _, result = called
        return result
    return called


def test_public_magic_item_spell_cast_hydrates_card_and_pays_action_and_charges(
    tmp_path: Path,
) -> None:
    workspace = Path(__file__).resolve().parents[2]
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=workspace / "SagaSmith-dnd-skills",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Magic item spells", "edition": "2014", "idempotency_key": "campaign"},
        )
        sheet = default_character_sheet()
        sheet["abilities"]["dexterity"]["score"] = 14
        sheet["spellcasting"]["ability"] = "intelligence"
        sheet["spellcasting"]["class_lists"] = ["wizard"]
        caster = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Caster",
                "sheet": sheet,
                "idempotency_key": "caster",
            },
        )
        added = await _call(
            server,
            "inventory_change",
            {
                "owner": "character",
                "action": "add",
                "owner_id": caster["id"],
                "payload": {
                    "item": {
                        "id": "staff-of-defense",
                        "name": "Staff of Defense",
                        "kind": "magic_item",
                        "source_key": "module-chunk:staff-of-defense",
                        "attunement": "attuned",
                        "charges": {
                            "label": "Staff charges",
                            "value": 10,
                            "max": 10,
                            "recovers_on": "dawn",
                            "source_key": "module-chunk:staff-of-defense",
                        },
                        "mechanics": {
                            "ac_bonus": 1,
                            "charge_rules": {
                                "recovery_trigger": "dawn",
                                "recovery_formula": "1d6+4",
                                "last_charge_check_formula": "1d20",
                                "destroy_on": [1],
                            },
                            "spellcasting": {
                                "requires_attunement": True,
                                "requires_class_spell_list": True,
                                "components_required": False,
                                "spells": [
                                    {
                                        "artifact_id": CORE_MAGE_ARMOR_SPELL_ID,
                                        "charge_cost": 1,
                                        "casting_time": "1 action",
                                    },
                                    {
                                        "artifact_id": CORE_SHIELD_SPELL_ID,
                                        "charge_cost": 2,
                                        "casting_time": "1 action",
                                    },
                                ],
                            },
                        },
                    }
                },
                "expected_revision": caster["revision"],
                "idempotency_key": "add-staff",
            },
        )
        hydrated_staff = next(
            item
            for item in added["character"]["sheet"]["inventory"]["items"]
            if item["id"] == "staff-of-defense"
        )
        bound_spells = hydrated_staff["mechanics"]["spellcasting"]["spells"]
        assert [item["card"]["id"] for item in bound_spells] == [
            CORE_MAGE_ARMOR_SPELL_ID,
            CORE_SHIELD_SPELL_ID,
        ]
        assert all(item["card"]["pack_id"] == "dnd5e.content.srd2014" for item in bound_spells)
        assert all(item["card"]["rule_refs"] for item in bound_spells)

        recharged = await _call(
            server,
            "inventory_change",
            {
                "owner": "character",
                "action": "recharge",
                "owner_id": caster["id"],
                "payload": {"item_id": "staff-of-defense", "trigger": "dawn"},
                "expected_revision": added["character"]["revision"],
                "idempotency_key": "recharge-staff",
            },
        )
        assert recharged["recharge"]["formula"] == "1d6+4"
        assert recharged["recharge"]["recovered"] == 0
        assert 5 <= recharged["recharge"]["roll"]["total"] <= 10

        equipped = await _call(
            server,
            "inventory_change",
            {
                "owner": "character",
                "action": "equip",
                "owner_id": caster["id"],
                "payload": {"item_id": "staff-of-defense", "slot": "main_hand"},
                "expected_revision": recharged["character"]["revision"],
                "idempotency_key": "equip-staff",
            },
        )
        assert equipped["derived"]["armor_class"] == 13

        current_campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
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
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [caster["id"]],
                "participant_config": [
                    {
                        "actor_id": caster["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                    }
                ],
                "map": {
                    "width": 4,
                    "height": 4,
                    "cell_ft": 5,
                    "terrain": [],
                    "walls": [],
                    "doors": [],
                    "objects": [],
                    "metadata": {
                        "temporary": True,
                        "encounter_local": True,
                        "provenance": "test",
                    },
                },
                "expected_revision": phase["campaign_revision"],
                "idempotency_key": "start",
            },
        )
        cast = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign["id"],
                "actor_id": caster["id"],
                "spell_id": CORE_MAGE_ARMOR_SPELL_ID,
                "source_item_id": "staff-of-defense",
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "cast-mage-armor",
            },
        )

        assert cast["status"] == "committed"
        assert cast["result"]["automatic_effect"] == "mage_armor"
        assert cast["result"]["payment"] == {
            "economy": "item_charges",
            "item_id": "staff-of-defense",
            "cost": 1,
            "level": 1,
            "ritual": False,
        }
        caster_state = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": caster["id"]}},
        )
        assert caster_state["derived"]["armor_class"] == 16
        staff = next(
            item
            for item in caster_state["sheet"]["inventory"]["items"]
            if item["id"] == "staff-of-defense"
        )
        assert staff["charges"]["value"] == 9
        combatant = next(
            item for item in cast["combat"]["combatants"] if item["actor_id"] == caster["id"]
        )
        assert combatant["turn_budget"]["main_action"] == 0
        assert cast["combat"]["turn_spell_casts"][caster["id"]][0]["source_item_id"] == (
            "staff-of-defense"
        )

        replay = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign["id"],
                "actor_id": caster["id"],
                "spell_id": CORE_MAGE_ARMOR_SPELL_ID,
                "source_item_id": "staff-of-defense",
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "cast-mage-armor",
            },
        )
        assert replay == cast

        ended = await _call(
            server,
            "combat_end",
            {
                "campaign_id": campaign["id"],
                "outcome": {
                    "status": "interrupted",
                    "summary": "Resource-boundary regression continues in a new encounter.",
                },
                "expected_revision": cast["campaign_revision"],
                "idempotency_key": "end-first-combat",
            },
        )
        after_end_caster = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": caster["id"]}},
        )
        one_charge = dict(staff["charges"])
        one_charge["value"] = 1
        reduced = await _call(
            server,
            "inventory_change",
            {
                "owner": "character",
                "action": "update",
                "owner_id": caster["id"],
                "payload": {
                    "item_id": "staff-of-defense",
                    "patch": {"charges": one_charge},
                },
                "expected_revision": after_end_caster["revision"],
                "idempotency_key": "set-last-charge",
            },
        )
        current_campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        last_started = await _call(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [caster["id"]],
                "participant_config": [
                    {
                        "actor_id": caster["id"],
                        "initiative": 20,
                        "position": {"x": 0, "y": 0},
                    }
                ],
                "map": {
                    "width": 4,
                    "height": 4,
                    "cell_ft": 5,
                    "terrain": [],
                    "walls": [],
                    "doors": [],
                    "objects": [],
                    "metadata": {
                        "temporary": True,
                        "encounter_local": True,
                        "provenance": "test:last-charge",
                    },
                },
                "expected_revision": current_campaign["revision"],
                "idempotency_key": "start-last-charge",
            },
        )
        last_cast = await _raw(
            server,
            "combat_cast_spell",
            {
                "campaign_id": campaign["id"],
                "actor_id": caster["id"],
                "spell_id": CORE_MAGE_ARMOR_SPELL_ID,
                "source_item_id": "staff-of-defense",
                "expected_revision": last_started["campaign_revision"],
                "idempotency_key": "cast-last-charge",
            },
        )
        last_charge = last_cast["result"]["last_charge_resolution"]
        assert last_charge["formula"] == "1d20"
        assert 1 <= last_charge["roll"]["total"] <= 20
        assert last_charge["rolled_total"] == last_charge["roll"]["total"]
        final_caster = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": caster["id"]}},
        )
        final_staff = next(
            item
            for item in final_caster["sheet"]["inventory"]["items"]
            if item["id"] == "staff-of-defense"
        )
        assert final_staff["charges"]["value"] == 0
        assert (final_staff["condition"] == "destroyed") is last_charge["destroyed"]
        assert final_caster["derived"]["armor_class"] == (
            15 if last_charge["destroyed"] else 16
        )
        assert ended["outcome"]["status"] == "interrupted"
        assert reduced["revision"] == after_end_caster["revision"] + 1

    asyncio.run(exercise())
