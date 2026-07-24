from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    value = result.get("result", result) if isinstance(result, dict) else result
    if isinstance(value, dict) and "action" in value and "result" in value:
        return value["result"]
    return value


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


def test_character_batch_query_returns_targeted_campaign_actors_in_requested_order(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Batch query", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor_ids = []
        for index, name in enumerate(("Cleric", "Fighter"), start=1):
            actor = await _call(
                server,
                "character_create_from",
                {
                    "mode": "direct",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "name": name,
                        "sheet": default_character_sheet(),
                    },
                    "idempotency_key": f"actor-{index}",
                },
            )
            actor_ids.append(actor["id"])

        values = await _call(
            server,
            "character_query",
            {
                "view": "batch",
                "payload": {
                    "campaign_id": campaign["id"],
                    "character_ids": list(reversed(actor_ids)),
                },
            },
        )

        assert [item["id"] for item in values] == list(reversed(actor_ids))
        assert all("sheet" in item and "derived" in item for item in values)

        with pytest.raises(Exception, match="unique"):
            await _call(
                server,
                "character_query",
                {
                    "view": "batch",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "character_ids": [actor_ids[0], actor_ids[0]],
                    },
                },
            )

    asyncio.run(exercise())
