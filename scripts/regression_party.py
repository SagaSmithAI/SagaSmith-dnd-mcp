"""Build a source-audited campaign party only through public stdio MCP tools.

The driver intentionally starts from the campaign's active content catalog. Base
class mechanics and starting equipment are submitted through the validated public
character sheet API because the SRD class and item catalog cards are source-linked
but deliberately catalog-only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from sagasmith_core.text import ascii_slug
from sagasmith_dnd.abilities import ABILITY_NAMES
from sagasmith_dnd.engine import ability_modifier
from sagasmith_dnd.progression import CANTRIPS_KNOWN, KNOWN_SPELLS
from sagasmith_dnd.spell_resolution import audit_spell_resolution_paths
from sagasmith_dnd.vocabulary import (
    CAMPAIGN_GAME_PHASES,
    EFFECTIVE_GAME_PHASES,
    PREPARED_SELECTION_MODES,
    SPELLCASTING_RESOURCE_MODELS,
)

from scripts.regression_modules import ExposureClient, _facade_value, _token, campaign_view
from scripts.regression_rulings import raise_for_pending_ruling, ruling_failure_fields
from scripts.regression_runtime import (
    exception_leaf_messages,
    regression_server_parameters,
)

_ITEM_WEIGHT_OZ: dict[str, int | float] = {
    "arrows": 0.8,
    "backpack": 80,
    "ball bearings (bag of 1,000)": 32,
    "bedroll": 112,
    "blanket": 48,
    "book of lore": 80,
    "case, map or scroll": 16,
    "chain mail": 880,
    "chest": 400,
    "common clothes": 48,
    "component pouch": 32,
    "crossbow bolts": 1.2,
    "crossbow, light": 80,
    "crowbar": 80,
    "dagger": 16,
    "fine clothes": 96,
    "hammer": 48,
    "hempen rope (50 feet)": 160,
    "holy symbol (amulet)": 16,
    "lamp": 16,
    "lantern, hooded": 32,
    "leather": 160,
    "longbow": 32,
    "lute": 32,
    "mace": 64,
    "mess kit": 16,
    "oil (flask)": 16,
    "piton": 4,
    "pouch": 16,
    "prayer book": 80,
    "quarterstaff": 64,
    "rapier": 32,
    "rations (1 day)": 32,
    "shield": 96,
    "scale mail": 720,
    "shortbow": 32,
    "shortsword": 32,
    "spellbook": 48,
    "thieves' tools": 16,
    "tinderbox": 16,
    "torch": 16,
    "quiver": 16,
    "waterskin": 80,
}


def _item_weight_oz(name: str) -> int | float:
    return _ITEM_WEIGHT_OZ.get(name.casefold(), 0)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", default="full-playthrough-v1")
    parser.add_argument(
        "--party",
        choices=(
            "lost-mine-of-phandelver",
            "waterdeep-dragon-heist",
            "tyranny-of-dragons",
            "storm-kings-thunder",
            "tomb-of-annihilation",
        ),
        default="lost-mine-of-phandelver",
    )
    parser.add_argument(
        "--profile-name",
        default="",
        help="Build only one named source-audited profile, for example a replacement PC",
    )
    parser.add_argument(
        "--actor-name",
        default="",
        help="Override the new actor's name when --profile-name selects one profile",
    )
    parser.add_argument(
        "--return-phase",
        choices=tuple(sorted(CAMPAIGN_GAME_PHASES)),
        default="",
        help="Phase to expose after construction; defaults to the entry phase",
    )
    return parser.parse_args()


def _server_parameters(args: argparse.Namespace) -> StdioServerParameters:
    return regression_server_parameters(
        home=args.home,
        auto_seed=True,
    )


def _weapon(
    identifier: str,
    name: str,
    source_key: str,
    *,
    category: str,
    damage: str,
    damage_type: str,
    attack_type: str = "melee",
    attack_ability: str = "strength",
    properties: list[str] | None = None,
    versatile: str = "",
    normal_range: int = 0,
    long_range: int = 0,
    ammunition_item_id: str | None = None,
    quantity: int = 1,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "name": name,
        "kind": "weapon",
        "quantity": quantity,
        "weight_oz": _item_weight_oz(name),
        "source_key": source_key,
        "mechanics": {
            "category": category,
            "attack_type": attack_type,
            "attack_ability": attack_ability,
            "damage_formula": damage,
            "damage_type": damage_type,
            "versatile_damage_formula": versatile,
            "properties": list(properties or []),
            "normal_range_ft": normal_range,
            "long_range_ft": long_range,
            "ammunition_item_id": ammunition_item_id,
            "proficient": True,
        },
    }


def _armor(
    identifier: str,
    name: str,
    source_key: str,
    *,
    base_ac: int,
    dexterity_mode: str,
    dexterity_max: int | None = None,
    stealth_disadvantage: bool = False,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "name": name,
        "kind": "armor",
        "quantity": 1,
        "weight_oz": _item_weight_oz(name),
        "source_key": source_key,
        "equipped": True,
        "equipped_slot": "armor",
        "mechanics": {
            "base_ac": base_ac,
            "dexterity_mode": dexterity_mode,
            "dexterity_max": dexterity_max,
            "stealth_disadvantage": stealth_disadvantage,
        },
    }


def _shield(identifier: str, source_key: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "name": "Shield",
        "kind": "shield",
        "quantity": 1,
        "weight_oz": _item_weight_oz("Shield"),
        "source_key": source_key,
        "equipped": True,
        "equipped_slot": "shield",
        "mechanics": {"ac_bonus": 2},
    }


def _equipment(
    identifier: str,
    name: str,
    source_key: str,
    *,
    kind: str = "equipment",
    quantity: int = 1,
    mechanics: dict[str, Any] | None = None,
    description: str = "",
    source_kind: str = "item",
) -> dict[str, Any]:
    item = {
        "id": identifier,
        "name": name,
        "kind": kind,
        "quantity": quantity,
        "weight_oz": _item_weight_oz(name),
        "source_key": source_key,
        "mechanics": deepcopy(mechanics or {}),
        "description": description,
    }
    if source_kind != "item":
        item["_source_kind"] = source_kind
    return item


def _profile_item_prefix(profile: dict[str, Any]) -> str:
    return ascii_slug(profile["name"])


OIL_RULE = (
    "As an action, you can splash the oil in this flask onto a creature within "
    "5 feet of you or throw it up to 20 feet, shattering it on impact. Make a "
    "ranged attack against a target creature or object, treating the oil as an "
    "improvised weapon. On a hit, the target is covered in oil. If the target "
    "takes any fire damage before the oil dries (after 1 minute), the target "
    "takes an additional 5 fire damage from the burning oil."
)
TORCH_RULE = (
    "A torch burns for 1 hour, providing bright light in a 20-foot radius and "
    "dim light for an additional 20 feet. If you make a melee attack with a "
    "burning torch and hit, it deals 1 fire damage."
)
_PACK_CONTENTS: dict[str, tuple[tuple[str, int], ...]] = {
    "explorer's pack": (
        ("Backpack", 1),
        ("Bedroll", 1),
        ("Mess kit", 1),
        ("Tinderbox", 1),
        ("Torch", 10),
        ("Rations (1 day)", 10),
        ("Waterskin", 1),
        ("Hempen rope (50 feet)", 1),
    ),
    "burglar's pack": (
        ("Backpack", 1),
        ("Ball bearings (bag of 1,000)", 1),
        ("String (10 feet)", 1),
        ("Bell", 1),
        ("Candle", 5),
        ("Crowbar", 1),
        ("Hammer", 1),
        ("Piton", 10),
        ("Lantern, hooded", 1),
        ("Oil (flask)", 2),
        ("Rations (1 day)", 5),
        ("Tinderbox", 1),
        ("Waterskin", 1),
        ("Hempen rope (50 feet)", 1),
    ),
    "scholar's pack": (
        ("Backpack", 1),
        ("Book of lore", 1),
        ("Ink (1 ounce bottle)", 1),
        ("Ink pen", 1),
        ("Parchment (one sheet)", 10),
        ("Sand (small bag)", 1),
        ("Knife, small", 1),
    ),
    "priest's pack": (
        ("Backpack", 1),
        ("Blanket", 1),
        ("Candle", 10),
        ("Tinderbox", 1),
        ("Alms box", 1),
        ("Incense (block)", 2),
        ("Censer", 1),
        ("Vestments", 1),
        ("Rations (1 day)", 2),
        ("Waterskin", 1),
    ),
    "diplomat's pack": (
        ("Chest", 1),
        ("Case, map or scroll", 2),
        ("Fine clothes", 1),
        ("Ink (1 ounce bottle)", 1),
        ("Ink pen", 1),
        ("Lamp", 1),
        ("Oil (flask)", 2),
        ("Paper (one sheet)", 5),
        ("Perfume (vial)", 1),
        ("Sealing wax", 1),
        ("Soap", 1),
    ),
}
_CLASS_PACK: dict[str, str] = {
    "bard": "Diplomat's Pack",
    "cleric": "Priest's Pack",
    "fighter": "Explorer's Pack",
    "rogue": "Burglar's Pack",
    "ranger": "Explorer's Pack",
    "wizard": "Scholar's Pack",
}


def _pack_contents(
    profile: dict[str, Any],
    pack_name: str,
) -> list[dict[str, Any]]:
    contents = _PACK_CONTENTS.get(pack_name.casefold())
    if contents is None:
        raise ValueError(f"unsupported audited equipment pack: {pack_name}")
    prefix = _profile_item_prefix(profile)
    pack_slug = ascii_slug(pack_name)
    source_key = str(profile["class"])
    items: list[dict[str, Any]] = []
    for name, quantity in contents:
        item_slug = ascii_slug(name)
        description = (
            OIL_RULE
            if name == "Oil (flask)"
            else TORCH_RULE
            if name == "Torch"
            else f"Included in the source-granted {pack_name}."
        )
        mechanics = (
            {
                "consumable": True,
                "use_action": "use_object",
                "covered_duration_rounds": 10,
                "trigger_damage_type": "fire",
                "additional_fire_damage": 5,
            }
            if name == "Oil (flask)"
            else {
                "use_action": "attack",
                "damage_formula": "1",
                "damage_type": "fire",
                "burn_duration_minutes": 60,
            }
            if name == "Torch"
            else {}
        )
        items.append(
            _equipment(
                f"{prefix}-{pack_slug}-{item_slug}",
                name,
                source_key,
                quantity=quantity,
                mechanics=mechanics,
                description=description,
                source_kind="class",
            )
        )
    return items


def _class_pack_name(profile: dict[str, Any]) -> str:
    class_name = str(profile["class"]).casefold()
    try:
        return _CLASS_PACK[class_name]
    except KeyError as error:
        raise ValueError(f"no audited starting-equipment pack for {profile['class']}") from error


def _class_starting_supplements(profile: dict[str, Any]) -> list[dict[str, Any]]:
    prefix = _profile_item_prefix(profile)
    class_name = str(profile["class"]).casefold()
    if class_name == "fighter":
        return _pack_contents(profile, _class_pack_name(profile))
    if class_name == "rogue":
        return [
            _weapon(
                f"{prefix}-daggers",
                "Dagger",
                "Dagger",
                category="simple",
                damage="1d4",
                damage_type="piercing",
                attack_ability="dexterity",
                properties=["finesse", "light", "thrown"],
                normal_range=20,
                long_range=60,
                quantity=2,
            ),
            *_pack_contents(profile, _class_pack_name(profile)),
        ]
    if class_name == "wizard":
        return _pack_contents(profile, _class_pack_name(profile))
    if class_name == "cleric":
        bolts_id = f"{prefix}-class-bolts"
        return [
            _equipment(
                bolts_id,
                "Crossbow bolts",
                "Crossbow bolts (20)",
                kind="ammunition",
                quantity=20,
            ),
            _weapon(
                f"{prefix}-class-crossbow",
                "Crossbow, light",
                "Crossbow, light",
                category="simple",
                damage="1d8",
                damage_type="piercing",
                attack_type="ranged",
                attack_ability="dexterity",
                properties=["ammunition", "loading", "two-handed"],
                normal_range=80,
                long_range=320,
                ammunition_item_id=bolts_id,
            ),
            *_pack_contents(profile, _class_pack_name(profile)),
        ]
    if class_name == "bard":
        return _pack_contents(profile, _class_pack_name(profile))
    if class_name == "ranger":
        return _pack_contents(profile, _class_pack_name(profile))
    raise ValueError(f"no audited starting-equipment supplement for {profile['class']}")


def _background_starting_items(profile: dict[str, Any]) -> list[dict[str, Any]]:
    if str(profile.get("background_base") or profile["background"]) != "Acolyte":
        raise ValueError("campaign regression profiles require an audited background package")
    prefix = _profile_item_prefix(profile)
    source = "Acolyte"
    profiles = [
        _equipment(
            f"{prefix}-background-holy-symbol",
            "Holy symbol",
            source,
            source_kind="background",
        ),
        _equipment(
            f"{prefix}-background-prayer-book",
            "Prayer book",
            source,
            source_kind="background",
        ),
        _equipment(
            f"{prefix}-background-incense",
            "Incense",
            source,
            quantity=5,
            source_kind="background",
        ),
        _equipment(
            f"{prefix}-background-vestments",
            "Vestments",
            source,
            source_kind="background",
        ),
        _equipment(
            f"{prefix}-background-common-clothes",
            "Common clothes",
            source,
            source_kind="background",
        ),
        _equipment(
            f"{prefix}-background-pouch",
            "Pouch",
            source,
            description="Contains the 15 gp granted by the Acolyte equipment package.",
            source_kind="background",
        ),
    ]
    return profiles


CORE_BACKGROUND_CUSTOMIZATIONS = {
    "Dorn Thistle": {
        "name": "Militia Chaplain",
        "skills": ["insight", "religion"],
        "personality_traits": [
            "I measure plans against lessons from old campaign journals.",
            "I make time to tend the frightened before celebrating victory.",
        ],
        "ideal": "Duty. Power is worthwhile only when it protects the vulnerable.",
        "bond": "My old militia chapel sheltered me when I had nowhere else to go.",
        "flaw": "I assume discipline can solve problems that really require trust.",
    },
    "Pip Underbough": {
        "name": "Street Penitent",
        "skills": ["deception", "perception"],
        "personality_traits": [
            "I notice exits and unattended purses without meaning to.",
            "I deflect uncomfortable questions with an easy joke.",
        ],
        "ideal": "Redemption. A past mistake does not have to decide the next choice.",
        "bond": "A small shrine once hid me from people I had wronged.",
        "flaw": "I keep contingency plans secret even from allies.",
    },
    "Aelar Quill": {
        "name": "Cloistered Researcher",
        "skills": ["insight", "investigation"],
        "personality_traits": [
            "I annotate every mystery before I offer a theory.",
            "I become animated when someone challenges my evidence.",
        ],
        "ideal": "Knowledge. Truth should survive the institutions that guard it.",
        "bond": "My mentor entrusted me with an unfinished catalogue of dangerous lore.",
        "flaw": "I delay decisions while looking for one more corroborating source.",
    },
    "Brynja Stonefaith": {
        "name": "Temple Warden",
        "skills": ["insight", "religion"],
        "personality_traits": [
            "I quietly check whether everyone has eaten and rested.",
            "I speak plainly when ritual or rank obscures immediate harm.",
        ],
        "ideal": "Stewardship. Sacred power exists to preserve life.",
        "bond": "I promised to return a stolen relic to the community that forged it.",
        "flaw": "I take responsibility for wounds no one could have prevented.",
    },
    "Seraphine Vale": {
        "name": "Sacred Envoy",
        "skills": ["insight", "religion"],
        "personality_traits": [
            "I remember the names and customs people use to describe themselves.",
            "I look for the sentence both sides can honestly agree to.",
        ],
        "ideal": "Concord. Lasting peace begins with accurate understanding.",
        "bond": "A network of humble shrines carried my messages through dangerous country.",
        "flaw": "I sometimes promise compromise where a clear refusal is needed.",
    },
    "Tala Windmere": {
        "name": "Frontier Giant-Speaker",
        "skills": ["insight", "investigation"],
        "personality_traits": [
            "I compare every trail against the weather and the stories of local guides.",
            "I learn a stranger's language before I ask them to trust my intentions.",
        ],
        "ideal": "Balance. Strength carries an obligation to leave room for smaller lives.",
        "bond": "A wandering giant once spared my village after I answered in its own tongue.",
        "flaw": "I keep pursuing a trail after wiser companions have decided to turn back.",
    },
}


def _customize_core_backgrounds(
    profiles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result = deepcopy(profiles)
    for profile in result:
        customization = CORE_BACKGROUND_CUSTOMIZATIONS.get(str(profile["name"]))
        if customization is None:
            raise ValueError(f"missing audited custom background for {profile['name']}")
        profile["background_base"] = str(profile["background"])
        profile["background"] = str(customization["name"])
        profile["background_skills"] = list(customization["skills"])
        profile["background_characteristics"] = {
            "personality_traits": list(customization["personality_traits"]),
            "ideals": [str(customization["ideal"])],
            "bonds": [str(customization["bond"])],
            "flaws": [str(customization["flaw"])],
        }
    return result


def lost_mine_party_profiles() -> list[dict[str, Any]]:
    """Return five deliberately varied, level-one 2014 Core character plans."""

    profiles = [
        {
            "name": "Dorn Thistle",
            "class": "Fighter",
            "species": "Human",
            "background": "Acolyte",
            "ability_method": "standard_array",
            "abilities": {
                "strength": 15,
                "dexterity": 12,
                "constitution": 14,
                "intelligence": 8,
                "wisdom": 13,
                "charisma": 10,
            },
            "species_selection": {"languages": ["Elvish"]},
            "background_languages": ["Celestial", "Draconic"],
            "hit_die": 10,
            "saving_throws": ["strength", "constitution"],
            "skills": ["athletics", "perception"],
            "armor_proficiencies": ["all armor", "shields"],
            "weapon_proficiencies": ["simple weapons", "martial weapons"],
            "feature_choices": {"Fighting Style": {"option": "Defense"}},
            "items": [
                _armor(
                    "dorn-chain-mail",
                    "Chain mail",
                    "Chain mail",
                    base_ac=16,
                    dexterity_mode="none",
                    stealth_disadvantage=True,
                ),
                _shield("dorn-shield", "Shield"),
                _weapon(
                    "dorn-longsword",
                    "Longsword",
                    "Longsword",
                    category="martial",
                    damage="1d8",
                    damage_type="slashing",
                    versatile="1d10",
                    properties=["versatile"],
                ),
                _equipment(
                    "dorn-bolts",
                    "Crossbow bolts",
                    "Crossbow bolts (20)",
                    kind="ammunition",
                    quantity=20,
                ),
                _weapon(
                    "dorn-crossbow",
                    "Crossbow, light",
                    "Crossbow, light",
                    category="simple",
                    damage="1d8",
                    damage_type="piercing",
                    attack_type="ranged",
                    attack_ability="dexterity",
                    properties=["ammunition", "loading", "two-handed"],
                    normal_range=80,
                    long_range=320,
                    ammunition_item_id="dorn-bolts",
                ),
            ],
            "main_hand": "dorn-longsword",
        },
        {
            "name": "Pip Underbough",
            "class": "Rogue",
            "species": "Lightfoot",
            "background": "Acolyte",
            "ability_method": "point_buy",
            "abilities": {
                "strength": 8,
                "dexterity": 15,
                "constitution": 14,
                "intelligence": 12,
                "wisdom": 10,
                "charisma": 13,
            },
            "species_selection": {},
            "background_languages": ["Elvish", "Goblin"],
            "hit_die": 8,
            "saving_throws": ["dexterity", "intelligence"],
            "skills": ["stealth", "investigation", "sleight_of_hand", "persuasion"],
            "armor_proficiencies": ["light armor"],
            "weapon_proficiencies": [
                "simple weapons",
                "hand crossbows",
                "longswords",
                "rapiers",
                "shortswords",
            ],
            "tool_proficiencies": ["thieves' tools"],
            "feature_choices": {"Expertise": {"proficiencies": ["stealth", "persuasion"]}},
            "items": [
                _armor(
                    "pip-leather",
                    "Leather",
                    "Leather",
                    base_ac=11,
                    dexterity_mode="full",
                ),
                _weapon(
                    "pip-shortsword",
                    "Shortsword",
                    "Shortsword",
                    category="martial",
                    damage="1d6",
                    damage_type="piercing",
                    attack_ability="dexterity",
                    properties=["finesse", "light"],
                ),
                _equipment(
                    "pip-arrows",
                    "Arrows",
                    "Arrows (20)",
                    kind="ammunition",
                    quantity=20,
                ),
                _weapon(
                    "pip-shortbow",
                    "Shortbow",
                    "Shortbow",
                    category="simple",
                    damage="1d6",
                    damage_type="piercing",
                    attack_type="ranged",
                    attack_ability="dexterity",
                    properties=["ammunition", "two-handed"],
                    normal_range=80,
                    long_range=320,
                    ammunition_item_id="pip-arrows",
                ),
                _equipment(
                    "pip-thieves-tools",
                    "Thieves' tools",
                    "Thieves' tools",
                    kind="tool",
                ),
            ],
            "main_hand": "pip-shortsword",
        },
        {
            "name": "Aelar Quill",
            "class": "Wizard",
            "species": "High Elf",
            "background": "Acolyte",
            "ability_method": "manual",
            "abilities": {
                "strength": 8,
                "dexterity": 13,
                "constitution": 14,
                "intelligence": 15,
                "wisdom": 12,
                "charisma": 10,
            },
            "species_selection": {
                "languages": ["Sylvan"],
                "cantrip": "Dancing Lights",
            },
            "background_languages": ["Celestial", "Draconic"],
            "hit_die": 6,
            "saving_throws": ["intelligence", "wisdom"],
            "skills": ["arcana", "history"],
            "armor_proficiencies": [],
            "weapon_proficiencies": [
                "daggers",
                "darts",
                "slings",
                "quarterstaffs",
                "light crossbows",
            ],
            "spellcasting": {
                "ability": "intelligence",
                "mode": "spellbook",
                "cantrips": ["Mage Hand", "Minor Illusion", "Ray of Frost"],
                "spells": [
                    "Detect Magic",
                    "Mage Armor",
                    "Magic Missile",
                    "Shield",
                    "Sleep",
                    "Thunderwave",
                ],
                "prepared": ["Detect Magic", "Mage Armor", "Magic Missile", "Sleep"],
                "ritual_casting": True,
            },
            "feature_choices": {},
            "items": [
                _weapon(
                    "aelar-quarterstaff",
                    "Quarterstaff",
                    "Quarterstaff",
                    category="simple",
                    damage="1d6",
                    damage_type="bludgeoning",
                    versatile="1d8",
                    properties=["versatile"],
                ),
                _equipment(
                    "aelar-components",
                    "Component pouch",
                    "Component pouch",
                    kind="focus",
                ),
                _equipment(
                    "aelar-spellbook",
                    "Spellbook",
                    "Spellbook",
                    kind="spellbook",
                    mechanics={
                        "edition": "2014",
                        "spell_ids": [],
                        "owner_mark": "Aelar Quill",
                        "deciphered": True,
                        "copyable": True,
                    },
                ),
            ],
            "main_hand": "aelar-quarterstaff",
        },
        {
            "name": "Brynja Stonefaith",
            "class": "Cleric",
            "species": "Hill Dwarf",
            "background": "Acolyte",
            "ability_method": "standard_array",
            "abilities": {
                "strength": 13,
                "dexterity": 10,
                "constitution": 14,
                "intelligence": 8,
                "wisdom": 15,
                "charisma": 12,
            },
            "species_selection": {"tools": ["smith's tools"]},
            "background_languages": ["Celestial", "Giant"],
            "hit_die": 8,
            "saving_throws": ["wisdom", "charisma"],
            "skills": ["medicine", "persuasion"],
            "armor_proficiencies": ["light armor", "medium armor", "shields"],
            "weapon_proficiencies": ["simple weapons"],
            "spellcasting": {
                "ability": "wisdom",
                "mode": "prepared",
                "cantrips": ["Guidance", "Sacred Flame", "Thaumaturgy"],
                "spells": [
                    "Detect Magic",
                    "Guiding Bolt",
                    "Healing Word",
                    "Sanctuary",
                ],
                "prepared": [
                    "Detect Magic",
                    "Guiding Bolt",
                    "Healing Word",
                    "Sanctuary",
                ],
                "ritual_casting": True,
            },
            "subclass": "Life Domain",
            "feature_choices": {},
            "items": [
                _armor(
                    "brynja-chain-mail",
                    "Chain mail",
                    "Chain mail",
                    base_ac=16,
                    dexterity_mode="none",
                    stealth_disadvantage=True,
                ),
                _shield("brynja-shield", "Shield"),
                _weapon(
                    "brynja-mace",
                    "Mace",
                    "Mace",
                    category="simple",
                    damage="1d6",
                    damage_type="bludgeoning",
                ),
                _equipment(
                    "brynja-symbol",
                    "Holy symbol (amulet)",
                    "Amulet",
                    kind="focus",
                ),
            ],
            "main_hand": "brynja-mace",
        },
        {
            "name": "Seraphine Vale",
            "class": "Bard",
            "species": "Half-Elf",
            "background": "Acolyte",
            "ability_method": "point_buy",
            "abilities": {
                "strength": 8,
                "dexterity": 14,
                "constitution": 13,
                "intelligence": 10,
                "wisdom": 12,
                "charisma": 15,
            },
            "species_selection": {
                "languages": ["Dwarvish"],
                "skills": ["perception", "survival"],
                "abilities": ["dexterity", "constitution"],
            },
            "background_languages": ["Celestial", "Draconic"],
            "hit_die": 8,
            "saving_throws": ["dexterity", "charisma"],
            "skills": ["acrobatics", "deception", "performance"],
            "armor_proficiencies": ["light armor"],
            "weapon_proficiencies": [
                "simple weapons",
                "hand crossbows",
                "longswords",
                "rapiers",
                "shortswords",
            ],
            "tool_proficiencies": ["lute"],
            "spellcasting": {
                "ability": "charisma",
                "mode": "known",
                "cantrips": ["Vicious Mockery", "Light"],
                "spells": ["Charm Person", "Faerie Fire", "Healing Word", "Heroism"],
                "prepared": [],
                "ritual_casting": True,
            },
            "feature_choices": {},
            # Bardic Inspiration is a card-local ``uses`` counter.  Seeding a
            # second top-level counter creates two independently recoverable
            # representations of the same class feature.
            "resources": {},
            "items": [
                _armor(
                    "seraphine-leather",
                    "Leather",
                    "Leather",
                    base_ac=11,
                    dexterity_mode="full",
                ),
                _weapon(
                    "seraphine-rapier",
                    "Rapier",
                    "Rapier",
                    category="martial",
                    damage="1d8",
                    damage_type="piercing",
                    attack_ability="dexterity",
                    properties=["finesse"],
                ),
                _weapon(
                    "seraphine-dagger",
                    "Dagger",
                    "Dagger",
                    category="simple",
                    damage="1d4",
                    damage_type="piercing",
                    attack_ability="dexterity",
                    properties=["finesse", "light", "thrown"],
                    normal_range=20,
                    long_range=60,
                ),
                _equipment("seraphine-lute", "Lute", "Lute", kind="tool"),
            ],
            "main_hand": "seraphine-rapier",
        },
    ]
    return _customize_core_backgrounds(profiles)


def waterdeep_party_profiles() -> list[dict[str, Any]]:
    """Return the four-PC party approved by Waterdeep's Agent-as-DM review.

    Dragon Heist itself specifies the level span but no player-count range. The
    corpus review therefore uses the 2014 Core CR baseline of an appropriately
    equipped and well-rested party of four instead of representing four as a
    module-authored recommendation. These four existing, source-audited plans
    cover manual, point-buy, and standard-array ability generation plus known,
    prepared, and spellbook casting.
    """

    selected_classes = {"Rogue", "Wizard", "Cleric", "Bard"}
    return [
        deepcopy(profile)
        for profile in lost_mine_party_profiles()
        if str(profile["class"]) in selected_classes
    ]


def tyranny_party_profiles() -> list[dict[str, Any]]:
    """Return four legal level-one PCs for the continuous Tyranny campaign.

    Hoard of the Dragon Queen explicitly starts four characters at 1st level,
    and The Rise of Tiamat continues with the same party. The corpus supplies no
    complete pregenerated character sheets, so these source-audited Core plans
    fill all four seats while retaining the required ability-generation and
    spell-resource diversity.
    """

    return waterdeep_party_profiles()


def _ranger_profile(*, favored_terrain: str) -> dict[str, Any]:
    return _customize_core_backgrounds(
        [
            {
                "name": "Tala Windmere",
                "class": "Ranger",
                "species": "Half-Orc",
                "background": "Acolyte",
                "ability_method": "manual",
                "abilities": {
                    "strength": 10,
                    "dexterity": 15,
                    "constitution": 14,
                    "intelligence": 12,
                    "wisdom": 13,
                    "charisma": 8,
                },
                "species_selection": {},
                "background_languages": ["Giant", "Primordial"],
                "hit_die": 10,
                "saving_throws": ["strength", "dexterity"],
                "skills": ["animal_handling", "nature", "survival"],
                "armor_proficiencies": ["light armor", "medium armor", "shields"],
                "weapon_proficiencies": ["simple weapons", "martial weapons"],
                "feature_choices": {
                    "Favored Enemy": {
                        "favored_enemy": {
                            "creature_type": "Giants",
                            "humanoid_races": [],
                            # The source-recommended Giant language is already
                            # granted by this customized background. Favored
                            # Enemy permits a language spoken by the enemy but
                            # does not grant a language the Ranger already
                            # knows, so this selection must not duplicate it.
                            "enemy_speaks_language": False,
                            "language": "",
                        }
                    },
                    "Natural Explorer": {"terrain": favored_terrain},
                },
                "items": [
                    _armor(
                        "tala-scale-mail",
                        "Scale mail",
                        "Scale mail",
                        base_ac=14,
                        dexterity_mode="max",
                        dexterity_max=2,
                        stealth_disadvantage=True,
                    ),
                    _weapon(
                        "tala-shortswords",
                        "Shortsword",
                        "Shortsword",
                        category="martial",
                        damage="1d6",
                        damage_type="piercing",
                        attack_ability="dexterity",
                        properties=["finesse", "light"],
                        quantity=2,
                    ),
                    _equipment(
                        "tala-arrows",
                        "Arrows",
                        "Arrows (20)",
                        kind="ammunition",
                        quantity=20,
                    ),
                    _weapon(
                        "tala-longbow",
                        "Longbow",
                        "Longbow",
                        category="martial",
                        damage="1d8",
                        damage_type="piercing",
                        attack_type="ranged",
                        attack_ability="dexterity",
                        properties=["ammunition", "heavy", "two-handed"],
                        normal_range=150,
                        long_range=600,
                        ammunition_item_id="tala-arrows",
                    ),
                    _equipment(
                        "tala-quiver",
                        "Quiver",
                        "Quiver",
                    ),
                ],
                "main_hand": "tala-longbow",
            }
        ]
    )[0]


def storm_kings_party_profiles() -> list[dict[str, Any]]:
    """Return six legal level-one PCs for SKT's formal chapter-one opening."""

    return [
        *lost_mine_party_profiles(),
        _ranger_profile(favored_terrain="Mountain"),
    ]


def tomb_of_annihilation_party_profiles() -> list[dict[str, Any]]:
    """Return six legal level-one PCs for the complete Chult expedition."""

    return [
        *lost_mine_party_profiles(),
        _ranger_profile(favored_terrain="Forest"),
    ]


def audit_profiles(
    profiles: list[dict[str, Any]],
    *,
    campaign_line_id: str = "lost-mine-of-phandelver",
) -> dict[str, Any]:
    if campaign_line_id == "lost-mine-of-phandelver":
        size_basis = {
            "kind": "module_source_maximum",
            "source_minimum": 4,
            "source_maximum": 5,
            "selected": 5,
        }
        pregen_review = {
            "module_mentions_included_characters": True,
            "official_sheets_present_in_corpus": False,
            "associated_pc_smalls_disposition": (
                "reviewed and excluded: non-module, incomplete, and requires "
                "unconfirmed Artificer/Gunsmith content"
            ),
        }
    elif campaign_line_id == "waterdeep-dragon-heist":
        size_basis = {
            "kind": "explicit_dm_review",
            "module_party_size_status": "not_stated_after_text_and_visual_review",
            "core_fallback": "2014 SRD Challenge baseline: party of four adventurers",
            "selected": 4,
            "represented_as_module_recommendation": False,
        }
        pregen_review = {
            "module_mentions_included_characters": False,
            "official_sheets_present_in_corpus": False,
            "associated_templates_present": 0,
            "disposition": "legally generate all four Agent-reviewed seats",
        }
    elif campaign_line_id == "tyranny-of-dragons":
        size_basis = {
            "kind": "module_source_maximum",
            "source_minimum": 4,
            "source_maximum": 4,
            "selected": 4,
            "starting_level": 1,
            "continuation": "preserve the same party into The Rise of Tiamat",
        }
        pregen_review = {
            "module_mentions_included_characters": False,
            "official_sheets_present_in_corpus": False,
            "associated_templates_present": 0,
            "disposition": (
                "legally generate all four source-confirmed seats once and "
                "preserve them across both volumes"
            ),
        }
    elif campaign_line_id == "storm-kings-thunder":
        size_basis = {
            "kind": "module_source_maximum",
            "source_minimum": 4,
            "source_maximum": 6,
            "selected": 6,
            "starting_level": 1,
            "alternate_starting_level": 5,
            "expected_ending_level": 11,
            "route": "formal chapter-one opening",
        }
        pregen_review = {
            "module_mentions_included_characters": False,
            "official_sheets_present_in_corpus": False,
            "associated_archetype_templates": 7,
            "associated_pc_stats_asset": "SKT-PCStats.txt",
            "disposition": (
                "reviewed and excluded as pregenerated PCs: the fillable PDFs "
                "preselect race, background, subclass, and later-level feature "
                "text but leave identity, level, all ability scores, hit points, "
                "armor class, attacks, equipment, and spells blank; the separate "
                "ability-number rows have no actor mapping or generation provenance"
            ),
        }
    elif campaign_line_id == "tomb-of-annihilation":
        size_basis = {
            "kind": "module_source_maximum",
            "source_minimum": 4,
            "source_maximum": 6,
            "selected": 6,
            "starting_level": 1,
            "expected_ending_level": 11,
            "route": "formal Syndra Silvane opening",
        }
        pregen_review = {
            "module_mentions_included_characters": False,
            "official_sheets_present_in_corpus": False,
            "associated_templates_present": 0,
            "disposition": "legally generate all six source-confirmed seats",
        }
    else:
        raise ValueError(f"unsupported campaign party profile: {campaign_line_id}")
    for profile in profiles:
        if set(profile["abilities"]) != set(ABILITY_NAMES):
            raise ValueError(f"{profile['name']} does not assign all six abilities")
    classes = [str(item["class"]) for item in profiles]
    species = [str(item["species"]) for item in profiles]
    methods = [str(item["ability_method"]) for item in profiles]
    if len(set(classes)) != len(classes):
        raise ValueError("party classes must be distinct")
    if len(set(species)) != len(species):
        raise ValueError("party species must be distinct")
    required_methods = {"manual", "standard_array", "point_buy"}
    if not required_methods.issubset(methods):
        raise ValueError("party must cover manual, standard-array, and point-buy generation")
    spell_modes = {str(dict(item.get("spellcasting") or {}).get("mode") or "") for item in profiles}
    if not SPELLCASTING_RESOURCE_MODELS.issubset(spell_modes):
        raise ValueError("party must cover known, prepared, and spellbook casting")
    backgrounds = sorted({str(item["background"]) for item in profiles})
    if len(backgrounds) != len(profiles):
        raise ValueError("party custom backgrounds must be distinct")
    for profile in profiles:
        background_skills = list(profile.get("background_skills") or [])
        background_languages = list(profile.get("background_languages") or [])
        characteristics = dict(profile.get("background_characteristics") or {})
        if len(background_skills) != 2 or len(set(background_skills)) != 2:
            raise ValueError("custom backgrounds must choose exactly two distinct skills")
        if len(background_languages) != 2 or len(set(background_languages)) != 2:
            raise ValueError("Core custom backgrounds must choose exactly two distinct languages")
        if len(characteristics.get("personality_traits") or []) != 2:
            raise ValueError("custom backgrounds must choose two personality traits")
        if any(
            len(characteristics.get(field) or []) != 1 for field in ("ideals", "bonds", "flaws")
        ):
            raise ValueError("custom backgrounds must choose one ideal, bond, and flaw")
    return {
        "selected_size": len(profiles),
        "source_maximum": (
            int(size_basis["source_maximum"]) if "source_maximum" in size_basis else None
        ),
        "party_size_basis": size_basis,
        "classes_unique": True,
        "species_unique": True,
        "ability_methods": sorted(set(methods)),
        "spell_resource_models": sorted(spell_modes - {""}),
        "backgrounds": backgrounds,
        "backgrounds_unique": True,
        "background_customization": {
            "base_artifact": "Acolyte",
            "rule": "2014 Core: Customizing a Background",
            "feature_disposition": "retain Shelter of the Faithful",
            "equipment_disposition": "retain the complete Acolyte package",
            "unconfirmed_extensions_used": False,
        },
        "pregenerated_first": pregen_review,
    }


def select_profiles(
    profiles: list[dict[str, Any]],
    *,
    profile_name: str,
    actor_name: str,
    campaign_line_id: str = "lost-mine-of-phandelver",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select a full party or one independently named replacement plan."""
    requested = profile_name.strip()
    replacement_name = actor_name.strip()
    if not requested:
        if replacement_name:
            raise ValueError("--actor-name requires --profile-name")
        return [deepcopy(item) for item in profiles], audit_profiles(
            profiles,
            campaign_line_id=campaign_line_id,
        )
    matches = [item for item in profiles if str(item["name"]).casefold() == requested.casefold()]
    if len(matches) != 1:
        raise ValueError("--profile-name must identify exactly one campaign party profile")
    profile = deepcopy(matches[0])
    source_profile_name = str(profile["name"])
    if replacement_name:
        profile["name"] = replacement_name
        for item in profile.get("items") or []:
            mechanics = dict(item.get("mechanics") or {})
            if mechanics.get("owner_mark") == source_profile_name:
                mechanics["owner_mark"] = replacement_name
                item["mechanics"] = mechanics
    return [profile], {
        "selected_size": 1,
        "purpose": "legal_replacement",
        "source_profile_name": source_profile_name,
        "actor_name": str(profile["name"]),
        "class": str(profile["class"]),
        "species": str(profile["species"]),
        "background": str(profile["background"]),
        "ability_method": str(profile["ability_method"]),
        "spell_resource_model": str(dict(profile.get("spellcasting") or {}).get("mode") or "none"),
        "knowledge_inheritance": "none",
    }


def _normalized_catalog_name(value: str) -> str:
    cleaned = re.sub(r"^[~*\s]+|[~*\s]+$", "", value)
    return re.sub(r"\s+", " ", cleaned).casefold()


def _catalog_match(
    catalog: list[dict[str, Any]],
    *,
    kind: str,
    name: str,
) -> dict[str, Any]:
    expected = _normalized_catalog_name(name)
    match = next(
        (
            item
            for item in catalog
            if item["kind"] == kind
            and _normalized_catalog_name(str(item["name"])) == expected
            and item.get("application_state") == "selection_ready"
        ),
        None,
    )
    if match is None:
        raise RuntimeError(f"active catalog has no selection-ready {kind}: {name}")
    return match


def _catalog_source(
    catalog: list[dict[str, Any]],
    name: str,
    *,
    kind: str = "item",
) -> str:
    expected = _normalized_catalog_name(name)
    match = next(
        (
            item
            for item in catalog
            if item["kind"] == kind and _normalized_catalog_name(str(item["name"])) == expected
        ),
        None,
    )
    if match is None:
        raise RuntimeError(f"active catalog has no source-linked {kind}: {name}")
    return str(match["id"])


def _source_linked_starting_items(
    profile: dict[str, Any],
    item_catalog: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items = deepcopy(
        [
            *list(profile["items"]),
            *_class_starting_supplements(profile),
            *_background_starting_items(profile),
        ]
    )
    for item in items:
        source_kind = str(item.pop("_source_kind", "item"))
        item["source_key"] = _catalog_source(
            item_catalog,
            str(item["source_key"]),
            kind=source_kind,
        )
    return items


def _configure_base_sheet(
    actor: dict[str, Any],
    profile: dict[str, Any],
    item_catalog: list[dict[str, Any]],
) -> dict[str, Any]:
    sheet = deepcopy(actor["sheet"])
    class_name = str(profile["class"])
    hit_die = int(profile["hit_die"])
    sheet["progression"]["level"] = 1
    sheet["progression"]["classes"] = [
        {"name": class_name, "level": 1, "subclass": "", "hit_die": hit_die}
    ]
    for ability in profile["saving_throws"]:
        sheet["abilities"][ability]["save_proficient"] = True
    for skill in profile["skills"]:
        sheet["skills"][skill]["proficiency"] = "proficient"
    constitution = int(sheet["abilities"]["constitution"]["score"])
    hp = hit_die + ability_modifier(constitution)
    sheet["combat"]["hp"] = {"value": hp, "max": hp, "temp": 0}
    sheet["combat"]["hit_dice"] = {
        f"d{hit_die}": {
            "label": f"d{hit_die}",
            "value": 1,
            "max": 1,
            "recovers_on": "long_rest",
            "source_key": class_name,
        }
    }
    sheet["combat"]["hp_progression"] = [
        {
            "level": 1,
            "method": "fixed",
            "value": hp,
            "source": f"dnd5e.content.srd2014 class {class_name} level 1",
        }
    ]
    sheet["traits"]["proficiencies"]["armor"] = list(profile["armor_proficiencies"])
    sheet["traits"]["proficiencies"]["weapons"] = list(profile["weapon_proficiencies"])
    sheet["traits"]["proficiencies"]["tools"] = list(profile.get("tool_proficiencies") or [])
    sheet["resources"] = deepcopy(profile.get("resources") or {})
    items = _source_linked_starting_items(profile, item_catalog)
    sheet["inventory"]["items"] = items
    sheet["inventory"]["wallet"]["gp"] = 15
    sheet["inventory"]["equipment_slots"]["armor"] = next(
        (item["id"] for item in items if item["kind"] == "armor" and item.get("equipped")),
        None,
    )
    sheet["inventory"]["equipment_slots"]["shield"] = next(
        (item["id"] for item in items if item["kind"] == "shield" and item.get("equipped")),
        None,
    )
    sheet["inventory"]["equipment_slots"]["main_hand"] = str(profile["main_hand"])
    for item in items:
        if item["id"] == profile["main_hand"]:
            item["equipped"] = True
            item["equipped_slot"] = "main_hand"
    spellcasting = dict(profile.get("spellcasting") or {})
    if spellcasting:
        mode = str(spellcasting["mode"])
        ability = str(spellcasting["ability"])
        modifier = ability_modifier(int(sheet["abilities"][ability]["score"]))
        sheet["spellcasting"]["ability"] = ability
        sheet["spellcasting"]["class_lists"] = [class_name.casefold()]
        sheet["spellcasting"]["spell_slots"] = {
            "1": {
                "label": "Level 1 spell slots",
                "value": 2,
                "max": 2,
                "recovers_on": "long_rest",
                "source_key": class_name,
                "slot_level": 1,
            }
        }
        max_prepared = max(1, modifier + 1) if mode in PREPARED_SELECTION_MODES else 0
        sheet["spellcasting"]["preparation"] = {
            "mode": mode,
            "max_prepared": max_prepared,
            "changes_on": "long_rest",
            "selected_spell_ids": [],
        }
        sheet["spellcasting"]["ritual_casting"] = bool(spellcasting.get("ritual_casting"))
        sheet["spellcasting"]["spellbook"] = {
            "enabled": mode == "spellbook",
            "spell_ids": [],
        }
    return sheet


def _configure_notes(actor: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    notes = deepcopy(actor["notes"])
    characteristics = dict(profile["background_characteristics"])
    notes["profile"]["personality_traits"] = list(characteristics["personality_traits"])
    notes["profile"]["ideals"] = list(characteristics["ideals"])
    notes["profile"]["bonds"] = list(characteristics["bonds"])
    notes["profile"]["flaws"] = list(characteristics["flaws"])
    return notes


def _spellcasting_audit(
    actor: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Prove that the generated card retained every legal level-one spell choice."""

    configured = dict(profile.get("spellcasting") or {})
    sheet = dict(actor["sheet"])
    spellcasting = dict(sheet["spellcasting"])
    cards = [
        dict(item)
        for item in dict(sheet.get("content") or {}).get("spells", [])
        if isinstance(item, dict)
    ]
    cantrips = [item for item in cards if int(item.get("level", -1)) == 0]
    leveled = [item for item in cards if int(item.get("level", -1)) > 0]
    known_ids = [
        str(item["id"]) for item in cards if bool(dict(item.get("access") or {}).get("known"))
    ]
    prepared_ids = [
        str(item)
        for item in dict(spellcasting.get("preparation") or {}).get("selected_spell_ids", [])
    ]
    spellbook_ids = [
        str(item) for item in dict(spellcasting.get("spellbook") or {}).get("spell_ids", [])
    ]
    mode = str(configured.get("mode") or "none")
    resolution_audit = audit_spell_resolution_paths(sheet)
    if not configured:
        return {
            "mode": "none",
            "cantrip_spell_ids": [str(item["id"]) for item in cantrips],
            "cantrip_spell_names": [str(item["name"]) for item in cantrips],
            "known_spell_ids": known_ids,
            "prepared_spell_ids": prepared_ids,
            "spellbook_spell_ids": spellbook_ids,
            "leveled_spell_ids": [str(item["id"]) for item in leveled],
            "resolution_audit": resolution_audit,
        }

    class_name = str(profile["class"]).casefold()
    level = int(sheet["progression"]["level"])
    expected_class_cantrips = [str(item) for item in configured.get("cantrips", [])]
    expected_cantrip_count = CANTRIPS_KNOWN.get(class_name, (0,) * 20)[level - 1]
    if len(expected_class_cantrips) != expected_cantrip_count:
        raise RuntimeError(
            f"{profile['name']} profile records {len(expected_class_cantrips)} "
            f"class cantrips but 2014 {profile['class']} level {level} requires "
            f"{expected_cantrip_count}"
        )
    expected_species_cantrip = str(
        dict(profile.get("species_selection") or {}).get("cantrip") or ""
    ).strip()
    expected_cantrip_names = [
        *expected_class_cantrips,
        *([expected_species_cantrip] if expected_species_cantrip else []),
    ]
    cards_by_name = {str(item.get("name") or "").strip().casefold(): item for item in cards}
    missing_cantrips = [
        name for name in expected_cantrip_names if name.casefold() not in cards_by_name
    ]
    invalid_cantrips = [
        name
        for name in expected_cantrip_names
        if name.casefold() in cards_by_name
        and (
            int(cards_by_name[name.casefold()].get("level", -1)) != 0
            or not bool(dict(cards_by_name[name.casefold()].get("access") or {}).get("known"))
        )
    ]
    if missing_cantrips or invalid_cantrips:
        raise RuntimeError(
            f"{profile['name']} generated card has an incomplete cantrip grant: "
            f"missing={missing_cantrips}, invalid={invalid_cantrips}"
        )

    expected_leveled_names = [str(item) for item in configured.get("spells", [])]
    missing_leveled = [
        name for name in expected_leveled_names if name.casefold() not in cards_by_name
    ]
    if missing_leveled:
        raise RuntimeError(
            f"{profile['name']} generated card is missing configured spells: "
            + ", ".join(missing_leveled)
        )
    expected_leveled_ids = [
        str(cards_by_name[name.casefold()]["id"]) for name in expected_leveled_names
    ]
    if class_name in KNOWN_SPELLS:
        required_known = KNOWN_SPELLS[class_name][level - 1]
        if len(expected_leveled_names) != required_known:
            raise RuntimeError(
                f"{profile['name']} profile records {len(expected_leveled_names)} "
                f"known leveled spells but 2014 {profile['class']} level {level} "
                f"requires {required_known}"
            )
    if mode == "known" and any(
        not bool(dict(cards_by_name[name.casefold()].get("access") or {}).get("known"))
        for name in expected_leveled_names
    ):
        raise RuntimeError(f"{profile['name']} known caster has an unowned spell")
    if mode == "spellbook" and set(spellbook_ids) != set(expected_leveled_ids):
        raise RuntimeError(
            f"{profile['name']} spellbook does not exactly match its six level-one Wizard spells"
        )
    expected_prepared_ids = [
        str(cards_by_name[str(name).casefold()]["id"]) for name in configured.get("prepared", [])
    ]
    if prepared_ids != expected_prepared_ids:
        raise RuntimeError(f"{profile['name']} prepared spell ids do not match the audited profile")
    if resolution_audit["missing_spell_ids"]:
        raise RuntimeError(
            f"{profile['name']} generated card has spells without a settlement path: "
            + ", ".join(resolution_audit["missing_spell_ids"])
        )
    return {
        "mode": mode,
        "cantrip_spell_ids": [str(item["id"]) for item in cantrips],
        "cantrip_spell_names": [str(item["name"]) for item in cantrips],
        "known_spell_ids": known_ids,
        "prepared_spell_ids": prepared_ids,
        "spellbook_spell_ids": spellbook_ids,
        "leveled_spell_ids": [str(item["id"]) for item in leveled],
        "resolution_audit": resolution_audit,
    }


async def _catalog(client: ExposureClient, campaign_id: str) -> list[dict[str, Any]]:
    return list(
        _facade_value(
            await client.domain(
                "character_query",
                {
                    "view": "catalog",
                    "payload": {"campaign_id": campaign_id},
                },
            )
        )
    )


def _mutation_actor(value: Any) -> dict[str, Any]:
    normalized = dict(_facade_value(value))
    return dict(normalized.get("character") or normalized)


async def _apply_artifact(
    client: ExposureClient,
    *,
    actor: dict[str, Any],
    artifact: dict[str, Any],
    selection: dict[str, Any],
    key: str,
) -> dict[str, Any]:
    result = await client.domain(
        "character_content_apply",
        {
            "character_id": actor["id"],
            "artifact_id": artifact["id"],
            "selection": selection,
            "expected_revision": actor["revision"],
            "idempotency_key": key,
        },
    )
    value = _facade_value(result)
    raise_for_pending_ruling(
        value,
        operation="character_content_apply.party",
        context={
            "actor_id": str(actor["id"]),
            "artifact_id": str(artifact["id"]),
            "artifact_name": str(artifact["name"]),
        },
    )
    return dict(value)


async def _initialize_prepared_spells(
    client: ExposureClient,
    *,
    actor: dict[str, Any],
    prepared_ids: list[str],
    idempotency_key: str,
) -> dict[str, Any]:
    """Resume one-time setup without replaying an already committed spell list."""
    actor = dict(
        _facade_value(
            await client.domain(
                "character_query",
                {
                    "view": "get",
                    "payload": {"character_id": actor["id"]},
                },
            )
        )
    )
    preparation = dict(dict(actor["sheet"].get("spellcasting") or {}).get("preparation") or {})
    existing_ids = [str(item) for item in preparation.get("selected_spell_ids") or []]
    if existing_ids:
        if existing_ids != prepared_ids:
            raise RuntimeError(
                "resumed party construction found a different committed prepared-spell "
                "list; use the edition's legal long-rest or level-up workflow"
            )
        return actor
    prepared = _facade_value(
        await client.domain(
            "character_spell_prepare",
            {
                "character_id": actor["id"],
                "mode": "replace_all",
                "payload": {
                    "spell_ids": prepared_ids,
                    "event": "setup",
                },
                "expected_revision": actor["revision"],
                "idempotency_key": idempotency_key,
            },
        )
    )
    return dict(prepared.get("character") or prepared)


async def _build_character(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    campaign_line_id: str,
    profile: dict[str, Any],
    catalog: list[dict[str, Any]],
) -> dict[str, Any]:
    slug = _token(f"{run_id}:{profile['name']}", length=20)
    built = _facade_value(
        await client.domain(
            "character_create_from",
            {
                "mode": "build",
                "payload": {
                    "campaign_id": campaign_id,
                    "name": profile["name"],
                    "summary": (
                        f"Generated source-audited PC for {campaign_line_id} after "
                        "the corpus pregen-first review found no usable official sheet."
                    ),
                },
                "idempotency_key": f"full-party-{slug}-build",
            },
        )
    )
    actor = dict(built["instance"])
    ability = _facade_value(
        await client.domain(
            "character_ability_apply",
            {
                "character_id": actor["id"],
                "method": profile["ability_method"],
                "assignments": profile["abilities"],
                "expected_revision": actor["revision"],
                "idempotency_key": f"full-party-{slug}-abilities",
            },
        )
    )
    actor = dict(ability["character"])
    actor = dict(
        _facade_value(
            await client.domain(
                "character_sheet_replace",
                {
                    "character_id": actor["id"],
                    "sheet": _configure_base_sheet(actor, profile, catalog),
                    "notes": _configure_notes(actor, profile),
                    "expected_revision": actor["revision"],
                    "idempotency_key": f"full-party-{slug}-class-sheet",
                },
            )
        )
    )
    species = _catalog_match(catalog, kind="species", name=str(profile["species"]))
    species_selection = deepcopy(profile["species_selection"])
    cantrip_name = str(species_selection.pop("cantrip", "") or "")
    if cantrip_name:
        species_selection["cantrip_artifact_id"] = _catalog_match(
            catalog, kind="spell", name=cantrip_name
        )["id"]
    actor = await _apply_artifact(
        client,
        actor=actor,
        artifact=species,
        selection=species_selection,
        key=f"full-party-{slug}-species",
    )
    background = _catalog_match(
        catalog,
        kind="background",
        name=str(profile.get("background_base") or profile["background"]),
    )
    background_item_ids = [str(item["id"]) for item in _background_starting_items(profile)]
    actor = await _apply_artifact(
        client,
        actor=actor,
        artifact=background,
        selection={
            "custom_name": str(profile["background"]),
            "skills": list(profile["background_skills"]),
            "languages": list(profile["background_languages"]),
            "equipment_item_ids": background_item_ids,
        },
        key=f"full-party-{slug}-background",
    )
    class_features = [
        item
        for item in catalog
        if item["kind"] == "feature"
        and str(item["selection_requirements"].get("class_name") or "").casefold()
        == str(profile["class"]).casefold()
        and not str(item["selection_requirements"].get("subclass_name") or "")
        and int(item["selection_requirements"].get("minimum_level", 1) or 1) <= 1
    ]
    applied_features: list[str] = []
    for feature in class_features:
        actor = await _apply_artifact(
            client,
            actor=actor,
            artifact=feature,
            selection=deepcopy(
                dict(profile.get("feature_choices") or {}).get(feature["name"]) or {}
            ),
            key=f"full-party-{slug}-feature-{_token(str(feature['id']))}",
        )
        applied_features.append(str(feature["id"]))
    subclass_name = str(profile.get("subclass") or "")
    if subclass_name:
        subclass = _catalog_match(catalog, kind="subclass", name=subclass_name)
        actor = await _apply_artifact(
            client,
            actor=actor,
            artifact=subclass,
            selection={"target_class_name": profile["class"]},
            key=f"full-party-{slug}-subclass",
        )
        subclass_features = [
            item
            for item in catalog
            if item["kind"] == "feature"
            and str(item["selection_requirements"].get("subclass_name") or "").casefold()
            == subclass_name.casefold()
            and int(item["selection_requirements"].get("minimum_level", 1) or 1) <= 1
        ]
        for feature in subclass_features:
            actor = await _apply_artifact(
                client,
                actor=actor,
                artifact=feature,
                selection={},
                key=f"full-party-{slug}-subclass-feature-{_token(str(feature['id']))}",
            )
            applied_features.append(str(feature["id"]))
    spellcasting = dict(profile.get("spellcasting") or {})
    spell_ids_by_name: dict[str, str] = {}
    if spellcasting:
        mode = str(spellcasting["mode"])
        for name in [*spellcasting["cantrips"], *spellcasting["spells"]]:
            if name in spell_ids_by_name:
                continue
            artifact = _catalog_match(catalog, kind="spell", name=name)
            spell_ids_by_name[name] = str(artifact["id"])
            level = int(artifact["selection_requirements"].get("level", 0) or 0)
            existing_ids = {str(item.get("id")) for item in actor["sheet"]["content"]["spells"]}
            if artifact["id"] in existing_ids:
                continue
            method = (
                "known"
                if level == 0 or mode == "known"
                else ("spellbook" if mode == "spellbook" else "class_prepared")
            )
            actor = await _apply_artifact(
                client,
                actor=actor,
                artifact=artifact,
                selection={"source_class": profile["class"], "method": method},
                key=f"full-party-{slug}-spell-{_token(str(artifact['id']))}",
            )
        prepared_ids = [spell_ids_by_name[name] for name in spellcasting["prepared"]]
        if prepared_ids:
            actor = await _initialize_prepared_spells(
                client,
                actor=actor,
                prepared_ids=prepared_ids,
                idempotency_key=f"full-party-{slug}-prepare-spells",
            )
    if str(profile["class"]).casefold() == "wizard":
        spellbook_item = next(
            item for item in actor["sheet"]["inventory"]["items"] if item["kind"] == "spellbook"
        )
        updated = _facade_value(
            await client.domain(
                "inventory_change",
                {
                    "owner": "character",
                    "action": "update",
                    "owner_id": actor["id"],
                    "payload": {
                        "item_id": spellbook_item["id"],
                        "patch": {
                            "mechanics": {
                                **dict(spellbook_item["mechanics"]),
                                "spell_ids": [
                                    spell_ids_by_name[name] for name in spellcasting["spells"]
                                ],
                            }
                        },
                    },
                    "expected_revision": actor["revision"],
                    "idempotency_key": f"full-party-{slug}-spellbook-item",
                },
            )
        )
        actor = dict(updated)
    spellcasting_audit = _spellcasting_audit(actor, profile)
    return {
        "actor_id": actor["id"],
        "name": actor["name"],
        "class": profile["class"],
        "species": actor["sheet"]["progression"]["species"],
        "background": actor["sheet"]["progression"]["background"],
        "background_base": actor["sheet"]["progression"]["background_grants"]["choices"][
            "base_background"
        ],
        "background_skill_ids": list(
            actor["sheet"]["progression"]["background_grants"]["choices"]["selected_skills"]
        ),
        "background_equipment_item_ids": list(
            actor["sheet"]["progression"]["background_grants"]["equipment_item_ids"]
        ),
        "background_characteristics": {
            "personality_traits": list(actor["notes"]["profile"]["personality_traits"]),
            "ideals": list(actor["notes"]["profile"]["ideals"]),
            "bonds": list(actor["notes"]["profile"]["bonds"]),
            "flaws": list(actor["notes"]["profile"]["flaws"]),
        },
        "ability_method": actor["sheet"]["ability_generation"]["method"],
        "level": actor["sheet"]["progression"]["level"],
        "hp": deepcopy(actor["derived"]["hit_points"]),
        "armor_class": actor["derived"]["armor_class"],
        "spellcasting_mode": spellcasting_audit["mode"],
        "cantrip_spell_ids": spellcasting_audit["cantrip_spell_ids"],
        "known_spell_ids": spellcasting_audit["known_spell_ids"],
        "prepared_spell_ids": spellcasting_audit["prepared_spell_ids"],
        "spellbook_spell_ids": spellcasting_audit["spellbook_spell_ids"],
        "spellcasting_audit": spellcasting_audit,
        "inventory_item_ids": [str(item["id"]) for item in actor["sheet"]["inventory"]["items"]],
        "wallet": deepcopy(actor["sheet"]["inventory"]["wallet"]),
        "applied_feature_ids": applied_features,
        "source": "generated",
        "source_asset_path": "",
        "status": "active",
    }


async def _campaign(client: ExposureClient, campaign_id: str) -> dict[str, Any]:
    return await campaign_view(client, campaign_id)


async def _switch_phase(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
    current_phase: str,
    target_phase: str,
    purpose: str,
) -> dict[str, Any] | None:
    if current_phase == target_phase:
        return None
    if current_phase not in CAMPAIGN_GAME_PHASES or target_phase not in CAMPAIGN_GAME_PHASES:
        raise RuntimeError("party construction cannot transition through combat")
    branches = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in branches if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    campaign = await _campaign(client, campaign_id)
    changed = _facade_value(
        await client.core(
            "game_phase",
            {
                "campaign_id": campaign_id,
                "action": "set",
                "tool_profile": target_phase,
                "expected_revision": campaign["revision"],
                "branch_id": str(branch["id"]),
                "idempotency_key": (
                    f"full-party-phase-{_token(run_id)}-{_token(purpose)}-"
                    f"{current_phase}-{target_phase}-r{campaign['revision']}"
                ),
            },
        )
    )
    await client.open(campaign_id)
    await client.load()
    return dict(changed)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    profile_factories = {
        "lost-mine-of-phandelver": lost_mine_party_profiles,
        "waterdeep-dragon-heist": waterdeep_party_profiles,
        "tyranny-of-dragons": tyranny_party_profiles,
        "storm-kings-thunder": storm_kings_party_profiles,
        "tomb-of-annihilation": tomb_of_annihilation_party_profiles,
    }
    profiles, profile_audit = select_profiles(
        profile_factories[args.party](),
        profile_name=args.profile_name,
        actor_name=args.actor_name,
        campaign_line_id=args.party,
    )
    async with stdio_client(_server_parameters(args)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            client = ExposureClient(session)
            await client.open(args.campaign_id)
            campaign = await _campaign(client, args.campaign_id)
            entry_phase = str(campaign.get("effective_game_phase") or "")
            if entry_phase not in EFFECTIVE_GAME_PHASES:
                raise RuntimeError(
                    f"campaign view has no valid effective_game_phase: {entry_phase!r}"
                )
            if entry_phase == "combat":
                raise RuntimeError("party construction cannot run during active combat")
            if len(profiles) > 1 and entry_phase != "lobby":
                raise RuntimeError("full party construction requires the public Lobby profile")
            return_phase = args.return_phase or entry_phase
            if return_phase not in CAMPAIGN_GAME_PHASES:
                raise ValueError("--return-phase must be lobby or play")
            if entry_phase == "lobby":
                await client.load()
            else:
                await client.load()
            phase_changes: list[dict[str, Any]] = []
            current_phase = entry_phase
            if current_phase == "play":
                changed = await _switch_phase(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    current_phase=current_phase,
                    target_phase="lobby",
                    purpose="enter-lobby",
                )
                if changed is not None:
                    phase_changes.append(changed)
                current_phase = "lobby"
            try:
                catalog = await _catalog(client, args.campaign_id)
                characters = [
                    await _build_character(
                        client,
                        campaign_id=args.campaign_id,
                        run_id=args.run_id,
                        campaign_line_id=args.party,
                        profile=profile,
                        catalog=catalog,
                    )
                    for profile in profiles
                ]
            except Exception:
                if entry_phase == "play" and current_phase == "lobby":
                    await _switch_phase(
                        client,
                        campaign_id=args.campaign_id,
                        run_id=args.run_id,
                        current_phase="lobby",
                        target_phase="play",
                        purpose="failure-restore-play",
                    )
                raise
            if current_phase != return_phase:
                changed = await _switch_phase(
                    client,
                    campaign_id=args.campaign_id,
                    run_id=args.run_id,
                    current_phase=current_phase,
                    target_phase=return_phase,
                    purpose="return",
                )
                if changed is not None:
                    phase_changes.append(changed)
            return {
                "action": "build-campaign-party",
                "transport": "stdio",
                "campaign_id": args.campaign_id,
                "campaign_line_id": args.party,
                "profile_audit": profile_audit,
                "characters": characters,
                "entry_phase": entry_phase,
                "return_phase": return_phase,
                "phase_changes": phase_changes,
                "manifest_members": [
                    {
                        key: character[key]
                        for key in (
                            "actor_id",
                            "source",
                            "source_asset_path",
                            "status",
                        )
                    }
                    for character in characters
                ],
            }


def main() -> int:
    args = _arguments()
    try:
        report = asyncio.run(_run(args))
    except Exception as error:
        report = {
            "action": "build-campaign-party",
            "campaign_id": args.campaign_id,
            "run_id": args.run_id,
            "campaign_line_id": args.party,
            "passed": False,
            "error": "; ".join(exception_leaf_messages(error)),
            **ruling_failure_fields(error),
        }
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("passed", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
