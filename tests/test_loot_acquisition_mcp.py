from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from tests.authoring_helpers import finalize_and_activate_module


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    value = result.get("result", result) if isinstance(result, dict) else result
    if isinstance(value, dict) and "action" in value and "result" in value:
        return value["result"]
    return value


def test_source_bound_loot_is_atomic_idempotent_and_branch_audited(tmp_path: Path) -> None:
    module_root = tmp_path / "modules"
    module_root.mkdir()
    source = module_root / "adventure.md"
    source.write_text(
        "# Chapter One\n\n"
        "## Treasure Room\n\n"
        "The chest contains 60 cp, two healing draughts, and a jade frog.\n",
        encoding="utf-8",
    )
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        module_import_roots=(module_root,),
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Loot acquisition", "idempotency_key": "campaign"},
        )
        character = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Tribute owner"},
                "principal_id": "system:local",
                "idempotency_key": "tribute-owner",
            },
        )
        added_character_item = await _call(
            server,
            "inventory_change",
            {
                "owner": "character",
                "action": "add",
                "owner_id": character["id"],
                "payload": {
                    "item": {
                        "id": "spare-cloak",
                        "name": "Spare cloak",
                        "kind": "equipment",
                        "quantity": 2,
                    }
                },
                "expected_revision": character["revision"],
                "idempotency_key": "tribute-owner-cloak",
            },
        )
        character = added_character_item["character"]
        staged = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "loot-module",
                    "title": "Loot Module",
                },
                "idempotency_key": "stage",
            },
        )
        activation = await finalize_and_activate_module(
            _call,
            server,
            campaign["id"],
            staged,
            source_key="loot-module",
            title="Loot Module",
            portable_id="dnd5e.module.loot-test",
        )
        module_id = activation["activated"]["activation"]["module_id"]
        search = await _call(
            server,
            "module_search",
            {
                "campaign_id": campaign["id"],
                "query": "jade frog",
                "top_k": 3,
                "module_ids": [module_id],
            },
        )
        assert {item["source_id"] for item in search} == {module_id}
        chunk_id = search[0]["id"]
        expanded = await _call(server, "module_expand", {"chunk_id": chunk_id})
        assert expanded["source_ref"]["module_id"] == module_id
        assert expanded["source_ref"]["scene_id"] == expanded["scene"]["id"]
        assert expanded["source_ref"]["chunk_id"] == chunk_id
        assert expanded["source_ref"]["content_sha256"] == expanded["content_sha256"]
        source_ref = json.dumps(
            expanded["source_ref"],
            sort_keys=True,
            separators=(",", ":"),
        )
        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        play = await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": current["revision"],
                "idempotency_key": "play",
            },
        )
        arguments = {
            "campaign_id": campaign["id"],
            "action": "loot_acquire",
            "payload": {
                "acquisition_id": "chapter-one-chest",
                "coins": {"cp": 60},
                "items": [
                    {
                        "id": "healing-draught",
                        "name": "Healing draught",
                        "kind": "consumable",
                        "quantity": 2,
                    },
                    {
                        "id": "jade-frog",
                        "name": "Jade frog",
                        "kind": "loot",
                        "quantity": 1,
                        "price_cp": 4000,
                    },
                ],
                "reason": "The party opened the source-defined treasure chest.",
                "source_ref": source_ref,
            },
            "expected_revision": play["campaign_revision"],
            "idempotency_key": "loot",
        }
        invalid_source = json.loads(source_ref)
        invalid_source["content_sha256"] = "0" * 64
        with pytest.raises(Exception, match="content_sha256 does not match"):
            await _call(
                server,
                "campaign_change",
                {
                    **arguments,
                    "payload": {
                        **arguments["payload"],
                        "source_ref": json.dumps(
                            invalid_source, sort_keys=True, separators=(",", ":")
                        ),
                    },
                    "idempotency_key": "invalid-source",
                },
            )
        acquired = await _call(server, "campaign_change", arguments)
        replay = await _call(
            server,
            "campaign_change",
            {
                **arguments,
                "expected_revision": acquired["campaign"]["revision"],
            },
        )

        assert replay == acquired
        assert acquired["status"] == "committed"
        assert acquired["party"]["inventory"]["wallet"]["cp"] == 60
        assert [item["id"] for item in acquired["items"]] == [
            "healing-draught",
            "jade-frog",
        ]
        assert acquired["campaign"]["revision"] == play["campaign_revision"] + 1
        assert acquired["campaign"]["state"]["loot_acquisitions"][0]["id"] == ("chapter-one-chest")
        with pytest.raises(Exception, match="acquisition_id already exists"):
            await _call(
                server,
                "campaign_change",
                {
                    **arguments,
                    "expected_revision": acquired["campaign"]["revision"],
                    "idempotency_key": "duplicate-loot",
                },
            )
        spend_arguments = {
            "campaign_id": campaign["id"],
            "action": "currency_spend",
            "payload": {
                "spend_id": "chapter-one-lodging",
                "coins": {"cp": 25},
                "reason": "The party paid its source-bound lodging expense.",
                "source_ref": source_ref,
                "rule_ref": "srd2014.expenses.food-drink-lodging.modest-inn",
            },
            "expected_revision": acquired["campaign"]["revision"],
            "idempotency_key": "spend",
        }
        with pytest.raises(Exception, match="wallet balance cannot be negative"):
            await _call(
                server,
                "campaign_change",
                {
                    **spend_arguments,
                    "payload": {
                        **spend_arguments["payload"],
                        "coins": {"cp": 25, "gp": 1},
                    },
                    "idempotency_key": "spend-insufficient",
                },
            )
        unchanged = await _call(
            server,
            "campaign_query",
            {"view": "party", "payload": {"campaign_id": campaign["id"]}},
        )
        assert unchanged["inventory"]["wallet"]["cp"] == 60
        spent = await _call(server, "campaign_change", spend_arguments)
        spend_replay = await _call(
            server,
            "campaign_change",
            {
                **spend_arguments,
                "expected_revision": spent["campaign"]["revision"],
            },
        )

        assert spend_replay == spent
        assert spent["status"] == "committed"
        assert spent["coins"] == {"cp": 25}
        assert spent["party"]["inventory"]["wallet"]["cp"] == 35
        assert spent["campaign"]["state"]["currency_spends"] == [
            {
                "id": "chapter-one-lodging",
                "reason": "The party paid its source-bound lodging expense.",
                "source_ref": source_ref,
                "rule_ref": "srd2014.expenses.food-drink-lodging.modest-inn",
                "coins": {"cp": 25},
            }
        ]
        with pytest.raises(Exception, match="spend_id already exists"):
            await _call(
                server,
                "campaign_change",
                {
                    **spend_arguments,
                    "expected_revision": spent["campaign"]["revision"],
                    "idempotency_key": "duplicate-spend",
                },
            )
        item_spend_arguments = {
            "campaign_id": campaign["id"],
            "action": "item_spend",
            "payload": {
                "spend_id": "offer-jade-frog",
                "item_id": "jade-frog",
                "quantity": 1,
                "reason": "The party surrendered the source-bound jade frog.",
                "source_ref": source_ref,
            },
            "expected_revision": spent["campaign"]["revision"],
            "idempotency_key": "item-spend",
        }
        with pytest.raises(Exception, match="quantity exceeds the item stack"):
            await _call(
                server,
                "campaign_change",
                {
                    **item_spend_arguments,
                    "payload": {**item_spend_arguments["payload"], "quantity": 2},
                    "idempotency_key": "item-spend-too-many",
                },
            )
        unchanged = await _call(
            server,
            "campaign_query",
            {"view": "party", "payload": {"campaign_id": campaign["id"]}},
        )
        assert any(item["id"] == "jade-frog" for item in unchanged["inventory"]["items"])
        item_spent = await _call(server, "campaign_change", item_spend_arguments)
        item_spend_replay = await _call(
            server,
            "campaign_change",
            {
                **item_spend_arguments,
                "expected_revision": item_spent["campaign"]["revision"],
            },
        )

        assert item_spend_replay == item_spent
        assert item_spent["status"] == "committed"
        assert item_spent["removed"]["id"] == "jade-frog"
        assert all(item["id"] != "jade-frog" for item in item_spent["party"]["inventory"]["items"])
        assert item_spent["campaign"]["state"]["item_spends"] == [
            {
                "id": "offer-jade-frog",
                "item_id": "jade-frog",
                "quantity": 1,
                "reason": "The party surrendered the source-bound jade frog.",
                "source_ref": source_ref,
                "removed": item_spent["removed"],
            }
        ]
        with pytest.raises(Exception, match="item spend_id already exists"):
            await _call(
                server,
                "campaign_change",
                {
                    **item_spend_arguments,
                    "expected_revision": item_spent["campaign"]["revision"],
                    "idempotency_key": "duplicate-item-spend",
                },
            )
        character_item_spend_arguments = {
            "campaign_id": campaign["id"],
            "action": "item_spend",
            "payload": {
                "spend_id": "offer-spare-cloak",
                "item_id": "spare-cloak",
                "quantity": 1,
                "reason": "The character surrendered one source-bound spare cloak.",
                "source_ref": source_ref,
                "character_id": character["id"],
                "expected_character_revision": character["revision"],
            },
            "expected_revision": item_spent["campaign"]["revision"],
            "idempotency_key": "character-item-spend",
        }
        with pytest.raises(
            Exception,
            match="character_id and expected_character_revision must be provided together",
        ):
            await _call(
                server,
                "campaign_change",
                {
                    **character_item_spend_arguments,
                    "payload": {
                        key: value
                        for key, value in character_item_spend_arguments["payload"].items()
                        if key != "expected_character_revision"
                    },
                    "idempotency_key": "character-item-spend-missing-revision",
                },
            )
        character_item_spent = await _call(
            server,
            "campaign_change",
            character_item_spend_arguments,
        )

        assert character_item_spent["status"] == "committed"
        assert character_item_spent["owner"] == {
            "kind": "character",
            "character_id": character["id"],
        }
        remaining_cloak = next(
            item
            for item in character_item_spent["character"]["sheet"]["inventory"]["items"]
            if item["id"] == "spare-cloak"
        )
        assert remaining_cloak["quantity"] == 1
        assert character_item_spent["campaign"]["state"]["item_spends"][1] == {
            "id": "offer-spare-cloak",
            "item_id": "spare-cloak",
            "quantity": 1,
            "reason": "The character surrendered one source-bound spare cloak.",
            "source_ref": source_ref,
            "character_id": character["id"],
            "owner": {
                "kind": "character",
                "character_id": character["id"],
            },
            "removed": character_item_spent["removed"],
        }

    asyncio.run(exercise())
