"""Run the discovered D&D module corpus through the real SagaSmith Agent.

This is deliberately a thin process orchestrator.  It does not choose story
answers or call domain services: nanobot makes the decisions through the native
MCP facade, while this command preserves transcripts and checks the resulting
public-tool evidence against the dynamically generated corpus matrix.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

repo = Path(__file__).resolve().parents[1]
workspace = repo.parent

CORE_TOOLS = frozenset(
    {
        "skill_query",
        "campaign_query",
        "exposure",
        "game_phase",
        "server_capabilities",
        "storage_status",
    }
)
TOOL_PREFIX = "mcp_sagasmith_dnd_"
LIST_CHANGED_LOG = "refreshed tools after list_changed"
ERROR_PREFIXES = (
    "Error:",
    "Error executing tool ",
    "(MCP tool call failed:",
    "(no output)",
)
ERROR_SENTINELS = ("\n\n[Analyze the error above",)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--agent-config-template", type=Path, required=True)
    parser.add_argument(
        "--nanobot",
        type=Path,
        default=workspace / "SagaSmith-agent" / ".venv" / "Scripts" / "nanobot.exe",
    )
    parser.add_argument("--run-id", default="full-agent-corpus-v1")
    parser.add_argument("--campaign", action="append", default=[])
    parser.add_argument("--max-cycles", type=int, default=24)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--module-root", type=Path, action="append", default=[])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    return parser.parse_args()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected an object in {path}")
    return value


def _safe_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not normalized:
        raise ValueError("identifier must contain an ASCII letter or digit")
    return normalized


def _session_path(agent_workspace: Path, session_id: str) -> Path:
    key = base64.urlsafe_b64encode(session_id.encode()).decode().rstrip("=")
    return agent_workspace / "sessions" / f"{key}.jsonl"


def _normalize_tool_name(name: Any) -> str:
    value = str(name or "")
    return value[len(TOOL_PREFIX) :] if value.startswith(TOOL_PREFIX) else value


def _decode_tool_content(content: Any) -> Any:
    if not isinstance(content, str):
        return content
    text = content.strip()
    if not text or _is_tool_error(text):
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        try:
            return json.loads(value["text"])
        except json.JSONDecodeError:
            return value
    return value


def _is_tool_error(content: Any) -> bool:
    if not isinstance(content, str):
        return False
    return content.startswith(ERROR_PREFIXES) or any(
        sentinel in content for sentinel in ERROR_SENTINELS
    )


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _read_session(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"invalid session row {path}:{number}")
        rows.append(value)
    return rows


def _read_tool_audit(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in _read_session(path):
        process_id = str(record.get("process_id") or f"legacy:{path.resolve()}")
        assistant = record.get("assistant_message")
        if isinstance(assistant, dict):
            rows.append({**assistant, "_process_id": process_id})
        for result in record.get("tool_results") or []:
            if isinstance(result, dict):
                rows.append({**result, "_process_id": process_id})
    return rows


def _tool_timeline(rows: list[dict[str, Any]], *, principal: str) -> list[dict[str, Any]]:
    pending: dict[str, dict[str, Any]] = {}
    timeline: list[dict[str, Any]] = []
    for row in rows:
        if row.get("role") == "assistant":
            for call in row.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                call_id = str(call.get("id") or "")
                try:
                    arguments = json.loads(str(function.get("arguments") or "{}"))
                except json.JSONDecodeError:
                    arguments = {"_invalid_json": str(function.get("arguments") or "")}
                pending[call_id] = {
                    "principal": principal,
                    "process_id": row.get("_process_id"),
                    "tool_call_id": call_id,
                    "tool": _normalize_tool_name(function.get("name")),
                    "arguments": arguments,
                    "called_at": row.get("timestamp"),
                }
        if row.get("role") != "tool":
            continue
        call_id = str(row.get("tool_call_id") or "")
        entry = pending.pop(
            call_id,
            {
                "principal": principal,
                "process_id": row.get("_process_id"),
                "tool_call_id": call_id,
                "tool": _normalize_tool_name(row.get("name")),
                "arguments": {},
                "called_at": None,
            },
        )
        content = row.get("content")
        entry.update(
            {
                "completed_at": row.get("timestamp"),
                "ok": not _is_tool_error(content),
                "result": _decode_tool_content(content),
                "error": content if _is_tool_error(content) else None,
            }
        )
        timeline.append(entry)
    return timeline


def _player_ready(calls: list[dict[str, Any]], *, principal_id: str) -> bool:
    """Return true only after campaign membership and actor control both exist."""
    trusted_id = f"cli:{principal_id}"
    campaign_grant = any(
        call.get("ok")
        and call.get("tool") == "access_grant"
        and (call.get("arguments") or {}).get("scope") == "campaign"
        and (call.get("arguments") or {}).get("principal_id") == trusted_id
        and ((call.get("arguments") or {}).get("payload") or {}).get("role") == "player"
        for call in calls
    )
    actor_grant = any(
        call.get("ok")
        and call.get("tool") == "access_grant"
        and (call.get("arguments") or {}).get("scope") == "actor"
        and (call.get("arguments") or {}).get("principal_id") == trusted_id
        and bool(((call.get("arguments") or {}).get("payload") or {}).get("actor_id"))
        for call in calls
    )
    return campaign_grant and actor_grant


def _has_player_access_pair(calls: list[dict[str, Any]]) -> bool:
    campaign_principals = {
        (call.get("arguments") or {}).get("principal_id")
        for call in calls
        if call.get("ok")
        and call.get("tool") == "access_grant"
        and (call.get("arguments") or {}).get("scope") == "campaign"
        and ((call.get("arguments") or {}).get("payload") or {}).get("role") == "player"
    }
    actor_principals = {
        (call.get("arguments") or {}).get("principal_id")
        for call in calls
        if call.get("ok")
        and call.get("tool") == "access_grant"
        and (call.get("arguments") or {}).get("scope") == "actor"
        and bool(((call.get("arguments") or {}).get("payload") or {}).get("actor_id"))
    }
    return bool(campaign_principals & actor_principals)


def _phase_exposure_timeline(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    for entry in tools:
        result = entry.get("result")
        if result is None:
            continue
        phase = None
        loaded_tools = None
        binding = None
        for node in _walk(result):
            if not isinstance(node, dict):
                continue
            phase = phase or node.get("effective_game_phase") or node.get("game_phase")
            if phase is None and node.get("phase") in {"lobby", "play", "combat"}:
                phase = node.get("phase")
            if loaded_tools is None and isinstance(node.get("loaded_tools"), list):
                loaded_tools = node["loaded_tools"]
            if binding is None and isinstance(node.get("host_context_binding"), dict):
                binding = node["host_context_binding"]
        if phase is None and loaded_tools is None and binding is None:
            continue
        timeline.append(
            {
                "tool_call_id": entry.get("tool_call_id"),
                "principal": entry.get("principal"),
                "tool": entry.get("tool"),
                "phase": phase,
                "loaded_tools": loaded_tools,
                "host_context_binding": binding,
            }
        )
    return timeline


def _random_receipts(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in tools:
        for node in _walk(entry.get("result")):
            if not isinstance(node, dict):
                continue
            if not {"position_before", "position_after", "operation"}.issubset(node):
                continue
            token = json.dumps(node, sort_keys=True, ensure_ascii=False)
            if token in seen:
                continue
            seen.add(token)
            receipts.append(deepcopy(node))
    return receipts


def _call_matches(
    calls: list[dict[str, Any]],
    tool: str,
    *,
    action: str | None = None,
    argument: tuple[str, Any] | None = None,
) -> bool:
    for call in calls:
        if call.get("tool") != tool or not call.get("ok"):
            continue
        args = call.get("arguments") or {}
        if action is not None and args.get("action") != action and args.get("view") != action:
            continue
        if argument is not None and args.get(argument[0]) != argument[1]:
            continue
        return True
    return False


def _has_idempotent_retry(calls: list[dict[str, Any]]) -> bool:
    seen: set[tuple[str, str, str]] = set()
    for call in calls:
        arguments = call.get("arguments") or {}
        key = str(arguments.get("idempotency_key") or "")
        if not key:
            continue
        identity = (
            str(call.get("tool") or ""),
            key,
            json.dumps(arguments, sort_keys=True, ensure_ascii=False),
        )
        if identity in seen:
            return True
        seen.add(identity)
    return False


def _has_revision_refresh(calls: list[dict[str, Any]]) -> bool:
    conflict_index = next(
        (
            index
            for index, call in enumerate(calls)
            if isinstance(call.get("error"), str)
            and "revision" in call["error"].lower()
            and ("conflict" in call["error"].lower() or "stale" in call["error"].lower())
        ),
        None,
    )
    if conflict_index is None:
        return False
    return any(
        call.get("tool") == "campaign_query"
        and (call.get("arguments") or {}).get("view") == "resume"
        and call.get("ok")
        for call in calls[conflict_index + 1 :]
    )


def _has_exposure_reopen_after_transition(calls: list[dict[str, Any]]) -> bool:
    opened: dict[str, bool] = {}
    transitioned: dict[str, bool] = {}
    for call in calls:
        process_id = str(call.get("process_id") or "legacy")
        arguments = call.get("arguments") or {}
        tool = call.get("tool")
        action = arguments.get("action")
        if tool == "exposure" and action == "open" and call.get("ok"):
            if opened.get(process_id, False) and transitioned.get(process_id, False):
                return True
            opened[process_id] = True
            continue
        if call.get("ok") and (
            tool in {"combat_start", "combat_end", "snapshot_restore"}
            or (tool == "game_phase" and action == "set")
            or (tool == "branch_change" and action == "checkout")
            or (tool == "state_revision" and action in {"undo", "redo"})
        ) and opened.get(process_id, False):
            transitioned[process_id] = True
    return False


def _ordered_success(
    calls: list[dict[str, Any]], requirements: list[tuple[str, str | None]]
) -> bool:
    cursor = 0
    for tool, action in requirements:
        found = False
        while cursor < len(calls):
            call = calls[cursor]
            cursor += 1
            args = call.get("arguments") or {}
            if call.get("tool") != tool or not call.get("ok"):
                continue
            if action is not None and args.get("action") != action and args.get("view") != action:
                continue
            found = True
            break
        if not found:
            return False
    return True


def _ordered_pattern(
    calls: list[dict[str, Any]],
    requirements: list[tuple[str, str | None, bool]],
) -> bool:
    cursor = 0
    for tool, action, expected_ok in requirements:
        found = False
        while cursor < len(calls):
            call = calls[cursor]
            cursor += 1
            args = call.get("arguments") or {}
            if call.get("tool") != tool or bool(call.get("ok")) is not expected_ok:
                continue
            if action is not None and args.get("action") != action and args.get("view") != action:
                continue
            found = True
            break
        if not found:
            return False
    return True


def _ending_completed(calls: list[dict[str, Any]]) -> bool:
    for call in calls:
        if call.get("tool") != "playthrough_manifest" or not call.get("ok"):
            continue
        args = call.get("arguments") or {}
        if args.get("action") not in {"verify_ending", "verify-ending"}:
            continue
        for node in _walk(call.get("result")):
            if not isinstance(node, dict):
                continue
            if node.get("status") in {"completed", "achieved"}:
                return True
            if node.get("achieved") is True and node.get("completed") is not False:
                return True
    return False


def _manifest_party_ready(calls: list[dict[str, Any]]) -> bool:
    for call in reversed(calls):
        if call.get("tool") != "playthrough_manifest" or not call.get("ok"):
            continue
        for node in _walk(call.get("result")):
            if not isinstance(node, dict) or not isinstance(node.get("manifest"), dict):
                continue
            manifest = node["manifest"]
            party = manifest.get("party") or {}
            selected_size = party.get("selected_size")
            members = party.get("members") or []
            active_count = sum(
                1
                for member in members
                if isinstance(member, dict) and member.get("status") == "active"
            )
            return (
                manifest.get("status") in {"ready", "in_progress", "completed"}
                and isinstance(selected_size, int)
                and active_count == selected_size
            )
    return False


def _campaign_profile_matches(
    calls: list[dict[str, Any]],
    *,
    expected_edition: str,
    expected_advancement_mode: str,
) -> bool:
    return any(
        call.get("tool") == "campaign_create"
        and call.get("ok")
        and str((call.get("arguments") or {}).get("edition") or "")
        == expected_edition
        and str((call.get("arguments") or {}).get("advancement_mode") or "")
        == expected_advancement_mode
        for call in calls
    )


def _manifest_party_ids(calls: list[dict[str, Any]]) -> set[str]:
    for call in reversed(calls):
        if call.get("tool") != "playthrough_manifest" or not call.get("ok"):
            continue
        for node in _walk(call.get("result")):
            if not isinstance(node, dict) or not isinstance(node.get("manifest"), dict):
                continue
            members = (node["manifest"].get("party") or {}).get("members") or []
            return {
                str(member.get("actor_id"))
                for member in members
                if isinstance(member, dict) and member.get("actor_id")
            }
    return set()


def _manifest_selected_size(calls: list[dict[str, Any]]) -> int | None:
    for call in reversed(calls):
        if call.get("tool") != "playthrough_manifest" or not call.get("ok"):
            continue
        for node in _walk(call.get("result")):
            if not isinstance(node, dict) or not isinstance(node.get("manifest"), dict):
                continue
            selected_size = dict(node["manifest"].get("party") or {}).get("selected_size")
            if isinstance(selected_size, int) and not isinstance(selected_size, bool):
                return selected_size
    return None


def _campaign_pc_ids(calls: list[dict[str, Any]]) -> set[str]:
    actor_ids: set[str] = set()
    for call in calls:
        if not call.get("ok"):
            continue
        for node in _walk(call.get("result")):
            if (
                isinstance(node, dict)
                and node.get("character_type") == "pc"
                and str(node.get("campaign_id") or "")
                and str(node.get("id") or "")
            ):
                actor_ids.add(str(node["id"]))
    return actor_ids


def _party_character_views(calls: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    party_ids = _manifest_party_ids(calls)
    latest: dict[str, dict[str, Any]] = {}
    for call in calls:
        if not call.get("ok"):
            continue
        for node in _walk(call.get("result")):
            if not isinstance(node, dict):
                continue
            actor_id = str(node.get("id") or "")
            if (
                actor_id in party_ids
                and node.get("character_type") == "pc"
                and isinstance(node.get("sheet"), dict)
            ):
                latest[actor_id] = node
    return latest


def _party_mechanical_readiness(calls: list[dict[str, Any]]) -> dict[str, list[str]]:
    party_ids = _manifest_party_ids(calls)
    views = _party_character_views(calls)
    gaps: dict[str, list[str]] = {}
    for actor_id in sorted(party_ids):
        character = views.get(actor_id)
        if character is None:
            gaps[actor_id] = ["authoritative_character_view_missing"]
            continue
        sheet = dict(character.get("sheet") or {})
        notes = dict(character.get("notes") or {})
        progression = dict(sheet.get("progression") or {})
        combat = dict(sheet.get("combat") or {})
        ability_generation = dict(sheet.get("ability_generation") or {})
        classes = [item for item in progression.get("classes") or [] if isinstance(item, dict)]
        selections = [
            item
            for item in dict(sheet.get("content") or {}).get("selections") or []
            if isinstance(item, dict)
        ]
        selection_kinds = {str(item.get("kind") or "") for item in selections}
        hit_dice = dict(combat.get("hit_dice") or {})
        profile = dict(notes.get("profile") or {})
        actor_gaps: list[str] = []
        if sheet.get("schema_version") != 2:
            actor_gaps.append("sheet_v2_missing")
        if str(ability_generation.get("method") or "") in {
            "",
            "unrecorded",
            "roll_4d6_drop_lowest_pending",
        }:
            actor_gaps.append("ability_generation_incomplete")
        level = progression.get("level")
        if not isinstance(level, int) or isinstance(level, bool) or level < 1:
            actor_gaps.append("level_missing")
        if not classes or any(
            not str(item.get("name") or "")
            or not isinstance(item.get("level"), int)
            or int(item.get("level") or 0) < 1
            or not isinstance(item.get("hit_die"), int)
            or int(item.get("hit_die") or 0) < 1
            for item in classes
        ):
            actor_gaps.append("class_progression_incomplete")
        if not str(progression.get("species") or ""):
            actor_gaps.append("species_missing")
        if not str(progression.get("background") or ""):
            actor_gaps.append("background_missing")
        for kind in ("class", "species", "background"):
            if kind not in selection_kinds:
                actor_gaps.append(f"{kind}_catalog_provenance_missing")
        hp = dict(combat.get("hp") or {})
        if int(hp.get("max", 0) or 0) < 1 or int(hp.get("value", 0) or 0) < 1:
            actor_gaps.append("hit_points_incomplete")
        if not any(
            isinstance(pool, dict)
            and int(pool.get("max", 0) or 0) >= 1
            and int(pool.get("value", 0) or 0) >= 1
            for pool in hit_dice.values()
        ):
            actor_gaps.append("hit_dice_incomplete")
        if not list(dict(sheet.get("inventory") or {}).get("items") or []):
            actor_gaps.append("starting_equipment_missing")
        for field in ("personality_traits", "ideals", "bonds", "flaws"):
            if not list(profile.get(field) or []):
                actor_gaps.append(f"background_{field}_missing")
        if actor_gaps:
            gaps[actor_id] = actor_gaps
    return gaps


def _source_combat_actor_ids(calls: list[dict[str, Any]]) -> set[str]:
    authoritative_modes = {
        "statblock",
        "reviewed_rule_statblock",
        "module_statblock",
        "content_actor",
    }
    actor_ids: set[str] = set()
    for call in calls:
        if (
            call.get("tool") != "character_create_from"
            or not call.get("ok")
            or (call.get("arguments") or {}).get("mode") not in authoritative_modes
        ):
            continue
        for node in _walk(call.get("result")):
            if not isinstance(node, dict) or not isinstance(node.get("character"), dict):
                continue
            character = node["character"]
            if character.get("character_type") in {"npc", "monster"} and character.get("id"):
                actor_ids.add(str(character["id"]))
    return actor_ids


def _source_combat_actor_identities(calls: list[dict[str, Any]]) -> dict[str, str]:
    identities: dict[str, str] = {}
    for call in calls:
        if (
            call.get("tool") != "character_create_from"
            or not call.get("ok")
            or (call.get("arguments") or {}).get("mode")
            not in {"statblock", "module_statblock"}
        ):
            continue
        for node in _walk(call.get("result")):
            if not isinstance(node, dict) or not isinstance(node.get("character"), dict):
                continue
            character = node["character"]
            statblock = dict(node.get("statblock") or {})
            if character.get("id") and statblock.get("source_identity"):
                identities[str(character["id"])] = str(statblock["source_identity"])
    return identities


def _scenario_source_opposition_covered(
    scenario: dict[str, Any], calls: list[dict[str, Any]]
) -> bool:
    expected_groups = list(scenario.get("initial_source_groups") or [])
    if not expected_groups:
        return bool(_source_combat_actor_ids(calls))
    source_actor_ids = _source_combat_actor_ids(calls)
    source_identities = _source_combat_actor_identities(calls)
    for call in calls:
        arguments = call.get("arguments") or {}
        if not call.get("ok") or call.get("tool") != "combat_start":
            continue
        if scenario.get("positioning_mode") in {"grid", "agent"} and arguments.get(
            "positioning_mode"
        ) != scenario.get("positioning_mode"):
            continue
        participants = {str(item) for item in arguments.get("participant_ids") or []}
        manifest = arguments.get("participant_manifest") or {}
        actual_groups = list(manifest.get("groups") or [])
        matched_indexes: set[int] = set()
        complete = True
        for expected in expected_groups:
            expected_excerpt = " ".join(
                str(expected.get("source_excerpt") or "").split()
            ).casefold()
            match = next(
                (
                    (index, actual)
                    for index, actual in enumerate(actual_groups)
                    if index not in matched_indexes
                    and actual.get("role") == expected.get("role")
                    and actual.get("required_count") == expected.get("required_count")
                    and " ".join(str(actual.get("source_excerpt") or "").split()).casefold()
                    == expected_excerpt
                ),
                None,
            )
            if match is None:
                complete = False
                break
            index, actual = match
            actor_ids = {str(item) for item in actual.get("actor_ids") or []}
            expected_identity = " ".join(
                str(expected.get("statblock_source_identity") or "").split()
            ).casefold()
            if (
                len(actor_ids) != expected.get("required_count")
                or not actor_ids <= source_actor_ids
                or not actor_ids <= participants
                or (
                    expected_identity
                    and any(
                        " ".join(source_identities.get(actor_id, "").split()).casefold()
                        != expected_identity
                        for actor_id in actor_ids
                    )
                )
            ):
                complete = False
                break
            matched_indexes.add(index)
        if complete:
            return True
    return False


def _source_combat_sequence(
    calls: list[dict[str, Any]], *, mode: str | None = None, require_render: bool = False
) -> bool:
    party_ids = _manifest_party_ids(calls)
    source_actor_ids = _source_combat_actor_ids(calls)
    if not party_ids or not source_actor_ids:
        return False
    for index, call in enumerate(calls):
        arguments = call.get("arguments") or {}
        participants = {str(item) for item in arguments.get("participant_ids") or []}
        if (
            not call.get("ok")
            or call.get("tool") != "combat_start"
            or (mode is not None and arguments.get("positioning_mode") != mode)
            or not (participants & party_ids)
            or not (participants & source_actor_ids)
        ):
            continue
        later = calls[index + 1 :]
        if require_render and not _call_matches(later, "combat_query", action="render"):
            continue
        if _call_matches(later, "combat_end"):
            return True
    return False


def _mechanism_covered(mechanism: str, calls: list[dict[str, Any]]) -> bool:
    if mechanism == "preparation":
        return _ordered_success(
            calls,
            [
                ("module_draft", "finalize"),
                ("content_pack", "import"),
                ("content_pack", "activate"),
            ],
        )
    if mechanism == "idempotent_retry":
        return _has_idempotent_retry(calls)
    if mechanism == "revision_conflict_refresh":
        return _has_revision_refresh(calls)
    if mechanism == "conversation_to_mechanic":
        return _ordered_success(
            calls,
            [
                ("npc_conversation", "close"),
                ("character_check", None),
                ("npc_conversation", "open"),
            ],
        )
    if mechanism == "combat":
        return _source_combat_sequence(calls)
    if mechanism == "combat_render":
        return _source_combat_sequence(calls, require_render=True)
    if mechanism == "conversation_to_combat":
        return _ordered_pattern(
            calls,
            [
                ("npc_conversation", "open", True),
                ("combat_start", None, False),
                ("npc_conversation", "close", True),
                ("combat_start", None, True),
            ],
        )
    if mechanism == "agent_semantic_spell_ruling":
        return _ordered_success(
            calls,
            [
                ("content_solution", "compile"),
                ("combat_cast_spell", None),
                ("combat_choice", "execute_plan"),
            ],
        )
    if mechanism == "chase_to_combat":
        return _ordered_pattern(
            calls,
            [
                ("chase", "start", True),
                ("combat_start", None, False),
                ("chase", "end", True),
                ("combat_start", None, True),
            ],
        )
    mappings: dict[str, tuple[tuple[str, str | None], ...]] = {
        "play_scene": (("module_query", "scene"),),
        "noncombat_check": (("character_check", None),),
        "npc_conversation": (("npc_conversation", None),),
        "resource_settlement": (("campaign_change", None), ("character_action", None)),
        "chase": (("chase", None),),
        "ending": (("playthrough_manifest", "verify_ending"),),
        "save_restore": (("snapshot_restore", None),),
        "phase_exposure_refresh": (("exposure", "search"), ("exposure", "set")),
    }
    required = mappings.get(mechanism)
    if required is None:
        return False
    return all(_call_matches(calls, tool, action=action) for tool, action in required)


def _coverage_audit(
    route: dict[str, Any],
    calls: list[dict[str, Any]],
    *,
    process_count: int,
    list_changed_count: int,
    expected_edition: str | None = None,
    expected_advancement_mode: str | None = None,
) -> dict[str, Any]:
    gaps: list[str] = []
    scenarios: list[dict[str, Any]] = []
    for scenario in route.get("scenarios") or []:
        mechanisms = list(scenario.get("mechanisms") or [])
        scenario_gaps = [item for item in mechanisms if not _mechanism_covered(item, calls)]
        mode = scenario.get("positioning_mode")
        if (
            "combat" in mechanisms
            and mode in {"grid", "agent"}
            and not _source_combat_sequence(calls, mode=mode)
        ):
            scenario_gaps.append(f"positioning_mode:{mode}")
        if "combat" in mechanisms and not _scenario_source_opposition_covered(
            scenario, calls
        ):
            scenario_gaps.append("source_opposition_missing")
        audience = scenario.get("audience")
        if audience == "player" and not any(call.get("principal") == "player" for call in calls):
            scenario_gaps.append("audience:player")
        if scenario.get("ending_status") == "legal_complete" and not _ending_completed(calls):
            scenario_gaps.append("legal_ending_not_verified")
        for operation in scenario.get("recovery_operations") or []:
            covered = {
                "process_restart": process_count >= 2,
                "snapshot_restore": _call_matches(calls, "snapshot_restore"),
                "branch_checkout": _call_matches(calls, "branch_change", action="checkout"),
                "undo_redo": _call_matches(calls, "state_revision", action="undo")
                and _call_matches(calls, "state_revision", action="redo"),
            }.get(operation, False)
            if not covered:
                scenario_gaps.append(f"recovery:{operation}")
        scenarios.append({"id": scenario.get("id"), "gaps": sorted(set(scenario_gaps))})
        gaps.extend(f"{scenario.get('id')}:{gap}" for gap in scenario_gaps)
    if not calls or calls[0].get("tool") not in CORE_TOOLS:
        gaps.append("cold_start:first_call_not_core")
    if not _call_matches(calls, "skill_query"):
        gaps.append("cold_start:skill_query_missing")
    if not _call_matches(calls, "exposure", action="open"):
        gaps.append("cold_start:exposure_open_missing")
    if (
        expected_edition
        and expected_advancement_mode
        and not _campaign_profile_matches(
            calls,
            expected_edition=expected_edition,
            expected_advancement_mode=expected_advancement_mode,
        )
    ):
        gaps.append("preparation:campaign_profile_unverified_or_mismatch")
    if not _has_player_access_pair(calls):
        gaps.append("preparation:player_membership_or_actor_grant_missing")
    if not _manifest_party_ready(calls):
        gaps.append("preparation:manifest_party_not_ready")
    selected_size = _manifest_selected_size(calls)
    campaign_pc_ids = _campaign_pc_ids(calls)
    if selected_size is not None and len(campaign_pc_ids) > selected_size:
        gaps.append("preparation:extra_campaign_pcs_created")
    party_mechanical_gaps = _party_mechanical_readiness(calls)
    if party_mechanical_gaps:
        gaps.append("preparation:party_mechanics_not_ready")
    if list_changed_count < 1:
        gaps.append("host:list_changed_not_observed")
    if _has_exposure_reopen_after_transition(calls):
        gaps.append("exposure:reopened_after_transition")
    return {
        "complete": not gaps,
        "gaps": sorted(set(gaps)),
        "scenarios": scenarios,
        "ending_completed": _ending_completed(calls),
        "party_mechanical_gaps": party_mechanical_gaps,
        "campaign_pc_ids": sorted(campaign_pc_ids),
    }


def _inventory(args: argparse.Namespace) -> dict[str, Any]:
    output = args.output_dir / "inventory-and-matrix.json"
    command = [
        sys.executable,
        "-m",
        "scripts.regression_corpus",
        "--workspace",
        str(workspace),
        "--output",
        str(output),
        "--fail-on-pending",
        "--fail-on-incomplete-coverage",
    ]
    subprocess.run(command, cwd=repo, check=True)
    return _read_json(output)


def _routes() -> dict[str, dict[str, Any]]:
    fixture = _read_json(repo / "fixtures" / "module_corpus_decisions.json")
    routes: dict[str, dict[str, Any]] = {}
    for route in fixture.get("coverage_routes") or []:
        line_id = str(route.get("campaign_line_id") or "")
        if not line_id or line_id in routes:
            raise ValueError(f"invalid duplicate coverage route {line_id!r}")
        routes[line_id] = route
    return routes


def _runnable_units(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("coverage_units", "runnable_units"):
        value = inventory.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    disposition = inventory.get("disposition") or {}
    value = disposition.get("runnable") if isinstance(disposition, dict) else None
    return [item for item in value or [] if isinstance(item, dict)]


def _unit_id(unit: dict[str, Any]) -> str:
    return str(unit.get("campaign_line_id") or unit.get("id") or unit.get("unit_id") or "")


def _configure_agent(
    args: argparse.Namespace,
    *,
    unit_dir: Path,
    home: Path,
    agent_workspace: Path,
) -> Path:
    config = _read_json(args.agent_config_template)
    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
    defaults["workspace"] = str(agent_workspace.resolve())
    defaults["dream"] = {"enabled": False, "interval_h": 2}
    skills = str((workspace / "SagaSmith-dnd-skills" / "full" / "skills").resolve())
    external = list(defaults.get("external_skills_dirs") or [])
    if skills not in external:
        external.append(skills)
    defaults["external_skills_dirs"] = external
    servers = config.setdefault("tools", {}).setdefault("mcp_servers", {})
    server = servers.get("sagasmith_dnd")
    if not isinstance(server, dict):
        raise ValueError("agent config template must define tools.mcp_servers.sagasmith_dnd")
    server["inject_principal"] = True
    server["enabled_tools"] = ["*"]
    server["expose_resources_and_prompts"] = False
    env = server.setdefault("env", {})
    env["PYTHONUTF8"] = "1"
    env["SAGASMITH_DND_MCP_HOME"] = str(home.resolve())
    env["SAGASMITH_DND_SKILLS_DIR"] = str((workspace / "SagaSmith-dnd-skills").resolve())
    roots = args.module_root or [
        workspace / "reference" / "DnD-Books" / "5e" / "Campaign",
        workspace / "reference" / "DnD-Books" / "5e" / "One Shots",
        workspace / "test_pdfs",
    ]
    env["SAGASMITH_DND_MCP_MODULE_IMPORT_ROOTS"] = os.pathsep.join(
        str(path.resolve()) for path in roots
    )
    path = unit_dir / "agent-config.json"
    _write_json(path, config)
    return path


def _evidence_summary(route: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": item.get("id"),
            "source_sha256": item.get("source_sha256"),
            "heading_path": item.get("heading_path"),
            "content_sha256": item.get("content_sha256"),
            "page_start": item.get("page_start"),
            "page_end": item.get("page_end"),
        }
        for item in route.get("evidence") or []
    ]


def _managed_source_summary(unit: dict[str, Any]) -> list[dict[str, str]]:
    paths = list(unit.get("module_paths") or [])
    checksums = list(unit.get("module_sha256") or [])
    return [
        {
            "source_path": str((workspace / path).resolve()),
            "source_sha256": str(checksums[index]) if index < len(checksums) else "",
        }
        for index, path in enumerate(paths)
    ]


def _execution_order_gaps(gaps: list[str]) -> list[str]:
    """Put mechanical prerequisites before outcomes and historical audit debt."""

    return sorted(
        gaps,
        key=lambda gap: (
            0
            if gap.startswith("preparation:")
            else 1
            if gap.endswith(":source_opposition_missing")
            else 3
            if gap.endswith(":ending") or gap.endswith(":legal_ending_not_verified")
            else 4
            if gap == "exposure:reopened_after_transition"
            else 2,
            gap,
        ),
    )


def _dm_prompt(
    *,
    run_id: str,
    line_id: str,
    unit: dict[str, Any],
    route: dict[str, Any],
    player_principal: str,
    cycle: int,
    gaps: list[str],
) -> str:
    return f"""You are the DM Agent for a real full-campaign regression.
Run id: {run_id}
Campaign line label (never a campaign UUID): {line_id}
Source-declared D&D edition: {unit.get("edition")}
Source-selected advancement mode: {unit.get("advancement_mode")}
Source-reviewed preparation profile (re-resolve its exact current Pack evidence):
{json.dumps(unit.get("play_requirements") or {}, ensure_ascii=False)}
Trusted player principal to grant one actor: cli:{player_principal}
Cycle: {cycle}

Use dnd.full and CAMPAIGN_REGRESSION. Start this process session from the six
core tools, open exposure, consume native list changes, and call only exposed
native tools. Resume authoritative state if the campaign already exists. Never
use shell, direct database access, an internal service, invented tool results,
or narration as a substitute for a committed result.
For the regression reference use `skill_query(kind="asset")` with identifier
`dnd:full/skills/dnd-dm/references/CAMPAIGN_REGRESSION.md`; search/outline it and
read the bounded section relevant to the first current gap. Do not treat that
asset id as a `kind="skill"` document id. A prefix-only asset read is not proof
that the relevant workflow was consulted: before declaring a gap blocked, search
the asset using that gap's mechanism and read the matching bounded section.

This runner gives each campaign line a fresh MCP home. On the first cycle,
`campaign_query(view="list")` normally returns no campaign. Do not pass the
campaign-line label as `campaign_id` and do not diagnose that absence as an
authorization failure. Open exposure without a campaign, search for the exact
`campaign_create` tool, set it, refresh the native list, call it directly with a
reproducible seed, explicit `edition={json.dumps(unit.get("edition"))}` and
`advancement_mode={json.dumps(unit.get("advancement_mode"))}`, and this line
label in its slug/name, then reopen exposure with
the returned real campaign UUID. On later cycles, locate that created campaign
through the authenticated list and resume using its UUID.

The following fixture is coverage evidence and route intent, not a story answer.
Retrieve and expand the exact managed source before deciding what happens:
managed_sources={json.dumps(_managed_source_summary(unit), ensure_ascii=False)}
evidence={json.dumps(_evidence_summary(route), ensure_ascii=False)}
scenarios={json.dumps(route.get("scenarios") or [], ensure_ascii=False)}
When a combat scenario includes `initial_source_groups`, treat each entry as a
source-backed audit expectation: re-read the cited source, preserve every listed
group and exact count in preflight, and instantiate all of its canonical actors
before `combat_start`. Do not reduce or omit a group to make preflight pass.

Treat the current evidence-gap list below as authoritative for what remains;
prior Agent narration is not proof of a blocker. Query current state first and
do not repeat a prerequisite that is no longer listed. In particular, when no
`preparation` gap remains, do not rebuild the existing party or re-import an
unchanged Pack. A `source_opposition_missing` gap does not by itself prove that
the active Pack needs a new review. In Lobby, first use exact `rule_search` with
only `campaign_id`, the exact printed identity as `query`, and optional `top_k`.
Do not send `filters` on that first lookup: the campaign binding already scopes
enabled rule sources. Later exact filters belong only inside the optional
`filters` object. If a filtered lookup returns no hits, retry the minimal shape
before any module draft operation. When an enabled canonical rule source
contains the exact printed card, use `character_create_from(mode="statblock")`
with its returned `source_id`, exact `payload.chunk_ids` (never
`exact_chunks`), and `source_statblock_name`; give
repeated instances distinct names and verify returned
`statblock.source_identity`. Only when the card exists exclusively in the module
and its active Pack lacks the review is new Pack data mechanically indispensable.
A missing structured ending likewise belongs in the Pack. For those Pack-only
gaps, start an explicit new draft/version from the same managed source, add only
the evidence-backed missing review/package decisions, finalize it, import the
new artifact, and activate only the module id returned by that import.
Never guess a review id, edit a finalized Pack in place, or re-import the old
artifact as a substitute for the new reviewed revision.
When the active Pack already has the required immutable content review, do not
author another revision. Return to Lobby, query that Pack with
`module_query(view="content")`, load `character_create_from`, instantiate every
required encounter actor with `mode="module_statblock"`, pass the exact printed
card name as `payload.source_identity`, give repeated instances distinct names,
and re-read each actor plus its returned `statblock.source_identity`
before returning to Play. Writing a review id or opposition name into
`module_set_progress` is only narrative progress metadata; it never creates or
preflights a mechanical combat participant.

Before creating any opposition for a resumed campaign, call
`character_query(view="list")` and reuse every existing actor whose returned
source identity matches the required card. A coverage gap named
`source_opposition_missing` means no qualifying completed `combat_start` is in
the audit yet; it does not mean the actors are absent. Only create the exact
shortfall, and use `character_query(view="get")` with a returned actor id rather
than unsupported name filters or an empty batch.

When a scenario requires `agent_semantic_spell_ruling`, inspect preflight's
`ruling_spell_ids` and the actor's hydrated spell cards. Select one exact
source-backed spell with an Agent-owned semantic resolution path, query and (if
missing) compile its generic persisted `content_solution`, then actually cast it
and settle the returned plan through `combat_choice(action="execute_plan")`.
Bind the decision to the active scene and exact actor/rule evidence; MCP must pay
the action, slot or innate use and own all rolls/state mutations. Do not replace
this obligation with a weapon attack, narration, a raw sheet edit, or a spell
whose parser-damaged name never produced a hydrated card.

Prepare/finalize/import/activate the current Pack through the public lifecycle;
before any module authoring write, read the current
`dnd:full/references/skill-groups/lobby/modules-import.md` asset and follow its
public request shapes exactly;
create or resume one reproducibly seeded campaign; create the source-sized legal
party; grant the named player principal both campaign membership with role
`player` and explicit control of one PC through separate public `access_grant`
calls; then progress the source-backed
route to one legal verified ending. Exercise the listed Play, NPC, chase,
combat, audience, and recovery obligations at genuine scene boundaries. Keep
NPC workers isolated and close/abort before mechanics or combat. Use both
spatial modes only where assigned by the matrix. Let MCP own dice and state.
Keep the campaign in Lobby until the current Pack is active, the party is ready,
and the player grant exists. If an earlier interrupted cycle entered Play before
those prerequisites, close any active Play workflow and return to Lobby before
continuing preparation. Do not chase later matrix gaps ahead of prerequisites.
When the current gaps include `preparation`, do not initialize the playthrough
manifest or enter Play: read the finalized draft artifact, complete a successful
`content_pack(import, kind="module")`, and activate only the new module id
returned by that import. A prior activation without a successful Pack import
does not satisfy preparation. Here `preparation` means a scenario gap ending in
`:preparation`; the separate
`preparation:player_membership_or_actor_grant_missing` gap requires only the
missing campaign/actor grants and never authorizes rebuilding the Pack or party.
`preparation:manifest_party_not_ready` requires only creating any source-sized
missing PCs, replacing the complete manifest with full member records, and
syncing it to `ready`; it also never authorizes rebuilding the Pack.
An empty manifest does not mean the campaign has no PCs. Before any build, call
`character_query(view="list")`, count the distinct campaign-bound PC instances,
and calculate the exact shortfall from source-confirmed `selected_size`. If that
shortfall is zero, make no build call and register the existing actors. Never
create a reserve/bench PC in this fresh regression campaign.
`preparation:party_mechanics_not_ready` requires completing the existing party,
not creating replacements. Read the exact
`dnd:full/skills/dnd-dm/references/CHAR_CREATION.md` asset, follow its bootstrap,
ability, exact catalog-application, metadata-profile, and final re-read sequence
for every manifest PC, then sync the refreshed member records. Do not use
`character_sheet_replace` as a parallel bootstrap path. Do not enter Play until
the coverage audit no longer reports this gap.
Before that work, read
`dnd:full/skills/dnd-dm/references/CAMPAIGN_REGRESSION.md` through
`skill_query(kind="asset", action="read", identifier=...)`. This gap is not
satisfied by `module_set_progress` state or by a
successful `sync` that still returns an empty member list. Do not stop after
either result: source-confirmed `selected_size` remains the recommended maximum,
so create any missing PCs, register every full member record with manifest
`replace`, and verify the subsequent `sync` response itself is `ready`.
After every exposure open, seeing only core tools is expected, not a blocker:
search and set the next required native tool. A cycle that only lists state or
opens exposure has made no progress. Unless a true external boundary is reached,
complete at least one successful authoritative mutation toward the first unmet
prerequisite before stopping the cycle.
Stop only for a real external boundary or when the current cycle has exhausted
its tool budget; in that case report the exact authoritative blocker and leave
state resumable.

Current evidence gaps from prior cycles, ordered by execution dependency rather
than alphabetically: {json.dumps(_execution_order_gaps(gaps), ensure_ascii=False)}
`exposure:reopened_after_transition` is immutable historical audit debt in a
resumed artifact. Do not repeat it, but finish the remaining mechanical route;
the runner will require a clean fresh campaign after the route is complete.
"""


def _player_prompt(*, run_id: str, line_id: str, cycle: int) -> str:
    return f"""You are the authenticated player Agent in D&D regression {run_id},
campaign line {line_id}, cycle {cycle}. Cold-start from the native core tools,
read dnd.full, open the campaign exposure, and use only player-visible native
tools. The campaign-line label is not a campaign UUID: locate the campaign you
were granted through authenticated `campaign_query(view="list")`, then bind its
returned UUID. Prove the projection contains no DM-only module, continuity, NPC-private,
or combat information and that DM-only tools cannot be loaded. If your granted
PC currently has a real unresolved choice, make your own legal player decision
from the player-safe evidence and commit it through the exposed facade. Do not
invent hidden facts and do not make choices for other principals. Otherwise
perform the read-only player audit and stop cleanly.
"""


@dataclass(frozen=True)
class AgentProcess:
    principal: str
    session_id: str
    cycle: int
    returncode: int
    failure_kind: str | None
    stdout_path: Path
    stderr_path: Path
    audit_path: Path


def _agent_failure_kind(stdout: str, stderr: str) -> str | None:
    stdout_text = stdout.lower()
    terminal_stderr = "\n".join(
        line
        for line in stderr.lower().splitlines()
        if "retrying" not in line and "codex api request failed" not in line
    )
    combined = f"{stdout_text}\n{terminal_stderr}"
    if (
        "server_is_overloaded" in combined
        or "our servers are currently overloaded" in combined
    ):
        return "provider_overloaded"
    if "error calling codex" in combined or "llm returned error" in combined:
        return "provider_error"
    return None


def _run_agent(
    args: argparse.Namespace,
    *,
    config: Path,
    agent_workspace: Path,
    unit_dir: Path,
    principal: str,
    session_id: str,
    cycle: int,
    prompt: str,
    audit_path: Path,
) -> AgentProcess:
    stem = f"cycle-{cycle:03d}-{principal}"
    stdout_path = unit_dir / "process" / f"{stem}.stdout.txt"
    stderr_path = unit_dir / "process" / f"{stem}.stderr.txt"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(args.nanobot.resolve()),
        "agent",
        "--config",
        str(config.resolve()),
        "--workspace",
        str(agent_workspace.resolve()),
        "--session",
        session_id,
        "--sender-id",
        principal,
        "--no-markdown",
        "--logs",
        "--message",
        prompt,
    ]
    try:
        process_env = dict(os.environ)
        process_env["NANOBOT_TOOL_AUDIT_PATH"] = str(audit_path.resolve())
        process_env["NANOBOT_TOOL_AUDIT_PROCESS_ID"] = (
            f"{args.run_id}:{principal}:cycle-{cycle:03d}"
        )
        completed = subprocess.run(
            command,
            cwd=workspace / "SagaSmith-agent",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=process_env,
            timeout=args.timeout_seconds,
            check=False,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
        failure_kind = _agent_failure_kind(stdout, stderr)
        if failure_kind is not None and returncode == 0:
            returncode = 75
    except subprocess.TimeoutExpired as error:
        returncode = 124
        stdout = error.stdout or ""
        stderr = (error.stderr or "") + f"\nTimed out after {args.timeout_seconds}s\n"
        failure_kind = "timeout"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return AgentProcess(
        principal=principal,
        session_id=session_id,
        cycle=cycle,
        returncode=returncode,
        failure_kind=failure_kind,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        audit_path=audit_path,
    )


def _process_artifacts(
    unit_dir: Path, current: list[AgentProcess] | None = None
) -> list[dict[str, Any]]:
    process_dir = unit_dir / "process"
    prior: dict[str, dict[str, Any]] = {}
    report_path = unit_dir / "campaign-report.json"
    if report_path.is_file():
        report = _read_json(report_path)
        prior = {
            str(item.get("stdout")): dict(item)
            for item in report.get("agent_processes") or []
            if item.get("stdout")
        }
    for item in current or []:
        prior[str(item.stdout_path.resolve())] = {
            "principal": item.principal,
            "session_id": item.session_id,
            "cycle": item.cycle,
            "returncode": item.returncode,
            "failure_kind": item.failure_kind,
            "stdout": str(item.stdout_path.resolve()),
            "stderr": str(item.stderr_path.resolve()),
            "tool_audit": str(item.audit_path.resolve()),
        }
    artifacts: list[dict[str, Any]] = []
    for stdout_path in sorted(process_dir.glob("cycle-*-*.stdout.txt")):
        stem = stdout_path.name.removesuffix(".stdout.txt")
        cycle_text, principal = stem.removeprefix("cycle-").split("-", 1)
        row = prior.get(str(stdout_path.resolve()), {})
        stderr_path = process_dir / f"{stem}.stderr.txt"
        failure_kind = row.get("failure_kind") or _agent_failure_kind(
            stdout_path.read_text(encoding="utf-8", errors="replace"),
            stderr_path.read_text(encoding="utf-8", errors="replace")
            if stderr_path.is_file()
            else "",
        )
        returncode = row.get("returncode")
        if failure_kind is not None and not returncode:
            returncode = 75 if failure_kind != "timeout" else 124
        artifacts.append(
            {
                "principal": row.get("principal", principal),
                "session_id": row.get("session_id"),
                "cycle": int(row.get("cycle", cycle_text)),
                "returncode": returncode,
                "failure_kind": failure_kind,
                "stdout": str(stdout_path.resolve()),
                "stderr": str(
                    stderr_path.resolve()
                ),
                "tool_audit": row.get("tool_audit"),
            }
        )
    return artifacts


def _next_cycle(unit_dir: Path) -> int:
    cycles = [int(item["cycle"]) for item in _process_artifacts(unit_dir)]
    for audit_name in ("dm-tool-audit.jsonl", "player-tool-audit.jsonl"):
        for row in _read_session(unit_dir / "artifacts" / audit_name):
            identity = str(row.get("process_id") or row.get("session_key") or "")
            match = re.search(r":cycle-(\d+)(?::|$)", identity)
            if match is not None:
                cycles.append(int(match.group(1)))
    return max(cycles, default=0) + 1


def _list_changed_count(unit_dir: Path) -> int:
    count = 0
    for process in _process_artifacts(unit_dir):
        for key in ("stdout", "stderr"):
            path = Path(process[key])
            if path.is_file():
                count += path.read_text(encoding="utf-8").count(LIST_CHANGED_LOG)
    return count


def _aggregate_transcripts(
    agent_workspace: Path,
    session_ids: list[str],
    target: Path,
) -> None:
    sources = [
        (session_id, _session_path(agent_workspace, session_id))
        for session_id in dict.fromkeys(session_ids)
    ]
    sources = [(session_id, path) for session_id, path in sources if path.is_file()]
    if not sources:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as stream:
        for session_id, source in sources:
            stream.write(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_type": "session_boundary",
                        "session_id": session_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            content = source.read_text(encoding="utf-8")
            stream.write(content)
            if content and not content.endswith("\n"):
                stream.write("\n")


def _run_unit(
    args: argparse.Namespace, unit: dict[str, Any], route: dict[str, Any]
) -> dict[str, Any]:
    line_id = _unit_id(unit)
    expected_edition = str(unit.get("edition") or "").strip()
    expected_advancement_mode = str(unit.get("advancement_mode") or "").strip()
    if not expected_edition:
        raise ValueError(f"runnable coverage unit {line_id!r} is missing source edition")
    if expected_advancement_mode not in {"milestone", "xp"}:
        raise ValueError(
            f"runnable coverage unit {line_id!r} is missing source advancement mode"
        )
    unit_dir = args.output_dir / "campaigns" / _safe_id(line_id)
    home = unit_dir / "mcp-home"
    agent_workspace = unit_dir / "agent-workspace"
    home.mkdir(parents=True, exist_ok=True)
    agent_workspace.mkdir(parents=True, exist_ok=True)
    config = _configure_agent(
        args,
        unit_dir=unit_dir,
        home=home,
        agent_workspace=agent_workspace,
    )
    dm_principal = f"regression-dm-{_safe_id(line_id)}"
    player_principal = f"regression-player-{_safe_id(line_id)}"
    dm_session_prefix = f"{args.run_id}:{line_id}:dm"
    player_session_prefix = f"{args.run_id}:{line_id}:player"
    dm_audit = unit_dir / "artifacts" / "dm-tool-audit.jsonl"
    player_audit = unit_dir / "artifacts" / "player-tool-audit.jsonl"
    processes: list[AgentProcess] = []
    audit: dict[str, Any] = {"complete": False, "gaps": ["not_started"]}
    prior_calls = _tool_timeline(
        _read_tool_audit(dm_audit), principal="dm"
    ) + _tool_timeline(_read_tool_audit(player_audit), principal="player")
    if prior_calls:
        audit = _coverage_audit(
            route,
            prior_calls,
            process_count=len(_process_artifacts(unit_dir)),
            list_changed_count=_list_changed_count(unit_dir),
            expected_edition=expected_edition,
            expected_advancement_mode=expected_advancement_mode,
        )
    start_cycle = _next_cycle(unit_dir)

    for cycle in range(start_cycle, start_cycle + args.max_cycles):
        dm_session = f"{dm_session_prefix}:cycle-{cycle:03d}"
        dm = _run_agent(
            args,
            config=config,
            agent_workspace=agent_workspace,
            unit_dir=unit_dir,
            principal=dm_principal,
            session_id=dm_session,
            cycle=cycle,
            prompt=_dm_prompt(
                run_id=args.run_id,
                line_id=line_id,
                unit=unit,
                route=route,
                player_principal=player_principal,
                cycle=cycle,
                gaps=list(audit.get("gaps") or []),
            ),
            audit_path=dm_audit,
        )
        processes.append(dm)
        if dm.returncode and args.fail_fast:
            break
        dm_rows = _read_tool_audit(dm_audit)
        dm_calls = _tool_timeline(dm_rows, principal="dm")
        if not _player_ready(dm_calls, principal_id=player_principal):
            audit = _coverage_audit(
                route,
                dm_calls,
                process_count=len(_process_artifacts(unit_dir)),
                list_changed_count=_list_changed_count(unit_dir),
                expected_edition=expected_edition,
                expected_advancement_mode=expected_advancement_mode,
            )
            continue
        player = _run_agent(
            args,
            config=config,
            agent_workspace=agent_workspace,
            unit_dir=unit_dir,
            principal=player_principal,
            session_id=f"{player_session_prefix}:cycle-{cycle:03d}",
            cycle=cycle,
            prompt=_player_prompt(run_id=args.run_id, line_id=line_id, cycle=cycle),
            audit_path=player_audit,
        )
        processes.append(player)

        dm_rows = _read_tool_audit(dm_audit)
        player_rows = _read_tool_audit(player_audit)
        calls = _tool_timeline(dm_rows, principal="dm") + _tool_timeline(
            player_rows, principal="player"
        )
        audit = _coverage_audit(
            route,
            calls,
            process_count=len(_process_artifacts(unit_dir)),
            list_changed_count=_list_changed_count(unit_dir),
            expected_edition=expected_edition,
            expected_advancement_mode=expected_advancement_mode,
        )
        if audit["complete"]:
            break
        if player.returncode and args.fail_fast:
            break

    dm_rows = _read_tool_audit(dm_audit)
    player_rows = _read_tool_audit(player_audit)
    calls = _tool_timeline(dm_rows, principal="dm") + _tool_timeline(
        player_rows, principal="player"
    )
    process_artifacts = _process_artifacts(unit_dir, processes)
    dm_session_ids = [
        str(item["session_id"])
        for item in process_artifacts
        if item.get("principal") == dm_principal and item.get("session_id")
    ]
    player_session_ids = [
        str(item["session_id"])
        for item in process_artifacts
        if item.get("principal") == player_principal and item.get("session_id")
    ]
    _aggregate_transcripts(
        agent_workspace,
        dm_session_ids,
        unit_dir / "artifacts" / "dm-transcript.jsonl",
    )
    _aggregate_transcripts(
        agent_workspace,
        player_session_ids,
        unit_dir / "artifacts" / "player-transcript.jsonl",
    )
    list_changed_count = _list_changed_count(unit_dir)
    audit = _coverage_audit(
        route,
        calls,
        process_count=len(process_artifacts),
        list_changed_count=list_changed_count,
        expected_edition=expected_edition,
        expected_advancement_mode=expected_advancement_mode,
    )
    report = {
        "schema_version": 1,
        "campaign_line_id": line_id,
        "discovered_unit": unit,
        "route": route,
        "agent_processes": process_artifacts,
        "tool_timeline": calls,
        "phase_exposure_timeline": _phase_exposure_timeline(calls),
        "tools_list_changed_observed": list_changed_count,
        "random_receipts": _random_receipts(calls),
        "coverage": audit,
        "transcripts": {
            "dm": str((unit_dir / "artifacts" / "dm-transcript.jsonl").resolve()),
            "player": str((unit_dir / "artifacts" / "player-transcript.jsonl").resolve()),
            "dm_tool_audit": str(dm_audit.resolve()),
            "player_tool_audit": str(player_audit.resolve()),
            "dm_sessions": dm_session_ids,
            "player_sessions": player_session_ids,
        },
    }
    _write_json(unit_dir / "campaign-report.json", report)
    return report


def _run(args: argparse.Namespace) -> int:
    if args.max_cycles < 1:
        raise ValueError("--max-cycles must be positive")
    if args.output_dir.exists() and any(args.output_dir.iterdir()) and not args.resume:
        raise ValueError("--output-dir already contains artifacts; use --resume or a fresh path")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    inventory = _inventory(args)
    units = _runnable_units(inventory)
    if not units:
        raise RuntimeError("dynamic corpus inventory returned no runnable units")
    routes = _routes()
    discovered = {_unit_id(unit) for unit in units}
    missing_routes = sorted(item for item in discovered if item not in routes)
    if missing_routes:
        raise RuntimeError(f"runnable units lack source-backed routes: {missing_routes}")
    selected = set(args.campaign or discovered)
    unknown = sorted(selected - discovered)
    if unknown:
        raise ValueError(f"selected campaign is not dynamically runnable: {unknown}")
    if args.inventory_only:
        return 0
    reports: list[dict[str, Any]] = []
    for unit in units:
        line_id = _unit_id(unit)
        if line_id not in selected:
            continue
        report_path = args.output_dir / "campaigns" / _safe_id(line_id) / "campaign-report.json"
        if args.resume and report_path.is_file():
            existing = _read_json(report_path)
            if dict(existing.get("coverage") or {}).get("complete") is True:
                reports.append(existing)
                continue
        report = _run_unit(args, unit, routes[line_id])
        reports.append(report)
        if args.fail_fast and not dict(report.get("coverage") or {}).get("complete"):
            break
    complete = len(reports) == len(selected) and all(
        dict(item.get("coverage") or {}).get("complete") is True for item in reports
    )
    summary = {
        "schema_version": 1,
        "run_id": args.run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "inventory": str((args.output_dir / "inventory-and-matrix.json").resolve()),
        "selected_campaigns": sorted(selected),
        "complete": complete,
        "campaigns": [
            {
                "campaign_line_id": item.get("campaign_line_id"),
                "complete": dict(item.get("coverage") or {}).get("complete") is True,
                "gaps": dict(item.get("coverage") or {}).get("gaps") or [],
            }
            for item in reports
        ],
    }
    _write_json(args.output_dir / "summary.json", summary)
    return 0 if complete else 1


def main() -> None:
    raise SystemExit(_run(_arguments()))


if __name__ == "__main__":
    main()
