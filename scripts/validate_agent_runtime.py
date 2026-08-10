"""Validate the external Full D&D Skills -> Agent -> D&D MCP runtime chain."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

REQUIRED_DND_CORE_TOOLS = {
    "campaign_query",
    "exposure",
    "game_phase",
    "server_capabilities",
    "skill_query",
    "storage_status",
}
REQUIRED_DND_SKILLS = ("dnd-dm", "dnd-campaign-manager")


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
        elif dnd_skill_roots and expected not in dnd_skill_roots:
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
