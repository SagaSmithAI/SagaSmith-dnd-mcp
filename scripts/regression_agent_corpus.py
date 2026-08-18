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
        "resolution_presentation",
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


def _decision_timing(records: list[dict[str, Any]], *, principal: str) -> dict[str, Any]:
    """Summarize observable turn boundaries without inventing hidden-thought timing."""

    by_process: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        recorded_at = record.get("recorded_at_unix")
        assistant = record.get("assistant_message")
        if (
            not isinstance(recorded_at, (int, float))
            or isinstance(recorded_at, bool)
            or not isinstance(assistant, dict)
        ):
            continue
        process_id = str(record.get("process_id") or "legacy")
        tool_names = [
            _normalize_tool_name(dict(call.get("function") or {}).get("name"))
            for call in assistant.get("tool_calls") or []
            if isinstance(call, dict)
        ]
        by_process.setdefault(process_id, []).append(
            {"recorded_at_unix": float(recorded_at), "tools": tool_names}
        )

    processes: list[dict[str, Any]] = []
    for process_id, turns in sorted(by_process.items()):
        turns.sort(key=lambda item: item["recorded_at_unix"])
        gaps = [
            round(turns[index]["recorded_at_unix"] - turns[index - 1]["recorded_at_unix"], 3)
            for index in range(1, len(turns))
        ]
        long_gaps = [
            {
                "seconds": gap,
                "after_tools": turns[index - 1]["tools"],
                "before_tools": turns[index]["tools"],
            }
            for index, gap in enumerate(gaps, start=1)
            if gap >= 30
        ]
        processes.append(
            {
                "principal": principal,
                "process_id": process_id,
                "decision_turns": len(turns),
                "observable_span_seconds": (
                    round(turns[-1]["recorded_at_unix"] - turns[0]["recorded_at_unix"], 3)
                    if len(turns) > 1
                    else 0.0
                ),
                "maximum_inter_turn_gap_seconds": max(gaps, default=0.0),
                "inter_turn_gaps_at_least_30_seconds": long_gaps,
                "attribution": (
                    "Inter-turn gaps combine model generation, provider queueing, host "
                    "work, and any tool execution not separately timestamped; they are "
                    "not hidden chain-of-thought timing."
                ),
            }
        )
    return {"processes": processes}


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
        if tool == "exposure" and action == "open":
            if opened.get(process_id, False) and transitioned.get(process_id, False):
                return True
            if call.get("ok"):
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


def _has_agent_semantic_spell_ruling(calls: list[dict[str, Any]]) -> bool:
    if _ordered_success(
        calls,
        [
            ("content_solution", "compile"),
            ("combat_cast_spell", None),
            ("combat_choice", "execute_plan"),
        ],
    ):
        return True
    for call in calls:
        if call.get("tool") != "combat_cast_spell" or not call.get("ok"):
            continue
        declaration = dict((call.get("arguments") or {}).get("declaration") or {})
        ruling = declaration.get("agent_ruling")
        if not isinstance(ruling, dict):
            continue
        if (
            ruling.get("default_resolver") != "agent"
            or ruling.get("ruling_kind") != "generic_spell_effect"
            or not str(ruling.get("source_excerpt") or "").strip()
        ):
            continue
        nodes = [node for node in _walk(call.get("result")) if isinstance(node, dict)]
        if any(
            node.get("status") == "agent_ruling_committed"
            and node.get("payment_recorded") is True
            for node in nodes
        ):
            return True
    return False


def _combat_start_probe_payload(arguments: dict[str, Any]) -> str:
    """Return the mechanically stable portion of a combat-start retry."""

    payload = deepcopy(arguments)
    payload.pop("expected_revision", None)
    payload.pop("idempotency_key", None)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _latest_combat_start_business_template(
    calls: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the latest successful public combat-start payload without controls."""

    for call in reversed(calls):
        if call.get("tool") != "combat_start" or not call.get("ok"):
            continue
        payload = deepcopy(call.get("arguments") or {})
        payload.pop("expected_revision", None)
        payload.pop("idempotency_key", None)
        return payload
    return None


def _conversation_to_combat_covered(calls: list[dict[str, Any]]) -> bool:
    """Require the exact rejected open-conversation probe to succeed after closure."""

    for open_index, opened in enumerate(calls):
        opened_args = opened.get("arguments") or {}
        if (
            opened.get("tool") != "npc_conversation"
            or not opened.get("ok")
            or opened_args.get("action") != "open"
        ):
            continue
        for failed_index in range(open_index + 1, len(calls)):
            failed = calls[failed_index]
            error = str(failed.get("error") or "")
            if failed.get("tool") != "combat_start" or failed.get("ok"):
                continue
            if "active npc conversation" not in error.casefold():
                continue
            failed_payload = _combat_start_probe_payload(failed.get("arguments") or {})
            closed = False
            for retry in calls[failed_index + 1 :]:
                retry_args = retry.get("arguments") or {}
                if (
                    retry.get("tool") == "npc_conversation"
                    and retry.get("ok")
                    and retry_args.get("action") in {"close", "abort"}
                ):
                    closed = True
                    continue
                if (
                    closed
                    and retry.get("tool") == "combat_start"
                    and retry.get("ok")
                    and _combat_start_probe_payload(retry_args) == failed_payload
                ):
                    return True
    return False


def _persistent_npc_conversation_covered(calls: list[dict[str, Any]]) -> bool:
    """Require one completed public conversation, not a successful read action."""

    required_actions = ("open", "ingest", "publish", "close")
    cursor = 0
    for call in calls:
        if call.get("tool") != "npc_conversation" or not call.get("ok"):
            continue
        action = (call.get("arguments") or {}).get("action")
        if action != required_actions[cursor]:
            if action == "open":
                cursor = 1
            continue
        cursor += 1
        if cursor == len(required_actions):
            return True
    return False


def _chase_terminal_receipt(call: dict[str, Any]) -> bool:
    """Return whether a successful chase mutation authoritatively ended the chase."""

    if call.get("tool") != "chase" or not call.get("ok"):
        return False
    action = (call.get("arguments") or {}).get("action")
    if action == "end":
        return True
    if action != "take_turn":
        return False
    for node in _walk(call.get("result")):
        if not isinstance(node, dict) or node.get("active") is not False:
            continue
        is_chase = str(node.get("id") or "").startswith("chase-") or {
            "quarry_ids",
            "pursuer_ids",
        }.issubset(node)
        outcome = node.get("outcome")
        if is_chase and isinstance(outcome, dict) and str(outcome.get("status") or ""):
            return True
    return False


def _chase_sequence_covered(
    calls: list[dict[str, Any]], *, require_combat_start: bool = False
) -> bool:
    """Require start -> authoritative terminal receipt [-> Combat] in one sequence."""

    started = False
    terminated = False
    for call in calls:
        arguments = call.get("arguments") or {}
        if (
            call.get("tool") == "chase"
            and call.get("ok")
            and arguments.get("action") == "start"
        ):
            started = True
            terminated = False
            continue
        if started and _chase_terminal_receipt(call):
            terminated = True
            if not require_combat_start:
                return True
            continue
        if terminated and call.get("tool") == "combat_start" and call.get("ok"):
            return True
    return False


def _successful_check_receipt(
    call: dict[str, Any],
    prerequisite: dict[str, Any],
    matched_receipts: list[tuple[dict[str, Any], dict[str, Any]]],
) -> bool:
    if call.get("tool") != "character_check" or not call.get("ok"):
        return False
    arguments = call.get("arguments") or {}
    payload = arguments.get("payload") or {}
    expected_skill = str(prerequisite.get("skill") or "")
    if expected_skill and str(payload.get("skill") or "").casefold() != expected_skill.casefold():
        return False
    expected_dc = prerequisite.get("dc")
    if "base_dc" in prerequisite:
        reducer_ids = [
            str(value) for value in prerequisite.get("applied_reducer_ids") or []
        ]
        reducers = {
            str(item.get("id")): (item, receipt_call)
            for item, receipt_call in matched_receipts
            if item.get("receipt") == "semantic_event" and item.get("dc_reduction") is not None
        }
        if set(reducer_ids) != set(reducers) or len(reducer_ids) != len(reducers):
            return False
        expected_dc = int(prerequisite["base_dc"]) - sum(
            int(reducers[reducer_id][0]["dc_reduction"]) for reducer_id in reducer_ids
        )
        if "dc" in prerequisite and prerequisite["dc"] != expected_dc:
            return False
        source_scene_id = str(payload.get("source_scene_id") or "")
        for reducer_id in reducer_ids:
            reducer_call = reducers[reducer_id][1]
            reducer_source_ref = (
                ((reducer_call.get("arguments") or {}).get("payload") or {})
                .get("event", {})
                .get("payload", {})
                .get("source_ref")
            )
            if isinstance(reducer_source_ref, str):
                try:
                    reducer_source_ref = json.loads(reducer_source_ref)
                except json.JSONDecodeError:
                    return False
            if str((reducer_source_ref or {}).get("scene_id") or "") != source_scene_id:
                return False
    if expected_dc is not None and payload.get("dc") != expected_dc:
        return False
    expected_success = prerequisite.get("success")
    committed = any(
        isinstance(node, dict)
        and node.get("status") == "committed"
        and (
            expected_success is None
            or any(
                isinstance(result, dict) and result.get("success") is expected_success
                for result in _walk(node.get("result"))
            )
        )
        for node in _walk(call.get("result"))
    )
    has_random_receipt = any(
        isinstance(node, dict) and node.get("operation") == "character_check"
        for node in _walk(call.get("result"))
    )
    return committed and has_random_receipt and bool(payload.get("source_scene_id")) and bool(
        str(payload.get("source_excerpt") or "").strip()
    )


def _semantic_event_receipt(call: dict[str, Any], prerequisite: dict[str, Any]) -> bool:
    if call.get("tool") != "memory_change" or not call.get("ok"):
        return False
    arguments = call.get("arguments") or {}
    if arguments.get("action") != "commit":
        return False
    payload = arguments.get("payload") or {}
    event = payload.get("event") or {}
    event_payload = event.get("payload") or {}
    fact_key = str(prerequisite.get("fact_key") or "")
    reducer_id = str(prerequisite.get("id") or "")
    if (
        str(event.get("event_type") or "") != "source_semantic_event"
        or not str(event.get("summary") or "").strip()
        or event.get("audience_scope") not in {"actor", "party", "public"}
        or str(event_payload.get("reducer_id") or "") != reducer_id
        or not _source_ref_matches_evidence(
            event_payload.get("source_ref"), prerequisite
        )
    ):
        return False
    requested_fact = next(
        (
            fact
            for fact in payload.get("facts") or []
            if isinstance(fact, dict) and str(fact.get("fact_key") or "") == fact_key
        ),
        None,
    )
    if requested_fact is None or not str(requested_fact.get("content") or "").strip():
        return False
    result_event = next(
        (
            node
            for node in _walk(call.get("result"))
            if isinstance(node, dict)
            and node.get("id")
            and node.get("event_type") == "source_semantic_event"
        ),
        None,
    )
    if result_event is None:
        return False
    event_id = str(result_event["id"])
    return any(
        isinstance(node, dict)
        and str(node.get("fact_key") or "") == fact_key
        and event_id in [str(value) for value in node.get("source_event_ids") or []]
        for node in _walk(call.get("result"))
    )


def _valid_managed_source_ref(value: Any) -> bool:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return False
    return isinstance(value, dict) and all(
        value.get(field)
        for field in ("module_id", "scene_id", "chunk_id", "content_sha256")
    )


def _source_ref_matches_evidence(value: Any, prerequisite: dict[str, Any]) -> bool:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return False
    if not _valid_managed_source_ref(value):
        return False
    evidence = prerequisite.get("source_evidence") or {}
    for field in ("page_start", "page_end"):
        if field in evidence and value.get(field) != evidence[field]:
            return False
    if "heading_path" in evidence and list(value.get("heading_path") or []) != list(
        evidence["heading_path"]
    ):
        return False
    return True


def _source_acquisition_item_ids(call: dict[str, Any], expected_name: str) -> set[str]:
    """Return only item ids minted by this acquisition, never projected inventory."""

    expected = expected_name.casefold()
    for node in _walk(call.get("result")):
        if not isinstance(node, dict) or node.get("status") != "committed":
            continue
        declared_ids = {str(value) for value in node.get("item_ids") or []}
        if not declared_ids:
            continue
        minted_ids = {
            str(item.get("id"))
            for item in node.get("items") or []
            if isinstance(item, dict)
            and item.get("id")
            and str(item.get("name") or "").casefold() == expected
        }
        matched = declared_ids & minted_ids
        if matched:
            return matched
    return set()


def _source_item_receipt(
    call: dict[str, Any],
    prerequisite: dict[str, Any],
    matched_receipts: list[tuple[dict[str, Any], dict[str, Any]]],
) -> bool:
    if call.get("tool") != "campaign_change" or not call.get("ok"):
        return False
    arguments = call.get("arguments") or {}
    action = arguments.get("action")
    expected_action = prerequisite.get("receipt")
    if action != expected_action:
        return False
    expected_name = str(prerequisite.get("item_name") or "")
    payload = arguments.get("payload") or {}
    if not _source_ref_matches_evidence(payload.get("source_ref"), prerequisite):
        return False
    result = call.get("result")
    if not any(
        isinstance(node, dict) and node.get("status") == "committed"
        for node in _walk(result)
    ):
        return False
    if expected_action == "loot_acquire":
        return bool(_source_acquisition_item_ids(call, expected_name))
    if expected_action == "item_spend":
        item_id = str(payload.get("item_id") or "")
        acquired_item_ids = {
            acquired_item_id
            for prior_prerequisite, prior_call in matched_receipts
            if prior_prerequisite.get("receipt") == "loot_acquire"
            for acquired_item_id in _source_acquisition_item_ids(
                prior_call, expected_name
            )
        }
        return item_id in acquired_item_ids and any(
            isinstance(node, dict)
            and str(node.get("id") or "") == item_id
            and str(node.get("name") or "").casefold() == expected_name.casefold()
            for node in _walk(result)
        )
    return False


def _ending_prerequisite_receipt(
    call: dict[str, Any],
    prerequisite: dict[str, Any],
    matched_receipts: list[tuple[dict[str, Any], dict[str, Any]]],
) -> bool:
    receipt = prerequisite.get("receipt")
    if receipt == "character_check":
        return _successful_check_receipt(call, prerequisite, matched_receipts)
    if receipt == "semantic_event":
        return _semantic_event_receipt(call, prerequisite)
    if receipt in {"loot_acquire", "item_spend"}:
        return _source_item_receipt(call, prerequisite, matched_receipts)
    return False


def _call_branch_id(call: dict[str, Any]) -> str | None:
    for node in _walk(call.get("result")):
        if not isinstance(node, dict):
            continue
        binding = node.get("host_context_binding")
        if isinstance(binding, dict) and binding.get("branch_id"):
            return str(binding["branch_id"])
    arguments = call.get("arguments") or {}
    branch_id = arguments.get("branch_id") or arguments.get("expected_branch_id")
    return str(branch_id) if branch_id else None


def _source_item_chain_is_live(
    calls: list[dict[str, Any]],
    matched: list[tuple[dict[str, Any], dict[str, Any], int]],
    stop: int,
    *,
    branch_id: str | None = None,
) -> bool:
    """Reject a receipt prefix after its selected source item was already spent."""

    acquired: dict[str, int] = {}
    for prerequisite, call, call_index in matched:
        if prerequisite.get("receipt") != "loot_acquire":
            continue
        expected_name = str(prerequisite.get("item_name") or "")
        for item_id in _source_acquisition_item_ids(call, expected_name):
            acquired[item_id] = call_index
    for item_id, acquired_at in acquired.items():
        for call in calls[acquired_at + 1 : stop]:
            if branch_id and _call_branch_id(call) != branch_id:
                continue
            arguments = call.get("arguments") or {}
            payload = arguments.get("payload") or {}
            if (
                call.get("tool") == "campaign_change"
                and call.get("ok")
                and arguments.get("action") == "item_spend"
                and str(payload.get("item_id") or "") == item_id
                and any(
                    isinstance(node, dict) and node.get("status") == "committed"
                    for node in _walk(call.get("result"))
                )
            ):
                return False
    return True


def _complete_ending_receipt_sequence(
    calls: list[dict[str, Any]],
    prerequisites: list[dict[str, Any]],
    *,
    stop: int | None = None,
    branch_id: str | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any], int]] | None:
    """Find an ordered complete chain without committing to an earlier dead end."""

    limit = len(calls) if stop is None else min(stop, len(calls))

    def _search(
        prerequisite_index: int,
        cursor: int,
        matched: list[tuple[dict[str, Any], dict[str, Any], int]],
    ) -> list[tuple[dict[str, Any], dict[str, Any], int]] | None:
        if prerequisite_index >= len(prerequisites):
            return matched
        prerequisite = prerequisites[prerequisite_index]
        prior_receipts = [(expected, call) for expected, call, _index in matched]
        for call_index in range(cursor, limit):
            if branch_id and _call_branch_id(calls[call_index]) != branch_id:
                continue
            if not _source_item_chain_is_live(
                calls, matched, call_index, branch_id=branch_id
            ):
                continue
            if not _ending_prerequisite_receipt(
                calls[call_index], prerequisite, prior_receipts
            ):
                continue
            completed = _search(
                prerequisite_index + 1,
                call_index + 1,
                [*matched, (prerequisite, calls[call_index], call_index)],
            )
            if completed is not None:
                return completed
        return None

    return _search(0, 0, [])


def _best_partial_ending_receipt_sequence(
    calls: list[dict[str, Any]],
    prerequisites: list[dict[str, Any]],
    *,
    branch_id: str | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any], int]]:
    """Choose the longest valid prefix, preferring the newer prefix on ties."""

    best: list[tuple[dict[str, Any], dict[str, Any], int]] = []

    def _search(
        prerequisite_index: int,
        cursor: int,
        matched: list[tuple[dict[str, Any], dict[str, Any], int]],
    ) -> None:
        nonlocal best
        complete = len(matched) >= len(prerequisites)
        if complete or _source_item_chain_is_live(
            calls, matched, len(calls), branch_id=branch_id
        ):
            if len(matched) > len(best) or (
                len(matched) == len(best)
                and matched
                and (not best or matched[0][2] > best[0][2])
            ):
                best = matched
        if prerequisite_index >= len(prerequisites):
            return
        prerequisite = prerequisites[prerequisite_index]
        prior_receipts = [(expected, call) for expected, call, _index in matched]
        for call_index in range(cursor, len(calls)):
            if branch_id and _call_branch_id(calls[call_index]) != branch_id:
                continue
            if not _source_item_chain_is_live(
                calls, matched, call_index, branch_id=branch_id
            ):
                continue
            if _ending_prerequisite_receipt(
                calls[call_index], prerequisite, prior_receipts
            ):
                _search(
                    prerequisite_index + 1,
                    call_index + 1,
                    [*matched, (prerequisite, calls[call_index], call_index)],
                )

    _search(0, 0, [])
    return best


def _ending_completed(
    calls: list[dict[str, Any]], *, prerequisites: list[dict[str, Any]] | None = None
) -> bool:
    required = list(prerequisites or [])
    for verify_index, call in enumerate(calls):
        if call.get("tool") != "playthrough_manifest" or not call.get("ok"):
            continue
        args = call.get("arguments") or {}
        if args.get("action") not in {"verify_ending", "verify-ending"}:
            continue
        verify_branch_id = _call_branch_id(call)
        contradicts_surrender = _ending_verify_contradicts_surrender(call, required)
        for node in _walk(call.get("result")):
            if not isinstance(node, dict):
                continue
            completed = node.get("status") in {"completed", "achieved"} or (
                node.get("achieved") is True and node.get("completed") is not False
            )
            if (
                completed
                and not contradicts_surrender
                and _complete_ending_receipt_sequence(
                    calls,
                    required,
                    stop=verify_index,
                    branch_id=verify_branch_id,
                )
                is not None
            ):
                return True
    return False


def _ending_verify_contradicts_surrender(
    call: dict[str, Any], prerequisites: list[dict[str, Any]]
) -> bool:
    return any(item.get("receipt") == "item_spend" for item in prerequisites) and any(
        isinstance(check, dict)
        and check.get("kind") == "campaign_state_value"
        and check.get("path") == "party.inventory.items"
        and check.get("operator") == "truthy"
        for node in _walk(call.get("result"))
        if isinstance(node, dict)
        for check in node.get("verification") or []
    )


def _ending_prerequisite_audit(
    route: dict[str, Any], calls: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Report the ordered authoritative receipts already present for each ending."""

    audits: list[dict[str, Any]] = []
    current_branch_id = next(
        (
            branch_id
            for call in reversed(calls)
            if call.get("ok")
            for branch_id in [_call_branch_id(call)]
            if branch_id
        ),
        None,
    )
    for scenario in route.get("scenarios") or []:
        prerequisites = list(scenario.get("ending_prerequisites") or [])
        if not prerequisites:
            continue
        complete_sequence = _complete_ending_receipt_sequence(
            calls, prerequisites, branch_id=current_branch_id
        )
        partial_sequence = (
            []
            if complete_sequence is not None
            else _best_partial_ending_receipt_sequence(
                calls, prerequisites, branch_id=current_branch_id
            )
        )
        matched_receipts: list[tuple[dict[str, Any], dict[str, Any]]] = []
        receipts: list[dict[str, Any]] = []
        first_missing_id: str | None = None
        contradictory_completed_verification = any(
            call.get("tool") == "playthrough_manifest"
            and call.get("ok")
            and (call.get("arguments") or {}).get("action")
            in {"verify_ending", "verify-ending"}
            and _ending_verify_contradicts_surrender(call, prerequisites)
            and (
                current_branch_id is None
                or _call_branch_id(call) == current_branch_id
            )
            and any(
                isinstance(node, dict)
                and (
                    node.get("status") in {"completed", "achieved"}
                    or (
                        node.get("achieved") is True
                        and node.get("completed") is not False
                    )
                )
                for node in _walk(call.get("result"))
            )
            for call in calls
        )
        replacement_blocked_by_completed_manifest = any(
            call.get("tool") == "playthrough_manifest"
            and not call.get("ok")
            and (call.get("arguments") or {}).get("action") == "configure_ending"
            and "completed playthrough ending conditions cannot be changed"
            in str(call.get("error") or "")
            and (
                current_branch_id is None
                or _call_branch_id(call) == current_branch_id
            )
            for call in calls
        )
        for prerequisite_index, prerequisite in enumerate(prerequisites):
            evidence = prerequisite.get("source_evidence") or {}
            headings = [
                str(value).strip()
                for value in evidence.get("heading_path") or []
                if str(value).strip()
            ]
            safe_query = re.sub(r"[^\w\s-]", " ", headings[-1] if headings else "")
            safe_query = " ".join(safe_query.split())
            if first_missing_id is not None:
                receipts.append(
                    {
                        "id": prerequisite.get("id"),
                        "receipt": prerequisite.get("receipt"),
                        "status": "blocked_by_prior",
                        "expected": prerequisite,
                        "safe_source_query": safe_query,
                    }
                )
                continue
            if complete_sequence is not None:
                matched = complete_sequence[prerequisite_index][2]
            elif prerequisite_index < len(partial_sequence):
                matched = partial_sequence[prerequisite_index][2]
            else:
                matched = None
            if matched is None:
                first_missing_id = str(prerequisite.get("id") or "")
                receipts.append(
                    {
                        "id": prerequisite.get("id"),
                        "receipt": prerequisite.get("receipt"),
                        "status": "missing",
                        "expected": prerequisite,
                        "safe_source_query": safe_query,
                    }
                )
                continue
            matched_call = calls[matched]
            matched_receipts.append((prerequisite, matched_call))
            receipt_result = {
                "id": prerequisite.get("id"),
                "receipt": prerequisite.get("receipt"),
                "status": "matched",
                "call_index": matched,
                "tool": matched_call.get("tool"),
                "action": (matched_call.get("arguments") or {}).get("action"),
                "expected": prerequisite,
                "safe_source_query": safe_query,
            }
            if prerequisite.get("receipt") == "loot_acquire":
                expected_name = str(prerequisite.get("item_name") or "")
                receipt_result["acquired_item_ids"] = sorted(
                    _source_acquisition_item_ids(matched_call, expected_name)
                )
            receipts.append(receipt_result)
        audits.append(
            {
                "scenario_id": scenario.get("id"),
                "receipts": receipts,
                "first_missing_id": first_missing_id,
                "ready_for_verification": first_missing_id is None,
                "current_branch_id": current_branch_id,
                "contradictory_completed_verification": (
                    contradictory_completed_verification
                ),
                "replacement_blocked_by_completed_manifest": (
                    replacement_blocked_by_completed_manifest
                ),
            }
        )
    return audits


def _manifest_party_ready(calls: list[dict[str, Any]]) -> bool:
    for call in reversed(calls):
        if call.get("tool") != "playthrough_manifest" or not call.get("ok"):
            continue
        for node in _walk(call.get("result")):
            if not isinstance(node, dict) or not isinstance(node.get("manifest"), dict):
                continue
            manifest = node["manifest"]
            party = manifest.get("party") or {}
            members = party.get("members") or []
            active_count = sum(
                1
                for member in members
                if isinstance(member, dict) and member.get("status") == "active"
            )
            return (
                manifest.get("status") in {"ready", "in_progress", "completed"}
                and active_count > 0
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


def _final_campaign_state(calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the last authoritative DM campaign state projection in the audit."""

    for call in reversed(calls):
        if call.get("principal") != "dm" or not call.get("ok"):
            continue
        for node in _walk(call.get("result")):
            if not isinstance(node, dict):
                continue
            binding = node.get("host_context_binding")
            if not isinstance(binding, dict) or not binding.get("branch_id"):
                continue
            phase = node.get("effective_game_phase") or node.get("game_phase")
            if phase is None and node.get("phase") in {"lobby", "play", "combat"}:
                phase = node.get("phase")
            if phase is None:
                for child in _walk(node):
                    if isinstance(child, dict) and child.get("game_phase") in {
                        "lobby",
                        "play",
                        "combat",
                    }:
                        phase = child["game_phase"]
                        break
            if phase is None:
                continue
            return {"branch_id": str(binding["branch_id"]), "phase": phase}
    return None


def _initial_campaign_branch(calls: list[dict[str, Any]]) -> str | None:
    for call in calls:
        if call.get("principal") != "dm" or not call.get("ok"):
            continue
        if call.get("tool") not in {"campaign_create", "campaign_query"}:
            continue
        for node in _walk(call.get("result")):
            binding = node.get("host_context_binding") if isinstance(node, dict) else None
            if isinstance(binding, dict) and binding.get("branch_id"):
                return str(binding["branch_id"])
    return None


def _latest_snapshot_on_branch(
    calls: list[dict[str, Any]], branch_id: str | None
) -> dict[str, Any] | None:
    if not branch_id:
        return None
    snapshots: dict[str, dict[str, Any]] = {}
    for call in calls:
        if not call.get("ok"):
            continue
        for node in _walk(call.get("result")):
            if (
                not isinstance(node, dict)
                or str(node.get("branch_id") or "") != branch_id
                or not node.get("id")
                or not isinstance(node.get("slot"), int)
                or isinstance(node.get("slot"), bool)
            ):
                continue
            snapshots[str(node["id"])] = {
                "id": str(node["id"]),
                "slot": int(node["slot"]),
                "label": str(node.get("label") or ""),
                "branch_id": branch_id,
            }
    return max(snapshots.values(), key=lambda item: item["slot"], default=None)


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
                if isinstance(member, dict)
                and member.get("actor_id")
                and member.get("status") == "active"
            }
    return set()


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
                current = latest.get(actor_id)
                current_revision = (
                    current.get("revision", -1) if current is not None else -1
                )
                candidate_revision = node.get("revision", -1)
                if (
                    not isinstance(current_revision, int)
                    or isinstance(current_revision, bool)
                ):
                    current_revision = -1
                if (
                    not isinstance(candidate_revision, int)
                    or isinstance(candidate_revision, bool)
                ):
                    candidate_revision = -1
                if candidate_revision >= current_revision:
                    latest[actor_id] = node
    return latest


def _class_feature_catalog(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect current-level feature requirements from public catalog receipts."""

    features: dict[str, dict[str, Any]] = {}
    for call in calls:
        arguments = call.get("arguments") or {}
        if (
            call.get("tool") != "character_query"
            or not call.get("ok")
            or arguments.get("view") != "catalog"
        ):
            continue
        for node in _walk(call.get("result")):
            if not isinstance(node, dict) or node.get("kind") != "feature":
                continue
            artifact_id = str(node.get("id") or "")
            requirements = dict(node.get("selection_requirements") or {})
            class_name = str(requirements.get("class_name") or "")
            if not artifact_id or not class_name:
                continue
            features[artifact_id] = {
                "artifact_id": artifact_id,
                "name": str(node.get("name") or artifact_id),
                "class_name": class_name,
                "minimum_level": int(requirements.get("minimum_level", 1) or 1),
                "application_state": str(node.get("application_state") or ""),
            }
    return list(features.values())


def _party_mechanical_readiness(calls: list[dict[str, Any]]) -> dict[str, list[str]]:
    party_ids = _manifest_party_ids(calls)
    views = _party_character_views(calls)
    class_features = _class_feature_catalog(calls)
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
        applied_feature_ids = {
            str(item.get("id") or "")
            for item in dict(sheet.get("content") or {}).get("features") or []
            if isinstance(item, dict)
        }
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
        for class_entry in classes:
            class_name = str(class_entry.get("name") or "")
            class_level = int(class_entry.get("level", 0) or 0)
            for feature in class_features:
                if (
                    feature["application_state"] == "selection_ready"
                    and feature["class_name"].casefold() == class_name.casefold()
                    and feature["minimum_level"] <= class_level
                    and feature["artifact_id"] not in applied_feature_ids
                ):
                    actor_gaps.append(f"class_feature_missing:{feature['artifact_id']}")
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


def _source_combat_actor_variants(calls: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    variants: dict[str, dict[str, Any]] = {}
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
            actor_id = node["character"].get("id")
            if actor_id:
                variants[str(actor_id)] = {
                    "variant": dict(node.get("variant") or {}),
                    "evidence": dict(node.get("variant_evidence") or {}),
                }
    return variants


def _scenario_source_opposition_covered(
    scenario: dict[str, Any], calls: list[dict[str, Any]]
) -> bool:
    expected_groups = list(scenario.get("initial_source_groups") or [])
    if not expected_groups:
        return bool(_source_combat_actor_ids(calls))
    source_actor_ids = _source_combat_actor_ids(calls)
    source_identities = _source_combat_actor_identities(calls)
    source_variants = _source_combat_actor_variants(calls)
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
            required_variant = dict(expected.get("required_variant") or {})
            variant_source_kind = str(expected.get("variant_source_kind") or "")
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
                or (
                    required_variant
                    and any(
                        any(
                            source_variants.get(actor_id, {})
                            .get("variant", {})
                            .get(key)
                            != value
                            for key, value in required_variant.items()
                        )
                        for actor_id in actor_ids
                    )
                )
                or (
                    variant_source_kind
                    and any(
                        source_variants.get(actor_id, {})
                        .get("evidence", {})
                        .get("kind")
                        != variant_source_kind
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


def _source_opposition_evidence_audit(
    route: dict[str, Any], calls: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Expose why the latest successful source manifest still misses route evidence."""

    audits: list[dict[str, Any]] = []
    successful_starts = [
        call for call in calls if call.get("tool") == "combat_start" and call.get("ok")
    ]
    for scenario in route.get("scenarios") or []:
        expected_groups = list(scenario.get("initial_source_groups") or [])
        if not expected_groups or _scenario_source_opposition_covered(scenario, calls):
            continue
        mode = scenario.get("positioning_mode")
        matching_starts = [
            call
            for call in successful_starts
            if mode not in {"grid", "agent"}
            or (call.get("arguments") or {}).get("positioning_mode") == mode
        ]
        if not matching_starts:
            continue
        latest = matching_starts[-1]
        actual_groups = list(
            dict((latest.get("arguments") or {}).get("participant_manifest") or {}).get(
                "groups"
            )
            or []
        )
        comparisons = []
        for expected in expected_groups:
            actual = next(
                (
                    item
                    for item in actual_groups
                    if item.get("role") == expected.get("role")
                    and item.get("required_count") == expected.get("required_count")
                    and str(item.get("label") or "").casefold()
                    in {
                        str(expected.get("subject") or "").casefold(),
                        str(expected.get("statblock_source_identity") or "").casefold(),
                    }
                ),
                None,
            )
            expected_excerpt = " ".join(str(expected.get("source_excerpt") or "").split())
            actual_excerpt = " ".join(str((actual or {}).get("source_excerpt") or "").split())
            comparisons.append(
                {
                    "subject": expected.get("subject"),
                    "expected_source_excerpt": expected_excerpt,
                    "actual_source_excerpt": actual_excerpt,
                    "exact_excerpt_match": actual_excerpt.casefold()
                    == expected_excerpt.casefold(),
                    "actual_actor_ids": list((actual or {}).get("actor_ids") or []),
                }
            )
        audits.append(
            {
                "scenario_id": scenario.get("id"),
                "latest_successful_start_key": (latest.get("arguments") or {}).get(
                    "idempotency_key"
                ),
                "groups": comparisons,
            }
        )
    return audits


def _current_opposition_audit(
    route: dict[str, Any], audits: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep historical receipts diagnostic without presenting stale route truth."""

    expected_by_scenario = {
        str(scenario.get("id") or ""): {
            str(group.get("subject") or ""): " ".join(
                str(group.get("source_excerpt") or "").split()
            )
            for group in scenario.get("initial_source_groups") or []
            if isinstance(group, dict)
        }
        for scenario in route.get("scenarios") or []
        if isinstance(scenario, dict)
    }
    current: list[dict[str, Any]] = []
    for audit in audits:
        item = deepcopy(audit)
        expected = expected_by_scenario.get(str(item.get("scenario_id") or ""), {})
        groups = []
        for group in item.get("groups") or []:
            refreshed = deepcopy(group)
            source_excerpt = expected.get(str(refreshed.get("subject") or ""))
            if source_excerpt is not None:
                previous = str(refreshed.get("expected_source_excerpt") or "")
                if previous and previous != source_excerpt:
                    refreshed["historical_expected_source_excerpt"] = previous
                    refreshed["route_evidence_changed"] = True
                refreshed["expected_source_excerpt"] = source_excerpt
                actual = " ".join(
                    str(refreshed.get("actual_source_excerpt") or "").split()
                )
                refreshed["exact_excerpt_match"] = (
                    actual.casefold() == source_excerpt.casefold()
                )
            groups.append(refreshed)
        item["groups"] = groups
        current.append(item)
    return current


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
        end_index = next(
            (
                later_index
                for later_index in range(index + 1, len(calls))
                if _call_matches([calls[later_index]], "combat_end")
            ),
            None,
        )
        if end_index is None:
            continue
        encounter_calls = calls[index + 1 : end_index]
        if require_render and not _call_matches(
            encounter_calls, "combat_query", action="render"
        ):
            continue
        if not any(_committed_combat_execution(item) for item in encounter_calls):
            continue
        return True
    return False


def _committed_combat_execution(call: dict[str, Any]) -> bool:
    if not call.get("ok"):
        return False
    tool = call.get("tool")
    if tool == "combat_cast_spell":
        return any(
            isinstance(node, dict) and node.get("status") == "committed"
            for node in _walk(call.get("result"))
        )
    if tool == "combat_choice":
        return (call.get("arguments") or {}).get("action") == "execute_plan" and any(
            isinstance(node, dict) and node.get("status") == "committed"
            for node in _walk(call.get("result"))
        )
    return tool in {
        "combat_common_action",
        "combat_reaction_attack",
        "combat_resolve_attack",
        "combat_use_activity",
    }


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
        return _conversation_to_combat_covered(calls)
    if mechanism == "npc_conversation":
        return _persistent_npc_conversation_covered(calls)
    if mechanism == "agent_semantic_spell_ruling":
        return _has_agent_semantic_spell_ruling(calls)
    if mechanism == "chase_to_combat":
        return _chase_sequence_covered(calls, require_combat_start=True)
    if mechanism == "chase":
        return _chase_sequence_covered(calls)
    mappings: dict[str, tuple[tuple[str, str | None], ...]] = {
        "play_scene": (("module_query", "scene"),),
        "noncombat_check": (("character_check", None),),
        "resource_settlement": (("campaign_change", None), ("character_action", None)),
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
        ending_prerequisites = list(scenario.get("ending_prerequisites") or [])
        scenario_gaps = [
            item
            for item in mechanisms
            if (
                not _ending_completed(calls, prerequisites=ending_prerequisites)
                if item == "ending"
                else not _mechanism_covered(item, calls)
            )
        ]
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
        if scenario.get("ending_status") == "legal_complete" and not _ending_completed(
            calls, prerequisites=ending_prerequisites
        ):
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
    campaign_pc_ids = _campaign_pc_ids(calls)
    party_mechanical_gaps = _party_mechanical_readiness(calls)
    if party_mechanical_gaps:
        gaps.append("preparation:party_mechanics_not_ready")
    if list_changed_count < 1:
        gaps.append("host:list_changed_not_observed")
    if _has_exposure_reopen_after_transition(calls):
        gaps.append("exposure:reopened_after_transition")
    if any(
        scenario.get("ending_status") == "legal_complete"
        and _ending_completed(
            calls,
            prerequisites=list(scenario.get("ending_prerequisites") or []),
        )
        for scenario in route.get("scenarios") or []
    ) and any(
        scenario.get("recovery_operations") for scenario in route.get("scenarios") or []
    ):
        initial_branch = _initial_campaign_branch(calls)
        final_state = _final_campaign_state(calls)
        if (
            initial_branch is None
            or final_state is None
            or final_state.get("branch_id") != initial_branch
            or final_state.get("phase") != "play"
        ):
            gaps.append("final_state:source_branch_play_unverified")
    return {
        "complete": not gaps,
        "gaps": sorted(set(gaps)),
        "scenarios": scenarios,
        "ending_completed": any(
            scenario.get("ending_status") == "legal_complete"
            and _ending_completed(
                calls,
                prerequisites=list(scenario.get("ending_prerequisites") or []),
            )
            for scenario in route.get("scenarios") or []
        ),
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


def _execution_order_gaps(
    gaps: list[str], route: dict[str, Any] | None = None
) -> list[str]:
    """Put mechanical prerequisites before outcomes and historical audit debt."""

    scenario_order = {
        str(scenario.get("id") or ""): index
        for index, scenario in enumerate((route or {}).get("scenarios") or [])
    }

    return sorted(
        gaps,
        key=lambda gap: (
            0
            if gap.startswith("preparation:")
            else 3
            if gap.endswith(":ending") or gap.endswith(":legal_ending_not_verified")
            else 4
            if gap == "exposure:reopened_after_transition"
            else 1,
            scenario_order.get(gap.split(":", 1)[0], len(scenario_order)),
            0 if gap.endswith(":source_opposition_missing") else 1,
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
    source_opposition_audit: list[dict[str, Any]] | None = None,
    ending_prerequisite_audit: list[dict[str, Any]] | None = None,
    combat_start_business_template: dict[str, Any] | None = None,
    party_mechanical_gaps: dict[str, list[str]] | None = None,
    initial_source_branch_id: str | None = None,
    latest_source_snapshot: dict[str, Any] | None = None,
) -> str:
    opposition_audit_json = json.dumps(source_opposition_audit or [], ensure_ascii=False)
    ending_audit_json = json.dumps(ending_prerequisite_audit or [], ensure_ascii=False)
    mandatory_ending_mutation: dict[str, Any] = {}
    receipt_tools = {
        "loot_acquire": ("campaign_change", "loot_acquire"),
        "item_spend": ("campaign_change", "item_spend"),
        "semantic_event": ("memory_change", "commit"),
        "character_check": ("character_check", "check"),
    }
    for audit in ending_prerequisite_audit or []:
        scenario_id = str(audit.get("scenario_id") or "")
        ending_gaps = {
            f"{scenario_id}:ending",
            f"{scenario_id}:legal_ending_not_verified",
        }
        if (
            audit.get("replacement_blocked_by_completed_manifest") is True
            and ending_gaps.intersection(gaps)
        ):
            mandatory_ending_mutation = {
                "scenario_id": scenario_id,
                "tool": "branch_change",
                "action": "create",
                "required_phase": "lobby",
                "source_branch_id": initial_source_branch_id or "",
                "from_snapshot_id": str((latest_source_snapshot or {}).get("id") or ""),
                "from_snapshot_slot": (latest_source_snapshot or {}).get("slot"),
                "from_snapshot_label": str(
                    (latest_source_snapshot or {}).get("label") or ""
                ),
                "checkout": True,
                "ready_for_verification": False,
                "forbidden_actions_until_checked_out": [
                    "configure_ending",
                    "verify_ending",
                    "loot_acquire",
                ],
                "reason": (
                    "the current branch has an immutable completed ending whose "
                    "condition contradicts the ordered receipt chain"
                ),
            }
            break
    if not mandatory_ending_mutation:
        for audit in ending_prerequisite_audit or []:
            first_missing = str(audit.get("first_missing_id") or "")
            if not first_missing:
                continue
            receipt = next(
                (
                    item
                    for item in audit.get("receipts") or []
                    if str(item.get("id") or "") == first_missing
                ),
                {},
            )
            tool, action = receipt_tools.get(
                str(receipt.get("receipt") or ""), ("", "")
            )
            mandatory_ending_mutation = {
                "scenario_id": audit.get("scenario_id"),
                "first_missing_id": first_missing,
                "tool": tool,
                "action": action,
                "expected": receipt.get("expected") or {},
                "safe_source_query": receipt.get("safe_source_query") or "",
                "ready_for_verification": False,
            }
            if action == "item_spend":
                write_key = f"{run_id}-{line_id}-cycle-{cycle:03d}-{first_missing}"
                acquired_item_ids = [
                    str(item_id)
                    for prior in audit.get("receipts") or []
                    if prior.get("receipt") == "loot_acquire"
                    and prior.get("status") == "matched"
                    for item_id in prior.get("acquired_item_ids") or []
                ]
                mandatory_ending_mutation["write_ids"] = {
                    "idempotency_key": write_key,
                    "spend_id": f"{write_key}-spend",
                }
                mandatory_ending_mutation["matched_acquisition_item_ids"] = (
                    acquired_item_ids
                )
            break
    if not mandatory_ending_mutation:
        for audit in ending_prerequisite_audit or []:
            scenario_id = str(audit.get("scenario_id") or "")
            ending_gaps = {
                f"{scenario_id}:ending",
                f"{scenario_id}:legal_ending_not_verified",
            }
            if (
                audit.get("ready_for_verification") is True
                and audit.get("contradictory_completed_verification") is True
                and ending_gaps.intersection(gaps)
            ):
                mandatory_ending_mutation = {
                    "scenario_id": scenario_id,
                    "tool": "playthrough_manifest",
                    "action": "configure_ending",
                    "ready_for_verification": True,
                    "require_new_condition_id": True,
                    "forbidden_actions_until_configured": [
                        "verify_ending",
                        "loot_acquire",
                    ],
                    "reason": (
                        "the completed historical condition requires party inventory "
                        "truthy after the ordered source item surrender"
                    ),
                }
                break
    mandatory_ending_mutation_json = json.dumps(
        mandatory_ending_mutation, ensure_ascii=False
    )
    combat_template_json = json.dumps(
        combat_start_business_template or {}, ensure_ascii=False
    )
    party_gaps_json = json.dumps(party_mechanical_gaps or {}, ensure_ascii=False)
    return f"""You are the DM Agent for a real full-campaign regression.
Run id: {run_id}
Campaign line label (never a campaign UUID): {line_id}
Source-declared D&D edition: {unit.get("edition")}
MANDATORY_FIRST_ENDING_MUTATION={mandatory_ending_mutation_json}
When this object is non-empty, execute its named tool/action as the first
authoritative write after the required source lookup. Until it succeeds, do not
call `playthrough_manifest` except when it is the named tool/action, and do not
report the ending complete.
When it names `branch_change(create)`, copy its exact non-empty
`from_snapshot_id` into `payload.from_snapshot_id`; the facade does not accept
`source_branch_id` or `from_snapshot_selection` as substitutes. First use
`game_phase(set)` only if needed
to enter Lobby, query and verify the selected source-branch snapshot, then create
and checkout one recovery branch from that snapshot. Those phase/preflight calls
are the only allowed writes before the named branch mutation. After checkout,
consume `tools/list_changed`, retain the binding with `exposure(get/search/set)`,
and re-read the branch-local manifest. Never mutate or delete the immutable
historical branch.
When it includes `write_ids`, copy those exact fresh values into the tool's
top-level `idempotency_key` and `payload.spend_id`; do not derive either from the
fixture receipt id or any historical attempt. For an item surrender, copy the
single `matched_acquisition_item_ids` value into `payload.item_id`; never choose
another same-named item from inventory or history.
Source-selected advancement mode: {unit.get("advancement_mode")}
Source-reviewed preparation profile (re-resolve its exact current Pack evidence):
{json.dumps(unit.get("play_requirements") or {}, ensure_ascii=False)}
Trusted player principal to grant one actor: cli:{player_principal}
Cycle: {cycle}
Current authoritative party mechanical gaps: {party_gaps_json}

Use dnd.full and CAMPAIGN_REGRESSION. At the physical process's first bootstrap,
start from the six core tools, open exposure once, consume native list changes,
and call only exposed native tools. A host context-barrier rebuild may replay
this original prompt inside the same process; that replay is not a new session
and never authorizes another `exposure(open)`. After the first campaign binding,
retain it and recover tools only through native `tools/list_changed`, a fresh
native list, and `exposure(get/search/set)`. Resume authoritative state if the
campaign already exists. Never
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
prior_source_opposition_evidence_audit={opposition_audit_json}
current_ending_prerequisite_receipt_audit={ending_audit_json}
latest_successful_combat_start_business_template={combat_template_json}
When a combat scenario includes `initial_source_groups`, treat each entry as a
source-backed audit expectation: re-read the cited source, preserve every listed
group, exact count, source identity, and `required_variant` in preflight, and
instantiate all of its canonical actors before `combat_start`. A variant must
cite the requested managed `variant_source_kind`. For `module-chunk`, use
`module_search` on the exact heading or printed phrase, expand the chosen hit,
and copy its returned chunk id; a scenario/evidence id is never a chunk id.
When `statblock_evidence` is present, retrieve that exact page and heading before
concluding the mechanical card is absent or starting its Pack review.
Do not reduce or omit a group to make preflight pass, and do not omit a printed
override.

When an ending scenario lists `ending_prerequisites`, those Pack-local entries
are mandatory receipt expectations, not story answers. Re-read their managed
source evidence and satisfy each prerequisite through the named public facade
before configuring or verifying the ending. `receipt="loot_acquire"` and
`receipt="item_spend"` require ordered, committed source-bound `campaign_change`
acquisition/surrender receipts for the named item. For `item_spend`, send a new
top-level idempotency key and `payload={{"spend_id": <new stable id>,
"item_id": <the exact matched acquisition item id>, "quantity": 1,
"reason": <source-defined surrender>, "source_ref": <managed ref>}}`; do not
put `excerpt` or other unsupported fields inside that source_ref, and never
reuse a spend id from a rejected attempt. `receipt="character_check"`
requires a committed engine roll with exact scene evidence, skill/DC, required
success, and an authoritative random receipt. Put the check evidence in the
public receipt fields `payload.source_scene_id` and `payload.source_excerpt`;
an otherwise identical nested `payload.source_evidence` object does not satisfy
that receipt. For a reduced check, also send `payload.base_dc` and the exact
`payload.applied_reducer_ids` declared by the fixture. Preserve the check's
separate ability and skill fields—for this fixture that means
`payload.ability="Charisma"` and `payload.skill="Persuasion"`, never
`ability="persuasion"` with the skill omitted. A preceding
`receipt="semantic_event"` requires `memory_change(action="commit")`, an event
with `event_type="source_semantic_event"`, the exact fixture `id` in
`event.payload.reducer_id`, a managed `event.payload.source_ref`, and the exact
fixture `fact_key`; the returned fact must cite the returned event id. A reduced
check must use `base_dc - sum(dc_reduction)` for exactly its declared
`applied_reducer_ids`. Bare add/upsert facts never qualify.
Treat `current_ending_prerequisite_receipt_audit` as the machine authority for
the ordered receipt chain. Resume at its `first_missing_id`; historical completed
manifest status or successful verification never substitutes for a missing
receipt. For that entry, follow its full `expected` object, including exact
`source_evidence`, `fact_key`, item, check, and reducer fields. A semantic event
must resolve and use that entry's own page range and heading. Never reuse the
preceding acquisition's source reference for a presentation or reducer event,
even when both concern the same named item. A semantic event
must use party/public/actor audience and request the fact in the same atomic
commit with `payload.event.audience_scope="party"` (the audience does not belong
on a fact) and through the plural array
`payload.facts=[{{"kind":"memory_fact","fact_key":"...", ...}}]`; singular
`payload.fact`, `facts=[]`, or a returned fact without the event id never
matches. When
that exact stable fact already exists, use public `memory_query` to fresh-read
its `revision_id`, then supply it as the fact's `expected_revision_id` in the
same source-bound atomic commit. Do not change keys or fall back to an unlinked
upsert merely because the authoritative fact already exists. When
all entries are matched, configure and verify the exact ending without replaying
the receipt chain.
If an immutable historical condition contradicts the completed receipt chain
(for example, it requires `party.inventory.items` truthy after a source item was
surrendered), never reacquire the item to satisfy that condition. Configure a
new condition id using an exact source-bound fact/content and current runtime
checks, then verify that new condition.
While `ready_for_verification=false`, do not call
`playthrough_manifest(verify_ending)`, do not describe the ending as complete,
and do not use a historical completed manifest as evidence. After the required
source lookup, the first authoritative write of the cycle must be the exact
`first_missing_id` receipt; a read-only manifest verification is not progress.
Campaign memory fact `content` is always a string. A later ending
`kind="memory_fact"` equality check must copy the successful commit's returned
`fact.content` exactly; do not substitute boolean `true` for string `"True"` or
add implicit coercion. Standalone `memory_change(action="revise")` uses
`payload={{"memory_id": <fact.id>, "content": <string>,
"expected_revision_id": <fact.revision_id>}}`; the campaign CAS
`expected_revision` remains top-level. Never confuse stable `id` with the
concurrency token `revision_id` or put `expected_revision_id` at tool top-level.
Do not manufacture the result with
`memory_change`, `module_set_progress`, or manifest fields. Those projections
may record an outcome only after the independent prerequisite receipts exist.
A remaining ending gap is never, by itself, permission to rebuild or re-import
an already-active source volume. First compare `managed_sources` with the
campaign's active module sources and query the exact indexed ending evidence.
When the matching source volume is active and resolves that evidence, do not
call `module_draft`. When a multi-volume campaign's managed next volume is not
active and public queries prove that its ending evidence is consequently absent,
that absent volume is a real Pack lifecycle obligation: in Lobby, use its exact
managed path/checksum to resume or start one draft, finalize/import it, require
`skipped=false`, and activate only the module id returned by that import. Never
retry the already-active earlier volume as a substitute.

Treat the current evidence-gap list below as authoritative for what remains;
prior Agent narration is not proof of a blocker. Query current state first and
do not repeat a prerequisite that is no longer listed. When `Current
authoritative party mechanical gaps` names
`class_feature_missing:<artifact_id>`, the catalog receipt proves that exact
feature is selection-ready and available at the actor's current class level.
Fresh-read that actor, load `character_content_apply`, apply the exact artifact
id with only its catalog-required selection fields, and re-read the resulting
features, activities, and resources. Do not call `character_action` with a
feature or core mechanic id before the feature has materialized its returned
activity card, and never synthesize the missing resource through a state patch.
In particular, when no `preparation` gap remains, do not rebuild the existing
party or re-import an unchanged Pack. A `source_opposition_missing` gap does not
by itself prove that the active Pack needs a new review. In Lobby, first use
exact `rule_search` with only `campaign_id`, the exact printed identity as
`query`, and optional `top_k`.
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
An ending is missing Pack content only when public active-Pack queries cannot
resolve its indexed source evidence or prove that evidence corrupted; an empty
runtime manifest condition or unmet receipt is not missing Pack content. For
genuinely proven Pack-only gaps, start an explicit new draft/version from the
same managed source and add only
the evidence-backed missing review/package decisions, finalize it, import the
new artifact, and activate only the module id returned by that import.
For an exact managed image-only card with no text candidate, `content_key` is a
Pack-local stable slot chosen deterministically from the exact printed identity
as lowercase ASCII words joined by hyphens; it is not an opaque server id.
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
`source_opposition_missing` means no `combat_start` in the audit has matched the
complete expected group evidence; it does not mean the actors are absent or
that another combat action is needed. Compare the latest successful start's
role, count, actor source identity, variant evidence, and exact normalized
`source_excerpt` with `initial_source_groups`. The participant excerpt is
encounter evidence, while the actor's content review is mechanical statblock
evidence; these separate passages need not match. A rejected or stale manifest
is not proof of Pack corruption, and an identical statblock review cannot fix
encounter prose. Re-read the exact managed encounter excerpt and the Pack copy
of that same passage before authoring anything. Only if that same Pack passage
is demonstrably extraction-corrupted relative to managed source should you
follow OPPOSITION_HYDRATION and repair the bounded scene in a new Pack version.
Only create the exact actor shortfall, and use
`character_query(view="get")` with a returned actor id rather than unsupported
name filters or an empty batch.

When a scenario requires `agent_semantic_spell_ruling`, inspect preflight's
`ruling_spell_ids` and the actor's hydrated spell cards. Select one exact
source-backed spell with an Agent-owned semantic resolution path. If its card is
    standard content with a persisted `agent_ruling` clause, do not call
    `content_solution`: first call `combat_cast_spell` without a declaration to read
    its exact `agent_ruling_contract`, then resubmit the cast as
    `declaration={{"agent_ruling": {{...}}}}` with that exact source excerpt plus
    the Agent's bounded decision. Do not flatten those fields or use
    `component_ruling` for the spell effect. Require the committed response to
record payment and `semantic_solution.status="agent_ruling_committed"`. For a
statblock/innate spell, omit `signature_free_cast`; the engine must consume its
recorded innate resource. A custom/imported card lacking a persisted plan uses
`content_solution(compile)`,
followed by `combat_cast_spell` and
`combat_choice(action="execute_plan")`. Bind either decision to the exact actor,
card, and current evidence; MCP must pay the action, slot or innate use and own
all random results and mechanical state changes. Do not replace this obligation
with a weapon attack, narration, a raw sheet edit, or a spell whose
parser-damaged name never produced a hydrated card.
A `pending_ruling` response only returns the declaration contract and spends
nothing; it is not a Combat execution receipt. Before the corrected submission,
read `combat_query(status)`. If another actor owns the turn, call
`combat_end_turn` only for the returned current actor with the latest revision,
then query status again after every committed turn write. Never guess or cache
the initiative sequence. Once the selected caster is current, submit the exact
declaration and require `status="committed"`; do not end the encounter after a
rejected or merely pending cast.
On resume, compare any active encounter's immutable participants and source
manifest with the remaining evidence before the first Combat mutation. Take
participant ids from `combat_query(status)`, then load `character_query` and
read each required actor individually with `view="get"`; do not assume a host's
bounded summary of the nested encounter exposed its hydrated cards or
`ruling_spell_ids`. If it cannot
qualify because it contains the wrong actor revision, lacks the required
hydrated card, or used non-matching source evidence, end it through
`combat_end` and rebuild the qualifying encounter once from current actors.
Search and load the exact `combat_end` tool, then close immediately with a
    truthful `outcome.status="interrupted"` and a summary naming the nonqualifying
    evidence. `combat_end_turn` only passes one actor's turn and must not be repeated
    to simulate ending the encounter. Resolve a genuinely blocking pending window
    first, but do not grind irrelevant turns or replace participants inside active
    Combat.
    If Combat coverage and every remaining Combat-specific mechanism are already
    satisfied, an active encounter left by an interrupted regression cycle is no
    longer part of the route. "Satisfied" requires audit receipts from one bounded
    encounter: the qualifying source-backed `combat_start`, at least one successful
    engine-owned attack/activity/spell execution before its `combat_end`, and a
    `combat_query(view="render")` receipt when the scenario requires rendering.
    Participants, a ready manifest, grid coordinates, or `combat_start` alone never
    prove execution. An otherwise qualifying active encounter at round 1 with no
    such action receipt is unfinished: resume it and perform the remaining Combat
    mechanisms instead of closing it. Only after the receipts already exist should
    you query status and use `combat_end` with truthful
    `outcome.status="interrupted"`; do not replay a completed encounter before
    returning to the first remaining Play/ending gap.
    In `positioning_mode="agent"`, a pending attack's Agent spatial ruling belongs
    at `action.context.spatial_facts`, not at `action.spatial_facts`,
    `action.attack`, `agent_ruling`, or `declaration`. Copy the preflight contract
    exactly. Its attack facts require `decision_id`, a source-grounded `reason`,
    `targetable`, `in_range`, `cover_degree`, `attacker_can_see_target`, and
    `target_can_see_attacker`. Re-run preflight with that context until it is
    `ready`, then submit the same action context to the engine-owned resolver and
    require a committed result. Do not pass turns to avoid an unresolved spatial
    ruling or end the encounter while every action remains pending.

Prepare/finalize/import/activate the current Pack through the public lifecycle;
before any module authoring write, read the current
`dnd:full/references/skill-groups/lobby/modules-import.md` asset and follow its
public request shapes exactly;
when source review, opposition hydration, a directly proven missing/corrupted
ending source, or another Pack authoring obligation remains, stay in or return
to Lobby, search and load
`module_draft`, and call `module_draft(action="get")` with no payload before
creating an actor, starting another draft, or entering Play. Resume the newest
matching unfinished job and preserve its public ids. An empty candidate list is
not evidence that this matching draft is unusable and never authorizes another
`start`; use the managed page/statblock recovery path on that job. If prior
cycles already left duplicate matching unfinished jobs, resume only the first
newest matching handle returned by the public list and create no more. Start a
new draft only if that public list proves no matching resumable job exists or a
finalized Pack requires an explicit new version.
create or resume one reproducibly seeded campaign; create a positive-sized legal
party; grant the named player principal both campaign membership with role
`player` and explicit control of one PC through separate public `access_grant`
calls; then progress the source-backed
route to one legal verified ending. Exercise the listed Play, NPC, chase,
combat, audience, and recovery obligations at genuine scene boundaries. Keep
    NPC workers isolated and close/abort before mechanics or combat. Use both
    spatial modes only where assigned by the matrix. Let MCP own dice and state.
    `conversation_to_combat` is a controlled negative invariant probe, not a
    normal mechanic. For that remaining gap, open the authoritative conversation
    but do not ingest, activate a worker, or close it yet. Submit an otherwise
    valid, source-backed `combat_start` while the conversation is open and require
    the call to fail specifically because the conversation is active; an unrelated
    revision, participant, map, or coordinate error does not count. Because the
    rejected call cannot mutate state, this probe does not violate the normal
    close-before-mechanic rule. Then `get` and close/abort the conversation,
    release any worker, and retry the same valid combat start at the refreshed
    revision. Require that retry to succeed, then immediately end the now-covered
    encounter truthfully as interrupted and return to Play.
    Before opening the probe conversation, rebuild every participant and manifest
    actor id from a fresh `character_query(view="list")`; never copy an id from a
    prior failed request or narration. Construct the combat payload once and reuse
    it verbatim for the rejected and successful calls, changing only
    `expected_revision` and `idempotency_key`.
    When `latest_successful_combat_start_business_template` is non-empty, it is a
    public receipt from this campaign's latest successful start. Re-read its
    actors and current scene to confirm that it remains valid, then use that JSON
    object as the business payload for both calls. Do not retype identifiers,
    omit fields, or redesign the manifest. Add only the current
    `expected_revision` and a distinct `idempotency_key` to each call.
    For a remaining `resource_settlement` gap in Play, first query the current
    party cards and choose one actually available, source-bound noncombat activity
    or spell; commit it through `character_action` with that actor's exact current
    revision. Do not invent an activity merely to satisfy coverage. Then settle a
    source-compatible party rest through `campaign_change(action="party_rest")`.
    Its payload uses `members`, not `actor_ids`: each member object requires the
    returned `character_id` and its exact `expected_revision`; `duration_minutes`
    and `rest_type` are siblings of `members`. For a plain Long Rest, omit optional
    prepared-spell and Hit Die choices unless the authoritative card and Agent
    decision require them. Refresh every actor revision after the character action
    and any preceding stabilization before constructing the one atomic rest.
Keep the campaign in Lobby until the current Pack is active, the party is ready,
and the player grant exists. If an earlier interrupted cycle entered Play before
those prerequisites, close any active Play workflow and return to Lobby before
continuing preparation. Do not chase later matrix gaps ahead of prerequisites.
Never issue `game_phase`, `combat_start`, `combat_end`, restore, checkout,
undo, or redo in the same parallel tool batch as an `exposure(set)` built from
the old native list. Wait for the authoritative transition, consume
`tools/list_changed`, refresh the native list, and only then search/set the next
phase's tools; never call `exposure(open)` for that refresh. A context-barrier
replay after checkout or restore is still the same process and binding. After a
successful recovery operation, do not repeat that operation merely because the
barrier rebuilt context: consume its returned binding/state once, refresh the
native list, then query and set any tool cropped by the phase change.
When the current gaps include `preparation`, do not initialize the playthrough
manifest or enter Play: read the finalized draft artifact, complete a successful
`content_pack(import, kind="module")`, and activate only the new module id
returned by that import. A prior activation without a successful Pack import
does not satisfy preparation. Here `preparation` means a scenario gap ending in
`:preparation`; the separate
`preparation:player_membership_or_actor_grant_missing` gap requires only the
missing campaign/actor grants and never authorizes rebuilding the Pack or party.
`preparation:manifest_party_not_ready` requires only creating at least one
campaign PC when none exists, replacing the complete manifest with the current
active members' full records, and
syncing it to `ready`; it also never authorizes rebuilding the Pack.
An empty manifest does not mean the campaign has no PCs. Before any build, call
`character_query(view="list")`, count the distinct campaign-bound PC instances,
and reuse the current active party. If at least one suitable PC exists, make no
build call and register the existing actors. Never create a reserve/bench PC in
this fresh regression campaign merely to match a recommendation or old plan.
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
either result: `selected_size` is an explicit positive Agent selection and the
source minimum/maximum are advisory only. Never change or block that selection
merely to match a recommendation. The initial selection is planning metadata,
not an invariant: the registered active party may gain or lose members during
the campaign, but must never be empty. Register every current active member with manifest
`replace`, and verify the subsequent `sync` response itself is `ready`.
After the one permitted initial exposure open, seeing only core tools is
expected, not a blocker: search and set the next required native tool. A cycle
that only lists state or
opens exposure has made no progress. Unless a true external boundary is reached,
complete at least one successful authoritative mutation toward the first unmet
prerequisite before stopping the cycle.
For `final_state:source_branch_play_unverified`, do not replay any completed
route or ending. In Lobby, query branches and checkout the campaign's original
source branch (the branch used before recovery), consume the binding barrier,
then enter Play through `game_phase`. After every transition use only native
list refresh plus `exposure(get/search/set)`, never open. Finish with a fresh DM
`campaign_query(view="resume")` and player-safe read proving that exact branch
and Play phase.
Stop only for a real external boundary or when the current cycle has exhausted
its tool budget; in that case report the exact authoritative blocker and leave
state resumable.
When the first current gap ends in `source_opposition_missing`, read the focused
Skill asset
`dnd:full/skills/dnd-dm/references/OPPOSITION_HYDRATION.md` in full before
choosing rule-source hydration, reviewed rulebook repair, or module review. It
is intentionally small enough for one bounded read. Follow its exact-id and
localized-canonical-source sequence; do not reconstruct that sequence from the
large CAMPAIGN_REGRESSION parent section. This routes to reusable Skill
procedure; it supplies no creature identity or module answer.

Current evidence gaps from prior cycles, ordered by execution dependency rather
than alphabetically: {json.dumps(_execution_order_gaps(gaps, route), ensure_ascii=False)}
If `current_ending_prerequisite_receipt_audit` has a `first_missing_id`, that is
the first executable action. Before any ending manifest, progress, or memory
write, use `module_search` and `module_expand` to resolve the missing entry's
exact `expected.source_evidence`, then copy the returned managed `module_id`,
`scene_id`, `chunk_id`, `content_sha256`, pages, heading, and excerpt. The
manifest's current conclusion source, an asset checksum, or a hand-written
source object never substitutes for that prerequisite source. Do not load or
call `module_set_progress` until the missing receipt has been accepted by the
machine audit.
Use that entry's machine-generated `safe_source_query` verbatim for the first
search; it is derived from the expected heading with FTS punctuation removed.
If it returns no exact page/heading match, simplify words from the same expected
heading only. Never switch to a conclusion/current-scene query or select a hit
whose page/heading differs from `expected.source_evidence`; previously expanded
mismatched sources remain negative evidence, not fallbacks.
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
    prompt_path = unit_dir / "process" / f"{stem}.prompt.txt"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
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
        "--message-file",
        str(prompt_path.resolve()),
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
        current_calls = _tool_timeline(
            _read_tool_audit(dm_audit), principal="dm"
        ) + _tool_timeline(_read_tool_audit(player_audit), principal="player")
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
                source_opposition_audit=_current_opposition_audit(
                    route,
                    _source_opposition_evidence_audit(route, current_calls),
                ),
                ending_prerequisite_audit=_ending_prerequisite_audit(
                    route, current_calls
                ),
                combat_start_business_template=_latest_combat_start_business_template(
                    current_calls
                ),
                party_mechanical_gaps=dict(audit.get("party_mechanical_gaps") or {}),
                initial_source_branch_id=_initial_campaign_branch(current_calls),
                latest_source_snapshot=_latest_snapshot_on_branch(
                    current_calls, _initial_campaign_branch(current_calls)
                ),
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

    dm_records = _read_session(dm_audit)
    player_records = _read_session(player_audit)
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
        "observable_turn_timing": {
            "dm": _decision_timing(dm_records, principal="dm"),
            "player": _decision_timing(player_records, principal="player"),
        },
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


def _refresh_completed_report(
    report: dict[str, Any], unit: dict[str, Any], route: dict[str, Any]
) -> dict[str, Any]:
    """Re-evaluate a saved run against the current source-backed route contract."""

    refreshed = dict(report)
    calls = list(report.get("tool_timeline") or [])
    coverage = _coverage_audit(
        route,
        calls,
        process_count=len(report.get("agent_processes") or []),
        list_changed_count=int(report.get("tools_list_changed_observed") or 0),
        expected_edition=str(unit.get("edition") or "").strip() or None,
        expected_advancement_mode=(
            str(unit.get("advancement_mode") or "").strip() or None
        ),
    )
    refreshed.update(
        {
            "discovered_unit": unit,
            "route": route,
            "coverage": coverage,
        }
    )
    return refreshed


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
            existing = _refresh_completed_report(
                _read_json(report_path), unit, routes[line_id]
            )
            _write_json(report_path, existing)
            if dict(existing["coverage"]).get("complete") is True:
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
