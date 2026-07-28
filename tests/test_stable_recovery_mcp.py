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
        clock = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "clock_set",
                "payload": {"day": 1},
                "expected_revision": campaign["revision"],
                "idempotency_key": "clock",
            },
        )
        await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "effect_add",
                "payload": {
                    "effect": {
                        "id": "recovery-light",
                        "name": "Recovery room light",
                        "target": {"kind": "object", "id": "mace"},
                        "duration": {"period": "hour", "remaining": 1},
                    }
                },
                "expected_revision": clock["campaign_revision"],
                "idempotency_key": "world-effect",
            },
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
        current_campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        arguments = {
            "campaign_id": campaign["id"],
            "action": "stable_recovery",
            "payload": {
                "members": [
                    {
                        "character_id": actor["id"],
                        "expected_revision": actor["revision"],
                    }
                ]
            },
            "expected_revision": current_campaign["revision"],
            "idempotency_key": "recover",
        }

        recovered = await _call(server, "campaign_change", arguments)
        replay = await _call(server, "campaign_change", arguments)

        assert recovered["status"] == "recovered"
        actor_recovery = recovered["recoveries"][actor["id"]]
        assert actor_recovery["recovery_roll"]["expression"] == "1d4"
        assert actor_recovery["recovery_hours"] == 4
        assert recovered["world_time"]["day"] == 1
        assert recovered["world_time"]["hour"] == 4
        recovered_actor = recovered["characters"][actor["id"]]
        assert recovered_actor["sheet"]["combat"]["hp"]["value"] == 1
        assert recovered_actor["sheet"]["conditions"] == ["prone"]
        assert recovered["world_expired"] == ["recovery-light"]
        assert replay == recovered
        receipt = await _call(
            server,
            "state_revision",
            {
                "campaign_id": campaign["id"],
                "action": "receipt",
                "payload": {"idempotency_key": "recover"},
            },
        )
        assert receipt["response"] == recovered
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


def test_party_stable_recovery_uses_longest_concurrent_wait(
    tmp_path: Path, monkeypatch
) -> None:
    original_roll = server_module.roll
    totals = iter((1, 4))

    class FixedRandom:
        def __init__(self, total: int) -> None:
            self.total = total

        def randint(self, minimum: int, maximum: int) -> int:
            assert minimum == 1
            assert self.total <= maximum
            return self.total

    def deterministic_roll(expression: str):
        assert expression == "1d4"
        total = next(totals)
        return original_roll(expression, rng=FixedRandom(total))

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
            {
                "name": "Concurrent Stable Recovery",
                "edition": "2014",
                "idempotency_key": "c",
            },
        )
        await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "clock_set",
                "payload": {"day": 1},
                "expected_revision": campaign["revision"],
                "idempotency_key": "clock",
            },
        )
        actors = []
        for index in range(2):
            sheet = default_character_sheet()
            sheet["combat"]["hp"] = {"value": 0, "max": 12, "temp": 0}
            sheet["conditions"] = ["prone", "stable", "unconscious"]
            actors.append(
                await _call(
                    server,
                    "character_create_from",
                    {
                        "mode": "direct",
                        "payload": {
                            "campaign_id": campaign["id"],
                            "name": f"Stable Actor {index + 1}",
                            "sheet": sheet,
                        },
                        "idempotency_key": f"actor-{index}",
                    },
                )
            )
        current_campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        arguments = {
            "campaign_id": campaign["id"],
            "action": "stable_recovery",
            "payload": {
                "members": [
                    {
                        "character_id": actor["id"],
                        "expected_revision": actor["revision"],
                    }
                    for actor in actors
                ]
            },
            "expected_revision": current_campaign["revision"],
            "idempotency_key": "recover-party",
        }

        recovered = await _call(server, "campaign_change", arguments)
        replay = await _call(server, "campaign_change", arguments)

        assert recovered["status"] == "recovered"
        assert recovered["elapsed_hours"] == 4
        assert recovered["world_time"]["hour"] == 4
        assert sorted(
            item["recovery_hours"] for item in recovered["recoveries"].values()
        ) == [1, 4]
        assert {
            item["sheet"]["combat"]["hp"]["value"]
            for item in recovered["characters"].values()
        } == {1}
        assert replay == recovered
        receipt = await _call(
            server,
            "state_revision",
            {
                "campaign_id": campaign["id"],
                "action": "receipt",
                "payload": {"idempotency_key": "recover-party"},
            },
        )
        assert receipt["response"] == recovered

    asyncio.run(exercise())


def test_stable_recovery_and_companion_short_rest_share_one_clock(
    tmp_path: Path, monkeypatch
) -> None:
    original_roll = server_module.roll

    class FixedRandom:
        def randint(self, minimum: int, maximum: int) -> int:
            assert minimum == 1
            return min(2, maximum)

    def deterministic_recovery_roll(expression: str):
        assert expression == "1d4"
        return original_roll(expression, rng=FixedRandom())

    monkeypatch.setattr(server_module, "roll", deterministic_recovery_roll)
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
            {
                "name": "Concurrent Recovery and Rest",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        stable_sheet = default_character_sheet()
        stable_sheet["combat"]["hp"] = {"value": 0, "max": 12, "temp": 0}
        stable_sheet["conditions"] = ["prone", "stable", "unconscious"]
        stable = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Stable companion",
                    "sheet": stable_sheet,
                },
                "idempotency_key": "stable",
            },
        )
        resting_sheet = default_character_sheet()
        resting_sheet["combat"]["hp"] = {"value": 1, "max": 12, "temp": 0}
        resting_sheet["combat"]["hit_dice"] = {
            "fighter:d10": {
                "label": "Fighter d10",
                "value": 1,
                "max": 1,
                "recovers_on": "long_rest",
                "source_key": "Fighter",
                "slot_level": 0,
            }
        }
        resting = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Resting companion",
                    "sheet": resting_sheet,
                },
                "idempotency_key": "resting",
            },
        )
        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        clock = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "clock_set",
                "payload": {"day": 2, "hour": 10},
                "expected_revision": current["revision"],
                "idempotency_key": "clock",
            },
        )
        arguments = {
            "campaign_id": campaign["id"],
            "action": "stable_recovery",
            "payload": {
                "members": [
                    {
                        "character_id": stable["id"],
                        "expected_revision": stable["revision"],
                    }
                ],
                "resting_members": [
                    {
                        "character_id": resting["id"],
                        "expected_revision": resting["revision"],
                        "hit_dice_spends": [{"key": "fighter:d10", "count": 1}],
                    }
                ],
            },
            "expected_revision": clock["campaign_revision"],
            "idempotency_key": "recover-and-rest",
        }

        recovered = await _call(server, "campaign_change", arguments)
        assert await _call(server, "campaign_change", arguments) == recovered
        assert recovered["elapsed_hours"] == 2
        assert recovered["world_time"]["hour"] == 12
        assert recovered["member_ids"] == [stable["id"]]
        assert recovered["resting_member_ids"] == [resting["id"]]
        assert recovered["recoveries"][stable["id"]]["after_hp"] == 1
        assert len(recovered["rested"][resting["id"]]["hit_dice_rolls"]) == 1

        stable_after = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": stable["id"]}},
        )
        resting_after = await _call(
            server,
            "character_query",
            {"view": "get", "payload": {"character_id": resting["id"]}},
        )
        assert stable_after["sheet"]["combat"]["hp"]["value"] == 1
        assert resting_after["sheet"]["combat"]["hp"]["value"] > 1
        assert (
            resting_after["sheet"]["combat"]["hit_dice"]["fighter:d10"]["value"]
            == 0
        )
        assert resting_after["sheet"]["combat"]["rest_history"][
            "last_rest_completed_elapsed_ticks"
        ] == 1200

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
        current_campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        with pytest.raises(Exception, match="Stable creature at 0"):
            await _call(
                server,
                "campaign_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "stable_recovery",
                    "payload": {
                        "members": [
                            {
                                "character_id": actor["id"],
                                "expected_revision": actor["revision"],
                            }
                        ]
                    },
                    "expected_revision": current_campaign["revision"],
                    "idempotency_key": "recover",
                },
            )

    asyncio.run(exercise())


def test_stable_recovery_advances_unanchored_game_time(tmp_path: Path, monkeypatch) -> None:
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
            {"name": "Clock first", "edition": "2014", "idempotency_key": "campaign"},
        )
        sheet = default_character_sheet()
        sheet["combat"]["hp"] = {"value": 0, "max": 12, "temp": 0}
        sheet["conditions"] = ["stable", "unconscious"]
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Stable without clock",
                    "sheet": sheet,
                },
                "idempotency_key": "actor",
            },
        )
        current_campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )

        recovered = await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "stable_recovery",
                "payload": {
                    "members": [
                        {
                            "character_id": actor["id"],
                            "expected_revision": actor["revision"],
                        }
                    ]
                },
                "expected_revision": current_campaign["revision"],
                "idempotency_key": "recover",
            },
        )
        assert recovered["recoveries"][actor["id"]]["recovery_hours"] == 4
        assert recovered["game_time"]["elapsed_ticks"] == 2400
        assert recovered["world_time"] is None
        persisted = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        assert persisted["state"]["game_time"]["elapsed_ticks"] == 2400
        assert "world_time" not in persisted["state"]

    asyncio.run(exercise())


def test_stable_recovery_validates_character_revision_before_rolling(
    tmp_path: Path, monkeypatch
) -> None:
    def unexpected_roll(_expression: str):
        raise AssertionError("RNG must not be consumed before revision validation")

    monkeypatch.setattr(server_module, "roll", unexpected_roll)
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
            {"name": "Revision first", "edition": "2014", "idempotency_key": "campaign"},
        )
        await _call(
            server,
            "campaign_change",
            {
                "campaign_id": campaign["id"],
                "action": "clock_set",
                "payload": {"day": 1},
                "expected_revision": campaign["revision"],
                "idempotency_key": "clock",
            },
        )
        sheet = default_character_sheet()
        sheet["combat"]["hp"] = {"value": 0, "max": 12, "temp": 0}
        sheet["conditions"] = ["stable", "unconscious"]
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Stable stale actor",
                    "sheet": sheet,
                },
                "idempotency_key": "actor",
            },
        )
        current_campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )

        with pytest.raises(Exception, match="character revision conflict"):
            await _call(
                server,
                "campaign_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "stable_recovery",
                    "payload": {
                        "members": [
                            {
                                "character_id": actor["id"],
                                "expected_revision": actor["revision"] + 1,
                            }
                        ]
                    },
                    "expected_revision": current_campaign["revision"],
                    "idempotency_key": "recover",
                },
            )

    asyncio.run(exercise())


def test_recovered_actor_can_stand_through_restricted_state_action(tmp_path: Path) -> None:
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
            {"name": "Stand", "edition": "2014", "idempotency_key": "campaign"},
        )
        sheet = default_character_sheet()
        sheet["combat"]["hp"] = {"value": 1, "max": 12, "temp": 0}
        sheet["conditions"] = ["prone"]
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Prone", "sheet": sheet},
                "idempotency_key": "actor",
            },
        )

        stood = await _call(
            server,
            "character_state_change",
            {
                "character_id": actor["id"],
                "action": "stand",
                "payload": {},
                "expected_revision": actor["revision"],
                "idempotency_key": "stand",
            },
        )

        assert stood["status"] == "stood"
        assert stood["character"]["sheet"]["conditions"] == []
        receipts = await _call(
            server,
            "campaign_rules",
            {
                "campaign_id": campaign["id"],
                "action": "receipts",
                "payload": {"mechanic_id": "dnd5e.core.movement.prone_crawl_stand"},
            },
        )
        assert len(receipts) == 1
        assert receipts[0]["event"] == "character.stand"

    asyncio.run(exercise())
