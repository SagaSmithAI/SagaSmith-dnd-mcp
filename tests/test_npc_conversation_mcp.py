import asyncio
from pathlib import Path

import pytest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


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


async def _call(server, name: str, arguments: dict):
    called = await server.call_tool(name, arguments)
    if isinstance(called, tuple):
        _, result = called
        return result.get("result", result) if isinstance(result, dict) else result
    return called


async def _campaign_with_actors(server):
    campaign = await _call(
        server,
        "campaign_create",
        {"name": "NPC conversation", "idempotency_key": "campaign"},
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
                "summary": "A guarded dockworker.",
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
        server,
        "campaign_query",
        {"view": "get", "payload": {"campaign_id": campaign["id"]}},
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


def test_multi_turn_conversation_keeps_private_capsules_out_of_director_and_commits_once(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign, npc, pc = await _campaign_with_actors(server)
        opened = await _call(
            server,
            "conversation_open",
            {
                "campaign_id": campaign["id"],
                "participant_actor_ids": [pc["id"], npc["id"]],
                "query": "identity and current goals",
            },
        )
        assert opened["status"] == "open"
        assert "actor_knowledge" not in str(opened)
        conversation_id = opened["conversation_id"]

        ingested = await _call(
            server,
            "conversation_ingest",
            {
                "campaign_id": campaign["id"],
                "conversation_id": conversation_id,
                "event": {
                    "type": "speech",
                    "speaker_actor_id": pc["id"],
                    "content": "Were you at the docks last night?",
                    "language": "Common",
                    "delivery": "normal",
                    "declared_target_actor_ids": [npc["id"]],
                },
            },
        )
        activation = ingested["activations"][0]
        assert activation["actor_id"] == npc["id"]
        assert activation["response_required"] is True
        assert "context" not in activation
        with pytest.raises(Exception, match="unfinished NPC activations"):
            await _call(
                server,
                "conversation_close",
                {
                    "campaign_id": campaign["id"],
                    "conversation_id": conversation_id,
                    "idempotency_key": "premature-close",
                },
            )

        capsule = await _call(
            server,
            "npc_activation_checkout",
            {
                "campaign_id": campaign["id"],
                "conversation_id": conversation_id,
                "activation_id": activation["activation_id"],
                "worker_handle": activation["worker_handle"],
                "cursor": 0,
                "include_bootstrap": True,
            },
        )
        assert capsule["bootstrap"]["actor"]["id"] == npc["id"]
        assert capsule["bootstrap"]["delegation"]["persist_worker_session"] is True
        identity_ref = f"actor:{npc['id']}:identity"
        assert identity_ref in capsule["constraints"]["allowed_basis_refs"]

        proposal = {
            "schema_version": 2,
            "conversation_id": conversation_id,
            "activation_id": activation["activation_id"],
            "actor_runtime_id": activation["actor_runtime_id"],
            "response_bid": {
                "should_respond": True,
                "urgency": 80,
                "reason": "Aria addressed Mara directly.",
            },
            "private_intent": "Avoid further questions about the docks.",
            "utterance_segments": [
                {
                    "text": "No. I stayed home.",
                    "speech_act": "lie",
                    "truth_posture": "intentional_deception",
                    "basis_refs": [identity_ref],
                    "targets": [pc["id"]],
                    "language": "Common",
                    "delivery": "flatly",
                }
            ],
            "proposed_action": {"kind": "none", "target_ref": "", "summary": ""},
            "resolution_requests": [],
            "working_deltas": {
                "facts": [],
                "actor_knowledge": [
                    {
                        "action": "add",
                        "actor_id": pc["id"],
                        "knowledge_key": f"conversation:{conversation_id}:mara-denial",
                        "proposition": "Mara claimed that she stayed home.",
                        "subject_ref": f"actor:{npc['id']}",
                        "epistemic_status": "rumor",
                        "confidence": 2,
                        "cause": f"told_by:{npc['id']}",
                        "disclosure_scope": "owner",
                    }
                ],
                "commitments": [],
            },
            "visible_cues": ["Mara looks away before answering."],
            "decision_summary": "Deny the dock visit.",
        }
        submitted = await _call(
            server,
            "npc_activation_submit",
            {
                "campaign_id": campaign["id"],
                "conversation_id": conversation_id,
                "activation_id": activation["activation_id"],
                "worker_handle": activation["worker_handle"],
                "lease_id": capsule["lease_id"],
                "proposal": proposal,
            },
        )
        assert submitted["publication"]["speech"] == "No. I stayed home."
        assert "private_intent" not in str(submitted["publication"])
        assert "intentional_deception" not in str(submitted["publication"])

        committed = await _call(
            server,
            "conversation_close",
            {
                "campaign_id": campaign["id"],
                "conversation_id": conversation_id,
                "accepted_working_deltas": {
                    npc["id"]: {
                        "fact_indexes": [],
                        "actor_knowledge_indexes": [0],
                        "commitment_indexes": [],
                    }
                },
                "idempotency_key": "close-conversation",
            },
        )
        replay = await _call(
            server,
            "conversation_close",
            {
                "campaign_id": campaign["id"],
                "conversation_id": conversation_id,
                "accepted_working_deltas": {},
                "idempotency_key": "close-conversation",
            },
        )
        assert replay == committed
        assert committed["event"]["event_type"] == "npc_conversation"
        assert [item["type"] for item in committed["event"]["payload"]["transcript"]] == [
            "speech",
            "npc_publication",
        ]
        assert committed["actor_knowledge"][0]["epistemic_status"] == "rumor"
        status = await _call(
            server,
            "conversation_status",
            {"campaign_id": campaign["id"], "conversation_id": conversation_id},
        )
        assert status["status"] == "closed"

    asyncio.run(exercise())


def test_external_authority_change_marks_conversation_stale(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign, npc, pc = await _campaign_with_actors(server)
        opened = await _call(
            server,
            "conversation_open",
            {
                "campaign_id": campaign["id"],
                "participant_actor_ids": [pc["id"], npc["id"]],
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
                    "summary": "The city bell rings.",
                    "audience_scope": "public",
                },
                "idempotency_key": "bell",
            },
        )
        with pytest.raises(Exception, match="SESSION_STALE"):
            await _call(
                server,
                "conversation_ingest",
                {
                    "campaign_id": campaign["id"],
                    "conversation_id": opened["conversation_id"],
                    "event": {
                        "type": "speech",
                        "speaker_actor_id": pc["id"],
                        "content": "What was that?",
                    },
                },
            )
        status = await _call(
            server,
            "conversation_status",
            {
                "campaign_id": campaign["id"],
                "conversation_id": opened["conversation_id"],
            },
        )
        assert status["status"] == "stale"
        assert status["stale_reasons"]

    asyncio.run(exercise())
