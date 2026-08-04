"""Build the source-reviewed Sword Coast Adventurer's Guide addon fixture."""

# ruff: noqa: E501, I001 -- printed names and source-facing tables stay directly auditable.

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "fixtures" / "books_catalog_review_sword_coast_v1.json"
BOOK = "D&D 5E - Sword Coast Adventurer's Guide.pdf"

SKILLS = [
    "Arcana", "Athletics", "Deception", "History", "Insight", "Investigation",
    "Nature", "Perception", "Persuasion", "Religion", "Stealth", "Survival",
]
ARTISAN_TOOLS = [
    "Alchemist's Supplies", "Brewer's Supplies", "Calligrapher's Supplies",
    "Carpenter's Tools", "Cartographer's Tools", "Cobbler's Tools",
    "Cook's Utensils", "Glassblower's Tools", "Jeweler's Tools",
    "Leatherworker's Tools", "Mason's Tools", "Painter's Supplies",
    "Potter's Tools", "Smith's Tools", "Tinker's Tools", "Weaver's Tools",
    "Woodcarver's Tools",
]
GAMING_SETS = ["Dice", "Dragonchess", "Playing Cards", "Three-Dragon Ante"]
INSTRUMENTS = [
    "Bagpipes", "Drum", "Dulcimer", "Flute", "Horn", "Lute", "Lyre",
    "Pan Flute", "Shawm", "Viol",
]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def _pack_id() -> str:
    digest = hashlib.sha256(BOOK.encode("utf-8")).hexdigest()[:12]
    return f"dnd5e.addon.rulebook.{_slug(Path(BOOK).stem)[:80]}.{digest}"


def _selector(heading: str, page: int, **extra: Any) -> dict[str, Any]:
    return {"heading_exact": heading, "page_start": page, **extra}


def _addition(
    kind: str,
    name: str,
    selectors: list[dict[str, Any]],
    card: dict[str, Any],
) -> dict[str, Any]:
    return {"kind": kind, "name": name, "source_selectors": selectors, "card": card}


def _feature_summary(name: str, description: str) -> dict[str, str]:
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
    ignore_material_components: bool = False,
) -> dict[str, Any]:
    grant: dict[str, Any] = {
        "name": name,
        "level": level,
        "eligible_classes": [eligible_class],
        "method": method,
        "spellcasting_ability": ability,
        "free_casts": free_casts,
        "recovers_on": recovers_on,
        "allow_slot_cast": allow_slot_cast,
        "minimum_level": minimum_level,
        "ritual_only": False,
    }
    if ignore_material_components:
        grant["casting_overrides"] = {"ignore_material_components": True}
    return grant


def _cantrip(name: str, ability: str, *, eligible_class: str = "Wizard") -> dict[str, Any]:
    return _spell_grant(
        name,
        0,
        ability,
        eligible_class=eligible_class,
        method="known",
        free_casts=0,
        recovers_on=None,
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


def _species() -> list[dict[str, Any]]:
    elf_base = {
        "base_species": "Half-Elf",
        "abilities": {"charisma": 2},
        "ability_choice": {
            "count": 2,
            "amount": 1,
            "exclude": ["charisma"],
            "options": ["strength", "dexterity", "constitution", "intelligence", "wisdom"],
        },
        "size": "medium",
        "speed": 30,
        "darkvision_ft": 60,
        "languages": ["Common", "Elvish"],
        "language_choice_count": 1,
        "language_options": [],
        "allow_any_language": True,
    }
    half_elf_features = [
        _feature_summary("Darkvision", "See in darkness to the source distance."),
        _feature_summary("Fey Ancestry", "Advantage against charm and immunity to magical sleep."),
    ]
    half_elves = [
        (
            "Half-Elf (Wood Elf + Keen Senses)",
            {"skill_proficiencies": ["Perception"]},
            _feature_summary("Keen Senses", "Replaces Skill Versatility and grants Perception proficiency."),
        ),
        (
            "Half-Elf (Wood Elf + Elf Weapon Training)",
            {"weapon_proficiencies": ["Longsword", "Shortsword", "Shortbow", "Longbow"]},
            _feature_summary("Elf Weapon Training", "Replaces Skill Versatility with the printed elf weapons."),
        ),
        (
            "Half-Elf (Wood Elf + Fleet of Foot)",
            {"walk_speed": 35},
            _feature_summary("Fleet of Foot", "Replaces Skill Versatility and increases walking speed to 35 feet."),
        ),
        (
            "Half-Elf (Wood Elf + Mask of the Wild)",
            {},
            _feature_summary("Mask of the Wild", "Replaces Skill Versatility with the source concealment permission."),
        ),
        (
            "Half-Elf (High Elf + Elf Weapon Training)",
            {"weapon_proficiencies": ["Longsword", "Shortsword", "Shortbow", "Longbow"]},
            _feature_summary("Elf Weapon Training", "Replaces Skill Versatility with the printed elf weapons."),
        ),
        (
            "Half-Elf (High Elf + Cantrip)",
            {
                "cantrip_choice": {
                    "class": "Wizard",
                    "level": 0,
                    "spellcasting_ability": "intelligence",
                    "method": "known",
                    "free_casts": 0,
                    "recovers_on": None,
                    "allow_slot_cast": False,
                    "minimum_level": 1,
                    "ritual_only": False,
                }
            },
            _feature_summary("Cantrip", "Replaces Skill Versatility with one Wizard cantrip using Intelligence."),
        ),
        (
            "Half-Elf (Drow + Drow Magic)",
            {
                "spell_grants": [
                    _cantrip("Dancing Lights", "charisma"),
                    _spell_grant("Faerie Fire", 1, "charisma", minimum_level=3),
                    _spell_grant("Darkness", 2, "charisma", minimum_level=5),
                ]
            },
            _feature_summary("Drow Magic", "Replaces Skill Versatility with the source level-gated drow spells."),
        ),
        (
            "Half-Elf (Aquatic + Swimming Speed)",
            {"swim_speed": 30},
            _feature_summary("Swimming", "Replaces Skill Versatility with a 30-foot swimming speed."),
        ),
    ]
    result = [
        _addition(
            "species",
            "Duergar",
            [_selector("DUERGAR SUBRACE TRAITS", 105)],
            _species_card(
                base_species="Dwarf",
                abilities={"constitution": 2, "strength": 1},
                size="medium",
                speed=25,
                darkvision_ft=120,
                languages=["Common", "Dwarvish", "Undercommon"],
                weapon_proficiencies=["Battleaxe", "Handaxe", "Light Hammer", "Warhammer"],
                tool_choice_count=1,
                tool_options=["Smith's Tools", "Brewer's Supplies", "Mason's Tools"],
                resistances=["poison"],
                spell_grants=[
                    _spell_grant("Enlarge/Reduce", 2, "intelligence", minimum_level=3),
                    _spell_grant("Invisibility", 2, "intelligence", minimum_level=5),
                ],
                features=[
                    _feature_summary("Dwarven Resilience", "Source poison saving-throw advantage and resistance."),
                    _feature_summary("Stonecunning", "Source History expertise-like stonework check benefit."),
                    _feature_summary("Duergar Resilience", "Advantage against illusions, charm, and paralysis."),
                    _feature_summary("Duergar Magic", "The level-gated spells cannot be cast in direct sunlight."),
                    _feature_summary("Sunlight Sensitivity", "Source attack and Perception penalty in direct sunlight."),
                ],
            ),
        ),
        _addition(
            "species",
            "Ghostwise Halfling",
            [_selector("GHOSTWISE HALFLINGS", 111)],
            _species_card(
                base_species="Halfling",
                abilities={"dexterity": 2, "wisdom": 1},
                size="small",
                speed=25,
                languages=["Common", "Halfling"],
                features=[
                    _feature_summary("Lucky", "Reroll a natural 1 on an attack, check, or save."),
                    _feature_summary("Brave", "Advantage on saves against frightened."),
                    _feature_summary("Halfling Nimbleness", "Move through spaces of larger creatures."),
                    _feature_summary("Silent Speech", "Telepathically speak to one creature within 30 feet sharing a language."),
                ],
            ),
        ),
        _addition(
            "species",
            "Deep Gnome",
            [_selector("DEEP GNOMES (SVIRFNEBLIN)", 116, content_contains="Stone Camouflage")],
            _species_card(
                base_species="Gnome",
                abilities={"intelligence": 2, "dexterity": 1},
                size="small",
                speed=25,
                darkvision_ft=120,
                languages=["Common", "Gnomish", "Undercommon"],
                features=[
                    _feature_summary("Gnome Cunning", "Advantage on mental saves against magic."),
                    _feature_summary("Stone Camouflage", "Advantage to hide in rocky terrain."),
                ],
            ),
        ),
    ]
    for name, extra, variant_feature in half_elves:
        result.append(
            _addition(
                "species",
                name,
                [_selector("HALF-ELF VARIANTS", 117)],
                _species_card(features=[*half_elf_features, variant_feature], **elf_base, **extra),
            )
        )

    infernal_spells = [
        _cantrip("Thaumaturgy", "charisma", eligible_class="Cleric"),
        _spell_grant("Hellish Rebuke", 1, "charisma", eligible_class="Warlock", minimum_level=3),
        _spell_grant("Darkness", 2, "charisma", minimum_level=5),
    ]
    devil_spells = [
        _cantrip("Vicious Mockery", "charisma", eligible_class="Bard"),
        _spell_grant("Charm Person", 1, "charisma", minimum_level=3),
        _spell_grant("Enthrall", 2, "charisma", eligible_class="Bard", minimum_level=5),
    ]
    hellfire_spells = [
        _cantrip("Thaumaturgy", "charisma", eligible_class="Cleric"),
        _spell_grant("Burning Hands", 1, "charisma", minimum_level=3),
        _spell_grant("Darkness", 2, "charisma", minimum_level=5),
    ]
    tieflings = [
        ("Tiefling (Feral)", {"dexterity": 2, "intelligence": 1}, infernal_spells, 0, "Infernal Legacy"),
        ("Tiefling (Devil's Tongue)", {"charisma": 2, "intelligence": 1}, devil_spells, 0, "Devil's Tongue"),
        ("Tiefling (Feral + Devil's Tongue)", {"dexterity": 2, "intelligence": 1}, devil_spells, 0, "Devil's Tongue"),
        ("Tiefling (Hellfire)", {"charisma": 2, "intelligence": 1}, hellfire_spells, 0, "Hellfire"),
        ("Tiefling (Feral + Hellfire)", {"dexterity": 2, "intelligence": 1}, hellfire_spells, 0, "Hellfire"),
        ("Tiefling (Winged)", {"charisma": 2, "intelligence": 1}, [], 30, "Winged"),
        ("Tiefling (Feral + Winged)", {"dexterity": 2, "intelligence": 1}, [], 30, "Winged"),
    ]
    for name, abilities, spells, fly_speed, legacy in tieflings:
        extra: dict[str, Any] = {"spell_grants": spells}
        if fly_speed:
            extra["fly_speed"] = fly_speed
        result.append(
            _addition(
                "species",
                name,
                [_selector("TIEFLING VARIANTS", 119)],
                _species_card(
                    base_species="Tiefling",
                    abilities=abilities,
                    size="medium",
                    speed=30,
                    darkvision_ft=60,
                    languages=["Common", "Infernal"],
                    resistances=["fire"],
                    features=[
                        _feature_summary("Darkvision", "See in darkness to 60 feet."),
                        _feature_summary("Hellish Resistance", "Resistance to fire damage."),
                        _feature_summary(legacy, "The exact mutually exclusive source variant replacing the base legacy."),
                    ],
                    **extra,
                ),
            )
        )
    return result


def _feat_and_item() -> list[dict[str, Any]]:
    return [
        _addition(
            "feat",
            "Svirfneblin Magic",
            [_selector("DEEP GNOME FEAT", 116)],
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
                            "Nondetection", 3, "intelligence", method="at_will",
                            free_casts=0, recovers_on=None,
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
        _addition(
            "item",
            "Spiked Armor",
            [_selector("SPIKED ARMOR", 122)],
            {
                "inventory_template": {
                    "name": "Spiked Armor",
                    "kind": "armor",
                    "quantity": 1,
                    "description": "Medium armor used by Battleragers; 75 gp and 45 lb.",
                    "weight_oz": 720,
                    "mechanics": {
                        "base_ac": 14,
                        "dexterity_mode": "max",
                        "dexterity_max": 2,
                        "stealth_disadvantage": True,
                    },
                }
            },
        ),
    ]


def _subclass(
    name: str,
    class_name: str,
    level: int,
    page: int,
    *,
    always_prepared: list[tuple[str, int]] | None = None,
    expansion: list[str] | None = None,
) -> dict[str, Any]:
    return _addition(
        "subclass",
        name,
        [_selector(name.upper(), page)],
        {
            "name": name,
            "class_name": class_name,
            "minimum_level": level,
            "always_prepared_spells": [
                {"name": spell, "minimum_level": unlock}
                for spell, unlock in (always_prepared or [])
            ],
            "spell_list_expansion": list(expansion or []),
        },
    )


def _feature_card(
    name: str,
    class_name: str,
    subclass_name: str,
    level: int,
    page: int,
    *,
    description: str,
    mechanical_grants: dict[str, Any] | None = None,
    selection_requirements: dict[str, Any] | None = None,
    feature_subtype: str = "",
) -> dict[str, Any]:
    return _addition(
        "feature",
        name,
        [_selector(name.upper(), page)],
        {
            "name": name,
            "class_name": class_name,
            "subclass_name": subclass_name,
            "feature_subtype": feature_subtype,
            "minimum_level": level,
            "description": description,
            "repeatable_selection_levels": [],
            "selection_requirements": selection_requirements or {},
            "selection_requirements_by_level": {},
            "mechanical_grants": mechanical_grants or {},
        },
    )


def _subclasses_and_features() -> list[dict[str, Any]]:
    arcana_spells = list(
        zip(
            [
                "Detect Magic", "Magic Missile", "Magic Weapon", "Nystul's Magic Aura",
                "Dispel Magic", "Magic Circle", "Arcane Eye", "Leomund's Secret Chest",
                "Planar Binding", "Teleportation Circle",
            ],
            [1, 1, 3, 3, 5, 5, 7, 7, 9, 9],
            strict=True,
        )
    )
    crown_spells = list(
        zip(
            [
                "Command", "Compelled Duel", "Warding Bond", "Zone of Truth",
                "Aura of Vitality", "Spirit Guardians", "Banishment", "Guardian of Faith",
                "Circle of Power", "Geas",
            ],
            [3, 3, 5, 5, 9, 9, 13, 13, 17, 17],
            strict=True,
        )
    )
    undying_expansion = [
        "False Life", "Ray of Sickness", "Blindness/Deafness", "Silence",
        "Feign Death", "Speak with Dead", "Aura of Life", "Death Ward",
        "Contagion", "Legend Lore",
    ]
    result = [
        _subclass("Path of the Battlerager", "Barbarian", 3, 122),
        _subclass("Arcana Domain", "Cleric", 1, 126, always_prepared=arcana_spells),
        _subclass("Purple Dragon Knight", "Fighter", 3, 129),
        _subclass("Way of the Long Death", "Monk", 3, 131),
        _subclass("Way of the Sun Soul", "Monk", 3, 132),
        _addition(
            "subclass",
            "Oath of the Crown",
            [_selector("OATH OF THE CROW-N", 133), _selector("OATH OF THE CROWN SPELLS", 134)],
            {
                "name": "Oath of the Crown",
                "class_name": "Paladin",
                "minimum_level": 3,
                "always_prepared_spells": [
                    {"name": spell, "minimum_level": unlock}
                    for spell, unlock in crown_spells
                ],
                "spell_list_expansion": [],
            },
        ),
        _subclass("Mastermind", "Rogue", 3, 136),
        _subclass("Swashbuckler", "Rogue", 3, 136),
        _subclass("Storm Sorcery", "Sorcerer", 1, 138),
        _subclass("The Undying", "Warlock", 1, 140, expansion=undying_expansion),
        _subclass("Bladesinging", "Wizard", 2, 142),
    ]
    specs = [
        ("Battlerager Armor", "Barbarian", "Path of the Battlerager", 3, 122, "Use spiked armor for the printed rage bonus attack and grapple damage.", {}),
        ("Reckless Abandon", "Barbarian", "Path of the Battlerager", 6, 122, "Reckless Attack while raging grants Constitution-modifier temporary hit points.", {}),
        ("Battlerager Charge", "Barbarian", "Path of the Battlerager", 10, 122, "Dash as a bonus action while raging.", {}),
        ("Spiked Retribution", "Barbarian", "Path of the Battlerager", 14, 122, "A nearby attacker takes the printed piercing damage while you rage in spiked armor.", {}),
        ("Channel Divinity: Arcane Abjuration", "Cleric", "Arcana Domain", 2, 126, "Turn one celestial, elemental, fey, or fiend using the printed Channel Divinity save.", {}),
        ("Arcane Banishment", "Cleric", "Arcana Domain", 5, 127, "Upgrades Arcane Abjuration to banish qualifying creatures by CR.", {}),
        ("Spell Breaker", "Cleric", "Arcana Domain", 6, 127, "Healing a creature with a spell can end a qualifying spell on it.", {}),
        ("Potent Spellcasting", "Cleric", "Arcana Domain", 8, 127, "Add Wisdom modifier to cleric cantrip damage.", {}),
        ("Rallying Cry", "Fighter", "Purple Dragon Knight", 3, 129, "Second Wind also heals up to three visible or audible allies by fighter level.", {}),
        ("Inspiring Surge", "Fighter", "Purple Dragon Knight", 10, 129, "Action Surge lets an ally use its reaction for one weapon attack; two allies at level 18.", {}),
        ("Bulwark", "Fighter", "Purple Dragon Knight", 15, 129, "Indomitable can extend its reroll to one failing ally on the same effect.", {}),
        ("Touch of Death", "Monk", "Way of the Long Death", 3, 131, "Reducing a nearby creature to 0 HP grants Wisdom plus monk-level temporary HP.", {}),
        ("Hour of Reaping", "Monk", "Way of the Long Death", 6, 131, "Action frightens nearby creatures that can see you on a Wisdom save.", {}),
        ("Mastery of Death", "Monk", "Way of the Long Death", 11, 132, "Spend 1 ki to remain at 1 HP instead of falling to 0 HP.", {}),
        ("Touch of the Long Death", "Monk", "Way of the Long Death", 17, 132, "Spend 1-10 ki after touching a creature for the printed necrotic damage save.", {}),
        ("Radiant Sun Bolt", "Monk", "Way of the Sun Soul", 3, 132, "Ranged radiant spell attack uses Dexterity and Martial Arts die; supports Flurry bolts.", {}),
        ("Searing Arc Strike", "Monk", "Way of the Sun Soul", 6, 132, "After Attack, spend ki to cast and upcast Burning Hands as a bonus action.", {}),
        ("Searing Sunburst", "Monk", "Way of the Sun Soul", 11, 132, "Action creates a ranged radiant burst; spend up to 3 ki to add damage dice.", {}),
        ("Sun Shield", "Monk", "Way of the Sun Soul", 17, 132, "Emit or suppress light; reaction deals Wisdom-modifier radiant damage to a melee attacker.", {}),
        ("Channel Divinity", "Paladin", "Oath of the Crown", 3, 134, "Champion Challenge and Turn the Tide use the paladin's Channel Divinity.", {}),
        ("Divine Allegiance", "Paladin", "Oath of the Crown", 7, 134, "Reaction takes damage for a creature within 5 feet without transferring other effects.", {}),
        ("Unyielding Spirit", "Paladin", "Oath of the Crown", 15, 134, "Advantage on saves against paralysis and stun.", {}),
        ("Exalted Champion", "Paladin", "Oath of the Crown", 20, 134, "One-hour transformation grants the printed resistances, ally save advantage, and death-save benefit once per long rest.", {}),
        ("Master of Tactics", "Rogue", "Mastermind", 3, 136, "Help as a bonus action, including aiding an attack within 30 feet of the target.", {}),
        ("Insightful Manipulator", "Rogue", "Mastermind", 9, 136, "One minute of interaction reveals the source-defined comparative capabilities.", {}),
        ("Misdirection", "Rogue", "Mastermind", 13, 136, "Redirect a cover-providing attack to the creature granting cover.", {}),
        ("Soul of Deceit", "Rogue", "Mastermind", 17, 136, "Thought reading and truth magic receive the exact source false result.", {}),
        ("Fancy Footwork", "Rogue", "Swashbuckler", 3, 136, "A creature you melee attack cannot opportunity attack you that turn.", {}),
        ("Rakish Audacity", "Rogue", "Swashbuckler", 3, 137, "Add Charisma to initiative and gain the printed isolated-duel Sneak Attack route.", {}),
        ("Panache", "Rogue", "Swashbuckler", 9, 137, "Persuasion contested by Insight produces the source combat or noncombat effect.", {}),
        ("Elegant Maneuver", "Rogue", "Swashbuckler", 13, 137, "Bonus action grants advantage on the next Acrobatics or Athletics check that turn.", {}),
        ("Master Duelist", "Rogue", "Swashbuckler", 17, 137, "Reroll a missed attack with advantage once per short or long rest.", {"resources": {"feature:swashbuckler:master_duelist": {"label": "Master Duelist", "value": 1, "max": 1, "recovers_on": "short_rest", "source_key": "Master Duelist"}}}),
        ("Wind Speaker", "Sorcerer", "Storm Sorcery", 1, 138, "Speak, read, and write Primordial and its four elemental dialects.", {"languages": ["Primordial"]}),
        ("Tempestuous Magic", "Sorcerer", "Storm Sorcery", 1, 138, "Bonus action before or after a 1st-level-or-higher spell flies 10 feet without opportunity attacks.", {}),
        ("Heart of the Storm", "Sorcerer", "Storm Sorcery", 6, 138, "Lightning/thunder resistance and source-level splash damage when casting such spells.", {"resistances": ["lightning", "thunder"]}),
        ("Storm Guide", "Sorcerer", "Storm Sorcery", 6, 138, "Use an action to stop rain or a bonus action to redirect wind in the source area.", {}),
        ("Storm's Fury", "Sorcerer", "Storm Sorcery", 14, 138, "Reaction damages and may push a melee attacker on a failed Strength save.", {}),
        ("Wind Soul", "Sorcerer", "Storm Sorcery", 18, 138, "Lightning/thunder immunity, flight, and the source one-hour party flight grant.", {}),
        ("Among the Dead", "Warlock", "The Undying", 1, 140, "Learn Spare the Dying and gain the source disease and undead-targeting defenses.", {}),
        ("Defy Death", "Warlock", "The Undying", 6, 141, "Regain source hit points after a death save success or Spare the Dying once per long rest.", {"resources": {"feature:undying:defy_death": {"label": "Defy Death", "value": 1, "max": 1, "recovers_on": "long_rest", "source_key": "Defy Death"}}}),
        ("Undying Nature", "Warlock", "The Undying", 10, 141, "Source breathing, sustenance, aging, sleep, and magical-aging protections.", {}),
        ("Indestructible Life", "Warlock", "The Undying", 14, 141, "Bonus action regains source hit points and can reattach a severed part once per rest.", {"resources": {"feature:undying:indestructible_life": {"label": "Indestructible Life", "value": 1, "max": 1, "recovers_on": "short_rest", "source_key": "Indestructible Life"}}}),
        ("Bladesong", "Wizard", "Bladesinging", 2, 143, "Bonus action starts the one-minute source AC, speed, Acrobatics, and concentration benefits.", {"resources": {"feature:bladesinger:bladesong": {"label": "Bladesong", "value": 2, "max": 2, "recovers_on": "short_rest", "source_key": "Bladesong"}}}),
        ("Extra Attack", "Wizard", "Bladesinging", 6, 143, "Attack twice when taking the Attack action.", {}),
        ("Song of Defense", "Wizard", "Bladesinging", 10, 143, "Reaction spends a spell slot to reduce damage by five times the slot level.", {}),
        ("Song of Victory", "Wizard", "Bladesinging", 14, 143, "Add Intelligence modifier to melee weapon damage while Bladesong is active.", {}),
    ]
    for name, class_name, subclass_name, level, page, description, grants in specs:
        if "resistances" in grants:
            # Feature grants deliberately support durable proficiencies/resources only;
            # conditional or level-changing resistances remain source-bound Agent context.
            grants = {}
        feature = _feature_card(
                name,
                class_name,
                subclass_name,
                level,
                page,
                description=description,
                mechanical_grants=grants,
            )
        if name == "Reckless Abandon":
            feature["source_selectors"] = [
                _selector("BATTLERAGER ARMOR", 122, content_contains="ReCKLESS ABANDON")
            ]
        result.append(feature)

    result.extend(
        [
            _feature_card(
                "Arcane Initiate", "Cleric", "Arcana Domain", 1, 126,
                description="Gain Arcana proficiency, or expertise when already proficient, and two Wizard cantrips treated as cleric cantrips.",
                mechanical_grants={"skill_proficiency_or_expertise": ["Arcana"]},
                selection_requirements={
                    "field": "arcane_cantrips",
                    "kind": "known_spell_grants",
                    "count": 2,
                    "required_spell_levels": [0, 0],
                    "eligible_class": "Wizard",
                    "source_class": "Cleric",
                    "grant_method": "known",
                },
            ),
            _feature_card(
                "Arcane Mastery", "Cleric", "Arcana Domain", 17, 127,
                description="Choose one Wizard spell of each level 6 through 9; each becomes a domain spell and is always prepared.",
                selection_requirements={
                    "field": "arcane_mastery_spells",
                    "kind": "known_spell_grants",
                    "count": 4,
                    "required_spell_levels": [6, 7, 8, 9],
                    "eligible_class": "Wizard",
                    "source_class": "Cleric",
                    "grant_method": "class_prepared",
                    "always_prepared": True,
                },
            ),
            _feature_card(
                "Royal Envoy", "Fighter", "Purple Dragon Knight", 7, 129,
                description="Gain Persuasion proficiency, or double proficiency when already proficient.",
                mechanical_grants={"skill_proficiency_or_expertise": ["Persuasion"]},
            ),
            _feature_card(
                "Master of Intrigue", "Rogue", "Mastermind", 3, 136,
                description="Gain Disguise Kit and Forgery Kit plus two languages and one gaming set; mimicry remains source context.",
                mechanical_grants={"tool_proficiencies": ["Disguise Kit", "Forgery Kit"]},
                selection_requirements={
                    "field": "intrigue_proficiencies",
                    "kind": "proficiency_grants",
                    "groups": [
                        {"id": "languages", "kind": "language", "count": 2, "options": [], "allow_unlisted": True},
                        {"id": "gaming_set", "kind": "tool", "count": 1, "options": GAMING_SETS},
                    ],
                },
            ),
            _feature_card(
                "Training in War and Song", "Wizard", "Bladesinging", 2, 143,
                description="Gain light armor and one one-handed melee weapon proficiency.",
                mechanical_grants={"armor_proficiencies": ["light armor"]},
                selection_requirements={
                    "field": "war_and_song_training",
                    "kind": "proficiency_grants",
                    "groups": [
                        {
                            "id": "weapon",
                            "kind": "weapon",
                            "count": 1,
                            "options": [
                                "Club", "Dagger", "Handaxe", "Javelin", "Light Hammer",
                                "Mace", "Quarterstaff", "Sickle", "Spear", "Battleaxe",
                                "Flail", "Longsword", "Morningstar", "Rapier", "Scimitar",
                                "Shortsword", "Trident", "War Pick", "Warhammer", "Whip",
                            ],
                        }
                    ],
                },
            ),
        ]
    )
    # The fixed Spare the Dying grant belongs on the subclass card so it materializes
    # without inventing a no-choice feature spell selector.
    for addition in result:
        if addition["kind"] == "subclass" and addition["name"] == "The Undying":
            addition["card"]["spell_grants"] = [
                {"name": "Spare the Dying", "minimum_level": 1, "method": "known"}
            ]
            break
    return result


def _totem_options() -> list[dict[str, Any]]:
    pack_id = _pack_id()
    tiers = [
        (
            "Totem Spirit", 3, 123,
            [
                ("Totem Spirit (Elk)", "While raging, walking speed increases by 15 feet."),
                ("Totem Spirit (Tiger)", "While raging, long jump increases 10 feet and high jump 3 feet."),
            ],
        ),
        (
            "Aspect of the Beast", 6, 123,
            [
                ("Aspect of the Beast (Elk)", "Mounted or foot travel pace doubles for up to ten companions within 60 feet."),
                ("Aspect of the Beast (Tiger)", "Gain proficiency in two of Athletics, Acrobatics, Stealth, and Survival."),
            ],
        ),
        (
            "Totemic Attunement", 14, 123,
            [
                ("Totemic Attunement (Elk)", "Bonus action passes through a Large-or-smaller creature and may knock it prone with source damage."),
                ("Totemic Attunement (Tiger)", "While raging, move at least 20 feet then make an extra melee weapon attack as a bonus action."),
            ],
        ),
    ]
    result: list[dict[str, Any]] = []
    for selector_name, level, page, options in tiers:
        names = [name for name, _ in options]
        selector_feature = _feature_card(
                selector_name, "Barbarian", "Path of the Totem Warrior", level, page,
                description="Choose exactly one of the source's additional Uthgardt totem options.",
                selection_requirements={
                    "field": "totem_option",
                    "kind": "feature_grants",
                    "count": 1,
                    "options": names,
                    "option_artifact_ids": {
                        name: f"{pack_id}.feature.{_slug(name)}" for name in names
                    },
                    "option_prerequisites": {name: {"minimum_level": level} for name in names},
                    "option_subtype": "selectable_option",
                },
            )
        source_heading = {
            3: "UTHGARDT TOTEMS",
            6: "ASPECT OF THE BEAST",
            14: "TOTEMIC ATTUNEMENT",
        }[level]
        selector_feature["source_selectors"] = [_selector(source_heading, page)]
        result.append(selector_feature)
        for name, description in options:
            option_feature = _feature_card(
                    name, "Barbarian", "Path of the Totem Warrior", level, page,
                    description=description,
                    feature_subtype="selectable_option",
                )
            option_feature["source_selectors"] = [_selector(source_heading, page)]
            result.append(option_feature)
    return result


def _inventory_item(name: str, description: str = "Source-reviewed background equipment.") -> dict[str, Any]:
    return {
        "inventory_template": {
            "name": name,
            "kind": "equipment",
            "quantity": 1,
            "description": description,
            "mechanics": {},
        }
    }


def _background(
    name: str,
    page: int,
    *,
    skills: list[str],
    feature: str,
    gp: int,
    items: list[str],
    skill_count: int = 0,
    skill_options: list[str] | None = None,
    languages: list[str] | None = None,
    language_count: int = 0,
    language_options: list[str] | None = None,
    allow_any_language: bool = False,
    tools: list[str] | None = None,
    tool_count: int = 0,
    tool_options: list[str] | None = None,
    tool_groups: list[dict[str, Any]] | None = None,
    include_selected_tool: bool = False,
    source_heading: str | None = None,
    source_match_all: bool = False,
) -> dict[str, Any]:
    equipment_items = [_inventory_item(item) for item in items]
    if include_selected_tool:
        equipment_items.append({"selected_tool": True})
    return _addition(
        "background",
        name,
        [_selector(source_heading or name.upper(), page, match_all=source_match_all)],
        {
            "name": name,
            "skill_proficiencies": skills,
            "background_grants": {
                "feature": feature,
                "skills": skills,
                "languages": languages or [],
                "spell_list_expansion": [],
                "tools": tools or [],
                "equipment_item_ids": [],
                "equipment": {"items": equipment_items, "wallet": {"gp": gp}},
                "choices": {
                    "skill_choice_count": skill_count,
                    "skill_options": skill_options or [],
                    "language_count": language_count,
                    "language_options": language_options or [],
                    "allow_any_language": allow_any_language,
                    "tool_choice_count": tool_count,
                    "tool_options": tool_options or [],
                    "tool_option_groups": tool_groups or [],
                },
            },
        },
    )


def _backgrounds() -> list[dict[str, Any]]:
    any_int_wis_cha = [
        "Animal Handling", "Arcana", "Deception", "History", "Intimidation",
        "Investigation", "Medicine", "Nature", "Perception", "Performance",
        "Persuasion", "Religion", "Survival",
    ]
    result = [
        _background("City Watch", 146, skills=["Athletics", "Insight"], feature="Watcher's Eye", gp=10, items=["Watch Uniform", "Horn", "Manacles"], language_count=2, allow_any_language=True),
        _background("Investigator", 146, skills=["Investigation", "Insight"], feature="Watcher's Eye", gp=10, items=["Investigator Uniform", "Horn", "Manacles"], language_count=2, allow_any_language=True, source_heading="VARIANT: INVESTIGATOR"),
        _background("Clan Crafter", 146, skills=["History", "Insight"], feature="Respect of the Stout Folk", gp=5, items=["Maker's Mark Chisel", "Traveler's Clothes", "10 gp Gem"], language_count=1, language_options=["Dwarvish"], tools=[], tool_count=1, tool_options=ARTISAN_TOOLS, include_selected_tool=True),
        _background("Cloistered Scholar", 147, skills=["History"], skill_count=1, skill_options=["Arcana", "Nature", "Religion"], feature="Library Access", gp=10, items=["Scholar's Robes", "Writing Kit", "Borrowed Book"], language_count=2, allow_any_language=True),
        _background("Courtier", 147, skills=["Insight", "Persuasion"], feature="Court Functionary", gp=5, items=["Fine Clothes"], language_count=2, allow_any_language=True),
        _background("Faction Agent", 148, skills=["Insight"], skill_count=1, skill_options=any_int_wis_cha, feature="Safe Haven", gp=15, items=["Faction Badge", "Faction Text", "Common Clothes"], language_count=2, allow_any_language=True, source_match_all=True),
        _background("Far Traveler", 149, skills=["Insight", "Perception"], feature="All Eyes on You", gp=5, items=["Poorly Wrought Maps", "10 gp Jewelry", "Traveler's Clothes"], language_count=1, allow_any_language=True, tool_count=1, tool_options=[*GAMING_SETS, *INSTRUMENTS], include_selected_tool=True),
        _background("Inheritor", 151, skills=["Survival"], skill_count=1, skill_options=["Arcana", "History", "Religion"], feature="Inheritance", gp=15, items=["Inheritance", "Traveler's Clothes"], language_count=1, allow_any_language=True, tool_count=1, tool_options=[*GAMING_SETS, *INSTRUMENTS]),
        _background("Knight of the Order", 152, skills=["Persuasion"], skill_count=1, skill_options=["Arcana", "History", "Nature", "Religion"], feature="Knightly Regard", gp=10, items=["Traveler's Clothes", "Order Insignia"], language_count=1, allow_any_language=True, tool_count=1, tool_options=[*GAMING_SETS, *INSTRUMENTS]),
        _background("Mercenary Veteran", 153, skills=["Athletics", "Persuasion"], feature="Mercenary Life", gp=10, items=["Mercenary Uniform", "Rank Insignia", "Common Clothes"], tools=["Vehicles (Land)"], tool_count=1, tool_options=GAMING_SETS, include_selected_tool=True),
        _background(
            "Urban Bounty Hunter", 154, skills=[], skill_count=2,
            skill_options=["Deception", "Insight", "Persuasion", "Stealth"],
            feature="Ear to the Ground", gp=20, items=["Work Clothes"], tool_count=2,
            tool_options=[*GAMING_SETS, *INSTRUMENTS, "Thieves' Tools"],
            tool_groups=[
                {"id": "gaming_set", "maximum": 1, "options": GAMING_SETS},
                {"id": "instrument", "maximum": 1, "options": INSTRUMENTS},
                {"id": "thieves_tools", "maximum": 1, "options": ["Thieves' Tools"]},
            ],
        ),
        _background("Uthgardt Tribe Member", 154, skills=["Athletics", "Survival"], feature="Uthgardt Heritage", gp=10, items=["Hunting Trap", "Tribal Totem", "Traveler's Clothes"], language_count=1, allow_any_language=True, tool_count=1, tool_options=[*ARTISAN_TOOLS, *INSTRUMENTS], source_match_all=True),
        _background("Waterdhavian Noble", 155, skills=["History", "Persuasion"], feature="Kept in Style", gp=20, items=["Fine Clothes", "Signet Ring", "Pedigree Scroll"], language_count=1, allow_any_language=True, tool_count=1, tool_options=[*GAMING_SETS, *INSTRUMENTS]),
    ]
    return result


def _runtime_probes() -> list[dict[str, Any]]:
    return [
        {
            "name": "scag-feral-winged-tiefling",
            "steps": [
                {
                    "kind": "species",
                    "name": "Tiefling (Feral + Winged)",
                    "selection": {},
                    "expect": [
                        {"path": "sheet.abilities.dexterity.score", "equals": 12},
                        {"path": "sheet.abilities.intelligence.score", "equals": 11},
                    ],
                }
            ],
        },
        {
            "name": "scag-urban-bounty-bounded-tools",
            "steps": [
                {
                    "kind": "background",
                    "name": "Urban Bounty Hunter",
                    "selection": {"skills": ["Deception", "Stealth"], "tools": ["Dice", "Lute"]},
                    "expect": [
                        {"path": "sheet.skills.deception.proficiency", "equals": "proficient"},
                        {"path": "sheet.traits.proficiencies.tools", "contains": "Dice"},
                        {"path": "sheet.inventory.wallet.gp", "equals": 20},
                    ],
                }
            ],
        },
        {
            "name": "scag-master-intrigue-groups",
            "level": 3,
            "class_name": "Rogue",
            "steps": [
                {
                    "kind": "subclass",
                    "name": "Mastermind",
                    "selection": {},
                    "expect": [
                        {
                            "path": "sheet.progression.classes",
                            "contains_names": ["Rogue"],
                        }
                    ],
                },
                {
                    "kind": "feature",
                    "name": "Master of Intrigue",
                    "selection": {"intrigue_proficiencies": {"languages": ["Elvish", "Goblin"], "gaming_set": ["Dice"]}},
                    "expect": [
                        {"path": "sheet.traits.languages", "contains": "Goblin"},
                        {"path": "sheet.traits.proficiencies.tools", "contains": "Disguise Kit"},
                        {"path": "sheet.traits.proficiencies.tools", "contains": "Dice"},
                    ],
                }
            ],
        },
        {
            "name": "scag-bladesinger-training",
            "level": 2,
            "class_name": "Wizard",
            "steps": [
                {
                    "kind": "subclass",
                    "name": "Bladesinging",
                    "selection": {},
                    "expect": [
                        {
                            "path": "sheet.progression.classes",
                            "contains_names": ["Wizard"],
                        }
                    ],
                },
                {
                    "kind": "feature",
                    "name": "Training in War and Song",
                    "selection": {"war_and_song_training": {"weapon": ["Rapier"]}},
                    "expect": [
                        {"path": "sheet.traits.proficiencies.armor", "contains": "light armor"},
                        {"path": "sheet.traits.proficiencies.weapons", "contains": "Rapier"},
                    ],
                }
            ],
        },
        {
            "name": "scag-spiked-armor",
            "steps": [
                {
                    "kind": "item",
                    "name": "Spiked Armor",
                    "selection": {},
                    "expect": [{"path": "sheet.inventory.items", "contains_names": ["Spiked Armor"]}],
                }
            ],
        },
    ]


def main() -> None:
    additions = [
        *_species(),
        *_feat_and_item(),
        *_subclasses_and_features(),
        *_totem_options(),
        *_backgrounds(),
    ]
    counts: dict[str, int] = {"spell": 4}
    for addition in additions:
        kind = str(addition["kind"])
        counts[kind] = counts.get(kind, 0) + 1
    document = {
        "complete_review": True,
        "default_status": "rejected",
        "addition_default_status": "accepted",
        "default_status_by_kind": {"spell": "accepted"},
        "rationale": (
            "Agent reviewed every printed player option: subraces and mutually exclusive "
            "variants, the deep-gnome feat, Spiked Armor, all eleven subclasses, the six "
            "additional totem options, four cantrips, the Investigator variant, and all "
            "twelve named backgrounds. Setting lore remains searchable source context."
        ),
        "expected_counts": counts,
        "runtime_probes": _runtime_probes(),
        "additions": additions,
    }
    payload = {"version": 1, "documents": {BOOK: document}}
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT} with {len(additions)} source-bound additions and 4 parsed spells")


if __name__ == "__main__":
    main()
