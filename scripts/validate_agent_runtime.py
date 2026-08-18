"""Validate the external Full D&D Skills -> Agent -> D&D MCP runtime chain."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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

    if dnd.get("type") != "streamableHttp":
        errors.append("sagasmith_dnd.type must be 'streamableHttp' for the single local MCP host.")
    url = dnd.get("url")
    parsed_url = urlparse(url) if isinstance(url, str) else None
    if (
        parsed_url is None
        or parsed_url.scheme != "http"
        or parsed_url.hostname not in {"127.0.0.1", "localhost"}
        or parsed_url.port != 8767
        or parsed_url.path.rstrip("/") != "/mcp"
    ):
        errors.append("sagasmith_dnd.url must be http://127.0.0.1:8767/mcp.")

    if dnd.get("enabledTools") != ["*"]:
        errors.append(
            "sagasmith_dnd.enabledTools must be ['*'] so tools/list_changed can refresh "
            "the server-owned native list."
        )
    if dnd.get("injectPrincipal") is not True:
        errors.append("sagasmith_dnd.injectPrincipal must be true for actor authorization.")
    if dnd.get("sessionScoped") is not True:
        errors.append(
            "sagasmith_dnd.sessionScoped must be true so principal, campaign, phase, "
            "and mutable native tools cannot leak between Agent sessions."
        )
    timeout = dnd.get("toolTimeout")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout < 900:
        errors.append("sagasmith_dnd.toolTimeout must be at least 900 seconds for PDF imports.")

    tools = _mapping(config.get("tools"))
    whitelist = tools.get("ssrfWhitelist")
    if not isinstance(whitelist, list) or "127.0.0.1/32" not in whitelist:
        errors.append(
            "tools.ssrfWhitelist must include 127.0.0.1/32 for the local HTTP MCP."
        )

    expected_skills = (agent_root.parent / "SagaSmith-dnd-skills" / "full" / "skills").resolve()
    if dnd_skill_roots and expected_skills not in dnd_skill_roots:
        errors.append(
            "Agent externalSkillsDirs must expose the sibling Full D&D skill pack."
        )

    for label, root in (
        (
            "D&D rulebook import root",
            agent_root.parent / "reference" / "DnD-Books" / "5e" / "Books",
        ),
        (
            "D&D module import root",
            agent_root.parent / "reference" / "DnD-Books" / "5e" / "Campaign",
        ),
    ):
        if not root.is_dir():
            errors.append(f"{label} does not exist: {root}")
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
