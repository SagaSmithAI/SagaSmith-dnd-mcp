"""Versioned public-tool and schema budgets for the compact MCP contract."""

from __future__ import annotations

TOOL_BUDGET_VERSION = "2026-08-portable-monster-semantics-v32"

# Captured before the conservative facade consolidation.  Keep this historical
# baseline so a lower tool count cannot conceal a larger aggregate input schema.
BASELINE_PUBLIC_TOOL_COUNT = 92
BASELINE_INPUT_SCHEMA_BYTES = 56_611

TARGET_PUBLIC_TOOL_COUNT = 85
TARGET_CORE_TOOL_COUNT = 13
# Combat transaction-history and receipt views make interrupted multi-call Agent
# rulings publicly recoverable without another tool or direct storage access.
# Version 5 adds 181 bytes to enumerate every legal combat_common_action value,
# including the ordinary object-interaction budget, instead of accepting an
# arbitrary string. Version 6 adds 14 bytes for the generic, action-bound
# combat_hp_change(save_damage) settlement. Version 7 adds 15 bytes for the
# source-bound combat_choice(execute_plan) action without another public tool.
# Version 8's paid-use solution-authoring action is retired; portable addon
# resolution is now completed before release.
# Version 9 added the DM-only content_solution tool. Custom cards may now be
# compiled at first use in Play or Combat, while the persisted source-bound plan
# remains the only executable path.
# Version 10 adds the hard-standard sustain_spell action to the existing common
# action facade; no public or core tool is added.
# Version 11 formerly added a creature-specific death-trigger selector. That
# selector is retired; custom death triggers now use source-bound plans. Version
# 32 also retires the creature-specific detach selector and legacy combat-cleanup
# manifest action. Their three enum entries remove 61 bytes from version 30's
# 53,441-byte aggregate without changing any public/core tool count.
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
# Version 17 adds strict NPC-turn context selectors to continuity_context and
# makes that existing tool available during Combat; no public/core tool is
# added. Version 18 adds one strict bounded_evaluation validation facade for
# actor, audience, faction, source, and ruling proposals. Context creation stays
# in continuity_context, and the aggregate remains below the 92-tool baseline.
# Version 19 adds the read-only campaign_query(binding) selector used by generic
# hosts to refresh principal/role/audience/branch context before replaying any
# conversation history. It adds no public or core tool.
# Version 20 makes legacy top-level memory identity fields explicitly optional,
# preserving the difference between omitted fields and an attempted identity
# change during memory_change(upsert). Version 22 adds portable actor-card and
# module-package actions to five existing facades. Public/core tool counts and
# phase limits stay fixed; the 97-byte schema increase remains below baseline.
# Version 23 adds rule_import(import_package/inspect_release) plus package and
# release views to existing facades. Versions 24-25 add self-contained addon
# import, export, inspection, and branch activation to the same compact
# facades. The 60-byte schema increase adds no public/core tool and keeps the
# aggregate below the pre-consolidation baseline. Version 26 adds the actor
# preset-package view to the existing rule-pack query facade, so normalized
# creature cards can travel inside an addon without another public tool.
# Version 27 adds the batch statblock-recovery action to rule_import. It reuses
# the existing facade and adds 21 bytes without changing any tool/profile count.
# Version 28 makes an omitted rule_seed_bundled limit mean complete coverage;
# the nullable compatibility parameter adds 30 bytes and no public tool.
# Version 30 adds one DM-only, phase-safe addon actor materializer.  Its strict
# typed inputs derive owner statistics server-side and keep the aggregate below
# the historical 92-tool/56,611-byte baseline.
TARGET_INPUT_SCHEMA_BYTES = 53_380
PROFILE_TOOL_LIMITS = {
    "lobby": 64,
    "play": 50,
    "combat": 49,
}
