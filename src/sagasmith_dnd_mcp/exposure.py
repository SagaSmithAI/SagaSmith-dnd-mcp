"""Session-scoped progressive tool exposure for the D&D MCP server.

This is intentionally independent of a particular agent framework. Native MCP
clients receive a filtered ``tools/list`` response, while clients that cannot
refresh tool schemas can use ``exposure_call`` as a protocol-preserving
fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .tool_profiles import CORE_TOOLS, GROUP_BY_ID, TOOL_GROUPS


class ExposureError(ValueError):
    """Raised when a session attempts to use an unexposed capability."""


@dataclass
class Exposure:
    id: str
    session_key: str
    principal_id: str
    campaign_id: str | None
    phase: str
    loaded_groups: set[str] = field(default_factory=set)
    remaining_calls: dict[str, int | None] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ExposureRegistry:
    """Own session exposure state; storage and agent prompts never own it."""

    def __init__(self) -> None:
        self._by_id: dict[str, Exposure] = {}
        self._active_by_session: dict[str, str] = {}

    def open(
        self,
        *,
        session_key: str,
        principal_id: str,
        campaign_id: str | None,
        phase: str,
    ) -> Exposure:
        prior_id = self._active_by_session.get(session_key)
        if prior_id:
            self._by_id.pop(prior_id, None)
        exposure = Exposure(
            id=f"exp_{uuid4().hex}",
            session_key=session_key,
            principal_id=principal_id,
            campaign_id=campaign_id,
            phase=phase,
        )
        self._by_id[exposure.id] = exposure
        self._active_by_session[session_key] = exposure.id
        return exposure

    def get(self, exposure_id: str, session_key: str | None = None) -> Exposure:
        exposure = self._by_id.get(exposure_id)
        if exposure is None:
            raise ExposureError("Unknown or expired exposure_id.")
        if session_key is not None and exposure.session_key != session_key:
            raise ExposureError("exposure_id belongs to another MCP session.")
        return exposure

    def active(self, session_key: str) -> Exposure | None:
        exposure_id = self._active_by_session.get(session_key)
        return self._by_id.get(exposure_id) if exposure_id else None

    def refresh_phase(self, exposure: Exposure, phase: str) -> bool:
        if exposure.phase == phase:
            return False
        exposure.phase = phase
        # A phase transition must never carry writable combat/lobby tools over.
        exposure.loaded_groups = {
            group_id for group_id in exposure.loaded_groups if GROUP_BY_ID[group_id].phase == phase
        }
        exposure.remaining_calls = {
            group_id: calls
            for group_id, calls in exposure.remaining_calls.items()
            if group_id in exposure.loaded_groups
        }
        exposure.updated_at = datetime.now(UTC)
        return True

    def load(self, exposure: Exposure, group_id: str, ttl_calls: int | None = None) -> Exposure:
        group = GROUP_BY_ID.get(group_id)
        if group is None:
            raise ExposureError(f"Unknown tool group: {group_id}")
        if group.phase != exposure.phase:
            raise ExposureError(
                f"Tool group {group_id!r} is valid only during {group.phase!r}; "
                f"this session is in {exposure.phase!r}."
            )
        if ttl_calls is not None and ttl_calls < 1:
            raise ExposureError("ttl_calls must be at least 1 when provided.")
        exposure.loaded_groups.add(group_id)
        exposure.remaining_calls[group_id] = ttl_calls
        exposure.updated_at = datetime.now(UTC)
        return exposure

    def unload(self, exposure: Exposure, group_id: str) -> Exposure:
        exposure.loaded_groups.discard(group_id)
        exposure.remaining_calls.pop(group_id, None)
        exposure.updated_at = datetime.now(UTC)
        return exposure

    def visible_tools(self, exposure: Exposure | None) -> set[str]:
        if exposure is None:
            return set(CORE_TOOLS)
        tools = set(CORE_TOOLS)
        for group_id in exposure.loaded_groups:
            tools.update(GROUP_BY_ID[group_id].tools)
        return tools

    def require_tool(self, exposure: Exposure, tool_id: str) -> None:
        if tool_id in CORE_TOOLS:
            return
        groups = [
            group_id
            for group_id in exposure.loaded_groups
            if tool_id in GROUP_BY_ID[group_id].tools
        ]
        if not groups:
            raise ExposureError(
                f"Tool {tool_id!r} is not exposed for this session. "
                "Use exposure_search and exposure_load first."
            )
        for group_id in groups:
            remaining = exposure.remaining_calls.get(group_id)
            if remaining is None:
                continue
            if remaining <= 0:
                self.unload(exposure, group_id)
                continue
            return

    def consume_tool(self, exposure: Exposure, tool_id: str) -> bool:
        """Consume one TTL unit after a successful non-core invocation."""
        changed = False
        for group_id in tuple(exposure.loaded_groups):
            if tool_id not in GROUP_BY_ID[group_id].tools:
                continue
            remaining = exposure.remaining_calls.get(group_id)
            if remaining is None:
                continue
            changed = True
            if remaining <= 1:
                self.unload(exposure, group_id)
            else:
                exposure.remaining_calls[group_id] = remaining - 1
                exposure.updated_at = datetime.now(UTC)
            break
        return changed

    def status(self, exposure: Exposure) -> dict[str, Any]:
        return {
            "exposure_id": exposure.id,
            "campaign_id": exposure.campaign_id,
            "principal_id": exposure.principal_id,
            "phase": exposure.phase,
            "loaded_groups": sorted(exposure.loaded_groups),
            "visible_tools": sorted(self.visible_tools(exposure)),
            "created_at": exposure.created_at.isoformat(),
            "updated_at": exposure.updated_at.isoformat(),
        }

    def search(self, query: str, phase: str | None = None) -> list[dict[str, Any]]:
        terms = {term.lower() for term in query.split() if term.strip()}
        candidates = [group for group in TOOL_GROUPS if phase is None or group.phase == phase]
        scored: list[tuple[int, Any]] = []
        for group in candidates:
            haystack = " ".join((group.id, group.title, group.description, *group.tools)).lower()
            score = sum(term in haystack for term in terms)
            if score:
                scored.append((score, group))
        if not scored and not terms:
            scored = [(0, group) for group in candidates]
        return [
            {
                "id": group.id,
                "phase": group.phase,
                "title": group.title,
                "description": group.description,
                "risk": group.risk,
            }
            for _, group in sorted(scored, key=lambda item: (-item[0], item[1].id))
        ]

    def inspect(self, group_id: str) -> dict[str, Any]:
        group = GROUP_BY_ID.get(group_id)
        if group is None:
            raise ExposureError(f"Unknown tool group: {group_id}")
        return {
            "id": group.id,
            "phase": group.phase,
            "title": group.title,
            "description": group.description,
            "risk": group.risk,
            "tools": sorted(group.tools),
        }
