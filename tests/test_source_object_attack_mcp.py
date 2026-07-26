from __future__ import annotations

import asyncio
from pathlib import Path

from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


def test_public_character_action_attacks_and_persists_a_source_object(
    tmp_path: Path,
) -> None:
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
            {
                "name": "Source object",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        sheet = default_character_sheet()
        sheet["abilities"]["strength"]["score"] = 16
        sheet["combat"]["hp"] = {"value": 12, "max": 12, "temp": 0}
        sheet["inventory"]["items"] = [
            {
                "id": "mace",
                "name": "Mace",
                "kind": "weapon",
                "equipped": True,
                "equipped_slot": "main_hand",
                "mechanics": {
                    "attack_type": "melee",
                    "attack_ability": "strength",
                    "damage_formula": "1d6",
                    "damage_type": "bludgeoning",
                    "properties": [],
                    "proficient": True,
                },
            }
        ]
        sheet["inventory"]["equipment_slots"]["main_hand"] = "mace"
        actor = await call(
            server,
            "character_create",
            {
                "name": "Breaker",
                "campaign_id": campaign["id"],
                "character_type": "pc",
                "sheet": sheet,
                "idempotency_key": "actor",
            },
        )
        source_ref = {
            "module_id": "module-1",
            "scene_id": "scene-1",
            "chunk_id": "chunk-1",
            "page_start": 96,
            "page_end": 96,
            "heading_path": ["Vault", "Fresco"],
            "content_sha256": "a" * 64,
        }
        source_object = {
            "id": "fresco-section",
            "name": "Enthralling Fresco Section",
            "scene_id": "scene-1",
            "armor_class": 17,
            "hit_points": 5,
            "damage_immunities": ["poison", "psychic"],
        }
        result = None
        last_arguments = None
        for index in range(100):
            campaign = await call(
                server,
                "campaign_query",
                {"view": "get", "payload": {"campaign_id": campaign["id"]}},
            )
            actor = await call(
                server,
                "character_query",
                {"view": "get", "payload": {"character_id": actor["id"]}},
            )
            last_arguments = {
                "character_id": actor["id"],
                "action": "attack_source_object",
                "payload": {
                    "object": source_object,
                    "weapon_id": "mace",
                    "source_ref": source_ref,
                    "reason": "The source-defined object is within melee reach.",
                    "expected_campaign_revision": campaign["revision"],
                },
                "expected_revision": actor["revision"],
                "idempotency_key": f"attack-{index}",
            }
            result = await call(
                server,
                "character_action",
                last_arguments,
            )
            if result["object"]["destroyed"]:
                break

        assert result is not None
        assert result["status"] == "committed"
        assert result["object"]["destroyed"] is True
        assert result["object"]["hit_points"] == 0
        assert result["object"]["damage_immunities"] == ["poison", "psychic"]
        replay = await call(server, "character_action", last_arguments)
        assert replay["object"] == result["object"]

    asyncio.run(exercise())
