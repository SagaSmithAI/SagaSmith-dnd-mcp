"""Authoritative MCP tool visibility profiles for authoring and live play."""

from __future__ import annotations

from collections.abc import Iterable

PROFILE_AUTHORING = "authoring"
PROFILE_PLAY = "play"
PROFILE_COMBAT = "combat"
PROFILES = (PROFILE_AUTHORING, PROFILE_PLAY, PROFILE_COMBAT)

ALWAYS_TOOLS = {
    "storage_status",
    "server_capabilities",
    "server_tool_profiles",
    "system_list",
    "campaign_list",
    "campaign_get",
    "game_phase_get",
    "game_phase_set",
    "character_list",
    "character_get",
    "party_show",
    "branch_list",
    "snapshot_list",
    "snapshot_verify",
    "state_history",
    "continuity_context",
    "rule_search",
    "rule_expand",
    "skill_list",
    "skill_read",
    "skill_asset_list",
    "skill_asset_read",
}

AUTHORING_TOOLS = ALWAYS_TOOLS | {
    "storage_migrate",
    "rule_seed_status",
    "rule_seed_bundled",
    "campaign_create",
    "campaign_member_grant",
    "actor_grant",
    "campaign_update",
    "branch_compare",
    "branch_create",
    "branch_checkout",
    "snapshot_create",
    "snapshot_restore",
    "snapshot_lineage",
    "snapshot_regenerate_recap",
    "character_create",
    "character_library_list",
    "character_instantiate",
    "character_build",
    "character_sheet_replace",
    "character_inventory_add",
    "character_inventory_update",
    "character_inventory_remove",
    "character_inventory_equip",
    "character_ability_apply",
    "character_spell_prepare",
    "character_spell_prepare_list",
    "character_update",
    "memory_add",
    "memory_list",
    "memory_search",
    "event_add",
    "event_list",
    "actor_knowledge_add",
    "actor_knowledge_revise",
    "actor_knowledge_list",
    "actor_knowledge_search",
    "state_undo",
    "state_redo",
    "module_write",
    "module_inspect",
    "module_import",
    "module_list",
    "module_index",
    "module_expand",
    "module_read_scene",
    "module_current",
    "module_search",
    "rule_ingest",
}

PLAY_TOOLS = ALWAYS_TOOLS | {
    "campaign_update",
    "campaign_advance_effects",
    "combat_start",
    "branch_compare",
    "branch_create",
    "branch_checkout",
    "snapshot_create",
    "snapshot_restore",
    "snapshot_lineage",
    "snapshot_regenerate_recap",
    "character_wallet_adjust",
    "character_inventory_add",
    "character_inventory_update",
    "character_inventory_remove",
    "character_inventory_equip",
    "character_ammunition_consume",
    "character_inventory_transfer",
    "character_effect_add",
    "character_effect_remove",
    "character_rest",
    "character_cast_spell",
    "character_use_activity",
    "character_resource_set",
    "character_spell_prepare",
    "character_memory_add",
    "character_memory_resolve",
    "party_inventory_add",
    "party_inventory_remove",
    "party_inventory_transfer",
    "party_wallet_adjust",
    "party_wallet_transfer",
    "dnd_dice_roll",
    "dnd_check",
    "dnd_ability_roll",
    "memory_add",
    "memory_list",
    "memory_search",
    "event_add",
    "event_list",
    "actor_knowledge_add",
    "actor_knowledge_revise",
    "actor_knowledge_list",
    "actor_knowledge_search",
    "state_undo",
    "state_redo",
    "module_list",
    "module_expand",
    "module_read_scene",
    "module_current",
    "module_set_progress",
    "module_search",
}

COMBAT_TOOLS = ALWAYS_TOOLS | {
    "campaign_advance_effects",
    "combat_status",
    "combat_available_actions",
    "combat_preflight_attack",
    "combat_resolve_attack",
    "combat_end_turn",
    "combat_reaction_attack",
    "combat_move",
    "combat_common_action",
    "combat_reactions",
    "combat_cast_spell",
    "combat_ready_spell",
    "combat_readied_spell_trigger",
    "combat_readied_spell_resolve",
    "combat_use_activity",
    "combat_check",
    "combat_concentration_check",
    "combat_apply_damage",
    "combat_heal",
    "combat_choice_open",
    "combat_choice_resolve",
    "combat_end",
    "character_effect_add",
    "character_effect_remove",
    "character_resource_set",
    "dnd_dice_roll",
    "dnd_check",
    "event_add",
    "event_list",
    "memory_list",
    "memory_search",
    "actor_knowledge_list",
    "actor_knowledge_search",
    "snapshot_create",
    "module_read_scene",
    "module_current",
    "module_search",
}

TOOLS_BY_PROFILE = {
    PROFILE_AUTHORING: AUTHORING_TOOLS,
    PROFILE_PLAY: PLAY_TOOLS,
    PROFILE_COMBAT: COMBAT_TOOLS,
}


def profiles_for_tool(name: str) -> tuple[str, ...]:
    """Return every profile in which a tool is visible."""
    return tuple(profile for profile in PROFILES if name in TOOLS_BY_PROFILE[profile])


def validate_profile_coverage(tool_names: Iterable[str]) -> None:
    """Fail server construction if a newly added tool has no explicit phase."""
    missing = sorted(name for name in tool_names if not profiles_for_tool(name))
    if missing:
        raise RuntimeError(f"MCP tools missing a tool profile: {', '.join(missing)}")


def profile_catalog() -> dict[str, list[str]]:
    return {profile: sorted(TOOLS_BY_PROFILE[profile]) for profile in PROFILES}
