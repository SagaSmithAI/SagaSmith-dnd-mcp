import asyncio
from pathlib import Path

import pytest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def _config(tmp_path: Path) -> McpConfig:
    dnd = tmp_path / "dnd"
    modulegen = tmp_path / "modulegen"
    (dnd / "full").mkdir(parents=True)
    modulegen.mkdir(parents=True)
    (dnd / "full" / "SKILL.md").write_text("# D&D Full\n", encoding="utf-8")
    (modulegen / "SKILL.md").write_text("# Module Generator\n", encoding="utf-8")
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=dnd,
        modulegen_skills_dir=modulegen,
        auto_seed_rules=False,
    )


def test_character_state_facade_has_no_second_actor_memory_authority(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        tool = next(
            item for item in await server.list_tools() if item.name == "character_state_change"
        )
        actions = tool.inputSchema["properties"]["action"]["enum"]
        assert "memory_add" not in actions
        assert "memory_resolve" not in actions
        assert {"actor_knowledge_query", "actor_knowledge_change"}.issubset(
            {item.name for item in await server.list_tools()}
        )
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "One knowledge authority", "idempotency_key": "campaign"},
        )
        with pytest.raises(Exception, match="unsupported fields: memories"):
            await _call(
                server,
                "character_create_from",
                {
                    "mode": "direct",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "name": "Invalid witness",
                        "notes": {
                            "memories": [
                                {
                                    "id": "invalid-memory",
                                    "summary": "Must use ActorKnowledge instead.",
                                },
                            ],
                        },
                    },
                    "idempotency_key": "invalid-actor",
                },
            )

    asyncio.run(exercise())


def test_memory_facade_supports_stable_upsert_revision_and_supersede(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Stable facts", "idempotency_key": "campaign"},
        )
        created = await _call(
            server,
            "memory_change",
            {
                "campaign_id": campaign["id"],
                "action": "upsert",
                "payload": {
                    "fact_key": "location:cellar:door-state",
                    "subject": "Cellar door",
                    "subject_ref": "location:cellar",
                    "predicate": "door-state",
                    "content": "The cellar door is locked.",
                    "importance": 4,
                    "disclosure_scope": "party",
                },
                "idempotency_key": "fact-create",
            },
        )
        assert created["fact_key"] == "location:cellar:door-state"

        with pytest.raises(Exception, match="expected_revision_id"):
            await _call(
                server,
                "memory_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "upsert",
                    "payload": {
                        "fact_key": "location:cellar:door-state",
                        "content": "An unsafe overwrite.",
                    },
                    "idempotency_key": "unsafe-upsert",
                },
            )

        revised = await _call(
            server,
            "memory_change",
            {
                "campaign_id": campaign["id"],
                "action": "upsert",
                "payload": {
                    "fact_key": "location:cellar:door-state",
                    "content": "The cellar door is open.",
                    "expected_revision_id": created["revision_id"],
                    "source_event_ids": ["event:door-opened"],
                },
                "idempotency_key": "fact-revise",
            },
        )
        assert revised["id"] == created["id"]
        assert revised["content"] == "The cellar door is open."
        assert revised["importance"] == 4
        assert revised["disclosure_scope"] == "party"

        with pytest.raises(Exception, match="fact_key identity conflict.*subject"):
            await _call(
                server,
                "memory_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "upsert",
                    "payload": {
                        "fact_key": "location:cellar:door-state",
                        "subject": "A different fact identity",
                        "content": "This write must be rejected.",
                        "expected_revision_id": revised["revision_id"],
                    },
                    "idempotency_key": "fact-identity-conflict",
                },
            )

        superseded = await _call(
            server,
            "memory_change",
            {
                "campaign_id": campaign["id"],
                "action": "supersede",
                "payload": {
                    "memory_id": created["id"],
                    "expected_revision_id": revised["revision_id"],
                },
                "idempotency_key": "fact-supersede",
            },
        )
        assert superseded["status"] == "superseded"
        assert (
            await _call(
                server,
                "memory_query",
                {"campaign_id": campaign["id"], "view": "list"},
            )
            == []
        )
        history = await _call(
            server,
            "memory_query",
            {
                "campaign_id": campaign["id"],
                "view": "list",
                "payload": {"include_inactive": True},
            },
        )
        assert [item["id"] for item in history] == [created["id"]]

    asyncio.run(exercise())


def test_memory_commit_is_atomic_idempotent_and_pins_skill_manifest(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Atomic scene", "idempotency_key": "campaign"},
        )
        actor = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {"campaign_id": campaign["id"], "name": "Witness"},
                "idempotency_key": "actor",
            },
        )
        arguments = {
            "campaign_id": campaign["id"],
            "action": "commit",
            "payload": {
                "event": {
                    "summary": "The witness hears the midnight bell.",
                    "audience_scope": "actor",
                },
                "facts": [
                    {
                        "fact_key": "world:midnight-bell:heard",
                        "content": "The midnight bell rang.",
                        "disclosure_scope": "party",
                    }
                ],
                "actor_knowledge": [
                    {
                        "actor_id": actor["id"],
                        "knowledge_key": "midnight-bell",
                        "proposition": "I heard the midnight bell.",
                        "disclosure_scope": "owner",
                    }
                ],
                "snapshot": {"label": "Midnight bell"},
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": "scene-commit",
        }
        committed = await _call(server, "memory_change", arguments)
        current_after_commit = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        replayed = await _call(
            server,
            "memory_change",
            {
                **arguments,
                "expected_revision": current_after_commit["revision"],
            },
        )

        assert replayed["event"]["id"] == committed["event"]["id"]
        assert committed["snapshot"] is not None
        assert len(committed["skill_manifest"]) == 2
        assert all(len(item["checksum"]) == 64 for item in committed["skill_manifest"])
        assert (
            committed["event"]["payload"]["_sagasmith_skill_manifest"]
            == (committed["skill_manifest"])
        )
        assert committed["facts"][0]["source_event_ids"] == [committed["event"]["id"]]
        assert committed["actor_knowledge"][0]["source_event_id"] == (committed["event"]["id"])
        context = await _call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "query": "midnight bell",
                "budget_chars": 1_000,
            },
        )
        assert context["retrieval"]["budget_chars"] == 1_000
        assert context["retrieval"]["strategy"] == ("lexical_structured_shared_budget_v2")
        diagnostics = await _call(
            server,
            "memory_query",
            {"campaign_id": campaign["id"], "view": "diagnostics"},
        )
        assert diagnostics["facts"]["active"] == 1
        assert diagnostics["actor_knowledge"]["active"] == 1
        assert diagnostics["skill_manifest"]["drift"] is False
        assert diagnostics["recap"]["source"] == "deterministic"

        before = await _call(
            server,
            "campaign_event",
            {"campaign_id": campaign["id"], "action": "list"},
        )
        with pytest.raises(Exception, match="live character"):
            await _call(
                server,
                "memory_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "commit",
                    "payload": {
                        "event": {"summary": "This unit must roll back."},
                        "facts": [{"fact_key": "rollback:test", "content": "Must roll back."}],
                        "actor_knowledge": [
                            {
                                "actor_id": "missing",
                                "knowledge_key": "invalid",
                                "proposition": "Must fail.",
                            }
                        ],
                    },
                    "idempotency_key": "rollback",
                },
            )
        after = await _call(
            server,
            "campaign_event",
            {"campaign_id": campaign["id"], "action": "list"},
        )
        assert [item["id"] for item in after] == [item["id"] for item in before]

    asyncio.run(exercise())
