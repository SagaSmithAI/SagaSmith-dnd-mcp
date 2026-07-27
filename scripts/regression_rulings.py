"""Shared structured handoff for unresolved public-tool adjudications."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

EXTERNAL_RULING_KINDS = frozenset(
    {
        "player_owned_choice",
        "owner_approval",
        "permission_escalation",
        "missing_or_conflicting_source_review",
    }
)
AGENT_RULING_KINDS = (
    "agent_dm_adjudication",
    "source_or_scene_fact",
    "descriptive_activity",
    "generic_spell_effect",
    "ready_release_effect",
    "environmental_consequence",
    "module_specific_procedure",
)


def pending_ruling_kind(ruling: dict[str, Any]) -> str:
    """Classify every nested requirement, with true external boundaries taking priority."""

    kinds: list[str] = []
    direct_kind = str(ruling.get("ruling_kind") or "")
    if direct_kind:
        kinds.append(direct_kind)
    for field in ("pending", "ruling_requirements"):
        kinds.extend(
            str(item.get("ruling_kind") or "")
            for item in ruling.get(field, [])
            if isinstance(item, dict) and str(item.get("ruling_kind") or "")
        )
    requirement = ruling.get("ruling_requirement")
    if isinstance(requirement, dict) and str(requirement.get("ruling_kind") or ""):
        kinds.append(str(requirement["ruling_kind"]))
    external = next((kind for kind in kinds if kind in EXTERNAL_RULING_KINDS), "")
    if external:
        return external
    return next(
        (kind for kind in kinds if kind in AGENT_RULING_KINDS),
        direct_kind or "agent_dm_adjudication",
    )


def normalize_pending_ruling(ruling: dict[str, Any]) -> dict[str, Any]:
    """Default unclassified DM work to the Agent without erasing exceptions."""

    normalized = deepcopy(dict(ruling))
    normalized.setdefault("status", "pending_ruling")
    normalized["ruling_kind"] = pending_ruling_kind(normalized)
    normalized["default_resolver"] = (
        "external_input"
        if normalized["ruling_kind"] in EXTERNAL_RULING_KINDS
        else "agent"
    )
    return normalized


class RegressionRulingRequiredError(RuntimeError):
    """Preserve a public ruling response for the acting Agent or external owner."""

    def __init__(
        self,
        ruling: dict[str, Any],
        *,
        operation: str,
        context: dict[str, Any] | None = None,
        retry_hint: str = "",
    ) -> None:
        normalized = normalize_pending_ruling(ruling)
        self.requirement = {
            "operation": str(operation),
            "context": deepcopy(context or {}),
            "ruling": normalized,
            **({"retry_hint": str(retry_hint)} if retry_hint else {}),
        }
        reason = str(normalized.get("reason") or "adjudication is required")
        resolver = str(normalized["default_resolver"])
        super().__init__(f"{operation} returns to {resolver}: {reason}")


def raise_for_pending_ruling(
    result: dict[str, Any],
    *,
    operation: str,
    context: dict[str, Any] | None = None,
    retry_hint: str = "",
) -> None:
    """Raise only for a live ruling boundary, retaining its typed ownership."""

    if result.get("status") != "pending_ruling":
        return
    raise RegressionRulingRequiredError(
        result,
        operation=operation,
        context=context,
        retry_hint=retry_hint,
    )


def ruling_requirements_from_error(error: BaseException) -> list[dict[str, Any]]:
    """Flatten ExceptionGroup leaves without discarding structured rulings."""

    nested = getattr(error, "exceptions", ())
    if nested:
        return [
            requirement
            for child in nested
            for requirement in ruling_requirements_from_error(child)
        ]
    if isinstance(error, RegressionRulingRequiredError):
        return [deepcopy(error.requirement)]
    return []


def ruling_failure_fields(error: BaseException) -> dict[str, Any]:
    """Build the machine-readable CLI fields that return control to the owner."""

    requirements = ruling_requirements_from_error(error)
    if not requirements:
        return {}
    resolvers = {
        str(dict(requirement.get("ruling") or {}).get("default_resolver") or "agent")
        for requirement in requirements
    }
    return {
        "status": "pending_ruling",
        "default_resolver": "agent" if resolvers == {"agent"} else "external_input",
        "ruling_requirements": requirements,
    }
