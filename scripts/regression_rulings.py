"""Shared structured handoff for unresolved public-tool adjudications."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


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
        normalized = deepcopy(dict(ruling))
        normalized.setdefault("status", "pending_ruling")
        normalized.setdefault("default_resolver", "agent")
        normalized.setdefault("ruling_kind", "agent_dm_adjudication")
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
