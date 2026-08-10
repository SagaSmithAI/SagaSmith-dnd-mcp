import asyncio
import json
import os
from pathlib import Path

import pytest
from mcp.types import ImageContent, TextContent
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sagasmith_core import OcrPageLayout, OcrTextBlock, RapidOcrProvider

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from tests.authoring_helpers import finalize_and_activate_module

NECROMITE = """# Necromite of Myrkul

*Medium humanoid (human), neutral evil*

**Armor Class** 11
**Hit Points** 13 (2d8 + 4)
**Speed** 30 ft.

| STR | DEX | CON | INT | WIS | CHA |
|---|---|---|---|---|---|
| 10 (+0) | 13 (+1) | 15 (+2) | 16 (+3) | 11 (+0) | 10 (+0) |

**Skills** Arcana +5, Religion +5
**Senses** passive Perception 10
**Languages** Abyssal, Common, Infernal
**Challenge** 1/2 (100 XP)

## Actions

***Skull Flail***. *Melee Weapon Attack:* +2 to hit, reach 5 ft., one target.
*Hit:* 4 (1d8) bludgeoning damage.

***Claws of the Grave***. *Ranged Spell Attack:* +5 to hit, range 90 ft., one target.
*Hit:* 8 (2d4 + 3) necrotic damage.
"""


def _write_text_pdf(path: Path) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    resources = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    page[NameObject("/Resources")] = resources
    lines = [
        "Chapter 1: Dungeon",
        "D5. Entry",
        "A stone corridor descends into darkness.",
        "D6. Morgue",
        "A chamber holds a bloated corpse.",
        "D7. Altar",
        "An altar stands in the flooded room.",
    ]
    operators = [b"BT /F1 12 Tf 72 720 Td 16 TL"]
    for index, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if index:
            operators.append(b"T*")
        operators.append(f"({escaped}) Tj".encode("ascii"))
    operators.append(b"ET")
    stream = DecodedStreamObject()
    stream.set_data(b"\n".join(operators))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as output:
        writer.write(output)
    assert "D5. Entry" in (PdfReader(str(path)).pages[0].extract_text() or "")


async def _call(server, name: str, arguments: dict):
    called = await server.call_tool(name, arguments)
    if isinstance(called, tuple):
        _, result = called
        return result.get("result", result) if isinstance(result, dict) else result
    return called


def test_pdf_page_review_becomes_snapshot_managed_scene_atlas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_root = tmp_path / "modules"
    import_root.mkdir()
    source = import_root / "dungeon.pdf"
    _write_text_pdf(source)
    layout = OcrPageLayout(
        page_number=1,
        width=450,
        height=300,
        blocks=(OcrTextBlock("D5. Entry", 0.97, 20, 20, 120, 45),),
    )

    def extract_layout(
        provider: RapidOcrProvider,
        path: Path,
        *,
        page_numbers: list[int] | None = None,
    ) -> list[OcrPageLayout]:
        assert provider.model_type in {"medium", "small"}
        assert page_numbers == [1]
        return [layout]

    monkeypatch.setattr(RapidOcrProvider, "extract_layout", extract_layout)
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
        module_import_roots=(import_root,),
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Visual atlas", "edition": "2014", "idempotency_key": "campaign"},
        )
        staged = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "visual-dungeon",
                    "title": "Visual Dungeon",
                },
                "idempotency_key": "stage",
            },
        )
        module_id = staged["module_id"]
        assets = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "assets",
                "payload": {"module_id": module_id},
            },
        )
        source_asset = next(item for item in assets if item["media_type"] == "application/pdf")
        rendered = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "evidence",
                "payload": {"kind": "page", "module_id": module_id, "page_number": 1},
            },
        )
        assert isinstance(rendered.content[0], TextContent)
        assert isinstance(rendered.content[1], ImageContent)
        render_metadata = json.loads(rendered.content[0].text)
        assert render_metadata["page_number"] == 1
        assert render_metadata["transcription"]["ocr"]["text"] == "D5. Entry"
        assert render_metadata["transcription"]["ocr"]["model"] == "medium"
        assert rendered.structuredContent == render_metadata
        assert rendered.content[1].mimeType == "image/png"

        index = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "index",
                "payload": {"module_id": module_id},
            },
        )
        located_scenes = [item for item in index if item["spatial"].get("locations")]
        scene = located_scenes[0]
        reviewed = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "edit",
                "payload": {
                    "operation": "content",
                    "module_id": module_id,
                    "scene_id": scene["scene_id"],
                    "content_key": "necromite-of-myrkul",
                    "normalized_content": NECROMITE,
                    "source_asset_id": source_asset["id"],
                    "page_number": 1,
                    "observation": (
                        "The reviewed page visibly contains the complete creature card."
                    ),
                },
                "idempotency_key": "review-necromite",
            },
        )
        assert reviewed["validation"]["settlement"] == "automatic"
        review_id = reviewed["review"]["id"]
        queried = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "content",
                "payload": {"review_id": review_id},
            },
        )
        assert queried["evidence"]["asset_checksum"] == source_asset["checksum"]
        created = await _call(
            server,
            "character_create_from",
            {
                "mode": "module_statblock",
                "payload": {
                    "campaign_id": campaign["id"],
                    "review_id": review_id,
                    "name": "D10 Necromite 1",
                    "character_type": "monster",
                },
                "idempotency_key": "create-necromite",
            },
        )
        attacks = {
            item["name"]: item
            for item in created["character"]["derived"]["inventory"]["weapon_attacks"]
        }
        assert attacks["Claws of the Grave"]["attack_bonus"] == 5
        assert attacks["Claws of the Grave"]["damage_expression"] == "2d4 + 3"
        assert created["statblock"]["settlement"] == "automatic"
        await finalize_and_activate_module(
            _call,
            server,
            campaign["id"],
            staged,
            source_key="visual-dungeon",
            title="Visual Dungeon",
            portable_id="dnd5e.module.visual-dungeon-test",
        )
        active_index = await _call(
            server,
            "module_query",
            {"campaign_id": campaign["id"], "view": "index"},
        )
        active_scenes = [item for item in active_index if item["spatial"].get("locations")]
        active_scene = active_scenes[0]
        active_keys = [
            active_scenes[0]["spatial"]["locations"][0]["key"],
            active_scenes[1]["spatial"]["locations"][0]["key"],
        ]
        active_assets = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "assets",
                "payload": {"module_id": active_scene["module_id"]},
            },
        )
        active_source_asset = next(
            item for item in active_assets if item["media_type"] == "application/pdf"
        )
        progress = await _call(
            server,
            "module_set_progress",
            {
                "campaign_id": campaign["id"],
                "scene_id": active_scene["scene_id"],
                "current_location_key": active_keys[0],
                "expected_state_version": 0,
                "idempotency_key": "review-map",
                "spatial_review": {
                    "source_asset_id": active_source_asset["id"],
                    "page_number": 1,
                    "connections": [
                        {
                            "from": active_keys[0],
                            "to": active_keys[1],
                            "kind": "passage",
                            "observation": "The reviewed page visibly joins these rooms.",
                        }
                    ],
                },
            },
        )
        assert progress["state_version"] == 1
        current = await _call(
            server,
            "module_query",
            {"campaign_id": campaign["id"], "view": "current"},
        )
        connection = current["spatial"]["connections"][0]
        assert connection["confidence"] == "reviewed_image"
        assert connection["evidence"]["asset_id"] == active_source_asset["id"]
        assert connection["evidence"]["branch_id"]

    asyncio.run(exercise())


def test_module_statblock_ocr_recovery_supports_text_only_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_root = tmp_path / "modules"
    import_root.mkdir()
    source = import_root / "text-only-review.pdf"
    _write_text_pdf(source)

    def block(
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
            block("COMMONER", 30, 20, 180, 45),
            block("Medium humanoid, any alignment", 30, 45, 250, 65),
            block("Armor Class 10", 30, 75, 160, 95),
            block("Hit Points 4 (1d8)", 30, 95, 190, 115),
            block("Speed 30 ft.", 30, 115, 150, 135),
            *tuple(
                block(label, 30 + index * 70, 145, 70 + index * 70, 165)
                for index, label in enumerate(("STR", "DEX", "CON", "INT", "WIS", "CHA"))
            ),
            *tuple(
                block("10 (+0)", 25 + index * 70, 165, 80 + index * 70, 185) for index in range(6)
            ),
            block("Senses passive Perception 10", 30, 200, 250, 220),
            block("Languages Common", 30, 220, 180, 240),
            block("Challenge 0 (10 XP)", 30, 240, 200, 260),
            block("ACTIONS", 30, 275, 130, 295),
            block(
                "Club. Melee Weapon Attack: +2 to hit, reach 5 ft., one target.",
                30,
                305,
                480,
                325,
            ),
            block("Hit: 2 (1d4) bludgeoning damage.", 30, 325, 310, 345),
            block("COMMONER", 30, 355, 180, 380),
        ),
    )
    multiattack_layout = OcrPageLayout(
        page_number=1,
        width=600,
        height=400,
        blocks=(
            *layout.blocks[:20],
            block("ACTIONS", 30, 265, 130, 285),
            block(
                "Multiattack. The commoner makes two attacks with its club.",
                30,
                290,
                500,
                310,
            ),
            block(
                "Club. Melee Weapon Attack: +2 to hit, reach 5 ft., one target.",
                30,
                315,
                480,
                335,
            ),
            block("Hit: 2 (1d4) bludgeoning damage.", 30, 335, 310, 355),
            block("COMMONER", 30, 370, 180, 390),
        ),
    )
    active_layout = [layout]
    ocr_calls = 0
    ocr_sources: list[Path] = []

    def extract_layout(
        provider: RapidOcrProvider,
        path: Path,
        *,
        page_numbers: list[int] | None = None,
    ) -> list[OcrPageLayout]:
        nonlocal ocr_calls
        ocr_calls += 1
        ocr_sources.append(Path(path))
        return [active_layout[0]]

    monkeypatch.setattr(RapidOcrProvider, "extract_layout", extract_layout)
    monkeypatch.setattr(
        "sagasmith_dnd_mcp.server.extract_pdf_page_text",
        lambda path, page_number: (
            "Medium humanoid, any alignment\n"
            "Armor Class 10\n"
            "Hit Points 4 (1d8)\n"
            "Speed 30 ft.\n"
            "STR 10 (+0) DEX 10 (+0) CON 10 (+0) "
            "INT 10 (+0) WIS 10 (+0) CHA 10 (+0)\n"
            "Senses passive Perception 10\n"
            "Languages Common\n"
            "Challenge 0 (10 XP)"
        ),
    )
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
        module_import_roots=(import_root,),
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Module OCR", "edition": "2014", "idempotency_key": "campaign"},
        )
        staged = await _call(
            server,
            "module_draft",
            {
                "campaign_id": campaign["id"],
                "action": "start",
                "payload": {
                    "source_path": str(source),
                    "source_key": "module-ocr",
                    "title": "Module OCR",
                },
                "idempotency_key": "stage",
            },
        )
        module_id = staged["module_id"]
        scene_id = (
            await _call(
                server,
                "module_query",
                {
                    "campaign_id": campaign["id"],
                    "view": "index",
                    "payload": {"module_id": module_id},
                },
            )
        )[0]["scene_id"]
        arguments = {
            "campaign_id": campaign["id"],
            "action": "edit",
            "payload": {
                "operation": "statblock",
                "module_id": module_id,
                "scene_id": scene_id,
                "content_key": "commoner",
                "name": "Commoner",
                "page_number": 1,
            },
            "idempotency_key": "recover-commoner",
        }
        recovered = await _call(server, "module_draft", arguments)
        replayed = await _call(server, "module_draft", arguments)

        assert replayed == recovered
        assert ocr_calls == 1
        assert recovered["provider"] == "rapidocr"
        assert recovered["corroboration_mode"] == "embedded_text"
        assert recovered["recovery"]["evidence"]["text_only"] is True
        assert recovered["review"]["evidence"]["confidence"] == "reviewed_image"
        assert recovered["review"]["metadata"]["text_layout_recovery"]["text_only"] is True
        assert recovered["validation"]["name"] == "Commoner"
        assert recovered["validation"]["settlement"] == "automatic"
        assert recovered["requires_agent_fill"] is False

        active_layout[0] = multiattack_layout
        # A page layout is cached for one immutable source revision.  Advance
        # the source mtime to model a replaced PDF before changing the mocked
        # provider output; repeated reviews of the same revision reuse OCR.
        cached_source = ocr_sources[-1]
        source_stat = cached_source.stat()
        os.utime(
            cached_source,
            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns + 1_000_000_000),
        )
        preview_arguments = {
            **arguments,
            "payload": {
                **arguments["payload"],
                "content_key": "commoner-multiattack",
            },
            "idempotency_key": "recover-commoner-multiattack-preview",
        }
        preview = await _call(server, "module_draft", preview_arguments)
        replayed_preview = await _call(server, "module_draft", preview_arguments)
        assert replayed_preview == preview
        assert preview["review"] is None
        assert preview["requires_agent_fill"] is True
        requirements = preview["validation"]["agent_fill_requirements"]
        assert requirements["required"] is True
        assert requirements["parser_authoritative"] is False
        activity = requirements["multiattack_options"][0]
        club = next(item for item in requirements["available_weapons"] if item["name"] == "Club")

        filled_arguments = {
            **preview_arguments,
            "payload": {
                **preview_arguments["payload"],
                "agent_fill": {
                    "multiattack_options": [
                        {
                            "activity_id": activity["activity_id"],
                            "source_excerpt": activity["source_excerpt"],
                            "reason": (
                                "The printed module action explicitly says the "
                                "commoner makes two attacks with its club."
                            ),
                            "options": [
                                {
                                    "id": "two-club-attacks",
                                    "attacks": [
                                        {
                                            "weapon_id": club["weapon_id"],
                                            "attack_mode": "melee",
                                            "count": 2,
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                },
            },
            "idempotency_key": "recover-commoner-multiattack-filled",
        }
        filled = await _call(server, "module_draft", filled_arguments)
        assert filled["requires_agent_fill"] is False
        assert filled["review"]["id"]
        assert filled["validation"]["agent_fill"]["multiattack_options"][0]["options"] == [
            {
                "id": "two-club-attacks",
                "attacks": [
                    {
                        "weapon_id": club["weapon_id"],
                        "attack_mode": "melee",
                        "count": 2,
                    }
                ],
            }
        ]
        assert ocr_calls == 2
        await finalize_and_activate_module(
            _call,
            server,
            campaign["id"],
            staged,
            source_key="module-ocr",
            title="Module OCR",
            portable_id="dnd5e.module.module-ocr-test",
        )

    asyncio.run(exercise())
