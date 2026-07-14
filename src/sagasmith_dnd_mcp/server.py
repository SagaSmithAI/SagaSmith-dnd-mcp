"""MCP surface for the SagaSmith D&D runtime and bundled skill packs."""

from __future__ import annotations

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
from sagasmith_core.modules import MarkdownModuleParser
from sagasmith_core.systems import SystemRegistry
from sagasmith_dnd.ability_generation import apply_ability_generation, roll_ability_scores
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
from sagasmith_dnd.engine import resolve_check, roll
from sagasmith_dnd.module_profile import DndModuleProfile
from sagasmith_dnd.system import DND5E

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.skills import SkillCatalog
from sagasmith_dnd_mcp.storage import SagaSmithStorage


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
    ) -> dict[str, Any]:
        updated = characters.update(before.id, sheet=sheet, notes=notes)
        record_character_revision(before, updated, operation)
        return character_view(updated)

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
        return result.response if result is not None else None

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
        """Describe the MCP contract and the intentionally non-engine combat boundary."""
        return {
            "contract_version": "2026-07-integrity-v1",
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
                "structured_combat_engine": False,
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
    ) -> dict[str, Any]:
        """Create a D&D 5e campaign inside the MCP-owned SQLite database."""
        access.ensure_principal(principal_id, platform="mcp", external_id=principal_id)
        created = campaigns.create(
            system_id="dnd5e",
            name=name,
            description=description,
            settings={"edition": edition, "locale": locale},
        )
        access.grant_campaign(created.id, principal_id, role="owner")
        return asdict(created)

    @mcp.tool()
    def campaign_list(status: str | None = None) -> list[dict[str, Any]]:
        """List D&D 5e campaigns."""
        return [asdict(item) for item in campaigns.list(system_id="dnd5e", status=status)]

    @mcp.tool()
    def campaign_get(campaign_id: str, principal_id: str = "system:local") -> dict[str, Any]:
        """Read one campaign, including its persisted party and combat state."""
        access.require_campaign(campaign_id, principal_id)
        return asdict(campaigns.get(campaign_id))

    @mcp.tool()
    def campaign_member_grant(
        campaign_id: str,
        principal_id: str,
        role: str = "player",
        by_principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Grant DM/player/observer campaign access; caller role is resolved server-side."""
        access.require_campaign(campaign_id, by_principal_id, roles={"owner", "dm"})
        access.ensure_principal(principal_id, platform="mcp", external_id=principal_id)
        return asdict(access.grant_campaign(campaign_id, principal_id, role=role))

    @mcp.tool()
    def actor_grant(
        campaign_id: str,
        principal_id: str,
        actor_id: str,
        can_control: bool = False,
        can_view_private: bool = False,
        by_principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Grant an explicit PC/NPC control and private-sheet view permission."""
        access.require_campaign(campaign_id, by_principal_id, roles={"owner", "dm"})
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
    ) -> dict[str, Any]:
        """Apply a reviewed campaign-level update without bypassing its state document."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        before = campaigns.get(campaign_id)
        after = campaigns.update(
            campaign_id,
            name=name,
            status=status,
            description=description,
            settings=settings,
            state=validate_party_state(state) if state is not None else None,
            expected_revision=expected_revision,
        )
        fields = ("name", "status", "description", "settings", "state", "revision")
        revisions.record(
            campaign_id,
            operation="campaign.update",
            entity_type="campaign",
            entity_id=campaign_id,
            before={field: getattr(before, field) for field in fields},
            after={field: getattr(after, field) for field in fields},
            actor="mcp",
        )
        return asdict(after)

    @mcp.tool()
    def branch_list(campaign_id: str) -> list[dict[str, Any]]:
        """List playable, non-destructive campaign timelines."""
        return [asdict(item) for item in branches.list(campaign_id)]

    @mcp.tool()
    def branch_compare(
        campaign_id: str,
        left_branch_id: str,
        right_branch_id: str,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Compare facts and actor knowledge across branches without auto-merging them."""
        access.require_campaign(campaign_id, principal_id)
        return branches.compare(campaign_id, left_branch_id, right_branch_id)

    @mcp.tool()
    def branch_create(
        campaign_id: str,
        name: str,
        from_snapshot_id: str | None = None,
        checkout: bool = False,
    ) -> dict[str, Any]:
        """Fork a timeline from a snapshot without changing its source branch."""
        return asdict(
            branches.create(
                campaign_id,
                name=name,
                from_snapshot_id=from_snapshot_id,
                checkout=checkout,
            )
        )

    @mcp.tool()
    def branch_checkout(campaign_id: str, branch_id: str) -> dict[str, Any]:
        """Load a branch head as live campaign state without creating a new save."""
        snapshot = snapshots.checkout_branch(campaign_id, branch_id)
        return {
            "branch": asdict(branches.current(campaign_id)),
            "snapshot": asdict(snapshot) if snapshot else None,
        }

    @mcp.tool()
    def snapshot_create(campaign_id: str, label: str = "") -> dict[str, Any]:
        """Commit current D&D state, events, facts, and actor knowledge to this branch."""
        return asdict(snapshots.create(campaign_id, label=label))

    @mcp.tool()
    def snapshot_list(campaign_id: str) -> list[dict[str, Any]]:
        return [asdict(item) for item in snapshots.list(campaign_id)]

    @mcp.tool()
    def snapshot_restore(campaign_id: str, slot: int) -> dict[str, Any]:
        """Fork from an earlier save; existing future history remains intact."""
        return asdict(snapshots.restore(campaign_id, slot))

    @mcp.tool()
    def snapshot_verify(campaign_id: str, slot: int) -> dict[str, bool]:
        """Verify that a saved snapshot has an internally consistent payload."""
        return {"valid": snapshots.verify(campaign_id, slot)}

    @mcp.tool()
    def snapshot_lineage(campaign_id: str, slot: int | None = None) -> list[dict[str, Any]]:
        """List the lineage of a save without mutating campaign history."""
        return [asdict(item) for item in snapshots.lineage(campaign_id, slot)]

    @mcp.tool()
    def snapshot_regenerate_recap(campaign_id: str, slot: int) -> dict[str, Any]:
        """Regenerate a deterministic recap from a saved snapshot payload."""
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
    ) -> dict[str, Any]:
        """Create a D&D PC, NPC, or monster; optionally bind it to a campaign."""
        if campaign_id is not None:
            access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        normalized_sheet = validate_character_sheet(sheet or default_character_sheet())
        normalized_notes = validate_character_notes(notes or default_character_notes())
        return character_view(
            characters.create(
                system_id="dnd5e",
                name=name,
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
            character_view(item)
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
        return character_view(
            characters.instantiate(
                template_id,
                campaign_id=campaign_id,
                name=name,
                player_name=player_name,
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
        normalized_sheet = validate_character_sheet(sheet or default_character_sheet())
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
    def character_get(
        character_id: str, principal_id: str = "system:local"
    ) -> dict[str, Any]:
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
        character_id: str, sheet: dict[str, Any], *, operation: str = "character.sheet.update"
    ) -> dict[str, Any]:
        """Persist a D&D schema mutation with derived values recalculated."""
        normalized_sheet = validate_character_sheet(sheet)
        return update_character(
            characters.get(character_id), operation=operation, sheet=normalized_sheet
        )

    @mcp.tool()
    def character_sheet_replace(
        character_id: str,
        sheet: dict[str, Any],
        notes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate and replace a complete D&D v2 sheet, deriving combat and inventory fields."""
        current = characters.get(character_id)
        normalized_sheet = validate_character_sheet(sheet)
        normalized_notes = validate_character_notes(notes if notes is not None else current.notes)
        return update_character(
            current,
            operation="character.sheet.replace",
            sheet=normalized_sheet,
            notes=normalized_notes,
        )

    @mcp.tool()
    def character_wallet_adjust(
        character_id: str, denomination: str, amount: int
    ) -> dict[str, Any]:
        """Adjust one D&D character wallet denomination through the v2 schema."""
        current = characters.get(character_id)
        return update_sheet(
            character_id,
            adjust_wallet(current.sheet, denomination, amount),
            operation="character.wallet.adjust",
        )

    @mcp.tool()
    def character_inventory_add(character_id: str, item: dict[str, Any]) -> dict[str, Any]:
        """Add a normalized inventory item and return its assigned item id."""
        current = characters.get(character_id)
        sheet, item_id = add_inventory_item(current.sheet, item)
        updated = update_sheet(character_id, sheet, operation="character.inventory.add")
        return {"character": updated, "item_id": item_id}

    @mcp.tool()
    def character_inventory_update(
        character_id: str, item_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        """Update one structured inventory item without bypassing D&D validation."""
        current = characters.get(character_id)
        return update_sheet(
            character_id,
            update_inventory_item(current.sheet, item_id, patch),
            operation="character.inventory.update",
        )

    @mcp.tool()
    def character_inventory_remove(
        character_id: str, item_id: str, quantity: int | None = None
    ) -> dict[str, Any]:
        """Remove an inventory stack or quantity and return the removed item data."""
        current = characters.get(character_id)
        sheet, removed = remove_inventory_item(current.sheet, item_id, quantity)
        updated = update_sheet(character_id, sheet, operation="character.inventory.remove")
        return {"character": updated, "removed": removed}

    @mcp.tool()
    def character_inventory_equip(
        character_id: str, item_id: str, slot: str | None
    ) -> dict[str, Any]:
        """Equip an inventory item in a validated D&D equipment slot, or unequip it."""
        current = characters.get(character_id)
        return update_sheet(
            character_id,
            equip_inventory_item(current.sheet, item_id, slot),
            operation="character.inventory.equip",
        )

    @mcp.tool()
    def character_ammunition_consume(
        character_id: str, weapon_id: str, quantity: int = 1
    ) -> dict[str, Any]:
        """Consume ammunition linked to a weapon through structured mechanics."""
        current = characters.get(character_id)
        sheet, consumed = consume_weapon_ammunition(current.sheet, weapon_id, quantity)
        updated = update_sheet(character_id, sheet, operation="character.ammunition.consume")
        return {"character": updated, "consumed": consumed}

    @mcp.tool()
    def character_inventory_transfer(
        source_character_id: str,
        target_character_id: str,
        item_id: str,
        quantity: int | None = None,
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Move an inventory item between two actors in the same campaign."""
        payload = {
            "source_character_id": source_character_id,
            "target_character_id": target_character_id,
            "item_id": item_id,
            "quantity": quantity,
        }
        replay = replay_idempotent(f"character-inventory:{principal_id}", idempotency_key, payload)
        if replay is not None:
            return replay
        source = characters.get(source_character_id)
        target = characters.get(target_character_id)
        if source.campaign_id is None or source.campaign_id != target.campaign_id:
            raise ValueError("characters must belong to the same campaign")
        access.require_actor(source.campaign_id, source.id, principal_id, control=True)
        source_sheet, moved = remove_inventory_item(source.sheet, item_id, quantity)
        target_sheet = receive_inventory_item(target.sheet, moved)
        mutations = StateMutationService(storage.database)
        mutations.replace(
            source.campaign_id,
            character_updates=[
                CharacterStateUpdate(source.id, source_sheet, source.notes, source.revision),
                CharacterStateUpdate(target.id, target_sheet, target.notes, target.revision),
            ],
            operation="character.inventory.transfer",
            actor="mcp",
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
            f"character-inventory:{principal_id}",
            idempotency_key,
            payload,
            response,
            campaign_id=source.campaign_id,
        )

    @mcp.tool()
    def character_effect_add(character_id: str, effect: dict[str, Any]) -> dict[str, Any]:
        """Add a validated active D&D effect and return its assigned effect id."""
        current = characters.get(character_id)
        sheet, effect_id = add_effect(current.sheet, effect)
        updated = update_sheet(character_id, sheet, operation="character.effect.add")
        return {"character": updated, "effect_id": effect_id}

    @mcp.tool()
    def character_effect_remove(character_id: str, effect_id: str) -> dict[str, Any]:
        """Remove an active D&D effect."""
        current = characters.get(character_id)
        return update_sheet(
            character_id,
            remove_effect(current.sheet, effect_id),
            operation="character.effect.remove",
        )

    @mcp.tool()
    def character_resource_set(character_id: str, resource: str, value: int) -> dict[str, Any]:
        """Set a named character resource, enforcing its schema-defined maximum."""
        current = characters.get(character_id)
        return update_sheet(
            character_id,
            set_resource_value(current.sheet, resource, value),
            operation="character.resource.set",
        )

    @mcp.tool()
    def character_spell_prepare(character_id: str, spell_id: str, prepared: bool) -> dict[str, Any]:
        """Prepare or unprepare a spell under the D&D spellcasting constraints."""
        current = characters.get(character_id)
        return update_sheet(
            character_id,
            set_spell_prepared(current.sheet, spell_id, prepared),
            operation="character.spell.prepare" if prepared else "character.spell.unprepare",
        )

    @mcp.tool()
    def character_ability_apply(
        character_id: str,
        method: str,
        assignments: dict[str, int],
        rolls: list[int] | None = None,
    ) -> dict[str, Any]:
        """Apply a validated ability-generation method to a complete D&D character sheet."""
        current = characters.get(character_id)
        sheet = apply_ability_generation(
            current.sheet,
            method=method,
            assignments=assignments,
            rolls=rolls,
        )
        return update_sheet(character_id, sheet, operation="character.ability.apply")

    @mcp.tool()
    def character_memory_add(character_id: str, memory: dict[str, Any]) -> dict[str, Any]:
        """Append a legacy actor-notes memory without altering actor knowledge."""
        current = characters.get(character_id)
        notes, memory_id = add_memory(current.notes, memory)
        return {
            "character": update_character(
                current,
                operation="character.memory.add",
                notes=validate_character_notes(notes, character_type=current.character_type),
            ),
            "memory_id": memory_id,
        }

    @mcp.tool()
    def character_memory_resolve(
        character_id: str, memory_id: str, status: str = "resolved"
    ) -> dict[str, Any]:
        """Resolve one legacy actor-notes memory without altering actor knowledge."""
        current = characters.get(character_id)
        notes = resolve_memory(current.notes, memory_id, status=status)
        return update_character(
            current,
            operation="character.memory.resolve",
            notes=validate_character_notes(notes, character_type=current.character_type),
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
    ) -> dict[str, Any]:
        """Add an item to the campaign shared inventory."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        before = campaigns.get(campaign_id)
        sheet, item_id = add_inventory_item(party_sheet(before.state), item)
        after = campaigns.update(campaign_id, state=party_state(before.state, sheet))
        revisions.record(
            campaign_id,
            operation="party.inventory.add",
            entity_type="campaign",
            entity_id=campaign_id,
            before={"state": before.state, "revision": before.revision},
            after={"state": after.state, "revision": after.revision},
            actor="mcp",
        )
        return {"inventory": sheet["inventory"], "item_id": item_id}

    @mcp.tool()
    def party_inventory_remove(
        campaign_id: str,
        item_id: str,
        quantity: int | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Remove an item or partial stack from the campaign shared inventory."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        before = campaigns.get(campaign_id)
        sheet, removed = remove_inventory_item(party_sheet(before.state), item_id, quantity)
        after = campaigns.update(campaign_id, state=party_state(before.state, sheet))
        revisions.record(
            campaign_id,
            operation="party.inventory.remove",
            entity_type="campaign",
            entity_id=campaign_id,
            before={"state": before.state, "revision": before.revision},
            after={"state": after.state, "revision": after.revision},
            actor="mcp",
        )
        return {"inventory": sheet["inventory"], "removed": removed}

    @mcp.tool()
    def party_inventory_transfer(
        campaign_id: str,
        character_id: str,
        item_id: str,
        direction: str,
        quantity: int | None = None,
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Deposit an actor item to, or withdraw one from, the party shared inventory."""
        if direction not in {"deposit", "withdraw"}:
            raise ValueError("direction must be deposit or withdraw")
        payload = {
            "campaign_id": campaign_id,
            "character_id": character_id,
            "item_id": item_id,
            "direction": direction,
            "quantity": quantity,
        }
        replay = replay_idempotent(f"party-inventory:{principal_id}", idempotency_key, payload)
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
                    character.id, character_sheet, character.notes, character.revision
                )
            ],
            operation=f"party.inventory.{direction}",
            actor="mcp",
            idempotency_key=idempotency_key,
        )
        character_after = characters.get(character_id)
        response = {
            "party": party_show(campaign_id, principal_id=principal_id),
            "character": character_view(character_after),
            "item": moved,
        }
        return remember_idempotent(
            f"party-inventory:{principal_id}",
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
    ) -> dict[str, Any]:
        """Credit or debit one denomination in the shared party wallet."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        before = campaigns.get(campaign_id)
        sheet = adjust_wallet(party_sheet(before.state), denomination, amount)
        after = campaigns.update(campaign_id, state=party_state(before.state, sheet))
        revisions.record(
            campaign_id,
            operation="party.wallet.adjust",
            entity_type="campaign",
            entity_id=campaign_id,
            before={"state": before.state, "revision": before.revision},
            after={"state": after.state, "revision": after.revision},
            actor="mcp",
        )
        return sheet["inventory"]["wallet"]

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
        payload = {
            "campaign_id": campaign_id,
            "character_id": character_id,
            "denomination": denomination,
            "amount": amount,
            "direction": direction,
            "expected_campaign_revision": expected_campaign_revision,
            "expected_character_revision": expected_character_revision,
        }
        replay = replay_idempotent(f"party-wallet:{principal_id}", idempotency_key, payload)
        if replay is not None:
            return replay
        campaign = campaigns.get(campaign_id)
        character = characters.get(character_id)
        if character.campaign_id != campaign_id:
            raise ValueError("character must belong to the campaign")
        access.require_actor(campaign_id, character_id, principal_id, control=True)
        if (
            expected_campaign_revision is not None
            and campaign.revision != expected_campaign_revision
        ):
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
            idempotency_key=idempotency_key,
        )
        character_after = characters.get(character_id)
        response = {
            "party": party_show(campaign_id, principal_id=principal_id),
            "character": character_view(character_after),
        }
        return remember_idempotent(
            f"party-wallet:{principal_id}",
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
    ) -> dict[str, Any]:
        """Resolve a D&D ability check with proficiency and advantage rules."""
        return resolve_check(
            dc=dc,
            ability_score=ability_score,
            proficient=proficient,
            level=level,
            bonus=bonus,
            advantage=advantage,
            disadvantage=disadvantage,
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
    ) -> dict[str, Any]:
        """Update a D&D character sheet or supporting notes."""
        normalized_sheet = validate_character_sheet(sheet) if sheet is not None else None
        normalized_notes = validate_character_notes(notes) if notes is not None else None
        before = characters.get(character_id)
        if before.campaign_id is not None:
            access.require_actor(before.campaign_id, before.id, principal_id, control=True)
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

    @mcp.tool()
    def memory_add(
        campaign_id: str,
        content: str,
        kind: str = "fact",
        subject: str = "",
        metadata: dict[str, Any] | None = None,
        branch_id: str | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Record a durable campaign fact, event, relationship, or NPC memory."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        return asdict(
            memories.add(
                campaign_id,
                content=content,
                kind=kind,
                subject=subject,
                metadata=metadata,
                branch_id=branch_id,
            )
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
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Append a branch-local chronology event; an event is not actor knowledge."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        return asdict(
            events.add(
                campaign_id,
                summary=summary,
                event_type=event_type,
                payload=payload,
                audience_scope=audience_scope,
                branch_id=branch_id,
            )
        )

    @mcp.tool()
    def event_list(
        campaign_id: str,
        limit: int = 50,
        branch_id: str | None = None,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        membership = access.require_campaign(campaign_id, principal_id)
        values = events.list(campaign_id, limit=limit, branch_id=branch_id)
        if membership.role not in {"owner", "dm"}:
            values = [
                item
                for item in values
                if item.audience_scope in {"public", "party", "player"}
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
    ) -> dict[str, Any]:
        """Record what one live PC, NPC, or monster knows or believes."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        return asdict(
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
    ) -> dict[str, Any]:
        """Append a new subjective revision, e.g. a rumor or Modify Memory effect."""
        current = knowledge.get(knowledge_id)
        access.require_campaign(current.campaign_id, principal_id, roles={"owner", "dm"})
        return asdict(
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

    @mcp.tool()
    def actor_knowledge_list(
        campaign_id: str,
        actor_id: str,
        branch_id: str | None = None,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        access.require_actor(campaign_id, actor_id, principal_id, private=True)
        return [
            asdict(item)
            for item in knowledge.list(campaign_id, actor_id=actor_id, branch_id=branch_id)
        ]

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
        return [
            asdict(item)
            for item in knowledge.search(
                campaign_id,
                actor_id=actor_id,
                query=query,
                branch_id=branch_id,
                limit=limit,
            )
        ]

    @mcp.tool()
    def state_history(campaign_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """List audited reversible campaign and character mutations."""
        return [asdict(item) for item in revisions.history(campaign_id, limit=limit)]

    @mcp.tool()
    def state_undo(campaign_id: str) -> dict[str, Any]:
        """Undo the latest audited mutation without deleting snapshots."""
        return asdict(revisions.undo(campaign_id))

    @mcp.tool()
    def state_redo(campaign_id: str) -> dict[str, Any]:
        """Redo the next audited mutation on the current state-revision branch."""
        return asdict(revisions.redo(campaign_id))

    @mcp.tool()
    def combat_status(campaign_id: str) -> dict[str, Any] | None:
        """Read the campaign's persisted combat turn state."""
        return campaigns.get(campaign_id).state.get("combat")

    @mcp.tool()
    def combat_start(campaign_id: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
        """Start combat with an optional initiative/participant payload."""
        campaign = campaigns.get(campaign_id)
        updated_state = dict(campaign.state)
        updated_state["combat"] = {"active": True, "round": 1, "turn": 0, **(state or {})}
        return campaign_update(campaign_id, state=updated_state)["state"]["combat"]

    @mcp.tool()
    def combat_act(campaign_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Advance or patch active combat state through the auditable campaign document."""
        campaign = campaigns.get(campaign_id)
        combat = dict(campaign.state.get("combat") or {})
        if not combat.get("active"):
            raise ValueError("combat is not active")
        combat.update(patch)
        updated_state = dict(campaign.state)
        updated_state["combat"] = combat
        return campaign_update(campaign_id, state=updated_state)["state"]["combat"]

    @mcp.tool()
    def combat_end(campaign_id: str) -> dict[str, Any]:
        """End combat while returning the preserved final combat state."""
        campaign = campaigns.get(campaign_id)
        combat = campaign.state.get("combat")
        updated_state = dict(campaign.state)
        updated_state["combat"] = None
        campaign_update(campaign_id, state=updated_state)
        return {"ended": True, "combat": combat}

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
    def module_write(name: str, content: str) -> dict[str, str]:
        """Write generated Markdown to the managed artifact directory before importing it."""
        path = storage.write_module(name, content)
        return {"artifact": path.name, "path": str(path)}

    @mcp.tool()
    def module_inspect(artifact: str) -> dict[str, Any]:
        """Inspect a managed Markdown artifact before importing it into a campaign."""
        return modules.inspect_path(storage.artifact_module_path(artifact))

    @mcp.tool()
    def module_import(campaign_id: str, artifact: str, title: str | None = None) -> dict[str, Any]:
        """Import a Markdown artifact created by module_write into a campaign."""
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
    def module_list(campaign_id: str) -> list[dict[str, Any]]:
        """List a campaign's imported modules."""
        return modules.list(campaign_id)

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
        return [
            item
            for item in index
            if item.get("visibility", "keeper") in {"public", "party"}
        ]

    @mcp.tool()
    def module_expand(chunk_id: str) -> dict[str, Any]:
        """Read a complete module chunk after it was selected by search."""
        return modules.expand(chunk_id)

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
    def module_current(campaign_id: str, scope_id: str = "party") -> dict[str, Any] | None:
        """Read the current scene for party, group, or player scope with party fallback."""
        return modules.current_scene(campaign_id, scope_id=scope_id)

    @mcp.tool()
    def module_set_progress(
        campaign_id: str,
        scene_id: str,
        scope_id: str = "party",
        status: str = "current",
        progress: int = 0,
        state: dict[str, Any] | None = None,
        current_room: str | None = None,
    ) -> dict[str, Any]:
        """Persist scoped scene progress without changing another scope's current scene."""
        return modules.set_scene_progress(
            campaign_id=campaign_id,
            scene_id=scene_id,
            scope_id=scope_id,
            status=status,
            progress=progress,
            state=state,
            current_room=current_room,
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

    return mcp


def main() -> None:
    create_server().run(transport="stdio")
