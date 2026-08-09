from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from tests.authoring_helpers import finalize_and_activate_module


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
        staged = await call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "name": "vault.md",
                    "content": (
                        "# Vault\n\n## Fresco\n\n"
                        "The enthralling fresco section has AC 17 and 5 hit points. "
                        "It is immune to poison and psychic damage."
                    ),
                    "source_key": "vault",
                    "title": "Vault",
                },
                "idempotency_key": "stage-module",
            },
        )
        activation = await finalize_and_activate_module(
            call,
            server,
            campaign["id"],
            staged,
            source_key="vault",
            title="Vault",
            portable_id="dnd5e.module.vault-test",
        )
        module_id = activation["activated"]["activation"]["module_id"]
        hits = await call(
            server,
            "module_search",
            {
                "campaign_id": campaign["id"],
                "query": "enthralling fresco AC hit points",
                "top_k": 3,
            },
        )
        expanded = await call(
            server,
            "module_expand",
            {"chunk_id": hits[0]["id"]},
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
            },
            {
                "id": "magic-mace",
                "name": "Attuned Magic Mace",
                "kind": "weapon",
                "equipped": True,
                "equipped_slot": "off_hand",
                "attunement": "attuned",
                "mechanics": {
                    "attack_type": "melee",
                    "attack_ability": "strength",
                    "damage_formula": "1d6",
                    "damage_type": "bludgeoning",
                    "properties": [],
                    "proficient": True,
                },
            },
        ]
        sheet["inventory"]["equipment_slots"]["main_hand"] = "mace"
        sheet["inventory"]["equipment_slots"]["off_hand"] = "magic-mace"
        actor = await call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "name": "Breaker",
                    "campaign_id": campaign["id"],
                    "character_type": "pc",
                    "sheet": sheet,
                },
                "principal_id": "system:local",
                "idempotency_key": "actor",
            },
        )
        source_ref = {
            "module_id": module_id,
            "scene_id": expanded["scene"]["id"],
            "chunk_id": expanded["chunk_id"],
            "page_start": expanded["page_start"],
            "page_end": expanded["page_end"],
            "heading_path": expanded["heading_path"],
            "content_sha256": hashlib.sha256(expanded["content"].encode("utf-8")).hexdigest(),
        }
        source_object = {
            "id": "fresco-section",
            "name": "Enthralling Fresco Section",
            "scene_id": expanded["scene"]["id"],
            "armor_class": 17,
            "hit_points": 5,
            "damage_immunities": ["poison", "psychic"],
            "damage_filter": {
                "allowed_damage_types": ["bludgeoning"],
                "required_any_weapon_traits": ["magical"],
            },
        }
        current_campaign = await call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        with pytest.raises(Exception, match="content_sha256"):
            await call(
                server,
                "character_action",
                {
                    "character_id": actor["id"],
                    "action": "attack_source_object",
                    "payload": {
                        "object": source_object,
                        "weapon_id": "mace",
                        "source_ref": {**source_ref, "content_sha256": "0" * 64},
                        "reason": "Invalid source evidence must not mutate the object.",
                        "expected_campaign_revision": current_campaign["revision"],
                    },
                    "expected_revision": actor["revision"],
                    "idempotency_key": "invalid-source",
                },
            )
        mundane_result = None
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
            if result["attack"]["hit"]:
                mundane_result = result
                break

        assert mundane_result is not None
        assert mundane_result["object"]["hit_points"] == 5
        assert mundane_result["object"]["destroyed"] is False
        assert mundane_result["object"]["last_attack"]["weapon_traits"] == []
        assert mundane_result["object"]["last_attack"]["weapon_trait_requirement_met"] is False

        result = None
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
                    "weapon_id": "magic-mace",
                    "source_ref": source_ref,
                    "reason": "The source-defined object is within melee reach.",
                    "expected_campaign_revision": campaign["revision"],
                },
                "expected_revision": actor["revision"],
                "idempotency_key": f"magic-attack-{index}",
            }
            result = await call(server, "character_action", last_arguments)
            if result["object"]["destroyed"]:
                break

        assert result is not None
        assert result["status"] == "committed"
        assert result["object"]["destroyed"] is True
        assert result["object"]["hit_points"] == 0
        assert result["object"]["damage_immunities"] == ["poison", "psychic"]
        assert result["object"]["damage_filter"] == {
            "allowed_damage_types": ["bludgeoning"],
            "required_any_weapon_traits": ["magical"],
        }
        assert result["object"]["last_attack"]["weapon_traits"] == ["magical"]
        replay = await call(server, "character_action", last_arguments)
        assert replay["object"] == result["object"]

    asyncio.run(exercise())
