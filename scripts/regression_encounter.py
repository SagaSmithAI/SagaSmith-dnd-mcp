"""Run a source-defined encounter exclusively through public stdio MCP tools."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from scripts.regression_modules import PRINCIPAL_ID, ExposureClient, _token
from scripts.regression_playthrough import _checkpoint

GUIDING_BOLT_ID = "dnd5e.content.srd2014.spell.guiding-bolt"
HEALING_WORD_ID = "dnd5e.content.srd2014.spell.healing-word"
MAGIC_MISSILE_ID = "dnd5e.content.srd2014.spell.magic-missile"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--action", choices=("start", "status", "auto-run"), required=True)
    parser.add_argument("--run-id", default="full-playthrough-encounter-v1")
    parser.add_argument("--party-report", type=Path, required=True)
    parser.add_argument("--hostile-report", type=Path, action="append", default=[])
    parser.add_argument(
        "--required-hostile-weapon-id",
        action="append",
        default=[],
        help=(
            "Require every source hostile to expose this reviewed weapon id. "
            "Repeat for statblocks that must provide multiple attacks."
        ),
    )
    parser.add_argument("--scene-id")
    parser.add_argument("--location-key")
    parser.add_argument("--source-excerpt")
    parser.add_argument("--encounter-name", default="Source-defined encounter")
    parser.add_argument("--hostile-label", default="Source-defined hostiles")
    parser.add_argument("--surprise-check-report", type=Path)
    parser.add_argument(
        "--no-surprise",
        action="store_true",
        help="Explicitly start with neither side surprised when the cited scene warrants it",
    )
    parser.add_argument("--flee-after-defeated", type=int, default=0)
    parser.add_argument("--flee-actor-id", default="")
    parser.add_argument("--flee-trigger-defeated-actor-id", default="")
    parser.add_argument("--flee-on-start-actor-id", default="")
    parser.add_argument("--flee-destination-location-key", default="")
    parser.add_argument("--flee-source-excerpt", default="")
    parser.add_argument("--truce-after-defeated", type=int, default=0)
    parser.add_argument("--truce-actor-id", default="")
    parser.add_argument("--truce-source-excerpt", default="")
    parser.add_argument("--max-turns", type=int, default=200)
    parser.add_argument("--checkpoint-label", default="Encounter complete")
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


def _read_report(path: Path) -> dict[str, Any]:
    return json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))


def _party_ids(path: Path) -> list[str]:
    report = _read_report(path)
    values = [
        str(item.get("actor_id") or "")
        for item in report.get("characters", [])
        if isinstance(item, dict)
    ]
    if not values or any(not item for item in values) or len(values) != len(set(values)):
        raise ValueError("party report must contain unique character actor_id values")
    return values


def _hostile_ids(paths: list[Path]) -> list[str]:
    values = [
        str(
            dict(dict(_read_report(path).get("created") or {}).get("character") or {}).get(
                "id"
            )
            or ""
        )
        for path in paths
    ]
    if not values or any(not item for item in values) or len(values) != len(set(values)):
        raise ValueError("hostile reports must contain unique created.character.id values")
    return values


def _participant_manifest(
    hostile_ids: list[str],
    *,
    label: str,
    source_excerpt: str,
) -> dict[str, Any]:
    if not source_excerpt.strip():
        raise ValueError("encounter start requires an exact source excerpt")
    return {
        "schema_version": 1,
        "groups": [
            {
                "key": "source-hostiles",
                "label": label,
                "role": "combatant",
                "required_count": len(hostile_ids),
                "actor_ids": hostile_ids,
                "source_excerpt": source_excerpt,
            }
        ],
        "notes": "Exact source count; no party-size scaling was applied.",
    }


def _source_departure_patch(
    actor_id: str,
    *,
    reason: str,
    destination_location_key: str = "",
) -> dict[str, Any]:
    if not actor_id or not reason.strip():
        raise ValueError("source departure requires actor_id and reason")
    return {
        "key": "combatant_departure",
        "value": {
            "actor_id": actor_id,
            "reason": reason.strip(),
            "destination_location_key": destination_location_key.strip(),
        },
    }


def _participant_config(
    party_ids: list[str],
    hostile_ids: list[str],
    *,
    surprise_by_actor: dict[str, bool],
    hostiles_hidden: bool = True,
) -> list[dict[str, Any]]:
    if len(party_ids) > 10 or len(hostile_ids) > 10:
        raise ValueError("default encounter layout supports at most 10 PCs and 10 hostiles")
    configs = [
        {
            "actor_id": actor_id,
            "position": {"x": 1, "y": index + 1},
            "disposition": "friendly",
            "surprised": bool(surprise_by_actor.get(actor_id, False)),
            "death_saves": True,
        }
        for index, actor_id in enumerate(party_ids)
    ]
    hostile_positions = (
        (2, 2),
        (2, 4),
        (7, 2),
        (7, 4),
        (4, 2),
        (4, 4),
        (9, 2),
        (9, 4),
        (6, 6),
        (8, 6),
    )
    configs.extend(
        {
            "actor_id": actor_id,
            "position": {"x": hostile_positions[index][0], "y": hostile_positions[index][1]},
            "disposition": "hostile",
            "hidden": hostiles_hidden and not bool(surprise_by_actor.get(actor_id, False)),
            "surprised": bool(surprise_by_actor.get(actor_id, False)),
            "death_saves": False,
        }
        for index, actor_id in enumerate(hostile_ids)
    )
    return configs


def _facade_value(value: Any) -> Any:
    if isinstance(value, dict) and "result" in value:
        return value["result"]
    return value


def _roll_total(value: dict[str, Any]) -> int:
    if "total" in value:
        return int(value["total"])
    return int(dict(value.get("result") or {}).get("total", 0))


def _surprise_from_check_report(
    path: Path,
    *,
    campaign_id: str,
    scene_id: str,
    location_key: str,
    party_ids: list[str],
    hostile_ids: list[str],
) -> tuple[dict[str, bool], dict[str, Any]]:
    report = _read_report(path)
    result = dict(report.get("result") or {})
    scene = dict(result.get("scene") or {})
    actor = dict(result.get("actor") or {})
    check = dict(result.get("check") or {})
    if (
        report.get("passed") is not True
        or report.get("action") != "resolve-check"
        or report.get("campaign_id") != campaign_id
        or scene.get("scene_id") != scene_id
        or scene.get("location_key") != location_key
        or actor.get("id") not in party_ids
        or not isinstance(check.get("success"), bool)
    ):
        raise ValueError("surprise check report does not match this encounter")
    surprise = {actor_id: False for actor_id in party_ids}
    surprise.update({actor_id: bool(check["success"]) for actor_id in hostile_ids})
    return surprise, {
        "mode": "source_cited_party_scout",
        "report_path": str(path.expanduser().resolve()),
        "actor": actor,
        "check": check,
    }


async def _campaign(client: ExposureClient, campaign_id: str) -> dict[str, Any]:
    return _facade_value(
        await client.core(
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign_id},
                "principal_id": PRINCIPAL_ID,
            },
        )
    )


async def _current_branch(client: ExposureClient, campaign_id: str) -> dict[str, Any]:
    values = await client.domain(
        "branch_query",
        {"campaign_id": campaign_id, "view": "list"},
    )
    branch = next((item for item in values if item.get("is_current")), None)
    if branch is None:
        raise RuntimeError("campaign has no current branch")
    return branch


async def _character(
    client: ExposureClient,
    actor_id: str,
) -> dict[str, Any]:
    return await client.domain(
        "character_query",
        {"view": "get", "payload": {"character_id": actor_id}},
    )


def _character_summary(actor: dict[str, Any]) -> dict[str, Any]:
    derived = dict(actor.get("derived") or {})
    sheet = dict(actor.get("sheet") or {})
    return {
        "id": actor["id"],
        "name": actor["name"],
        "hp": dict(derived.get("hit_points") or {}),
        "conditions": list(sheet.get("conditions") or []),
        "weapons": [
            {
                "item_id": item.get("item_id"),
                "name": item.get("name"),
                "attack_type": item.get("attack_type"),
                "range_ft": item.get("range_ft"),
                "on_hit_effect": item.get("on_hit_effect"),
            }
            for item in dict(derived.get("inventory") or {}).get("weapon_attacks", [])
        ],
    }


def _validate_hostile_attacks(
    actor_id: str,
    attacks: list[dict[str, Any]],
    *,
    required_weapon_ids: list[str],
) -> None:
    attack_ids = {str(item.get("item_id") or "") for item in attacks}
    if not attack_ids - {""}:
        raise RuntimeError(f"source hostile {actor_id} has no executable weapon attack")
    missing = set(required_weapon_ids) - attack_ids
    if missing:
        raise RuntimeError(
            f"source hostile {actor_id} lacks required reviewed attacks: "
            f"{', '.join(sorted(missing))}"
        )
    if "shortbow" in required_weapon_ids:
        shortbow = next(item for item in attacks if item.get("item_id") == "shortbow")
        if dict(shortbow.get("range_ft") or {}) != {"normal": 80, "long": 320}:
            raise RuntimeError(f"source hostile {actor_id} has an invalid Shortbow range")
        if str(shortbow.get("on_hit_effect") or ""):
            raise RuntimeError(
                f"source hostile {actor_id} has unresolved trailing action prose"
            )


def _preferred_hostile_weapon_id(
    actor: dict[str, Any],
    *,
    hostile_index: int,
) -> str:
    weapons = list(
        dict(dict(actor.get("derived") or {}).get("inventory") or {}).get(
            "weapon_attacks", []
        )
    )
    attack_ids = {str(item.get("item_id") or "") for item in weapons}
    if hostile_index >= 2 and "shortbow" in attack_ids:
        return "shortbow"
    if "scimitar" in attack_ids:
        return "scimitar"
    melee = next(
        (
            str(item.get("item_id") or "")
            for item in weapons
            if item.get("attack_type") == "melee"
        ),
        "",
    )
    return melee or (str(weapons[0].get("item_id") or "") if weapons else "")


async def _start(
    client: ExposureClient,
    args: argparse.Namespace,
    party_ids: list[str],
    hostile_ids: list[str],
) -> dict[str, Any]:
    if not args.scene_id or not args.location_key:
        raise ValueError("encounter start requires --scene-id and --location-key")
    opened_play = await client.open(args.campaign_id)
    await client.load(
        "play.scene",
        "play.characters",
        "play.resolution",
        "play.combat_control",
    )
    campaign = await _campaign(client, args.campaign_id)
    phase = str(dict(campaign.get("state") or {}).get("game_phase") or "")
    if phase != "play":
        raise RuntimeError("encounter start requires the play phase")
    branch = await _current_branch(client, args.campaign_id)
    actors = {
        actor_id: await _character(client, actor_id)
        for actor_id in [*party_ids, *hostile_ids]
    }
    for actor_id in hostile_ids:
        attacks = list(
            dict(dict(actors[actor_id].get("derived") or {}).get("inventory") or {}).get(
                "weapon_attacks", []
            )
        )
        _validate_hostile_attacks(
            actor_id,
            attacks,
            required_weapon_ids=args.required_hostile_weapon_id,
        )
    passive_perception: dict[str, int] = {}
    if args.no_surprise and args.surprise_check_report is not None:
        raise ValueError("--no-surprise cannot be combined with --surprise-check-report")
    if args.no_surprise:
        surprise = {actor_id: False for actor_id in [*party_ids, *hostile_ids]}
        surprise_basis = {
            "mode": "source_scene_no_surprise",
            "source_excerpt": str(args.source_excerpt or ""),
        }
        expected_revision = campaign["revision"]
    elif args.surprise_check_report is not None:
        surprise, surprise_basis = _surprise_from_check_report(
            args.surprise_check_report,
            campaign_id=args.campaign_id,
            scene_id=args.scene_id,
            location_key=args.location_key,
            party_ids=party_ids,
            hostile_ids=hostile_ids,
        )
        expected_revision = campaign["revision"]
    else:
        rolled = await client.domain(
            "dnd_dice_roll",
            {
                "campaign_id": args.campaign_id,
                "expression": "1d20+6",
                "branch_id": branch["id"],
                "expected_campaign_revision": campaign["revision"],
                "idempotency_key": (
                    f"encounter-stealth-{_token(f'{args.run_id}:{args.scene_id}', length=24)}"
                ),
            },
        )
        stealth_total = _roll_total(rolled)
        passive_perception = {
            actor_id: int(
                dict(actors[actor_id].get("derived") or {}).get("passive_perception", 10)
            )
            for actor_id in party_ids
        }
        surprise = {
            actor_id: score < stealth_total
            for actor_id, score in passive_perception.items()
        }
        surprise.update({actor_id: False for actor_id in hostile_ids})
        surprise_basis = {"mode": "hostile_group_stealth", "roll": rolled}
        expected_revision = rolled["campaign_revision"]
    started = await client.domain(
        "combat_start",
        {
            "campaign_id": args.campaign_id,
            "participant_ids": [*party_ids, *hostile_ids],
            "participant_config": _participant_config(
                party_ids,
                hostile_ids,
                surprise_by_actor=surprise,
                hostiles_hidden=not args.no_surprise,
            ),
            "participant_manifest": _participant_manifest(
                hostile_ids,
                label=args.hostile_label,
                source_excerpt=str(args.source_excerpt or ""),
            ),
            "name": args.encounter_name,
            "scene_id": args.scene_id,
            "battle_map": {"location_key": args.location_key},
            "ruleset": "2014",
            "branch_id": branch["id"],
            "expected_revision": expected_revision,
            "idempotency_key": (
                f"encounter-start-{_token(f'{args.run_id}:{args.scene_id}', length=24)}"
            ),
        },
    )
    opened_combat = await client.open(args.campaign_id)
    await client.load(
        "combat.observe",
        "combat.actions",
        "combat.turn",
        "combat.control",
        "combat.save",
        "combat.map",
    )
    status = await client.domain(
        "combat_query",
        {"campaign_id": args.campaign_id, "view": "status"},
    )
    return {
        "play_exposure": opened_play,
        "surprise_basis": surprise_basis,
        "passive_perception": passive_perception,
        "surprise": surprise,
        "start": started,
        "combat_exposure": opened_combat,
        "combat": status,
        "actors": [_character_summary(actors[item]) for item in actors],
    }


def _hit_points(actor: dict[str, Any]) -> int:
    return int(
        dict(dict(actor.get("sheet") or {}).get("combat") or {})
        .get("hp", {})
        .get("value", 0)
        or 0
    )


def _conditions(actor: dict[str, Any]) -> set[str]:
    return {
        str(item).casefold()
        for item in dict(actor.get("sheet") or {}).get("conditions", [])
    }


def _choose_party_spell(
    actor_id: str,
    *,
    party_ids: list[str],
    actors: dict[str, dict[str, Any]],
    living_targets: list[str],
    leveled_spell_available: bool = True,
) -> tuple[str, str] | None:
    """Choose a supported level-1 spell with an explicit auditable target."""

    if actor_id not in party_ids or not leveled_spell_available:
        return None
    actor = actors[actor_id]
    spells = {
        str(item.get("id") or "")
        for item in dict(actor.get("sheet") or {}).get("content", {}).get("spells", [])
    }
    slot = (
        dict(dict(actor.get("sheet") or {}).get("spellcasting") or {})
        .get("spell_slots", {})
        .get("1", {})
    )
    if int(dict(slot).get("value", 0) or 0) <= 0:
        return None
    downed_allies = [
        ally_id
        for ally_id in party_ids
        if ally_id != actor_id
        and _hit_points(actors[ally_id]) == 0
        and "dead" not in _conditions(actors[ally_id])
    ]
    downed_allies.sort(key=lambda item: "stable" in _conditions(actors[item]))
    if HEALING_WORD_ID in spells and downed_allies:
        return HEALING_WORD_ID, downed_allies[0]
    if MAGIC_MISSILE_ID in spells and living_targets:
        return MAGIC_MISSILE_ID, living_targets[0]
    if GUIDING_BOLT_ID in spells and living_targets:
        return GUIDING_BOLT_ID, living_targets[0]
    return None


def _distance(left: dict[str, Any], right: dict[str, Any]) -> int:
    return max(abs(int(left["x"]) - int(right["x"])), abs(int(left["y"]) - int(right["y"])))


def _choose_destination(
    combat: dict[str, Any],
    actor_id: str,
    target_id: str,
) -> tuple[dict[str, int], int] | None:
    combatants = list(combat.get("combatants") or [])
    acting = next(item for item in combatants if item.get("actor_id") == actor_id)
    target = next(item for item in combatants if item.get("actor_id") == target_id)
    origin = dict(acting.get("position") or {})
    goal = dict(target.get("position") or {})
    if set(origin) != {"x", "y"} or set(goal) != {"x", "y"}:
        return None
    budget_cells = int(dict(acting.get("turn_budget") or {}).get("movement", 0) or 0) // 5
    occupied = {
        (
            int(dict(item.get("position") or {}).get("x", -1)),
            int(dict(item.get("position") or {}).get("y", -1)),
        )
        for item in combatants
        if item.get("actor_id") != actor_id and isinstance(item.get("position"), dict)
    }
    bounds = dict(dict(combat.get("battle_map") or {}).get("bounds") or {})
    candidates: list[tuple[int, int, int]] = []
    for x in range(int(goal["x"]) - 1, int(goal["x"]) + 2):
        for y in range(int(goal["y"]) - 1, int(goal["y"]) + 2):
            destination = {"x": x, "y": y}
            if (
                (x, y) in occupied
                or (x == int(goal["x"]) and y == int(goal["y"]))
                or not 0 <= x < int(bounds.get("width_cells", 0) or 0)
                or not 0 <= y < int(bounds.get("height_cells", 0) or 0)
            ):
                continue
            steps = _distance(origin, destination)
            if 0 < steps <= budget_cells:
                candidates.append((steps, x, y))
    if not candidates:
        return None
    steps, x, y = min(candidates)
    return {"x": x, "y": y}, steps * 5


def _current_actor_id(combat: dict[str, Any]) -> str:
    combatants = list(combat.get("combatants") or [])
    if not combatants:
        raise RuntimeError("combat has no participants")
    return str(combatants[int(combat.get("turn_index", 0)) % len(combatants)]["actor_id"])


def _source_outcome(
    *,
    defeated_hostiles: int,
    fled_hostiles: int = 0,
    hostile_count: int,
    flee_after_defeated: int,
    unresolved_party: bool,
    party_down: bool,
) -> tuple[str, str] | None:
    if unresolved_party:
        return None
    if hostile_count > 0 and defeated_hostiles + fled_hostiles >= hostile_count:
        if fled_hostiles:
            return (
                "victory",
                f"{defeated_hostiles} source-defined hostiles were defeated and "
                f"{fled_hostiles} followed a source instruction to flee.",
            )
        return (
            "victory",
            f"All {hostile_count} source-defined hostiles were defeated.",
        )
    if flee_after_defeated and defeated_hostiles >= flee_after_defeated:
        return (
            "victory",
            f"{defeated_hostiles} source-defined hostiles were defeated; "
            "the last surviving hostile followed the source instruction and fled.",
        )
    if party_down:
        return (
            "defeat",
            "The party was defeated; surviving hostiles stopped attacking as required "
            "by the source development and left resolved unconscious or dead characters.",
        )
    return None


def _source_truce_outcome(
    *,
    defeated_hostiles: int,
    truce_after_defeated: int,
    truce_actor_alive: bool,
    unresolved_party: bool,
) -> tuple[str, str] | None:
    if (
        truce_after_defeated > 0
        and defeated_hostiles >= truce_after_defeated
        and truce_actor_alive
        and not unresolved_party
    ):
        return (
            "truce",
            f"After {defeated_hostiles} source-defined hostiles were defeated, "
            "the source-designated leader invoked the hostage truce.",
        )
    return None


async def _resolve_pending(
    client: ExposureClient,
    args: argparse.Namespace,
    branch_id: str,
    combat: dict[str, Any],
) -> dict[str, Any] | None:
    pending = next(
        (
            item
            for item in combat.get("pending", [])
            if item.get("status", "pending") == "pending"
        ),
        None,
    )
    if pending is None:
        return None
    campaign = await _campaign(client, args.campaign_id)
    actor_id = str(pending.get("actor_id") or "")
    identity = f"{pending.get('id')}:{campaign['revision']}"
    if pending.get("kind") == "concentration":
        return await client.domain(
            "combat_concentration_check",
            {
                "campaign_id": args.campaign_id,
                "target_id": actor_id,
                "dc": int(pending["dc"]),
                "effect_ids": list(pending.get("effect_ids") or []),
                "branch_id": branch_id,
                "expected_revision": campaign["revision"],
                "idempotency_key": f"encounter-concentration-{_token(identity, length=24)}",
            },
        )
    action = (
        "resolve_defense"
        if pending.get("trigger") in {"attack_hit_defense", "magic_missile_targeted"}
        else "resolve"
    )
    return await client.domain(
        "combat_choice",
        {
            "campaign_id": args.campaign_id,
            "actor_id": actor_id,
            "action": action,
            "payload": {
                "choice_id": pending["id"],
                "selection": {"id": "decline"},
            },
            "branch_id": branch_id,
            "expected_revision": campaign["revision"],
            "idempotency_key": f"encounter-choice-{_token(identity, length=24)}",
        },
    )


async def _preflight_attack(
    client: ExposureClient,
    args: argparse.Namespace,
    actor: dict[str, Any],
    target_ids: list[str],
    *,
    preferred_weapon_id: str = "",
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    weapons = list(
        dict(dict(actor.get("derived") or {}).get("inventory") or {}).get(
            "weapon_attacks", []
        )
    )
    weapons.sort(key=lambda item: item.get("item_id") != preferred_weapon_id)
    for target_id in target_ids:
        for weapon in weapons or [{"item_id": "unarmed-strike", "attack_type": "melee"}]:
            action = {
                "weapon_id": weapon.get("item_id"),
                "attack_mode": weapon.get("attack_type") or "melee",
            }
            try:
                plan = await client.domain(
                    "combat_preflight_attack",
                    {
                        "campaign_id": args.campaign_id,
                        "actor_id": actor["id"],
                        "target_id": target_id,
                        "action": action,
                    },
                )
            except RuntimeError:
                continue
            return target_id, action, plan
    return None


async def _end_turn(
    client: ExposureClient,
    args: argparse.Namespace,
    branch_id: str,
    actor_id: str,
    sequence: int,
) -> dict[str, Any]:
    campaign = await _campaign(client, args.campaign_id)
    return await client.domain(
        "combat_end_turn",
        {
            "campaign_id": args.campaign_id,
            "actor_id": actor_id,
            "branch_id": branch_id,
            "expected_revision": campaign["revision"],
            "idempotency_key": (
                "encounter-end-turn-"
                + _token(
                    f"{args.run_id}:{sequence}:{campaign['revision']}",
                    length=24,
                )
            ),
        },
    )


async def _auto_run(
    client: ExposureClient,
    args: argparse.Namespace,
    party_ids: list[str],
    hostile_ids: list[str],
) -> dict[str, Any]:
    opened_combat = await client.open(args.campaign_id)
    await client.load(
        "combat.observe",
        "combat.actions",
        "combat.turn",
        "combat.control",
        "combat.save",
        "combat.map",
    )
    campaign = await _campaign(client, args.campaign_id)
    if str(dict(campaign.get("state") or {}).get("game_phase") or "") != "combat":
        raise RuntimeError("auto-run requires an active combat")
    branch = await _current_branch(client, args.campaign_id)
    source_flee_ids = {
        str(args.flee_actor_id or ""),
        str(args.flee_trigger_defeated_actor_id or ""),
        str(args.flee_on_start_actor_id or ""),
    } - {""}
    if bool(args.flee_actor_id) != bool(args.flee_trigger_defeated_actor_id):
        raise ValueError(
            "source-specific flee requires both --flee-actor-id and "
            "--flee-trigger-defeated-actor-id"
        )
    if source_flee_ids and (
        not source_flee_ids <= set(hostile_ids)
        or not str(args.flee_source_excerpt or "").strip()
    ):
        raise ValueError(
            "source-specific flee actors must be encounter hostiles and require "
            "--flee-source-excerpt"
        )
    if args.flee_actor_id and (
        args.flee_actor_id == args.flee_trigger_defeated_actor_id
        or args.flee_on_start_actor_id
    ):
        raise ValueError(
            "triggered and on-start source departures are mutually exclusive, and "
            "triggered actors must be distinct"
        )
    if bool(args.truce_after_defeated) != bool(args.truce_actor_id):
        raise ValueError(
            "source truce requires both --truce-after-defeated and --truce-actor-id"
        )
    if args.truce_after_defeated < 0:
        raise ValueError("--truce-after-defeated must not be negative")
    if args.truce_actor_id and (
        args.truce_actor_id not in hostile_ids
        or not str(args.truce_source_excerpt or "").strip()
    ):
        raise ValueError(
            "source truce actor must be an encounter hostile and require "
            "--truce-source-excerpt"
        )
    initial_combat = await client.domain(
        "combat_query",
        {"campaign_id": args.campaign_id, "view": "status"},
    )
    revealed_surprised = [
        str(item["actor_id"])
        for item in initial_combat.get("combatants", [])
        if item.get("actor_id") in hostile_ids
        and item.get("surprised")
        and item.get("hidden")
    ]
    visibility_patch = None
    if revealed_surprised:
        campaign = await _campaign(client, args.campaign_id)
        visibility_patch = await client.domain(
            "combat_map_patch",
            {
                "campaign_id": args.campaign_id,
                "patches": [
                    {
                        "key": "combatant_visibility",
                        "value": {
                            "actor_id": actor_id,
                            "hidden": False,
                            "reason": (
                                "The source-cited successful scout check surprised this "
                                "lookout, so the party located it before initiative."
                            ),
                        },
                    }
                    for actor_id in revealed_surprised
                ],
                "branch_id": branch["id"],
                "expected_revision": campaign["revision"],
                "idempotency_key": (
                    f"encounter-reveal-surprised-{_token(args.run_id, length=24)}"
                ),
            },
        )
    turns: list[dict[str, Any]] = []
    fled_hostile_ids: set[str] = set()
    if args.flee_on_start_actor_id:
        campaign = await _campaign(client, args.campaign_id)
        escaped = await client.domain(
            "combat_map_patch",
            {
                "campaign_id": args.campaign_id,
                "patches": [
                    _source_departure_patch(
                        args.flee_on_start_actor_id,
                        reason=str(args.flee_source_excerpt),
                        destination_location_key=args.flee_destination_location_key,
                    )
                ],
                "branch_id": branch["id"],
                "expected_revision": campaign["revision"],
                "idempotency_key": (
                    f"encounter-source-start-flee-"
                    f"{_token(f'{args.run_id}:{args.flee_on_start_actor_id}', length=24)}"
                ),
            },
        )
        fled_hostile_ids.add(args.flee_on_start_actor_id)
        turns.append(
            {
                "sequence": 0,
                "kind": "source_flee",
                "actor_id": args.flee_on_start_actor_id,
                "trigger": "combat_start",
                "source_excerpt": str(args.flee_source_excerpt).strip(),
                "destination_location_key": args.flee_destination_location_key,
                "map_patch": escaped,
            }
        )
    outcome_status = ""
    outcome_summary = ""
    for sequence in range(1, args.max_turns + 1):
        combat = await client.domain(
            "combat_query",
            {"campaign_id": args.campaign_id, "view": "status"},
        )
        actors = {
            actor_id: await _character(client, actor_id)
            for actor_id in [*party_ids, *hostile_ids]
        }
        defeated_hostiles = [
            actor_id
            for actor_id in hostile_ids
            if _hit_points(actors[actor_id]) <= 0 or "dead" in _conditions(actors[actor_id])
        ]
        unresolved_party = [
            actor_id
            for actor_id in party_ids
            if _hit_points(actors[actor_id]) == 0
            and not _conditions(actors[actor_id]) & {"dead", "stable"}
        ]
        party_down = all(_hit_points(actors[actor_id]) <= 0 for actor_id in party_ids)
        outcome = _source_truce_outcome(
            defeated_hostiles=len(defeated_hostiles),
            truce_after_defeated=args.truce_after_defeated,
            truce_actor_alive=bool(
                args.truce_actor_id
                and _hit_points(actors[args.truce_actor_id]) > 0
                and "dead" not in _conditions(actors[args.truce_actor_id])
            ),
            unresolved_party=bool(unresolved_party),
        )
        if outcome is None:
            outcome = _source_outcome(
                defeated_hostiles=len(defeated_hostiles),
                fled_hostiles=len(fled_hostile_ids),
                hostile_count=len(hostile_ids),
                flee_after_defeated=args.flee_after_defeated,
                unresolved_party=bool(unresolved_party),
                party_down=party_down,
            )
        if outcome is not None:
            outcome_status, outcome_summary = outcome
            break
        flee_triggered = bool(
            args.flee_after_defeated
            and len(defeated_hostiles) >= args.flee_after_defeated
        )
        pending_result = await _resolve_pending(
            client,
            args,
            str(branch["id"]),
            combat,
        )
        if pending_result is not None:
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "pending_resolution",
                    "result": pending_result,
                }
            )
            continue
        actor_id = _current_actor_id(combat)
        actor = actors[actor_id]
        actor_conditions = _conditions(actor)
        if (
            actor_id == args.flee_actor_id
            and args.flee_trigger_defeated_actor_id in defeated_hostiles
            and _hit_points(actor) > 0
            and actor_id not in fled_hostile_ids
        ):
            campaign = await _campaign(client, args.campaign_id)
            escaped = await client.domain(
                "combat_map_patch",
                {
                    "campaign_id": args.campaign_id,
                    "patches": [
                        {
                            **_source_departure_patch(
                                actor_id,
                                reason=str(args.flee_source_excerpt),
                                destination_location_key=(
                                    args.flee_destination_location_key
                                ),
                            ),
                        }
                    ],
                    "branch_id": branch["id"],
                    "expected_revision": campaign["revision"],
                    "idempotency_key": (
                        f"encounter-source-flee-"
                        f"{_token(f'{args.run_id}:{actor_id}', length=24)}"
                    ),
                },
            )
            fled_hostile_ids.add(actor_id)
            ended_turn = await _end_turn(
                client,
                args,
                str(branch["id"]),
                actor_id,
                sequence,
            )
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "source_flee",
                    "actor_id": actor_id,
                    "trigger_actor_id": args.flee_trigger_defeated_actor_id,
                    "source_excerpt": str(args.flee_source_excerpt).strip(),
                    "map_patch": escaped,
                    "end_turn": ended_turn,
                }
            )
            continue
        if _hit_points(actor) == 0 and actor_id in party_ids and not actor_conditions & {
            "dead",
            "stable",
        }:
            campaign = await _campaign(client, args.campaign_id)
            saved = await client.domain(
                "combat_check",
                {
                    "campaign_id": args.campaign_id,
                    "actor_id": actor_id,
                    "kind": "death_save",
                    "branch_id": branch["id"],
                    "expected_revision": campaign["revision"],
                    "idempotency_key": (
                        "encounter-death-save-"
                        + _token(
                            f"{args.run_id}:{sequence}:{campaign['revision']}",
                            length=24,
                        )
                    ),
                },
            )
            turns.append({"sequence": sequence, "kind": "death_save", "result": saved})
            await _end_turn(client, args, str(branch["id"]), actor_id, sequence)
            continue
        available = await client.domain(
            "combat_query",
            {
                "campaign_id": args.campaign_id,
                "view": "available_actions",
                "actor_id": actor_id,
            },
        )
        if (
            flee_triggered
            or actor_id in fled_hostile_ids
            or party_down
            or _hit_points(actor) <= 0
            or "attack" not in set(available.get("actions") or [])
        ):
            ended_turn = await _end_turn(
                client,
                args,
                str(branch["id"]),
                actor_id,
                sequence,
            )
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "end_turn",
                    "actor_id": actor_id,
                    "result": ended_turn,
                }
            )
            continue
        opponents = (
            [
                hostile_id
                for hostile_id in hostile_ids
                if hostile_id not in fled_hostile_ids
            ]
            if actor_id in party_ids
            else party_ids
        )
        living_targets = [
            target_id for target_id in opponents if _hit_points(actors[target_id]) > 0
        ]
        combatants = {str(item["actor_id"]): item for item in combat["combatants"]}
        living_targets.sort(
            key=lambda item: _distance(
                dict(combatants[actor_id].get("position") or {"x": 0, "y": 0}),
                dict(combatants[item].get("position") or {"x": 0, "y": 0}),
            )
        )
        spell_choice = _choose_party_spell(
            actor_id,
            party_ids=party_ids,
            actors=actors,
            living_targets=living_targets,
            leveled_spell_available=not bool(
                dict(combatants[actor_id].get("turn_flags") or {}).get("cast_declared")
            ),
        )
        if spell_choice is not None:
            spell_id, spell_target_id = spell_choice
            campaign = await _campaign(client, args.campaign_id)
            cast_arguments: dict[str, Any] = {
                "campaign_id": args.campaign_id,
                "actor_id": actor_id,
                "spell_id": spell_id,
                "cast_level": 1,
                "branch_id": branch["id"],
                "expected_revision": campaign["revision"],
                "idempotency_key": (
                    f"encounter-spell-"
                    f"{_token(f'{args.run_id}:{sequence}:{spell_id}', length=24)}"
                ),
            }
            if spell_id == MAGIC_MISSILE_ID:
                cast_arguments["target_allocations"] = [
                    {"target_id": spell_target_id, "darts": 3}
                ]
            elif spell_id == HEALING_WORD_ID:
                cast_arguments["declaration"] = {"target_id": spell_target_id}
            cast = await client.domain("combat_cast_spell", cast_arguments)
            spell_result: dict[str, Any] = {"cast": cast}
            pending_reaction = cast.get("status") == "pending_reaction"
            if spell_id == GUIDING_BOLT_ID:
                if cast.get("status") != "pending_resolution":
                    raise RuntimeError(
                        "Guiding Bolt did not open a source-bound spell attack resolution"
                    )
                campaign = await _campaign(client, args.campaign_id)
                settled = await client.domain(
                    "combat_resolve_attack",
                    {
                        "campaign_id": args.campaign_id,
                        "actor_id": actor_id,
                        "target_id": spell_target_id,
                        "action": {
                            "spell_resolution_id": str(cast["result"]["resolution_id"])
                        },
                        "branch_id": branch["id"],
                        "expected_revision": campaign["revision"],
                        "idempotency_key": (
                            f"encounter-guiding-bolt-"
                            f"{_token(f'{args.run_id}:{sequence}', length=24)}"
                        ),
                    },
                )
                spell_result["settlement"] = settled
                pending_reaction = settled.get("status") == "pending_reaction"
                if settled.get("status") not in {"committed", "pending_reaction"}:
                    raise RuntimeError(
                        "Guiding Bolt spell attack did not commit or open a reaction"
                    )
            elif cast.get("status") not in {"committed", "pending_reaction"}:
                raise RuntimeError(
                    f"{spell_id} did not commit through structured spell settlement"
                )
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "spell",
                    "actor_id": actor_id,
                    "spell_id": spell_id,
                    "target_id": spell_target_id,
                    "result": spell_result,
                }
            )
            if pending_reaction:
                continue
            await _end_turn(client, args, str(branch["id"]), actor_id, sequence)
            continue
        preferred_weapon_id = (
            _preferred_hostile_weapon_id(
                actor,
                hostile_index=hostile_ids.index(actor_id),
            )
            if actor_id in hostile_ids
            else ""
        )
        plan = await _preflight_attack(
            client,
            args,
            actor,
            living_targets,
            preferred_weapon_id=preferred_weapon_id,
        )
        if plan is None and living_targets:
            destination = _choose_destination(combat, actor_id, living_targets[0])
            if destination is not None:
                campaign = await _campaign(client, args.campaign_id)
                moved = await client.domain(
                    "combat_movement",
                    {
                        "campaign_id": args.campaign_id,
                        "actor_id": actor_id,
                        "action": "move",
                        "payload": {
                            "distance": destination[1],
                            "destination": destination[0],
                        },
                        "branch_id": branch["id"],
                        "expected_revision": campaign["revision"],
                        "idempotency_key": (
                            f"encounter-move-{_token(f'{args.run_id}:{sequence}', length=24)}"
                        ),
                    },
                )
                turns.append(
                    {
                        "sequence": sequence,
                        "kind": "move",
                        "actor_id": actor_id,
                        "result": moved,
                    }
                )
                plan = await _preflight_attack(
                    client,
                    args,
                    actor,
                    living_targets,
                    preferred_weapon_id=preferred_weapon_id,
                )
        if plan is not None:
            target_id, action, preflight = plan
            campaign = await _campaign(client, args.campaign_id)
            resolved = await client.domain(
                "combat_resolve_attack",
                {
                    "campaign_id": args.campaign_id,
                    "actor_id": actor_id,
                    "target_id": target_id,
                    "action": action,
                    "branch_id": branch["id"],
                    "expected_revision": campaign["revision"],
                    "idempotency_key": (
                        "encounter-attack-"
                        + _token(
                            f"{args.run_id}:{sequence}:{campaign['revision']}",
                            length=24,
                        )
                    ),
                },
            )
            turns.append(
                {
                    "sequence": sequence,
                    "kind": "attack",
                    "actor_id": actor_id,
                    "target_id": target_id,
                    "preflight": preflight,
                    "result": resolved,
                }
            )
        await _end_turn(client, args, str(branch["id"]), actor_id, sequence)
    else:
        raise RuntimeError(f"combat did not reach a source outcome in {args.max_turns} turns")
    campaign = await _campaign(client, args.campaign_id)
    ended = await client.domain(
        "combat_end",
        {
            "campaign_id": args.campaign_id,
            "outcome": {"status": outcome_status, "summary": outcome_summary},
            "branch_id": branch["id"],
            "expected_revision": campaign["revision"],
            "idempotency_key": (
                f"encounter-end-{_token(f'{args.run_id}:{outcome_status}', length=24)}"
            ),
        },
    )
    opened_play = await client.open(args.campaign_id)
    await client.load("play.scene", "play.scene_control", "play.characters")
    checkpoint = await _checkpoint(
        client,
        campaign_id=args.campaign_id,
        run_id=args.run_id,
        label=args.checkpoint_label,
    )
    final_actors = [
        _character_summary(await _character(client, actor_id))
        for actor_id in [*party_ids, *hostile_ids]
    ]
    return {
        "combat_exposure": opened_combat,
        "visibility_patch": visibility_patch,
        "turns": turns,
        "fled_hostile_ids": sorted(fled_hostile_ids),
        "truce": (
            {
                "actor_id": args.truce_actor_id,
                "after_defeated": args.truce_after_defeated,
                "source_excerpt": str(args.truce_source_excerpt or "").strip(),
            }
            if args.truce_actor_id
            else None
        ),
        "outcome": ended,
        "play_exposure": opened_play,
        "checkpoint": checkpoint,
        "actors": final_actors,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    party_ids = _party_ids(args.party_report)
    hostile_ids = _hostile_ids(args.hostile_report)
    report: dict[str, Any] = {
        "action": args.action,
        "transport": "stdio",
        "campaign_id": args.campaign_id,
        "run_id": args.run_id,
        "party_ids": party_ids,
        "hostile_ids": hostile_ids,
    }
    async with stdio_client(_server_parameters(args)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            client = ExposureClient(session)
            if args.action == "start":
                report["result"] = await _start(client, args, party_ids, hostile_ids)
            elif args.action == "auto-run":
                report["result"] = await _auto_run(client, args, party_ids, hostile_ids)
            else:
                opened = await client.open(args.campaign_id)
                await client.load("combat.observe")
                report["result"] = {
                    "exposure": opened,
                    "combat": await client.domain(
                        "combat_query",
                        {"campaign_id": args.campaign_id, "view": "status"},
                    ),
                    "actors": [
                        _character_summary(await _character(client, actor_id))
                        for actor_id in [*party_ids, *hostile_ids]
                    ],
                }
    report["passed"] = True
    return report


def _leaf_messages(error: BaseException) -> list[str]:
    nested = getattr(error, "exceptions", ())
    if nested:
        return [message for child in nested for message in _leaf_messages(child)]
    return [f"{type(error).__name__}: {error}"]


def main() -> int:
    args = _arguments()
    try:
        report = asyncio.run(_run(args))
    except Exception as error:
        report = {
            "action": args.action,
            "campaign_id": args.campaign_id,
            "run_id": args.run_id,
            "passed": False,
            "error": "; ".join(_leaf_messages(error)),
        }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
