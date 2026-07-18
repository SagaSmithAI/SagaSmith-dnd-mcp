import asyncio
import random
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

import sagasmith_dnd_mcp.server as server_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    value = result.get("result", result) if isinstance(result, dict) else result
    if isinstance(value, dict) and "action" in value and "result" in value:
        return value["result"]
    return value


def test_stable_recovery_is_rolled_atomic_idempotent_and_audited(
    tmp_path: Path, monkeypatch
) -> None:
    original_roll = server_module.roll

    def deterministic_roll(expression: str):
        return original_roll(expression, rng=random.Random(0))

    monkeypatch.setattr(server_module, "roll", deterministic_roll)
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Stable Recovery", "edition": "2014", "idempotency_key": "campaign"},
        )
        sheet = default_character_sheet()
        sheet["combat"]["hp"] = {"value": 0, "max": 12, "temp": 0}
        sheet["conditions"] = ["prone", "stable", "unconscious"]
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Stable Actor",
                    "sheet": sheet,
                },
                "idempotency_key": "actor",
            },
        )
        arguments = {
            "character_id": actor["id"],
            "action": "stable_recovery",
            "payload": {},
            "expected_revision": actor["revision"],
            "idempotency_key": "recover",
        }

        recovered = await _call(server, "character_state_change", arguments)
        replay = await _call(server, "character_state_change", arguments)

        assert recovered["status"] == "recovered"
        assert recovered["recovery_roll"]["expression"] == "1d4"
        assert recovered["recovery_hours"] == 4
        assert recovered["character"]["sheet"]["combat"]["hp"]["value"] == 1
        assert recovered["character"]["sheet"]["conditions"] == ["prone"]
        assert replay == recovered
        receipts = await _call(
            server,
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "receipts",
                "payload": {"mechanic_id": "dnd5e.core.damage.stable_recovery"},
            },
        )
        assert len(receipts) == 1
        assert receipts[0]["event"] == "character.stable_recovery"

    asyncio.run(exercise())


def test_stable_recovery_rejects_a_healthy_actor(tmp_path: Path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Healthy", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Healthy Actor"},
                "idempotency_key": "actor",
            },
        )
        with pytest.raises(Exception, match="Stable creature at 0"):
            await _call(
                server,
                "character_state_change",
                {
                    "character_id": actor["id"],
                    "action": "stable_recovery",
                    "payload": {},
                    "expected_revision": actor["revision"],
                    "idempotency_key": "recover",
                },
            )

    asyncio.run(exercise())
