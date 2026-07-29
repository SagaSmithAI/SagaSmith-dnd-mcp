from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    value = result.get("result", result) if isinstance(result, dict) else result
    if isinstance(value, dict) and "action" in value and "result" in value:
        return value["result"]
    return value


def test_source_condition_is_validated_persisted_and_cleared_with_encounter(
    tmp_path: Path,
) -> None:
    module_root = tmp_path / "modules"
    module_root.mkdir()
    source = module_root / "hideout.md"
    source.write_text(
        "# Redbrand Hideout\n\n"
        "## 10. Common Room\n\n"
        "Four Redbrand ruffians are drinking and playing knucklebones. "
        "All four are heavily drunk and poisoned. One ruffian is wearing a "
        "knucklebones sack over his head and is blinded while it remains in place.\n",
        encoding="utf-8",
    )
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        module_import_roots=(module_root,),
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Source encounter condition",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        staged = await _call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "stage",
                "payload": {
                    "source_path": str(source),
                    "source_key": "hideout",
                    "title": "Hideout",
                },
                "idempotency_key": "stage",
            },
        )
        job_id = staged["job"]["id"]
        for action in ("inspect", "validate"):
            await _call(
                server,
                "module_import",
                {
                    "campaign_id": campaign["id"],
                    "action": action,
                    "payload": {"job_id": job_id},
                    "idempotency_key": action,
                },
            )
        await _call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "ingest",
                "payload": {"job_id": job_id},
                "idempotency_key": "ingest",
            },
        )
        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        await _call(
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
        search = await _call(
            server,
            "module_search",
            {
                "campaign_id": campaign["id"],
                "query": "heavily drunk poisoned",
                "top_k": 3,
            },
        )
        expanded = await _call(
            server,
            "module_expand",
            {"chunk_id": search[0]["id"]},
        )
        ruffian = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Ruffian",
                "character_type": "monster",
                "idempotency_key": "ruffian",
            },
        )
        hero = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Hero",
                "character_type": "pc",
                "idempotency_key": "hero",
            },
        )
        late_ruffian = await _call(
            server,
            "character_create",
            {
                "campaign_id": campaign["id"],
                "name": "Late Ruffian",
                "character_type": "monster",
                "idempotency_key": "late-ruffian",
            },
        )
        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        play = await _call(
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
        source_condition = {
            "condition": "poisoned",
            "duration": "encounter",
            "source_ref": expanded["source_ref"],
            "source_excerpt": "All four are heavily drunk and poisoned.",
        }
        removable_condition = {
            "condition": "blinded",
            "duration": "encounter",
            "source_ref": expanded["source_ref"],
            "source_excerpt": (
                "One ruffian is wearing a knucklebones sack over his head and "
                "is blinded while it remains in place."
            ),
        }
        invalid_condition = {
            **source_condition,
            "source_ref": {
                **source_condition["source_ref"],
                "content_sha256": "0" * 64,
            },
        }
        start_arguments = {
            "campaign_id": campaign["id"],
            "participant_ids": [ruffian["id"], hero["id"]],
            "participant_config": [
                {
                    "actor_id": ruffian["id"],
                    "initiative": 20,
                    "disposition": "hostile",
                    "source_conditions": [source_condition, removable_condition],
                },
                {
                    "actor_id": hero["id"],
                    "initiative": 10,
                    "disposition": "friendly",
                },
            ],
            "scene_id": expanded["scene"]["id"],
            "ruleset": "2014",
            "expected_revision": play["campaign_revision"],
            "idempotency_key": "start",
        }
        with pytest.raises(Exception, match="authoritative campaign rule profile"):
            await _call(
                server,
                "combat_start",
                {
                    **start_arguments,
                    "ruleset": "2024",
                    "idempotency_key": "wrong-ruleset",
                },
            )
        with pytest.raises(Exception, match="content_sha256 does not match"):
            await _call(
                server,
                "combat_start",
                {
                    **start_arguments,
                    "participant_config": [
                        {
                            **start_arguments["participant_config"][0],
                            "source_conditions": [invalid_condition],
                        },
                        start_arguments["participant_config"][1],
                    ],
                    "idempotency_key": "invalid-start",
                },
            )

        started = await _call(server, "combat_start", start_arguments)
        ruffian_combatant = next(
            item
            for item in started["combat"]["combatants"]
            if item["actor_id"] == ruffian["id"]
        )
        assert set(ruffian_combatant["conditions"]) == {"blinded", "poisoned"}
        assert started["combat"]["source_conditions"][0]["source_ref"] == (
            expanded["source_ref"]
        )
        during = await _call(
            server,
            "character_get",
            {"character_id": ruffian["id"]},
        )
        assert set(during["sheet"]["conditions"]) == {"blinded", "poisoned"}
        interacted = await _call(
            server,
            "combat_common_action",
            {
                "campaign_id": campaign["id"],
                "actor_id": ruffian["id"],
                "action": "interact_object",
                "payload": {
                    "object_description": "a knucklebones sack worn over his head",
                    "interaction": "remove",
                    "remove_source_condition": "blinded",
                    "source_ref": expanded["source_ref"],
                    "source_excerpt": removable_condition["source_excerpt"],
                    "agent_ruling": {
                        "default_resolver": "agent",
                        "ruling_kind": "agent_dm_adjudication",
                        "decision": (
                            "The ruffian removes the ordinary sack from his own head "
                            "during his turn."
                        ),
                        "reason": (
                            "The source makes Blinded conditional on the removable "
                            "sack remaining over the ruffian's head."
                        ),
                    },
                },
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "remove-sack",
            },
        )
        interacted_combatant = next(
            item
            for item in interacted["combat"]["combatants"]
            if item["actor_id"] == ruffian["id"]
        )
        assert interacted["condition_resolution"]["removed"] is True
        assert interacted_combatant["turn_budget"]["object_interaction"] == 0
        assert interacted_combatant["turn_budget"]["main_action"] == 1
        ended_source_condition = next(
            item
            for item in interacted["combat"]["source_conditions"]
            if item["condition"] == "blinded"
        )
        assert ended_source_condition["active"] is False
        assert ended_source_condition["ended_reason"] == "agent_object_interaction"
        after_interaction = await _call(
            server,
            "character_get",
            {"character_id": ruffian["id"]},
        )
        assert after_interaction["sheet"]["conditions"] == ["poisoned"]
        joined = await _call(
            server,
            "combat_join",
            {
                "campaign_id": campaign["id"],
                "actor_id": late_ruffian["id"],
                "participant_config": {
                    "initiative": 5,
                    "disposition": "hostile",
                    "source_conditions": [source_condition],
                },
                "expected_revision": interacted["campaign_revision"],
                "idempotency_key": "join-late-ruffian",
            },
        )
        assert joined["queued"]["conditions"] == ["poisoned"]
        assert {
            item["actor_id"] for item in joined["combat"]["source_conditions"]
        } == {ruffian["id"], late_ruffian["id"]}
        late_during = await _call(
            server,
            "character_get",
            {"character_id": late_ruffian["id"]},
        )
        assert late_during["sheet"]["conditions"] == ["poisoned"]
        preflight = await _call(
            server,
            "combat_preflight_attack",
            {
                "campaign_id": campaign["id"],
                "actor_id": ruffian["id"],
                "target_id": hero["id"],
                "action": {
                    "weapon_id": "unarmed-strike",
                    "attack_mode": "melee",
                },
            },
        )
        assert preflight["disadvantage"] is True

        ended = await _call(
            server,
            "combat_end",
            {
                "campaign_id": campaign["id"],
                "outcome": {
                    "status": "interrupted",
                    "summary": "The encounter-scoped condition cleanup was verified.",
                },
                "expected_revision": joined["campaign_revision"],
                "idempotency_key": "end",
            },
        )
        after = await _call(
            server,
            "character_get",
            {"character_id": ruffian["id"]},
        )
        assert ended["ended"] is True
        assert after["sheet"]["conditions"] == []
        late_after = await _call(
            server,
            "character_get",
            {"character_id": late_ruffian["id"]},
        )
        assert late_after["sheet"]["conditions"] == []

    asyncio.run(exercise())
