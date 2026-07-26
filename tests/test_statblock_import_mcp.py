import asyncio
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server

COMMONER = """### Commoner

*Medium humanoid (any race), any alignment*

**Armor Class** 10

**Hit Points** 4 (1d8)

**Speed** 30 ft.

| STR | DEX | CON | INT | WIS | CHA |
|:---:|:---:|:---:|:---:|:---:|:---:|
| 10 (+0) | 10 (+0) | 10 (+0) | 10 (+0) | 10 (+0) | 10 (+0) |

**Senses** passive Perception 10

**Languages** any one language (usually Common)

**Challenge** 0 (10 XP)

###### Actions

***Club***. *Melee Weapon Attack:* +2 to hit, reach 5 ft., one target.
*Hit:* 2 (1d4) bludgeoning damage.
"""


REACTIVE_COMMONER = COMMONER + """

###### Reactions

***Parry***. The commoner adds 2 to its AC against one melee attack that would hit it.
"""


SPLIT_GUARD_LAYOUT = """# Appendix B: Nonplayer Characters

## CULT FANATIC

### GUARD

Medium humanoid (any race), any alignment Armor Class 16 (chain shirt, shield)
Hit Points 11 (2d8 + 2) Speed 30ft.

#### STR

13 (+1)

#### DEX

12 (+1) Skills Perception +2

#### CON

12 (+1) Senses passive Perception 12

#### INT

10 (+0)

#### WIS

11 (+0) Languages any one language (usually Common) Challenge 1/8 (25 XP)

#### ACTIONS

#### CHA

10 (+0) Spear. Melee or Ranged Weapon Attack: +3 to hit, reach 5 ft. or range
20f60 ft., one target. Hit: 4 (1d6 + 1) piercing damage. Guards include members
of a city watch, sentries in a citadel or fortified town.

### KNIGHT

Medium humanoid (any race), any alignment Armor Class 18 (plate) Hit Points 52
(8d8 + 16) Speed 30ft.
"""


STATBLOCK_SPELLCASTER = """### Master of Souls

*Medium humanoid (human), neutral evil*

**Armor Class** 12
**Hit Points** 45 (6d8 + 18)
**Speed** 30 ft.

| STR | DEX | CON | INT | WIS | CHA |
|:---:|:---:|:---:|:---:|:---:|:---:|
| 10 (+0) | 14 (+2) | 17 (+3) | 19 (+4) | 14 (+2) | 13 (+1) |

**Senses** passive Perception 12
**Languages** Common
**Challenge** 4 (1,100 XP)

***Spellcasting***. The master of souls is a 5th-level spellcaster. Its spellcasting
ability is Intelligence (spell save DC 14, +6 to hit with spell attacks). It has the
following wizard spells prepared:

Cantrips (at will): chill touch, mage hand

1st level (4 slots): ray of sickness, shield

2nd level (3 slots): scorching ray

###### Actions

***Multiattack***. The master of souls makes two attacks with its silvered skull flail.

***Silvered Skull Flail***. *Melee Weapon Attack:* +2 to hit, reach 5 ft., one target.
*Hit:* 4 (1d8) bludgeoning damage plus 14 (4d6) necrotic damage. Until the end of
the target's next turn, it has disadvantage on saving throws against effects that
turn undead.

***Chill Touch***. *Ranged Spell Attack:* +6 to hit, range 120 ft., one target.
*Hit:* 13 (2d8) necrotic damage.

***Ray of Sickness (1st-Level Spell; Requires a Spell Slot)***.
*Ranged Spell Attack:* +6 to hit, range 60 ft., one target.
*Hit:* 9 (2d8) poison damage.

***Scorching Ray (2nd-Level Spell; Requires a Spell Slot)***.
*Ranged Spell Attack:* +6 to hit, range 60 ft., one target.
*Hit:* 7 (2d6) fire damage.
"""


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    value = result.get("result", result) if isinstance(result, dict) else result
    if isinstance(value, dict) and "action" in value and "result" in value:
        return value["result"]
    return value


def test_imported_rule_source_creates_a_source_bound_combat_actor(tmp_path: Path) -> None:
    import_root = tmp_path / "rules"
    import_root.mkdir()
    commoner = import_root / "commoner.md"
    commoner.write_text(COMMONER, encoding="utf-8")
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        rule_import_roots=(import_root,),
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Statblock actors",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        staged = await _call(
            server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "stage",
                "payload": {
                    "source_path": str(commoner),
                    "source_key": "srd/commoner",
                    "title": "Commoner",
                    "edition": "2014",
                    "publication_id": "srd2014",
                },
                "idempotency_key": "stage-commoner",
            },
        )
        job_id = staged["job"]["id"]
        await _call(
            server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "inspect",
                "payload": {"job_id": job_id},
                "idempotency_key": "inspect-commoner",
            },
        )
        ingested = await _call(
            server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "ingest",
                "payload": {"job_id": job_id},
                "idempotency_key": "ingest-commoner",
            },
        )
        chunks = await _call(
            server,
            "rule_pack_query",
            {
                "view": "source_chunks",
                "payload": {
                    "source_id": ingested["source_id"],
                    "query": "commoner",
                },
            },
        )
        assert chunks
        assert any(
            "commoner"
            in "\n".join([*item["heading_path"], item["content"]]).casefold()
            for item in chunks
        )
        arguments = {
            "mode": "statblock",
            "payload": {
                "campaign_id": campaign["id"],
                "source_id": ingested["source_id"],
                "name": "Falten",
                "character_type": "npc",
                "summary": "A tavern patron grounded in the imported module scene.",
            },
            "idempotency_key": "actor-falten",
        }
        created = await _call(server, "character_create_from", arguments)
        replay = await _call(server, "character_create_from", arguments)

        assert replay == created
        assert created["source"]["id"] == ingested["source_id"]
        assert len(created["source"]["chunk_ids"]) >= 2
        assert created["statblock"] == {
            "challenge_rating": "0",
            "experience_points": 10,
            "warnings": [],
            "settlement": "automatic",
            "ruling_requirements": [],
            "default_dm_resolver": "agent",
        }
        actor = created["character"]
        assert actor["name"] == "Falten"
        assert actor["summary"].startswith("A tavern patron")
        club = actor["derived"]["inventory"]["weapon_attacks"][0]
        assert club["item_id"] == "club"
        assert club["attack_bonus"] == 2
        assert club["damage_expression"] == "1d4"
        assert "rule-source:srd/commoner" in actor["notes"]["profile"]["dm_notes"]

        replacement_arguments = {
            "mode": "statblock",
            "payload": {
                "campaign_id": campaign["id"],
                "source_id": ingested["source_id"],
                "name": "Falten",
                "character_type": "npc",
                "replace_character_id": actor["id"],
                "expected_revision": actor["revision"],
                "variant": {
                    "source_ref": f"rule-chunk:{created['source']['chunk_ids'][0]}",
                    "current_hit_points": 1,
                },
            },
            "idempotency_key": "replace-actor-falten",
        }
        replaced = await _call(
            server,
            "character_create_from",
            replacement_arguments,
        )
        replacement_replay = await _call(
            server,
            "character_create_from",
            replacement_arguments,
        )
        assert replacement_replay == replaced
        assert replaced["character"]["id"] == actor["id"]
        assert replaced["character"]["revision"] == actor["revision"] + 1
        assert replaced["character"]["sheet"]["combat"]["hp"]["value"] == 1

        variant = await _call(
            server,
            "character_create_from",
            {
                "mode": "statblock",
                "payload": {
                    "campaign_id": campaign["id"],
                    "source_id": ingested["source_id"],
                    "name": "Source-bound Variant",
                    "character_type": "npc",
                    "variant": {
                        "source_ref": f"rule-chunk:{created['source']['chunk_ids'][0]}",
                        "source_refs": [
                            f"rule-chunk:{created['source']['chunk_ids'][1]}"
                        ],
                        "challenge_rating": "1/8",
                        "experience_points": 25,
                        "creature_type": "undead",
                        "current_hit_points": 1,
                        "armor_class": 12,
                        "alignment": "chaotic evil",
                        "darkvision_ft": 60,
                        "languages": ["Common", "Elvish"],
                        "relentless_endurance": {
                            "feature_id": "relentless-endurance",
                            "source_excerpt": (
                                "When reduced to 0 hit points, he drops to 1 hit point "
                                "instead (but can't do this again until he finishes a "
                                "long rest)."
                            ),
                        },
                        "action_overrides": {
                            "club": {
                                "id": "gauntlet-slam",
                                "name": "Gauntlet Slam",
                                "damage_type": "force",
                            }
                        },
                    },
                },
                "idempotency_key": "actor-source-bound-variant",
            },
        )
        variant_actor = variant["character"]
        assert variant["statblock"]["challenge_rating"] == "1/8"
        assert variant["statblock"]["experience_points"] == 25
        assert variant_actor["sheet"]["progression"]["species"] == "undead"
        assert variant_actor["sheet"]["combat"]["hp"] == {"value": 1, "max": 4, "temp": 0}
        assert variant_actor["derived"]["armor_class"] == 12
        assert variant_actor["sheet"]["traits"]["alignment"] == "chaotic evil"
        assert variant_actor["sheet"]["traits"]["senses"]["darkvision"] == 60
        assert variant_actor["sheet"]["traits"]["languages"] == ["Common", "Elvish"]
        feature = next(
            item
            for item in variant_actor["sheet"]["content"]["features"]
            if item["id"] == "relentless-endurance"
        )
        assert {
            key: feature["uses"][key]
            for key in ("label", "value", "max", "recovers_on")
        } == {
            "label": "uses",
            "value": 1,
            "max": 1,
            "recovers_on": "long_rest",
        }
        assert variant_actor["derived"]["inventory"]["weapon_attacks"][0]["item_id"] == (
            "gauntlet-slam"
        )
        assert "Variant source: rule-chunk:" in (
            variant_actor["notes"]["profile"]["dm_notes"]
        )
        assert variant["variant_evidence"]["kind"] == "multiple"
        assert len(variant["variant_evidence"]["sources"]) == 2
        assert {
            item["source_id"] for item in variant["variant_evidence"]["sources"]
        } == {ingested["source_id"]}
        current_campaign = await _call(
            server,
            "campaign_get",
            {"campaign_id": campaign["id"]},
        )
        endured = await _call(
            server,
            "combat_apply_damage",
            {
                "campaign_id": campaign["id"],
                "target_id": variant_actor["id"],
                "parts": [{"amount": 1, "damage_type": "cold"}],
                "expected_revision": current_campaign["revision"],
                "idempotency_key": "variant-relentless-endurance",
            },
        )
        after_endurance = await _call(
            server,
            "character_get",
            {"character_id": variant_actor["id"]},
        )
        assert endured["after_hp"] == 1
        assert endured["relentless_endurance_triggered"] is True
        assert endured["relentless_endurance_use"]["after_uses"] == 0
        persisted_feature = next(
            item
            for item in after_endurance["sheet"]["content"]["features"]
            if item["id"] == "relentless-endurance"
        )
        assert persisted_feature["uses"]["value"] == 0

        current_campaign = await _call(
            server,
            "campaign_get",
            {"campaign_id": campaign["id"]},
        )
        spent = await _call(
            server,
            "combat_apply_damage",
            {
                "campaign_id": campaign["id"],
                "target_id": variant_actor["id"],
                "parts": [{"amount": 1, "damage_type": "cold"}],
                "expected_revision": current_campaign["revision"],
                "idempotency_key": "variant-relentless-spent",
            },
        )
        assert spent["after_hp"] == 0
        assert spent["relentless_endurance_triggered"] is False

        downed = await _call(
            server,
            "character_create_from",
            {
                "mode": "statblock",
                "payload": {
                    "campaign_id": campaign["id"],
                    "source_id": ingested["source_id"],
                    "name": "Source-authored Captive",
                    "character_type": "npc",
                    "variant": {
                        "source_ref": f"rule-chunk:{created['source']['chunk_ids'][0]}",
                        "current_hit_points": 0,
                    },
                },
                "idempotency_key": "actor-source-authored-captive",
            },
        )
        source_state_arguments = {
            "character_id": downed["character"]["id"],
            "action": "source_state",
            "payload": {
                "state": "stable_unconscious",
                "source_ref": f"rule-chunk:{created['source']['chunk_ids'][0]}",
                "reason": "The adventure introduces the captive unconscious and stable.",
            },
            "expected_revision": downed["character"]["revision"],
            "idempotency_key": "source-state-captive",
        }
        initialized = await _call(server, "character_state_change", source_state_arguments)
        replay = await _call(server, "character_state_change", source_state_arguments)

        assert replay == initialized
        assert initialized["result"] == {
            "status": "initialized",
            "source_state": "stable_unconscious",
        }
        assert initialized["character"]["sheet"]["combat"]["hp"]["value"] == 0
        assert initialized["character"]["sheet"]["combat"]["death_saves"] == {
            "successes": 0,
            "failures": 0,
        }
        assert initialized["character"]["sheet"]["conditions"] == [
            "prone",
            "stable",
            "unconscious",
        ]
        assert initialized["source_evidence"]["source_id"] == ingested["source_id"]
        with pytest.raises(ToolError, match="managed sources"):
            await _call(
                server,
                "character_state_change",
                {
                    **source_state_arguments,
                    "payload": {
                        **source_state_arguments["payload"],
                        "source_ref": "rule-chunk:not-managed",
                    },
                    "expected_revision": initialized["character"]["revision"],
                    "idempotency_key": "source-state-unmanaged",
                },
            )

    asyncio.run(exercise())


def test_rule_statblock_recovers_split_text_layout_without_images(tmp_path: Path) -> None:
    import_root = tmp_path / "rules"
    import_root.mkdir()
    source_path = import_root / "split-guard.md"
    source_path.write_text(SPLIT_GUARD_LAYOUT, encoding="utf-8")
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        rule_import_roots=(import_root,),
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Text layout recovery",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        staged = await _call(
            server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "stage",
                "payload": {
                    "source_path": str(source_path),
                    "source_key": "mm/split-guard",
                    "title": "Split Guard",
                    "edition": "2014",
                    "publication_id": "mm2014",
                },
                "idempotency_key": "stage",
            },
        )
        job_id = staged["job"]["id"]
        await _call(
            server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "inspect",
                "payload": {"job_id": job_id},
                "idempotency_key": "inspect",
            },
        )
        ingested = await _call(
            server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "ingest",
                "payload": {"job_id": job_id},
                "idempotency_key": "ingest",
            },
        )
        chunks = await _call(
            server,
            "rule_pack_query",
            {
                "view": "source_chunks",
                "payload": {"source_id": ingested["source_id"], "limit": 200},
            },
        )

        created = await _call(
            server,
            "character_create_from",
            {
                "mode": "statblock",
                "payload": {
                    "campaign_id": campaign["id"],
                    "source_id": ingested["source_id"],
                    "chunk_ids": [item["id"] for item in chunks],
                    "source_statblock_name": "Guard",
                    "name": "Mill Ruse Guard",
                    "character_type": "monster",
                },
                "idempotency_key": "create-guard",
            },
        )

        recovery = created["source"]["text_layout_recovery"]
        assert recovery["profile"] == "deterministic-text-layout-v1"
        assert recovery["source_statblock_name"] == "Guard"
        assert created["source"]["chunk_ids"] == recovery["chunk_ids"]
        assert len(recovery["chunk_ids"]) == 8
        assert all(
            "KNIGHT"
            not in next(item for item in chunks if item["id"] == chunk_id)["heading_path"]
            for chunk_id in recovery["chunk_ids"]
        )
        assert created["statblock"]["challenge_rating"] == "1/8"
        assert created["statblock"]["experience_points"] == 25
        spear = created["character"]["derived"]["inventory"]["weapon_attacks"][0]
        assert spear["item_id"] == "spear"
        assert spear["attack_bonus"] == 3
        assert spear["range_ft"] == {"normal": 20, "long": 60}
        assert "Text-layout recovery: deterministic-text-layout-v1" in created[
            "character"
        ]["notes"]["profile"]["dm_notes"]

    asyncio.run(exercise())


def test_statblock_spellcasting_binds_slots_and_active_content(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[2]
    import_root = tmp_path / "rules"
    import_root.mkdir()
    source_path = import_root / "master-of-souls.md"
    source_path.write_text(STATBLOCK_SPELLCASTER, encoding="utf-8")
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=workspace / "SagaSmith-dnd-skills",
        modulegen_skills_dir=tmp_path / "modulegen",
        rule_import_roots=(import_root,),
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Spellcaster import", "edition": "2014", "idempotency_key": "campaign"},
        )
        staged = await _call(
            server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "stage",
                "payload": {
                    "source_path": str(source_path),
                    "source_key": "module/master-of-souls",
                    "title": "Master of Souls",
                    "edition": "2014",
                    "publication_id": "module",
                },
                "idempotency_key": "stage",
            },
        )
        await _call(
            server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "inspect",
                "payload": {"job_id": staged["job"]["id"]},
                "idempotency_key": "inspect",
            },
        )
        ingested = await _call(
            server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "ingest",
                "payload": {"job_id": staged["job"]["id"]},
                "idempotency_key": "ingest",
            },
        )
        created = await _call(
            server,
            "character_create_from",
            {
                "mode": "statblock",
                "payload": {
                    "campaign_id": campaign["id"],
                    "source_id": ingested["source_id"],
                    "name": "Flennis",
                    "character_type": "monster",
                },
                "idempotency_key": "create",
            },
        )

        actor = created["character"]
        assert actor["sheet"]["spellcasting"]["ability"] == "intelligence"
        assert actor["sheet"]["spellcasting"]["attack_bonus_override"] == 6
        assert actor["sheet"]["spellcasting"]["save_dc_override"] == 14
        assert actor["derived"]["spellcasting"]["attack_bonus"] == 6
        assert actor["derived"]["spellcasting"]["save_dc"] == 14
        assert actor["sheet"]["spellcasting"]["spell_slots"] == {
            "1": {
                "label": "Level 1 spell slots",
                "value": 4,
                "max": 4,
                "recovers_on": "long_rest",
                "source_key": "rule-source:module/master-of-souls",
                "slot_level": 1,
            },
            "2": {
                "label": "Level 2 spell slots",
                "value": 3,
                "max": 3,
                "recovers_on": "long_rest",
                "source_key": "rule-source:module/master-of-souls",
                "slot_level": 2,
            },
        }
        spells = {item["name"]: item for item in actor["sheet"]["content"]["spells"]}
        assert spells["Chill Touch"]["id"] == "dnd5e.content.srd2014.spell.chill-touch"
        assert spells["Shield"]["id"] == "dnd5e.content.srd2014.spell.shield"
        assert spells["Scorching Ray"]["id"] == (
            "dnd5e.content.srd2014.spell.scorching-ray"
        )
        assert spells["Ray of Sickness"]["id"] == (
            "rule-source:module/master-of-souls.spell.ray-of-sickness"
        )
        assert spells["Ray of Sickness"]["custom_definition"] == {
            "source": "rule-source:module/master-of-souls",
            "component_details": "not_repeated_in_statblock",
        }
        assert spells["Scorching Ray"]["resolution"]["attack"]["count"]["base"] == 3
        assert spells["Scorching Ray"]["resolution"]["attack"][
            "attack_bonus_override"
        ] == 6
        assert spells["Scorching Ray"]["resolution"]["attack"][
            "range_ft_override"
        ] == 60
        assert spells["Scorching Ray"]["definition"]["range"]["normal_ft"] == 60
        assert spells["Scorching Ray"]["definition"]["range"]["long_ft"] == 0
        assert "range 60 ft." in spells["Scorching Ray"]["definition"]["effect"]
        assert "Statblock action overrides" in spells["Scorching Ray"]["notes"]
        assert spells["Ray of Sickness"]["resolution"]["attack"][
            "attack_bonus_override"
        ] == 6
        assert spells["Ray of Sickness"]["definition"]["range"]["normal_ft"] == 60
        assert spells["Ray of Sickness"]["definition"]["range"]["long_ft"] == 0
        assert spells["Ray of Sickness"]["mechanic_refs"] == [
            "dnd5e.core.spell.structured_resolution"
        ]
        source_chunk_id = created["source"]["chunk_ids"][0]
        variant = await _call(
            server,
            "character_create_from",
            {
                "mode": "statblock",
                "payload": {
                    "campaign_id": campaign["id"],
                    "source_id": ingested["source_id"],
                    "name": "Source Variant Spellcaster",
                    "character_type": "npc",
                    "variant": {
                        "source_ref": f"rule-chunk:{source_chunk_id}",
                        "size": "small",
                        "walking_speed_ft": 25,
                        "maximum_hit_points": 31,
                        "current_hit_points": 31,
                        "spell_replacements": [
                            {
                                "remove_spell_id": (
                                    "dnd5e.content.srd2014.spell.shield"
                                ),
                                "add_spell_id": (
                                    "dnd5e.content.srd2014.spell.magic-missile"
                                ),
                            }
                        ],
                        "expend_all_spell_slots": True,
                        "add_features": [
                            {
                                "id": "variant-brave",
                                "name": "Brave",
                                "description": (
                                    "The actor has advantage on saving throws "
                                    "against being frightened."
                                ),
                            }
                        ],
                    },
                },
                "idempotency_key": "create-source-variant-spellcaster",
            },
        )
        variant_actor = variant["character"]
        assert variant_actor["sheet"]["traits"]["size"] == "small"
        assert variant_actor["sheet"]["combat"]["speed"]["walk"] == 25
        assert variant_actor["sheet"]["combat"]["hp"] == {
            "value": 31,
            "max": 31,
            "temp": 0,
        }
        assert all(
            slot["value"] == 0
            for slot in variant_actor["sheet"]["spellcasting"]["spell_slots"].values()
        )
        variant_spell_ids = {
            item["id"] for item in variant_actor["sheet"]["content"]["spells"]
        }
        assert "dnd5e.content.srd2014.spell.shield" not in variant_spell_ids
        assert "dnd5e.content.srd2014.spell.magic-missile" in variant_spell_ids
        assert "dnd5e.content.srd2014.spell.magic-missile" in (
            variant_actor["derived"]["spellcasting"]["prepared_spell_ids"]
        )
        assert any(
            item["id"] == "variant-brave"
            for item in variant_actor["sheet"]["content"]["features"]
        )
        assert variant["variant_evidence"]["id"] == source_chunk_id
        ray_id = spells["Ray of Sickness"]["id"]
        pending_components = await _call(
            server,
            "character_cast_spell",
            {
                "character_id": actor["id"],
                "spell_id": ray_id,
                "expected_revision": actor["revision"],
                "idempotency_key": "cast-without-component-ruling",
            },
        )
        assert pending_components["status"] == "pending_ruling"
        assert pending_components["default_resolver"] == "external_input"
        assert pending_components["ruling_kind"] == (
            "missing_or_conflicting_source_review"
        )
        assert pending_components["committed"] is False
        assert pending_components["missing"] == ["source_components"]
        cast = await _call(
            server,
            "character_cast_spell",
            {
                "character_id": actor["id"],
                "spell_id": ray_id,
                "component_ruling": {"source_components_confirmed": True},
                "expected_revision": actor["revision"],
                "idempotency_key": "cast-with-component-ruling",
            },
        )
        assert cast["status"] == "committed"
        assert cast["payment"] == {
            "economy": "slots",
            "level": 1,
            "ritual": False,
        }
        assert "source_components" in cast["ruling_required"]
        updated_actor = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": actor["id"]}},
        )
        assert updated_actor["sheet"]["spellcasting"]["spell_slots"]["1"]["value"] == 3
        assert [
            item["item_id"] for item in actor["derived"]["inventory"]["weapon_attacks"]
        ] == ["silvered-skull-flail"]
        flail = actor["derived"]["inventory"]["weapon_attacks"][0]
        assert flail["additional_damage"] == [
            {
                "damage_formula": "4d6",
                "damage_bonus": 0,
                "damage_type": "necrotic",
                "damage_expression": "4d6",
            }
        ]
        assert flail["on_hit_effect"].startswith("Until the end of the target's next turn")
        assert actor["derived"]["multiattack_options"] == [
            {
                "id": "melee",
                "attacks": [
                    {
                        "weapon_id": "silvered-skull-flail",
                        "attack_mode": "melee",
                        "count": 2,
                    }
                ],
            }
        ]
        assert created["statblock"]["warnings"] == [
            "Silvered Skull Flail: on-hit effect requires DM settlement",
            "Ray of Sickness: source-bound statblock spell requires component ruling",
        ]
        assert {
            item["default_resolver"]
            for item in created["statblock"]["ruling_requirements"]
        } == {"agent"}

    asyncio.run(exercise())


def test_statblock_reconstruction_preserves_reaction_heading_paths(tmp_path: Path) -> None:
    import_root = tmp_path / "rules"
    import_root.mkdir()
    reactive = import_root / "reactive-commoner.md"
    reactive.write_text(REACTIVE_COMMONER, encoding="utf-8")
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        rule_import_roots=(import_root,),
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Reaction", "edition": "2014", "idempotency_key": "campaign"},
        )
        staged = await _call(
            server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "stage",
                "payload": {
                    "source_path": str(reactive),
                    "source_key": "test/reactive-commoner",
                    "title": "Reactive Commoner",
                    "edition": "2014",
                },
                "idempotency_key": "stage",
            },
        )
        job_id = staged["job"]["id"]
        await _call(
            server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "inspect",
                "payload": {"job_id": job_id},
                "idempotency_key": "inspect",
            },
        )
        ingested = await _call(
            server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "ingest",
                "payload": {"job_id": job_id},
                "idempotency_key": "ingest",
            },
        )
        created = await _call(
            server,
            "character_create_from",
            {
                "mode": "statblock",
                "payload": {
                    "campaign_id": campaign["id"],
                    "source_id": ingested["source_id"],
                    "name": "Reactive Commoner",
                },
                "idempotency_key": "actor",
            },
        )

        parry = next(
            item
            for item in created["character"]["sheet"]["content"]["activities"]
            if item["name"] == "Parry"
        )
        assert parry["activation"] == {
            "type": "reaction",
            "cost": 1,
            "trigger": "hit by a melee attack",
        }
        assert parry["choices"]["reaction_defense"] == {
            "kind": "armor_class_bonus",
            "bonus": 2,
            "attack_modes": ["melee"],
            "requires_visible_attacker": False,
            "requires_wielded_melee_weapon": False,
        }
        assert created["statblock"]["settlement"] == "automatic"
        assert created["statblock"]["warnings"] == []
        variant = await _call(
            server,
            "character_create_from",
            {
                "mode": "statblock",
                "payload": {
                    "campaign_id": campaign["id"],
                    "source_id": ingested["source_id"],
                    "name": "Disarmed Reactive Commoner",
                    "variant": {
                        "source_ref": f"rule-chunk:{created['source']['chunk_ids'][0]}",
                        "remove_activities": ["Parry"],
                    },
                },
                "idempotency_key": "disarmed-actor",
            },
        )

        assert all(
            item["name"] != "Parry"
            for item in variant["character"]["sheet"]["content"]["activities"]
        )
        assert variant["statblock"]["settlement"] == "automatic"
        assert variant["statblock"]["warnings"] == []

    asyncio.run(exercise())
