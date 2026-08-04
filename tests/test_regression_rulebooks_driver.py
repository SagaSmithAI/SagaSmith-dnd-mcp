from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts import regression_rulebooks as driver


class _FakeServer:
    def __init__(self, responses: list[tuple[str, dict[str, Any]]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[None, dict[str, Any]]:
        self.calls.append((name, arguments))
        expected_name, response = self.responses.pop(0)
        assert name == expected_name
        return None, response


def test_catalog_review_token_is_canonical_and_changes_with_agent_decisions() -> None:
    first = {
        "complete_review": True,
        "decisions": [{"name": "Drider", "note": "source checked"}],
    }
    reordered = {
        "decisions": [{"note": "source checked", "name": "Drider"}],
        "complete_review": True,
    }
    revised = {
        **first,
        "decisions": [{"name": "Drider", "note": "page and heading checked"}],
    }

    assert driver._catalog_review_token(first) == driver._catalog_review_token(reordered)
    assert driver._catalog_review_token(first) != driver._catalog_review_token(revised)
    assert driver._catalog_review_token({}) != driver._catalog_review_token(first)


def test_agent_statblock_slot_reviews_replay_bounded_ocr_corrections() -> None:
    server = _FakeServer(
        [
            (
                "rule_import",
                {
                    "result": {
                        "job": {"revision": 7},
                        "recovery": {
                            "evidence": {
                                "heading_match_mode": "agent_named_structural_slot",
                                "statblock_slot_summary": {
                                    "slot": 2,
                                    "identity": "Large fiend (demon), chaotic evil",
                                    "core": {
                                        "Armor Class": "Armor Class 18",
                                        "Hit Points": "Hit Points 184 (16d10 + 96)",
                                        "Speed": "Speed 20 ft., fly 30 ft.",
                                    },
                                },
                            }
                        },
                        "review": {
                            "id": "review-1",
                            "derived_from_review_id": "bad-caption",
                            "source_checksum": "source",
                            "image_checksum": "image",
                            "normalized_content_sha256": "content",
                        },
                    }
                },
            )
        ]
    )

    result = asyncio.run(
        driver._apply_statblock_slot_reviews(
            server,
            campaign_id="campaign-1",
            job_id="job-1",
            review_spec={
                "statblock_slot_reviews": [
                    {
                        "page_number": 63,
                        "statblock_slot": 2,
                        "name": "Nalfeshnee",
                        "expected_identity": "Large fiend (demon), chaotic evil",
                        "ocr_corrections": {
                            "abilities": {"str": "21 (+5)"},
                            "text_replacements": [
                                {
                                    "old": "ii g",
                                    "new": "Hit: 2 (1d4) piercing damage.",
                                }
                            ],
                        },
                        "note": "Agent identified the creature from its actions and page context.",
                    }
                ]
            },
            id_key="book-review",
        )
    )

    assert result is not None
    assert result["job_revision"] == 7
    assert result["reviews"][0]["derived_from_review_id"] == "bad-caption"
    name, arguments = server.calls[0]
    assert name == "rule_import"
    assert arguments["action"] == "recover_statblock"
    assert arguments["payload"] == {
        "job_id": "job-1",
        "name": "Nalfeshnee",
        "page_number": 63,
        "statblock_slot": 2,
        "ocr_corrections": {
            "abilities": {"str": "21 (+5)"},
            "text_replacements": [
                {"old": "ii g", "new": "Hit: 2 (1d4) piercing damage."}
            ],
        },
    }
    normalized_spec = driver._statblock_slot_review_specs(
        {
            "statblock_slot_reviews": [
                {
                    "page_number": 63,
                    "statblock_slot": 2,
                    "name": "Nalfeshnee",
                    "expected_identity": "Large fiend (demon), chaotic evil",
                    "ocr_corrections": {
                        "abilities": {"str": "21 (+5)"},
                        "text_replacements": [
                            {
                                "old": "ii g",
                                "new": "Hit: 2 (1d4) piercing damage.",
                            }
                        ],
                    },
                    "note": (
                        "Agent identified the creature from its actions and page context."
                    ),
                }
            ]
        }
    )[0]
    assert arguments["idempotency_key"] == (
        "regression-agent-statblock-slot-wrapper-v1-"
        f"r{driver.OCR_STATBLOCK_RECOVERY_VERSION}-book-review-"
        f"{driver._statblock_slot_review_token(normalized_spec)}"
    )


def test_agent_statblock_slot_token_is_local_to_one_correction() -> None:
    first = {
        "page_number": 63,
        "statblock_slot": 2,
        "name": "Nalfeshnee",
        "ocr_corrections": {"abilities": {"str": "21 (+5)"}},
    }
    reordered = {
        "name": "Nalfeshnee",
        "ocr_corrections": {"abilities": {"str": "21 (+5)"}},
        "statblock_slot": 2,
        "page_number": 63,
    }
    revised = {
        **first,
        "ocr_corrections": {"abilities": {"str": "22 (+6)"}},
    }

    assert driver._statblock_slot_review_token(first) == (
        driver._statblock_slot_review_token(reordered)
    )
    assert driver._statblock_slot_review_token(first) != (
        driver._statblock_slot_review_token(revised)
    )


def test_agent_statblock_slot_manifest_rejects_duplicate_or_unbounded_slots() -> None:
    with pytest.raises(ValueError, match="unique page slots"):
        driver._statblock_slot_review_specs(
            {
                "statblock_slot_reviews": [
                    {"page_number": 1, "statblock_slot": 1, "name": "First"},
                    {"page_number": 1, "statblock_slot": 1, "name": "Second"},
                ]
            }
        )
    with pytest.raises(ValueError, match="positive page/slot"):
        driver._statblock_slot_review_specs(
            {
                "statblock_slot_reviews": [
                    {"page_number": 1, "statblock_slot": 0, "name": "Invalid"}
                ]
            }
        )
    with pytest.raises(ValueError, match="unknown ability"):
        driver._statblock_slot_review_specs(
            {
                "statblock_slot_reviews": [
                    {
                        "page_number": 1,
                        "statblock_slot": 1,
                        "name": "Invalid",
                        "ocr_corrections": {"abilities": {"luck": "20 (+5)"}},
                    }
                ]
            }
        )
    with pytest.raises(ValueError, match="duplicate OCR text replacement"):
        driver._statblock_slot_review_specs(
            {
                "statblock_slot_reviews": [
                    {
                        "page_number": 1,
                        "statblock_slot": 1,
                        "name": "Invalid",
                        "ocr_corrections": {
                            "text_replacements": [
                                {"old": "noise", "new": "first repair"},
                                {"old": "NOISE", "new": "second repair"},
                            ]
                        },
                    }
                ]
            }
        )


def test_agent_transcription_reviews_replay_before_rulebook_ingest() -> None:
    base_hash = "a" * 64
    server = _FakeServer(
        [
            (
                "rule_import",
                {
                    "image_checksum": "b" * 64,
                    "transcription": {
                        "normalized": {"text_sha256": base_hash}
                    },
                },
            ),
            (
                "rule_import",
                {
                    "result": {
                        "job": {"revision": 3},
                        "inspection": {"warnings": [], "page_revisions": [{}]},
                        "review": {
                            "review_method": "agent",
                            "evidence": {"basis": "agent_context"},
                        },
                    }
                },
            ),
        ]
    )

    result = asyncio.run(
        driver._apply_transcription_reviews(
            server,
            campaign_id="campaign-1",
            job_id="job-1",
            initial_revision=2,
            review_spec={
                "text_reviews": [
                    {
                        "page_number": 7,
                        "base_text_sha256": base_hash,
                        "replacements": [{"old": "F i reball", "new": "Fireball"}],
                        "rationale": "Agent restores a split heading without changing numbers.",
                        "evidence_basis": "agent_context",
                    }
                ]
            },
            id_key="book",
        )
    )

    assert result is not None
    assert result["count"] == 1
    assert result["job_revision"] == 3
    render_call, review_call = server.calls
    assert render_call[1]["action"] == "render_page"
    assert render_call[1]["payload"]["include_ocr_text"] is False
    assert review_call[1]["action"] == "review_text"
    assert review_call[1]["expected_revision"] == 2
    assert review_call[1]["payload"]["review_method"] == "agent"
    assert review_call[1]["payload"]["base_text_sha256"] == base_hash


def test_transcription_review_manifest_rejects_unbound_or_unsafe_entries() -> None:
    with pytest.raises(ValueError, match="base_text_sha256"):
        driver._transcription_review_specs(
            {
                "text_reviews": [
                    {
                        "page_number": 1,
                        "base_text_sha256": "not-a-hash",
                        "replacements": [{"old": "F ire", "new": "Fire"}],
                        "rationale": "Agent repairs a split word.",
                        "evidence_basis": "agent_context",
                    }
                ]
            }
        )
    with pytest.raises(ValueError, match="rendered_image_checksum requires"):
        driver._transcription_review_specs(
            {
                "text_reviews": [
                    {
                        "page_number": 1,
                        "base_text_sha256": "a" * 64,
                        "replacements": [{"old": "F ire", "new": "Fire"}],
                        "rationale": "Agent repairs a split word.",
                        "evidence_basis": "agent_context",
                        "rendered_image_checksum": "b" * 64,
                    }
                ]
            }
        )


def test_portable_pack_id_is_stable_across_regression_runs() -> None:
    first = driver._portable_pack_id("book.pdf", run_id="one")
    assert first == driver._portable_pack_id("book.pdf", run_id="one")
    assert first == driver._portable_pack_id("book.pdf", run_id="two")
    assert first.startswith("dnd5e.addon.rulebook.book.")


def test_dependency_addons_require_exact_review_selection(tmp_path) -> None:
    component = {
        "kind": "rule_pack",
        "id": "dnd5e.private.phb",
        "version": "1.0.0",
        "checksum": "a" * 64,
    }
    package = {
        "kind": "addon_pack",
        "id": "dnd5e.private.phb.addon",
        "version": "1.0.0",
        "checksum": "b" * 64,
        "payload": {"components": [component, {"kind": "preset_pack"}]},
    }
    path = tmp_path / "phb.addon.sagasmith.json"
    path.write_text(json.dumps(package), encoding="utf-8")

    available = driver._load_dependency_addons([path])
    selected = driver._selected_dependency_addons(
        {
            "dependency_addons": [
                {"id": package["id"], "version": package["version"]}
            ]
        },
        available,
    )

    assert selected == [package]
    assert driver._dependency_rule_components(selected) == [component]
    with pytest.raises(ValueError, match="was not supplied"):
        driver._selected_dependency_addons(
            {
                "dependency_addons": [
                    {"id": "missing.addon", "version": "1.0.0"}
                ]
            },
            available,
        )


def test_source_selector_recovers_bounded_ocr_heading_spacing() -> None:
    chunk = {
        "heading_path": [
            "Character Options",
            "M ANTLE OF I NSPIRATION",
        ],
        "content": "At 3rd level, the feature applies.",
        "page_start": 15,
        "page_end": 15,
    }

    assert driver._source_selector_matches(
        chunk,
        {"heading_contains": "Mantle of Inspiration", "page_start": 15},
    )
    assert not driver._source_selector_matches(
        chunk,
        {"heading_contains": "Mantle of Majesty", "page_start": 15},
    )


def test_actor_name_gate_requires_the_exact_reviewed_multiset() -> None:
    package = {
        "package": {
            "payload": {
                "cards": [
                    {"payload": {"name": "Cackler"}},
                    {"payload": {"name": "Sire of Insanity"}},
                ]
            }
        }
    }

    driver._require_expected_actor_names(
        {"expected_actor_names": ["CACKLER", "Sire of Insanity"]},
        package,
    )
    with pytest.raises(RuntimeError, match="missing=.*skyjek roc"):
        driver._require_expected_actor_names(
            {"expected_actor_names": ["Cackler", "Skyjek Roc"]},
            package,
        )
    reviewed_names = '["cackler","sire of insanity"]'
    driver._require_expected_actor_names(
        {
            "expected_actor_names_sha256": hashlib.sha256(
                reviewed_names.encode("utf-8")
            ).hexdigest()
        },
        package,
    )
    with pytest.raises(RuntimeError, match="name manifest checksum differs"):
        driver._require_expected_actor_names(
            {"expected_actor_names_sha256": "0" * 64},
            package,
        )


def test_publication_metadata_marks_only_core_dependencies_as_standard() -> None:
    assert driver._publication_metadata("D&D 5E - Player's Handbook.pdf") == (
        "phb2014",
        "core",
    )
    assert driver._publication_metadata("D&D 5E - Monster Manual.pdf") == (
        "mm2014",
        "core",
    )
    assert driver._publication_metadata(
        "D&D 5E - Xanathar's Guide to Everything.pdf"
    ) == ("xgte2014", "supplement")
    assert driver._core_content_dependency("2014") == {
        "id": "dnd5e.content.srd2014",
        "version": "1.20.0",
    }
    assert driver._core_content_dependency("2024") == {
        "id": "dnd5e.content.srd2024",
        "version": "1.2.0",
    }


def test_include_globs_are_case_insensitive_and_optional() -> None:
    path = "Sword Coast Adventurer's Guide.pdf"

    assert driver._matches_includes(path, []) is True
    assert driver._matches_includes(path, ["*SWORD COAST*.PDF"]) is True
    assert driver._matches_includes(path, ["Player*.pdf", "*guide.PDF"]) is True
    assert driver._matches_includes(path, ["Monster*.pdf"]) is False
    assert driver._matches_includes(path, [], empty=False) is False


def test_runtime_probe_hp_uses_legal_fixed_average_progression() -> None:
    sheet = driver.default_character_sheet()
    sheet["progression"]["level"] = 10
    sheet["progression"]["classes"] = [
        {"name": "Wizard", "level": 10, "subclass": "", "hit_die": 8}
    ]
    sheet["abilities"]["constitution"]["score"] = 18

    driver._complete_probe_hit_points(sheet, source="runtime probe")

    assert sheet["combat"]["hp"] == {"value": 93, "max": 93, "temp": 0}
    assert sheet["combat"]["hit_dice"]["d8"]["max"] == 10
    assert [entry["value"] for entry in sheet["combat"]["hp_progression"]] == [
        12,
        9,
        9,
        9,
        9,
        9,
        9,
        9,
        9,
        9,
    ]


def test_large_catalog_augmentation_uses_revisioned_public_batches() -> None:
    additions = [{"kind": "feature", "name": f"Feature {index}"} for index in range(205)]
    server = _FakeServer(
        [
            (
                "rule_import",
                {
                    "result": {
                        "job": {"revision": revision},
                        "candidates": [{"id": f"candidate-{revision}"}],
                        "added_candidate_ids": [
                            f"added-{index}" for index in range(start, end)
                        ],
                    }
                },
            )
            for revision, start, end in ((8, 0, 100), (9, 100, 200), (10, 200, 205))
        ]
    )

    result = asyncio.run(
        driver._augment_catalog_batches(
            server,
            campaign_id="campaign",
            job_id="job",
            additions=additions,
            rationale="Complete source review.",
            expected_revision=7,
            idempotency_key="augment",
        )
    )

    assert result["job_revision"] == 10
    assert result["batch_count"] == 3
    assert len(result["added_candidate_ids"]) == 205
    assert [call[1]["expected_revision"] for call in server.calls] == [7, 8, 9]
    assert [len(call[1]["payload"]["additions"]) for call in server.calls] == [100, 100, 5]
    assert [call[1]["idempotency_key"] for call in server.calls] == [
        "augment-batch-1",
        "augment-batch-2",
        "augment-batch-3",
    ]


def test_catalog_manifest_resolves_stable_sources_and_review_actions(tmp_path) -> None:
    manifest_path = tmp_path / "catalog.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "documents": {
                    "UO/Artificer.pdf": {
                        "complete_review": True,
                        "default_status": "accepted",
                        "additions": [
                            {
                                "kind": "subclass",
                                "name": "Gunsmith",
                                "replace_existing": True,
                                "source_selectors": [
                                    {
                                        "heading_exact": "Artificer Specialists",
                                        "content_contains": "A gunsmith is a master engineer",
                                        "start_contains": "A gunsmith",
                                        "end_contains": "uses magic.",
                                    }
                                ],
                                "card": {"class_name": "Artificer"},
                            }
                        ],
                        "decisions": [
                            {
                                "kind": "feature",
                                "name": "Layout Note",
                                "status": "rejected",
                                "note": "This is explanatory layout prose.",
                            },
                            {
                                "kind": "subclass",
                                "name": "Gunsmith",
                                "artifact_patch": {
                                    "card": {"minimum_level": 1},
                                },
                            },
                        ],
                        "expected_catalog": [
                            {"kind": "subclass", "name": "Gunsmith"}
                        ],
                        "expected_counts": {"subclass": 1},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    manifest = driver._load_catalog_manifest(manifest_path)
    review = driver._catalog_document_review(manifest, "UO\\Artificer.pdf")
    additions = driver._resolve_catalog_additions(
        review["additions"],
        [
            {
                "id": "chunk-specialist",
                "heading_path": ["Artificer Specialists"],
                "content": "A gunsmith is a master engineer who uses magic.",
            },
            {
                "id": "chunk-other",
                "heading_path": ["Other"],
                "content": "Unrelated text.",
            },
        ],
        relative_path="UO\\Artificer.pdf",
    )
    assert additions == [
        {
            "kind": "subclass",
            "name": "Gunsmith",
            "card": {"class_name": "Artificer"},
            "replace_existing": True,
            "source_chunk_ids": ["chunk-specialist"],
            "source_spans": [
                {
                    "source_chunk_id": "chunk-specialist",
                    "start": 0,
                    "end": len("A gunsmith is a master engineer who uses magic."),
                    "checksum": hashlib.sha256(
                        b"A gunsmith is a master engineer who uses magic."
                    ).hexdigest(),
                }
            ],
        }
    ]
    candidates = [
        {
            "id": "layout-note",
            "kind": "feature",
            "name": "Layout Note",
            "artifact": {"kind": "feature", "card": {"name": "Layout Note"}},
        },
        {
            "id": "gunsmith",
            "kind": "subclass",
            "name": "Gunsmith",
            "artifact": {
                "kind": "subclass",
                "card": {"name": "Gunsmith", "class_name": "Artificer"},
            },
        },
    ]
    decisions = driver._review_spec_decisions(
        candidates,
        review,
        reviewer="agent:catalog-author",
        method="agent",
    )
    assert decisions[0] == {"id": "layout-note", "review_status": "rejected"}
    assert decisions[1]["artifact"]["card"] == {
        "name": "Gunsmith",
        "class_name": "Artificer",
        "minimum_level": 1,
    }
    assert decisions[1]["catalog_review_decision"]["method"] == "agent"


def test_expected_catalog_uses_reviewed_names_instead_of_damaged_ocr() -> None:
    candidates = [
        {
            "id": "orc",
            "kind": "species",
            "name": "0 RC",
            "artifact": {
                "kind": "species",
                "card": {"name": "0 RC"},
            },
        }
    ]
    review = {
        "default_status": "accepted",
        "decisions": [
            {
                "kind": "species",
                "name": "0 RC",
                "artifact_patch": {"card": {"name": "Orc"}},
            }
        ],
        "expected_catalog": [{"kind": "species", "name": "Orc"}],
    }

    decisions = driver._review_spec_decisions(
        candidates,
        review,
        reviewer="agent:catalog",
        method="agent",
    )

    assert decisions[0]["artifact"]["card"]["name"] == "Orc"


def test_catalog_review_recovers_bounded_ocr_spacing_in_identity() -> None:
    decisions = driver._review_spec_decisions(
        [
            {
                "id": "boots",
                "kind": "item",
                "name": "B OOT S OF FALSE TRACKS",
                "artifact": {
                    "kind": "item",
                    "card": {"name": "B OOT S OF FALSE TRACKS"},
                },
            }
        ],
        {
            "default_status": "rejected",
            "decisions": [
                {
                    "kind": "item",
                    "name": "B OOTS OF FALSE TRACKS",
                    "status": "accepted",
                    "artifact_patch": {"card": {"name": "Boots of False Tracks"}},
                }
            ],
        },
        reviewer="agent:catalog",
        method="agent",
    )

    assert decisions[0]["review_status"] == "accepted"
    assert decisions[0]["artifact"]["card"]["name"] == "Boots of False Tracks"


def test_catalog_manifest_can_accept_only_fully_reviewed_content_kinds() -> None:
    candidates = [
        {
            "id": kind,
            "kind": kind,
            "name": name,
            "artifact": {"kind": kind, "card": {"name": name}},
        }
        for kind, name in (
            ("feature", "Layout Heading"),
            ("item", "Arcane Focus"),
            ("statblock", "Iron Defender"),
        )
    ]

    decisions = driver._review_spec_decisions(
        candidates,
        {
            "default_status": "rejected",
            "default_status_by_kind": {
                "item": "accepted",
                "statblock": "accepted",
            },
            "expected_counts": {"item": 1, "statblock": 1},
        },
        reviewer="agent:catalog",
        method="agent",
    )

    assert [item["review_status"] for item in decisions] == [
        "rejected",
        "accepted",
        "accepted",
    ]


def test_catalog_manifest_can_accept_only_source_bound_additions_by_default() -> None:
    candidates = [
        {
            "id": "candidate:automatic",
            "kind": "feature",
            "name": "Automatic Noise",
            "artifact": {"kind": "feature", "card": {"name": "Automatic Noise"}},
        },
        {
            "id": "candidate:agent:addition",
            "kind": "feature",
            "name": "Reviewed Addition",
            "agent_catalog_addition": {"principal_id": "system:local"},
            "artifact": {"kind": "feature", "card": {"name": "Reviewed Addition"}},
        },
    ]

    decisions = driver._review_spec_decisions(
        candidates,
        {
            "default_status": "rejected",
            "addition_default_status": "accepted",
        },
        reviewer="agent:catalog",
        method="agent",
    )

    assert decisions[0]["review_status"] == "rejected"
    assert decisions[1]["review_status"] == "accepted"


def test_catalog_manifest_rejects_ambiguous_source_selectors() -> None:
    addition = {
        "kind": "subclass",
        "name": "Gunsmith",
        "source_selectors": [{"content_contains": "gunsmith"}],
    }
    with pytest.raises(ValueError, match="matched 2 chunks"):
        driver._resolve_catalog_additions(
            [addition],
            [
                {"id": "one", "content": "Gunsmith rules."},
                {"id": "two", "content": "More gunsmith rules."},
            ],
            relative_path="Artificer.pdf",
        )


def test_catalog_manifest_can_bind_one_entry_to_all_matching_source_chunks() -> None:
    additions = driver._resolve_catalog_additions(
        [
            {
                "kind": "subclass",
                "name": "Order of the Mutant",
                "source_selectors": [
                    {"heading_exact": "ORDER OF THE MUTANT", "match_all": True}
                ],
            }
        ],
        [
            {
                "id": "heading",
                "ordinal": 2,
                "heading_path": ["ORDER OF THE MUTANT"],
                "content": "Order of the Mutant studies controlled transformation.",
            },
            {
                "id": "continuation",
                "ordinal": 3,
                "heading_path": ["ORDER OF THE MUTANT"],
                "content": "Order of the Mutant features continue here.",
            },
        ],
        relative_path="Blood Hunter.pdf",
    )

    assert additions[0]["source_chunk_ids"] == ["heading", "continuation"]
    assert [
        span["source_chunk_id"] for span in additions[0]["source_spans"]
    ] == ["heading", "continuation"]


def test_catalog_additions_replace_only_one_exact_extracted_identity() -> None:
    additions = [
        {"kind": "subclass", "name": "Path of the Battlerager"},
        {"kind": "feature", "name": "Reckless Abandon"},
        {
            "kind": "feature",
            "name": "Explicit Addition",
            "replace_existing": False,
        },
    ]

    bound = driver._bind_catalog_addition_replacements(
        additions,
        [
            {"kind": "subclass", "name": "PATH OF THE BATTLERAGER"},
            {"kind": "feature", "name": "EXPLICIT ADDITION"},
        ],
    )

    assert bound[0]["replace_existing"] is True
    assert "replace_existing" not in bound[1]
    assert bound[2]["replace_existing"] is False
    assert "replace_existing" not in additions[0]


def test_catalog_additions_disambiguate_same_name_by_source_chunk() -> None:
    additions = [
        {
            "kind": "feature",
            "name": "Bonus Proficiencies",
            "source_chunk_ids": ["drunken-master"],
        }
    ]

    bound = driver._bind_catalog_addition_replacements(
        additions,
        [
            {
                "kind": "feature",
                "name": "BONUS PROFICIENCIES",
                "source_chunk_ids": ["college-of-swords"],
            },
            {
                "kind": "feature",
                "name": "BONUS PROFICIENCIES",
                "source_chunk_ids": ["drunken-master"],
            },
        ],
    )

    assert bound[0]["replace_existing"] is True


def test_catalog_additions_split_one_merged_same_name_candidate() -> None:
    bound = driver._bind_catalog_addition_replacements(
        [
            {
                "kind": "feature",
                "name": "Bonus Proficiency",
                "source_chunk_ids": ["cavalier"],
            },
            {
                "kind": "feature",
                "name": "Bonus Proficiency",
                "source_chunk_ids": ["samurai"],
            },
        ],
        [
            {
                "kind": "feature",
                "name": "BONUS PROFICIENCY",
                "source_chunk_ids": ["cavalier", "samurai"],
            }
        ],
    )

    assert bound[0]["replace_existing"] is True
    assert bound[1]["replace_existing"] is False


def test_runtime_probe_expectations_support_nested_and_named_content() -> None:
    driver._assert_runtime_expectations(
        {
            "sheet": {
                "combat": {"speed": {"swim": 30}},
                "content": {
                    "spells": [{"name": "Gust"}, {"name": "Gust of Wind"}]
                },
            }
        },
        [
            {"path": "sheet.combat.speed.swim", "equals": 30},
            {
                "path": "sheet.content.spells",
                "contains_names": ["Gust", "Gust of Wind"],
            },
            {"path": "sheet.content.spells", "length": 2},
        ],
    )


def test_catalog_manifest_keeps_empty_heading_chunk_as_identity_evidence() -> None:
    additions = driver._resolve_catalog_additions(
        [
            {
                "kind": "subclass",
                "name": "Onomancy",
                "source_selectors": [
                    {"heading_contains": "Onomancy", "match_all": True}
                ],
            }
        ],
        [
            {
                "id": "heading",
                "ordinal": 1,
                "heading_path": ["Onomancy"],
                "content": "",
            },
            {
                "id": "body",
                "ordinal": 2,
                "heading_path": ["Onomancy", "True Names"],
                "content": "Onomancy is the study of true names.",
            },
        ],
        relative_path="Twilight.pdf",
    )

    assert additions[0]["source_chunk_ids"] == ["heading", "body"]
    assert [
        span["source_chunk_id"] for span in additions[0]["source_spans"]
    ] == ["body"]


def test_portable_receiver_enables_exact_core_dependency() -> None:
    server = _FakeServer(
        [
            (
                "campaign_rules",
                {
                    "result": {
                        "activations": [],
                        "campaign_revision": 4,
                    }
                },
            ),
            (
                "campaign_rules",
                {
                    "result": {
                        "activation": {
                            "pack_id": "dnd5e.content.srd2014",
                            "version": "1.20.0",
                            "enabled": True,
                        }
                    }
                },
            ),
        ]
    )

    activation = asyncio.run(
        driver._enable_core_content_pack(
            server,
            campaign_id="receiver",
            edition="2014",
            run_id="reviewed-v2",
        )
    )

    assert activation["enabled"] is True
    assert server.calls[1][1]["payload"] == {
        "pack_id": "dnd5e.content.srd2014",
        "version": "1.20.0",
    }
    assert server.calls[1][1]["expected_revision"] == 4
    assert server.calls[1][1]["idempotency_key"].endswith(
        f"{driver._run_token('reviewed-v2')}-r4"
    )


def test_only_parse_ready_parameterized_statblock_skips_visual_ocr_recovery() -> None:
    candidates = [
        {
            "id": "homunculus",
            "kind": "statblock",
            "name": "Alchemical Homunculus",
            "source_chunk_ids": ["core"],
            "artifact": {
                "kind": "statblock",
                "card": {"name": "Alchemical Homunculus"},
            },
        }
    ]
    chunks = [
        {
            "id": "core",
            "content": (
                "Tiny construct, neutral Armor Class 13 (natural armor) "
                "Hit Points equal to five times your level in this class + "
                "your Intelligence modifier Speed 20 ft., fly 30 ft. "
                "STR DEX CON INT WIS CHA"
            ),
        }
    ]

    assert driver._statblock_recovery_needed(candidates, chunks) is True
    candidates[0]["artifact"]["card"]["normalized_content"] = (
        "# Alchemical Homunculus\n\n"
        "*Tiny construct, neutral*\n\n"
        "**Armor Class** 13 (natural armor)\n"
        "**Hit Points** equal to five times your level in this class + your "
        "Intelligence modifier\n"
        "**Speed** 20 ft., fly 30 ft.\n\n"
        "| STR | DEX | CON | INT | WIS | CHA |\n"
        "|---:|---:|---:|---:|---:|---:|\n"
        "| 4 (-3) | 15 (+2) | 12 (+1) | 10 (+0) | 10 (+0) | 7 (-2) |\n\n"
        "**Senses** darkvision 60 ft., passive Perception 10\n"
        "**Languages** understands the languages you speak\n"
        "**Challenge** —\n\n"
        "## Actions\n\n"
        "***Force Strike.*** *Ranged Weapon Attack:* your spell attack modifier "
        "to hit, range 30 ft., one target. *Hit:* 1d4 + PB force damage."
    )
    assert driver._statblock_recovery_needed(candidates, chunks) is False
    assert driver._statblock_recovery_needed([], chunks) is True
    assert driver._statblock_recovery_needed(
        [
            {
                "id": "broken",
                "kind": "statblock",
                "name": "Broken Creature",
                "source_chunk_ids": ["broken"],
                "artifact": {"kind": "statblock", "card": {"name": "Broken"}},
            }
        ],
        [{"id": "broken", "content": "Armor Class unreadable"}],
    ) is True


def test_statblock_index_triggers_recovery_for_a_missing_visual_card() -> None:
    candidates = [
        {
            "id": name.casefold(),
            "kind": "statblock",
            "name": name,
            "page_start": printed_page + 4,
            "source_chunk_ids": [],
            "artifact": {"kind": "statblock", "card": {"name": name}},
        }
        for name, printed_page in (("Alpha", 10), ("Beta", 11), ("Gamma", 12))
    ]
    chunks = [
        {
            "id": "index",
            "page_start": 2,
            "page_end": 2,
            "heading_path": ["Index of Statblocks"],
            "content": (
                "Alpha ..... 10\nBeta ..... 11\nGamma ..... 12\n"
                "Missing Horror ..... 13"
            ),
        },
        {"id": "last", "page_start": 20, "page_end": 20, "content": ""},
    ]

    assert driver._statblock_recovery_needed(candidates, chunks) is True


def test_unclaimed_statblock_core_triggers_recovery_without_a_printed_index() -> None:
    candidate = {
        "id": "known",
        "kind": "statblock",
        "name": "Known Creature",
        "source_chunk_ids": ["known"],
        "artifact": {
            "kind": "statblock",
            "card": {
                "name": "Known Creature",
                "normalized_content": (
                    "KNOWN CREATURE\nMedium humanoid, neutral\n"
                    "Armor Class 10\nHit Points 4 (1d8)\nSpeed 30 ft.\n"
                    "STR DEX CON INT WIS CHA\n10 (+0) 10 (+0) 10 (+0) "
                    "10 (+0) 10 (+0) 10 (+0)\n"
                    "Challenge 0 (10 XP)\nActions\nClub. Melee Weapon Attack: "
                    "+2 to hit, reach 5 ft., one target. Hit: 2 (1d4) bludgeoning damage."
                ),
            },
        },
    }
    chunks = [
        {"id": "known", "content": "Known Creature"},
        {
            "id": "missed",
            "content": (
                "MISSED HORROR Large aberration, evil Armor Class 16 "
                "Hit Points 90 Speed 30 ft. STR DEX CON INT WIS CHA"
            ),
        },
    ]

    assert driver._statblock_recovery_needed([candidate], chunks) is True


def test_strict_catalog_manifest_requires_every_selected_document() -> None:
    with pytest.raises(ValueError, match="no complete review"):
        driver._catalog_document_review(
            {"version": 1, "strict": True, "documents": {}},
            "Missing.pdf",
        )


def test_catalog_manifest_merges_relative_per_book_includes(tmp_path: Path) -> None:
    included = tmp_path / "included.json"
    included.write_text(
        json.dumps(
            {
                "version": 1,
                "documents": {
                    "Book B.pdf": {"complete_review": True},
                },
            }
        ),
        encoding="utf-8",
    )
    root = tmp_path / "root.json"
    root.write_text(
        json.dumps(
            {
                "version": 1,
                "strict": True,
                "includes": ["included.json"],
                "documents": {
                    "Book A.pdf": {"complete_review": True},
                },
            }
        ),
        encoding="utf-8",
    )

    manifest = driver._load_catalog_manifest(root)

    assert manifest["strict"] is True
    assert set(manifest["documents"]) == {"Book A.pdf", "Book B.pdf"}


def test_catalog_decisions_distinguish_same_named_contextual_features() -> None:
    candidates = [
        {
            "id": owner.casefold(),
            "kind": "feature",
            "name": "Tools of the Trade",
            "source_heading_path": ["Artificer Specialists", owner, "Tools of the Trade"],
            "artifact": {
                "kind": "feature",
                "card": {"name": "Tools of the Trade"},
            },
        }
        for owner in ("Alchemist", "Artillerist")
    ]
    review = {
        "default_status": "accepted",
        "decisions": [
            {
                "kind": "feature",
                "name": "Tools of the Trade",
                "source_heading_contains": owner,
                "artifact_patch": {"card": {"subclass_name": owner}},
            }
            for owner in ("Alchemist", "Artillerist")
        ],
        "expected_catalog": [
            {"kind": "feature", "name": "Tools of the Trade"},
            {"kind": "feature", "name": "Tools of the Trade"},
        ],
    }

    decisions = driver._review_spec_decisions(
        candidates,
        review,
        reviewer="agent:catalog",
        method="agent",
    )

    assert [
        item["artifact"]["card"]["subclass_name"] for item in decisions
    ] == ["Alchemist", "Artillerist"]


def test_portable_roundtrip_uses_public_facades_and_preserves_package() -> None:
    package = {
        "id": "dnd5e.regression.rulebook.0123456789abcdef",
        "version": "1.0.0",
        "checksum": "a" * 64,
        "metadata": {"definition_checksum": "b" * 64},
    }
    addon = {
        "id": f"{package['id']}.addon",
        "version": "1.0.0",
        "checksum": "c" * 64,
        "payload": {
            "manifest": {
                "readiness_policy": "build_time_complete",
                "readiness": {
                    "complete": True,
                    **{
                        dimension: {"complete": True, "blockers": []}
                        for dimension in ("source", "catalog", "selection", "runtime")
                    },
                },
                "resolution_readiness": {
                    "complete": True,
                    "first_use_compilation_required": False,
                    "unresolved": [],
                }
            }
        },
    }
    source = _FakeServer(
        [
            (
                "rule_pack_query",
                {
                    "result": [
                        {
                            "id": "source-chunk",
                            "ordinal": 0,
                            "heading_path": ["Rules"],
                            "content": "Exact indexed evidence.",
                        },
                        {
                            "id": "source-chunk-two",
                            "ordinal": 1,
                            "heading_path": ["Rules", "Random table"],
                            "content": "A random table with mechanical consequences.",
                        },
                        {
                            "id": "empty-heading-chunk",
                            "ordinal": 2,
                            "heading_path": ["Empty heading"],
                            "content": "",
                        },
                    ]
                },
            ),
            ("rule_pack_compile", {"result": {"status": "validated"}}),
            (
                "rule_pack_query",
                {
                    "result": {
                        "package": package,
                        "artifact": {"path": "managed-package.json"},
                    }
                },
            ),
            (
                "rule_pack_query",
                {
                    "result": {
                        "package": addon,
                        "artifact": {"path": "managed-addon.json"},
                    }
                },
            ),
        ]
    )
    target = _FakeServer(
        [
            (
                "rule_import",
                {
                    "result": {
                        "installed": True,
                        "activated": False,
                        "components": [{"status": "installed"}],
                    }
                },
            ),
            ("campaign_rules", {"result": {"campaign_revision": 1}}),
            ("campaign_rules", {"result": {"activation": {"enabled": True}}}),
            ("campaign_query", {"result": {"revision": 2}}),
            ("campaign_rules", {"result": {"activation": {"enabled": False}}}),
            ("rule_pack_query", {"result": {"package": package}}),
            ("rule_pack_query", {"result": {"package": addon}}),
            (
                "rule_pack_query",
                {"result": [{"id": "fresh-source", "source_key": "regression.book"}]},
            ),
        ]
    )

    result = asyncio.run(
        driver._portable_roundtrip(
            source_server=source,
            source_campaign_id="source-campaign",
            target_server=target,
            target_campaign_id="target-campaign",
                source_id="local-source",
                source_key="regression.book",
                job_id="job",
                candidates=[],
            relative_path="book.pdf",
            edition="2014",
            run_id="one",
            id_key="request",
            primary_reviewer="agent:test-primary",
            primary_review_method="agent",
            critic_reviewer="agent:test-critic",
            critic_review_method="agent",
        )
    )

    assert result["reexport_identical"] is True
    assert result["fresh_source_ids"] is True
    assert result["installed"] is True
    assert result["activated"] is True
    assert result["deactivated"] is True
    assert [name for name, _arguments in source.calls] == [
        "rule_pack_query",
        "rule_pack_compile",
        "rule_pack_query",
        "rule_pack_query",
    ]
    fallback_compile = source.calls[1][1]["payload"]
    assert len(fallback_compile["artifacts"]) == 2
    assert all(
        item["mechanical_scope"] == "mechanical"
        for item in fallback_compile["artifacts"]
    )
    assert [
        item["source_chunk_ids"] for item in fallback_compile["artifacts"]
    ] == [["source-chunk"], ["source-chunk-two"]]
    assert fallback_compile["provenance"]["source_catalog_fallback"] == (
        "per_chunk_source_bound_agent_ruling"
    )
    assert fallback_compile["provenance"]["source_catalog_artifact_count"] == 2
    assert fallback_compile["provenance"]["empty_source_chunk_count"] == 1
    assert "descriptive_fallback" not in fallback_compile["provenance"]
    assert fallback_compile["manifest"]["dependencies"] == [
        {"id": "dnd5e.content.srd2014", "version": "1.20.0"}
    ]
    assert [name for name, _arguments in target.calls] == [
        "rule_import",
        "campaign_rules",
        "campaign_rules",
        "campaign_query",
        "campaign_rules",
        "rule_pack_query",
        "rule_pack_query",
        "rule_pack_query",
    ]
    assert target.calls[2][1]["idempotency_key"] == (
        "regression-addon-enable-request-r1"
    )
    assert target.calls[4][1]["idempotency_key"] == (
        "regression-addon-disable-request-r2"
    )


def test_portable_roundtrip_rejects_deferred_actor_presets() -> None:
    package = {
        "id": "dnd5e.addon.rulebook.example",
        "version": "1.0.0",
        "checksum": "a" * 64,
        "metadata": {"definition_checksum": "b" * 64},
    }
    addon = {
        "id": f"{package['id']}.addon",
        "version": "1.0.0",
        "checksum": "c" * 64,
    }
    candidate = {
        "id": "candidate:unusual-creature",
        "kind": "statblock",
        "artifact": {
            "id": f"{package['id']}.statblock.unusual-creature",
            "kind": "statblock",
            "card": {"name": "Unusual Creature", "normalized_content": ""},
            "source_chunk_ids": ["source-chunk"],
        },
    }
    source = _FakeServer(
        [
            (
                "rule_pack_query",
                {
                    "result": [
                        {
                            "id": "source-chunk",
                            "content": "Exact indexed creature evidence.",
                        }
                    ]
                },
            ),
                (
                    "rule_import",
                    {
                        "result": {
                            "candidates": [
                                {
                                    "id": candidate["id"],
                                    "review_status": "needs_revision",
                                }
                            ]
                        }
                    },
                ),
                (
                    "rule_import",
                    {
                        "result": {
                            "candidates": [
                                {
                                    "id": candidate["id"],
                                    "review_status": "accepted",
                                }
                            ]
                        }
                    },
                ),
            ("rule_import", {"result": {"draft": {"status": "validated"}}}),
            (
                "rule_pack_query",
                {"result": {"package": package, "artifact": {"path": "rules.json"}}},
            ),
            (
                "rule_pack_query",
                {
                    "result": {
                        "package": None,
                        "artifact": None,
                        "summary": {
                            "cards": 0,
                            "failures": [
                                {
                                    "artifact_id": candidate["artifact"]["id"],
                                    "error": "not normalized",
                                }
                            ],
                            "complete": False,
                            "deferred": 1,
                        },
                    }
                },
            ),
            (
                "rule_pack_query",
                {"result": {"package": addon, "artifact": {"path": "addon.json"}}},
            ),
        ]
    )
    target = _FakeServer(
        [
            (
                "rule_import",
                {
                    "result": {
                        "installed": True,
                        "activated": False,
                        "components": [{"status": "installed"}],
                    }
                },
            ),
            ("campaign_rules", {"result": {"campaign_revision": 1}}),
            ("campaign_rules", {"result": {"activation": {"enabled": True}}}),
            ("campaign_query", {"result": {"revision": 2}}),
            ("campaign_rules", {"result": {"activation": {"enabled": False}}}),
            ("rule_pack_query", {"result": {"package": package}}),
            ("rule_pack_query", {"result": {"package": addon}}),
            (
                "rule_pack_query",
                {"result": [{"id": "fresh-source", "source_key": "regression.book"}]},
            ),
        ]
    )

    with pytest.raises(RuntimeError, match="actor preset export is incomplete"):
        asyncio.run(
            driver._portable_roundtrip(
                source_server=source,
                source_campaign_id="source-campaign",
                target_server=target,
                target_campaign_id="target-campaign",
                source_id="local-source",
                source_key="regression.book",
                job_id="job",
                candidates=[candidate],
                relative_path="example.pdf",
                edition="2014",
                run_id="one",
                id_key="request",
                primary_reviewer="agent:test-primary",
                primary_review_method="agent",
                critic_reviewer="agent:test-critic",
                critic_review_method="agent",
            )
        )

    preset_call = source.calls[5]
    decision = source.calls[1][1]["payload"]["decisions"][0]
    assert decision["id"] == candidate["id"]
    assert decision["review_status"] == "accepted"
    assert decision["catalog_review_decision"]["role"] == "primary"
    critic_decision = source.calls[2][1]["payload"]["decisions"][0]
    assert critic_decision["catalog_review_decision"]["role"] == "critic"
    assert preset_call[0] == "rule_pack_query"
    assert preset_call[1]["view"] == "preset_package"
    assert preset_call[1]["payload"]["allow_partial"] is True
    assert [
        item["role"]
        for item in preset_call[1]["payload"]["catalog_review_decisions"]
    ] == ["primary", "critic"]
    assert len(source.calls) == 6
    assert target.calls == []


def test_release_check_is_inspection_only() -> None:
    manifest = {"kind": "release_manifest"}
    source = _FakeServer(
        [
            (
                "rule_pack_query",
                {
                    "result": {
                        "artifact": {"path": "release.json"},
                        "release_manifest": manifest,
                    }
                },
            )
        ]
    )
    target = _FakeServer(
        [
            (
                "rule_import",
                {
                    "result": {
                        "release": {"checksum": "c" * 64},
                        "components": [
                            {
                                "local_status": "installed",
                                "portable_checksum_status": "match",
                            }
                        ],
                        "authority": "manifest_only",
                        "auto_install": False,
                        "auto_activate": False,
                    }
                },
            )
        ]
    )

    result = asyncio.run(
        driver._portable_release_check(
            source_server=source,
            source_campaign_id="source-campaign",
            target_server=target,
            target_campaign_id="target-campaign",
            components=[
                {
                    "kind": "rule_pack",
                    "id": "dnd5e.example.rules",
                    "version": "1.0.0",
                    "checksum": "a" * 64,
                    "optional": False,
                }
            ],
            run_id="one",
        )
    )

    assert result["authority"] == "manifest_only"
    assert result["auto_install"] is False
    assert result["auto_activate"] is False
    assert result["all_components_installed"] is True
    assert result["all_envelope_checksums_match"] is True
