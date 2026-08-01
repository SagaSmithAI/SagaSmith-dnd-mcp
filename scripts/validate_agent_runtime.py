"""Validate the external Full D&D Skills -> Agent -> D&D MCP runtime chain."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

REQUIRED_DND_CORE_TOOLS = {
    "campaign_query",
    "exposure_call",
    "exposure_inspect",
    "exposure_load",
    "exposure_open",
    "exposure_search",
    "exposure_status",
    "exposure_unload",
    "game_phase",
    "server_capabilities",
    "server_tool_profiles",
    "skill_query",
    "storage_status",
}
REQUIRED_DND_SKILLS = ("dnd-dm", "dnd-campaign-manager")
REQUIRED_DND_PHASES = {"lobby", "play", "combat"}
SKILL_PLAN_RELATIVE_PATH = Path("full") / "data" / "skill-plan.v1.json"


def _resolve_config_path(agent_root: Path, value: str) -> Path:
    normalized = value.replace("\\", os.sep).replace("/", os.sep)
    path = Path(normalized).expanduser()
    return path.resolve() if path.is_absolute() else (agent_root / path).resolve()


def _resolve_config_roots(agent_root: Path, value: str) -> list[Path]:
    return [
        _resolve_config_path(agent_root, item)
        for item in value.split(os.pathsep)
        if item.strip()
    ]


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _validate_skill_plan(skill_root: Path) -> list[str]:
    plan_path = skill_root / SKILL_PLAN_RELATIVE_PATH
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [
            "SAGASMITH_DND_SKILLS_DIR does not contain "
            "full/data/skill-plan.v1.json."
        ]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"Cannot read the Full D&D skill plan as UTF-8 JSON: {exc}"]

    if not isinstance(plan, dict):
        return ["The Full D&D skill plan root must be a JSON object."]
    errors: list[str] = []
    if plan.get("schema_version") != 1:
        errors.append("The Full D&D skill plan schema_version must equal 1.")

    groups = _mapping(plan.get("groups"))
    core_groups = plan.get("core_groups")
    if not groups:
        errors.append("The Full D&D skill plan must declare skill groups.")
    if (
        not isinstance(core_groups, list)
        or not core_groups
        or any(not isinstance(group_id, str) for group_id in core_groups)
    ):
        errors.append("The Full D&D skill plan must declare non-empty core_groups.")
    elif any(group_id not in groups for group_id in core_groups):
        errors.append("The Full D&D skill plan core_groups reference unknown groups.")

    for group_id, raw_group in groups.items():
        group = _mapping(raw_group)
        dependencies = group.get("depends_on", [])
        if not isinstance(dependencies, list) or any(
            not isinstance(dependency, str) or dependency not in groups
            for dependency in dependencies
        ):
            errors.append(
                f"The Full D&D skill group {group_id!r} has invalid dependencies."
            )
        documents = group.get("documents")
        if not isinstance(documents, list) or not documents:
            errors.append(
                f"The Full D&D skill group {group_id!r} must declare documents."
            )
            continue
        for document in documents:
            value = _mapping(document)
            kind = value.get("kind")
            identifier = value.get("identifier")
            max_chars = value.get("max_chars")
            if (
                kind not in {"asset", "skill"}
                or not isinstance(identifier, str)
                or not identifier
                or not isinstance(max_chars, int)
                or isinstance(max_chars, bool)
                or max_chars < 256
            ):
                errors.append(
                    f"The Full D&D skill group {group_id!r} has an invalid document."
                )
                continue
            if kind != "asset":
                continue
            source, separator, relative_value = identifier.partition(":")
            relative = Path(relative_value.replace("/", os.sep))
            asset_path = (skill_root / relative).resolve()
            try:
                asset_path.relative_to(skill_root.resolve())
            except ValueError:
                errors.append(
                    f"The Full D&D skill group {group_id!r} escapes the Skills root."
                )
                continue
            if (
                source != "dnd"
                or not separator
                or relative.is_absolute()
                or ".." in relative.parts
                or not asset_path.is_file()
            ):
                errors.append(
                    f"The Full D&D skill group {group_id!r} references a missing asset."
                )
                continue
            try:
                chars = len(asset_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError) as exc:
                errors.append(
                    f"Cannot read a Full D&D skill-plan asset as UTF-8: {exc}"
                )
            else:
                if chars > max_chars:
                    errors.append(
                        f"The Full D&D skill group {group_id!r} exceeds max_chars."
                    )

    phase_baselines = _mapping(plan.get("phase_baselines"))
    if set(phase_baselines) != REQUIRED_DND_PHASES:
        errors.append(
            "The Full D&D skill plan must declare exactly lobby, play, and combat "
            "phase baselines."
        )
    else:
        for phase, group_ids in phase_baselines.items():
            if (
                not isinstance(group_ids, list)
                or not group_ids
                or any(
                    not isinstance(group_id, str) or group_id not in groups
                    for group_id in group_ids
                )
            ):
                errors.append(
                    f"The Full D&D skill plan {phase} baseline references invalid groups."
                )

    bindings = _mapping(plan.get("tool_group_bindings"))
    if not bindings:
        errors.append("The Full D&D skill plan must declare tool_group_bindings.")
    for tool_group_id, binding in bindings.items():
        required = _mapping(binding).get("required")
        if (
            not isinstance(required, list)
            or not required
            or any(
                not isinstance(group_id, str) or group_id not in groups
                for group_id in required
            )
        ):
            errors.append(
                "The Full D&D skill plan tool-group binding "
                f"{tool_group_id!r} references invalid skill groups."
            )
    return errors


def validate_runtime(config_path: Path, agent_root: Path) -> list[str]:
    """Return actionable errors without ever echoing configuration contents."""
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"Cannot read UTF-8 JSON config {config_path}: {exc}"]

    errors: list[str] = []
    defaults = _mapping(_mapping(config.get("agents")).get("defaults"))
    raw_skill_dirs = defaults.get("externalSkillsDirs")
    if not isinstance(raw_skill_dirs, list) or not raw_skill_dirs:
        errors.append(
            "agents.defaults.externalSkillsDirs must include the Full D&D skill directory."
        )
        skill_dirs: list[Path] = []
    else:
        skill_dirs = [
            _resolve_config_path(agent_root, value)
            for value in raw_skill_dirs
            if isinstance(value, str) and value.strip()
        ]

    dnd_skill_roots = [
        root
        for root in skill_dirs
        if all((root / name / "SKILL.md").is_file() for name in REQUIRED_DND_SKILLS)
    ]
    if not dnd_skill_roots:
        errors.append(
            "No externalSkillsDirs entry exposes both dnd-dm and "
            "dnd-campaign-manager from the Full D&D skill pack."
        )

    servers = _mapping(_mapping(config.get("tools")).get("mcpServers"))
    dnd = _mapping(servers.get("sagasmith_dnd"))
    if not dnd:
        return [*errors, "tools.mcpServers.sagasmith_dnd is not configured."]

    command = dnd.get("command")
    if not isinstance(command, str) or not _resolve_config_path(agent_root, command).is_file():
        errors.append("The configured sagasmith_dnd MCP executable does not exist.")
    cwd = dnd.get("cwd")
    if not isinstance(cwd, str) or not _resolve_config_path(agent_root, cwd).is_dir():
        errors.append("The configured sagasmith_dnd MCP working directory does not exist.")

    enabled = dnd.get("enabledTools")
    enabled_tools = set(enabled) if isinstance(enabled, list) else set()
    missing_tools = sorted(REQUIRED_DND_CORE_TOOLS - enabled_tools)
    if missing_tools:
        errors.append(f"sagasmith_dnd.enabledTools is missing: {', '.join(missing_tools)}")
    if dnd.get("injectPrincipal") is not True:
        errors.append("sagasmith_dnd.injectPrincipal must be true for actor authorization.")
    timeout = dnd.get("toolTimeout")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout < 900:
        errors.append("sagasmith_dnd.toolTimeout must be at least 900 seconds for PDF imports.")

    env = _mapping(dnd.get("env"))
    raw_dnd_skills = env.get("SAGASMITH_DND_SKILLS_DIR")
    if not isinstance(raw_dnd_skills, str):
        errors.append("SAGASMITH_DND_SKILLS_DIR is missing from the D&D MCP environment.")
    else:
        mcp_skill_root = _resolve_config_path(agent_root, raw_dnd_skills)
        expected = (mcp_skill_root / "full" / "skills").resolve()
        if not expected.is_dir():
            errors.append("SAGASMITH_DND_SKILLS_DIR does not contain full/skills.")
        else:
            errors.extend(_validate_skill_plan(mcp_skill_root))
            if dnd_skill_roots and expected not in dnd_skill_roots:
                errors.append(
                    "Agent externalSkillsDirs and SAGASMITH_DND_SKILLS_DIR do not point "
                    "to the same Full D&D skill pack."
                )

    raw_rule_root = env.get("SAGASMITH_DND_MCP_RULE_IMPORT_ROOTS")
    rule_roots = (
        _resolve_config_roots(agent_root, raw_rule_root)
        if isinstance(raw_rule_root, str)
        else []
    )
    if not rule_roots or not all(root.is_dir() for root in rule_roots):
        errors.append("SAGASMITH_DND_MCP_RULE_IMPORT_ROOTS does not exist.")
    raw_module_root = env.get("SAGASMITH_DND_MCP_MODULE_IMPORT_ROOTS")
    module_roots = (
        _resolve_config_roots(agent_root, raw_module_root)
        if isinstance(raw_module_root, str)
        else []
    )
    if not module_roots or not all(root.is_dir() for root in module_roots):
        errors.append("SAGASMITH_DND_MCP_MODULE_IMPORT_ROOTS does not exist.")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/config.json")
    parser.add_argument(
        "--agent-root",
        default=str(Path(__file__).resolve().parents[2] / "SagaSmith-agent"),
    )
    args = parser.parse_args()
    agent_root = Path(args.agent_root).expanduser().resolve()
    config_path = _resolve_config_path(agent_root, args.config)
    errors = validate_runtime(config_path, agent_root)
    if errors:
        for error in errors:
            print(f"[ERROR] {error}")
        return 1
    print("SagaSmith Full D&D Skills and MCP runtime configuration: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
