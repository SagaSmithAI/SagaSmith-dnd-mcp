import asyncio
import json
from pathlib import Path

import pytest
from mcp.types import ImageContent, TextContent
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sagasmith_core import OcrPageLayout, OcrTextBlock, RapidOcrProvider
from sagasmith_core.rules import RuleService

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


def test_rule_import_discovers_nested_allowlisted_rulebooks(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    nested = import_root / "third-party"
    nested.mkdir(parents=True)
    (import_root / "core.pdf").write_bytes(b"pdf")
    (nested / "supplement.md").write_text("# Supplement\n", encoding="utf-8")
    (nested / "ignored.exe").write_bytes(b"ignored")
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
            {"name": "Discovery", "idempotency_key": "campaign"},
        )
        _, discovered = await server.call_tool(
            "rule_import",
            {"campaign_id": campaign["id"], "action": "discover"},
        )

        assert discovered["result"]["count"] == 2
        assert {item["relative_path"] for item in discovered["result"]["documents"]} == {
            "core.pdf",
            str(Path("third-party") / "supplement.md"),
        }

    asyncio.run(exercise())


def test_rule_import_renders_a_checksum_bound_review_page(
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
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "stage",
                "payload": {
                    "source_path": str(source),
                    "source_key": "review",
                    "title": "Review",
                    "edition": "2014",
                },
                "idempotency_key": "stage",
            },
        )
        job_id = staged["result"]["job"]["id"]
        rendered = await server.call_tool(
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "render_page",
                "payload": {"job_id": job_id, "page_number": 1},
            },
        )

        assert isinstance(rendered.content[0], TextContent)
        assert isinstance(rendered.content[1], ImageContent)
        metadata = json.loads(rendered.content[0].text)
        assert metadata["page_number"] == 1
        assert metadata["source_checksum"] == staged["result"]["checksum"]
        assert rendered.structuredContent == metadata
        assert rendered.content[1].mimeType == "image/png"

        _, inspected = await server.call_tool(
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "inspect",
                "payload": {"job_id": job_id},
                "idempotency_key": "inspect",
            },
        )
        _, ingested = await server.call_tool(
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "ingest",
                "payload": {
                    "job_id": job_id,
                    "acknowledge_warnings": bool(inspected["result"]["inspection"]["warnings"]),
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

***Shout***. The commoner calls loudly for help.
"""
        review_arguments = {
            "campaign_id": campaign["id"],
            "action": "review_statblock",
            "payload": {
                "job_id": job_id,
                "page_number": 1,
                "normalized_content": commoner,
                "observation": "DM compared every field with the rendered source page.",
            },
            "idempotency_key": "review-statblock",
        }
        _, reviewed = await server.call_tool("rule_import", review_arguments)
        _, replayed = await server.call_tool("rule_import", review_arguments)
        assert replayed == reviewed
        review = reviewed["result"]["review"]
        assert review["source_id"] == ingested["result"]["source_id"]
        assert review["asset_checksum"] == metadata["source_checksum"]
        assert review["image_checksum"] == metadata["image_checksum"]
        validation = reviewed["result"]["validation"]
        assert validation["default_dm_resolver"] == "agent"
        assert validation["settlement"] == "mixed"
        assert validation["ruling_requirements"] == [
            {
                "reason": "Shout: descriptive action is not automatically settled",
                "default_resolver": "agent",
                "ruling_kind": "agent_dm_adjudication",
                "policy_ref": "server_capabilities.ruling_policy",
                "requires_external_input_only_for": [
                    "player_owned_choice",
                    "owner_approval",
                    "permission_escalation",
                    "missing_or_conflicting_source_review",
                ],
            }
        ]

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
            "action": "review_statblock",
            "payload": {
                "job_id": job_id,
                "page_number": 1,
                "normalized_content": reviewed_monster,
                "observation": "DM compared every monster field with the rendered page.",
            },
            "idempotency_key": "review-monster-without-fill",
        }
        with pytest.raises(Exception, match="requires an Agent statblock fill"):
            await server.call_tool("rule_import", missing_fill_arguments)

        source_excerpt = (
            "The hunter makes one bite attack and one claw attack."
        )
        agent_fill = {
            "multiattack_options": [
                {
                    "activity_id": "multiattack-activity",
                    "source_excerpt": source_excerpt,
                    "reason": (
                        "The reviewed sentence explicitly requires one bite and one claw."
                    ),
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
        filled_arguments = {
            **missing_fill_arguments,
            "payload": {
                **missing_fill_arguments["payload"],
                "agent_fill": agent_fill,
            },
            "idempotency_key": "review-monster-with-fill",
        }
        _, filled_review_response = await server.call_tool(
            "rule_import",
            filled_arguments,
        )
        filled_review = filled_review_response["result"]["review"]
        filled_validation = filled_review_response["result"]["validation"]
        assert filled_review["agent_statblock_fill"]["multiattack_options"][0][
            "activity_id"
        ] == "multiattack-activity"
        assert filled_validation["resolved_warnings"] == []
        assert filled_validation["warnings"] == []
        assert filled_validation["agent_fill_requirements"]["required"] is True

        _, augmented_review_response = await server.call_tool(
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "review_statblock",
                "payload": {
                    "job_id": job_id,
                    "base_review_id": filled_review["id"],
                    "observation": (
                        "Agent reused the checksum-bound transcription and "
                        "confirmed the exact Multiattack composition."
                    ),
                    "agent_fill": agent_fill,
                },
                "idempotency_key": "augment-reviewed-monster-fill",
            },
        )
        augmented_review = augmented_review_response["result"]["review"]
        assert augmented_review["derived_from_review_id"] == filled_review["id"]
        assert (
            augmented_review["normalized_content_sha256"]
            == filled_review["normalized_content_sha256"]
        )
        assert (
            augmented_review["agent_statblock_fill"]
            == filled_review["agent_statblock_fill"]
        )
        with pytest.raises(Exception, match="does not belong"):
            await server.call_tool(
                "rule_import",
                {
                    "campaign_id": campaign["id"],
                    "action": "review_statblock",
                    "payload": {
                        "job_id": job_id,
                        "base_review_id": "rule-statblock-review:unknown",
                        "observation": "Agent checked the retained transcription.",
                        "agent_fill": agent_fill,
                    },
                    "idempotency_key": "augment-unknown-reviewed-monster",
                },
            )

        _, filled_actor_response = await server.call_tool(
            "character_create_from",
            {
                "mode": "reviewed_rule_statblock",
                "payload": {
                    "campaign_id": campaign["id"],
                    "job_id": job_id,
                    "review_id": filled_review["id"],
                    "name": "Reviewed Hunter",
                    "character_type": "monster",
                },
                "idempotency_key": "reviewed-hunter",
            },
        )
        filled_actor = filled_actor_response["result"]
        assert filled_actor["character"]["derived"]["multiattack_options"] == [
            {
                "id": "bite-and-claw",
                "attacks": [
                    {"weapon_id": "bite", "attack_mode": "melee", "count": 1},
                    {"weapon_id": "claw", "attack_mode": "melee", "count": 1},
                ],
            }
        ]
        assert filled_actor["statblock"]["warnings"] == []
        assert filled_actor["statblock"]["agent_fill"]["multiattack_options"][0][
            "default_resolver"
        ] == "agent"
        assert (
            "Agent statblock fill: multiattack-activity."
            in filled_actor["character"]["notes"]["profile"]["dm_notes"]
        )

        evidence_chunks = [
            {
                "id": "commoner-core",
                "ordinal": 0,
                "heading_path": ["COMMONER"],
                "content": (
                    "Medium humanoid (any race), any alignment Armor Class 10 "
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
                        "10 (+0) Senses passive Perception 10 Languages Common "
                        "Challenge 0 (10 XP)"
                        if ability == "WIS"
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
                    "Shout. The commoner calls loudly for help. "
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
        _, agent_reviewed = await server.call_tool("rule_import", agent_arguments)
        agent_review = agent_reviewed["result"]["review"]
        assert agent_review["review_mode"] == "agent_text"
        assert agent_review["confidence"] == "reviewed_text"
        assert agent_review["evidence_chunk_ids"] == [
            item["id"] for item in evidence_chunks
        ]
        assert agent_review["text_evidence"][0]["ordinal"] == 0

        with pytest.raises(Exception, match="facts absent"):
            await server.call_tool(
                "rule_import",
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
                "rule_import",
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
                "rule_import",
                {
                    **agent_arguments,
                    "payload": {
                        **agent_arguments["payload"],
                        "evidence_chunk_ids": [
                            item["id"]
                            for item in evidence_chunks
                            if item["ordinal"] != 3
                        ],
                    },
                    "idempotency_key": "review-statblock-agent-gap",
                },
            )

    asyncio.run(exercise())


@pytest.mark.parametrize("embedded_text", [True, False])
def test_rule_import_recovers_statblock_for_text_only_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    embedded_text: bool,
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
            b"(Hit Points 4 [1d8]) Tj T* "
            b"(Speed 30 ft.) Tj T* "
            b"(STR) Tj T* (10 [+0]) Tj T* "
            b"(DEX) Tj T* (10 [+0]) Tj T* "
            b"(CON) Tj T* (10 [+0]) Tj T* "
            b"(INT) Tj T* (10 [+0]) Tj T* "
            b"(WIS) Tj T* (10 [+0]) Tj T* "
            b"(CHA) Tj T* (10 [+0]) Tj T* "
            b"(Senses passive Perception 10) Tj T* "
            b"(Languages Common) Tj T* "
            b"(Challenge 0 [10 XP]) Tj ET"
        )
        page[NameObject("/Contents")] = writer._add_object(content)
    with source.open("wb") as stream:
        writer.write(stream)

    def ocr_block(text: str, x0: int, y0: int, x1: int, y1: int) -> OcrTextBlock:
        return OcrTextBlock(text, 0.99, x0, y0, x1, y1)

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
                ocr_block(label, 30 + index * 70, 145, 70 + index * 70, 165)
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
    monkeypatch.setattr(
        RapidOcrProvider,
        "extract_layout",
        lambda self, path, *, page_numbers=None: [layout],
    )
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
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "stage",
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
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "inspect",
                "payload": {"job_id": job_id},
                "idempotency_key": "inspect",
            },
        )
        await server.call_tool(
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "ingest",
                "payload": {
                    "job_id": job_id,
                    "acknowledge_warnings": bool(
                        inspected["result"]["inspection"]["warnings"]
                    ),
                },
                "idempotency_key": "ingest",
            },
        )
        arguments = {
            "campaign_id": campaign["id"],
            "action": "recover_statblock",
            "payload": {
                "job_id": job_id,
                "name": "Commoner",
                "page_number": 1,
            },
            "idempotency_key": "recover",
        }
        _, recovered = await server.call_tool("rule_import", arguments)
        _, replayed = await server.call_tool("rule_import", arguments)

        assert replayed == recovered
        result = recovered["result"]
        assert result["page_number"] == 1
        assert result["provider"] == "rapidocr"
        assert result["corroboration_mode"] == (
            "embedded_text" if embedded_text else "dual_layout_ocr"
        )
        assert result["recovery"]["evidence"]["text_only"] is True
        assert result["recovery"]["evidence"]["matching_heading_count"] == 2
        assert result["recovery"]["evidence"]["structural_heading_count"] == 1
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
        if embedded_text:
            with pytest.raises(Exception, match="unsupported .* payload fields"):
                await server.call_tool(
                    "rule_import",
                    {
                        **arguments,
                        "payload": {**arguments["payload"], "unreviewed_text": "no"},
                        "idempotency_key": "invalid-payload",
                    },
                )
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
                    "rule_import",
                    {
                        **arguments,
                        "principal_id": "player:ocr",
                        "idempotency_key": "player-recovery",
                    },
                )
            await server.call_tool(
                "campaign_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "update",
                    "payload": {"state": {"game_phase": "play"}},
                    "expected_revision": campaign["revision"],
                    "idempotency_key": "enter-play",
                },
            )
            with pytest.raises(Exception, match="only available during lobby"):
                await server.call_tool(
                    "rule_import",
                    {
                        **arguments,
                        "idempotency_key": "wrong-phase-recovery",
                    },
                )

    asyncio.run(exercise())


def test_rule_import_requires_explicit_dm_acknowledgement_for_warnings(tmp_path: Path) -> None:
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
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "stage",
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
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "inspect",
                "payload": {"job_id": job_id},
                "idempotency_key": "inspect",
            },
        )
        assert inspected["result"]["inspection"]["warnings"]
        with pytest.raises(Exception, match="must be a boolean"):
            await server.call_tool(
                "rule_import",
                {
                    "campaign_id": campaign["id"],
                    "action": "ingest",
                    "payload": {
                        "job_id": job_id,
                        "acknowledge_warnings": "false",
                    },
                    "idempotency_key": "ingest-string-false",
                },
            )
        _, blocked = await server.call_tool(
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "ingest",
                "payload": {"job_id": job_id},
                "idempotency_key": "ingest-blocked",
            },
        )
        assert blocked["status"] == "pending_ruling"
        assert blocked["default_resolver"] == "agent"
        assert blocked["ruling_kind"] == "source_or_scene_fact"
        assert blocked["result"]["committed"] is False
        _, ingested = await server.call_tool(
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "ingest",
                "payload": {"job_id": job_id, "acknowledge_warnings": True},
                "idempotency_key": "ingest-acknowledged",
            },
        )
        assert ingested["result"]["source"]["source_key"] == "warning-source"

    asyncio.run(exercise())


def test_rule_and_module_import_jobs_are_reviewable_and_activation_safe(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    rulebook = import_root / "supplement.md"
    rulebook.write_text(
        "# Optional Spells\n\n## Spark\n\n1st-level evocation spell\nCasting Time: 1 action\n",
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
            {"name": "Import lifecycle", "idempotency_key": "campaign"},
        )
        staged = await call(
            server,
            "rule_document_stage",
            {"campaign_id": campaign["id"], "source_path": str(rulebook)},
        )
        rule_job = await call(
            server,
            "rule_import_job_create",
            {
                "campaign_id": campaign["id"],
                "artifact": staged["artifact"],
                "source_key": "xgte-pilot",
                "title": "Xanathar Pilot",
                "edition": "2014",
                "publication_id": "xgte",
                "idempotency_key": "rule-job-create",
            },
        )
        rule_job_id = rule_job["job"]["id"]
        inspected = await call(
            server,
            "rule_import_job_inspect",
            {
                "campaign_id": campaign["id"],
                "job_id": rule_job_id,
                "idempotency_key": "rule-job-inspect",
            },
        )
        assert inspected["job"]["state"] == "inspected"
        ingest_arguments = {
            "campaign_id": campaign["id"],
            "job_id": rule_job_id,
            "idempotency_key": "rule-job-ingest",
        }
        indexed = await call(
            server,
            "rule_import_job_ingest",
            ingest_arguments,
        )
        assert indexed["source"]["edition"] == "2014"
        extracted = await call(
            server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "extract_candidates",
                "payload": {"job_id": rule_job_id},
                "idempotency_key": "rule-job-extract",
            },
        )
        spark = next(item for item in extracted["candidates"] if item["name"] == "Spark")
        assert spark["ruling_requirement"]["default_resolver"] == "agent"
        assert spark["ruling_requirement"]["ruling_kind"] == "source_or_scene_fact"
        assert extracted["job"]["review_resolution"]["default_resolver"] == "agent"
        assert extracted["job"]["review_resolution"]["ruling_kind"] == "source_or_scene_fact"
        assert extracted["job"]["review_requirements"]
        reviewed = await call(
            server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "review",
                "payload": {
                    "job_id": rule_job_id,
                    "decisions": [
                        {
                            "id": spark["id"],
                            "review_status": "accepted",
                            "artifact": {
                                "kind": "spell",
                                "application_state": "selection_ready",
                                "card": {
                                    "name": "Spark",
                                    "level": 1,
                                    "classes": ["wizard"],
                                    "definition": {},
                                },
                            },
                        }
                    ],
                },
                "idempotency_key": "rule-job-review",
            },
        )
        assert reviewed["job"]["state"] == "reviewed"
        compile_arguments = {
            "campaign_id": campaign["id"],
            "job_id": rule_job_id,
            "manifest": {
                "id": "dnd5e.xgte.import-job",
                "version": "1.0.0",
                "title": "Xanathar import job",
                "namespace": "dnd5e.xgte.import-job",
                "system_id": "dnd5e",
                "editions": ["2014"],
            },
            "idempotency_key": "rule-job-compile",
        }
        compiled = await call(
            server,
            "rule_import_job_compile",
            compile_arguments,
        )
        assert compiled["draft"]["status"] == "validated"
        install_arguments = {
            "campaign_id": campaign["id"],
            "job_id": rule_job_id,
            "idempotency_key": "rule-job-install",
        }
        installed = await call(
            server,
            "rule_import_job_install",
            install_arguments,
        )
        assert installed["job"]["state"] == "installed"
        profile = await call(
            server,
            "campaign_rule_profile_set",
            {
                "campaign_id": campaign["id"],
                "edition": "2014",
                "expected_revision": campaign["revision"],
                "idempotency_key": "profile",
            },
        )
        activate_arguments = {
            "campaign_id": campaign["id"],
            "job_id": rule_job_id,
            "expected_revision": profile["campaign_revision"],
            "idempotency_key": "rule-job-activate",
        }
        activated = await call(
            server,
            "rule_import_job_activate",
            activate_arguments,
        )
        assert activated["job"]["state"] == "activated"
        assert await call(server, "rule_import_job_ingest", ingest_arguments) == indexed
        assert await call(server, "rule_import_job_compile", compile_arguments) == compiled
        assert await call(server, "rule_import_job_install", install_arguments) == installed
        assert await call(server, "rule_import_job_activate", activate_arguments) == activated
        catalog = await call(
            server,
            "content_catalog_list",
            {"campaign_id": campaign["id"], "query": "Spark"},
        )
        assert catalog[0]["application_state"] == "selection_ready"
        assert catalog[0]["source_citations"][0]["source_key"] == "xgte-pilot"

        artifact = await call(
            server,
            "module_write",
            {
                "name": "import-job-module",
                "content": "# Chapter One\n\n## Arrival\n\n#### A1. Courtyard\n30 by 20 feet\n",
            },
        )
        module_job = await call(
            server,
            "module_import_job_create",
            {
                "campaign_id": campaign["id"],
                "artifact": artifact["artifact"],
                "source_key": "import-job-module",
                "idempotency_key": "module-job-create",
            },
        )
        module_job_id = module_job["job"]["id"]
        await call(
            server,
            "module_import_job_inspect",
            {
                "campaign_id": campaign["id"],
                "job_id": module_job_id,
                "idempotency_key": "module-job-inspect",
            },
        )
        validation = await call(
            server,
            "module_import_job_validate",
            {
                "campaign_id": campaign["id"],
                "job_id": module_job_id,
                "idempotency_key": "module-job-validate",
            },
        )
        assert validation["validation"]["valid"] is True
        assert validation["validation"]["diff"]["current_module_id"] is None
        imported_module = await call(
            server,
            "module_import_job_import",
            {
                "campaign_id": campaign["id"],
                "job_id": module_job_id,
                "idempotency_key": "module-job-import",
            },
        )
        assert await call(server, "module_index", {"campaign_id": campaign["id"]}) == []
        current = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
        module_activated = await call(
            server,
            "module_import_job_activate",
            {
                "campaign_id": campaign["id"],
                "job_id": module_job_id,
                "expected_revision": current["revision"],
                "idempotency_key": "module-job-activate",
            },
        )
        assert module_activated["activation"]["module_id"] == imported_module["module_id"]
        index = await call(server, "module_index", {"campaign_id": campaign["id"]})
        assert "Arrival" in {item["title"] for item in index}
        arrival = next(item for item in index if item["title"] == "Arrival")
        await call(
            server,
            "module_set_progress",
            {
                "campaign_id": campaign["id"],
                "scene_id": arrival["scene_id"],
                "progress": 25,
                "expected_state_version": 0,
                "idempotency_key": "arrival-progress",
            },
        )

        await call(
            server,
            "module_write",
            {
                "name": "import-job-module",
                "content": (
                    "# Chapter One\n\n## Finale\n\n"
                    "#### B1. Observatory\n25 by 25 feet\n"
                ),
            },
        )
        revision_job = await call(
            server,
            "module_import_job_create",
            {
                "campaign_id": campaign["id"],
                "artifact": artifact["artifact"],
                "source_key": "import-job-module",
                "idempotency_key": "module-revision-create",
            },
        )
        await call(
            server,
            "module_import_job_inspect",
            {
                "campaign_id": campaign["id"],
                "job_id": revision_job["job"]["id"],
                "idempotency_key": "module-revision-inspect",
            },
        )
        revision_validation = await call(
            server,
            "module_import_job_validate",
            {
                "campaign_id": campaign["id"],
                "job_id": revision_job["job"]["id"],
                "idempotency_key": "module-revision-validate",
            },
        )
        assert (
            revision_validation["validation"]["diff"]["current_module_id"]
            == imported_module["module_id"]
        )
        assert revision_validation["validation"]["diff"]["added"]
        progress_impact = revision_validation["validation"]["diff"]["progress_impact"]
        assert len(progress_impact) == 1
        assert progress_impact[0]["action"] == "needs_dm_review"
        assert progress_impact[0]["ruling_requirement"]["default_resolver"] == "agent"
        assert (
            progress_impact[0]["ruling_requirement"]["ruling_kind"]
            == "source_or_scene_fact"
        )
        aggregate = revision_validation["validation"]["ruling_requirements"]
        assert len(aggregate) == 1
        assert aggregate[0]["scope_id"] == "party"
        assert aggregate[0]["scene_id"] == arrival["scene_id"]
        assert aggregate[0]["default_resolver"] == "agent"
        assert aggregate[0]["ruling_kind"] == "source_or_scene_fact"

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
                "module_import",
                {
                    "campaign_id": campaign["id"],
                    "action": "stage",
                    "payload": {"source_path": str(outside)},
                    "idempotency_key": "outside",
                },
            )
        staged = await call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "stage",
                "payload": {
                    "source_path": str(source),
                    "source_key": "managed-adventure",
                    "title": "Managed Adventure",
                },
                "idempotency_key": "stage",
            },
        )
        assert staged["staged"] is True
        assert staged["artifact"].endswith("-adventure.md")
        job_id = staged["job"]["id"]

        inspected = await call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "inspect",
                "payload": {"job_id": job_id},
                "idempotency_key": "inspect",
            },
        )
        assert inspected["preview"]["valid"] is True
        assert inspected["preview"]["metadata"]["normalization_cache_hit"] is True
        assert (
            inspected["preview"]["profile_metadata"]["runtime_manifest"]["module_key"]
            == "managed-adventure"
        )
        validated = await call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "validate",
                "payload": {"job_id": job_id},
                "idempotency_key": "validate",
            },
        )
        assert validated["validation"]["valid"] is True
        ingested = await call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "ingest",
                "payload": {"job_id": job_id},
                "idempotency_key": "ingest",
            },
        )
        current = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
        activated = await call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "activate",
                "payload": {"job_id": job_id},
                "expected_revision": current["revision"],
                "idempotency_key": "activate",
            },
        )
        assert activated["activation"]["module_id"] == ingested["module_id"]
        listed = await call(
            server,
            "module_query",
            {"campaign_id": campaign["id"], "view": "list"},
        )
        assert listed[0]["runtime_manifest"]["module_key"] == "managed-adventure"

    asyncio.run(exercise())


def test_module_import_exact_stage_retries_survive_later_job_states(tmp_path: Path) -> None:
    import_root = tmp_path / "modules"
    import_root.mkdir()
    source = import_root / "resume.md"
    source.write_text("# Chapter One\n\n## Arrival\n\nThe party arrives.\n", encoding="utf-8")
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
            {"name": "Resumable module", "idempotency_key": "campaign"},
        )
        campaign_id = campaign["id"]
        stage_arguments = {
            "campaign_id": campaign_id,
            "action": "stage",
            "payload": {
                "source_path": str(source),
                "source_key": "resume-module",
                "title": "Resume Module",
            },
            "idempotency_key": "stage",
        }
        staged = await call(server, "module_import", stage_arguments)
        job_id = staged["job"]["id"]
        inspect_arguments = {
            "campaign_id": campaign_id,
            "action": "inspect",
            "payload": {"job_id": job_id},
            "idempotency_key": "inspect",
        }
        validate_arguments = {
            "campaign_id": campaign_id,
            "action": "validate",
            "payload": {"job_id": job_id},
            "idempotency_key": "validate",
        }
        ingest_arguments = {
            "campaign_id": campaign_id,
            "action": "ingest",
            "payload": {"job_id": job_id},
            "idempotency_key": "ingest",
        }
        inspected = await call(server, "module_import", inspect_arguments)
        validated = await call(server, "module_import", validate_arguments)
        ingested = await call(server, "module_import", ingest_arguments)
        current = await call(server, "campaign_get", {"campaign_id": campaign_id})
        activate_arguments = {
            "campaign_id": campaign_id,
            "action": "activate",
            "payload": {"job_id": job_id},
            "expected_revision": current["revision"],
            "idempotency_key": "activate",
        }
        activated = await call(server, "module_import", activate_arguments)

        assert await call(server, "module_import", stage_arguments) == staged
        assert await call(server, "module_import", inspect_arguments) == inspected
        assert await call(server, "module_import", validate_arguments) == validated
        assert await call(server, "module_import", ingest_arguments) == ingested
        assert await call(server, "module_import", activate_arguments) == activated

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
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "stage",
                "payload": {
                    "source_path": str(source),
                    "source_key": "attached-map",
                    "title": "Attached Map",
                },
                "idempotency_key": "stage",
            },
        )
        job_id = staged["job"]["id"]
        ingested = None
        for action in ("inspect", "validate", "ingest"):
            ingested = await call(
                server,
                "module_import",
                {
                    "campaign_id": campaign["id"],
                    "action": action,
                    "payload": {"job_id": job_id},
                    "idempotency_key": action,
                },
            )
        assert ingested is not None
        campaign = await call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        activated = await call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "activate",
                "payload": {"job_id": job_id},
                "expected_revision": campaign["revision"],
                "idempotency_key": "activate",
            },
        )
        module_id = activated["activation"]["module_id"]
        scenes = await call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "index",
                "payload": {"module_id": module_id},
            },
        )
        scene = next(item for item in scenes if item["title"] == "Arrival")
        arguments = {
            "campaign_id": campaign["id"],
            "action": "attach_asset",
            "payload": {
                "module_id": module_id,
                "source_path": str(map_path),
                "asset_kind": "encounter_map",
                "scene_id": scene["scene_id"],
                "location_key": "a1-courtyard",
                "title": "Courtyard",
            },
            "idempotency_key": "attach-map",
        }
        attached = await call(server, "module_import", arguments)
        assert await call(server, "module_import", arguments) == attached
        assert attached["asset"]["media_type"] == "image/png"
        assert attached["asset"]["metadata"] == {
            "kind": "encounter_map",
            "source_name": "courtyard.png",
            "title": "Courtyard",
            "scene_id": scene["scene_id"],
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
                "module_import",
                {
                    **arguments,
                    "payload": {**arguments["payload"], "source_path": str(outside)},
                    "idempotency_key": "attach-outside",
                },
            )

    asyncio.run(exercise())
