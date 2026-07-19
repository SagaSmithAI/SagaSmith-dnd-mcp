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
        (
            item
            for item in character["sheet"]["inventory"]["items"]
            if item["id"] == item_id
        ),
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
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Source",
                "idempotency_key": "source",
            },
        )
        target = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Target",
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
                        "name": "Torch",
                        "kind": "equipment",
                        "quantity": 1,
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
        assert _item(withdrew["character"], "party-torch")["quantity"] == 1
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
