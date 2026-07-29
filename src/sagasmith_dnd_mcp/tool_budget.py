"""Versioned public-tool and schema budgets for the compact MCP contract."""

from __future__ import annotations

TOOL_BUDGET_VERSION = "2026-07-compact-facades-v8"

# Captured before the conservative facade consolidation.  Keep this historical
# baseline so a lower tool count cannot conceal a larger aggregate input schema.
BASELINE_PUBLIC_TOOL_COUNT = 92
BASELINE_INPUT_SCHEMA_BYTES = 56_611

TARGET_PUBLIC_TOOL_COUNT = 82
TARGET_CORE_TOOL_COUNT = 12
# Combat transaction-history and receipt views make interrupted multi-call Agent
# rulings publicly recoverable without another tool or direct storage access.
# Version 5 adds 181 bytes to enumerate every legal combat_common_action value,
# including the ordinary object-interaction budget, instead of accepting an
# arbitrary string. Version 6 adds 14 bytes for the generic, action-bound
# combat_hp_change(save_damage) settlement. Version 7 adds 15 bytes for the
# source-bound combat_choice(execute_plan) action without another public tool.
# Version 8 adds the first-use combat_choice(compile_solution) action while
# keeping the same public and core tool counts.
# The aggregate remains well below the captured 92-tool baseline.
TARGET_INPUT_SCHEMA_BYTES = 47_990
PROFILE_TOOL_LIMITS = {
    "lobby": 61,
    "play": 46,
    "combat": 44,
}
