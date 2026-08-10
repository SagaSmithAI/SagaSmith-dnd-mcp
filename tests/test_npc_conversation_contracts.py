import json

import pytest
from jsonschema import Draft202012Validator

from sagasmith_dnd_mcp.npc_conversations import (
    ConversationStore,
    derive_publication,
    normalize_audience_facts,
    normalize_conversation_proposal,
)


def _proposal(**overrides):
    value = {
        "schema_version": 3,
        "conversation_id": "conversation",
        "activation_id": "activation",
        "actor_runtime_id": "conversation:npc",
        "response_bid": {"should_respond": True, "urgency": 70, "reason": "Addressed."},
        "private_intent": "Hide the visit.",
        "utterance_segments": [
            {
                "text": "I never went to the docks.",
                "speech_act": "deflect_with_a_denial",
                "truth_posture": "intentional_deception",
                "basis_refs": ["knowledge:dock-visit:rev-1"],
                "targets": ["pc"],
                "language": "Common",
                "delivery": "quietly",
            }
        ],
        "proposed_action": {
            "summary": "",
            "target_refs": [],
            "settlement": "narrative",
            "mechanic_hint": "",
        },
        "resolution_requests": [],
        "working_deltas": {"facts": [], "actor_knowledge": [], "commitments": []},
        "visible_cues": ["She avoids eye contact."],
        "decision_summary": "Deny the accusation.",
    }
    value.update(overrides)
    return value


def _context(actor_id="npc"):
    return {
        "authority": {"actor_revision": 2, "campaign_revision": 5},
        "actor": {"id": actor_id, "name": "Mara"},
        "constraints": {
            "allowed_basis_refs": ["knowledge:dock-visit:rev-1"],
            "allowed_target_actor_ids": ["npc", "npc-2", "pc"],
        },
    }


def _audience(*, response=("npc",), understood=("npc",), perceived=("npc",), partial=None):
    return normalize_audience_facts(
        {
            "decision_id": "audience-1",
            "resolver": "agent",
            "perceived_actor_ids": list(perceived),
            "understood_actor_ids": list(understood),
            "response_actor_ids": list(response),
            "partial_renditions": partial or {},
            "basis_refs": ["scene:line-of-sight"],
            "reason": "Agent applied the current scene and delivery facts.",
        },
        participant_ids={"npc", "npc-2", "pc"},
        response_actor_ids={"npc", "npc-2"},
    )


def _open(store):
    return store.open(
        campaign_id="campaign",
        branch_id="branch",
        principal_id="dm",
        scope_id="party",
        scene_id="scene",
        authority={"campaign_revision": 5, "scene_state_version": 0},
        participants=[
            {"actor_id": "npc", "name": "Mara", "kind": "npc"},
            {"actor_id": "npc-2", "name": "Tomas", "kind": "npc"},
            {"actor_id": "pc", "name": "Aria", "kind": "pc"},
        ],
        actor_contexts={"npc": _context(), "npc-2": _context("npc-2")},
        idempotency_key="open-1",
    )


def test_v3_is_strict_but_speech_act_is_open() -> None:
    normalized = normalize_conversation_proposal(_proposal())
    assert normalized["utterance_segments"][0]["speech_act"] == "deflect_with_a_denial"
    old = _proposal(schema_version=2)
    with pytest.raises(ValueError, match="must be 3"):
        normalize_conversation_proposal(old)
    uncited = _proposal()
    uncited["utterance_segments"][0]["basis_refs"] = []
    with pytest.raises(ValueError, match="requires a basis_ref"):
        normalize_conversation_proposal(uncited)


def test_schema_accepts_normalized_v3_proposal() -> None:
    schema_path = (
        __import__("pathlib").Path(__file__).parents[1]
        / "src"
        / "sagasmith_dnd_mcp"
        / "contracts"
        / "npc-conversation-proposal.v3.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(schema).iter_errors(_proposal()))


def test_publication_drops_private_semantics() -> None:
    publication = derive_publication(
        normalize_conversation_proposal(_proposal()), publication_id="publication"
    )
    encoded = json.dumps(publication)
    assert publication["speech"] == "I never went to the docks."
    assert "private_intent" not in encoded
    assert "intentional_deception" not in encoded


def test_audience_facts_select_activation_and_redact_each_inbox(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    opened = _open(store)
    session = store.get(opened["conversation_id"])
    audience = _audience(
        response=("npc",),
        understood=("npc",),
        perceived=("npc", "npc-2"),
    )
    ingested = store.append_event(
        session,
        event={"type": "speech", "speaker_actor_id": "pc", "content": "Secret words."},
        audience_facts=audience,
        expected_revision=0,
        idempotency_key="ingest-1",
    )
    assert [item["actor_id"] for item in ingested["activations"]] == ["npc"]
    activation = ingested["activations"][0]
    capsule = store.checkout(
        store.get(opened["conversation_id"]),
        activation_ref=activation["activation_ref"],
        cursor=0,
        include_bootstrap=True,
        expected_revision=1,
        idempotency_key="claim-1",
    )
    assert capsule["inbox"][0]["content"] == "Secret words."
    event = store.get(opened["conversation_id"])["events"][0]
    assert "Secret words." not in json.dumps(event["actor_inboxes"]["npc-2"])
    assert event["actor_inboxes"]["npc-2"]["comprehension"] == "perceived_only"


def test_submit_validation_keeps_lease_and_success_waits_for_publication(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    opened = _open(store)
    ingested = store.append_event(
        store.get(opened["conversation_id"]),
        event={"type": "speech", "speaker_actor_id": "pc", "content": "Answer."},
        audience_facts=_audience(),
        expected_revision=0,
        idempotency_key="ingest-1",
    )
    activation = ingested["activations"][0]
    capsule = store.checkout(
        store.get(opened["conversation_id"]),
        activation_ref=activation["activation_ref"],
        cursor=0,
        include_bootstrap=True,
        expected_revision=1,
        idempotency_key="claim-1",
    )
    bad = _proposal(schema_version=2)
    rejected = store.submit(
        store.get(opened["conversation_id"]),
        activation_ref=activation["activation_ref"],
        lease_id=capsule["lease_id"],
        proposal=bad,
        expected_revision=2,
        idempotency_key="submit-bad",
    )
    assert rejected["status"] == "validation_failed"
    assert rejected["lease_retained"] is True
    good = _proposal(
        conversation_id=opened["conversation_id"],
        activation_id=capsule["activation_id"],
        actor_runtime_id=capsule["actor_runtime_id"],
    )
    submitted = store.submit(
        store.get(opened["conversation_id"]),
        activation_ref=activation["activation_ref"],
        lease_id=capsule["lease_id"],
        proposal=good,
        expected_revision=2,
        idempotency_key="submit-good",
    )
    assert submitted["status"] == "publication_ready"
    assert len(store.get(opened["conversation_id"])["events"]) == 1
    publication_audience = _audience(response=(), understood=("npc", "pc"), perceived=("npc", "pc"))
    publication_audience["decision_id"] = "audience-2"
    published = store.publish(
        store.get(opened["conversation_id"]),
        publication_id=submitted["publication"]["publication_id"],
        audience_facts=publication_audience,
        segment_audience_facts=None,
        expected_revision=3,
        idempotency_key="publish-1",
    )
    assert published["status"] == "published"
    assert len(store.get(opened["conversation_id"])["events"]) == 2
    candidates = store.get(opened["conversation_id"])["listener_knowledge_candidates"]
    assert candidates["pc"][0]["metadata"]["statement_truth_not_implied"] is True


def test_every_mutation_requires_current_revision_and_replays_identically(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    opened = _open(store)
    kwargs = {
        "event": {"type": "speech", "speaker_actor_id": "pc", "content": "Answer."},
        "audience_facts": _audience(),
        "expected_revision": 0,
        "idempotency_key": "ingest-1",
    }
    first = store.append_event(store.get(opened["conversation_id"]), **kwargs)
    replay = store.append_event(store.get(opened["conversation_id"]), **kwargs)
    assert replay == first
    with pytest.raises(ValueError, match="REVISION_CONFLICT"):
        store.append_event(
            store.get(opened["conversation_id"]),
            event={"type": "speech", "speaker_actor_id": "pc", "content": "Too late."},
            audience_facts={**_audience(), "decision_id": "audience-2"},
            expected_revision=0,
            idempotency_key="ingest-2",
        )


def test_publication_redacts_each_segment_and_derives_only_understood_claims(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    opened = _open(store)
    ingested = store.append_event(
        store.get(opened["conversation_id"]),
        event={"type": "speech", "speaker_actor_id": "pc", "content": "Tell us both."},
        audience_facts=_audience(),
        expected_revision=0,
        idempotency_key="ingest-1",
    )
    activation = ingested["activations"][0]
    capsule = store.checkout(
        store.get(opened["conversation_id"]),
        activation_ref=activation["activation_ref"],
        cursor=0,
        include_bootstrap=True,
        expected_revision=1,
        idempotency_key="claim-1",
    )
    proposal = _proposal(
        conversation_id=opened["conversation_id"],
        activation_id=capsule["activation_id"],
        actor_runtime_id=capsule["actor_runtime_id"],
    )
    proposal["utterance_segments"] = [
        {**proposal["utterance_segments"][0], "text": "The gate opens at dusk."},
        {**proposal["utterance_segments"][0], "text": "The password is heron."},
    ]
    submitted = store.submit(
        store.get(opened["conversation_id"]),
        activation_ref=activation["activation_ref"],
        lease_id=capsule["lease_id"],
        proposal=proposal,
        expected_revision=2,
        idempotency_key="submit-1",
    )
    overall = _audience(response=(), understood=("npc",), perceived=("npc", "npc-2", "pc"))
    overall["decision_id"] = "publication-overall"
    first = _audience(
        response=(),
        understood=("npc", "npc-2", "pc"),
        perceived=("npc", "npc-2", "pc"),
    )
    first["decision_id"] = "publication-segment-1"
    second = _audience(
        response=(),
        understood=("npc", "pc"),
        perceived=("npc", "npc-2", "pc"),
    )
    second["decision_id"] = "publication-segment-2"
    store.publish(
        store.get(opened["conversation_id"]),
        publication_id=submitted["publication"]["publication_id"],
        audience_facts=overall,
        segment_audience_facts=[first, second],
        expected_revision=3,
        idempotency_key="publish-1",
    )

    session = store.get(opened["conversation_id"])
    inbox = session["events"][-1]["actor_inboxes"]["npc-2"]
    assert inbox["utterance_segments"][0]["text"] == "The gate opens at dusk."
    assert inbox["utterance_segments"][1]["comprehension"] == "perceived_only"
    assert "password" not in json.dumps(inbox)
    claims = session["listener_knowledge_candidates"]["npc-2"]
    assert [item["proposition"] for item in claims] == ["npc said: The gate opens at dusk."]


def test_mechanical_action_waits_locally_without_blocking_public_speech(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    opened = _open(store)
    ingested = store.append_event(
        store.get(opened["conversation_id"]),
        event={"type": "speech", "speaker_actor_id": "pc", "content": "Stop!"},
        audience_facts=_audience(),
        expected_revision=0,
        idempotency_key="ingest-1",
    )
    activation = ingested["activations"][0]
    capsule = store.checkout(
        store.get(opened["conversation_id"]),
        activation_ref=activation["activation_ref"],
        cursor=0,
        include_bootstrap=True,
        expected_revision=1,
        idempotency_key="claim-1",
    )
    proposal = _proposal(
        conversation_id=opened["conversation_id"],
        activation_id=capsule["activation_id"],
        actor_runtime_id=capsule["actor_runtime_id"],
        proposed_action={
            "summary": "Mara shoves Aria aside.",
            "target_refs": ["actor:pc"],
            "settlement": "mechanical",
            "mechanic_hint": "Resolve the shove with the normal rules.",
        },
        resolution_requests=[
            {
                "kind": "contest",
                "reason": "The shove outcome is uncertain.",
                "actor_ids": ["npc", "pc"],
            }
        ],
    )
    submitted = store.submit(
        store.get(opened["conversation_id"]),
        activation_ref=activation["activation_ref"],
        lease_id=capsule["lease_id"],
        proposal=proposal,
        expected_revision=2,
        idempotency_key="submit-1",
    )
    assert submitted["status"] == "publication_ready"
    assert submitted["publication"]["speech"] == "I never went to the docks."
    assert submitted["publication"]["visible_action"] == ""
    assert submitted["publication"]["action_pending_resolution"] is True
    session = store.get(opened["conversation_id"])
    assert session["status"] == "open"
    assert session["pending_resolutions"][0]["status"] == "pending"

    audience = _audience(response=(), understood=("npc", "pc"), perceived=("npc", "pc"))
    audience["decision_id"] = "publication-audience"
    published = store.publish(
        session,
        publication_id=submitted["publication"]["publication_id"],
        audience_facts=audience,
        segment_audience_facts=None,
        expected_revision=3,
        idempotency_key="publish-1",
    )
    assert published["status"] == "published"
    assert store.get(opened["conversation_id"])["status"] == "open"


def test_retired_conversation_journal_is_rejected_instead_of_migrated(tmp_path) -> None:
    root = tmp_path / "conversations"
    store = ConversationStore(root)
    conversation_id = "00000000-0000-0000-0000-000000000001"
    (root / f"{conversation_id}.json").write_text(
        json.dumps({"schema_version": 1, "contract": "npc-conversation.v1", "status": "open"}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="retired contract"):
        store.get(conversation_id)
