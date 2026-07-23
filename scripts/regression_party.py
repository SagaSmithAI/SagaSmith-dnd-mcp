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
import os
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from scripts.regression_modules import ExposureClient, _token

ABILITY_NAMES = (
    "strength",
    "dexterity",
    "constitution",
    "intelligence",
    "wisdom",
    "charisma",
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", default="full-playthrough-v1")
    parser.add_argument(
        "--party",
        choices=("lost-mine-of-phandelver",),
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
        choices=("lobby", "play"),
        default="",
        help="Phase to expose after construction; defaults to the entry phase",
    )
    return parser.parse_args()


def _server_parameters(args: argparse.Namespace) -> StdioServerParameters:
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "SAGASMITH_DND_MCP_HOME": str(args.home.expanduser().resolve()),
            "SAGASMITH_DND_MCP_AUTO_SEED": "1",
        }
    )
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "sagasmith_dnd_mcp.server"],
        cwd=repo,
        env=env,
    )


def _facade_value(value: Any) -> Any:
    if isinstance(value, dict) and "result" in value:
        return value["result"]
    return value


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
) -> dict[str, Any]:
    return {
        "id": identifier,
        "name": name,
        "kind": "weapon",
        "quantity": 1,
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
) -> dict[str, Any]:
    return {
        "id": identifier,
        "name": name,
        "kind": kind,
        "quantity": quantity,
        "source_key": source_key,
        "mechanics": deepcopy(mechanics or {}),
    }


def lost_mine_party_profiles() -> list[dict[str, Any]]:
    """Return five deliberately varied, level-one 2014 Core character plans."""

    return [
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
            "feature_choices": {
                "Expertise": {"proficiencies": ["stealth", "persuasion"]}
            },
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
            "resources": {
                "bardic_inspiration": {
                    "label": "Bardic Inspiration",
                    "value": 3,
                    "max": 3,
                    "recovers_on": "long_rest",
                    "source_key": "Bard",
                }
            },
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


def audit_profiles(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    if len(profiles) != 5:
        raise ValueError("Lost Mine of Phandelver must use the source maximum of five PCs")
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
    spell_modes = {
        str(dict(item.get("spellcasting") or {}).get("mode") or "")
        for item in profiles
    }
    if not {"known", "prepared", "spellbook"}.issubset(spell_modes):
        raise ValueError("party must cover known, prepared, and spellbook casting")
    backgrounds = sorted({str(item["background"]) for item in profiles})
    return {
        "selected_size": len(profiles),
        "source_maximum": 5,
        "classes_unique": True,
        "species_unique": True,
        "ability_methods": sorted(set(methods)),
        "spell_resource_models": sorted(spell_modes - {""}),
        "backgrounds": backgrounds,
        "background_diversity_exception": (
            "The enabled 2014 Core structured catalog exposes only Acolyte; "
            "no unconfirmed extension background was invented."
        ),
        "pregenerated_first": {
            "module_mentions_included_characters": True,
            "official_sheets_present_in_corpus": False,
            "associated_pc_smalls_disposition": (
                "reviewed and excluded: non-module, incomplete, and requires "
                "unconfirmed Artificer/Gunsmith content"
            ),
        },
    }


def select_profiles(
    profiles: list[dict[str, Any]],
    *,
    profile_name: str,
    actor_name: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select a full party or one independently named replacement plan."""
    requested = profile_name.strip()
    replacement_name = actor_name.strip()
    if not requested:
        if replacement_name:
            raise ValueError("--actor-name requires --profile-name")
        return [deepcopy(item) for item in profiles], audit_profiles(profiles)
    matches = [
        item for item in profiles if str(item["name"]).casefold() == requested.casefold()
    ]
    if len(matches) != 1:
        raise ValueError("--profile-name must identify exactly one campaign party profile")
    profile = deepcopy(matches[0])
    source_profile_name = str(profile["name"])
    if replacement_name:
        profile["name"] = replacement_name
    return [profile], {
        "selected_size": 1,
        "purpose": "legal_replacement",
        "source_profile_name": source_profile_name,
        "actor_name": str(profile["name"]),
        "class": str(profile["class"]),
        "species": str(profile["species"]),
        "background": str(profile["background"]),
        "ability_method": str(profile["ability_method"]),
        "spell_resource_model": str(
            dict(profile.get("spellcasting") or {}).get("mode") or "none"
        ),
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
) -> str:
    expected = _normalized_catalog_name(name)
    match = next(
        (
            item
            for item in catalog
            if item["kind"] == "item"
            and _normalized_catalog_name(str(item["name"])) == expected
        ),
        None,
    )
    if match is None:
        raise RuntimeError(f"active catalog has no source-linked item: {name}")
    return str(match["id"])


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
    hp = hit_die + (constitution - 10) // 2
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
    sheet["traits"]["proficiencies"]["tools"] = list(
        profile.get("tool_proficiencies") or []
    )
    sheet["resources"] = deepcopy(profile.get("resources") or {})
    items = deepcopy(list(profile["items"]))
    for item in items:
        item["source_key"] = _catalog_source(item_catalog, str(item["source_key"]))
    sheet["inventory"]["items"] = items
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
        modifier = (int(sheet["abilities"][ability]["score"]) - 10) // 2
        sheet["spellcasting"]["ability"] = ability
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
        max_prepared = (
            max(1, modifier + 1) if mode in {"prepared", "spellbook"} else 0
        )
        sheet["spellcasting"]["preparation"] = {
            "mode": mode,
            "max_prepared": max_prepared,
            "changes_on": "long_rest",
            "selected_spell_ids": [],
        }
        sheet["spellcasting"]["ritual_casting"] = bool(
            spellcasting.get("ritual_casting")
        )
        sheet["spellcasting"]["spellbook"] = {
            "enabled": mode == "spellbook",
            "spell_ids": [],
        }
    return sheet


async def _catalog(client: ExposureClient, campaign_id: str) -> list[dict[str, Any]]:
    return list(
        _facade_value(
            await client.domain(
                "rule_pack_query",
                {
                    "view": "content_catalog",
                    "payload": {"campaign_id": campaign_id},
                },
            )
        )
    )


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
    if value.get("status") == "pending_ruling":
        raise RuntimeError(
            f"catalog artifact needs review: {artifact['name']}: {value['reason']}"
        )
    return dict(value)


async def _build_character(
    client: ExposureClient,
    *,
    campaign_id: str,
    run_id: str,
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
                        "Generated fallback PC for Lost Mine of Phandelver after "
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
        catalog, kind="background", name=str(profile["background"])
    )
    actor = await _apply_artifact(
        client,
        actor=actor,
        artifact=background,
        selection={"languages": list(profile["background_languages"])},
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
            existing_ids = {
                str(item.get("id")) for item in actor["sheet"]["content"]["spells"]
            }
            if artifact["id"] in existing_ids:
                continue
            method = "known" if level == 0 or mode == "known" else (
                "spellbook" if mode == "spellbook" else "class_prepared"
            )
            actor = await _apply_artifact(
                client,
                actor=actor,
                artifact=artifact,
                selection={"source_class": profile["class"], "method": method},
                key=f"full-party-{slug}-spell-{_token(str(artifact['id']))}",
            )
        prepared_ids = [
            spell_ids_by_name[name] for name in spellcasting["prepared"]
        ]
        if prepared_ids:
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
                        "idempotency_key": f"full-party-{slug}-prepare-spells",
                    },
                )
            )
            actor = dict(prepared.get("character") or prepared)
    if str(profile["class"]).casefold() == "wizard":
        spellbook_item = next(
            item
            for item in actor["sheet"]["inventory"]["items"]
            if item["kind"] == "spellbook"
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
                                    spell_ids_by_name[name]
                                    for name in spellcasting["spells"]
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
    return {
        "actor_id": actor["id"],
        "name": actor["name"],
        "class": profile["class"],
        "species": actor["sheet"]["progression"]["species"],
        "background": actor["sheet"]["progression"]["background"],
        "ability_method": actor["sheet"]["ability_generation"]["method"],
        "level": actor["sheet"]["progression"]["level"],
        "hp": deepcopy(actor["derived"]["hit_points"]),
        "armor_class": actor["derived"]["armor_class"],
        "spellcasting_mode": actor["sheet"]["spellcasting"]["preparation"]["mode"],
        "prepared_spell_ids": list(
            actor["sheet"]["spellcasting"]["preparation"]["selected_spell_ids"]
        ),
        "inventory_item_ids": [
            str(item["id"]) for item in actor["sheet"]["inventory"]["items"]
        ],
        "applied_feature_ids": applied_features,
        "source": "generated",
        "source_asset_path": "",
        "status": "active",
    }


async def _campaign(client: ExposureClient, campaign_id: str) -> dict[str, Any]:
    return dict(
        _facade_value(
            await client.core(
                "campaign_query",
                {
                    "view": "get",
                    "payload": {"campaign_id": campaign_id},
                },
            )
        )
    )


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
    if current_phase not in {"lobby", "play"} or target_phase not in {"lobby", "play"}:
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
    if target_phase == "lobby":
        await client.load("lobby.campaign", "lobby.rules", "lobby.characters")
    else:
        await client.load("play.scene_control", "play.scene")
    return dict(changed)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    profiles, profile_audit = select_profiles(
        lost_mine_party_profiles(),
        profile_name=args.profile_name,
        actor_name=args.actor_name,
    )
    async with stdio_client(_server_parameters(args)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            client = ExposureClient(session)
            await client.open(args.campaign_id)
            campaign = await _campaign(client, args.campaign_id)
            entry_phase = str(
                dict(campaign.get("state") or {}).get("game_phase") or "lobby"
            )
            if entry_phase == "combat":
                raise RuntimeError("party construction cannot run during active combat")
            if len(profiles) > 1 and entry_phase != "lobby":
                raise RuntimeError("full party construction requires the public Lobby profile")
            return_phase = args.return_phase or entry_phase
            if return_phase not in {"lobby", "play"}:
                raise ValueError("--return-phase must be lobby or play")
            if entry_phase == "lobby":
                await client.load("lobby.campaign", "lobby.rules", "lobby.characters")
            else:
                await client.load("play.scene_control", "play.scene")
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
                        for key in ("actor_id", "source", "source_asset_path", "status")
                    }
                    for character in characters
                ],
            }


def main() -> int:
    args = _arguments()
    report = asyncio.run(_run(args))
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
