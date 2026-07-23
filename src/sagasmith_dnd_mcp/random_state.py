"""Atomic persistence adapter for D&D campaign random streams."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from sagasmith_core import CampaignService
from sagasmith_core import StateMutationService as CoreStateMutationService
from sagasmith_dnd.character_schema import validate_party_state
from sagasmith_dnd.random_stream import active_random_stream


class RandomStateMutationService(CoreStateMutationService):
    """Persist active random-stream progress with the domain mutation that used it."""

    def replace(
        self,
        campaign_id: str,
        *,
        campaign_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
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

