from __future__ import annotations

import asyncio
from pathlib import Path

from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


def test_play_hp_changes_use_the_same_zero_hp_and_recovery_rules(tmp_path: Path) -> None:
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
        if isinstance(result, dict) and "action" in result and "result" in result:
            return result["result"]
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Play HP", "edition": "2014", "idempotency_key": "campaign"},
        )

        pc_sheet = default_character_sheet()
        pc_sheet["edition"] = "2014"
        pc_sheet["combat"]["hp"] = {"value": 0, "max": 8, "temp": 0}
        pc_sheet["combat"]["death_saves"] = {"successes": 2, "failures": 1}
        pc_sheet["conditions"] = ["stable", "unconscious", "prone"]
        pc = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "name": "Fallen PC",
                    "campaign_id": campaign["id"],
                    "character_type": "pc",
                    "sheet": pc_sheet,
                },
                "principal_id": "system:local",
                "idempotency_key": "pc",
            },
        )
        healed = await call(
            server,
            "character_state_change",
            {
                "character_id": pc["id"],
                "action": "heal",
                "payload": {"amount": 3},
                "expected_revision": pc["revision"],
                "idempotency_key": "heal",
            },
        )
        healed_character = healed["character"]
        assert healed["result"]["after_hp"] == 3
        assert healed_character["sheet"]["conditions"] == ["prone"]
        assert healed_character["sheet"]["combat"]["death_saves"] == {
            "successes": 0,
            "failures": 0,
        }
        stood = await call(
            server,
            "character_state_change",
            {
                "character_id": pc["id"],
                "action": "stand",
                "expected_revision": healed_character["revision"],
                "idempotency_key": "stand",
            },
        )
        knocked_prone = await call(
            server,
            "character_state_change",
            {
                "character_id": pc["id"],
                "action": "knock_prone",
                "expected_revision": stood["character"]["revision"],
                "idempotency_key": "knock-prone",
            },
        )
        assert knocked_prone["status"] == "knocked_prone"
        assert knocked_prone["added_condition"] == "prone"
        assert knocked_prone["character"]["sheet"]["conditions"] == ["prone"]
        exhausted = await call(
            server,
            "character_state_change",
            {
                "character_id": pc["id"],
                "action": "exhaustion_set",
                "payload": {"value": 2},
                "expected_revision": knocked_prone["character"]["revision"],
                "idempotency_key": "exhaustion",
            },
        )
        assert exhausted["sheet"]["combat"]["exhaustion"] == 2
        fatally_exhausted = await call(
            server,
            "character_state_change",
            {
                "character_id": pc["id"],
                "action": "exhaustion_set",
                "payload": {"value": 6},
                "expected_revision": exhausted["revision"],
                "idempotency_key": "fatal-exhaustion",
            },
        )
        assert fatally_exhausted["sheet"]["combat"]["exhaustion"] == 6
        assert "dead" in fatally_exhausted["sheet"]["conditions"]

        monster_sheet = default_character_sheet()
        monster_sheet["edition"] = "2014"
        monster_sheet["combat"]["hp"] = {"value": 5, "max": 5, "temp": 0}
        monster = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "name": "Monster",
                    "campaign_id": campaign["id"],
                    "character_type": "monster",
                    "sheet": monster_sheet,
                },
                "principal_id": "system:local",
                "idempotency_key": "monster",
            },
        )
        damaged = await call(
            server,
            "character_state_change",
            {
                "character_id": monster["id"],
                "action": "damage",
                "payload": {"parts": [{"amount": 5, "damage_type": "radiant"}]},
                "expected_revision": monster["revision"],
                "idempotency_key": "damage",
            },
        )
        assert {"dead", "prone"} <= set(damaged["character"]["sheet"]["conditions"])

        fallen_sheet = default_character_sheet()
        fallen_sheet["edition"] = "2014"
        fallen_sheet["combat"]["hp"] = {"value": 0, "max": 18, "temp": 0}
        fallen_sheet["combat"]["death_saves"] = {"successes": 0, "failures": 3}
        fallen_sheet["conditions"] = ["dead", "prone", "unconscious"]
        fallen = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "name": "Raise Dead target",
                    "campaign_id": campaign["id"],
                    "character_type": "pc",
                    "sheet": fallen_sheet,
                },
                "principal_id": "system:local",
                "idempotency_key": "fallen",
            },
        )
        revival_arguments = {
            "character_id": fallen["id"],
            "action": "revive",
            "payload": {
                "elapsed_days": 1,
                "soul_willing": True,
                "body_intact": True,
                "source_ref": "module:rise-of-tiamat:p57",
                "reason": "The allied factions restore the fallen party after the second attack.",
            },
            "expected_revision": fallen["revision"],
            "idempotency_key": "revive",
        }
        revived = await call(server, "character_state_change", revival_arguments)
        replay = await call(server, "character_state_change", revival_arguments)
        assert replay == revived
        assert revived["result"]["status"] == "revived"
        assert revived["rule_receipts"][0]["mechanic_id"] == "dnd5e.core.spell.raise_dead"
        assert revived["character"]["sheet"]["combat"]["hp"]["value"] == 1
        assert revived["character"]["sheet"]["conditions"] == ["prone"]
        ordeal = next(
            effect
            for effect in revived["character"]["sheet"]["effects"]
            if effect["kind"] == "revival_ordeal"
        )
        assert {change["value"] for change in ordeal["changes"]} == {-4}

    asyncio.run(exercise())
