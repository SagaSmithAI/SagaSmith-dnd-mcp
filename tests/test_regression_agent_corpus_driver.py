from __future__ import annotations

import argparse
import base64
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
    _decode_tool_content,
    _dm_prompt,
    _execution_order_gaps,
    _mechanism_covered,
    _next_cycle,
    _player_ready,
    _process_artifacts,
    _read_tool_audit,
    _runnable_units,
    _tool_timeline,
)


def test_execution_order_places_prerequisites_before_historical_audit_debt() -> None:
    assert _execution_order_gaps(
        [
            "exposure:reopened_after_transition",
            "ending:legal_ending_not_verified",
            "fight:combat",
            "fight:source_opposition_missing",
            "preparation:manifest_party_not_ready",
        ]
    ) == [
        "preparation:manifest_party_not_ready",
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
) -> dict[str, object]:
    return {
        "tool": tool,
        "arguments": arguments or {},
        "ok": ok,
        "result": result,
        "error": None if ok else "Error executing tool: revision conflict",
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
            arguments={"positioning_mode": "agent", "participant_ids": ["pc-1", "enemy-1"]},
        ),
        _call("combat_cast_spell"),
        _call("combat_choice", arguments={"action": "execute_plan"}),
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
        *_player_grants(),
        _ready_manifest_call(),
        _ready_pc_call(),
        _call("campaign_query", principal="player"),
    ]
    audit = _coverage_audit(route, calls, process_count=4, list_changed_count=3)
    assert audit["complete"] is True
    assert audit["gaps"] == []


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


def test_preparation_rejects_extra_campaign_pc_builds() -> None:
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

    assert "preparation:extra_campaign_pcs_created" in audit["gaps"]
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
    assert "never a campaign UUID" in prompt
    assert "Open exposure without a campaign" in prompt
    assert 'explicit `edition="2014"`' in prompt
    assert '`advancement_mode="xp"`' in prompt
    assert '"selected": 5' in prompt
    assert "re-resolve its exact current Pack evidence" in prompt
    assert "start an explicit new draft/version from the same managed source" in prompt
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
    assert "do not spend actions" in prompt
    assert '`module_draft(action="get")` with no payload' in prompt
    assert "matching unfinished job and preserve its public ids" in prompt
    assert "same parallel tool batch as an `exposure(set)`" in prompt
    assert "`tools/list_changed`, refresh the native list" in prompt
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
