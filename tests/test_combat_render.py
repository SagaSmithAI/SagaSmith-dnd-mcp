from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from io import BytesIO
from pathlib import Path

from mcp.types import ImageContent, TextContent
from PIL import Image
from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp.combat_render import render_combat_png
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from sagasmith_dnd_mcp.storage import SagaSmithStorage


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def _config(tmp_path: Path) -> McpConfig:
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )


def _portrait_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (20, 30), "#315a72").save(output, format="PNG")
    return output.getvalue()


def test_render_combat_png_is_deterministic_and_uses_portrait() -> None:
    encounter = {
        "id": "encounter-1",
        "name": "The Broken Gate",
        "positioning_mode": "grid",
        "round": 2,
        "turn_index": 0,
        "battle_map": {
            "map_revision": 3,
            "grid": {"kind": "square", "cell_ft": 5},
            "bounds": {"width_cells": 5, "height_cells": 4},
            "blocked_cells": ["2,1"],
            "difficult_cells": ["3,2"],
        },
        "combatants": [
            {
                "actor_id": "hero",
                "name": "Hero",
                "initiative": 18,
                "position": {"x": 1, "y": 1},
                "disposition": "friendly",
            },
            {
                "actor_id": "foe",
                "name": "Foe",
                "initiative": 12,
                "position": {"x": 4, "y": 2},
                "disposition": "hostile",
            },
        ],
    }
    first_metadata, first = render_combat_png(
        encounter,
        portraits={"hero": _portrait_bytes()},
        audience_projection="party_public",
    )
    second_metadata, second = render_combat_png(
        encounter,
        portraits={"hero": _portrait_bytes()},
        audience_projection="party_public",
    )

    assert first.startswith(b"\x89PNG\r\n\x1a\n")
    assert first == second
    assert first_metadata == second_metadata
    assert first_metadata["image_checksum"] == hashlib.sha256(first).hexdigest()
    assert first_metadata["map_revision"] == 3
    assert first_metadata["current_actor_id"] == "hero"
    assert "Hero" in first_metadata["alt_text"]


def test_managed_actor_image_is_checksum_bound(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.prepare()
    storage = SagaSmithStorage(config)
    content = _portrait_bytes()
    checksum = hashlib.sha256(content).hexdigest()

    stored = storage.store_actor_image(
        {"checksum": checksum, "media_type": "image/png"},
        content,
    )

    assert Path(stored["path"]).parent == config.actor_images_dir
    assert storage.read_actor_image(checksum) == content
    assert storage.read_actor_image("../portrait.png") is None


def test_combat_query_render_returns_image_and_party_projection_hides_actor(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Render", "edition": "2014", "idempotency_key": "campaign"},
        )
        characters = []
        for key, name in (("hero", "Visible Hero"), ("foe", "Hidden Foe")):
            created = await _call(
                server,
                "character_create_from",
                {
                    "mode": "direct",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "name": name,
                        "character_type": "monster" if key == "foe" else "pc",
                        "sheet": default_character_sheet(),
                    },
                    "idempotency_key": key,
                },
            )
            characters.append(created)
        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        phase = await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": current["revision"],
                "idempotency_key": "play",
            },
        )
        await _call(
            server,
            "combat_start",
            {
                "campaign_id": campaign["id"],
                "participant_ids": [item["id"] for item in characters],
                "positioning_mode": "grid",
                "battle_map": {"width_cells": 6, "height_cells": 4},
                "participant_config": [
                    {
                        "actor_id": characters[0]["id"],
                        "initiative": 20,
                        "position": {"x": 1, "y": 1},
                    },
                    {
                        "actor_id": characters[1]["id"],
                        "initiative": 10,
                        "position": {"x": 4, "y": 1},
                        "hidden": True,
                    },
                ],
                "expected_revision": phase["campaign_revision"],
                "idempotency_key": "start",
            },
        )

        rendered = await server.call_tool(
            "combat_query",
            {
                "campaign_id": campaign["id"],
                "view": "render",
                "payload": {"audience_projection": "party_public"},
            },
        )

        assert isinstance(rendered.content[0], TextContent)
        assert isinstance(rendered.content[1], ImageContent)
        metadata = json.loads(rendered.content[0].text)
        content = base64.b64decode(rendered.content[1].data)
        assert rendered.structuredContent == metadata
        assert rendered.content[1].mimeType == "image/png"
        assert content.startswith(b"\x89PNG\r\n\x1a\n")
        assert metadata["audience_projection"] == "party_public"
        assert "Visible Hero" in metadata["alt_text"]
        assert "Hidden Foe" not in metadata["alt_text"]
        assert metadata["campaign_revision"] == phase["campaign_revision"] + 1

    asyncio.run(exercise())
