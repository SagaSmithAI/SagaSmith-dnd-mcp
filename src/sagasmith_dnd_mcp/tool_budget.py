"""Versioned public-tool and schema budgets for the compact MCP contract."""

from __future__ import annotations

TOOL_BUDGET_VERSION = "2026-07-compact-facades-v3"

# Captured before the conservative facade consolidation.  Keep this historical
# baseline so a lower tool count cannot conceal a larger aggregate input schema.
BASELINE_PUBLIC_TOOL_COUNT = 92
BASELINE_INPUT_SCHEMA_BYTES = 56_611

TARGET_PUBLIC_TOOL_COUNT = 82
TARGET_CORE_TOOL_COUNT = 12
# The campaign Rule Profile now owns low-level ability-roll edition selection.
# Retaining a blank compatibility argument instead of a second default reduced
# the compact facade schema by four bytes.
TARGET_INPUT_SCHEMA_BYTES = 47_558
PROFILE_TOOL_LIMITS = {
    "lobby": 61,
    "play": 46,
    "combat": 44,
}
