"""Versioned public-tool and schema budgets for the compact MCP contract."""

from __future__ import annotations

TOOL_BUDGET_VERSION = "2026-08-architecture-simplification-v1"

# Captured before facade consolidation. Tool counts are exact interface
# contracts; schema bytes are bounded because harmless serializer and
# description changes must not require a new historical byte fingerprint.
BASELINE_PUBLIC_TOOL_COUNT = 92
BASELINE_INPUT_SCHEMA_BYTES = 56_611

TARGET_PUBLIC_TOOL_COUNT = 85
TARGET_CORE_TOOL_COUNT = 13
MAX_INPUT_SCHEMA_BYTES = BASELINE_INPUT_SCHEMA_BYTES

PROFILE_TOOL_LIMITS = {
    "lobby": 64,
    "play": 50,
    "combat": 49,
}
