from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


def test_discovered_spellbook_copy_is_source_bound_paid_timed_and_atomic(
    tmp_path: Path,
) -> None:
    workspace = Path(__file__).resolve().parents[2]
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=workspace / "SagaSmith-dnd-skills",
        modulegen_skills_dir=workspace / "SagaSmith-module-gen-skills",
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        value = result.get("result", result) if isinstance(result, dict) else result
        if isinstance(value, dict) and "action" in value and "result" in value:
            return value["result"]
        return value

    async def campaign(server, campaign_id: str) -> dict:
        return await call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign_id}},
        )

    async def exercise() -> None:
        server = create_server(config)
        created = await call(
            server,
            "campaign_create",
            {"name": "Spellbook copy", "edition": "2014", "idempotency_key": "campaign"},
        )
        await call(
            server,
            "campaign_rule_profile_set",
            {
                "campaign_id": created["id"],
                "edition": "2014",
                "expected_revision": created["revision"],
                "idempotency_key": "profile",
            },
        )
        spells = await call(
            server,
            "content_catalog_list",
            {"campaign_id": created["id"], "kind": "spell", "query": "Burning Hands"},
        )
        burning_hands = next(item for item in spells if item["name"] == "Burning Hands")

        sheet = default_character_sheet()
        sheet["edition"] = "2014"
        sheet["progression"].update(
            {
                "level": 2,
                "classes": [{"name": "Wizard", "level": 2, "subclass": "", "hit_die": 6}],
            }
        )
        sheet["spellcasting"]["preparation"].update(
            {"mode": "spellbook", "max_prepared": 4, "changes_on": "long_rest"}
        )
        sheet["spellcasting"]["spellbook"]["enabled"] = True
        sheet["inventory"]["wallet"]["gp"] = 50
        wizard = await call(
            server,
            "character_create",
            {
                "campaign_id": created["id"],
                "name": "Copying Wizard",
                "sheet": sheet,
                "idempotency_key": "wizard",
            },
        )
        current_campaign = await campaign(server, created["id"])
        book = await call(
            server,
            "inventory_change",
            {
                "owner": "party",
                "action": "add",
                "owner_id": created["id"],
                "payload": {
                    "item": {
                        "id": "d11-red-spellbook",
                        "name": "Red leather spellbook",
                        "kind": "spellbook",
                        "source_key": "module:avernus:d11:red-spellbook",
                        "mechanics": {
                            "edition": "2014",
                            "spell_ids": [burning_hands["id"]],
                            "source_scene_id": "d11",
                            "deciphered": True,
                            "copyable": True,
                        },
                    }
                },
                "expected_revision": current_campaign["revision"],
                "idempotency_key": "book",
            },
        )
        assert book["item_id"] == "d11-red-spellbook"
        current_campaign = await campaign(server, created["id"])
        await call(
            server,
            "campaign_change",
            {
                "campaign_id": created["id"],
                "action": "clock_set",
                "payload": {"day": 1, "hour": 10, "label": "Copy test"},
                "expected_revision": current_campaign["revision"],
                "idempotency_key": "clock",
            },
        )
        current_campaign = await campaign(server, created["id"])
        await call(
            server,
            "game_phase",
            {
                "campaign_id": created["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": current_campaign["revision"],
                "idempotency_key": "play",
            },
        )

        with pytest.raises(Exception, match="only source-bound spellbook_copy"):
            await call(
                server,
                "character_content_apply",
                {
                    "character_id": wizard["id"],
                    "artifact_id": burning_hands["id"],
                    "selection": {"source_class": "Wizard", "method": "spellbook"},
                    "expected_revision": wizard["revision"],
                    "idempotency_key": "free-copy",
                },
            )

        arguments = {
            "character_id": wizard["id"],
            "artifact_id": burning_hands["id"],
            "selection": {
                "source_class": "Wizard",
                "method": "spellbook_copy",
                "source_owner": "party",
                "source_item_id": "d11-red-spellbook",
                "payment_owner": "character",
                "payment": {"gp": 50},
            },
            "expected_revision": wizard["revision"],
            "idempotency_key": "copy",
        }
        copied = await call(server, "character_content_apply", arguments)
        replay = await call(server, "character_content_apply", arguments)

        assert copied == replay
        assert copied["sheet"]["inventory"]["wallet"]["gp"] == 0
        assert burning_hands["id"] in copied["sheet"]["spellcasting"]["spellbook"][
            "spell_ids"
        ]
        assert copied["spellbook_copy"]["cost_cp"] == 5000
        assert copied["spellbook_copy"]["hours"] == 2
        assert copied["spellbook_copy"]["world_time"]["hour"] == 12
        after = await campaign(server, created["id"])
        assert after["state"]["party"]["inventory"]["items"][0]["id"] == (
            "d11-red-spellbook"
        )
        receipts = await call(
            server,
            "campaign_rules",
            {
                "campaign_id": created["id"],
                "action": "receipts",
                "payload": {"mechanic_id": "dnd5e.core.spell.spellbook_copy"},
            },
        )
        assert len(receipts) == 1
        assert receipts[0]["event"] == "character.spellbook.copy"

    asyncio.run(exercise())
