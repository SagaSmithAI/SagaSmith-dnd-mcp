"""Build the source-reviewed Mordenkainen's Tome of Foes addon fixture."""

# ruff: noqa: E501 -- source-facing names and compact card tables stay auditable.

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "fixtures" / "books_catalog_review_mordenkainen_v1.json"
BOOK = "D&D 5E - Mordenkainen's Tome of Foes.pdf"

SKILLS = [
    "Acrobatics",
    "Animal Handling",
    "Arcana",
    "Athletics",
    "Deception",
    "History",
    "Insight",
    "Intimidation",
    "Investigation",
    "Medicine",
    "Nature",
    "Perception",
    "Performance",
    "Persuasion",
    "Religion",
    "Sleight of Hand",
    "Stealth",
    "Survival",
]
TOOLS = [
    "Alchemist's Supplies",
    "Brewer's Supplies",
    "Calligrapher's Supplies",
    "Carpenter's Tools",
    "Cartographer's Tools",
    "Cobbler's Tools",
    "Cook's Utensils",
    "Glassblower's Tools",
    "Jeweler's Tools",
    "Leatherworker's Tools",
    "Mason's Tools",
    "Painter's Supplies",
    "Potter's Tools",
    "Smith's Tools",
    "Tinker's Tools",
    "Weaver's Tools",
    "Woodcarver's Tools",
    "Disguise Kit",
    "Forgery Kit",
    "Herbalism Kit",
    "Navigator's Tools",
    "Poisoner's Kit",
    "Thieves' Tools",
    "Dice",
    "Dragonchess",
    "Playing Cards",
    "Three-Dragon Ante",
    "Bagpipes",
    "Drum",
    "Dulcimer",
    "Flute",
    "Horn",
    "Lute",
    "Lyre",
    "Pan Flute",
    "Shawm",
    "Viol",
    "Vehicles (Land)",
    "Vehicles (Sea)",
]

STATBLOCK_NAMES = """Allip
Astral Dreadnought
Balhannoth
Berbalang
Boneclaw
Cadaver Collector
Choker
Bronze Scout
Iron Cobra
Oaken Bolter
Stone Defender
Corpse Flower
Deathlock
Deathlock Mastermind
Deathlock Wight
Alkilith
Bulezau
Armanite
Dybbuk
Maurezhi
Molydeus
Nabassu
Rutterkin
Abyssal Wretch
Sibriex
Wastrilith
Baphomet
Demogorgon
Fraz-Urb'luu
Graz'zt
Juiblex
Orcus
Yeenoghu
Zuggtmoy
Derro
Derro Savant
Black Abishai
Blue Abishai
Green Abishai
Red Abishai
White Abishai
Amnizu
Hellfire Engine
Merregon
Narzugon
Nupperibo
Orthon
Bael
Geryon
Hutijin
Moloch
Titivilus
Zariel
Drow Arachnomancer
Drow Favored Consort
Drow House Captain
Drow Inquisitor
Drow Matron Mother
Drow Shadowblade
Duergar Despot
Duergar Hammerer
Duergar Kavalrachni
Duergar Mind Master
Duergar Screamer
Duergar Soulblade
Duergar Stone Guard
Duergar Warlord
Duergar Xarrorn
Eidolon
Sacred Statue
Autumn Eladrin
Spring Eladrin
Summer Eladrin
Winter Eladrin
Leviathan
Phoenix
Elder Tempest
Zaratan
Air Elemental Myrmidon
Earth Elemental Myrmidon
Fire Elemental Myrmidon
Water Elemental Myrmidon
Giff
Githyanki Gish
Githyanki Kith'rak
Githyanki Supreme Commander
Githzerai Anarch
Githzerai Enlightened
Gray Render
Howler
Young Kruthik
Adult Kruthik
Kruthik Hive Lord
Marut
Meazel
Nagpa
Nightwalker
Oblex Spawn
Adult Oblex
Elder Oblex
Ogre Battering Ram
Ogre Bolt Launcher
Ogre Chain Brute
Ogre Howdah
Retriever
Frost Salamander
Gloom Weaver
Shadow Dancer
Soul Monger
Skulk
Skull Lord
The Angry
The Hungry
The Lonely
The Lost
The Wretched
Star Spawn Grue
Star Spawn Hulk
Star Spawn Larva Mage
Star Spawn Mangler
Star Spawn Seer
Female Steeder
Male Steeder
Steel Predator
Stone Cursed
Sword Wraith Commander
Sword Wraith Warrior
Tortle
Tortle Druid
Dire Troll
Rot Troll
Spirit Troll
Venom Troll
Vampiric Mist
Canoloth
Dhergoloth
Hydroloth
Merrenoloth
Oinoloth
Yagnoloth""".splitlines()

STATBLOCK_NAME_CORRECTIONS = {
    "0RCUS": "Orcus",
    "ADULT0BLEX": "Adult Oblex",
    "AIR ELEMENTA L M YRMIDON": "Air Elemental Myrmidon",
    "BONEC LAW": "Boneclaw",
    "COMMANDER": "Githyanki Supreme Commander",
    "D U ERGA R KAVALRACHNI": "Duergar Kavalrachni",
    "D UERGAR SCREAMER": "Duergar Screamer",
    "DROW HOUSE C APTAIN": "Drow House Captain",
    "DROW SHA DOW BLA DE": "Drow Shadowblade",
    "ELDER0BLEX": "Elder Oblex",
    "FRAZ- URB'LUU": "Fraz-Urb'luu",
    "HELLFIRE ENGIN E": "Hellfire Engine",
    "J UIBLEX": "Juiblex",
    "NAGP A": "Nagpa",
    "OGRE B ATT ERING R A M": "Ogre Battering Ram",
    "REDABISHAI": "Red Abishai",
    "SouLMONGER": "Soul Monger",
    "Z ARATAN": "Zaratan",
    "ZARI EL": "Zariel",
}


def _selector(heading: str, page: int, **extra: Any) -> dict[str, Any]:
    return {"heading_exact": heading, "page_start": page, **extra}


def _addition(
    kind: str,
    name: str,
    selectors: list[dict[str, Any]],
    card: dict[str, Any],
) -> dict[str, Any]:
    return {"kind": kind, "name": name, "source_selectors": selectors, "card": card}


def _feature(name: str, description: str) -> dict[str, str]:
    return {"name": name, "description": description}


def _spell_grant(
    name: str,
    level: int,
    ability: str,
    *,
    eligible_class: str = "Wizard",
    method: str = "limited_use",
    free_casts: int = 1,
    recovers_on: str | None = "long_rest",
    minimum_level: int = 1,
    allow_slot_cast: bool = False,
    ritual_only: bool = False,
    fixed_cast_level: int | None = None,
    ignore_components: bool = False,
    ignore_material_components: bool = False,
) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if fixed_cast_level is not None:
        overrides["fixed_cast_level"] = fixed_cast_level
    if ignore_components:
        overrides["ignore_components"] = True
    if ignore_material_components:
        overrides["ignore_material_components"] = True
    result: dict[str, Any] = {
        "name": name,
        "level": level,
        "eligible_classes": [eligible_class],
        "method": method,
        "spellcasting_ability": ability,
        "free_casts": free_casts,
        "recovers_on": recovers_on,
        "allow_slot_cast": allow_slot_cast,
        "minimum_level": minimum_level,
        "ritual_only": ritual_only,
    }
    if overrides:
        result["casting_overrides"] = overrides
    return result


def _cantrip(
    name: str,
    ability: str,
    *,
    eligible_class: str = "Wizard",
    ignore_components: bool = False,
) -> dict[str, Any]:
    return _spell_grant(
        name,
        0,
        ability,
        eligible_class=eligible_class,
        method="known",
        free_casts=0,
        recovers_on=None,
        ignore_components=ignore_components,
    )


def _species_card(
    *,
    base_species: str,
    abilities: dict[str, int],
    size: str,
    speed: int,
    languages: list[str],
    features: list[dict[str, str]],
    **grants: Any,
) -> dict[str, Any]:
    return {
        "base_species": base_species,
        **(
            {"replaces_base_traits": ["Ability Score Increase", "Infernal Legacy"]}
            if base_species == "Tiefling"
            else {}
        ),
        "grants": {
            "ability_score_increases": abilities,
            "size": size,
            "walk_speed": speed,
            "languages": languages,
            "features": features,
            "unresolved": [],
            **grants,
        },
    }


def _tiefling_additions() -> list[dict[str, Any]]:
    specs = [
        (
            "Tiefling (Baalzebul)",
            "BAALZEBUL",
            22,
            {"charisma": 2, "intelligence": 1},
            "Legacy of Maladomini",
            [
                _cantrip("Thaumaturgy", "charisma", eligible_class="Cleric"),
                _spell_grant("Ray of Sickness", 1, "charisma", minimum_level=3, fixed_cast_level=2),
                _spell_grant("Crown of Madness", 2, "charisma", minimum_level=5),
            ],
        ),
        (
            "Tiefling (Dispater)",
            "DISPATER",
            22,
            {"charisma": 2, "dexterity": 1},
            "Legacy of Dis",
            [
                _cantrip("Thaumaturgy", "charisma", eligible_class="Cleric"),
                _spell_grant("Disguise Self", 1, "charisma", minimum_level=3),
                _spell_grant("Detect Thoughts", 2, "charisma", minimum_level=5),
            ],
        ),
        (
            "Tiefling (Fierna)",
            "FI ERNA",
            22,
            {"charisma": 2, "wisdom": 1},
            "Legacy of Phlegethos",
            [
                _cantrip("Friends", "charisma"),
                _spell_grant("Charm Person", 1, "charisma", minimum_level=3, fixed_cast_level=2),
                _spell_grant("Suggestion", 2, "charisma", minimum_level=5),
            ],
        ),
        (
            "Tiefling (Glasya)",
            "GLASYA",
            23,
            {"charisma": 2, "dexterity": 1},
            "Legacy of Malbolge",
            [
                _cantrip("Minor Illusion", "charisma"),
                _spell_grant("Disguise Self", 1, "charisma", minimum_level=3),
                _spell_grant("Invisibility", 2, "charisma", minimum_level=5),
            ],
        ),
        (
            "Tiefling (Levistus)",
            "L EVISTUS",
            23,
            {"charisma": 2, "constitution": 1},
            "Legacy of Stygia",
            [
                _cantrip("Ray of Frost", "charisma"),
                _spell_grant(
                    "Armor of Agathys",
                    1,
                    "charisma",
                    eligible_class="Warlock",
                    minimum_level=3,
                    fixed_cast_level=2,
                ),
                _spell_grant("Darkness", 2, "charisma", minimum_level=5),
            ],
        ),
        (
            "Tiefling (Mammon)",
            "MAMMON",
            23,
            {"charisma": 2, "intelligence": 1},
            "Legacy of Minauros",
            [
                _cantrip("Mage Hand", "charisma"),
                _spell_grant(
                    "Tenser's Floating Disk",
                    1,
                    "charisma",
                    minimum_level=3,
                    recovers_on="short_rest",
                ),
                _spell_grant(
                    "Arcane Lock", 2, "charisma", minimum_level=5, ignore_material_components=True
                ),
            ],
        ),
        (
            "Tiefling (Mephistopheles)",
            "MEPHISTOPHELES",
            24,
            {"charisma": 2, "intelligence": 1},
            "Legacy of Cania",
            [
                _cantrip("Mage Hand", "charisma"),
                _spell_grant("Burning Hands", 1, "charisma", minimum_level=3, fixed_cast_level=2),
                _spell_grant("Flame Blade", 2, "charisma", eligible_class="Druid", minimum_level=5),
            ],
        ),
        (
            "Tiefling (Zariel)",
            "ZARIEL",
            24,
            {"charisma": 2, "strength": 1},
            "Legacy of Avernus",
            [
                _cantrip("Thaumaturgy", "charisma", eligible_class="Cleric"),
                _spell_grant(
                    "Searing Smite",
                    1,
                    "charisma",
                    eligible_class="Paladin",
                    minimum_level=3,
                    fixed_cast_level=2,
                ),
                _spell_grant(
                    "Branding Smite", 2, "charisma", eligible_class="Paladin", minimum_level=5
                ),
            ],
        ),
    ]
    result = []
    for name, heading, page, abilities, legacy, spells in specs:
        selectors = [_selector(heading, page)]
        if name == "Tiefling (Fierna)":
            selectors.append(
                _selector(
                    "CHAPTER I I T HE BLOOD WAR",
                    23,
                    content_contains="cast the charm per",
                )
            )
        result.append(
            _addition(
                "species",
                name,
                selectors,
                _species_card(
                    base_species="Tiefling",
                    abilities=abilities,
                    size="medium",
                    speed=30,
                    darkvision_ft=60,
                    languages=["Common", "Infernal"],
                    resistances=["fire"],
                    spell_grants=spells,
                    features=[
                        _feature("Hellish Resistance", "Resistance to fire damage."),
                        _feature(legacy, "Source-bound infernal spellcasting legacy."),
                    ],
                ),
            )
        )
    return result


def _elf_additions() -> list[dict[str, Any]]:
    base = {
        "size": "medium",
        "speed": 30,
        "darkvision_ft": 60,
        "languages": ["Common", "Elvish"],
        "skill_proficiencies": ["Perception"],
    }
    base_features = [
        _feature("Fey Ancestry", "Base elf charm and magical-sleep defenses."),
        _feature("Trance", "Base elf four-hour trance."),
    ]
    return [
        _addition(
            "species",
            "Eladrin",
            [_selector("ELADRIN TRAITS", 63)],
            _species_card(
                base_species="Elf",
                abilities={"dexterity": 2, "charisma": 1},
                narrative_choice_groups=[
                    {
                        "id": "season",
                        "count": 1,
                        "options": ["Autumn", "Winter", "Spring", "Summer"],
                    }
                ],
                resources={
                    "species:eladrin:fey_step": {
                        "label": "Fey Step",
                        "value": 1,
                        "max": 1,
                        "recovers_on": "short_rest",
                        "source_key": "Eladrin",
                    }
                },
                features=[
                    *base_features,
                    _feature(
                        "Fey Step",
                        "Bonus-action 30-foot teleport; the chosen season adds the source-bound 3rd-level effect.",
                    ),
                ],
                **base,
            ),
        ),
        _addition(
            "species",
            "Sea Elf",
            [_selector("SEA ELF TRAITS", 63)],
            _species_card(
                base_species="Elf",
                abilities={"dexterity": 2, "constitution": 1},
                swim_speed=30,
                weapon_proficiencies=["Spear", "Trident", "Light Crossbow", "Net"],
                features=[
                    *base_features,
                    _feature(
                        "Child of the Sea",
                        "Breathe air and water and gain a 30-foot swimming speed.",
                    ),
                    _feature(
                        "Friend of the Sea",
                        "Communicate simple ideas with beasts that have an innate swimming speed.",
                    ),
                ],
                **{**base, "languages": ["Common", "Elvish", "Aquan"]},
            ),
        ),
        _addition(
            "species",
            "Shadar-kai",
            [_selector("SHADAR- KAI TRAITS", 64)],
            _species_card(
                base_species="Elf",
                abilities={"dexterity": 2, "constitution": 1},
                resistances=["necrotic"],
                resources={
                    "species:shadar-kai:blessing": {
                        "label": "Blessing of the Raven Queen",
                        "value": 1,
                        "max": 1,
                        "recovers_on": "long_rest",
                        "source_key": "Shadar-kai",
                    }
                },
                features=[
                    *base_features,
                    _feature(
                        "Blessing of the Raven Queen",
                        "Bonus-action 30-foot teleport; from 3rd level its source-bound resistance lasts until the next turn.",
                    ),
                ],
                **base,
            ),
        ),
    ]


def _other_species_and_feat() -> list[dict[str, Any]]:
    no_components = {"ignore_components": True}
    return [
        _addition(
            "species",
            "Duergar",
            [_selector("DUERGAR TRAITS", 82)],
            _species_card(
                base_species="Dwarf",
                abilities={"constitution": 2, "strength": 1},
                size="medium",
                speed=25,
                darkvision_ft=120,
                languages=["Common", "Dwarvish", "Undercommon"],
                resistances=["poison"],
                weapon_proficiencies=["Battleaxe", "Handaxe", "Light Hammer", "Warhammer"],
                tool_choice_count=1,
                tool_options=["Smith's Tools", "Brewer's Supplies", "Mason's Tools"],
                spell_grants=[
                    _spell_grant(
                        "Enlarge/Reduce",
                        2,
                        "intelligence",
                        minimum_level=3,
                        ignore_material_components=True,
                    ),
                    _spell_grant(
                        "Invisibility",
                        2,
                        "intelligence",
                        minimum_level=5,
                        ignore_material_components=True,
                    ),
                ],
                features=[
                    _feature("Dwarven Resilience", "Base dwarf poison defenses."),
                    _feature("Stonecunning", "Base dwarf stonework knowledge."),
                    _feature(
                        "Duergar Resilience",
                        "Source-bound illusion, charm, and paralysis saving-throw advantage.",
                    ),
                    _feature(
                        "Duergar Magic",
                        "Source-bound self-only, enlarge-only, sunlight, and component restrictions apply to the granted spells.",
                    ),
                    _feature(
                        "Sunlight Sensitivity",
                        "Source-bound attack and sight-based Perception disadvantage in direct sunlight.",
                    ),
                ],
            ),
        ),
        _addition(
            "species",
            "Githyanki",
            [_selector("G ITH TRA ITS", 97), _selector("GITHYANKI", 97)],
            _species_card(
                base_species="Gith",
                abilities={"intelligence": 1, "strength": 2},
                size="medium",
                speed=30,
                languages=["Common", "Gith"],
                language_choice_count=1,
                language_options=[],
                allow_any_language=True,
                armor_proficiencies=["light armor", "medium armor"],
                weapon_proficiencies=["Shortsword", "Longsword", "Greatsword"],
                proficiency_choice_groups=[
                    {
                        "id": "decadent_mastery",
                        "count": 1,
                        "options": [
                            *[{"kind": "skill", "name": value} for value in SKILLS],
                            *[{"kind": "tool", "name": value} for value in TOOLS],
                        ],
                    }
                ],
                spell_grants=[
                    _cantrip("Mage Hand", "intelligence", ignore_components=True),
                    _spell_grant("Jump", 1, "intelligence", minimum_level=3, **no_components),
                    _spell_grant("Misty Step", 2, "intelligence", minimum_level=5, **no_components),
                ],
                features=[
                    _feature(
                        "Decadent Mastery", "Choose one language and one skill or tool proficiency."
                    ),
                    _feature("Martial Prodigy", "Reviewed armor and sword proficiencies."),
                    _feature(
                        "Githyanki Psionics",
                        "Granted spells require no components; Mage Hand is invisible.",
                    ),
                ],
            ),
        ),
        _addition(
            "species",
            "Githzerai",
            [_selector("G ITH TRA ITS", 97), _selector("GITHZERAI", 97)],
            _species_card(
                base_species="Gith",
                abilities={"intelligence": 1, "wisdom": 2},
                size="medium",
                speed=30,
                languages=["Common", "Gith"],
                spell_grants=[
                    _cantrip("Mage Hand", "wisdom", ignore_components=True),
                    _spell_grant("Shield", 1, "wisdom", minimum_level=3, **no_components),
                    _spell_grant("Detect Thoughts", 2, "wisdom", minimum_level=5, **no_components),
                ],
                features=[
                    _feature(
                        "Mental Discipline",
                        "Advantage on saving throws against charmed and frightened.",
                    ),
                    _feature(
                        "Githzerai Psionics",
                        "Granted spells require no components; Mage Hand is invisible.",
                    ),
                ],
            ),
        ),
        _addition(
            "species",
            "Deep Gnome",
            [_selector("D EEP GNOME TRAITS", 114)],
            _species_card(
                base_species="Gnome",
                abilities={"intelligence": 2, "dexterity": 1},
                size="small",
                speed=25,
                darkvision_ft=120,
                languages=["Common", "Gnomish", "Undercommon"],
                features=[
                    _feature(
                        "Gnome Cunning", "Base gnome mental saving-throw advantage against magic."
                    ),
                    _feature(
                        "Stone Camouflage",
                        "Advantage on Stealth checks to hide in rocky terrain and underground.",
                    ),
                ],
            ),
        ),
        _addition(
            "feat",
            "Svirfneblin Magic",
            [{"heading_contains": "O PTIONAL DEEP GNO", "page_start": 115}],
            {
                "prerequisites": [{"kind": "species_required", "species": ["Deep Gnome"]}],
                "repeatable": False,
                "selection_requirements": None,
                "mechanical_grants": {
                    "ability_score_increases": {},
                    "maximum_ability_score": 20,
                    "languages": [],
                    "tool_proficiencies": [],
                    "weapon_proficiencies": [],
                    "spell_grants": [
                        _spell_grant(
                            "Nondetection",
                            3,
                            "intelligence",
                            method="at_will",
                            free_casts=0,
                            recovers_on=None,
                            ignore_material_components=True,
                        ),
                        _spell_grant("Blindness/Deafness", 2, "intelligence"),
                        _spell_grant("Blur", 2, "intelligence"),
                        _spell_grant("Disguise Self", 1, "intelligence"),
                    ],
                },
                "choices": {"source_effect": "Nondetection targets only the character."},
            },
        ),
    ]


def _items() -> list[dict[str, Any]]:
    return [
        _addition(
            "item",
            "Greater Silver Sword",
            [_selector("M AC IC ITEM: GREATER SILVER SWORD", 90, match_all=True)],
            {
                "inventory_template": {
                    "name": "Greater Silver Sword",
                    "kind": "weapon",
                    "quantity": 1,
                    "description": "Legendary attunement weapon for a psionic creature. It grants the exact source-bound mental defenses and astral-cord critical-hit option.",
                    "attunement": "required",
                    "mechanics": {
                        "category": "martial",
                        "attack_type": "melee",
                        "attack_ability": "strength",
                        "damage_formula": "2d6",
                        "damage_type": "slashing",
                        "properties": ["heavy", "two-handed"],
                        "magic_bonus": 3,
                    },
                }
            },
        ),
        _addition(
            "item",
            "Infernal Tack",
            [_selector("M AG IC ITEM: INFE RNAL T ACK", 168)],
            {
                "inventory_template": {
                    "name": "Infernal Tack",
                    "kind": "magic_item",
                    "quantity": 1,
                    "description": "Legendary evil-attunement tack. Nightmare binding, summoning, dismissal, death, and reformation remain exact source-bound Agent-as-DM context.",
                    "attunement": "required",
                    "mechanics": {},
                }
            },
        ),
    ]


def _runtime_probes() -> list[dict[str, Any]]:
    return [
        {
            "name": "mtof-mephistopheles-fixed-upcast",
            "level": 5,
            "steps": [
                {
                    "kind": "species",
                    "name": "Tiefling (Mephistopheles)",
                    "selection": {},
                    "expect": [
                        {"path": "sheet.abilities.charisma.score", "equals": 12},
                        {"path": "sheet.abilities.intelligence.score", "equals": 11},
                        {
                            "path": "sheet.content.spells",
                            "contains_names": ["Mage Hand", "Burning Hands", "Flame Blade"],
                        },
                    ],
                }
            ],
        },
        {
            "name": "mtof-eladrin-season-and-resource",
            "steps": [
                {
                    "kind": "species",
                    "name": "Eladrin",
                    "selection": {"feature_choices": {"season": ["Autumn"]}},
                    "expect": [
                        {"path": "sheet.abilities.dexterity.score", "equals": 12},
                        {"path": "sheet.resources.species:eladrin:fey_step.max", "equals": 1},
                    ],
                }
            ],
        },
        {
            "name": "mtof-githyanki-mixed-mastery",
            "level": 5,
            "steps": [
                {
                    "kind": "species",
                    "name": "Githyanki",
                    "selection": {
                        "languages": ["Draconic"],
                        "proficiency_choices": {
                            "decadent_mastery": [{"kind": "skill", "name": "Arcana"}]
                        },
                    },
                    "expect": [
                        {"path": "sheet.skills.arcana.proficiency", "equals": "proficient"},
                        {"path": "sheet.traits.proficiencies.armor", "contains": "medium armor"},
                        {
                            "path": "sheet.content.spells",
                            "contains_names": ["Mage Hand", "Jump", "Misty Step"],
                        },
                    ],
                }
            ],
        },
        {
            "name": "mtof-svirfneblin-at-will",
            "level": 5,
            "steps": [
                {"kind": "species", "name": "Deep Gnome", "selection": {}},
                {
                    "kind": "feat",
                    "name": "Svirfneblin Magic",
                    "selection": {},
                    "expect": [
                        {
                            "path": "sheet.content.spells",
                            "contains_names": [
                                "Nondetection",
                                "Blindness/Deafness",
                                "Blur",
                                "Disguise Self",
                            ],
                        },
                    ],
                },
            ],
        },
        {
            "name": "mtof-greater-silver-sword",
            "steps": [
                {
                    "kind": "item",
                    "name": "Greater Silver Sword",
                    "selection": {},
                    "expect": [
                        {
                            "path": "sheet.inventory.items",
                            "contains_names": ["Greater Silver Sword"],
                        },
                    ],
                }
            ],
        },
    ]


def main() -> None:
    additions = [
        *_tiefling_additions(),
        *_elf_additions(),
        *_other_species_and_feat(),
        *_items(),
    ]
    document = {
        "complete_review": True,
        "default_status": "rejected",
        "addition_default_status": "accepted",
        "default_status_by_kind": {"statblock": "accepted"},
        "rationale": (
            "Agent reviewed all player-facing lineages, the optional deep-gnome feat, both "
            "magic items, and every bestiary statblock against the indexed source. Lore, "
            "tables, monster customization procedures, and lair text remain searchable "
            "source context rather than false character options."
        ),
        "expected_counts": {
            "feat": 1,
            "item": 2,
            "species": 15,
            "statblock": len(STATBLOCK_NAMES),
        },
        "expected_actor_names": STATBLOCK_NAMES,
        "statblock_slot_reviews": [
            {
                "page_number": 170,
                "statblock_slot": 1,
                "name": "Orthon",
                "expected_identity": "Large fiend (devil), lawful evil",
                "ocr_corrections": {
                    "abilities": {
                        "str": "22 (+6)",
                        "dex": "16 (+3)",
                        "con": "21 (+5)",
                        "int": "15 (+2)",
                        "wis": "15 (+2)",
                        "cha": "16 (+3)",
                    }
                },
                "correction_evidence_basis": "rendered_page",
                "rendered_image_checksum": (
                    "984b5f0dec025c01667f0dce4a1b47136d58369ec9033d51e151ae71e0a751ac"
                ),
                "note": (
                    "Agent read the six ability cells from the rendered Orthon statblock; "
                    "the text layer confused the printed +5 modifier with +S."
                ),
            }
        ],
        "runtime_probes": _runtime_probes(),
        "decisions": [
            *[
                {
                    "kind": "statblock",
                    "name": extracted_name,
                    "status": "accepted",
                    "artifact_patch": {"card": {"name": canonical_name}},
                    "note": (
                        "Agent reviewed the same-page identity and corrected only OCR spacing, "
                        "decorative glyphs, or a split display heading; mechanics remain unchanged."
                    ),
                }
                for extracted_name, canonical_name in STATBLOCK_NAME_CORRECTIONS.items()
            ],
        ],
        "additions": additions,
    }
    payload = {"version": 1, "documents": {BOOK: document}}
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT} with {len(additions)} source-bound additions")


if __name__ == "__main__":
    main()
