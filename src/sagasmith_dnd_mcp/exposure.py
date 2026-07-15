"""Session-scoped progressive tool exposure for the D&D MCP server.

This is intentionally independent of a particular agent framework. Native MCP
clients receive a filtered ``tools/list`` response, while clients that cannot
refresh tool schemas can use ``exposure_call`` as a protocol-preserving
fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
    expires_at: datetime = field(default_factory=lambda: datetime.now(UTC) + timedelta(hours=12))


class ExposureRegistry:
    """Own session exposure state; storage and agent prompts never own it."""

    def __init__(self, *, ttl: timedelta = timedelta(hours=12)) -> None:
        self._by_id: dict[str, Exposure] = {}
        self._active_by_session: dict[str, str] = {}
        self._ttl = ttl

    def _prune(self) -> None:
        now = datetime.now(UTC)
        expired = [
            exposure_id for exposure_id, item in self._by_id.items() if item.expires_at <= now
        ]
        for exposure_id in expired:
            exposure = self._by_id.pop(exposure_id)
            if self._active_by_session.get(exposure.session_key) == exposure_id:
                self._active_by_session.pop(exposure.session_key, None)

    def touch(self, exposure: Exposure) -> Exposure:
        now = datetime.now(UTC)
        exposure.updated_at = now
        exposure.expires_at = now + self._ttl
        return exposure

    def open(
        self,
        *,
        session_key: str,
        principal_id: str,
        campaign_id: str | None,
        phase: str,
    ) -> Exposure:
        self._prune()
        prior_id = self._active_by_session.get(session_key)
        if prior_id:
            self._by_id.pop(prior_id, None)
        exposure = Exposure(
            id=f"exp_{uuid4().hex}",
            session_key=session_key,
            principal_id=principal_id,
            campaign_id=campaign_id,
            phase=phase,
            expires_at=datetime.now(UTC) + self._ttl,
        )
        self._by_id[exposure.id] = exposure
        self._active_by_session[session_key] = exposure.id
        return exposure

    def get(self, exposure_id: str, session_key: str | None = None) -> Exposure:
        self._prune()
        exposure = self._by_id.get(exposure_id)
        if exposure is None:
            raise ExposureError("Unknown or expired exposure_id.")
        if session_key is not None and exposure.session_key != session_key:
            raise ExposureError("exposure_id belongs to another MCP session.")
        return self.touch(exposure)

    def active(self, session_key: str) -> Exposure | None:
        self._prune()
        exposure_id = self._active_by_session.get(session_key)
        exposure = self._by_id.get(exposure_id) if exposure_id else None
        return self.touch(exposure) if exposure else None

    def for_campaign(self, campaign_id: str) -> tuple[Exposure, ...]:
        self._prune()
        return tuple(item for item in self._by_id.values() if item.campaign_id == campaign_id)

    def active_items(self, campaign_id: str | None = None) -> tuple[tuple[str, Exposure], ...]:
        self._prune()
        items: list[tuple[str, Exposure]] = []
        for session_key, exposure_id in self._active_by_session.items():
            exposure = self._by_id.get(exposure_id)
            if exposure is not None and (
                campaign_id is None or exposure.campaign_id == campaign_id
            ):
                items.append((session_key, exposure))
        return tuple(items)

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
        self.touch(exposure)
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
        if group.requires_campaign and exposure.campaign_id is None:
            raise ExposureError(
                f"Tool group {group_id!r} requires a campaign-bound exposure. "
                "Open a new exposure with campaign_id first."
            )
        if group.local_only and exposure.principal_id != "system:local":
            raise ExposureError(f"Tool group {group_id!r} is restricted to system:local.")
        if ttl_calls is not None and ttl_calls < 1:
            raise ExposureError("ttl_calls must be at least 1 when provided.")
        exposure.loaded_groups.add(group_id)
        exposure.remaining_calls[group_id] = ttl_calls
        self.touch(exposure)
        return exposure

    def unload(self, exposure: Exposure, group_id: str) -> Exposure:
        exposure.loaded_groups.discard(group_id)
        exposure.remaining_calls.pop(group_id, None)
        self.touch(exposure)
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
        valid_groups: list[str] = []
        for group_id in sorted(groups):
            remaining = exposure.remaining_calls.get(group_id)
            if remaining is None:
                valid_groups.append(group_id)
                continue
            if remaining <= 0:
                self.unload(exposure, group_id)
                continue
            valid_groups.append(group_id)
        if not valid_groups:
            raise ExposureError(
                f"Tool {tool_id!r} has no remaining exposure calls. "
                "Load its capability group again."
            )

    def consume_tool(self, exposure: Exposure, tool_id: str) -> bool:
        """Consume one TTL unit after a successful non-core invocation."""
        changed = False
        matching = sorted(
            group_id
            for group_id in exposure.loaded_groups
            if tool_id in GROUP_BY_ID[group_id].tools
        )
        if any(exposure.remaining_calls.get(group_id) is None for group_id in matching):
            self.touch(exposure)
            return False
        for group_id in matching:
            remaining = exposure.remaining_calls.get(group_id)
            changed = True
            if remaining <= 1:
                self.unload(exposure, group_id)
            else:
                exposure.remaining_calls[group_id] = remaining - 1
                self.touch(exposure)
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
            "expires_at": exposure.expires_at.isoformat(),
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
                "requires_campaign": group.requires_campaign,
                "local_only": group.local_only,
                "roles": sorted(group.roles),
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
            "requires_campaign": group.requires_campaign,
            "local_only": group.local_only,
            "roles": sorted(group.roles),
            "tools": sorted(group.tools),
        }
