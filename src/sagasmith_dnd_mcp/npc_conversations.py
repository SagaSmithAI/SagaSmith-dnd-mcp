"""Durable, provider-neutral NPC conversation runtimes.

The MCP owns semantic continuity and actor isolation.  Model processes and
provider KV caches remain host concerns; a host can rebuild either from the
actor-scoped bootstrap plus the durable event journal kept here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

NPC_CONVERSATION_SCHEMA_VERSION = 1
NPC_CONVERSATION_PROPOSAL_SCHEMA_VERSION = 2
NPC_CONVERSATION_CONTRACT = "npc-conversation.v1"

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
ACTIVE_CONVERSATION_STATUSES = frozenset({"open", "suspended", "stale"})


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


def _string_list(value: Any, field: str, *, maximum: int = 200) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    result = [_text(item, f"{field}[]", required=True, maximum=maximum) for item in value]
    if len(result) != len(set(result)):
        raise ValueError(f"{field} must not contain duplicates")
    return result


def normalize_conversation_proposal(value: Any) -> dict[str, Any]:
    """Normalize v2 proposals where every speakable byte belongs to a cited segment."""

    data = _object(value, "npc_conversation.proposal")
    allowed = {
        "schema_version",
        "conversation_id",
        "activation_id",
        "actor_runtime_id",
        "response_bid",
        "private_intent",
        "utterance_segments",
        "proposed_action",
        "resolution_requests",
        "working_deltas",
        "visible_cues",
        "decision_summary",
    }
    _strict(data, "npc_conversation.proposal", allowed)
    if data.get("schema_version") != NPC_CONVERSATION_PROPOSAL_SCHEMA_VERSION:
        raise ValueError("npc_conversation.proposal.schema_version must be 2")

    response_bid = _object(data.get("response_bid") or {}, "response_bid")
    _strict(response_bid, "response_bid", {"should_respond", "urgency", "reason"})
    if not isinstance(response_bid.get("should_respond"), bool):
        raise ValueError("response_bid.should_respond must be boolean")
    urgency = response_bid.get("urgency", 0)
    if type(urgency) is not int or not 0 <= urgency <= 100:
        raise ValueError("response_bid.urgency must be an integer from 0 to 100")

    raw_segments = data.get("utterance_segments") or []
    if not isinstance(raw_segments, list) or len(raw_segments) > 12:
        raise ValueError("utterance_segments must be a list with at most 12 items")
    segments: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_segments):
        item = _object(raw, f"utterance_segments[{index}]")
        _strict(
            item,
            f"utterance_segments[{index}]",
            {
                "text",
                "speech_act",
                "truth_posture",
                "basis_refs",
                "targets",
                "language",
                "delivery",
            },
        )
        speech_act = _text(
            item.get("speech_act"),
            f"utterance_segments[{index}].speech_act",
            required=True,
            maximum=40,
        )
        if speech_act not in NPC_SPEECH_ACT_KINDS:
            raise ValueError(f"unsupported NPC speech act kind: {speech_act}")
        truth_posture = _text(
            item.get("truth_posture"),
            f"utterance_segments[{index}].truth_posture",
            required=True,
            maximum=40,
        )
        if truth_posture not in NPC_TRUTH_POSTURES:
            raise ValueError(f"unsupported NPC truth posture: {truth_posture}")
        basis_refs = _string_list(
            item.get("basis_refs"),
            f"utterance_segments[{index}].basis_refs",
            maximum=300,
        )
        if (
            speech_act in {"assert", "reveal", "lie"}
            and truth_posture in {"believes_true", "uncertain", "intentional_deception"}
            and not basis_refs
        ):
            raise ValueError(f"utterance_segments[{index}] factual content requires a basis_ref")
        segments.append(
            {
                "text": _text(
                    item.get("text"),
                    f"utterance_segments[{index}].text",
                    required=True,
                    maximum=2_000,
                ),
                "speech_act": speech_act,
                "truth_posture": truth_posture,
                "basis_refs": basis_refs,
                "targets": _string_list(
                    item.get("targets"), f"utterance_segments[{index}].targets"
                ),
                "language": _text(
                    item.get("language"), f"utterance_segments[{index}].language", maximum=100
                ),
                "delivery": _text(
                    item.get("delivery"), f"utterance_segments[{index}].delivery", maximum=500
                ),
            }
        )

    action = _object(data.get("proposed_action") or {}, "proposed_action")
    _strict(action, "proposed_action", {"kind", "target_ref", "summary"})
    action_kind = _text(action.get("kind"), "proposed_action.kind", maximum=50) or "none"
    if action_kind not in NPC_ACTION_KINDS:
        raise ValueError(f"unsupported NPC action kind: {action_kind}")

    raw_requests = data.get("resolution_requests") or []
    if not isinstance(raw_requests, list) or len(raw_requests) > 8:
        raise ValueError("resolution_requests must be a list with at most 8 items")
    requests: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_requests):
        item = _object(raw, f"resolution_requests[{index}]")
        _strict(item, f"resolution_requests[{index}]", {"kind", "reason", "actor_ids"})
        kind = _text(item.get("kind"), f"resolution_requests[{index}].kind", required=True)
        if kind not in NPC_RESOLUTION_KINDS:
            raise ValueError(f"unsupported NPC resolution kind: {kind}")
        requests.append(
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
            }
        )
    if action_kind not in NPC_NARRATIVE_ACTION_KINDS and not requests:
        raise ValueError(f"NPC action {action_kind!r} requires an explicit resolution request")

    deltas = _object(data.get("working_deltas") or {}, "working_deltas")
    _strict(deltas, "working_deltas", {"facts", "actor_knowledge", "commitments"})
    normalized_deltas: dict[str, list[dict[str, Any]]] = {}
    for field in ("facts", "actor_knowledge", "commitments"):
        raw_items = deltas.get(field) or []
        if not isinstance(raw_items, list) or not all(isinstance(item, dict) for item in raw_items):
            raise ValueError(f"working_deltas.{field} must be a list of objects")
        if len(raw_items) > 20:
            raise ValueError(f"working_deltas.{field} exceeds 20 items")
        normalized_deltas[field] = [deepcopy(dict(item)) for item in raw_items]

    should_respond = response_bid["should_respond"]
    if should_respond and not segments and action_kind == "none" and not requests:
        raise ValueError("responding NPC must speak, act, or request resolution")
    if not should_respond and (segments or action_kind != "none" or requests):
        raise ValueError("non-responding NPC must not speak, act, or request resolution")

    return {
        "schema_version": NPC_CONVERSATION_PROPOSAL_SCHEMA_VERSION,
        "conversation_id": _text(
            data.get("conversation_id"), "conversation_id", required=True, maximum=100
        ),
        "activation_id": _text(
            data.get("activation_id"), "activation_id", required=True, maximum=100
        ),
        "actor_runtime_id": _text(
            data.get("actor_runtime_id"), "actor_runtime_id", required=True, maximum=220
        ),
        "response_bid": {
            "should_respond": should_respond,
            "urgency": urgency,
            "reason": _text(response_bid.get("reason"), "response_bid.reason", maximum=500),
        },
        "private_intent": _text(data.get("private_intent"), "private_intent", maximum=1_000),
        "utterance_segments": segments,
        "proposed_action": {
            "kind": action_kind,
            "target_ref": _text(
                action.get("target_ref"), "proposed_action.target_ref", maximum=300
            ),
            "summary": _text(action.get("summary"), "proposed_action.summary", maximum=1_000),
        },
        "resolution_requests": requests,
        "working_deltas": normalized_deltas,
        "visible_cues": _string_list(data.get("visible_cues"), "visible_cues", maximum=500),
        "decision_summary": _text(
            data.get("decision_summary"), "decision_summary", maximum=500
        ),
    }


def validate_conversation_proposal(
    proposal: dict[str, Any],
    *,
    conversation_id: str,
    activation_id: str,
    actor_runtime_id: str,
    actor_id: str,
    allowed_basis_refs: set[str],
    allowed_actor_ids: set[str],
) -> None:
    if proposal["conversation_id"] != conversation_id:
        raise ValueError("NPC proposal belongs to another conversation")
    if proposal["activation_id"] != activation_id:
        raise ValueError("NPC proposal belongs to another activation")
    if proposal["actor_runtime_id"] != actor_runtime_id:
        raise ValueError("NPC proposal belongs to another actor runtime")
    cited_basis = {
        ref for segment in proposal["utterance_segments"] for ref in segment["basis_refs"]
    }
    if unknown := sorted(cited_basis - allowed_basis_refs):
        raise ValueError(f"NPC proposal cites basis refs outside its actor capsule: {unknown}")
    cited_actor_ids = {
        target for segment in proposal["utterance_segments"] for target in segment["targets"]
    }
    cited_actor_ids.update(
        item for request in proposal["resolution_requests"] for item in request["actor_ids"]
    )
    if unknown := sorted(cited_actor_ids - allowed_actor_ids):
        raise ValueError(f"NPC proposal cites actors outside its conversation: {unknown}")
    target_ref = str(proposal["proposed_action"].get("target_ref") or "")
    if target_ref and target_ref not in {f"actor:{item}" for item in allowed_actor_ids}:
        raise ValueError("NPC proposal action target is outside its conversation")
    for index, item in enumerate(proposal["working_deltas"]["facts"]):
        if str(item.get("subject_ref") or "") != f"actor:{actor_id}":
            raise ValueError(f"working_deltas.facts[{index}] must belong to the speaking actor")
        if str(item.get("kind") or "") != "actor_state":
            raise ValueError(f"working_deltas.facts[{index}] must use kind='actor_state'")
        if str(item.get("predicate") or "") not in {
            "relationship_to",
            "goal",
            "commitment",
        }:
            raise ValueError(
                f"working_deltas.facts[{index}] may update only relationships, goals, "
                "or commitments"
            )
    for index, item in enumerate(proposal["working_deltas"]["actor_knowledge"]):
        if str(item.get("actor_id") or "") not in allowed_actor_ids:
            raise ValueError(
                f"working_deltas.actor_knowledge[{index}] targets an actor outside the conversation"
            )
    for index, item in enumerate(proposal["working_deltas"]["commitments"]):
        if str(item.get("actor_id") or "") != actor_id:
            raise ValueError(
                f"working_deltas.commitments[{index}] must belong to the speaking actor"
            )
        _text(
            item.get("commitment_key"),
            f"working_deltas.commitments[{index}].commitment_key",
            required=True,
            maximum=200,
        )
        _text(
            item.get("content"),
            f"working_deltas.commitments[{index}].content",
            required=True,
            maximum=2_000,
        )


def derive_publication(proposal: dict[str, Any], *, publication_id: str) -> dict[str, Any]:
    """Return the only model output that a Director may show to players."""

    segments = [
        {
            "text": item["text"],
            "speech_act": item["speech_act"],
            "targets": list(item["targets"]),
            "language": item["language"],
            "delivery": item["delivery"],
        }
        for item in proposal["utterance_segments"]
    ]
    action = proposal["proposed_action"]
    return {
        "schema_version": 1,
        "publication_id": publication_id,
        "conversation_id": proposal["conversation_id"],
        "activation_id": proposal["activation_id"],
        "actor_runtime_id": proposal["actor_runtime_id"],
        "utterance_segments": segments,
        "speech": " ".join(item["text"] for item in segments),
        "visible_cues": list(proposal["visible_cues"]),
        "visible_action": (
            str(action.get("summary") or action["kind"]) if action["kind"] != "none" else ""
        ),
    }


class ConversationStore:
    """Atomic JSON journal for active, not-yet-authoritative conversations."""

    def __init__(self, root: Path, *, lease_ttl_s: int = 120) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lease_ttl_s = max(10, int(lease_ttl_s))
        self._lock = threading.RLock()
        self._secret = self._load_secret()

    def _load_secret(self) -> bytes:
        path = self.root / ".capability-key"
        if path.exists():
            value = path.read_bytes()
            if len(value) >= 32:
                return value
            raise RuntimeError("NPC conversation capability key is invalid")
        value = secrets.token_bytes(32)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_bytes(value)
        os.replace(temporary, path)
        return value

    def _path(self, conversation_id: str) -> Path:
        if not conversation_id or any(ch not in "0123456789abcdef-" for ch in conversation_id):
            raise ValueError("invalid conversation_id")
        return self.root / f"{conversation_id}.json"

    def _write(self, session: dict[str, Any]) -> None:
        path = self._path(str(session["conversation_id"]))
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        encoded = json.dumps(session, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def get(self, conversation_id: str) -> dict[str, Any]:
        with self._lock:
            path = self._path(conversation_id)
            if not path.is_file():
                raise LookupError(conversation_id)
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise RuntimeError("NPC conversation journal is invalid")
            return value

    def save(self, session: dict[str, Any]) -> None:
        with self._lock:
            self._write(session)

    def delete(self, conversation_id: str) -> None:
        with self._lock:
            path = self._path(conversation_id)
            if path.exists():
                path.unlink()

    def open(
        self,
        *,
        campaign_id: str,
        branch_id: str,
        principal_id: str,
        scope_id: str,
        scene_id: str,
        authority: dict[str, Any],
        participants: list[dict[str, Any]],
        actor_contexts: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        with self._lock:
            participant_ids = [str(item["actor_id"]) for item in participants]
            for path in self.root.glob("*.json"):
                existing = json.loads(path.read_text(encoding="utf-8"))
                if (
                    existing.get("campaign_id") == campaign_id
                    and existing.get("branch_id") == branch_id
                    and existing.get("status") in ACTIVE_CONVERSATION_STATUSES
                    and set(existing.get("participant_ids") or []) & set(participant_ids)
                ):
                    raise ValueError(
                        "an actor is already participating in another active conversation"
                    )
            conversation_id = str(uuid4())
            now_ns = time.time_ns()
            runtimes = {}
            for actor_id, context in actor_contexts.items():
                runtimes[actor_id] = {
                    "actor_runtime_id": f"{conversation_id}:{actor_id}",
                    "actor_id": actor_id,
                    "status": "idle",
                    "inbox_cursor": 0,
                    "working_state_revision": 0,
                    "context": deepcopy(context),
                    "working_deltas": {
                        "facts": [],
                        "actor_knowledge": [],
                        "commitments": [],
                    },
                }
            session = {
                "schema_version": NPC_CONVERSATION_SCHEMA_VERSION,
                "contract": NPC_CONVERSATION_CONTRACT,
                "conversation_id": conversation_id,
                "campaign_id": campaign_id,
                "branch_id": branch_id,
                "principal_id": principal_id,
                "scope_id": scope_id,
                "scene_id": scene_id,
                "status": "open",
                "created_at_ns": now_ns,
                "updated_at_ns": now_ns,
                "authority": deepcopy(authority),
                "participants": deepcopy(participants),
                "participant_ids": participant_ids,
                "actor_runtimes": runtimes,
                "events": [],
                "activations": {},
                "publications": [],
            }
            self._write(session)
            return self.public_status(session)

    @staticmethod
    def public_status(session: dict[str, Any]) -> dict[str, Any]:
        return {
            key: deepcopy(session[key])
            for key in (
                "schema_version",
                "contract",
                "conversation_id",
                "campaign_id",
                "branch_id",
                "scope_id",
                "scene_id",
                "status",
                "created_at_ns",
                "updated_at_ns",
                "authority",
                "participants",
            )
        } | {
            "cursor": len(session["events"]),
            "pending_activation_count": sum(
                item.get("status") in {"pending", "claimed"}
                for item in session["activations"].values()
            ),
            "publication_count": len(session["publications"]),
            "actor_runtimes": [
                {
                    key: deepcopy(runtime[key])
                    for key in (
                        "actor_runtime_id",
                        "actor_id",
                        "status",
                        "inbox_cursor",
                        "working_state_revision",
                    )
                }
                for runtime in session["actor_runtimes"].values()
            ],
        }

    def require_owner(
        self, conversation_id: str, *, campaign_id: str, principal_id: str
    ) -> dict[str, Any]:
        session = self.get(conversation_id)
        if session.get("campaign_id") != campaign_id:
            raise ValueError("conversation belongs to another campaign")
        if session.get("principal_id") != principal_id:
            raise PermissionError("conversation belongs to another principal")
        return session

    def append_event(
        self,
        session: dict[str, Any],
        *,
        event: dict[str, Any],
        perceived_by: list[str],
        understood_by: list[str],
        activate_actor_ids: list[str],
        response_required_actor_ids: set[str],
    ) -> dict[str, Any]:
        if session["status"] == "suspended" and event.get("type") == "resolution":
            session["status"] = "open"
        elif session["status"] != "open":
            raise ValueError(f"conversation is not open: {session['status']}")
        sequence = len(session["events"]) + 1
        saved = {
            "event_id": f"conversation-event:{session['conversation_id']}:{sequence}",
            "sequence": sequence,
            **deepcopy(event),
            "perceived_by": list(perceived_by),
            "understood_by": list(understood_by),
        }
        session["events"].append(saved)
        activations = []
        for actor_id in activate_actor_ids:
            runtime = session["actor_runtimes"].get(actor_id)
            if runtime is None:
                continue
            activation_id = str(uuid4())
            activation = {
                "activation_id": activation_id,
                "actor_runtime_id": runtime["actor_runtime_id"],
                "actor_id": actor_id,
                "reason": (
                    "directly_addressed"
                    if actor_id in response_required_actor_ids
                    else "conversation_observed"
                ),
                "response_required": actor_id in response_required_actor_ids,
                "from_cursor": runtime["inbox_cursor"],
                "to_cursor": sequence,
                "status": "pending",
                "lease": None,
            }
            session["activations"][activation_id] = activation
            activations.append(self._public_activation(session, activation))
        session["updated_at_ns"] = time.time_ns()
        self.save(session)
        return {"event": deepcopy(saved), "activations": activations}

    def _capability(self, session: dict[str, Any], activation: dict[str, Any]) -> str:
        message = ":".join(
            (
                str(session["conversation_id"]),
                str(activation["activation_id"]),
                str(activation["actor_runtime_id"]),
                str(session["principal_id"]),
            )
        )
        return hmac.new(self._secret, message.encode("utf-8"), hashlib.sha256).hexdigest()

    def _public_activation(
        self, session: dict[str, Any], activation: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            key: deepcopy(activation[key])
            for key in (
                "activation_id",
                "actor_runtime_id",
                "actor_id",
                "reason",
                "response_required",
                "from_cursor",
                "to_cursor",
                "status",
            )
        } | {"worker_handle": self._capability(session, activation)}

    def list_activations(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            self._public_activation(session, item)
            for item in session["activations"].values()
            if item["status"] in {"pending", "claimed"}
        ]

    def checkout(
        self,
        session: dict[str, Any],
        *,
        activation_id: str,
        worker_handle: str,
        cursor: int,
        include_bootstrap: bool,
    ) -> dict[str, Any]:
        activation = session["activations"].get(activation_id)
        if activation is None:
            raise LookupError(activation_id)
        if not secrets.compare_digest(worker_handle, self._capability(session, activation)):
            raise PermissionError("invalid actor-scoped worker handle")
        if activation["status"] == "completed":
            raise ValueError("activation is already completed")
        now_ns = time.time_ns()
        current_lease = activation.get("lease")
        if current_lease and int(current_lease.get("expires_at_ns", 0)) > now_ns:
            raise ValueError("activation is already leased")
        lease_id = str(uuid4())
        expires_at_ns = now_ns + self.lease_ttl_s * 1_000_000_000
        activation["lease"] = {"lease_id": lease_id, "expires_at_ns": expires_at_ns}
        activation["status"] = "claimed"
        runtime = session["actor_runtimes"][activation["actor_id"]]
        inbox = [
            deepcopy(event)
            for event in session["events"]
            if int(event["sequence"]) > max(0, int(cursor))
            and activation["actor_id"] in event["perceived_by"]
        ]
        event_basis_refs = [str(item["event_id"]) for item in inbox]
        context = runtime["context"]
        allowed_basis_refs = sorted(
            {
                *(str(item) for item in context["constraints"]["allowed_basis_refs"]),
                *event_basis_refs,
            }
        )
        capsule = {
            "schema_version": 1,
            "contract": NPC_CONVERSATION_CONTRACT,
            "conversation_id": session["conversation_id"],
            "activation_id": activation_id,
            "actor_runtime_id": activation["actor_runtime_id"],
            "actor_id": activation["actor_id"],
            "lease_id": lease_id,
            "lease_expires_at_ns": expires_at_ns,
            "context_manifest": {
                "campaign_id": session["campaign_id"],
                "branch_id": session["branch_id"],
                "actor_revision": context["authority"]["actor_revision"],
                "campaign_revision": context["authority"]["campaign_revision"],
                "working_state_revision": runtime["working_state_revision"],
                "inbox_cursor": len(session["events"]),
            },
            "bootstrap": deepcopy(context) if include_bootstrap else None,
            "working_state": deepcopy(runtime["working_deltas"]),
            "inbox": inbox,
            "constraints": {
                "allowed_basis_refs": allowed_basis_refs,
                "allowed_target_actor_ids": list(session["participant_ids"]),
                "may_call_tools": False,
                "may_roll_dice": False,
                "may_write_state": False,
                "output_contract": "npc-conversation-proposal.v2",
            },
        }
        session["updated_at_ns"] = now_ns
        self.save(session)
        return capsule

    def submit(
        self,
        session: dict[str, Any],
        *,
        activation_id: str,
        worker_handle: str,
        lease_id: str,
        proposal: dict[str, Any],
    ) -> dict[str, Any]:
        activation = session["activations"].get(activation_id)
        if activation is None:
            raise LookupError(activation_id)
        if not secrets.compare_digest(worker_handle, self._capability(session, activation)):
            raise PermissionError("invalid actor-scoped worker handle")
        lease = activation.get("lease") or {}
        if lease.get("lease_id") != lease_id:
            raise PermissionError("invalid activation lease")
        if int(lease.get("expires_at_ns", 0)) <= time.time_ns():
            raise ValueError("activation lease expired")
        runtime = session["actor_runtimes"][activation["actor_id"]]
        allowed_basis_refs = {
            *(str(item) for item in runtime["context"]["constraints"]["allowed_basis_refs"]),
            *(
                str(event["event_id"])
                for event in session["events"]
                if activation["actor_id"] in event["perceived_by"]
            ),
        }
        validate_conversation_proposal(
            proposal,
            conversation_id=session["conversation_id"],
            activation_id=activation_id,
            actor_runtime_id=activation["actor_runtime_id"],
            actor_id=activation["actor_id"],
            allowed_basis_refs=allowed_basis_refs,
            allowed_actor_ids=set(session["participant_ids"]),
        )
        if proposal["resolution_requests"]:
            session["status"] = "suspended"
            activation["status"] = "completed"
            activation["lease"] = None
            session["updated_at_ns"] = time.time_ns()
            self.save(session)
            return {
                "status": "resolution_required",
                "publication": None,
                "resolution_requests": deepcopy(proposal["resolution_requests"]),
            }
        if not proposal["response_bid"]["should_respond"]:
            activation["status"] = "completed"
            activation["lease"] = None
            runtime["inbox_cursor"] = len(session["events"])
            session["updated_at_ns"] = time.time_ns()
            self.save(session)
            return {"status": "passed", "publication": None, "resolution_requests": []}

        publication_id = str(uuid4())
        publication = derive_publication(proposal, publication_id=publication_id)
        session["publications"].append(publication)
        sequence = len(session["events"]) + 1
        targets = {
            item for segment in publication["utterance_segments"] for item in segment["targets"]
        }
        perceived_by = list(session["participant_ids"])
        session["events"].append(
            {
                "event_id": f"conversation-event:{session['conversation_id']}:{sequence}",
                "sequence": sequence,
                "type": "npc_publication",
                "speaker_actor_id": activation["actor_id"],
                "publication_id": publication_id,
                "content": publication["speech"],
                "utterance_segments": deepcopy(publication["utterance_segments"]),
                "visible_cues": deepcopy(publication["visible_cues"]),
                "visible_action": publication["visible_action"],
                "perceived_by": perceived_by,
                "understood_by": perceived_by,
            }
        )
        for field, values in proposal["working_deltas"].items():
            runtime["working_deltas"][field].extend(deepcopy(values))
        if any(proposal["working_deltas"].values()):
            runtime["working_state_revision"] += 1
        runtime["inbox_cursor"] = sequence
        activation["status"] = "completed"
        activation["lease"] = None

        followups = []
        for target_actor_id in sorted(targets - {activation["actor_id"]}):
            target_runtime = session["actor_runtimes"].get(target_actor_id)
            if target_runtime is None:
                continue
            followup_id = str(uuid4())
            followup = {
                "activation_id": followup_id,
                "actor_runtime_id": target_runtime["actor_runtime_id"],
                "actor_id": target_actor_id,
                "reason": "directly_addressed",
                "response_required": True,
                "from_cursor": target_runtime["inbox_cursor"],
                "to_cursor": sequence,
                "status": "pending",
                "lease": None,
            }
            session["activations"][followup_id] = followup
            followups.append(self._public_activation(session, followup))
        session["updated_at_ns"] = time.time_ns()
        self.save(session)
        return {
            "status": "published",
            "publication": publication,
            "resolution_requests": [],
            "followup_activations": followups,
        }
