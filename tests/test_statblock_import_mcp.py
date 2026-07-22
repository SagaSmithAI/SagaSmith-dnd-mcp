import asyncio
from pathlib import Path

import pytest

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
        assert created["source"]["chunk_ids"]
        assert created["statblock"] == {
            "challenge_rating": "0",
            "experience_points": 10,
            "warnings": [],
            "settlement": "automatic",
        }
        actor = created["character"]
        assert actor["name"] == "Falten"
        assert actor["summary"].startswith("A tavern patron")
        club = actor["derived"]["inventory"]["weapon_attacks"][0]
        assert club["item_id"] == "club"
        assert club["attack_bonus"] == 2
        assert club["damage_expression"] == "1d4"
        assert "rule-source:srd/commoner" in actor["notes"]["profile"]["dm_notes"]

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
                        "creature_type": "undead",
                        "current_hit_points": 1,
                        "armor_class": 12,
                        "languages": ["Common", "Elvish"],
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
        assert variant_actor["sheet"]["progression"]["species"] == "undead"
        assert variant_actor["sheet"]["combat"]["hp"] == {"value": 1, "max": 4, "temp": 0}
        assert variant_actor["derived"]["armor_class"] == 12
        assert variant_actor["sheet"]["traits"]["languages"] == ["Common", "Elvish"]
        assert variant_actor["derived"]["inventory"]["weapon_attacks"][0]["item_id"] == (
            "gauntlet-slam"
        )
        assert "Variant source: rule-chunk:" in (
            variant_actor["notes"]["profile"]["dm_notes"]
        )
        assert variant["variant_evidence"]["kind"] == "rule-chunk"
        assert variant["variant_evidence"]["source_id"] == ingested["source_id"]

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
        ray_id = spells["Ray of Sickness"]["id"]
        with pytest.raises(Exception, match="source_components_confirmed"):
            await _call(
                server,
                "character_cast_spell",
                {
                    "character_id": actor["id"],
                    "spell_id": ray_id,
                    "expected_revision": actor["revision"],
                    "idempotency_key": "cast-without-component-ruling",
                },
            )
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
            "Ray of Sickness: source-bound statblock spell requires component and effect ruling"
        ]

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
        assert parry["activation"]["type"] == "reaction"
        assert created["statblock"]["settlement"] == "mixed"
        assert created["statblock"]["warnings"] == [
            "Parry: descriptive reaction is not automatically settled"
        ]

    asyncio.run(exercise())
