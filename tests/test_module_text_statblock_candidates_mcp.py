import asyncio
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import derive_character_sheet
from sagasmith_dnd.statblocks import parse_2014_statblock

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from tests.authoring_helpers import finalize_and_activate_module

GOBLIN_MODULE = (
    "# Appendix B: Monsters\n\n"
    "## MONSTER DESCRIPTIONS\n\n"
    "##### GOBLIN\n\n"
    "Small humanoid (goblinoid), neutral evil Armor Class 15 "
    "(leather armor, shield) Hit Points 7 (2d6) Speed 30 ft.\n\n"
    "##### STR\n\n8 (-1)\n\n"
    "##### DEX\n\n14 (+2)\n\n"
    "##### CON\n\n10 (+0)\n\n"
    "##### INT\n\n10 (+0)\n\n"
    "##### WIS\n\n8 (-1)\n\n"
    "##### CHA\n\n"
    "8 (-1) Skills Stealth +6 Senses darkvision 60 ft., passive Perception 9 "
    "Languages Common, Goblin Challenge 1/4 (50 XP) Nimble Escape. "
    "The goblin can take the Disengage or Hide action as a bonus action.\n\n"
    "##### ACTIONS\n\n"
    "Scimitar. Melee Weapon Attack: +4 to hit, reach 5 ft., one target. "
    "Hit: 5 (ld6 + 2) slashing damage. Shortbow. Ranged Weapon Attack: "
    "+4 to hit, range 80 ft./320 ft., one target. Hit: 5 (1d6 + 2) "
    "piercing damage.\n"
)

EVIL_MAGE_MODULE = (
    "# Appendix B: Monsters\n\n"
    "## MONSTER DESCRIPTIONS\n\n"
    "##### EVILMAGE\n\n"
    "Medium humanoid (human), lawful evil Armor Class 12 "
    "Hit Points 22 (5d8) Speed 30 ft.\n\n"
    "##### STR\n\n9 (-1)\n\n"
    "##### DEX\n\n14 (+2)\n\n"
    "##### CON\n\n11 (+0)\n\n"
    "##### INT\n\n17 (+3)\n\n"
    "##### WIS\n\n12 (+1)\n\n"
    "##### CHA\n\n"
    "11 (+0) Saving Throws Int +5, Wis +3 Skills Arcana +5, History +5 "
    "Senses passive Perception 11 Languages Common, Draconic, Dwarvish, Elvish "
    "Challenge 1 (200 XP) Spellcasting. The mage is a 4th·level spellcaster "
    "that uses Intelligence as its spellcasting ability (spell save DC 13; "
    "+5 to hit with spell attacks). The mage knows the following spells from "
    "the wizard's spell list: Cantrips (at will): light, mage hand, shocking "
    "grasp l st Level (4 slots): charm person, magic missile 2nd Level "
    "(3 slots): hold person, misty step\n\n"
    "##### ACTIONS\n\n"
    "Quarterstaff. Melee Weapon Attack: +1 to hit, reach 5 ft., one target. "
    "Hit: 3 (1d8 - 1) bludgeoning damage.\n"
)

AGENT_FILLED_GOBLIN_MODULE = GOBLIN_MODULE.replace(
    "##### ACTIONS\n\n",
    (
        "##### ACTIONS\n\n"
        "Multiattack. The goblin attacks twice, once with its scimitar and once "
        "with its shortbow. "
    ),
)


async def _call(server, name: str, arguments: dict):
    called = await server.call_tool(name, arguments)
    if isinstance(called, tuple):
        _, result = called
        return result.get("result", result) if isinstance(result, dict) else result
    return called


def test_text_module_statblock_candidate_can_create_a_source_bound_actor(
    tmp_path: Path,
) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=True,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Text statblock", "edition": "2014", "idempotency_key": "campaign"},
        )
        staged = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "name": "goblins.md",
                    "content": GOBLIN_MODULE,
                    "source_key": "goblins",
                    "title": "Goblins",
                },
                "idempotency_key": "stage",
            },
        )
        module_id = staged["module_id"]
        candidates = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "candidates",
                "payload": {"module_id": module_id},
            },
        )

        assert len(candidates) == 1
        candidate = candidates[0]
        source_chunks = [
            await _call(server, "module_expand", {"chunk_id": chunk_id})
            for chunk_id in candidate["source_chunk_ids"]
        ]
        assert candidate["execution_state"] == "review_ready", candidate.get("review_error")
        assert [item["chunk_id"] for item in source_chunks] == candidate["source_chunk_ids"]
        assert candidate["validation"]["name"] == "GOBLIN"
        assert candidate["ruling_requirement"]["default_resolver"] == "agent"
        assert candidate["ruling_requirement"]["ruling_kind"] == "source_or_scene_fact"
        reviewed = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "operation": "content",
                    "module_id": module_id,
                    "scene_id": candidate["scene_id"],
                    "content_key": "goblin",
                    "normalized_content": candidate["normalized_content"],
                    "source_chunk_ids": candidate["source_chunk_ids"],
                    "observation": "Reviewed normalized text against all source chunks.",
                },
                "idempotency_key": "review-goblin",
            },
        )
        assert reviewed["review"]["evidence"]["confidence"] == "reviewed_text"
        with pytest.raises(Exception, match="source_identity.*expected 'GOBLIN'"):
            await _call(
                server,
                "character_create_from",
                {
                    "mode": "module_statblock",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "review_id": reviewed["review"]["id"],
                        "source_identity": "Giant Spider",
                        "name": "Cragmaw Goblin",
                        "character_type": "monster",
                    },
                    "idempotency_key": "reject-wrong-source-identity",
                },
            )
        with pytest.raises(Exception, match="unsupported fields: source_ref"):
            await _call(
                server,
                "character_create_from",
                {
                    "mode": "module_statblock",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "review_id": reviewed["review"]["id"],
                        "source_ref": {"caller": "claim"},
                    },
                    "idempotency_key": "reject-pseudo-source-ref",
                },
            )
        created = await _call(
            server,
            "character_create_from",
            {
                "mode": "module_statblock",
                "payload": {
                    "campaign_id": campaign["id"],
                    "review_id": reviewed["review"]["id"],
                    "source_identity": "Goblin",
                    "name": "Cragmaw Goblin",
                    "character_type": "monster",
                },
                "idempotency_key": "create-goblin",
            },
        )
        assert created["character"]["name"] == "Cragmaw Goblin"
        assert created["statblock"]["source_identity"] == "GOBLIN"
        assert created["character"]["derived"]["armor_class"] == 15
        assert created["character"]["derived"]["hit_points"]["max"] == 7
        attacks = {
            item["item_id"]: item
            for item in created["character"]["derived"]["inventory"]["weapon_attacks"]
        }
        assert set(attacks) == {"scimitar", "shortbow"}
        assert attacks["shortbow"]["range_ft"] == {"normal": 80, "long": 320}
        assert {
            item["source_key"] for item in created["character"]["sheet"]["inventory"]["items"]
        } == {f"module-review:{reviewed['review']['id']}"}
        await finalize_and_activate_module(
            _call,
            server,
            campaign["id"],
            staged,
            source_key="goblins",
            title="Goblins",
            portable_id="dnd5e.module.goblins-test",
        )

    asyncio.run(exercise())


def test_agent_can_fill_custom_monster_multiattack_from_exact_module_source(
    tmp_path: Path,
) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=True,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Agent-filled monster",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        staged = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "name": "agent-filled-goblin.md",
                    "content": AGENT_FILLED_GOBLIN_MODULE,
                    "source_key": "agent-filled-goblin",
                    "title": "Agent-filled Goblin",
                },
                "idempotency_key": "stage",
            },
        )
        module_id = staged["module_id"]
        candidates = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "candidates",
                "payload": {"module_id": module_id},
            },
        )
        candidate = candidates[0]
        parsed = parse_2014_statblock(
            candidate["normalized_content"],
            source_key="test-agent-fill",
        )
        activity = next(
            item for item in parsed.sheet["content"]["activities"] if item["name"] == "Multiattack"
        )
        assert derive_character_sheet(parsed.sheet)["multiattack_options"]
        requirements = candidate["agent_fill_requirements"]
        assert requirements["required"] is True
        assert requirements["parser_authoritative"] is False
        assert requirements["allowed_resolutions"] == ["structured", "agent_ruling"]
        assert requirements["submission_schema"]["root_fields"] == ["multiattack_options"]
        assert requirements["submission_schema"]["declaration_fields"] == [
            "activity_id",
            "source_excerpt",
            "reason",
            "resolution",
            "options",
        ]
        assert requirements["submission_schema"]["structured_option_fields"] == [
            "id",
            "attacks",
        ]
        assert requirements["submission_schema"]["attack_fields"] == [
            "weapon_id",
            "attack_mode",
            "count",
        ]
        assert requirements["multiattack_options"] == [
            {
                "activity_id": activity["id"],
                "source_excerpt": activity["description"],
            }
        ]
        assert {item["weapon_id"] for item in requirements["available_weapons"]} == {
            "scimitar",
            "shortbow",
        }

        pending = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "operation": "content",
                    "module_id": module_id,
                    "scene_id": candidate["scene_id"],
                    "content_key": "parser-only-goblin",
                    "normalized_content": candidate["normalized_content"],
                    "source_chunk_ids": candidate["source_chunk_ids"],
                    "observation": "Request the explicit Agent-fill contract.",
                },
                "idempotency_key": "review-agent-filled-goblin",
            },
        )
        assert pending["review"] is None
        assert pending["requires_agent_fill"] is True
        assert pending["validation"]["agent_fill"] is None
        assert pending["validation"]["agent_fill_requirements"] == requirements

        reviewed = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "operation": "content",
                    "module_id": module_id,
                    "scene_id": candidate["scene_id"],
                    "content_key": "agent-filled-goblin",
                    "normalized_content": candidate["normalized_content"],
                    "source_chunk_ids": candidate["source_chunk_ids"],
                    "observation": (
                        "The Agent reviewed the exact custom monster source and filled "
                        "the semantic Multiattack composition."
                    ),
                    "agent_fill": {
                        "multiattack_options": [
                            {
                                "activity_id": activity["id"],
                                "source_excerpt": activity["description"],
                                "reason": (
                                    "The sentence names one scimitar attack followed by "
                                    "one shortbow attack."
                                ),
                                "options": [
                                    {
                                        "id": "coordinated-assault",
                                        "attacks": [
                                            {
                                                "weapon_id": "scimitar",
                                                "attack_mode": "melee",
                                                "count": 1,
                                            },
                                            {
                                                "weapon_id": "shortbow",
                                                "attack_mode": "ranged",
                                                "count": 1,
                                            },
                                        ],
                                    }
                                ],
                            }
                        ]
                    },
                },
                # The pending response was read-only, so the completed submission may
                # retain the logical operation's idempotency key.
                "idempotency_key": "review-agent-filled-goblin",
            },
        )
        assert (
            reviewed["validation"]["agent_fill"]["multiattack_options"][0]["default_resolver"]
            == "agent"
        )
        assert reviewed["validation"]["resolved_warnings"] == []
        assert (
            "Multiattack: Multiattack composition requires a DM ruling"
            not in reviewed["validation"]["warnings"]
        )

        created = await _call(
            server,
            "character_create_from",
            {
                "mode": "module_statblock",
                "payload": {
                    "campaign_id": campaign["id"],
                    "review_id": reviewed["review"]["id"],
                    "name": "Custom Goblin",
                    "character_type": "monster",
                },
                "idempotency_key": "create-agent-filled-goblin",
            },
        )
        assert created["character"]["derived"]["multiattack_options"] == [
            {
                "id": "coordinated-assault",
                "attacks": [
                    {"weapon_id": "scimitar", "attack_mode": "melee", "count": 1},
                    {"weapon_id": "shortbow", "attack_mode": "ranged", "count": 1},
                ],
            }
        ]
        assert (
            "Multiattack: Multiattack composition requires a DM ruling"
            not in created["statblock"]["warnings"]
        )
        assert (
            created["statblock"]["agent_fill"]["multiattack_options"][0]["ruling_kind"]
            == "module_specific_procedure"
        )
        assert (
            f"Agent statblock fill: {activity['id']}."
            in created["character"]["notes"]["profile"]["dm_notes"]
        )

        ruled_review = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "operation": "content",
                    "module_id": module_id,
                    "scene_id": candidate["scene_id"],
                    "content_key": "agent-ruled-goblin",
                    "normalized_content": candidate["normalized_content"],
                    "source_chunk_ids": candidate["source_chunk_ids"],
                    "observation": (
                        "The Agent retained this custom procedure as a DM ruling "
                        "instead of trusting the parser proposal."
                    ),
                    "agent_fill": {
                        "multiattack_options": [
                            {
                                "activity_id": activity["id"],
                                "source_excerpt": activity["description"],
                                "reason": (
                                    "This regression exercises the explicit Agent-ruling route."
                                ),
                                "resolution": "agent_ruling",
                            }
                        ]
                    },
                },
                "idempotency_key": "review-agent-ruled-goblin",
            },
        )
        assert ruled_review["validation"]["settlement"] == "mixed"
        assert ruled_review["validation"]["ruling_requirements"][0]["default_resolver"] == "agent"
        ruled_actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "module_statblock",
                "payload": {
                    "campaign_id": campaign["id"],
                    "review_id": ruled_review["review"]["id"],
                    "name": "Custom Goblin Agent-Ruled",
                    "character_type": "monster",
                },
                "idempotency_key": "create-agent-ruled-goblin",
            },
        )
        assert ruled_actor["character"]["derived"]["multiattack_options"] == []
        assert ruled_actor["statblock"]["settlement"] == "mixed"
        assert ruled_actor["statblock"]["ruling_requirements"][0]["default_resolver"] == "agent"
        await finalize_and_activate_module(
            _call,
            server,
            campaign["id"],
            staged,
            source_key="agent-filled-goblin",
            title="Agent-filled Goblin",
            portable_id="dnd5e.module.agent-filled-goblin-test",
        )

    asyncio.run(exercise())


def test_text_module_spellcaster_ocr_hydrates_source_bound_spells(
    tmp_path: Path,
) -> None:
    workspace = Path(__file__).resolve().parents[2]
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=workspace / "SagaSmith-dnd-skills",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "OCR spellcaster", "edition": "2014", "idempotency_key": "campaign"},
        )
        staged = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "name": "evil-mage.md",
                    "content": EVIL_MAGE_MODULE,
                    "source_key": "evil-mage",
                    "title": "Evil Mage",
                },
                "idempotency_key": "stage",
            },
        )
        module_id = staged["module_id"]
        candidates = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "candidates",
                "payload": {"module_id": module_id},
            },
        )

        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate["execution_state"] == "review_ready"
        assert candidate["validation"]["warnings"] == []
        assert "4th-level spellcaster" in candidate["normalized_content"]
        assert "1st level (4 slots)" in candidate["normalized_content"]

        reviewed = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "operation": "content",
                    "module_id": module_id,
                    "scene_id": candidate["scene_id"],
                    "content_key": "evil-mage",
                    "normalized_content": candidate["normalized_content"],
                    "source_chunk_ids": candidate["source_chunk_ids"],
                    "observation": "Reviewed normalized text against all source chunks.",
                },
                "idempotency_key": "review-evil-mage",
            },
        )
        created = await _call(
            server,
            "character_create_from",
            {
                "mode": "module_statblock",
                "payload": {
                    "campaign_id": campaign["id"],
                    "review_id": reviewed["review"]["id"],
                    "name": "Iarno Albrek",
                    "character_type": "npc",
                },
                "idempotency_key": "create-iarno",
            },
        )

        actor = created["character"]
        assert actor["sheet"]["spellcasting"]["ability"] == "intelligence"
        assert actor["sheet"]["spellcasting"]["class_lists"] == ["wizard"]
        assert actor["sheet"]["spellcasting"]["spell_slots"] == {
            "1": {
                "label": "Level 1 spell slots",
                "value": 4,
                "max": 4,
                "recovers_on": "long_rest",
                "source_key": f"module-review:{reviewed['review']['id']}",
                "slot_level": 1,
            },
            "2": {
                "label": "Level 2 spell slots",
                "value": 3,
                "max": 3,
                "recovers_on": "long_rest",
                "source_key": f"module-review:{reviewed['review']['id']}",
                "slot_level": 2,
            },
        }
        assert {item["name"] for item in actor["sheet"]["content"]["spells"]} == {
            "Light",
            "Mage Hand",
            "Shocking Grasp",
            "Charm Person",
            "Magic Missile",
            "Hold Person",
            "Misty Step",
        }
        assert len(actor["derived"]["spellcasting"]["prepared_spell_ids"]) == 7
        assert created["statblock"]["warnings"] == []
        await finalize_and_activate_module(
            _call,
            server,
            campaign["id"],
            staged,
            source_key="evil-mage",
            title="Evil Mage",
            portable_id="dnd5e.module.evil-mage-test",
        )

    asyncio.run(exercise())


def test_reimported_module_statblock_candidates_have_globally_unique_ids(
    tmp_path: Path,
) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=True,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Candidate identity",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        module_ids: list[str] = []
        for version in ("one", "two"):
            staged = await _call(
                server,
                "module_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "start",
                    "payload": {
                        "name": f"goblins-{version}.md",
                        "content": GOBLIN_MODULE,
                        "source_key": f"goblins-{version}",
                        "title": "Goblins",
                    },
                    "idempotency_key": f"stage-{version}",
                },
            )
            activation = await finalize_and_activate_module(
                _call,
                server,
                campaign["id"],
                staged,
                source_key=f"goblins-{version}",
                title=f"Goblins {version}",
                portable_id=f"dnd5e.module.goblins-{version}-test",
            )
            module_ids.append(activation["activated"]["activation"]["module_id"])

        candidate_ids = []
        for module_id in module_ids:
            candidates = await _call(
                server,
                "module_query",
                {
                    "campaign_id": campaign["id"],
                    "view": "candidates",
                    "payload": {"module_id": module_id},
                },
            )
            candidate_ids.append(candidates[0]["id"])

        assert len(set(candidate_ids)) == 2

    asyncio.run(exercise())
