from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from scripts.regression_full_campaigns import (
    _build_playthrough_manifest,
    _create_campaign,
    _line_review_blocks,
    _load_and_verify_manifest,
    _selected_lines,
)
from scripts.regression_modules import _create_baseline_snapshot, _facade_value


def test_exposure_facade_unwrap_preserves_structured_domain_status() -> None:
    facade = {"status": "ok", "action": "get", "result": {"id": "campaign"}}
    structured = {
        "status": "committed",
        "result": {"kind": "healing", "amount": 9},
        "campaign_revision": 12,
    }

    assert _facade_value(facade) == {"id": "campaign"}
    assert _facade_value(structured) == structured


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
            return {
                "id": "campaign-1",
                "revision": 4,
                "state": {
                    "random_stream": {
                        "algorithm": "sha256-counter-v1",
                        "seed": "a" * 64,
                        "position": 0,
                        "last_receipt": None,
                    }
                },
            }

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True, "head_snapshot_id": "snap-1"}]
            if tool_id == "snapshot_query" and arguments["view"] == "list":
                return [
                    {
                        "id": "snap-1",
                        "branch_id": "branch-1",
                        "slot": 1,
                        "label": "Imported campaign baseline v2: line-1",
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
    assert result["branch_id"] == "branch-1"
    assert result["random_stream"]["position"] == 0
    assert client.created is False


def test_full_campaign_creation_configures_selected_advancement_mode() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def open(self, campaign_id: str | None = None) -> None:
            self.calls.append(("open", {"campaign_id": campaign_id}))

        async def load(self, *group_ids: str) -> None:
            self.calls.append(("load", {"group_ids": group_ids}))

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append((tool_id, arguments))
            if tool_id == "campaign_create":
                return {"id": "campaign-1", "revision": 1}
            if tool_id == "campaign_change":
                assert arguments["payload"] == {"mode": "xp"}
                return {
                    "campaign": {
                        "id": "campaign-1",
                        "revision": 2,
                        "settings": {"advancement": {"mode": "xp"}},
                    }
                }
            raise AssertionError(tool_id)

    args = argparse.Namespace(
        run_id="run-1",
        edition="2014",
        locale="en",
    )
    line = {
        "id": "line-1",
        "title": "Line One",
        "play_requirements": {"advancement": {"selected": "xp"}},
    }
    client = Client()

    campaign = asyncio.run(_create_campaign(client, line=line, args=args))

    assert campaign["settings"]["advancement"]["mode"] == "xp"
    assert [name for name, _ in client.calls] == [
        "open",
        "load",
        "campaign_create",
        "open",
        "load",
        "campaign_change",
    ]


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


def test_reviewed_non_module_character_material_does_not_block_fallback_party() -> None:
    line = {
        "id": "line-1",
        "play_requirements": {
            "recommended_party_size": {
                "status": "source_confirmed",
                "minimum": 4,
                "maximum": 5,
                "selected": 5,
            }
        },
    }
    player_documents = [
        {
            "relative_path": "associated.pdf",
            "declared_player_material": {
                "review_status": "reviewed_excluded_from_party",
            },
            "character_document": {
                "document_kind": "character_sheet",
                "ready_to_create": False,
                "missing_fields": ["level", "ability_scores", "hp"],
            },
        }
    ]

    assert _line_review_blocks(line, player_documents) == []


def test_playthrough_manifest_builder_preserves_unknown_party_size_review() -> None:
    line = {
        "id": "line-1",
        "play_requirements": {
            "recommended_party_size": {
                "status": "dm_review_required",
                "minimum": None,
                "maximum": None,
                "selected": None,
            },
            "source_refs": [
                {
                    "purpose": "level_span",
                    "asset_path": "Campaign.pdf",
                    "asset_sha256": "a" * 64,
                    "page_start": 1,
                    "page_end": 1,
                    "heading_path": ["Introduction"],
                    "chunk_content_sha256": "b" * 64,
                }
            ],
        },
    }
    review = [{"kind": "recommended_party_size", "reason": "DM review required"}]
    manifest = _build_playthrough_manifest(
        line=line,
        module_ids=["module-1"],
        run_id="run-1",
        review_blocks=review,
    )

    assert manifest["party"]["selected_size"] is None
    assert manifest["party"]["use_pregenerated_first"] is True
    assert manifest["review_blocks"] == review
