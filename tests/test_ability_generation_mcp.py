import asyncio
from pathlib import Path

import pytest
import sagasmith_dnd.ability_generation as ability_module
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
    workspace = Path(__file__).resolve().parents[2]
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=workspace / "SagaSmith-dnd-skills",
        modulegen_skills_dir=workspace / "SagaSmith-module-gen-skills",
        auto_seed_rules=True,
    )


def test_rolled_ability_generation_is_two_phase_engine_owned_and_idempotent(
    tmp_path: Path, monkeypatch
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Ability rolls", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Unassigned Hero",
                    "sheet": default_character_sheet(),
                },
                "idempotency_key": "actor",
            },
        )
        roll_arguments = {
            "character_id": actor["id"],
            "method": "roll_4d6_drop_lowest",
            "expected_revision": actor["revision"],
            "idempotency_key": "roll-scores",
        }

        pending = await _call(server, "character_ability_apply", roll_arguments)

        def unexpected_roll(*_args, **_kwargs):
            raise AssertionError("recorded ability rolls must not be regenerated")

        monkeypatch.setattr(ability_module, "roll_ability_scores", unexpected_roll)
        replay = await _call(server, "character_ability_apply", roll_arguments)
        assert replay == pending
        assert pending["status"] == "pending_choice"
        assert len(pending["rolls"]) == 6
        assert (
            pending["character"]["sheet"]["ability_generation"]["method"]
            == "roll_4d6_drop_lowest_pending"
        )

        with pytest.raises(Exception, match="already been generated|pending"):
            await _call(
                server,
                "character_ability_apply",
                {
                    **roll_arguments,
                    "expected_revision": pending["character"]["revision"],
                    "idempotency_key": "reroll-scores",
                },
            )

        scores = sorted(item["score"] for item in pending["rolls"])
        assignments = dict(
            zip(
                ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"),
                scores,
                strict=True,
            )
        )
        completed = await _call(
            server,
            "character_ability_apply",
            {
                "character_id": actor["id"],
                "method": "roll_4d6_drop_lowest",
                "assignments": assignments,
                "expected_revision": pending["character"]["revision"],
                "idempotency_key": "assign-scores",
            },
        )
        assert completed["status"] == "committed"
        assert completed["character"]["sheet"]["ability_generation"]["rolls"] == pending["rolls"]
        assert completed["character"]["sheet"]["abilities"]["strength"]["score"] == scores[0]

    asyncio.run(exercise())


def test_character_build_rejects_silently_ignored_catalog_shortcuts(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Strict character build", "edition": "2014", "idempotency_key": "campaign"},
        )

        with pytest.raises(
            Exception,
            match=(
                "unsupported fields: background_id, class_id, species_id; "
                "bootstrap .* character_ability_apply .* character_content_apply"
            ),
        ):
            await _call(
                server,
                "character_create_from",
                {
                    "mode": "build",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "name": "Not Yet Built",
                        "class_id": "fighter",
                        "species_id": "human",
                        "background_id": "soldier",
                    },
                    "idempotency_key": "actor",
                },
            )

    asyncio.run(exercise())


def test_bundled_2014_class_catalog_can_complete_a_bootstrap_actor(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Catalog character build", "edition": "2014", "idempotency_key": "campaign"},
        )
        built = await _call(
            server,
            "character_create_from",
            {
                "mode": "build",
                "payload": {"campaign_id": campaign["id"], "name": "Catalog Fighter"},
                "idempotency_key": "actor",
            },
        )
        actor = built["instance"]
        scored = await _call(
            server,
            "character_ability_apply",
            {
                "character_id": actor["id"],
                "method": "standard_array",
                "assignments": {
                    "strength": 15,
                    "dexterity": 14,
                    "constitution": 13,
                    "intelligence": 12,
                    "wisdom": 10,
                    "charisma": 8,
                },
                "expected_revision": actor["revision"],
                "idempotency_key": "scores",
            },
        )
        catalog = await _call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {"campaign_id": campaign["id"], "kind": "class", "query": "Fighter"},
            },
        )
        fighter = next(item for item in catalog if item["name"] == "Fighter")
        assert fighter["application_state"] == "selection_ready"
        assert fighter["selection_requirements"] == {
            "fields": ["skills"],
            "skill_choice_count": 2,
            "skill_options": [
                "acrobatics",
                "athletics",
                "history",
                "insight",
                "intimidation",
                "perception",
            ],
            "tool_choice_count": 0,
            "tool_options": [],
        }

        applied = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": fighter["id"],
                "selection": {"skills": ["athletics", "perception"]},
                "expected_revision": scored["character"]["revision"],
                "idempotency_key": "fighter",
            },
        )

        assert "sheet" in applied, applied
        sheet = applied["sheet"]
        assert sheet["progression"]["classes"] == [
            {"name": "Fighter", "level": 1, "subclass": "", "hit_die": 10}
        ]
        assert sheet["combat"]["hit_dice"]["d10"] == {
            "label": "d10",
            "value": 1,
            "max": 1,
            "recovers_on": "long_rest",
            "source_key": "Fighter",
            "slot_level": 0,
        }
        assert sheet["skills"]["athletics"]["proficiency"] == "proficient"
        assert sheet["content"]["selections"][0]["artifact_id"] == fighter["id"]

        item_catalog = await _call(
            server,
            "character_query",
            {
                "view": "catalog",
                "payload": {
                    "campaign_id": campaign["id"],
                    "kind": "item",
                    "query": "Chain mail",
                },
            },
        )
        chain_mail = next(item for item in item_catalog if item["name"] == "Chain mail")
        assert chain_mail["application_state"] == "selection_ready"
        equipped = await _call(
            server,
            "character_content_apply",
            {
                "character_id": actor["id"],
                "artifact_id": chain_mail["id"],
                "selection": {},
                "expected_revision": applied["revision"],
                "idempotency_key": "chain-mail",
            },
        )
        inventory = equipped["sheet"]["inventory"]["items"]
        assert len(inventory) == 1
        assert inventory[0]["name"] == "Chain mail"
        assert inventory[0]["kind"] == "armor"
        assert inventory[0]["weight_oz"] == 880
        assert inventory[0]["price_cp"] == 7500
        assert inventory[0]["source_key"] == "dnd5e.content.srd2014.item.chain-mail"
        assert inventory[0]["mechanics"]["base_ac"] == 16
        assert inventory[0]["mechanics"]["dexterity_mode"] == "none"
        assert inventory[0]["mechanics"]["stealth_disadvantage"] is True
        assert equipped["sheet"]["content"]["selections"][-1]["artifact_id"] == chain_mail["id"]

    asyncio.run(exercise())


def test_ability_roll_rejects_stale_revision_and_caller_roll_payload(
    tmp_path: Path, monkeypatch
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Ability safety", "edition": "2014", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Safe Hero",
                    "sheet": default_character_sheet(),
                },
                "idempotency_key": "actor",
            },
        )
        manual_actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Manual Hero",
                    "sheet": default_character_sheet(),
                },
                "idempotency_key": "manual-actor",
            },
        )
        manual_scores = {
            "strength": 18,
            "dexterity": 14,
            "constitution": 16,
            "intelligence": 10,
            "wisdom": 12,
            "charisma": 8,
        }
        manual = await _call(
            server,
            "character_ability_apply",
            {
                "character_id": manual_actor["id"],
                "method": "manual",
                "assignments": manual_scores,
                "expected_revision": manual_actor["revision"],
                "idempotency_key": "manual-scores",
            },
        )
        assert manual["status"] == "committed"
        assert manual["character"]["sheet"]["ability_generation"]["method"] == "manual"
        assert manual["character"]["sheet"]["ability_generation"]["rolls"] == []

        def unexpected_roll(*_args, **_kwargs):
            raise AssertionError("ability RNG must follow revision validation")

        monkeypatch.setattr(ability_module, "roll_ability_scores", unexpected_roll)
        with pytest.raises(Exception, match="character revision conflict"):
            await _call(
                server,
                "character_ability_apply",
                {
                    "character_id": actor["id"],
                    "method": "roll_4d6_drop_lowest",
                    "expected_revision": actor["revision"] + 1,
                    "idempotency_key": "stale-roll",
                },
            )
        with pytest.raises(Exception, match="rolls|unexpected"):
            await _call(
                server,
                "character_ability_apply",
                {
                    "character_id": actor["id"],
                    "method": "roll_4d6_drop_lowest",
                    "rolls": [18, 18, 18, 18, 18, 18],
                    "expected_revision": actor["revision"],
                    "idempotency_key": "forged-roll",
                },
            )

    asyncio.run(exercise())


def test_low_level_ability_roll_uses_campaign_edition_authority(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Edition authority", "edition": "2014", "idempotency_key": "campaign"},
        )

        with pytest.raises(Exception, match="must match the campaign rule profile"):
            await _call(
                server,
                "dnd_ability_roll",
                {
                    "campaign_id": campaign["id"],
                    "edition": "2024",
                    "expected_campaign_revision": campaign["revision"],
                    "idempotency_key": "wrong-edition",
                },
            )

        rolled = await _call(
            server,
            "dnd_ability_roll",
            {
                "campaign_id": campaign["id"],
                "expected_campaign_revision": campaign["revision"],
                "idempotency_key": "profile-edition",
            },
        )
        assert rolled["ruleset"] == "dnd5e-2014"

    asyncio.run(exercise())
