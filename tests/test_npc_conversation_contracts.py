import json

import pytest
from jsonschema import Draft202012Validator

from sagasmith_dnd_mcp.npc_conversations import (
    NPC_CONVERSATION_SCHEMA_VERSION,
    ConversationStore,
    derive_publication,
    normalize_audience_facts,
    normalize_conversation_proposal,
)


def _proposal(**overrides):
    value = {
        "schema_version": 4,
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


def test_v4_requires_only_text_for_each_utterance() -> None:
    normalized = normalize_conversation_proposal(_proposal())
    assert normalized["utterance_segments"][0]["speech_act"] == "deflect_with_a_denial"
    old = _proposal(schema_version=2)
    with pytest.raises(ValueError, match="must be 4"):
        normalize_conversation_proposal(old)
    minimal = _proposal(
        response_bid={"should_respond": True},
        utterance_segments=[{"text": "No."}],
    )
    normalized_minimal = normalize_conversation_proposal(minimal)
    assert normalized_minimal["utterance_segments"] == [
        {
            "text": "No.",
            "speech_act": "",
            "truth_posture": "",
            "basis_refs": [],
            "targets": [],
            "language": "",
            "delivery": "",
        }
    ]


def test_schema_accepts_minimal_v4_proposal() -> None:
    schema_path = (
        __import__("pathlib").Path(__file__).parents[1]
        / "src"
        / "sagasmith_dnd_mcp"
        / "contracts"
        / "npc-conversation-proposal.v4.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    minimal = {
        "schema_version": 4,
        "conversation_id": "conversation",
        "activation_id": "activation",
        "actor_runtime_id": "conversation:npc",
        "response_bid": {"should_respond": True},
        "utterance_segments": [{"text": "No."}],
    }
    assert not list(Draft202012Validator(schema).iter_errors(minimal))


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
    candidates = store.memory_candidates(store.get(opened["conversation_id"]))
    pc_candidate = next(item for item in candidates if item["actor_id"] == "pc")
    assert pc_candidate["value"]["metadata"]["statement_truth_not_implied"] is True


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
    claims = [
        item["value"]["proposition"]
        for item in store.memory_candidates(session)
        if item["actor_id"] == "npc-2"
    ]
    assert claims == ["npc said: The gate opens at dusk."]


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
    assert submitted["resolution_requests"][0]["resolution_id"]
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
    session = store.get(opened["conversation_id"])
    assert session["status"] == "open"
    resolution_id = submitted["resolution_requests"][0]["resolution_id"]
    resolution_audience = _audience(response=(), understood=("npc", "pc"), perceived=("npc", "pc"))
    resolution_audience["decision_id"] = "resolution-audience"
    resolved = store.append_event(
        session,
        event={
            "type": "resolution",
            "speaker_actor_id": "",
            "content": "Aria wins the contest and keeps her footing.",
            "resolved_resolution_ids": [resolution_id],
        },
        audience_facts=resolution_audience,
        expected_revision=4,
        idempotency_key="resolve-1",
    )
    final = store.get(opened["conversation_id"])
    assert resolved["event"]["resolved_resolution_ids"] == [resolution_id]
    assert final["pending_resolutions"][0]["status"] == "resolved"
    assert final["pending_resolutions"][0]["resolution_event_id"] == resolved["event"]["event_id"]
    assert final["events"][-1]["actor_inboxes"]["pc"]["resolved_resolution_ids"] == [resolution_id]


def test_retired_conversation_journal_is_ignored_by_current_runtime(tmp_path) -> None:
    root = tmp_path / "conversations"
    store = ConversationStore(root)
    conversation_id = "00000000-0000-0000-0000-000000000001"
    (root / f"{conversation_id}.json").write_text(
        json.dumps(
            {
                "schema_version": NPC_CONVERSATION_SCHEMA_VERSION - 1,
                "contract": (
                    f"npc-conversation.v{NPC_CONVERSATION_SCHEMA_VERSION - 1}"
                ),
                "status": "open",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(LookupError):
        store.get(conversation_id)
    assert store.cleanup_terminal_receipts() == 1
    assert not (root / f"{conversation_id}.json").exists()
    opened = _open(store)
    assert opened["status"] == "open"
    assert store.active_ids(campaign_id="campaign", branch_id="branch") == [
        opened["conversation_id"]
    ]


def test_terminal_journal_is_compact_and_replays_the_exact_result(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    opened = _open(store)
    session, replay = store.begin_mutation(
        opened["conversation_id"],
        expected_revision=0,
        idempotency_key="abort",
        operation="abort",
        payload={},
    )
    assert replay is None
    session["status"] = "aborted"
    result = store.finish_mutation(session, {"status": "aborted", "detail": "done"})
    stored = store.get(opened["conversation_id"])

    assert stored["events"] == []
    assert stored["memory_candidates"] == []
    assert "compressed_result" in stored["idempotency"]["abort"]
    _, replayed = store.begin_mutation(
        opened["conversation_id"],
        expected_revision=0,
        idempotency_key="abort",
        operation="abort",
        payload={},
    )
    assert replayed == result


def test_conversation_event_limit_is_enforced_before_another_append(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    opened = _open(store)
    session = store.get(opened["conversation_id"])
    session["events"] = [
        {"event_id": f"event:{index}", "sequence": index + 1, "type": "scene_prompt"}
        for index in range(200)
    ]
    store.save(session)

    with pytest.raises(ValueError, match="200-event limit"):
        store.append_event(
            store.get(opened["conversation_id"]),
            event={"type": "scene_prompt", "content": "Too late."},
            audience_facts=_audience(),
            expected_revision=0,
            idempotency_key="overflow",
        )
