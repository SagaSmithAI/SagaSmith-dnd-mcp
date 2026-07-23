import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server

NARRATIVE_MODULE = """# Part 2: Phandalin

## TOWN DESCRIPTION

### ALDERLEAF FARM

Qelline Alderleaf is a pragmatic halfling farmer and a kind host.
Her son Carp found a secret tunnel in the woods near Tresendar Manor.
Carp can take the characters to the tunnel or provide directions.
"""


async def _call(server, name: str, arguments: dict):
    called = await server.call_tool(name, arguments)
    if isinstance(called, tuple):
        _, result = called
        return result.get("result", result) if isinstance(result, dict) else result
    return called


async def _campaign_with_narrative_module(tmp_path: Path):
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=True,
    )
    server = create_server(config)
    campaign = await _call(
        server,
        "campaign_create",
        {
            "name": "Narrative NPC",
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
                "name": "phandalin.md",
                "content": NARRATIVE_MODULE,
                "source_key": "phandalin",
                "title": "Phandalin",
            },
            "idempotency_key": "stage",
        },
    )
    job_id = staged["job"]["id"]
    ingested = None
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
            "expected_revision": campaign["revision"],
            "idempotency_key": "activate",
        },
    )
    hits = await _call(
        server,
        "module_search",
        {
            "campaign_id": campaign["id"],
            "query": "Qelline Alderleaf Carp secret tunnel",
            "top_k": 5,
        },
    )
    expanded = await _call(server, "module_expand", {"chunk_id": hits[0]["id"]})
    source_ref = {
        "module_id": ingested["module_id"],
        "scene_id": expanded["scene"]["id"],
        "chunk_id": expanded["chunk_id"],
        "page_start": expanded["page_start"] or 1,
        "page_end": expanded["page_end"] or expanded["page_start"] or 1,
        "heading_path": expanded["heading_path"],
        "content_sha256": hashlib.sha256(
            expanded["content"].encode("utf-8")
        ).hexdigest(),
    }
    return server, campaign["id"], source_ref


def test_narrative_npc_is_source_bound_and_explicitly_noncombat(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server, campaign_id, source_ref = await _campaign_with_narrative_module(
            tmp_path
        )
        arguments = {
            "mode": "narrative_npc",
            "payload": {
                "campaign_id": campaign_id,
                "name": "Qelline Alderleaf",
                "role": "Pragmatic farmer and source of local guidance.",
                "summary": "Qelline hosts the party and can point them toward Carp.",
                "source_ref": source_ref,
                "source_excerpt": (
                    "Qelline Alderleaf is a pragmatic halfling farmer and a kind host."
                ),
            },
            "idempotency_key": "narrative-qelline",
        }
        created = await _call(server, "character_create_from", arguments)
        replay = await _call(server, "character_create_from", arguments)

        assert replay == created
        assert created["character"]["character_type"] == "npc"
        assert created["character"]["sheet"]["adventure_state"]["status_tags"] == [
            "narrative_only",
            "source_bound",
        ]
        assert created["character"]["sheet"]["content"] == {
            "spells": [],
            "features": [],
            "feats": [],
            "activities": [],
            "selections": [],
        }
        assert created["narrative_npc"] == {
            "kind": "source_bound_narrative_npc",
            "role": "Pragmatic farmer and source of local guidance.",
            "combat_statblock": "not_imported",
            "source_ref": source_ref,
            "source_excerpt": (
                "Qelline Alderleaf is a pragmatic halfling farmer and a kind host."
            ),
            "combat_eligible": False,
        }
        evidence_prefix = "sagasmith:narrative-npc-source:"
        dm_notes = created["character"]["notes"]["profile"]["dm_notes"]
        assert dm_notes.startswith(evidence_prefix)
        evidence = json.loads(dm_notes.removeprefix(evidence_prefix))
        assert evidence["source_ref"] == source_ref
        assert evidence["combat_statblock"] == "not_imported"

        campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign_id}},
        )
        branches = await _call(
            server,
            "branch_query",
            {"campaign_id": campaign_id, "view": "list"},
        )
        branch_id = next(item["id"] for item in branches if item["is_current"])
        with pytest.raises(Exception, match="cannot make checks"):
            await _call(
                server,
                "character_check",
                {
                    "campaign_id": campaign_id,
                    "actor_id": created["character"]["id"],
                    "kind": "ability",
                    "ability": "wisdom",
                    "dc": 10,
                    "expected_revision": campaign["revision"],
                    "branch_id": branch_id,
                    "idempotency_key": "narrative-check",
                },
            )
        with pytest.raises(Exception, match="cannot enter combat"):
            await _call(
                server,
                "combat_start",
                {
                    "campaign_id": campaign_id,
                    "participant_ids": [created["character"]["id"]],
                    "expected_revision": campaign["revision"],
                    "branch_id": branch_id,
                    "idempotency_key": "narrative-combat",
                },
            )

    asyncio.run(exercise())


def test_narrative_npc_rejects_unverifiable_identity_and_source(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server, campaign_id, source_ref = await _campaign_with_narrative_module(
            tmp_path
        )
        payload = {
            "campaign_id": campaign_id,
            "name": "Invented Stranger",
            "role": "Unsupported identity.",
            "summary": "This actor is not in the cited source.",
            "source_ref": source_ref,
            "source_excerpt": (
                "Qelline Alderleaf is a pragmatic halfling farmer and a kind host."
            ),
        }
        with pytest.raises(Exception, match="name is not present"):
            await _call(
                server,
                "character_create_from",
                {
                    "mode": "narrative_npc",
                    "payload": payload,
                    "idempotency_key": "invented",
                },
            )
        payload["name"] = "Qelline Alderleaf"
        payload["source_ref"] = {**source_ref, "content_sha256": "0" * 64}
        with pytest.raises(Exception, match="content_sha256"):
            await _call(
                server,
                "character_create_from",
                {
                    "mode": "narrative_npc",
                    "payload": payload,
                    "idempotency_key": "bad-hash",
                },
            )

    asyncio.run(exercise())
