from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.regression_agent_corpus import (
    _configure_agent,
    _coverage_audit,
    _decode_tool_content,
    _dm_prompt,
    _next_cycle,
    _player_ready,
    _process_artifacts,
    _read_tool_audit,
    _runnable_units,
    _tool_timeline,
)


def _call(
    tool: str,
    *,
    arguments: dict[str, object] | None = None,
    ok: bool = True,
    result: object | None = None,
    principal: str = "dm",
) -> dict[str, object]:
    return {
        "tool": tool,
        "arguments": arguments or {},
        "ok": ok,
        "result": result,
        "error": None if ok else "Error executing tool: revision conflict",
        "principal": principal,
    }


def test_wrapped_mcp_text_is_decoded_without_losing_artifacts() -> None:
    value = _decode_tool_content(
        json.dumps(
            {
                "artifacts": [{"path": "render.png"}],
                "text": json.dumps({"campaign_revision": 4, "positioning_mode": "grid"}),
            }
        )
    )
    assert value == {"campaign_revision": 4, "positioning_mode": "grid"}


def test_session_parser_retains_arguments_failures_and_native_results() -> None:
    rows = [
        {
            "role": "assistant",
            "timestamp": "2026-08-11T00:00:00",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "mcp_sagasmith_dnd_combat_start",
                        "arguments": json.dumps({"positioning_mode": "agent"}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "timestamp": "2026-08-11T00:00:01",
            "tool_call_id": "call-1",
            "name": "mcp_sagasmith_dnd_combat_start",
            "content": "Error executing tool combat_start: active chase must end first",
        },
    ]
    timeline = _tool_timeline(rows, principal="dm")
    assert timeline[0]["tool"] == "combat_start"
    assert timeline[0]["arguments"] == {"positioning_mode": "agent"}
    assert timeline[0]["ok"] is False
    assert "active chase" in timeline[0]["error"]


def test_session_parser_treats_no_output_as_failure() -> None:
    rows = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "empty",
                    "function": {"name": "mcp_sagasmith_dnd_exposure", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "empty",
            "name": "mcp_sagasmith_dnd_exposure",
            "content": "(no output)\n\n[Analyze the result and decide the next action.]",
        },
    ]
    timeline = _tool_timeline(rows, principal="dm")
    assert timeline[0]["ok"] is False
    assert timeline[0]["result"] is None


def test_session_parser_treats_bare_host_error_as_failure() -> None:
    rows = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "bare-error",
                    "function": {
                        "name": "mcp_sagasmith_dnd_module_expand",
                        "arguments": "{}",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "bare-error",
            "name": "mcp_sagasmith_dnd_module_expand",
            "content": (
                "No row was found when one was required\n\n"
                "[Analyze the error above and try a different approach.]"
            ),
        },
    ]

    timeline = _tool_timeline(rows, principal="dm")

    assert timeline[0]["ok"] is False
    assert timeline[0]["result"] is None
    assert "No row was found" in timeline[0]["error"]


def test_append_only_tool_audit_survives_context_barrier(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "assistant_message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "create",
                            "function": {
                                "name": "mcp_sagasmith_dnd_campaign_create",
                                "arguments": '{"name":"campaign"}',
                            },
                        }
                    ],
                },
                "tool_results": [
                    {
                        "role": "tool",
                        "tool_call_id": "create",
                        "name": "mcp_sagasmith_dnd_campaign_create",
                        "content": '{"id":"campaign-1"}',
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    timeline = _tool_timeline(_read_tool_audit(path), principal="dm")
    assert timeline[0]["tool"] == "campaign_create"
    assert timeline[0]["result"] == {"id": "campaign-1"}


def test_resume_cycles_preserve_existing_process_artifacts(tmp_path: Path) -> None:
    process_dir = tmp_path / "process"
    process_dir.mkdir()
    for cycle in (1, 4):
        stem = f"cycle-{cycle:03d}-regression-dm-module"
        (process_dir / f"{stem}.stdout.txt").write_text(
            "ToolListChangedNotification\n", encoding="utf-8"
        )
        (process_dir / f"{stem}.stderr.txt").write_text("", encoding="utf-8")

    artifacts = _process_artifacts(tmp_path)

    assert [item["cycle"] for item in artifacts] == [1, 4]
    assert _next_cycle(tmp_path) == 5


def test_player_starts_only_after_successful_actor_grant() -> None:
    principal = "regression-player-module"
    grant = _call(
        "access_grant",
        arguments={
            "scope": "actor",
            "principal_id": f"cli:{principal}",
            "payload": {"actor_id": "actor-1", "can_control": True},
        },
    )
    assert _player_ready([grant], principal_id=principal) is True
    assert _player_ready([{**grant, "ok": False}], principal_id=principal) is False
    assert (
        _player_ready(
            [{**grant, "arguments": {**grant["arguments"], "scope": "campaign"}}],
            principal_id=principal,
        )
        is False
    )


def test_coverage_requires_real_ordered_boundaries_retries_and_recovery() -> None:
    route = {
        "scenarios": [
            {
                "id": "route",
                "mechanisms": [
                    "npc_conversation",
                    "conversation_to_mechanic",
                    "conversation_to_combat",
                    "chase",
                    "chase_to_combat",
                    "combat",
                    "combat_render",
                    "idempotent_retry",
                    "revision_conflict_refresh",
                    "phase_exposure_refresh",
                    "ending",
                ],
                "positioning_mode": "agent",
                "audience": "player",
                "path": "recovery",
                "ending_status": "legal_complete",
                "recovery_operations": [
                    "process_restart",
                    "snapshot_restore",
                    "branch_checkout",
                    "undo_redo",
                ],
            }
        ]
    }
    retry = {"action": "write", "idempotency_key": "same-key", "expected_revision": 7}
    calls = [
        _call("skill_query"),
        _call("exposure", arguments={"action": "open"}),
        _call("exposure", arguments={"action": "search"}),
        _call("exposure", arguments={"action": "set"}),
        _call("npc_conversation", arguments={"action": "open"}),
        _call("npc_conversation", arguments={"action": "close"}),
        _call("character_check"),
        _call("npc_conversation", arguments={"action": "open"}),
        _call("combat_start", ok=False),
        _call("npc_conversation", arguments={"action": "close"}),
        _call("combat_start", arguments={"positioning_mode": "agent"}),
        _call("combat_end"),
        _call("chase", arguments={"action": "start"}),
        _call("combat_start", ok=False),
        _call("chase", arguments={"action": "end"}),
        _call("combat_start", arguments={"positioning_mode": "agent"}),
        _call("combat_query", arguments={"view": "render"}),
        _call("combat_end"),
        _call("campaign_event", arguments=retry),
        _call("campaign_event", arguments=retry),
        _call("campaign_event", ok=False),
        _call("campaign_query", arguments={"view": "resume"}),
        _call("snapshot_restore"),
        _call("branch_change", arguments={"action": "checkout"}),
        _call("state_revision", arguments={"action": "undo"}),
        _call("state_revision", arguments={"action": "redo"}),
        _call(
            "playthrough_manifest",
            arguments={"action": "verify_ending"},
            result={"status": "completed", "achieved": True},
        ),
        _call("campaign_query", principal="player"),
    ]
    audit = _coverage_audit(route, calls, process_count=4, list_changed_count=3)
    assert audit["complete"] is True
    assert audit["gaps"] == []


def test_coverage_does_not_accept_narration_or_unordered_successes() -> None:
    route = {
        "scenarios": [
            {
                "id": "boundary",
                "mechanisms": ["conversation_to_combat", "idempotent_retry", "ending"],
                "positioning_mode": "grid",
                "audience": "player",
                "ending_status": "legal_complete",
            }
        ]
    }
    calls = [
        _call("skill_query"),
        _call("exposure", arguments={"action": "open"}),
        _call("combat_start", arguments={"positioning_mode": "grid"}),
        _call("npc_conversation", arguments={"action": "close"}),
    ]
    audit = _coverage_audit(route, calls, process_count=1, list_changed_count=0)
    assert audit["complete"] is False
    assert any("conversation_to_combat" in gap for gap in audit["gaps"])
    assert any("idempotent_retry" in gap for gap in audit["gaps"])
    assert any("legal_ending_not_verified" in gap for gap in audit["gaps"])
    assert "host:list_changed_not_observed" in audit["gaps"]


def test_preparation_requires_finalize_import_activate_order() -> None:
    route = {"scenarios": [{"id": "prep", "mechanisms": ["preparation"]}]}
    bypassed = [
        _call("skill_query"),
        _call("exposure", arguments={"action": "open"}),
        _call("module_draft", arguments={"action": "finalize"}),
        _call("content_pack", arguments={"action": "activate"}),
    ]
    complete = [
        *bypassed[:3],
        _call("content_pack", arguments={"action": "import"}),
        bypassed[3],
    ]

    assert _coverage_audit(route, bypassed, process_count=1, list_changed_count=1)[
        "complete"
    ] is False
    assert _coverage_audit(route, complete, process_count=1, list_changed_count=1)[
        "complete"
    ] is True


def test_phase_transition_rejects_exposure_reopen_as_refresh() -> None:
    route = {"scenarios": []}
    calls = [
        _call("skill_query"),
        _call("exposure", arguments={"action": "open"}),
        _call("game_phase", arguments={"action": "set"}),
        _call("exposure", arguments={"action": "open"}),
    ]

    audit = _coverage_audit(route, calls, process_count=1, list_changed_count=1)

    assert "exposure:reopened_after_transition" in audit["gaps"]


def test_dynamic_inventory_is_the_only_source_of_runnable_units() -> None:
    future = {"campaign_line_id": "future-module"}
    assert _runnable_units({"coverage_units": [future]}) == [future]
    assert _runnable_units({"runnable_units": [future]}) == [future]
    assert _runnable_units({"disposition": {"runnable": [future]}}) == [future]


def test_agent_config_uses_fresh_home_current_skills_and_real_native_tools(tmp_path: Path) -> None:
    template = tmp_path / "template.json"
    template.write_text(
        json.dumps(
            {
                "agents": {"defaults": {}},
                "tools": {"mcp_servers": {"sagasmith_dnd": {"command": "server", "env": {}}}},
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(agent_config_template=template, module_root=[])
    path = _configure_agent(
        args,
        unit_dir=tmp_path,
        home=tmp_path / "home",
        agent_workspace=tmp_path / "workspace",
    )
    config = json.loads(path.read_text(encoding="utf-8"))
    server = config["tools"]["mcp_servers"]["sagasmith_dnd"]
    assert server["enabled_tools"] == ["*"]
    assert server["inject_principal"] is True
    assert server["env"]["SAGASMITH_DND_MCP_HOME"] == str((tmp_path / "home").resolve())
    assert config["agents"]["defaults"]["dream"]["enabled"] is False


def test_dm_prompt_contains_coverage_evidence_but_no_authored_story_outcome() -> None:
    route = {
        "evidence": [
            {
                "id": "ending",
                "source_sha256": "a" * 64,
                "heading_path": ["Conclusion"],
                "content_sha256": "b" * 64,
                "page_start": 10,
                "page_end": 10,
            }
        ],
        "scenarios": [
            {
                "id": "ending",
                "mechanisms": ["ending"],
                "ending_status": "legal_complete",
            }
        ],
    }
    prompt = _dm_prompt(
        run_id="run",
        line_id="module",
        unit={
            "module_paths": ["reference/module.pdf"],
            "module_sha256": ["c" * 64],
        },
        route=route,
        player_principal="player",
        cycle=1,
        gaps=[],
    )
    assert "Retrieve and expand the exact managed source before deciding" in prompt
    assert "dnd:full/references/skill-groups/lobby/modules-import.md" in prompt
    assert "A prior activation without a successful Pack import" in prompt
    source_path = str(
        (Path(__file__).resolve().parents[2] / "reference/module.pdf").resolve()
    )
    assert source_path.replace("\\", "\\\\") in prompt
    assert "coverage evidence and route intent, not a story answer" in prompt
    assert "never a campaign UUID" in prompt
    assert "Open exposure without a campaign" in prompt
    assert '"decision"' not in prompt
    assert '"outcome"' not in prompt


@pytest.mark.full_agent
@pytest.mark.skipif(
    os.environ.get("SAGASMITH_RUN_FULL_AGENT_CORPUS") != "1",
    reason="nightly/full real-provider corpus run",
)
def test_real_agent_corpus_single_command(tmp_path: Path) -> None:
    config = os.environ.get("SAGASMITH_AGENT_CONFIG_TEMPLATE")
    assert config, "SAGASMITH_AGENT_CONFIG_TEMPLATE is required for a full Agent run"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.regression_agent_corpus",
            "--output-dir",
            str(tmp_path / "full-agent-corpus"),
            "--agent-config-template",
            config,
            "--run-id",
            "pytest-full-agent-corpus",
            "--fail-fast",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
    )
    assert completed.returncode == 0
