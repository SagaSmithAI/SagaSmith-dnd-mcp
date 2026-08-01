import asyncio
import json
from importlib.resources import files
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from sagasmith_dnd_mcp.bounded_evaluations import normalize_bounded_proposal
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
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


async def _live_campaign(server):
    campaign = await _call(
        server,
        "campaign_create",
        {"name": "Bounded contexts", "idempotency_key": "campaign"},
    )
    npc = await _call(
        server,
        "character_create_from",
        {
            "mode": "direct",
            "payload": {
                "campaign_id": campaign["id"],
                "name": "Autonomous envoy",
                "character_type": "npc",
                "summary": "A cautious envoy.",
            },
            "idempotency_key": "npc",
        },
    )
    pc = await _call(
        server,
        "character_create_from",
        {
            "mode": "direct",
            "payload": {
                "campaign_id": campaign["id"],
                "name": "Human hero",
            },
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


def _actor_proposal(bundle: dict, actor_id: str) -> dict:
    return {
        "schema_version": 1,
        "bundle_id": bundle["bundle_id"],
        "purpose": "actor_turn",
        "actor_id": actor_id,
        "intent": "Keep the negotiation open.",
        "proposed_action": {"kind": "none", "target_ref": "", "summary": ""},
        "claims": [],
        "resolution_requests": [],
        "decision_summary": "The envoy listens without conceding.",
    }


def test_actor_audience_and_faction_bundles_validate_without_writing_state(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign, npc, pc = await _live_campaign(server)
        actor_bundle = await _call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "purpose": "actor_turn",
                "actor_id": npc["id"],
                "interlocutor_actor_ids": [pc["id"]],
                "stimulus": {
                    "kind": "speech",
                    "speaker_actor_id": pc["id"],
                    "target_actor_ids": [npc["id"]],
                    "content": "Will you negotiate?",
                },
            },
        )
        bundle_schema = json.loads(
            files("sagasmith_dnd_mcp")
            .joinpath("contracts")
            .joinpath("bounded-evaluation-bundle.v1.schema.json")
            .read_text(encoding="utf-8")
        )
        Draft202012Validator(bundle_schema).validate(actor_bundle)
        assert "principal_id" not in actor_bundle["bundle_receipt"]
        assert len(actor_bundle["bundle_receipt"]["principal_fingerprint"]) == 64
        assert actor_bundle["subject"] == {
            "kind": "actor",
            "id": npc["id"],
            "name": "Autonomous envoy",
        }
        assert actor_bundle["constraints"]["may_call_tools"] is False
        assert actor_bundle["constraints"]["may_write_state"] is False
        assert actor_bundle["constraints"]["output_contract"] == (
            "actor-turn-proposal.v1"
        )
        actor_proposal = _actor_proposal(actor_bundle, npc["id"])
        validated = await _call(
            server,
            "bounded_evaluation",
            {
                "campaign_id": campaign["id"],
                "action": "validate",
                "proposal": actor_proposal,
                "bundle_receipt": actor_bundle["bundle_receipt"],
            },
        )
        assert validated["validated"] is True
        assert validated["authoritative_state_changed"] is False
        assert validated["proposal"] == actor_proposal
        assert "principal_id" not in validated["validation_receipt"]
        assert len(validated["validation_receipt"]["principal_fingerprint"]) == 64

        with pytest.raises(Exception, match="human-owned PC"):
            await _call(
                server,
                "continuity_context",
                {
                    "campaign_id": campaign["id"],
                    "purpose": "actor_turn",
                    "actor_id": pc["id"],
                },
            )

        with pytest.raises(Exception, match="requires audience='player'"):
            await _call(
                server,
                "continuity_context",
                {
                    "campaign_id": campaign["id"],
                    "purpose": "audience_render",
                    "actor_id": pc["id"],
                },
            )

        with pytest.raises(Exception, match="has no faction_state"):
            await _call(
                server,
                "continuity_context",
                {
                    "campaign_id": campaign["id"],
                    "purpose": "faction_turn",
                    "subject_ref": "faction:unknown",
                    "query": "Act now.",
                },
            )

        await _call(
            server,
            "memory_change",
            {
                "campaign_id": campaign["id"],
                "action": "upsert",
                "payload": {
                    "fact_key": "faction:ember-court:goal",
                    "kind": "faction_state",
                    "subject_ref": "faction:ember-court",
                    "predicate": "goal",
                    "content": "Preserve the alliance.",
                    "disclosure_scope": "dm",
                },
                "idempotency_key": "faction-goal",
            },
        )
        faction_bundle = await _call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "purpose": "faction_turn",
                "subject_ref": "faction:ember-court",
                "evaluation_target_refs": [f"actor:{pc['id']}"],
                "query": "Respond to the hero's demand.",
            },
        )
        assert faction_bundle["subject"]["kind"] == "faction"
        assert {
            fact["subject_ref"] for fact in faction_bundle["context"]["facts"]
        } == {"faction:ember-court"}
        faction_proposal = {
            "schema_version": 1,
            "bundle_id": faction_bundle["bundle_id"],
            "purpose": "faction_turn",
            "faction_id": "ember-court",
            "intent": "Preserve the alliance.",
            "proposed_actions": [
                {
                    "kind": "send_message",
                    "target_ref": f"actor:{pc['id']}",
                    "summary": "Send a guarded reply.",
                    "basis_refs": [],
                }
            ],
            "claims": [],
            "resolution_requests": [],
            "decision_summary": "The court delays escalation.",
        }
        faction_validated = await _call(
            server,
            "bounded_evaluation",
            {
                "campaign_id": campaign["id"],
                "action": "validate",
                "proposal": faction_proposal,
                "bundle_receipt": faction_bundle["bundle_receipt"],
            },
        )
        assert faction_validated["proposal"] == faction_proposal

        audience_bundle = await _call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "purpose": "audience_render",
                "actor_id": pc["id"],
                "audience": "player",
                "query": "Describe only what the hero can perceive.",
            },
        )
        assert audience_bundle["context"]["source_evidence"] == []
        audience_proposal = {
            "schema_version": 1,
            "bundle_id": audience_bundle["bundle_id"],
            "purpose": "audience_render",
            "text": "The envoy waits for your answer.",
            "cited_basis_refs": [],
            "omitted_sensitive_refs": [],
            "decision_summary": "Only observable behavior was rendered.",
        }
        publication = await _call(
            server,
            "bounded_evaluation",
            {
                "campaign_id": campaign["id"],
                "action": "validate",
                "proposal": audience_proposal,
                "bundle_receipt": audience_bundle["bundle_receipt"],
            },
        )
        assert publication["publication"]["text"] == audience_proposal["text"]

        await _call(
            server,
            "memory_change",
            {
                "campaign_id": campaign["id"],
                "action": "commit",
                "payload": {"event": {"summary": "The scene changes."}},
                "idempotency_key": "advance-event",
            },
        )
        with pytest.raises(Exception, match="stale after a continuity event"):
            await _call(
                server,
                "bounded_evaluation",
                {
                    "campaign_id": campaign["id"],
                    "action": "validate",
                    "proposal": audience_proposal,
                    "bundle_receipt": audience_bundle["bundle_receipt"],
                },
            )

    asyncio.run(exercise())


def test_actor_bundle_is_stale_after_new_memory_or_first_actor_knowledge(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign, npc, _pc = await _live_campaign(server)
        arguments = {
            "campaign_id": campaign["id"],
            "purpose": "actor_turn",
            "actor_id": npc["id"],
        }

        before_memory = await _call(server, "continuity_context", arguments)
        await _call(
            server,
            "memory_change",
            {
                "campaign_id": campaign["id"],
                "action": "upsert",
                "payload": {
                    "fact_key": "world:new-warning",
                    "content": "A warning bell has started ringing.",
                    "disclosure_scope": "dm",
                },
                "idempotency_key": "new-warning",
            },
        )
        with pytest.raises(Exception, match="stale at campaign memory"):
            await _call(
                server,
                "bounded_evaluation",
                {
                    "campaign_id": campaign["id"],
                    "action": "validate",
                    "proposal": _actor_proposal(before_memory, npc["id"]),
                    "bundle_receipt": before_memory["bundle_receipt"],
                },
            )

        before_knowledge = await _call(server, "continuity_context", arguments)
        assert before_knowledge["context"]["actor_knowledge"] == []
        await _call(
            server,
            "actor_knowledge_change",
            {
                "action": "add",
                "payload": {
                    "campaign_id": campaign["id"],
                    "actor_id": npc["id"],
                    "knowledge_key": "warning-bell",
                    "proposition": "The warning bell means the gate is closing.",
                    "disclosure_scope": "owner",
                },
                "idempotency_key": "npc-learns-warning",
            },
        )
        with pytest.raises(Exception, match="stale at ActorKnowledge"):
            await _call(
                server,
                "bounded_evaluation",
                {
                    "campaign_id": campaign["id"],
                    "action": "validate",
                    "proposal": _actor_proposal(before_knowledge, npc["id"]),
                    "bundle_receipt": before_knowledge["bundle_receipt"],
                },
            )

    asyncio.run(exercise())


def test_bounded_actor_mechanics_require_public_engine_resolution() -> None:
    proposal = {
        "schema_version": 1,
        "bundle_id": "bundle",
        "purpose": "actor_turn",
        "actor_id": "npc",
        "intent": "Fight.",
        "proposed_action": {
            "kind": "attack",
            "target_ref": "actor:pc",
            "summary": "Attack the hero.",
        },
        "claims": [],
        "resolution_requests": [],
        "decision_summary": "",
    }
    with pytest.raises(ValueError, match="requires an explicit resolution request"):
        normalize_bounded_proposal("actor_turn", proposal)


def test_generic_actor_contract_rejects_dialogue_in_favor_of_npc_turn() -> None:
    proposal = {
        "schema_version": 1,
        "bundle_id": "bundle",
        "purpose": "actor_turn",
        "actor_id": "npc",
        "intent": "Negotiate.",
        "utterance": "This field would bypass structured speech acts.",
        "proposed_action": {"kind": "none", "target_ref": "", "summary": ""},
        "claims": [],
        "resolution_requests": [],
        "decision_summary": "",
    }

    with pytest.raises(ValueError, match="unknown fields.*utterance"):
        normalize_bounded_proposal("actor_turn", proposal)


def test_generic_turn_contracts_reject_untyped_state_deltas() -> None:
    actor = {
        "schema_version": 1,
        "bundle_id": "bundle",
        "purpose": "actor_turn",
        "actor_id": "npc",
        "intent": "Wait.",
        "proposed_action": {"kind": "none", "target_ref": "", "summary": ""},
        "claims": [],
        "resolution_requests": [],
        "proposed_deltas": [{"hp": -99}],
        "decision_summary": "",
    }
    with pytest.raises(ValueError, match="unknown fields.*proposed_deltas"):
        normalize_bounded_proposal("actor_turn", actor)

    faction = {
        "schema_version": 1,
        "bundle_id": "bundle",
        "purpose": "faction_turn",
        "faction_id": "court",
        "intent": "Wait.",
        "proposed_actions": [],
        "claims": [],
        "resolution_requests": [],
        "proposed_deltas": [{"world_state": "won"}],
        "decision_summary": "",
    }
    with pytest.raises(ValueError, match="unknown fields.*proposed_deltas"):
        normalize_bounded_proposal("faction_turn", faction)


def test_source_interpretation_requires_evidence_and_reviews_uncertainty() -> None:
    proposal = {
        "schema_version": 1,
        "bundle_id": "bundle",
        "purpose": "source_interpretation",
        "question": "What does the source require?",
        "interpretation": "The source is incomplete.",
        "claims": [],
        "ambiguities": [],
        "requires_dm_review": False,
    }
    with pytest.raises(ValueError, match="evidence-bound claim"):
        normalize_bounded_proposal("source_interpretation", proposal)

    proposal["claims"] = [
        {"statement": "A guess.", "basis_refs": [], "posture": "opinion"}
    ]
    with pytest.raises(ValueError, match="evidence-bound claim"):
        normalize_bounded_proposal("source_interpretation", proposal)

    proposal["claims"] = [
        {
            "statement": "The threshold is unclear.",
            "basis_refs": ["source:one"],
            "posture": "uncertain",
        }
    ]
    with pytest.raises(ValueError, match="require DM review"):
        normalize_bounded_proposal("source_interpretation", proposal)


def test_bounded_evaluation_contract_schemas_ship_and_are_strict() -> None:
    names = [
        "bounded-evaluation-bundle.v1.schema.json",
        "actor-turn-proposal.v1.schema.json",
        "audience-render-proposal.v1.schema.json",
        "faction-turn-proposal.v1.schema.json",
        "source-interpretation-proposal.v1.schema.json",
        "bounded-ruling-proposal.v1.schema.json",
    ]
    contracts = files("sagasmith_dnd_mcp").joinpath("contracts")
    schemas = {
        name: json.loads(contracts.joinpath(name).read_text(encoding="utf-8"))
        for name in names
    }
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False
    assert schemas["bounded-evaluation-bundle.v1.schema.json"]["properties"][
        "constraints"
    ]["properties"]["may_call_tools"]["const"] is False
