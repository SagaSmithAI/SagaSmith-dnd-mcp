from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet
from sagasmith_dnd.content_readiness import (
    build_catalog_review,
    build_selection_contract,
)
from sagasmith_dnd.statblocks import parameterized_statblock_requirements

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def _review_decision(role: str, reviewer: str) -> dict:
    return {
        "role": role,
        "reviewer": reviewer,
        "method": "agent",
        "checks": {
            "identity": True,
            "classification": True,
            "entry_boundary": True,
            "references": True,
        },
        "notes": "Verified against the exact source-bound actor template.",
    }


@pytest.mark.fresh_database
def test_reviewed_addon_actor_template_derives_owner_values_and_receipt(
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
    source_text = """### Steel Defender

*Medium construct, neutral*

**Armor Class** 15 (natural armor)

**Hit Points** equal the steel defender's Constitution modifier + your
Intelligence modifier + five times your artificer level

**Speed** 40 ft.

| STR | DEX | CON | INT | WIS | CHA |
|:---:|:---:|:---:|:---:|:---:|:---:|
| 14 (+2) | 12 (+1) | 14 (+2) | 4 (-3) | 10 (+0) | 6 (-2) |

**Senses** darkvision 60 ft., passive Perception 10

**Languages** understands the languages you speak

**Challenge** 1 (200 XP)

###### Actions

***Force-Empowered Rend.*** *Melee Weapon Attack:* your spell attack modifier
to hit, reach 5 ft., one target. *Hit:* 1d8 + PB force damage.
"""
    requirement = parameterized_statblock_requirements(source_text)
    assert requirement is not None and requirement["runtime_ready"] is True
    artifact = {
        "id": "dnd5e.addon.defender.statblock.steel-defender",
        "kind": "statblock",
        "application_state": "catalog_only",
        "mechanical_scope": "mechanical",
        "execution_state": "ruling_ready",
        "semantic_resolution": {
            "status": "resolved",
            "mode": "agent_ruling",
            "first_use_compilation_required": False,
            "clause_ids": ["steel-defender-source"],
        },
        "rule_clauses": [
            {
                "schema_version": 1,
                "id": "steel-defender-source",
                "title": "Steel Defender",
                "scope": "mechanical",
                "source_citations": [
                    {
                        "source": "book:addon:defender",
                        "source_ref": {"page": 1},
                        "source_excerpt": "Steel Defender",
                    }
                ],
                "settlement": {
                    "mode": "agent_ruling",
                    "default_resolver": "agent",
                    "ruling_kind": "agent_dm_adjudication",
                    "reason": "Resolve remaining source-specific behavior as DM.",
                },
            }
        ],
        "card": {
            "name": "Steel Defender",
            "normalized_content": source_text,
            "dependent_actor_template": requirement,
        },
        "rule_refs": ["book:addon:defender:p1"],
    }
    artifact["selection_contract"] = build_selection_contract(
        artifact,
        status="not_applicable",
        references=["book:addon:defender:p1"],
    )
    artifact["catalog_review"] = build_catalog_review(
        artifact,
        decisions=[
            _review_decision("primary", "agent:template-author"),
            _review_decision("critic", "agent:template-critic"),
        ],
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Addon actor", "idempotency_key": "addon-actor-campaign"},
        )
        profile = await _call(
            server,
            "campaign_rule_profile_set",
            {
                "campaign_id": campaign["id"],
                "edition": "2014",
                "expected_revision": campaign["revision"],
                "idempotency_key": "addon-actor-profile",
            },
        )
        draft = await _call(
            server,
            "rule_pack_draft",
            {
                "manifest": {
                    "id": "dnd5e.addon.defender",
                    "version": "1.0.0",
                    "title": "Reviewed Defender",
                    "namespace": "dnd5e.addon.defender",
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
            {"pack_id": "dnd5e.addon.defender", "version": "1.0.0"},
        )
        await _call(
            server,
            "campaign_rule_pack_set",
            {
                "campaign_id": campaign["id"],
                "pack_id": "dnd5e.addon.defender",
                "version": "1.0.0",
                "expected_revision": profile["campaign_revision"],
                "idempotency_key": "addon-actor-activate",
            },
        )
        owner_sheet = default_character_sheet()
        owner_sheet["progression"]["level"] = 5
        owner_sheet["progression"]["classes"] = [
            {"name": "Artificer", "level": 5, "subclass": "", "hit_die": 8}
        ]
        owner_sheet["abilities"]["intelligence"]["score"] = 18
        owner_sheet["spellcasting"]["ability"] = "intelligence"
        owner = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Owner",
                "sheet": owner_sheet,
                "idempotency_key": "addon-actor-owner",
            },
        )
        catalog = await _call(
            server,
            "content_catalog_list",
            {"campaign_id": campaign["id"], "query": artifact["id"]},
        )
        assert catalog[0]["selection_requirements"]["creation_tool"] == ("addon_actor_instantiate")
        created = await _call(
            server,
            "addon_actor_instantiate",
            {
                "campaign_id": campaign["id"],
                "artifact_id": artifact["id"],
                "owner_character_id": owner["id"],
                "idempotency_key": "addon-actor-create",
            },
        )
        replay = await _call(
            server,
            "addon_actor_instantiate",
            {
                "campaign_id": campaign["id"],
                "artifact_id": artifact["id"],
                "owner_character_id": owner["id"],
                "idempotency_key": "addon-actor-create",
            },
        )

        assert created["character"]["id"] == replay["character"]["id"]
        assert created["character"]["sheet"]["combat"]["hp"]["max"] == 31
        assert created["content_receipt"]["numeric_parameters"] == {
            "owner_class_level": 5,
            "owner_intelligence_modifier": 4,
            "owner_proficiency_bonus": 3,
            "owner_spell_attack_modifier": 7,
        }
        assert created["actor_knowledge_imported"] is False
        assert (
            "sagasmith:addon-actor-template:"
            in created["character"]["notes"]["profile"]["dm_notes"]
        )

    import asyncio

    asyncio.run(exercise())


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
            "dnd5e.addon.reviewed-item@1.0.0:dnd5e.addon.reviewed-item.item.moon-blade"
        )
        assert applied["sheet"]["content"]["selections"][0]["selection"] == {
            "inventory_item_id": item["id"]
        }
        assert applied["content_context"]["artifact_id"] == artifact["id"]
        assert applied["content_context"]["card"]["inventory_template"]["name"] == ("Moon Blade")
        assert applied["rule_receipts"][0]["mechanic_id"] == ("dnd5e.character.inventory_item.v1")
        queried = await _call(
            server,
            "content_catalog_list",
            {
                "campaign_id": campaign["id"],
                "query": artifact["id"],
                "include_context": True,
            },
        )
        assert (
            queried[0]["runtime_context"]["content_hash"]
            == (applied["content_context"]["content_hash"])
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


@pytest.mark.fresh_database
def test_reviewed_addon_base_class_uses_bound_level_one_materializer(tmp_path: Path) -> None:
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
            {"name": "Addon class", "idempotency_key": "addon-class-campaign"},
        )
        profile = await _call(
            server,
            "campaign_rule_profile_set",
            {
                "campaign_id": campaign["id"],
                "edition": "2014",
                "expected_revision": campaign["revision"],
                "idempotency_key": "addon-class-profile",
            },
        )
        artifact = {
            "id": "dnd5e.addon.artificer.class.artificer",
            "kind": "class",
            "application_state": "selection_ready",
            "mechanical_scope": "descriptive",
            "execution_state": "descriptive_ready",
            "semantic_resolution": {
                "status": "resolved",
                "mode": "descriptive",
                "first_use_compilation_required": False,
            },
            "card": {
                "name": "Artificer",
                "class_definition": {
                    "hit_die": 8,
                    "saving_throw_proficiencies": ["constitution", "intelligence"],
                    "armor_proficiencies": ["light armor", "medium armor", "shields"],
                    "weapon_proficiencies": ["simple weapons"],
                    "tool_proficiencies": ["thieves' tools", "tinker's tools"],
                    "skill_choice_count": 2,
                    "skill_options": ["arcana", "history", "investigation", "medicine"],
                },
            },
            "rule_refs": ["book:addon:artificer:p2"],
        }
        artifact["selection_contract"] = build_selection_contract(
            artifact,
            status="ready",
            references=["book:addon:artificer:p2"],
        )
        draft = await _call(
            server,
            "rule_pack_draft",
            {
                "manifest": {
                    "id": "dnd5e.addon.artificer",
                    "version": "1.0.0",
                    "title": "Reviewed Artificer",
                    "namespace": "dnd5e.addon.artificer",
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
            {"pack_id": "dnd5e.addon.artificer", "version": "1.0.0"},
        )
        await _call(
            server,
            "campaign_rule_pack_set",
            {
                "campaign_id": campaign["id"],
                "pack_id": "dnd5e.addon.artificer",
                "version": "1.0.0",
                "expected_revision": profile["campaign_revision"],
                "idempotency_key": "addon-class-activate",
            },
        )
        sheet = default_character_sheet()
        sheet["abilities"]["constitution"]["score"] = 14
        character = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Class Tester",
                "sheet": sheet,
                "idempotency_key": "addon-class-character",
            },
        )

        applied = await _call(
            server,
            "character_content_apply",
            {
                "character_id": character["id"],
                "artifact_id": artifact["id"],
                "selection": {"skills": ["arcana", "investigation"]},
                "expected_revision": character["revision"],
                "idempotency_key": "addon-class-apply",
            },
        )
        assert applied["sheet"]["progression"]["classes"] == [
            {"name": "Artificer", "level": 1, "subclass": "", "hit_die": 8}
        ]
        assert applied["sheet"]["combat"]["hp"]["max"] == 10
        assert applied["sheet"]["skills"]["arcana"]["proficiency"] == "proficient"
        assert applied["class_materialization"]["saving_throw_proficiencies"] == [
            "constitution",
            "intelligence",
        ]
        assert applied["rule_receipts"][0]["mechanic_id"] == ("dnd5e.character.base_class.v1")
        assert applied["sheet"]["content"]["selections"][0]["selection"] == {
            "skills": ["arcana", "investigation"]
        }

    import asyncio

    asyncio.run(exercise())
