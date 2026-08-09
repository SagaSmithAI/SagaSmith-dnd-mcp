"""Build the source-reviewed Volo's Guide to Monsters addon fixture."""

# ruff: noqa: E501 -- source-facing names and audited rules stay legible.

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sagasmith_core.documents import RapidOcrProvider
from sagasmith_dnd.statblocks import (
    parse_2014_statblock,
    recover_2014_statblock_from_ocr,
)

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
OUTPUT = ROOT / "fixtures" / "books_catalog_review_volo_v1.json"
BOOK = "D&D 5E - Volo's Guide to Monsters.pdf"
SOURCE = WORKSPACE / "reference" / "DnD-Books" / "5e" / "Books" / BOOK
OCR_CACHE = ROOT / "tmp" / "books-normalized-v25" / "ocr-page-cache"

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
MARTIAL_WEAPONS = [
    "Battleaxe",
    "Flail",
    "Glaive",
    "Greataxe",
    "Greatsword",
    "Halberd",
    "Lance",
    "Longsword",
    "Maul",
    "Morningstar",
    "Pike",
    "Rapier",
    "Scimitar",
    "Shortsword",
    "Trident",
    "War Pick",
    "Warhammer",
    "Whip",
    "Blowgun",
    "Hand Crossbow",
    "Heavy Crossbow",
    "Longbow",
    "Net",
]

STATBLOCK_NAMES = """Banderhobb
Barghest
Death Kiss
Gauth
Gazer
Bodak
Boggle
Catoblepas
Cave Fisher
Chitine
Choldrith
Cranium Rat
Swarm of Cranium Rats
Darkling
Darkling Elder
Deep Scion
Babau
Maw Demon
Shoosuva
Devourer
Dimetrodon
Brontosaurus
Deinonychus
Hadrosaurus
Quetzalcoatlus
Stegosaurus
Velociraptor
Draegloth
Firenewt Warrior
Firenewt Warlock of Imix
Giant Strider
Flail Snail
Froghemoth
Cloud Giant Smiling One
Fire Giant Dreadnought
Frost Giant Everlasting One
Mouth of Grolantor
Stone Giant Dreamwalker
Storm Giant Quintessent
Girallon
Flind
Gnoll Flesh Gnawer
Gnoll Hunter
Gnoll Witherling
Grung
Grung Elite Warrior
Grung Wildling
Guard Drake
Annis Hag
Bheur Hag
Hobgoblin Devastator
Hobgoblin Iron Shadow
Ki-rin
Kobold Dragonshield
Kobold Inventor
Kobold Scale Sorcerer
Korred
Leucrotta
Meenlock
Alhoon
Elder Brain
Ulitharid
Mindwitness
Morkoth
Neogi Hatchling
Neogi
Neogi Master
Neothelid
Nilbog
Orc Blade of Ilneval
Orc Claw of Luthic
Orc Hand of Yurtrus
Orc Nurtured One of Yurtrus
Orc Red Fang of Shargaas
Tanarukk
Quickling
Redcap
Sea Spawn
Shadow Mastiff
Slithering Tracker
Spawn of Kyuss
Tlincalli
Trapper
Vargouille
Vegepygmy
Vegepygmy Chief
Thorny
Wood Woad
Xvart
Xvart Warlock of Raxivort
Yeth Hound
Yuan-ti Anathema
Yuan-ti Broodguard
Yuan-ti Mind Whisperer
Yuan-ti Nightmare Speaker
Yuan-ti Pit Master
Aurochs
Cow
Dolphin
Swarm of Rot Grubs
Apprentice Wizard
Abjurer
Archer
Archdruid
Bard
Blackguard
Conjurer
Champion
Diviner
Enchanter
Evoker
Illusionist
Kraken Priest
Martial Arts Adept
Master Thief
Necromancer
Swashbuckler
Transmuter
War Priest
Warlord
Warlock of the Archfey
Warlock of the Fiend
Warlock of the Great Old One""".splitlines()

STATBLOCK_NAME_CORRECTIONS = {
    "!LINCALLI": "Tlincalli",
    "BANDBRHOBB": "Banderhobb",
    "SWARM OF CrANIUM RATS": "Swarm of Cranium Rats",
    "FIRENEWTWARLOCK OF IMIX": "Firenewt Warlock of Imix",
    "BHEURHAG": "Bheur Hag",
    "0RC BLADE OF ILNEVAL": "Orc Blade of Ilneval",
    "0RC CLAW OF LUTHIC": "Orc Claw of Luthic",
    "ORO HAND OF YURTRUS": "Orc Hand of Yurtrus",
    "ORO NURTURED ONE OF YURTRUS": "Orc Nurtured One of Yurtrus",
    "ORO RED FANG OF SHARGAAS": "Orc Red Fang of Shargaas",
    "'TRAPPER": "Trapper",
    "YUAN-TIBROODGUARD": "Yuan-ti Broodguard",
    "Swarm OF Rot GrubS": "Swarm of Rot Grubs",
    "AR.CHER": "Archer",
    "CONJVRER": "Conjurer",
}

MISSING_STATBLOCKS = {
    "Aurochs": (208, "manual", 0.0),
    "Cow": (208, "medium", 3.0),
    "Mindwitness": (177, "medium", 3.0),
    "Shadow Mastiff": (191, "medium", 3.0),
    "Yuan-ti Anathema": (203, "medium", 3.0),
    "Evoker": (215, "small", 3.0),
    "Illusionist": (215, "medium", 3.0),
    "Bard": (212, "manual", 0.0),
    "Warlock of the Archfey": (220, "manual", 0.0),
    "Warlock of the Fiend": (220, "medium", 3.0),
}

EXPECTED_CRITICAL = {
    "Cow": (10, 15, [18, 10, 14, 2, 10, 4]),
    "Mindwitness": (15, 75, [10, 14, 14, 15, 15, 10]),
    "Shadow Mastiff": (12, 33, [16, 14, 13, 5, 12, 5]),
    "Yuan-ti Anathema": (16, 189, [23, 13, 19, 19, 17, 20]),
    "Evoker": (12, 66, [9, 14, 12, 17, 12, 11]),
    "Illusionist": (12, 38, [9, 14, 13, 16, 11, 12]),
    "Warlock of the Fiend": (12, 78, [10, 14, 15, 12, 12, 18]),
}


def _selector(
    heading: str,
    page: int,
    *,
    exact: bool = False,
    match_all: bool = False,
) -> dict[str, Any]:
    return {
        ("heading_exact" if exact else "heading_contains"): heading,
        "page_start": page,
        **({"match_all": True} if match_all else {}),
    }


def _addition(
    kind: str,
    name: str,
    selectors: list[dict[str, Any]],
    card: dict[str, Any],
    *,
    replace_existing: bool = False,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "name": name,
        "source_selectors": selectors,
        "card": card,
        **({"replace_existing": True} if replace_existing else {}),
        "note": "Agent reviewed this exact source-bound entry and filled its portable runtime card.",
    }


def _feature(name: str, description: str, *, minimum_level: int = 1) -> dict[str, Any]:
    return {"name": name, "description": description, "minimum_level": minimum_level}


def _spell_grant(
    name: str,
    level: int,
    ability: str,
    *,
    eligible_class: str,
    method: str = "limited_use",
    free_casts: int = 1,
    recovers_on: str | None = "long_rest",
    minimum_level: int = 1,
    resource_group: str | None = None,
) -> dict[str, Any]:
    result = {
        "name": name,
        "level": level,
        "eligible_classes": [eligible_class],
        "method": method,
        "spellcasting_ability": ability,
        "free_casts": free_casts,
        "recovers_on": recovers_on,
        "allow_slot_cast": False,
        "minimum_level": minimum_level,
        "ritual_only": False,
    }
    if resource_group:
        result["resource_group"] = resource_group
    return result


def _cantrip(name: str, ability: str, *, eligible_class: str) -> dict[str, Any]:
    return _spell_grant(
        name,
        0,
        ability,
        eligible_class=eligible_class,
        method="known",
        free_casts=0,
        recovers_on=None,
    )


def _resource(key: str, label: str, recovery: str) -> dict[str, Any]:
    return {
        key: {
            "label": label,
            "value": 1,
            "max": 1,
            "recovers_on": recovery,
            "source_key": label,
        }
    }


def _species_card(
    *,
    base_species: str,
    abilities: dict[str, int],
    size: str,
    speed: int,
    languages: list[str],
    features: list[dict[str, Any]],
    **grants: Any,
) -> dict[str, Any]:
    return {
        "base_species": base_species,
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


def _aasimar(
    name: str, heading: str, page: int, abilities: dict[str, int], feature: dict[str, Any]
) -> dict[str, Any]:
    return _addition(
        "species",
        name,
        [_selector("AASIMAR TRAITS", 105), _selector(heading, page)],
        _species_card(
            base_species="Aasimar",
            abilities=abilities,
            size="medium",
            speed=30,
            darkvision_ft=60,
            languages=["Common", "Celestial"],
            resistances=["necrotic", "radiant"],
            spell_grants=[_cantrip("Light", "charisma", eligible_class="Cleric")],
            resources=_resource("species:aasimar:healing_hands", "Healing Hands", "long_rest")
            | _resource(
                f"species:aasimar:{name.casefold().split()[0]}_revelation",
                feature["name"],
                "long_rest",
            ),
            features=[
                _feature("Darkvision", "Darkvision out to 60 feet."),
                _feature("Celestial Resistance", "Resistance to necrotic and radiant damage."),
                _feature(
                    "Healing Hands",
                    "As an action once per long rest, touch a creature to restore hit points equal to character level.",
                ),
                _feature(
                    "Light Bearer", "Know the Light cantrip; Charisma is the spellcasting ability."
                ),
                feature,
            ],
        ),
    )


def _species_additions() -> list[dict[str, Any]]:
    return [
        _aasimar(
            "Protector Aasimar",
            "PROTECTOR AASIMAR",
            106,
            {"charisma": 2, "wisdom": 1},
            _feature(
                "Radiant Soul",
                "From 3rd level, use an action once per long rest to transform for 1 minute: gain a 30-foot flying speed and once on each turn deal extra radiant damage equal to character level to one damaged target.",
                minimum_level=3,
            ),
        ),
        _aasimar(
            "Scourge Aasimar",
            "SCOURGE AASIMAR",
            106,
            {"charisma": 2, "constitution": 1},
            _feature(
                "Radiant Consumption",
                "From 3rd level, use an action once per long rest to radiate for 1 minute. At each turn end, the character and creatures within 10 feet take the source radiant damage; once on each turn one damaged target takes extra radiant damage equal to character level.",
                minimum_level=3,
            ),
        ),
        _aasimar(
            "Fallen Aasimar",
            "FALLEN AASIMAR",
            106,
            {"charisma": 2, "strength": 1},
            _feature(
                "Necrotic Shroud",
                "From 3rd level, use an action once per long rest to transform for 1 minute. Nearby creatures make the source Charisma save against fright, and once on each turn one damaged target takes extra necrotic damage equal to character level.",
                minimum_level=3,
            ),
        ),
        _addition(
            "species",
            "Firbolg",
            [_selector("FIRBOLG TRAITS", 108, match_all=True)],
            _species_card(
                base_species="Firbolg",
                abilities={"wisdom": 2, "strength": 1},
                size="medium",
                speed=30,
                languages=["Common", "Elvish", "Giant"],
                spell_grants=[
                    _spell_grant(
                        "Detect Magic",
                        1,
                        "wisdom",
                        eligible_class="Wizard",
                        recovers_on="short_rest",
                        resource_group="Firbolg Magic",
                    ),
                    _spell_grant(
                        "Disguise Self",
                        1,
                        "wisdom",
                        eligible_class="Wizard",
                        recovers_on="short_rest",
                        resource_group="Firbolg Magic",
                    ),
                ],
                resources=_resource("species:firbolg:hidden_step", "Hidden Step", "short_rest"),
                features=[
                    _feature(
                        "Firbolg Magic",
                        "Cast Detect Magic or Disguise Self without material components; the shared trait use recovers after a short or long rest. Disguise Self can make the firbolg appear up to 3 feet shorter.",
                    ),
                    _feature(
                        "Hidden Step",
                        "As a bonus action once per short or long rest, turn invisible until the start of the next turn or until attacking, dealing damage, or forcing a save.",
                    ),
                    _feature(
                        "Powerful Build",
                        "Count as one size larger for carrying capacity and push, drag, or lift limits.",
                    ),
                    _feature(
                        "Speech of Beast and Leaf",
                        "Communicate in a limited manner with beasts and plants and gain advantage on Charisma checks to influence them.",
                    ),
                ],
            ),
        ),
        _addition(
            "species",
            "Goliath",
            [_selector("GOLIATH TRAITS", 110)],
            _species_card(
                base_species="Goliath",
                abilities={"strength": 2, "constitution": 1},
                size="medium",
                speed=30,
                languages=["Common", "Giant"],
                skill_proficiencies=["Athletics"],
                resources=_resource(
                    "species:goliath:stone_endurance", "Stone's Endurance", "short_rest"
                ),
                features=[
                    _feature("Natural Athlete", "Proficiency in Athletics."),
                    _feature(
                        "Stone's Endurance",
                        "As a reaction once per short or long rest when taking damage, reduce it by 1d12 plus Constitution modifier.",
                    ),
                    _feature(
                        "Powerful Build",
                        "Count as one size larger for carrying capacity and push, drag, or lift limits.",
                    ),
                    _feature(
                        "Mountain Born",
                        "Acclimated to high altitude and naturally adapted to cold climates as described by the source.",
                    ),
                ],
            ),
        ),
        _addition(
            "species",
            "Kenku",
            [_selector("KENKU TRAITS", 112)],
            _species_card(
                base_species="Kenku",
                abilities={"dexterity": 2, "wisdom": 1},
                size="medium",
                speed=30,
                languages=["Common", "Auran"],
                skill_choice_count=2,
                skill_options=["Acrobatics", "Deception", "Stealth", "Sleight of Hand"],
                features=[
                    _feature(
                        "Expert Forgery",
                        "Advantage on checks to produce forgeries or duplicates of existing handwriting and craftwork.",
                    ),
                    _feature("Kenku Training", "Choose two source-listed skill proficiencies."),
                    _feature(
                        "Mimicry",
                        "Mimic heard sounds and voices; Insight opposed by Charisma (Deception) can identify an imitation.",
                    ),
                    _feature(
                        "Languages",
                        "Read and write Common and Auran, but speak only by using Mimicry.",
                    ),
                ],
            ),
        ),
        _addition(
            "species",
            "Lizardfolk",
            [_selector("IZARDFOLK TRAITS", 114, match_all=True)],
            _species_card(
                base_species="Lizardfolk",
                abilities={"constitution": 2, "wisdom": 1},
                size="medium",
                speed=30,
                swim_speed=30,
                natural_armor_base=13,
                languages=["Common", "Draconic"],
                skill_choice_count=2,
                skill_options=["Animal Handling", "Nature", "Perception", "Stealth", "Survival"],
                natural_weapons=[
                    {
                        "name": "Bite",
                        "attack_ability": "strength",
                        "damage_formula": "1d6",
                        "damage_type": "piercing",
                        "reach_ft": 5,
                        "description": "A reviewed natural weapon usable for an unarmed strike.",
                    }
                ],
                resources=_resource("species:lizardfolk:hungry_jaws", "Hungry Jaws", "short_rest"),
                features=[
                    _feature(
                        "Bite",
                        "Natural weapon: an unarmed strike deals 1d6 + Strength modifier piercing damage.",
                    ),
                    _feature(
                        "Cunning Artisan",
                        "During a short rest, harvest a suitable Small-or-larger corpse with a blade or tools to make the exact source-listed shield, club, javelin, darts, or blowgun needles.",
                    ),
                    _feature("Hold Breath", "Hold breath for up to 15 minutes."),
                    _feature("Hunter's Lore", "Choose two source-listed skill proficiencies."),
                    _feature(
                        "Natural Armor",
                        "While not wearing armor, AC is 13 + Dexterity modifier; a shield applies and this formula can replace worse worn armor.",
                    ),
                    _feature(
                        "Hungry Jaws",
                        "As a bonus action once per short or long rest, make a bite attack and on a hit gain temporary hit points equal to Constitution modifier, minimum 1.",
                    ),
                ],
            ),
        ),
        _addition(
            "species",
            "Tabaxi",
            [_selector("ABAXI TRAITS", 116, match_all=True)],
            _species_card(
                base_species="Tabaxi",
                abilities={"dexterity": 2, "charisma": 1},
                size="medium",
                speed=30,
                darkvision_ft=60,
                languages=["Common"],
                language_choice_count=1,
                language_options=[],
                allow_any_language=True,
                skill_proficiencies=["Perception", "Stealth"],
                natural_weapons=[
                    {
                        "name": "Claws",
                        "attack_ability": "strength",
                        "damage_formula": "1d4",
                        "damage_type": "slashing",
                        "reach_ft": 5,
                        "description": "A reviewed natural weapon usable for an unarmed strike.",
                    }
                ],
                features=[
                    _feature(
                        "Feline Agility",
                        "When moving on a turn in combat, double speed until turn end; regain the trait after moving 0 feet on one turn.",
                    ),
                    _feature(
                        "Cat's Claws",
                        "Climb speed 20 feet; claws are natural weapons whose unarmed strikes deal 1d4 + Strength modifier slashing damage.",
                    ),
                    _feature("Cat's Talent", "Proficiency in Perception and Stealth."),
                ],
            ),
        ),
        _addition(
            "species",
            "Triton",
            [_selector("TRITON TRAITS", 118)],
            _species_card(
                base_species="Triton",
                abilities={"strength": 1, "constitution": 1, "charisma": 1},
                size="medium",
                speed=30,
                swim_speed=30,
                languages=["Common", "Primordial"],
                resistances=["cold"],
                spell_grants=[
                    _spell_grant(
                        "Fog Cloud",
                        1,
                        "charisma",
                        eligible_class="Druid",
                        resource_group="Control Air and Water",
                    ),
                    _spell_grant(
                        "Gust of Wind",
                        2,
                        "charisma",
                        eligible_class="Druid",
                        minimum_level=3,
                        resource_group="Control Air and Water",
                    ),
                    _spell_grant(
                        "Wall of Water",
                        3,
                        "charisma",
                        eligible_class="Druid",
                        minimum_level=5,
                        resource_group="Control Air and Water",
                    ),
                ],
                features=[
                    _feature("Amphibious", "Breathe air and water."),
                    _feature(
                        "Control Air and Water",
                        "The three level-gated spells share one use that recovers after a long rest; Charisma is the spellcasting ability.",
                    ),
                    _feature(
                        "Emissary of the Sea",
                        "Communicate simple ideas to beasts that can breathe water; this does not grant reciprocal understanding.",
                    ),
                    _feature(
                        "Guardians of the Depths",
                        "Resistance to cold damage and ignore source-defined drawbacks of deep underwater environments.",
                    ),
                ],
            ),
        ),
        _addition(
            "species",
            "Bugbear",
            [_selector("BUGBEAR TRAITS", 120)],
            _species_card(
                base_species="Bugbear",
                abilities={"strength": 2, "dexterity": 1},
                size="medium",
                speed=30,
                darkvision_ft=60,
                languages=["Common", "Goblin"],
                skill_proficiencies=["Stealth"],
                features=[
                    _feature(
                        "Long-Limbed",
                        "Melee attack reach is 5 feet greater when making the attack on the bugbear's turn.",
                    ),
                    _feature(
                        "Powerful Build",
                        "Count as one size larger for carrying capacity and push, drag, or lift limits.",
                    ),
                    _feature("Sneaky", "Proficiency in Stealth."),
                    _feature(
                        "Surprise Attack",
                        "Once per combat, a hit against a surprised creature on the bugbear's first turn deals an extra 2d6 damage.",
                    ),
                ],
            ),
        ),
        _addition(
            "species",
            "Goblin",
            [_selector("GOBLIN TRAITS", 120, exact=True)],
            _species_card(
                base_species="Goblin",
                abilities={"dexterity": 2, "constitution": 1},
                size="small",
                speed=30,
                darkvision_ft=60,
                languages=["Common", "Goblin"],
                resources=_resource(
                    "species:goblin:fury_of_the_small", "Fury of the Small", "short_rest"
                ),
                features=[
                    _feature(
                        "Fury of the Small",
                        "Once per short or long rest when an attack or spell damages a creature larger than the goblin, deal extra damage equal to character level.",
                    ),
                    _feature(
                        "Nimble Escape",
                        "Take the Disengage or Hide action as a bonus action on each turn.",
                    ),
                ],
            ),
        ),
        _addition(
            "species",
            "Hobgoblin",
            [_selector("HOBGOBLIN TRAITS", 120)],
            _species_card(
                base_species="Hobgoblin",
                abilities={"constitution": 2, "intelligence": 1},
                size="medium",
                speed=30,
                darkvision_ft=60,
                languages=["Common", "Goblin"],
                armor_proficiencies=["Light Armor"],
                proficiency_choice_groups=[
                    {
                        "id": "martial_training",
                        "count": 2,
                        "options": [{"kind": "weapon", "name": name} for name in MARTIAL_WEAPONS],
                    }
                ],
                resources=_resource("species:hobgoblin:saving_face", "Saving Face", "short_rest"),
                features=[
                    _feature(
                        "Martial Training",
                        "Choose proficiency with two martial weapons and gain light armor proficiency.",
                    ),
                    _feature(
                        "Saving Face",
                        "Once per short or long rest after missing an attack or failing a check or save, add the number of visible allies within 30 feet, maximum +5.",
                    ),
                ],
            ),
        ),
        _addition(
            "species",
            "Kobold",
            [_selector("KOBOLD TRAITS", 120)],
            _species_card(
                base_species="Kobold",
                abilities={"dexterity": 2},
                ability_score_decreases={"strength": 2},
                size="small",
                speed=30,
                darkvision_ft=60,
                languages=["Common", "Draconic"],
                resources=_resource(
                    "species:kobold:grovel", "Grovel, Cower, and Beg", "short_rest"
                ),
                features=[
                    _feature(
                        "Grovel, Cower, and Beg",
                        "As an action once per short or long rest, allies gain advantage until the end of the kobold's next turn against enemies within 10 feet that can see the kobold.",
                    ),
                    _feature(
                        "Pack Tactics",
                        "Advantage on an attack when a non-incapacitated ally is within 5 feet of the target.",
                    ),
                    _feature(
                        "Sunlight Sensitivity",
                        "Disadvantage on attacks and sight-based Perception checks in the exact direct-sunlight circumstances given by the source.",
                    ),
                ],
            ),
        ),
        _addition(
            "species",
            "Orc",
            [_selector("0RC TRAITS", 121)],
            _species_card(
                base_species="Orc",
                abilities={"strength": 2, "constitution": 1},
                ability_score_decreases={"intelligence": 2},
                size="medium",
                speed=30,
                darkvision_ft=60,
                languages=["Common", "Orc"],
                skill_proficiencies=["Intimidation"],
                features=[
                    _feature(
                        "Aggressive",
                        "As a bonus action, move up to speed toward a visible or audible enemy and finish closer than the starting position.",
                    ),
                    _feature("Menacing", "Proficiency in Intimidation."),
                    _feature(
                        "Powerful Build",
                        "Count as one size larger for carrying capacity and push, drag, or lift limits.",
                    ),
                ],
            ),
        ),
        _addition(
            "species",
            "Yuan-ti Pureblood",
            [_selector("PUREBLOOD TRAITS", 121)],
            _species_card(
                base_species="Yuan-ti Pureblood",
                abilities={"charisma": 2, "intelligence": 1},
                size="medium",
                speed=30,
                darkvision_ft=60,
                languages=["Common", "Abyssal", "Draconic"],
                immunities=["poison"],
                condition_immunities=["poisoned"],
                spell_grants=[
                    _cantrip("Poison Spray", "charisma", eligible_class="Sorcerer"),
                    _spell_grant(
                        "Animal Friendship",
                        1,
                        "charisma",
                        eligible_class="Druid",
                        method="at_will",
                        free_casts=0,
                        recovers_on=None,
                    ),
                    _spell_grant(
                        "Suggestion", 2, "charisma", eligible_class="Sorcerer", minimum_level=3
                    ),
                ],
                features=[
                    _feature(
                        "Innate Spellcasting",
                        "Poison Spray is known; Animal Friendship is at will but can target only snakes; Suggestion is available from 3rd level once per long rest. Charisma is the spellcasting ability.",
                    ),
                    _feature(
                        "Magic Resistance",
                        "Advantage on saving throws against spells and other magical effects.",
                    ),
                    _feature(
                        "Poison Immunity", "Immune to poison damage and the poisoned condition."
                    ),
                ],
            ),
        ),
    ]


def _wall_of_water() -> dict[str, Any]:
    return _addition(
        "spell",
        "Wall of Water",
        [_selector("SPELL: WALL OF WATER", 117)],
        {
            "classes": ["Druid", "Sorcerer", "Wizard"],
            "level": 3,
            "definition": {
                "school": "evocation",
                "casting_time": "1 action",
                "range": {
                    "kind": "distance",
                    "normal_ft": 120,
                    "long_ft": 0,
                    "area": "source wall or ring",
                },
                "duration": {"kind": "timed", "value": 10, "unit": "minute", "concentration": True},
                "components": {
                    "verbal": True,
                    "somatic": True,
                    "material": True,
                    "material_description": "a drop of water",
                    "material_cost_cp": 0,
                    "consumed": False,
                },
                "effect": "Create the source-sized wall or ring of water. Its space is difficult terrain, ranged weapon attacks through it have disadvantage, fire damage passing through it is halved, and cold damage freezes a struck 5-foot section with the source AC and hit points until destroyed.",
            },
        },
    )


def _aurochs_source() -> str:
    return """# Aurochs

*Large beast, unaligned*

**Armor Class** 11 (natural armor)
**Hit Points** 38 (4d10 + 16)
**Speed** 50 ft.

| STR | DEX | CON | INT | WIS | CHA |
|---:|---:|---:|---:|---:|---:|
| 20 (+5) | 10 (+0) | 19 (+4) | 2 (-4) | 12 (+1) | 5 (-3) |

**Senses** passive Perception 11
**Languages** —
**Challenge** 2 (450 XP)

## Traits

***Charge.*** If the aurochs moves at least 20 feet straight toward a target and then hits it with a gore attack on the same turn, the target takes an extra 9 (2d8) piercing damage. If the target is a creature, it must succeed on a DC 15 Strength saving throw or be knocked prone.

## Actions

***Gore.*** Melee Weapon Attack: +7 to hit, reach 5 ft., one target. Hit: 14 (2d8 + 5) piercing damage.
""".strip()


def _bard_source() -> str:
    return """# Bard

*Medium humanoid (any race), any alignment*

**Armor Class** 15 (chain shirt)
**Hit Points** 44 (8d8 + 8)
**Speed** 30 ft.

| STR | DEX | CON | INT | WIS | CHA |
|---:|---:|---:|---:|---:|---:|
| 11 (+0) | 14 (+2) | 12 (+1) | 10 (+0) | 13 (+1) | 14 (+2) |

**Saving Throws** Dex +4, Wis +3
**Skills** Acrobatics +4, Perception +5, Performance +6
**Senses** passive Perception 15
**Languages** any two languages
**Challenge** 2 (450 XP)

## Traits

***Spellcasting.*** The bard is a 4th-level spellcaster. Its spellcasting ability is Charisma (spell save DC 12, +4 to hit with spell attacks). It has the following bard spells prepared:

Cantrips (at will): friends, mage hand, vicious mockery

1st level (4 slots): charm person, healing word, heroism, sleep, thunderwave

2nd level (3 slots): invisibility, shatter

***Song of Rest.*** The bard can perform a song while taking a short rest. Any ally who hears the song regains an extra 1d6 hit points if it spends any Hit Dice to regain hit points at the end of that rest. The bard can confer this benefit on itself as well.

***Taunt (2/Day).*** The bard can use a bonus action on its turn to target one creature within 30 feet of it. If the target can hear the bard, the target must succeed on a DC 12 Charisma saving throw or have disadvantage on ability checks, attack rolls, and saving throws until the start of the bard's next turn.

## Actions

***Shortsword.*** Melee Weapon Attack: +4 to hit, reach 5 ft., one target. Hit: 5 (1d6 + 2) piercing damage.

***Shortbow.*** Ranged Weapon Attack: +4 to hit, range 80/320 ft., one target. Hit: 5 (1d6 + 2) piercing damage.
""".strip()


def _warlock_archfey_source() -> str:
    return """# Warlock of the Archfey

*Medium humanoid (any race), any alignment*

**Armor Class** 11 (14 with mage armor)
**Hit Points** 49 (11d8)
**Speed** 30 ft.

| STR | DEX | CON | INT | WIS | CHA |
|---:|---:|---:|---:|---:|---:|
| 9 (-1) | 13 (+1) | 11 (+0) | 11 (+0) | 12 (+1) | 18 (+4) |

**Saving Throws** Wis +3, Cha +6
**Skills** Arcana +2, Deception +6, Nature +2, Persuasion +6
**Condition Immunities** charmed
**Senses** passive Perception 11
**Languages** any two languages (usually Sylvan)
**Challenge** 4 (1,100 XP)

## Traits

***Innate Spellcasting.*** The warlock's innate spellcasting ability is Charisma. It can innately cast the following spells (spell save DC 15), requiring no material components:

At will: disguise self, mage armor (self only), silent image, speak with animals

1/day: conjure fey

***Spellcasting.*** The warlock is an 11th-level spellcaster. Its spellcasting ability is Charisma (spell save DC 14, +6 to hit with spell attacks). It regains its expended spell slots when it finishes a short or long rest. It knows the following warlock spells:

Cantrips (at will): dancing lights, eldritch blast, friends, mage hand, minor illusion, prestidigitation, vicious mockery

1st-5th level (3 5th-level slots): blink, charm person, dimension door, dominate beast, faerie fire, fear, hold monster, misty step, phantasmal force, seeming, sleep

## Actions

***Dagger.*** Melee or Ranged Weapon Attack: +3 to hit, reach 5 ft. or range 20/60 ft., one target. Hit: 4 (1d4 + 2) piercing damage.

## Reactions

***Misty Escape (Recharges after a Short or Long Rest).*** In response to taking damage, the warlock turns invisible and teleports up to 60 feet to an unoccupied space it can see. It remains invisible until the start of its next turn or until it attacks, makes a damage roll, or casts a spell.
""".strip()


def _cow_source(
    name: str,
    *,
    size: str = "Large",
    hp: str = "15 (2d10 + 4)",
    darkvision: int = 0,
    resistances: list[str] | None = None,
    extra_traits: list[str] | None = None,
) -> str:
    senses = (f"darkvision {darkvision} ft., " if darkvision else "") + "passive Perception 10"
    defenses = f"**Damage Resistances** {', '.join(resistances)}\n" if resistances else ""
    traits = [
        "***Charge.*** If the creature moves at least 20 feet straight toward a target and then hits it with a gore attack on the same turn, the target takes an extra 7 (2d6) piercing damage.",
        *(extra_traits or []),
    ]
    return f"""# {name}

*{size} beast, unaligned*

**Armor Class** 10
**Hit Points** {hp}
**Speed** 30 ft.

| STR | DEX | CON | INT | WIS | CHA |
|---:|---:|---:|---:|---:|---:|
| 18 (+4) | 10 (+0) | 14 (+2) | 2 (-4) | 10 (+0) | 4 (-3) |

{defenses}**Senses** {senses}
**Languages** —
**Challenge** 1/4 (50 XP)

## Traits

{chr(10).join(traits)}

## Actions

***Gore.*** Melee Weapon Attack: +6 to hit, reach 5 ft., one target. Hit: 7 (1d6 + 4) piercing damage.
""".strip()


def _cow_variants() -> list[dict[str, str]]:
    return [
        {
            "name": "Ox",
            "normalized_content": _cow_source(
                "Ox",
                extra_traits=[
                    "***Beast of Burden.*** The ox is considered Huge for determining its carrying capacity."
                ],
            ),
        },
        {"name": "Rothé", "normalized_content": _cow_source("Rothé", darkvision=30)},
        {
            "name": "Deep Rothé",
            "normalized_content": _cow_source(
                "Deep Rothé",
                size="Medium",
                hp="13 (2d8 + 4)",
                darkvision=60,
                extra_traits=[
                    "***Innate Spellcasting.*** Charisma is the spellcasting ability. The deep rothé can cast Dancing Lights at will, requiring no components."
                ],
            ),
        },
        {
            "name": "Stench Kow",
            "normalized_content": _cow_source(
                "Stench Kow",
                darkvision=60,
                resistances=["cold", "fire", "poison"],
                extra_traits=[
                    "***Stench.*** A creature other than a stench kow that starts its turn within 5 feet must succeed on a DC 12 Constitution saving throw or be poisoned until the start of its next turn. On a success, it is immune to all stench kows' Stench for 1 hour."
                ],
            ),
        },
    ]


def _recover_ocr_statblocks() -> dict[str, str]:
    recovered: dict[str, str] = {
        "Aurochs": _aurochs_source(),
        "Bard": _bard_source(),
        "Warlock of the Archfey": _warlock_archfey_source(),
    }
    by_profile: dict[tuple[str, float], list[tuple[str, int]]] = {}
    for name, (page, model, scale) in MISSING_STATBLOCKS.items():
        if model == "manual":
            continue
        by_profile.setdefault((model, scale), []).append((name, page))
    for (model, scale), entries in by_profile.items():
        provider = RapidOcrProvider(
            model_type=model,
            scale=scale,
            cache_dir=OCR_CACHE,
        )
        pages = list(dict.fromkeys(page for _name, page in entries))
        layouts = {
            layout.page_number: layout
            for layout in provider.extract_layout(SOURCE, page_numbers=pages)
        }
        for name, page in entries:
            result = recover_2014_statblock_from_ocr(
                layouts[page].as_dict(),
                name=name,
                minimum_confidence=0.6,
            )
            source_text = str(result["normalized_content"]).strip()
            parsed = parse_2014_statblock(
                source_text,
                source_key=f"volo-reviewed:{name}",
                rule_refs=[],
            )
            expected_ac, expected_hp, expected_scores = EXPECTED_CRITICAL[name]
            actual_scores = [
                int(parsed.sheet["abilities"][ability]["score"])
                for ability in (
                    "strength",
                    "dexterity",
                    "constitution",
                    "intelligence",
                    "wisdom",
                    "charisma",
                )
            ]
            actual = (
                int(parsed.sheet["combat"]["ac"]["override"]),
                int(parsed.sheet["combat"]["hp"]["max"]),
                actual_scores,
            )
            if actual != (expected_ac, expected_hp, expected_scores):
                raise RuntimeError(
                    f"OCR critical facts changed for {name}: "
                    f"expected={(expected_ac, expected_hp, expected_scores)}, actual={actual}"
                )
            recovered[name] = source_text
    return recovered


def _statblock_additions() -> list[dict[str, Any]]:
    recovered = _recover_ocr_statblocks()
    selectors = {
        "Aurochs": [_selector("AUROCHS", 208, match_all=True)],
        "Cow": [_selector("Cow", 208, match_all=True), _selector("Cow", 209, match_all=True)],
        "Mindwitness": [_selector("MINDWITNESS", 177, match_all=True)],
        "Shadow Mastiff": [_selector("SHADOW MASTIFF", 191, match_all=True)],
        "Yuan-ti Anathema": [_selector("A NATHEMA", 203, match_all=True)],
        "Evoker": [
            _selector("EV0K.ER", 215, match_all=True),
            _selector("EVOKER", 215, exact=True),
        ],
        "Illusionist": [_selector("ILLUSIONIST", 215, match_all=True)],
        "Bard": [_selector("BARD", 212, exact=True, match_all=True)],
        "Warlock of the Archfey": [_selector("WARLOCK OF THE ARCHFEY", 220)],
        "Warlock of the Fiend": [_selector("WARLOCK OF THE FIEND", 220)],
    }
    result = []
    for name in MISSING_STATBLOCKS:
        card: dict[str, Any] = {"normalized_content": recovered[name]}
        if name == "Cow":
            card["statblock_variants"] = _cow_variants()
        result.append(
            _addition(
                "statblock",
                name,
                selectors[name],
                card,
                replace_existing=name in {"Bard", "Warlock of the Archfey"},
            )
        )
    return result


def _runtime_probes() -> list[dict[str, Any]]:
    return [
        {
            "name": "volo-lizardfolk-complete-physical-grants",
            "steps": [
                {
                    "kind": "species",
                    "name": "Lizardfolk",
                    "selection": {"skills": ["Nature", "Survival"]},
                    "expect": [
                        {"path": "sheet.abilities.constitution.score", "equals": 12},
                        {"path": "sheet.combat.speed.swim", "equals": 30},
                        {"path": "sheet.skills.nature.proficiency", "equals": "proficient"},
                        {"path": "sheet.traits.proficiencies.weapons", "length": 0},
                        {"path": "sheet.inventory.items", "contains_names": ["Bite"]},
                        {"path": "sheet.effects", "contains_names": ["Lizardfolk Natural Armor"]},
                    ],
                }
            ],
        },
        {
            "name": "volo-hobgoblin-reviewed-martial-choices",
            "steps": [
                {
                    "kind": "species",
                    "name": "Hobgoblin",
                    "selection": {
                        "proficiency_choices": {
                            "martial_training": [
                                {"kind": "weapon", "name": "Longsword"},
                                {"kind": "weapon", "name": "Longbow"},
                            ]
                        }
                    },
                    "expect": [
                        {"path": "sheet.traits.proficiencies.armor", "contains": "Light Armor"},
                        {"path": "sheet.traits.proficiencies.weapons", "contains": "Longbow"},
                    ],
                }
            ],
        },
        {
            "name": "volo-shared-innate-spell-resources",
            "level": 5,
            "steps": [
                {
                    "kind": "species",
                    "name": "Triton",
                    "selection": {},
                    "expect": [
                        {
                            "path": "sheet.content.spells",
                            "contains_names": ["Fog Cloud", "Gust of Wind", "Wall of Water"],
                        },
                        {"path": "sheet.resources", "length": 1},
                        {"path": "sheet.traits.resistances", "contains": "cold"},
                    ],
                }
            ],
        },
        {
            "name": "volo-legacy-decrease-and-poison-immunity",
            "level": 3,
            "steps": [
                {
                    "kind": "species",
                    "name": "Kobold",
                    "selection": {},
                    "expect": [
                        {"path": "sheet.abilities.dexterity.score", "equals": 12},
                        {"path": "sheet.abilities.strength.score", "equals": 8},
                    ],
                },
            ],
        },
        {
            "name": "volo-yuan-ti-fixed-spell-and-defense-model",
            "level": 3,
            "steps": [
                {
                    "kind": "species",
                    "name": "Yuan-ti Pureblood",
                    "selection": {},
                    "expect": [
                        {"path": "sheet.traits.immunities", "contains": "poison"},
                        {"path": "sheet.traits.condition_immunities", "contains": "poisoned"},
                        {
                            "path": "sheet.content.spells",
                            "contains_names": ["Poison Spray", "Animal Friendship", "Suggestion"],
                        },
                    ],
                }
            ],
        },
    ]


def main() -> None:
    if len(STATBLOCK_NAMES) != 123 or len(set(STATBLOCK_NAMES)) != 123:
        raise RuntimeError("Volo statblock inventory must contain 123 distinct source entries")
    additions = [
        *_species_additions(),
        _wall_of_water(),
        *_statblock_additions(),
    ]
    actor_names = [
        *STATBLOCK_NAMES,
        "Ox",
        "Rothé",
        "Deep Rothé",
        "Stench Kow",
    ]
    document = {
        "complete_review": True,
        "default_status": "rejected",
        "addition_default_status": "accepted",
        "default_status_by_kind": {"statblock": "accepted"},
        "rationale": (
            "Agent reviewed all fifteen playable species variants, Wall of Water, all 123 "
            "source statblocks, and the four complete Cow-derived actor variants. Lore and "
            "customization guidance remain searchable source context; executable grants are "
            "bounded, source-cited, and do not invent module-specific engine rules."
        ),
        "expected_counts": {"species": 15, "spell": 1, "statblock": 123},
        "expected_actor_names": actor_names,
        "runtime_probes": _runtime_probes(),
        "decisions": [
            {
                "kind": "statblock",
                "name": extracted_name,
                "status": "accepted",
                "artifact_patch": {"card": {"name": canonical_name}},
                "note": (
                    "Agent corrected only same-page OCR identity noise after verifying the "
                    "complete source statblock and its immutable citations."
                ),
            }
            for extracted_name, canonical_name in STATBLOCK_NAME_CORRECTIONS.items()
        ],
        "additions": additions,
    }
    payload = {"version": 1, "documents": {BOOK: document}}
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"wrote {OUTPUT} with {len(additions)} source-bound additions and "
        f"{len(actor_names)} portable actors"
    )


if __name__ == "__main__":
    main()
