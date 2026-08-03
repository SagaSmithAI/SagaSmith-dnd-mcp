from __future__ import annotations

import asyncio
import hashlib
import json
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


def test_portable_pack_id_is_stable_across_regression_runs() -> None:
    first = driver._portable_pack_id("book.pdf", run_id="one")
    assert first == driver._portable_pack_id("book.pdf", run_id="one")
    assert first == driver._portable_pack_id("book.pdf", run_id="two")
    assert first.startswith("dnd5e.addon.rulebook.book.")


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


def test_parameterized_statblock_skips_visual_ocr_recovery() -> None:
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
