from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.regression_agent_corpus import (
    _agent_failure_kind,
    _aggregate_transcripts,
    _configure_agent,
    _coverage_audit,
    _current_opposition_audit,
    _decision_timing,
    _decode_tool_content,
    _dm_prompt,
    _ending_prerequisite_audit,
    _execution_order_gaps,
    _final_campaign_state,
    _latest_combat_start_business_template,
    _mechanism_covered,
    _next_cycle,
    _player_ready,
    _process_artifacts,
    _read_tool_audit,
    _refresh_completed_report,
    _run_agent,
    _runnable_units,
    _source_opposition_evidence_audit,
    _tool_timeline,
)


def test_decision_timing_reports_observable_gaps_without_claiming_hidden_reasoning() -> None:
    records = [
        {
            "process_id": "run:dm:cycle-1",
            "recorded_at_unix": 100.0,
            "assistant_message": {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "mcp_sagasmith_dnd_module_draft"}}
                ],
            },
        },
        {
            "process_id": "run:dm:cycle-1",
            "recorded_at_unix": 187.0,
            "assistant_message": {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "mcp_sagasmith_dnd_module_query"}}
                ],
            },
        },
    ]

    timing = _decision_timing(records, principal="dm")["processes"][0]

    assert timing["decision_turns"] == 2
    assert timing["observable_span_seconds"] == 87.0
    assert timing["maximum_inter_turn_gap_seconds"] == 87.0
    assert timing["inter_turn_gaps_at_least_30_seconds"] == [
        {
            "seconds": 87.0,
            "after_tools": ["module_draft"],
            "before_tools": ["module_query"],
        }
    ]
    assert "not hidden chain-of-thought timing" in timing["attribution"]


def test_execution_order_follows_route_before_historical_audit_debt() -> None:
    route = {
        "scenarios": [
            {"id": "opening"},
            {"id": "fight"},
            {"id": "ending"},
        ]
    }
    assert _execution_order_gaps(
        [
            "exposure:reopened_after_transition",
            "ending:legal_ending_not_verified",
            "fight:combat",
            "fight:source_opposition_missing",
            "preparation:manifest_party_not_ready",
            "opening:npc_conversation",
        ],
        route,
    ) == [
        "preparation:manifest_party_not_ready",
        "opening:npc_conversation",
        "fight:source_opposition_missing",
        "fight:combat",
        "ending:legal_ending_not_verified",
        "exposure:reopened_after_transition",
    ]


def _call(
    tool: str,
    *,
    arguments: dict[str, object] | None = None,
    ok: bool = True,
    result: object | None = None,
    principal: str = "dm",
    error: str | None = None,
) -> dict[str, object]:
    return {
        "tool": tool,
        "arguments": arguments or {},
        "ok": ok,
        "result": result,
        "error": None if ok else error or "Error executing tool: revision conflict",
        "principal": principal,
    }


def _player_grants(principal: str = "cli:player") -> list[dict[str, object]]:
    return [
        _call(
            "access_grant",
            arguments={
                "scope": "campaign",
                "principal_id": principal,
                "payload": {"role": "player"},
            },
        ),
        _call(
            "access_grant",
            arguments={
                "scope": "actor",
                "principal_id": principal,
                "payload": {"actor_id": "pc-1", "can_control": True},
            },
        ),
    ]


def _ready_manifest_call() -> dict[str, object]:
    return _call(
        "playthrough_manifest",
        arguments={"action": "sync"},
        result={
            "manifest": {
                "status": "ready",
                "party": {
                    "selected_size": 1,
                    "members": [{"actor_id": "pc-1", "status": "active"}],
                },
            }
        },
    )


def _ready_pc_call() -> dict[str, object]:
    return _call(
        "character_query",
        arguments={"view": "get", "payload": {"character_id": "pc-1"}},
        result={
            "id": "pc-1",
            "campaign_id": "campaign-1",
            "character_type": "pc",
            "sheet": {
                "schema_version": 2,
                "ability_generation": {"method": "standard_array"},
                "progression": {
                    "level": 1,
                    "classes": [{"name": "Fighter", "level": 1, "hit_die": 10}],
                    "species": "Human",
                    "background": "Soldier",
                },
                "combat": {
                    "hp": {"value": 12, "max": 12, "temp": 0},
                    "hit_dice": {"d10": {"value": 1, "max": 1}},
                },
                "content": {
                    "selections": [
                        {"kind": "class", "artifact_id": "fighter"},
                        {"kind": "species", "artifact_id": "human"},
                        {"kind": "background", "artifact_id": "soldier"},
                    ]
                },
                "inventory": {"items": [{"id": "sword", "name": "Longsword"}]},
            },
            "notes": {
                "profile": {
                    "personality_traits": ["Steady"],
                    "ideals": ["Duty"],
                    "bonds": ["Company"],
                    "flaws": ["Stubborn"],
                }
            },
        },
    )


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


def test_process_sessions_are_aggregated_with_explicit_boundaries(tmp_path: Path) -> None:
    workspace = tmp_path / "agent"
    first = "run:module:dm:cycle-001"
    second = "run:module:dm:cycle-002"
    for session_id, content in ((first, '{"role":"user"}\n'), (second, '{"role":"assistant"}\n')):
        path = workspace / "sessions" / (
            base64.urlsafe_b64encode(session_id.encode()).decode().rstrip("=") + ".jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    target = tmp_path / "aggregate.jsonl"

    _aggregate_transcripts(workspace, [first, second, first], target)

    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert [
        row.get("session_id")
        for row in rows
        if row.get("record_type") == "session_boundary"
    ] == [first, second]


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

    audit = tmp_path / "artifacts" / "dm-tool-audit.jsonl"
    audit.parent.mkdir()
    audit.write_text(
        json.dumps({"process_id": "run:module:dm:cycle-007"}) + "\n",
        encoding="utf-8",
    )
    assert _next_cycle(tmp_path) == 8


def test_resume_reopens_completed_report_when_current_route_adds_receipt() -> None:
    old_route = {
        "scenarios": [
            {
                "id": "ending",
                "mechanisms": ["ending"],
                "ending_status": "legal_complete",
            }
        ]
    }
    calls = [
        _call("skill_query"),
        _call("exposure", arguments={"action": "open"}),
        _call(
            "playthrough_manifest",
            arguments={"action": "verify_ending"},
            result={"status": "completed", "achieved": True},
        ),
    ]
    report = {
        "route": old_route,
        "tool_timeline": calls,
        "agent_processes": [{"cycle": 1}],
        "tools_list_changed_observed": 1,
        "coverage": {"complete": True, "gaps": []},
    }
    new_route = copy.deepcopy(old_route)
    new_route["scenarios"][0]["ending_prerequisites"] = [
        {
            "id": "source-outcome",
            "receipt": "semantic_event",
            "fact_key": "ending.source-outcome",
        }
    ]

    refreshed = _refresh_completed_report(
        report,
        {"edition": "2014", "advancement_mode": "milestone"},
        new_route,
    )

    assert refreshed["route"] == new_route
    assert refreshed["coverage"]["complete"] is False
    assert "ending:ending" in refreshed["coverage"]["gaps"]
    assert "ending:legal_ending_not_verified" in refreshed["coverage"]["gaps"]


def test_agent_provider_overload_is_machine_readable(tmp_path: Path) -> None:
    process_dir = tmp_path / "process"
    process_dir.mkdir()
    stdout = process_dir / "cycle-004-regression-dm-module.stdout.txt"
    stderr = process_dir / "cycle-004-regression-dm-module.stderr.txt"
    stdout.write_text(
        "Error calling Codex (RuntimeError): server_is_overloaded",
        encoding="utf-8",
    )
    stderr.write_text(
        "Our servers are currently overloaded. Please try again later.",
        encoding="utf-8",
    )

    assert _agent_failure_kind(stdout.read_text(), stderr.read_text()) == (
        "provider_overloaded"
    )
    artifacts = _process_artifacts(tmp_path)
    assert artifacts == [
        {
            "principal": "regression-dm-module",
            "session_id": None,
            "cycle": 4,
            "returncode": 75,
            "failure_kind": "provider_overloaded",
            "stdout": str(stdout.resolve()),
            "stderr": str(stderr.resolve()),
            "tool_audit": None,
        }
    ]


def test_recovered_provider_overload_does_not_override_successful_response() -> None:
    stdout = """Using config: config.json

nanobot
Cycle completed and the authoritative state remains resumable.
"""
    stderr = """Codex API request failed: error_code=server_is_overloaded
LLM transient error (attempt 1/3), retrying in 1s: server_is_overloaded
"""

    assert _agent_failure_kind(stdout, stderr) is None


def test_terminal_provider_error_in_stderr_remains_machine_readable() -> None:
    assert (
        _agent_failure_kind("", "Error calling Codex: server_is_overloaded")
        == "provider_overloaded"
    )


def test_agent_runner_uses_prompt_file_for_windows_safe_long_messages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command: list[str] = []

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command.extend(args)
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    prompt = "source-backed prompt\n" * 5000
    args = argparse.Namespace(
        nanobot=tmp_path / "nanobot.exe",
        timeout_seconds=30,
        run_id="run",
    )
    unit_dir = tmp_path / "unit"

    result = _run_agent(
        args,
        config=tmp_path / "config.json",
        agent_workspace=tmp_path / "workspace",
        unit_dir=unit_dir,
        principal="regression-dm-module",
        session_id="run:module:dm:cycle-001",
        cycle=1,
        prompt=prompt,
        audit_path=tmp_path / "audit.jsonl",
    )

    prompt_path = unit_dir / "process" / "cycle-001-regression-dm-module.prompt.txt"
    assert result.returncode == 0
    assert "--message" not in command
    assert command[command.index("--message-file") + 1] == str(prompt_path.resolve())
    assert prompt_path.read_text(encoding="utf-8") == prompt


def test_player_starts_only_after_successful_actor_grant() -> None:
    principal = "regression-player-module"
    actor_grant = _call(
        "access_grant",
        arguments={
            "scope": "actor",
            "principal_id": f"cli:{principal}",
            "payload": {"actor_id": "actor-1", "can_control": True},
        },
    )
    campaign_grant = _call(
        "access_grant",
        arguments={
            "scope": "campaign",
            "principal_id": f"cli:{principal}",
            "payload": {"role": "player"},
        },
    )
    assert _player_ready([campaign_grant, actor_grant], principal_id=principal) is True
    assert _player_ready([actor_grant], principal_id=principal) is False
    assert _player_ready([campaign_grant], principal_id=principal) is False
    assert (
        _player_ready(
            [{**actor_grant, "ok": False}, campaign_grant],
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
                    "agent_semantic_spell_ruling",
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
    conversation_combat = {
        "campaign_id": "campaign-1",
        "positioning_mode": "agent",
        "participant_ids": ["pc-1", "enemy-1"],
        "expected_revision": 7,
        "idempotency_key": "conversation-probe",
    }
    calls = [
        _call("skill_query"),
        _call("exposure", arguments={"action": "open"}),
        _call(
            "campaign_query",
            arguments={"view": "resume"},
            result={
                "host_context_binding": {"branch_id": "source-branch"},
                "game_phase": "play",
            },
        ),
        _call("exposure", arguments={"action": "search"}),
        _call("exposure", arguments={"action": "set"}),
        _call("npc_conversation", arguments={"action": "open"}),
        _call("npc_conversation", arguments={"action": "ingest"}),
        _call("npc_conversation", arguments={"action": "publish"}),
        _call("npc_conversation", arguments={"action": "close"}),
        _call("character_check"),
        _call("npc_conversation", arguments={"action": "open"}),
        _call(
            "combat_start",
            arguments=conversation_combat,
            ok=False,
            error=(
                "Error executing tool combat_start: close or abort the active NPC "
                "conversation before starting combat"
            ),
        ),
        _call("npc_conversation", arguments={"action": "abort"}),
        _call(
            "character_create_from",
            arguments={"mode": "statblock"},
            result={
                "character": {"id": "enemy-1", "character_type": "monster"}
            },
        ),
        _call("content_solution", arguments={"action": "compile"}),
        _call(
            "combat_start",
            arguments={
                **conversation_combat,
                "expected_revision": 8,
                "idempotency_key": "conversation-retry",
            },
        ),
        _call("combat_query", arguments={"view": "render"}),
        _call("combat_cast_spell"),
        _call(
            "combat_choice",
            arguments={"action": "execute_plan"},
            result={"status": "committed"},
        ),
        _call("combat_end"),
        _call("chase", arguments={"action": "start"}),
        _call("combat_start", ok=False),
        _call("chase", arguments={"action": "end"}),
        _call(
            "combat_start",
            arguments={"positioning_mode": "agent", "participant_ids": ["pc-1", "enemy-1"]},
        ),
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
        _call(
            "campaign_query",
            arguments={"view": "resume"},
            result={
                "host_context_binding": {"branch_id": "source-branch"},
                "game_phase": "play",
            },
        ),
        *_player_grants(),
        _ready_manifest_call(),
        _ready_pc_call(),
        _call("campaign_query", principal="player"),
    ]
    audit = _coverage_audit(route, calls, process_count=4, list_changed_count=3)
    assert audit["complete"] is True
    assert audit["gaps"] == []


def test_chase_coverage_requires_successful_start_and_end_receipts() -> None:
    assert (
        _mechanism_covered(
            "chase",
            [_call("chase", arguments={"action": "query"}, result={"chase": None})],
        )
        is False
    )
    assert (
        _mechanism_covered(
            "chase",
            [
                _call("chase", arguments={"action": "start"}, ok=False),
                _call("chase", arguments={"action": "query"}, result={"chase": None}),
            ],
        )
        is False
    )
    assert (
        _mechanism_covered(
            "chase",
            [_call("chase", arguments={"action": "start"})],
        )
        is False
    )
    assert (
        _mechanism_covered(
            "chase",
            [
                _call("chase", arguments={"action": "start"}),
                _call("chase", arguments={"action": "end"}),
            ],
        )
        is True
    )


def test_chase_coverage_accepts_authoritative_automatic_terminal_receipt() -> None:
    caught = _call(
        "chase",
        arguments={"action": "take_turn"},
        result={
            "result": {
                "chase": {
                    "id": "chase-1",
                    "active": False,
                    "quarry_ids": ["quarry-1"],
                    "pursuer_ids": ["pc-1"],
                    "outcome": {"status": "caught"},
                }
            }
        },
    )
    calls = [
        _call("chase", arguments={"action": "start"}),
        caught,
    ]
    assert _mechanism_covered("chase", calls) is True
    assert _mechanism_covered("chase_to_combat", calls) is False
    assert (
        _mechanism_covered(
            "chase_to_combat",
            [*calls, _call("combat_start")],
        )
        is True
    )


def test_chase_coverage_rejects_read_only_or_unrelated_inactive_state() -> None:
    inactive = {
        "result": {
            "chase": {
                "id": "chase-1",
                "active": False,
                "quarry_ids": ["quarry-1"],
                "pursuer_ids": ["pc-1"],
                "outcome": {"status": "caught"},
            }
        }
    }
    assert (
        _mechanism_covered(
            "chase",
            [
                _call("chase", arguments={"action": "start"}),
                _call("chase", arguments={"action": "query"}, result=inactive),
            ],
        )
        is False
    )
    assert (
        _mechanism_covered(
            "chase",
            [
                _call("chase", arguments={"action": "start"}),
                _call(
                    "chase",
                    arguments={"action": "take_turn"},
                    result={"combat": {"id": "combat-1", "active": False}},
                ),
            ],
        )
        is False
    )


def test_coverage_accepts_paid_standard_agent_spell_clause() -> None:
    calls = [
        _call(
            "combat_cast_spell",
            arguments={
                "spell_id": "standard-darkness",
                "declaration": {
                    "agent_ruling": {
                        "default_resolver": "agent",
                        "ruling_kind": "generic_spell_effect",
                        "source_excerpt": "Exact persisted standard spell source excerpt.",
                    }
                },
            },
            result={
                "status": "committed",
                "result": {
                    "semantic_solution": {
                        "status": "agent_ruling_committed",
                        "payment_recorded": True,
                    }
                },
            },
        )
    ]
    assert _mechanism_covered("agent_semantic_spell_ruling", calls) is True


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


def test_ending_requires_independent_source_item_and_check_receipts() -> None:
    source_ref = {
        "module_id": "module-1",
        "scene_id": "ending-scene",
        "chunk_id": "ending-chunk",
        "content_sha256": "a" * 64,
        "page_start": 10,
        "page_end": 11,
        "heading_path": ["Chapter", "Ending"],
    }
    prerequisites = [
        {
            "id": "source-item-acquired",
            "receipt": "loot_acquire",
            "item_name": "Source Sword",
            "source_evidence": {
                "page_start": 10,
                "page_end": 11,
                "heading_path": ["Chapter", "Ending"],
            },
        },
        {
            "id": "ally-present",
            "receipt": "semantic_event",
            "fact_key": "ending.ally-present",
            "dc_reduction": 5,
        },
        {
            "id": "mentor-present",
            "receipt": "semantic_event",
            "fact_key": "ending.mentor-present",
            "dc_reduction": 5,
        },
        {
            "id": "persuasion-success",
            "receipt": "character_check",
            "skill": "Persuasion",
            "base_dc": 25,
            "applied_reducer_ids": ["ally-present", "mentor-present"],
            "dc": 15,
            "success": True,
        },
        {
            "id": "source-item-surrendered",
            "receipt": "item_spend",
            "item_name": "Source Sword",
            "source_evidence": {
                "page_start": 10,
                "page_end": 11,
                "heading_path": ["Chapter", "Ending"],
            },
        },
    ]
    route = {
        "scenarios": [
            {
                "id": "ending",
                "mechanisms": ["ending"],
                "positioning_mode": "agent",
                "audience": "player",
                "path": "normal",
                "ending_status": "legal_complete",
                "ending_prerequisites": prerequisites,
            }
        ]
    }
    self_certifying = [
        _call("memory_change", arguments={"action": "upsert"}),
        _call("module_set_progress"),
        _call(
            "playthrough_manifest",
            arguments={"action": "verify_ending"},
            result={"status": "completed", "achieved": True},
        ),
    ]
    audit = _coverage_audit(route, self_certifying, process_count=2, list_changed_count=1)
    assert "ending:ending" in audit["gaps"]
    assert "ending:legal_ending_not_verified" in audit["gaps"]
    receipt_audit = _ending_prerequisite_audit(route, self_certifying)
    assert receipt_audit[0]["first_missing_id"] == "source-item-acquired"
    assert receipt_audit[0]["receipts"][0]["status"] == "missing"
    assert receipt_audit[0]["receipts"][0]["expected"] == prerequisites[0]
    assert receipt_audit[0]["receipts"][0]["safe_source_query"] == "Ending"
    assert receipt_audit[0]["receipts"][1]["status"] == "blocked_by_prior"

    receipts = [
        _call(
            "campaign_change",
            arguments={
                "action": "loot_acquire",
                "payload": {
                    "items": [{"id": "source-sword", "name": "Source Sword"}],
                    "source_ref": source_ref,
                },
            },
            result={
                "status": "committed",
                "acquisition_id": "source-sword-acquisition",
                "items": [{"id": "source-sword", "name": "Source Sword"}],
                "item_ids": ["source-sword"],
                "party": {"inventory": {"items": [{"id": "source-sword", "name": "Source Sword"}]}},
            },
        ),
        _call(
            "memory_change",
            arguments={
                "action": "commit",
                "payload": {
                    "event": {
                        "event_type": "source_semantic_event",
                        "summary": "The ally is present for the appeal.",
                        "audience_scope": "party",
                        "payload": {
                            "reducer_id": "ally-present",
                            "source_ref": source_ref,
                        },
                    },
                    "facts": [
                        {
                            "fact_key": "ending.ally-present",
                            "content": "The ally is present for the appeal.",
                        }
                    ],
                },
            },
            result={
                "event": {"id": "event-ally", "event_type": "source_semantic_event"},
                "facts": [
                    {
                        "fact_key": "ending.ally-present",
                        "source_event_ids": ["event-ally"],
                    }
                ],
            },
        ),
        _call(
            "memory_change",
            arguments={
                "action": "commit",
                "payload": {
                    "event": {
                        "event_type": "source_semantic_event",
                        "summary": "The mentor is present for the appeal.",
                        "audience_scope": "party",
                        "payload": {
                            "reducer_id": "mentor-present",
                            "source_ref": source_ref,
                        },
                    },
                    "facts": [
                        {
                            "fact_key": "ending.mentor-present",
                            "content": "The mentor is present for the appeal.",
                        }
                    ],
                },
            },
            result={
                "event": {"id": "event-mentor", "event_type": "source_semantic_event"},
                "facts": [
                    {
                        "fact_key": "ending.mentor-present",
                        "source_event_ids": ["event-mentor"],
                    }
                ],
            },
        ),
        _call(
            "character_check",
            arguments={
                "action": "check",
                "payload": {
                    "skill": "Persuasion",
                    "dc": 15,
                    "source_scene_id": "ending-scene",
                    "source_excerpt": "The source requires a DC 25 Persuasion check.",
                },
            },
            result={
                "status": "committed",
                "result": {
                    "success": True,
                    "random_stream_receipt": {"operation": "character_check"},
                },
            },
        ),
        _call(
            "campaign_change",
            arguments={
                "action": "item_spend",
                "payload": {"item_id": "source-sword", "source_ref": source_ref},
            },
            result={
                "status": "committed",
                "removed": {"id": "source-sword", "name": "Source Sword"},
            },
        ),
        *self_certifying,
    ]
    audit = _coverage_audit(route, receipts, process_count=2, list_changed_count=1)
    assert "ending:ending" not in audit["gaps"]
    assert "ending:legal_ending_not_verified" not in audit["gaps"]
    receipt_audit = _ending_prerequisite_audit(route, receipts)
    assert receipt_audit[0]["first_missing_id"] is None
    assert receipt_audit[0]["ready_for_verification"] is True
    assert receipt_audit[0]["receipts"][0]["acquired_item_ids"] == ["source-sword"]
    assert [item["status"] for item in receipt_audit[0]["receipts"]] == [
        "matched"
    ] * len(prerequisites)

    contradictory_inventory_verify = copy.deepcopy(receipts)
    contradictory_inventory_verify[-1]["result"] = {
        "status": "completed",
        "achieved": True,
        "verification": [
            {
                "kind": "campaign_state_value",
                "path": "party.inventory.items",
                "operator": "truthy",
                "passed": True,
            }
        ],
    }
    audit = _coverage_audit(
        route,
        contradictory_inventory_verify,
        process_count=2,
        list_changed_count=1,
    )
    assert "ending:ending" in audit["gaps"]
    assert "ending:legal_ending_not_verified" in audit["gaps"]
    receipt_audit = _ending_prerequisite_audit(
        route, contradictory_inventory_verify
    )
    assert receipt_audit[0]["ready_for_verification"] is True
    assert receipt_audit[0]["contradictory_completed_verification"] is True

    cross_branch_receipts = copy.deepcopy(receipts)
    for call in cross_branch_receipts[:-1]:
        call["result"] = {
            "host_context_binding": {"branch_id": "branch-a"},
            "wrapped": call["result"],
        }
    cross_branch_receipts[-1]["result"] = {
        "host_context_binding": {"branch_id": "branch-b"},
        "wrapped": cross_branch_receipts[-1]["result"],
    }
    audit = _coverage_audit(
        route,
        cross_branch_receipts,
        process_count=2,
        list_changed_count=1,
    )
    assert "ending:ending" in audit["gaps"]
    receipt_audit = _ending_prerequisite_audit(route, cross_branch_receipts)
    assert receipt_audit[0]["current_branch_id"] == "branch-b"
    assert receipt_audit[0]["first_missing_id"] == prerequisites[0]["id"]

    audit = _coverage_audit(
        route,
        [*self_certifying, *receipts],
        process_count=2,
        list_changed_count=1,
    )
    assert "ending:ending" not in audit["gaps"]


    missing_reducer = [call for call in receipts if "event-mentor" not in str(call)]
    audit = _coverage_audit(route, missing_reducer, process_count=2, list_changed_count=1)
    assert "ending:ending" in audit["gaps"]

    naked_fact = [
        receipts[0],
        receipts[1],
        _call(
            "memory_change",
            arguments={"action": "upsert", "payload": {"fact_key": "ending.mentor-present"}},
        ),
        *receipts[3:],
    ]
    audit = _coverage_audit(route, naked_fact, process_count=2, list_changed_count=1)
    assert "ending:ending" in audit["gaps"]

    wrong_source = {**source_ref, "page_start": 12, "page_end": 12}
    wrong_source_receipts = [
        {
            **receipts[0],
            "arguments": {
                **receipts[0]["arguments"],
                "payload": {**receipts[0]["arguments"]["payload"], "source_ref": wrong_source},
            },
        },
        *receipts[1:],
    ]
    audit = _coverage_audit(
        route, wrong_source_receipts, process_count=2, list_changed_count=1
    )
    assert "ending:ending" in audit["gaps"]

    item_spend_index = next(
        index
        for index, call in enumerate(receipts)
        if call["tool"] == "campaign_change"
        and call["arguments"].get("action") == "item_spend"
    )
    different_item_spend = {
        **receipts[item_spend_index],
        "arguments": {
            **receipts[item_spend_index]["arguments"],
            "payload": {
                **receipts[item_spend_index]["arguments"]["payload"],
                "item_id": "replacement-sword",
            },
        },
        "result": {
            "status": "committed",
            "removed": {"id": "replacement-sword", "name": "Source Sword"},
        },
    }
    different_sword = [
        *receipts[:item_spend_index],
        different_item_spend,
        *receipts[item_spend_index + 1 :],
    ]
    audit = _coverage_audit(route, different_sword, process_count=2, list_changed_count=1)
    assert "ending:ending" in audit["gaps"]

    projected_same_name_is_not_minted = copy.deepcopy(different_sword)
    projected_same_name_is_not_minted[0]["result"]["party"]["inventory"][
        "items"
    ].append({"id": "replacement-sword", "name": "Source Sword"})
    receipt_audit = _ending_prerequisite_audit(route, projected_same_name_is_not_minted)
    assert receipt_audit[0]["receipts"][0]["acquired_item_ids"] == ["source-sword"]
    assert receipt_audit[0]["first_missing_id"] == "source-item-surrendered"

    valid_receipts = receipts[:5]
    restarted_receipts = copy.deepcopy(valid_receipts)
    for call in (restarted_receipts[0], restarted_receipts[-1]):
        call["arguments"]["payload"]["item_id"] = "replacement-sword"
        for node in call["result"].values():
            if isinstance(node, dict) and node.get("id") == "source-sword":
                node["id"] = "replacement-sword"
    restarted_receipts[0]["result"]["party"]["inventory"]["items"][0]["id"] = (
        "replacement-sword"
    )
    restarted_receipts[0]["result"]["items"][0]["id"] = "replacement-sword"
    restarted_receipts[0]["result"]["item_ids"] = ["replacement-sword"]
    stale_then_restarted = [
        valid_receipts[0],
        valid_receipts[-1],
        *restarted_receipts,
        self_certifying[-1],
    ]
    audit = _coverage_audit(
        route, stale_then_restarted, process_count=2, list_changed_count=1
    )
    assert "ending:ending" not in audit["gaps"]
    assert "ending:legal_ending_not_verified" not in audit["gaps"]

    partial_restart = [valid_receipts[0], valid_receipts[-1], restarted_receipts[0]]
    receipt_audit = _ending_prerequisite_audit(route, partial_restart)
    assert receipt_audit[0]["receipts"][0]["call_index"] == 2
    assert receipt_audit[0]["first_missing_id"] == "ally-present"

    prematurely_spent = [
        valid_receipts[0],
        valid_receipts[-1],
        *valid_receipts[1:4],
    ]
    receipt_audit = _ending_prerequisite_audit(route, prematurely_spent)
    assert receipt_audit[0]["receipts"][0]["status"] == "missing"
    assert receipt_audit[0]["first_missing_id"] == "source-item-acquired"

    reacquired_after_premature_spend = [
        *prematurely_spent,
        restarted_receipts[0],
    ]
    receipt_audit = _ending_prerequisite_audit(route, reacquired_after_premature_spend)
    assert receipt_audit[0]["receipts"][0]["call_index"] == len(
        reacquired_after_premature_spend
    ) - 1
    assert receipt_audit[0]["first_missing_id"] == "ally-present"

    longer_older_prefix = [valid_receipts[0], valid_receipts[1], restarted_receipts[0]]
    receipt_audit = _ending_prerequisite_audit(route, longer_older_prefix)
    assert receipt_audit[0]["receipts"][0]["call_index"] == 0
    assert receipt_audit[0]["receipts"][1]["call_index"] == 1
    assert receipt_audit[0]["first_missing_id"] == "mentor-present"


def test_completed_recovery_route_must_finish_on_source_branch_in_play() -> None:
    route = {
        "scenarios": [
            {
                "id": "ending",
                "mechanisms": ["ending"],
                "ending_status": "legal_complete",
            },
            {
                "id": "recovery",
                "mechanisms": [],
                "recovery_operations": ["branch_checkout"],
            },
        ]
    }
    source_binding = {
        "host_context_binding": {"branch_id": "source-branch"},
        "game_phase": "play",
    }
    restored_binding = {
        "host_context_binding": {"branch_id": "restore-branch"},
        "game_phase": "lobby",
    }
    common = [
        _call("campaign_create", result=source_binding),
        _call(
            "branch_change",
            arguments={"action": "checkout"},
            result=restored_binding,
        ),
        _call(
            "playthrough_manifest",
            arguments={"action": "verify_ending"},
            result={"status": "completed", "achieved": True},
        ),
        _call("campaign_query", arguments={"view": "resume"}, result=restored_binding),
    ]

    audit = _coverage_audit(route, common, process_count=2, list_changed_count=1)
    assert "final_state:source_branch_play_unverified" in audit["gaps"]

    settled = [
        *common,
        _call(
            "branch_change",
            arguments={"action": "checkout"},
            result=source_binding,
        ),
        _call(
            "game_phase",
            arguments={"action": "set", "phase": "play"},
            result=source_binding,
        ),
        _call("campaign_query", arguments={"view": "resume"}, result=source_binding),
    ]
    audit = _coverage_audit(route, settled, process_count=2, list_changed_count=1)
    assert "final_state:source_branch_play_unverified" not in audit["gaps"]


def test_final_campaign_state_skips_newer_binding_without_phase() -> None:
    source_binding = {
        "host_context_binding": {"branch_id": "source-branch"},
        "game_phase": "play",
    }
    calls = [
        _call("campaign_query", arguments={"view": "resume"}, result=source_binding),
        _call(
            "playthrough_manifest",
            arguments={"action": "get"},
            result={
                "manifest": {"status": "completed"},
                "host_context_binding": {"branch_id": "source-branch"},
            },
        ),
    ]

    assert _final_campaign_state(calls) == {
        "branch_id": "source-branch",
        "phase": "play",
    }


def test_conversation_combat_probe_requires_same_valid_retry_payload() -> None:
    base = {
        "campaign_id": "campaign-1",
        "participant_ids": ["pc-1", "npc-correct"],
        "positioning_mode": "agent",
        "expected_revision": 3,
        "idempotency_key": "probe",
    }
    active_error = (
        "Error executing tool combat_start: close or abort the active NPC conversation "
        "before starting combat"
    )
    invalid_probe = [
        _call("npc_conversation", arguments={"action": "open"}),
        _call(
            "combat_start",
            arguments={**base, "participant_ids": ["pc-1", "npc-typo"]},
            ok=False,
            error=active_error,
        ),
        _call("npc_conversation", arguments={"action": "abort"}),
        _call(
            "combat_start",
            arguments={**base, "expected_revision": 4, "idempotency_key": "retry"},
        ),
    ]
    assert _mechanism_covered("conversation_to_combat", invalid_probe) is False

    valid_probe = [
        _call("npc_conversation", arguments={"action": "open"}),
        _call("combat_start", arguments=base, ok=False, error=active_error),
        _call("npc_conversation", arguments={"action": "close"}),
        _call(
            "combat_start",
            arguments={**base, "expected_revision": 4, "idempotency_key": "retry"},
        ),
    ]
    assert _mechanism_covered("conversation_to_combat", valid_probe) is True


def test_npc_conversation_requires_complete_publication_sequence() -> None:
    assert (
        _mechanism_covered(
            "npc_conversation",
            [_call("npc_conversation", arguments={"action": "list"})],
        )
        is False
    )
    incomplete = [
        _call("npc_conversation", arguments={"action": "open"}),
        _call("npc_conversation", arguments={"action": "ingest"}),
        _call("npc_conversation", arguments={"action": "close"}),
    ]
    assert _mechanism_covered("npc_conversation", incomplete) is False
    complete = [
        _call("npc_conversation", arguments={"action": "open"}),
        _call("npc_conversation", arguments={"action": "ingest"}),
        _call("npc_conversation", arguments={"action": "publish"}),
        _call("npc_conversation", arguments={"action": "close"}),
    ]
    assert _mechanism_covered("npc_conversation", complete) is True


def test_latest_combat_start_template_uses_public_success_without_controls() -> None:
    calls = [
        _call(
            "combat_start",
            arguments={
                "campaign_id": "campaign-1",
                "participant_ids": ["pc-1", "npc-1"],
                "participant_manifest": {"groups": [{"actor_ids": ["npc-1"]}]},
                "expected_revision": 8,
                "idempotency_key": "start-1",
            },
            ok=False,
        ),
        _call(
            "combat_start",
            arguments={
                "campaign_id": "campaign-1",
                "participant_ids": ["pc-1", "npc-1"],
                "participant_manifest": {"groups": [{"actor_ids": ["npc-1"]}]},
                "expected_revision": 9,
                "idempotency_key": "start-2",
            },
        ),
    ]

    assert _latest_combat_start_business_template(calls) == {
        "campaign_id": "campaign-1",
        "participant_ids": ["pc-1", "npc-1"],
        "participant_manifest": {"groups": [{"actor_ids": ["npc-1"]}]},
    }


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
        *_player_grants(),
        _ready_manifest_call(),
        _ready_pc_call(),
    ]

    bypassed_audit = _coverage_audit(
        route, bypassed, process_count=1, list_changed_count=1
    )
    assert bypassed_audit["complete"] is False
    assert "preparation:player_membership_or_actor_grant_missing" in bypassed_audit["gaps"]
    assert "preparation:manifest_party_not_ready" in bypassed_audit["gaps"]
    assert _coverage_audit(route, complete, process_count=1, list_changed_count=1)[
        "complete"
    ] is True


def test_preparation_rejects_manifest_ready_skeletal_party() -> None:
    route = {"scenarios": []}
    skeletal = _call(
        "character_create_from",
        arguments={"mode": "build"},
        result={
            "instance": {
                "id": "pc-1",
                "character_type": "pc",
                "sheet": {
                    "schema_version": 2,
                    "ability_generation": {"method": "unrecorded"},
                    "progression": {"level": 0, "classes": [], "species": "", "background": ""},
                    "combat": {"hp": {"value": 0, "max": 0}, "hit_dice": {}},
                    "content": {"selections": []},
                    "inventory": {"items": []},
                },
                "notes": {"profile": {}},
            }
        },
    )
    calls = [
        _call("skill_query"),
        _call("exposure", arguments={"action": "open"}),
        *_player_grants(),
        skeletal,
        _ready_manifest_call(),
    ]

    audit = _coverage_audit(route, calls, process_count=1, list_changed_count=1)

    assert "preparation:manifest_party_not_ready" not in audit["gaps"]
    assert "preparation:party_mechanics_not_ready" in audit["gaps"]
    assert "ability_generation_incomplete" in audit["party_mechanical_gaps"]["pc-1"]
    assert "class_catalog_provenance_missing" in audit["party_mechanical_gaps"]["pc-1"]
    assert "starting_equipment_missing" in audit["party_mechanical_gaps"]["pc-1"]


def test_preparation_uses_highest_character_revision_across_principals() -> None:
    newest = _ready_pc_call()
    newest["result"]["revision"] = 11
    older_player_view = copy.deepcopy(newest)
    older_player_view["principal"] = "player"
    older_player_view["result"]["revision"] = 7
    older_player_view["result"]["sheet"]["inventory"]["items"] = []
    calls = [
        _call("skill_query"),
        _call("exposure", arguments={"action": "open"}),
        *_player_grants(),
        _ready_manifest_call(),
        newest,
        older_player_view,
    ]

    audit = _coverage_audit({"scenarios": []}, calls, process_count=2, list_changed_count=1)

    assert "preparation:party_mechanics_not_ready" not in audit["gaps"]
    assert audit["party_mechanical_gaps"] == {}


def test_preparation_reports_current_level_catalog_feature_shortfall() -> None:
    catalog = _call(
        "character_query",
        arguments={
            "view": "catalog",
            "payload": {"campaign_id": "campaign-1", "query": "Fighter"},
        },
        result=[
            {
                "id": "fighter-fighting-style",
                "kind": "feature",
                "name": "Fighting Style",
                "application_state": "selection_ready",
                "selection_requirements": {
                    "class_name": "Fighter",
                    "minimum_level": 1,
                    "fields": ["option"],
                },
            },
            {
                "id": "fighter-second-wind",
                "kind": "feature",
                "name": "Second Wind",
                "application_state": "selection_ready",
                "selection_requirements": {
                    "class_name": "Fighter",
                    "minimum_level": 1,
                    "fields": [],
                },
            },
            {
                "id": "fighter-action-surge",
                "kind": "feature",
                "name": "Action Surge",
                "application_state": "selection_ready",
                "selection_requirements": {
                    "class_name": "Fighter",
                    "minimum_level": 2,
                    "fields": [],
                },
            },
        ],
    )
    pc = _ready_pc_call()
    pc["result"]["sheet"]["content"]["features"] = [
        {"id": "fighter-fighting-style"}
    ]
    calls = [
        _call("skill_query"),
        _call("exposure", arguments={"action": "open"}),
        *_player_grants(),
        _ready_manifest_call(),
        catalog,
        pc,
    ]

    audit = _coverage_audit({"scenarios": []}, calls, process_count=1, list_changed_count=1)

    assert audit["party_mechanical_gaps"] == {
        "pc-1": ["class_feature_missing:fighter-second-wind"]
    }


def test_preparation_allows_campaign_party_to_grow_beyond_initial_selection() -> None:
    route = {"scenarios": []}
    extra_pc = _call(
        "character_create_from",
        arguments={"mode": "build"},
        result={
            "instance": {
                "id": "pc-2",
                "campaign_id": "campaign-1",
                "character_type": "pc",
                "sheet": {},
            }
        },
    )
    calls = [
        _call("skill_query"),
        _call("exposure", arguments={"action": "open"}),
        *_player_grants(),
        _ready_manifest_call(),
        _ready_pc_call(),
        extra_pc,
    ]

    audit = _coverage_audit(route, calls, process_count=1, list_changed_count=1)

    assert "preparation:extra_campaign_pcs_created" not in audit["gaps"]
    assert audit["campaign_pc_ids"] == ["pc-1", "pc-2"]


def test_preparation_requires_explicit_source_matching_campaign_profile() -> None:
    route = {"scenarios": []}
    shared = [
        _call("skill_query"),
        _call("exposure", arguments={"action": "open"}),
        *_player_grants(),
        _ready_manifest_call(),
        _ready_pc_call(),
    ]
    omitted = [*shared, _call("campaign_create", arguments={"name": "campaign"})]
    wrong_edition = [
        *shared,
        _call(
            "campaign_create",
            arguments={"name": "campaign", "edition": "2024", "advancement_mode": "xp"},
        ),
    ]
    wrong_advancement = [
        *shared,
        _call(
            "campaign_create",
            arguments={
                "name": "campaign",
                "edition": "2014",
                "advancement_mode": "milestone",
            },
        ),
    ]
    matching = [
        *shared,
        _call(
            "campaign_create",
            arguments={"name": "campaign", "edition": "2014", "advancement_mode": "xp"},
        ),
    ]

    for calls in (omitted, wrong_edition, wrong_advancement):
        audit = _coverage_audit(
            route,
            calls,
            process_count=1,
            list_changed_count=1,
            expected_edition="2014",
            expected_advancement_mode="xp",
        )
        assert "preparation:campaign_profile_unverified_or_mismatch" in audit["gaps"]
    assert "preparation:campaign_profile_unverified_or_mismatch" not in _coverage_audit(
        route,
        matching,
        process_count=1,
        list_changed_count=1,
        expected_edition="2014",
        expected_advancement_mode="xp",
    )["gaps"]


def test_combat_coverage_requires_a_non_party_participant() -> None:
    route = {
        "scenarios": [
            {"id": "fight", "mechanisms": ["combat", "combat_render"], "positioning_mode": "grid"}
        ]
    }
    calls = [
        _call("skill_query"),
        _call("exposure", arguments={"action": "open"}),
        *_player_grants(),
        _ready_manifest_call(),
        _call(
            "combat_start",
            arguments={"positioning_mode": "grid", "participant_ids": ["pc-1"]},
        ),
        _call("combat_query", arguments={"view": "render"}),
        _call("combat_end"),
    ]

    audit = _coverage_audit(route, calls, process_count=1, list_changed_count=1)

    assert "fight:combat" in audit["gaps"]
    assert "fight:combat_render" in audit["gaps"]
    assert "fight:positioning_mode:grid" in audit["gaps"]
    assert "fight:source_opposition_missing" in audit["gaps"]


def test_combat_coverage_requires_execution_inside_the_same_encounter() -> None:
    source_actor = _call(
        "character_create_from",
        arguments={"mode": "module_statblock"},
        result={
            "character": {"id": "monster-1", "character_type": "monster"},
            "statblock": {"source_identity": "TEST MONSTER"},
        },
    )
    start = _call(
        "combat_start",
        arguments={
            "positioning_mode": "grid",
            "participant_ids": ["pc-1", "monster-1"],
        },
    )
    prefix = [_ready_manifest_call(), source_actor, start]

    assert _mechanism_covered("combat", [*prefix, _call("combat_end")]) is False
    assert (
        _mechanism_covered(
            "combat_render",
            [
                *prefix,
                _call("combat_query", arguments={"view": "render"}),
                _call("combat_end"),
            ],
        )
        is False
    )
    assert (
        _mechanism_covered(
            "combat",
            [
                *prefix,
                _call("combat_cast_spell", result={"status": "pending_ruling"}),
                _call("combat_end"),
            ],
        )
        is False
    )

    executed = [
        *prefix,
        _call("combat_query", arguments={"view": "render"}),
        _call("combat_resolve_attack"),
        _call("combat_end"),
    ]
    assert _mechanism_covered("combat", executed) is True
    assert _mechanism_covered("combat_render", executed) is True

    committed_spell = [
        *prefix,
        _call("combat_cast_spell", result={"status": "committed"}),
        _call("combat_end"),
    ]
    assert _mechanism_covered("combat", committed_spell) is True

    different_encounter = [
        *prefix,
        _call("combat_end"),
        _call("combat_resolve_attack"),
        _call("combat_query", arguments={"view": "render"}),
        _call("combat_end"),
    ]
    assert _mechanism_covered("combat", different_encounter) is False


def test_combat_coverage_requires_every_source_expected_group() -> None:
    excerpt = (
        "Nezznar the Black Spider is joined by four giant spiders that defend "
        "their master to the death."
    )
    route = {
        "scenarios": [
            {
                "id": "fight",
                "mechanisms": ["combat"],
                "positioning_mode": "grid",
                "initial_source_groups": [
                    {
                        "role": "combatant",
                        "required_count": 1,
                        "source_excerpt": excerpt,
                        "statblock_source_identity": "NEZZNAR THE BLACK SPIDER",
                    },
                    {
                        "role": "combatant",
                        "required_count": 4,
                        "source_excerpt": excerpt,
                        "statblock_source_identity": "GIANT SPIDER",
                    },
                ],
            }
        ]
    }
    source_actors = [
        _call(
            "character_create_from",
            arguments={"mode": "module_statblock"},
            result={
                "character": {
                    "id": actor_id,
                    "character_type": "monster",
                },
                "statblock": {
                    "source_identity": (
                        "NEZZNAR THE BLACK SPIDER"
                        if actor_id == "nezznar"
                        else "GIANT SPIDER"
                    )
                },
            },
        )
        for actor_id in ("nezznar", "spider-1", "spider-2", "spider-3", "spider-4")
    ]
    shared = [
        _call("skill_query"),
        _call("exposure", arguments={"action": "open"}),
        *_player_grants(),
        _ready_manifest_call(),
        *source_actors,
    ]
    incomplete = [
        *shared,
        _call(
            "combat_start",
            arguments={
                "positioning_mode": "grid",
                "participant_ids": ["pc-1", "nezznar"],
                "participant_manifest": {
                    "groups": [
                        {
                            "role": "combatant",
                            "required_count": 1,
                            "actor_ids": ["nezznar"],
                            "source_excerpt": excerpt,
                        }
                    ]
                },
            },
        ),
        _call("combat_end"),
    ]
    complete_ids = ["nezznar", "spider-1", "spider-2", "spider-3", "spider-4"]
    complete = [
        *shared,
        _call(
            "combat_start",
            arguments={
                "positioning_mode": "grid",
                "participant_ids": ["pc-1", *complete_ids],
                "participant_manifest": {
                    "groups": [
                        {
                            "role": "combatant",
                            "required_count": 1,
                            "actor_ids": ["nezznar"],
                            "source_excerpt": excerpt,
                        },
                        {
                            "role": "combatant",
                            "required_count": 4,
                            "actor_ids": complete_ids[1:],
                            "source_excerpt": excerpt,
                        },
                    ]
                },
            },
        ),
        _call("combat_end"),
    ]

    assert "fight:source_opposition_missing" in _coverage_audit(
        route, incomplete, process_count=1, list_changed_count=1
    )["gaps"]
    assert "fight:source_opposition_missing" not in _coverage_audit(
        route, complete, process_count=1, list_changed_count=1
    )["gaps"]


def test_source_opposition_audit_exposes_exact_excerpt_mismatch() -> None:
    route = {
        "scenarios": [
            {
                "id": "fight",
                "positioning_mode": "grid",
                "initial_source_groups": [
                    {
                        "subject": "Flennis",
                        "role": "combatant",
                        "required_count": 1,
                        "source_excerpt": "The managed source text.",
                    }
                ],
            }
        ]
    }
    calls = [
        _call(
            "combat_start",
            arguments={
                "positioning_mode": "grid",
                "idempotency_key": "start",
                "participant_manifest": {
                    "groups": [
                        {
                            "label": "Flennis",
                            "role": "combatant",
                            "required_count": 1,
                            "actor_ids": ["flennis"],
                            "source_excerpt": "The corrupted Pack text.",
                        }
                    ]
                },
            },
        )
    ]

    audit = _source_opposition_evidence_audit(route, calls)

    assert audit == [
        {
            "scenario_id": "fight",
            "latest_successful_start_key": "start",
            "groups": [
                {
                    "subject": "Flennis",
                    "expected_source_excerpt": "The managed source text.",
                    "actual_source_excerpt": "The corrupted Pack text.",
                    "exact_excerpt_match": False,
                    "actual_actor_ids": ["flennis"],
                }
            ],
        }
    ]


def test_current_opposition_audit_refreshes_changed_route_evidence() -> None:
    route = {
        "scenarios": [
            {
                "id": "fight",
                "initial_source_groups": [
                    {"subject": "Flennis", "source_excerpt": "Current source text."}
                ],
            }
        ]
    }
    historical = [
        {
            "scenario_id": "fight",
            "groups": [
                {
                    "subject": "Flennis",
                    "expected_source_excerpt": "Superseded fixture text.",
                    "actual_source_excerpt": "Current source text.",
                    "exact_excerpt_match": False,
                }
            ],
        }
    ]

    current = _current_opposition_audit(route, historical)

    assert current[0]["groups"] == [
        {
            "subject": "Flennis",
            "expected_source_excerpt": "Current source text.",
            "actual_source_excerpt": "Current source text.",
            "exact_excerpt_match": True,
            "historical_expected_source_excerpt": "Superseded fixture text.",
            "route_evidence_changed": True,
        }
    ]


def test_combat_coverage_requires_source_backed_variant() -> None:
    excerpt = "Use swarm of rats statistics, replacing Beast with Undead."
    route = {
        "scenarios": [
            {
                "id": "fight",
                "mechanisms": ["combat"],
                "positioning_mode": "grid",
                "initial_source_groups": [
                    {
                        "role": "combatant",
                        "required_count": 1,
                        "source_excerpt": excerpt,
                        "statblock_source_identity": "Swarm of Rats",
                        "required_variant": {"creature_type": "undead"},
                        "variant_source_kind": "module-chunk",
                    }
                ],
            }
        ]
    }
    start = _call(
        "combat_start",
        arguments={
            "positioning_mode": "grid",
            "participant_ids": ["pc-1", "rats"],
            "participant_manifest": {
                "groups": [
                    {
                        "role": "combatant",
                        "required_count": 1,
                        "actor_ids": ["rats"],
                        "source_excerpt": excerpt,
                    }
                ]
            },
        },
    )
    actor = _call(
        "character_create_from",
        arguments={"mode": "statblock"},
        result={
            "character": {"id": "rats", "character_type": "monster"},
            "statblock": {"source_identity": "Swarm of Rats"},
        },
    )
    shared = [
        _call("skill_query"),
        _call("exposure", arguments={"action": "open"}),
        *_player_grants(),
        _ready_manifest_call(),
    ]

    missing_variant = [*shared, actor, start, _call("combat_end")]
    assert "fight:source_opposition_missing" in _coverage_audit(
        route, missing_variant, process_count=1, list_changed_count=1
    )["gaps"]

    actor["result"]["variant"] = {
        "source_ref": "module-chunk:d13",
        "creature_type": "undead",
    }
    actor["result"]["variant_evidence"] = {"kind": "module-chunk", "id": "d13"}
    complete = [*shared, actor, start, _call("combat_end")]
    assert "fight:source_opposition_missing" not in _coverage_audit(
        route, complete, process_count=1, list_changed_count=1
    )["gaps"]


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


def test_rejected_exposure_reopen_attempt_after_transition_is_audit_debt() -> None:
    route = {"scenarios": []}
    calls = [
        _call("skill_query"),
        _call("exposure", arguments={"action": "open"}),
        _call("branch_change", arguments={"action": "checkout"}),
        {
            **_call("exposure", arguments={"action": "open"}),
            "ok": False,
            "error": "exposure is already bound; retain it and use search/set",
        },
    ]

    audit = _coverage_audit(route, calls, process_count=1, list_changed_count=1)

    assert "exposure:reopened_after_transition" in audit["gaps"]


def test_new_agent_process_may_cold_start_exposure_after_prior_transition() -> None:
    route = {"scenarios": []}
    calls = [
        {**_call("skill_query"), "process_id": "process-1"},
        {
            **_call("exposure", arguments={"action": "open"}),
            "process_id": "process-1",
        },
        {
            **_call("game_phase", arguments={"action": "set"}),
            "process_id": "process-1",
        },
        {**_call("skill_query"), "process_id": "process-2"},
        {
            **_call("exposure", arguments={"action": "open"}),
            "process_id": "process-2",
        },
    ]

    audit = _coverage_audit(route, calls, process_count=2, list_changed_count=2)

    assert "exposure:reopened_after_transition" not in audit["gaps"]


def test_first_exposure_open_may_follow_core_phase_selection() -> None:
    route = {"scenarios": []}
    calls = [
        {**_call("skill_query"), "process_id": "process-1"},
        {
            **_call("game_phase", arguments={"action": "set"}),
            "process_id": "process-1",
        },
        {
            **_call("exposure", arguments={"action": "open"}),
            "process_id": "process-1",
        },
    ]

    audit = _coverage_audit(route, calls, process_count=1, list_changed_count=1)

    assert "exposure:reopened_after_transition" not in audit["gaps"]


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
    assert server["expose_resources_and_prompts"] is False
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
            "edition": "2014",
            "advancement_mode": "xp",
            "play_requirements": {
                "recommended_party_size": {
                    "status": "source_confirmed",
                    "minimum": 4,
                    "maximum": 5,
                    "selected": 5,
                },
                "starting_level": {"selected": 1},
            },
        },
        route=route,
        player_principal="player",
        cycle=1,
        gaps=[],
        ending_prerequisite_audit=[
            {
                "scenario_id": "ending",
                "first_missing_id": "source-item-presented",
                "ready_for_verification": False,
                "receipts": [
                    {
                        "id": "source-item-acquired",
                        "receipt": "loot_acquire",
                        "status": "matched",
                        "acquired_item_ids": ["source-sword"],
                    },
                    {
                        "id": "source-item-presented",
                        "receipt": "semantic_event",
                        "status": "missing",
                        "expected": {
                            "id": "source-item-presented",
                            "receipt": "semantic_event",
                            "fact_key": "ending.source-item-presented",
                        },
                        "safe_source_query": "Conclusion",
                    }
                ],
            }
        ],
        party_mechanical_gaps={
            "pc-1": ["class_feature_missing:fighter-second-wind"]
        },
    )
    assert "Retrieve and expand the exact managed source before deciding" in prompt
    assert "dnd:full/references/skill-groups/lobby/modules-import.md" in prompt
    assert "A prior activation without a successful Pack import" in prompt
    source_path = str(
        (Path(__file__).resolve().parents[2] / "reference/module.pdf").resolve()
    )
    assert source_path.replace("\\", "\\\\") in prompt
    assert "coverage evidence and route intent, not a story answer" in prompt
    assert "Do not reduce or omit a group to make preflight pass" in prompt
    assert "A prefix-only asset read is not proof" in prompt
    assert "dnd:full/skills/dnd-dm/references/OPPOSITION_HYDRATION.md" in prompt
    assert "read the focused" in prompt
    assert "localized-canonical-source sequence" in prompt
    assert "required_variant" in prompt
    assert "variant_source_kind" in prompt
    assert "scenario/evidence id is never a chunk id" in prompt
    assert "statblock_evidence" in prompt
    assert "Pack-local stable slot" in prompt
    assert "An empty candidate list is" in prompt
    assert "never a campaign UUID" in prompt
    assert "Open exposure without a campaign" in prompt
    assert 'explicit `edition="2014"`' in prompt
    assert '`advancement_mode="xp"`' in prompt
    assert '"selected": 5' in prompt
    assert "source minimum/maximum are advisory only" in prompt
    assert "Never change or block that selection" in prompt
    assert "re-resolve its exact current Pack evidence" in prompt
    assert "genuinely proven Pack-only gaps" in prompt
    assert "runtime manifest condition or unmet receipt is not missing Pack content" in prompt
    assert "multi-volume campaign's managed next volume" in prompt
    assert "Never\nretry the already-active earlier volume" in prompt
    assert "Do not send `filters` on that first lookup" in prompt
    assert "retry the minimal shape" in prompt
    assert "exact `payload.chunk_ids` (never" in prompt
    assert "Never guess a review id" in prompt
    assert "module_set_progress` is only narrative progress metadata" in prompt
    assert '`character_create_from`' in prompt
    assert "compare any active encounter's immutable participants" in prompt
    assert 'read each required actor individually with `view="get"`' in prompt
    assert '`outcome.status="interrupted"`' in prompt
    assert "`combat_end_turn` only passes one actor's turn" in prompt
    assert "do not grind irrelevant turns" in prompt
    assert "every remaining Combat-specific mechanism are already" in prompt
    assert "at least one successful" in prompt
    assert "`combat_start` alone never" in prompt
    assert "`action.context.spatial_facts`" in prompt
    assert "`action.spatial_facts`" in prompt
    assert "`target_can_see_attacker`" in prompt
    assert "A `pending_ruling` response" in prompt
    assert "Never guess or cache" in prompt
    assert '`module_draft(action="get")` with no payload' in prompt
    assert "matching unfinished job and preserve its public ids" in prompt
    assert "participant excerpt is" in prompt
    assert "mechanical statblock" in prompt
    assert "identical statblock review cannot fix" in prompt
    assert "latest_successful_combat_start_business_template=" in prompt
    assert "current_ending_prerequisite_receipt_audit=" in prompt
    assert '`payload={"spend_id": <new stable id>' in prompt
    assert '"item_id": <the exact matched acquisition item id>' in prompt
    assert "do not\nput `excerpt`" in prompt
    assert "never\nreuse a spend id from a rejected attempt" in prompt
    assert "MANDATORY_FIRST_ENDING_MUTATION=" in prompt
    assert '"tool": "memory_change"' in prompt
    assert '"action": "commit"' in prompt
    assert "do not\ncall `playthrough_manifest` except when it is the named" in prompt
    assert "historical completed\nmanifest status" in prompt
    assert "follow its full `expected` object" in prompt
    assert "Never reuse the\npreceding acquisition's source reference" in prompt
    assert "`ready_for_verification=false`" in prompt
    assert "do not call\n`playthrough_manifest(verify_ending)`" in prompt
    assert "first authoritative write of the cycle must be the exact" in prompt
    assert "read-only manifest verification is not progress" in prompt
    assert "never reacquire the item to satisfy that condition" in prompt
    assert "Configure a\nnew condition id" in prompt
    assert "`payload.source_scene_id` and `payload.source_excerpt`" in prompt
    assert "nested `payload.source_evidence` object does not satisfy" in prompt
    assert "`payload.base_dc`" in prompt
    assert "`payload.applied_reducer_ids`" in prompt
    assert '`payload.ability="Charisma"`' in prompt
    assert '`payload.skill="Persuasion"`' in prompt
    assert '`ability="persuasion"` with the skill omitted' in prompt
    assert '`payload.event.audience_scope="party"`' in prompt
    assert "audience does not belong\non a fact" in prompt
    assert '`payload.facts=[{"kind":"memory_fact","fact_key":"...", ...}]`' in prompt
    assert "singular\n`payload.fact`" in prompt
    assert "`facts=[]`" in prompt
    assert "Campaign memory fact `content` is always a string" in prompt
    assert "`fact.content` exactly" in prompt
    assert '"expected_revision_id": <fact.revision_id>' in prompt
    assert "that is\nthe first executable action" in prompt
    assert "use `module_search` and `module_expand`" in prompt
    assert "manifest's current conclusion source" in prompt
    assert "machine-generated `safe_source_query` verbatim" in prompt
    assert "mismatched sources remain negative evidence" in prompt
    assert "Do not retype identifiers" in prompt
    assert "class_feature_missing:fighter-second-wind" in prompt
    assert "load `character_content_apply`" in prompt
    assert "same parallel tool batch as an `exposure(set)`" in prompt
    assert "`tools/list_changed`, refresh the native list" in prompt
    assert "context-barrier rebuild may replay" in prompt
    assert "that replay is not a new session" in prompt
    assert "never call `exposure(open)` for that refresh" in prompt
    assert "do not repeat that operation" in prompt
    assert "controlled negative invariant probe" in prompt
    assert "fail specifically because the conversation is active" in prompt
    assert "an unrelated" in prompt
    assert "Its payload uses `members`, not `actor_ids`" in prompt
    assert "actually available, source-bound noncombat activity" in prompt
    assert '"decision"' not in prompt
    assert '"outcome"' not in prompt


def test_dm_prompt_generates_fresh_item_spend_write_ids() -> None:
    prompt = _dm_prompt(
        run_id="run",
        line_id="module",
        unit={"edition": "2014", "advancement_mode": "xp"},
        route={"scenarios": []},
        player_principal="player",
        cycle=42,
        gaps=["ending:ending"],
        ending_prerequisite_audit=[
            {
                "scenario_id": "ending",
                "first_missing_id": "source-item-surrendered",
                "ready_for_verification": False,
                "receipts": [
                    {
                        "id": "source-item-acquired",
                        "receipt": "loot_acquire",
                        "status": "matched",
                        "acquired_item_ids": ["source-sword"],
                    },
                    {
                        "id": "source-item-surrendered",
                        "receipt": "item_spend",
                        "status": "missing",
                        "expected": {
                            "id": "source-item-surrendered",
                            "receipt": "item_spend",
                            "item_name": "Source Sword",
                        },
                    }
                ],
            }
        ],
    )
    assert '"idempotency_key": "run-module-cycle-042-source-item-surrendered"' in prompt
    assert '"spend_id": "run-module-cycle-042-source-item-surrendered-spend"' in prompt
    assert '"matched_acquisition_item_ids": ["source-sword"]' in prompt
    assert "copy those exact fresh values" in prompt
    assert "do not derive either from the\nfixture receipt id" in prompt
    assert "never choose\nanother same-named item" in prompt


def test_dm_prompt_makes_replacement_ending_condition_the_first_write() -> None:
    prompt = _dm_prompt(
        run_id="run",
        line_id="module",
        unit={"edition": "2014", "advancement_mode": "xp"},
        route={"scenarios": []},
        player_principal="player",
        cycle=46,
        gaps=["ending:ending", "ending:legal_ending_not_verified"],
        ending_prerequisite_audit=[
            {
                "scenario_id": "ending",
                "first_missing_id": None,
                "ready_for_verification": True,
                "contradictory_completed_verification": True,
                "receipts": [],
            }
        ],
    )
    mandatory = prompt.split("MANDATORY_FIRST_ENDING_MUTATION=", 1)[1].splitlines()[0]
    assert '"tool": "playthrough_manifest"' in mandatory
    assert '"action": "configure_ending"' in mandatory
    assert '"require_new_condition_id": true' in mandatory
    assert '"verify_ending", "loot_acquire"' in mandatory
    assert "execute its named tool/action as the first" in prompt


def test_dm_prompt_recovers_immutable_invalid_ending_on_a_new_branch() -> None:
    prompt = _dm_prompt(
        run_id="run",
        line_id="module",
        unit={"edition": "2014", "advancement_mode": "xp"},
        route={"scenarios": []},
        player_principal="player",
        cycle=48,
        gaps=["ending:ending", "ending:legal_ending_not_verified"],
        ending_prerequisite_audit=[
            {
                "scenario_id": "ending",
                "first_missing_id": None,
                "ready_for_verification": True,
                "contradictory_completed_verification": True,
                "replacement_blocked_by_completed_manifest": True,
                "receipts": [],
            }
        ],
        initial_source_branch_id="source-branch",
        latest_source_snapshot={
            "id": "source-snapshot",
            "slot": 7,
            "label": "source-before-invalid-ending",
            "branch_id": "source-branch",
        },
    )
    mandatory = prompt.split("MANDATORY_FIRST_ENDING_MUTATION=", 1)[1].splitlines()[0]
    assert '"tool": "branch_change"' in mandatory
    assert '"action": "create"' in mandatory
    assert '"source_branch_id": "source-branch"' in mandatory
    assert '"from_snapshot_id": "source-snapshot"' in mandatory
    assert '"from_snapshot_slot": 7' in mandatory
    assert '"checkout": true' in mandatory
    assert "copy its exact non-empty\n`from_snapshot_id`" in prompt
    assert "does not accept\n`source_branch_id`" in prompt
    assert "verify the selected source-branch snapshot" in prompt
    assert "Never mutate or delete the immutable" in prompt


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
