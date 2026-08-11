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


REACTIVE_COMMONER = (
    COMMONER
    + """

###### Reactions

***Parry***. The commoner adds 2 to its AC against one melee attack that would hit it.
"""
)


BROKEN_SPELLCASTER = COMMONER.replace(
    "###### Actions",
    """***Spellcasting***. The commoner is a 1st-level spellcaster. Its
spellcasting ability is Intelligence (spell save DC 10, +2 to hit with spell
attacks). It has the following wizard spells prepared:

Cantrips (at will): fire boit

###### Actions""",
)

UNSUPPORTED_STANDARD_ON_HIT = COMMONER.replace(
    "*Hit:* 2 (1d4) bludgeoning damage.",
    "*Hit:* 2 (1d4) bludgeoning damage. The target is grappled.",
)


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

STATBLOCK_INNATE_SPELLCASTER = """### Yuan-ti Malison

*Medium monstrosity (shapechanger, yuan-ti), neutral evil*

**Armor Class** 12
**Hit Points** 66 (12d8 + 12)
**Speed** 30 ft.

| STR | DEX | CON | INT | WIS | CHA |
|:---:|:---:|:---:|:---:|:---:|:---:|
| 16 (+3) | 14 (+2) | 13 (+1) | 14 (+2) | 12 (+1) | 16 (+3) |

**Senses** darkvision 60 ft., passive Perception 11
**Languages** Abyssal, Common, Draconic
**Challenge** 3 (700 XP)

***Innate Spellcasting (Yuan-ti Form Only).*** The yuan-ti's innate spellcasting
ability is Charisma (spell save DC 13). The yuan-ti can innately cast the
following spells, requiring no material components:

At will: animal friendship (snakes only)

3/day: suggestion

###### Actions

***Bite.*** *Melee Weapon Attack:* +5 to hit, reach 5 ft., one target.
*Hit:* 5 (1d4 + 3) piercing damage.
"""


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def test_imported_rule_source_creates_a_source_bound_combat_actor(tmp_path: Path) -> None:
    import_root = tmp_path / "rules"
    import_root.mkdir()
    commoner = import_root / "commoner.md"
    commoner.write_text(
        COMMONER + "\nA commoner is an ordinary resident with no special training.\n",
        encoding="utf-8",
    )
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
        with pytest.raises(
            ToolError,
            match=(
                "payload.source_id must identify an indexed rule source; "
                "module ids are not rule source ids"
            ),
        ):
            await _call(
                server,
                "character_create_from",
                {
                    "mode": "statblock",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "source_id": "not-a-rule-source",
                        "name": "Commoner",
                    },
                    "idempotency_key": "unknown-rule-source",
                },
            )
        staged = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
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
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "idempotency_key": "inspect-commoner",
            },
        )
        ingested = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "idempotency_key": "ingest-commoner",
            },
        )
        chunks = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "evidence",
                "payload": {
                    "job_id": job_id,
                    "kind": "chunks",
                    "query": "commoner",
                },
            },
        )
        assert chunks
        assert any(
            "commoner" in "\n".join([*item["heading_path"], item["content"]]).casefold()
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
            "normalization_notes": [
                "Club: trailing creature prose excluded from action settlement"
            ],
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
        assert (
            "Normalization notes: Club: trailing creature prose excluded"
            in (actor["notes"]["profile"]["dm_notes"])
        )
        assert (
            "Manual rulings: Club: trailing creature prose excluded"
            not in (actor["notes"]["profile"]["dm_notes"])
        )

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
        assert replaced["character"]["summary"].startswith("A tavern patron")
        assert replaced["character"]["notes"]["profile"]["dm_notes"].count("Statblock import:") == 2

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
                        "source_refs": [f"rule-chunk:{created['source']['chunk_ids'][1]}"],
                        "challenge_rating": "1/8",
                        "experience_points": 25,
                        "creature_type": "undead",
                        "current_hit_points": 1,
                        "armor_class": 12,
                        "ability_scores": {
                            "intelligence": 10,
                            "wisdom": 10,
                        },
                        "alignment": "chaotic evil",
                        "darkvision_ft": 60,
                        "languages": ["Common", "Elvish"],
                        "damage_resistances": ["fire"],
                        "damage_immunities": ["cold"],
                        "damage_vulnerabilities": ["radiant"],
                        "add_features": [
                            {
                                "id": "last-defiance",
                                "name": "Last Defiance",
                                "description": (
                                    "When reduced to 0 hit points, he drops to 1 hit "
                                    "point instead once, then must finish a long rest "
                                    "before doing so again."
                                ),
                            }
                        ],
                        "action_overrides": {
                            "club": {
                                "id": "gauntlet-slam",
                                "name": "Gauntlet Slam",
                                "damage_type": "force",
                                "additional_damage": [
                                    {
                                        "damage_formula": "1d6",
                                        "damage_bonus": 0,
                                        "damage_type": "fire",
                                    }
                                ],
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
        assert variant_actor["derived"]["ability_scores"]["intelligence"] == 10
        assert variant_actor["derived"]["ability_scores"]["wisdom"] == 10
        assert variant_actor["derived"]["passive_perception"] == 10
        assert variant_actor["sheet"]["traits"]["alignment"] == "chaotic evil"
        assert variant_actor["sheet"]["traits"]["senses"]["darkvision"] == 60
        assert variant_actor["sheet"]["traits"]["languages"] == ["Common", "Elvish"]
        assert variant_actor["sheet"]["traits"]["resistances"] == ["fire"]
        assert variant_actor["sheet"]["traits"]["immunities"] == ["cold"]
        assert variant_actor["sheet"]["traits"]["vulnerabilities"] == ["radiant"]
        feature = next(
            item
            for item in variant_actor["sheet"]["content"]["features"]
            if item["id"] == "last-defiance"
        )
        assert feature["description"].startswith("When reduced to 0 hit points")
        assert feature["choices"] == {}
        assert feature["mechanic_refs"] == []
        assert variant_actor["derived"]["inventory"]["weapon_attacks"][0]["item_id"] == (
            "gauntlet-slam"
        )
        assert variant_actor["derived"]["inventory"]["weapon_attacks"][0]["additional_damage"][
            0
        ] == {
            "damage_formula": "1d6",
            "damage_bonus": 0,
            "damage_type": "fire",
            "damage_expression": "1d6",
        }
        assert "Variant source: rule-chunk:" in (variant_actor["notes"]["profile"]["dm_notes"])
        assert variant["variant_evidence"]["kind"] == "multiple"
        assert len(variant["variant_evidence"]["sources"]) == 2
        assert {item["source_id"] for item in variant["variant_evidence"]["sources"]} == {
            ingested["source_id"]
        }
        with pytest.raises(
            ToolError,
            match="module_search and module_expand.*route label",
        ):
            await _call(
                server,
                "character_create_from",
                {
                    "mode": "statblock",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "source_id": ingested["source_id"],
                        "name": "Invalid Route Label Variant",
                        "character_type": "npc",
                        "variant": {
                            "source_ref": "module-chunk:encounter",
                            "creature_type": "undead",
                        },
                    },
                    "idempotency_key": "invalid-route-label-variant",
                },
            )
        current_campaign = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        immune = await _call(
            server,
            "combat_hp_change",
            {
                "campaign_id": campaign["id"],
                "target_id": variant_actor["id"],
                "action": "damage",
                "payload": {"parts": [{"amount": 1, "damage_type": "cold"}]},
                "principal_id": "system:local",
                "expected_revision": current_campaign["revision"],
                "idempotency_key": "variant-cold-immunity",
            },
        )
        assert immune["result"]["after_hp"] == 1
        assert immune["result"]["applied_amount"] == 0

        current_campaign = await _call(
            server,
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign["id"]},
                "principal_id": "system:local",
            },
        )
        downed_by_damage = await _call(
            server,
            "combat_hp_change",
            {
                "campaign_id": campaign["id"],
                "target_id": variant_actor["id"],
                "action": "damage",
                "payload": {"parts": [{"amount": 1, "damage_type": "force"}]},
                "principal_id": "system:local",
                "expected_revision": current_campaign["revision"],
                "idempotency_key": "variant-force-damage",
            },
        )
        assert downed_by_damage["result"]["after_hp"] == 0

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
    source_path.write_text(
        COMMONER + "\n\n" + SPLIT_GUARD_LAYOUT,
        encoding="utf-8",
    )
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
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
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
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "idempotency_key": "inspect",
            },
        )
        ingested = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "idempotency_key": "ingest",
            },
        )
        chunks = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "evidence",
                "payload": {
                    "job_id": job_id,
                    "kind": "chunks",
                    "limit": 200,
                },
            },
        )

        with pytest.raises(ToolError, match="unsupported fields: exact_chunks"):
            await _call(
                server,
                "character_create_from",
                {
                    "mode": "statblock",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "source_id": ingested["source_id"],
                        "exact_chunks": [item["id"] for item in chunks],
                        "source_statblock_name": "Guard",
                        "name": "Invalid Guard",
                    },
                    "idempotency_key": "reject-exact-chunks",
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
        assert created["statblock"]["source_identity"] == "Guard"
        assert len(recovery["chunk_ids"]) == 8
        assert all(
            "KNIGHT" not in next(item for item in chunks if item["id"] == chunk_id)["heading_path"]
            for chunk_id in recovery["chunk_ids"]
        )
        assert created["statblock"]["challenge_rating"] == "1/8"
        assert created["statblock"]["experience_points"] == 25
        spear = created["character"]["derived"]["inventory"]["weapon_attacks"][0]
        assert spear["item_id"] == "spear"
        assert spear["attack_bonus"] == 3
        assert spear["range_ft"] == {"normal": 20, "long": 60}
        assert (
            "Text-layout recovery: deterministic-text-layout-v1"
            in created["character"]["notes"]["profile"]["dm_notes"]
        )
        with pytest.raises(ToolError, match="no creature core headed 'Archmage'"):
            await _call(
                server,
                "character_create_from",
                {
                    "mode": "statblock",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "source_id": ingested["source_id"],
                        "chunk_ids": [item["id"] for item in chunks],
                        "source_statblock_name": "Archmage",
                        "name": "Wrong Card",
                        "character_type": "monster",
                    },
                    "idempotency_key": "reject-wrong-card",
                },
            )

    asyncio.run(exercise())


def test_standard_statblock_rejects_damaged_spell_names_before_persist(
    tmp_path: Path,
) -> None:
    import_root = tmp_path / "rules"
    import_root.mkdir()
    source_path = import_root / "broken-spellcaster.md"
    source_path.write_text(BROKEN_SPELLCASTER, encoding="utf-8")
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
                "name": "Damaged spell source gate",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        staged = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source_path),
                    "source_key": "mm/broken-spellcaster",
                    "title": "Broken Spellcaster",
                    "edition": "2014",
                    "publication_id": "mm2014",
                },
                "idempotency_key": "stage",
            },
        )
        await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": staged["job"]["id"]},
                "idempotency_key": "inspect",
            },
        )
        ingested = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": staged["job"]["id"]},
                "idempotency_key": "ingest",
            },
        )

        with pytest.raises(
            ToolError,
            match="standard rule spell list requires source recovery.*fire boit",
        ):
            await _call(
                server,
                "character_create_from",
                {
                    "mode": "statblock",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "source_id": ingested["source_id"],
                        "name": "Rejected Broken Spellcaster",
                        "character_type": "monster",
                    },
                    "idempotency_key": "reject-broken-spellcaster",
                },
            )

    asyncio.run(exercise())


def test_standard_statblock_prefills_source_specific_weapon_rider_ruling(
    tmp_path: Path,
) -> None:
    import_root = tmp_path / "rules"
    import_root.mkdir()
    source_path = import_root / "unsupported-on-hit.md"
    source_path.write_text(UNSUPPORTED_STANDARD_ON_HIT, encoding="utf-8")
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
                "name": "Standard weapon rider gate",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        staged = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source_path),
                    "source_key": "mm/unsupported-on-hit",
                    "title": "Unsupported Standard On-Hit",
                    "edition": "2014",
                    "publication_id": "mm2014",
                },
                "idempotency_key": "stage",
            },
        )
        await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": staged["job"]["id"]},
                "idempotency_key": "inspect",
            },
        )
        ingested = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
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
                    "name": "Source-bound Commoner",
                    "character_type": "monster",
                },
                "idempotency_key": "source-bound-commoner",
            },
        )
        club = next(
            item
            for item in created["character"]["sheet"]["inventory"]["items"]
            if item["name"] == "Club"
        )
        requirement = club["ruling_requirements"][0]
        assert requirement["default_resolver"] == "agent"
        assert requirement["policy_ref"] == "actor_card.import.v1"
        assert "grappled" in requirement["source_excerpt"]
        assert created["statblock"]["settlement"] == "mixed"

    asyncio.run(exercise())


def test_standard_statblock_keeps_open_multiattack_as_source_bound_agent_ruling(
    tmp_path: Path,
) -> None:
    import_root = tmp_path / "rules"
    import_root.mkdir()
    source_path = import_root / "open-multiattack.md"
    source_path.write_text(
        COMMONER.replace(
            "###### Actions",
            (
                "###### Actions\n\n"
                "***Multiattack.*** The commoner makes two attacks and can ring "
                "its alarm bell before, between, or after them."
            ),
        ),
        encoding="utf-8",
    )
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
                "name": "Direct Multiattack ruling",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        staged = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source_path),
                    "source_key": "mm/open-multiattack",
                    "title": "Open Multiattack",
                    "edition": "2014",
                    "publication_id": "mm2014",
                },
                "idempotency_key": "stage",
            },
        )
        await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": staged["job"]["id"]},
                "idempotency_key": "inspect",
            },
        )
        ingested = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
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
                    "name": "Open Multiattack Commoner",
                    "character_type": "monster",
                },
                "idempotency_key": "create",
            },
        )
        multiattack = next(
            activity
            for activity in created["character"]["sheet"]["content"]["activities"]
            if activity["name"] == "Multiattack"
        )
        assert multiattack["mechanic_refs"] == []
        assert multiattack["choices"]["manual_ruling"] == {
            "kind": "multiattack_composition",
            "default_resolver": "agent",
            "source_excerpt": (
                "The commoner makes two attacks and can ring its alarm bell "
                "before, between, or after them."
            ),
        }

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
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
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
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": staged["job"]["id"]},
                "idempotency_key": "inspect",
            },
        )
        ingested = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
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
        assert spells["Scorching Ray"]["id"] == ("dnd5e.content.srd2014.spell.scorching-ray")
        assert spells["Ray of Sickness"]["id"] == (
            "rule-source:module/master-of-souls.spell.ray-of-sickness"
        )
        assert spells["Ray of Sickness"]["custom_definition"] == {
            "source": "rule-source:module/master-of-souls",
            "component_details": "not_repeated_in_statblock",
        }
        assert spells["Scorching Ray"]["resolution"]["attack"]["count"]["base"] == 3
        assert spells["Scorching Ray"]["resolution"]["attack"]["attack_bonus_override"] == 6
        assert spells["Scorching Ray"]["resolution"]["attack"]["range_ft_override"] == 60
        assert spells["Scorching Ray"]["definition"]["range"]["normal_ft"] == 60
        assert spells["Scorching Ray"]["definition"]["range"]["long_ft"] == 0
        assert "range 60 ft." in spells["Scorching Ray"]["definition"]["effect"]
        assert "Statblock action overrides" in spells["Scorching Ray"]["notes"]
        assert spells["Ray of Sickness"]["resolution"]["attack"]["attack_bonus_override"] == 6
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
                                "remove_spell_id": ("dnd5e.content.srd2014.spell.shield"),
                                "add_spell_id": ("dnd5e.content.srd2014.spell.magic-missile"),
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
        variant_spell_ids = {item["id"] for item in variant_actor["sheet"]["content"]["spells"]}
        assert "dnd5e.content.srd2014.spell.shield" not in variant_spell_ids
        assert "dnd5e.content.srd2014.spell.magic-missile" in variant_spell_ids
        assert (
            "dnd5e.content.srd2014.spell.magic-missile"
            in (variant_actor["derived"]["spellcasting"]["prepared_spell_ids"])
        )
        assert any(
            item["id"] == "variant-brave" for item in variant_actor["sheet"]["content"]["features"]
        )
        assert variant["variant_evidence"]["id"] == source_chunk_id
        ray_id = spells["Ray of Sickness"]["id"]
        pending_components = await _call(
            server,
            "character_action",
            {
                "character_id": actor["id"],
                "action": "cast_spell",
                "payload": {"spell_id": ray_id},
                "principal_id": "system:local",
                "expected_revision": actor["revision"],
                "idempotency_key": "cast-without-component-ruling",
            },
        )
        assert pending_components["status"] == "pending_ruling"
        assert pending_components["default_resolver"] == "external_input"
        assert pending_components["ruling_kind"] == ("missing_or_conflicting_source_review")
        assert pending_components["committed"] is False
        assert pending_components["missing"] == ["source_components"]
        after_pending = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        assert after_pending["state"]["game_time"]["elapsed_ticks"] == 0
        cast = await _call(
            server,
            "character_action",
            {
                "character_id": actor["id"],
                "action": "cast_spell",
                "payload": {
                    "spell_id": ray_id,
                    "component_ruling": {"source_components_confirmed": True},
                },
                "principal_id": "system:local",
                "expected_revision": actor["revision"],
                "idempotency_key": "cast-with-component-ruling",
            },
        )
        assert cast["status"] == "pending_ruling"
        assert cast["default_resolver"] == "agent"
        assert cast["ruling_kind"] == "generic_spell_effect"
        assert cast["result"]["status"] == "committed"
        assert cast["result"]["payment"] == {
            "economy": "slots",
            "level": 1,
            "ritual": False,
        }
        assert "source_components" in cast["result"]["ruling_required"]
        after_cast = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        assert after_cast["state"]["game_time"]["elapsed_ticks"] == 1
        updated_actor = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": actor["id"]}},
        )
        assert updated_actor["sheet"]["spellcasting"]["spell_slots"]["1"]["value"] == 3
        assert [item["item_id"] for item in actor["derived"]["inventory"]["weapon_attacks"]] == [
            "silvered-skull-flail"
        ]
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
            item["default_resolver"] for item in created["statblock"]["ruling_requirements"]
        } == {"agent"}

    asyncio.run(exercise())


def test_innate_statblock_spellcasting_binds_daily_uses_and_qualifiers(
    tmp_path: Path,
) -> None:
    workspace = Path(__file__).resolve().parents[2]
    import_root = tmp_path / "rules"
    import_root.mkdir()
    source_path = import_root / "yuan-ti-malison.md"
    source_path.write_text(STATBLOCK_INNATE_SPELLCASTER, encoding="utf-8")
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
            {
                "name": "Innate spellcaster import",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        staged = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source_path),
                    "source_key": "module/yuan-ti-malison",
                    "title": "Yuan-ti Malison",
                    "edition": "2014",
                    "publication_id": "module",
                },
                "idempotency_key": "stage",
            },
        )
        await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": staged["job"]["id"]},
                "idempotency_key": "inspect",
            },
        )
        ingested = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
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
                    "name": "Yuan-ti Malison",
                    "character_type": "monster",
                },
                "idempotency_key": "create",
            },
        )

        actor = created["character"]
        spells = {item["name"]: item for item in actor["sheet"]["content"]["spells"]}
        animal_friendship = spells["Animal Friendship"]
        suggestion = spells["Suggestion"]
        assert animal_friendship["grant"]["method"] == "innate"
        assert animal_friendship["access"]["at_will"] is True
        assert animal_friendship["custom_definition"]["statblock_source_qualifier"] == "snakes only"
        assert suggestion["grant"]["method"] == "innate"
        assert suggestion["access"]["at_will"] is False
        resource_key = suggestion["custom_definition"]["innate_resource_key"]
        assert actor["sheet"]["resources"][resource_key] == {
            "label": "Suggestion (3/day)",
            "value": 3,
            "max": 3,
            "recovers_on": "long_rest",
            "source_key": "rule-source:module/yuan-ti-malison",
            "slot_level": 0,
        }
        assert suggestion["definition"]["duration"]["concentration"] is True
        assert suggestion["definition"]["components"]["material"] is False
        assert not any(
            warning.startswith("Innate Spellcasting")
            for warning in created["statblock"]["warnings"]
        )

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
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
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
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "idempotency_key": "inspect",
            },
        )
        ingested = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
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
            "trigger": "",
        }
        assert parry["choices"]["manual_ruling"]["default_resolver"] == "agent"
        assert created["statblock"]["settlement"] == "mixed"
        assert created["statblock"]["warnings"]
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
