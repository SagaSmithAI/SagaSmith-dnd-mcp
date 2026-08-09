import json

import pytest
from jsonschema import Draft202012Validator

from sagasmith_dnd_mcp.npc_conversations import (
    ConversationStore,
    derive_publication,
    normalize_conversation_proposal,
)


def _proposal(**overrides):
    value = {
        "schema_version": 2,
        "conversation_id": "conversation",
        "activation_id": "activation",
        "actor_runtime_id": "conversation:npc",
        "response_bid": {
            "should_respond": True,
            "urgency": 70,
            "reason": "Directly addressed.",
        },
        "private_intent": "Hide the visit to the docks.",
        "utterance_segments": [
            {
                "text": "I never went to the docks.",
                "speech_act": "lie",
                "truth_posture": "intentional_deception",
                "basis_refs": ["knowledge:dock-visit:rev-1"],
                "targets": ["pc"],
                "language": "Common",
                "delivery": "quietly",
            }
        ],
        "proposed_action": {"kind": "none", "target_ref": "", "summary": ""},
        "resolution_requests": [],
        "working_deltas": {"facts": [], "actor_knowledge": [], "commitments": []},
        "visible_cues": ["She avoids eye contact."],
        "decision_summary": "Deny the accusation.",
    }
    value.update(overrides)
    return value


def _context():
    return {
        "authority": {"actor_revision": 2, "campaign_revision": 5},
        "actor": {"id": "npc", "name": "Mara"},
        "constraints": {
            "allowed_basis_refs": ["knowledge:dock-visit:rev-1"],
            "allowed_target_actor_ids": ["npc", "pc"],
        },
    }


def test_v2_has_no_free_utterance_bypass() -> None:
    raw = _proposal(utterance={"text": "Unsupported assertion."}, utterance_segments=[])
    with pytest.raises(ValueError, match="unknown fields"):
        normalize_conversation_proposal(raw)

    uncited = _proposal()
    uncited["utterance_segments"][0]["basis_refs"] = []
    with pytest.raises(ValueError, match="requires a basis_ref"):
        normalize_conversation_proposal(uncited)


def test_schema_accepts_normalized_v2_proposal() -> None:
    schema_path = (
        __import__("pathlib").Path(__file__).parents[1]
        / "src"
        / "sagasmith_dnd_mcp"
        / "contracts"
        / "npc-conversation-proposal.v2.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(schema).iter_errors(_proposal()))


def test_publication_drops_private_intent_and_truth_posture() -> None:
    publication = derive_publication(
        normalize_conversation_proposal(_proposal()), publication_id="publication"
    )
    assert publication["speech"] == "I never went to the docks."
    assert publication["visible_cues"] == ["She avoids eye contact."]
    encoded = json.dumps(publication)
    assert "private_intent" not in encoded
    assert "intentional_deception" not in encoded


def test_store_persists_actor_scoped_activation_and_incremental_inbox(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    opened = store.open(
        campaign_id="campaign",
        branch_id="branch",
        principal_id="dm",
        scope_id="party",
        scene_id="scene",
        authority={"campaign_revision": 5, "latest_event_sequence": 9},
        participants=[
            {"actor_id": "npc", "name": "Mara", "kind": "npc"},
            {"actor_id": "pc", "name": "Aria", "kind": "pc"},
        ],
        actor_contexts={"npc": _context()},
    )
    conversation_id = opened["conversation_id"]
    session = store.require_owner(conversation_id, campaign_id="campaign", principal_id="dm")
    ingested = store.append_event(
        session,
        event={
            "type": "speech",
            "speaker_actor_id": "pc",
            "content": "I know you went to the docks.",
            "language": "Common",
        },
        perceived_by=["npc"],
        understood_by=["npc"],
        activate_actor_ids=["npc"],
        response_required_actor_ids={"npc"},
    )
    activation = ingested["activations"][0]

    reloaded = ConversationStore(tmp_path / "conversations")
    session = reloaded.require_owner(
        conversation_id, campaign_id="campaign", principal_id="dm"
    )
    capsule = reloaded.checkout(
        session,
        activation_id=activation["activation_id"],
        worker_handle=activation["worker_handle"],
        cursor=0,
        include_bootstrap=True,
    )
    assert capsule["bootstrap"]["actor"]["id"] == "npc"
    assert [item["content"] for item in capsule["inbox"]] == [
        "I know you went to the docks."
    ]
    assert capsule["constraints"]["may_call_tools"] is False

    proposal = _proposal(
        conversation_id=conversation_id,
        activation_id=activation["activation_id"],
        actor_runtime_id=activation["actor_runtime_id"],
    )
    result = reloaded.submit(
        session,
        activation_id=activation["activation_id"],
        worker_handle=activation["worker_handle"],
        lease_id=capsule["lease_id"],
        proposal=normalize_conversation_proposal(proposal),
    )
    assert result["status"] == "published"
    assert result["publication"]["speech"] == "I never went to the docks."
    status = reloaded.public_status(reloaded.get(conversation_id))
    assert status["publication_count"] == 1


def test_worker_handle_cannot_checkout_another_activation(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    opened = store.open(
        campaign_id="campaign",
        branch_id="branch",
        principal_id="dm",
        scope_id="party",
        scene_id="scene",
        authority={"campaign_revision": 5},
        participants=[
            {"actor_id": "npc", "name": "Mara", "kind": "npc"},
            {"actor_id": "npc-2", "name": "Tomas", "kind": "npc"},
            {"actor_id": "pc", "name": "Aria", "kind": "pc"},
        ],
        actor_contexts={"npc": _context(), "npc-2": _context()},
    )
    session = store.get(opened["conversation_id"])
    ingested = store.append_event(
        session,
        event={"type": "speech", "speaker_actor_id": "pc", "content": "Answer me."},
        perceived_by=["npc", "npc-2"],
        understood_by=["npc", "npc-2"],
        activate_actor_ids=["npc", "npc-2"],
        response_required_actor_ids={"npc", "npc-2"},
    )
    first, second = ingested["activations"]
    with pytest.raises(PermissionError, match="worker handle"):
        store.checkout(
            store.get(opened["conversation_id"]),
            activation_id=second["activation_id"],
            worker_handle=first["worker_handle"],
            cursor=0,
            include_bootstrap=True,
        )
