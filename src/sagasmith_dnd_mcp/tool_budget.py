"""Versioned public-tool and schema budgets for the compact MCP contract."""

from __future__ import annotations

TOOL_BUDGET_VERSION = "2026-07-phase-skill-plans-v16"

# Captured before the conservative facade consolidation.  Keep this historical
# baseline so a lower tool count cannot conceal a larger aggregate input schema.
BASELINE_PUBLIC_TOOL_COUNT = 92
BASELINE_INPUT_SCHEMA_BYTES = 56_611

TARGET_PUBLIC_TOOL_COUNT = 83
TARGET_CORE_TOOL_COUNT = 13
# Combat transaction-history and receipt views make interrupted multi-call Agent
# rulings publicly recoverable without another tool or direct storage access.
# Version 5 adds 181 bytes to enumerate every legal combat_common_action value,
# including the ordinary object-interaction budget, instead of accepting an
# arbitrary string. Version 6 adds 14 bytes for the generic, action-bound
# combat_hp_change(save_damage) settlement. Version 7 adds 15 bytes for the
# source-bound combat_choice(execute_plan) action without another public tool.
# Version 8 adds the first-use combat_choice(compile_solution) action while
# keeping the same public and core tool counts.
# Version 9 deliberately adds one DM-only, cross-phase content_solution tool.
# This keeps reusable recipe compilation separate from character replacement
# and from paid combat settlement.
# Version 10 adds the hard-standard sustain_spell action to the existing common
# action facade; no public or core tool is added.
# Version 11 adds the hard-standard resolve_death_trigger action to the existing
# combat choice facade. The 24-byte schema increase preserves the same public
# and core tool counts while keeping server saves and scene facts auditable.
# Version 12 adds related_refs to continuity_context so the Agent can receive
# exact, non-executable module evidence without another public tool.
# Version 13 adds the source-exact shake_hypnotic_pattern action to the existing
# common-action facade. The 25-byte increase adds no public or core tool.
# Version 14 adds text-only module statblock recovery to the existing review
# facade. The 20-byte increase adds no public or core tool. Version 15 promotes
# the existing read-only skill_query facade into the cold-start core so a host
# can discover compact workflow guidance before a campaign exists. It also adds
# bounded contract inspection and the campaign resume selector without another
# public tool. Version 16 adds the phase/tool-group Skill-plan selector and
# trusted campaign/exposure context to the existing core skill_query facade.
# The aggregate remains well below the captured 92-tool baseline.
TARGET_INPUT_SCHEMA_BYTES = 50_171
PROFILE_TOOL_LIMITS = {
    "lobby": 62,
    "play": 48,
    "combat": 46,
}
