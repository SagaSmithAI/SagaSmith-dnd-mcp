from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_agent_runtime.py"
SPEC = importlib.util.spec_from_file_location("validate_sagasmith_runtime", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


def _prepare_runtime(tmp_path: Path) -> tuple[Path, Path, dict]:
    agent_root = tmp_path / "SagaSmith-agent"
    config_path = agent_root / "config" / "config.json"
    config_path.parent.mkdir(parents=True)

    skill_root = tmp_path / "SagaSmith-dnd-skills" / "full" / "skills"
    for name in ("dnd-dm", "dnd-campaign-manager"):
        target = skill_root / name / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text(f"# {name}\n", encoding="utf-8")
    executable = (
        tmp_path
        / "SagaSmith-dnd-mcp"
        / ".venv"
        / "Scripts"
        / "sagasmith-dnd-mcp.exe"
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    rule_root = tmp_path / "reference" / "DnD-Books" / "5e" / "Books"
    rule_root.mkdir(parents=True)
    module_root = tmp_path / "reference" / "DnD-Books" / "5e" / "Campaign"
    module_root.mkdir(parents=True)

    config = {
        "agents": {
            "defaults": {
                "externalSkillsDirs": ["../SagaSmith-dnd-skills/full/skills"]
            }
        },
        "tools": {
            "mcpServers": {
                "sagasmith_dnd": {
                    "command": "../SagaSmith-dnd-mcp/.venv/Scripts/sagasmith-dnd-mcp.exe",
                    "cwd": "../SagaSmith-dnd-mcp",
                    "env": {
                        "SAGASMITH_DND_SKILLS_DIR": "../SagaSmith-dnd-skills",
                        "SAGASMITH_DND_MCP_RULE_IMPORT_ROOTS": "../reference/DnD-Books/5e/Books",
                        "SAGASMITH_DND_MCP_MODULE_IMPORT_ROOTS": (
                            "../reference/DnD-Books/5e/Campaign"
                        ),
                    },
                    "toolTimeout": 900,
                    "injectPrincipal": True,
                    "enabledTools": ["*"],
                }
            }
        },
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return agent_root, config_path, config


def test_sagasmith_runtime_preflight_accepts_complete_external_chain(tmp_path: Path) -> None:
    agent_root, config_path, _ = _prepare_runtime(tmp_path)

    assert VALIDATOR.validate_runtime(config_path, agent_root) == []


def test_sagasmith_runtime_preflight_rejects_mcp_without_full_skills(tmp_path: Path) -> None:
    agent_root, config_path, config = _prepare_runtime(tmp_path)
    config["agents"]["defaults"]["externalSkillsDirs"] = []
    config_path.write_text(json.dumps(config), encoding="utf-8")

    errors = VALIDATOR.validate_runtime(config_path, agent_root)

    assert any("externalSkillsDirs" in error for error in errors)
    assert any("dnd-campaign-manager" in error for error in errors)


def test_sagasmith_runtime_preflight_enforces_auth_tools_and_pdf_timeout(
    tmp_path: Path,
) -> None:
    agent_root, config_path, config = _prepare_runtime(tmp_path)
    dnd = config["tools"]["mcpServers"]["sagasmith_dnd"]
    dnd["enabledTools"] = []
    dnd["injectPrincipal"] = False
    dnd["toolTimeout"] = 60
    config_path.write_text(json.dumps(config), encoding="utf-8")

    errors = VALIDATOR.validate_runtime(config_path, agent_root)

    assert any("tools/list_changed" in error for error in errors)
    assert any("injectPrincipal" in error for error in errors)
    assert any("at least 900" in error for error in errors)


def test_sagasmith_runtime_preflight_requires_campaign_import_root(
    tmp_path: Path,
) -> None:
    agent_root, config_path, config = _prepare_runtime(tmp_path)
    del config["tools"]["mcpServers"]["sagasmith_dnd"]["env"][
        "SAGASMITH_DND_MCP_MODULE_IMPORT_ROOTS"
    ]
    config_path.write_text(json.dumps(config), encoding="utf-8")

    errors = VALIDATOR.validate_runtime(config_path, agent_root)

    assert any("MODULE_IMPORT_ROOTS" in error for error in errors)
