"""MCP capabilities required by the full D&D skill workflow."""

from __future__ import annotations

FULL_SKILL_CAPABILITIES: dict[str, frozenset[str]] = {
    "campaign": frozenset(
        {
            "campaign_create",
            "campaign_query",
            "campaign_change",
            "access_grant",
            "game_phase",
            "server_capabilities",
        }
    ),
    "characters": frozenset(
        {
            "character_ability_apply",
            "character_create_from",
            "character_query",
            "character_metadata_update",
            "character_sheet_replace",
            "character_spell_prepare",
            "character_state_change",
            "character_action",
            "character_content_apply",
            "inventory_change",
            "inventory_transfer",
            "wallet_change",
        }
    ),
    "continuity": frozenset(
        {
            "actor_knowledge_change",
            "actor_knowledge_query",
            "branch_change",
            "branch_query",
            "continuity_context",
            "continuity_commit",
            "campaign_event",
            "memory_change",
            "memory_query",
        }
    ),
    "modules": frozenset(
        {
            "module_expand",
            "module_import",
            "module_page_render",
            "module_content_review",
            "module_query",
            "module_search",
            "module_set_progress",
        }
    ),
    "rules_and_rolls": frozenset(
        {
            "dnd_ability_roll",
            "dnd_check",
            "dnd_dice_roll",
            "rule_expand",
            "rule_import",
            "rule_search",
            "rule_seed_status",
            "rule_seed_bundled",
        }
    ),
    "snapshots_and_audit": frozenset(
        {
            "snapshot_create",
            "snapshot_query",
            "snapshot_restore",
            "state_revision",
        }
    ),
}


def required_tool_names() -> frozenset[str]:
    """Return the stable tool contract the MCP-first full skill relies on."""
    return frozenset().union(*FULL_SKILL_CAPABILITIES.values())
