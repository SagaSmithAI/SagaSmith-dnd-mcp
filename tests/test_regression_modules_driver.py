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
    _resolve_playthrough_source_refs,
    _selected_lines,
)
from scripts.regression_modules import (
    _create_baseline_snapshot,
    _domain_value,
    _facade_value,
    _module_import_identity,
)


def test_exposure_facade_unwrap_preserves_structured_domain_status() -> None:
    facade = {"status": "ok", "action": "get", "result": {"id": "campaign"}}
    query_facade = {"view": "get", "result": {"id": "campaign"}}
    mock_facade = {"result": {"id": "campaign"}}
    structured = {
        "status": "committed",
        "result": {"kind": "healing", "amount": 9},
        "campaign_revision": 12,
    }

    assert _facade_value(facade) == {"id": "campaign"}
    assert _facade_value(query_facade) == {"id": "campaign"}
    assert _facade_value(mock_facade) == {"id": "campaign"}
    assert _facade_value(structured) == structured


def test_exposure_domain_preserves_random_stream_receipt() -> None:
    receipt = {
        "operation": "character_action",
        "position_before": 7,
        "position_after": 8,
    }
    wrapped = {
        "result": {
            "action": "use_activity",
            "result": {"status": "committed", "result": {"kind": "second_wind"}},
        },
        "random_stream_receipt": receipt,
    }

    assert _domain_value(wrapped) == {
        "status": "committed",
        "result": {"kind": "second_wind"},
        "random_stream_receipt": receipt,
    }


def test_exposure_domain_accepts_current_direct_structured_result() -> None:
    campaign = {"id": "campaign-1", "revision": 1, "name": "Fresh campaign"}

    assert _domain_value(campaign) == campaign


def test_module_stage_identity_changes_with_source_or_normalizer() -> None:
    common = {
        "run_id": "run-1",
        "relative_path": "Campaign.pdf",
        "source_checksum": "a" * 64,
        "title": "Campaign",
    }
    v13 = _module_import_identity(
        **common,
        normalizer="sagasmith-core/pdf-layout-v13",
        parser="dnd5e-v11",
    )
    v14 = _module_import_identity(
        **common,
        normalizer="sagasmith-core/pdf-layout-v14",
        parser="dnd5e-v11",
    )
    parser_v12 = _module_import_identity(
        **common,
        normalizer="sagasmith-core/pdf-layout-v14",
        parser="dnd5e-v12",
    )
    changed_source = _module_import_identity(
        **{**common, "source_checksum": "b" * 64},
        normalizer="sagasmith-core/pdf-layout-v14",
        parser="dnd5e-v12",
    )

    assert v13 != v14
    assert v14 != parser_v12
    assert parser_v12 != changed_source
    assert parser_v12 == _module_import_identity(
        **common,
        normalizer="sagasmith-core/pdf-layout-v14",
        parser="dnd5e-v12",
    )


def test_campaign_baseline_reuses_existing_public_snapshot() -> None:
    class Client:
        def __init__(self) -> None:
            self.created = False

        async def open(self, campaign_id: str) -> None:
            raise AssertionError(f"baseline must reuse the bound exposure: {campaign_id}")

        async def load(self, *group_ids: str) -> None:
            assert group_ids == ()

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


def test_full_campaign_creation_retry_keeps_existing_advancement_receipt() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def open(self, campaign_id: str | None = None) -> None:
            self.calls.append("open")

        async def load(self, *group_ids: str) -> None:
            self.calls.append("load")

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append(tool_id)
            if tool_id == "campaign_create":
                return {
                    "id": "campaign-1",
                    "revision": 7,
                    "settings": {"advancement": {"mode": "milestone"}},
                }
            raise AssertionError("retry must not resubmit a completed advancement change")

    args = argparse.Namespace(run_id="run-1", edition="2014", locale="en")
    line = {
        "id": "line-1",
        "title": "Line One",
        "play_requirements": {"advancement": {"selected": "milestone"}},
    }
    client = Client()

    campaign = asyncio.run(_create_campaign(client, line=line, args=args))

    assert campaign["revision"] == 7
    assert client.calls == ["open", "load", "campaign_create", "open", "load"]


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


def test_full_campaign_party_recommendation_is_advisory_but_incomplete_preset_blocks() -> None:
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


def test_completed_party_size_dm_review_is_advisory_even_without_evidence() -> None:
    line = {
        "id": "waterdeep-dragon-heist",
        "play_requirements": {
            "recommended_party_size": {
                "status": "dm_review_completed",
                "minimum": 4,
                "maximum": 4,
                "selected": 4,
                "review": {
                    "module_party_size_status": "not_stated",
                    "represented_as_module_recommendation": False,
                },
            }
        },
    }

    assert _line_review_blocks(line, []) == []
    del line["play_requirements"]["recommended_party_size"]["review"]
    assert _line_review_blocks(line, []) == []


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
                    "content_sha256": "b" * 64,
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
    assert manifest["party"]["party_size_status"] == "dm_review_required"
    assert manifest["party"]["party_size_review"] == {
        "default_resolver": "agent",
        "ruling_kind": "source_or_scene_fact",
    }
    assert manifest["party"]["use_pregenerated_first"] is True
    assert manifest["review_blocks"] == review


def test_skt_archetype_references_are_not_mislabeled_as_pregenerated_pcs() -> None:
    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "full_campaign_corpus.json"
    manifest = json.loads(fixture_path.read_text(encoding="utf-8"))
    line = next(
        item
        for item in manifest["campaign_lines"]
        if item["id"] == "storm-kings-thunder"
    )
    templates = [
        item
        for item in line["player_materials"]
        if item["role"] == "associated_character_template"
    ]
    pregenerated = line["play_requirements"]["pregenerated_characters"]

    assert len(templates) == 7
    assert {
        item["review_status"] for item in templates
    } == {"reviewed_not_module_pregen"}
    assert all("Character name, level" in item["notes"][1] for item in templates)
    assert pregenerated["official_sheets_present_in_corpus"] is False
    assert pregenerated["selected_count"] == 0
    assert pregenerated["status"] == "reviewed_not_module_pregen"


def test_corpus_source_refs_resolve_to_one_exact_managed_chunk() -> None:
    content = "Characters begin at 1st level. The ideal party size is four characters."
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    line = {
        "id": "tyranny-of-dragons",
        "play_requirements": {
            "source_refs": [
                {
                    "purpose": "party_size",
                    "asset_path": "Hoard.pdf",
                    "asset_sha256": "a" * 64,
                    "page_start": 6,
                    "page_end": 6,
                    "heading_path": ["Front Matter", "Introduction"],
                    "content_sha256": content_sha256,
                }
            ]
        },
    }

    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append((tool_id, arguments))
            if tool_id == "module_search":
                assert arguments == {
                    "campaign_id": "campaign-1",
                    "module_ids": ["module-1"],
                    "query": "Introduction",
                    "top_k": 50,
                }
                return {"status": "ok", "result": [{"id": "chunk-1"}]}
            assert tool_id == "module_expand"
            assert arguments == {"chunk_id": "chunk-1"}
            return {
                "chunk_id": "chunk-1",
                "content": content,
                "content_sha256": content_sha256,
                "source_ref": {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "chunk_id": "chunk-1",
                    "page_start": 6,
                    "page_end": 6,
                    "heading_path": ["Front Matter", "Introduction"],
                    "content_sha256": content_sha256,
                },
            }

    client = Client()
    resolved = asyncio.run(
        _resolve_playthrough_source_refs(
            client,
            campaign_id="campaign-1",
            line=line,
            module_documents=[
                {
                    "module_id": "module-1",
                    "checksum": "a" * 64,
                }
            ],
        )
    )

    assert resolved == [
        {
            **line["play_requirements"]["source_refs"][0],
            "module_id": "module-1",
            "scene_id": "scene-1",
            "chunk_id": "chunk-1",
            "excerpt": content,
        }
    ]


@pytest.mark.parametrize(
    ("search_hits", "expanded_scene_id", "expected"),
    [
        ([], "scene-1", "must resolve exactly once"),
        ([{"id": "chunk-1"}, {"id": "chunk-2"}], "scene-1", "must resolve exactly once"),
        ([{"id": "chunk-1"}], "", "has no managed scene_id"),
    ],
)
def test_corpus_source_ref_resolution_fails_closed(
    search_hits: list[dict[str, str]],
    expanded_scene_id: str,
    expected: str,
) -> None:
    content = "The ideal party size is four characters."
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    line = {
        "id": "tyranny-of-dragons",
        "play_requirements": {
            "source_refs": [
                {
                    "asset_sha256": "a" * 64,
                    "page_start": 6,
                    "page_end": 6,
                    "heading_path": ["Introduction"],
                    "content_sha256": content_sha256,
                }
            ]
        },
    }

    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "module_search":
                return {"result": search_hits}
            chunk_id = str(arguments["chunk_id"])
            return {
                "content": content,
                "content_sha256": content_sha256,
                "source_ref": {
                    "module_id": "module-1",
                    "scene_id": expanded_scene_id,
                    "chunk_id": chunk_id,
                    "page_start": 6,
                    "page_end": 6,
                    "heading_path": ["Introduction"],
                },
            }

    with pytest.raises(RuntimeError, match=expected):
        asyncio.run(
            _resolve_playthrough_source_refs(
                Client(),
                campaign_id="campaign-1",
                line=line,
                module_documents=[
                    {
                        "module_id": "module-1",
                        "checksum": "a" * 64,
                    }
                ],
            )
        )
