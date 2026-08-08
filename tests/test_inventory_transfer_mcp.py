import asyncio
from pathlib import Path

import pytest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def _item(character: dict, item_id: str) -> dict | None:
    return next(
        (item for item in character["sheet"]["inventory"]["items"] if item["id"] == item_id),
        None,
    )


def test_inventory_transfer_facade_is_authorized_atomic_and_directional(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Transfers", "idempotency_key": "campaign"},
        )
        source = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Source"},
                "principal_id": "system:local",
                "idempotency_key": "source",
            },
        )
        target = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Target"},
                "principal_id": "system:local",
                "idempotency_key": "target",
            },
        )
        added = await _call(
            server,
            "inventory_change",
            {
                "owner": "character",
                "action": "add",
                "owner_id": source["id"],
                "payload": {
                    "item": {
                        "id": "silk-rope",
                        "name": "Silk rope",
                        "kind": "equipment",
                        "quantity": 2,
                    }
                },
                "expected_revision": source["revision"],
                "idempotency_key": "source-rope",
            },
        )
        source = added["character"]
        campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        await _call(
            server,
            "inventory_change",
            {
                "owner": "party",
                "action": "add",
                "owner_id": campaign["id"],
                "payload": {
                    "item": {
                        "id": "party-torch",
                        "name": "Storm Brand",
                        "kind": "weapon",
                        "quantity": 1,
                        "mechanics": {
                            "category": "martial",
                            "attack_type": "melee",
                            "attack_ability": "strength",
                            "damage_formula": "1d8",
                            "damage_type": "slashing",
                            "properties": [],
                            "on_hit_effect": (
                                "On a hit, the brand invokes its source-defined storm mark."
                            ),
                        },
                    }
                },
                "expected_revision": campaign["revision"],
                "idempotency_key": "party-torch",
            },
        )
        await _call(
            server,
            "access_grant",
            {
                "scope": "campaign",
                "campaign_id": campaign["id"],
                "principal_id": "player:alice",
                "payload": {"role": "player"},
            },
        )
        await _call(
            server,
            "access_grant",
            {
                "scope": "actor",
                "campaign_id": campaign["id"],
                "principal_id": "player:alice",
                "payload": {
                    "actor_id": source["id"],
                    "can_control": True,
                    "can_view_private": True,
                },
            },
        )
        campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )

        with pytest.raises(Exception, match="actor"):
            await _call(
                server,
                "inventory_transfer",
                {
                    "mode": "character_to_character",
                    "payload": {
                        "source_character_id": source["id"],
                        "target_character_id": target["id"],
                        "item_id": "silk-rope",
                        "quantity": 1,
                        "expected_campaign_revision": campaign["revision"],
                        "expected_source_revision": source["revision"],
                        "expected_target_revision": target["revision"],
                    },
                    "principal_id": "player:alice",
                    "idempotency_key": "unauthorized-transfer",
                },
            )
        unchanged_source = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": source["id"]}},
        )
        unchanged_target = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": target["id"]}},
        )
        assert _item(unchanged_source, "silk-rope")["quantity"] == 2
        assert _item(unchanged_target, "silk-rope") is None

        await _call(
            server,
            "access_grant",
            {
                "scope": "actor",
                "campaign_id": campaign["id"],
                "principal_id": "player:alice",
                "payload": {
                    "actor_id": target["id"],
                    "can_control": True,
                    "can_view_private": True,
                },
            },
        )
        moved = await _call(
            server,
            "inventory_transfer",
            {
                "mode": "character_to_character",
                "payload": {
                    "source_character_id": source["id"],
                    "target_character_id": target["id"],
                    "item_id": "silk-rope",
                    "quantity": 1,
                    "expected_campaign_revision": campaign["revision"],
                    "expected_source_revision": unchanged_source["revision"],
                    "expected_target_revision": unchanged_target["revision"],
                },
                "principal_id": "player:alice",
                "idempotency_key": "authorized-transfer",
            },
        )
        assert _item(moved["source"], "silk-rope")["quantity"] == 1
        assert _item(moved["target"], moved["item"]["id"])["quantity"] == 1

        with pytest.raises(Exception, match="revision"):
            await _call(
                server,
                "inventory_transfer",
                {
                    "mode": "character_to_character",
                    "payload": {
                        "source_character_id": source["id"],
                        "target_character_id": target["id"],
                        "item_id": "silk-rope",
                        "quantity": 1,
                        "expected_campaign_revision": campaign["revision"],
                        "expected_source_revision": unchanged_source["revision"],
                        "expected_target_revision": unchanged_target["revision"],
                    },
                    "principal_id": "player:alice",
                    "idempotency_key": "stale-transfer",
                },
            )
        after_stale = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": source["id"]}},
        )
        assert _item(after_stale, "silk-rope")["quantity"] == 1

        campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        withdrew = await _call(
            server,
            "inventory_transfer",
            {
                "mode": "party_to_character",
                "payload": {
                    "campaign_id": campaign["id"],
                    "character_id": source["id"],
                    "item_id": "party-torch",
                    "expected_campaign_revision": campaign["revision"],
                    "expected_character_revision": after_stale["revision"],
                },
                "principal_id": "player:alice",
                "idempotency_key": "withdraw-torch",
            },
        )
        withdrawn_item = _item(withdrew["character"], "party-torch")
        assert withdrawn_item["quantity"] == 1
        assert withdrawn_item["ruling_requirements"][0]["policy_ref"] == ("actor_card.import.v1")
        assert all(item["id"] != "party-torch" for item in withdrew["party"]["inventory"]["items"])

        campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        deposited = await _call(
            server,
            "inventory_transfer",
            {
                "mode": "character_to_party",
                "payload": {
                    "campaign_id": campaign["id"],
                    "character_id": source["id"],
                    "item_id": "party-torch",
                    "expected_campaign_revision": campaign["revision"],
                    "expected_character_revision": withdrew["character"]["revision"],
                },
                "principal_id": "player:alice",
                "idempotency_key": "deposit-torch",
            },
        )
        assert _item(deposited["character"], "party-torch") is None
        assert any(item["id"] == "party-torch" for item in deposited["party"]["inventory"]["items"])

    asyncio.run(exercise())


def test_inventory_transfer_detaches_ammunition_links_missing_at_destination(
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

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Ammunition Transfers", "idempotency_key": "campaign"},
        )
        source = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Source"},
                "principal_id": "system:local",
                "idempotency_key": "source",
            },
        )
        target = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Target"},
                "principal_id": "system:local",
                "idempotency_key": "target",
            },
        )
        for item in (
            {
                "id": "arrows",
                "name": "Arrows",
                "kind": "ammunition",
                "quantity": 20,
            },
            {
                "id": "party-bow",
                "name": "Shortbow",
                "kind": "weapon",
                "mechanics": {
                    "category": "simple",
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
            {
                "id": "target-bow",
                "name": "Shortbow",
                "kind": "weapon",
                "mechanics": {
                    "category": "simple",
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
        ):
            added = await _call(
                server,
                "inventory_change",
                {
                    "owner": "character",
                    "action": "add",
                    "owner_id": source["id"],
                    "payload": {"item": item},
                    "expected_revision": source["revision"],
                    "idempotency_key": f"add-{item['id']}",
                },
            )
            source = added["character"]

        campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        deposited = await _call(
            server,
            "inventory_transfer",
            {
                "mode": "character_to_party",
                "payload": {
                    "campaign_id": campaign["id"],
                    "character_id": source["id"],
                    "item_id": "party-bow",
                    "expected_campaign_revision": campaign["revision"],
                    "expected_character_revision": source["revision"],
                },
                "idempotency_key": "deposit-party-bow",
            },
        )
        assert _item(deposited["character"], "arrows") is not None
        assert _item(deposited["character"], "party-bow") is None
        party_bow = next(
            item for item in deposited["party"]["inventory"]["items"] if item["id"] == "party-bow"
        )
        assert party_bow["mechanics"]["ammunition_item_id"] is None
        assert deposited["item"]["mechanics"]["ammunition_item_id"] is None

        campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        source = deposited["character"]
        target = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": target["id"]}},
        )
        transferred = await _call(
            server,
            "inventory_transfer",
            {
                "mode": "character_to_character",
                "payload": {
                    "source_character_id": source["id"],
                    "target_character_id": target["id"],
                    "item_id": "target-bow",
                    "expected_campaign_revision": campaign["revision"],
                    "expected_source_revision": source["revision"],
                    "expected_target_revision": target["revision"],
                },
                "idempotency_key": "transfer-target-bow",
            },
        )
        assert _item(transferred["source"], "arrows") is not None
        assert _item(transferred["target"], "target-bow")["mechanics"]["ammunition_item_id"] is None
        assert transferred["item"]["mechanics"]["ammunition_item_id"] is None

    asyncio.run(exercise())
