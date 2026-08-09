import asyncio
import json
from importlib.resources import files
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.npc_turns import (
    normalize_npc_turn_proposal,
    validate_npc_targets,
)
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
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


async def _campaign_with_npc(server):
    campaign = await _call(
        server,
        "campaign_create",
        {"name": "Isolated NPC turns", "idempotency_key": "campaign"},
    )
    npc = await _call(
        server,
        "character_create_from",
        {
            "mode": "direct",
            "payload": {
                "campaign_id": campaign["id"],
                "name": "Zaltember",
                "character_type": "npc",
                "summary": "A wary fire giant child who values survival and family.",
            },
            "principal_id": "system:local",
            "idempotency_key": "npc",
        },
    )
    pc = await _call(
        server,
        "character_create_from",
        {
            "mode": "direct",
            "payload": {"campaign_id": campaign["id"], "name": "Envoy"},
            "principal_id": "system:local",
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
            "idempotency_key": "start-play",
        },
    )
    return campaign, npc, pc


def test_npc_turn_bundle_is_actor_scoped_and_commits_only_accepted_deltas(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign, npc, pc = await _campaign_with_npc(server)
        await _call(
            server,
            "memory_change",
            {
                "campaign_id": campaign["id"],
                "action": "upsert",
                "payload": {
                    "fact_key": f"actor.relationship:{npc['id']}:party",
                    "kind": "actor_state",
                    "subject_ref": f"actor:{npc['id']}",
                    "predicate": "relationship_to",
                    "content": "Zaltember distrusts the party but wants to survive.",
                    "metadata": {"target_ref": "party:main", "trust": -4},
                    "disclosure_scope": "dm",
                },
                "idempotency_key": "relationship",
            },
        )
        await _call(
            server,
            "memory_change",
            {
                "campaign_id": campaign["id"],
                "action": "upsert",
                "payload": {
                    "fact_key": "world:family-secret",
                    "kind": "world",
                    "subject_ref": "faction:fire-giants",
                    "predicate": "secret",
                    "content": "The family hides a secret vault below the forge.",
                    "disclosure_scope": "public",
                },
                "idempotency_key": "public-world-fact",
            },
        )
        bundle = await _call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "purpose": "npc_turn",
                "actor_id": npc["id"],
                "interlocutor_actor_ids": [pc["id"]],
                "stimulus": {
                    "kind": "speech",
                    "speaker_actor_id": pc["id"],
                    "target_actor_ids": [npc["id"]],
                    "content": "Tell us who you are.",
                    "language": "Common",
                },
                "query": "identity survival family",
            },
        )
        bundle_schema = json.loads(
            files("sagasmith_dnd_mcp")
            .joinpath("contracts")
            .joinpath("npc-turn-bundle.v2.schema.json")
            .read_text(encoding="utf-8")
        )
        Draft202012Validator(bundle_schema).validate(bundle)

        assert bundle["purpose"] == "npc_turn"
        assert bundle["schema_version"] == 2
        assert bundle["conversation"]["campaign_id"] == campaign["id"]
        assert bundle["conversation"]["participants"][0]["actor_id"] == npc["id"]
        assert bundle["delegation"]["contract"] == "sagasmith.delegation.v1"
        assert bundle["delegation"]["tools_exposed"] is False
        assert "principal_id" not in bundle["bundle_receipt"]
        assert len(bundle["bundle_receipt"]["principal_fingerprint"]) == 64
        assert bundle["actor"]["id"] == npc["id"]
        assert bundle["interlocutors"] == [
            {"id": pc["id"], "name": "Envoy", "character_type": "pc"}
        ]
        assert bundle["relationships"][0]["predicate"] == "relationship_to"
        assert bundle["perception"][0]["kind"] == "interlocutor_presence"
        assert bundle["constraints"]["may_call_tools"] is False
        assert bundle["constraints"]["module_evidence_is_actor_knowledge"] is False
        assert bundle["constraints"]["common_context_is_actor_knowledge"] is False
        identity_ref = f"actor:{npc['id']}:identity"
        assert identity_ref in bundle["constraints"]["allowed_basis_refs"]
        relationship_ref = (
            f"fact:{bundle['relationships'][0]['id']}:{bundle['relationships'][0]['revision_id']}"
        )
        assert relationship_ref in bundle["constraints"]["allowed_basis_refs"]
        assert bundle["perception"][0]["basis_ref"] in bundle["constraints"]["allowed_basis_refs"]
        assert bundle["common_context"]
        assert not {
            f"fact:{item['id']}:{item['revision_id']}" for item in bundle["common_context"]
        } & set(bundle["constraints"]["allowed_basis_refs"])

        proposal = {
            "schema_version": 1,
            "bundle_id": bundle["bundle_id"],
            "speaker_actor_id": npc["id"],
            "intent": {
                "kind": "negotiate",
                "summary": "Use his identity to make captivity safer.",
            },
            "utterance": {
                "text": "Keep me alive. I am Duke Zalto's son.",
                "language": "Common",
                "delivery": "frightened but defiant",
            },
            "speech_acts": [
                {
                    "kind": "assert",
                    "content": "He claims to be Duke Zalto's son.",
                    "truth_posture": "believes_true",
                    "basis_refs": [identity_ref],
                    "targets": [pc["id"]],
                }
            ],
            "proposed_action": {"kind": "none", "target_ref": "", "summary": ""},
            "resolution_requests": [],
            "proposed_deltas": {
                "facts": [],
                "actor_knowledge": [
                    {
                        "actor_id": pc["id"],
                        "knowledge_key": "zaltember-identity-claim",
                        "proposition": "Zaltember claims to be Duke Zalto's son.",
                        "epistemic_status": "rumor",
                        "confidence": 2,
                        "cause": f"told_by:{npc['id']}",
                        "disclosure_scope": "owner",
                    }
                ],
            },
            "portrayal": {
                "emotion": "afraid",
                "visible_cues": ["tries to hide his fear"],
            },
            "decision_summary": "He has no safe escape and reveals leverage.",
        }
        commit_arguments = {
            "campaign_id": campaign["id"],
            "action": "commit",
            "payload": {
                "event": {
                    "summary": "Zaltember identifies himself to the envoy.",
                    "audience_scope": "actor",
                },
                "npc_turn": {
                    "bundle_receipt": bundle["bundle_receipt"],
                    "proposal": proposal,
                    "accepted_fact_indexes": [],
                    "accepted_actor_knowledge_indexes": [0],
                    "accepted_action": False,
                    "isolation_level": "isolated",
                },
            },
            "idempotency_key": "npc-turn",
        }
        committed = await _call(server, "memory_change", commit_arguments)
        replay = await _call(server, "memory_change", commit_arguments)

        assert replay == committed
        assert committed["event"]["event_type"] == "npc_dialogue_turn"
        assert committed["event"]["payload"]["utterance"].startswith("Keep me alive")
        assert {item["role"] for item in committed["event"]["participants"]} == {
            "speaker",
            "listener",
        }
        assert committed["actor_knowledge"][0]["epistemic_status"] == "rumor"
        assert "basis_refs" not in committed["event"]["payload"]
        assert committed["event"]["payload"]["public_speech_acts"] == [
            {
                "kind": "assert",
                "content": "He claims to be Duke Zalto's son.",
                "targets": [pc["id"]],
            }
        ]
        assert committed["event"]["payload"]["visible_portrayal_cues"] == [
            "tries to hide his fear"
        ]
        assert "truth_posture" not in committed["event"]["payload"]["public_speech_acts"][0]
        next_bundle = await _call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "purpose": "npc_turn",
                "actor_id": npc["id"],
                "interlocutor_actor_ids": [pc["id"]],
            },
        )
        assert next_bundle["conversation"]["cursor"]["event_count"] == 1
        assert next_bundle["conversation"]["events"][0]["public_speech_acts"] == [
            committed["event"]["payload"]["public_speech_acts"][0]
        ]
        context = await _call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "actor_id": pc["id"],
                "audience": "player",
                "query": "Zalto son",
            },
        )
        assert [item["id"] for item in context["events"]] == [committed["event"]["id"]]
        assert context["actor_knowledge"][0]["epistemic_status"] == "rumor"

    asyncio.run(exercise())


def test_npc_turn_bundle_rejects_privilege_leaks_tampering_and_stale_commits(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign, npc, pc = await _campaign_with_npc(server)
        bundle = await _call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "purpose": "npc_turn",
                "actor_id": npc["id"],
                "interlocutor_actor_ids": [pc["id"]],
                "stimulus": {"kind": "scene_prompt", "content": "The NPC is addressed."},
            },
        )
        base_proposal = {
            "schema_version": 1,
            "bundle_id": bundle["bundle_id"],
            "speaker_actor_id": npc["id"],
            "intent": {"kind": "refuse", "summary": "Avoid answering."},
            "utterance": {"text": "No.", "language": "Common", "delivery": "flat"},
            "speech_acts": [],
            "proposed_action": {"kind": "none", "target_ref": "", "summary": ""},
            "resolution_requests": [],
            "proposed_deltas": {"facts": [], "actor_knowledge": []},
            "portrayal": {"emotion": "guarded", "visible_cues": []},
            "decision_summary": "He refuses.",
        }
        tampered = {
            **base_proposal,
            "speech_acts": [
                {
                    "kind": "assert",
                    "content": "An unsupported secret.",
                    "truth_posture": "believes_true",
                    "basis_refs": ["knowledge:someone-else:secret"],
                    "targets": [pc["id"]],
                }
            ],
        }
        with pytest.raises(Exception, match="outside its bundle"):
            await _call(
                server,
                "memory_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "commit",
                    "payload": {
                        "event": {"summary": "Must fail."},
                        "npc_turn": {
                            "bundle_receipt": bundle["bundle_receipt"],
                            "proposal": tampered,
                        },
                    },
                    "idempotency_key": "tampered",
                },
            )
        wrong_target = {
            **base_proposal,
            "speech_acts": [
                {
                    "kind": "assert",
                    "content": "He identifies himself.",
                    "truth_posture": "believes_true",
                    "basis_refs": [f"actor:{npc['id']}:identity"],
                    "targets": ["actor-from-another-conversation"],
                }
            ],
        }
        with pytest.raises(Exception, match="targets outside its bundle"):
            await _call(
                server,
                "memory_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "commit",
                    "payload": {
                        "event": {"summary": "Must also fail."},
                        "npc_turn": {
                            "bundle_receipt": bundle["bundle_receipt"],
                            "proposal": wrong_target,
                        },
                    },
                    "idempotency_key": "wrong-target",
                },
            )
        with pytest.raises(Exception, match="accepted_action must be boolean"):
            await _call(
                server,
                "memory_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "commit",
                    "payload": {
                        "event": {"summary": "Must reject coercion."},
                        "npc_turn": {
                            "bundle_receipt": bundle["bundle_receipt"],
                            "proposal": base_proposal,
                            "accepted_action": "false",
                        },
                    },
                    "idempotency_key": "non-boolean-action",
                },
            )
        await _call(
            server,
            "memory_change",
            {
                "campaign_id": campaign["id"],
                "action": "upsert",
                "payload": {
                    "fact_key": f"actor.relationship:{npc['id']}:party",
                    "kind": "actor_state",
                    "subject_ref": f"actor:{npc['id']}",
                    "predicate": "relationship_to",
                    "content": "The NPC becomes wary of the party.",
                    "metadata": {"target_ref": "party:main"},
                    "disclosure_scope": "dm",
                },
                "idempotency_key": "advance-actor-state",
            },
        )
        with pytest.raises(Exception, match="stale at an actor-state fact"):
            await _call(
                server,
                "memory_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "commit",
                    "payload": {
                        "event": {"summary": "Stale actor-state proposal."},
                        "npc_turn": {
                            "bundle_receipt": bundle["bundle_receipt"],
                            "proposal": base_proposal,
                        },
                    },
                    "idempotency_key": "stale-actor-state",
                },
            )
        bundle = await _call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "purpose": "npc_turn",
                "actor_id": npc["id"],
                "interlocutor_actor_ids": [pc["id"]],
                "stimulus": {"kind": "scene_prompt", "content": "The NPC is addressed."},
            },
        )
        base_proposal = {
            **base_proposal,
            "bundle_id": bundle["bundle_id"],
        }
        await _call(
            server,
            "memory_change",
            {
                "campaign_id": campaign["id"],
                "action": "commit",
                "payload": {"event": {"summary": "Another event advances continuity."}},
                "idempotency_key": "advance",
            },
        )
        with pytest.raises(Exception, match="stale after a continuity event"):
            await _call(
                server,
                "memory_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "commit",
                    "payload": {
                        "event": {"summary": "Stale NPC turn."},
                        "npc_turn": {
                            "bundle_receipt": bundle["bundle_receipt"],
                            "proposal": base_proposal,
                        },
                    },
                    "idempotency_key": "stale",
                },
            )
        await _call(
            server,
            "access_grant",
            {
                "scope": "campaign",
                "campaign_id": campaign["id"],
                "principal_id": "player:untrusted",
                "payload": {"role": "player"},
            },
        )
        with pytest.raises(Exception, match="Owner/DM"):
            await _call(
                server,
                "continuity_context",
                {
                    "campaign_id": campaign["id"],
                    "purpose": "npc_turn",
                    "actor_id": npc["id"],
                    "principal_id": "player:untrusted",
                },
            )

    asyncio.run(exercise())


def test_npc_turn_is_live_phase_only_and_contract_schemas_ship(tmp_path: Path) -> None:
    bundle_schema = json.loads(
        files("sagasmith_dnd_mcp")
        .joinpath("contracts")
        .joinpath("npc-turn-bundle.v2.schema.json")
        .read_text(encoding="utf-8")
    )
    proposal_schema = json.loads(
        files("sagasmith_dnd_mcp")
        .joinpath("contracts")
        .joinpath("npc-turn-proposal.v1.schema.json")
        .read_text(encoding="utf-8")
    )
    assert bundle_schema["properties"]["purpose"]["const"] == "npc_turn"
    assert (
        bundle_schema["properties"]["constraints"]["properties"]["may_call_tools"]["const"] is False
    )
    assert proposal_schema["additionalProperties"] is False
    assert proposal_schema["properties"]["proposed_action"]["properties"]["kind"]["enum"] == [
        "none",
        "gesture",
        "offer",
        "refuse",
        "surrender",
        "move",
        "flee",
        "attack",
        "use_item",
        "exchange_item",
        "scene_transition",
        "observe",
        "interact",
        "follow",
        "wait",
        "other",
    ]
    Draft202012Validator.check_schema(proposal_schema)

    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Lobby portrayal rejected", "idempotency_key": "campaign"},
        )
        npc = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Waiting NPC",
                    "character_type": "npc",
                },
                "principal_id": "system:local",
                "idempotency_key": "npc",
            },
        )
        with pytest.raises(Exception, match="only during Play or Combat"):
            await _call(
                server,
                "continuity_context",
                {
                    "campaign_id": campaign["id"],
                    "purpose": "npc_turn",
                    "actor_id": npc["id"],
                },
            )

    asyncio.run(exercise())


def test_mechanical_npc_proposals_must_request_public_resolution() -> None:
    proposal = {
        "schema_version": 1,
        "bundle_id": "bundle",
        "speaker_actor_id": "npc",
        "intent": {"kind": "attack", "summary": "Strike the intruder."},
        "utterance": {"text": "", "language": "", "delivery": ""},
        "speech_acts": [],
        "proposed_action": {
            "kind": "attack",
            "target_ref": "actor:pc",
            "summary": "Attack the intruder.",
        },
        "resolution_requests": [],
        "proposed_deltas": {"facts": [], "actor_knowledge": []},
        "portrayal": {"emotion": "angry", "visible_cues": []},
        "decision_summary": "The NPC chooses violence.",
    }

    with pytest.raises(ValueError, match="requires an explicit resolution request"):
        normalize_npc_turn_proposal(proposal)

    proposal_schema = json.loads(
        files("sagasmith_dnd_mcp")
        .joinpath("contracts")
        .joinpath("npc-turn-proposal.v1.schema.json")
        .read_text(encoding="utf-8")
    )
    schema_errors = list(Draft202012Validator(proposal_schema).iter_errors(proposal))
    assert any(error.validator == "minItems" for error in schema_errors)

    invalid_version = {**proposal, "schema_version": True}
    with pytest.raises(ValueError, match="schema_version must be 1"):
        normalize_npc_turn_proposal(invalid_version)

    proposal["resolution_requests"] = [
        {
            "kind": "attack",
            "reason": "Resolve attack and action economy through the combat engine.",
            "actor_ids": ["npc", "pc"],
            "suggested_skill": "",
        }
    ]
    normalized = normalize_npc_turn_proposal(proposal)
    assert normalized["proposed_action"]["kind"] == "attack"
    validate_npc_targets(normalized, allowed_actor_ids={"npc", "pc"})

    normalized["resolution_requests"][0]["actor_ids"] = ["npc", "outsider"]
    with pytest.raises(ValueError, match="targets outside its bundle"):
        validate_npc_targets(normalized, allowed_actor_ids={"npc", "pc"})

    normalized["resolution_requests"][0]["actor_ids"] = ["npc", "pc"]
    normalized["proposed_action"]["target_ref"] = "actor:outsider"
    with pytest.raises(ValueError, match="action target is outside its bundle"):
        validate_npc_targets(normalized, allowed_actor_ids={"npc", "pc"})

    narrative = {
        **proposal,
        "proposed_action": {
            "kind": "move",
            "target_ref": "actor:pc",
            "summary": "Steps back toward the doorway.",
        },
        "resolution_requests": [],
    }
    assert normalize_npc_turn_proposal(narrative)["proposed_action"]["kind"] == "move"
