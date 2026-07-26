"""Versioned public-tool and schema budgets for the compact MCP contract."""

from __future__ import annotations

TOOL_BUDGET_VERSION = "2026-07-compact-facades-v1"

# Captured before the conservative facade consolidation.  Keep this historical
# baseline so a lower tool count cannot conceal a larger aggregate input schema.
BASELINE_PUBLIC_TOOL_COUNT = 92
BASELINE_INPUT_SCHEMA_BYTES = 56_611

TARGET_PUBLIC_TOOL_COUNT = 82
TARGET_CORE_TOOL_COUNT = 12
TARGET_INPUT_SCHEMA_BYTES = 47_617
PROFILE_TOOL_LIMITS = {
    "lobby": 61,
    "play": 46,
    "combat": 44,
}
