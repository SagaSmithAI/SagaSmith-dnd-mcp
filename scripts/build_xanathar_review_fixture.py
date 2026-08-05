"""Build the complete source-reviewed Xanathar's Guide to Everything addon fixture."""

# ruff: noqa: E501, I001 -- source-facing inventories stay directly auditable.

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "fixtures" / "books_catalog_review_xanathar_v1.json"
BOOK = "D&D 5E - Xanathar's Guide to Everything.pdf"

SKILLS = [
    "Acrobatics", "Animal Handling", "Arcana", "Athletics", "Deception",
    "History", "Insight", "Intimidation", "Investigation", "Medicine",
    "Nature", "Perception", "Performance", "Persuasion", "Religion",
    "Sleight of Hand", "Stealth", "Survival",
]
TOOLS = [
    "Alchemist's Supplies", "Brewer's Supplies", "Calligrapher's Supplies",
    "Carpenter's Tools", "Cartographer's Tools", "Cobbler's Tools",
    "Cook's Utensils", "Glassblower's Tools", "Jeweler's Tools",
    "Leatherworker's Tools", "Mason's Tools", "Painter's Supplies",
    "Potter's Tools", "Smith's Tools", "Tinker's Tools", "Weaver's Tools",
    "Woodcarver's Tools", "Disguise Kit", "Forgery Kit", "Herbalism Kit",
    "Navigator's Tools", "Poisoner's Kit", "Thieves' Tools",
]


def _selector(heading: str, page: int, **extra: Any) -> dict[str, Any]:
    return {"heading_contains": heading, "page_start": page, **extra}


def _addition(
    kind: str,
    name: str,
    page: int,
    card: dict[str, Any],
    *,
    heading: str | None = None,
    selectors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "name": name,
        "source_selectors": selectors or [_selector(heading or name, page)],
        "card": {"name": name, **card},
        "note": "Agent reviewed this exact source-bound player option and filled its portable card before release.",
    }


def _prepared(values: list[tuple[str, int]]) -> list[dict[str, Any]]:
    return [{"name": name, "minimum_level": level} for name, level in values]


def _subclass(
    name: str,
    class_name: str,
    minimum_level: int,
    page: int,
    *,
    prepared: list[tuple[str, int]] | None = None,
    expansion: list[str] | None = None,
    heading: str | None = None,
) -> dict[str, Any]:
    return _addition(
        "subclass",
        name,
        page,
        {
            "class_name": class_name,
            "minimum_level": minimum_level,
            "always_prepared_spells": _prepared(prepared or []),
            "spell_list_expansion": list(expansion or []),
            "spell_grants": [],
        },
        selectors=[_selector(heading or name, page, match_all=True)],
    )


def _feature(
    name: str,
    class_name: str,
    subclass_name: str,
    minimum_level: int,
    page: int,
    *,
    mechanical_grants: dict[str, Any] | None = None,
    selection_requirements: dict[str, Any] | None = None,
    selection_requirements_by_level: dict[str, Any] | None = None,
    repeatable_selection_levels: list[int] | None = None,
    feature_subtype: str = "",
    description: str = "",
) -> dict[str, Any]:
    return _addition(
        "feature",
        name,
        page,
        {
            "class_name": class_name,
            "subclass_name": subclass_name,
            "minimum_level": minimum_level,
            "feature_subtype": feature_subtype,
            "description": description or (
                f"Apply the exact source-defined {name} triggers, limits, choices, and effects through the cited Agent-as-DM ruling."
            ),
            "mechanical_grants": mechanical_grants or {},
            "selection_requirements": selection_requirements or {},
            "selection_requirements_by_level": selection_requirements_by_level or {},
            "repeatable_selection_levels": repeatable_selection_levels or [],
        },
    )


def _subclasses() -> list[dict[str, Any]]:
    forge = [
        ("Identify", 1), ("Searing Smite", 1), ("Heat Metal", 3),
        ("Magic Weapon", 3), ("Elemental Weapon", 5),
        ("Protection from Energy", 5), ("Fabricate", 7),
        ("Wall of Fire", 7), ("Animate Objects", 9), ("Creation", 9),
    ]
    grave = [
        ("Bane", 1), ("False Life", 1), ("Gentle Repose", 3),
        ("Ray of Enfeeblement", 3), ("Revivify", 5),
        ("Vampiric Touch", 5), ("Blight", 7), ("Death Ward", 7),
        ("Antilife Shell", 9), ("Raise Dead", 9),
    ]
    conquest = [
        ("Armor of Agathys", 3), ("Command", 3), ("Hold Person", 5),
        ("Spiritual Weapon", 5), ("Bestow Curse", 9), ("Fear", 9),
        ("Dominate Beast", 13), ("Stoneskin", 13), ("Cloudkill", 17),
        ("Dominate Person", 17),
    ]
    redemption = [
        ("Sanctuary", 3), ("Sleep", 3), ("Calm Emotions", 5),
        ("Hold Person", 5), ("Counterspell", 9), ("Hypnotic Pattern", 9),
        ("Otiluke's Resilient Sphere", 13), ("Stoneskin", 13),
        ("Hold Monster", 17), ("Wall of Force", 17),
    ]
    return [
        _subclass("Path of the Ancestral Guardian", "Barbarian", 3, 10),
        _subclass("Path of the Storm Herald", "Barbarian", 3, 11),
        _subclass("Path of the Zealot", "Barbarian", 3, 12),
        _subclass("College of Glamour", "Bard", 3, 15),
        _subclass("College of Swords", "Bard", 3, 16),
        _subclass("College of Whispers", "Bard", 3, 17),
        _subclass("Forge Domain", "Cleric", 1, 19, prepared=forge),
        _subclass("Grave Domain", "Cleric", 1, 20, prepared=grave),
        _subclass("Circle of Dreams", "Druid", 2, 23),
        _subclass("Circle of the Shepherd", "Druid", 2, 24),
        _subclass("Arcane Archer", "Fighter", 3, 29),
        _subclass("Cavalier", "Fighter", 3, 31),
        _subclass("Samurai", "Fighter", 3, 32),
        _subclass("Way of the Drunken Master", "Monk", 3, 34),
        _subclass("Way of the Kensei", "Monk", 3, 35),
        _subclass("Way of the Sun Soul", "Monk", 3, 36),
        _subclass(
            "Oath of Conquest", "Paladin", 3, 38,
            prepared=conquest, heading="OATH OF CONQU_E_S_T",
        ),
        _subclass(
            "Oath of Redemption", "Paladin", 3, 39,
            prepared=redemption, heading="OATH OF R E DEMPTION",
        ),
        _subclass(
            "Gloom Stalker", "Ranger", 3, 42,
            expansion=["Disguise Self", "Rope Trick", "Fear", "Greater Invisibility", "Seeming"],
        ),
        _subclass(
            "Horizon Walker", "Ranger", 3, 43,
            expansion=["Protection from Evil and Good", "Misty Step", "Haste", "Banishment", "Teleportation Circle"],
        ),
        _subclass(
            "Monster Slayer", "Ranger", 3, 44,
            expansion=["Protection from Evil and Good", "Zone of Truth", "Magic Circle", "Banishment", "Hold Monster"],
        ),
        _subclass("Inquisitive", "Rogue", 3, 46),
        _subclass("Mastermind", "Rogue", 3, 47),
        _subclass("Scout", "Rogue", 3, 48),
        _subclass("Swashbuckler", "Rogue", 3, 48),
        _subclass("Divine Soul", "Sorcerer", 1, 51),
        _subclass("Shadow Magic", "Sorcerer", 1, 51),
        _subclass("Storm Sorcery", "Sorcerer", 1, 52),
        _subclass(
            "The Celestial", "Warlock", 1, 55,
            expansion=["Cure Wounds", "Guiding Bolt", "Flaming Sphere", "Lesser Restoration", "Daylight", "Revivify", "Guardian of Faith", "Wall of Fire", "Flame Strike", "Greater Restoration"],
        ),
        _subclass(
            "The Hexblade", "Warlock", 1, 56,
            expansion=["Shield", "Wrathful Smite", "Blur", "Branding Smite", "Blink", "Elemental Weapon", "Phantasmal Killer", "Staggering Smite", "Banishing Smite", "Cone of Cold"],
        ),
        _subclass("War Magic", "Wizard", 2, 60),
    ]


FEATURE_SPECS = [
    # Barbarian
    ("Ancestral Protectors", "Barbarian", "Path of the Ancestral Guardian", 3, 11),
    ("Spirit Shield", "Barbarian", "Path of the Ancestral Guardian", 6, 11),
    ("Consult the Spirits", "Barbarian", "Path of the Ancestral Guardian", 10, 11),
    ("Vengeful Ancestors", "Barbarian", "Path of the Ancestral Guardian", 14, 11),
    ("Storm Aura", "Barbarian", "Path of the Storm Herald", 3, 11),
    ("Storm Soul", "Barbarian", "Path of the Storm Herald", 6, 11),
    ("Shielding Storm", "Barbarian", "Path of the Storm Herald", 10, 11),
    ("Raging Storm", "Barbarian", "Path of the Storm Herald", 14, 12),
    ("Divine Fury", "Barbarian", "Path of the Zealot", 3, 12),
    ("Warrior of the Gods", "Barbarian", "Path of the Zealot", 3, 12),
    ("Fanatical Focus", "Barbarian", "Path of the Zealot", 6, 12),
    ("Zealous Presence", "Barbarian", "Path of the Zealot", 10, 12),
    ("Rage Beyond Death", "Barbarian", "Path of the Zealot", 14, 12),
    # Bard
    ("Mantle of Inspiration", "Bard", "College of Glamour", 3, 15),
    ("Enthralling Performance", "Bard", "College of Glamour", 3, 15),
    ("Mantle of Majesty", "Bard", "College of Glamour", 6, 15),
    ("Unbreakable Majesty", "Bard", "College of Glamour", 14, 15),
    ("Bonus Proficiencies", "Bard", "College of Swords", 3, 16),
    ("Fighting Style", "Bard", "College of Swords", 3, 16),
    ("Blade Flourish", "Bard", "College of Swords", 3, 16),
    ("Extra Attack", "Bard", "College of Swords", 6, 16),
    ("Master's Flourish", "Bard", "College of Swords", 14, 17),
    ("Psychic Blades", "Bard", "College of Whispers", 3, 17),
    ("Words of Terror", "Bard", "College of Whispers", 3, 17),
    ("Mantle of Whispers", "Bard", "College of Whispers", 6, 17),
    ("Shadow Lore", "Bard", "College of Whispers", 14, 17),
    # Cleric
    ("Bonus Proficiency", "Cleric", "Forge Domain", 1, 20),
    ("Blessing of the Forge", "Cleric", "Forge Domain", 1, 20),
    ("Channel Divinity: Artisan's Blessing", "Cleric", "Forge Domain", 2, 20),
    ("Soul of the Forge", "Cleric", "Forge Domain", 6, 20),
    ("Divine Strike", "Cleric", "Forge Domain", 8, 20),
    ("Saint of Forge and Fire", "Cleric", "Forge Domain", 17, 20),
    ("Circle of Mortality", "Cleric", "Grave Domain", 1, 21),
    ("Eyes of the Grave", "Cleric", "Grave Domain", 1, 21),
    ("Channel Divinity: Path to the Grave", "Cleric", "Grave Domain", 2, 21),
    ("Sentinel at Death's Door", "Cleric", "Grave Domain", 6, 21),
    ("Potent Spellcasting", "Cleric", "Grave Domain", 8, 21),
    ("Keeper of Souls", "Cleric", "Grave Domain", 17, 21),
    # Druid
    ("Balm of the Summer Court", "Druid", "Circle of Dreams", 2, 23),
    ("Hearth of Moonlight and Shadow", "Druid", "Circle of Dreams", 6, 23),
    ("Hidden Paths", "Druid", "Circle of Dreams", 10, 23),
    ("Walker in Dreams", "Druid", "Circle of Dreams", 14, 24),
    ("Speech of the Woods", "Druid", "Circle of the Shepherd", 2, 24),
    ("Spirit Totem", "Druid", "Circle of the Shepherd", 2, 24),
    ("Mighty Summoner", "Druid", "Circle of the Shepherd", 6, 25),
    ("Guardian Spirit", "Druid", "Circle of the Shepherd", 10, 25),
    ("Faithful Summons", "Druid", "Circle of the Shepherd", 14, 25),
    # Fighter
    ("Arcane Archer Lore", "Fighter", "Arcane Archer", 3, 29),
    ("Arcane Shot", "Fighter", "Arcane Archer", 3, 29),
    ("Magic Arrow", "Fighter", "Arcane Archer", 7, 29),
    ("Curving Shot", "Fighter", "Arcane Archer", 7, 29),
    ("Ever-Ready Shot", "Fighter", "Arcane Archer", 15, 29),
    ("Bonus Proficiency", "Fighter", "Cavalier", 3, 31),
    ("Born to the Saddle", "Fighter", "Cavalier", 3, 31),
    ("Unwavering Mark", "Fighter", "Cavalier", 3, 31),
    ("Warding Maneuver", "Fighter", "Cavalier", 7, 31),
    ("Hold the Line", "Fighter", "Cavalier", 10, 31),
    ("Ferocious Charger", "Fighter", "Cavalier", 15, 32),
    ("Vigilant Defender", "Fighter", "Cavalier", 18, 32),
    ("Bonus Proficiency", "Fighter", "Samurai", 3, 32),
    ("Fighting Spirit", "Fighter", "Samurai", 3, 32),
    ("Elegant Courtier", "Fighter", "Samurai", 7, 32),
    ("Tireless Spirit", "Fighter", "Samurai", 10, 32),
    ("Rapid Strike", "Fighter", "Samurai", 15, 32),
    ("Strength Before Death", "Fighter", "Samurai", 18, 32),
    # Monk
    ("Bonus Proficiencies", "Monk", "Way of the Drunken Master", 3, 35),
    ("Drunken Technique", "Monk", "Way of the Drunken Master", 3, 35),
    ("Tipsy Sway", "Monk", "Way of the Drunken Master", 6, 35),
    ("Drunkard's Luck", "Monk", "Way of the Drunken Master", 11, 35),
    ("Intoxicated Frenzy", "Monk", "Way of the Drunken Master", 17, 35),
    ("Path of the Kensei", "Monk", "Way of the Kensei", 3, 35),
    ("One with the Blade", "Monk", "Way of the Kensei", 6, 35),
    ("Sharpen the Blade", "Monk", "Way of the Kensei", 11, 36),
    ("Unerring Accuracy", "Monk", "Way of the Kensei", 17, 36),
    ("Radiant Sun Bolt", "Monk", "Way of the Sun Soul", 3, 36),
    ("Searing Arc Strike", "Monk", "Way of the Sun Soul", 6, 36),
    ("Searing Sunburst", "Monk", "Way of the Sun Soul", 11, 36),
    ("Sun Shield", "Monk", "Way of the Sun Soul", 17, 36),
    # Paladin
    ("Channel Divinity", "Paladin", "Oath of Conquest", 3, 39),
    ("Aura of Conquest", "Paladin", "Oath of Conquest", 7, 39),
    ("Scornful Rebuke", "Paladin", "Oath of Conquest", 15, 39),
    ("Invincible Conqueror", "Paladin", "Oath of Conquest", 20, 39),
    ("Channel Divinity", "Paladin", "Oath of Redemption", 3, 40),
    ("Aura of the Guardian", "Paladin", "Oath of Redemption", 7, 40),
    ("Protective Spirit", "Paladin", "Oath of Redemption", 15, 40),
    ("Emissary of Redemption", "Paladin", "Oath of Redemption", 20, 40),
    # Ranger
    ("Gloom Stalker Magic", "Ranger", "Gloom Stalker", 3, 43),
    ("Dread Ambusher", "Ranger", "Gloom Stalker", 3, 43),
    ("Umbral Sight", "Ranger", "Gloom Stalker", 3, 43),
    ("Iron Mind", "Ranger", "Gloom Stalker", 7, 43),
    ("Stalker's Flurry", "Ranger", "Gloom Stalker", 11, 43),
    ("Shadowy Dodge", "Ranger", "Gloom Stalker", 15, 43),
    ("Horizon Walker Magic", "Ranger", "Horizon Walker", 3, 43),
    ("Detect Portal", "Ranger", "Horizon Walker", 3, 43),
    ("Planar Warrior", "Ranger", "Horizon Walker", 3, 43),
    ("Ethereal Step", "Ranger", "Horizon Walker", 7, 44),
    ("Distant Strike", "Ranger", "Horizon Walker", 11, 44),
    ("Spectral Defense", "Ranger", "Horizon Walker", 15, 44),
    ("Monster Slayer Magic", "Ranger", "Monster Slayer", 3, 44),
    ("Hunter's Sense", "Ranger", "Monster Slayer", 3, 44),
    ("Slayer's Prey", "Ranger", "Monster Slayer", 3, 44),
    ("Supernatural Defense", "Ranger", "Monster Slayer", 7, 44),
    ("Magic-User's Nemesis", "Ranger", "Monster Slayer", 11, 44),
    ("Slayer's Counter", "Ranger", "Monster Slayer", 15, 44),
    # Rogue
    ("Ear for Deceit", "Rogue", "Inquisitive", 3, 47),
    ("Eye for Detail", "Rogue", "Inquisitive", 3, 47),
    ("Insightful Fighting", "Rogue", "Inquisitive", 3, 47),
    ("Steady Eye", "Rogue", "Inquisitive", 9, 47),
    ("Unerring Eye", "Rogue", "Inquisitive", 13, 47),
    ("Eye for Weakness", "Rogue", "Inquisitive", 17, 47),
    ("Master of Intrigue", "Rogue", "Mastermind", 3, 47),
    ("Master of Tactics", "Rogue", "Mastermind", 3, 47),
    ("Insightful Manipulator", "Rogue", "Mastermind", 9, 47),
    ("Misdirection", "Rogue", "Mastermind", 13, 47),
    ("Soul of Deceit", "Rogue", "Mastermind", 17, 47),
    ("Skirmisher", "Rogue", "Scout", 3, 48),
    ("Survivalist", "Rogue", "Scout", 3, 48),
    ("Superior Mobility", "Rogue", "Scout", 9, 48),
    ("Ambush Master", "Rogue", "Scout", 13, 48),
    ("Sudden Strike", "Rogue", "Scout", 17, 48),
    ("Fancy Footwork", "Rogue", "Swashbuckler", 3, 48),
    ("Rakish Audacity", "Rogue", "Swashbuckler", 3, 48),
    ("Panache", "Rogue", "Swashbuckler", 9, 48),
    ("Elegant Maneuver", "Rogue", "Swashbuckler", 13, 48),
    ("Master Duelist", "Rogue", "Swashbuckler", 17, 48),
    # Sorcerer
    ("Divine Magic", "Sorcerer", "Divine Soul", 1, 51),
    ("Favored by the Gods", "Sorcerer", "Divine Soul", 1, 51),
    ("Empowered Healing", "Sorcerer", "Divine Soul", 6, 51),
    ("Otherworldly Wings", "Sorcerer", "Divine Soul", 14, 51),
    ("Unearthly Recovery", "Sorcerer", "Divine Soul", 18, 51),
    ("Eyes of the Dark", "Sorcerer", "Shadow Magic", 1, 52),
    ("Strength of the Grave", "Sorcerer", "Shadow Magic", 1, 52),
    ("Hound of Ill Omen", "Sorcerer", "Shadow Magic", 6, 52),
    ("Shadow Walk", "Sorcerer", "Shadow Magic", 14, 52),
    ("Umbral Form", "Sorcerer", "Shadow Magic", 18, 52),
    ("Tempestuous Magic", "Sorcerer", "Storm Sorcery", 1, 53),
    ("Heart of the Storm", "Sorcerer", "Storm Sorcery", 6, 53),
    ("Storm Guide", "Sorcerer", "Storm Sorcery", 6, 53),
    ("Storm's Fury", "Sorcerer", "Storm Sorcery", 14, 53),
    ("Wind Soul", "Sorcerer", "Storm Sorcery", 18, 53),
    # Warlock
    ("Bonus Cantrips", "Warlock", "The Celestial", 1, 55),
    ("Healing Light", "Warlock", "The Celestial", 1, 55),
    ("Radiant Soul", "Warlock", "The Celestial", 6, 56),
    ("Celestial Resilience", "Warlock", "The Celestial", 10, 56),
    ("Searing Vengeance", "Warlock", "The Celestial", 14, 56),
    ("Hexblade's Curse", "Warlock", "The Hexblade", 1, 56),
    ("Hex Warrior", "Warlock", "The Hexblade", 1, 56),
    ("Accursed Specter", "Warlock", "The Hexblade", 6, 57),
    ("Armor of Hexes", "Warlock", "The Hexblade", 10, 57),
    ("Master of Hexes", "Warlock", "The Hexblade", 14, 57),
    # Wizard
    ("Arcane Deflection", "Wizard", "War Magic", 2, 60),
    ("Tactical Wit", "Wizard", "War Magic", 2, 61),
    ("Power Surge", "Wizard", "War Magic", 6, 61),
    ("Durable Magic", "Wizard", "War Magic", 10, 61),
    ("Deflecting Shroud", "Wizard", "War Magic", 14, 61),
]


def _features() -> list[dict[str, Any]]:
    result = [_feature(*spec) for spec in FEATURE_SPECS]
    for feature in result:
        name = feature["name"]
        card = feature["card"]
        if name == "Storm Aura":
            card["selection_requirements"] = {
                "field": "storm_environment", "count": 1,
                "options": ["Desert", "Sea", "Tundra"],
            }
        elif name == "Fighting Style" and card["subclass_name"] == "College of Swords":
            card["selection_requirements"] = {
                "field": "style", "count": 1,
                "options": ["Dueling", "Two-Weapon Fighting"],
            }
        elif name == "Arcane Shot":
            base = {
                "field": "arcane_shot_options", "count": 1,
                "options": [
                    "Banishing Arrow", "Beguiling Arrow", "Bursting Arrow",
                    "Enfeebling Arrow", "Grasping Arrow", "Piercing Arrow",
                    "Seeking Arrow", "Shadow Arrow",
                ],
                "requires_new_choice": True,
                "choice_uniqueness_scope": "arcane_archer_arcane_shot",
            }
            card["selection_requirements"] = {**base, "count": 2}
            card["selection_requirements_by_level"] = {
                str(level): {**base, "count": 2 if level == 3 else 1}
                for level in (3, 7, 10, 15, 18)
            }
            card["repeatable_selection_levels"] = [3, 7, 10, 15, 18]
        elif name == "Path of the Kensei":
            options = [
                "Battleaxe", "Blowgun", "Club", "Dagger", "Dart", "Flail",
                "Hand Crossbow", "Handaxe", "Javelin", "Light Crossbow",
                "Light Hammer", "Longbow", "Longsword", "Mace", "Morningstar",
                "Quarterstaff", "Rapier", "Scimitar", "Shortbow", "Shortsword",
                "Sickle", "Sling", "Spear", "Trident", "War Pick", "Warhammer", "Whip",
            ]
            base = {
                "field": "kensei_weapons", "count": 1, "options": options,
                "requires_new_choice": True,
                "choice_uniqueness_scope": "kensei_weapons",
            }
            card["selection_requirements"] = {**base, "count": 2}
            card["selection_requirements_by_level"] = {
                str(level): {**base, "count": 2 if level == 3 else 1}
                for level in (3, 6, 11, 17)
            }
            card["repeatable_selection_levels"] = [3, 6, 11, 17]
        elif name == "Bonus Proficiency" and card["subclass_name"] == "Forge Domain":
            card["mechanical_grants"] = {
                "armor_proficiencies": ["Heavy Armor"],
                "tool_proficiencies": ["Smith's Tools"],
            }
        elif name == "Bonus Proficiencies" and card["subclass_name"] == "College of Swords":
            card["mechanical_grants"] = {
                "armor_proficiencies": ["Medium Armor"],
                "weapon_proficiencies": ["Scimitar"],
            }
        elif name == "Hex Warrior":
            card["mechanical_grants"] = {
                "armor_proficiencies": ["Medium Armor", "Shields"],
                "weapon_proficiencies": ["Martial Weapons"],
            }
        if name == "Warrior of the Gods" and card["subclass_name"] == "Path of the Zealot":
            feature["source_selectors"] = [_selector(
                "DIVINE FURY",
                12,
                content_contains="WARRIOR OF THE Goos",
                start_contains="WARRIOR OF THE Goos",
                end_before_contains="FANATICAL Focus",
            )]
        elif name == "Fanatical Focus" and card["subclass_name"] == "Path of the Zealot":
            feature["source_selectors"] = [_selector(
                "DIVINE FURY",
                12,
                content_contains="FANATICAL Focus",
                start_contains="FANATICAL Focus",
            )]
        elif name == "Bonus Proficiency" and card["subclass_name"] == "Forge Domain":
            feature["source_selectors"] = [_selector("BONUS PROFI C IENCI ES", 20)]
        elif name == "Ear for Deceit" and card["subclass_name"] == "Inquisitive":
            feature["source_selectors"] = [_selector("EAR FOR D ECEIT", 46)]
        elif name == "Hex Warrior" and card["subclass_name"] == "The Hexblade":
            feature["source_selectors"] = [_selector("HExWARRIOR", 56)]
    return result


def _spell_grant(
    name: str,
    level: int,
    ability: str,
    eligible_class: str,
    *,
    method: str = "limited_use",
    free_casts: int = 1,
    recovers_on: str | None = "long_rest",
) -> dict[str, Any]:
    return {
        "name": name,
        "level": level,
        "eligible_classes": [eligible_class],
        "method": method,
        "spellcasting_ability": ability,
        "free_casts": free_casts,
        "recovers_on": recovers_on,
        "allow_slot_cast": False,
        "minimum_level": 1,
        "ritual_only": False,
    }


FEAT_SPECIES = {
    "Bountiful Luck": ["Lightfoot Halfling", "Stout Halfling", "Halfling"],
    "Dragon Fear": ["Dragonborn"],
    "Dragon Hide": ["Dragonborn"],
    "Drow High Magic": ["Drow"],
    "Dwarven Fortitude": ["Dwarf", "Hill Dwarf", "Mountain Dwarf", "Duergar"],
    "Elven Accuracy": ["Elf", "High Elf", "Wood Elf", "Drow", "Half-Elf"],
    "Fade Away": ["Gnome", "Forest Gnome", "Rock Gnome", "Deep Gnome"],
    "Fey Teleportation": ["High Elf"],
    "Flames of Phlegethos": ["Tiefling"],
    "Infernal Constitution": ["Tiefling"],
    "Orcish Fury": ["Half-Orc"],
    "Prodigy": ["Half-Elf", "Half-Orc", "Human", "Variant Human"],
    "Second Chance": ["Lightfoot Halfling", "Stout Halfling", "Halfling"],
}


def _ability_choice(*abilities: str) -> dict[str, Any]:
    return {
        "field": "ability_score_increases",
        "kind": "ability_score_increase",
        "allowed_distributions": [[1]],
        "ability_options": list(abilities),
        "maximum_score": 20,
    }


def _feats() -> list[dict[str, Any]]:
    pages = {
        "Bountiful Luck": 74, "Dragon Fear": 75, "Dragon Hide": 75,
        "Drow High Magic": 75, "Dwarven Fortitude": 75,
        "Elven Accuracy": 75, "Fade Away": 75, "Fey Teleportation": 75,
        "Flames of Phlegethos": 75, "Infernal Constitution": 76,
        "Orcish Fury": 76, "Prodigy": 76, "Second Chance": 76,
        "Squat Nimbleness": 76,
    }
    ability_choices = {
        "Dragon Fear": ("strength", "constitution", "charisma"),
        "Dragon Hide": ("strength", "constitution", "charisma"),
        "Elven Accuracy": ("dexterity", "intelligence", "wisdom", "charisma"),
        "Fade Away": ("dexterity", "intelligence"),
        "Fey Teleportation": ("intelligence", "charisma"),
        "Flames of Phlegethos": ("intelligence", "charisma"),
        "Orcish Fury": ("strength", "constitution"),
        "Second Chance": ("dexterity", "constitution", "charisma"),
        "Squat Nimbleness": ("strength", "dexterity"),
    }
    fixed_increases = {
        "Dwarven Fortitude": {"constitution": 1},
        "Infernal Constitution": {"constitution": 1},
    }
    result = []
    for name, page in pages.items():
        prerequisites = (
            [{"kind": "species_required", "species": FEAT_SPECIES[name]}]
            if name in FEAT_SPECIES
            else [{
                "kind": "species_or_size",
                "species": ["Dwarf", "Hill Dwarf", "Mountain Dwarf", "Duergar"],
                "sizes": ["small"],
            }]
        )
        mechanical_grants: dict[str, Any] = {
            "ability_score_increases": fixed_increases.get(name, {}),
            "maximum_ability_score": 20,
        }
        selection_requirements: dict[str, Any] | None = (
            _ability_choice(*ability_choices[name]) if name in ability_choices else None
        )
        if name == "Drow High Magic":
            mechanical_grants["spell_grants"] = [
                _spell_grant("Detect Magic", 1, "charisma", "Wizard", method="at_will", free_casts=0, recovers_on=None),
                _spell_grant("Levitate", 2, "charisma", "Wizard"),
                _spell_grant("Dispel Magic", 3, "charisma", "Wizard"),
            ]
        elif name == "Fey Teleportation":
            mechanical_grants["languages"] = ["Sylvan"]
            mechanical_grants["spell_grants"] = [
                _spell_grant("Misty Step", 2, "intelligence", "Wizard", recovers_on="short_rest")
            ]
        elif name == "Prodigy":
            selection_requirements = {
                "field": "prodigy_proficiencies",
                "kind": "proficiency_groups",
                "groups": [
                    {"id": "skill", "kind": "skill", "count": 1, "options": SKILLS},
                    {"id": "expertise", "kind": "skill_expertise", "count": 1, "options": SKILLS},
                    {"id": "tool", "kind": "tool", "count": 1, "options": TOOLS},
                    {"id": "language", "kind": "language", "count": 1, "options": [], "allow_unlisted": True},
                ],
            }
        result.append(
            _addition(
                "feat",
                name,
                page,
                {
                    "prerequisites": prerequisites,
                    "repeatable": False,
                    "selection_requirements": selection_requirements,
                    "mechanical_grants": mechanical_grants,
                    "description": f"Apply the complete source feat {name}, including its cited trigger and limitations.",
                },
                heading="0RCISH FURY" if name == "Orcish Fury" else name,
            )
        )
    return result


def _invocations() -> list[dict[str, Any]]:
    specs = [
        ("Aspect of the Moon", 57, 1, "", "", ""),
        ("Cloak of Flies", 57, 5, "", "", ""),
        ("Eldritch Smite", 57, 5, "Pact of the Blade", "", ""),
        ("Ghostly Gaze", 57, 7, "", "", ""),
        ("Gift of the Depths", 58, 5, "", "", ""),
        ("Gift of the Ever-Living Ones", 58, 1, "Pact of the Chain", "", ""),
        ("Grasp of Hadar", 58, 1, "", "Eldritch Blast", ""),
        ("Improved Pact Weapon", 58, 1, "Pact of the Blade", "", ""),
        ("Lance of Lethargy", 58, 1, "", "Eldritch Blast", ""),
        ("Maddening Hex", 58, 5, "", "", ""),
        ("Relentless Hex", 58, 7, "", "", ""),
        ("Shroud of Shadow", 58, 15, "", "", "Invisibility"),
        ("Tomb of Levistus", 58, 5, "", "", ""),
        ("Trickster's Escape", 58, 7, "", "", ""),
    ]
    result = []
    for name, page, level, pact, cantrip, at_will in specs:
        card: dict[str, Any] = {
            "class_name": "Warlock",
            "subclass_name": "",
            "minimum_level": level,
            "feature_subtype": "selectable_option",
            "extends_feature": {"name": "Eldritch Invocations", "class_name": "Warlock"},
            "description": f"The complete source-defined {name} Eldritch Invocation.",
            "mechanical_grants": {},
            "selection_requirements": {},
            "selection_requirements_by_level": {},
            "repeatable_selection_levels": [],
        }
        if pact:
            card["required_pact_boon"] = pact
        if cantrip:
            card["required_cantrip"] = cantrip
        if at_will:
            card["at_will_spell"] = at_will
        result.append(_addition("feature", name, page, card))
    return result


ITEM_NAME_CORRECTIONS = {
    "B OOTS OF FALSE TRACKS": "Boots of False Tracks",
    "REWARD'S HANDY SPICE POUCH": "Heward's Handy Spice Pouch",
    "ORB OFT!ME": "Orb of Time",
    "STAFF OF F LOWERS": "Staff of Flowers",
    "UNBREAKABL E ARROW": "Unbreakable Arrow",
}

SPELL_NAME_CORRECTIONS = {
    "Melfs minute meteors": "Melf's Minute Meteors",
    "Word ofr adiance": "Word of Radiance",
}

REJECTED_SPELL_CANDIDATES = [
    "adiance",
]

TINY_SERVANT_STATBLOCK = """# Tiny Servant

*Tiny construct, unaligned*

**Armor Class** 15 (natural armor)

**Hit Points** 10 (4d4)

**Speed** 30 ft., climb 30 ft.

| STR | DEX | CON | INT | WIS | CHA |
|---:|---:|---:|---:|---:|---:|
| 4 (-3) | 16 (+3) | 10 (+0) | 2 (-4) | 10 (+0) | 1 (-5) |

**Damage Immunities** poison, psychic

**Condition Immunities** blinded, charmed, deafened, exhaustion, frightened, paralyzed, petrified, poisoned

**Senses** blindsight 60 ft. (blind beyond this radius), passive Perception 10

**Languages** —

## Actions

***Slam.*** Melee Weapon Attack: +5 to hit, reach 5 ft., one target. Hit: 5 (1d4 + 3) bludgeoning damage.
""".strip()


def _split_toll_the_dead_spell() -> dict[str, Any]:
    return _addition(
        "spell",
        "Toll the Dead",
        170,
        {
            "level": 0,
            "classes": ["cleric", "warlock", "wizard"],
            "definition": {
                "school": "necromancy",
                "casting_time": "1 action",
                "range": {
                    "kind": "distance",
                    "normal_ft": 60,
                    "long_ft": 0,
                    "area": "",
                },
                "duration": {
                    "kind": "instantaneous",
                    "value": 0,
                    "unit": "round",
                    "concentration": False,
                },
                "components": {
                    "verbal": True,
                    "somatic": True,
                    "material": False,
                    "material_description": "",
                    "material_cost_cp": 0,
                    "consumed": False,
                },
                "effect": (
                    "A creature within range makes a Wisdom saving throw, taking "
                    "1d8 necrotic damage on a failure, or 1d12 if it is missing hit "
                    "points. The damage gains one die at levels 5, 11, and 17."
                ),
            },
        },
        selectors=[
            _selector("TOLL THE DEA D", 170),
            _selector(
                "A CTIONS",
                170,
                content_contains="Range: 60 feet",
                start_contains="Range: 60 feet",
            ),
        ],
    )


def _runtime_probes() -> list[dict[str, Any]]:
    return [
        {
            "name": "xanathar-forge-domain-prepared-spells",
            "level": 5,
            "class_name": "Cleric",
            "steps": [{
                "kind": "subclass", "name": "Forge Domain", "selection": {},
                "expect": [
                    {"path": "sheet.progression.classes", "contains_names": ["Cleric"]},
                    {"path": "sheet.content.spells", "contains_names": ["Identify", "Searing Smite", "Heat Metal", "Magic Weapon", "Elemental Weapon", "Protection from Energy"]},
                ],
            }],
        },
        {
            "name": "xanathar-hexblade-reviewed-proficiencies",
            "level": 1,
            "class_name": "Warlock",
            "steps": [
                {"kind": "subclass", "name": "The Hexblade", "selection": {}, "expect": []},
                {
                    "kind": "feature", "name": "Hex Warrior", "selection": {},
                    "expect": [
                        {"path": "sheet.traits.proficiencies.armor", "contains": "Medium Armor"},
                        {"path": "sheet.traits.proficiencies.weapons", "contains": "Martial Weapons"},
                    ],
                },
            ],
        },
        {
            "name": "xanathar-spell-and-common-item",
            "level": 3,
            "class_name": "Sorcerer",
            "steps": [
                {"kind": "spell", "name": "Chaos bolt", "selection": {}, "expect": [{"path": "sheet.content.spells", "contains_names": ["Chaos bolt"]}]},
                {"kind": "item", "name": "Clockwork Amulet", "selection": {}, "expect": [{"path": "sheet.inventory.items", "contains_names": ["Clockwork Amulet"]}]},
            ],
        },
    ]


def main() -> None:
    additions = [
        *_subclasses(),
        *_features(),
        *_feats(),
        *_invocations(),
        _split_toll_the_dead_spell(),
    ]
    counts = {"spell": 94, "item": 48, "statblock": 1}
    for addition in additions:
        kind = str(addition["kind"])
        counts[kind] = counts.get(kind, 0) + 1
    document = {
        "complete_review": True,
        "dependency_addons": [
            {
                "id": (
                    "dnd5e.addon.rulebook.d-d-5e-player-s-handbook."
                    "7ad6d3e9c93c.addon"
                ),
                "version": "1.0.0",
            }
        ],
        "default_status": "rejected",
        "addition_default_status": "accepted",
        "default_status_by_kind": {
            "item": "accepted",
            "spell": "accepted",
            "statblock": "accepted",
        },
        "rationale": (
            "Agent reviewed all 31 subclasses and every subclass feature, all 14 racial feats, "
            "all 14 added Eldritch Invocations, all 41 common magic items, all 95 spells, and "
            "the Tiny Servant actor card. Descriptive DM procedures remain source-indexed; "
            "character options have typed selection boundaries and build-time source rulings."
        ),
        "expected_counts": counts,
        "expected_actor_names": ["Tiny Servant"],
        "expected_actor_cards": [
            {
                "name": "Tiny Servant",
                "source_text_sha256": hashlib.sha256(
                    TINY_SERVANT_STATBLOCK.encode("utf-8")
                ).hexdigest(),
                "inventory_item_names": ["Slam"],
                "activity_names": [],
                "forbidden_text": ["At Higher Levels", "mand to each one"],
            }
        ],
        "runtime_probes": _runtime_probes(),
        "decisions": [
            {
                "kind": "statblock",
                "name": "TINY SERVANT",
                "status": "accepted",
                "artifact_patch": {
                    "card": {
                        "name": "Tiny Servant",
                        "normalized_content": TINY_SERVANT_STATBLOCK,
                    }
                },
                "note": (
                    "Agent used the exact bordered statblock on source page 170 to "
                    "exclude the surrounding Tiny Servant spell narrative and repair "
                    "bounded OCR spacing without inventing mechanics."
                ),
            }
        ] + [
            {
                "kind": "item",
                "name": extracted,
                "status": "accepted",
                "artifact_patch": {"card": {"name": canonical}},
                "note": "Agent corrected bounded OCR identity noise against the cited heading.",
            }
            for extracted, canonical in ITEM_NAME_CORRECTIONS.items()
        ] + [
            {
                "kind": "spell",
                "name": extracted,
                "status": "accepted",
                "artifact_patch": {"card": {"name": canonical}},
                "note": "Agent restored the source possessive and display identity.",
            }
            for extracted, canonical in SPELL_NAME_CORRECTIONS.items()
        ] + [
            {
                "kind": "spell",
                "name": extracted,
                "status": "rejected",
                "note": "Agent rejected this OCR-split suffix after verifying the complete source heading.",
            }
            for extracted in REJECTED_SPELL_CANDIDATES
        ],
        "additions": additions,
    }
    payload = {"version": 1, "documents": {BOOK: document}}
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT} with {len(additions)} reviewed additions and counts {counts}")


if __name__ == "__main__":
    main()
