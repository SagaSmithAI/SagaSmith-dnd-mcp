"""Atomic persistence adapter for D&D campaign random streams."""

from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sagasmith_core import CampaignService
from sagasmith_core import StateMutationService as CoreStateMutationService
from sagasmith_core.idempotency import request_hash
from sagasmith_dnd.character_schema import validate_party_state
from sagasmith_dnd.random_stream import active_random_stream


@dataclass(frozen=True)
class PendingIdempotencyRequest:
    campaign_id: str
    branch_id: str | None
    key: str
    request_hash: str


_PENDING_IDEMPOTENCY_REQUEST: ContextVar[PendingIdempotencyRequest | None] = ContextVar(
    "sagasmith_dnd_mcp_pending_idempotency_request",
    default=None,
)


def bind_idempotency_request(
    campaign_id: str,
    branch_id: str | None,
    key: str,
    payload: Any,
) -> None:
    """Bind the public request digest to the next mutation in this call context."""

    _PENDING_IDEMPOTENCY_REQUEST.set(
        PendingIdempotencyRequest(
            campaign_id=campaign_id,
            branch_id=branch_id,
            key=key,
            request_hash=request_hash(payload),
        )
    )


class RandomStateMutationService(CoreStateMutationService):
    """Persist random progress and the public retry digest with one mutation."""

    def replace(
        self,
        campaign_id: str,
        *,
        campaign_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        idempotency_key = kwargs.get("idempotency_key")
        branch_id = kwargs.get("branch_id")
        pending = _PENDING_IDEMPOTENCY_REQUEST.get()
        bound_public_request = (
            idempotency_key
            and pending is not None
            and pending.campaign_id == campaign_id
            and pending.key == idempotency_key
            and (branch_id is None or branch_id == pending.branch_id)
        )
        if bound_public_request:
            if kwargs.get("idempotency_write") is None:
                raise RuntimeError(
                    "public state mutations must persist their exact replay response "
                    "in the owning transaction"
                )
            kwargs["idempotency_request_hash"] = pending.request_hash
        stream = active_random_stream()
        should_persist = (
            stream is not None
            and stream.campaign_id == campaign_id
            and stream.has_unpersisted_draws
        )
        if should_persist:
            source_state = (
                deepcopy(campaign_state)
                if campaign_state is not None
                else deepcopy(CampaignService(self.database).get(campaign_id).state)
            )
            source_state["random_stream"] = stream.persisted_state()
            campaign_state = validate_party_state(source_state)
        result = super().replace(
            campaign_id,
            campaign_state=campaign_state,
            **kwargs,
        )
        if should_persist:
            stream.mark_persisted()
        return result
