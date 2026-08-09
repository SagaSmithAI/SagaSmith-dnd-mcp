"""Strict, provider-neutral contracts for isolated NPC portrayal turns."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

NPC_TURN_SCHEMA_VERSION = 1
NPC_TURN_BUNDLE_SCHEMA_VERSION = 2
NPC_TURN_PURPOSES = frozenset({"general", "npc_turn"})
NPC_TRUTH_POSTURES = frozenset(
    {"believes_true", "uncertain", "intentional_deception", "opinion", "nonfactual"}
)
NPC_SPEECH_ACT_KINDS = frozenset(
    {"assert", "ask", "promise", "threaten", "refuse", "reveal", "withhold", "lie"}
)
NPC_ACTION_KINDS = frozenset(
    {
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
    }
)
NPC_NARRATIVE_ACTION_KINDS = frozenset(
    {
        "none",
        "gesture",
        "offer",
        "refuse",
        "surrender",
        "move",
        "flee",
        "scene_transition",
        "observe",
        "interact",
        "follow",
        "wait",
    }
)
NPC_RESOLUTION_KINDS = frozenset(
    {"ability_check", "contest", "saving_throw", "attack", "dm_adjudication"}
)


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return dict(value)


def _strict(value: dict[str, Any], field: str, allowed: set[str]) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{field} has unknown fields: {sorted(unknown)}")


def _text(
    value: Any,
    field: str,
    *,
    required: bool = False,
    maximum: int = 4_000,
) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ValueError(f"{field} is required")
    if len(result) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return result


def _string_list(value: Any, field: str, *, maximum: int = 100) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    result = [_text(item, f"{field}[]", required=True, maximum=maximum) for item in value]
    if len(result) != len(set(result)):
        raise ValueError(f"{field} must not contain duplicates")
    return result


def normalize_npc_stimulus(value: Any) -> dict[str, Any]:
    data = _object(value or {}, "stimulus")
    _strict(
        data,
        "stimulus",
        {
            "kind",
            "speaker_actor_id",
            "content",
            "language",
            "target_actor_ids",
            "source_event_ids",
        },
    )
    kind = _text(data.get("kind"), "stimulus.kind", maximum=40) or "none"
    if kind not in {"none", "speech", "action", "state_change", "scene_prompt"}:
        raise ValueError(f"unsupported stimulus kind: {kind}")
    return {
        "kind": kind,
        "speaker_actor_id": _text(
            data.get("speaker_actor_id"), "stimulus.speaker_actor_id", maximum=100
        ),
        "content": _text(data.get("content"), "stimulus.content", maximum=6_000),
        "language": _text(data.get("language"), "stimulus.language", maximum=100),
        "target_actor_ids": _string_list(
            data.get("target_actor_ids"), "stimulus.target_actor_ids"
        ),
        "source_event_ids": _string_list(
            data.get("source_event_ids"), "stimulus.source_event_ids"
        ),
    }


def normalize_npc_turn_proposal(value: Any) -> dict[str, Any]:
    data = _object(value, "npc_turn.proposal")
    _strict(
        data,
        "npc_turn.proposal",
        {
            "schema_version",
            "bundle_id",
            "speaker_actor_id",
            "intent",
            "utterance",
            "speech_acts",
            "proposed_action",
            "resolution_requests",
            "proposed_deltas",
            "portrayal",
            "decision_summary",
        },
    )
    schema_version = data.get("schema_version")
    if type(schema_version) is not int or schema_version != NPC_TURN_SCHEMA_VERSION:
        raise ValueError(
            f"npc_turn.proposal.schema_version must be {NPC_TURN_SCHEMA_VERSION}"
        )
    intent = _object(data.get("intent") or {}, "npc_turn.proposal.intent")
    _strict(intent, "npc_turn.proposal.intent", {"kind", "summary"})
    utterance = _object(data.get("utterance") or {}, "npc_turn.proposal.utterance")
    _strict(utterance, "npc_turn.proposal.utterance", {"text", "language", "delivery"})

    speech_acts: list[dict[str, Any]] = []
    raw_speech_acts = data.get("speech_acts") or []
    if not isinstance(raw_speech_acts, list):
        raise ValueError("npc_turn.proposal.speech_acts must be a list")
    for index, raw in enumerate(raw_speech_acts):
        item = _object(raw, f"npc_turn.proposal.speech_acts[{index}]")
        _strict(
            item,
            f"npc_turn.proposal.speech_acts[{index}]",
            {"kind", "content", "truth_posture", "basis_refs", "targets"},
        )
        kind = _text(item.get("kind"), f"speech_acts[{index}].kind", required=True)
        if kind not in NPC_SPEECH_ACT_KINDS:
            raise ValueError(f"unsupported NPC speech act kind: {kind}")
        truth_posture = _text(
            item.get("truth_posture"),
            f"speech_acts[{index}].truth_posture",
            required=True,
        )
        if truth_posture not in NPC_TRUTH_POSTURES:
            raise ValueError(f"unsupported NPC truth posture: {truth_posture}")
        speech_acts.append(
            {
                "kind": kind,
                "content": _text(
                    item.get("content"),
                    f"speech_acts[{index}].content",
                    required=True,
                    maximum=2_000,
                ),
                "truth_posture": truth_posture,
                "basis_refs": _string_list(
                    item.get("basis_refs"), f"speech_acts[{index}].basis_refs", maximum=300
                ),
                "targets": _string_list(
                    item.get("targets"), f"speech_acts[{index}].targets", maximum=200
                ),
            }
        )
        if (
            truth_posture in {"believes_true", "uncertain", "intentional_deception"}
            and kind in {"assert", "reveal", "lie"}
            and not speech_acts[-1]["basis_refs"]
        ):
            raise ValueError(
                f"speech_acts[{index}] factual/deceptive content requires a basis_ref"
            )

    action = _object(data.get("proposed_action") or {}, "npc_turn.proposal.proposed_action")
    _strict(action, "npc_turn.proposal.proposed_action", {"kind", "target_ref", "summary"})
    action_kind = _text(action.get("kind"), "proposed_action.kind", maximum=50) or "none"
    if action_kind not in NPC_ACTION_KINDS:
        raise ValueError(f"unsupported NPC action kind: {action_kind}")

    resolution_requests: list[dict[str, Any]] = []
    raw_requests = data.get("resolution_requests") or []
    if not isinstance(raw_requests, list):
        raise ValueError("npc_turn.proposal.resolution_requests must be a list")
    for index, raw in enumerate(raw_requests):
        item = _object(raw, f"npc_turn.proposal.resolution_requests[{index}]")
        _strict(
            item,
            f"npc_turn.proposal.resolution_requests[{index}]",
            {"kind", "reason", "actor_ids", "suggested_skill"},
        )

        kind = _text(item.get("kind"), f"resolution_requests[{index}].kind", required=True)
        if kind not in NPC_RESOLUTION_KINDS:
            raise ValueError(f"unsupported NPC resolution kind: {kind}")
        resolution_requests.append(
            {
                "kind": kind,
                "reason": _text(
                    item.get("reason"),
                    f"resolution_requests[{index}].reason",
                    required=True,
                    maximum=1_000,
                ),
                "actor_ids": _string_list(
                    item.get("actor_ids"), f"resolution_requests[{index}].actor_ids"
                ),
                "suggested_skill": _text(
                    item.get("suggested_skill"),
                    f"resolution_requests[{index}].suggested_skill",
                    maximum=100,
                ),
            }
        )

    if action_kind not in NPC_NARRATIVE_ACTION_KINDS and not resolution_requests:
        raise ValueError(
            f"NPC action {action_kind!r} requires an explicit resolution request"
        )

    deltas = _object(data.get("proposed_deltas") or {}, "npc_turn.proposal.proposed_deltas")
    _strict(deltas, "npc_turn.proposal.proposed_deltas", {"facts", "actor_knowledge"})
    facts = deltas.get("facts") or []
    actor_knowledge = deltas.get("actor_knowledge") or []
    if not isinstance(facts, list) or not all(isinstance(item, dict) for item in facts):
        raise ValueError("npc_turn.proposal.proposed_deltas.facts must be a list of objects")
    if not isinstance(actor_knowledge, list) or not all(
        isinstance(item, dict) for item in actor_knowledge
    ):
        raise ValueError(
            "npc_turn.proposal.proposed_deltas.actor_knowledge must be a list of objects"
        )

    portrayal = _object(data.get("portrayal") or {}, "npc_turn.proposal.portrayal")
    _strict(portrayal, "npc_turn.proposal.portrayal", {"emotion", "visible_cues"})
    result = {
        "schema_version": NPC_TURN_SCHEMA_VERSION,
        "bundle_id": _text(data.get("bundle_id"), "npc_turn.proposal.bundle_id", required=True),
        "speaker_actor_id": _text(
            data.get("speaker_actor_id"),
            "npc_turn.proposal.speaker_actor_id",
            required=True,
        ),
        "intent": {
            "kind": _text(intent.get("kind"), "intent.kind", required=True, maximum=100),
            "summary": _text(intent.get("summary"), "intent.summary", maximum=1_000),
        },
        "utterance": {
            "text": _text(utterance.get("text"), "utterance.text", maximum=4_000),
            "language": _text(utterance.get("language"), "utterance.language", maximum=100),
            "delivery": _text(utterance.get("delivery"), "utterance.delivery", maximum=500),
        },
        "speech_acts": speech_acts,
        "proposed_action": {
            "kind": action_kind,
            "target_ref": _text(
                action.get("target_ref"), "proposed_action.target_ref", maximum=300
            ),
            "summary": _text(
                action.get("summary"), "proposed_action.summary", maximum=1_000
            ),
        },
        "resolution_requests": resolution_requests,
        "proposed_deltas": {
            "facts": [deepcopy(item) for item in facts],
            "actor_knowledge": [deepcopy(item) for item in actor_knowledge],
        },
        "portrayal": {
            "emotion": _text(portrayal.get("emotion"), "portrayal.emotion", maximum=200),
            "visible_cues": _string_list(
                portrayal.get("visible_cues"), "portrayal.visible_cues", maximum=500
            ),
        },
        "decision_summary": _text(
            data.get("decision_summary"), "decision_summary", maximum=500
        ),
    }
    if not result["utterance"]["text"] and action_kind == "none":
        raise ValueError("NPC proposal must contain an utterance or a proposed action")
    return result


def validate_npc_basis_refs(
    proposal: dict[str, Any],
    *,
    allowed_basis_refs: set[str],
) -> None:
    cited = {
        ref
        for speech_act in proposal["speech_acts"]
        for ref in speech_act["basis_refs"]
    }
    unknown = sorted(cited - allowed_basis_refs)
    if unknown:
        raise ValueError(f"NPC proposal cites basis refs outside its bundle: {unknown}")


def validate_npc_targets(
    proposal: dict[str, Any],
    *,
    allowed_actor_ids: set[str],
) -> None:
    cited_actor_ids = {
        target
        for speech_act in proposal["speech_acts"]
        for target in speech_act["targets"]
    }
    cited_actor_ids.update(
        actor_id
        for request in proposal["resolution_requests"]
        for actor_id in request["actor_ids"]
    )
    unknown = sorted(cited_actor_ids - allowed_actor_ids)
    if unknown:
        raise ValueError(f"NPC proposal cites targets outside its bundle: {unknown}")
    action_target_ref = str(proposal["proposed_action"].get("target_ref") or "")
    allowed_target_refs = {f"actor:{actor_id}" for actor_id in allowed_actor_ids}
    if action_target_ref and action_target_ref not in allowed_target_refs:
        raise ValueError("NPC proposal action target is outside its bundle")


def accepted_proposal_deltas(
    proposal: dict[str, Any],
    *,
    fact_indexes: list[int],
    actor_knowledge_indexes: list[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    def select(
        values: list[dict[str, Any]],
        indexes: list[int],
        field: str,
    ) -> list[dict[str, Any]]:
        if len(indexes) != len(set(indexes)):
            raise ValueError(f"{field} must not contain duplicate indexes")
        result: list[dict[str, Any]] = []
        for index in indexes:
            if not isinstance(index, int) or isinstance(index, bool):
                raise ValueError(f"{field} must contain integers")
            if index < 0 or index >= len(values):
                raise ValueError(f"{field} contains an out-of-range index: {index}")
            result.append(deepcopy(values[index]))
        return result

    return (
        select(proposal["proposed_deltas"]["facts"], fact_indexes, "accepted_fact_indexes"),
        select(
            proposal["proposed_deltas"]["actor_knowledge"],
            actor_knowledge_indexes,
            "accepted_actor_knowledge_indexes",
        ),
    )
