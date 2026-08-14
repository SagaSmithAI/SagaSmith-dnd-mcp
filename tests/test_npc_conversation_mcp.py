import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from sagasmith_dnd_mcp.tool_profiles import HOST_PRIVATE_TOOLS, policy_for_tool

HOST_TOKEN = "test-host-token-with-sufficient-entropy"


def _config(tmp_path: Path) -> McpConfig:
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
        npc_host_token=HOST_TOKEN,
    )


async def _call(server, name: str, arguments: dict):
    called = await server.call_tool(name, arguments)
    if isinstance(called, tuple):
        _, result = called
        return result.get("result", result) if isinstance(result, dict) else result
    return called


async def _campaign_with_actors(server):
    campaign = await _call(
        server, "campaign_create", {"name": "NPC", "idempotency_key": "campaign"}
    )
    npc = await _call(
        server,
        "character_create_from",
        {
            "mode": "direct",
            "payload": {
                "campaign_id": campaign["id"],
                "name": "Mara",
                "character_type": "npc",
                "summary": "Guarded.",
            },
            "idempotency_key": "npc",
        },
    )
    pc = await _call(
        server,
        "character_create_from",
        {
            "mode": "direct",
            "payload": {"campaign_id": campaign["id"], "name": "Aria"},
            "idempotency_key": "pc",
        },
    )
    current = await _call(
        server, "campaign_query", {"view": "get", "payload": {"campaign_id": campaign["id"]}}
    )
    await _call(
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
    return campaign, npc, pc


def _audience(decision_id, *, perceived, understood, response):
    return {
        "decision_id": decision_id,
        "resolver": "agent",
        "perceived_actor_ids": perceived,
        "understood_actor_ids": understood,
        "response_actor_ids": response,
        "partial_renditions": {},
        "basis_refs": ["scene:current"],
        "reason": "Agent resolved scene range, occlusion, delivery, and language.",
    }


def test_public_surface_is_one_facade_and_host_transport_is_unloadable() -> None:
    assert policy_for_tool("npc_conversation").phases == frozenset({"play"})
    assert HOST_PRIVATE_TOOLS == frozenset({"npc_conversation_transport"})


def test_open_errors_explain_the_single_participant_array(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign, _npc, pc = await _campaign_with_actors(server)
        with pytest.raises(Exception, match="every PC and NPC campaign runtime id"):
            await _call(
                server,
                "npc_conversation",
                {
                    "campaign_id": campaign["id"],
                    "action": "open",
                    "payload": {"npc_actor_ids": [pc["id"]]},
                },
            )
        with pytest.raises(Exception, match="payload.idempotency_key"):
            await _call(
                server,
                "npc_conversation",
                {
                    "campaign_id": campaign["id"],
                    "action": "open",
                    "payload": {"participant_actor_ids": [pc["id"]]},
                },
            )
        with pytest.raises(Exception, match="inside payload.participant_actor_ids"):
            await _call(
                server,
                "npc_conversation",
                {
                    "campaign_id": campaign["id"],
                    "action": "open",
                    "payload": {
                        "participant_actor_ids": [pc["id"]],
                        "idempotency_key": "pc-only",
                    },
                },
            )

    asyncio.run(exercise())


def test_active_conversation_list_exposes_only_public_recovery_handles(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign, npc, pc = await _campaign_with_actors(server)
        opened = await _call(
            server,
            "npc_conversation",
            {
                "campaign_id": campaign["id"],
                "action": "open",
                "payload": {
                    "participant_actor_ids": [pc["id"], npc["id"]],
                    "idempotency_key": "open-for-list",
                },
            },
        )

        listed = await _call(
            server,
            "npc_conversation",
            {
                "campaign_id": campaign["id"],
                "action": "list",
                "payload": {},
            },
        )
        assert listed["count"] == 1
        assert listed["conversations"][0]["conversation_id"] == opened["conversation_id"]
        assert listed["conversations"][0]["conversation_revision"] == 0
        assert "actor_contexts" not in str(listed)
        assert "private" not in str(listed)

        await _call(
            server,
            "npc_conversation",
            {
                "campaign_id": campaign["id"],
                "action": "abort",
                "payload": {
                    "conversation_id": opened["conversation_id"],
                    "expected_conversation_revision": 0,
                    "idempotency_key": "abort-listed",
                },
            },
        )
        listed = await _call(
            server,
            "npc_conversation",
            {
                "campaign_id": campaign["id"],
                "action": "list",
                "payload": {},
            },
        )
        assert listed["conversations"] == []

    asyncio.run(exercise())


def test_stdio_restart_lists_and_aborts_active_conversation(tmp_path: Path) -> None:
    async def exercise() -> None:
        principal_id = "discord:npc-restart"
        env = dict(os.environ)
        env.update(
            {
                "SAGASMITH_DND_MCP_HOME": str(tmp_path / "home"),
                "SAGASMITH_DND_MCP_AUTO_SEED": "0",
            }
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "sagasmith_dnd_mcp.server"],
            cwd=Path(__file__).parents[1],
            env=env,
        )

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.call_tool(
                    "exposure",
                    {"action": "open", "principal_id": principal_id},
                )
                await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": ["campaign_create"],
                        "principal_id": principal_id,
                    },
                )
                created = await session.call_tool(
                    "campaign_create",
                    {"name": "NPC restart", "idempotency_key": "create"},
                )
                campaign_id = json.loads(created.content[0].text)["id"]
                await session.call_tool(
                    "exposure",
                    {
                        "action": "open",
                        "campaign_id": campaign_id,
                        "principal_id": principal_id,
                    },
                )
                await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": ["character_create_from"],
                        "principal_id": principal_id,
                    },
                )
                npc_result = await session.call_tool(
                    "character_create_from",
                    {
                        "mode": "direct",
                        "payload": {
                            "campaign_id": campaign_id,
                            "name": "Mara",
                            "character_type": "npc",
                        },
                        "idempotency_key": "npc",
                    },
                )
                pc_result = await session.call_tool(
                    "character_create_from",
                    {
                        "mode": "direct",
                        "payload": {"campaign_id": campaign_id, "name": "Aria"},
                        "idempotency_key": "pc",
                    },
                )
                npc_id = json.loads(npc_result.content[0].text)["result"]["id"]
                pc_id = json.loads(pc_result.content[0].text)["result"]["id"]
                current = await session.call_tool(
                    "campaign_query",
                    {"view": "get", "payload": {"campaign_id": campaign_id}},
                )
                revision = json.loads(current.content[0].text)["result"]["revision"]
                entered = await session.call_tool(
                    "game_phase",
                    {
                        "campaign_id": campaign_id,
                        "action": "set",
                        "tool_profile": "play",
                        "expected_revision": revision,
                        "idempotency_key": "play",
                    },
                )
                assert not entered.isError
                await session.list_tools()
                await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": ["npc_conversation"],
                        "principal_id": principal_id,
                    },
                )
                opened = await session.call_tool(
                    "npc_conversation",
                    {
                        "campaign_id": campaign_id,
                        "action": "open",
                        "payload": {
                            "participant_actor_ids": [pc_id, npc_id],
                            "idempotency_key": "open",
                        },
                    },
                )
                assert not opened.isError
                conversation_id = json.loads(opened.content[0].text)["conversation_id"]

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.call_tool(
                    "exposure",
                    {
                        "action": "open",
                        "campaign_id": campaign_id,
                        "principal_id": principal_id,
                    },
                )
                await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": ["npc_conversation"],
                        "principal_id": principal_id,
                    },
                )
                listed = await session.call_tool(
                    "npc_conversation",
                    {"campaign_id": campaign_id, "action": "list", "payload": {}},
                )
                assert not listed.isError
                recovery = json.loads(listed.content[0].text)
                assert recovery["count"] == 1
                assert recovery["conversations"][0]["conversation_id"] == conversation_id
                assert "actor_contexts" not in str(recovery)
                aborted = await session.call_tool(
                    "npc_conversation",
                    {
                        "campaign_id": campaign_id,
                        "action": "abort",
                        "payload": {
                            "conversation_id": conversation_id,
                            "expected_conversation_revision": 0,
                            "idempotency_key": "abort-after-restart",
                        },
                    },
                )
                assert not aborted.isError
                listed = await session.call_tool(
                    "npc_conversation",
                    {"campaign_id": campaign_id, "action": "list", "payload": {}},
                )
                assert json.loads(listed.content[0].text)["conversations"] == []

    asyncio.run(exercise())


def test_active_conversation_blocks_combat_and_leaving_play(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign, npc, pc = await _campaign_with_actors(server)
        opened = await _call(
            server,
            "npc_conversation",
            {
                "campaign_id": campaign["id"],
                "action": "open",
                "payload": {
                    "participant_actor_ids": [pc["id"], npc["id"]],
                    "idempotency_key": "open",
                },
            },
        )
        await _call(
            server,
            "campaign_event",
            {
                "campaign_id": campaign["id"],
                "action": "add",
                "payload": {
                    "summary": "An unrelated clocktower bell rings elsewhere.",
                    "event_type": "ambient",
                    "audience_scope": "dm",
                },
                "idempotency_key": "unrelated-play-event",
            },
        )
        still_open = await _call(
            server,
            "npc_conversation",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"conversation_id": opened["conversation_id"]},
            },
        )
        assert still_open["status"] == "open"
        current = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        current_branch = next(
            item
            for item in await _call(
                server,
                "branch_query",
                {"campaign_id": campaign["id"], "view": "list", "payload": {}},
            )
            if item["is_current"]
        )

        with pytest.raises(Exception, match="before resolving an authoritative character check"):
            await _call(
                server,
                "character_check",
                {
                    "campaign_id": campaign["id"],
                    "action": "check",
                    "payload": {
                        "actor_id": pc["id"],
                        "kind": "ability",
                        "ability": "dexterity",
                        "dc": 10,
                    },
                    "expected_revision": current["revision"],
                    "idempotency_key": "check-with-open-conversation",
                },
            )

        with pytest.raises(Exception, match="close or abort the active NPC conversation"):
            await _call(
                server,
                "combat_start",
                {
                    "campaign_id": campaign["id"],
                    "participant_ids": [pc["id"], npc["id"]],
                    "positioning_mode": "agent",
                    "expected_revision": current["revision"],
                    "idempotency_key": "combat-with-open-conversation",
                },
            )
        with pytest.raises(Exception, match="close or abort the active NPC conversation"):
            await _call(
                server,
                "game_phase",
                {
                    "campaign_id": campaign["id"],
                    "action": "set",
                    "tool_profile": "lobby",
                    "expected_revision": current["revision"],
                    "idempotency_key": "lobby-with-open-conversation",
                },
            )
        with pytest.raises(Exception, match="close or abort the active NPC conversation"):
            await _call(
                server,
                "chase",
                {
                    "campaign_id": campaign["id"],
                    "action": "start",
                    "payload": {
                        "participant_ids": [pc["id"], npc["id"]],
                        "quarry_ids": [npc["id"]],
                        "initial_distance_ft": 30,
                        "scene_id": "blocked-before-source-resolution",
                        "source_ref": {},
                        "source_excerpt": "blocked",
                    },
                    "expected_revision": current["revision"],
                    "idempotency_key": "chase-with-open-conversation",
                },
            )
        with pytest.raises(Exception, match="before creating and checking out a branch"):
            await _call(
                server,
                "branch_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "create",
                    "payload": {"name": "blocked", "checkout": True},
                    "expected_revision": current["revision"],
                    "expected_branch_id": current_branch["id"],
                    "idempotency_key": "branch-with-open-conversation",
                },
            )

    asyncio.run(exercise())


def test_conversation_facade_private_transport_and_commit(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign, npc, pc = await _campaign_with_actors(server)
        opened = await _call(
            server,
            "npc_conversation",
            {
                "campaign_id": campaign["id"],
                "action": "open",
                "payload": {
                    "participant_actor_ids": [pc["id"], npc["id"]],
                    "query": "identity and goals",
                    "idempotency_key": "open",
                },
            },
        )
        assert opened["conversation_revision"] == 0
        assert "actor_knowledge" not in str(opened)
        conversation_id = opened["conversation_id"]
        ingested = await _call(
            server,
            "npc_conversation",
            {
                "campaign_id": campaign["id"],
                "action": "ingest",
                "payload": {
                    "conversation_id": conversation_id,
                    "event": {
                        "type": "speech",
                        "speaker_actor_id": pc["id"],
                        "content": "Were you at the docks?",
                        "language": "Common",
                        "declared_target_actor_ids": [npc["id"]],
                    },
                    "audience_facts": _audience(
                        "audience-1",
                        perceived=[pc["id"], npc["id"]],
                        understood=[pc["id"], npc["id"]],
                        response=[npc["id"]],
                    ),
                    "expected_conversation_revision": 0,
                    "idempotency_key": "ingest",
                },
            },
        )
        activation = ingested["activations"][0]
        assert set(activation) == {
            "activation_ref",
            "actor_id",
            "reason",
            "response_required",
            "from_cursor",
            "to_cursor",
            "status",
            "conversation_revision",
        }
        with pytest.raises(Exception, match="authentication"):
            await _call(
                server,
                "npc_conversation_transport",
                {
                    "campaign_id": campaign["id"],
                    "conversation_id": conversation_id,
                    "action": "claim_activation",
                    "host_token": "wrong",
                    "payload": {
                        "activation_ref": activation["activation_ref"],
                        "expected_conversation_revision": 1,
                        "idempotency_key": "claim",
                    },
                },
            )
        capsule = await _call(
            server,
            "npc_conversation_transport",
            {
                "campaign_id": campaign["id"],
                "conversation_id": conversation_id,
                "action": "claim_activation",
                "host_token": HOST_TOKEN,
                "payload": {
                    "activation_ref": activation["activation_ref"],
                    "expected_conversation_revision": 1,
                    "idempotency_key": "claim",
                    "cursor": 0,
                    "include_bootstrap": True,
                },
            },
        )
        identity_ref = f"actor:{npc['id']}:identity"
        proposal = {
            "schema_version": 4,
            "conversation_id": conversation_id,
            "activation_id": capsule["activation_id"],
            "actor_runtime_id": capsule["actor_runtime_id"],
            "response_bid": {"should_respond": True, "urgency": 80, "reason": "Addressed."},
            "private_intent": "Deflect.",
            "utterance_segments": [
                {
                    "text": "No. I stayed home.",
                    "speech_act": "deny",
                    "truth_posture": "intentional_deception",
                    "basis_refs": [identity_ref],
                    "targets": [pc["id"]],
                    "language": "Common",
                    "delivery": "flatly",
                }
            ],
            "proposed_action": {
                "summary": "",
                "target_refs": [],
                "settlement": "narrative",
                "mechanic_hint": "",
            },
            "resolution_requests": [
                {
                    "kind": "dm_adjudication",
                    "reason": "Determine whether Aria notices the evasive movement.",
                    "actor_ids": [npc["id"], pc["id"]],
                }
            ],
            "working_deltas": {
                "facts": [],
                "actor_knowledge": [
                    {
                        "action": "add",
                        "actor_id": npc["id"],
                        "knowledge_key": f"conversation:{conversation_id}:questioned",
                        "proposition": "Aria asked about the docks.",
                        "subject_ref": f"actor:{pc['id']}",
                        "epistemic_status": "belief",
                        "confidence": 3,
                        "cause": f"conversation:{conversation_id}",
                        "disclosure_scope": "dm",
                    }
                ],
                "commitments": [],
            },
            "visible_cues": ["Mara looks away."],
            "decision_summary": "Deny.",
        }
        submitted = await _call(
            server,
            "npc_conversation_transport",
            {
                "campaign_id": campaign["id"],
                "conversation_id": conversation_id,
                "action": "submit_proposal",
                "host_token": HOST_TOKEN,
                "payload": {
                    "activation_ref": activation["activation_ref"],
                    "lease_id": capsule["lease_id"],
                    "proposal": proposal,
                    "expected_conversation_revision": 2,
                    "idempotency_key": "submit",
                },
            },
        )
        assert submitted["status"] == "publication_ready"
        assert "private_intent" not in str(submitted["publication"])
        with pytest.raises(Exception, match="unpublished NPC output"):
            await _call(
                server,
                "npc_conversation",
                {
                    "campaign_id": campaign["id"],
                    "action": "close",
                    "payload": {
                        "conversation_id": conversation_id,
                        "expected_conversation_revision": 3,
                        "accepted_candidate_ids": [],
                        "idempotency_key": "close-before-publication",
                    },
                },
            )
        published = await _call(
            server,
            "npc_conversation",
            {
                "campaign_id": campaign["id"],
                "action": "publish",
                "payload": {
                    "conversation_id": conversation_id,
                    "publication_id": submitted["publication"]["publication_id"],
                    "audience_facts": _audience(
                        "audience-2",
                        perceived=[pc["id"], npc["id"]],
                        understood=[pc["id"], npc["id"]],
                        response=[],
                    ),
                    "expected_conversation_revision": 3,
                    "idempotency_key": "publish",
                },
            },
        )
        assert published["publication"]["speech"] == "No. I stayed home."
        with pytest.raises(Exception, match="unresolved mechanic requests"):
            await _call(
                server,
                "npc_conversation",
                {
                    "campaign_id": campaign["id"],
                    "action": "close",
                    "payload": {
                        "conversation_id": conversation_id,
                        "expected_conversation_revision": 4,
                        "accepted_candidate_ids": [],
                        "idempotency_key": "close-before-resolution",
                    },
                },
            )
        resolution_id = submitted["resolution_requests"][0]["resolution_id"]
        resolved = await _call(
            server,
            "npc_conversation",
            {
                "campaign_id": campaign["id"],
                "action": "ingest",
                "payload": {
                    "conversation_id": conversation_id,
                    "event": {
                        "type": "resolution",
                        "content": "Aria notices Mara edging toward the door.",
                        "resolved_resolution_ids": [resolution_id],
                    },
                    "audience_facts": _audience(
                        "audience-3",
                        perceived=[pc["id"], npc["id"]],
                        understood=[pc["id"], npc["id"]],
                        response=[],
                    ),
                    "expected_conversation_revision": 4,
                    "idempotency_key": "resolve",
                },
            },
        )
        assert resolved["event"]["resolved_resolution_ids"] == [resolution_id]
        status = await _call(
            server,
            "npc_conversation",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"conversation_id": conversation_id},
            },
        )
        accepted_candidate_ids = [
            item["candidate_id"]
            for item in status["memory_candidates"]
            if (
                item["actor_id"] == pc["id"]
                or item["value"].get("knowledge_key")
                == f"conversation:{conversation_id}:questioned"
            )
        ]
        assert len(accepted_candidate_ids) == 2
        committed = await _call(
            server,
            "npc_conversation",
            {
                "campaign_id": campaign["id"],
                "action": "close",
                "payload": {
                    "conversation_id": conversation_id,
                    "expected_conversation_revision": 5,
                    "accepted_candidate_ids": accepted_candidate_ids,
                    "idempotency_key": "close",
                },
            },
        )
        replayed = await _call(
            server,
            "npc_conversation",
            {
                "campaign_id": campaign["id"],
                "action": "close",
                "payload": {
                    "conversation_id": conversation_id,
                    "expected_conversation_revision": 5,
                    "accepted_candidate_ids": accepted_candidate_ids,
                    "idempotency_key": "close",
                },
            },
        )
        assert replayed == committed
        assert committed["event"]["event_type"] == "npc_conversation"
        assert committed["conversation_revision"] == 6
        assert committed["event"]["payload"]["unresolved_resolution_requests"] == []
        transcript = committed["event"]["payload"]["transcript"]
        assert all("audience_facts" in event for event in transcript)
        assert transcript[-1]["resolved_resolution_ids"] == [resolution_id]
        assert committed["event"]["retrieval_text"].endswith(
            "Scene: Aria notices Mara edging toward the door."
        )
        recalled = await _call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "actor_id": npc["id"],
                "purpose": "npc_turn",
                "query": "stayed home",
                "interlocutor_actor_ids": [pc["id"]],
            },
        )
        assert "No. I stayed home." in [
            item["utterance"]
            for item in recalled["conversation"]["events"]
            if item["event_type"] == "npc_conversation_turn"
        ]
        heard = await _call(
            server,
            "actor_knowledge_query",
            {
                "campaign_id": campaign["id"],
                "actor_id": pc["id"],
                "view": "list",
                "payload": {},
            },
        )
        assert [item["proposition"] for item in heard] == [f"{npc['id']} said: No. I stayed home."]

    asyncio.run(exercise())


def test_unrelated_campaign_event_does_not_stale_conversation(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign, npc, pc = await _campaign_with_actors(server)
        opened = await _call(
            server,
            "npc_conversation",
            {
                "campaign_id": campaign["id"],
                "action": "open",
                "payload": {
                    "participant_actor_ids": [pc["id"], npc["id"]],
                    "idempotency_key": "open",
                },
            },
        )
        await _call(
            server,
            "campaign_event",
            {
                "campaign_id": campaign["id"],
                "action": "add",
                "payload": {
                    "event_type": "world_change",
                    "summary": "A remote bell rings.",
                    "audience_scope": "public",
                },
                "idempotency_key": "bell",
            },
        )
        status = await _call(
            server,
            "npc_conversation",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"conversation_id": opened["conversation_id"]},
            },
        )
        assert status["status"] == "open"
        assert status["conversation_revision"] == 0

    asyncio.run(exercise())


def test_new_actor_knowledge_refreshes_the_open_actor_runtime(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign, npc, pc = await _campaign_with_actors(server)
        opened = await _call(
            server,
            "npc_conversation",
            {
                "campaign_id": campaign["id"],
                "action": "open",
                "payload": {
                    "participant_actor_ids": [pc["id"], npc["id"]],
                    "idempotency_key": "open",
                },
            },
        )
        await _call(
            server,
            "actor_knowledge_change",
            {
                "action": "add",
                "payload": {
                    "campaign_id": campaign["id"],
                    "actor_id": npc["id"],
                    "knowledge_key": "new-secret",
                    "proposition": "The duke is compromised.",
                    "subject_ref": "actor:duke",
                    "epistemic_status": "known",
                },
                "idempotency_key": "knowledge-add",
            },
        )
        status = await _call(
            server,
            "npc_conversation",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"conversation_id": opened["conversation_id"]},
            },
        )
        assert status["conversation_revision"] == 1
        assert status["refreshed_actor_ids"] == [npc["id"]]

    asyncio.run(exercise())


def test_selected_actor_knowledge_change_refreshes_only_that_runtime(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign, npc, pc = await _campaign_with_actors(server)
        knowledge = await _call(
            server,
            "actor_knowledge_change",
            {
                "action": "add",
                "payload": {
                    "campaign_id": campaign["id"],
                    "actor_id": npc["id"],
                    "knowledge_key": "dock-secret",
                    "proposition": "The ledger is under the pier.",
                    "subject_ref": "location:docks",
                    "epistemic_status": "known",
                },
                "idempotency_key": "knowledge-add",
            },
        )
        opened = await _call(
            server,
            "npc_conversation",
            {
                "campaign_id": campaign["id"],
                "action": "open",
                "payload": {
                    "participant_actor_ids": [pc["id"], npc["id"]],
                    "query": "dock secret",
                    "idempotency_key": "open",
                },
            },
        )
        await _call(
            server,
            "actor_knowledge_change",
            {
                "action": "revise",
                "payload": {
                    "knowledge_id": knowledge["id"],
                    "proposition": "The ledger was moved to the warehouse.",
                    "epistemic_status": "known",
                    "expected_revision_id": knowledge["revision_id"],
                },
                "idempotency_key": "knowledge-revise",
            },
        )
        status = await _call(
            server,
            "npc_conversation",
            {
                "campaign_id": campaign["id"],
                "action": "get",
                "payload": {"conversation_id": opened["conversation_id"]},
            },
        )
        assert status["status"] == "open"
        assert status["conversation_revision"] == 1
        assert status["refreshed_actor_ids"] == [npc["id"]]

    asyncio.run(exercise())
