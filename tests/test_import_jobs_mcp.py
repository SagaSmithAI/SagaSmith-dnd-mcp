import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from mcp.types import ImageContent, TextContent
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sagasmith_core import (
    OcrPageLayout,
    OcrTextBlock,
    RapidOcrProvider,
)
from sagasmith_core.rules import RuleService
from sagasmith_dnd.statblock_ocr import (
    matching_statblock_recovery_pair as _matching_statblock_recovery_pair,
)
from sagasmith_dnd.statblocks import parse_2014_statblock

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import (
    _artifact_source_pages,
    _bounded_ocr_heading_equivalent,
    _bundled_mm2014_actor_card,
    _canonical_statblock_artifact_for_mechanics,
    _canonical_statblock_artifact_for_review,
    _catalog_identity_is_evidenced,
    _catalog_statblock_text_superseding_source_review,
    _claim_catalog_artifact_for_source_review,
    _compact_transcription_key,
    _merge_statblock_discoveries,
    _noisy_ocr_heading_equivalent,
    _ocr_fact_key,
    _portable_statblock_review_audit,
    _project_recovered_statblock_candidates,
    _select_catalog_ocr_identity_evidence,
    _select_preferred_statblock_reviews,
    _source_statblock_hint_additions,
    _source_statblock_recovery_hints,
    _statblock_index_recovery_hints,
    _statblock_mechanical_identity,
    _statblock_ocr_discovery_needed,
    _valid_statblock_heading,
    create_server,
)
from tests.authoring_helpers import finalize_and_activate_module


def test_bounded_ocr_heading_equivalence_allows_one_glyph_only() -> None:
    assert _bounded_ocr_heading_equivalent("0INOLOTH", "OINOLOTH")
    assert _bounded_ocr_heading_equivalent("Jarad Von Savo", "JARAD VOD SAVO")


def test_transcription_context_key_preserves_non_ascii_words() -> None:
    assert _compact_transcription_key("## 章节 标题") == "章节标题"
    assert _compact_transcription_key("Épée +1") == "épée1"
    assert not _bounded_ocr_heading_equivalent("Imp", "Ink")


def test_catalog_identity_can_span_ordered_sibling_headings_only() -> None:
    evidence = (
        "Channel Divinity: Twilight Sanctuary "
        "Divine Domain Channel Divinity: Twilight Divine Domain Sanctuary "
        "2nd-level Twilight Domain feature"
    )

    assert _catalog_identity_is_evidenced("Channel Divinity: Twilight Sanctuary", evidence)
    assert _catalog_identity_is_evidenced(
        "The Right Tool for the Job",
        "THE RIGHT TO OL FOR THE OB Create artisan tools at 3rd level",
    )
    assert not _catalog_identity_is_evidenced(
        "Channel Divinity: Solar Twilight Sanctuary", evidence
    )
    assert not _catalog_identity_is_evidenced("Sanctuary Twilight", evidence)
    assert _catalog_identity_is_evidenced(
        "House Agent (Cannith)",
        "HOUSE AGENT Tool Proficiency: Two tools by house: Cannith",
    )
    assert _catalog_identity_is_evidenced(
        "House Agent (Vadalis)",
        "HOUSE AGENT Tool Proficiency: Vada I is Herbalism kit and vehicles",
    )
    assert not _catalog_identity_is_evidenced(
        "House Agent (Cannith Operative)",
        "HOUSE AGENT Tool Proficiency: Two tools by house: Cannith",
    )
    assert _catalog_identity_is_evidenced(
        "Tiefling (Feral + Winged)",
        "TIEFLING VARIANTS Feral replaces the ability increase. Winged replaces Infernal Legacy.",
    )
    assert not _catalog_identity_is_evidenced(
        "Tiefling (Feral + Winged + Aquatic)",
        "TIEFLING VARIANTS Feral replaces the ability increase. Winged replaces Infernal Legacy.",
    )
    assert not _bounded_ocr_heading_equivalent("Female Steeder", "Male Steeder")


def test_catalog_identity_ocr_evidence_can_fall_back_to_another_local_model() -> None:
    selected = _select_catalog_ocr_identity_evidence(
        "Orc",
        [
            {
                "provider": "rapidocr",
                "profile": "medium-profile",
                "model": "medium",
                "scale": 2.0,
                "page_number": 33,
                "text_sha256": "medium-checksum",
                "text": "0 RC TRAITS An ore character has these traits.",
            },
            {
                "provider": "rapidocr",
                "profile": "small-profile",
                "model": "small",
                "scale": 2.0,
                "page_number": 33,
                "text_sha256": "small-checksum",
                "text": "ORC TRAITS An orc character has these traits.",
            },
        ],
    )

    assert selected == {
        "provider": "rapidocr",
        "profile": "small-profile",
        "model": "small",
        "scale": 2.0,
        "page_number": 33,
        "text_sha256": "small-checksum",
    }


def test_bundled_mm_actor_reuse_is_book_bound_and_requires_one_match() -> None:
    cards = [
        {"id": "actor.tiger", "payload": {"name": "Tiger"}},
        {"id": "actor.drider", "payload": {"name": "Drider"}},
    ]

    matched = _bundled_mm2014_actor_card(
        name="llGER",
        edition="2014",
        publication_id="mm2014",
        cards=cards,
    )

    assert matched == cards[0]
    assert matched is not cards[0]
    assert (
        _bundled_mm2014_actor_card(
            name="Tiger",
            edition="2014",
            publication_id="vgm2014",
            cards=cards,
        )
        is None
    )
    assert (
        _bundled_mm2014_actor_card(
            name="Tiger",
            edition="2024",
            publication_id="mm2014",
            cards=cards,
        )
        is None
    )
    assert (
        _bundled_mm2014_actor_card(
            name="Tiger",
            edition="2014",
            publication_id="mm2014",
            cards=[*cards, {"id": "actor.tiger.duplicate", "payload": {"name": "Tiger"}}],
        )
        is None
    )


def test_noisy_review_heading_match_requires_visible_ocr_damage() -> None:
    assert _noisy_ocr_heading_equivalent("I FOM01,HAN", "Fomorian")
    assert _noisy_ocr_heading_equivalent("ARCANALOT>< IN SIGIL", "Arcanaloth")
    assert not _noisy_ocr_heading_equivalent("Veteran", "Veteran")
    assert not _noisy_ocr_heading_equivalent("Veteran", "Vermin")


def test_review_identity_uses_one_same_page_catalog_artifact_only() -> None:
    assert _canonical_statblock_artifact_for_review(
        "JARAD Von SAvo",
        [("Jarad Vod Savo", "artifact.jarad")],
    ) == ("Jarad Vod Savo", "artifact.jarad")
    assert (
        _canonical_statblock_artifact_for_review(
            "CATEGORY l KRASIS",
            [
                ("Category 1 Krasis", "artifact.one"),
                ("Category 2 Krasis", "artifact.two"),
            ],
        )
        is None
    )


def test_actor_catalog_identity_covers_the_full_source_citation_range() -> None:
    assert _artifact_source_pages(
        {
            "source_citations": [
                {"page_start": 252, "page_end": 253},
                {"page_start": 0, "page_end": 10},
                {"page_start": 9, "page_end": 8},
            ]
        }
    ) == {252, 253}


def test_catalog_identity_can_use_one_unique_same_page_mechanical_match() -> None:
    assert _canonical_statblock_artifact_for_mechanics(
        "mechanics:commander",
        [
            ("mechanics:gish", "Githyanki Gish", "artifact.gish"),
            (
                "mechanics:commander",
                "Githyanki Supreme Commander",
                "artifact.commander",
            ),
        ],
    ) == ("Githyanki Supreme Commander", "artifact.commander")
    assert (
        _canonical_statblock_artifact_for_mechanics(
            "mechanics:shared",
            [
                ("mechanics:shared", "First", "artifact.first"),
                ("mechanics:shared", "Second", "artifact.second"),
            ],
        )
        is None
    )


def test_statblock_discovery_unions_ocr_only_siblings() -> None:
    merged = _merge_statblock_discoveries(
        [{"name": "VELOCIRAPTOR"}, {"name": "i·"}],
        primary_provider="pdf-text-layout",
        secondary=[
            {"name": "VELOCIRAPTOR"},
            {"name": "QUETZALCOATLUS"},
            {"name": "• '1"},
        ],
        secondary_provider="rapidocr",
    )

    assert [(item["name"], provider) for item, provider in merged] == [
        ("VELOCIRAPTOR", "pdf-text-layout"),
        ("QUETZALCOATLUS", "rapidocr"),
    ]
    assert _ocr_fact_key("any álignment") == _ocr_fact_key("any alignment")


def test_source_statblock_hints_fill_only_unclaimed_layout_identities() -> None:
    identities = [
        {"text": "Medium humanoid (changeling), any alignment"},
        {"text": "Medium humanoid (kalashtar), any alignment"},
    ]
    discoveries = [{"name": "CHANGELING"}, {"name": "KALASHTAR"}]

    assert (
        _source_statblock_hint_additions(
            discoveries,
            ["AS A KALASHTAR"],
            layout_blocks=identities,
        )
        == []
    )
    assert _source_statblock_hint_additions(
        [],
        ["RADIANT IDOL"],
        layout_blocks=[{"text": "Large celestial, lawful evil"}],
    ) == ["RADIANT IDOL"]


def test_statblock_ocr_discovery_fills_empty_and_partially_paired_text_layers() -> None:
    assert _statblock_ocr_discovery_needed(
        [],
        layout_blocks=[],
        usable_catalog_count=0,
    )
    assert _statblock_ocr_discovery_needed(
        [{"name": "CLAWFOOT"}],
        layout_blocks=[
            {"text": "Medium beast, unaligned"},
            {"text": "Medium beast, unaligned"},
        ],
        usable_catalog_count=0,
    )
    assert not _statblock_ocr_discovery_needed(
        [{"name": "MORDAKHESH"}],
        layout_blocks=[{"text": "Medium.fiend, lawful evil"}],
        usable_catalog_count=0,
    )
    assert not _statblock_ocr_discovery_needed(
        [],
        layout_blocks=[],
        usable_catalog_count=1,
    )
    assert _statblock_ocr_discovery_needed(
        [],
        layout_blocks=[
            {"text": "Medium undead, lawful evil"},
            {"text": "Armor Class 9"},
            {"text": "Hit Points 63"},
            {"text": "Speed 0 ft., fly 40 ft."},
            {"text": "Armor Class 19"},
            {"text": "Hit Points 95"},
            {"text": "Speed 25 ft."},
        ],
        usable_catalog_count=1,
    )
    assert _statblock_ocr_discovery_needed(
        [{"name": "GIANT EAGLE"}, {"name": "GIANT SPIDER"}, {"name": "IMP"}],
        layout_blocks=[
            *[{"text": "Armar Class 13"} for _ in range(4)],
            *[{"text": "Hil Poinls 10 (3d4 + 3)"} for _ in range(4)],
            *[{"text": "Speed 20 fI."} for _ in range(4)],
        ],
        usable_catalog_count=0,
    )


def test_recovered_statblock_projection_preserves_unreviewed_usable_pages() -> None:
    projected = _project_recovered_statblock_candidates(
        [
            {
                "id": "steel-defender",
                "kind": "statblock",
                "page_start": 62,
                "page_end": 62,
            },
            {
                "id": "noisy-kalaraq",
                "kind": "statblock",
                "page_start": 307,
                "page_end": 307,
            },
            {
                "id": "other-feature",
                "kind": "feature",
                "page_start": 307,
                "page_end": 307,
            },
        ],
        [
            {
                "id": "reviewed-kalaraq",
                "kind": "statblock",
                "page_start": 307,
                "page_end": 307,
            }
        ],
        complete_pages={307},
    )

    assert [item["id"] for item in projected] == [
        "steel-defender",
        "other-feature",
        "reviewed-kalaraq",
    ]


def test_source_reviews_claim_only_one_accepted_catalog_artifact() -> None:
    claimed: set[str] = set()

    assert (
        _claim_catalog_artifact_for_source_review(("Fomorian", "artifact:fomorian"), claimed)
        == "accepted"
    )
    assert (
        _claim_catalog_artifact_for_source_review(("Fomorian", "artifact:fomorian"), claimed)
        == "duplicate_review_for_catalog_artifact"
    )
    assert _claim_catalog_artifact_for_source_review(None, claimed) == "not_in_accepted_catalog"


def test_catalog_reviewed_boundary_supersedes_only_a_distinct_evidenced_review() -> None:
    source_text = "# Tiny Servant\n\n***Slam.*** Hit: 5 bludgeoning damage."
    source_checksum = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    artifact = {
        "kind": "statblock",
        "card": {
            "normalized_content": source_text,
            "review_evidence": {"normalized_content_sha256": source_checksum},
        },
    }

    assert (
        _catalog_statblock_text_superseding_source_review(
            artifact,
            "0" * 64,
        )
        == source_text
    )
    assert (
        _catalog_statblock_text_superseding_source_review(
            artifact,
            source_checksum,
        )
        == ""
    )
    artifact["card"]["review_evidence"]["normalized_content_sha256"] = "f" * 64
    assert (
        _catalog_statblock_text_superseding_source_review(
            artifact,
            "0" * 64,
        )
        == ""
    )


def test_partial_page_statblock_projection_replaces_only_an_exact_review_match() -> None:
    commoner = """# Commoner

*Medium humanoid, any alignment*

**Armor Class** 10
**Hit Points** 4 (1d8)
**Speed** 30 ft.

| STR | DEX | CON | INT | WIS | CHA |
|:---:|:---:|:---:|:---:|:---:|:---:|
| 10 (+0) | 10 (+0) | 10 (+0) | 10 (+0) | 10 (+0) | 10 (+0) |

**Senses** passive Perception 10
**Languages** Common
**Challenge** 0 (10 XP)

###### Actions

***Club***. *Melee Weapon Attack:* +2 to hit, reach 5 ft., one target.
*Hit:* 2 (1d4) bludgeoning damage.
"""
    guard = commoner.replace("# Commoner", "# Guard").replace(
        "**Armor Class** 10", "**Armor Class** 16"
    )
    projected = _project_recovered_statblock_candidates(
        [
            {
                "id": "noisy-commoner",
                "kind": "statblock",
                "name": "C0MMONER",
                "page_start": 1,
                "page_end": 1,
                "artifact": {"card": {"normalized_content": commoner}},
            },
            {
                "id": "guard",
                "kind": "statblock",
                "name": "Guard",
                "page_start": 1,
                "page_end": 1,
                "artifact": {"card": {"normalized_content": guard}},
            },
            {
                "id": "ambiguous-noise",
                "kind": "statblock",
                "name": "ACTIONS",
                "page_start": 1,
                "page_end": 1,
                "artifact": {"card": {"normalized_content": ""}},
            },
        ],
        [
            {
                "id": "reviewed-commoner",
                "kind": "statblock",
                "name": "Reviewed Commoner",
                "page_start": 1,
                "page_end": 1,
                "artifact": {"card": {"normalized_content": commoner}},
            }
        ],
        complete_pages=set(),
    )

    assert [item["id"] for item in projected] == [
        "guard",
        "ambiguous-noise",
        "reviewed-commoner",
    ]


def test_source_statblock_hints_skip_structural_page_headers() -> None:
    hints = _source_statblock_recovery_hints(
        [
            {
                "page_start": 293,
                "heading_path": [
                    "Ch. 6: Friends and Foes",
                    "Dusk Hag",
                    "CHAPTER 6 | FRIENDS AND FOES",
                ],
                "content": ("Medium fey, neutral evil Armor Class 17 Hit Points 82 Speed 30 ft."),
            },
            {
                "page_start": 294,
                "heading_path": ["Ch. 6", "ACTIONS"],
                "content": "Armor Class 10 Hit Points 2 Speed 30 ft.",
            },
            {
                "page_start": 295,
                "heading_path": [
                    "Ch. 6: Bestiary",
                    "GITHYANKI GISH",
                    "GITHYANKI SUPREME",
                    "COMMANDER",
                ],
                "content": (
                    "Medium humanoid, lawful evil Armor Class 18 Hit Points 187 Speed 30 ft."
                ),
            },
        ]
    )

    assert hints == {
        "entry_count": 2,
        "by_page": {
            293: ["Dusk Hag"],
            295: ["GITHYANKI SUPREME COMMANDER"],
        },
    }


def test_statblock_index_hints_require_a_corroborated_printed_page_offset() -> None:
    chunks = [
        {
            "heading_path": ["Index of Stat Blocks - Monsters and NPCs"],
            "content": (
                "Cackler ........ 195 Kraul Warrior ........ 213 "
                "Skyjek Roc ........ 219 Nivix Cyctops ........ 216"
            ),
        }
    ]
    candidates = [
        {"name": "CACKLER", "page_start": 196},
        {"name": "KRAUL WARRIOR", "page_start": 214},
        {"name": "SKYJEK ROC", "page_start": 220},
    ]

    hints = _statblock_index_recovery_hints(chunks, candidates, page_count=230)

    assert hints["entry_count"] == 4
    assert hints["page_offset"] == 1
    assert hints["offset_support"] == 3
    assert hints["by_page"][217] == ["Nivix Cyctops"]


def test_statblock_corroboration_can_select_a_later_exact_scale_pair() -> None:
    first = {"critical_facts": {"name": "Deer", "fields": {"Languages": "-"}}}
    later = {"critical_facts": {"name": "Deer", "fields": {}}}

    pair = _matching_statblock_recovery_pair([(2.5, first), (3.0, later), (3.5, later)])

    assert pair is not None
    assert [item[0] for item in pair] == [3.0, 3.5]


def test_preset_export_selects_one_strongest_review_per_source_card() -> None:
    selected = _select_preferred_statblock_reviews(
        [
            {
                "id": "zariel-v6",
                "page_number": 181,
                "review_mode": "layout_ocr",
                "observation": "Text-only layout OCR v6 recovered Zari El.",
                "normalized_content": "# ZARI EL\n\nold",
            },
            {
                "id": "zariel-v10",
                "page_number": 181,
                "review_mode": "layout_ocr",
                "observation": "Text-only layout OCR v10 recovered Zariel.",
                "normalized_content": "# ZARIEL\n\ncomplete",
            },
            {
                "id": "hungry-indexed-filled",
                "page_number": 233,
                "review_mode": "indexed_text",
                "observation": "indexed",
                "normalized_content": "# THE HUNGRY\n\nindexed",
                "agent_statblock_fill": {"resolution_plans": []},
            },
            {
                "id": "hungry-visual",
                "page_number": 233,
                "review_mode": "visual",
                "observation": "visual",
                "normalized_content": "# The Hungry\n\ncomplete visual review",
            },
            {
                "id": "female-steeder",
                "page_number": 239,
                "review_mode": "layout_text",
                "observation": "layout",
                "normalized_content": "# FEMALE STEEDER\n\ncomplete",
            },
            {
                "id": "male-steeder",
                "page_number": 239,
                "review_mode": "layout_text",
                "observation": "layout",
                "normalized_content": "# MALE STEEDER\n\ncomplete",
            },
            {
                "id": "category-one-letter",
                "page_number": 211,
                "review_mode": "layout_text",
                "observation": "layout",
                "normalized_content": "# CATEGORY l KRASIS\n\ncomplete",
            },
            {
                "id": "category-one-digit",
                "page_number": 211,
                "review_mode": "layout_text",
                "observation": "layout",
                "normalized_content": "# CATEGORY 1 KRASIS\n\ncomplete",
            },
            {
                "id": "ocr-debris",
                "page_number": 237,
                "review_mode": "layout_ocr",
                "observation": "layout",
                "normalized_content": "# i\u00b7\n\nnot an actor identity",
            },
            {
                "id": "caption-base",
                "page_number": 121,
                "review_mode": "layout_ocr",
                "observation": "layout",
                "normalized_content": "# -PELLANISTRA THE DRIDER\n\ncomplete",
            },
            {
                "id": "drider-agent-correction",
                "page_number": 121,
                "review_mode": "layout_ocr",
                "observation": "Agent named structural slot",
                "normalized_content": "# DRIDER\n\ncomplete",
                "derived_from_review_id": "caption-base",
            },
        ]
    )

    assert {item["id"] for item in selected} == {
        "zariel-v10",
        "hungry-visual",
        "female-steeder",
        "male-steeder",
        "category-one-digit",
        "drider-agent-correction",
    }
    assert _valid_statblock_heading("Ox") is True
    assert _valid_statblock_heading("i\u00b7") is False


def test_agent_named_slot_wins_local_id_ties_and_reviews_keep_stable_source_order() -> None:
    raw = {
        "id": "z-local-id",
        "page_number": 12,
        "review_mode": "layout_ocr",
        "observation": "Text-only layout OCR v20 recovered WEREWOLF.",
        "normalized_content": "# WEREWOLF\n\ncomplete mechanics",
    }
    corrected = {
        "id": "a-local-id",
        "page_number": 12,
        "review_mode": "layout_ocr",
        "observation": (
            "Text-only layout OCR v20 recovered Werewolf. "
            "Agent named structural statblock slot; the engine retained mechanics."
        ),
        "normalized_content": "# Werewolf\n\ncomplete mechanics",
    }
    later = {
        "id": "different-local-id",
        "page_number": 40,
        "review_mode": "layout_ocr",
        "observation": "Text-only layout OCR v20 recovered Aboleth.",
        "normalized_content": "# Aboleth\n\ncomplete mechanics",
    }

    selected = _select_preferred_statblock_reviews([later, raw, corrected])

    assert [item["normalized_content"].splitlines()[0] for item in selected] == [
        "# Werewolf",
        "# Aboleth",
    ]


def test_portable_excluded_review_audit_uses_content_identity_not_local_id() -> None:
    source_text = "# Trostani\n\nreviewed mechanics"
    first = _portable_statblock_review_audit(
        {
            "id": "rule-statblock-review:first-local-id",
            "page_number": 230,
            "normalized_content": source_text,
        },
        reviewed_name="Trostani",
        basis="duplicate_review_for_catalog_artifact",
    )
    second = _portable_statblock_review_audit(
        {
            "id": "rule-statblock-review:second-local-id",
            "page_number": 230,
            "normalized_content": source_text,
        },
        reviewed_name="Trostani",
        basis="duplicate_review_for_catalog_artifact",
    )

    assert first == second
    assert "review_id" not in first
    assert first["source_review_sha256"] == hashlib.sha256(source_text.encode("utf-8")).hexdigest()


def test_statblock_mechanical_identity_matches_corrected_ocr_heading() -> None:
    body = """
*Medium undead, lawful evil*

**Armor Class** 16
**Hit Points** 45 (6d8 + 18)
**Speed** 30 ft.

| STR | DEX | CON | INT | WIS | CHA |
|---:|---:|---:|---:|---:|---:|
| 18 (+4) | 12 (+1) | 17 (+3) | 6 (-2) | 9 (-1) | 10 (+0) |

**Challenge** 3 (700 XP)

## Actions

***Longsword.*** Melee Weapon Attack: +6 to hit, reach 5 ft., one target.
Hit: 8 (1d8 + 4) slashing damage.
"""
    damaged = parse_2014_statblock(f'# • • "-\n{body}', source_key="damaged")
    reviewed = parse_2014_statblock(f"# SWORD WRAITH WARRIOR\n{body}", source_key="reviewed")

    assert _statblock_mechanical_identity(damaged) == (_statblock_mechanical_identity(reviewed))


def test_rulebook_draft_agent_can_add_only_source_bound_catalog_entities(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    rulebook = import_root / "specialists.md"
    rulebook.write_text(
        (
            "# Artificer Specialists\n\n"
            "A gunsmith is a master engineer who forges a firearm powered by magic.\n\n"
            "### Master Smith\n\n"
            "When you choose this specialization at 1st level, you gain proficiency "
            "with smith's tools.\n\n"
            "# Rival Specialists\n\n"
            "### Master Smith\n\n"
            "When you choose this rival specialization at 1st level, you gain "
            "proficiency with jeweler's tools.\n"
        ),
        encoding="utf-8",
    )
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        rule_import_roots=(import_root,),
    )

    async def _call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Agent catalog", "idempotency_key": "catalog-campaign"},
        )
        staged = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(rulebook),
                    "source_key": "agent-catalog-test",
                    "title": "Agent Catalog Test",
                    "edition": "2014",
                },
                "idempotency_key": "catalog-stage",
            },
        )
        job_id = staged["job"]["id"]
        await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "idempotency_key": "catalog-inspect",
            },
        )
        await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "idempotency_key": "catalog-ingest",
            },
        )
        extracted = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "idempotency_key": "catalog-extract",
            },
        )
        chunks = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "evidence",
                "payload": {
                    "job_id": job_id,
                    "kind": "chunks",
                    "query": "gunsmith",
                },
            },
        )
        gunsmith_chunk = next(
            item for item in chunks if "gunsmith" in item.get("content", "").casefold()
        )
        evidence = "A gunsmith is a master engineer who forges a firearm powered by magic."
        evidence_start = gunsmith_chunk["content"].index(evidence)
        arguments = {
            "campaign_id": campaign["id"],
            "action": "edit",
            "payload": {
                "operation": "catalog",
                "job_id": job_id,
                "rationale": "The layout parser found the feature but missed its parent subclass.",
                "additions": [
                    {
                        "kind": "subclass",
                        "name": "Gunsmith",
                        "source_spans": [
                            {
                                "source_chunk_id": gunsmith_chunk["id"],
                                "start": evidence_start,
                                "end": evidence_start + len(evidence),
                                "checksum": hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
                            }
                        ],
                        "card": {
                            "class_name": "Artificer",
                            "minimum_level": 1,
                            "spell_grants": [],
                            "description": "invented text must not survive",
                        },
                    }
                ],
            },
            "expected_revision": extracted["job"]["revision"],
            "idempotency_key": "catalog-augment",
        }
        augmented = await _call(server, "rulebook_draft", arguments)
        added = next(
            item
            for item in augmented["candidates"]
            if item["id"] in augmented["added_candidate_ids"]
        )
        assert added["name"] == "Gunsmith"
        assert added["artifact"]["card"]["description"] == evidence
        assert added["source_spans"][0]["start"] == evidence_start
        assert "invented text" not in added["artifact"]["card"]["description"]
        assert added["source_citations"]
        replay = await _call(server, "rulebook_draft", arguments)
        assert replay["job"]["revision"] == augmented["job"]["revision"]

        master_smiths = [item for item in augmented["candidates"] if item["name"] == "Master Smith"]
        assert len(master_smiths) == 2
        master_smith = next(
            item
            for item in master_smiths
            if "smith's tools" in item["artifact"]["card"]["description"]
            and "jeweler's tools" not in item["artifact"]["card"]["description"]
        )
        replacement = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "operation": "catalog",
                    "job_id": job_id,
                    "rationale": ("The automatic card omitted the bounded class feature fields."),
                    "additions": [
                        {
                            "kind": "feature",
                            "name": "Master Smith",
                            "replace_existing": True,
                            "source_chunk_ids": master_smith["source_chunk_ids"],
                            "card": {
                                "class_name": "Artificer",
                                "subclass_name": "Gunsmith",
                                "minimum_level": 1,
                                "selection_requirements": {},
                                "selection_requirements_by_level": {},
                                "mechanical_grants": {"tool_proficiencies": ["Smith's Tools"]},
                            },
                        }
                    ],
                },
                "expected_revision": augmented["job"]["revision"],
                "idempotency_key": "catalog-replace",
            },
        )
        assert replacement["replaced_candidate_ids"] == [master_smith["id"]]
        assert all(item["id"] != master_smith["id"] for item in replacement["candidates"])
        assert any(
            item["id"]
            == next(
                candidate["id"]
                for candidate in master_smiths
                if candidate["id"] != master_smith["id"]
            )
            for item in replacement["candidates"]
        )
        replacement_card = next(
            item
            for item in replacement["candidates"]
            if item["id"] in replacement["added_candidate_ids"]
        )
        assert (
            replacement_card["agent_catalog_addition"]["replaced_candidate_id"]
            == master_smith["id"]
        )

        with pytest.raises(Exception, match="outside the indexed source"):
            await _call(
                server,
                "rulebook_draft",
                {
                    **arguments,
                    "payload": {
                        **arguments["payload"],
                        "additions": [
                            {
                                "kind": "subclass",
                                "name": "Invented",
                                "source_chunk_ids": ["foreign-chunk"],
                            }
                        ],
                    },
                    "expected_revision": replacement["job"]["revision"],
                    "idempotency_key": "catalog-forged",
                },
            )

        with pytest.raises(Exception, match="not evidenced"):
            await _call(
                server,
                "rulebook_draft",
                {
                    **arguments,
                    "payload": {
                        **arguments["payload"],
                        "additions": [
                            {
                                "kind": "subclass",
                                "name": "Invented Name",
                                "source_chunk_ids": [gunsmith_chunk["id"]],
                            }
                        ],
                    },
                    "expected_revision": replacement["job"]["revision"],
                    "idempotency_key": "catalog-invented-name",
                },
            )

    asyncio.run(exercise())


def test_rulebook_draft_renders_a_checksum_bound_review_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    source = import_root / "review.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 20 160 Td (Commoner rulebook review page) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    with source.open("wb") as stream:
        writer.write(stream)
    layout = OcrPageLayout(
        page_number=1,
        width=450,
        height=300,
        blocks=(
            OcrTextBlock(
                "Commoner rulebook review page",
                0.98,
                20,
                20,
                300,
                45,
            ),
        ),
    )

    layout_calls: list[str] = []

    def extract_layout(
        provider: RapidOcrProvider,
        path: Path,
        *,
        page_numbers: list[int] | None = None,
    ) -> list[OcrPageLayout]:
        assert provider.model_type in {"medium", "small"}
        assert page_numbers == [1]
        layout_calls.append(provider.model_type)
        return [layout]

    monkeypatch.setattr(RapidOcrProvider, "extract_layout", extract_layout)
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        rule_import_roots=(import_root,),
    )

    async def exercise() -> None:
        server = create_server(config)
        _, campaign = await server.call_tool(
            "campaign_create",
            {"name": "Page review", "edition": "2014", "idempotency_key": "campaign"},
        )
        _, staged = await server.call_tool(
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "review",
                    "title": "Review",
                    "edition": "2014",
                    "publication_id": "srd2014",
                },
                "idempotency_key": "stage",
            },
        )
        job_id = staged["result"]["job"]["id"]
        rendered = await server.call_tool(
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "evidence",
                "payload": {"kind": "page", "job_id": job_id, "page_number": 1},
            },
        )

        assert isinstance(rendered.content[0], TextContent)
        assert isinstance(rendered.content[1], ImageContent)
        metadata = json.loads(rendered.content[0].text)
        assert metadata["page_number"] == 1
        assert metadata["source_checksum"] == staged["result"]["checksum"]
        assert metadata["ocr"]["included"] is True
        assert metadata["ocr"]["available"] is True
        assert metadata["ocr"]["provider"] == "rapidocr"
        assert ":ocr=PP-OCRv6:model=medium:scale=2.00" in metadata["ocr"]["profile"]
        assert metadata["ocr"]["model"] == "medium"
        assert metadata["ocr"]["scale"] == 2.0
        assert metadata["ocr"]["page_number"] == 1
        assert metadata["ocr"]["used_column_recovery"] is False
        assert metadata["ocr"]["block_count"] == 1
        assert metadata["ocr"]["average_confidence"] == 0.98
        assert metadata["ocr"]["minimum_confidence"] == 0.98
        assert (
            metadata["ocr"]["text_sha256"]
            == hashlib.sha256(b"Commoner rulebook review page").hexdigest()
        )
        assert metadata["ocr"]["text"] == "Commoner rulebook review page"
        assert metadata["ocr"]["truncated"] is False
        assert [item["model"] for item in metadata["ocr"]["variants"]] == [
            "medium",
            "small",
        ]
        assert layout_calls == ["medium", "small"]
        assert metadata["transcription"]["source_checksum"] == staged["result"]["checksum"]
        assert metadata["transcription"]["normalized"]["text_sha256"]
        assert metadata["transcription"]["native_text"]["text_sha256"]
        assert rendered.structuredContent == metadata
        assert rendered.content[1].mimeType == "image/png"

        _, inspected = await server.call_tool(
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "idempotency_key": "inspect",
            },
        )
        with pytest.raises(Exception, match="cannot alter numeric evidence"):
            await server.call_tool(
                "rulebook_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "edit",
                    "payload": {
                        "operation": "source_text",
                        "job_id": job_id,
                        "page_number": 1,
                        "base_text_sha256": metadata["transcription"]["normalized"]["text_sha256"],
                        "replacements": [
                            {
                                "old": "Commoner rulebook review page",
                                "new": "Commoner rulebook review page 2",
                            }
                        ],
                        "rationale": "An invalid Agent attempt to invent a page number.",
                        "evidence_basis": "agent_context",
                    },
                    "expected_revision": inspected["result"]["job"]["revision"],
                    "idempotency_key": "reject-numeric-text-change",
                },
            )
        with pytest.raises(Exception, match="cannot alter numeric evidence"):
            await server.call_tool(
                "rulebook_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "edit",
                    "payload": {
                        "operation": "source_text",
                        "job_id": job_id,
                        "page_number": 1,
                        "base_text_sha256": metadata["transcription"]["normalized"]["text_sha256"],
                        "replacements": [
                            {
                                "old": "Commoner rulebook review page",
                                "new": "Commoner rulebook review page two",
                            }
                        ],
                        "rationale": "An invalid Agent attempt to invent a written number.",
                        "evidence_basis": "agent_context",
                    },
                    "expected_revision": inspected["result"]["job"]["revision"],
                    "idempotency_key": "reject-written-number-text-change",
                },
            )
        cross_text_arguments = {
            "campaign_id": campaign["id"],
            "action": "edit",
            "payload": {
                "operation": "source_text",
                "job_id": job_id,
                "page_number": 1,
                "base_text_sha256": metadata["transcription"]["normalized"]["text_sha256"],
                "replacements": [{"old": "rulebook", "new": "RULEBOOK"}],
                "rationale": "Native text and both OCR variants corroborate this token.",
                "evidence_basis": "cross_text",
            },
            "expected_revision": inspected["result"]["job"]["revision"],
            "idempotency_key": "review-cross-text",
        }
        _, cross_text_reviewed = await server.call_tool("rulebook_draft", cross_text_arguments)
        assert cross_text_reviewed["result"]["review"]["evidence"]["basis"] == "cross_text"
        assert [
            item["model"]
            for item in cross_text_reviewed["result"]["review"]["evidence"]["ocr_variants"]
        ] == ["medium", "small"]
        current_render = await server.call_tool(
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "evidence",
                "payload": {
                    "kind": "page",
                    "job_id": job_id,
                    "page_number": 1,
                    "include_ocr_text": False,
                },
            },
        )
        current_metadata = json.loads(current_render.content[0].text)
        text_review_arguments = {
            "campaign_id": campaign["id"],
            "action": "edit",
            "payload": {
                "operation": "source_text",
                "job_id": job_id,
                "page_number": 1,
                "base_text_sha256": current_metadata["transcription"]["normalized"]["text_sha256"],
                "replacements": [
                    {
                        "old": "Commoner RULEBOOK review page",
                        "new": "## COMMONER RULEBOOK REVIEW PAGE",
                    }
                ],
                "rationale": (
                    "The independent native and OCR transcripts agree on the words; "
                    "the Agent restores the source heading capitalization."
                ),
                "evidence_basis": "agent_context",
            },
            "expected_revision": cross_text_reviewed["result"]["job"]["revision"],
            "idempotency_key": "review-text",
        }
        _, text_reviewed = await server.call_tool("rulebook_draft", text_review_arguments)
        _, text_review_replayed = await server.call_tool("rulebook_draft", text_review_arguments)
        assert text_review_replayed == text_reviewed
        assert layout_calls == ["medium", "small"]
        assert text_reviewed["result"]["review"]["review_method"] == "agent"
        assert (
            text_reviewed["result"]["inspection"]["page_revisions"][1]["evidence"]["basis"]
            == "agent_context"
        )
        assert text_reviewed["result"]["review"]["evidence"]["ocr_variants"] == []
        rerendered = await server.call_tool(
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "evidence",
                "payload": {
                    "kind": "page",
                    "job_id": job_id,
                    "page_number": 1,
                    "include_ocr_text": False,
                },
            },
        )
        revised_metadata = json.loads(rerendered.content[0].text)
        _, text_refined = await server.call_tool(
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "operation": "source_text",
                    "job_id": job_id,
                    "page_number": 1,
                    "base_text_sha256": revised_metadata["transcription"]["normalized"][
                        "text_sha256"
                    ],
                    "replacements": [
                        {
                            "old": "## COMMONER RULEBOOK REVIEW PAGE",
                            "new": "##### COMMONER RULEBOOK REVIEW PAGE",
                        }
                    ],
                    "rationale": "Match the adjacent entry heading depth.",
                    "evidence_basis": "agent_context",
                },
                "expected_revision": text_reviewed["result"]["job"]["revision"],
                "idempotency_key": "refine-text-review",
            },
        )
        assert len(text_refined["result"]["inspection"]["page_revisions"]) == 3
        _, ingested = await server.call_tool(
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "operation": "advance",
                    "job_id": job_id,
                    "acknowledge_warnings": bool(text_refined["result"]["inspection"]["warnings"]),
                },
                "idempotency_key": "ingest",
            },
        )
        commoner = """### Commoner

*Medium humanoid (any race), any alignment*

**Armor Class** 10
**Hit Points** 4 (1d8)
**Speed** 30 ft.

| STR | DEX | CON | INT | WIS | CHA |
|:---:|:---:|:---:|:---:|:---:|:---:|
| 10 (+0) | 10 (+0) | 10 (+0) | 10 (+0) | 10 (+0) | 10 (+0) |

**Senses** passive Perception 10
**Languages** Common
**Challenge** 0 (10 XP)

###### Actions

***Club***. *Melee Weapon Attack:* +2 to hit, reach 5 ft., one target.
*Hit:* 2 (1d4) bludgeoning damage.
"""
        review_arguments = {
            "campaign_id": campaign["id"],
            "action": "edit",
            "payload": {
                "operation": "statblock_review",
                "job_id": job_id,
                "page_number": 1,
                "normalized_content": commoner,
                "observation": "DM compared every field with the rendered source page.",
            },
            "idempotency_key": "review-statblock",
        }
        _, reviewed = await server.call_tool("rulebook_draft", review_arguments)
        _, replayed = await server.call_tool("rulebook_draft", review_arguments)
        assert replayed == reviewed
        review = reviewed["result"]["review"]
        assert review["source_id"] == ingested["result"]["source_id"]
        assert review["asset_checksum"] == metadata["source_checksum"]
        assert review["image_checksum"] == metadata["image_checksum"]
        validation = reviewed["result"]["validation"]
        assert validation["default_dm_resolver"] == "agent"
        assert validation["settlement"] == "automatic"
        assert validation["ruling_requirements"] == []
        with pytest.raises(Exception, match="indexed_text is reserved"):
            await server.call_tool(
                "rulebook_draft",
                {
                    **review_arguments,
                    "payload": {
                        **review_arguments["payload"],
                        "review_mode": "indexed_text",
                        "evidence_chunk_ids": ["caller-controlled"],
                    },
                    "idempotency_key": "reject-public-indexed-text",
                },
            )

        _, created = await server.call_tool(
            "character_create_from",
            {
                "mode": "reviewed_rule_statblock",
                "payload": {
                    "campaign_id": campaign["id"],
                    "job_id": job_id,
                    "review_id": review["id"],
                    "name": "Reviewed Commoner",
                    "character_type": "npc",
                },
                "idempotency_key": "reviewed-commoner",
            },
        )
        created = created["result"]
        assert created["source"]["normalized_content_sha256"]
        assert created["character"]["derived"]["hit_points"]["max"] == 4
        assert (
            "Reviewed rule statblock: rule-source:"
            in (created["character"]["notes"]["profile"]["dm_notes"])
        )

        reviewed_monster = """### Reviewed Hunter

*Medium monstrosity, unaligned*

**Armor Class** 13
**Hit Points** 22 (4d8 + 4)
**Speed** 30 ft.

| STR | DEX | CON | INT | WIS | CHA |
|:---:|:---:|:---:|:---:|:---:|:---:|
| 14 (+2) | 12 (+1) | 12 (+1) | 8 (-1) | 12 (+1) | 8 (-1) |

**Senses** passive Perception 11
**Languages** understands Common but can't speak
**Challenge** 1 (200 XP)

###### Actions

***Multiattack.*** The hunter makes one bite attack and one claw attack.

***Bite.*** *Melee Weapon Attack:* +4 to hit, reach 5 ft., one target.
*Hit:* 6 (1d8 + 2) piercing damage.

***Claw.*** *Melee Weapon Attack:* +4 to hit, reach 5 ft., one target.
*Hit:* 5 (1d6 + 2) slashing damage.
"""
        missing_fill_arguments = {
            "campaign_id": campaign["id"],
            "action": "edit",
            "payload": {
                "operation": "statblock_review",
                "job_id": job_id,
                "page_number": 1,
                "normalized_content": reviewed_monster,
                "observation": "DM compared every monster field with the rendered page.",
            },
            "idempotency_key": "review-monster-without-fill",
        }
        _, engine_review_response = await server.call_tool(
            "rulebook_draft",
            missing_fill_arguments,
        )
        engine_review = engine_review_response["result"]["review"]
        engine_validation = engine_review_response["result"]["validation"]
        assert engine_review["agent_statblock_fill"] is None
        assert engine_validation["warnings"] == []
        assert engine_validation["agent_fill_requirements"] == {
            "required": False,
            "default_resolver": "engine",
            "ruling_kind": "standard_rule",
            "parser_authoritative": True,
            "allowed_resolutions": ["engine"],
            "source_bound_rulings": [],
            "multiattack_options": [
                {
                    "activity_id": "multiattack-activity",
                    "source_excerpt": ("The hunter makes one bite attack and one claw attack."),
                    "options": [
                        {
                            "id": "melee",
                            "attacks": [
                                {
                                    "weapon_id": "bite",
                                    "attack_mode": "melee",
                                    "count": 1,
                                },
                                {
                                    "weapon_id": "claw",
                                    "attack_mode": "melee",
                                    "count": 1,
                                },
                            ],
                        }
                    ],
                }
            ],
            "available_weapons": [],
        }

        agent_fill = {
            "multiattack_options": [
                {
                    "activity_id": "multiattack-activity",
                    "source_excerpt": ("The hunter makes one bite attack and one claw attack."),
                    "reason": ("The reviewed sentence explicitly requires one bite and one claw."),
                    "options": [
                        {
                            "id": "bite-and-claw",
                            "attacks": [
                                {
                                    "weapon_id": "bite",
                                    "attack_mode": "melee",
                                    "count": 1,
                                },
                                {
                                    "weapon_id": "claw",
                                    "attack_mode": "melee",
                                    "count": 1,
                                },
                            ],
                        }
                    ],
                }
            ]
        }
        with pytest.raises(Exception, match="do not accept Agent semantic fills"):
            await server.call_tool(
                "rulebook_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "edit",
                    "payload": {
                        "operation": "statblock_review",
                        "job_id": job_id,
                        "page_number": 1,
                        "normalized_content": reviewed_monster,
                        "observation": ("Attempted to override an engine-parsed standard rule."),
                        "agent_fill": agent_fill,
                    },
                    "idempotency_key": "reject-standard-rule-agent-fill",
                },
            )
            _, direct_ruling_response = await server.call_tool(
                "rulebook_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "edit",
                    "payload": {
                        "operation": "statblock_review",
                        "job_id": job_id,
                        "page_number": 1,
                        "normalized_content": reviewed_monster.replace(
                            "The hunter makes one bite attack and one claw attack.",
                            "The hunter attacks and shouts a command.",
                        ),
                        "observation": (
                            "Reviewed an exact source-specific composition for a "
                            "build-time Agent ruling boundary."
                        ),
                    },
                    "idempotency_key": "direct-ruling-standard-source-content",
                },
            )
            direct_validation = direct_ruling_response["result"]["validation"]
            assert direct_ruling_response["result"]["review"]["agent_statblock_fill"] is None
            assert direct_validation["default_dm_resolver"] == "agent"
            assert direct_validation["agent_fill_requirements"]["source_bound_rulings"] == [
                "Multiattack: Multiattack composition requires a DM ruling"
            ]
            assert direct_validation["agent_fill_requirements"]["allowed_resolutions"] == [
                "engine",
                "agent_dm_adjudication",
            ]

        _, engine_actor_response = await server.call_tool(
            "character_create_from",
            {
                "mode": "reviewed_rule_statblock",
                "payload": {
                    "campaign_id": campaign["id"],
                    "job_id": job_id,
                    "review_id": engine_review["id"],
                    "name": "Reviewed Hunter",
                    "character_type": "monster",
                },
                "idempotency_key": "reviewed-hunter",
            },
        )
        engine_actor = engine_actor_response["result"]
        assert engine_actor["character"]["derived"]["multiattack_options"] == [
            {
                "id": "melee",
                "attacks": [
                    {"weapon_id": "bite", "attack_mode": "melee", "count": 1},
                    {"weapon_id": "claw", "attack_mode": "melee", "count": 1},
                ],
            }
        ]
        assert engine_actor["statblock"]["warnings"] == []
        assert engine_actor["statblock"]["agent_fill"] is None
        assert (
            "Agent statblock fill:"
            not in (engine_actor["character"]["notes"]["profile"]["dm_notes"])
        )

        evidence_chunks = [
            {
                "id": "commoner-core",
                "ordinal": 0,
                "heading_path": ["COMMONER"],
                "content": (
                    "Medium humanoid (any race), any alignment Armor Class l0 "
                    "Hit Points 4 (1d8) Speed 30 ft."
                ),
                "page_start": 1,
                "page_end": 1,
            },
            *[
                {
                    "id": f"commoner-{ability.casefold()}",
                    "ordinal": index,
                    "heading_path": [ability],
                    "content": (
                        "10 (+0) Senses passive Perception 10 Languages Common Challenge 0 (10 XP)"
                        if ability == "WIS"
                        else "l0 (+0)"
                        if ability == "STR"
                        else "10 (+0)"
                    ),
                    "page_start": 1,
                    "page_end": 1,
                }
                for index, ability in enumerate(
                    ("STR", "DEX", "CON", "INT", "WIS", "CHA"),
                    start=1,
                )
            ],
            {
                "id": "commoner-actions",
                "ordinal": 7,
                "heading_path": ["ACTIONS"],
                "content": (
                    "Club. Melee Weapon Attack: +2 to hit, reach 5 ft., one target. "
                    "Hit: 2 (1d4) bludgeoning damage. "
                    "Commoners include laborers, servants, and ordinary travelers."
                ),
                "page_start": 1,
                "page_end": 1,
            },
        ]
        monkeypatch.setattr(
            RuleService,
            "source_chunks",
            lambda _service, _source_id: evidence_chunks,
        )
        monkeypatch.setattr(
            RuleService,
            "expand",
            lambda _service, chunk_id: {
                **next(item for item in evidence_chunks if item["id"] == chunk_id),
                "source": {"id": ingested["result"]["source_id"]},
            },
        )
        agent_commoner = (
            commoner
            + "\n###### Commoner\n\n"
            + "Commoners include laborers, servants, and ordinary travelers.\n"
        )
        agent_arguments = {
            **review_arguments,
            "payload": {
                **review_arguments["payload"],
                "normalized_content": agent_commoner,
                "observation": (
                    "Agent normalized only the selected contiguous indexed text evidence."
                ),
                "review_mode": "agent_text",
                "evidence_chunk_ids": [item["id"] for item in evidence_chunks],
            },
            "idempotency_key": "review-statblock-agent-text",
        }
        _, agent_reviewed = await server.call_tool("rulebook_draft", agent_arguments)
        agent_review = agent_reviewed["result"]["review"]
        assert agent_review["review_mode"] == "agent_text"
        assert agent_review["confidence"] == "reviewed_text"
        assert agent_review["evidence_chunk_ids"] == [item["id"] for item in evidence_chunks]
        assert agent_review["text_evidence"][0]["ordinal"] == 0

        base_actions_content = evidence_chunks[-1]["content"]
        web_garrote_excerpt = (
            "Melee Weapon Attack: +4 to hit, reach 5 ft., one Medium or Small "
            "creature against which the ettercap has advantage on the attack roll. "
            "Hit: 4 (1d4 + 2) bludgeoning damage, and the target is grappled "
            "(escape DC 12). Until this grapple ends, the target can't breathe, "
            "and the ettercap has advantage on attack rolls against it."
        )
        web_garrote_evidence = " Web Garrote. " + web_garrote_excerpt
        evidence_chunks[-1]["content"] += web_garrote_evidence
        with pytest.raises(
            Exception,
            match="do not accept Agent semantic fills",
        ):
            await server.call_tool(
                "rulebook_draft",
                {
                    **agent_arguments,
                    "payload": {
                        **agent_arguments["payload"],
                        "evidence_exclusions": [
                            {
                                "chunk_id": evidence_chunks[-1]["id"],
                                "exact_text": web_garrote_evidence,
                                "reason": (
                                    "The adjacent variant action is not part of "
                                    "the selected standard statblock."
                                ),
                            }
                        ],
                        "agent_fill": {
                            "additional_actions": [
                                {
                                    "name": "Web Garrote",
                                    "source_ref": "rule-chunk:commoner-actions",
                                    "source_excerpt": web_garrote_excerpt,
                                    "reason": (
                                        "This attempted semantic addition must be "
                                        "implemented by the standard-rule engine."
                                    ),
                                }
                            ]
                        },
                    },
                    "idempotency_key": "reject-rule-additional-action-fill",
                },
            )
        evidence_chunks[-1]["content"] = base_actions_content

        adjacent_column = (
            " Large dragon, chaotic evil Armor Class 17 Hit Points 133 Challenge 6 (2,300 XP)."
        )
        evidence_chunks[-1]["content"] += adjacent_column
        _, excluded_reviewed = await server.call_tool(
            "rulebook_draft",
            {
                **agent_arguments,
                "payload": {
                    **agent_arguments["payload"],
                    "evidence_exclusions": [
                        {
                            "chunk_id": evidence_chunks[-1]["id"],
                            "exact_text": adjacent_column,
                            "reason": (
                                "The selected page segment crosses into the adjacent "
                                "creature column after the reviewed target."
                            ),
                        }
                    ],
                },
                "idempotency_key": "review-statblock-agent-adjacent-column",
            },
        )
        exclusion = excluded_reviewed["result"]["review"]["evidence_exclusions"][0]
        assert exclusion["chunk_id"] == evidence_chunks[-1]["id"]
        assert exclusion["reason"].startswith("The selected page segment")
        assert len(exclusion["exact_text_sha256"]) == 64

        with pytest.raises(Exception, match="facts absent"):
            await server.call_tool(
                "rulebook_draft",
                {
                    **agent_arguments,
                    "payload": {
                        **agent_arguments["payload"],
                        "normalized_content": agent_commoner.replace(
                            "*Hit:* 2 (1d4)",
                            "*Hit:* 99 (1d4)",
                        ),
                    },
                    "idempotency_key": "review-statblock-agent-invented",
                },
            )
        with pytest.raises(Exception, match="exactly preserve STR"):
            await server.call_tool(
                "rulebook_draft",
                {
                    **agent_arguments,
                    "payload": {
                        **agent_arguments["payload"],
                        "normalized_content": agent_commoner.replace(
                            "10 (+0) | 10 (+0)",
                            "10 (+9) | 10 (+0)",
                            1,
                        ),
                    },
                    "idempotency_key": "review-statblock-agent-wrong-modifier",
                },
            )
        with pytest.raises(Exception, match="ordered contiguous"):
            await server.call_tool(
                "rulebook_draft",
                {
                    **agent_arguments,
                    "payload": {
                        **agent_arguments["payload"],
                        "evidence_chunk_ids": [
                            item["id"] for item in evidence_chunks if item["ordinal"] != 3
                        ],
                    },
                    "idempotency_key": "review-statblock-agent-gap",
                },
            )

    asyncio.run(exercise())


def test_rulebook_draft_recovers_wholly_empty_page_from_rendered_agent_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    source = import_root / "missed-page.pdf"
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    for page_number in range(1, 6):
        page = writer.add_blank_page(width=300, height=200)
        if page_number == 5:
            continue
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
        )
        content = DecodedStreamObject()
        content.set_data(
            (
                "BT /F1 12 Tf 20 160 Td "
                f"(Usable rule page {page_number} contains enough ordinary text for inspection.) "
                "Tj ET"
            ).encode()
        )
        page[NameObject("/Contents")] = writer._add_object(content)
    with source.open("wb") as stream:
        writer.write(stream)

    def extract_layout(
        provider: RapidOcrProvider,
        path: Path,
        *,
        page_numbers: list[int] | None = None,
    ) -> list[OcrPageLayout]:
        assert provider.model_type in {"medium", "small"}
        assert path.name.endswith("missed-page.pdf")
        assert page_numbers is not None
        return [
            OcrPageLayout(
                page_number=page_number,
                width=450,
                height=300,
                blocks=(
                    ()
                    if page_number == 5
                    else (
                        OcrTextBlock(
                            (
                                f"Usable rule page {page_number} contains enough ordinary text "
                                "for inspection."
                            ),
                            0.99,
                            20,
                            20,
                            250,
                            45,
                        ),
                    )
                ),
            )
            for page_number in page_numbers
        ]

    monkeypatch.setattr(RapidOcrProvider, "extract_layout", extract_layout)
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        rule_import_roots=(import_root,),
    )

    async def exercise() -> None:
        server = create_server(config)
        _, campaign = await server.call_tool(
            "campaign_create",
            {"name": "Empty page recovery", "edition": "2014", "idempotency_key": "campaign"},
        )
        _, staged = await server.call_tool(
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "missed-page",
                    "title": "Missed Page",
                    "edition": "2014",
                    "publication_id": "homebrew",
                },
                "idempotency_key": "stage",
            },
        )
        job_id = staged["result"]["job"]["id"]
        _, inspected = await server.call_tool(
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "idempotency_key": "inspect",
            },
        )
        rendered = await server.call_tool(
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "evidence",
                "payload": {
                    "kind": "page",
                    "job_id": job_id,
                    "page_number": 5,
                    "include_ocr_text": False,
                },
            },
        )
        metadata = json.loads(rendered.content[0].text)
        normalized = metadata["transcription"]["normalized"]
        assert not normalized["text"].strip()

        _, reviewed = await server.call_tool(
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "operation": "source_text",
                    "job_id": job_id,
                    "page_number": 5,
                    "base_text_sha256": normalized["text_sha256"],
                    "replacements": [
                        {
                            "old": "",
                            "new": (
                                "\n\n# CLASS FEATURES\n\nRecovered exact page transcription.\n\n"
                            ),
                        }
                    ],
                    "rationale": "Agent transcribed the page that both text extractors omitted.",
                    "evidence_basis": "rendered_page",
                    "rendered_image_checksum": metadata["image_checksum"],
                },
                "expected_revision": inspected["result"]["job"]["revision"],
                "idempotency_key": "recover-empty-page",
            },
        )

        assert reviewed["result"]["inspection"]["outline"][0]["title"] == "CLASS FEATURES"
        assert reviewed["result"]["review"]["evidence"]["basis"] == "rendered_page"

    asyncio.run(exercise())


def test_module_import_accepts_checksum_bound_agent_transcript_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_root = tmp_path / "modules"
    import_root.mkdir()
    source = import_root / "transcript.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    content = DecodedStreamObject()
    content.set_data(
        b"BT /F1 12 Tf 20 160 Td (Arrival transcript contains enough exact source words.) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(content)
    with source.open("wb") as stream:
        writer.write(stream)
    layout = OcrPageLayout(
        page_number=1,
        width=450,
        height=300,
        blocks=(
            OcrTextBlock(
                "Arrival transcript contains enough exact source words.",
                0.98,
                20,
                20,
                420,
                45,
            ),
        ),
    )

    layout_calls: list[str] = []

    def extract_layout(
        provider: RapidOcrProvider,
        path: Path,
        *,
        page_numbers: list[int] | None = None,
    ) -> list[OcrPageLayout]:
        assert provider.model_type in {"medium", "small"}
        assert path.name.endswith("transcript.pdf")
        assert page_numbers == [1]
        layout_calls.append(provider.model_type)
        if provider.model_type == "medium":
            raise RuntimeError("primary OCR model unavailable")
        return [layout]

    monkeypatch.setattr(RapidOcrProvider, "extract_layout", extract_layout)
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        module_import_roots=(import_root,),
    )

    async def exercise() -> None:
        server = create_server(config)
        _, campaign = await server.call_tool(
            "campaign_create",
            {"name": "Module transcript", "idempotency_key": "campaign"},
        )
        _, staged = await server.call_tool(
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "transcript",
                    "title": "Transcript",
                },
                "idempotency_key": "stage",
            },
        )
        job_id = staged["result"]["job"]["id"]
        _, inspected = await server.call_tool(
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "idempotency_key": "inspect",
            },
        )
        rendered = await server.call_tool(
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "evidence",
                "payload": {"job_id": job_id, "page_number": 1},
            },
        )
        metadata = json.loads(rendered.content[0].text)
        assert isinstance(rendered.content[1], ImageContent)
        assert metadata["citation_candidates"]
        page_source_ref = metadata["citation_candidates"][0]["source_ref"]
        assert page_source_ref["source_key"] == "transcript"
        assert page_source_ref["page"] == 1
        assert len(page_source_ref["chunk_hash"]) == 64
        assert page_source_ref["note"].startswith("Agent-reviewed source evidence:")
        _, chunk_evidence = await server.call_tool(
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "evidence",
                "payload": {
                    "job_id": job_id,
                    "kind": "chunks",
                    "query": "Arrival transcript",
                },
            },
        )
        assert chunk_evidence["result"][0]["source_ref"] == page_source_ref
        assert [item["model"] for item in metadata["transcription"]["ocr"]["variants"]] == [
            "medium",
            "small",
        ]
        assert [item["available"] for item in metadata["transcription"]["ocr"]["variants"]] == [
            False,
            True,
        ]
        assert metadata["transcription"]["ocr"]["model"] == "small"
        assert layout_calls == ["medium", "small"]
        review_arguments = {
            "campaign_id": campaign["id"],
            "action": "edit",
            "payload": {
                "operation": "source_text",
                "job_id": job_id,
                "page_number": 1,
                "base_text_sha256": metadata["transcription"]["normalized"]["text_sha256"],
                "replacements": [{"old": "Arrival transcript", "new": "ARRIVAL TRANSCRIPT"}],
                "rationale": "Restore the display-heading capitalization.",
                "evidence_basis": "agent_context",
            },
            "expected_revision": inspected["result"]["job"]["revision"],
            "idempotency_key": "review-transcript",
        }
        _, reviewed = await server.call_tool("module_draft", review_arguments)
        _, replayed = await server.call_tool("module_draft", review_arguments)

        assert replayed == reviewed
        assert layout_calls == ["medium", "small"]
        assert reviewed["result"]["job"]["state"] == "inspected"
        assert reviewed["result"]["inspection"]["page_revisions"][0]["replacements"] == [
            {"old": "Arrival transcript", "new": "ARRIVAL TRANSCRIPT"}
        ]

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("embedded_text", "corrupt_embedded_text", "fallback_model"),
    [
        (True, False, False),
        (False, False, False),
        (True, True, False),
        (False, False, True),
    ],
)
def test_rulebook_draft_recovers_statblock_for_text_only_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    embedded_text: bool,
    corrupt_embedded_text: bool,
    fallback_model: bool,
) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    source = import_root / "ocr-review.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=400)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    if embedded_text:
        content = DecodedStreamObject()
        content.set_data(
            b"BT /F1 8 Tf 10 370 Td 10 TL "
            b"(Medium humanoid, any alignment) Tj T* "
            b"(Armor Class 10) Tj T* "
            + (
                b"(Hit Points 4 [ld8]) Tj T* "
                if corrupt_embedded_text
                else b"(Hit Points 4 [1d8]) Tj T* "
            )
            + b"(Speed 30 ft.) Tj T* "
            b"(STR) Tj T* (DEX) Tj T* (CON) Tj T* "
            b"(INT) Tj T* (WIS) Tj T* (CHA) Tj T* "
            b"(10 [+0]) Tj T* (10 [+0]) Tj T* (10 [+0]) Tj T* "
            b"(10 [+0]) Tj T* (10 [+0]) Tj T* (10 [+0]) Tj T* "
            b"(Senses passive Perception 10) Tj T* "
            b"(Languages Common) Tj T* "
            b"(Challenge 0 [10 XP]) Tj T* "
            b"(Club. Melee Weapon Attack: +2 to hit, reach 5 ft., one creature.) Tj ET"
        )
        page[NameObject("/Contents")] = writer._add_object(content)
    with source.open("wb") as stream:
        writer.write(stream)

    def ocr_block(
        text: str,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        confidence: float = 0.99,
    ) -> OcrTextBlock:
        return OcrTextBlock(text, confidence, x0, y0, x1, y1)

    layout = OcrPageLayout(
        page_number=1,
        width=600,
        height=400,
        blocks=(
            ocr_block("COMMONER", 30, 20, 180, 45),
            ocr_block("Medium humanoid, any alignment", 30, 45, 250, 65),
            ocr_block("Armor Class 10", 30, 75, 160, 95),
            ocr_block("Hit Points 4 (1d8)", 30, 95, 190, 115),
            ocr_block("Speed 30 ft.", 30, 115, 150, 135),
            *tuple(
                ocr_block(
                    label,
                    30 + index * 70,
                    145,
                    70 + index * 70,
                    165,
                    0.73 if label == "INT" else 0.99,
                )
                for index, label in enumerate(("STR", "DEX", "CON", "INT", "WIS", "CHA"))
            ),
            *tuple(
                ocr_block("10 (+0)", 25 + index * 70, 165, 80 + index * 70, 185)
                for index in range(6)
            ),
            ocr_block("Senses passive Perception 10", 30, 200, 250, 220),
            ocr_block("Languages Common", 30, 220, 180, 240),
            ocr_block("Challenge 0 (10 XP)", 30, 240, 200, 260),
            ocr_block("ACTIONS", 30, 275, 130, 295),
            ocr_block(
                "Club. Melee Weapon Attack: +2 to hit, reach 5 ft., one target.",
                30,
                305,
                480,
                325,
            ),
            ocr_block("Hit: 2 (1d4) bludgeoning damage.", 30, 325, 310, 345),
            ocr_block("COMMONER", 30, 355, 180, 380),
        ),
    )
    layout_calls: list[tuple[str, float, tuple[int, ...]]] = []

    def extract_layout(
        provider: RapidOcrProvider,
        path: Path,
        *,
        page_numbers: list[int] | None = None,
    ) -> list[OcrPageLayout]:
        layout_calls.append(
            (
                provider.model_type,
                provider.scale,
                tuple(page_numbers or ()),
            )
        )
        if fallback_model and provider.model_type == "medium":
            return [
                OcrPageLayout(
                    page_number=layout.page_number,
                    width=layout.width,
                    height=layout.height,
                    blocks=(),
                )
            ]
        if not embedded_text and provider.scale == 3.0:
            return [
                OcrPageLayout(
                    page_number=layout.page_number,
                    width=layout.width,
                    height=layout.height,
                    blocks=tuple(block for block in layout.blocks if block.text != "CON"),
                )
            ]
        return [layout]

    monkeypatch.setattr(RapidOcrProvider, "extract_layout", extract_layout)
    monkeypatch.setattr(
        RapidOcrProvider,
        "extract",
        lambda self, path, *, page_numbers=None: [
            "Medium humanoid, any alignment\nArmor Class 10\n"
            "Hit Points 4 (1d8)\nSpeed 30 ft.\nChallenge 0 (10 XP)"
        ],
    )
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        rule_import_roots=(import_root,),
    )

    async def exercise() -> None:
        server = create_server(config)
        _, campaign = await server.call_tool(
            "campaign_create",
            {"name": "OCR recovery", "edition": "2014", "idempotency_key": "campaign"},
        )
        _, staged = await server.call_tool(
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "ocr-review",
                    "title": "OCR Review",
                    "edition": "2014",
                },
                "idempotency_key": "stage",
            },
        )
        job_id = staged["result"]["job"]["id"]
        _, inspected = await server.call_tool(
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "idempotency_key": "inspect",
            },
        )
        await server.call_tool(
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "operation": "advance",
                    "job_id": job_id,
                    "acknowledge_warnings": bool(inspected["result"]["inspection"]["warnings"]),
                },
                "idempotency_key": "ingest",
            },
        )
        arguments = {
            "campaign_id": campaign["id"],
            "action": "edit",
            "payload": {
                "operation": "statblock_recovery",
                "job_id": job_id,
                "name": "Commoner",
                "page_number": 1,
            },
            "idempotency_key": "recover",
        }
        _, recovered = await server.call_tool("rulebook_draft", arguments)
        recovery_layout_call_count = len(layout_calls)
        _, replayed = await server.call_tool("rulebook_draft", arguments)

        assert replayed == recovered
        assert len(layout_calls) == recovery_layout_call_count
        result = recovered["result"]
        assert result["page_number"] == 1
        assert result["provider"] == "rapidocr"
        assert result["ocr_model"] == ("small" if fallback_model else "medium")
        assert result["corroboration_models"] == (
            ["small", "small"]
            if fallback_model
            else ["medium"]
            if embedded_text and not corrupt_embedded_text
            else ["medium", "medium"]
        )
        assert result["corroboration_mode"] == (
            "embedded_text" if embedded_text and not corrupt_embedded_text else "dual_layout_ocr"
        )
        assert result["corroboration_scales"] == (
            [2.0] if embedded_text and not corrupt_embedded_text else [2.0, 3.0]
        )
        assert result["recovery"]["evidence"]["text_only"] is True
        assert result["recovery"]["evidence"]["matching_heading_count"] == 2
        assert result["recovery"]["evidence"]["structural_heading_count"] == 1
        assert result["recovery"]["evidence"]["minimum_core_confidence"] == 0.73
        assert result["review"]["page_number"] == 1
        assert result["validation"]["experience_points"] == 10
        assert [item["field"] for item in result["corroborated_facts"]] == [
            "Identity",
            "Armor Class",
            "Hit Points",
            "Speed",
            "Challenge",
            "Senses",
            "Languages",
            "STR",
            "DEX",
            "CON",
            "INT",
            "WIS",
            "CHA",
        ]
        if fallback_model:
            return
        recovered_content = str(result["recovery"]["normalized_content"])

        def contaminated_inventory(chunks: list[dict], *, source_title: str) -> dict:
            del source_title
            return {
                "candidates": [
                    {
                        "id": "contaminated-commoner",
                        "kind": "statblock",
                        "name": "COMMONER",
                        "page_start": 1,
                        "page_end": 1,
                        "execution_state": "review_ready",
                        "normalized_content": recovered_content.replace("| 10 (+0)", "| 23 (+6)", 1)
                        + "\n\nGIANT APE Huge beast, unaligned.",
                        "source_chunk_ids": [str(item["id"]) for item in chunks],
                    }
                ]
            }

        monkeypatch.setattr(
            server_module,
            "extract_content_inventory",
            contaminated_inventory,
        )
        batch_arguments = {
            "campaign_id": campaign["id"],
            "action": "edit",
            "payload": {"operation": "statblock_recovery", "job_id": job_id, "page_numbers": [1]},
            "idempotency_key": "recover-catalog",
        }
        _, batch = await server.call_tool("rulebook_draft", batch_arguments)
        _, batch_replay = await server.call_tool("rulebook_draft", batch_arguments)
        assert batch_replay == batch
        assert batch["result"]["status"] == "complete"
        assert batch["result"]["complete_pages"] == [1]
        assert [item["name"] for item in batch["result"]["recovered"]] == ["COMMONER"]
        assert batch["result"]["failures"] == []
        assert len(batch["result"]["indexed_fallbacks"]) == 1
        assert batch["result"]["unresolved_indexed_fallbacks"] == []
        assert batch["result"]["recovered"][0]["recovery_mode"] in {
            "layout_text",
            "layout_ocr",
        }

        # Simulate a process interruption after the individual review committed
        # but before the enclosing batch receipt became durable. The same public
        # call must resume from the checksum-bound review, not try to mutate it
        # again or turn it into an Agent-fill failure.
        database_path = config.home / "data" / "ttrpgbase.db"
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "DELETE FROM idempotency_records WHERE key = ?",
                (batch_arguments["idempotency_key"],),
            )
            connection.commit()
        _, resumed_batch = await server.call_tool("rulebook_draft", batch_arguments)
        assert resumed_batch["result"]["status"] == "complete"
        assert resumed_batch["result"]["failures"] == []
        assert (
            resumed_batch["result"]["recovered"][0]["validation"]["resumed_from_persisted_review"]
            is True
        )
        agent_named_arguments = {
            "campaign_id": campaign["id"],
            "action": "edit",
            "payload": {
                "operation": "statblock_recovery",
                "job_id": job_id,
                "name": "Reviewed Commoner",
                "page_number": 1,
                "statblock_slot": 1,
                **(
                    {
                        "ocr_corrections": {
                            "text_replacements": [
                                {
                                    "old": (
                                        "Club. Melee Weapon Attack: +2 to hit, "
                                        "reach 5 ft., one target."
                                    ),
                                    "new": (
                                        "Club. Melee Weapon Attack: +2 to hit, "
                                        "reach 5 ft., one creature."
                                    ),
                                }
                            ]
                        }
                    }
                    if embedded_text
                    else {}
                ),
            },
            "idempotency_key": "recover-agent-named-slot",
        }
        _, agent_named = await server.call_tool(
            "rulebook_draft",
            agent_named_arguments,
        )
        agent_named_layout_call_count = len(layout_calls)
        _, replayed_agent_named = await server.call_tool(
            "rulebook_draft",
            agent_named_arguments,
        )
        assert replayed_agent_named == agent_named
        assert len(layout_calls) == agent_named_layout_call_count
        assert (
            agent_named["result"]["recovery"]["evidence"]["heading_match_mode"]
            == "agent_named_structural_slot"
        )
        assert agent_named["result"]["recovery"]["evidence"]["statblock_slot"] == 1
        assert agent_named["result"]["review"]["normalized_content"].startswith(
            "# Reviewed Commoner"
        )
        assert agent_named["result"]["review"]["derived_from_review_id"] == result["review"]["id"]
        if embedded_text:
            assert agent_named["result"]["recovery"]["evidence"]["reviewed_ocr_corrections"] == {
                "text_replacements": [
                    {
                        "old": ("Club. Melee Weapon Attack: +2 to hit, reach 5 ft., one target."),
                        "new": ("Club. Melee Weapon Attack: +2 to hit, reach 5 ft., one creature."),
                    }
                ]
            }
            if not corrupt_embedded_text:
                rendered_page = await server.call_tool(
                    "rulebook_draft",
                    {
                        "campaign_id": campaign["id"],
                        "action": "evidence",
                        "payload": {
                            "kind": "page",
                            "job_id": job_id,
                            "page_number": 1,
                            "scale": 1.5,
                            "include_ocr_text": False,
                        },
                    },
                )
                rendered_metadata = json.loads(rendered_page.content[0].text)
                rendered_arguments = {
                    "campaign_id": campaign["id"],
                    "action": "edit",
                    "payload": {
                        "operation": "statblock_recovery",
                        "job_id": job_id,
                        "name": "Rendered Commoner",
                        "page_number": 1,
                        "statblock_slot": 1,
                        "ocr_corrections": {
                            "text_replacements": [
                                {
                                    "old": (
                                        "Club. Melee Weapon Attack: +2 to hit, "
                                        "reach 5 ft., one target."
                                    ),
                                    "new": (
                                        "Club. Melee Weapon Attack: +2 to hit, "
                                        "reach 5 ft., one creature."
                                    ),
                                }
                            ]
                        },
                        "correction_evidence_basis": "rendered_page",
                        "rendered_image_checksum": rendered_metadata["image_checksum"],
                    },
                    "idempotency_key": "recover-rendered-agent-correction",
                }
                _, rendered_review = await server.call_tool(
                    "rulebook_draft",
                    rendered_arguments,
                )
                assert rendered_review["result"]["recovery"]["evidence"]["correction_evidence"] == {
                    "basis": "rendered_page",
                    "rendered_image_checksum": rendered_metadata["image_checksum"],
                    "page_number": 1,
                }
                with pytest.raises(Exception, match="checksum does not match"):
                    await server.call_tool(
                        "rulebook_draft",
                        {
                            **rendered_arguments,
                            "payload": {
                                **rendered_arguments["payload"],
                                "rendered_image_checksum": "0" * 64,
                            },
                            "idempotency_key": "reject-wrong-rendered-checksum",
                        },
                    )
            with pytest.raises(Exception, match="not corroborated by the staged page"):
                await server.call_tool(
                    "rulebook_draft",
                    {
                        "campaign_id": campaign["id"],
                        "action": "edit",
                        "payload": {
                            "operation": "statblock_recovery",
                            "job_id": job_id,
                            "name": "Invented Commoner",
                            "page_number": 1,
                            "statblock_slot": 1,
                            "ocr_corrections": {
                                "text_replacements": [
                                    {
                                        "old": "Hit: 2 (1d4) bludgeoning damage.",
                                        "new": "Hit: 99 force damage.",
                                    }
                                ]
                            },
                        },
                        "idempotency_key": "reject-invented-ocr-correction",
                    },
                )
        if embedded_text:
            await server.call_tool(
                "access_grant",
                {
                    "scope": "campaign",
                    "campaign_id": campaign["id"],
                    "principal_id": "player:ocr",
                    "payload": {"role": "player"},
                },
            )
            with pytest.raises(Exception, match="cannot access"):
                await server.call_tool(
                    "rulebook_draft",
                    {
                        **arguments,
                        "principal_id": "player:ocr",
                        "idempotency_key": "player-recovery",
                    },
                )
            await server.call_tool(
                "game_phase",
                {
                    "campaign_id": campaign["id"],
                    "action": "set",
                    "tool_profile": "play",
                    "expected_revision": campaign["revision"],
                    "idempotency_key": "enter-play",
                },
            )
            with pytest.raises(Exception, match="only available during lobby"):
                await server.call_tool(
                    "rulebook_draft",
                    {
                        **arguments,
                        "idempotency_key": "wrong-phase-recovery",
                    },
                )

    asyncio.run(exercise())


def test_rulebook_draft_requires_explicit_dm_acknowledgement_for_warnings(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    source = import_root / "unstructured.txt"
    source.write_text("Unstructured optional rule text.", encoding="utf-8")
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        rule_import_roots=(import_root,),
    )

    async def exercise() -> None:
        server = create_server(config)
        _, campaign = await server.call_tool(
            "campaign_create",
            {"name": "Warning gate", "idempotency_key": "campaign"},
        )
        _, staged = await server.call_tool(
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "warning-source",
                    "title": "Warning source",
                    "edition": "2014",
                },
                "idempotency_key": "stage",
            },
        )
        job_id = staged["result"]["job"]["id"]
        _, inspected = await server.call_tool(
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "idempotency_key": "inspect",
            },
        )
        assert inspected["result"]["inspection"]["warnings"]
        with pytest.raises(Exception, match="must be a boolean"):
            await server.call_tool(
                "rulebook_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "edit",
                    "payload": {
                        "operation": "advance",
                        "job_id": job_id,
                        "acknowledge_warnings": "false",
                    },
                    "idempotency_key": "ingest-string-false",
                },
            )
        _, blocked = await server.call_tool(
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {"operation": "advance", "job_id": job_id},
                "idempotency_key": "ingest-blocked",
            },
        )
        assert blocked["status"] == "pending_ruling"
        assert blocked["default_resolver"] == "agent"
        assert blocked["ruling_kind"] == "source_or_scene_fact"
        assert blocked["result"]["committed"] is False
        _, ingested = await server.call_tool(
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "operation": "advance",
                    "job_id": job_id,
                    "acknowledge_warnings": True,
                },
                "idempotency_key": "ingest-acknowledged",
            },
        )
        assert ingested["result"]["source"]["source_key"] == "warning-source"

    asyncio.run(exercise())


def test_rule_review_rejects_clause_excerpts_not_in_the_cited_chunk(
    tmp_path: Path,
) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    rulebook = import_root / "ward.md"
    exact_excerpt = "The silver ward glows when a dragon enters the chamber."
    rulebook.write_text(
        (
            "# Optional Spells\n\n## Spark\n\n"
            "1st-level evocation spell\nCasting Time: 1 action\n"
            f"{exact_excerpt}\n"
        ),
        encoding="utf-8",
    )
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        rule_import_roots=(import_root,),
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Exact evidence", "idempotency_key": "campaign"},
        )
        job = await call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_key": "silver-ward",
                    "title": "Silver Ward",
                    "edition": "2014",
                    "source_path": str(rulebook),
                },
                "principal_id": "system:local",
                "idempotency_key": "job",
            },
        )
        job_id = job["job"]["id"]
        await call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "principal_id": "system:local",
                "idempotency_key": "inspect",
            },
        )
        await call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "principal_id": "system:local",
                "idempotency_key": "ingest",
            },
        )
        extracted = await call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"job_id": job_id},
                "idempotency_key": "extract",
            },
        )
        candidate = next(item for item in extracted["candidates"] if item["name"] == "Spark")
        artifact = {
            "kind": candidate["kind"],
            "application_state": "catalog_only",
            "card": {"name": candidate["name"]},
            "rule_clauses": [
                {
                    "schema_version": 1,
                    "id": "ward-lore",
                    "title": "Ward Lore",
                    "scope": "descriptive",
                    "source_citations": [
                        {
                            "source": candidate["source_citations"][0]["source"],
                            "source_ref": {"chunk_id": candidate["source_chunk_ids"][0]},
                            "source_excerpt": ("The gold ward flashes when a giant enters."),
                        }
                    ],
                    "settlement": {"mode": "descriptive"},
                }
            ],
        }
        decision = {
            "id": candidate["id"],
            "review_status": "accepted",
            "artifact": artifact,
        }
        with pytest.raises(Exception, match="not exact text"):
            await call(
                server,
                "rulebook_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "edit",
                    "payload": {
                        "operation": "candidates",
                        "job_id": job_id,
                        "decisions": [decision],
                    },
                    "idempotency_key": "edit-inexact-source",
                },
            )
        decision["artifact"]["rule_clauses"][0]["source_citations"][0]["source_excerpt"] = (
            exact_excerpt
        )
        revised = await call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "operation": "candidates",
                    "job_id": job_id,
                    "decisions": [decision],
                },
                "idempotency_key": "edit-exact-source",
            },
        )
        assert revised["job"]["state"] == "review_required"

    asyncio.run(exercise())


def test_module_import_facade_stages_only_allowlisted_documents(tmp_path: Path) -> None:
    import_root = tmp_path / "modules"
    import_root.mkdir()
    source = import_root / "adventure.md"
    source.write_text(
        "<!-- sagasmith-runtime-manifest\n"
        '{"schema_version":1,"module_key":"managed-adventure",'
        '"entities":[{"id":"npc:keeper"}],'
        '"clues":[{"id":"clue:seal","trigger":"inspect the seal"}]}\n'
        "-->\n# Chapter One\n\n## Arrival\n\n"
        "#### A1. Courtyard\n30 by 20 feet\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        module_import_roots=(import_root,),
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Managed module", "idempotency_key": "campaign"},
        )
        with pytest.raises(Exception, match="outside configured import roots"):
            await call(
                server,
                "module_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "start",
                    "payload": {"source_path": str(outside)},
                    "idempotency_key": "outside",
                },
            )
        staged = await call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "managed-adventure",
                    "title": "Managed Adventure",
                },
                "idempotency_key": "stage",
            },
        )
        assert staged["job"]["state"] == "imported"
        assert staged["validation"]["valid"] is True
        assert staged["inspection"]["valid"] is True
        assert staged["inspection"]["metadata"]["normalization_cache_hit"] is True
        assert (
            staged["inspection"]["profile_metadata"]["runtime_manifest"]["module_key"]
            == "managed-adventure"
        )
        activation = await finalize_and_activate_module(
            call,
            server,
            campaign["id"],
            staged,
            source_key="managed-adventure",
            title="Managed Adventure",
            portable_id="dnd5e.module.managed-adventure",
            edition="2024",
        )
        assert activation["activated"]["activation"]["module_id"]
        listed = await call(
            server,
            "module_query",
            {"campaign_id": campaign["id"], "view": "list"},
        )
        assert listed[0]["runtime_manifest"]["module_key"] == "managed-adventure"

    asyncio.run(exercise())


def test_module_import_attaches_allowlisted_map_to_exact_scene(tmp_path: Path) -> None:
    import_root = tmp_path / "modules"
    import_root.mkdir()
    source = import_root / "adventure.md"
    source.write_text(
        "# Chapter One\n\n## Arrival\n\n#### A1. Courtyard\n30 by 20 feet\n",
        encoding="utf-8",
    )
    map_path = import_root / "courtyard.png"
    map_path.write_bytes(b"\x89PNG\r\n\x1a\ncampaign-map")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\noutside")
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        module_import_roots=(import_root,),
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Attached map", "idempotency_key": "campaign"},
        )
        staged = await call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "attached-map",
                    "title": "Attached Map",
                },
                "idempotency_key": "stage",
            },
        )
        module_id = staged["module_id"]
        chunks = await call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "evidence",
                "payload": {
                    "job_id": staged["job"]["id"],
                    "kind": "chunks",
                    "query": "Arrival",
                },
            },
        )
        scene_id = next(item["scene_id"] for item in chunks if item.get("scene_id"))
        arguments = {
            "campaign_id": campaign["id"],
            "action": "edit",
            "payload": {
                "operation": "asset",
                "module_id": module_id,
                "source_path": str(map_path),
                "asset_kind": "encounter_map",
                "scene_id": scene_id,
                "location_key": "a1-courtyard",
                "title": "Courtyard",
            },
            "idempotency_key": "attach-map",
        }
        attached = await call(server, "module_draft", arguments)
        assert await call(server, "module_draft", arguments) == attached
        assert attached["asset"]["media_type"] == "image/png"
        assert attached["asset"]["metadata"] == {
            "kind": "encounter_map",
            "source_name": "courtyard.png",
            "title": "Courtyard",
            "scene_id": scene_id,
            "location_key": "a1-courtyard",
        }
        assert Path(attached["asset"]["source_path"]).parent.name == module_id
        assets = await call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "assets",
                "payload": {"module_id": module_id},
            },
        )
        assert attached["asset"]["id"] in {item["id"] for item in assets}
        with pytest.raises(Exception, match="outside configured import roots"):
            await call(
                server,
                "module_draft",
                {
                    **arguments,
                    "payload": {**arguments["payload"], "source_path": str(outside)},
                    "idempotency_key": "attach-outside",
                },
            )
        await finalize_and_activate_module(
            call,
            server,
            campaign["id"],
            staged,
            source_key="attached-map",
            title="Attached Map",
            portable_id="dnd5e.module.attached-map",
            edition="2024",
        )

    asyncio.run(exercise())
