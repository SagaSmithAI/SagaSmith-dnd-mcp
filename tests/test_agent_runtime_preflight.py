import json
from pathlib import Path

from scripts.validate_agent_runtime import validate_runtime


def _local_config(skills: Path) -> dict:
    return {
        "agents": {"defaults": {"externalSkillsDirs": [str(skills)]}},
        "tools": {
            "ssrfWhitelist": ["127.0.0.1/32"],
            "mcpServers": {
                "sagasmith_dnd": {
                    "type": "streamableHttp",
                    "url": "http://127.0.0.1:8767/mcp",
                    "enabledTools": ["*"],
                    "injectPrincipal": True,
                    "sessionScoped": True,
                    "toolTimeout": 900,
                }
            },
        },
    }


def _workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    agent_root = tmp_path / "SagaSmith-agent"
    skills = tmp_path / "SagaSmith-dnd-skills" / "full" / "skills"
    for skill in ("dnd-dm", "dnd-campaign-manager"):
        path = skills / skill / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {skill}\n", encoding="utf-8")
    (tmp_path / "reference" / "DnD-Books" / "5e" / "Books").mkdir(parents=True)
    (tmp_path / "reference" / "DnD-Books" / "5e" / "Campaign").mkdir(parents=True)
    config_path = agent_root / "config" / "config.json"
    config_path.parent.mkdir(parents=True)
    return agent_root, skills, config_path


def test_preflight_accepts_single_dynamic_http_mcp(tmp_path: Path) -> None:
    agent_root, skills, config_path = _workspace(tmp_path)
    config_path.write_text(json.dumps(_local_config(skills)), encoding="utf-8")

    assert validate_runtime(config_path, agent_root) == []


def test_preflight_rejects_fixed_stdio_tool_configuration(tmp_path: Path) -> None:
    agent_root, skills, config_path = _workspace(tmp_path)
    config = _local_config(skills)
    config["tools"]["ssrfWhitelist"] = []
    config["tools"]["mcpServers"]["sagasmith_dnd"] = {
        "command": "sagasmith-dnd-mcp.exe",
        "enabledTools": ["campaign_query"],
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    errors = validate_runtime(config_path, agent_root)

    assert any("streamableHttp" in error for error in errors)
    assert any("tools/list_changed" in error for error in errors)
    assert any("127.0.0.1/32" in error for error in errors)


def test_preflight_requires_full_dnd_skills(tmp_path: Path) -> None:
    agent_root, skills, config_path = _workspace(tmp_path)
    config = _local_config(skills)
    config["agents"]["defaults"]["externalSkillsDirs"] = []
    config_path.write_text(json.dumps(config), encoding="utf-8")

    errors = validate_runtime(config_path, agent_root)

    assert any("externalSkillsDirs" in error for error in errors)
    assert any("dnd-campaign-manager" in error for error in errors)


def test_preflight_enforces_principal_and_pdf_timeout(tmp_path: Path) -> None:
    agent_root, skills, config_path = _workspace(tmp_path)
    config = _local_config(skills)
    dnd = config["tools"]["mcpServers"]["sagasmith_dnd"]
    dnd["injectPrincipal"] = False
    dnd["toolTimeout"] = 60
    config_path.write_text(json.dumps(config), encoding="utf-8")

    errors = validate_runtime(config_path, agent_root)

    assert any("injectPrincipal" in error for error in errors)
    assert any("at least 900" in error for error in errors)


def test_preflight_requires_session_scoped_dynamic_mcp(tmp_path: Path) -> None:
    agent_root, skills, config_path = _workspace(tmp_path)
    config = _local_config(skills)
    config["tools"]["mcpServers"]["sagasmith_dnd"].pop("sessionScoped")
    config_path.write_text(json.dumps(config), encoding="utf-8")

    errors = validate_runtime(config_path, agent_root)

    assert any("sessionScoped" in error for error in errors)


def test_preflight_requires_local_module_source_root(tmp_path: Path) -> None:
    agent_root, skills, config_path = _workspace(tmp_path)
    config_path.write_text(json.dumps(_local_config(skills)), encoding="utf-8")
    (tmp_path / "reference" / "DnD-Books" / "5e" / "Campaign").rmdir()

    errors = validate_runtime(config_path, agent_root)

    assert any("module import root" in error for error in errors)
