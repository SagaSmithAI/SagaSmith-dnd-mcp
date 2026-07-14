"""MCP surface for the SagaSmith D&D runtime and bundled skill packs."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from typing import Any

from mcp.server.fastmcp import FastMCP
from sagasmith_core import (
    AccessService,
    ActorKnowledgeService,
    BranchService,
    CampaignService,
    CharacterService,
    CharacterStateUpdate,
    ContinuityService,
    EventService,
    IdempotencyService,
    MemoryService,
    ModuleService,
    RevisionService,
    RuleService,
    SnapshotService,
    StateMutationService,
    default_local_principal,
)
from sagasmith_core.idempotency import request_hash
from sagasmith_core.modules import MarkdownModuleParser
from sagasmith_core.systems import SystemRegistry
from sagasmith_dnd.ability_generation import apply_ability_generation, roll_ability_scores
from sagasmith_dnd.activities import ActivityError, consume_activity
from sagasmith_dnd.character_schema import (
    add_effect,
    add_inventory_item,
    add_memory,
    adjust_wallet,
    consume_weapon_ammunition,
    default_character_notes,
    default_character_sheet,
    derive_character_sheet,
    equip_inventory_item,
    receive_inventory_item,
    remove_effect,
    remove_inventory_item,
    resolve_memory,
    set_resource_value,
    set_spell_prepared,
    update_inventory_item,
    validate_character_notes,
    validate_character_sheet,
    validate_party_state,
)
from sagasmith_dnd.combat_engine import (
    CombatEngineError,
    NeedsRulingError,
    add_choice_window,
    apply_concentration_result,
    apply_damage_parts_to_sheet,
    apply_healing_to_sheet,
    arm_readied_spell,
    available_actions,
    available_reactions,
    current_combatant,
    end_turn,
    pay_activity_activation,
    preflight_attack,
    resolve_actor_check,
    resolve_attack_action,
    resolve_choice_window,
    resolve_common_action,
    resolve_death_save_to_sheet,
    resolve_readied_spell_window,
    spend_movement,
    start_encounter,
    trigger_readied_spell,
)
from sagasmith_dnd.engine import resolve_check, roll
from sagasmith_dnd.lifecycle import advance_effect_durations, apply_rest
from sagasmith_dnd.module_profile import DndModuleProfile
from sagasmith_dnd.spells import (
    consume_readied_spell,
    consume_spell_cast,
    replace_prepared_spells,
)
from sagasmith_dnd.system import DND5E

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.skills import SkillCatalog
from sagasmith_dnd_mcp.storage import SagaSmithStorage
from sagasmith_dnd_mcp.tool_profiles import (
    PROFILE_AUTHORING,
    PROFILE_COMBAT,
    PROFILE_PLAY,
    profile_catalog,
    profiles_for_tool,
    validate_profile_coverage,
)


def create_server(config: McpConfig | None = None) -> FastMCP:
    """Create a stdio-capable server with one MCP-owned local data directory."""
    config = config or McpConfig.from_environment()
    storage = SagaSmithStorage(config)
    storage.migrate()
    campaigns = CampaignService(storage.database)
    characters = CharacterService(storage.database)
    branches = BranchService(storage.database)
    continuity = ContinuityService(storage.database)
    events = EventService(storage.database)
    knowledge = ActorKnowledgeService(storage.database)
    access = AccessService(storage.database)
    idempotency = IdempotencyService(storage.database)
    default_local_principal(storage.database)
    memories = MemoryService(storage.database)
    modules = ModuleService(storage.database)
    rules = RuleService(storage.database)
    revisions = RevisionService(storage.database)
    snapshots = SnapshotService(storage.database)
    catalog = SkillCatalog(
        dnd_root=config.dnd_skills_dir,
        modulegen_root=config.modulegen_skills_dir,
    )

    def seed_bundled_rules(*, max_files: int = 64) -> dict[str, Any]:
        """Idempotently index the compact bundled SRD reference corpus."""
        existing = rules.sources(system_id="dnd5e")
        if existing:
            return {"status": "ready", "skipped": True, "sources": len(existing)}
        root = config.dnd_skills_dir / "full" / "skills" / "dnd-dm" / "srd"
        paths = sorted((root / "references").glob("*.md"))
        paths += sorted((root / "references-2014-en" / "06_Gameplay").glob("*.md"))
        paths = paths[: max(1, min(max_files, 256))]
        seeded = 0
        for path in paths:
            content = path.read_text(encoding="utf-8")
            edition = "2014" if "references-2014-en" in path.parts else "2024"
            rules.ingest(
                system_id="dnd5e",
                source_key=f"bundled/{path.relative_to(root).as_posix()}",
                title=path.stem,
                content=content,
                locale="en",
                edition=edition,
                version="bundled-srd-2026-07",
                publication_id="srd",
            )
            seeded += 1
        return {"status": "ready", "skipped": False, "sources": seeded}

    if config.auto_seed_rules:
        seed_bundled_rules()
    mcp = FastMCP(
        "SagaSmith D&D",
        instructions="D&D 5e campaign runtime, module storage, and skill packs.",
    )

    def character_view(character: Any) -> dict[str, Any]:
        """Return a raw validated sheet together with its non-persisted derived view."""
        value = asdict(character)
        value["derived"] = derive_character_sheet(value["sheet"])
        return value

    def public_character_view(character: Any) -> dict[str, Any]:
        """Return the campaign-safe card for actors a player does not control."""
        return {
            "id": character.id,
            "campaign_id": character.campaign_id,
            "system_id": character.system_id,
            "character_type": character.character_type,
            "name": character.name,
            "summary": character.summary,
            "revision": character.revision,
        }

    def is_dm(campaign_id: str, principal_id: str) -> bool:
        return access.require_campaign(campaign_id, principal_id).role in {"owner", "dm"}

    def require_character_control(character: Any, principal_id: str) -> None:
        if character.campaign_id is None:
            if principal_id != "system:local":
                raise PermissionError("only the local service may modify library characters")
            return
        access.require_actor(character.campaign_id, character.id, principal_id, control=True)

    def visible_character_view(character: Any, principal_id: str) -> dict[str, Any]:
        if character.campaign_id is None:
            if principal_id != "system:local":
                return public_character_view(character)
            return character_view(character)
        if is_dm(character.campaign_id, principal_id):
            return character_view(character)
        try:
            access.require_actor(character.campaign_id, character.id, principal_id, private=True)
        except PermissionError:
            return public_character_view(character)
        return character_view(character)

    def combat_actor_snapshot(character_id: str) -> dict[str, Any]:
        """Build the pure engine input from the canonical Character row."""
        return character_view(characters.get(character_id))

    def require_campaign_actor(campaign_id: str, character_id: str) -> Any:
        character = characters.get(character_id)
        if character.campaign_id != campaign_id:
            raise ValueError("actor does not belong to the campaign")
        return character

    def combat_view(campaign_id: str, principal_id: str) -> dict[str, Any] | None:
        campaign = campaigns.get(campaign_id)
        encounter = dict(campaign.state or {}).get("combat")
        if encounter is None:
            return None
        membership = access.require_campaign(campaign_id, principal_id)
        value = dict(encounter)
        if membership.role not in {"owner", "dm"}:
            viewer_actor_ids: set[str] = set()
            for item in encounter.get("combatants", []):
                actor_id_value = str(item.get("actor_id") or "")
                try:
                    access.require_actor(
                        campaign_id,
                        actor_id_value,
                        principal_id,
                        private=True,
                    )
                except PermissionError:
                    continue
                viewer_actor_ids.add(actor_id_value)

            def player_can_see(item: dict[str, Any]) -> bool:
                actor_id_value = str(item.get("actor_id") or "")
                if actor_id_value in viewer_actor_ids:
                    return True
                concealed = bool(item.get("hidden", False)) or "invisible" in {
                    str(condition).casefold() for condition in item.get("conditions", [])
                }
                if not concealed:
                    return True
                visible_to = item.get("visible_to_actor_ids")
                return isinstance(visible_to, list) and bool(
                    viewer_actor_ids & {str(actor_id) for actor_id in visible_to}
                )

            visible_combatants = [
                {
                    key: item[key]
                    for key in ("actor_id", "token_id", "name", "initiative", "position")
                    if key in item
                }
                for item in encounter.get("combatants", [])
                if player_can_see(item)
            ]
            current = current_combatant(encounter)
            current_id = str(current.get("actor_id")) if current is not None else None
            visible_ids = [str(item.get("actor_id")) for item in visible_combatants]
            value["combatants"] = visible_combatants
            value["turn_index"] = (
                visible_ids.index(current_id) if current_id in visible_ids else None
            )
            value.pop("log", None)
            value.pop("rulings", None)
            value.pop("pending", None)
            value.pop("readied", None)
            value.pop("effects", None)
        return value

    def combat_response(
        campaign_id: str, principal_id: str, response: dict[str, Any]
    ) -> dict[str, Any]:
        """Project every combat write result through the same audience boundary."""
        if is_dm(campaign_id, principal_id):
            return response
        value = dict(response)
        if "combat" in value:
            value["combat"] = combat_view(campaign_id, principal_id)
        result = value.get("result")
        if isinstance(result, dict):
            allowed = {
                "kind",
                "attacker_id",
                "target_id",
                "hit",
                "critical",
                "fumble",
                "success",
                "amount",
                "applied_amount",
                "hp_damage",
                "healed",
                "after_hp",
                "effects_active",
                "conditions",
                "parts",
                "activity_id",
                "content_type",
                "name",
                "payment",
                "requires_ruling",
                "declaration",
            }
            value["result"] = {key: item for key, item in result.items() if key in allowed}
        value.pop("revisions", None)
        return value

    def current_branch_id(campaign_id: str) -> str | None:
        current = branches.current(campaign_id)
        return current.id if current is not None else None

    def active_encounter(campaign_id: str) -> tuple[Any, dict[str, Any]]:
        campaign = campaigns.get(campaign_id)
        encounter = dict(campaign.state or {}).get("combat")
        if not isinstance(encounter, dict) or not encounter.get("active", False):
            raise CombatEngineError("combat is not active")
        return campaign, encounter

    def mutation_revision(campaign_id: str) -> int:
        """Read the committed campaign revision after mixed entity writes."""
        return int(campaigns.get(campaign_id).revision)

    def require_current_branch(campaign_id: str, branch_id: str | None) -> str | None:
        current = current_branch_id(campaign_id)
        if branch_id is not None and current is not None and branch_id != current:
            raise ValueError("branch_id must match the campaign's checked-out branch")
        return current if branch_id is None else branch_id

    def readable_branch(campaign_id: str, branch_id: str | None, principal_id: str) -> str | None:
        """Players can read only the checked-out timeline; DM roles may inspect alternatives."""
        current = current_branch_id(campaign_id)
        if not is_dm(campaign_id, principal_id) and branch_id not in {None, current}:
            raise PermissionError("players may only inspect the checked-out branch")
        return current if branch_id is None else branch_id

    def require_write_contract(expected_revision: int | None, idempotency_key: str | None) -> None:
        if expected_revision is None:
            raise ValueError("expected_revision is required for a combat mutation")
        if not idempotency_key:
            raise ValueError("idempotency_key is required for a combat mutation")

    def sanitize_attack_action(
        campaign_id: str, principal_id: str, action: dict[str, Any]
    ) -> dict[str, Any]:
        membership = access.require_campaign(campaign_id, principal_id)
        value = dict(action)
        if membership.role not in {"owner", "dm"}:
            # Tactical context (cover, advantage, concealment and reach) is a
            # scene/DM fact, not a client-controlled modifier.
            value["context"] = {}
            value["rulings"] = [
                item
                for item in value.get("rulings", [])
                if isinstance(item, dict) and item.get("source") == "dm_ruling"
            ]
        return value

    def sync_combatant_conditions(
        encounter: dict[str, Any], actor_id: str, sheet: dict[str, Any]
    ) -> None:
        for combatant in encounter.get("combatants", []):
            if combatant.get("actor_id") == actor_id:
                combatant["conditions"] = list(sheet.get("conditions") or [])
                return

    def add_concentration_window(
        encounter: dict[str, Any],
        target_id: str,
        concentration: dict[str, Any] | None,
        *,
        next_revision: int,
    ) -> None:
        """Persist one immediate concentration save without silently replacing another."""
        if not concentration:
            return
        if any(
            item.get("status", "pending") == "pending"
            and item.get("kind") == "concentration"
            and item.get("actor_id") == target_id
            for item in encounter.get("pending", [])
        ):
            raise CombatEngineError(
                "the actor already has a pending concentration save; resolve it first"
            )
        pending = dict(concentration)
        pending.update(
            {
                "id": f"concentration:{target_id}:{next_revision}",
                "kind": "concentration",
                "actor_id": target_id,
            }
        )
        encounter["pending"] = [*list(encounter.get("pending") or []), pending]

    def require_no_blocking_pending(encounter: dict[str, Any]) -> None:
        if any(item.get("status", "pending") == "pending" for item in encounter.get("pending", [])):
            raise CombatEngineError("resolve the pending save or choice before another action")

    def reconcile_readied_spells(
        encounter: dict[str, Any], actor_id: str, sheet: dict[str, Any]
    ) -> list[str]:
        """Dissipate readied spells whose holding concentration is no longer active."""
        active_effect_ids = {
            str(effect.get("id"))
            for effect in sheet.get("effects", [])
            if effect.get("active") and effect.get("concentration")
        }
        expired = [
            item
            for item in encounter.get("readied", [])
            if item.get("kind") == "spell"
            and item.get("actor_id") == actor_id
            and str(item.get("holding_effect_id")) not in active_effect_ids
        ]
        if not expired:
            return []
        expired_ids = {str(item.get("id")) for item in expired}
        encounter["readied"] = [
            item for item in encounter.get("readied", []) if str(item.get("id")) not in expired_ids
        ]
        encounter["pending"] = [
            item
            for item in encounter.get("pending", [])
            if str(item.get("readied_id")) not in expired_ids
        ]
        encounter["log"] = [
            *list(encounter.get("log") or []),
            *[
                {
                    "type": "readied_spell_dissipated",
                    "actor_id": actor_id,
                    "readied_id": item.get("id"),
                    "reason": "concentration_ended",
                }
                for item in expired
            ],
        ][-100:]
        return sorted(expired_ids)

    def record_character_revision(before: Any, after: Any, operation: str) -> None:
        if before.campaign_id is None:
            return
        fields = ("name", "player_name", "summary", "sheet", "notes", "revision")
        revisions.record(
            before.campaign_id,
            operation=operation,
            entity_type="character",
            entity_id=before.id,
            before={field: getattr(before, field) for field in fields},
            after={field: getattr(after, field) for field in fields},
            actor="mcp",
        )

    def update_character(
        before: Any,
        *,
        operation: str,
        sheet: dict[str, Any] | None = None,
        notes: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        payload: dict[str, Any] | None = None,
        response_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        def response_for(character: dict[str, Any]) -> dict[str, Any]:
            if response_extra is None:
                return character
            return {"character": character, **response_extra}

        if before.campaign_id is None:
            updated = characters.update(before.id, sheet=sheet, notes=notes)
            record_character_revision(before, updated, operation)
            return response_for(character_view(updated))
        if expected_revision is None or not idempotency_key:
            raise ValueError(
                "expected_revision and idempotency_key are required for character writes"
            )
        branch_id = require_current_branch(before.campaign_id, None)
        request_payload = {
            "operation": operation,
            "character_id": before.id,
            **(payload or {}),
        }
        scope = f"character-write:{before.campaign_id}:{branch_id}:{principal_id}:{before.id}"
        replay = replay_idempotent(scope, idempotency_key, request_payload)
        if replay is not None:
            return replay
        StateMutationService(storage.database).replace(
            before.campaign_id,
            character_updates=[
                CharacterStateUpdate(
                    character_id=before.id,
                    sheet=validate_character_sheet(sheet if sheet is not None else before.sheet),
                    notes=validate_character_notes(notes if notes is not None else before.notes),
                    expected_revision=expected_revision,
                )
            ],
            operation=operation,
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=idempotency_key,
        )
        response = response_for(character_view(characters.get(before.id)))
        return remember_idempotent(
            scope,
            idempotency_key,
            request_payload,
            response,
            campaign_id=before.campaign_id,
        )

    def party_sheet(state: dict[str, Any]) -> dict[str, Any]:
        value = validate_party_state(state)
        sheet = default_character_sheet()
        sheet["inventory"] = value["party"]["inventory"]
        return validate_character_sheet(sheet)

    def party_state(state: dict[str, Any], sheet: dict[str, Any]) -> dict[str, Any]:
        value = validate_party_state(state)
        value["party"]["inventory"] = sheet["inventory"]
        return validate_party_state(value)

    def replay_idempotent(
        scope: str, key: str | None, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not key:
            return None
        result = idempotency.lookup(scope, key, payload)
        if result is not None:
            return result.response
        campaign_id = scope.split(":", 2)[1] if ":" in scope else ""
        if campaign_id and idempotency.mutation_committed(campaign_id, key, payload):
            return {
                "status": "committed",
                "idempotency_replayed": True,
                "response_recovery": "read_current_state",
            }
        return None

    def remember_idempotent(
        scope: str,
        key: str | None,
        payload: dict[str, Any],
        response: dict[str, Any],
        campaign_id: str | None = None,
    ) -> dict[str, Any]:
        if key:
            idempotency.remember(scope, key, payload, response, campaign_id=campaign_id)
        return response

    @mcp.tool()
    def storage_status() -> dict[str, Any]:
        """Return the MCP-owned SQLite, ChromaDB, and artifact locations."""
        return storage.status()

    @mcp.tool()
    def server_capabilities() -> dict[str, Any]:
        """Describe the MCP contract and the automatic-vs-ruling combat boundary."""
        return {
            "contract_version": "2026-07-integrity-v3",
            "transport": "stdio",
            "state_owner": "sagasmith-dnd-mcp",
            "features": {
                "mutation_groups": True,
                "atomic_undo_redo": True,
                "idempotency": True,
                "optimistic_concurrency": True,
                "principal_memberships": True,
                "actor_knowledge_isolation": True,
                "branch_compare": True,
                "bundled_rule_seed": True,
                "module_visibility_filter": True,
                "structured_combat_engine": True,
                "combat_preflight_commit": True,
                "combat_choice_windows": True,
                "combat_multi_damage": True,
                "combat_death_saves": True,
                "combat_concentration_checks": True,
                "combat_ruleset_adapter": True,
                "combat_authoritative_attack_data": True,
                "combat_target_mechanics_redacted": True,
                "combat_active_state_guard": True,
                "combat_spatial_reactions": True,
                "class_aware_prepared_spells": True,
                "structured_activity_accounting": True,
                "campaign_effect_timeline": True,
                "idempotency_crash_recovery": True,
            },
            "write_requirements": ["principal_id", "expected_revision", "idempotency_key"],
        }

    @mcp.tool()
    def storage_migrate() -> dict[str, str]:
        """Run the embedded SQLite schema migrations."""
        storage.migrate()
        return {"status": "ok", "database": storage.database.url}

    @mcp.tool()
    def rule_seed_status() -> dict[str, Any]:
        """Return whether the bundled SRD corpus is indexed."""
        return {
            "sources": rules.sources(system_id="dnd5e"),
            "auto_seed": config.auto_seed_rules,
        }

    @mcp.tool()
    def rule_seed_bundled(max_files: int = 64) -> dict[str, Any]:
        """Idempotently index the bundled compact SRD corpus."""
        return seed_bundled_rules(max_files=max_files)

    @mcp.tool()
    def system_list() -> list[dict[str, Any]]:
        """List systems exposed by this MCP server."""
        registry = SystemRegistry()
        registry.register(DND5E)
        return [
            {
                "id": system.id,
                "display_name": system.display_name,
                "character_types": list(system.character_types),
                "campaign_defaults": system.campaign_defaults,
            }
            for system in registry.list()
        ]

    @mcp.tool()
    def campaign_create(
        name: str,
        description: str = "",
        edition: str = "2024",
        locale: str = "en",
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a D&D 5e campaign inside the MCP-owned SQLite database."""
        if not idempotency_key:
            raise ValueError("idempotency_key is required for campaign creation")
        created = campaigns.create_owned(
            system_id="dnd5e",
            name=name,
            principal_id=principal_id,
            idempotency_key=idempotency_key,
            description=description,
            settings={"edition": edition, "locale": locale},
        )
        return asdict(created)

    @mcp.tool()
    def campaign_list(
        status: str | None = None,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        """List D&D 5e campaigns."""
        allowed = access.accessible_campaign_ids(principal_id)
        return [
            asdict(item)
            for item in campaigns.list(system_id="dnd5e", status=status)
            if item.id in allowed
        ]

    @mcp.tool()
    def campaign_get(campaign_id: str, principal_id: str = "system:local") -> dict[str, Any]:
        """Read one campaign, including its persisted party and combat state."""
        access.require_campaign(campaign_id, principal_id)
        return asdict(campaigns.get(campaign_id))

    @mcp.tool()
    def server_tool_profiles() -> dict[str, list[str]]:
        """List the exact game-outside, exploration, and combat MCP tool sets."""
        return profile_catalog()

    @mcp.tool()
    def game_phase_get(
        campaign_id: str,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Return the authoritative tool profile for this campaign."""
        access.require_campaign(campaign_id, principal_id)
        campaign = campaigns.get(campaign_id)
        state = dict(campaign.state or {})
        combat = state.get("combat")
        if isinstance(combat, dict) and combat.get("active", False):
            profile = PROFILE_COMBAT
        else:
            profile = str(state.get("game_phase") or PROFILE_AUTHORING)
            if profile not in {PROFILE_AUTHORING, PROFILE_PLAY}:
                profile = PROFILE_AUTHORING
        return {
            "campaign_id": campaign_id,
            "tool_profile": profile,
            "combat_active": profile == PROFILE_COMBAT,
            "campaign_revision": campaign.revision,
        }

    @mcp.tool()
    def game_phase_set(
        campaign_id: str,
        tool_profile: str,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Switch between game-outside authoring and live non-combat play."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        profile = str(tool_profile).strip().lower()
        if profile not in {PROFILE_AUTHORING, PROFILE_PLAY}:
            raise ValueError(
                "tool_profile must be authoring or play; combat starts via combat_start"
            )
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        payload = {"tool_profile": profile, "branch_id": resolved_branch_id}
        scope = f"game-phase:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        campaign = campaigns.get(campaign_id)
        if campaign.revision != expected_revision:
            raise ValueError(
                f"campaign revision conflict: expected {expected_revision}, "
                f"found {campaign.revision}"
            )
        combat = dict(campaign.state or {}).get("combat")
        if isinstance(combat, dict) and combat.get("active", False):
            raise CombatEngineError("end the active combat before leaving the combat profile")
        state = dict(campaign.state or {})
        state["game_phase"] = profile
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(state),
            expected_campaign_revision=campaign.revision,
            operation="game.phase.set",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
        )
        response = {
            "campaign_id": campaign_id,
            "tool_profile": profile,
            "combat_active": False,
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
        )

    @mcp.tool()
    def campaign_member_grant(
        campaign_id: str,
        principal_id: str,
        role: str = "player",
        by_principal_id: str | None = None,
    ) -> dict[str, Any]:
        """Grant DM/player/observer campaign access; caller role is resolved server-side."""
        caller = by_principal_id or "system:local"
        access.require_campaign(campaign_id, caller, roles={"owner", "dm"})
        access.ensure_principal(principal_id, platform="mcp", external_id=principal_id)
        return asdict(access.grant_campaign(campaign_id, principal_id, role=role))

    @mcp.tool()
    def actor_grant(
        campaign_id: str,
        principal_id: str,
        actor_id: str,
        can_control: bool = False,
        can_view_private: bool = False,
        by_principal_id: str | None = None,
    ) -> dict[str, Any]:
        """Grant an explicit PC/NPC control and private-sheet view permission."""
        caller = by_principal_id or "system:local"
        access.require_campaign(campaign_id, caller, roles={"owner", "dm"})
        access.ensure_principal(principal_id, platform="mcp", external_id=principal_id)
        return asdict(
            access.grant_actor(
                campaign_id,
                principal_id,
                actor_id,
                can_control=can_control,
                can_view_private=can_view_private,
            )
        )

    @mcp.tool()
    def campaign_update(
        campaign_id: str,
        name: str | None = None,
        status: str | None = None,
        description: str | None = None,
        settings: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Apply a reviewed campaign-level update without bypassing its state document."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if expected_revision is None or not idempotency_key:
            raise ValueError(
                "expected_revision and idempotency_key are required for campaign updates"
            )
        if state is not None and "combat" in state:
            raise ValueError("combat state is owned by structured combat tools")
        branch_id = require_current_branch(campaign_id, None)
        payload = {
            "name": name,
            "status": status,
            "description": description,
            "settings": settings,
            "state": state,
            "branch_id": branch_id,
        }
        scope = f"campaign-update:{campaign_id}:{branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        before = campaigns.get(campaign_id)
        normalized_state = None
        if state is not None:
            normalized_state = validate_party_state(state)
            # A generic campaign update may manage the shared party document,
            # but it must never erase an encounter maintained by combat tools.
            if isinstance(before.state, dict) and "combat" in before.state:
                normalized_state = {**before.state, "party": normalized_state["party"]}
        after = campaigns.update_audited(
            campaign_id,
            name=name,
            status=status,
            description=description,
            settings=settings,
            state=normalized_state,
            expected_revision=expected_revision,
            operation="campaign.update",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash(payload),
        )
        return remember_idempotent(
            scope,
            idempotency_key,
            payload,
            asdict(after),
            campaign_id=campaign_id,
        )

    @mcp.tool()
    def combat_start(
        campaign_id: str,
        participant_ids: list[str],
        participant_config: list[dict[str, Any]] | None = None,
        name: str = "Combat",
        scene_id: str | None = None,
        ruleset: str | None = None,
        principal_id: str = "system:local",
        branch_id: str | None = None,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Start a structured encounter from canonical campaign actors."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        participant_config = participant_config or []
        payload = {
            "participant_ids": list(participant_ids),
            "participant_config": participant_config,
            "name": name,
            "scene_id": scene_id,
            "ruleset": ruleset,
            "branch_id": branch_id,
        }
        scope = f"combat-start:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        campaign = campaigns.get(campaign_id)
        if expected_revision is not None and campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        if not participant_ids:
            raise ValueError("participant_ids must not be empty")
        if isinstance(campaign.state, dict) and campaign.state.get("combat", {}).get("active"):
            raise CombatEngineError(
                "combat is already active; end it before starting another encounter"
            )
        if len(set(participant_ids)) != len(participant_ids):
            raise ValueError("participant_ids must be unique")
        config_by_actor: dict[str, dict[str, Any]] = {}
        for entry in participant_config:
            if not isinstance(entry, dict) or not entry.get("actor_id"):
                raise ValueError("each participant_config entry needs actor_id")
            actor_id_value = str(entry["actor_id"])
            if actor_id_value not in participant_ids:
                raise ValueError("participant_config actor_id must be a participant")
            if actor_id_value in config_by_actor:
                raise ValueError("participant_config actor_id must be unique")
            allowed = {
                "actor_id",
                "token_id",
                "position",
                "hidden",
                "visible_to_actor_ids",
                "disposition",
                "reach_ft",
                "surprised",
                "death_saves",
                "initiative",
                "tie_breaker",
            }
            unknown = set(entry) - allowed
            if unknown:
                raise ValueError(f"unsupported participant config fields: {sorted(unknown)}")
            visible_to = entry.get("visible_to_actor_ids")
            if visible_to is not None:
                if not isinstance(visible_to, list) or any(
                    str(item) not in participant_ids for item in visible_to
                ):
                    raise ValueError("visible_to_actor_ids must contain only participant actor IDs")
            config_by_actor[actor_id_value] = dict(entry)
        participants = [characters.get(item) for item in participant_ids]
        if any(char.campaign_id != campaign_id for char in participants):
            raise ValueError("all participants must belong to the campaign")
        actors = []
        for character_id in participant_ids:
            actor = combat_actor_snapshot(character_id)
            actor.update(config_by_actor.get(character_id, {}))
            actors.append(actor)
        encounter = start_encounter(
            actors,
            ruleset=ruleset or str(campaign.settings.get("edition") or "2024"),
            scene_id=scene_id,
            name=name,
        )
        updated_state = dict(campaign.state or {})
        updated_state["combat"] = encounter
        updated_state["game_phase"] = PROFILE_COMBAT
        updated_state = validate_party_state(updated_state)
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=updated_state,
            expected_campaign_revision=campaign.revision,
            operation="combat.start",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
        )
        response = {
            "combat": encounter,
            "tool_profile": PROFILE_COMBAT,
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return combat_response(
            campaign_id,
            principal_id,
            remember_idempotent(scope, idempotency_key, payload, response, campaign_id=campaign_id),
        )

    @mcp.tool()
    def combat_status(
        campaign_id: str,
        principal_id: str = "system:local",
    ) -> dict[str, Any] | None:
        """Read an audience-filtered structured encounter."""
        return combat_view(campaign_id, principal_id)

    @mcp.tool()
    def combat_available_actions(
        campaign_id: str,
        actor_id: str,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """List legal action categories without consuming a turn resource."""
        access.require_actor(campaign_id, actor_id, principal_id, private=True)
        campaign = campaigns.get(campaign_id)
        campaign, encounter = active_encounter(campaign_id)
        combatant = next(
            (item for item in encounter.get("combatants", []) if item.get("actor_id") == actor_id),
            None,
        )
        if combatant is None:
            raise CombatEngineError("actor is not a combatant")
        return {
            "actor_id": actor_id,
            "actions": available_actions(encounter, actor_id),
            "turn_budget": combatant.get("turn_budget", {}),
        }

    @mcp.tool()
    def combat_preflight_attack(
        campaign_id: str,
        actor_id: str,
        target_id: str,
        action: dict[str, Any] | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Validate an attack and return a non-mutating resolution plan."""
        access.require_actor(campaign_id, actor_id, principal_id, control=True)
        campaign, encounter = active_encounter(campaign_id)
        require_campaign_actor(campaign_id, target_id)
        action = sanitize_attack_action(campaign_id, principal_id, dict(action or {}))
        try:
            plan = preflight_attack(
                combat_actor_snapshot(actor_id),
                combat_actor_snapshot(target_id),
                action={**action, "target_id": target_id},
                encounter=encounter,
            )
        except NeedsRulingError:
            if access.require_campaign(campaign_id, principal_id).role not in {"owner", "dm"}:
                raise CombatEngineError("attack requires a DM ruling") from None
            raise
        # Mechanical details are for the commit path and DM audit only.  A
        # player receives an opaque plan token and the legal action metadata,
        # never target AC, attack bonus, or damage formulas.
        membership = access.require_campaign(campaign_id, principal_id)
        if membership.role not in {"owner", "dm"}:
            return {
                "status": plan["status"],
                "kind": plan["kind"],
                "attacker_id": plan["attacker_id"],
                "target_id": plan["target_id"],
                "weapon_id": plan.get("weapon_id"),
                "opaque": True,
            }
        return plan

    @mcp.tool()
    def combat_resolve_attack(
        campaign_id: str,
        actor_id: str,
        target_id: str,
        action: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        branch_id: str | None = None,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Resolve an attack and atomically update the attacker, target and encounter."""
        access.require_actor(campaign_id, actor_id, principal_id, control=True)
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        if actor_id == target_id:
            raise CombatEngineError("an actor cannot attack itself")
        campaign = campaigns.get(campaign_id)
        action_payload = sanitize_attack_action(campaign_id, principal_id, dict(action or {}))
        payload = {
            "actor_id": actor_id,
            "target_id": target_id,
            "action": action_payload,
            "branch_id": branch_id,
        }
        scope = f"combat-attack:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        if expected_revision is not None and campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        _, encounter = active_encounter(campaign_id)
        require_no_blocking_pending(encounter)
        require_campaign_actor(campaign_id, target_id)
        attacker = combat_actor_snapshot(actor_id)
        target = combat_actor_snapshot(target_id)
        try:
            plan = preflight_attack(attacker, target, action=action_payload, encounter=encounter)
        except NeedsRulingError:
            if access.require_campaign(campaign_id, principal_id).role not in {"owner", "dm"}:
                raise CombatEngineError("attack requires a DM ruling") from None
            raise
        updated_attacker, updated_target, result = resolve_attack_action(
            attacker,
            target,
            plan=plan,
        )
        ammunition = None
        weapon_id = plan.get("weapon_id")
        if weapon_id and weapon_id != "unarmed-strike":
            weapon = next(
                item for item in attacker["sheet"]["inventory"]["items"] if item["id"] == weapon_id
            )
            if weapon["mechanics"].get("ammunition_item_id"):
                updated_sheet, ammunition = consume_weapon_ammunition(
                    updated_attacker["sheet"], weapon_id
                )
                updated_attacker["sheet"] = updated_sheet
                result["ammunition"] = ammunition
        next_encounter = dict(encounter)
        current = next(
            item for item in next_encounter["combatants"] if item.get("actor_id") == actor_id
        )
        budget = dict(current.get("turn_budget") or {})
        if budget.get("attack_budget", 0) > 0:
            budget["attack_budget"] -= 1
        elif budget.get("main_action", 0) > 0:
            budget["main_action"] -= 1
            budget["attack_budget"] = max(
                0, int(attacker.get("derived", {}).get("attacks_per_action", 1) or 1) - 1
            )
        else:
            raise CombatEngineError("actor has no attack payment available")
        current["turn_budget"] = budget
        sync_combatant_conditions(next_encounter, actor_id, updated_attacker["sheet"])
        sync_combatant_conditions(next_encounter, target_id, updated_target["sheet"])
        reconcile_readied_spells(next_encounter, target_id, updated_target["sheet"])
        damage_result = result.get("damage")
        if isinstance(damage_result, dict):
            add_concentration_window(
                next_encounter,
                target_id,
                damage_result.get("concentration"),
                next_revision=campaign.revision + 1,
            )
        if isinstance(result.get("damage"), dict):
            result["damage"] = {
                key: value for key, value in result["damage"].items() if key != "sheet"
            }
        next_encounter["log"] = [
            *list(next_encounter.get("log") or []),
            {"type": "attack", "result": result},
        ][-100:]
        next_state = dict(campaign.state or {})
        next_state["combat"] = next_encounter
        updates = [
            CharacterStateUpdate(
                character_id=actor_id,
                sheet=validate_character_sheet(updated_attacker["sheet"]),
                notes=validate_character_notes(characters.get(actor_id).notes),
                expected_revision=characters.get(actor_id).revision,
            ),
            CharacterStateUpdate(
                character_id=target_id,
                sheet=validate_character_sheet(updated_target["sheet"]),
                notes=validate_character_notes(characters.get(target_id).notes),
                expected_revision=characters.get(target_id).revision,
            ),
        ]
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            character_updates=updates,
            expected_campaign_revision=campaign.revision,
            operation="combat.attack.resolve",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
        )
        response = {
            "status": "committed",
            "result": result,
            "combat": next_encounter,
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return combat_response(
            campaign_id,
            principal_id,
            remember_idempotent(scope, idempotency_key, payload, response, campaign_id=campaign_id),
        )

    @mcp.tool()
    def combat_end_turn(
        campaign_id: str,
        actor_id: str,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Advance a structured encounter turn with optimistic concurrency."""
        access.require_actor(campaign_id, actor_id, principal_id, control=True)
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        campaign = campaigns.get(campaign_id)
        payload = {"actor_id": actor_id, "branch_id": resolved_branch_id}
        scope = f"combat-end-turn:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        if expected_revision is not None and campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        _, encounter = active_encounter(campaign_id)
        before_readied = list(encounter.get("readied", []))
        current = characters.get(actor_id)
        duration = advance_effect_durations(current.sheet, period="turn_end")
        next_state = dict(campaign.state or {})
        next_state["combat"] = end_turn(encounter, actor_id_value=actor_id)
        remaining_readied_ids = {
            str(item.get("id")) for item in next_state["combat"].get("readied", [])
        }
        expired_readied = [
            item
            for item in before_readied
            if item.get("kind") == "spell" and str(item.get("id")) not in remaining_readied_ids
        ]
        next_combatant = current_combatant(next_state["combat"])
        round_changed = int(next_state["combat"].get("round", 1)) > int(encounter.get("round", 1))
        combat_updates: list[CharacterStateUpdate] = []
        expired_effects = set(duration["expired"])
        for combatant in next_state["combat"].get("combatants", []):
            target_id = str(combatant.get("actor_id"))
            target = characters.get(target_id)
            sheet = duration["sheet"] if target_id == actor_id else target.sheet
            for readied in expired_readied:
                if str(readied.get("actor_id")) != target_id:
                    continue
                for effect in sheet.get("effects", []):
                    if effect.get("id") == readied.get("holding_effect_id"):
                        effect["active"] = False
            expired: list[str] = []
            if next_combatant and target_id == next_combatant.get("actor_id"):
                started = advance_effect_durations(sheet, period="turn_start")
                sheet = started["sheet"]
                expired.extend(started["expired"])
            if round_changed:
                rounded = advance_effect_durations(sheet, period="round")
                sheet = rounded["sheet"]
                expired.extend(rounded["expired"])
            expired_effects.update(expired)
            combat_updates.append(
                CharacterStateUpdate(
                    character_id=target_id,
                    sheet=validate_character_sheet(sheet),
                    notes=validate_character_notes(target.notes),
                    expected_revision=target.revision,
                )
            )
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            character_updates=combat_updates,
            expected_campaign_revision=campaign.revision,
            operation="combat.turn.end",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
        )
        response = {
            "status": "committed",
            "combat": next_state["combat"],
            "effects_expired": sorted(expired_effects),
            "readied_spells_expired": sorted(str(item.get("id")) for item in expired_readied),
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return combat_response(
            campaign_id,
            principal_id,
            remember_idempotent(scope, idempotency_key, payload, response, campaign_id=campaign_id),
        )

    @mcp.tool()
    def campaign_advance_effects(
        campaign_id: str,
        period: str,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Advance a campaign's minute/hour/day timed effects in one atomic mutation."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        normalized_period = str(period).strip().lower().replace("-", "_")
        if normalized_period not in {"minute", "hour", "day", "round", "encounter"}:
            raise ValueError("period must be minute, hour, day, round, or encounter")
        payload = {"period": normalized_period, "branch_id": resolved_branch_id}
        scope = f"campaign-advance-effects:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        campaign = campaigns.get(campaign_id)
        if expected_revision is not None and campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        updates: list[CharacterStateUpdate] = []
        advanced: dict[str, list[str]] = {}
        expired: dict[str, list[str]] = {}
        for character in characters.list(campaign_id=campaign_id):
            result = advance_effect_durations(character.sheet, period=normalized_period)
            if not result["advanced"] and not result["expired"]:
                continue
            updates.append(
                CharacterStateUpdate(
                    character_id=character.id,
                    sheet=validate_character_sheet(result["sheet"]),
                    notes=validate_character_notes(character.notes),
                    expected_revision=character.revision,
                )
            )
            advanced[character.id] = result["advanced"]
            expired[character.id] = result["expired"]
        revisions_result = None
        if updates:
            revisions_result = StateMutationService(storage.database).replace(
                campaign_id,
                character_updates=updates,
                expected_campaign_revision=campaign.revision,
                operation="campaign.effects.advance",
                actor=principal_id,
                branch_id=resolved_branch_id,
                idempotency_key=idempotency_key,
            )
        response = {
            "status": "committed" if updates else "no_change",
            "period": normalized_period,
            "advanced": advanced,
            "expired": expired,
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
        )

    @mcp.tool()
    def combat_reaction_attack(
        campaign_id: str,
        actor_id: str,
        choice_id: str,
        target_id: str,
        action: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        branch_id: str | None = None,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Resolve an owned opportunity-attack window atomically with its attack."""
        access.require_actor(campaign_id, actor_id, principal_id, control=True)
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        if actor_id == target_id:
            raise CombatEngineError("an actor cannot attack itself")
        campaign = campaigns.get(campaign_id)
        action_payload = sanitize_attack_action(campaign_id, principal_id, dict(action or {}))
        payload = {
            "actor_id": actor_id,
            "choice_id": choice_id,
            "target_id": target_id,
            "action": action_payload,
            "branch_id": resolved_branch_id,
        }
        scope = f"combat-reaction-attack:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        if expected_revision is not None and campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        _, encounter = active_encounter(campaign_id)
        window = next(
            (item for item in encounter.get("pending", []) if item.get("id") == choice_id),
            None,
        )
        if (
            not isinstance(window, dict)
            or window.get("kind") != "reaction"
            or window.get("actor_id") != actor_id
            or window.get("trigger") != "opportunity_attack"
            or window.get("target_id") != target_id
        ):
            raise CombatEngineError("choice_id is not this actor's opportunity-attack window")
        require_campaign_actor(campaign_id, target_id)
        attacker = combat_actor_snapshot(actor_id)
        target = combat_actor_snapshot(target_id)
        combatant_position = next(
            (
                item.get("position")
                for item in encounter.get("combatants", [])
                if item.get("actor_id") == actor_id
            ),
            None,
        )
        if isinstance(combatant_position, dict):
            attacker["position"] = dict(combatant_position)
        if isinstance(window.get("target_position"), dict):
            target["position"] = dict(window["target_position"])
        if window.get("target_visible"):
            action_payload = dict(action_payload)
            action_payload["context"] = {
                **dict(action_payload.get("context") or {}),
                "attacker_can_see_target": True,
            }
        plan = preflight_attack(
            attacker,
            target,
            action=action_payload,
            encounter=None,
            allow_out_of_turn=True,
        )
        updated_attacker, updated_target, result = resolve_attack_action(
            attacker,
            target,
            plan=plan,
        )
        weapon_id = plan.get("weapon_id")
        if weapon_id and weapon_id != "unarmed-strike":
            weapon = next(
                item for item in attacker["sheet"]["inventory"]["items"] if item["id"] == weapon_id
            )
            if weapon["mechanics"].get("ammunition_item_id"):
                updated_sheet, ammunition = consume_weapon_ammunition(
                    updated_attacker["sheet"], weapon_id
                )
                updated_attacker["sheet"] = updated_sheet
                result["ammunition"] = ammunition
        next_encounter = resolve_choice_window(
            encounter,
            choice_id=choice_id,
            actor_id_value=actor_id,
            selection={"id": "opportunity_attack"},
        )
        combatant = next(
            item for item in next_encounter["combatants"] if item.get("actor_id") == actor_id
        )
        budget = dict(combatant.get("turn_budget") or {})
        if int(budget.get("reaction", 0) or 0) <= 0:
            raise CombatEngineError("actor has no reaction remaining")
        budget["reaction"] = int(budget["reaction"]) - 1
        combatant["turn_budget"] = budget
        sync_combatant_conditions(next_encounter, actor_id, updated_attacker["sheet"])
        sync_combatant_conditions(next_encounter, target_id, updated_target["sheet"])
        reconcile_readied_spells(next_encounter, target_id, updated_target["sheet"])
        damage_result = result.get("damage")
        if isinstance(damage_result, dict):
            add_concentration_window(
                next_encounter,
                target_id,
                damage_result.get("concentration"),
                next_revision=campaign.revision + 1,
            )
        if isinstance(result.get("damage"), dict):
            result["damage"] = {
                key: value for key, value in result["damage"].items() if key != "sheet"
            }
        next_encounter["log"] = [
            *list(next_encounter.get("log") or []),
            {"type": "reaction_attack", "choice_id": choice_id, "result": result},
        ][-100:]
        next_state = {**dict(campaign.state or {}), "combat": next_encounter}
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            character_updates=[
                CharacterStateUpdate(
                    character_id=actor_id,
                    sheet=validate_character_sheet(updated_attacker["sheet"]),
                    notes=validate_character_notes(characters.get(actor_id).notes),
                    expected_revision=characters.get(actor_id).revision,
                ),
                CharacterStateUpdate(
                    character_id=target_id,
                    sheet=validate_character_sheet(updated_target["sheet"]),
                    notes=validate_character_notes(characters.get(target_id).notes),
                    expected_revision=characters.get(target_id).revision,
                ),
            ],
            expected_campaign_revision=campaign.revision,
            operation="combat.reaction.attack",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
        )
        response = {
            "status": "committed",
            "result": result,
            "combat": next_encounter,
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return combat_response(
            campaign_id,
            principal_id,
            remember_idempotent(scope, idempotency_key, payload, response, campaign_id=campaign_id),
        )

    @mcp.tool()
    def combat_move(
        campaign_id: str,
        actor_id: str,
        distance: int,
        destination: Any = None,
        path: list[Any] | None = None,
        movement_mode: str = "voluntary",
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Spend movement and open owned opportunity-reaction windows from known positions."""
        access.require_actor(campaign_id, actor_id, principal_id, control=True)
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        payload = {
            "actor_id": actor_id,
            "distance": distance,
            "destination": destination,
            "path": path,
            "movement_mode": movement_mode,
            "branch_id": resolved_branch_id,
        }
        scope = f"combat-move:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        campaign = campaigns.get(campaign_id)
        if expected_revision is not None and campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        _, encounter = active_encounter(campaign_id)
        require_no_blocking_pending(encounter)
        next_encounter = spend_movement(
            encounter,
            actor_id,
            distance,
            destination=destination,
            path=path,
            movement_mode=movement_mode,
        )
        next_state = {**dict(campaign.state or {}), "combat": next_encounter}
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            expected_campaign_revision=campaign.revision,
            operation="combat.movement.spend",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
        )
        response = {
            "status": "committed",
            "combat": next_encounter,
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return combat_response(
            campaign_id,
            principal_id,
            remember_idempotent(scope, idempotency_key, payload, response, campaign_id),
        )

    @mcp.tool()
    def combat_common_action(
        campaign_id: str,
        actor_id: str,
        action: str,
        target_id: str | None = None,
        trigger: str | None = None,
        payload: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Settle Dash, Disengage, Dodge, Help, Hide, Search, or Ready action payment."""
        access.require_actor(campaign_id, actor_id, principal_id, control=True)
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        payload_value = {
            "actor_id": actor_id,
            "action": action,
            "target_id": target_id,
            "trigger": trigger,
            "payload": payload or {},
            "branch_id": resolved_branch_id,
        }
        scope = f"combat-common-action:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload_value)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        campaign, encounter = active_encounter(campaign_id)
        require_no_blocking_pending(encounter)
        if campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        if str(action).strip().lower().replace("-", "_") == "cast":
            raise CombatEngineError("use combat_cast_spell for spell resource settlement")
        if target_id is not None:
            require_campaign_actor(campaign_id, target_id)
        next_encounter = resolve_common_action(
            encounter,
            actor_id_value=actor_id,
            action=action,
            target_id=target_id,
            trigger=trigger,
            payload=payload,
        )
        next_state = {**dict(campaign.state or {}), "combat": next_encounter}
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            expected_campaign_revision=campaign.revision,
            operation=f"combat.common.{action}",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
        )
        response = {
            "status": "committed",
            "action": action,
            "combat": next_encounter,
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return combat_response(
            campaign_id,
            principal_id,
            remember_idempotent(scope, idempotency_key, payload_value, response, campaign_id),
        )

    @mcp.tool()
    def combat_reactions(
        campaign_id: str,
        actor_id: str,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        """Read reaction windows an actor may resolve outside its own turn."""
        access.require_actor(campaign_id, actor_id, principal_id, control=True)
        _campaign, encounter = active_encounter(campaign_id)
        return available_reactions(encounter, actor_id)

    @mcp.tool()
    def combat_cast_spell(
        campaign_id: str,
        actor_id: str,
        spell_id: str,
        cast_level: int | None = None,
        ritual: bool = False,
        choice_id: str | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Pay a combat action and canonical spell resources in one state mutation."""
        access.require_actor(campaign_id, actor_id, principal_id, control=True)
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        payload = {
            "actor_id": actor_id,
            "spell_id": spell_id,
            "cast_level": cast_level,
            "ritual": ritual,
            "choice_id": choice_id,
            "branch_id": resolved_branch_id,
        }
        scope = f"combat-cast:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        campaign, encounter = active_encounter(campaign_id)
        if campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        current = characters.get(actor_id)
        applied = consume_spell_cast(
            current.sheet,
            spell_id=spell_id,
            cast_level=cast_level,
            ritual=ritual,
        )
        spell_entry = next(
            item
            for item in current.sheet.get("content", {}).get("spells", [])
            if item.get("id") == spell_id
        )
        casting_time = str(spell_entry.get("definition", {}).get("casting_time") or "1 action")
        normalized_casting_time = casting_time.casefold().strip()
        if ritual and not normalized_casting_time.startswith(
            ("1 action", "bonus action", "reaction")
        ):
            raise CombatEngineError("ritual casting cannot be completed inside an active encounter")
        if normalized_casting_time.startswith("bonus action"):
            payment = "bonus_action"
        elif normalized_casting_time.startswith("reaction"):
            payment = "reaction"
        elif normalized_casting_time.startswith("1 action"):
            payment = "main_action"
        else:
            raise CombatEngineError(
                "this spell's casting time requires an explicit out-of-combat time ruling"
            )
        if payment == "reaction":
            window = next(
                (
                    item
                    for item in encounter.get("pending", [])
                    if item.get("id") == choice_id
                    and item.get("kind") == "reaction"
                    and item.get("actor_id") == actor_id
                    and item.get("status", "pending") == "pending"
                ),
                None,
            )
            if window is None:
                raise CombatEngineError(
                    "a reaction spell requires its owned pending reaction choice_id"
                )
            if any(
                item.get("status", "pending") == "pending" and item.get("id") != choice_id
                for item in encounter.get("pending", [])
            ):
                raise CombatEngineError("resolve the earlier pending save or choice first")
        else:
            require_no_blocking_pending(encounter)

        ruleset = str(encounter.get("ruleset") or current.sheet.get("edition") or "2014")
        spell_level = int(spell_entry.get("level", 0) or 0)
        spent_slot = applied["payment"].get("economy") == "slots"
        turn_casts = list(dict(encounter.get("turn_spell_casts") or {}).get(actor_id, []))
        if ruleset == "2024" and spent_slot and any(item.get("spent_slot") for item in turn_casts):
            raise CombatEngineError("2024 rules allow only one expended spell slot per turn")
        if ruleset == "2014":
            current_is_bonus = payment == "bonus_action"
            previous_bonus = any(item.get("payment") == "bonus_action" for item in turn_casts)
            if current_is_bonus or previous_bonus:
                casts = [
                    *turn_casts,
                    {
                        "payment": payment,
                        "spell_level": spell_level,
                        "casting_time": normalized_casting_time,
                    },
                ]
                if any(
                    item.get("payment") != "bonus_action"
                    and not (
                        int(item.get("spell_level", 1)) == 0
                        and str(item.get("casting_time") or "").startswith("1 action")
                    )
                    for item in casts
                ):
                    raise CombatEngineError(
                        "2014 bonus-action spell rule permits only a 1-action cantrip "
                        "as another spell on the same turn"
                    )
        next_encounter = resolve_common_action(
            encounter,
            actor_id_value=actor_id,
            action="cast",
            payload={"spell_id": spell_id, "cast_level": cast_level, "ritual": ritual},
            payment=payment,
        )
        if payment == "reaction":
            assert choice_id is not None
            next_encounter = resolve_choice_window(
                next_encounter,
                choice_id=choice_id,
                actor_id_value=actor_id,
                selection={"id": spell_id, "kind": "reaction_spell"},
            )
        casts_by_actor = dict(next_encounter.get("turn_spell_casts") or {})
        casts_by_actor[actor_id] = [
            *turn_casts,
            {
                "spell_id": spell_id,
                "spell_level": spell_level,
                "payment": payment,
                "casting_time": normalized_casting_time,
                "spent_slot": spent_slot,
            },
        ]
        next_encounter["turn_spell_casts"] = casts_by_actor
        sync_combatant_conditions(next_encounter, actor_id, applied["sheet"])
        next_state = {**dict(campaign.state or {}), "combat": next_encounter}
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            character_updates=[
                CharacterStateUpdate(
                    character_id=actor_id,
                    sheet=validate_character_sheet(applied["sheet"]),
                    notes=validate_character_notes(current.notes),
                    expected_revision=current.revision,
                )
            ],
            expected_campaign_revision=campaign.revision,
            operation="combat.spell.cast",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
        )
        response = {
            "status": "committed",
            "result": {key: value for key, value in applied.items() if key != "sheet"},
            "combat": next_encounter,
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return combat_response(
            campaign_id,
            principal_id,
            remember_idempotent(scope, idempotency_key, payload, response, campaign_id),
        )

    @mcp.tool()
    def combat_ready_spell(
        campaign_id: str,
        actor_id: str,
        spell_id: str,
        trigger: str,
        cast_level: int | None = None,
        declaration: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Cast and hold a one-action spell, paying its action, slot, and concentration now."""
        access.require_actor(campaign_id, actor_id, principal_id, control=True)
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        payload = {
            "actor_id": actor_id,
            "spell_id": spell_id,
            "trigger": trigger,
            "cast_level": cast_level,
            "declaration": declaration or {},
            "branch_id": resolved_branch_id,
        }
        scope = f"combat-ready-spell:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        campaign, encounter = active_encounter(campaign_id)
        require_no_blocking_pending(encounter)
        if campaign.revision != expected_revision:
            raise ValueError(
                f"campaign revision conflict: expected {expected_revision}, "
                f"found {campaign.revision}"
            )
        current = characters.get(actor_id)
        applied = consume_readied_spell(
            current.sheet,
            spell_id=spell_id,
            cast_level=cast_level,
        )
        spell_entry = next(
            item
            for item in current.sheet.get("content", {}).get("spells", [])
            if item.get("id") == spell_id
        )
        spell_level = int(spell_entry.get("level", 0) or 0)
        spent_slot = applied["payment"].get("economy") == "slots"
        turn_casts = list(dict(encounter.get("turn_spell_casts") or {}).get(actor_id, []))
        ruleset = str(encounter.get("ruleset") or current.sheet.get("edition") or "2014")
        if ruleset == "2024" and spent_slot and any(item.get("spent_slot") for item in turn_casts):
            raise CombatEngineError("2024 rules allow only one expended spell slot per turn")
        if (
            ruleset == "2014"
            and any(item.get("payment") == "bonus_action" for item in turn_casts)
            and spell_level != 0
        ):
            raise CombatEngineError(
                "2014 bonus-action spell rule permits only a 1-action cantrip "
                "as another spell on the same turn"
            )
        next_encounter = resolve_common_action(
            encounter,
            actor_id_value=actor_id,
            action="cast",
            payload={"spell_id": spell_id, "cast_level": cast_level, "readied": True},
            payment="main_action",
        )
        next_encounter = arm_readied_spell(
            next_encounter,
            actor_id_value=actor_id,
            spell_id=spell_id,
            trigger=trigger,
            holding_effect_id=applied["holding_effect_id"],
            release_concentration=applied["release_concentration"],
            release_duration=applied["release_duration"],
            release_effect_kind=applied["release_effect_kind"],
            declaration=declaration,
        )
        casts_by_actor = dict(next_encounter.get("turn_spell_casts") or {})
        casts_by_actor[actor_id] = [
            *turn_casts,
            {
                "spell_id": spell_id,
                "spell_level": spell_level,
                "payment": "main_action",
                "casting_time": applied["casting_time"],
                "spent_slot": spent_slot,
                "readied": True,
            },
        ]
        next_encounter["turn_spell_casts"] = casts_by_actor
        sync_combatant_conditions(next_encounter, actor_id, applied["sheet"])
        reconcile_readied_spells(next_encounter, actor_id, applied["sheet"])
        next_state = {**dict(campaign.state or {}), "combat": next_encounter}
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            character_updates=[
                CharacterStateUpdate(
                    character_id=actor_id,
                    sheet=validate_character_sheet(applied["sheet"]),
                    notes=validate_character_notes(current.notes),
                    expected_revision=current.revision,
                )
            ],
            expected_campaign_revision=campaign.revision,
            operation="combat.spell.ready",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
        )
        readied = next_encounter["readied"][-1]
        response = {
            "status": "armed",
            "readied": readied,
            "result": {key: item for key, item in applied.items() if key != "sheet"},
            "combat": next_encounter,
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return combat_response(
            campaign_id,
            principal_id,
            remember_idempotent(scope, idempotency_key, payload, response, campaign_id),
        )

    @mcp.tool()
    def combat_readied_spell_trigger(
        campaign_id: str,
        readied_id: str,
        event: str,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Confirm that a readied spell's perceivable trigger occurred and open its reaction."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        payload = {"readied_id": readied_id, "event": event, "branch_id": resolved_branch_id}
        scope = f"combat-ready-trigger:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        campaign, encounter = active_encounter(campaign_id)
        if campaign.revision != expected_revision:
            raise ValueError(
                f"campaign revision conflict: expected {expected_revision}, "
                f"found {campaign.revision}"
            )
        readied = next(
            (item for item in encounter.get("readied", []) if item.get("id") == readied_id),
            None,
        )
        if readied is None:
            raise CombatEngineError("readied spell not found")
        actor = characters.get(str(readied["actor_id"]))
        active_effect_ids = {
            str(effect.get("id"))
            for effect in actor.sheet.get("effects", [])
            if effect.get("active") and effect.get("concentration")
        }
        if str(readied.get("holding_effect_id")) not in active_effect_ids:
            next_encounter = deepcopy(encounter)
            expired = reconcile_readied_spells(next_encounter, actor.id, actor.sheet)
            next_state = {**dict(campaign.state or {}), "combat": next_encounter}
            revisions_result = StateMutationService(storage.database).replace(
                campaign_id,
                campaign_state=validate_party_state(next_state),
                expected_campaign_revision=campaign.revision,
                operation="combat.spell.ready.dissipate",
                actor=principal_id,
                branch_id=resolved_branch_id,
                idempotency_key=idempotency_key,
            )
            response = {
                "status": "dissipated",
                "readied_spells_expired": expired,
                "combat": next_encounter,
                "campaign_revision": mutation_revision(campaign_id),
                "revisions": [asdict(item) for item in revisions_result or []],
            }
            return combat_response(
                campaign_id,
                principal_id,
                remember_idempotent(scope, idempotency_key, payload, response, campaign_id),
            )
        next_encounter = trigger_readied_spell(encounter, readied_id=readied_id, event=event)
        next_state = {**dict(campaign.state or {}), "combat": next_encounter}
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            expected_campaign_revision=campaign.revision,
            operation="combat.spell.ready.trigger",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
        )
        response = {
            "status": "pending",
            "choice": next_encounter["pending"][-1],
            "combat": next_encounter,
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return combat_response(
            campaign_id,
            principal_id,
            remember_idempotent(scope, idempotency_key, payload, response, campaign_id),
        )

    @mcp.tool()
    def combat_readied_spell_resolve(
        campaign_id: str,
        actor_id: str,
        choice_id: str,
        release: bool,
        declaration: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Release a held spell with a reaction or ignore this occurrence of its trigger."""
        access.require_actor(campaign_id, actor_id, principal_id, control=True)
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        payload = {
            "actor_id": actor_id,
            "choice_id": choice_id,
            "release": release,
            "declaration": declaration or {},
            "branch_id": resolved_branch_id,
        }
        scope = f"combat-ready-resolve:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        campaign, encounter = active_encounter(campaign_id)
        if campaign.revision != expected_revision:
            raise ValueError(
                f"campaign revision conflict: expected {expected_revision}, "
                f"found {campaign.revision}"
            )
        window = next(
            (item for item in encounter.get("pending", []) if item.get("id") == choice_id),
            None,
        )
        readied = next(
            (
                item
                for item in encounter.get("readied", [])
                if window is not None and item.get("id") == window.get("readied_id")
            ),
            None,
        )
        if readied is None or readied.get("actor_id") != actor_id:
            raise CombatEngineError("choice_id is not this actor's readied spell")
        actor = characters.get(actor_id)
        sheet = deepcopy(actor.sheet)
        holding_effect = next(
            (
                effect
                for effect in sheet.get("effects", [])
                if effect.get("id") == readied.get("holding_effect_id")
            ),
            None,
        )
        if holding_effect is None or not holding_effect.get("active"):
            next_encounter = deepcopy(encounter)
            expired = reconcile_readied_spells(next_encounter, actor_id, sheet)
            next_state = {**dict(campaign.state or {}), "combat": next_encounter}
            revisions_result = StateMutationService(storage.database).replace(
                campaign_id,
                campaign_state=validate_party_state(next_state),
                expected_campaign_revision=campaign.revision,
                operation="combat.spell.ready.dissipate",
                actor=principal_id,
                branch_id=resolved_branch_id,
                idempotency_key=idempotency_key,
            )
            response = {
                "status": "dissipated",
                "readied_spells_expired": expired,
                "combat": next_encounter,
                "campaign_revision": mutation_revision(campaign_id),
                "revisions": [asdict(item) for item in revisions_result or []],
            }
            return combat_response(
                campaign_id,
                principal_id,
                remember_idempotent(scope, idempotency_key, payload, response, campaign_id),
            )
        if release and choice_id not in {
            str(item.get("id")) for item in available_reactions(encounter, actor_id)
        }:
            raise CombatEngineError("actor cannot take this reaction")
        next_encounter, resolved = resolve_readied_spell_window(
            encounter,
            actor_id_value=actor_id,
            choice_id=choice_id,
            release=release,
        )
        updates: list[CharacterStateUpdate] = []
        if release:
            if resolved.get("release_concentration"):
                holding_effect["kind"] = resolved.get("release_effect_kind") or "concentration"
                holding_effect["source"] = "spell.cast"
                holding_effect["duration"] = deepcopy(resolved.get("release_duration") or {})
            else:
                holding_effect["active"] = False
            sheet = validate_character_sheet(sheet)
            updates.append(
                CharacterStateUpdate(
                    character_id=actor_id,
                    sheet=sheet,
                    notes=validate_character_notes(actor.notes),
                    expected_revision=actor.revision,
                )
            )
        next_encounter["log"] = [
            *list(next_encounter.get("log") or []),
            {
                "type": "readied_spell_released" if release else "readied_spell_declined",
                "actor_id": actor_id,
                "readied_id": resolved.get("id"),
                "spell_id": resolved.get("spell_id"),
                "declaration": declaration or {},
            },
        ][-100:]
        next_state = {**dict(campaign.state or {}), "combat": next_encounter}
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            character_updates=updates,
            expected_campaign_revision=campaign.revision,
            operation="combat.spell.ready.release" if release else "combat.spell.ready.decline",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
        )
        response = {
            "status": "pending_ruling" if release else "armed",
            "released": release,
            "spell_id": resolved.get("spell_id"),
            "declaration": declaration or {},
            "combat": next_encounter,
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return combat_response(
            campaign_id,
            principal_id,
            remember_idempotent(scope, idempotency_key, payload, response, campaign_id),
        )

    @mcp.tool()
    def combat_use_activity(
        campaign_id: str,
        actor_id: str,
        activity_id: str,
        declaration: dict[str, Any] | None = None,
        choice_id: str | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Pay one structured activity's resource and activation timing, never its prose outcome."""
        access.require_actor(campaign_id, actor_id, principal_id, control=True)
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        payload = {
            "actor_id": actor_id,
            "activity_id": activity_id,
            "declaration": declaration or {},
            "choice_id": choice_id,
            "branch_id": resolved_branch_id,
        }
        scope = f"combat-activity:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        campaign, encounter = active_encounter(campaign_id)
        if expected_revision is not None and campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        current = characters.get(actor_id)
        try:
            applied = consume_activity(current.sheet, activity_id=activity_id)
        except ActivityError as exc:
            raise CombatEngineError(str(exc)) from exc
        activation_type = str(applied["activation"].get("type") or "")
        if activation_type == "reaction":
            window = next(
                (
                    item
                    for item in encounter.get("pending", [])
                    if item.get("id") == choice_id
                    and item.get("kind") == "reaction"
                    and item.get("actor_id") == actor_id
                    and item.get("status", "pending") == "pending"
                ),
                None,
            )
            if window is None:
                raise CombatEngineError(
                    "a reaction activity requires its owned pending reaction choice_id"
                )
            if any(
                item.get("status", "pending") == "pending" and item.get("id") != choice_id
                for item in encounter.get("pending", [])
            ):
                raise CombatEngineError("resolve the earlier pending save or choice first")
        else:
            require_no_blocking_pending(encounter)
        if activation_type == "special" and not is_dm(campaign_id, principal_id):
            raise CombatEngineError("special activity triggers require a DM resolution")
        next_encounter = pay_activity_activation(
            encounter,
            actor_id_value=actor_id,
            activation_type=activation_type,
        )
        if activation_type == "reaction":
            assert choice_id is not None
            next_encounter = resolve_choice_window(
                next_encounter,
                choice_id=choice_id,
                actor_id_value=actor_id,
                selection={"id": activity_id, "kind": "reaction_activity"},
            )
        next_encounter["log"] = [
            *list(next_encounter.get("log") or []),
            {
                "type": "activity",
                "actor_id": actor_id,
                "activity_id": activity_id,
                "declaration": declaration or {},
                "requires_ruling": applied["requires_ruling"],
            },
        ][-100:]
        next_state = {**dict(campaign.state or {}), "combat": next_encounter}
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            character_updates=[
                CharacterStateUpdate(
                    character_id=actor_id,
                    sheet=validate_character_sheet(applied["sheet"]),
                    notes=validate_character_notes(current.notes),
                    expected_revision=current.revision,
                )
            ],
            expected_campaign_revision=campaign.revision,
            operation="combat.activity.use",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
        )
        result = {key: value for key, value in applied.items() if key != "sheet"}
        result["declaration"] = declaration or {}
        response = {
            "status": "pending_ruling" if applied["requires_ruling"] else "committed",
            "result": result,
            "combat": next_encounter,
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return combat_response(
            campaign_id,
            principal_id,
            remember_idempotent(scope, idempotency_key, payload, response, campaign_id),
        )

    @mcp.tool()
    def combat_check(
        campaign_id: str,
        actor_id: str,
        kind: str,
        ability: str,
        dc: int = 0,
        proficient: bool = False,
        bonus: int = 0,
        advantage: bool = False,
        disadvantage: bool = False,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a check/save/death-save and persist only its mechanical result."""
        access.require_actor(campaign_id, actor_id, principal_id, control=True)
        require_write_contract(expected_revision, idempotency_key)
        if not is_dm(campaign_id, principal_id):
            if kind != "death_save":
                raise CombatEngineError("checks and saves require a DM-issued resolution")
            if advantage or disadvantage or proficient or bonus:
                raise CombatEngineError("death-save modifiers require a DM ruling")
        payload = {
            "actor_id": actor_id,
            "kind": kind,
            "ability": ability,
            "dc": dc,
            "proficient": proficient,
            "bonus": bonus,
            "advantage": advantage,
            "disadvantage": disadvantage,
            "branch_id": branch_id,
        }
        scope = f"combat-check:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        campaign = campaigns.get(campaign_id)
        if expected_revision is not None and campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        death_save_combatant: dict[str, Any] | None = None
        if kind == "death_save":
            _campaign, active = active_encounter(campaign_id)
            require_no_blocking_pending(active)
            death_save_combatant = current_combatant(active)
            if death_save_combatant is None or death_save_combatant.get("actor_id") != actor_id:
                raise CombatEngineError(
                    "a death save is made only at the start of this actor's turn"
                )
            if not death_save_combatant.get("death_saves", False):
                raise CombatEngineError("this combatant is not configured to make death saves")
            if dict(death_save_combatant.get("turn_flags") or {}).get("death_save_used"):
                raise CombatEngineError("this actor already made a death save this turn")
        actor = combat_actor_snapshot(actor_id)
        next_state = dict(campaign.state or {})
        encounter = dict(next_state.get("combat") or {})
        updates: list[CharacterStateUpdate] = []
        if kind == "death_save":
            exhaustion = int(actor["sheet"].get("combat", {}).get("exhaustion", 0) or 0)
            ruleset = str(encounter.get("ruleset") or actor["sheet"].get("edition") or "2014")
            death_save_bonus = -2 * exhaustion if ruleset == "2024" else 0
            if ruleset == "2014" and exhaustion >= 3:
                disadvantage = True
            updated = resolve_death_save_to_sheet(
                actor["sheet"],
                advantage=advantage,
                disadvantage=disadvantage,
                bonus=death_save_bonus,
            )
            result = {key: value for key, value in updated.items() if key != "sheet"}
            current = characters.get(actor_id)
            updates.append(
                CharacterStateUpdate(
                    character_id=actor_id,
                    sheet=validate_character_sheet(updated["sheet"]),
                    notes=validate_character_notes(current.notes),
                    expected_revision=current.revision,
                )
            )
        else:
            combatant = next(
                (
                    item
                    for item in encounter.get("combatants", [])
                    if item.get("actor_id") == actor_id
                ),
                None,
            )
            if combatant is not None and kind == "save" and ability in {"dex", "dexterity"}:
                flags = dict(combatant.get("turn_flags") or {})
                conditions = {str(item).casefold() for item in combatant.get("conditions", [])}
                if flags.get("dodging") and not conditions & {
                    "grappled",
                    "incapacitated",
                    "paralyzed",
                    "petrified",
                    "restrained",
                    "stunned",
                    "unconscious",
                }:
                    advantage = True
            result = resolve_actor_check(
                actor,
                kind=kind,
                ability=ability,
                dc=dc,
                proficient=proficient,
                bonus=bonus,
                advantage=advantage,
                disadvantage=disadvantage,
                ruleset=encounter.get("ruleset") if encounter else None,
            )
        if encounter:
            if updates:
                sync_combatant_conditions(encounter, actor_id, updates[0].sheet)
            if kind == "death_save":
                combatant = next(
                    item
                    for item in encounter.get("combatants", [])
                    if item.get("actor_id") == actor_id
                )
                flags = dict(combatant.get("turn_flags") or {})
                flags["death_save_used"] = True
                combatant["turn_flags"] = flags
            encounter["log"] = [
                *list(encounter.get("log") or []),
                {"type": kind, "actor_id": actor_id, "result": result},
            ][-100:]
            next_state["combat"] = encounter
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            character_updates=updates,
            expected_campaign_revision=campaign.revision,
            operation=f"combat.{kind}",
            actor=principal_id,
            branch_id=require_current_branch(campaign_id, branch_id),
            idempotency_key=idempotency_key,
        )
        response = {
            "status": "committed",
            "result": result,
            "combat": next_state.get("combat"),
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return combat_response(
            campaign_id,
            principal_id,
            remember_idempotent(scope, idempotency_key, payload, response, campaign_id),
        )

    @mcp.tool()
    def combat_concentration_check(
        campaign_id: str,
        target_id: str,
        dc: int,
        effect_ids: list[str],
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a pending concentration save and deactivate effects only on failure."""
        access.require_actor(campaign_id, target_id, principal_id, control=True)
        require_write_contract(expected_revision, idempotency_key)
        payload = {
            "target_id": target_id,
            "dc": dc,
            "effect_ids": list(effect_ids),
            "branch_id": branch_id,
        }
        scope = f"combat-concentration:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        campaign = campaigns.get(campaign_id)
        if expected_revision is not None and campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        encounter = dict(campaign.state or {}).get("combat")
        pending = next(
            (
                item
                for item in (encounter or {}).get("pending", [])
                if item.get("kind") == "concentration"
                and item.get("actor_id") == target_id
                and item.get("status") == "pending"
            ),
            None,
        )
        if pending is None:
            raise CombatEngineError("no pending concentration save for this actor")
        if int(pending.get("dc", 0)) != int(dc) or set(pending.get("effect_ids", [])) != set(
            effect_ids
        ):
            raise CombatEngineError(
                "concentration request does not match the pending damage window"
            )
        actor = combat_actor_snapshot(target_id)
        result = resolve_actor_check(
            actor,
            kind="save",
            ability="constitution",
            dc=dc,
        )
        updated_sheet = apply_concentration_result(
            actor["sheet"], effect_ids=effect_ids, success=result["success"]
        )
        current = characters.get(target_id)
        next_state = dict(campaign.state or {})
        if isinstance(encounter, dict):
            reconcile_readied_spells(encounter, target_id, updated_sheet)
            encounter["pending"] = [
                item for item in encounter.get("pending", []) if item.get("id") != pending.get("id")
            ]
            encounter["log"] = [
                *list(encounter.get("log") or []),
                {"type": "concentration", "actor_id": target_id, "result": result},
            ][-100:]
            next_state["combat"] = encounter
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            character_updates=[
                CharacterStateUpdate(
                    character_id=target_id,
                    sheet=validate_character_sheet(updated_sheet),
                    notes=validate_character_notes(current.notes),
                    expected_revision=current.revision,
                )
            ],
            expected_campaign_revision=campaign.revision,
            operation="combat.concentration.check",
            actor=principal_id,
            branch_id=require_current_branch(campaign_id, branch_id),
            idempotency_key=idempotency_key,
        )
        response = {
            "status": "committed",
            "result": result,
            "effects_active": result["success"],
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return combat_response(
            campaign_id,
            principal_id,
            remember_idempotent(scope, idempotency_key, payload, response, campaign_id),
        )

    @mcp.tool()
    def combat_apply_damage(
        campaign_id: str,
        target_id: str,
        parts: list[dict[str, Any]],
        critical: bool = False,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Apply DM-approved damage parts; automatic trait and HP settlement is deterministic."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        require_campaign_actor(campaign_id, target_id)
        payload = {
            "target_id": target_id,
            "parts": parts,
            "critical": critical,
            "branch_id": branch_id,
        }
        scope = f"combat-damage:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        campaign = campaigns.get(campaign_id)
        if expected_revision is not None and campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        target = combat_actor_snapshot(target_id)
        existing_encounter = dict(campaign.state or {}).get("combat")
        if isinstance(existing_encounter, dict) and existing_encounter.get("active", False):
            require_no_blocking_pending(existing_encounter)
        applied = apply_damage_parts_to_sheet(
            target["sheet"], parts, source=principal_id, critical=critical
        )
        encounter = existing_encounter
        next_state = dict(campaign.state or {})
        if encounter:
            sync_combatant_conditions(encounter, target_id, applied["sheet"])
            reconcile_readied_spells(encounter, target_id, applied["sheet"])
            add_concentration_window(
                encounter,
                target_id,
                applied.get("concentration"),
                next_revision=campaign.revision + 1,
            )
            encounter["log"] = [
                *list(encounter.get("log") or []),
                {"type": "damage", "target_id": target_id, "result": applied},
            ][-100:]
            next_state["combat"] = encounter
        current = characters.get(target_id)
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            character_updates=[
                CharacterStateUpdate(
                    character_id=target_id,
                    sheet=validate_character_sheet(applied["sheet"]),
                    notes=validate_character_notes(current.notes),
                    expected_revision=current.revision,
                )
            ],
            expected_campaign_revision=campaign.revision,
            operation="combat.damage.apply",
            actor=principal_id,
            branch_id=require_current_branch(campaign_id, branch_id),
            idempotency_key=idempotency_key,
        )
        response = {
            "status": "committed",
            "result": {key: value for key, value in applied.items() if key != "sheet"},
            "combat": next_state.get("combat"),
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return combat_response(
            campaign_id,
            principal_id,
            remember_idempotent(scope, idempotency_key, payload, response, campaign_id),
        )

    @mcp.tool()
    def combat_heal(
        campaign_id: str,
        target_id: str,
        amount: int,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Apply DM-approved healing with max-HP clamping."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        require_campaign_actor(campaign_id, target_id)
        if int(amount) <= 0:
            raise CombatEngineError("healing amount must be positive")
        payload = {"target_id": target_id, "amount": amount, "branch_id": branch_id}
        scope = f"combat-heal:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        campaign = campaigns.get(campaign_id)
        if expected_revision is not None and campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        target = combat_actor_snapshot(target_id)
        active = dict(campaign.state or {}).get("combat")
        if isinstance(active, dict) and active.get("active", False):
            require_no_blocking_pending(active)
        applied = apply_healing_to_sheet(target["sheet"], amount=amount)
        current = characters.get(target_id)
        next_state: dict[str, Any] | None = None
        encounter = dict(campaign.state or {}).get("combat")
        if isinstance(encounter, dict) and encounter.get("active", False):
            sync_combatant_conditions(encounter, target_id, applied["sheet"])
            next_state = {**dict(campaign.state or {}), "combat": encounter}
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state) if next_state is not None else None,
            character_updates=[
                CharacterStateUpdate(
                    character_id=target_id,
                    sheet=validate_character_sheet(applied["sheet"]),
                    notes=validate_character_notes(current.notes),
                    expected_revision=current.revision,
                )
            ],
            expected_campaign_revision=campaign.revision,
            operation="combat.heal.apply",
            actor=principal_id,
            branch_id=require_current_branch(campaign_id, branch_id),
            idempotency_key=idempotency_key,
        )
        response = {
            "status": "committed",
            "result": {key: value for key, value in applied.items() if key != "sheet"},
            "combat": next_state.get("combat") if next_state else None,
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return combat_response(
            campaign_id,
            principal_id,
            remember_idempotent(scope, idempotency_key, payload, response, campaign_id),
        )

    @mcp.tool()
    def combat_choice_open(
        campaign_id: str,
        actor_id: str,
        event: str,
        candidates: list[dict[str, Any]] | None = None,
        kind: str = "reaction",
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Open a reaction/ruling window; the engine never guesses a narrative choice."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        require_campaign_actor(campaign_id, actor_id)
        payload = {
            "actor_id": actor_id,
            "event": event,
            "candidates": candidates or [],
            "kind": kind,
            "branch_id": branch_id,
        }
        scope = f"combat-choice-open:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        campaign = campaigns.get(campaign_id)
        if expected_revision is not None and campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        _, encounter = active_encounter(campaign_id)
        next_encounter = add_choice_window(
            encounter,
            kind=kind,
            actor_id_value=actor_id,
            event=event,
            candidates=candidates or [],
        )
        next_state = {**dict(campaign.state or {}), "combat": next_encounter}
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            expected_campaign_revision=campaign.revision,
            operation="combat.choice.open",
            actor=principal_id,
            branch_id=require_current_branch(campaign_id, branch_id),
            idempotency_key=idempotency_key,
        )
        window = next_encounter["pending"][-1]
        response = {
            "status": "pending",
            "choice": window,
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return combat_response(
            campaign_id,
            principal_id,
            remember_idempotent(scope, idempotency_key, payload, response, campaign_id),
        )

    @mcp.tool()
    def combat_choice_resolve(
        campaign_id: str,
        actor_id: str,
        choice_id: str,
        selection: dict[str, Any],
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Commit one actor/DM choice and leave its downstream effect explicit."""
        access.require_actor(campaign_id, actor_id, principal_id, control=True)
        require_write_contract(expected_revision, idempotency_key)
        payload = {
            "actor_id": actor_id,
            "choice_id": choice_id,
            "selection": selection,
            "branch_id": branch_id,
        }
        scope = f"combat-choice-resolve:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        campaign = campaigns.get(campaign_id)
        if expected_revision is not None and campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        _, encounter = active_encounter(campaign_id)
        pending_choice = next(
            (item for item in encounter.get("pending", []) if item.get("id") == choice_id),
            None,
        )
        if pending_choice and pending_choice.get("trigger") == "readied_spell":
            raise CombatEngineError("readied-spell windows must use combat_readied_spell_resolve")
        next_encounter = resolve_choice_window(
            encounter,
            choice_id=choice_id,
            actor_id_value=actor_id,
            selection=selection,
        )
        selection_id = str(selection.get("id") or "").lower()
        if (
            pending_choice
            and pending_choice.get("kind") == "reaction"
            and pending_choice.get("trigger") == "opportunity_attack"
            and selection_id not in {"decline", "skip", "pass"}
        ):
            raise CombatEngineError(
                "opportunity attacks must be resolved with combat_reaction_attack"
            )
        if (
            pending_choice
            and pending_choice.get("kind") == "reaction"
            and selection_id not in {"decline", "skip", "pass"}
        ):
            combatant = next(
                item
                for item in next_encounter.get("combatants", [])
                if item.get("actor_id") == actor_id
            )
            budget = dict(combatant.get("turn_budget") or {})
            if int(budget.get("reaction", 0) or 0) <= 0:
                raise CombatEngineError("actor has no reaction remaining")
            budget["reaction"] = int(budget["reaction"]) - 1
            combatant["turn_budget"] = budget
        next_state = {**dict(campaign.state or {}), "combat": next_encounter}
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            expected_campaign_revision=campaign.revision,
            operation="combat.choice.resolve",
            actor=principal_id,
            branch_id=require_current_branch(campaign_id, branch_id),
            idempotency_key=idempotency_key,
        )
        response = {
            "status": "committed",
            "combat": next_encounter,
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return combat_response(
            campaign_id,
            principal_id,
            remember_idempotent(scope, idempotency_key, payload, response, campaign_id),
        )

    @mcp.tool()
    def branch_list(campaign_id: str, principal_id: str = "system:local") -> list[dict[str, Any]]:
        """List playable, non-destructive campaign timelines."""
        membership = access.require_campaign(campaign_id, principal_id)
        values = [asdict(item) for item in branches.list(campaign_id)]
        if membership.role not in {"owner", "dm"}:
            current = current_branch_id(campaign_id)
            return [item for item in values if item["id"] == current]
        return values

    @mcp.tool()
    def branch_compare(
        campaign_id: str,
        left_branch_id: str,
        right_branch_id: str,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Compare facts and actor knowledge across branches without auto-merging them."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        return branches.compare(campaign_id, left_branch_id, right_branch_id)

    @mcp.tool()
    def branch_create(
        campaign_id: str,
        name: str,
        from_snapshot_id: str | None = None,
        checkout: bool = False,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        expected_branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Fork a timeline from a snapshot without changing its source branch."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if expected_revision is None or expected_branch_id is None or not idempotency_key:
            raise ValueError(
                "expected_revision, expected_branch_id, and idempotency_key are required "
                "for branch creation"
            )
        request_payload = {
            "name": name,
            "from_snapshot_id": from_snapshot_id,
            "checkout": checkout,
            "expected_branch_id": expected_branch_id,
        }
        scope = f"branch-create:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, request_payload)
        if replay is not None:
            return replay
        campaign = campaigns.get(campaign_id)
        if campaign.revision != expected_revision:
            raise ValueError(
                f"campaign revision conflict: expected {expected_revision}, "
                f"found {campaign.revision}"
            )
        if current_branch_id(campaign_id) != expected_branch_id:
            raise ValueError("active branch changed before branch creation")
        created = branches.create(
            campaign_id,
            name=name,
            from_snapshot_id=from_snapshot_id,
            checkout=False,
        )
        snapshot = snapshots.checkout_branch(campaign_id, created.id) if checkout else None
        response = asdict(branches.get(campaign_id, created.id))
        if checkout:
            response["snapshot"] = asdict(snapshot) if snapshot else None
        return remember_idempotent(
            scope,
            idempotency_key,
            request_payload,
            response,
            campaign_id=campaign_id,
        )

    @mcp.tool()
    def branch_checkout(
        campaign_id: str,
        branch_id: str,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        expected_branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Load a branch head as live campaign state without creating a new save."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if expected_revision is None or expected_branch_id is None or not idempotency_key:
            raise ValueError(
                "expected_revision, expected_branch_id, and idempotency_key are required "
                "for branch checkout"
            )
        request_payload = {
            "branch_id": branch_id,
            "expected_branch_id": expected_branch_id,
        }
        scope = f"branch-checkout:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, request_payload)
        if replay is not None:
            return replay
        campaign = campaigns.get(campaign_id)
        if campaign.revision != expected_revision:
            raise ValueError(
                f"campaign revision conflict: expected {expected_revision}, "
                f"found {campaign.revision}"
            )
        if current_branch_id(campaign_id) != expected_branch_id:
            raise ValueError("active branch changed before branch checkout")
        snapshot = snapshots.checkout_branch(campaign_id, branch_id)
        response = {
            "branch": asdict(branches.current(campaign_id)),
            "snapshot": asdict(snapshot) if snapshot else None,
        }
        return remember_idempotent(
            scope,
            idempotency_key,
            request_payload,
            response,
            campaign_id=campaign_id,
        )

    @mcp.tool()
    def snapshot_create(
        campaign_id: str,
        label: str = "",
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        expected_head_snapshot_id: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Commit current D&D state, events, facts, and actor knowledge to this branch."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if expected_revision is None or not idempotency_key:
            raise ValueError(
                "expected_revision, expected_head_snapshot_id, and idempotency_key are "
                "required for snapshot creation"
            )
        request_payload = {
            "label": label,
            "expected_head_snapshot_id": expected_head_snapshot_id,
        }
        scope = f"snapshot-create:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, request_payload)
        if replay is not None:
            return replay
        campaign = campaigns.get(campaign_id)
        if campaign.revision != expected_revision:
            raise ValueError(
                f"campaign revision conflict: expected {expected_revision}, "
                f"found {campaign.revision}"
            )
        branch = branches.current(campaign_id)
        if (branch.head_snapshot_id or "") != expected_head_snapshot_id:
            raise ValueError("branch head changed before snapshot creation")
        response = asdict(snapshots.create(campaign_id, label=label))
        return remember_idempotent(
            scope,
            idempotency_key,
            request_payload,
            response,
            campaign_id=campaign_id,
        )

    @mcp.tool()
    def snapshot_list(campaign_id: str, principal_id: str = "system:local") -> list[dict[str, Any]]:
        access.require_campaign(campaign_id, principal_id)
        return [asdict(item) for item in snapshots.list(campaign_id)]

    @mcp.tool()
    def snapshot_restore(
        campaign_id: str,
        slot: int,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        expected_branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Fork from an earlier save; existing future history remains intact."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if expected_revision is None or expected_branch_id is None or not idempotency_key:
            raise ValueError(
                "expected_revision, expected_branch_id, and idempotency_key are required "
                "for snapshot restore"
            )
        request_payload = {
            "slot": slot,
            "expected_branch_id": expected_branch_id,
        }
        scope = f"snapshot-restore:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, request_payload)
        if replay is not None:
            return replay
        campaign = campaigns.get(campaign_id)
        if campaign.revision != expected_revision:
            raise ValueError(
                f"campaign revision conflict: expected {expected_revision}, "
                f"found {campaign.revision}"
            )
        if current_branch_id(campaign_id) != expected_branch_id:
            raise ValueError("active branch changed before snapshot restore")
        response = asdict(snapshots.restore(campaign_id, slot))
        return remember_idempotent(
            scope,
            idempotency_key,
            request_payload,
            response,
            campaign_id=campaign_id,
        )

    @mcp.tool()
    def snapshot_verify(
        campaign_id: str, slot: int, principal_id: str = "system:local"
    ) -> dict[str, bool]:
        """Verify that a saved snapshot has an internally consistent payload."""
        access.require_campaign(campaign_id, principal_id)
        return {"valid": snapshots.verify(campaign_id, slot)}

    @mcp.tool()
    def snapshot_lineage(
        campaign_id: str,
        slot: int | None = None,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        """List the lineage of a save without mutating campaign history."""
        access.require_campaign(campaign_id, principal_id)
        return [asdict(item) for item in snapshots.lineage(campaign_id, slot)]

    @mcp.tool()
    def snapshot_regenerate_recap(
        campaign_id: str, slot: int, principal_id: str = "system:local"
    ) -> dict[str, Any]:
        """Regenerate a deterministic recap from a saved snapshot payload."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        return snapshots.regenerate_recap(campaign_id, slot)

    @mcp.tool()
    def character_create(
        name: str,
        campaign_id: str | None = None,
        character_type: str = "pc",
        player_name: str | None = None,
        summary: str = "",
        sheet: dict[str, Any] | None = None,
        notes: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a D&D PC, NPC, or monster; optionally bind it to a campaign."""
        if campaign_id is not None:
            access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if not idempotency_key:
            raise ValueError("idempotency_key is required for character creation")
        sheet_value = deepcopy(sheet or default_character_sheet())
        if campaign_id is not None:
            sheet_value["edition"] = str(
                campaigns.get(campaign_id).settings.get("edition") or "2024"
            )
        normalized_sheet = validate_character_sheet(sheet_value)
        normalized_notes = validate_character_notes(notes or default_character_notes())
        return character_view(
            characters.create_idempotent(
                system_id="dnd5e",
                name=name,
                principal_id=principal_id,
                idempotency_key=idempotency_key,
                campaign_id=campaign_id,
                character_type=character_type,
                player_name=player_name,
                summary=summary,
                sheet=normalized_sheet,
                notes=normalized_notes,
            )
        )

    @mcp.tool()
    def character_list(
        campaign_id: str | None = None,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        """List D&D characters, optionally restricted to a campaign."""
        if campaign_id is not None:
            access.require_campaign(campaign_id, principal_id)
        return [
            visible_character_view(item, principal_id)
            for item in characters.list(system_id="dnd5e", campaign_id=campaign_id)
        ]

    @mcp.tool()
    def character_library_list(character_type: str | None = None) -> list[dict[str, Any]]:
        """List reusable D&D templates that are not bound to a campaign."""
        return [
            character_view(item)
            for item in characters.list_library(system_id="dnd5e", character_type=character_type)
        ]

    @mcp.tool()
    def character_instantiate(
        template_id: str,
        campaign_id: str,
        name: str | None = None,
        player_name: str | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Copy a public D&D character template into one campaign."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        template = characters.get(template_id)
        sheet = deepcopy(template.sheet)
        sheet["edition"] = str(campaigns.get(campaign_id).settings.get("edition") or "2024")
        return character_view(
            characters.instantiate(
                template_id,
                campaign_id=campaign_id,
                name=name,
                player_name=player_name,
                sheet=validate_character_sheet(sheet),
            )
        )

    @mcp.tool()
    def character_build(
        campaign_id: str,
        name: str,
        player_name: str | None = None,
        summary: str = "",
        sheet: dict[str, Any] | None = None,
        notes: dict[str, Any] | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Atomically create a PC library template and independent campaign instance."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        sheet_value = deepcopy(sheet or default_character_sheet())
        sheet_value["edition"] = str(campaigns.get(campaign_id).settings.get("edition") or "2024")
        normalized_sheet = validate_character_sheet(sheet_value)
        normalized_notes = validate_character_notes(notes or default_character_notes())
        template, instance = characters.create_with_instance(
            system_id="dnd5e",
            campaign_id=campaign_id,
            name=name,
            character_type="pc",
            player_name=player_name,
            summary=summary,
            sheet=normalized_sheet,
            notes=normalized_notes,
        )
        return {"template": character_view(template), "instance": character_view(instance)}

    @mcp.tool()
    def character_get(character_id: str, principal_id: str = "system:local") -> dict[str, Any]:
        """Read one validated D&D character card."""
        current = characters.get(character_id)
        if current.campaign_id is not None:
            access.require_actor(
                current.campaign_id,
                current.id,
                principal_id,
                private=True,
            )
        return character_view(current)

    def update_sheet(
        character_id: str,
        sheet: dict[str, Any],
        *,
        operation: str = "character.sheet.update",
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        payload: dict[str, Any] | None = None,
        response_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a D&D schema mutation with derived values recalculated."""
        current = characters.get(character_id)
        sheet_value = deepcopy(sheet)
        if current.campaign_id is not None:
            sheet_value["edition"] = str(
                campaigns.get(current.campaign_id).settings.get("edition") or "2024"
            )
        normalized_sheet = validate_character_sheet(sheet_value)
        return update_character(
            current,
            operation=operation,
            sheet=normalized_sheet,
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload=payload,
            response_extra=response_extra,
        )

    @mcp.tool()
    def character_sheet_replace(
        character_id: str,
        sheet: dict[str, Any],
        notes: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Validate and replace a complete D&D v2 sheet, deriving combat and inventory fields."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        sheet_value = deepcopy(sheet)
        if current.campaign_id is not None:
            sheet_value["edition"] = str(
                campaigns.get(current.campaign_id).settings.get("edition") or "2024"
            )
        normalized_sheet = validate_character_sheet(sheet_value)
        normalized_notes = validate_character_notes(notes if notes is not None else current.notes)
        return update_character(
            current,
            operation="character.sheet.replace",
            sheet=normalized_sheet,
            notes=normalized_notes,
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={"sheet": normalized_sheet, "notes": normalized_notes},
        )

    @mcp.tool()
    def character_wallet_adjust(
        character_id: str,
        denomination: str,
        amount: int,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Adjust one D&D character wallet denomination through the v2 schema."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        return update_sheet(
            character_id,
            adjust_wallet(current.sheet, denomination, amount),
            operation="character.wallet.adjust",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={"denomination": denomination, "amount": amount},
        )

    @mcp.tool()
    def character_inventory_add(
        character_id: str,
        item: dict[str, Any],
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Add a normalized inventory item and return its assigned item id."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        sheet, item_id = add_inventory_item(current.sheet, item)
        return update_sheet(
            character_id,
            sheet,
            operation="character.inventory.add",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={"item": item},
            response_extra={"item_id": item_id},
        )

    @mcp.tool()
    def character_inventory_update(
        character_id: str,
        item_id: str,
        patch: dict[str, Any],
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Update one structured inventory item without bypassing D&D validation."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        return update_sheet(
            character_id,
            update_inventory_item(current.sheet, item_id, patch),
            operation="character.inventory.update",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={"item_id": item_id, "patch": patch},
        )

    @mcp.tool()
    def character_inventory_remove(
        character_id: str,
        item_id: str,
        quantity: int | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Remove an inventory stack or quantity and return the removed item data."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        sheet, removed = remove_inventory_item(current.sheet, item_id, quantity)
        return update_sheet(
            character_id,
            sheet,
            operation="character.inventory.remove",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={"item_id": item_id, "quantity": quantity},
            response_extra={"removed": removed},
        )

    @mcp.tool()
    def character_inventory_equip(
        character_id: str,
        item_id: str,
        slot: str | None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Equip an inventory item in a validated D&D equipment slot, or unequip it."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        return update_sheet(
            character_id,
            equip_inventory_item(current.sheet, item_id, slot),
            operation="character.inventory.equip",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={"item_id": item_id, "slot": slot},
        )

    @mcp.tool()
    def character_ammunition_consume(
        character_id: str,
        weapon_id: str,
        quantity: int = 1,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Consume ammunition linked to a weapon through structured mechanics."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        sheet, consumed = consume_weapon_ammunition(current.sheet, weapon_id, quantity)
        return update_sheet(
            character_id,
            sheet,
            operation="character.ammunition.consume",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={"weapon_id": weapon_id, "quantity": quantity},
            response_extra={"consumed": consumed},
        )

    @mcp.tool()
    def character_inventory_transfer(
        source_character_id: str,
        target_character_id: str,
        item_id: str,
        quantity: int | None = None,
        principal_id: str = "system:local",
        expected_campaign_revision: int | None = None,
        expected_source_revision: int | None = None,
        expected_target_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Move an inventory item between two actors in the same campaign."""
        payload = {
            "source_character_id": source_character_id,
            "target_character_id": target_character_id,
            "item_id": item_id,
            "quantity": quantity,
        }
        source = characters.get(source_character_id)
        target = characters.get(target_character_id)
        if source.campaign_id is None or source.campaign_id != target.campaign_id:
            raise ValueError("characters must belong to the same campaign")
        if (
            expected_campaign_revision is None
            or expected_source_revision is None
            or expected_target_revision is None
            or not idempotency_key
        ):
            raise ValueError(
                "expected_campaign_revision, expected_source_revision, "
                "expected_target_revision, and idempotency_key are required for inventory transfer"
            )
        branch_id = require_current_branch(source.campaign_id, None)
        payload["branch_id"] = branch_id
        scope = f"character-inventory:{source.campaign_id}:{branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        access.require_actor(source.campaign_id, source.id, principal_id, control=True)
        source_sheet, moved = remove_inventory_item(source.sheet, item_id, quantity)
        target_sheet = receive_inventory_item(target.sheet, moved)
        mutations = StateMutationService(storage.database)
        mutations.replace(
            source.campaign_id,
            character_updates=[
                CharacterStateUpdate(
                    source.id, source_sheet, source.notes, expected_source_revision
                ),
                CharacterStateUpdate(
                    target.id, target_sheet, target.notes, expected_target_revision
                ),
            ],
            expected_campaign_revision=expected_campaign_revision,
            operation="character.inventory.transfer",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=idempotency_key,
        )
        source_after = characters.get(source.id)
        target_after = characters.get(target.id)
        response = {
            "source": character_view(source_after),
            "target": character_view(target_after),
            "item": moved,
        }
        return remember_idempotent(
            scope,
            idempotency_key,
            payload,
            response,
            campaign_id=source.campaign_id,
        )

    @mcp.tool()
    def character_effect_add(
        character_id: str,
        effect: dict[str, Any],
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Add a validated active D&D effect and return its assigned effect id."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        sheet, effect_id = add_effect(current.sheet, effect)
        return update_sheet(
            character_id,
            sheet,
            operation="character.effect.add",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={"effect": effect},
            response_extra={"effect_id": effect_id},
        )

    @mcp.tool()
    def character_effect_remove(
        character_id: str,
        effect_id: str,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Remove an active D&D effect."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        return update_sheet(
            character_id,
            remove_effect(current.sheet, effect_id),
            operation="character.effect.remove",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={"effect_id": effect_id},
        )

    @mcp.tool()
    def character_rest(
        character_id: str,
        rest_type: str,
        prepared_spell_ids: list[str] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Apply rest recovery and, on a long rest, atomically replace prepared spells."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        if current.campaign_id is None:
            raise ValueError("rest requires a campaign-bound character")
        campaign = campaigns.get(current.campaign_id)
        combat = dict(campaign.state or {}).get("combat")
        if isinstance(combat, dict) and combat.get("active", False):
            raise CombatEngineError("rest is not allowed while combat is active")
        if expected_revision is None or not idempotency_key:
            raise ValueError("expected_revision and idempotency_key are required for rest")
        if prepared_spell_ids is not None and str(rest_type).replace("-", "_") != "long_rest":
            raise CombatEngineError("prepared spells can be changed only as part of a long rest")
        payload = {
            "character_id": character_id,
            "rest_type": rest_type,
            "prepared_spell_ids": prepared_spell_ids,
        }
        branch_id = require_current_branch(current.campaign_id, None)
        scope = f"character-rest:{current.campaign_id}:{branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        applied = apply_rest(current.sheet, rest_type=rest_type)
        preparation_result = None
        if prepared_spell_ids is not None:
            preparation_result = replace_prepared_spells(
                applied["sheet"], spell_ids=prepared_spell_ids, event="long_rest"
            )
            applied["sheet"] = preparation_result["sheet"]
        StateMutationService(storage.database).replace(
            current.campaign_id,
            character_updates=[
                CharacterStateUpdate(
                    character_id=current.id,
                    sheet=validate_character_sheet(applied["sheet"]),
                    notes=validate_character_notes(current.notes),
                    expected_revision=expected_revision,
                )
            ],
            operation=f"character.rest.{rest_type}",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=idempotency_key,
        )
        response = {
            "status": "committed",
            "result": {key: value for key, value in applied.items() if key != "sheet"},
            "preparation": (
                {key: value for key, value in preparation_result.items() if key != "sheet"}
                if preparation_result is not None
                else None
            ),
            "character": character_view(characters.get(character_id)),
        }
        return remember_idempotent(
            scope,
            idempotency_key,
            payload,
            response,
            campaign_id=current.campaign_id,
        )

    @mcp.tool()
    def character_cast_spell(
        character_id: str,
        spell_id: str,
        cast_level: int | None = None,
        ritual: bool = False,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Pay canonical spell resources and start concentration from a v2 spell card."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        if current.campaign_id is None:
            raise ValueError("spell casting requires a campaign-bound character")
        if expected_revision is None or not idempotency_key:
            raise ValueError("expected_revision and idempotency_key are required for spell casting")
        branch_id = require_current_branch(current.campaign_id, None)
        payload = {
            "character_id": character_id,
            "spell_id": spell_id,
            "cast_level": cast_level,
            "ritual": ritual,
        }
        scope = f"character-cast:{current.campaign_id}:{branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        applied = consume_spell_cast(
            current.sheet,
            spell_id=spell_id,
            cast_level=cast_level,
            ritual=ritual,
        )
        StateMutationService(storage.database).replace(
            current.campaign_id,
            character_updates=[
                CharacterStateUpdate(
                    character_id=current.id,
                    sheet=validate_character_sheet(applied["sheet"]),
                    notes=validate_character_notes(current.notes),
                    expected_revision=expected_revision,
                )
            ],
            operation="character.spell.cast",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=idempotency_key,
        )
        response = {
            "status": "committed",
            "result": {key: value for key, value in applied.items() if key != "sheet"},
            "character": character_view(characters.get(character_id)),
        }
        return remember_idempotent(
            scope,
            idempotency_key,
            payload,
            response,
            campaign_id=current.campaign_id,
        )

    @mcp.tool()
    def character_use_activity(
        character_id: str,
        activity_id: str,
        declaration: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Consume one non-combat structured card use without fabricating its narrative result."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        if current.campaign_id is None:
            raise ValueError("activity use requires a campaign-bound character")
        if expected_revision is None or not idempotency_key:
            raise ValueError("expected_revision and idempotency_key are required for activity use")
        branch_id = require_current_branch(current.campaign_id, None)
        payload = {
            "character_id": character_id,
            "activity_id": activity_id,
            "declaration": declaration or {},
        }
        scope = f"character-activity:{current.campaign_id}:{branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        try:
            applied = consume_activity(current.sheet, activity_id=activity_id)
        except ActivityError as exc:
            raise ValueError(str(exc)) from exc
        activation_type = str(applied["activation"].get("type") or "")
        if activation_type in {"reaction", "special"} and not is_dm(
            current.campaign_id, principal_id
        ):
            raise PermissionError("reaction and special activity triggers require a DM resolution")
        StateMutationService(storage.database).replace(
            current.campaign_id,
            character_updates=[
                CharacterStateUpdate(
                    character_id=current.id,
                    sheet=validate_character_sheet(applied["sheet"]),
                    notes=validate_character_notes(current.notes),
                    expected_revision=expected_revision,
                )
            ],
            operation="character.activity.use",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=idempotency_key,
        )
        result = {key: value for key, value in applied.items() if key != "sheet"}
        result["declaration"] = declaration or {}
        response = {
            "status": "pending_ruling" if applied["requires_ruling"] else "committed",
            "result": result,
            "character": character_view(characters.get(character_id)),
        }
        return remember_idempotent(
            scope,
            idempotency_key,
            payload,
            response,
            campaign_id=current.campaign_id,
        )

    @mcp.tool()
    def character_resource_set(
        character_id: str,
        resource: str,
        value: int,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Set a named character resource, enforcing its schema-defined maximum."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        return update_sheet(
            character_id,
            set_resource_value(current.sheet, resource, value),
            operation="character.resource.set",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={"resource": resource, "value": value},
        )

    @mcp.tool()
    def character_spell_prepare(
        character_id: str,
        spell_id: str,
        prepared: bool,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Prepare or unprepare a spell under the D&D spellcasting constraints."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        if current.campaign_id is not None:
            campaign = campaigns.get(current.campaign_id)
            state = dict(campaign.state or {})
            combat = state.get("combat")
            if isinstance(combat, dict) and combat.get("active", False):
                raise CombatEngineError("prepared spells cannot be changed during combat")
            if state.get("game_phase", PROFILE_AUTHORING) != PROFILE_AUTHORING:
                raise CombatEngineError(
                    "live prepared-spell changes must be submitted atomically with character_rest"
                )
        return update_sheet(
            character_id,
            set_spell_prepared(current.sheet, spell_id, prepared),
            operation="character.spell.prepare" if prepared else "character.spell.unprepare",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={"spell_id": spell_id, "prepared": prepared},
        )

    @mcp.tool()
    def character_spell_prepare_list(
        character_id: str,
        spell_ids: list[str],
        event: str = "setup",
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Set the complete prepared list atomically during setup or level advancement."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        if current.campaign_id is not None:
            campaign = campaigns.get(current.campaign_id)
            state = dict(campaign.state or {})
            combat = state.get("combat")
            if isinstance(combat, dict) and combat.get("active", False):
                raise CombatEngineError("prepared spells cannot be changed during combat")
            if state.get("game_phase", PROFILE_AUTHORING) != PROFILE_AUTHORING:
                raise CombatEngineError(
                    "switch to authoring for setup or level-up preparation changes"
                )
        normalized_event = str(event).strip().lower().replace("-", "_")
        if normalized_event not in {"setup", "level_up"}:
            raise CombatEngineError(
                "this tool accepts setup or level_up; long-rest changes belong in character_rest"
            )
        result = replace_prepared_spells(
            current.sheet,
            spell_ids=list(spell_ids),
            event=normalized_event,
        )
        return update_sheet(
            character_id,
            validate_character_sheet(result["sheet"]),
            operation=f"character.spell.prepare.{normalized_event}",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={"spell_ids": list(spell_ids), "event": normalized_event},
            response_extra={
                "preparation": {key: value for key, value in result.items() if key != "sheet"}
            },
        )

    @mcp.tool()
    def character_ability_apply(
        character_id: str,
        method: str,
        assignments: dict[str, int],
        rolls: list[int] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Apply a validated ability-generation method to a complete D&D character sheet."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        sheet = apply_ability_generation(
            current.sheet,
            method=method,
            assignments=assignments,
            rolls=rolls,
        )
        return update_sheet(
            character_id,
            sheet,
            operation="character.ability.apply",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={"method": method, "assignments": assignments, "rolls": rolls},
        )

    @mcp.tool()
    def character_memory_add(
        character_id: str,
        memory: dict[str, Any],
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Append a legacy actor-notes memory without altering actor knowledge."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        notes, memory_id = add_memory(current.notes, memory)
        return update_character(
            current,
            operation="character.memory.add",
            notes=validate_character_notes(notes, character_type=current.character_type),
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={"memory": memory},
            response_extra={"memory_id": memory_id},
        )

    @mcp.tool()
    def character_memory_resolve(
        character_id: str,
        memory_id: str,
        status: str = "resolved",
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Resolve one legacy actor-notes memory without altering actor knowledge."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        notes = resolve_memory(current.notes, memory_id, status=status)
        return update_character(
            current,
            operation="character.memory.resolve",
            notes=validate_character_notes(notes, character_type=current.character_type),
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={"memory_id": memory_id, "status": status},
        )

    @mcp.tool()
    def party_show(campaign_id: str, principal_id: str = "system:local") -> dict[str, Any]:
        """Read the campaign shared stash, wallet, derived load, and party notes."""
        access.require_campaign(campaign_id, principal_id)
        state = validate_party_state(campaigns.get(campaign_id).state)
        sheet = party_sheet(state)
        return {
            "inventory": sheet["inventory"],
            "derived": derive_character_sheet(sheet)["inventory"],
            "notes": state["party"]["notes"],
        }

    @mcp.tool()
    def party_inventory_add(
        campaign_id: str,
        item: dict[str, Any],
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Add an item to the campaign shared inventory."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if expected_revision is None or not idempotency_key:
            raise ValueError(
                "expected_revision and idempotency_key are required for party inventory writes"
            )
        before = campaigns.get(campaign_id)
        branch_id = require_current_branch(campaign_id, None)
        payload = {"item": item, "expected_revision": expected_revision, "branch_id": branch_id}
        scope = f"party-inventory:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        sheet, item_id = add_inventory_item(party_sheet(before.state), item)
        after = campaigns.update_audited(
            campaign_id,
            state=party_state(before.state, sheet),
            expected_revision=expected_revision,
            operation="party.inventory.add",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=idempotency_key,
        )
        return remember_idempotent(
            scope,
            idempotency_key,
            payload,
            {"inventory": sheet["inventory"], "item_id": item_id, "campaign": asdict(after)},
            campaign_id=campaign_id,
        )

    @mcp.tool()
    def party_inventory_remove(
        campaign_id: str,
        item_id: str,
        quantity: int | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Remove an item or partial stack from the campaign shared inventory."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if expected_revision is None or not idempotency_key:
            raise ValueError(
                "expected_revision and idempotency_key are required for party inventory writes"
            )
        before = campaigns.get(campaign_id)
        branch_id = require_current_branch(campaign_id, None)
        payload = {
            "item_id": item_id,
            "quantity": quantity,
            "expected_revision": expected_revision,
            "branch_id": branch_id,
        }
        scope = f"party-inventory:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        sheet, removed = remove_inventory_item(party_sheet(before.state), item_id, quantity)
        after = campaigns.update_audited(
            campaign_id,
            state=party_state(before.state, sheet),
            expected_revision=expected_revision,
            operation="party.inventory.remove",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=idempotency_key,
        )
        return remember_idempotent(
            scope,
            idempotency_key,
            payload,
            {"inventory": sheet["inventory"], "removed": removed, "campaign": asdict(after)},
            campaign_id=campaign_id,
        )

    @mcp.tool()
    def party_inventory_transfer(
        campaign_id: str,
        character_id: str,
        item_id: str,
        direction: str,
        quantity: int | None = None,
        principal_id: str = "system:local",
        expected_campaign_revision: int | None = None,
        expected_character_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Deposit an actor item to, or withdraw one from, the party shared inventory."""
        if direction not in {"deposit", "withdraw"}:
            raise ValueError("direction must be deposit or withdraw")
        if (
            expected_campaign_revision is None
            or expected_character_revision is None
            or not idempotency_key
        ):
            raise ValueError(
                "expected campaign/character revisions and idempotency_key are required"
            )
        payload = {
            "campaign_id": campaign_id,
            "character_id": character_id,
            "item_id": item_id,
            "direction": direction,
            "quantity": quantity,
            "expected_campaign_revision": expected_campaign_revision,
            "expected_character_revision": expected_character_revision,
        }
        scope = f"party-inventory:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        campaign = campaigns.get(campaign_id)
        character = characters.get(character_id)
        if character.campaign_id != campaign_id:
            raise ValueError("character must belong to the campaign")
        access.require_actor(campaign_id, character_id, principal_id, control=True)
        shared = party_sheet(campaign.state)
        if direction == "deposit":
            character_sheet, moved = remove_inventory_item(character.sheet, item_id, quantity)
            shared_sheet = receive_inventory_item(shared, moved)
        else:
            shared_sheet, moved = remove_inventory_item(shared, item_id, quantity)
            character_sheet = receive_inventory_item(character.sheet, moved)
        updated_state = party_state(campaign.state, shared_sheet)
        StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=updated_state,
            character_updates=[
                CharacterStateUpdate(
                    character.id,
                    character_sheet,
                    character.notes,
                    expected_character_revision,
                )
            ],
            operation=f"party.inventory.{direction}",
            actor="mcp",
            expected_campaign_revision=expected_campaign_revision,
            idempotency_key=idempotency_key,
        )
        character_after = characters.get(character_id)
        response = {
            "party": party_show(campaign_id, principal_id=principal_id),
            "character": character_view(character_after),
            "item": moved,
        }
        return remember_idempotent(
            scope,
            idempotency_key,
            payload,
            response,
            campaign_id=campaign_id,
        )

    @mcp.tool()
    def party_wallet_adjust(
        campaign_id: str,
        denomination: str,
        amount: int,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Credit or debit one denomination in the shared party wallet."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if expected_revision is None or not idempotency_key:
            raise ValueError(
                "expected_revision and idempotency_key are required for party wallet writes"
            )
        before = campaigns.get(campaign_id)
        branch_id = require_current_branch(campaign_id, None)
        payload = {
            "denomination": denomination,
            "amount": amount,
            "expected_revision": expected_revision,
            "branch_id": branch_id,
        }
        scope = f"party-wallet:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        sheet = adjust_wallet(party_sheet(before.state), denomination, amount)
        after = campaigns.update_audited(
            campaign_id,
            state=party_state(before.state, sheet),
            expected_revision=expected_revision,
            operation="party.wallet.adjust",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=idempotency_key,
        )
        return remember_idempotent(
            scope,
            idempotency_key,
            payload,
            {"wallet": sheet["inventory"]["wallet"], "campaign": asdict(after)},
            campaign_id=campaign_id,
        )

    @mcp.tool()
    def party_wallet_transfer(
        campaign_id: str,
        character_id: str,
        denomination: str,
        amount: int,
        direction: str,
        principal_id: str = "system:local",
        expected_campaign_revision: int | None = None,
        expected_character_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Deposit currency to, or withdraw currency from, the shared party wallet."""
        if amount <= 0:
            raise ValueError("amount must be positive")
        if direction not in {"deposit", "withdraw"}:
            raise ValueError("direction must be deposit or withdraw")
        if (
            expected_campaign_revision is None
            or expected_character_revision is None
            or not idempotency_key
        ):
            raise ValueError(
                "expected campaign/character revisions and idempotency_key are required"
            )
        payload = {
            "campaign_id": campaign_id,
            "character_id": character_id,
            "denomination": denomination,
            "amount": amount,
            "direction": direction,
            "expected_campaign_revision": expected_campaign_revision,
            "expected_character_revision": expected_character_revision,
        }
        scope = f"party-wallet:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        campaign = campaigns.get(campaign_id)
        character = characters.get(character_id)
        if character.campaign_id != campaign_id:
            raise ValueError("character must belong to the campaign")
        access.require_actor(campaign_id, character_id, principal_id, control=True)
        if campaign.revision != expected_campaign_revision:
            raise ValueError(f"campaign revision conflict: {campaign_id}")
        shared = party_sheet(campaign.state)
        delta = amount if direction == "deposit" else -amount
        shared_sheet = adjust_wallet(shared, denomination, delta)
        character_sheet = adjust_wallet(character.sheet, denomination, -delta)
        StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=party_state(campaign.state, shared_sheet),
            character_updates=[
                CharacterStateUpdate(
                    character.id,
                    character_sheet,
                    character.notes,
                    expected_character_revision
                    if expected_character_revision is not None
                    else character.revision,
                )
            ],
            operation=f"party.wallet.{direction}",
            actor="mcp",
            expected_campaign_revision=expected_campaign_revision,
            idempotency_key=idempotency_key,
        )
        character_after = characters.get(character_id)
        response = {
            "party": party_show(campaign_id, principal_id=principal_id),
            "character": character_view(character_after),
        }
        return remember_idempotent(
            scope,
            idempotency_key,
            payload,
            response,
            campaign_id=campaign_id,
        )

    @mcp.tool()
    def dnd_dice_roll(expression: str) -> dict[str, Any]:
        """Roll a validated D&D dice expression such as 2d6+3."""
        return asdict(roll(expression))

    @mcp.tool()
    def dnd_check(
        dc: int,
        ability_score: int,
        proficient: bool = False,
        level: int = 1,
        bonus: int = 0,
        advantage: bool = False,
        disadvantage: bool = False,
        kind: str = "ability",
    ) -> dict[str, Any]:
        """Resolve an ability check or saving throw with explicit semantics."""
        return resolve_check(
            dc=dc,
            ability_score=ability_score,
            proficient=proficient,
            level=level,
            bonus=bonus,
            advantage=advantage,
            disadvantage=disadvantage,
            kind=kind,
        )

    @mcp.tool()
    def dnd_ability_roll(edition: str = "2024") -> dict[str, Any]:
        """Generate six ability scores using the D&D 4d6 drop-lowest rule."""
        return roll_ability_scores(edition)

    @mcp.tool()
    def character_update(
        character_id: str,
        name: str | None = None,
        player_name: str | None = None,
        summary: str | None = None,
        sheet: dict[str, Any] | None = None,
        notes: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Update a D&D character sheet or supporting notes."""
        normalized_sheet = validate_character_sheet(sheet) if sheet is not None else None
        normalized_notes = validate_character_notes(notes) if notes is not None else None
        before = characters.get(character_id)
        if before.campaign_id is None:
            updated = characters.update(
                character_id,
                name=name,
                player_name=player_name,
                summary=summary,
                sheet=normalized_sheet,
                notes=normalized_notes,
                expected_revision=expected_revision,
            )
            record_character_revision(before, updated, "character.update")
            return character_view(updated)
        access.require_actor(before.campaign_id, before.id, principal_id, control=True)
        if expected_revision is None or not idempotency_key:
            raise ValueError(
                "expected_revision and idempotency_key are required for character updates"
            )
        branch_id = require_current_branch(before.campaign_id, None)
        request_payload = {
            "character_id": character_id,
            "name": name,
            "player_name": player_name,
            "summary": summary,
            "sheet": normalized_sheet,
            "notes": normalized_notes,
        }
        scope = f"character-update:{before.campaign_id}:{branch_id}:{principal_id}:{before.id}"
        replay = replay_idempotent(scope, idempotency_key, request_payload)
        if replay is not None:
            return replay
        StateMutationService(storage.database).replace(
            before.campaign_id,
            character_updates=[
                CharacterStateUpdate(
                    character_id=before.id,
                    sheet=normalized_sheet if normalized_sheet is not None else before.sheet,
                    notes=normalized_notes if normalized_notes is not None else before.notes,
                    expected_revision=expected_revision,
                    name=name,
                    player_name=player_name,
                    summary=summary,
                )
            ],
            operation="character.update",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=idempotency_key,
        )
        response = character_view(characters.get(character_id))
        return remember_idempotent(
            scope,
            idempotency_key,
            request_payload,
            response,
            campaign_id=before.campaign_id,
        )

    @mcp.tool()
    def memory_add(
        campaign_id: str,
        content: str,
        kind: str = "fact",
        subject: str = "",
        metadata: dict[str, Any] | None = None,
        branch_id: str | None = None,
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Record a durable campaign fact, event, relationship, or NPC memory."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if not idempotency_key:
            raise ValueError("idempotency_key is required for memory writes")
        branch_id = require_current_branch(campaign_id, branch_id)
        request_payload = {
            "content": content,
            "kind": kind,
            "subject": subject,
            "metadata": metadata or {},
            "branch_id": branch_id,
        }
        scope = f"memory-add:{campaign_id}:{branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, request_payload)
        if replay is not None:
            return replay
        response = asdict(
            memories.add(
                campaign_id,
                content=content,
                kind=kind,
                subject=subject,
                metadata=metadata,
                branch_id=branch_id,
            )
        )
        return remember_idempotent(
            scope,
            idempotency_key,
            request_payload,
            response,
            campaign_id=campaign_id,
        )

    @mcp.tool()
    def memory_list(
        campaign_id: str,
        kind: str | None = None,
        branch_id: str | None = None,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        """List durable world facts visible from one campaign branch."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        return [asdict(item) for item in memories.list(campaign_id, kind=kind, branch_id=branch_id)]

    @mcp.tool()
    def memory_search(
        campaign_id: str,
        query: str,
        limit: int = 8,
        branch_id: str | None = None,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        """Retrieve branch-scoped durable world facts for DM administration."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        return [
            asdict(item)
            for item in memories.search(campaign_id, query, limit=limit, branch_id=branch_id)
        ]

    @mcp.tool()
    def event_add(
        campaign_id: str,
        summary: str,
        event_type: str = "narrative",
        payload: dict[str, Any] | None = None,
        audience_scope: str = "dm",
        branch_id: str | None = None,
        known_by_actor_ids: list[str] | None = None,
        knowledge_key: str | None = None,
        knowledge_proposition: str | None = None,
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Append a branch-local chronology event; an event is not actor knowledge."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if not idempotency_key:
            raise ValueError("idempotency_key is required for event writes")
        branch_id = require_current_branch(campaign_id, branch_id)
        if known_by_actor_ids:
            if not knowledge_key or not knowledge_proposition:
                raise ValueError(
                    "knowledge_key and knowledge_proposition are required when actors are listed"
                )
            for actor_id in known_by_actor_ids:
                access.require_actor(campaign_id, actor_id, principal_id, private=True)
        request_payload = {
            "summary": summary,
            "event_type": event_type,
            "payload": payload or {},
            "audience_scope": audience_scope,
            "branch_id": branch_id,
            "known_by_actor_ids": known_by_actor_ids or [],
            "knowledge_key": knowledge_key,
            "knowledge_proposition": knowledge_proposition,
        }
        scope = f"event-add:{campaign_id}:{branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, request_payload)
        if replay is not None:
            return replay
        created = events.add(
            campaign_id,
            summary=summary,
            event_type=event_type,
            payload=payload,
            audience_scope=audience_scope,
            branch_id=branch_id,
        )
        knowledge_ids: list[str] = []
        if known_by_actor_ids:
            for actor_id in known_by_actor_ids:
                knowledge_ids.append(
                    knowledge.add(
                        campaign_id,
                        actor_id=actor_id,
                        knowledge_key=knowledge_key,
                        proposition=knowledge_proposition,
                        source_event_id=created.id,
                        cause="witnessed",
                        branch_id=branch_id,
                    ).id
                )
        response = {**asdict(created), "actor_knowledge_ids": knowledge_ids}
        return remember_idempotent(
            scope,
            idempotency_key,
            request_payload,
            response,
            campaign_id=campaign_id,
        )

    @mcp.tool()
    def event_list(
        campaign_id: str,
        limit: int = 50,
        branch_id: str | None = None,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        membership = access.require_campaign(campaign_id, principal_id)
        values = events.list(
            campaign_id,
            limit=limit,
            branch_id=readable_branch(campaign_id, branch_id, principal_id),
        )
        if membership.role not in {"owner", "dm"}:
            values = [
                item for item in values if item.audience_scope in {"public", "party", "player"}
            ]
        return [asdict(item) for item in values]

    @mcp.tool()
    def actor_knowledge_add(
        campaign_id: str,
        actor_id: str,
        knowledge_key: str,
        proposition: str,
        subject_ref: str = "",
        epistemic_status: str = "known",
        confidence: int = 3,
        source_event_id: str | None = None,
        cause: str = "witnessed",
        disclosure_scope: str = "dm",
        branch_id: str | None = None,
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Record what one live PC, NPC, or monster knows or believes."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        access.require_actor(campaign_id, actor_id, principal_id, private=True)
        if not idempotency_key:
            raise ValueError("idempotency_key is required for actor knowledge writes")
        branch_id = require_current_branch(campaign_id, branch_id)
        request_payload = {
            "actor_id": actor_id,
            "knowledge_key": knowledge_key,
            "proposition": proposition,
            "subject_ref": subject_ref,
            "epistemic_status": epistemic_status,
            "confidence": confidence,
            "source_event_id": source_event_id,
            "cause": cause,
            "disclosure_scope": disclosure_scope,
            "branch_id": branch_id,
        }
        scope = f"actor-knowledge:{campaign_id}:{branch_id}:{principal_id}:{actor_id}"
        replay = replay_idempotent(scope, idempotency_key, request_payload)
        if replay is not None:
            return replay
        response = asdict(
            knowledge.add(
                campaign_id,
                actor_id=actor_id,
                knowledge_key=knowledge_key,
                proposition=proposition,
                subject_ref=subject_ref,
                epistemic_status=epistemic_status,
                confidence=confidence,
                source_event_id=source_event_id,
                cause=cause,
                disclosure_scope=disclosure_scope,
                branch_id=branch_id,
            )
        )
        return remember_idempotent(
            scope,
            idempotency_key,
            request_payload,
            response,
            campaign_id=campaign_id,
        )

    @mcp.tool()
    def actor_knowledge_revise(
        knowledge_id: str,
        proposition: str,
        epistemic_status: str = "known",
        confidence: int = 3,
        source_event_id: str | None = None,
        cause: str = "told_by",
        disclosure_scope: str = "dm",
        branch_id: str | None = None,
        principal_id: str = "system:local",
        expected_revision_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Append a new subjective revision, e.g. a rumor or Modify Memory effect."""
        current = knowledge.get(knowledge_id)
        access.require_campaign(current.campaign_id, principal_id, roles={"owner", "dm"})
        if expected_revision_id is None or not idempotency_key:
            raise ValueError(
                "expected_revision_id and idempotency_key are required for knowledge revisions"
            )
        branch_id = require_current_branch(current.campaign_id, branch_id)
        request_payload = {
            "knowledge_id": knowledge_id,
            "proposition": proposition,
            "epistemic_status": epistemic_status,
            "confidence": confidence,
            "source_event_id": source_event_id,
            "cause": cause,
            "disclosure_scope": disclosure_scope,
            "branch_id": branch_id,
            "expected_revision_id": expected_revision_id,
        }
        scope = (
            f"actor-knowledge-revise:{current.campaign_id}:{branch_id}:"
            f"{principal_id}:{knowledge_id}"
        )
        replay = replay_idempotent(scope, idempotency_key, request_payload)
        if replay is not None:
            return replay
        if current.revision_id != expected_revision_id:
            raise ValueError(
                f"knowledge revision conflict: expected {expected_revision_id}, "
                f"found {current.revision_id}"
            )
        response = asdict(
            knowledge.revise(
                knowledge_id,
                proposition=proposition,
                epistemic_status=epistemic_status,
                confidence=confidence,
                source_event_id=source_event_id,
                cause=cause,
                disclosure_scope=disclosure_scope,
                branch_id=branch_id,
            )
        )
        return remember_idempotent(
            scope,
            idempotency_key,
            request_payload,
            response,
            campaign_id=current.campaign_id,
        )

    @mcp.tool()
    def actor_knowledge_list(
        campaign_id: str,
        actor_id: str,
        branch_id: str | None = None,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        access.require_actor(campaign_id, actor_id, principal_id, private=True)
        membership = access.require_campaign(campaign_id, principal_id)
        values = knowledge.list(
            campaign_id,
            actor_id=actor_id,
            branch_id=readable_branch(campaign_id, branch_id, principal_id),
        )
        if membership.role not in {"owner", "dm"}:
            values = [
                item
                for item in values
                if item.disclosure_scope in {"public", "party", "player", "owner"}
            ]
        return [asdict(item) for item in values]

    @mcp.tool()
    def actor_knowledge_search(
        campaign_id: str,
        actor_id: str,
        query: str,
        branch_id: str | None = None,
        limit: int = 8,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        """Search one actor's current subjective knowledge without leaking other actors."""
        access.require_actor(campaign_id, actor_id, principal_id, private=True)
        membership = access.require_campaign(campaign_id, principal_id)
        values = knowledge.search(
            campaign_id,
            actor_id=actor_id,
            query=query,
            branch_id=readable_branch(campaign_id, branch_id, principal_id),
            limit=limit,
        )
        if membership.role not in {"owner", "dm"}:
            values = [
                item
                for item in values
                if item.disclosure_scope in {"public", "party", "player", "owner"}
            ]
        return [asdict(item) for item in values]

    @mcp.tool()
    def state_history(
        campaign_id: str,
        limit: int = 100,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        """List audited reversible campaign and character mutations."""
        access.require_campaign(campaign_id, principal_id)
        return [asdict(item) for item in revisions.history(campaign_id, limit=limit)]

    @mcp.tool()
    def state_undo(
        campaign_id: str,
        principal_id: str = "system:local",
        expected_history_sequence: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Undo the latest audited mutation without deleting snapshots."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if expected_history_sequence is None or not idempotency_key:
            raise ValueError("expected_history_sequence and idempotency_key are required for undo")
        branch_id = current_branch_id(campaign_id)
        request_payload = {
            "expected_history_sequence": expected_history_sequence,
            "branch_id": branch_id,
        }
        scope = f"state-undo:{campaign_id}:{branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, request_payload)
        if replay is not None:
            return replay
        applied = next(
            (item for item in revisions.history(campaign_id) if item.applied),
            None,
        )
        actual_sequence = applied.sequence if applied is not None else 0
        if actual_sequence != expected_history_sequence:
            raise ValueError(
                f"history cursor conflict: expected {expected_history_sequence}, "
                f"found {actual_sequence}"
            )
        response = asdict(revisions.undo(campaign_id))
        return remember_idempotent(
            scope,
            idempotency_key,
            request_payload,
            response,
            campaign_id=campaign_id,
        )

    @mcp.tool()
    def state_redo(
        campaign_id: str,
        principal_id: str = "system:local",
        expected_history_sequence: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Redo the next audited mutation on the current state-revision branch."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if expected_history_sequence is None or not idempotency_key:
            raise ValueError("expected_history_sequence and idempotency_key are required for redo")
        branch_id = current_branch_id(campaign_id)
        request_payload = {
            "expected_history_sequence": expected_history_sequence,
            "branch_id": branch_id,
        }
        scope = f"state-redo:{campaign_id}:{branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, request_payload)
        if replay is not None:
            return replay
        applied = next(
            (item for item in revisions.history(campaign_id) if item.applied),
            None,
        )
        actual_sequence = applied.sequence if applied is not None else 0
        if actual_sequence != expected_history_sequence:
            raise ValueError(
                f"history cursor conflict: expected {expected_history_sequence}, "
                f"found {actual_sequence}"
            )
        response = asdict(revisions.redo(campaign_id))
        return remember_idempotent(
            scope,
            idempotency_key,
            request_payload,
            response,
            campaign_id=campaign_id,
        )

    @mcp.tool()
    def combat_end(
        campaign_id: str,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Close an encounter atomically while preserving its final audit state."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        campaign = campaigns.get(campaign_id)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        payload = {"branch_id": resolved_branch_id}
        scope = f"combat-end:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        if expected_revision is not None and campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        _, combat = active_encounter(campaign_id)
        require_no_blocking_pending(combat)
        for combatant in combat.get("combatants", []):
            if not combatant.get("death_saves", False):
                continue
            actor = characters.get(str(combatant["actor_id"]))
            hp = int(actor.sheet.get("combat", {}).get("hp", {}).get("value", 0) or 0)
            conditions = {str(item).casefold() for item in actor.sheet.get("conditions", [])}
            if hp == 0 and not conditions & {"dead", "stable"}:
                raise CombatEngineError(
                    f"cannot end combat while {actor.id} is still making death saves"
                )
        combat["active"] = False
        ending_readied = list(combat.get("readied", []))
        combat["readied"] = []
        updated_state = dict(campaign.state or {})
        updated_state["combat"] = combat
        updated_state["game_phase"] = PROFILE_PLAY
        character_updates: list[CharacterStateUpdate] = []
        expired_effects: set[str] = set()
        for combatant in combat.get("combatants", []):
            actor = characters.get(str(combatant["actor_id"]))
            sheet = deepcopy(actor.sheet)
            holding_ids = {
                str(item.get("holding_effect_id"))
                for item in ending_readied
                if item.get("kind") == "spell" and item.get("actor_id") == actor.id
            }
            for effect in sheet.get("effects", []):
                if str(effect.get("id")) in holding_ids:
                    effect["active"] = False
            advanced = advance_effect_durations(sheet, period="encounter")
            expired_effects.update(advanced["expired"])
            character_updates.append(
                CharacterStateUpdate(
                    character_id=actor.id,
                    sheet=validate_character_sheet(advanced["sheet"]),
                    notes=validate_character_notes(actor.notes),
                    expected_revision=actor.revision,
                )
            )
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(updated_state),
            character_updates=character_updates,
            expected_campaign_revision=campaign.revision,
            operation="combat.end",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
        )
        response = {
            "ended": True,
            "combat": combat,
            "tool_profile": PROFILE_PLAY,
            "effects_expired": sorted(expired_effects),
            "readied_spells_expired": sorted(
                str(item.get("id")) for item in ending_readied if item.get("kind") == "spell"
            ),
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return remember_idempotent(scope, idempotency_key, payload, response, campaign_id)

    @mcp.tool()
    def continuity_context(
        campaign_id: str,
        query: str = "",
        actor_id: str | None = None,
        scope_id: str = "party",
        audience: str = "dm",
        branch_id: str | None = None,
        limit: int = 8,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Retrieve only current-branch facts, events, and optional actor knowledge."""
        membership = access.require_campaign(campaign_id, principal_id)
        if membership.role not in {"owner", "dm"}:
            audience = "player"
            if actor_id:
                access.require_actor(campaign_id, actor_id, principal_id, private=True)
        branch_id = readable_branch(campaign_id, branch_id, principal_id)
        return continuity.context(
            campaign_id,
            query=query,
            actor_id=actor_id,
            scope_id=scope_id,
            audience=audience,
            branch_id=branch_id,
            limit=limit,
        )

    @mcp.tool()
    def module_write(name: str, content: str, principal_id: str = "system:local") -> dict[str, str]:
        """Write generated Markdown to the managed artifact directory before importing it."""
        if not principal_id:
            raise PermissionError("authenticated caller identity is required for module artifacts")
        path = storage.write_module(name, content)
        return {"artifact": path.name, "path": str(path)}

    @mcp.tool()
    def module_inspect(artifact: str, principal_id: str = "system:local") -> dict[str, Any]:
        """Inspect a managed Markdown artifact before importing it into a campaign."""
        if not principal_id:
            raise PermissionError("authenticated caller identity is required for module artifacts")
        return modules.inspect_path(storage.artifact_module_path(artifact))

    @mcp.tool()
    def module_import(
        campaign_id: str,
        artifact: str,
        title: str | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Import a Markdown artifact created by module_write into a campaign."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        path = storage.artifact_module_path(artifact)
        embedder, vectors = storage.dense_components()
        result = modules.ingest_path(
            campaign_id=campaign_id,
            path=path,
            title=title,
            parser=MarkdownModuleParser(profile=DndModuleProfile()),
            embedder=embedder,
            vector_store=vectors,
        )
        return asdict(result)

    @mcp.tool()
    def module_list(campaign_id: str, principal_id: str = "system:local") -> list[dict[str, Any]]:
        """List a campaign's imported modules."""
        membership = access.require_campaign(campaign_id, principal_id)
        rows = modules.list(campaign_id)
        if membership.role in {"owner", "dm"}:
            return rows
        return [
            {key: value for key, value in row.items() if key not in {"source_path", "metadata"}}
            for row in rows
        ]

    @mcp.tool()
    def module_index(
        campaign_id: str,
        module_id: str | None = None,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        """Return a stable scene index for scene selection and safe progression."""
        membership = access.require_campaign(campaign_id, principal_id)
        index = modules.scene_index(campaign_id, module_id=module_id)
        if membership.role in {"owner", "dm"}:
            return index
        return [item for item in index if item.get("visibility", "keeper") in {"public", "party"}]

    @mcp.tool()
    def module_expand(chunk_id: str, principal_id: str = "system:local") -> dict[str, Any]:
        """Read a complete module chunk after it was selected by search."""
        result = modules.expand(chunk_id)
        membership = access.require_campaign(result["campaign_id"], principal_id)
        visibility = result.get("scene", {}).get("visibility", "keeper")
        if membership.role in {"owner", "dm"} or visibility in {"public", "party"}:
            return result
        return {
            "chunk_id": result["chunk_id"],
            "campaign_id": result["campaign_id"],
            "redacted": True,
            "content": "[DM-only module content hidden]",
        }

    @mcp.tool()
    def module_read_scene(
        campaign_id: str,
        scene_id: str,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Read one full scene, including its structured rooms and visibility metadata."""
        membership = access.require_campaign(campaign_id, principal_id)
        result = modules.read_scene(campaign_id, scene_id)
        visibility = result.get("visibility", "keeper")
        if membership.role in {"owner", "dm"} or visibility in {"public", "party"}:
            return result
        redacted = dict(result)
        redacted["content"] = "[DM-only scene content hidden]"
        redacted["redacted"] = True
        return redacted

    @mcp.tool()
    def module_current(
        campaign_id: str,
        scope_id: str = "party",
        principal_id: str = "system:local",
    ) -> dict[str, Any] | None:
        """Read the current scene for party, group, or player scope with party fallback."""
        membership = access.require_campaign(campaign_id, principal_id)
        result = modules.current_scene(campaign_id, scope_id=scope_id)
        if result is None or membership.role in {"owner", "dm"}:
            return result
        if result.get("visibility", "keeper") in {"public", "party"}:
            return result
        return {
            "campaign_id": campaign_id,
            "redacted": True,
            "content": "[DM-only scene content hidden]",
        }

    @mcp.tool()
    def module_set_progress(
        campaign_id: str,
        scene_id: str,
        scope_id: str = "party",
        status: str = "current",
        progress: int = 0,
        state: dict[str, Any] | None = None,
        current_room: str | None = None,
        principal_id: str = "system:local",
        expected_state_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Persist scoped scene progress without changing another scope's current scene."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if expected_state_version is None or not idempotency_key:
            raise ValueError(
                "expected_state_version and idempotency_key are required for scene progress"
            )
        branch_id = current_branch_id(campaign_id)
        request_payload = {
            "scene_id": scene_id,
            "scope_id": scope_id,
            "status": status,
            "progress": progress,
            "state": state,
            "current_room": current_room,
            "expected_state_version": expected_state_version,
            "branch_id": branch_id,
        }
        scope = f"module-progress:{campaign_id}:{branch_id}:{principal_id}:{scope_id}"
        replay = replay_idempotent(scope, idempotency_key, request_payload)
        if replay is not None:
            return replay
        response = modules.set_scene_progress(
            campaign_id=campaign_id,
            scene_id=scene_id,
            scope_id=scope_id,
            status=status,
            progress=progress,
            state=state,
            current_room=current_room,
            expected_state_version=expected_state_version,
        )
        return remember_idempotent(
            scope,
            idempotency_key,
            request_payload,
            response,
            campaign_id=campaign_id,
        )

    @mcp.tool()
    def module_search(
        campaign_id: str,
        query: str,
        top_k: int = 8,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        """Search imported adventure content using SQLite FTS and optional Chroma vectors."""
        membership = access.require_campaign(campaign_id, principal_id)
        embedder, vectors = storage.dense_components()
        hits = modules.search(
            campaign_id=campaign_id,
            query=query,
            top_k=top_k,
            embedder=embedder,
            vector_store=vectors,
        )
        if membership.role in {"owner", "dm"}:
            return [asdict(hit) for hit in hits]
        return [
            asdict(hit)
            for hit in hits
            if hit.metadata.get("visibility", "keeper") in {"public", "party"}
        ]

    @mcp.tool()
    def rule_search(
        query: str,
        edition: str | None = None,
        locale: str | None = None,
        top_k: int = 8,
    ) -> list[dict[str, Any]]:
        """Search D&D rule documents previously ingested into the MCP-owned database."""
        embedder, vectors = storage.dense_components()
        hits = rules.search(
            system_id="dnd5e",
            query=query,
            edition=edition,
            locale=locale,
            top_k=top_k,
            embedder=embedder,
            vector_store=vectors,
        )
        return [asdict(hit) for hit in hits]

    @mcp.tool()
    def rule_expand(chunk_id: str) -> dict[str, Any]:
        """Read a complete indexed D&D rule chunk after it was selected by search."""
        return rules.expand(chunk_id)

    @mcp.tool()
    def rule_ingest(
        source_key: str,
        title: str,
        content: str,
        locale: str = "en",
        edition: str = "",
        publication_id: str = "",
    ) -> dict[str, Any]:
        """Ingest Markdown rule content into the MCP-owned D&D rule index."""
        embedder, vectors = storage.dense_components()
        result = rules.ingest(
            system_id="dnd5e",
            source_key=source_key,
            title=title,
            content=content,
            locale=locale,
            edition=edition,
            publication_id=publication_id,
            embedder=embedder,
            vector_store=vectors,
        )
        return asdict(result)

    @mcp.tool()
    def skill_list() -> list[dict[str, str]]:
        """List installed D&D DM, campaign-manager, and module-generator skill documents."""
        return [
            {"id": item.id, "title": item.title, "source": item.source} for item in catalog.list()
        ]

    @mcp.tool()
    def skill_read(skill_id: str) -> str:
        """Read one source-of-truth SKILL.md document."""
        return catalog.read(skill_id)

    @mcp.tool()
    def skill_asset_list(source: str | None = None) -> list[dict[str, str]]:
        """List bundled text references, templates, and data files."""
        return [
            {
                "id": asset.id,
                "source": asset.source,
                "resource_uri": (f"sagasmith://asset/{catalog.resource_id(asset.id)}"),
            }
            for asset in catalog.assets()
            if source is None or asset.source == source
        ]

    @mcp.tool()
    def skill_asset_read(asset_id: str) -> str:
        """Read one text skill asset by the id returned from skill_asset_list."""
        return catalog.read_asset(asset_id)

    @mcp.resource("sagasmith://skill/{skill_id}")
    def skill_resource(skill_id: str) -> str:
        """Skill document resource addressed by its id from skill_list."""
        return catalog.read(skill_id)

    @mcp.resource(
        "sagasmith://skills/overview",
        name="SagaSmith D&D skill overview",
        description="Installed D&D and module-generation skill document ids.",
        mime_type="text/markdown",
    )
    def skill_overview_resource() -> str:
        """Expose a static skill resource for MCP clients without template discovery."""
        lines = ["# SagaSmith D&D Skills", ""]
        for document in catalog.list():
            lines.append(f"- `{document.id}` ({document.source}): {document.title}")
        lines.extend(
            [
                "",
                "Read a document with `skill_read` or `sagasmith://skill/{skill_id}`.",
                (
                    "Use `skill_asset_list` and `skill_asset_read` for references, data, "
                    "and templates."
                ),
            ]
        )
        return "\n".join(lines)

    @mcp.resource("sagasmith://asset/{resource_id}")
    def skill_asset_resource(resource_id: str) -> str:
        """Skill reference, template, or data resource addressed by its encoded resource id."""
        return catalog.read_resource_asset(resource_id)

    @mcp.prompt()
    def dnd_dm(campaign_id: str, objective: str) -> str:
        """Start a D&D DM turn with the bundled D&D DM instructions available as a resource."""
        return (
            f"You are running campaign {campaign_id}. Objective: {objective}\n\n"
            "Read sagasmith://skill/dnd.full.skills.dnd-dm before acting. Use module_search and "
            "rule_search for factual retrieval; record durable changes through the MCP tools."
        )

    @mcp.prompt()
    def module_generator(campaign_id: str, brief: str) -> str:
        """Generate an importable adventure module using the bundled module-generation workflow."""
        return (
            f"Create a D&D module for campaign {campaign_id}. Brief: {brief}\n\n"
            "Read sagasmith://skill/modulegen.root first. Write the resulting Markdown with "
            "module_write, "
            "then call module_import using the returned artifact name."
        )

    registered_tools = mcp._tool_manager.list_tools()
    validate_profile_coverage(tool.name for tool in registered_tools)
    for registered_tool in registered_tools:
        registered_tool.meta = {
            **dict(registered_tool.meta or {}),
            "sagasmith_tool_profiles": list(profiles_for_tool(registered_tool.name)),
        }

    return mcp


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
