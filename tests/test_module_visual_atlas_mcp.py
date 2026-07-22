import asyncio
import json
from pathlib import Path

from mcp.types import CallToolResult, ImageContent, TextContent
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server

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
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
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


def test_pdf_page_review_becomes_snapshot_managed_scene_atlas(tmp_path: Path) -> None:
    import_root = tmp_path / "modules"
    import_root.mkdir()
    source = import_root / "dungeon.pdf"
    _write_text_pdf(source)
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
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "stage",
                "payload": {
                    "source_path": str(source),
                    "source_key": "visual-dungeon",
                    "title": "Visual Dungeon",
                },
                "idempotency_key": "stage",
            },
        )
        job_id = staged["job"]["id"]
        for action in ("inspect", "validate", "ingest"):
            ingested = await _call(
                server,
                "module_import",
                {
                    "campaign_id": campaign["id"],
                    "action": action,
                    "payload": {"job_id": job_id},
                    "idempotency_key": action,
                },
            )
        campaign = await _call(
            server, "campaign_query", {"view": "get", "payload": {"campaign_id": campaign["id"]}}
        )
        await _call(
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
        module_id = ingested["module_id"]
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
            "module_page_render",
            {
                "campaign_id": campaign["id"],
                "module_id": module_id,
                "page_number": 1,
            },
        )
        assert isinstance(rendered[0], TextContent)
        assert isinstance(rendered[1], ImageContent)
        render_metadata = json.loads(rendered[0].text)
        assert render_metadata["asset"]["metadata"]["source_page"] == 1
        assert rendered[1].mimeType == "image/png"

        opened = await _call(
            server,
            "exposure_open",
            {"campaign_id": campaign["id"], "principal_id": "system:local"},
        )
        await _call(
            server,
            "exposure_load",
            {"exposure_id": opened["exposure_id"], "group_id": "lobby.modules"},
        )
        fallback = await server.call_tool(
            "exposure_call",
            {
                "exposure_id": opened["exposure_id"],
                "tool_id": "module_page_render",
                "arguments": {
                    "campaign_id": campaign["id"],
                    "module_id": module_id,
                    "page_number": 1,
                },
            },
        )
        assert isinstance(fallback, CallToolResult)
        assert isinstance(fallback.content[0], TextContent)
        assert isinstance(fallback.content[1], ImageContent)
        fallback_envelope = json.loads(fallback.content[0].text)
        assert fallback_envelope["tool_id"] == "module_page_render"
        assert fallback_envelope["result"]["asset"]["metadata"]["source_page"] == 1
        assert fallback.structuredContent == fallback_envelope
        assert fallback.content[1].mimeType == "image/png"

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
        keys = [
            located_scenes[0]["spatial"]["locations"][0]["key"],
            located_scenes[1]["spatial"]["locations"][0]["key"],
        ]
        progress = await _call(
            server,
            "module_set_progress",
            {
                "campaign_id": campaign["id"],
                "scene_id": scene["scene_id"],
                "current_location_key": keys[0],
                "expected_state_version": 0,
                "idempotency_key": "review-map",
                "spatial_review": {
                    "source_asset_id": source_asset["id"],
                    "page_number": 1,
                    "connections": [
                        {
                            "from": keys[0],
                            "to": keys[1],
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
        assert connection["evidence"]["asset_id"] == source_asset["id"]
        assert connection["evidence"]["branch_id"]

        reviewed = await _call(
            server,
            "module_content_review",
            {
                "campaign_id": campaign["id"],
                "module_id": module_id,
                "scene_id": scene["scene_id"],
                "content_key": "necromite-of-myrkul",
                "normalized_content": NECROMITE,
                "source_asset_id": source_asset["id"],
                "page_number": 1,
                "observation": "The reviewed page visibly contains the complete creature card.",
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

    asyncio.run(exercise())
