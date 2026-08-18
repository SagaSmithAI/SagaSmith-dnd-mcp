from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from scripts.regression_corpus import (
    CORE_TOOLS,
    _build_coverage_matrix,
    _coverage_routes,
    _declared_records,
    _pack_record,
    _raw_records,
)


def _complete_route(line_id: str, source_sha256: str) -> dict[str, object]:
    evidence = {
        "id": "source",
        "source_sha256": source_sha256,
        "heading_path": ["Chapter 1", "Ending"],
        "content_sha256": "a" * 64,
        "page_start": 1,
        "page_end": 2,
    }
    shared_mechanisms = [
        "preparation",
        "play_scene",
        "noncombat_check",
        "resource_settlement",
        "npc_conversation",
        "conversation_to_mechanic",
        "conversation_to_combat",
        "combat",
        "combat_render",
        "ending",
        "save_restore",
        "idempotent_retry",
        "revision_conflict_refresh",
        "phase_exposure_refresh",
    ]
    return {
        "campaign_line_id": line_id,
        "evidence": [evidence],
        "scenarios": [
            {
                "id": "normal-grid-dm",
                "chapter_or_scene": "Chapter 1",
                "evidence_id": "source",
                "mechanisms": shared_mechanisms,
                "positioning_mode": "grid",
                "audience": "dm",
                "path": "normal",
                "ending_status": "legal_complete",
            },
            {
                "id": "recovery-agent-player",
                "chapter_or_scene": "Ending",
                "evidence_id": "source",
                "mechanisms": [],
                "positioning_mode": "agent",
                "audience": "player",
                "path": "recovery",
                "recovery_operations": [
                    "process_restart",
                    "snapshot_restore",
                    "branch_checkout",
                    "undo_redo",
                ],
            },
        ],
    }


def test_native_cold_start_contract_matches_current_core_tools() -> None:
    assert CORE_TOOLS == {
        "campaign_query",
        "exposure",
        "game_phase",
        "resolution_presentation",
        "server_capabilities",
        "skill_query",
        "storage_status",
    }


def test_declared_campaign_lines_are_data_driven(tmp_path: Path) -> None:
    campaign_root = tmp_path / "reference" / "DnD-Books" / "5e" / "Campaign"
    campaign_root.mkdir(parents=True)
    source = campaign_root / "New Adventure.md"
    source.write_text("# New Adventure\n", encoding="utf-8")
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "edition": "2014",
        "campaign_lines": [
            {
                "id": "new-adventure",
                "title": "New Adventure",
                "play_requirements": {"advancement": {"selected": "xp"}},
                "modules": [
                    {
                        "path": "New Adventure.md",
                        "role": "primary_campaign",
                        "size": source.stat().st_size,
                        "sha256": checksum,
                    }
                ],
                "player_materials": [],
                "assets": [],
            }
        ],
    }
    manifest_path = tmp_path / "corpus.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    records, units = _declared_records(manifest_path, tmp_path)

    assert [unit["id"] for unit in units] == ["new-adventure"]
    assert units[0]["module_sha256"] == [checksum]
    assert units[0]["edition"] == "2014"
    assert units[0]["advancement_mode"] == "xp"
    assert units[0]["play_requirements"] == {"advancement": {"selected": "xp"}}
    assert records[0]["checksum_valid"] is True
    assert records[0]["disposition"] == "runnable"


def test_unknown_raw_source_is_reported_pending(tmp_path: Path) -> None:
    source = tmp_path / "surprise-new-module.md"
    source.write_text("# A new candidate\n", encoding="utf-8")

    records = _raw_records([tmp_path], tmp_path, {}, set())

    assert records == [
        {
            "source_kind": "raw_source",
            "path": "surprise-new-module.md",
            "sha256": records[0]["sha256"],
            "size": source.stat().st_size,
            "classification": "unreviewed",
            "system_id": None,
            "edition": None,
            "advancement_mode": None,
            "disposition": "pending",
            "reason_code": "unreviewed_source_candidate",
            "campaign_line_id": None,
            "title": "surprise-new-module",
        }
    ]


def test_raw_source_decision_carries_play_requirements(tmp_path: Path) -> None:
    source = tmp_path / "reviewed-adventure.md"
    source.write_text("# Reviewed adventure\n", encoding="utf-8")
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    requirements = {
        "recommended_party_size": {"minimum": 4, "maximum": 6, "selected": 4},
        "advancement": {"selected": "milestone"},
    }

    records = _raw_records(
        [tmp_path],
        tmp_path,
        {
            checksum: {
                "classification": "adventure_module",
                "system_id": "dnd5e",
                "edition": "2014",
                "advancement_mode": "milestone",
                "play_requirements": requirements,
                "disposition": "runnable_installed_pack_required",
                "campaign_line_id": "reviewed-adventure",
            }
        },
        set(),
    )

    assert records[0]["advancement_mode"] == "milestone"
    assert records[0]["play_requirements"] == requirements


def test_unfinalized_module_pack_is_not_treated_as_runnable(tmp_path: Path) -> None:
    package = tmp_path / "module.sagasmith-pack"
    descriptor = {
        "schema_version": 2,
        "id": "dnd5e.module.example",
        "version": "1.0.0",
        "kind": "module",
        "checksum": "descriptor-checksum",
        "metadata": {"title": "Example"},
        "manifest": {
            "title": "Example",
            "classification": "campaign",
            "content_summary": {"endings": 0},
        },
        "readiness": {"complete": False},
        "assets": [],
    }
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("package.sagasmith.json", json.dumps(descriptor))

    record = _pack_record(package, tmp_path)

    assert record["package_kind"] == "module"
    assert record["readiness_complete"] is False
    assert record["agent_finalized"] is False
    assert record["disposition"] == "excluded"
    assert record["reason_code"] == "module_pack_not_agent_finalized"


def test_finalized_module_pack_requires_a_source_defined_ending(tmp_path: Path) -> None:
    package = tmp_path / "module.sagasmith-pack"
    descriptor = {
        "schema_version": 2,
        "id": "dnd5e.module.example",
        "version": "1.0.0",
        "kind": "module",
        "checksum": "descriptor-checksum",
        "metadata": {
            "title": "Example",
            "agent_finalization": {"confirmed": True},
        },
        "manifest": {
            "title": "Example",
            "classification": "campaign",
            "content_summary": {"endings": 0},
        },
        "readiness": {"complete": True},
        "assets": [],
    }
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("package.sagasmith.json", json.dumps(descriptor))

    record = _pack_record(package, tmp_path)

    assert record["disposition"] == "excluded"
    assert record["reason_code"] == "module_pack_missing_source_defined_ending"


def test_coverage_matrix_joins_any_dynamically_discovered_runnable_unit() -> None:
    checksum = "1" * 64
    units = [
        {
            "id": "future-module-not-in-code",
            "status": "runnable",
            "module_sha256": [checksum],
        }
    ]
    routes = _coverage_routes(
        {"coverage_routes": [_complete_route("future-module-not-in-code", checksum)]}
    )

    matrix, coverage = _build_coverage_matrix(units, routes)

    assert {row["campaign_line_id"] for row in matrix} == {
        "future-module-not-in-code"
    }
    assert coverage[0]["status"] == "complete"


def test_missing_route_is_a_machine_readable_coverage_gap() -> None:
    units = [{"id": "new-module", "status": "runnable", "module_sha256": ["2" * 64]}]

    matrix, coverage = _build_coverage_matrix(units, {})

    assert matrix == []
    assert coverage == [
        {
            "campaign_line_id": "new-module",
            "status": "incomplete",
            "gaps": ["coverage_route_missing"],
            "scenario_count": 0,
        }
    ]


def test_orphan_route_is_rejected_instead_of_becoming_a_runnable_whitelist() -> None:
    route = _complete_route("stale-module", "3" * 64)

    try:
        _build_coverage_matrix([], {"stale-module": route})
    except ValueError as exc:
        assert "dynamically discovered runnable units" in str(exc)
    else:
        raise AssertionError("orphan route was accepted")


def test_route_evidence_must_reference_a_discovered_source_checksum() -> None:
    units = [{"id": "module", "status": "runnable", "module_sha256": ["4" * 64]}]
    route = _complete_route("module", "5" * 64)

    try:
        _build_coverage_matrix(units, {"module": route})
    except ValueError as exc:
        assert "source checksum is not part of discovered unit" in str(exc)
    else:
        raise AssertionError("foreign source checksum was accepted")


def test_coverage_gaps_report_missing_dimensions_mechanisms_and_recovery() -> None:
    checksum = "6" * 64
    route = _complete_route("module", checksum)
    route["scenarios"] = [route["scenarios"][0]]

    _, coverage = _build_coverage_matrix(
        [{"id": "module", "status": "runnable", "module_sha256": [checksum]}],
        {"module": route},
    )

    assert coverage[0]["status"] == "incomplete"
    assert "missing_positioning_mode:agent" in coverage[0]["gaps"]
    assert "missing_audience:player" in coverage[0]["gaps"]
    assert "missing_path:recovery" in coverage[0]["gaps"]
    assert "missing_recovery_operation:snapshot_restore" in coverage[0]["gaps"]


def test_checked_in_routes_cover_every_fixture_declared_runnable_unit() -> None:
    repo = Path(__file__).resolve().parents[1]
    corpus = json.loads(
        (repo / "fixtures" / "full_campaign_corpus.json").read_text(encoding="utf-8")
    )
    decisions = json.loads(
        (repo / "fixtures" / "module_corpus_decisions.json").read_text(
            encoding="utf-8"
        )
    )
    units = [
        {
            "id": line["id"],
            "status": "runnable",
            "module_sha256": [entry["sha256"] for entry in line["modules"]],
        }
        for line in corpus["campaign_lines"]
    ]
    units.extend(
        {
            "id": decision["campaign_line_id"],
            "status": decision["disposition"],
            "module_sha256": [checksum],
        }
        for checksum, decision in decisions["decisions_by_sha256"].items()
        if decision.get("disposition") == "runnable_installed_pack_required"
    )

    matrix, coverage = _build_coverage_matrix(units, _coverage_routes(decisions))

    assert len(matrix) == 4 * len(units)
    assert {item["campaign_line_id"] for item in coverage} == {
        item["id"] for item in units
    }
    assert all(item["status"] == "complete" for item in coverage)

    tomb = next(
        item
        for item in decisions["coverage_routes"]
        if item["campaign_line_id"] == "tomb-of-annihilation"
    )
    tomb_ending = next(
        item for item in tomb["scenarios"] if item["id"] == "tomb-conclusion"
    )
    assert tomb_ending["ending_prerequisites"] == [
        {
            "id": "soulmonger-destroyed",
            "receipt": "semantic_event",
            "fact_key": "ending.tomb-of-annihilation.soulmonger-destroyed",
            "source_evidence": {
                "page_start": 179,
                "page_end": 179,
                "heading_path": [
                    "Ch 5: Tomb of the Nine Gods",
                    "Level 6: Cradle of the Death",
                ],
            },
        }
    ]

    avernus = next(
        item
        for item in decisions["coverage_routes"]
        if item["campaign_line_id"] == "descent-into-avernus-zh"
    )
    morgue = next(item for item in avernus["scenarios"] if item["id"] == "bathhouse-morgue")
    ending = next(item for item in avernus["scenarios"] if item["id"] == "redeeming-zariel")
    assert [item["receipt"] for item in ending["ending_prerequisites"]] == [
        "loot_acquire",
        "semantic_event",
        "semantic_event",
        "semantic_event",
        "character_check",
        "item_spend",
    ]
    assert ending["ending_prerequisites"][4] | {"source_evidence": None} == {
        "id": "zariel-persuasion-success",
        "receipt": "character_check",
        "skill": "Persuasion",
        "base_dc": 25,
        "applied_reducer_ids": [
            "lulu-present-reducer",
            "olanthius-allied-present-reducer",
        ],
        "dc": 15,
        "success": True,
        "source_evidence": None,
    }
    assert [
        (
            item["statblock_source_identity"],
            item["required_count"],
            item.get("required_variant"),
            item.get("variant_source_kind"),
        )
        for item in morgue["initial_source_groups"]
    ] == [
        ("Master of Souls", 1, None, None),
        ("Swarm of Rats", 1, {"creature_type": "undead"}, "module-chunk"),
    ]
    assert morgue["initial_source_groups"][0]["statblock_evidence"] == {
        "page_start": 181,
        "page_end": 181,
        "heading_path": ["Appendix", "驭魂者 Master of Souls"],
    }


def test_reduced_ending_check_requires_ordered_declared_reducers() -> None:
    checksum = "9" * 64
    units = [{"id": "reduced-ending", "status": "runnable", "module_sha256": [checksum]}]
    route = _complete_route("reduced-ending", checksum)
    normal = route["scenarios"][0]
    normal["ending_prerequisites"] = [
        {
            "id": "ally",
            "receipt": "semantic_event",
            "fact_key": "ending.ally",
            "dc_reduction": 5,
        },
        {
            "id": "check",
            "receipt": "character_check",
            "skill": "Persuasion",
            "base_dc": 25,
            "applied_reducer_ids": ["ally"],
            "dc": 15,
            "success": True,
        },
    ]

    with pytest.raises(ValueError, match="base_dc minus declared reducers"):
        _build_coverage_matrix(units, {"reduced-ending": route})

    normal["ending_prerequisites"][1]["dc"] = 20
    _build_coverage_matrix(units, {"reduced-ending": route})
