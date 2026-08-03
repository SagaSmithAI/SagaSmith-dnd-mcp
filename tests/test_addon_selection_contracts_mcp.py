from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet
from sagasmith_dnd.content_readiness import build_selection_contract

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


@pytest.mark.fresh_database
def test_reviewed_addon_item_uses_bound_inventory_materializer(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[2]
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=workspace / "SagaSmith-dnd-skills",
        modulegen_skills_dir=workspace / "SagaSmith-module-gen-skills",
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Addon item", "idempotency_key": "addon-item-campaign"},
        )
        profile = await _call(
            server,
            "campaign_rule_profile_set",
            {
                "campaign_id": campaign["id"],
                "edition": "2014",
                "expected_revision": campaign["revision"],
                "idempotency_key": "addon-item-profile",
            },
        )
        artifact = {
            "id": "dnd5e.addon.reviewed-item.item.moon-blade",
            "kind": "item",
            "application_state": "selection_ready",
            "mechanical_scope": "descriptive",
            "execution_state": "descriptive_ready",
            "semantic_resolution": {
                "status": "resolved",
                "mode": "descriptive",
                "first_use_compilation_required": False,
            },
            "card": {
                "name": "Moon Blade",
                "inventory_template": {
                    "name": "Moon Blade",
                    "kind": "weapon",
                    "quantity": 1,
                    "description": "A reviewed addon weapon.",
                    "mechanics": {
                        "damage_formula": "1d8",
                        "damage_type": "slashing",
                        "attack_ability": "strength",
                    },
                },
            },
            "rule_refs": ["book:addon:p1"],
        }
        artifact["selection_contract"] = build_selection_contract(
            artifact,
            status="ready",
            references=["book:addon:p1"],
        )
        draft = await _call(
            server,
            "rule_pack_draft",
            {
                "manifest": {
                    "id": "dnd5e.addon.reviewed-item",
                    "version": "1.0.0",
                    "title": "Reviewed item",
                    "namespace": "dnd5e.addon.reviewed-item",
                    "system_id": "dnd5e",
                    "editions": ["2014"],
                    "capabilities": [],
                },
                "artifacts": [artifact],
                "mechanics": [],
            },
        )
        assert draft["status"] == "validated", str(draft)
        await _call(
            server,
            "rule_pack_install",
            {"pack_id": "dnd5e.addon.reviewed-item", "version": "1.0.0"},
        )
        blocked = await _call(
            server,
            "rule_pack_draft",
            {
                "manifest": {
                    "id": "dnd5e.addon.blocked",
                    "version": "1.0.0",
                    "title": "Blocked addon",
                    "namespace": "dnd5e.addon.blocked",
                    "system_id": "dnd5e",
                    "editions": ["2014"],
                    "capabilities": [],
                    "readiness_policy": "review_required",
                },
                "artifacts": [],
                "mechanics": [],
            },
        )
        assert blocked["status"] == "validated"
        await _call(
            server,
            "rule_pack_install",
            {"pack_id": "dnd5e.addon.blocked", "version": "1.0.0"},
        )
        with pytest.raises(Exception, match="four-dimensional review"):
            await _call(
                server,
                "campaign_rule_pack_set",
                {
                    "campaign_id": campaign["id"],
                    "pack_id": "dnd5e.addon.blocked",
                    "version": "1.0.0",
                    "expected_revision": profile["campaign_revision"],
                    "idempotency_key": "blocked-addon-activate",
                },
            )
        await _call(
            server,
            "campaign_rule_pack_set",
            {
                "campaign_id": campaign["id"],
                "pack_id": "dnd5e.addon.reviewed-item",
                "version": "1.0.0",
                "expected_revision": profile["campaign_revision"],
                "idempotency_key": "addon-item-activate",
            },
        )
        character = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Item Tester",
                "sheet": default_character_sheet(),
                "idempotency_key": "addon-item-character",
            },
        )

        rejected = await _call(
            server,
            "character_content_apply",
            {
                "character_id": character["id"],
                "artifact_id": artifact["id"],
                "selection": {"raw_payload": {"mechanics": {"damage_formula": "99d99"}}},
                "expected_revision": character["revision"],
                "idempotency_key": "addon-item-rejected",
            },
        )
        assert rejected["status"] == "pending_ruling"
        assert "raw_payload" in rejected["errors"][0]

        applied = await _call(
            server,
            "character_content_apply",
            {
                "character_id": character["id"],
                "artifact_id": artifact["id"],
                "expected_revision": character["revision"],
                "idempotency_key": "addon-item-applied",
            },
        )
        assert "sheet" in applied, str(applied)
        item = applied["sheet"]["inventory"]["items"][0]
        assert item["name"] == "Moon Blade"
        assert item["mechanics"]["damage_formula"] == "1d8"
        assert item["source_key"] == (
            "dnd5e.addon.reviewed-item@1.0.0:"
            "dnd5e.addon.reviewed-item.item.moon-blade"
        )
        assert applied["sheet"]["content"]["selections"][0]["selection"] == {
            "inventory_item_id": item["id"]
        }
        assert applied["content_context"]["artifact_id"] == artifact["id"]
        assert applied["content_context"]["card"]["inventory_template"]["name"] == (
            "Moon Blade"
        )
        assert applied["rule_receipts"][0]["mechanic_id"] == (
            "dnd5e.character.inventory_item.v1"
        )
        queried = await _call(
            server,
            "content_catalog_list",
            {
                "campaign_id": campaign["id"],
                "query": artifact["id"],
                "include_context": True,
            },
        )
        assert queried[0]["runtime_context"]["content_hash"] == (
            applied["content_context"]["content_hash"]
        )
        receipts = await _call(
            server,
            "campaign_rule_receipts",
            {
                "campaign_id": campaign["id"],
                "mechanic_id": "dnd5e.character.inventory_item.v1",
            },
        )
        assert receipts[0]["receipt"]["artifact_id"] == artifact["id"]

    import asyncio

    asyncio.run(exercise())
