"""One phase policy per public D&D MCP tool.

Exposure is session-scoped and mutable, but authorization is not encoded in an
Agent-owned load plan.  This module is the single catalogue used by native
``tools/list`` filtering and call-time phase/role checks.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sagasmith_core.access import CAMPAIGN_DM_ROLES

PROFILE_LOBBY = "lobby"
PROFILE_PLAY = "play"
PROFILE_COMBAT = "combat"
PROFILES = (PROFILE_LOBBY, PROFILE_PLAY, PROFILE_COMBAT)


def campaign_phase(state: Mapping[str, Any] | None) -> str:
    """Resolve the authoritative phase from persisted campaign state."""

    value = dict(state or {})
    combat = value.get("combat")
    if isinstance(combat, Mapping) and bool(combat.get("active", False)):
        return PROFILE_COMBAT
    phase = str(value.get("game_phase") or PROFILE_LOBBY)
    if phase not in {PROFILE_LOBBY, PROFILE_PLAY}:
        raise ValueError(f"unsupported persisted campaign phase: {phase}")
    return phase


# These tools are always visible. ``exposure`` owns the mutable native list;
# authorization remains a call-time concern.
CORE_TOOLS = frozenset(
    {
        "exposure",
        "server_capabilities",
        "storage_status",
        "campaign_query",
        "game_phase",
        "resolution_presentation",
        "skill_query",
    }
)

HOST_PRIVATE_TOOLS = frozenset({"npc_conversation_transport"})


def _names(value: str) -> frozenset[str]:
    return frozenset(value.split())


PHASE_TOOLS = {
    PROFILE_LOBBY: _names(
        """
        access_grant access_revoke actor_knowledge_change actor_knowledge_query
        addon_actor_instantiate bounded_evaluation branch_change branch_query campaign_change
        campaign_create
        campaign_event campaign_rules character_ability_apply character_action
        character_content_apply character_create_from character_metadata_update character_query
        character_sheet_replace character_spell_prepare character_state_change content_pack
        content_solution continuity_context dnd_ability_roll dnd_dice_roll inventory_change
        inventory_transfer memory_change memory_query module_draft module_expand module_query
        module_search module_set_progress playthrough_manifest rule_expand rule_search
        rule_seed_bundled rule_seed_status rulebook_draft snapshot_create snapshot_query
        snapshot_restore state_revision storage_migrate system_list wallet_change
        """
    ),
    PROFILE_PLAY: _names(
        """
        access_grant access_revoke actor_knowledge_change actor_knowledge_query
        addon_actor_instantiate bounded_evaluation
        branch_query campaign_change campaign_event campaign_rules character_action
        character_check character_content_apply character_metadata_update
        character_query character_state_change chase combat_start content_solution
        continuity_context dnd_ability_roll dnd_check dnd_dice_roll inventory_change
        inventory_transfer memory_change memory_query module_expand module_query module_search
        module_set_progress npc_conversation playthrough_manifest rule_expand rule_search
        snapshot_create snapshot_query state_revision wallet_change
        """
    ),
    PROFILE_COMBAT: _names(
        """
        access_grant access_revoke actor_knowledge_query addon_actor_instantiate bounded_evaluation
        branch_change branch_query
        campaign_rules character_query combat_cast_spell combat_check combat_choice
        combat_common_action combat_concentration_check combat_end combat_end_turn combat_hp_change
        combat_join combat_map_patch combat_movement combat_preflight_attack combat_query
        combat_reaction_attack combat_ready combat_resolve_attack combat_use_activity
        content_solution continuity_context dnd_check dnd_dice_roll module_query module_search
        playthrough_manifest rule_expand rule_search snapshot_create snapshot_query snapshot_restore
        state_revision
        """
    ),
}

PHASE_DM_TOOLS = {
    PROFILE_LOBBY: _names(
        """
        access_grant access_revoke actor_knowledge_change addon_actor_instantiate branch_change
        branch_query campaign_change campaign_event campaign_rules content_pack content_solution
        memory_change
        memory_query module_draft module_expand module_query module_search module_set_progress
        playthrough_manifest rule_expand rule_search rule_seed_bundled rule_seed_status
        rulebook_draft snapshot_create snapshot_query snapshot_restore state_revision
        """
    ),
    PROFILE_PLAY: _names(
        """
        access_grant access_revoke actor_knowledge_change addon_actor_instantiate
        campaign_change campaign_event
        campaign_rules character_content_apply chase
        combat_start content_solution memory_change memory_query module_set_progress
        npc_conversation playthrough_manifest snapshot_create snapshot_query state_revision
        """
    ),
    PROFILE_COMBAT: _names(
        """
        access_grant access_revoke addon_actor_instantiate branch_change campaign_rules combat_end
        combat_join combat_map_patch
        content_solution playthrough_manifest snapshot_create snapshot_query snapshot_restore
        state_revision
        """
    ),
}

NO_CAMPAIGN_TOOLS = frozenset({"campaign_create", "storage_migrate", "system_list"})
LOCAL_ONLY_TOOLS = frozenset({"storage_migrate"})


@dataclass(frozen=True)
class ToolPolicy:
    id: str
    phases: frozenset[str]
    roles_by_phase: Mapping[str, frozenset[str]]
    requires_campaign: bool
    local_only: bool

    def roles(self, phase: str) -> frozenset[str]:
        return self.roles_by_phase.get(phase, frozenset())


def _build_policies() -> dict[str, ToolPolicy]:
    tool_ids = frozenset().union(*PHASE_TOOLS.values())
    return {
        tool_id: ToolPolicy(
            id=tool_id,
            phases=frozenset(phase for phase in PROFILES if tool_id in PHASE_TOOLS[phase]),
            roles_by_phase={
                phase: frozenset(CAMPAIGN_DM_ROLES)
                for phase in PROFILES
                if tool_id in PHASE_DM_TOOLS[phase]
            },
            requires_campaign=tool_id not in NO_CAMPAIGN_TOOLS,
            local_only=tool_id in LOCAL_ONLY_TOOLS,
        )
        for tool_id in tool_ids
    }


TOOL_POLICIES = _build_policies()


def policy_for_tool(name: str) -> ToolPolicy | None:
    return TOOL_POLICIES.get(name)


def tools_for_phase(phase: str) -> frozenset[str]:
    if phase not in PHASE_TOOLS:
        raise ValueError(f"unsupported tool phase: {phase}")
    return PHASE_TOOLS[phase] | CORE_TOOLS


TOOLS_BY_PROFILE = {profile: tools_for_phase(profile) for profile in PROFILES}


def profiles_for_tool(name: str) -> tuple[str, ...]:
    if name in CORE_TOOLS:
        return PROFILES
    policy = policy_for_tool(name)
    return tuple(phase for phase in PROFILES if policy is not None and phase in policy.phases)


def validate_profile_coverage(tool_names: Iterable[str]) -> None:
    missing = sorted(
        name
        for name in tool_names
        if name not in HOST_PRIVATE_TOOLS and name not in CORE_TOOLS and name not in TOOL_POLICIES
    )
    if missing:
        raise RuntimeError(f"MCP tools missing a tool policy: {', '.join(missing)}")


def profile_catalog() -> dict[str, list[str]]:
    return {profile: sorted(TOOLS_BY_PROFILE[profile]) for profile in PROFILES}


def tool_catalog() -> list[dict[str, object]]:
    return [
        {
            "id": policy.id,
            "phases": sorted(policy.phases),
            "roles_by_phase": {
                phase: sorted(roles) for phase, roles in policy.roles_by_phase.items()
            },
            "requires_campaign": policy.requires_campaign,
            "local_only": policy.local_only,
        }
        for policy in sorted(TOOL_POLICIES.values(), key=lambda item: item.id)
    ]
