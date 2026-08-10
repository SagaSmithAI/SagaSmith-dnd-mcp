from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from scripts.regression_corpus import (
    CORE_TOOLS,
    _declared_records,
    _pack_record,
    _raw_records,
)


def test_native_cold_start_contract_is_exactly_six_tools() -> None:
    assert CORE_TOOLS == {
        "campaign_query",
        "exposure",
        "game_phase",
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
        "campaign_lines": [
            {
                "id": "new-adventure",
                "title": "New Adventure",
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
            "disposition": "pending",
            "reason_code": "unreviewed_source_candidate",
            "campaign_line_id": None,
            "title": "surprise-new-module",
        }
    ]


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
