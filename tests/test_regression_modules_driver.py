from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from scripts.regression_full_campaigns import (
    _line_review_blocks,
    _load_and_verify_manifest,
    _selected_lines,
)
from scripts.regression_modules import _create_baseline_snapshot


def test_campaign_baseline_reuses_existing_public_snapshot() -> None:
    class Client:
        def __init__(self) -> None:
            self.created = False

        async def open(self, campaign_id: str) -> None:
            assert campaign_id == "campaign-1"

        async def load(self, *group_ids: str) -> None:
            assert group_ids == ("lobby.campaign",)

        async def core(self, tool_id: str, arguments: dict):
            assert tool_id == "campaign_query"
            return {"id": "campaign-1", "revision": 4}

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True, "head_snapshot_id": "snap-1"}]
            if tool_id == "snapshot_query" and arguments["view"] == "list":
                return [
                    {
                        "id": "snap-1",
                        "branch_id": "branch-1",
                        "slot": 1,
                        "label": "Imported campaign baseline: line-1",
                    }
                ]
            if tool_id == "snapshot_query" and arguments["view"] == "verify":
                return {"valid": True}
            if tool_id == "snapshot_create":
                self.created = True
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _create_baseline_snapshot(
            client,
            campaign_key="line-1",
            campaign_id="campaign-1",
            run_id="run-1",
        )
    )

    assert result["reused"] is True
    assert result["verification"] == {"valid": True}
    assert client.created is False


def test_full_campaign_manifest_verifies_checksums_and_selection(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "campaign.md"
    source.write_text("# Campaign\n", encoding="utf-8")
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "expected_asset_count": 1,
                "campaign_lines": [
                    {
                        "id": "line-1",
                        "title": "Line One",
                        "modules": [
                            {
                                "path": "campaign.md",
                                "role": "primary_campaign",
                                "sequence": 1,
                                "size": source.stat().st_size,
                                "sha256": checksum,
                            }
                        ],
                        "player_materials": [],
                        "assets": [],
                    }
                ],
                "unassigned_assets": [],
            }
        ),
        encoding="utf-8",
    )

    manifest = _load_and_verify_manifest(manifest_path, root)

    assert manifest["verification"]["valid"] is True
    assert _selected_lines(manifest, ["line-1"])[0]["title"] == "Line One"
    with pytest.raises(ValueError, match="unknown campaign line"):
        _selected_lines(manifest, ["missing"])


def test_full_campaign_review_blocks_missing_party_count_and_incomplete_preset() -> None:
    line = {
        "id": "line-1",
        "play_requirements": {
            "recommended_party_size": {
                "status": "dm_review_required",
                "reason": "No range in source",
            }
        },
    }
    player_documents = [
        {
            "relative_path": "preset.pdf",
            "character_document": {
                "document_kind": "character_sheet",
                "ready_to_create": False,
                "missing_fields": ["level"],
            },
        }
    ]

    assert _line_review_blocks(line, player_documents) == [
        {
            "kind": "recommended_party_size",
            "campaign_line_id": "line-1",
            "reason": "No range in source",
        },
        {
            "kind": "incomplete_character_template",
            "campaign_line_id": "line-1",
            "path": "preset.pdf",
            "missing_fields": ["level"],
        },
    ]
