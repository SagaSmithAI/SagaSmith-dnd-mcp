import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from tests.authoring_helpers import finalize_and_activate_module

NARRATIVE_MODULE = """# Part 2: Phandalin

## TOWN DESCRIPTION

### ALDERLEAF FARM

Qelline Alderleaf is a pragmatic halfling farmer and a kind host.
Her son Carp found a secret tunnel in the woods near Tresendar Manor.
Carp can take the characters to the tunnel or provide directions.
Two townsfolk wait by the gate.
Renaer—the son of Lord Dagult Neverember—waits in hiding.
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
        "module_draft",
        {
            "campaign_id": campaign["id"],
            "action": "start",
            "payload": {
                "name": "phandalin.md",
                "content": NARRATIVE_MODULE,
                "source_key": "phandalin",
                "title": "Phandalin",
            },
            "idempotency_key": "stage",
        },
    )
    activation = await finalize_and_activate_module(
        _call,
        server,
        campaign["id"],
        staged,
        source_key="phandalin",
        title="Phandalin",
        portable_id="dnd5e.module.phandalin-test",
    )
    module_id = activation["activated"]["activation"]["module_id"]
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
        "module_id": module_id,
        "scene_id": expanded["scene"]["id"],
        "chunk_id": expanded["chunk_id"],
        "page_start": expanded["page_start"],
        "page_end": expanded["page_end"],
        "heading_path": expanded["heading_path"],
        "content_sha256": hashlib.sha256(expanded["content"].encode("utf-8")).hexdigest(),
    }
    return server, campaign["id"], source_ref


def test_narrative_npc_is_source_bound_and_explicitly_noncombat(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server, campaign_id, source_ref = await _campaign_with_narrative_module(tmp_path)
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
            "source_excerpt": ("Qelline Alderleaf is a pragmatic halfling farmer and a kind host."),
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
        await _call(
            server,
            "game_phase",
            {
                "campaign_id": campaign_id,
                "action": "set",
                "tool_profile": "play",
                "expected_revision": campaign["revision"],
                "idempotency_key": "enter-play",
            },
        )
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
                    "action": "check",
                    "payload": {
                        "actor_id": created["character"]["id"],
                        "kind": "ability",
                        "ability": "wisdom",
                        "dc": 10,
                    },
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
                    "positioning_mode": "agent",
                    "campaign_id": campaign_id,
                    "participant_ids": [created["character"]["id"]],
                    "expected_revision": campaign["revision"],
                    "branch_id": branch_id,
                    "idempotency_key": "narrative-combat",
                },
            )
        with pytest.raises(Exception, match="only available during lobby"):
            await _call(
                server,
                "character_create_from",
                {
                    **arguments,
                    "idempotency_key": "narrative-qelline-during-play",
                },
            )

    asyncio.run(exercise())


def test_narrative_npc_rejects_unverifiable_identity_and_source(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server, campaign_id, source_ref = await _campaign_with_narrative_module(tmp_path)
        payload = {
            "campaign_id": campaign_id,
            "name": "Invented Stranger",
            "role": "Unsupported identity.",
            "summary": "This actor is not in the cited source.",
            "source_ref": source_ref,
            "source_excerpt": ("Qelline Alderleaf is a pragmatic halfling farmer and a kind host."),
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


def test_narrative_npc_reports_missing_and_unsupported_request_fields(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server, _campaign_id, _source_ref = await _campaign_with_narrative_module(tmp_path)
        with pytest.raises(
            Exception,
            match=(
                "narrative NPC payload has missing fields: campaign_id, role, summary; "
                "unsupported fields: occurrence_id, tags"
            ),
        ):
            await _call(
                server,
                "character_create_from",
                {
                    "mode": "narrative_npc",
                    "payload": {
                        "name": "Qelline Alderleaf",
                        "source_ref": {},
                        "source_excerpt": "Qelline Alderleaf is a pragmatic farmer.",
                        "occurrence_id": "wrong-layer",
                        "tags": ["narrative_only"],
                    },
                    "idempotency_key": "incomplete-narrative-npc",
                },
            )
    asyncio.run(exercise())


def test_narrative_npc_supports_distinct_anonymous_source_instances(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server, campaign_id, source_ref = await _campaign_with_narrative_module(tmp_path)
        created = []
        for index in (1, 2):
            instance_key = f"retreat-{index}"
            created.append(
                await _call(
                    server,
                    "character_create_from",
                    {
                        "mode": "narrative_npc",
                        "payload": {
                            "campaign_id": campaign_id,
                            "name": f"Townsfolk [{instance_key}]",
                            "source_identity": "Townsfolk",
                            "instance_key": instance_key,
                            "role": "Anonymous source-counted townsperson.",
                            "summary": "A separately tracked anonymous townsperson.",
                            "source_ref": source_ref,
                            "source_excerpt": "Two townsfolk wait by the gate.",
                        },
                        "idempotency_key": f"townsfolk-{index}",
                    },
                )
            )

        assert created[0]["character"]["id"] != created[1]["character"]["id"]
        assert [item["character"]["name"] for item in created] == [
            "Townsfolk [retreat-1]",
            "Townsfolk [retreat-2]",
        ]
        for index, item in enumerate(created, start=1):
            assert item["narrative_npc"]["source_identity"] == "Townsfolk"
            assert item["narrative_npc"]["instance_key"] == f"retreat-{index}"
            assert (
                "anonymous_source_instance"
                in item["character"]["sheet"]["adventure_state"]["status_tags"]
            )

        with pytest.raises(Exception, match="anonymous narrative NPC name"):
            await _call(
                server,
                "character_create_from",
                {
                    "mode": "narrative_npc",
                    "payload": {
                        "campaign_id": campaign_id,
                        "name": "Invented Mayor",
                        "source_identity": "Townsfolk",
                        "instance_key": "retreat-3",
                        "role": "Unsupported proper name.",
                        "summary": "Must not relabel an anonymous source identity.",
                        "source_ref": source_ref,
                        "source_excerpt": "Two townsfolk wait by the gate.",
                    },
                    "idempotency_key": "invented-anonymous-name",
                },
            )

    asyncio.run(exercise())


def test_narrative_npc_accepts_agent_named_source_instance(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server, campaign_id, source_ref = await _campaign_with_narrative_module(tmp_path)
        ruling = {
            "default_resolver": "agent",
            "ruling_kind": "agent_dm_adjudication",
            "decision": (
                "Track the first source-authored townsperson as Caldan Voss so "
                "their independent knowledge remains addressable."
            ),
            "reason": (
                "The source establishes two anonymous townsfolk but supplies no individual names."
            ),
            "assigned_name": "Caldan Voss",
            "source_identity": "Townsfolk",
            "instance_key": "gate-1",
            "committed": True,
        }
        created = await _call(
            server,
            "character_create_from",
            {
                "mode": "narrative_npc",
                "payload": {
                    "campaign_id": campaign_id,
                    "name": "Caldan Voss",
                    "source_identity": "Townsfolk",
                    "instance_key": "gate-1",
                    "identity_agent_ruling": ruling,
                    "role": "Source-authored anonymous townsperson.",
                    "summary": "A separately tracked witness at the gate.",
                    "source_ref": source_ref,
                    "source_excerpt": "Two townsfolk wait by the gate.",
                },
                "idempotency_key": "agent-named-townsperson",
            },
        )

        assert created["character"]["name"] == "Caldan Voss"
        assert created["narrative_npc"]["source_identity"] == "Townsfolk"
        assert created["narrative_npc"]["instance_key"] == "gate-1"
        assert created["narrative_npc"]["identity_agent_ruling"] == ruling
        assert created["character"]["sheet"]["adventure_state"]["status_tags"] == [
            "narrative_only",
            "source_bound",
            "anonymous_source_instance",
            "agent_named_source_instance",
        ]

        mismatched = {
            **ruling,
            "assigned_name": "Invented Mayor",
        }
        with pytest.raises(Exception, match="assigned_name"):
            await _call(
                server,
                "character_create_from",
                {
                    "mode": "narrative_npc",
                    "payload": {
                        "campaign_id": campaign_id,
                        "name": "Caldan Voss",
                        "source_identity": "Townsfolk",
                        "instance_key": "gate-2",
                        "identity_agent_ruling": mismatched,
                        "role": "Unsupported identity mismatch.",
                        "summary": "The ruling must bind the assigned name.",
                        "source_ref": source_ref,
                        "source_excerpt": "Two townsfolk wait by the gate.",
                    },
                    "idempotency_key": "agent-name-mismatch",
                },
            )

    asyncio.run(exercise())


def test_narrative_npc_accepts_two_part_name_split_by_source_appositive(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server, campaign_id, source_ref = await _campaign_with_narrative_module(tmp_path)
        created = await _call(
            server,
            "character_create_from",
            {
                "mode": "narrative_npc",
                "payload": {
                    "campaign_id": campaign_id,
                    "name": "Renaer Neverember",
                    "role": "Hidden noble and witness.",
                    "summary": "Renaer waits in hiding after an attack.",
                    "source_ref": source_ref,
                    "source_excerpt": ("Renaer—the son of Lord Dagult Neverember—waits in hiding."),
                },
                "idempotency_key": "narrative-renaer",
            },
        )

        assert created["character"]["name"] == "Renaer Neverember"
        assert created["narrative_npc"]["combat_eligible"] is False

    asyncio.run(exercise())
