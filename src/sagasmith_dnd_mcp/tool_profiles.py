"""Phase and capability-group catalogue for the SagaSmith D&D MCP contract.

The catalogue is deliberately server-owned: an MCP client can discover groups
without having to duplicate the D&D tool taxonomy in an agent prompt.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

PROFILE_LOBBY = "lobby"
PROFILE_PLAY = "play"
PROFILE_COMBAT = "combat"
PROFILES = (PROFILE_LOBBY, PROFILE_PLAY, PROFILE_COMBAT)


@dataclass(frozen=True)
class ToolGroup:
    """A coherent, phase-safe set of tools that may be exposed together."""

    id: str
    phase: str
    title: str
    description: str
    risk: str
    tools: frozenset[str]
    requires_campaign: bool = True
    local_only: bool = False
    roles: frozenset[str] = frozenset()


# The first native tools/list response stays intentionally small. The exposure
# exposure tools are the progressive-discovery protocol; the remainder lets a
# host diagnose and choose a campaign without loading a domain group.
CORE_TOOLS = frozenset(
    {
        "exposure_open",
        "exposure_status",
        "exposure_search",
        "exposure_inspect",
        "exposure_load",
        "exposure_unload",
        "exposure_call",
        "server_capabilities",
        "server_tool_profiles",
        "storage_status",
        "campaign_query",
        "game_phase",
    }
)


def _group(
    id: str,
    phase: str,
    title: str,
    description: str,
    risk: str,
    *tools: str,
    requires_campaign: bool = True,
    local_only: bool = False,
    roles: tuple[str, ...] = (),
) -> ToolGroup:
    return ToolGroup(
        id,
        phase,
        title,
        description,
        risk,
        frozenset(tools),
        requires_campaign,
        local_only,
        frozenset(roles),
    )


TOOL_GROUPS = (
    _group(
        "lobby.bootstrap",
        PROFILE_LOBBY,
        "Campaign bootstrap",
        "List systems and create a campaign before opening a campaign-bound exposure.",
        "write",
        "system_list",
        "campaign_create",
        requires_campaign=False,
    ),
    _group(
        "lobby.campaign",
        PROFILE_LOBBY,
        "Campaign setup",
        "Create campaigns, manage members, branches, snapshots and campaign state.",
        "write",
        "campaign_change",
        "access_grant",
        "campaign_event",
        "branch_change",
        "branch_query",
        "snapshot_create",
        "snapshot_query",
        "snapshot_restore",
        "state_revision",
        "campaign_rules",
        roles=("owner", "dm"),
    ),
    _group(
        "lobby.characters",
        PROFILE_LOBBY,
        "Character building",
        "Create characters and apply structured sheets, content, inventory and prepared spells.",
        "write",
        "character_create_from",
        "character_query",
        "character_sheet_replace",
        "character_metadata_update",
        "character_content_apply",
        "character_ability_apply",
        "inventory_change",
        "inventory_transfer",
        "wallet_change",
        "character_state_change",
        "character_action",
        "character_spell_prepare",
        "dnd_dice_roll",
        "dnd_ability_roll",
    ),
    _group(
        "lobby.rules",
        PROFILE_LOBBY,
        "Rulebook import and rule packs",
        "Import rulebooks, compile rule packs, select campaign rules and inspect sources.",
        "write",
        "import_query",
        "rule_import",
        "rule_pack_compile",
        "rule_pack_query",
        "rule_pack_change",
        "rule_seed_status",
        "rule_seed_bundled",
        "rule_search",
        "rule_expand",
        "campaign_rules",
        roles=("owner", "dm"),
    ),
    _group(
        "lobby.modules",
        PROFILE_LOBBY,
        "Module import",
        "Import adventures, inspect scene indexes, and prepare a campaign module.",
        "write",
        "module_import",
        "module_query",
        "module_set_progress",
        "module_search",
        "module_expand",
        roles=("owner", "dm"),
    ),
    _group(
        "lobby.memory",
        PROFILE_LOBBY,
        "Continuity and actor knowledge",
        "Maintain campaign memory and separately scoped PC/NPC actor knowledge.",
        "write",
        "memory_change",
        "memory_query",
        "actor_knowledge_change",
        "actor_knowledge_query",
        "continuity_context",
        "skill_query",
    ),
    _group(
        "lobby.storage_admin",
        PROFILE_LOBBY,
        "Storage administration",
        "Run explicit schema migration actions. Load only for local administration.",
        "admin",
        "storage_migrate",
        requires_campaign=False,
        local_only=True,
    ),
    _group(
        "play.scene",
        PROFILE_PLAY,
        "Scene and campaign play",
        "Read and advance module scenes, campaign events and deterministic effects.",
        "write",
        "campaign_event",
        "campaign_advance_effects",
        "module_query",
        "module_set_progress",
        "module_search",
        "module_expand",
        "continuity_context",
        "memory_query",
        "actor_knowledge_query",
        "snapshot_create",
        "snapshot_query",
    ),
    _group(
        "play.characters",
        PROFILE_PLAY,
        "Character state",
        "Apply normal out-of-combat character, inventory, resource and prepared-spell changes.",
        "write",
        "character_query",
        "character_metadata_update",
        "character_content_apply",
        "inventory_change",
        "inventory_transfer",
        "wallet_change",
        "character_state_change",
        "character_action",
        "memory_change",
        "actor_knowledge_change",
    ),
    _group(
        "play.resolution",
        PROFILE_PLAY,
        "Checks and rolls",
        "Resolve D&D rolls and character checks before starting combat.",
        "write",
        "dnd_dice_roll",
        "dnd_check",
        "dnd_ability_roll",
        "character_check",
        "combat_start",
        "rule_search",
        "rule_expand",
    ),
    _group(
        "combat.observe",
        PROFILE_COMBAT,
        "Combat state",
        "Inspect encounter state, available combat options, map and current combatant.",
        "read",
        "combat_query",
        "character_query",
        "module_query",
        "module_search",
        "memory_query",
        "actor_knowledge_query",
        "rule_search",
        "rule_expand",
    ),
    _group(
        "combat.turn",
        PROFILE_COMBAT,
        "Turns and choices",
        "Advance turns, resolve choice windows, ready actions and end combat.",
        "write",
        "combat_end_turn",
        "combat_join",
        "combat_ready",
        "combat_choice",
        "combat_end",
    ),
    _group(
        "combat.actions",
        PROFILE_COMBAT,
        "Combat actions",
        "Resolve attacks, movement, actions, reactions, spells, activities and checks.",
        "write",
        "combat_preflight_attack",
        "combat_resolve_attack",
        "combat_reaction_attack",
        "combat_movement",
        "combat_common_action",
        "combat_cast_spell",
        "combat_use_activity",
        "combat_check",
        "combat_concentration_check",
        "combat_hp_change",
        "dnd_dice_roll",
        "dnd_check",
        "campaign_advance_effects",
    ),
    _group(
        "combat.save",
        PROFILE_COMBAT,
        "Combat saves",
        "Create and inspect branch-aware snapshots during an active encounter.",
        "write",
        "snapshot_create",
        "snapshot_query",
    ),
    _group(
        "combat.map",
        PROFILE_COMBAT,
        "Combat map control",
        "Patch the temporary combat map created for the current encounter.",
        "write",
        "combat_map_patch",
    ),
)

GROUP_BY_ID = {group.id: group for group in TOOL_GROUPS}


def tools_for_phase(phase: str) -> frozenset[str]:
    return frozenset().union(
        *(group.tools for group in TOOL_GROUPS if group.phase == phase), CORE_TOOLS
    )


TOOLS_BY_PROFILE = {profile: tools_for_phase(profile) for profile in PROFILES}


def profiles_for_tool(name: str) -> tuple[str, ...]:
    """Return every phase in which a public tool is valid."""
    if name in CORE_TOOLS:
        return PROFILES
    return tuple(profile for profile in PROFILES if name in TOOLS_BY_PROFILE[profile])


def groups_for_tool(name: str) -> tuple[str, ...]:
    return tuple(group.id for group in TOOL_GROUPS if name in group.tools)


def validate_profile_coverage(tool_names: Iterable[str]) -> None:
    """Fail server construction if a public tool has no explicit phase/group."""
    missing = sorted(name for name in tool_names if not profiles_for_tool(name))
    if missing:
        raise RuntimeError(f"MCP tools missing a tool profile: {', '.join(missing)}")


def profile_catalog() -> dict[str, list[str]]:
    return {profile: sorted(TOOLS_BY_PROFILE[profile]) for profile in PROFILES}


def group_catalog() -> list[dict[str, object]]:
    return [
        {
            "id": group.id,
            "phase": group.phase,
            "title": group.title,
            "description": group.description,
            "risk": group.risk,
            "requires_campaign": group.requires_campaign,
            "local_only": group.local_only,
            "roles": sorted(group.roles),
            "tools": sorted(group.tools),
        }
        for group in TOOL_GROUPS
    ]
