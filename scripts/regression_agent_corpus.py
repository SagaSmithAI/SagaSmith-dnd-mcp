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
        "combat": (("combat_start", None), ("combat_end", None)),
        "combat_render": (("combat_query", "render"),),
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
            and not _call_matches(calls, "combat_start", argument=("positioning_mode", mode))
        ):
            scenario_gaps.append(f"positioning_mode:{mode}")
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
    if not _has_player_access_pair(calls):
        gaps.append("preparation:player_membership_or_actor_grant_missing")
    if not _manifest_party_ready(calls):
        gaps.append("preparation:manifest_party_not_ready")
    if list_changed_count < 1:
        gaps.append("host:list_changed_not_observed")
    if _has_exposure_reopen_after_transition(calls):
        gaps.append("exposure:reopened_after_transition")
    return {
        "complete": not gaps,
        "gaps": sorted(set(gaps)),
        "scenarios": scenarios,
        "ending_completed": _ending_completed(calls),
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
Trusted player principal to grant one actor: cli:{player_principal}
Cycle: {cycle}

Use dnd.full and CAMPAIGN_REGRESSION. Start this process session from the six
core tools, open exposure, consume native list changes, and call only exposed
native tools. Resume authoritative state if the campaign already exists. Never
use shell, direct database access, an internal service, invented tool results,
or narration as a substitute for a committed result.

This runner gives each campaign line a fresh MCP home. On the first cycle,
`campaign_query(view="list")` normally returns no campaign. Do not pass the
campaign-line label as `campaign_id` and do not diagnose that absence as an
authorization failure. Open exposure without a campaign, search for the exact
`campaign_create` tool, set it, refresh the native list, call it directly with a
reproducible seed and this line label in its slug/name, then reopen exposure with
the returned real campaign UUID. On later cycles, locate that created campaign
through the authenticated list and resume using its UUID.

The following fixture is coverage evidence and route intent, not a story answer.
Retrieve and expand the exact managed source before deciding what happens:
managed_sources={json.dumps(_managed_source_summary(unit), ensure_ascii=False)}
evidence={json.dumps(_evidence_summary(route), ensure_ascii=False)}
scenarios={json.dumps(route.get("scenarios") or [], ensure_ascii=False)}

Treat the current evidence-gap list below as authoritative for what remains;
prior Agent narration is not proof of a blocker. Query current state first and
do not repeat a prerequisite that is no longer listed. In particular, when no
`preparation` gap remains, do not start, finalize, import, or activate another
module and do not rebuild the existing party.

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
After every exposure open, seeing only core tools is expected, not a blocker:
search and set the next required native tool. A cycle that only lists state or
opens exposure has made no progress. Unless a true external boundary is reached,
complete at least one successful authoritative mutation toward the first unmet
prerequisite before stopping the cycle.
Stop only for a real external boundary or when the current cycle has exhausted
its tool budget; in that case report the exact authoritative blocker and leave
state resumable.

Current evidence gaps from prior cycles: {json.dumps(gaps, ensure_ascii=False)}
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
    stdout_path: Path
    stderr_path: Path
    audit_path: Path


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
    except subprocess.TimeoutExpired as error:
        returncode = 124
        stdout = error.stdout or ""
        stderr = (error.stderr or "") + f"\nTimed out after {args.timeout_seconds}s\n"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return AgentProcess(
        principal=principal,
        session_id=session_id,
        cycle=cycle,
        returncode=returncode,
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
            "stdout": str(item.stdout_path.resolve()),
            "stderr": str(item.stderr_path.resolve()),
            "tool_audit": str(item.audit_path.resolve()),
        }
    artifacts: list[dict[str, Any]] = []
    for stdout_path in sorted(process_dir.glob("cycle-*-*.stdout.txt")):
        stem = stdout_path.name.removesuffix(".stdout.txt")
        cycle_text, principal = stem.removeprefix("cycle-").split("-", 1)
        row = prior.get(str(stdout_path.resolve()), {})
        artifacts.append(
            {
                "principal": row.get("principal", principal),
                "session_id": row.get("session_id"),
                "cycle": int(row.get("cycle", cycle_text)),
                "returncode": row.get("returncode"),
                "stdout": str(stdout_path.resolve()),
                "stderr": str(
                    (process_dir / f"{stem}.stderr.txt").resolve()
                ),
                "tool_audit": row.get("tool_audit"),
            }
        )
    return artifacts


def _next_cycle(unit_dir: Path) -> int:
    cycles = [int(item["cycle"]) for item in _process_artifacts(unit_dir)]
    return max(cycles, default=0) + 1


def _list_changed_count(unit_dir: Path) -> int:
    count = 0
    for process in _process_artifacts(unit_dir):
        for key in ("stdout", "stderr"):
            path = Path(process[key])
            if path.is_file():
                count += path.read_text(encoding="utf-8").count(LIST_CHANGED_LOG)
    return count


def _copy_transcript(source: Path, target: Path) -> None:
    if not source.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())


def _run_unit(
    args: argparse.Namespace, unit: dict[str, Any], route: dict[str, Any]
) -> dict[str, Any]:
    line_id = _unit_id(unit)
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
    dm_session = f"{args.run_id}:{line_id}:dm"
    player_session = f"{args.run_id}:{line_id}:player"
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
        )
    start_cycle = _next_cycle(unit_dir)

    for cycle in range(start_cycle, start_cycle + args.max_cycles):
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
            )
            continue
        player = _run_agent(
            args,
            config=config,
            agent_workspace=agent_workspace,
            unit_dir=unit_dir,
            principal=player_principal,
            session_id=player_session,
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
        )
        if audit["complete"]:
            break
        if player.returncode and args.fail_fast:
            break

    dm_source = _session_path(agent_workspace, dm_session)
    player_source = _session_path(agent_workspace, player_session)
    _copy_transcript(dm_source, unit_dir / "artifacts" / "dm-transcript.jsonl")
    _copy_transcript(player_source, unit_dir / "artifacts" / "player-transcript.jsonl")
    dm_rows = _read_tool_audit(dm_audit)
    player_rows = _read_tool_audit(player_audit)
    calls = _tool_timeline(dm_rows, principal="dm") + _tool_timeline(
        player_rows, principal="player"
    )
    process_artifacts = _process_artifacts(unit_dir, processes)
    list_changed_count = _list_changed_count(unit_dir)
    audit = _coverage_audit(
        route,
        calls,
        process_count=len(process_artifacts),
        list_changed_count=list_changed_count,
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
