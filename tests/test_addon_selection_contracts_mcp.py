from pathlib import Path

import pytest
from sagasmith_core.rule_packs import RulesetUnavailableError
from sagasmith_dnd.character_schema import default_character_sheet, derive_character_sheet
from sagasmith_dnd.content_validation import (
    build_catalog_review,
    build_selection_contract,
)
from sagasmith_dnd.statblocks import parameterized_statblock_requirements

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import (
    _append_selected_proficiencies,
    _apply_skill_proficiency_or_expertise,
    _character_spell_card,
    _feature_requirements_with_active_extensions,
    _materialize_feature_proficiency_groups,
    _reviewed_statblock_variants,
    _validate_group_limited_choices,
    _validated_additive_choices,
    _validated_narrative_choices,
    _validated_species_ability_choices,
    _validated_species_proficiency_choices,
    create_server,
)
from tests.authoring_helpers import import_and_activate_addon_fixture


def test_active_addon_options_extend_selector_without_granting_extra_choices() -> None:
    requirements = {
        "field": "options",
        "count": 1,
        "options": ["Armor of Shadows"],
        "option_prerequisites": {},
    }
    option = {
        "id": "dnd5e.addon.xanathar.feature.shroud-of-shadow",
        "kind": "feature",
        "card": {
            "name": "Shroud of Shadow",
            "class_name": "Warlock",
            "minimum_level": 15,
            "feature_subtype": "selectable_option",
            "extends_feature": {
                "name": "Eldritch Invocations",
                "class_name": "Warlock",
            },
            "at_will_spell": "Invisibility",
        },
    }

    extended = _feature_requirements_with_active_extensions(
        requirements,
        selector_card={"name": "Eldritch Invocations", "class_name": "Warlock"},
        candidates=[("dnd5e.addon.xanathar", "1.0.0", option)],
    )

    assert extended["count"] == 1
    assert extended["options"] == ["Armor of Shadows", "Shroud of Shadow"]
    assert extended["option_artifact_ids"] == {"Shroud of Shadow": option["id"]}
    assert extended["option_prerequisites"] == {"Shroud of Shadow": {"minimum_level": 15}}
    assert extended["at_will_spells"] == {"Shroud of Shadow": "Invisibility"}


def test_active_addon_option_rejects_conflicting_selector_identity() -> None:
    option = {
        "id": "dnd5e.addon.conflict.feature.armor-of-shadows",
        "kind": "feature",
        "card": {
            "name": "Armor of Shadows",
            "class_name": "Warlock",
            "feature_subtype": "selectable_option",
            "extends_feature": {
                "name": "Eldritch Invocations",
                "class_name": "Warlock",
            },
        },
    }

    with pytest.raises(RulesetUnavailableError, match="conflicts"):
        _feature_requirements_with_active_extensions(
            {
                "field": "options",
                "count": 1,
                "options": ["Armor of Shadows"],
                "option_artifact_ids": {
                    "Armor of Shadows": "dnd5e.srd2014.feature.armor-of-shadows"
                },
            },
            selector_card={
                "name": "Eldritch Invocations",
                "class_name": "Warlock",
            },
            candidates=[("dnd5e.addon.conflict", "1.0.0", option)],
        )


def test_reviewed_statblock_variants_require_full_distinct_matching_cards() -> None:
    source = """# Ox

*Large beast, unaligned*

**Armor Class** 10
**Hit Points** 15 (2d10 + 4)
**Speed** 30 ft.

| STR | DEX | CON | INT | WIS | CHA |
|---:|---:|---:|---:|---:|---:|
| 18 (+4) | 10 (+0) | 14 (+2) | 2 (-4) | 10 (+0) | 4 (-3) |

**Senses** passive Perception 10
**Languages** —
**Challenge** 1/4 (50 XP)

## Actions

***Gore.*** Melee Weapon Attack: +6 to hit, reach 5 ft., one target.
Hit: 7 (1d6 + 4) piercing damage.
"""
    card = {"statblock_variants": [{"name": "Ox", "normalized_content": source}]}

    assert _reviewed_statblock_variants(card) == [
        {"name": "Ox", "normalized_content": source.strip()}
    ]
    card["statblock_variants"][0]["name"] = "Rothé"
    with pytest.raises(ValueError, match="heading must match"):
        _reviewed_statblock_variants(card)


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


def test_background_group_limits_reject_two_choices_from_one_category() -> None:
    groups = [
        {"id": "gaming", "maximum": 1, "options": ["Dice", "Dragonchess"]},
        {"id": "instrument", "maximum": 1, "options": ["Lute"]},
    ]
    _validate_group_limited_choices(["Dice", "Lute"], groups=groups, label="background tool")
    with pytest.raises(ValueError, match="reviewed group limit: gaming"):
        _validate_group_limited_choices(
            ["Dice", "Dragonchess"], groups=groups, label="background tool"
        )


def test_selected_and_conditional_feature_proficiencies_mutate_exactly() -> None:
    languages = ["Common"]
    _append_selected_proficiencies(["Elvish"], target=languages, label="language")
    assert languages == ["Common", "Elvish"]
    with pytest.raises(ValueError, match="already proficient"):
        _append_selected_proficiencies(["elvish"], target=languages, label="language")

    sheet = default_character_sheet()
    _apply_skill_proficiency_or_expertise(sheet, ["Persuasion"])
    assert sheet["skills"]["persuasion"]["proficiency"] == "proficient"
    _apply_skill_proficiency_or_expertise(sheet, ["Persuasion"])
    assert sheet["skills"]["persuasion"]["proficiency"] == "expertise"

    selected = _materialize_feature_proficiency_groups(
        sheet,
        value={"languages": ["Elvish", "Goblin"], "gaming": ["Dice"]},
        groups=[
            {
                "id": "languages",
                "kind": "language",
                "count": 2,
                "options": [],
                "allow_unlisted": True,
            },
            {
                "id": "gaming",
                "kind": "tool",
                "count": 1,
                "options": ["Dice", "Dragonchess"],
            },
        ],
    )
    assert selected == {
        "languages": ["Elvish", "Goblin"],
        "gaming": ["Dice"],
    }
    assert sheet["traits"]["languages"] == ["Elvish", "Goblin"]
    assert sheet["traits"]["proficiencies"]["tools"] == ["Dice"]

    expertise_sheet = default_character_sheet()
    expertise = _materialize_feature_proficiency_groups(
        expertise_sheet,
        value={"skill": ["Stealth"], "expertise": ["Stealth"]},
        groups=[
            {
                "id": "skill",
                "kind": "skill",
                "count": 1,
                "options": ["Stealth"],
            },
            {
                "id": "expertise",
                "kind": "skill_expertise",
                "count": 1,
                "options": [],
                "allow_unlisted": True,
            },
        ],
    )
    assert expertise == {"skill": ["Stealth"], "expertise": ["Stealth"]}
    assert expertise_sheet["skills"]["stealth"]["proficiency"] == "expertise"
    with pytest.raises(ValueError, match="expertise requires proficiency"):
        _materialize_feature_proficiency_groups(
            default_character_sheet(),
            value={"expertise": ["Arcana"]},
            groups=[
                {
                    "id": "expertise",
                    "kind": "skill_expertise",
                    "count": 1,
                    "options": [],
                    "allow_unlisted": True,
                }
            ],
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
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "set_profile",
                "payload": {"edition": "2014"},
                "principal_id": "system:local",
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
                "prerequisites": [{"kind": "feature_forbidden", "feature": "dragonmark"}],
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
        await import_and_activate_addon_fixture(
            _call,
            server,
            campaign["id"],
            config.home,
            manifest={
                "id": "dnd5e.addon.eberron",
                "version": "1.0.0",
                "title": "Reviewed Eberron",
                "namespace": "dnd5e.addon.eberron",
                "system_id": "dnd5e",
                "editions": ["2014"],
                "capabilities": [],
            },
            artifacts=[artifact],
            mechanics=[],
            expected_revision=profile["campaign_revision"],
            request_key="addon-feat",
        )
        sheet = default_character_sheet()
        sheet["abilities"]["constitution"]["score"] = 10
        character = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Marked Tester", "sheet": sheet},
                "principal_id": "system:local",
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
                        "level_1_spell": ["dnd5e.content.srd2014.spell.burning-hands"],
                    }
                },
                "expected_revision": character["revision"],
                "idempotency_key": "addon-feat-apply",
            },
        )

        assert applied["sheet"]["abilities"]["constitution"]["score"] == 11
        spells = {item["id"]: item for item in applied["sheet"]["content"]["spells"]}
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
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "set_profile",
                "payload": {"edition": "2014"},
                "principal_id": "system:local",
                "expected_revision": campaign["revision"],
                "idempotency_key": "addon-actor-profile",
            },
        )
        await import_and_activate_addon_fixture(
            _call,
            server,
            campaign["id"],
            config.home,
            manifest={
                "id": "dnd5e.addon.defender",
                "version": "1.0.0",
                "title": "Reviewed Defender",
                "namespace": "dnd5e.addon.defender",
                "system_id": "dnd5e",
                "editions": ["2014"],
                "capabilities": [],
            },
            artifacts=[artifact],
            mechanics=[],
            expected_revision=profile["campaign_revision"],
            request_key="addon-actor",
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
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Owner", "sheet": owner_sheet},
                "principal_id": "system:local",
                "idempotency_key": "addon-actor-owner",
            },
        )
        catalog = await _call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "statblock",
                    "query": artifact["id"],
                },
                "principal_id": "system:local",
            },
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
        second_owner = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Second Owner",
                    "sheet": owner_sheet,
                },
                "principal_id": "system:local",
                "idempotency_key": "addon-actor-second-owner",
            },
        )
        second_created = await _call(
            server,
            "addon_actor_instantiate",
            {
                "campaign_id": campaign["id"],
                "artifact_id": artifact["id"],
                "owner_character_id": second_owner["id"],
                "idempotency_key": "addon-actor-second-create",
            },
        )

        assert created["character"]["id"] == replay["character"]["id"]
        assert created["character"]["name"] == "Steel Defender (Owner)"
        assert second_created["character"]["name"] == "Steel Defender (Second Owner)"
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

        current = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        phase = await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": current["revision"],
                "idempotency_key": "addon-actor-play",
            },
        )
        started = await _call(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "positioning_mode": "agent",
                "participant_ids": [owner["id"]],
                "expected_revision": phase["campaign_revision"],
                "idempotency_key": "addon-actor-combat",
            },
        )
        combat_arguments = {
            "campaign_id": campaign["id"],
            "artifact_id": artifact["id"],
            "owner_character_id": owner["id"],
            "name": "Combat Steel Defender",
            "participant_config": {
                "initiative": 12,
                "tie_breaker": 1,
                "disposition": "friendly",
            },
            "expected_revision": started["campaign_revision"],
            "idempotency_key": "addon-actor-combat-create",
        }
        combat_created = await _call(server, "addon_actor_instantiate", combat_arguments)
        combat_replay = await _call(server, "addon_actor_instantiate", combat_arguments)
        for field, value in combat_created["character"].items():
            assert combat_replay["character"][field] == value, field
        assert {**combat_replay, "character": {}} == {**combat_created, "character": {}}
        combat_actor_id = combat_created["character"]["id"]
        assert combat_actor_id in {
            item["actor_id"]
            for item in combat_created["combat"]["combat"]["reinforcements"]
        }

        history = await _call(
            server,
            "state_revision",
            {
                "campaign_id": campaign["id"],
                "action": "history",
                "payload": {},
                "principal_id": "system:local",
            },
        )
        await _call(
            server,
            "state_revision",
            {
                "campaign_id": campaign["id"],
                "action": "undo",
                "payload": {"expected_history_sequence": history[0]["sequence"]},
                "principal_id": "system:local",
                "idempotency_key": "undo-addon-actor-combat-create",
            },
        )
        with pytest.raises(Exception, match=combat_actor_id):
            await _call(
                server,
                "character_query",
                {
                    "view": "get",
                    "payload": {"character_id": combat_actor_id},
                    "principal_id": "system:local",
                },
            )
        undone_combat = await _call(
            server,
            "combat_query",
            {"campaign_id": campaign["id"], "view": "status"},
        )
        assert combat_actor_id not in {
            item["actor_id"] for item in undone_combat["reinforcements"]
        }
        undone_history = await _call(
            server,
            "state_revision",
            {
                "campaign_id": campaign["id"],
                "action": "history",
                "payload": {},
                "principal_id": "system:local",
            },
        )
        redo_cursor = next(
            item["sequence"] for item in undone_history if item["applied"]
        )
        await _call(
            server,
            "state_revision",
            {
                "campaign_id": campaign["id"],
                "action": "redo",
                "payload": {"expected_history_sequence": redo_cursor},
                "principal_id": "system:local",
                "idempotency_key": "redo-addon-actor-combat-create",
            },
        )
        restored_actor = await _call(
            server,
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": combat_actor_id},
                "principal_id": "system:local",
            },
        )
        restored_combat = await _call(
            server,
            "combat_query",
            {"campaign_id": campaign["id"], "view": "status"},
        )
        assert restored_actor["id"] == combat_actor_id
        assert combat_actor_id in {
            item["actor_id"] for item in restored_combat["reinforcements"]
        }

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
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "set_profile",
                "payload": {"edition": "2014"},
                "principal_id": "system:local",
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
        await import_and_activate_addon_fixture(
            _call,
            server,
            campaign["id"],
            config.home,
            manifest={
                "id": "dnd5e.addon.reviewed-item",
                "version": "1.0.0",
                "title": "Reviewed item",
                "namespace": "dnd5e.addon.reviewed-item",
                "system_id": "dnd5e",
                "editions": ["2014"],
                "capabilities": [],
            },
            artifacts=[artifact],
            mechanics=[],
            expected_revision=profile["campaign_revision"],
            request_key="addon-item",
        )
        character = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Item Tester",
                    "sheet": default_character_sheet(),
                },
                "principal_id": "system:local",
                "idempotency_key": "addon-item-character",
            },
        )

        with pytest.raises(Exception, match="item content selection does not accept input fields"):
            await _call(
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
        queried = await _call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "item",
                    "query": artifact["id"],
                    "include_context": True,
                },
                "principal_id": "system:local",
            },
        )
        assert (
            queried[0]["runtime_context"]["content_hash"]
            == (applied["content_context"]["content_hash"])
        )
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
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "set_profile",
                "payload": {"edition": "2014"},
                "principal_id": "system:local",
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
                "skill_proficiencies": ["investigation"],
                "background_grants": {
                    "skills": ["investigation"],
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
                        "skill_choice_count": 1,
                        "skill_options": ["Persuasion", "Religion"],
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
        reprinted_aid = {
            "id": "dnd5e.addon.guild.spell.aid",
            "kind": "spell",
            "application_state": "selection_ready",
            "mechanical_scope": "descriptive",
            "execution_state": "descriptive_ready",
            "semantic_resolution": {
                "status": "resolved",
                "mode": "descriptive",
                "first_use_compilation_required": False,
            },
            "card": {
                "name": "Aid",
                "classes": ["Cleric", "Paladin"],
                "level": 2,
                "description": "Catalog retrieval text must not enter a character card.",
                "source_title": "Guild addon source",
                "definition": {
                    "school": "abjuration",
                    "casting_time": "1 action",
                    "range": {
                        "kind": "distance",
                        "normal_ft": 30,
                        "long_ft": 0,
                        "area": "",
                    },
                    "duration": {
                        "kind": "timed",
                        "value": 8,
                        "unit": "hour",
                        "concentration": False,
                    },
                    "components": {
                        "verbal": True,
                        "somatic": True,
                        "material": True,
                        "material_description": "a tiny strip of white cloth",
                        "material_cost_cp": 0,
                        "consumed": False,
                    },
                    "effect": "Source-local reviewed reprint used by this addon.",
                },
            },
            "rule_refs": ["book:addon:p3"],
        }
        reprinted_aid["selection_contract"] = build_selection_contract(
            reprinted_aid,
            status="ready",
            references=["book:addon:p3"],
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
                    "ability_score_decreases": {"strength": 2},
                    "size": "medium",
                    "walk_speed": 30,
                    "fly_speed": 30,
                    "natural_armor_base": 13,
                    "natural_armor_includes_dexterity": False,
                    "natural_weapons": [
                        {
                            "name": "Marked Claws",
                            "attack_ability": "strength",
                            "damage_formula": "1d4",
                            "damage_type": "slashing",
                            "reach_ft": 5,
                            "description": "A reviewed natural weapon.",
                        }
                    ],
                    "languages": ["Common"],
                    "armor_proficiencies": ["Light Armor"],
                    "immunities": ["poison"],
                    "condition_immunities": ["poisoned"],
                    "feat_choice": {"count": 1, "allowed_categories": []},
                    "spell_list_expansion": ["Aid"],
                    "spell_grants": [
                        {
                            "name": "Detect Magic",
                            "level": 1,
                            "eligible_classes": ["Wizard"],
                            "method": "limited_use",
                            "spellcasting_ability": "intelligence",
                            "free_casts": 1,
                            "recovers_on": "long_rest",
                            "resource_group": "Marked Magic",
                            "allow_slot_cast": False,
                            "minimum_level": 1,
                            "ritual_only": False,
                            "casting_overrides": {"ignore_material_components": True},
                        },
                        {
                            "name": "Nondetection",
                            "level": 3,
                            "eligible_classes": ["Wizard"],
                            "method": "at_will",
                            "spellcasting_ability": "intelligence",
                            "free_casts": 0,
                            "recovers_on": None,
                            "allow_slot_cast": False,
                            "minimum_level": 1,
                            "ritual_only": False,
                            "casting_overrides": {"ignore_material_components": True},
                        },
                        {
                            "name": "Burning Hands",
                            "level": 1,
                            "eligible_classes": ["Wizard"],
                            "method": "limited_use",
                            "spellcasting_ability": "charisma",
                            "free_casts": 1,
                            "recovers_on": "long_rest",
                            "resource_group": "Marked Magic",
                            "allow_slot_cast": False,
                            "minimum_level": 3,
                            "ritual_only": False,
                            "casting_overrides": {"fixed_cast_level": 2},
                        },
                    ],
                    "resources": {
                        "species:marked-human:insight": {
                            "label": "Marked Insight",
                            "value": 1,
                            "max": 1,
                            "recovers_on": "short_rest",
                            "source_key": "Marked Human",
                        }
                    },
                    "features": [
                        {
                            "name": "Marked Awareness",
                            "description": "A source-reviewed level-one species feature.",
                            "minimum_level": 1,
                        },
                        {
                            "name": "Greater Mark",
                            "description": "A source-reviewed higher-level species feature.",
                            "minimum_level": 4,
                        },
                    ],
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
        species_feat_artifact = {
            "id": "dnd5e.addon.guild.feat.marked-training",
            "kind": "feat",
            "application_state": "selection_ready",
            "mechanical_scope": "mechanical",
            "execution_state": "engine_ready",
            "semantic_resolution": {
                "status": "resolved",
                "mode": "static_grant",
                "first_use_compilation_required": False,
                "clause_ids": ["marked-training-language"],
            },
            "rule_clauses": [
                {
                    "schema_version": 1,
                    "id": "marked-training-language",
                    "title": "Marked Training language",
                    "scope": "mechanical",
                    "source_citations": [
                        {
                            "source": "book:addon",
                            "source_ref": {"page": 3},
                            "source_excerpt": "Marked Training grants Dwarvish.",
                        }
                    ],
                    "settlement": {
                        "mode": "static_grant",
                        "grant_refs": ["card.mechanical_grants.languages"],
                    },
                }
            ],
            "card": {
                "name": "Marked Training",
                "prerequisites": [],
                "repeatable": False,
                "selection_requirements": None,
                "mechanical_grants": {
                    "ability_score_increases": {},
                    "maximum_ability_score": 20,
                    "languages": ["Dwarvish"],
                    "tool_proficiencies": [],
                    "weapon_proficiencies": [],
                    "spell_grants": [],
                },
            },
            "rule_refs": ["book:addon:p3"],
        }
        species_feat_artifact["selection_contract"] = build_selection_contract(
            species_feat_artifact,
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
                            "card.spell_grants",
                            "card.spell_list_expansion",
                        ],
                    },
                }
            ],
            "card": {
                "name": "Circle of Spores",
                "class_name": "Druid",
                "minimum_level": 2,
                "spell_grants": [
                    {
                        "name": "Blindness/Deafness",
                        "minimum_level": 3,
                        "method": "always_prepared",
                    },
                    {"name": "Chill Touch", "minimum_level": 2, "method": "known"},
                ],
                "spell_list_expansion": ["Aid"],
            },
            "rule_refs": ["book:addon:p2"],
        }
        subclass_artifact["selection_contract"] = build_selection_contract(
            subclass_artifact,
            status="ready",
            references=["book:addon:p2"],
        )
        await import_and_activate_addon_fixture(
            _call,
            server,
            campaign["id"],
            config.home,
            manifest={
                "id": "dnd5e.addon.guild",
                "version": "1.0.0",
                "title": "Guild addon",
                "namespace": "dnd5e.addon.guild",
                "system_id": "dnd5e",
                "editions": ["2014"],
                "capabilities": [],
            },
            artifacts=[
                artifact,
                reprinted_aid,
                species_artifact,
                species_feat_artifact,
                subclass_artifact,
            ],
            mechanics=[],
            expected_revision=profile["campaign_revision"],
            request_key="background",
        )
        catalog = await _call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "background",
                    "query": "Guild Agent",
                },
                "principal_id": "system:local",
            },
        )
        background_entry = next(item for item in catalog if item["id"] == artifact["id"])
        assert background_entry["selection_requirements"]["fields"] == [
            "skills",
            "equipment_package",
        ]
        assert background_entry["selection_requirements"]["skill_choice_count"] == 1
        assert background_entry["selection_requirements"]["skill_options"] == [
            "Persuasion",
            "Religion",
        ]
        species_catalog = await _call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "species",
                    "query": "Marked Human",
                },
                "principal_id": "system:local",
            },
        )
        marked_entry = next(
            item for item in species_catalog if item["id"] == species_artifact["id"]
        )
        assert "feat_selection" in marked_entry["selection_requirements"]["fields"]
        assert marked_entry["selection_requirements"]["feat_choice"] == {
            "count": 1,
            "allowed_categories": [],
        }
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
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Guild Initiate",
                    "sheet": character_sheet,
                },
                "principal_id": "system:local",
                "idempotency_key": "background-character",
            },
        )
        applied = await _call(
            server,
            "character_content_apply",
            {
                "character_id": character["id"],
                "artifact_id": artifact["id"],
                "selection": {
                    "skills": ["persuasion"],
                    "equipment_package": "A",
                },
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
        assert applied["sheet"]["progression"]["background_grants"]["equipment_item_ids"] == [
            item["id"] for item in items
        ]
        assert applied["sheet"]["progression"]["background_grants"]["spell_list_expansion"] == [
            {
                "artifact_id": "dnd5e.addon.guild.spell.aid",
                "name": "Aid",
                "pack_id": "dnd5e.addon.guild",
                "pack_version": "1.0.0",
            }
        ]
        assert applied["sheet"]["progression"]["background_grants"]["choices"][
            "selected_skill_choices"
        ] == ["persuasion"]
        spell = await _call(
            server,
            "character_content_apply",
            {
                "character_id": character["id"],
                "artifact_id": "dnd5e.addon.guild.spell.aid",
                "selection": {"source_class": "Wizard", "method": "spellbook"},
                "expected_revision": applied["revision"],
                "idempotency_key": "background-expanded-spell",
            },
        )
        aid = next(
            item
            for item in spell["sheet"]["content"]["spells"]
            if item["id"] == "dnd5e.addon.guild.spell.aid"
        )
        assert aid["grant"] == {
            "source_type": "class",
            "source_key": "wizard",
            "method": "spellbook",
        }
        assert "description" not in aid
        assert "source_title" not in aid

        marked_character = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Marked Wizard",
                    "sheet": character_sheet,
                },
                "principal_id": "system:local",
                "idempotency_key": "species-character",
            },
        )
        marked = await _call(
            server,
            "character_content_apply",
            {
                "character_id": marked_character["id"],
                "artifact_id": species_artifact["id"],
                "selection": {
                    "feat_selection": {
                        "artifact_id": species_feat_artifact["id"],
                        "selection": {},
                    }
                },
                "expected_revision": marked_character["revision"],
                "idempotency_key": "species-apply",
            },
        )
        assert marked["sheet"]["progression"]["species_grants"]["spell_list_expansion"] == [
            {
                "artifact_id": "dnd5e.addon.guild.spell.aid",
                "name": "Aid",
                "pack_id": "dnd5e.addon.guild",
                "pack_version": "1.0.0",
            }
        ]
        assert marked["sheet"]["traits"]["proficiencies"]["armor"] == ["Light Armor"]
        assert marked["sheet"]["combat"]["speed"]["fly"] == 30
        assert marked["sheet"]["abilities"]["strength"]["score"] == 8
        assert marked["sheet"]["abilities"]["intelligence"]["score"] == 11
        assert marked["sheet"]["traits"]["immunities"] == ["poison"]
        assert marked["sheet"]["traits"]["condition_immunities"] == ["poisoned"]
        assert "Dwarvish" in marked["sheet"]["traits"]["languages"]
        assert any(
            feat["id"] == species_feat_artifact["id"]
            and feat["choices"]["grant_source"] == "Marked Human species"
            for feat in marked["sheet"]["content"]["feats"]
        )
        assert derive_character_sheet(marked["sheet"])["armor_class"] == 13
        natural_armor = next(
            effect
            for effect in marked["sheet"]["effects"]
            if effect["name"].endswith("Natural Armor")
        )
        assert natural_armor["changes"] == [
            {
                "path": "combat.ac.unarmored_formula",
                "mode": "override",
                "value": {
                    "base": 13,
                    "ability": None,
                    "allows_shield": True,
                    "includes_dexterity": False,
                },
            }
        ]
        natural_weapon = next(
            item for item in marked["sheet"]["inventory"]["items"] if item["name"] == "Marked Claws"
        )
        assert natural_weapon["mechanics"]["damage_formula"] == "1d4"
        assert natural_weapon["mechanics"]["always_available"] is True
        detect_magic = next(
            item for item in marked["sheet"]["content"]["spells"] if item["name"] == "Detect Magic"
        )
        assert detect_magic["access"]["feature_casting_sources"][0]["casting_overrides"] == {
            "ignore_material_components": True
        }
        nondetection = next(
            item for item in marked["sheet"]["content"]["spells"] if item["name"] == "Nondetection"
        )
        assert nondetection["access"]["at_will"] is True
        assert nondetection["access"]["feature_casting_sources"][0]["method"] == ("at_will")
        burning_hands = next(
            item for item in marked["sheet"]["content"]["spells"] if item["name"] == "Burning Hands"
        )
        assert burning_hands["access"]["feature_casting_sources"][0]["casting_overrides"] == {
            "fixed_cast_level": 2
        }
        assert (
            detect_magic["access"]["feature_casting_sources"][0]["resource_key"]
            == burning_hands["access"]["feature_casting_sources"][0]["resource_key"]
        )
        assert marked["sheet"]["resources"]["species:marked-human:insight"]["value"] == 1
        assert [item["name"] for item in marked["sheet"]["content"]["features"]] == [
            "Marked Awareness"
        ]
        marked_awareness = marked["sheet"]["content"]["features"][0]
        assert marked_awareness["id"].endswith(".feature.marked-awareness")
        assert marked_awareness["source_key"] == "Marked Human"
        assert "minimum_level" not in marked_awareness
        marked_spell = await _call(
            server,
            "character_content_apply",
            {
                "character_id": marked_character["id"],
                "artifact_id": "dnd5e.addon.guild.spell.aid",
                "selection": {"source_class": "Wizard", "method": "spellbook"},
                "expected_revision": marked["revision"],
                "idempotency_key": "species-expanded-spell",
            },
        )
        assert any(
            item["id"] == "dnd5e.addon.guild.spell.aid"
            for item in marked_spell["sheet"]["content"]["spells"]
        )
        marked_level_four = await _call(
            server,
            "character_state_change",
            {
                "character_id": marked_character["id"],
                "action": "level_advance",
                "payload": {
                    "class_name": "Wizard",
                    "hp_method": "fixed",
                    "reason": "unlock the reviewed level-four species feature",
                    "source_ref": "book:addon:p3",
                },
                "expected_revision": marked_spell["revision"],
                "idempotency_key": "species-level-four",
            },
        )
        advanced_marked = marked_level_four["character"]
        assert [item["name"] for item in advanced_marked["sheet"]["content"]["features"]] == [
            "Marked Awareness",
            "Greater Mark",
        ]
        assert marked_level_four["advancement"]["species_feature_grants"] == [
            {
                "artifact_id": ("dnd5e.addon.guild.species.marked-human.feature.greater-mark"),
                "name": "Greater Mark",
                "minimum_level": 4,
                "source_species": "Marked Human",
            }
        ]

        dragonborn_catalog = await _call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "species",
                    "query": "Dragonborn",
                },
                "principal_id": "system:local",
            },
        )
        dragonborn_id = "dnd5e.content.standard2014.species.dragonborn"
        dragonborn_entry = next(item for item in dragonborn_catalog if item["id"] == dragonborn_id)
        assert dragonborn_entry["selection_requirements"]["fields"] == ["damage_affinity"]
        dragonborn_character = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Red Dragonborn",
                    "sheet": character_sheet,
                },
                "principal_id": "system:local",
                "idempotency_key": "dragonborn-character",
            },
        )
        dragonborn_applied = await _call(
            server,
            "character_content_apply",
            {
                "character_id": dragonborn_character["id"],
                "artifact_id": dragonborn_id,
                "selection": {"damage_affinity": "red"},
                "expected_revision": dragonborn_character["revision"],
                "idempotency_key": "dragonborn-species-apply",
            },
        )
        assert "fire" in dragonborn_applied["sheet"]["traits"]["resistances"]
        breath = next(
            item
            for item in dragonborn_applied["sheet"]["content"]["activities"]
            if item["name"] == "Breath Weapon"
        )
        breath_spec = breath["choices"]["standard_resolution"]
        assert breath_spec["kind"] == "area_save_damage"
        assert breath["mechanic_refs"] == ["dnd5e.core.activity.dragonborn_breath_weapon"]
        assert breath_spec["save_dc_formula"] == {
            "base": 8,
            "ability": "constitution",
            "include_proficiency": True,
        }
        assert breath_spec["damage_formula_by_level"]["1"] == "2d6"

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
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Spore Druid",
                    "sheet": druid_sheet,
                },
                "principal_id": "system:local",
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
            item["name"]: item for item in subclass_applied["sheet"]["content"]["spells"]
        }
        assert subclass_spells["Chill Touch"]["grant"]["method"] == "known"
        assert subclass_spells["Chill Touch"]["access"]["known"] is True
        assert subclass_spells["Chill Touch"]["access"]["always_prepared"] is False
        assert subclass_spells["Blindness/Deafness"]["grant"]["method"] == ("class_prepared")
        assert subclass_spells["Blindness/Deafness"]["access"]["always_prepared"] is True
        assert "Aid" not in subclass_spells
        assert subclass_applied["sheet"]["progression"]["subclass_grants"][
            "spell_list_expansion"
        ] == [
            {
                "artifact_id": "dnd5e.addon.guild.spell.aid",
                "name": "Aid",
                "pack_id": "dnd5e.addon.guild",
                "pack_version": "1.0.0",
                "source_class": "Druid",
            }
        ]
        expanded_spell = await _call(
            server,
            "character_content_apply",
            {
                "character_id": druid["id"],
                "artifact_id": "dnd5e.addon.guild.spell.aid",
                "selection": {
                    "source_class": "Druid",
                    "method": "class_prepared",
                },
                "expected_revision": subclass_applied["revision"],
                "idempotency_key": "subclass-expanded-spell",
            },
        )
        expanded_aid = next(
            item
            for item in expanded_spell["sheet"]["content"]["spells"]
            if item["id"] == "dnd5e.addon.guild.spell.aid"
        )
        assert expanded_aid["grant"] == {
            "source_type": "class",
            "source_key": "druid",
            "method": "class_prepared",
        }

    import asyncio

    asyncio.run(exercise())


@pytest.mark.fresh_database
def test_subclass_spell_prefers_exact_reviewed_dependency_over_bundled_duplicate(
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

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Pinned dependency spell",
                "idempotency_key": "dependency-spell-campaign",
            },
        )
        profile = await _call(
            server,
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "set_profile",
                "payload": {"edition": "2014"},
                "principal_id": "system:local",
                "expected_revision": campaign["revision"],
                "idempotency_key": "dependency-spell-profile",
            },
        )
        dependency_spell = {
            "id": "dnd5e.addon.reviewed-dependency.spell.identify",
            "kind": "spell",
            "application_state": "selection_ready",
            "mechanical_scope": "descriptive",
            "execution_state": "descriptive_ready",
            "semantic_resolution": {
                "status": "resolved",
                "mode": "descriptive",
                "first_use_compilation_required": False,
            },
            "card": {
                "name": "Identify",
                "classes": ["Bard", "Wizard"],
                "level": 1,
                "definition": {
                    "school": "divination",
                    "casting_time": "1 minute",
                    "range": {
                        "kind": "touch",
                        "normal_ft": 0,
                        "long_ft": 0,
                        "area": "",
                    },
                    "duration": {
                        "kind": "instantaneous",
                        "value": 0,
                        "unit": "special",
                        "concentration": False,
                    },
                    "components": {
                        "verbal": True,
                        "somatic": True,
                        "material": True,
                        "material_description": "a reviewed dependency component",
                        "material_cost_cp": 10000,
                        "consumed": False,
                    },
                    "effect": "The dependency's reviewed Identify printing.",
                },
            },
            "rule_refs": ["book:dependency:p1"],
        }
        dependency_spell["selection_contract"] = build_selection_contract(
            dependency_spell,
            status="ready",
            references=["book:dependency:p1"],
        )
        dependency_pack = await import_and_activate_addon_fixture(
            _call,
            server,
            campaign["id"],
            config.home,
            manifest={
                "id": "dnd5e.addon.reviewed-dependency",
                "version": "1.0.0",
                "title": "Reviewed dependency",
                "namespace": "dnd5e.addon.reviewed-dependency",
                "system_id": "dnd5e",
                "editions": ["2014"],
                "capabilities": [],
            },
            artifacts=[dependency_spell],
            mechanics=[],
            expected_revision=profile["campaign_revision"],
            request_key="dependency-spell",
        )

        subclass = {
            "id": "dnd5e.addon.dependent-domain.subclass.dependency-domain",
            "kind": "subclass",
            "application_state": "selection_ready",
            "mechanical_scope": "mechanical",
            "execution_state": "engine_ready",
            "semantic_resolution": {
                "status": "resolved",
                "mode": "static_grant",
                "first_use_compilation_required": False,
                "clause_ids": ["dependency-domain-spells"],
            },
            "rule_clauses": [
                {
                    "schema_version": 1,
                    "id": "dependency-domain-spells",
                    "title": "Dependency Domain spells",
                    "scope": "mechanical",
                    "source_citations": [
                        {
                            "source": "book:dependent-domain",
                            "source_ref": {"page": 2},
                            "source_excerpt": "Identify is always prepared.",
                        }
                    ],
                    "settlement": {
                        "mode": "static_grant",
                        "grant_refs": ["card.spell_grants"],
                    },
                }
            ],
            "card": {
                "name": "Dependency Domain",
                "class_name": "Cleric",
                "minimum_level": 1,
                "spell_grants": [
                    {"name": "Identify", "minimum_level": 1, "method": "always_prepared"}
                ],
            },
            "rule_refs": ["book:dependent-domain:p2"],
        }
        subclass["selection_contract"] = build_selection_contract(
            subclass,
            status="ready",
            references=["book:dependent-domain:p2"],
        )
        await import_and_activate_addon_fixture(
            _call,
            server,
            campaign["id"],
            config.home,
            manifest={
                "id": "dnd5e.addon.dependent-domain",
                "version": "1.0.0",
                "title": "Dependent domain",
                "namespace": "dnd5e.addon.dependent-domain",
                "system_id": "dnd5e",
                "editions": ["2014"],
                "capabilities": [],
                "dependencies": [
                    {
                        "id": "dnd5e.addon.reviewed-dependency",
                        "version": "1.0.0",
                        "checksum": dependency_pack["package"]["checksum"],
                        "rule_checksum": dependency_pack["imported"]["components"][0][
                            "checksum"
                        ],
                    }
                ],
            },
            artifacts=[subclass],
            mechanics=[],
            expected_revision=dependency_pack["campaign_revision"],
            request_key="dependent-domain",
        )
        identify_catalog = await _call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "spell",
                    "query": "Identify",
                },
            },
        )
        assert identify_catalog, "the active dependency must expose Identify"

        sheet = default_character_sheet()
        sheet["progression"]["level"] = 1
        sheet["progression"]["classes"] = [
            {"name": "Cleric", "level": 1, "subclass": "", "hit_die": 8}
        ]
        sheet["spellcasting"].update(
            {
                "ability": "wisdom",
                "class_lists": ["cleric"],
                "preparation": {
                    "mode": "prepared",
                    "max_prepared": 4,
                    "changes_on": "long_rest",
                    "selected_spell_ids": [],
                },
            }
        )
        character = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Dependency Cleric",
                    "sheet": sheet,
                },
                "principal_id": "system:local",
                "idempotency_key": "dependency-cleric-create",
            },
        )
        applied = await _call(
            server,
            "character_content_apply",
            {
                "character_id": character["id"],
                "artifact_id": subclass["id"],
                "selection": {"target_class_name": "Cleric"},
                "expected_revision": character["revision"],
                "idempotency_key": "dependency-domain-apply",
            },
        )
        assert "sheet" in applied, str(applied)

        identify = next(
            item for item in applied["sheet"]["content"]["spells"] if item["name"] == "Identify"
        )
        assert identify["pack_id"] == "dnd5e.addon.reviewed-dependency"
        assert identify["pack_version"] == "1.0.0"
        assert identify["definition"]["effect"] == ("The dependency's reviewed Identify printing.")

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
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "set_profile",
                "payload": {"edition": "2014"},
                "principal_id": "system:local",
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
                    "tool_proficiencies": [
                        "thieves' tools",
                        "tinker's tools",
                        "Alchemist's Supplies",
                    ],
                    "tool_choice_count": 1,
                    "tool_options": ["smith's tools", "weaver's tools"],
                    "skill_choice_count": 2,
                    "skill_options": ["arcana", "history", "investigation", "medicine"],
                    "spellcasting": {
                        "ability": "intelligence",
                        "class_list": "artificer",
                        "preparation_mode": "prepared",
                        "slot_progression": "half_round_up",
                        "ritual_casting": True,
                        "spellbook": False,
                        "cantrips_known_by_level": [
                            2,
                            2,
                            2,
                            2,
                            2,
                            2,
                            2,
                            2,
                            2,
                            3,
                            3,
                            3,
                            3,
                            4,
                            4,
                            4,
                            4,
                            4,
                            4,
                            4,
                        ],
                        "leveled_spells_known_by_level": [],
                        "prepared_limit": {
                            "ability": "intelligence",
                            "class_level_divisor": 2,
                            "rounding": "down",
                            "minimum": 1,
                        },
                        "spell_list_expansion": ["Cure Wounds", "Magic Weapon"],
                    },
                },
            },
            "rule_refs": ["book:addon:artificer:p2"],
        }
        artifact["selection_contract"] = build_selection_contract(
            artifact,
            status="ready",
            references=["book:addon:artificer:p2"],
        )
        tool_feature = {
            "id": "dnd5e.addon.artificer.feature.tool-expertise",
            "kind": "feature",
            "application_state": "selection_ready",
            "mechanical_scope": "mechanical",
            "execution_state": "engine_ready",
            "semantic_resolution": {
                "status": "resolved",
                "mode": "static_grant",
                "first_use_compilation_required": False,
                "clause_ids": ["artificer-tool-proficiencies"],
            },
            "rule_clauses": [
                {
                    "schema_version": 1,
                    "id": "artificer-tool-proficiencies",
                    "title": "Artificer tool proficiencies",
                    "scope": "mechanical",
                    "source_citations": [
                        {
                            "source": "book:addon:artificer",
                            "source_ref": {"page": 3},
                            "source_excerpt": "Tool Expertise doubles tool proficiency.",
                        }
                    ],
                    "settlement": {
                        "mode": "static_grant",
                        "grant_refs": ["card.mechanical_grants"],
                    },
                }
            ],
            "card": {
                "name": "Tool Expertise",
                "class_name": "Artificer",
                "subclass_name": "",
                "feature_subtype": "",
                "minimum_level": 1,
                "repeatable_selection_levels": [],
                "selection_requirements": {},
                "selection_requirements_by_level": {},
                "mechanical_grants": {
                    "tool_expertise_all": True,
                    "tool_proficiencies": ["Alchemist's Supplies"],
                    "tool_proficiency_replacement_options": {
                        "Alchemist's Supplies": [
                            "Smith's Tools",
                            "Weaver's Tools",
                        ]
                    },
                    "weapon_proficiencies": ["Martial Weapons"],
                    "skill_proficiencies": ["History"],
                },
            },
            "rule_refs": ["book:addon:artificer:p3"],
        }
        tool_feature["selection_contract"] = build_selection_contract(
            tool_feature,
            status="ready",
            references=["book:addon:artificer:p3"],
        )
        infusion_option = {
            "id": "dnd5e.addon.artificer.feature.enhanced-defense",
            "kind": "feature",
            "application_state": "selection_ready",
            "mechanical_scope": "descriptive",
            "execution_state": "descriptive_ready",
            "semantic_resolution": {
                "status": "resolved",
                "mode": "descriptive",
                "first_use_compilation_required": False,
            },
            "card": {
                "name": "Enhanced Defense",
                "class_name": "Artificer",
                "subclass_name": "",
                "feature_subtype": "selectable_option",
                "minimum_level": 1,
                "description": "A learned artificer infusion.",
                "selection_requirements": {},
                "selection_requirements_by_level": {},
                "mechanical_grants": {},
            },
            "rule_refs": ["book:addon:artificer:p4"],
        }
        infusion_option["selection_contract"] = build_selection_contract(
            infusion_option,
            status="ready",
            references=["book:addon:artificer:p4"],
        )
        infusion_feature = {
            "id": "dnd5e.addon.artificer.feature.infuse-item",
            "kind": "feature",
            "application_state": "selection_ready",
            "mechanical_scope": "descriptive",
            "execution_state": "descriptive_ready",
            "semantic_resolution": {
                "status": "resolved",
                "mode": "descriptive",
                "first_use_compilation_required": False,
            },
            "card": {
                "name": "Infuse Item",
                "class_name": "Artificer",
                "subclass_name": "",
                "minimum_level": 1,
                "selection_requirements": {
                    "field": "infusions",
                    "kind": "feature_grants",
                    "count": 1,
                    "options": ["Enhanced Defense"],
                    "option_artifact_ids": {
                        "Enhanced Defense": infusion_option["id"],
                    },
                    "option_prerequisites": {
                        "Enhanced Defense": {"minimum_level": 1},
                    },
                    "option_subtype": "selectable_option",
                },
                "selection_requirements_by_level": {},
                "mechanical_grants": {},
            },
            "rule_refs": ["book:addon:artificer:p4"],
        }
        infusion_feature["selection_contract"] = build_selection_contract(
            infusion_feature,
            status="ready",
            references=["book:addon:artificer:p4"],
        )
        await import_and_activate_addon_fixture(
            _call,
            server,
            campaign["id"],
            config.home,
            manifest={
                "id": "dnd5e.addon.artificer",
                "version": "1.0.0",
                "title": "Reviewed Artificer",
                "namespace": "dnd5e.addon.artificer",
                "system_id": "dnd5e",
                "editions": ["2014"],
                "capabilities": [],
            },
            artifacts=[artifact, tool_feature, infusion_feature, infusion_option],
            mechanics=[],
            expected_revision=profile["campaign_revision"],
            request_key="addon-class",
        )
        sheet = default_character_sheet()
        sheet["abilities"]["constitution"]["score"] = 14
        character = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Class Tester", "sheet": sheet},
                "principal_id": "system:local",
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
                    "tools": ["weaver's tools"],
                },
                "expected_revision": character["revision"],
                "idempotency_key": "addon-class-apply",
            },
        )
        assert applied["sheet"]["progression"]["classes"][0]["name"] == "Artificer"
        assert (
            applied["sheet"]["progression"]["classes"][0]["spellcasting"]
            == (artifact["card"]["class_definition"]["spellcasting"])
        )
        assert applied["sheet"]["combat"]["hp"]["max"] == 10
        assert applied["sheet"]["spellcasting"]["ability"] == "intelligence"
        assert applied["sheet"]["spellcasting"]["class_lists"] == ["artificer"]
        assert applied["sheet"]["spellcasting"]["spell_slots"]["1"]["max"] == 2
        assert applied["sheet"]["spellcasting"]["preparation"]["max_prepared"] == 1
        assert applied["sheet"]["skills"]["arcana"]["proficiency"] == "proficient"
        assert applied["class_materialization"]["saving_throw_proficiencies"] == [
            "constitution",
            "intelligence",
        ]
        assert applied["class_materialization"]["tool_proficiency_choices"] == ["weaver's tools"]
        assert applied["sheet"]["content"]["selections"][0]["selection"] == {
            "skills": ["arcana", "investigation"],
            "tools": ["weaver's tools"],
        }
        feature_applied = await _call(
            server,
            "character_content_apply",
            {
                "character_id": character["id"],
                "artifact_id": tool_feature["id"],
                "selection": {
                    "tool_replacements": {
                        "Alchemist's Supplies": "Smith's Tools",
                    }
                },
                "expected_revision": applied["revision"],
                "idempotency_key": "addon-tool-expertise-apply",
            },
        )
        proficiencies = feature_applied["sheet"]["traits"]["proficiencies"]
        assert proficiencies["weapons"] == ["simple weapons", "Martial Weapons"]
        assert proficiencies["tools"] == [
            "thieves' tools",
            "tinker's tools",
            "Alchemist's Supplies",
            "weaver's tools",
            "Smith's Tools",
        ]
        assert proficiencies["tool_expertise_all"] is True
        assert proficiencies["tool_expertise"] == proficiencies["tools"]
        assert feature_applied["sheet"]["skills"]["history"]["proficiency"] == ("proficient")
        infused = await _call(
            server,
            "character_content_apply",
            {
                "character_id": character["id"],
                "artifact_id": infusion_feature["id"],
                "selection": {"infusions": ["Enhanced Defense"]},
                "expected_revision": feature_applied["revision"],
                "idempotency_key": "addon-infusion-apply",
            },
        )
        enhanced_defense = next(
            item
            for item in infused["sheet"]["content"]["features"]
            if item["id"] == infusion_option["id"]
        )
        assert enhanced_defense["source_key"] == "Infuse Item"
        with pytest.raises(Exception, match="must be granted by their parent"):
            await _call(
                server,
                "character_content_apply",
                {
                    "character_id": character["id"],
                    "artifact_id": infusion_option["id"],
                    "selection": {},
                    "expected_revision": infused["revision"],
                    "idempotency_key": "addon-infusion-direct-rejected",
                },
            )

    import asyncio

    asyncio.run(exercise())
