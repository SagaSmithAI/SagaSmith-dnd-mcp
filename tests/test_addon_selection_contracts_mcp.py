from pathlib import Path

import pytest
from sagasmith_core.rule_packs import RulesetUnavailableError
from sagasmith_dnd.character_schema import default_character_sheet
from sagasmith_dnd.content_readiness import (
    build_catalog_review,
    build_selection_contract,
)
from sagasmith_dnd.statblocks import parameterized_statblock_requirements

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import (
    _character_spell_card,
    _validated_additive_choices,
    _validated_narrative_choices,
    _validated_species_ability_choices,
    _validated_species_proficiency_choices,
    create_server,
)


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


def test_catalog_spell_projection_strips_search_metadata_and_is_independent() -> None:
    catalog_card = {
        "name": "Source Spell",
        "level": 1,
        "classes": ["Wizard"],
        "description": "Catalog-only retrieval text.",
        "source_title": "Addon Source",
        "definition": {"school": "evocation"},
    }

    projected = _character_spell_card(catalog_card)

    assert projected == {
        "name": "Source Spell",
        "level": 1,
        "definition": {"school": "evocation"},
    }
    catalog_card["definition"]["school"] = "illusion"
    assert projected["definition"]["school"] == "evocation"


def test_background_additive_choices_preserve_fixed_and_enforce_bounds() -> None:
    selected, combined = _validated_additive_choices(
        ["gObLiN"],
        count=1,
        label="background language",
        fixed=["Common"],
        options=["Goblin", "Vedalken"],
    )
    assert selected == ["Goblin"]
    assert combined == ["Common", "Goblin"]

    with pytest.raises(ValueError, match="not one of the allowed options"):
        _validated_additive_choices(
            ["Abyssal"],
            count=1,
            label="background language",
            fixed=["Common"],
            options=["Goblin", "Vedalken"],
        )
    with pytest.raises(ValueError, match="cannot duplicate a fixed grant"):
        _validated_additive_choices(
            ["common"],
            count=1,
            label="background language",
            fixed=["Common"],
            options=[],
            allow_unlisted=True,
        )
    with pytest.raises(RulesetUnavailableError, match="reviewed options"):
        _validated_additive_choices(
            ["Smith's Tools"],
            count=1,
            label="background tool",
            fixed=[],
            options=[],
        )


def test_species_ability_choices_enforce_reviewed_option_subset() -> None:
    requirement = {
        "count": 1,
        "amount": 1,
        "exclude": ["charisma"],
        "options": ["dexterity", "intelligence"],
    }

    assert _validated_species_ability_choices(
        ["Dexterity"],
        requirement=requirement,
        valid_abilities={
            "strength",
            "dexterity",
            "constitution",
            "intelligence",
            "wisdom",
            "charisma",
        },
    ) == ["dexterity"]
    with pytest.raises(ValueError, match="allowed options"):
        _validated_species_ability_choices(
            ["wisdom"],
            requirement=requirement,
            valid_abilities={
                "strength",
                "dexterity",
                "constitution",
                "intelligence",
                "wisdom",
                "charisma",
            },
        )


def test_species_cross_kind_proficiency_choices_are_bounded_and_typed() -> None:
    groups = [
        {
            "id": "natural_talent",
            "count": 1,
            "options": [
                {"kind": "skill", "name": "Performance"},
                {"kind": "tool", "name": "Lute"},
            ],
        }
    ]

    assert _validated_species_proficiency_choices(
        {"natural_talent": [{"kind": "tool", "name": "lute"}]},
        groups=groups,
    ) == {"natural_talent": [{"kind": "tool", "name": "Lute"}]}
    with pytest.raises(ValueError, match="allowed option"):
        _validated_species_proficiency_choices(
            {"natural_talent": [{"kind": "skill", "name": "Stealth"}]},
            groups=groups,
        )


def test_narrative_choices_preserve_bounded_agent_context_without_false_grants() -> None:
    groups = [
        {
            "id": "psychic_glamour",
            "count": 1,
            "options": ["Insight", "Intimidation", "Performance", "Persuasion"],
        }
    ]

    assert _validated_narrative_choices(
        {"psychic_glamour": ["insight"]},
        groups=groups,
    ) == {"psychic_glamour": ["Insight"]}
    with pytest.raises(ValueError, match="not an allowed option"):
        _validated_narrative_choices(
            {"psychic_glamour": ["Perception"]},
            groups=groups,
        )


@pytest.mark.fresh_database
def test_reviewed_addon_feat_materializes_bounded_spell_sources(tmp_path: Path) -> None:
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
            {"name": "Addon feat", "idempotency_key": "addon-feat-campaign"},
        )
        profile = await _call(
            server,
            "campaign_rule_profile_set",
            {
                "campaign_id": campaign["id"],
                "edition": "2014",
                "expected_revision": campaign["revision"],
                "idempotency_key": "addon-feat-profile",
            },
        )
        artifact = {
            "id": "dnd5e.addon.eberron.feat.aberrant-dragonmark",
            "kind": "feat",
            "application_state": "selection_ready",
            "mechanical_scope": "mechanical",
            "execution_state": "engine_ready",
            "semantic_resolution": {
                "status": "resolved",
                "mode": "static_grant",
                "first_use_compilation_required": False,
                "clause_ids": ["aberrant-dragonmark-grants"],
            },
            "rule_clauses": [
                {
                    "schema_version": 1,
                    "id": "aberrant-dragonmark-grants",
                    "title": "Aberrant Dragonmark grants",
                    "scope": "mechanical",
                    "source_citations": [
                        {
                            "source": "book:eberron",
                            "source_ref": {"page": 112},
                            "source_excerpt": (
                                "Increase Constitution by 1 and choose Sorcerer spells."
                            ),
                        }
                    ],
                    "settlement": {
                        "mode": "static_grant",
                        "grant_refs": [
                            "card.mechanical_grants",
                            "card.selection_requirements",
                        ],
                    },
                }
            ],
            "card": {
                "name": "Aberrant Dragonmark",
                "prerequisites": [
                    {"kind": "feature_forbidden", "feature": "dragonmark"}
                ],
                "repeatable": False,
                "selection_requirements": {
                    "field": "spell_choices",
                    "kind": "spell_grants",
                    "groups": [
                        {
                            "id": "cantrip",
                            "count": 1,
                            "level": 0,
                            "eligible_classes": ["Sorcerer"],
                            "method": "known",
                            "spellcasting_ability": "constitution",
                            "free_casts": 0,
                            "recovers_on": None,
                            "allow_slot_cast": False,
                            "minimum_level": 1,
                            "ritual_only": False,
                        },
                        {
                            "id": "level_1_spell",
                            "count": 1,
                            "level": 1,
                            "eligible_classes": ["Sorcerer"],
                            "method": "limited_use",
                            "spellcasting_ability": "constitution",
                            "free_casts": 1,
                            "recovers_on": "long_rest",
                            "allow_slot_cast": False,
                            "minimum_level": 1,
                            "ritual_only": False,
                        },
                    ],
                },
                "mechanical_grants": {
                    "ability_score_increases": {"constitution": 1},
                    "maximum_ability_score": 20,
                    "languages": [],
                    "tool_proficiencies": [],
                    "weapon_proficiencies": [],
                    "spell_grants": [],
                },
            },
            "rule_refs": ["book:eberron:p112"],
        }
        artifact["selection_contract"] = build_selection_contract(
            artifact,
            status="ready",
            references=["book:eberron:p112"],
        )
        draft = await _call(
            server,
            "rule_pack_draft",
            {
                "manifest": {
                    "id": "dnd5e.addon.eberron",
                    "version": "1.0.0",
                    "title": "Reviewed Eberron",
                    "namespace": "dnd5e.addon.eberron",
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
            {"pack_id": "dnd5e.addon.eberron", "version": "1.0.0"},
        )
        await _call(
            server,
            "campaign_rule_pack_set",
            {
                "campaign_id": campaign["id"],
                "pack_id": "dnd5e.addon.eberron",
                "version": "1.0.0",
                "expected_revision": profile["campaign_revision"],
                "idempotency_key": "addon-feat-activate",
            },
        )
        sheet = default_character_sheet()
        sheet["abilities"]["constitution"]["score"] = 10
        character = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Marked Tester",
                "sheet": sheet,
                "idempotency_key": "addon-feat-character",
            },
        )
        applied = await _call(
            server,
            "character_content_apply",
            {
                "character_id": character["id"],
                "artifact_id": artifact["id"],
                "selection": {
                    "spell_choices": {
                        "cantrip": ["dnd5e.content.srd2014.spell.light"],
                        "level_1_spell": [
                            "dnd5e.content.srd2014.spell.burning-hands"
                        ],
                    }
                },
                "expected_revision": character["revision"],
                "idempotency_key": "addon-feat-apply",
            },
        )

        assert applied["sheet"]["abilities"]["constitution"]["score"] == 11
        spells = {
            item["id"]: item for item in applied["sheet"]["content"]["spells"]
        }
        burning_hands = spells["dnd5e.content.srd2014.spell.burning-hands"]
        casting_source = burning_hands["access"]["feature_casting_sources"][0]
        assert casting_source["spellcasting_ability"] == "constitution"
        assert casting_source["allow_slot_cast"] is False
        resource = applied["sheet"]["resources"][casting_source["resource_key"]]
        assert resource["value"] == resource["max"] == 1
        assert resource["recovers_on"] == "long_rest"
        feat = applied["sheet"]["content"]["feats"][0]
        assert feat["choices"]["spell_choices"] == {
            "cantrip": ["dnd5e.content.srd2014.spell.light"],
            "level_1_spell": ["dnd5e.content.srd2014.spell.burning-hands"],
        }

    import asyncio

    asyncio.run(exercise())


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
def test_reviewed_addon_background_materializes_embedded_equipment(tmp_path: Path) -> None:
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
            {"name": "Addon background", "idempotency_key": "background-campaign"},
        )
        profile = await _call(
            server,
            "campaign_rule_profile_set",
            {
                "campaign_id": campaign["id"],
                "edition": "2014",
                "expected_revision": campaign["revision"],
                "idempotency_key": "background-profile",
            },
        )
        artifact = {
            "id": "dnd5e.addon.guild.background.guild-agent",
            "kind": "background",
            "application_state": "selection_ready",
            "mechanical_scope": "descriptive",
            "execution_state": "descriptive_ready",
            "semantic_resolution": {
                "status": "resolved",
                "mode": "descriptive",
                "first_use_compilation_required": False,
            },
            "card": {
                "name": "Guild Agent",
                "skill_proficiencies": ["investigation", "persuasion"],
                "background_grants": {
                    "skills": ["investigation", "persuasion"],
                    "feature": "Guild Membership",
                    "languages": [],
                    "spell_list_expansion": ["Aid"],
                    "tools": [],
                    "equipment_item_ids": [],
                    "equipment": {
                        "items": [
                            {
                                "inventory_template": {
                                    "name": "Identification Papers",
                                    "kind": "equipment",
                                    "quantity": 1,
                                    "description": "Reviewed guild identification.",
                                    "mechanics": {},
                                }
                            }
                        ],
                        "wallet": {"gp": 2},
                    },
                    "choices": {
                        "language_count": 0,
                        "tool_choice_count": 0,
                        "equipment_packages": {
                            "A": {
                                "items": [
                                    {
                                        "inventory_template": {
                                            "name": "Guild Signet",
                                            "kind": "equipment",
                                            "quantity": 1,
                                            "description": "A reviewed guild signet.",
                                            "mechanics": {},
                                        },
                                        "quantity": 1,
                                    }
                                ],
                                "wallet": {"gp": 10},
                            }
                        },
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
        species_artifact = {
            "id": "dnd5e.addon.guild.species.marked-human",
            "kind": "species",
            "application_state": "selection_ready",
            "mechanical_scope": "mechanical",
            "execution_state": "engine_ready",
            "semantic_resolution": {
                "status": "resolved",
                "mode": "static_grant",
                "first_use_compilation_required": False,
                "clause_ids": ["marked-human-spell-list"],
            },
            "rule_clauses": [
                {
                    "schema_version": 1,
                    "id": "marked-human-spell-list",
                    "title": "Marked Human spell list",
                    "scope": "mechanical",
                    "source_citations": [
                        {
                            "source": "book:addon",
                            "source_ref": {"page": 3},
                            "source_excerpt": "Aid is added to the marked spell list.",
                        }
                    ],
                    "settlement": {
                        "mode": "static_grant",
                        "grant_refs": ["card.grants.spell_list_expansion"],
                    },
                }
            ],
            "card": {
                "name": "Marked Human",
                "base_species": "Human",
                "grants": {
                    "ability_score_increases": {"intelligence": 1},
                    "size": "medium",
                    "walk_speed": 30,
                    "languages": ["Common"],
                    "spell_list_expansion": ["Aid"],
                    "features": [],
                    "unresolved": [],
                },
            },
            "rule_refs": ["book:addon:p3"],
        }
        species_artifact["selection_contract"] = build_selection_contract(
            species_artifact,
            status="ready",
            references=["book:addon:p3"],
        )
        subclass_artifact = {
            "id": "dnd5e.addon.guild.subclass.circle-of-spores",
            "kind": "subclass",
            "application_state": "selection_ready",
            "mechanical_scope": "mechanical",
            "execution_state": "engine_ready",
            "semantic_resolution": {
                "status": "resolved",
                "mode": "static_grant",
                "first_use_compilation_required": False,
                "clause_ids": ["circle-of-spores-spell-grants"],
            },
            "rule_clauses": [
                {
                    "schema_version": 1,
                    "id": "circle-of-spores-spell-grants",
                    "title": "Circle of Spores spell grants",
                    "scope": "mechanical",
                    "source_citations": [
                        {
                            "source": "book:addon",
                            "source_ref": {"page": 2},
                            "source_excerpt": (
                                "You learn the chill touch cantrip and gain circle spells."
                            ),
                        }
                    ],
                    "settlement": {
                        "mode": "static_grant",
                        "grant_refs": [
                            "card.always_prepared_spells",
                            "card.spell_grants",
                        ],
                    },
                }
            ],
            "card": {
                "name": "Circle of Spores",
                "class_name": "Druid",
                "minimum_level": 2,
                "always_prepared_spells": [
                    {"name": "Blindness/Deafness", "minimum_level": 3}
                ],
                "spell_grants": [
                    {"name": "Chill Touch", "minimum_level": 2, "method": "known"}
                ],
            },
            "rule_refs": ["book:addon:p2"],
        }
        subclass_artifact["selection_contract"] = build_selection_contract(
            subclass_artifact,
            status="ready",
            references=["book:addon:p2"],
        )
        draft = await _call(
            server,
            "rule_pack_draft",
            {
                "manifest": {
                    "id": "dnd5e.addon.guild",
                    "version": "1.0.0",
                    "title": "Guild addon",
                    "namespace": "dnd5e.addon.guild",
                    "system_id": "dnd5e",
                    "editions": ["2014"],
                    "capabilities": [],
                },
                "artifacts": [artifact, species_artifact, subclass_artifact],
                "mechanics": [],
            },
        )
        assert draft["status"] == "validated", str(draft)
        await _call(
            server,
            "rule_pack_install",
            {"pack_id": "dnd5e.addon.guild", "version": "1.0.0"},
        )
        await _call(
            server,
            "campaign_rule_pack_set",
            {
                "campaign_id": campaign["id"],
                "pack_id": "dnd5e.addon.guild",
                "version": "1.0.0",
                "expected_revision": profile["campaign_revision"],
                "idempotency_key": "background-activate",
            },
        )
        character_sheet = default_character_sheet()
        character_sheet["progression"]["level"] = 3
        character_sheet["progression"]["classes"] = [
            {"name": "Wizard", "level": 3, "subclass": "", "hit_die": 6}
        ]
        character_sheet["spellcasting"].update(
            {
                "ability": "intelligence",
                "class_lists": ["wizard"],
                "spell_slots": {
                    "1": {
                        "label": "1st-level slots",
                        "value": 4,
                        "max": 4,
                        "recovers_on": "long_rest",
                        "source_key": "wizard",
                    },
                    "2": {
                        "label": "2nd-level slots",
                        "value": 2,
                        "max": 2,
                        "recovers_on": "long_rest",
                        "source_key": "wizard",
                    },
                },
                "preparation": {
                    "mode": "spellbook",
                    "max_prepared": 6,
                    "changes_on": "long_rest",
                    "selected_spell_ids": [],
                },
                "spellbook": {"enabled": True, "spell_ids": []},
            }
        )
        character = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Guild Initiate",
                "sheet": character_sheet,
                "idempotency_key": "background-character",
            },
        )
        applied = await _call(
            server,
            "character_content_apply",
            {
                "character_id": character["id"],
                "artifact_id": artifact["id"],
                "selection": {"equipment_package": "A"},
                "expected_revision": character["revision"],
                "idempotency_key": "background-apply",
            },
        )
        items = applied["sheet"]["inventory"]["items"]
        assert [item["name"] for item in items] == [
            "Identification Papers",
            "Guild Signet",
        ]
        assert applied["sheet"]["inventory"]["wallet"]["gp"] == 12
        assert applied["sheet"]["skills"]["investigation"]["proficiency"] == "proficient"
        assert applied["sheet"]["skills"]["persuasion"]["proficiency"] == "proficient"
        assert applied["sheet"]["progression"]["background_grants"][
            "equipment_item_ids"
        ] == [item["id"] for item in items]
        assert applied["sheet"]["progression"]["background_grants"][
            "spell_list_expansion"
        ] == [
            {
                "artifact_id": "dnd5e.content.srd2014.spell.aid",
                "name": "Aid",
                "pack_id": "dnd5e.content.srd2014",
                "pack_version": "1.20.0",
            }
        ]
        assert applied["rule_receipts"][0]["selection"] == {"equipment_package": "A"}
        spell = await _call(
            server,
            "character_content_apply",
            {
                "character_id": character["id"],
                "artifact_id": "dnd5e.content.srd2014.spell.aid",
                "selection": {"source_class": "Wizard", "method": "spellbook"},
                "expected_revision": applied["revision"],
                "idempotency_key": "background-expanded-spell",
            },
        )
        aid = next(
            item
            for item in spell["sheet"]["content"]["spells"]
            if item["id"] == "dnd5e.content.srd2014.spell.aid"
        )
        assert aid["grant"] == {
            "source_type": "class",
            "source_key": "wizard",
            "method": "spellbook",
        }

        marked_character = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Marked Wizard",
                "sheet": character_sheet,
                "idempotency_key": "species-character",
            },
        )
        marked = await _call(
            server,
            "character_content_apply",
            {
                "character_id": marked_character["id"],
                "artifact_id": species_artifact["id"],
                "selection": {},
                "expected_revision": marked_character["revision"],
                "idempotency_key": "species-apply",
            },
        )
        assert marked["sheet"]["progression"]["species_grants"][
            "spell_list_expansion"
        ] == [
            {
                "artifact_id": "dnd5e.content.srd2014.spell.aid",
                "name": "Aid",
                "pack_id": "dnd5e.content.srd2014",
                "pack_version": "1.20.0",
            }
        ]
        marked_spell = await _call(
            server,
            "character_content_apply",
            {
                "character_id": marked_character["id"],
                "artifact_id": "dnd5e.content.srd2014.spell.aid",
                "selection": {"source_class": "Wizard", "method": "spellbook"},
                "expected_revision": marked["revision"],
                "idempotency_key": "species-expanded-spell",
            },
        )
        assert any(
            item["id"] == "dnd5e.content.srd2014.spell.aid"
            for item in marked_spell["sheet"]["content"]["spells"]
        )

        druid_sheet = default_character_sheet()
        druid_sheet["progression"]["level"] = 3
        druid_sheet["progression"]["classes"] = [
            {"name": "Druid", "level": 3, "subclass": "", "hit_die": 8}
        ]
        druid_sheet["spellcasting"].update(
            {
                "ability": "wisdom",
                "class_lists": ["druid"],
                "spell_slots": {
                    "1": {
                        "label": "1st-level slots",
                        "value": 4,
                        "max": 4,
                        "recovers_on": "long_rest",
                        "source_key": "druid",
                    },
                    "2": {
                        "label": "2nd-level slots",
                        "value": 2,
                        "max": 2,
                        "recovers_on": "long_rest",
                        "source_key": "druid",
                    },
                },
                "preparation": {
                    "mode": "prepared",
                    "max_prepared": 6,
                    "changes_on": "long_rest",
                    "selected_spell_ids": [],
                },
            }
        )
        druid = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Spore Druid",
                "sheet": druid_sheet,
                "idempotency_key": "subclass-character",
            },
        )
        subclass_applied = await _call(
            server,
            "character_content_apply",
            {
                "character_id": druid["id"],
                "artifact_id": subclass_artifact["id"],
                "selection": {"target_class_name": "Druid"},
                "expected_revision": druid["revision"],
                "idempotency_key": "subclass-apply",
            },
        )
        subclass_spells = {
            item["name"]: item
            for item in subclass_applied["sheet"]["content"]["spells"]
        }
        assert subclass_spells["Chill Touch"]["grant"]["method"] == "known"
        assert subclass_spells["Chill Touch"]["access"]["known"] is True
        assert subclass_spells["Chill Touch"]["access"]["always_prepared"] is False
        assert subclass_spells["Blindness/Deafness"]["grant"]["method"] == (
            "class_prepared"
        )
        assert subclass_spells["Blindness/Deafness"]["access"][
            "always_prepared"
        ] is True

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
                    "tool_choice_count": 1,
                    "tool_options": ["smith's tools", "weaver's tools"],
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
                "selection": {
                    "skills": ["arcana", "investigation"],
                    "tools": ["smith's tools"],
                },
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
        assert applied["class_materialization"]["tool_proficiency_choices"] == [
            "smith's tools"
        ]
        assert applied["rule_receipts"][0]["mechanic_id"] == ("dnd5e.character.base_class.v1")
        assert applied["sheet"]["content"]["selections"][0]["selection"] == {
            "skills": ["arcana", "investigation"],
            "tools": ["smith's tools"],
        }

    import asyncio

    asyncio.run(exercise())
