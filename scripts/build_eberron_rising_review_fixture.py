"""Build the source-reviewed Eberron: Rising from the Last War addon fixture."""

# ruff: noqa: E501, I001 -- source card tables remain directly comparable to the book.

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MAIN_FIXTURE = ROOT / "fixtures" / "books_catalog_review_v1.json"
OUTPUT = ROOT / "fixtures" / "books_catalog_review_eberron_rising_v1.json"
BOOK = "D&D 5E - Eberron - Rising from the Last War.pdf"
WAYFINDER = "D&D 5E - Wayfinders Guide to Eberron.pdf"

ARTISAN_TOOLS = [
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
]
ALL_TOOLS = [
    *ARTISAN_TOOLS,
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
    return {
        "kind": kind,
        "name": name,
        "source_selectors": selectors,
        "card": card,
    }


def _spell_grant(
    name: str,
    level: int,
    ability: str,
    *,
    method: str = "limited_use",
    free_casts: int = 1,
    recovers_on: str | None = "long_rest",
    minimum_level: int = 1,
    eligible_class: str = "Wizard",
    casting_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    grant: dict[str, Any] = {
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
    if casting_overrides:
        grant["casting_overrides"] = casting_overrides
    return grant


def _known_cantrip(name: str, ability: str, eligible_class: str = "Wizard") -> dict[str, Any]:
    return _spell_grant(
        name,
        0,
        ability,
        method="known",
        free_casts=0,
        recovers_on=None,
        eligible_class=eligible_class,
    )


def _feature(name: str, description: str) -> dict[str, str]:
    return {"name": name, "description": description}


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


def _house_backgrounds(wayfinder_additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for original in wayfinder_additions:
        if original["kind"] != "background" or not original["name"].startswith("House Agent"):
            continue
        cloned = copy.deepcopy(original)
        cloned["source_selectors"] = [
            _selector("HOUSE TOOL PROFIC I EN C I ES", 54),
            _selector("FEATURE: HOUSE CONNECTIONS", 54),
        ]
        result.append(cloned)
    return result


def _class_addition() -> dict[str, Any]:
    spell_list = [
        "Acid Splash", "Create Bonfire", "Dancing Lights", "Fire Bolt", "Frostbite",
        "Guidance", "Light", "Mage Hand", "Magic Stone", "Mending", "Message",
        "Poison Spray", "Prestidigitation", "Ray of Frost", "Resistance",
        "Shocking Grasp", "Spare the Dying", "Thorn Whip", "Thunderclap",
        "Absorb Elements", "Alarm", "Catapult", "Cure Wounds", "Detect Magic",
        "Disguise Self", "Expeditious Retreat", "Faerie Fire", "False Life",
        "Feather Fall", "Grease", "Identify", "Jump", "Longstrider",
        "Purify Food and Drink", "Sanctuary", "Snare", "Aid", "Alter Self",
        "Arcane Lock", "Blur", "Continual Flame", "Darkvision", "Enhance Ability",
        "Enlarge/Reduce", "Heat Metal", "Invisibility", "Lesser Restoration",
        "Levitate", "Magic Mouth", "Magic Weapon", "Protection from Poison",
        "Pyrotechnics", "Rope Trick", "See Invisibility", "Skywrite", "Spider Climb",
        "Web", "Blink", "Catnap", "Create Food and Water", "Dispel Magic",
        "Elemental Weapon", "Flame Arrows", "Fly", "Glyph of Warding", "Haste",
        "Protection from Energy", "Revivify", "Tiny Servant", "Water Breathing",
        "Water Walk", "Arcane Eye", "Elemental Bane", "Fabricate",
        "Freedom of Movement", "Leomund's Secret Chest", "Mordenkainen's Faithful Hound",
        "Mordenkainen's Private Sanctum", "Otiluke's Resilient Sphere", "Stone Shape",
        "Stoneskin", "Animate Objects", "Bigby's Hand", "Creation",
        "Greater Restoration", "Skill Empowerment", "Transmute Rock", "Wall of Stone",
    ]
    return _addition(
        "class",
        "Artificer",
        [
            _selector("C LAS S : ARTI FICER", 55),
            _selector("HIT POINTS", 55),
            _selector("PROFICIENCIES", 55),
            _selector("TH E ARTI FICER", 56),
            _selector("SPELLCASTING", 56),
            _selector("PREPARING AND CASTING SPELLS", 56),
            _selector("TH E MAG IC OF ARTI FICE", 57, match_all=True),
            _selector("SPELLCASTING ABILITY", 57),
            _selector("RITUAL CASTING", 57),
            _selector("ARTIFICER SPELL LIST", 57),
            _selector("CAN T R I PS (o LEVEL)", 57),
            _selector("3 R D LEVEL", 57),
            _selector("4T H LEVEL", 57),
            _selector("freedom of movement", 57),
            _selector("2 N D LEVEL", 57),
            _selector("STH LEVEL", 57),
        ],
        {
            "name": "Artificer",
            "class_definition": {
                "hit_die": 8,
                "saving_throw_proficiencies": ["constitution", "intelligence"],
                "armor_proficiencies": ["light armor", "medium armor", "shields"],
                "weapon_proficiencies": ["simple weapons"],
                "tool_proficiencies": ["Thieves' Tools", "Tinker's Tools"],
                "tool_choice_count": 1,
                "tool_options": [tool for tool in ARTISAN_TOOLS if tool != "Tinker's Tools"],
                "skill_choice_count": 2,
                "skill_options": [
                    "arcana", "history", "investigation", "medicine", "nature",
                    "perception", "sleight_of_hand",
                ],
                "spellcasting": {
                    "ability": "intelligence",
                    "class_list": "artificer",
                    "preparation_mode": "prepared",
                    "slot_progression": "half_round_up",
                    "ritual_casting": True,
                    "spellbook": False,
                    "cantrips_known_by_level": [2] * 9 + [3] * 4 + [4] * 7,
                    "leveled_spells_known_by_level": [],
                    "prepared_limit": {
                        "ability": "intelligence",
                        "class_level_divisor": 2,
                        "rounding": "down",
                        "minimum": 1,
                    },
                    "spell_list_expansion": spell_list,
                },
            },
        },
    )


def _class_features() -> list[dict[str, Any]]:
    pack_id = _pack_id()
    infusion_specs = [
        ("Boots of the Winding Path", 6, [_selector("BOOTS OF THE WINDING PATH", 63)]),
        ("Enhanced Arcane Focus", 2, [_selector("BOOTS OF THE WINDING PATH", 63, content_contains="Enhanced Arcane Focus")]),
        ("Enhanced Defense", 2, [_selector("ENHANCED DEFENSE", 63)]),
        ("Enhanced Weapon", 2, [_selector("ENHANCED WEAPON", 63)]),
        ("Homunculus Servant", 6, [_selector("HOMUNCULUS SERVANT", 63, content_contains="Prerequisite")]),
        ("Radiant Weapon", 6, [_selector("RADIANT WEAPON", 63), _selector("C HAPTER 1 I C HARACTER CREATION", 63, content_contains="30-foot radius")]),
        ("Repeating Shot", 2, [_selector("REPEATING SHOT", 63)]),
        ("Replicate Magic Item", 2, [_selector("REPLICATE MAGIC ITEM", 64), _selector("REPLICABLE ITE M S (2N D-LEVEL ARTIFI CER)", 64), _selector("REPLICABLE ITE M S (6TH-LEVEL ARTIFICER)", 64)]),
        ("Repulsion Shield", 6, [_selector("REPULSION SHIELD", 64)]),
        ("Resistant Armor", 6, [_selector("RESISTANT ARMOR", 64)]),
        ("Returning Weapon", 2, [_selector("RETURNING WEAPON", 64)]),
    ]
    option_ids = {
        name: f"{pack_id}.feature.{_slug(name)}" for name, _, _ in infusion_specs
    }
    options = [name for name, _, _ in infusion_specs]
    prerequisites = {name: {"minimum_level": level} for name, level, _ in infusion_specs}
    result = [
        _addition(
            "feature",
            "Infuse Item",
            [
                _selector("INFUSE ITE M", 58),
                _selector("INFUSIONS KNOWN", 58),
                _selector("INFUSING AN ITEM", 58),
            ],
            {
                "name": "Infuse Item",
                "class_name": "Artificer",
                "subclass_name": "",
                "minimum_level": 2,
                "repeatable_selection_levels": [6, 10, 14, 18],
                "selection_requirements": {
                    "field": "infusions",
                    "kind": "feature_grants",
                    "count": 4,
                    "options": options,
                    "option_artifact_ids": option_ids,
                    "option_prerequisites": prerequisites,
                    "option_subtype": "selectable_option",
                },
                "selection_requirements_by_level": {
                    str(level): {
                        "field": "infusions",
                        "kind": "feature_grants",
                        "count": 2,
                        "options": options,
                        "option_artifact_ids": option_ids,
                        "option_prerequisites": prerequisites,
                        "option_subtype": "selectable_option",
                    }
                    for level in (6, 10, 14, 18)
                },
                "mechanical_grants": {},
            },
        )
    ]
    base_specs = [
        ("Magical Tinkering", 1, [_selector("MAGICAL TI NKERING", 56, match_all=True)], {}),
        ("Spellcasting", 1, [_selector("SPELLCASTING", 56), _selector("TOOLS REQUIRED", 56), _selector("CANTRIPS (0 -LEVEL SPELLS)", 56), _selector("PREPARING AND CASTING SPELLS", 56)], {}),
        ("Artificer Specialist", 3, [_selector("ARTIFICER S PE C I ALIST", 58)], {}),
        ("The Right Tool for the Job", 3, [_selector("THE RIGHT TO OL FOR THE OB", 58)], {}),
        ("Tool Expertise", 6, [_selector("TO OL EXPERTI S E", 58)], {"tool_expertise_all": True}),
        ("Flash of Genius", 7, [_selector("FLASH OF GENIUS", 58)], {}),
        ("Magic Item Adept", 10, [_selector("MAGIC ITE M ADEPT", 58)], {}),
        ("Spell-Storing Item", 11, [_selector("SPELL- STORING ITEM", 59)], {}),
        ("Magic Item Savant", 14, [_selector("M AGIC ITEM SAVANT", 59)], {}),
        ("Magic Item Master", 18, [_selector("M AGIC ITE M MASTER", 59)], {}),
        ("Soul of Artifice", 20, [_selector("S OU L OF ARTI FICE", 59)], {}),
    ]
    for name, level, selectors, grants in base_specs:
        result.append(
            _addition(
                "feature",
                name,
                selectors,
                {
                    "name": name,
                    "class_name": "Artificer",
                    "subclass_name": "",
                    "minimum_level": level,
                    "repeatable_selection_levels": [],
                    "selection_requirements": {},
                    "selection_requirements_by_level": {},
                    "mechanical_grants": grants,
                },
            )
        )
    for name, level, selectors in infusion_specs:
        result.append(
            _addition(
                "feature",
                name,
                selectors,
                {
                    "name": name,
                    "class_name": "Artificer",
                    "subclass_name": "",
                    "feature_subtype": "selectable_option",
                    "minimum_level": level,
                    "repeatable_selection_levels": [],
                    "selection_requirements": {},
                    "selection_requirements_by_level": {},
                    "mechanical_grants": {},
                },
            )
        )
    return result


def _subclasses_and_features() -> list[dict[str, Any]]:
    subclass_spells = {
        "Alchemist": [
            "Healing Word", "Ray of Sickness", "Flaming Sphere", "Melf's Acid Arrow",
            "Gaseous Form", "Mass Healing Word", "Blight", "Death Ward", "Cloudkill",
            "Raise Dead",
        ],
        "Artillerist": [
            "Shield", "Thunderwave", "Scorching Ray", "Shatter", "Fireball", "Wind Wall",
            "Ice Storm", "Wall of Fire", "Cone of Cold", "Wall of Force",
        ],
        "Battle Smith": [
            "Heroism", "Shield", "Branding Smite", "Warding Bond", "Aura of Vitality",
            "Conjure Barrage", "Aura of Purity", "Fire Shield", "Banishing Smite",
            "Mass Cure Wounds",
        ],
    }
    subclass_selectors = {
        "Alchemist": [_selector("Alchemist", 59), _selector("ALCHEMIST SPELLS", 59, match_all=True)],
        "Artillerist": [_selector("ARTILLERIST", 60), _selector("ARTILLERIST SPELLS", 60, match_all=True)],
        "Battle Smith": [_selector("BATTLE SM ITH", 61), _selector("BATTLE SMITH SPELLS", 61), _selector("BATTLE SM ITH SPELLS", 62)],
    }
    result = [
        _addition(
            "subclass",
            name,
            subclass_selectors[name],
            {
                "name": name,
                "class_name": "Artificer",
                "minimum_level": 3,
                "always_prepared_spells": [
                    {"name": spell, "minimum_level": minimum_level}
                    for spell, minimum_level in zip(
                        spells,
                        [3, 3, 5, 5, 9, 9, 13, 13, 17, 17],
                        strict=True,
                    )
                ],
            },
        )
        for name, spells in subclass_spells.items()
    ]
    feature_specs = [
        ("Tool Proficiency (Alchemist)", "Alchemist", 3, "TOOL PROFICIENCY", 59, "Alchemist's Supplies", {}),
        ("Alchemist Spells", "Alchemist", 3, "ALCHEMIST SPELLS", 59, None, {}),
        ("Experimental Elixir", "Alchemist", 3, "EXPERIMENTAL ELIXIR", 59, None, {}),
        ("Alchemical Savant", "Alchemist", 5, "ALCHEMICAL SAVANT", 59, None, {}),
        ("Restorative Reagents", "Alchemist", 9, "RESTORATIVE REAGENTS", 60, None, {}),
        ("Chemical Mastery", "Alchemist", 15, "CHEMICAL MASTERY", 60, None, {}),
        ("Tool Proficiency (Artillerist)", "Artillerist", 3, "TOOL PROFICIENCY", 60, "Woodcarver's Tools", {}),
        ("Artillerist Spells", "Artillerist", 3, "ARTILLERIST SPELLS", 60, None, {}),
        ("Eldritch Cannon", "Artillerist", 3, "ELDRITCH CANNON", 60, None, {}),
        ("Arcane Firearm", "Artillerist", 5, "ARCANE FIREARM", 60, None, {}),
        ("Explosive Cannon", "Artillerist", 9, "EXPLOSIVE CANNON", 61, None, {}),
        ("Fortified Position", "Artillerist", 15, "FORTIFIED POSITION", 61, None, {}),
        ("Tool Proficiency (Battle Smith)", "Battle Smith", 3, "TOOL PROFICIENCY", 61, "Smith's Tools", {}),
        ("Battle Smith Spells", "Battle Smith", 3, "BATTLE SMITH SPELLS", 61, None, {}),
        ("Battle Ready", "Battle Smith", 3, "BATTLE READY", 62, None, {"weapon_proficiencies": ["martial weapons"]}),
        ("Steel Defender", "Battle Smith", 3, "STEEL DEFENDER", 62, None, {}),
        ("Extra Attack", "Battle Smith", 5, "EXTRA ATTACK", 62, None, {"attack_scaling": {"class_name": "Artificer", "attacks_per_action_by_level": {"5": 2}}}),
        ("Arcane Jolt", "Battle Smith", 9, "ARCANE JOLT", 62, None, {}),
        ("Improved Defender", "Battle Smith", 15, "IMPROVED DEFENDER", 62, None, {}),
    ]
    for name, subclass, level, heading, page, fixed_tool, grants in feature_specs:
        selectors = [_selector(heading, page)]
        if name.endswith("Spells") and page in {59, 60}:
            selectors[0]["match_all"] = True
        if name == "Battle Smith Spells":
            selectors.append(_selector("BATTLE SM ITH SPELLS", 62))
        if name == "Steel Defender":
            selectors[0]["content_contains"] = "By 3rd level"
        mechanical_grants = copy.deepcopy(grants)
        if fixed_tool:
            subclass_heading = {
                "Alchemist": "Alchemist",
                "Artillerist": "ARTILLERIST",
                "Battle Smith": "BATTLE SM ITH",
            }[subclass]
            selectors.insert(0, _selector(subclass_heading, page))
            mechanical_grants.update(
                {
                    "tool_proficiencies": [fixed_tool],
                    "tool_proficiency_replacement_options": {
                        fixed_tool: [tool for tool in ARTISAN_TOOLS if tool != fixed_tool]
                    },
                }
            )
        card = {
            "name": name,
            "class_name": "Artificer",
            "subclass_name": subclass,
            "minimum_level": level,
            "repeatable_selection_levels": [],
            "selection_requirements": {},
            "selection_requirements_by_level": {},
            "mechanical_grants": mechanical_grants,
        }
        if "attack_scaling" in mechanical_grants:
            card["attack_scaling"] = mechanical_grants.pop("attack_scaling")
        result.append(
            _addition(
                "feature",
                name,
                selectors,
                card,
            )
        )
    return result


def _items_and_feats(wayfinder_additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {(item["kind"], item["name"]): item for item in wayfinder_additions}
    double_scimitar = copy.deepcopy(by_key[("item", "Double-Bladed Scimitar")])
    double_scimitar["source_selectors"] = [_selector("DOUBLE-BLADED SCIMITAR", 22)]
    revenant_blade = copy.deepcopy(by_key[("feat", "Revenant Blade")])
    revenant_blade["source_selectors"] = [_selector("FEAT: REVENANT BLADE", 23)]
    aberrant = copy.deepcopy(by_key[("feat", "Aberrant Dragonmark")])
    aberrant["source_selectors"] = [_selector("FEAT: ABERRANT D RAGONMARK", 53)]
    aberrant["card"]["selection_requirements"]["groups"][1]["recovers_on"] = "short_rest"
    aberrant["card"]["choices"]["source_effect"] = (
        "When a mark spell is cast, the character may spend and roll one Hit Die. An even "
        "result grants that many temporary hit points; an odd result damages one random "
        "creature within 30 feet, or the caster when no other creature is in range. Greater "
        "Aberrant Powers remain an explicit DM option."
    )
    result = [double_scimitar, revenant_blade, aberrant]
    for name, heading, page, kind in [
        ("Armblade", "ARM BLADE", 277, "weapon"),
        ("Dyrrn's Tentacle Whip", "DYRRN'S TENTACLE WHI P", 277, "weapon"),
        ("Imbued Wood Focus", "GLAMERWEAVE", 278, "equipment"),
        ("Living Armor", "LIVING ARMOR", 279, "magic_item"),
    ]:
        extra: dict[str, Any] = {}
        if name == "Imbued Wood Focus":
            extra["content_contains"] = "IMBUED Woon Focus"
        result.append(
            _addition(
                "item",
                name,
                [_selector(heading, page, **extra)],
                {
                    "inventory_template": {
                        "name": name,
                        "kind": kind,
                        "quantity": 1,
                        "description": (
                            "A source-reviewed Eberron item. Its exact attunement and activated "
                            "effects remain available in the bound source context for Agent-as-DM "
                            "adjudication."
                        ),
                        "mechanics": {},
                    }
                },
            )
        )
    return result


def _species_additions() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    result.append(
        _addition(
            "species",
            "Changeling",
            [_selector("C HANGELING TRAITS", 19)],
            _species_card(
                base_species="Changeling",
                abilities={"charisma": 2},
                ability_choice={"count": 1, "amount": 1, "exclude": [], "options": []},
                size="medium",
                speed=30,
                languages=["Common"],
                language_choice_count=2,
                language_options=[],
                allow_any_language=True,
                skill_choice_count=2,
                skill_options=["Deception", "Insight", "Intimidation", "Persuasion"],
                features=[
                    _feature("Shapechanger", "Source-bound action changes appearance and voice."),
                    _feature("Changeling Instincts", "Choose two reviewed social skills."),
                ],
            ),
        )
    )
    result.extend(
        [
            _addition(
                "species",
                "Bugbear",
                [
                    _selector("BUGBEAR TRAITS", 26),
                    _selector("C HAPTER 1 J C HARACTER C REATION", 26),
                ],
                _species_card(
                    base_species="Bugbear",
                    abilities={"strength": 2, "dexterity": 1},
                    size="medium",
                    speed=30,
                    darkvision_ft=60,
                    languages=["Common", "Goblin"],
                    skill_proficiencies=["Stealth"],
                    features=[
                        _feature("Long-Limbed", "Source-bound melee reach increase on the creature's turn."),
                        _feature("Powerful Build", "Counts as one size larger for carrying capacity."),
                        _feature("Surprise Attack", "Source-bound once-per-combat surprise damage."),
                    ],
                ),
            ),
            _addition(
                "species",
                "Goblin",
                [_selector("GOBLIN TRAITS", 27)],
                _species_card(
                    base_species="Goblin",
                    abilities={"dexterity": 2, "constitution": 1},
                    size="small",
                    speed=30,
                    darkvision_ft=60,
                    languages=["Common", "Goblin"],
                    features=[
                        _feature("Fury of the Small", "Source-bound once-per-rest damage bonus."),
                        _feature("Nimble Escape", "Disengage or Hide as a bonus action."),
                    ],
                ),
            ),
            _addition(
                "species",
                "Hobgoblin",
                [
                    _selector("HOBGOBLIN TRAITS", 27),
                    _selector("C HAPTER 1 I C HARACTER CREATION", 27),
                ],
                _species_card(
                    base_species="Hobgoblin",
                    abilities={"constitution": 2, "intelligence": 1},
                    size="medium",
                    speed=30,
                    darkvision_ft=60,
                    languages=["Common", "Goblin"],
                    armor_proficiencies=["light armor"],
                    proficiency_choice_groups=[
                        {
                            "id": "martial_weapons",
                            "count": 2,
                            "options": [
                                {"kind": "weapon", "name": name}
                                for name in [
                                    "Battleaxe", "Flail", "Glaive", "Greataxe", "Greatsword",
                                    "Halberd", "Lance", "Longsword", "Maul", "Morningstar",
                                    "Pike", "Rapier", "Scimitar", "Shortsword", "Trident",
                                    "War Pick", "Warhammer", "Whip", "Blowgun",
                                    "Hand Crossbow", "Heavy Crossbow", "Longbow", "Net",
                                ]
                            ],
                        }
                    ],
                    features=[_feature("Saving Face", "Source-bound failed-roll bonus from nearby allies.")],
                ),
            ),
        ]
    )
    result.append(
        _addition(
            "species",
            "Kalashtar",
            [_selector("KALASHTAR TRAITS", 31), _selector("KALASHTAR TRAITS", 32)],
            _species_card(
                base_species="Kalashtar",
                abilities={"wisdom": 2, "charisma": 1},
                size="medium",
                speed=30,
                languages=["Common", "Quori"],
                language_choice_count=1,
                language_options=[],
                allow_any_language=True,
                resistances=["psychic"],
                features=[
                    _feature("Dual Mind", "Advantage on all Wisdom saving throws."),
                    _feature("Mental Discipline", "Psychic damage resistance."),
                    _feature("Mind Link", "Source-bound telepathy and reciprocal link."),
                    _feature("Severed from Dreams", "Source-bound immunity to effects requiring dreams."),
                ],
            ),
        )
    )
    result.append(
        _addition(
            "species",
            "Orc",
            [_selector("0 RC TRAITS", 33)],
            _species_card(
                base_species="Orc",
                abilities={"strength": 2, "constitution": 1},
                size="medium",
                speed=30,
                darkvision_ft=60,
                languages=["Common", "Orc"],
                skill_choice_count=2,
                skill_options=[
                    "Animal Handling", "Insight", "Intimidation", "Medicine", "Nature",
                    "Perception", "Survival",
                ],
                features=[
                    _feature("Aggressive", "Move toward a visible hostile creature as a bonus action."),
                    _feature("Powerful Build", "Counts as one size larger for carrying capacity."),
                ],
            ),
        )
    )
    for variant, abilities, skill, heading in [
        ("Beasthide", {"constitution": 2, "strength": 1}, "Athletics", "BEASTHIDE"),
        ("Longtooth", {"strength": 2, "dexterity": 1}, "Intimidation", "LONGTOOTH"),
        ("Swiftstride", {"dexterity": 2, "charisma": 1}, "Acrobatics", "SWIFTSTRIDE"),
        ("Wildhunt", {"wisdom": 2, "dexterity": 1}, "Survival", "WILDHUNT"),
    ]:
        result.append(
            _addition(
                "species",
                f"Shifter ({variant})",
                [_selector("SHIFTER TRAITS", 34), _selector(heading, 35)],
                _species_card(
                    base_species="Shifter",
                    abilities=abilities,
                    size="medium",
                    speed=30,
                    darkvision_ft=60,
                    languages=["Common"],
                    skill_proficiencies=[skill],
                    features=[
                        _feature("Shifting", "Bonus-action transformation with temporary hit points."),
                        _feature(f"{variant} Shifting", "Source-bound shifter subrace benefit."),
                    ],
                ),
            )
        )
    result.append(
        _addition(
            "species",
            "Warforged",
            [_selector("WARFORGED TRAITS", 37, match_all=True)],
            _species_card(
                base_species="Warforged",
                abilities={"constitution": 2},
                ability_choice={
                    "count": 1,
                    "amount": 1,
                    "exclude": ["constitution"],
                    "options": [],
                },
                size="medium",
                speed=30,
                languages=["Common"],
                language_choice_count=1,
                language_options=[],
                allow_any_language=True,
                skill_choice_count=1,
                skill_options=[],
                allow_any_skill=True,
                tool_choice_count=1,
                tool_options=ALL_TOOLS,
                resistances=["poison"],
                features=[
                    _feature("Constructed Resilience", "Source-bound warforged physiological defenses."),
                    _feature("Sentry's Rest", "Source-bound inactive but conscious rest state."),
                    _feature("Integrated Protection", "Armor integrates into the warforged body."),
                    _feature("Specialized Design", "Choose one skill and one tool proficiency."),
                ],
            ),
        )
    )
    result.extend(_dragonmark_additions())
    return result


def _mark_features(name: str, *extras: str) -> list[dict[str, str]]:
    return [
        _feature(name, "Dragonmark identity and source context."),
        *[_feature(extra, "Source-bound dragonmark benefit.") for extra in extras],
    ]


def _mark_card(
    *,
    base_species: str,
    abilities: dict[str, int],
    expansion: list[str],
    size: str = "medium",
    speed: int = 30,
    languages: list[str] | None = None,
    spell_grants: list[dict[str, Any]] | None = None,
    features: list[dict[str, str]],
    **extra: Any,
) -> dict[str, Any]:
    return _species_card(
        base_species=base_species,
        abilities=abilities,
        size=size,
        speed=speed,
        languages=languages or ["Common"],
        spell_grants=spell_grants or [],
        spell_list_expansion=expansion,
        features=features,
        **extra,
    )


def _dragonmark_additions() -> list[dict[str, Any]]:
    no_material = {"ignore_material_components": True}
    additions: list[dict[str, Any]] = []
    additions.append(
        _addition(
            "species", "Mark of Detection", [_selector("MARK OF DETECTION", 41)],
            _mark_card(
                base_species="Half-Elf",
                abilities={"wisdom": 2},
                ability_choice={"count": 1, "amount": 1, "exclude": ["wisdom"], "options": []},
                darkvision_ft=60,
                languages=["Common", "Elvish"],
                language_choice_count=1,
                language_options=[],
                allow_any_language=True,
                spell_grants=[
                    _spell_grant("Detect Magic", 1, "intelligence", casting_overrides=no_material),
                    _spell_grant(
                        "Detect Poison and Disease",
                        1,
                        "intelligence",
                        casting_overrides=no_material,
                        eligible_class="Cleric",
                    ),
                    _spell_grant("See Invisibility", 2, "intelligence", minimum_level=3),
                ],
                expansion=["Detect Evil and Good", "Detect Poison and Disease", "Detect Thoughts", "Find Traps", "Clairvoyance", "Nondetection", "Arcane Eye", "Divination", "Legend Lore"],
                features=_mark_features("Mark of Detection", "Deductive Intuition", "Magical Detection", "Spells of the Mark", "Fey Ancestry"),
            ),
        )
    )
    finding_expansion = ["Faerie Fire", "Longstrider", "Locate Animals or Plants", "Locate Object", "Clairvoyance", "Speak with Plants", "Divination", "Locate Creature", "Commune with Nature"]
    finding_grants = [
        _spell_grant("Hunter's Mark", 1, "wisdom", eligible_class="Ranger"),
        _spell_grant("Locate Object", 2, "wisdom", minimum_level=3),
    ]
    for name, base in [("Mark of Finding (Human)", "Human"), ("Mark of Finding (Half-Orc)", "Half-Orc")]:
        additions.append(
            _addition(
                "species", name, [_selector("MARK OF FINDING", 42)],
                _mark_card(
                    base_species=base,
                    abilities={"wisdom": 2, "constitution": 1},
                    spell_grants=copy.deepcopy(finding_grants),
                    expansion=finding_expansion,
                    features=_mark_features("Mark of Finding", "Hunter's Intuition", "Finder's Magic", "Spells of the Mark"),
                ),
            )
        )
    additions.append(
        _addition(
            "species", "Mark of Handling", [_selector("MARK OF HANDLING", 43), _selector("C HAPTER I I C HARACTER CREATION", 43)],
            _mark_card(
                base_species="Human",
                abilities={"wisdom": 2},
                ability_choice={"count": 1, "amount": 1, "exclude": ["wisdom"], "options": []},
                spell_grants=[
                    _spell_grant("Animal Friendship", 1, "wisdom", recovers_on="short_rest", casting_overrides=no_material, eligible_class="Druid"),
                    _spell_grant("Speak with Animals", 1, "wisdom", recovers_on="short_rest", casting_overrides=no_material, eligible_class="Druid"),
                ],
                expansion=["Animal Friendship", "Speak with Animals", "Beast Sense", "Calm Emotions", "Beacon of Hope", "Conjure Animals", "Aura of Life", "Dominate Beast", "Awaken"],
                features=_mark_features("Mark of Handling", "Wild Intuition", "Primal Connection", "The Bigger They Are", "Spells of the Mark"),
            ),
        )
    )
    additions.extend(_halfling_marks(no_material))
    additions.extend(_human_marks(no_material))
    additions.extend(_ancestry_marks(no_material))
    return additions


def _halfling_marks(no_material: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _addition(
            "species", "Mark of Healing", [_selector("MARK OF H EALING", 44)],
            _mark_card(
                base_species="Halfling", abilities={"dexterity": 2, "wisdom": 1}, size="small", speed=25,
                languages=["Common", "Halfling"],
                spell_grants=[
                    _spell_grant("Cure Wounds", 1, "wisdom", eligible_class="Cleric"),
                    _spell_grant("Lesser Restoration", 2, "wisdom", minimum_level=3, eligible_class="Cleric"),
                ],
                expansion=["Cure Wounds", "Healing Word", "Lesser Restoration", "Prayer of Healing", "Aura of Vitality", "Mass Healing Word", "Aura of Purity", "Aura of Life", "Greater Restoration"],
                features=_mark_features("Mark of Healing", "Medical Intuition", "Healing Touch", "Spells of the Mark", "Lucky", "Brave", "Halfling Nimbleness"),
            ),
        ),
        _addition(
            "species", "Mark of Hospitality", [_selector("MARK O F HOSPITALITY", 45)],
            _mark_card(
                base_species="Halfling", abilities={"dexterity": 2, "charisma": 1}, size="small", speed=25,
                languages=["Common", "Halfling"],
                spell_grants=[
                    _known_cantrip("Prestidigitation", "charisma"),
                    _spell_grant("Purify Food and Drink", 1, "charisma", casting_overrides=no_material, eligible_class="Cleric"),
                    _spell_grant("Unseen Servant", 1, "charisma", casting_overrides=no_material),
                ],
                expansion=["Goodberry", "Sleep", "Aid", "Calm Emotions", "Create Food and Water", "Leomund's Tiny Hut", "Aura of Purity", "Mordenkainen's Private Sanctum", "Hallow"],
                features=_mark_features("Mark of Hospitality", "Ever Hospitable", "Innkeeper's Magic", "Spells of the Mark", "Lucky", "Brave", "Halfling Nimbleness"),
            ),
        ),
    ]


def _human_marks(no_material: dict[str, Any]) -> list[dict[str, Any]]:
    hour_override = {
        "ignore_material_components": True,
        "duration": {"kind": "timed", "value": 1, "unit": "hour", "concentration": False},
    }
    return [
        _addition(
            "species", "Mark of Making", [_selector("VARIANT HUMAN: MARK OF MAKING", 46)],
            _mark_card(
                base_species="Human", abilities={"intelligence": 2},
                ability_choice={"count": 1, "amount": 1, "exclude": ["intelligence"], "options": []},
                tool_choice_count=1, tool_options=ARTISAN_TOOLS,
                spell_grants=[_known_cantrip("Mending", "intelligence"), _spell_grant("Magic Weapon", 2, "intelligence", casting_overrides=hour_override)],
                expansion=["Identify", "Tenser's Floating Disk", "Continual Flame", "Magic Weapon", "Conjure Barrage", "Elemental Weapon", "Fabricate", "Stone Shape", "Creation"],
                features=_mark_features("Mark of Making", "Artisan's Intuition", "Maker's Gift", "Spellsmith", "Spells of the Mark"),
            ),
        ),
        _addition(
            "species", "Mark of Passage", [_selector("VARIANT HUMAN: MARK OF PAS SAGE", 47), _selector("C HAPTER 1 I C HARACTER CREATION", 47)],
            _mark_card(
                base_species="Human", abilities={"dexterity": 2}, speed=35,
                ability_choice={"count": 1, "amount": 1, "exclude": ["dexterity"], "options": []},
                spell_grants=[_spell_grant("Misty Step", 2, "dexterity", eligible_class="Wizard")],
                expansion=["Expeditious Retreat", "Jump", "Misty Step", "Pass Without Trace", "Blink", "Phantom Steed", "Dimension Door", "Freedom of Movement", "Teleportation Circle"],
                features=_mark_features("Mark of Passage", "Intuitive Motion", "Magical Passage", "Spells of the Mark"),
            ),
        ),
        _addition(
            "species", "Mark of Sentinel", [_selector("VARIANT HUMAN: MARK OF SENTINEL", 49)],
            _mark_card(
                base_species="Human", abilities={"constitution": 2, "wisdom": 1},
                spell_grants=[_spell_grant("Shield", 1, "wisdom")],
                expansion=["Compelled Duel", "Shield of Faith", "Warding Bond", "Zone of Truth", "Counterspell", "Protection from Energy", "Death Ward", "Guardian of Faith", "Bigby's Hand"],
                features=_mark_features("Mark of Sentinel", "Sentinel's Intuition", "Guardian's Shield", "Vigilant Guardian", "Spells of the Mark"),
            ),
        ),
    ]


def _ancestry_marks(no_material: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _addition(
            "species", "Mark of Scribing", [_selector("GNOME SUBRACE: MARK OF SCRIBING", 48)],
            _mark_card(
                base_species="Gnome", abilities={"intelligence": 2, "charisma": 1}, size="small", speed=25,
                darkvision_ft=60, languages=["Common", "Gnomish"],
                spell_grants=[
                    _known_cantrip("Message", "intelligence"),
                    _spell_grant("Comprehend Languages", 1, "intelligence", recovers_on="short_rest", eligible_class="Wizard"),
                    _spell_grant("Magic Mouth", 2, "intelligence", minimum_level=3),
                ],
                expansion=["Comprehend Languages", "Illusory Script", "Animal Messenger", "Silence", "Sending", "Tongues", "Arcane Eye", "Confusion", "Dream"],
                features=_mark_features("Mark of Scribing", "Gifted Scribe", "Scribe's Insight", "Spells of the Mark", "Gnome Cunning"),
            ),
        ),
        _addition(
            "species", "Mark of Shadow", [_selector("ELF SUBRAC E : MARK OF SHADOW", 50)],
            _mark_card(
                base_species="Elf", abilities={"dexterity": 2, "charisma": 1}, darkvision_ft=60,
                languages=["Common", "Elvish"], skill_proficiencies=["Perception"],
                spell_grants=[_known_cantrip("Minor Illusion", "charisma"), _spell_grant("Invisibility", 2, "charisma", minimum_level=3)],
                expansion=["Disguise Self", "Silent Image", "Darkness", "Pass Without Trace", "Clairvoyance", "Major Image", "Greater Invisibility", "Hallucinatory Terrain", "Mislead"],
                features=_mark_features("Mark of Shadow", "Cunning Intuition", "Shape Shadows", "Spells of the Mark", "Fey Ancestry", "Trance"),
            ),
        ),
        _addition(
            "species", "Mark of Storm", [
                _selector("VARIANT HALF-ELF: MARK OF STORM", 51),
                _selector(
                    "C HAPTER 1 I C HARACTER C REATION",
                    51,
                    content_contains="Spells oft he Mark",
                ),
            ],
            _mark_card(
                base_species="Half-Elf", abilities={"charisma": 2, "dexterity": 1}, darkvision_ft=60,
                languages=["Common", "Elvish"], language_choice_count=1, language_options=[], allow_any_language=True,
                resistances=["lightning"],
                spell_grants=[_known_cantrip("Gust", "charisma", "Druid"), _spell_grant("Gust of Wind", 2, "charisma", minimum_level=3, eligible_class="Druid")],
                expansion=["Feather Fall", "Fog Cloud", "Gust of Wind", "Levitate", "Sleet Storm", "Wind Wall", "Conjure Minor Elementals", "Control Water", "Conjure Elemental"],
                features=_mark_features("Mark of Storm", "Windwright's Intuition", "Storm's Boon", "Headwinds", "Spells of the Mark", "Fey Ancestry"),
            ),
        ),
        _addition(
            "species", "Mark of Warding", [_selector("DWARF SUBRAC E : MARK OF WARDING", 52)],
            _mark_card(
                base_species="Dwarf", abilities={"constitution": 2, "intelligence": 1}, speed=25,
                darkvision_ft=60, languages=["Common", "Dwarvish"], resistances=["poison"],
                weapon_proficiencies=["Battleaxe", "Handaxe", "Light Hammer", "Warhammer"],
                tool_choice_count=1, tool_options=["Smith's Tools", "Brewer's Supplies", "Mason's Tools"],
                spell_grants=[
                    _spell_grant("Alarm", 1, "intelligence", casting_overrides=no_material),
                    _spell_grant("Mage Armor", 1, "intelligence", casting_overrides=no_material),
                    _spell_grant("Arcane Lock", 2, "intelligence", minimum_level=3, casting_overrides=no_material),
                ],
                expansion=["Alarm", "Armor of Agathys", "Arcane Lock", "Knock", "Glyph of Warding", "Magic Circle", "Leomund's Secret Chest", "Mordenkainen's Faithful Hound", "Antilife Shell"],
                features=_mark_features("Mark of Warding", "Warder’s Intuition", "Wards and Seals", "Spells of the Mark", "Dwarven Resilience", "Stonecunning"),
            ),
        ),
    ]


def _gust_decision(wayfinder_document: dict[str, Any]) -> dict[str, Any]:
    for decision in wayfinder_document["decisions"]:
        if decision.get("kind") == "spell" and decision.get("artifact_patch", {}).get("card", {}).get("name") == "Gust":
            cloned = copy.deepcopy(decision)
            cloned["name"] = "GUST"
            return cloned
    raise RuntimeError("Wayfinder Gust decision is missing")


def _runtime_probes() -> list[dict[str, Any]]:
    return [
        {
            "name": "rising-house-agent-fixed-equipment",
            "steps": [
                {
                    "kind": "background", "name": "House Agent (Cannith)", "selection": {},
                    "expect": [
                        {"path": "sheet.progression.background", "equals": "House Agent (Cannith)"},
                        {"path": "sheet.inventory.wallet.gp", "equals": 20},
                        {"path": "sheet.traits.proficiencies.tools", "contains": "Alchemist's Supplies"},
                        {"path": "sheet.traits.proficiencies.tools", "contains": "Tinker's Tools"},
                    ],
                }
            ],
        },
        {
            "name": "rising-changeling-allows-charisma-choice",
            "steps": [
                {
                    "kind": "species", "name": "Changeling",
                    "selection": {"abilities": ["charisma"], "languages": ["Draconic", "Goblin"], "skills": ["Deception", "Insight"]},
                    "expect": [
                        {"path": "sheet.abilities.charisma.score", "equals": 13},
                        {"path": "sheet.skills.deception.proficiency", "equals": "proficient"},
                        {"path": "sheet.skills.insight.proficiency", "equals": "proficient"},
                    ],
                }
            ],
        },
        {
            "name": "rising-hobgoblin-bounded-weapons",
            "steps": [
                {
                    "kind": "species", "name": "Hobgoblin",
                    "selection": {
                        "proficiency_choices": {
                            "martial_weapons": [
                                {"kind": "weapon", "name": "Longsword"},
                                {"kind": "weapon", "name": "Longbow"},
                            ]
                        }
                    },
                    "expect": [
                        {"path": "sheet.traits.proficiencies.armor", "contains": "light armor"},
                        {"path": "sheet.traits.proficiencies.weapons", "contains": "Longsword"},
                        {"path": "sheet.traits.proficiencies.weapons", "contains": "Longbow"},
                    ],
                }
            ],
        },
        {
            "name": "rising-warforged-specialized-design",
            "steps": [
                {
                    "kind": "species", "name": "Warforged",
                    "selection": {"abilities": ["intelligence"], "languages": ["Goblin"], "skills": ["Arcana"], "tools": ["Smith's Tools"]},
                    "expect": [
                        {"path": "sheet.abilities.constitution.score", "equals": 12},
                        {"path": "sheet.abilities.intelligence.score", "equals": 11},
                        {"path": "sheet.skills.arcana.proficiency", "equals": "proficient"},
                        {"path": "sheet.traits.proficiencies.tools", "contains": "Smith's Tools"},
                    ],
                }
            ],
        },
        {
            "name": "rising-mark-of-detection-material-overrides",
            "level": 3,
            "steps": [
                {
                    "kind": "species", "name": "Mark of Detection",
                    "selection": {"abilities": ["dexterity"], "languages": ["Goblin"]},
                    "expect": [
                        {"path": "sheet.content.spells", "contains_names": ["Detect Magic", "Detect Poison and Disease", "See Invisibility"]},
                        {"path": "sheet.resources", "length": 3},
                    ],
                }
            ],
        },
        {
            "name": "rising-aberrant-short-rest-spell",
            "steps": [
                {
                    "kind": "feat", "name": "Aberrant Dragonmark",
                    "selection": {"spell_choices": {"cantrip": ["dnd5e.content.srd2014.spell.light"], "level_1_spell": ["dnd5e.content.srd2014.spell.burning-hands"]}},
                    "expect": [
                        {"path": "sheet.abilities.constitution.score", "equals": 11},
                        {"path": "sheet.content.spells", "contains_names": ["Light", "Burning Hands"]},
                        {"path": "sheet.resources", "length": 1},
                    ],
                }
            ],
        },
    ]


def _mark_automatic_replacements(additions: list[dict[str, Any]]) -> None:
    identities = {
        ("feature", "Infuse Item"),
        ("feature", "Magical Tinkering"),
        ("feature", "Artificer Specialist"),
        ("feature", "Tool Expertise"),
        ("feature", "Flash of Genius"),
        ("feature", "Magic Item Savant"),
        ("feature", "Soul of Artifice"),
        ("feature", "Alchemist Spells"),
        ("feature", "Experimental Elixir"),
        ("feature", "Alchemical Savant"),
        ("feature", "Restorative Reagents"),
        ("feature", "Artillerist Spells"),
        ("feature", "Eldritch Cannon"),
        ("feature", "Arcane Firearm"),
        ("feature", "Explosive Cannon"),
        ("feature", "Battle Smith Spells"),
        ("feature", "Extra Attack"),
        ("feature", "Arcane Jolt"),
        ("species", "Changeling"),
        ("species", "Bugbear"),
        ("species", "Goblin"),
        ("species", "Hobgoblin"),
        ("species", "Kalashtar"),
        ("species", "Warforged"),
    }
    for addition in additions:
        if (addition["kind"], addition["name"]) in identities:
            addition["replace_existing"] = True


def main() -> None:
    manifest = json.loads(MAIN_FIXTURE.read_text(encoding="utf-8"))
    wayfinder_document = manifest["documents"][WAYFINDER]
    wayfinder_additions = wayfinder_document["additions"]
    additions = [
        *_house_backgrounds(wayfinder_additions),
        _class_addition(),
        *_class_features(),
        *_subclasses_and_features(),
        *_items_and_feats(wayfinder_additions),
        *_species_additions(),
    ]
    _mark_automatic_replacements(additions)
    document = {
        "complete_review": True,
        "default_status": "rejected",
        "addition_default_status": "accepted",
        "default_status_by_kind": {"item": "accepted", "spell": "accepted", "statblock": "accepted"},
        "rationale": (
            "Agent reviewed every player option, item, and creature against the indexed Rising "
            "from the Last War pages. Automatic setting prose is rejected; every recovered card "
            "is bound to exact source chunks, has bounded choices, and preserves descriptive "
            "semantics as Agent-as-DM context instead of inventing one-off engine rules."
        ),
        "expected_counts": {
            "background": 13, "class": 1, "feat": 2, "feature": 42, "item": 26,
            "species": 24, "spell": 1, "statblock": 40, "subclass": 3,
        },
        "expected_actor_names": [
            "Belashyrra", "Dyrrn", "Clawfoot", "Fastieth", "Dolgaunt", "Dol Grim",
            "Dusk Hag", "Expeditious Messenger", "Iron Defender", "Inspired",
            "Karrnathi Undead Soldier", "Lady Illmarrow", "Living Burning Hands",
            "Living Lightning Bolt", "Living Cloudkill", "The Lord of Blades", "Mordakhesh",
            "Rak Tulkhesh", "Sul Khatesh", "Hashalaq Quori", "Kalaraq Quori",
            "Tsucora Quori", "Radiant Idol", "Zakya Rakshasa", "Undying Councilor",
            "Undying Soldier", "Valenar Hawk", "Valenar Hound", "Valenar Steed",
            "Warforged Colossus", "Warforged Titan", "Bone Knight", "Changeling",
            "Kalashtar", "Magewright", "Shifter", "Tarkanan Assassin",
            "Warforged Soldier",
        ],
        "runtime_probes": _runtime_probes(),
        "decisions": [
            _gust_decision(wayfinder_document),
            {
                "kind": "statblock",
                "name": "STEEL DEFENDER",
                "artifact_patch": {"card": {"owner_class_name": "Artificer"}},
            },
            {
                "kind": "statblock",
                "name": "HOMUNCULUS SERVANT",
                "artifact_patch": {"card": {"owner_class_name": "Artificer"}},
            },
            {
                "kind": "statblock",
                "name": "DOL GR IM",
                "artifact_patch": {"card": {"name": "Dol Grim"}},
            },
            {
                "kind": "statblock",
                "name": "LADY lLLMARROW",
                "artifact_patch": {"card": {"name": "Lady Illmarrow"}},
            },
        ],
        "additions": additions,
    }
    payload = {"version": 1, "documents": {BOOK: document}}
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT} with {len(additions)} source-bound additions")


if __name__ == "__main__":
    main()
