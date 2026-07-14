"""MCP capabilities required by the full D&D skill workflow."""

from __future__ import annotations

FULL_SKILL_CAPABILITIES: dict[str, frozenset[str]] = {
    "campaign": frozenset(
        {
            "campaign_create",
            "campaign_get",
            "campaign_list",
            "campaign_member_grant",
            "actor_grant",
            "server_capabilities",
        }
    ),
    "characters": frozenset(
        {
            "character_ability_apply",
            "character_build",
            "character_create",
            "character_get",
            "character_instantiate",
            "character_inventory_add",
            "character_inventory_equip",
            "character_inventory_remove",
            "character_inventory_transfer",
            "character_inventory_update",
            "character_library_list",
            "character_list",
            "character_memory_add",
            "character_memory_resolve",
            "character_resource_set",
            "character_sheet_replace",
            "character_spell_prepare",
            "character_wallet_adjust",
            "party_inventory_add",
            "party_inventory_remove",
            "party_inventory_transfer",
            "party_show",
            "party_wallet_adjust",
            "party_wallet_transfer",
        }
    ),
    "continuity": frozenset(
        {
            "actor_knowledge_add",
            "actor_knowledge_list",
            "actor_knowledge_revise",
            "actor_knowledge_search",
            "branch_checkout",
            "branch_create",
            "branch_list",
            "branch_compare",
            "continuity_context",
            "event_add",
            "event_list",
            "memory_add",
            "memory_list",
            "memory_search",
        }
    ),
    "modules": frozenset(
        {
            "module_current",
            "module_expand",
            "module_import",
            "module_index",
            "module_inspect",
            "module_list",
            "module_read_scene",
            "module_search",
            "module_set_progress",
            "module_write",
        }
    ),
    "rules_and_rolls": frozenset(
        {
            "dnd_ability_roll",
            "dnd_check",
            "dnd_dice_roll",
            "rule_expand",
            "rule_ingest",
            "rule_search",
            "rule_seed_status",
            "rule_seed_bundled",
        }
    ),
    "snapshots_and_audit": frozenset(
        {
            "snapshot_create",
            "snapshot_lineage",
            "snapshot_list",
            "snapshot_regenerate_recap",
            "snapshot_restore",
            "snapshot_verify",
            "state_history",
            "state_redo",
            "state_undo",
        }
    ),
}


def required_tool_names() -> frozenset[str]:
    """Return the stable tool contract the MCP-first full skill relies on."""
    return frozenset().union(*FULL_SKILL_CAPABILITIES.values())
