"""MCP surface for the SagaSmith D&D runtime and bundled skill packs."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
from typing import Any, Literal
from uuid import uuid4
from weakref import WeakValueDictionary

from mcp.server.fastmcp import FastMCP, Image
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
    ImportJobService,
    MemoryService,
    ModuleService,
    RevisionService,
    RulePackService,
    RuleProfileService,
    RuleReceiptService,
    RuleService,
    SnapshotService,
    StateMutationService,
    default_local_principal,
    render_pdf_page,
)
from sagasmith_core.idempotency import request_hash
from sagasmith_core.modules import MarkdownModuleParser
from sagasmith_core.rule_packs import RulePackError, RulesetUnavailableError, content_checksum
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
    validate_world_effect,
)
from sagasmith_dnd.combat_engine import (
    CombatEngineError,
    NeedsRulingError,
    add_choice_window,
    apply_attack_ac_bonus,
    apply_concentration_result,
    apply_damage_parts_to_sheet,
    apply_damage_to_sheet,
    apply_healing_to_sheet,
    arm_readied_spell,
    available_actions,
    available_attack_defenses,
    available_reactions,
    current_combatant,
    end_turn,
    pay_activity_activation,
    pay_attack_action,
    preflight_attack,
    queue_combatant,
    resolve_actor_check,
    resolve_attack_damage,
    resolve_choice_window,
    resolve_common_action,
    resolve_death_save_to_sheet,
    resolve_readied_action_window,
    resolve_readied_spell_window,
    resolve_second_wind_to_sheet,
    roll_attack_action,
    settle_core_activity_effect,
    spend_movement,
    stabilize_sheet,
    stand_up,
    start_encounter,
    trigger_readied_action,
    trigger_readied_spell,
)
from sagasmith_dnd.content_import import (
    compiled_artifacts_from_candidates,
    extract_content_candidates,
    validate_selection_ready_artifacts,
)
from sagasmith_dnd.core_content import PACK_ID as CORE_CONTENT_PACK_ID
from sagasmith_dnd.core_content import PACK_VERSION as CORE_CONTENT_PACK_VERSION
from sagasmith_dnd.core_content import build_srd2014_content
from sagasmith_dnd.core_rule_pack import get_core_rule_pack
from sagasmith_dnd.engine import resolve_check, roll
from sagasmith_dnd.lifecycle import (
    advance_effect_durations,
    advance_world_effect_durations,
    apply_rest,
    recover_stable_creature,
    stand_outside_combat,
)
from sagasmith_dnd.module_profile import DndModuleProfile
from sagasmith_dnd.progression import advance_single_class_level
from sagasmith_dnd.rule_engine import (
    RuleCompilationError,
    apply_rule_event,
    compile_mechanics,
    context_with_facts,
    core_receipts,
    resolution_context,
    run_mechanic_tests,
    validate_source_bound_mechanics,
)
from sagasmith_dnd.rule_providers import load_native_rule_providers
from sagasmith_dnd.spatial import (
    BattleMapError,
    compile_battle_map,
    patch_battle_map,
    validate_position,
)
from sagasmith_dnd.spells import (
    available_shield_attack_defenses,
    available_shield_magic_missile_defenses,
    consume_readied_spell,
    consume_shield_reaction,
    consume_spell_cast,
    is_core_magic_missile_spell,
    replace_prepared_spells,
    validate_magic_missile_allocations,
    validate_spell_grant,
)
from sagasmith_dnd.statblocks import apply_statblock_variant, parse_2014_statblock
from sagasmith_dnd.system import DND5E

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.exposure import Exposure, ExposureError, ExposureRegistry
from sagasmith_dnd_mcp.skills import SkillCatalog
from sagasmith_dnd_mcp.storage import SagaSmithStorage
from sagasmith_dnd_mcp.tool_profiles import (
    CORE_TOOLS,
    GROUP_BY_ID,
    PROFILE_COMBAT,
    PROFILE_LOBBY,
    PROFILE_PLAY,
    group_catalog,
    groups_for_tool,
    profile_catalog,
    profiles_for_tool,
    validate_profile_coverage,
)


def _validated_distinct_choices(value: Any, *, count: int, label: str) -> list[str]:
    if value is None:
        values: list[Any] = []
    elif isinstance(value, list):
        values = value
    else:
        raise ValueError(f"{label} must be a list")
    normalized = [str(item).strip() for item in values]
    if len(normalized) != count or any(not item for item in normalized):
        raise ValueError(f"{label} requires exactly {count} choices")
    if len({item.casefold() for item in normalized}) != len(normalized):
        raise ValueError(f"{label} choices must be distinct")
    return normalized


class SessionExposureFastMCP(FastMCP):
    """FastMCP with server-owned, session-scoped progressive tool exposure.

    Direct in-process calls retain FastMCP's normal behaviour for library users
    and deterministic unit tests. Actual MCP requests have a Context/session and
    are always checked against the exposure registry.
    """

    def __init__(
        self,
        *args: Any,
        exposure_registry: ExposureRegistry,
        phase_lookup: Any,
        scope_validator: Any,
        **kwargs: Any,
    ) -> None:
        self.exposure_registry = exposure_registry
        self._phase_lookup = phase_lookup
        self._scope_validator = scope_validator
        self._sessions: WeakValueDictionary[str, Any] = WeakValueDictionary()
        self._exposure_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        super().__init__(*args, **kwargs)

    def _request_session(self) -> tuple[str, Any] | None:
        try:
            context = self.get_context()
            session = context.session
        except (LookupError, ValueError):
            return None
        key = getattr(session, "_sagasmith_exposure_session_key", None)
        if key is None:
            key = f"mcp:{uuid4().hex}"
            setattr(session, "_sagasmith_exposure_session_key", key)
        self._sessions[key] = session
        return key, session

    def _exposure_lock(self, exposure_id: str) -> asyncio.Lock:
        return self._exposure_locks.setdefault(exposure_id, asyncio.Lock())

    def _principal_argument(self, tool_id: str) -> str | None:
        tool = self._tool_manager.get_tool(tool_id)
        properties = dict((tool.parameters if tool else {}).get("properties") or {})
        for name in ("auth_principal_id", "by_principal_id", "principal_id"):
            if name in properties:
                return name
        return None

    def _bind_exposure_principal(
        self,
        exposure: Exposure,
        tool_id: str,
        arguments: dict[str, Any],
        *,
        inject_missing: bool,
    ) -> dict[str, Any]:
        """Keep an exposure bound to the principal that opened it.

        ``access_grant`` is the lone public facade whose ``principal_id`` names
        the grant target; its authenticated writer is ``by_principal_id``.
        """
        result = dict(arguments)
        principal_argument = self._principal_argument(tool_id)
        if principal_argument is None:
            return result
        supplied = result.get(principal_argument)
        if supplied is not None and supplied != exposure.principal_id:
            raise ExposureError(
                "Tool principal_id does not match the principal that opened this session exposure."
            )
        if inject_missing:
            result[principal_argument] = exposure.principal_id
        return result

    async def _refresh(self, session_key: str, campaign_id: str | None = None) -> bool:
        changed = False
        exposures = (
            [self.exposure_registry.active(session_key)]
            if campaign_id is None
            else list(self.exposure_registry.for_campaign(campaign_id))
        )
        for exposure in exposures:
            if exposure is None or exposure.campaign_id is None:
                continue
            changed = (
                self.exposure_registry.refresh_phase(
                    exposure, self._phase_lookup(exposure.campaign_id)
                )
                or changed
            )
        if changed:
            for key, _ in self.exposure_registry.active_items(campaign_id):
                session = self._sessions.get(key)
                if session is not None:
                    await session.send_tool_list_changed()
        return changed

    async def list_tools(self):  # type: ignore[override]
        request = self._request_session()
        if request is None:
            return await super().list_tools()
        session_key, _ = request
        await self._refresh(session_key)
        visible = self.exposure_registry.visible_tools(self.exposure_registry.active(session_key))
        return [tool for tool in await super().list_tools() if tool.name in visible]

    async def call_tool(self, name: str, arguments: dict[str, Any]):  # type: ignore[override]
        request = self._request_session()
        if request is None:
            return await super().call_tool(name, arguments)
        session_key, _ = request
        await self._refresh(session_key)
        exposure = self.exposure_registry.active(session_key)
        if name not in CORE_TOOLS and exposure is None:
            raise ExposureError(
                "No active exposure for this MCP session. Call exposure_open, then exposure_load."
            )
        if exposure is not None and not name.startswith("exposure_"):
            arguments = self._bind_exposure_principal(
                exposure, name, arguments, inject_missing=False
            )
            self._scope_validator(exposure, name, arguments)
        if name not in CORE_TOOLS:
            assert exposure is not None
            async with self._exposure_lock(exposure.id):
                self.exposure_registry.require_tool(exposure, name)
                result = await super().call_tool(name, arguments)
                exposure_changed = self.exposure_registry.consume_tool(exposure, name)
        else:
            result = await super().call_tool(name, arguments)
            exposure_changed = False
        if name in {"exposure_open", "exposure_load", "exposure_unload"}:
            session = self._sessions.get(session_key)
            if session is not None:
                await session.send_tool_list_changed()
        if exposure_changed:
            session = self._sessions.get(session_key)
            if session is not None:
                await session.send_tool_list_changed()
        campaign_id = str(arguments.get("campaign_id") or "") or None
        if campaign_id and name in {"game_phase", "combat_start", "combat_end"}:
            await self._refresh(session_key, campaign_id)
        return result


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
    import_jobs = ImportJobService(storage.database)
    default_local_principal(storage.database)
    memories = MemoryService(storage.database)
    modules = ModuleService(storage.database)
    rules = RuleService(storage.database)
    rule_packs = RulePackService(storage.database)
    rule_profiles = RuleProfileService(storage.database)
    rule_receipts = RuleReceiptService(storage.database)
    revisions = RevisionService(storage.database)
    snapshots = SnapshotService(storage.database)
    catalog = SkillCatalog(
        dnd_root=config.dnd_skills_dir,
        modulegen_root=config.modulegen_skills_dir,
    )
    native_rule_providers = load_native_rule_providers()

    def profile_options_with_core_lock(
        edition: str, options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        core_pack = get_core_rule_pack(edition)
        return {
            **dict(options or {}),
            "_core_rule_pack_lock": {
                "id": core_pack.id,
                "version": core_pack.version,
                "fingerprint": core_pack.fingerprint,
            },
        }

    def effective_rule_context(
        campaign_id: str,
        *,
        facts: dict[str, Any] | None = None,
        branch_id: str | None = None,
    ) -> Any:
        """Compile the exact current branch lock for one pure runtime call."""
        effective = rule_packs.effective_ruleset(campaign_id, branch_id=branch_id)
        value = asdict(effective)
        enabled_pack_ids = {str(item["pack_id"]) for item in effective.lock}
        native = [
            {
                "id": provider.id,
                "abi_version": provider.abi_version,
                "pack_id": provider.pack_id,
                "mechanics": provider.mechanics(),
            }
            for provider in native_rule_providers.values()
            if provider.pack_id in enabled_pack_ids
        ]
        if native:
            value["mechanics"] = [
                *list(value["mechanics"]),
                *(mechanic for provider in native for mechanic in provider["mechanics"]),
            ]
            value["fingerprint"] = content_checksum(
                {"base": effective.fingerprint, "native": native}
            )
        try:
            context = resolution_context(value, facts=facts)
        except ValueError as error:
            raise RulesetUnavailableError(
                "campaign requires an unsupported built-in core edition"
            ) from error
        profile = rule_profiles.get(campaign_id)
        expected_core = dict((profile.options if profile else {}).get("_core_rule_pack_lock") or {})
        if not expected_core:
            raise RulesetUnavailableError(
                "campaign has no locked built-in core rule pack; "
                "the DM must explicitly set the campaign rule profile"
            )
        if expected_core != {
            "id": context.core_pack.id,
            "version": context.core_pack.version,
            "fingerprint": context.core_pack.fingerprint,
        }:
            raise RulesetUnavailableError(
                "locked built-in core rule pack is unavailable; "
                "runtime upgrade needs explicit relock"
            )
        return context

    def effective_ruleset_view(campaign_id: str, *, branch_id: str | None = None) -> dict[str, Any]:
        effective = rule_packs.effective_ruleset(campaign_id, branch_id=branch_id)
        context = effective_rule_context(campaign_id, branch_id=branch_id)
        value = asdict(effective)
        value["extension_fingerprint"] = value["fingerprint"]
        value["fingerprint"] = context.fingerprint
        value["core_pack"] = {
            "id": context.core_pack.id,
            "version": context.core_pack.version,
            "edition": context.core_pack.edition,
            "fingerprint": context.core_pack.fingerprint,
        }
        return value

    def save_rule_pack_draft(
        *,
        manifest: dict[str, Any],
        artifacts: list[dict[str, Any]] | None,
        mechanics: list[dict[str, Any]] | None,
        provenance: dict[str, Any] | None,
    ) -> dict[str, Any]:
        compiler_errors = validate_selection_ready_artifacts(artifacts or [])
        try:
            compile_mechanics(mechanics or [])
        except RuleCompilationError as error:
            compiler_errors.append(str(error))
        declared_tests = list(manifest.get("tests") or [])
        if mechanics and not declared_tests:
            compiler_errors.append("executable rule packs require declarative tests")
        elif mechanics and not compiler_errors:
            report = run_mechanic_tests(mechanics or [], declared_tests)
            if not report["passed"]:
                compiler_errors.extend(
                    error
                    for case in report["cases"]
                    if not case["passed"]
                    for error in case["errors"]
                )
                if report["mechanics_uncovered"]:
                    compiler_errors.append(
                        "declarative tests do not exercise mechanics: "
                        + ", ".join(report["mechanics_uncovered"])
                    )
        result = rule_packs.save_draft(
            manifest=manifest,
            artifacts=artifacts,
            mechanics=mechanics,
            provenance=provenance,
            additional_errors=compiler_errors,
        )
        return asdict(result)

    def ensure_core_content_pack() -> None:
        """Install the structured SRD catalog once; availability is edition-based."""
        if not config.dnd_skills_dir.exists():
            return
        try:
            existing = rule_packs.get_version(CORE_CONTENT_PACK_ID, CORE_CONTENT_PACK_VERSION)
            if existing.status == "installed":
                return
        except LookupError:
            pass
        manifest, artifacts = build_srd2014_content(config.dnd_skills_dir)
        if not artifacts:
            return
        result = rule_packs.save_draft(
            manifest=manifest,
            artifacts=artifacts,
            provenance={"source": "bundled-srd2014", "structured": True},
        )
        if result.status == "validated":
            rule_packs.install(CORE_CONTENT_PACK_ID, CORE_CONTENT_PACK_VERSION)

    ensure_core_content_pack()

    def checked_rule_facts(value: dict[str, Any] | None) -> dict[str, Any]:
        facts = dict(value or {})
        reserved = {"actor_id", "kind", "ability", "dc"} & facts.keys()
        if reserved:
            raise ValueError("rule_facts cannot override: " + ", ".join(sorted(reserved)))
        if len(facts) > 32 or len(repr(facts)) > 8192:
            raise ValueError("rule_facts exceed the safe settlement limit")
        if any(not isinstance(key, str) or not key.strip() for key in facts):
            raise ValueError("rule_facts keys must be non-empty strings")
        return facts

    def assert_snapshot_core_available(document: dict[str, Any]) -> None:
        """Fail before materialization when a save's exact built-in core is unavailable."""
        profile = dict(document.get("payload", {}).get("rule_profile") or {})
        options = dict(profile.get("options") or {})
        locked = dict(options.get("_core_rule_pack_lock") or {})
        if not locked:
            raise RulesetUnavailableError(
                "snapshot has no locked built-in core rule pack; "
                "it cannot be restored without explicit conversion"
            )
        edition = str(profile.get("edition") or "")
        try:
            core_pack = get_core_rule_pack(edition)
        except (KeyError, ValueError) as error:
            raise RulesetUnavailableError(
                f"snapshot requires unsupported D&D edition {edition!r}"
            ) from error
        available = {
            "id": core_pack.id,
            "version": core_pack.version,
            "fingerprint": core_pack.fingerprint,
        }
        if locked != available:
            raise RulesetUnavailableError(
                "snapshot's locked built-in core rule pack is unavailable; "
                "runtime upgrade needs an explicit conversion before restore"
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

    def authoritative_phase(campaign_id: str) -> str:
        campaign = campaigns.get(campaign_id)
        state = dict(campaign.state or {})
        combat = state.get("combat")
        if isinstance(combat, dict) and combat.get("active", False):
            return PROFILE_COMBAT
        phase = str(state.get("game_phase") or PROFILE_LOBBY)
        return phase if phase in {PROFILE_LOBBY, PROFILE_PLAY} else PROFILE_LOBBY

    def validate_exposure_scope(
        exposure: Exposure, tool_id: str, arguments: dict[str, Any]
    ) -> None:
        """Prevent one campaign's phase exposure from being reused for another campaign."""
        matching_groups = [
            GROUP_BY_ID[group_id]
            for group_id in exposure.loaded_groups
            if tool_id in GROUP_BY_ID[group_id].tools
        ]
        protected_groups = [group for group in matching_groups if group.roles]
        if matching_groups and len(protected_groups) == len(matching_groups):
            if exposure.campaign_id is None:
                raise ExposureError(f"Tool {tool_id!r} requires a campaign-bound exposure.")
            roles = set().union(*(group.roles for group in protected_groups))
            access.require_campaign(exposure.campaign_id, exposure.principal_id, roles=roles)
        if exposure.campaign_id is None:
            return

        campaign_ids: set[str] = set()
        character_ids: set[str] = set()

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "campaign_id" and item:
                        campaign_ids.add(str(item))
                    elif (key == "character_id" or key.endswith("_character_id")) and item:
                        character_ids.add(str(item))
                    elif key == "actor_id" and item:
                        character_ids.add(str(item))
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(arguments)
        owner = str(arguments.get("owner") or "")
        owner_id = arguments.get("owner_id")
        if owner == "party" and owner_id:
            campaign_ids.add(str(owner_id))
        elif owner == "character" and owner_id:
            character_ids.add(str(owner_id))
        if tool_id == "module_expand" and arguments.get("chunk_id"):
            expanded = modules.expand(str(arguments["chunk_id"]))
            if expanded.get("campaign_id"):
                campaign_ids.add(str(expanded["campaign_id"]))

        for character_id in character_ids:
            try:
                character = characters.get(character_id)
            except LookupError:
                continue
            if character.campaign_id:
                campaign_ids.add(str(character.campaign_id))

        mismatched = sorted(item for item in campaign_ids if item != exposure.campaign_id)
        if mismatched:
            raise ExposureError(
                f"Tool {tool_id!r} targets campaign {mismatched[0]!r}, but this exposure is "
                f"bound to {exposure.campaign_id!r}. Open a separate exposure for that campaign."
            )

    exposures = ExposureRegistry()
    mcp = SessionExposureFastMCP(
        "SagaSmith D&D",
        instructions="D&D 5e campaign runtime, module storage, and skill packs.",
        exposure_registry=exposures,
        phase_lookup=authoritative_phase,
        scope_validator=validate_exposure_scope,
    )

    def character_view(character: Any) -> dict[str, Any]:
        """Return a raw validated sheet together with its non-persisted derived view."""
        value = asdict(character)
        try:
            rules_context = (
                effective_rule_context(character.campaign_id) if character.campaign_id else None
            )
            value["derived"] = derive_character_sheet(value["sheet"], rules=rules_context)
        except RulesetUnavailableError as error:
            value["derived"] = derive_character_sheet(value["sheet"])
            value["derived"]["unresolved_rules"] = sorted(
                {*value["derived"].get("unresolved_rules", []), "ruleset_unavailable"}
            )
            value["ruleset_error"] = str(error)
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

    def library_character_view(character: Any, principal_id: str) -> dict[str, Any]:
        """Keep reusable sheets usable without exposing campaign-less private notes."""
        if principal_id == "system:local":
            return character_view(character)
        value = character_view(character)
        value.pop("notes", None)
        value.pop("player_name", None)
        value["notes_redacted"] = True
        return value

    def is_dm(campaign_id: str, principal_id: str) -> bool:
        return access.require_campaign(campaign_id, principal_id).role in {"owner", "dm"}

    def require_character_control(character: Any, principal_id: str) -> None:
        if character.campaign_id is None:
            if principal_id != "system:local":
                raise PermissionError("only the local service may modify library characters")
            return
        access.require_actor(character.campaign_id, character.id, principal_id, control=True)

    def require_outside_active_combat(character: Any, operation: str) -> None:
        """Keep direct card mutations from bypassing encounter action economy."""
        if character.campaign_id is None:
            return
        combat = dict(campaigns.get(character.campaign_id).state or {}).get("combat")
        if isinstance(combat, dict) and combat.get("active", False):
            raise CombatEngineError(f"{operation} is not allowed while combat is active")

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

    def combat_card_readiness(character: Any) -> dict[str, Any]:
        """Summarize whether a card can enter structured combat without hidden gaps."""
        view = character_view(character)
        derived = dict(view.get("derived") or {})
        sheet = dict(view.get("sheet") or {})
        attacks = list(dict(derived.get("inventory") or {}).get("weapon_attacks") or [])
        multiattacks = list(derived.get("multiattack_options") or [])
        spellcasting = dict(derived.get("spellcasting") or {})
        prepared_spells = list(spellcasting.get("prepared_spell_ids") or [])
        unresolved = list(derived.get("unresolved_rules") or [])
        hit_points = int(dict(derived.get("hit_points") or {}).get("value", 0) or 0)
        conditions = {str(item).strip().casefold() for item in sheet.get("conditions", [])}
        blockers = list(unresolved)
        if hit_points <= 0:
            blockers.append("zero_hit_points")
        if "dead" in conditions:
            blockers.append("dead")
        dm_notes = str(
            dict(dict(view.get("notes") or {}).get("profile") or {}).get("dm_notes") or ""
        )
        manual_rulings: list[str] = []
        for line in dm_notes.splitlines():
            if "Manual rulings:" not in line:
                continue
            value = line.split("Manual rulings:", 1)[1].strip().rstrip(".")
            value = value.partition(" Variant source:")[0].rstrip(". ")
            manual_rulings.extend(item.strip() for item in value.split(";") if item.strip())
        manual_rulings = list(dict.fromkeys(manual_rulings))
        specific_multiattacks = {
            item.split(":", 1)[0]
            for item in manual_rulings
            if item.endswith("Multiattack composition requires a DM ruling")
        }
        manual_rulings = [
            item
            for item in manual_rulings
            if not (
                item.endswith("descriptive action is not automatically settled")
                and item.split(":", 1)[0] in specific_multiattacks
            )
        ]
        settlement = (
            "dm_ruling_required" if unresolved else "mixed" if manual_rulings else "automatic"
        )
        return {
            "ready": not blockers,
            "settlement": settlement,
            "blocking_reasons": sorted(set(blockers)),
            "unresolved_rules": unresolved,
            "manual_rulings": manual_rulings,
            "hit_points": hit_points,
            "maximum_hit_points": int(dict(derived.get("hit_points") or {}).get("max", 0) or 0),
            "armor_class": int(derived.get("armor_class", 10) or 10),
            "weapon_attack_ids": [str(item.get("item_id") or "") for item in attacks],
            "multiattack_option_ids": [str(item.get("id") or "") for item in multiattacks],
            "prepared_spell_ids": prepared_spells,
            "unarmed_fallback": not attacks,
        }

    def statblock_variant_evidence(
        campaign_id: str,
        variant: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Resolve a variant citation instead of trusting a free-form source label."""
        if variant is None:
            return None
        if not isinstance(variant, dict):
            raise ValueError("statblock variant must be an object")
        source_ref = str(variant.get("source_ref") or "").strip()
        kind, separator, identifier = source_ref.partition(":")
        if not separator or not identifier:
            raise ValueError("statblock variant source_ref must identify a managed source")
        if kind == "module-chunk":
            expanded = modules.expand(identifier)
            if str(expanded.get("campaign_id") or "") != campaign_id:
                raise ValueError("statblock variant module chunk does not belong to campaign")
            return {
                "source_ref": source_ref,
                "kind": kind,
                "id": identifier,
                "module_id": expanded["module"]["id"],
                "scene_id": expanded["scene"]["id"],
                "page_start": expanded.get("page_start"),
                "page_end": expanded.get("page_end"),
            }
        if kind == "module-review":
            review = modules.get_content_review(campaign_id, identifier)
            return {
                "source_ref": source_ref,
                "kind": kind,
                "id": identifier,
                "module_id": review["module_id"],
                "scene_id": review["scene_id"],
                "evidence": deepcopy(review.get("evidence") or {}),
            }
        if kind == "rule-chunk":
            expanded = rules.expand(identifier)
            source = rules.source(str(expanded["source"]["id"]))
            campaign_edition = str(campaigns.get(campaign_id).settings.get("edition") or "2024")
            if str(source.get("system_id") or "") != "dnd5e":
                raise ValueError("statblock variant rule chunk must belong to D&D")
            if str(source.get("edition") or "") != campaign_edition:
                raise ValueError("statblock variant rule chunk edition does not match campaign")
            return {
                "source_ref": source_ref,
                "kind": kind,
                "id": identifier,
                "source_id": source["id"],
                "source_key": source["source_key"],
                "checksum": source["checksum"],
            }
        raise ValueError(
            "statblock variant source_ref must use module-chunk, module-review, or rule-chunk"
        )

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
            value.pop("reinforcements", None)
            value.pop("participant_manifest", None)
            battle_map = value.get("battle_map")
            if isinstance(battle_map, dict):
                value["battle_map"] = {
                    key: deepcopy(battle_map[key])
                    for key in (
                        "id",
                        "schema_version",
                        "lifecycle",
                        "source",
                        "grid",
                        "bounds",
                    )
                    if key in battle_map
                }
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
                "natural",
                "rolls",
                "rerolls",
                "total",
                "bonus",
                "success",
                "successes",
                "failures",
                "outcome",
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
                "defense",
                "reaction_defense",
                "pending_reaction",
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

    def require_encounter_combatant(
        encounter: dict[str, Any], actor_id: str, *, role: str = "actor"
    ) -> dict[str, Any]:
        """Return one encounter participant or reject cross-boundary settlement."""
        combatant = next(
            (
                item
                for item in encounter.get("combatants", [])
                if str(item.get("actor_id") or "") == actor_id
            ),
            None,
        )
        if combatant is None:
            raise CombatEngineError(f"{role} is not a combatant in the active encounter")
        return combatant

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

    def readable_scene_scope(campaign_id: str, scope_id: str, principal_id: str) -> str:
        """Prevent one player from reading another split-party progress ledger."""
        if is_dm(campaign_id, principal_id) or scope_id == "party":
            return scope_id
        if scope_id.startswith("player:"):
            actor_id_value = scope_id.split(":", 1)[1]
            try:
                access.require_actor(campaign_id, actor_id_value, principal_id, private=True)
            except PermissionError as error:
                raise PermissionError(
                    "players may read only party or an owned player scene scope"
                ) from error
            return scope_id
        raise PermissionError("players may read only party or an owned player scene scope")

    def require_import_job(campaign_id: str, job_id: str, kind: str | None = None) -> Any:
        job = import_jobs.get(job_id)
        if job.campaign_id != campaign_id:
            raise LookupError(job_id)
        if kind is not None and job.kind != kind:
            raise ValueError(f"import job is not a {kind} job")
        return job

    def require_write_contract(expected_revision: int | None, idempotency_key: str | None) -> None:
        if expected_revision is None:
            raise ValueError("expected_revision is required for this mutation")
        if not idempotency_key:
            raise ValueError("idempotency_key is required for this mutation")

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

    def require_combat_spell_turn_legal(
        encounter: dict[str, Any],
        *,
        actor_id: str,
        payment: str,
        spell_level: int,
        casting_time: str,
        spent_slot: bool,
    ) -> list[dict[str, Any]]:
        """Enforce the edition's per-turn spell limit before any resource is spent."""
        turn_casts = list(dict(encounter.get("turn_spell_casts") or {}).get(actor_id, []))
        ruleset = str(encounter.get("ruleset") or "2014")
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
                        "casting_time": casting_time,
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
        return turn_casts

    def record_combat_spell_cast(
        encounter: dict[str, Any],
        *,
        actor_id: str,
        spell_id: str,
        spell_level: int,
        payment: str,
        casting_time: str,
        spent_slot: bool,
        **extra: Any,
    ) -> None:
        casts_by_actor = dict(encounter.get("turn_spell_casts") or {})
        casts_by_actor[actor_id] = [
            *list(casts_by_actor.get(actor_id, [])),
            {
                "spell_id": spell_id,
                "spell_level": spell_level,
                "payment": payment,
                "casting_time": casting_time,
                "spent_slot": spent_slot,
                **extra,
            },
        ]
        encounter["turn_spell_casts"] = casts_by_actor

    def post_hit_attack_defenses(
        campaign_id: str,
        target: dict[str, Any],
        *,
        plan: dict[str, Any],
        attack: dict[str, Any],
        encounter: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Combine card activities with legal source-bound spell reactions."""
        spell_options: list[dict[str, Any]] = []
        for discovered in available_shield_attack_defenses(target["sheet"]):
            spell_id = str(discovered.get("spell_id") or discovered.get("id") or "")
            candidate = next(
                (
                    item
                    for item in available_shield_attack_defenses(
                        target["sheet"],
                        rules=effective_rule_context(
                            campaign_id,
                            facts={
                                "actor_id": str(plan["target_id"]),
                                "spell_id": spell_id,
                                "kind": "spell",
                            },
                        ),
                    )
                    if str(item.get("id") or "") == str(discovered.get("id") or "")
                ),
                None,
            )
            if candidate is None:
                continue
            legal_casts = []
            for option in candidate.get("cast_options", []):
                payment = dict(option.get("payment") or {})
                try:
                    require_combat_spell_turn_legal(
                        encounter,
                        actor_id=str(plan["target_id"]),
                        payment="reaction",
                        spell_level=1,
                        casting_time="reaction",
                        spent_slot=payment.get("economy") in {"slots", "pact_magic"},
                    )
                except CombatEngineError:
                    continue
                legal_casts.append(deepcopy(option))
            if legal_casts:
                spell_options.append(
                    {
                        **candidate,
                        "cast_levels": [int(item["cast_level"]) for item in legal_casts],
                        "cast_options": legal_casts,
                    }
                )
        return available_attack_defenses(
            target,
            plan=plan,
            attack=attack,
            encounter=encounter,
            extra_defenses=spell_options,
        )

    def magic_missile_shield_defenses(
        campaign_id: str,
        target_id: str,
        encounter: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Return Shield casts legal for this exact targeting reaction."""
        combatant = next(
            (
                item
                for item in encounter.get("combatants", [])
                if str(item.get("actor_id") or "") == target_id
            ),
            None,
        )
        if combatant is None:
            raise CombatEngineError(f"combatant not found: {target_id}")
        budget = dict(combatant.get("turn_budget") or {})
        blocked = {
            "dead",
            "unconscious",
            "stunned",
            "incapacitated",
            "paralyzed",
            "petrified",
        }
        if int(budget.get("reaction", 0) or 0) <= 0 or blocked & {
            str(item).casefold() for item in combatant.get("conditions", [])
        }:
            return []
        target = combat_actor_snapshot(target_id)
        result: list[dict[str, Any]] = []
        for candidate in available_shield_magic_missile_defenses(
            target["sheet"],
            rules=effective_rule_context(
                campaign_id,
                facts={
                    "actor_id": target_id,
                    "spell_id": "",
                    "kind": "spell_magic_missile_immunity",
                },
            ),
        ):
            legal_casts: list[dict[str, Any]] = []
            for option in candidate.get("cast_options", []):
                payment = dict(option.get("payment") or {})
                try:
                    require_combat_spell_turn_legal(
                        encounter,
                        actor_id=target_id,
                        payment="reaction",
                        spell_level=1,
                        casting_time="reaction",
                        spent_slot=payment.get("economy") in {"slots", "pact_magic"},
                    )
                except CombatEngineError:
                    continue
                legal_casts.append(deepcopy(option))
            if legal_casts:
                result.append(
                    {
                        **candidate,
                        "cast_levels": [int(item["cast_level"]) for item in legal_casts],
                        "cast_options": legal_casts,
                    }
                )
        return result

    def validate_magic_missile_targets(
        encounter: dict[str, Any],
        *,
        caster_id: str,
        allocations: list[dict[str, Any]],
        cast_level: int,
    ) -> list[dict[str, Any]]:
        """Validate source-rule targeting against current map and visibility facts."""
        normalized = validate_magic_missile_allocations(allocations, cast_level=cast_level)
        combatants = {
            str(item.get("actor_id") or ""): item for item in encounter.get("combatants", [])
        }
        caster = combatants.get(caster_id)
        if caster is None:
            raise CombatEngineError("Magic Missile caster is not in this encounter")

        def coordinates(position: Any) -> tuple[float, float] | None:
            if isinstance(position, dict) and "x" in position and "y" in position:
                return float(position["x"]), float(position["y"])
            if isinstance(position, (list, tuple)) and len(position) == 2:
                return float(position[0]), float(position[1])
            return None

        caster_position = coordinates(caster.get("position"))
        if caster_position is None:
            raise CombatEngineError("Magic Missile range requires the caster's map position")
        for allocation in normalized:
            target_id = str(allocation["target_id"])
            target = combatants.get(target_id)
            if target is None:
                raise CombatEngineError(
                    f"Magic Missile target is not in this encounter: {target_id}"
                )
            conditions = {str(item).casefold() for item in target.get("conditions", [])}
            if "dead" in conditions:
                raise CombatEngineError("Magic Missile cannot target a dead creature")
            target_position = coordinates(target.get("position"))
            if target_position is None:
                raise CombatEngineError("Magic Missile range requires every target's map position")
            distance = int(
                max(
                    abs(float(caster_position[0]) - float(target_position[0])),
                    abs(float(caster_position[1]) - float(target_position[1])),
                )
                * 5
            )
            if distance > 120:
                raise CombatEngineError("Magic Missile target is outside its 120-foot range")
            concealed = bool(target.get("hidden", False)) or "invisible" in conditions
            visible_to = {str(item) for item in target.get("visible_to_actor_ids") or []}
            if concealed and caster_id not in visible_to:
                raise CombatEngineError("Magic Missile requires a target the caster can see")
            allocation["distance_ft"] = distance
        return normalized

    def settle_magic_missile_damage(
        campaign_id: str,
        encounter: dict[str, Any],
        resolution: dict[str, Any],
        *,
        next_revision: int,
        sheet_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
        """Roll and apply every dart separately after all target reactions settle."""
        value = deepcopy(encounter)
        sheets = {str(key): deepcopy(item) for key, item in (sheet_overrides or {}).items()}
        shielded = {str(item) for item in resolution.get("shielded_target_ids", [])}
        target_results: list[dict[str, Any]] = []
        concentration_windows: list[dict[str, Any]] = []
        resolution_id = str(resolution["id"])
        spell_id = str(resolution["spell_id"])
        for allocation in resolution.get("allocations", []):
            target_id = str(allocation["target_id"])
            if target_id in shielded:
                target_results.append(
                    {
                        "target_id": target_id,
                        "darts": int(allocation["darts"]),
                        "shielded": True,
                        "dart_results": [],
                    }
                )
                continue
            sheet = sheets.get(target_id)
            if sheet is None:
                sheet = deepcopy(characters.get(target_id).sheet)
            combatant = next(
                item
                for item in value.get("combatants", [])
                if str(item.get("actor_id") or "") == target_id
            )
            dart_results: list[dict[str, Any]] = []
            for dart_index in range(int(allocation["darts"])):
                dice = asdict(roll("1d4+1"))
                applied = apply_damage_to_sheet(
                    sheet,
                    amount=int(dice["total"]),
                    damage_type="force",
                    source=spell_id,
                    ruleset=str(value.get("ruleset") or "2014"),
                    death_saves=bool(combatant.get("death_saves", False)),
                )
                sheet = applied["sheet"]
                concentration = applied.get("concentration")
                if concentration:
                    concentration_windows.append(
                        {
                            **deepcopy(concentration),
                            "id": (
                                f"concentration:{target_id}:{next_revision}:"
                                f"{resolution_id}:{dart_index}"
                            ),
                            "kind": "concentration",
                            "actor_id": target_id,
                            "source_resolution_id": resolution_id,
                            "dart_index": dart_index,
                        }
                    )
                dart_results.append(
                    {
                        "dart_index": dart_index,
                        "roll": dice,
                        **{key: item for key, item in applied.items() if key != "sheet"},
                    }
                )
            sheets[target_id] = sheet
            sync_combatant_conditions(value, target_id, sheet)
            reconcile_readied_spells(value, target_id, sheet)
            target_results.append(
                {
                    "target_id": target_id,
                    "darts": int(allocation["darts"]),
                    "shielded": False,
                    "dart_results": dart_results,
                }
            )
        value["pending"] = [
            item
            for item in value.get("pending", [])
            if str(item.get("spell_resolution_id") or "") != resolution_id
        ]
        value["pending"] = [*list(value.get("pending") or []), *concentration_windows]
        resolutions = dict(value.get("spell_resolutions") or {})
        resolutions.pop(resolution_id, None)
        if resolutions:
            value["spell_resolutions"] = resolutions
        else:
            value.pop("spell_resolutions", None)
        result = {
            "kind": "magic_missile",
            "spell_id": spell_id,
            "caster_id": str(resolution["caster_id"]),
            "cast_level": int(resolution["cast_level"]),
            "dart_count": sum(int(item["darts"]) for item in resolution["allocations"]),
            "targets": target_results,
            "concentration_windows": len(concentration_windows),
        }
        value["log"] = [
            *list(value.get("log") or []),
            {"type": "magic_missile", "result": deepcopy(result)},
        ][-100:]
        return value, sheets, result

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
        rule_receipts: list[dict[str, Any]] | None = None,
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
            rule_receipts=rule_receipts,
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
            "contract_version": "2026-07-session-exposure-v2",
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
                "module_revision_safe_snapshots": True,
                "scene_spatial_evidence": True,
                "module_page_visual_evidence": True,
                "snapshot_managed_spatial_review": True,
                "reviewed_image_statblock_import": True,
                "temporary_combat_maps": True,
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
                "structured_rulebook_import": True,
                "source_bound_rule_packs": True,
                "structured_content_catalog": True,
                "structured_content_selection_requirements": True,
                "module_import_idempotency": True,
                "managed_module_document_staging": True,
                "core_pdf_module_normalization": True,
                "player_safe_scene_scopes": True,
                "player_safe_combat_maps": True,
                "rule_aware_noncombat_checks": True,
                "compact_domain_facades": True,
                "legacy_tool_aliases": False,
                "session_scoped_tool_exposure": True,
                "native_tools_list_filtering": True,
                "exposure_call_fallback": True,
                "campaign_bound_exposure": True,
                "fallback_principal_binding": True,
                "exposure_expiry": True,
            },
            "rulebook_import": {
                "stages": [
                    "rule_import(stage)",
                    "rule_import(inspect)",
                    "rule_import(ingest)",
                    "rule_search",
                    "rule_expand",
                    "rule_pack_compile(from_source)",
                    "rule_pack_query(test)",
                    "rule_pack_change(install)",
                    "campaign_rules(set_pack)",
                ],
                "source_citation_fields": [
                    "source_id",
                    "source_key",
                    "source_checksum",
                    "chunk_id",
                    "heading_path",
                    "page_start",
                    "page_end",
                ],
                "settlement_tools": {
                    "play": "character_check",
                    "combat": "combat_check",
                },
            },
            "module_import": {
                "stages": [
                    "module_import(stage)",
                    "module_import(inspect)",
                    "module_import(validate)",
                    "module_import(ingest)",
                    "module_import(activate)",
                    "module_query(assets)",
                    "module_page_render",
                    "module_content_review",
                    "module_set_progress(spatial_review)",
                ],
                "stage_inputs": ["source_path", "name+content"],
                "managed_types": ["pdf", "markdown", "text"],
                "normalizer": "sagasmith-core",
            },
            "write_requirements": ["principal_id", "expected_revision", "idempotency_key"],
            "tool_exposure": {
                "owner": "sagasmith-dnd-mcp",
                "phases": [PROFILE_LOBBY, PROFILE_PLAY, PROFILE_COMBAT],
                "core_tools": sorted(CORE_TOOLS),
                "groups": group_catalog(),
                "native_flow": ["exposure_open", "exposure_load", "tools/list", "tools/call"],
                "fallback_flow": ["exposure_open", "exposure_load", "exposure_call"],
                "expiry": "sliding 12 hours",
            },
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
        # Reject before persistence so an unsupported edition cannot leave a
        # partially initialized campaign without its required Core lock.
        get_core_rule_pack(edition)
        created = campaigns.create_owned(
            system_id="dnd5e",
            name=name,
            principal_id=principal_id,
            idempotency_key=idempotency_key,
            description=description,
            settings={"edition": edition, "locale": locale},
        )
        if rule_profiles.get(created.id) is None:
            rule_profiles.set(
                created.id,
                edition=edition,
                locale=locale,
                options=profile_options_with_core_lock(edition),
            )
        return asdict(campaigns.get(created.id))

    @mcp.tool()
    def campaign_list(
        status: str | None = None,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        """List D&D 5e campaigns."""
        allowed = access.accessible_campaign_ids(principal_id)
        return [
            campaign_audience_view(item.id, principal_id)
            for item in campaigns.list(system_id="dnd5e", status=status)
            if item.id in allowed
        ]

    def campaign_audience_view(campaign_id: str, principal_id: str) -> dict[str, Any]:
        """Project campaign state through the same audience boundary as domain reads."""
        membership = access.require_campaign(campaign_id, principal_id)
        campaign = campaigns.get(campaign_id)
        value = asdict(campaign)
        if membership.role in {"owner", "dm"}:
            return value
        state = dict(value.get("state") or {})
        safe_state: dict[str, Any] = {
            "game_phase": str(state.get("game_phase") or PROFILE_LOBBY),
            "party": deepcopy(dict(state.get("party") or {})),
            "world_time": deepcopy(dict(state.get("world_time") or {})),
            "world_effects": [
                deepcopy(effect)
                for effect in state.get("world_effects", [])
                if str(effect.get("visibility") or "party") in {"public", "party"}
            ],
        }
        combat = combat_view(campaign_id, principal_id)
        if combat is not None:
            safe_state["combat"] = combat
        value["state"] = safe_state
        value["state_redacted"] = True
        return value

    @mcp.tool()
    def campaign_get(campaign_id: str, principal_id: str = "system:local") -> dict[str, Any]:
        """Read one campaign, including its persisted party and combat state."""
        return campaign_audience_view(campaign_id, principal_id)

    @mcp.tool()
    def server_tool_profiles() -> dict[str, Any]:
        """List phase profiles and session-loadable capability groups."""
        return {
            "profiles": profile_catalog(),
            "groups": group_catalog(),
            "core_tools": sorted(CORE_TOOLS),
        }

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
            profile = str(state.get("game_phase") or PROFILE_LOBBY)
            if profile not in {PROFILE_LOBBY, PROFILE_PLAY}:
                profile = PROFILE_LOBBY
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
        """Switch between game-outside lobby and live non-combat play."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        profile = str(tool_profile).strip().lower()
        if profile not in {PROFILE_LOBBY, PROFILE_PLAY}:
            raise ValueError("tool_profile must be lobby or play; combat starts via combat_start")
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
    def import_job_get(
        campaign_id: str,
        job_id: str,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Read the durable evidence, review state, and result for one lobby import."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        return asdict(require_import_job(campaign_id, job_id))

    @mcp.tool()
    def import_job_list(
        campaign_id: str,
        kind: str | None = None,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        """List rulebook or module imports, newest first, without reading local files."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        return [asdict(item) for item in import_jobs.list(campaign_id, kind=kind)]

    @mcp.tool()
    def rule_import_job_create(
        campaign_id: str,
        artifact: str,
        source_key: str,
        title: str,
        edition: str,
        locale: str = "en",
        publication_id: str = "",
        version: str = "",
        authority: str = "supplement",
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a reviewable rulebook import job for an already staged artifact."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if edition not in {"2014", "2024"}:
            raise ValueError("imported D&D rulebooks require edition 2014 or 2024")
        if not idempotency_key:
            raise ValueError("idempotency_key is required for an import job")
        path = storage.artifact_rulebook_path(artifact)
        inspection = rules.inspect_path(path)
        payload = {
            "artifact": artifact,
            "source_key": source_key,
            "title": title,
            "edition": edition,
            "locale": locale,
            "publication_id": publication_id,
            "version": version,
            "authority": authority,
        }
        scope = f"import-job-create:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        job = import_jobs.create(
            campaign_id=campaign_id,
            kind="rulebook",
            artifact=artifact,
            artifact_checksum=str(inspection.get("checksum") or ""),
            payload=payload,
        )
        response = {"job": asdict(job)}
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
        )

    @mcp.tool()
    def rule_import_job_inspect(
        campaign_id: str,
        job_id: str,
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Normalize a staged rulebook and persist the parser report before indexing it."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if not idempotency_key:
            raise ValueError("idempotency_key is required for import inspection")
        job = require_import_job(campaign_id, job_id, "rulebook")
        payload = {"job_id": job_id, "operation": "inspect"}
        scope = f"import-job:{campaign_id}:{job_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        inspection = rules.inspect_path(storage.artifact_rulebook_path(job.artifact))
        updated = import_jobs.record_inspection(job_id, inspection)
        response = {"job": asdict(updated), "inspection": inspection}
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
        )

    @mcp.tool()
    def rule_import_job_ingest(
        campaign_id: str,
        job_id: str,
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Index an inspected rulebook, retaining its source id for candidate citations."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if not idempotency_key:
            raise ValueError("idempotency_key is required for rulebook indexing")
        job = require_import_job(campaign_id, job_id, "rulebook")
        if job.state not in {"inspected", "failed"}:
            raise ValueError("rule import job must be inspected before indexing")
        payload = {"job_id": job_id, "operation": "ingest"}
        scope = f"import-job:{campaign_id}:{job_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        values = dict(job.payload)
        embedder, vectors = storage.dense_components()
        result = rules.ingest_path(
            system_id="dnd5e",
            path=storage.artifact_rulebook_path(job.artifact),
            source_key=str(values["source_key"]),
            title=str(values["title"]),
            locale=str(values.get("locale") or "en"),
            edition=str(values["edition"]),
            publication_id=str(values.get("publication_id") or ""),
            version=str(values.get("version") or ""),
            authority=str(values.get("authority") or "supplement"),
            embedder=embedder,
            vector_store=vectors,
        )
        source = rules.source(result.source_id)
        updated = import_jobs.record_result(
            job_id,
            {"ingest": asdict(result), "source": source},
            state="extracted",
            source_id=result.source_id,
        )
        response = {"job": asdict(updated), "source": source, **asdict(result)}
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
        )

    @mcp.tool()
    def rule_content_candidates_extract(
        campaign_id: str,
        job_id: str,
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Extract conservative source-linked D&D content candidates for DM review."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if not idempotency_key:
            raise ValueError("idempotency_key is required for candidate extraction")
        job = require_import_job(campaign_id, job_id, "rulebook")
        if not job.source_id:
            raise ValueError("rule import job must be indexed before candidate extraction")
        payload = {"job_id": job_id, "operation": "extract_candidates"}
        scope = f"import-job:{campaign_id}:{job_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        candidates = extract_content_candidates(rules.source_chunks(job.source_id))
        for candidate in candidates:
            candidate["source_citations"] = [
                rules.citation(chunk_id, source_id=job.source_id)
                for chunk_id in candidate["source_chunk_ids"]
            ]
        updated = import_jobs.set_candidates(job_id, candidates)
        response = {"job": asdict(updated), "candidates": updated.candidates}
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
        )

    @mcp.tool()
    def import_job_review_candidates(
        campaign_id: str,
        job_id: str,
        decisions: list[dict[str, Any]],
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Accept, reject, or complete extracted content cards before pack compilation."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if not idempotency_key:
            raise ValueError("idempotency_key is required for candidate review")
        require_import_job(campaign_id, job_id)
        payload = {"job_id": job_id, "operation": "review", "decisions": decisions}
        scope = f"import-job:{campaign_id}:{job_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        updated = import_jobs.review_candidates(job_id, decisions)
        response = {"job": asdict(updated), "candidates": updated.candidates}
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
        participant_manifest: dict[str, Any] | None = None,
        name: str = "Combat",
        scene_id: str | None = None,
        scope_id: str = "party",
        battle_map: dict[str, Any] | None = None,
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
            "participant_manifest": participant_manifest,
            "name": name,
            "scene_id": scene_id,
            "scope_id": scope_id,
            "battle_map": battle_map,
            "ruleset": ruleset,
            "branch_id": resolved_branch_id,
        }
        scope = f"combat-start:{campaign_id}:{resolved_branch_id}:{principal_id}"
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
        current_scene_context = modules.current_scene(campaign_id, scope_id=scope_id)
        scene_context = None
        resolved_scene_id = scene_id
        if resolved_scene_id is None:
            scene_context = current_scene_context
            if scene_context is not None:
                resolved_scene_id = str(scene_context["scene_id"])
        if resolved_scene_id is not None and scene_context is None:
            scene_context = (
                current_scene_context
                if current_scene_context is not None
                and str(current_scene_context["scene_id"]) == resolved_scene_id
                else modules.read_scene(campaign_id, resolved_scene_id)
            )
        readiness = None
        if participant_manifest is not None:
            if resolved_scene_id is None:
                raise ValueError("participant_manifest requires an encounter scene_id")
            readiness = module_scene_readiness(
                campaign_id,
                resolved_scene_id,
                participant_manifest,
                principal_id,
            )
            if not readiness["ready"]:
                unavailable_groups = [
                    item["key"]
                    for item in readiness["groups"]
                    if item["blocking"] and (item["missing_count"] or item["unready_count"])
                ]
                raise CombatEngineError(
                    "scene participant manifest has missing or unusable groups: "
                    + ", ".join(unavailable_groups)
                )
            omitted = sorted(set(readiness["initial_actor_ids"]) - set(participant_ids))
            if omitted:
                raise CombatEngineError(
                    "combat participant_ids omit manifest combatants: " + ", ".join(omitted)
                )
            premature = sorted(set(readiness["reinforcement_actor_ids"]) & set(participant_ids))
            if premature:
                raise CombatEngineError(
                    "manifest reinforcements must enter through combat_join: "
                    + ", ".join(premature)
                )
        compiled_map = None
        if scene_context is not None:
            try:
                battle_map_request = deepcopy(battle_map or {})
                progress_context = scene_context
                if current_scene_context is not None and current_scene_context.get(
                    "module_id"
                ) == scene_context.get("module_id"):
                    progress_context = current_scene_context
                progress = dict(progress_context.get("progress") or {})
                progress_location_key = progress.get("current_location_key")
                if progress_location_key and not battle_map_request.get("location_key"):
                    battle_map_request["location_key"] = progress_location_key
                map_scene_context = scene_context
                requested_location_key = battle_map_request.get("location_key")
                scene_location_keys = {
                    str(item.get("key"))
                    for item in dict(scene_context.get("spatial") or {}).get("locations", [])
                    if isinstance(item, dict) and item.get("key")
                }
                if requested_location_key and requested_location_key not in scene_location_keys:
                    progress_state = dict(progress.get("state") or {})
                    location_scene_id = progress_state.get("location_scene_id")
                    spatial_candidates = []
                    if location_scene_id:
                        location_scene = modules.read_scene(campaign_id, str(location_scene_id))
                        if location_scene.get("module_id") != scene_context.get("module_id"):
                            raise BattleMapError(
                                "progress location_scene_id must belong to the encounter module"
                            )
                        spatial_candidates = [location_scene]
                    else:
                        spatial_candidates = [
                            item
                            for item in modules.scene_index(
                                campaign_id, module_id=scene_context.get("module_id")
                            )
                            if requested_location_key
                            in {
                                str(location.get("key"))
                                for location in dict(item.get("spatial") or {}).get("locations", [])
                                if isinstance(location, dict) and location.get("key")
                            }
                        ]
                    if len(spatial_candidates) != 1:
                        raise BattleMapError(
                            "battle-map location_key must identify exactly one spatial "
                            "location in the encounter module"
                        )
                    candidate_keys = {
                        str(location.get("key"))
                        for location in dict(spatial_candidates[0].get("spatial") or {}).get(
                            "locations", []
                        )
                        if isinstance(location, dict) and location.get("key")
                    }
                    if requested_location_key not in candidate_keys:
                        raise BattleMapError(
                            "progress location_scene_id does not contain current_location_key"
                        )
                    map_scene_context = {
                        **spatial_candidates[0],
                        "encounter_scene_id": resolved_scene_id,
                    }
                compiled_map = compile_battle_map(map_scene_context, battle_map_request)
                for entry in config_by_actor.values():
                    validate_position(compiled_map, entry.get("position"))
            except BattleMapError as error:
                raise NeedsRulingError(str(error), missing=("battle_map",)) from error
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
            scene_id=resolved_scene_id,
            name=name,
            battle_map=compiled_map,
        )
        if readiness is not None:
            encounter["participant_manifest"] = readiness
        initiatives = [
            int(item.get("initiative", 0) or 0) for item in encounter.get("combatants", [])
        ]
        start_boundary_ids = (
            ["dnd5e.core.initiative.tie"] if len(initiatives) != len(set(initiatives)) else []
        )
        start_receipts = core_receipts(
            effective_rule_context(campaign_id),
            start_boundary_ids,
            "combat.start",
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
            rule_receipts=start_receipts,
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
    def combat_join(
        campaign_id: str,
        actor_id: str,
        participant_config: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        branch_id: str | None = None,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Queue one canonical campaign actor to enter combat at the next round."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        config_value = dict(participant_config or {})
        allowed = {
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
        unknown = set(config_value) - allowed
        if unknown:
            raise ValueError(f"unsupported participant config fields: {sorted(unknown)}")
        payload = {
            "actor_id": actor_id,
            "participant_config": config_value,
            "branch_id": resolved_branch_id,
        }
        scope = f"combat-join:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        campaign, encounter = active_encounter(campaign_id)
        require_no_blocking_pending(encounter)
        if campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        require_campaign_actor(campaign_id, actor_id)
        visible_to = config_value.get("visible_to_actor_ids")
        encounter_actor_ids = {
            str(item.get("actor_id") or "") for item in encounter.get("combatants", [])
        } | {actor_id}
        if visible_to is not None and (
            not isinstance(visible_to, list)
            or any(str(item) not in encounter_actor_ids for item in visible_to)
        ):
            raise ValueError(
                "visible_to_actor_ids must contain only current or joining participant IDs"
            )
        battle_map = encounter.get("battle_map")
        if isinstance(battle_map, dict):
            try:
                validate_position(battle_map, config_value.get("position"))
            except BattleMapError as error:
                raise NeedsRulingError(str(error), missing=("position",)) from error
        actor = combat_actor_snapshot(actor_id)
        actor.update(config_value)
        next_encounter = queue_combatant(encounter, actor)
        queued = next(
            item
            for item in next_encounter.get("reinforcements", [])
            if item.get("actor_id") == actor_id
        )
        tied = any(
            item.get("actor_id") != actor_id
            and int(item.get("initiative", 0) or 0) == int(queued.get("initiative", 0) or 0)
            for item in [
                *list(next_encounter.get("combatants") or []),
                *list(next_encounter.get("reinforcements") or []),
            ]
        )
        receipts = core_receipts(
            effective_rule_context(campaign_id),
            ["dnd5e.core.initiative.tie"] if tied else [],
            "combat.join",
        )
        next_state = {**dict(campaign.state or {}), "combat": next_encounter}
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            expected_campaign_revision=campaign.revision,
            operation="combat.participant.join",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
            rule_receipts=receipts,
        )
        response = {
            "status": "committed",
            "queued": deepcopy(queued),
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
        actions = available_actions(encounter, actor_id)
        actor = combat_actor_snapshot(actor_id)
        hit_points = int(dict(actor.get("derived", {}).get("hit_points") or {}).get("value", 0))
        conditions = {
            str(item).casefold()
            for item in actor.get("sheet", {}).get("conditions", [])
        }
        current = current_combatant(encounter)
        death_save_used = bool(
            dict(combatant.get("turn_flags") or {}).get("death_save_used")
        )
        if (
            current is not None
            and current.get("actor_id") == actor_id
            and bool(combatant.get("death_saves", False))
            and hit_points == 0
        ):
            actions = (
                ["death_save"]
                if not conditions & {"dead", "stable"} and not death_save_used
                else []
            )
        return {
            "actor_id": actor_id,
            "actions": actions,
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
                rules=effective_rule_context(
                    campaign_id,
                    facts={"actor_id": actor_id, "target_id": target_id, "kind": "attack"},
                ),
            )
            pay_attack_action(
                encounter,
                combat_actor_snapshot(actor_id),
                weapon_id=str(plan.get("weapon_id") or ""),
                attack_mode=str(plan.get("attack_mode") or "melee"),
                multiattack_option_id=action.get("multiattack_option_id"),
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
            "branch_id": resolved_branch_id,
        }
        scope = f"combat-attack:{campaign_id}:{resolved_branch_id}:{principal_id}"
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
        rule_context = effective_rule_context(
            campaign_id,
            facts={"actor_id": actor_id, "target_id": target_id, "kind": "attack"},
        )
        try:
            plan = preflight_attack(
                attacker,
                target,
                action=action_payload,
                encounter=encounter,
                rules=rule_context,
            )
        except NeedsRulingError:
            if access.require_campaign(campaign_id, principal_id).role not in {"owner", "dm"}:
                raise CombatEngineError("attack requires a DM ruling") from None
            raise
        next_encounter, attack_payment = pay_attack_action(
            encounter,
            attacker,
            weapon_id=str(plan.get("weapon_id") or ""),
            attack_mode=str(plan.get("attack_mode") or "melee"),
            multiattack_option_id=action_payload.get("multiattack_option_id"),
        )
        attack_payment_receipts = core_receipts(
            rule_context,
            ["dnd5e.core.action.multiattack_choice"],
            "combat.attack.payment",
        )
        attack_roll = roll_attack_action(plan=plan)
        defenses = post_hit_attack_defenses(
            campaign_id,
            target,
            plan=plan,
            attack=attack_roll,
            encounter=next_encounter,
        )
        updated_attacker = deepcopy(attacker)
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
        if defenses:
            result = {
                **attack_roll,
                "attack_payment": attack_payment,
                "pending_reaction": True,
            }
            if ammunition is not None:
                result["ammunition"] = ammunition
            current = next(
                item for item in next_encounter["combatants"] if item.get("actor_id") == actor_id
            )
            if plan.get("attacker_was_hidden"):
                current["hidden"] = False
                result["reveals_attacker"] = True
            if plan.get("helped_by"):
                helper = next(
                    (
                        item
                        for item in next_encounter["combatants"]
                        if item.get("actor_id") == plan["helped_by"]
                    ),
                    None,
                )
                if helper is not None:
                    helper_flags = dict(helper.get("turn_flags") or {})
                    helper_flags.pop("helping", None)
                    helper["turn_flags"] = helper_flags
            next_encounter = add_choice_window(
                next_encounter,
                kind="reaction",
                actor_id_value=target_id,
                event="attack.hit.before_damage",
                candidates=[*defenses, {"id": "decline", "name": "Decline"}],
            )
            window = next_encounter["pending"][-1]
            window.update(
                trigger="attack_hit_defense",
                attacker_id=actor_id,
                target_id=target_id,
                plan=deepcopy(plan),
                attack=deepcopy(attack_roll),
                attack_payment=deepcopy(attack_payment),
                ammunition=deepcopy(ammunition),
            )
            next_encounter["log"] = [
                *list(next_encounter.get("log") or []),
                {
                    "type": "attack_roll",
                    "result": result,
                    "pending_choice_id": window["id"],
                },
            ][-100:]
            next_state = {**dict(campaign.state or {}), "combat": next_encounter}
            updates = []
            if ammunition is not None:
                updates.append(
                    CharacterStateUpdate(
                        character_id=actor_id,
                        sheet=validate_character_sheet(updated_attacker["sheet"]),
                        notes=validate_character_notes(characters.get(actor_id).notes),
                        expected_revision=characters.get(actor_id).revision,
                    )
                )
            revisions_result = StateMutationService(storage.database).replace(
                campaign_id,
                campaign_state=validate_party_state(next_state),
                character_updates=updates,
                expected_campaign_revision=campaign.revision,
                operation="combat.attack.roll",
                actor=principal_id,
                branch_id=resolved_branch_id,
                idempotency_key=idempotency_key,
                rule_receipts=core_receipts(
                    rule_context,
                    ["dnd5e.core.reaction.post_hit_defense"],
                    "attack.hit.before_damage",
                )
                + attack_payment_receipts,
            )
            response = {
                "status": "pending_reaction",
                "result": result,
                "choice": window,
                "combat": next_encounter,
                "campaign_revision": mutation_revision(campaign_id),
                "revisions": [asdict(item) for item in revisions_result or []],
            }
            return combat_response(
                campaign_id,
                principal_id,
                remember_idempotent(
                    scope,
                    idempotency_key,
                    payload,
                    response,
                    campaign_id=campaign_id,
                ),
            )
        updated_attacker, updated_target, result = resolve_attack_damage(
            updated_attacker,
            target,
            plan=plan,
            attack=attack_roll,
            rules=rule_context,
        )
        if ammunition is not None:
            result["ammunition"] = ammunition
        result["attack_payment"] = attack_payment
        result["rule_receipts"] = [
            *list(result.get("rule_receipts") or []),
            *attack_payment_receipts,
        ]
        current = next(
            item for item in next_encounter["combatants"] if item.get("actor_id") == actor_id
        )
        sneak_attack = dict(result.get("sneak_attack") or {})
        if sneak_attack.get("used"):
            flags = dict(current.get("turn_flags") or {})
            flags["sneak_attack_turn_token"] = sneak_attack["turn_token"]
            current["turn_flags"] = flags
        if result.get("reveals_attacker"):
            current["hidden"] = False
        if plan.get("helped_by"):
            helper = next(
                (
                    item
                    for item in next_encounter["combatants"]
                    if item.get("actor_id") == plan["helped_by"]
                ),
                None,
            )
            if helper is not None:
                helper_flags = dict(helper.get("turn_flags") or {})
                helper_flags.pop("helping", None)
                helper["turn_flags"] = helper_flags
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
            rule_receipts=list(result.get("rule_receipts") or []),
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
        minute_changed = False
        if round_changed:
            elapsed_rounds = int(next_state["combat"].get("rounds_until_minute", 0) or 0) + 1
            minute_changed = elapsed_rounds >= 10
            next_state["combat"]["rounds_until_minute"] = 0 if minute_changed else elapsed_rounds
        combat_updates: list[CharacterStateUpdate] = []
        expired_effects = set(duration["expired"])
        rule_context = effective_rule_context(campaign_id)
        rule_receipts: list[dict[str, Any]] = core_receipts(
            rule_context,
            ["dnd5e.core.mcp.duration_clock"],
            "turn.end.duration_clock",
        )
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
            if minute_changed:
                minutes = advance_effect_durations(sheet, period="minute")
                sheet = minutes["sheet"]
                expired.extend(minutes["expired"])
            extension = apply_rule_event(
                sheet,
                "duration.advance",
                context_with_facts(
                    rule_context,
                    actor_id=target_id,
                    ended_actor_id=actor_id,
                    round_changed=round_changed,
                    minute_changed=minute_changed,
                ),
            )
            sheet = extension.sheet
            rule_receipts.extend(extension.receipts)
            if target_id == actor_id:
                ended = apply_rule_event(
                    sheet,
                    "turn.end",
                    context_with_facts(rule_context, actor_id=target_id),
                )
                sheet = ended.sheet
                rule_receipts.extend(ended.receipts)
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
            rule_receipts=rule_receipts,
        )
        response = {
            "status": "committed",
            "combat": next_state["combat"],
            "effects_expired": sorted(expired_effects),
            "readied_spells_expired": sorted(str(item.get("id")) for item in expired_readied),
            "rule_receipts": rule_receipts,
            "ruleset_fingerprint": rule_context.fingerprint,
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return combat_response(
            campaign_id,
            principal_id,
            remember_idempotent(scope, idempotency_key, payload, response, campaign_id=campaign_id),
        )

    def campaign_world_effect_change(
        campaign_id: str,
        action: str,
        payload: dict[str, Any],
        principal_id: str,
        expected_revision: int | None,
        branch_id: str | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        """Add or dismiss one structured campaign-space effect outside combat."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        if action not in {"effect_add", "effect_remove"}:
            raise ValueError("world effect action must be effect_add or effect_remove")
        request_payload = {
            "action": action,
            "payload": payload,
            "branch_id": resolved_branch_id,
        }
        scope = f"campaign-world-effect:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, request_payload)
        if replay is not None:
            return replay
        campaign = campaigns.get(campaign_id)
        if campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        state = validate_party_state(deepcopy(campaign.state or {}))
        if bool(dict(state.get("combat") or {}).get("active")):
            raise CombatEngineError("world effects cannot be edited during active combat")
        effects = list(state.get("world_effects") or [])
        if action == "effect_add":
            raw_effect = deepcopy(required(payload, "effect"))
            clock = dict(state.get("world_time") or {})
            raw_effect.setdefault(
                "created_at_elapsed_minutes", int(clock.get("elapsed_minutes", 0) or 0)
            )
            effect = validate_world_effect(raw_effect)
            if any(item["id"] == effect["id"] for item in effects):
                raise ValueError("world effect id is already present")
            if effect["duration"]["period"] in {"minute", "hour", "day"} and not clock:
                raise ValueError("set the campaign clock before adding a timed world effect")
            effects.append(effect)
        else:
            effect_id = str(required(payload, "effect_id"))
            effect = next((item for item in effects if item["id"] == effect_id), None)
            if effect is None:
                raise ValueError("world effect is not present")
            if not effect.get("active"):
                raise ValueError("world effect is already inactive")
            effect["active"] = False
            effect["metadata"] = {
                **dict(effect.get("metadata") or {}),
                "ended_by": principal_id,
                "ended_reason": str(payload.get("reason") or "dismissed"),
            }
        state["world_effects"] = effects
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=state,
            expected_campaign_revision=campaign.revision,
            operation=f"campaign.world_effect.{action.removeprefix('effect_')}",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
        )
        response = {
            "status": "committed",
            "effect": effect,
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return remember_idempotent(
            scope,
            idempotency_key,
            request_payload,
            response,
            campaign_id=campaign_id,
        )

    @mcp.tool()
    def campaign_clock_set(
        campaign_id: str,
        day: int,
        hour: int = 0,
        minute: int = 0,
        label: str = "",
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Set the branch-local campaign clock without fabricating elapsed time."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        if isinstance(day, bool) or not isinstance(day, int) or day < 1:
            raise ValueError("day must be a positive integer")
        if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
            raise ValueError("hour must be an integer from 0 to 23")
        if isinstance(minute, bool) or not isinstance(minute, int) or not 0 <= minute <= 59:
            raise ValueError("minute must be an integer from 0 to 59")
        payload = {
            "day": day,
            "hour": hour,
            "minute": minute,
            "label": str(label).strip(),
            "branch_id": resolved_branch_id,
        }
        scope = f"campaign-clock-set:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        campaign = campaigns.get(campaign_id)
        if campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        state = validate_party_state(deepcopy(campaign.state or {}))
        if bool(dict(state.get("combat") or {}).get("active")):
            raise CombatEngineError("campaign clock cannot be set during active combat")
        requested_elapsed = (day - 1) * 1440 + hour * 60 + minute
        existing_clock = dict(state.get("world_time") or {})
        if (
            existing_clock
            and int(existing_clock.get("elapsed_minutes", 0) or 0) != requested_elapsed
        ):
            raise ValueError(
                "campaign clock is already set; use clock_advance so timed effects "
                "stay synchronized"
            )
        world_time = {
            "schema_version": 1,
            "day": day,
            "hour": hour,
            "minute": minute,
            "elapsed_minutes": requested_elapsed,
            "label": str(label).strip(),
        }
        state["world_time"] = world_time
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=state,
            expected_campaign_revision=campaign.revision,
            operation="campaign.clock.set",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
        )
        response = {
            "status": "committed",
            "world_time": world_time,
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
        )

    @mcp.tool()
    def campaign_advance_effects(
        campaign_id: str,
        period: str,
        count: int = 1,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Advance the campaign clock and matching timed effects atomically."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        normalized_period = str(period).strip().lower().replace("-", "_")
        if normalized_period not in {"minute", "hour", "day", "round", "encounter"}:
            raise ValueError("period must be minute, hour, day, round, or encounter")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("count must be a positive integer")
        payload = {
            "period": normalized_period,
            "count": count,
            "branch_id": resolved_branch_id,
        }
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
        next_state = validate_party_state(deepcopy(campaign.state or {}))
        if bool(dict(next_state.get("combat") or {}).get("active")):
            raise CombatEngineError("campaign time cannot advance during active combat")
        time_minutes = {"minute": 1, "hour": 60, "day": 1440}
        world_time: dict[str, Any] | None = None
        if normalized_period in time_minutes:
            current_clock = dict(next_state.get("world_time") or {})
            if not current_clock:
                raise ValueError("set the campaign clock before advancing narrative time")
            elapsed = int(current_clock.get("elapsed_minutes", 0) or 0)
            elapsed += time_minutes[normalized_period] * count
            world_time = {
                "schema_version": 1,
                "day": elapsed // 1440 + 1,
                "hour": (elapsed % 1440) // 60,
                "minute": elapsed % 60,
                "elapsed_minutes": elapsed,
                "label": str(current_clock.get("label") or ""),
            }
            next_state["world_time"] = world_time
        effect_steps = {
            "minute": {"minute": count},
            "hour": {"minute": count * 60, "hour": count},
            "day": {"minute": count * 1440, "hour": count * 24, "day": count},
            "round": {"round": count},
            "encounter": {"encounter": count},
        }[normalized_period]
        world_advanced: list[str] = []
        world_expired: list[str] = []
        for effect_period, amount in effect_steps.items():
            world_result = advance_world_effect_durations(
                next_state, period=effect_period, amount=amount
            )
            next_state = world_result["state"]
            world_advanced.extend(world_result["advanced"])
            world_expired.extend(world_result["expired"])
        world_state_changed = bool(world_advanced or world_expired)
        updates: list[CharacterStateUpdate] = []
        advanced: dict[str, list[str]] = {}
        expired: dict[str, list[str]] = {}
        rule_receipts: list[dict[str, Any]] = []
        rule_context = effective_rule_context(campaign_id)
        for character in characters.list(campaign_id=campaign_id):
            sheet = character.sheet
            character_advanced: list[str] = []
            character_expired: list[str] = []
            for effect_period, amount in effect_steps.items():
                result = advance_effect_durations(sheet, period=effect_period, amount=amount)
                extension = apply_rule_event(
                    result["sheet"],
                    "duration.advance",
                    context_with_facts(
                        rule_context,
                        actor_id=character.id,
                        period=effect_period,
                        amount=amount,
                    ),
                )
                rule_receipts.extend(extension.receipts)
                sheet = extension.sheet
                character_advanced.extend(result["advanced"])
                character_expired.extend(result["expired"])
            if not character_advanced and not character_expired and sheet == character.sheet:
                continue
            updates.append(
                CharacterStateUpdate(
                    character_id=character.id,
                    sheet=validate_character_sheet(sheet),
                    notes=validate_character_notes(character.notes),
                    expected_revision=character.revision,
                )
            )
            advanced[character.id] = list(dict.fromkeys(character_advanced))
            expired[character.id] = list(dict.fromkeys(character_expired))
        revisions_result = None
        if updates or world_time is not None or world_state_changed:
            revisions_result = StateMutationService(storage.database).replace(
                campaign_id,
                campaign_state=(
                    next_state if world_time is not None or world_state_changed else None
                ),
                character_updates=updates,
                expected_campaign_revision=campaign.revision,
                operation="campaign.effects.advance",
                actor=principal_id,
                branch_id=resolved_branch_id,
                idempotency_key=idempotency_key,
                rule_receipts=rule_receipts,
            )
        response = {
            "status": (
                "committed"
                if updates or world_time is not None or world_state_changed
                else "no_change"
            ),
            "period": normalized_period,
            "count": count,
            "world_time": world_time,
            "advanced": advanced,
            "expired": expired,
            "world_advanced": list(dict.fromkeys(world_advanced)),
            "world_expired": list(dict.fromkeys(world_expired)),
            "rule_receipts": rule_receipts,
            "ruleset_fingerprint": rule_context.fingerprint,
            "campaign_revision": mutation_revision(campaign_id),
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
        for combatant_state, snapshot in (
            (
                next(item for item in encounter["combatants"] if item.get("actor_id") == actor_id),
                attacker,
            ),
            (
                next(item for item in encounter["combatants"] if item.get("actor_id") == target_id),
                target,
            ),
        ):
            snapshot["hidden"] = bool(combatant_state.get("hidden", False))
            snapshot["visible_to_actor_ids"] = deepcopy(combatant_state.get("visible_to_actor_ids"))
        if window.get("target_visible"):
            action_payload = dict(action_payload)
            action_payload["context"] = {
                **dict(action_payload.get("context") or {}),
                "attacker_can_see_target": True,
            }
        rule_context = effective_rule_context(
            campaign_id,
            facts={"actor_id": actor_id, "target_id": target_id, "kind": "attack"},
        )
        plan = preflight_attack(
            attacker,
            target,
            action=action_payload,
            encounter=None,
            allow_out_of_turn=True,
            rules=rule_context,
        )
        weapon = next(
            (
                item
                for item in attacker.get("derived", {})
                .get("inventory", {})
                .get("weapon_attacks", [])
                if item.get("item_id") == plan.get("weapon_id")
            ),
            None,
        )
        if weapon is not None and weapon.get("attack_type") != "melee":
            raise CombatEngineError("opportunity attacks require a melee attack")
        attack_roll = roll_attack_action(plan=plan)
        defenses = post_hit_attack_defenses(
            campaign_id,
            target,
            plan=plan,
            attack=attack_roll,
            encounter=encounter,
        )
        updated_attacker = deepcopy(attacker)
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
        if plan.get("attacker_was_hidden"):
            combatant["hidden"] = False
        if plan.get("helped_by"):
            helper = next(
                (
                    item
                    for item in next_encounter["combatants"]
                    if item.get("actor_id") == plan["helped_by"]
                ),
                None,
            )
            if helper is not None:
                helper_flags = dict(helper.get("turn_flags") or {})
                helper_flags.pop("helping", None)
                helper["turn_flags"] = helper_flags
        if defenses:
            attack_payment = {
                "kind": "reaction_attack",
                "payment": "reaction",
                "trigger": "opportunity_attack",
            }
            result = {
                **attack_roll,
                "attack_payment": attack_payment,
                "pending_reaction": True,
            }
            if ammunition is not None:
                result["ammunition"] = ammunition
            if plan.get("attacker_was_hidden"):
                result["reveals_attacker"] = True
            next_encounter = add_choice_window(
                next_encounter,
                kind="reaction",
                actor_id_value=target_id,
                event="attack.hit.before_damage",
                candidates=[*defenses, {"id": "decline", "name": "Decline"}],
            )
            defense_window = next_encounter["pending"][-1]
            defense_window.update(
                trigger="attack_hit_defense",
                attacker_id=actor_id,
                target_id=target_id,
                plan=deepcopy(plan),
                attack=deepcopy(attack_roll),
                attack_payment=attack_payment,
                ammunition=deepcopy(ammunition),
                source_choice_id=choice_id,
            )
            next_encounter["log"] = [
                *list(next_encounter.get("log") or []),
                {
                    "type": "reaction_attack_roll",
                    "choice_id": choice_id,
                    "result": result,
                    "pending_choice_id": defense_window["id"],
                },
            ][-100:]
            next_state = {**dict(campaign.state or {}), "combat": next_encounter}
            updates = []
            if ammunition is not None:
                updates.append(
                    CharacterStateUpdate(
                        character_id=actor_id,
                        sheet=validate_character_sheet(updated_attacker["sheet"]),
                        notes=validate_character_notes(characters.get(actor_id).notes),
                        expected_revision=characters.get(actor_id).revision,
                    )
                )
            revisions_result = StateMutationService(storage.database).replace(
                campaign_id,
                campaign_state=validate_party_state(next_state),
                character_updates=updates,
                expected_campaign_revision=campaign.revision,
                operation="combat.reaction.attack.roll",
                actor=principal_id,
                branch_id=resolved_branch_id,
                idempotency_key=idempotency_key,
                rule_receipts=core_receipts(
                    rule_context,
                    [
                        "dnd5e.core.mcp.opportunity_melee_only",
                        "dnd5e.core.reaction.post_hit_defense",
                    ],
                    "reaction.opportunity_attack.hit",
                ),
            )
            response = {
                "status": "pending_reaction",
                "result": result,
                "choice": defense_window,
                "combat": next_encounter,
                "campaign_revision": mutation_revision(campaign_id),
                "revisions": [asdict(item) for item in revisions_result or []],
            }
            return combat_response(
                campaign_id,
                principal_id,
                remember_idempotent(
                    scope,
                    idempotency_key,
                    payload,
                    response,
                    campaign_id=campaign_id,
                ),
            )
        updated_attacker, updated_target, result = resolve_attack_damage(
            updated_attacker,
            target,
            plan=plan,
            attack=attack_roll,
            rules=rule_context,
        )
        if ammunition is not None:
            result["ammunition"] = ammunition
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
            rule_receipts=[
                *list(result.get("rule_receipts") or []),
                *core_receipts(
                    effective_rule_context(campaign_id),
                    ["dnd5e.core.mcp.opportunity_melee_only"],
                    "reaction.opportunity_attack",
                ),
            ],
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

    def combat_reaction_defense(
        campaign_id: str,
        actor_id: str,
        choice_id: str,
        selection: dict[str, Any],
        principal_id: str = "system:local",
        branch_id: str | None = None,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a post-hit defensive reaction before any damage is rolled."""
        access.require_actor(campaign_id, actor_id, principal_id, control=True)
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        payload = {
            "actor_id": actor_id,
            "choice_id": choice_id,
            "selection": selection,
            "branch_id": resolved_branch_id,
        }
        scope = f"combat-reaction-defense:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        campaign, encounter = active_encounter(campaign_id)
        if campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        window = next(
            (item for item in encounter.get("pending", []) if item.get("id") == choice_id),
            None,
        )
        if (
            not isinstance(window, dict)
            or window.get("kind") != "reaction"
            or window.get("trigger") != "attack_hit_defense"
            or window.get("actor_id") != actor_id
            or window.get("target_id") != actor_id
        ):
            raise CombatEngineError("choice_id is not this actor's attack-defense window")
        selection_id = str(selection.get("id") or "")
        candidate = next(
            (
                item
                for item in window.get("candidates", [])
                if str(item.get("id") or "") == selection_id
            ),
            None,
        )
        if candidate is None:
            raise CombatEngineError("selection is not one of the defensive reaction choices")
        attacker_id = str(window.get("attacker_id") or "")
        require_campaign_actor(campaign_id, attacker_id)
        attacker = combat_actor_snapshot(attacker_id)
        target = combat_actor_snapshot(actor_id)
        plan = deepcopy(dict(window.get("plan") or {}))
        attack = deepcopy(dict(window.get("attack") or {}))
        next_encounter = deepcopy(encounter)
        used = selection_id not in {"decline", "skip", "pass"}
        defense_kind = str(candidate.get("kind") or "")
        spell_result: dict[str, Any] | None = None
        if used:
            next_encounter = pay_activity_activation(
                next_encounter,
                actor_id_value=actor_id,
                activation_type="reaction",
            )
            if defense_kind == "spell_armor_class_bonus":
                cast_level = selection.get("cast_level")
                if isinstance(cast_level, bool) or not isinstance(cast_level, int):
                    raise CombatEngineError("Shield selection requires an integer cast_level")
                cast_option = next(
                    (
                        item
                        for item in candidate.get("cast_options", [])
                        if int(item.get("cast_level", 0) or 0) == cast_level
                    ),
                    None,
                )
                if cast_option is None:
                    raise CombatEngineError("Shield cast_level is not one of the offered choices")
                cast_payment = dict(cast_option.get("payment") or {})
                require_combat_spell_turn_legal(
                    next_encounter,
                    actor_id=actor_id,
                    payment="reaction",
                    spell_level=1,
                    casting_time="reaction",
                    spent_slot=cast_payment.get("economy") in {"slots", "pact_magic"},
                )
                spell_result = consume_shield_reaction(
                    target["sheet"],
                    spell_id=str(candidate.get("spell_id") or selection_id),
                    cast_level=cast_level,
                    rules=effective_rule_context(
                        campaign_id,
                        facts={
                            "actor_id": actor_id,
                            "spell_id": str(candidate.get("spell_id") or selection_id),
                            "cast_level": cast_level,
                        },
                    ),
                )
                if spell_result.get("status") != "committed":
                    raise CombatEngineError("Shield has an unresolved rule choice")
                target["sheet"] = spell_result["sheet"]
                target["derived"] = derive_character_sheet(target["sheet"])
                record_combat_spell_cast(
                    next_encounter,
                    actor_id=actor_id,
                    spell_id=str(candidate.get("spell_id") or selection_id),
                    spell_level=1,
                    payment="reaction",
                    casting_time="reaction",
                    spent_slot=cast_payment.get("economy") in {"slots", "pact_magic"},
                    cast_level=cast_level,
                )
            elif defense_kind != "armor_class_bonus":
                raise CombatEngineError("defensive reaction kind is not executable")
            attack = apply_attack_ac_bonus(
                attack,
                bonus=int(candidate.get("bonus", 0) or 0),
                source_id=selection_id,
            )
        next_encounter = resolve_choice_window(
            next_encounter,
            choice_id=choice_id,
            actor_id_value=actor_id,
            selection={"id": selection_id},
        )
        rule_context = effective_rule_context(
            campaign_id,
            facts={"actor_id": attacker_id, "target_id": actor_id, "kind": "attack"},
        )
        updated_attacker, updated_target, result = resolve_attack_damage(
            attacker,
            target,
            plan=plan,
            attack=attack,
            rules=rule_context,
        )
        result["attack_payment"] = deepcopy(window.get("attack_payment") or {})
        if window.get("ammunition") is not None:
            result["ammunition"] = deepcopy(window["ammunition"])
        result["reaction_defense"] = {
            "used": used,
            "source_type": (
                "spell" if used and defense_kind == "spell_armor_class_bonus" else "activity"
            )
            if used
            else None,
            "activity_id": (selection_id if used and defense_kind == "armor_class_bonus" else None),
            "spell_id": (
                str(candidate.get("spell_id") or selection_id)
                if used and defense_kind == "spell_armor_class_bonus"
                else None
            ),
            "cast_level": spell_result.get("cast_level") if spell_result else None,
            "payment": deepcopy(spell_result.get("payment") or {}) if spell_result else None,
            "effect_id": spell_result.get("effect_id") if spell_result else None,
            "bonus": int(candidate.get("bonus", 0) or 0) if used else 0,
        }
        if spell_result is not None:
            result["rule_receipts"] = [
                *list(result.get("rule_receipts") or []),
                *list(spell_result.get("rule_receipts") or []),
            ]
        attacker_combatant = next(
            item for item in next_encounter["combatants"] if item.get("actor_id") == attacker_id
        )
        sneak_attack = dict(result.get("sneak_attack") or {})
        if sneak_attack.get("used"):
            flags = dict(attacker_combatant.get("turn_flags") or {})
            flags["sneak_attack_turn_token"] = sneak_attack["turn_token"]
            attacker_combatant["turn_flags"] = flags
        if result.get("reveals_attacker"):
            attacker_combatant["hidden"] = False
        sync_combatant_conditions(next_encounter, attacker_id, updated_attacker["sheet"])
        sync_combatant_conditions(next_encounter, actor_id, updated_target["sheet"])
        reconcile_readied_spells(next_encounter, actor_id, updated_target["sheet"])
        damage_result = result.get("damage")
        if isinstance(damage_result, dict):
            add_concentration_window(
                next_encounter,
                actor_id,
                damage_result.get("concentration"),
                next_revision=campaign.revision + 1,
            )
            result["damage"] = {
                key: value for key, value in damage_result.items() if key != "sheet"
            }
        next_encounter["log"] = [
            *list(next_encounter.get("log") or []),
            {
                "type": "attack_defense_resolved",
                "choice_id": choice_id,
                "selection_id": selection_id,
                "result": result,
            },
        ][-100:]
        next_state = {**dict(campaign.state or {}), "combat": next_encounter}
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            character_updates=[
                CharacterStateUpdate(
                    character_id=attacker_id,
                    sheet=validate_character_sheet(updated_attacker["sheet"]),
                    notes=validate_character_notes(characters.get(attacker_id).notes),
                    expected_revision=characters.get(attacker_id).revision,
                ),
                CharacterStateUpdate(
                    character_id=actor_id,
                    sheet=validate_character_sheet(updated_target["sheet"]),
                    notes=validate_character_notes(characters.get(actor_id).notes),
                    expected_revision=characters.get(actor_id).revision,
                ),
            ],
            expected_campaign_revision=campaign.revision,
            operation="combat.reaction.defense",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
            rule_receipts=[
                *list(result.get("rule_receipts") or []),
                *core_receipts(
                    rule_context,
                    [
                        "dnd5e.core.mcp.reaction_defense_atomicity",
                        *(
                            ["dnd5e.core.mcp.shield_attack_reaction_atomicity"]
                            if spell_result is not None
                            else []
                        ),
                    ],
                    "attack.hit.defense.resolve",
                ),
            ],
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
            remember_idempotent(
                scope,
                idempotency_key,
                payload,
                response,
                campaign_id=campaign_id,
            ),
        )

    @mcp.tool()
    def combat_move(
        campaign_id: str,
        actor_id: str,
        distance: int,
        destination: Any = None,
        path: list[Any] | None = None,
        movement_mode: str = "voluntary",
        crawl: bool = False,
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
            "crawl": crawl,
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
        moving_combatant = next(
            item for item in encounter.get("combatants", []) if item.get("actor_id") == actor_id
        )
        moving_conditions = {
            str(item).casefold() for item in moving_combatant.get("conditions", [])
        }
        pending_before = {str(item.get("id")) for item in encounter.get("pending", [])}
        next_encounter = spend_movement(
            encounter,
            actor_id,
            distance,
            destination=destination,
            path=path,
            movement_mode=movement_mode,
            crawl=crawl,
        )
        movement_boundary_ids: list[str] = []
        if "prone" in moving_conditions:
            movement_boundary_ids.append("dnd5e.core.movement.prone_crawl_stand")
        if "grappled" in moving_conditions:
            movement_boundary_ids.append("dnd5e.core.movement.grapple_source")
        if any(
            str(item.get("id")) not in pending_before
            and item.get("kind") == "reaction"
            and item.get("trigger") == "opportunity_attack"
            for item in next_encounter.get("pending", [])
        ):
            movement_boundary_ids.append("dnd5e.core.reaction.opportunity_path")
        movement_receipts = core_receipts(
            effective_rule_context(campaign_id),
            movement_boundary_ids,
            "movement.spend",
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
            rule_receipts=movement_receipts,
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
    def combat_stand(
        campaign_id: str,
        actor_id: str,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Stand from Prone by spending half the actor's speed, without using an action."""
        access.require_actor(campaign_id, actor_id, principal_id, control=True)
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        payload = {"actor_id": actor_id, "branch_id": resolved_branch_id}
        scope = f"combat-stand:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        campaign, encounter = active_encounter(campaign_id)
        if campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        require_no_blocking_pending(encounter)
        next_encounter = stand_up(encounter, actor_id)
        stand_receipts = core_receipts(
            effective_rule_context(campaign_id),
            ["dnd5e.core.movement.prone_crawl_stand"],
            "movement.stand",
        )
        current = characters.get(actor_id)
        combatant = next(
            item for item in next_encounter["combatants"] if item.get("actor_id") == actor_id
        )
        updated_sheet = deepcopy(current.sheet)
        updated_sheet["conditions"] = list(combatant.get("conditions") or [])
        next_state = {**dict(campaign.state or {}), "combat": next_encounter}
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            character_updates=[
                CharacterStateUpdate(
                    character_id=actor_id,
                    sheet=validate_character_sheet(updated_sheet),
                    notes=validate_character_notes(current.notes),
                    expected_revision=current.revision,
                )
            ],
            expected_campaign_revision=campaign.revision,
            operation="combat.stand",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
            rule_receipts=stand_receipts,
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
        normalized_action = str(action).strip().lower().replace("-", "_")
        action_receipts = (
            core_receipts(
                effective_rule_context(campaign_id),
                ["dnd5e.core.ready.action"],
                "action.ready",
            )
            if normalized_action == "ready"
            else []
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
            rule_receipts=action_receipts,
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

    def combat_magic_missile_defense(
        campaign_id: str,
        actor_id: str,
        choice_id: str,
        selection: dict[str, Any],
        principal_id: str = "system:local",
        branch_id: str | None = None,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Resolve one Shield targeting reaction, then settle all darts after the last choice."""
        access.require_actor(campaign_id, actor_id, principal_id, control=True)
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        payload = {
            "actor_id": actor_id,
            "choice_id": choice_id,
            "selection": selection,
            "branch_id": resolved_branch_id,
        }
        scope = f"combat-magic-missile-defense:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        campaign, encounter = active_encounter(campaign_id)
        if campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        window = next(
            (item for item in encounter.get("pending", []) if item.get("id") == choice_id),
            None,
        )
        if (
            not isinstance(window, dict)
            or window.get("kind") != "reaction"
            or window.get("trigger") != "magic_missile_targeted"
            or str(window.get("actor_id") or "") != actor_id
            or str(window.get("target_id") or "") != actor_id
        ):
            raise CombatEngineError("choice_id is not this actor's Magic Missile defense window")
        resolution_id = str(window.get("spell_resolution_id") or "")
        resolution = deepcopy(
            dict(dict(encounter.get("spell_resolutions") or {}).get(resolution_id) or {})
        )
        if resolution.get("kind") != "magic_missile":
            raise CombatEngineError("Magic Missile resolution state is missing")
        selection_id = str(selection.get("id") or "")
        candidate = next(
            (
                item
                for item in window.get("candidates", [])
                if str(item.get("id") or "") == selection_id
            ),
            None,
        )
        if candidate is None:
            raise CombatEngineError("selection is not one of the Magic Missile defenses")
        used = selection_id not in {"decline", "skip", "pass"}
        next_encounter = deepcopy(encounter)
        sheet_override: dict[str, dict[str, Any]] = {}
        spell_result: dict[str, Any] | None = None
        if used:
            if str(candidate.get("kind") or "") != "spell_magic_missile_immunity":
                raise CombatEngineError("Magic Missile defense is not executable")
            cast_level = selection.get("cast_level")
            if isinstance(cast_level, bool) or not isinstance(cast_level, int):
                raise CombatEngineError("Shield selection requires an integer cast_level")
            cast_option = next(
                (
                    item
                    for item in candidate.get("cast_options", [])
                    if int(item.get("cast_level", 0) or 0) == cast_level
                ),
                None,
            )
            if cast_option is None:
                raise CombatEngineError("Shield cast_level is not one of the offered choices")
            payment = dict(cast_option.get("payment") or {})
            require_combat_spell_turn_legal(
                next_encounter,
                actor_id=actor_id,
                payment="reaction",
                spell_level=1,
                casting_time="reaction",
                spent_slot=payment.get("economy") in {"slots", "pact_magic"},
            )
            next_encounter = pay_activity_activation(
                next_encounter,
                actor_id_value=actor_id,
                activation_type="reaction",
            )
            target = characters.get(actor_id)
            spell_result = consume_shield_reaction(
                target.sheet,
                spell_id=str(candidate.get("spell_id") or selection_id),
                cast_level=cast_level,
                trigger="magic_missile",
                rules=effective_rule_context(
                    campaign_id,
                    facts={
                        "actor_id": actor_id,
                        "spell_id": str(candidate.get("spell_id") or selection_id),
                        "cast_level": cast_level,
                        "trigger": "magic_missile_targeted",
                    },
                ),
            )
            if spell_result.get("status") != "committed":
                raise CombatEngineError("Shield has an unresolved rule choice")
            sheet_override[actor_id] = spell_result["sheet"]
            record_combat_spell_cast(
                next_encounter,
                actor_id=actor_id,
                spell_id=str(candidate.get("spell_id") or selection_id),
                spell_level=1,
                payment="reaction",
                casting_time="reaction",
                spent_slot=payment.get("economy") in {"slots", "pact_magic"},
                cast_level=cast_level,
            )
            resolution["shielded_target_ids"] = sorted(
                {*map(str, resolution.get("shielded_target_ids", [])), actor_id}
            )
        next_encounter = resolve_choice_window(
            next_encounter,
            choice_id=choice_id,
            actor_id_value=actor_id,
            selection={"id": selection_id},
        )
        resolutions = dict(next_encounter.get("spell_resolutions") or {})
        resolutions[resolution_id] = resolution
        next_encounter["spell_resolutions"] = resolutions
        remaining = [
            item
            for item in next_encounter.get("pending", [])
            if str(item.get("spell_resolution_id") or "") == resolution_id
            and item.get("status", "pending") == "pending"
        ]
        rule_receipts = [
            *list((spell_result or {}).get("rule_receipts") or []),
            *core_receipts(
                effective_rule_context(campaign_id),
                ["dnd5e.core.mcp.magic_missile_atomicity"],
                "combat.spell.magic_missile.defense",
            ),
        ]
        if remaining:
            if sheet_override:
                sync_combatant_conditions(next_encounter, actor_id, sheet_override[actor_id])
            next_state = {**dict(campaign.state or {}), "combat": next_encounter}
            revisions_result = StateMutationService(storage.database).replace(
                campaign_id,
                campaign_state=validate_party_state(next_state),
                character_updates=[
                    CharacterStateUpdate(
                        character_id=target_id,
                        sheet=validate_character_sheet(sheet),
                        notes=validate_character_notes(characters.get(target_id).notes),
                        expected_revision=characters.get(target_id).revision,
                    )
                    for target_id, sheet in sheet_override.items()
                ],
                expected_campaign_revision=campaign.revision,
                operation="combat.spell.magic_missile.defense",
                actor=principal_id,
                branch_id=resolved_branch_id,
                idempotency_key=idempotency_key,
                rule_receipts=rule_receipts,
            )
            response = {
                "status": "pending_reaction",
                "result": {
                    "kind": "magic_missile",
                    "spell_id": resolution["spell_id"],
                    "reaction_defense": {
                        "used": used,
                        "spell_id": selection_id if used else None,
                        "effect_id": (spell_result or {}).get("effect_id"),
                    },
                },
                "choices": remaining,
                "combat": next_encounter,
                "campaign_revision": mutation_revision(campaign_id),
                "revisions": [asdict(item) for item in revisions_result or []],
            }
        else:
            next_encounter, resolved_sheets, result = settle_magic_missile_damage(
                campaign_id,
                next_encounter,
                resolution,
                next_revision=campaign.revision + 1,
                sheet_overrides=sheet_override,
            )
            next_state = {**dict(campaign.state or {}), "combat": next_encounter}
            revisions_result = StateMutationService(storage.database).replace(
                campaign_id,
                campaign_state=validate_party_state(next_state),
                character_updates=[
                    CharacterStateUpdate(
                        character_id=target_id,
                        sheet=validate_character_sheet(sheet),
                        notes=validate_character_notes(characters.get(target_id).notes),
                        expected_revision=characters.get(target_id).revision,
                    )
                    for target_id, sheet in resolved_sheets.items()
                ],
                expected_campaign_revision=campaign.revision,
                operation="combat.spell.magic_missile.resolve",
                actor=principal_id,
                branch_id=resolved_branch_id,
                idempotency_key=idempotency_key,
                rule_receipts=rule_receipts,
            )
            response = {
                "status": "committed",
                "result": {
                    **result,
                    "reaction_defense": {
                        "used": used,
                        "spell_id": selection_id if used else None,
                        "effect_id": (spell_result or {}).get("effect_id"),
                    },
                },
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
    def combat_reactions(
        campaign_id: str,
        actor_id: str,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        """Read reaction windows an actor may resolve outside its own turn."""
        access.require_actor(campaign_id, actor_id, principal_id, control=True)
        _campaign, encounter = active_encounter(campaign_id)
        windows = available_reactions(encounter, actor_id)
        if is_dm(campaign_id, principal_id):
            return windows
        allowed = {
            "id",
            "kind",
            "actor_id",
            "event",
            "candidates",
            "deadline",
            "status",
            "trigger",
            "attacker_id",
            "target_id",
        }
        return [
            {key: value for key, value in window.items() if key in allowed} for window in windows
        ]

    @mcp.tool()
    def combat_cast_spell(
        campaign_id: str,
        actor_id: str,
        spell_id: str,
        cast_level: int | None = None,
        ritual: bool = False,
        component_ruling: dict[str, Any] | None = None,
        choice_id: str | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
        target_allocations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Pay a combat action and settle source-bound spell workflows atomically."""
        access.require_actor(campaign_id, actor_id, principal_id, control=True)
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        payload = {
            "actor_id": actor_id,
            "spell_id": spell_id,
            "cast_level": cast_level,
            "ritual": ritual,
            "component_ruling": component_ruling or {},
            "choice_id": choice_id,
            "target_allocations": target_allocations,
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
        spell_entry = next(
            item
            for item in current.sheet.get("content", {}).get("spells", [])
            if item.get("id") == spell_id
        )
        magic_missile = is_core_magic_missile_spell(spell_entry)
        if magic_missile and target_allocations is None:
            raise CombatEngineError("Magic Missile requires target_allocations at cast time")
        if not magic_missile and target_allocations is not None:
            raise CombatEngineError(
                "target_allocations are currently executable only for source-bound Magic Missile"
            )
        applied = consume_spell_cast(
            current.sheet,
            spell_id=spell_id,
            cast_level=cast_level,
            ritual=ritual,
            component_ruling=component_ruling,
            rules=effective_rule_context(
                campaign_id,
                facts={"actor_id": actor_id, "spell_id": spell_id, "cast_level": cast_level},
            ),
        )
        if applied.get("status") in {"pending_choice", "pending_ruling"}:
            return {
                "status": applied["status"],
                "result": {key: value for key, value in applied.items() if key != "sheet"},
                "campaign_revision": campaign.revision,
            }
        casting_time = str(spell_entry.get("definition", {}).get("casting_time") or "1 action")
        normalized_casting_time = casting_time.casefold().strip()
        if ritual:
            raise CombatEngineError("ritual casting cannot be completed inside an active encounter")
        if normalized_casting_time.startswith(("bonus action", "1 bonus action")):
            payment = "bonus_action"
        elif normalized_casting_time.startswith(("reaction", "1 reaction")):
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

        normalized_allocations: list[dict[str, Any]] | None = None
        if magic_missile:
            normalized_allocations = validate_magic_missile_targets(
                encounter,
                caster_id=actor_id,
                allocations=list(target_allocations or []),
                cast_level=int(applied.get("cast_level", cast_level or 1) or 1),
            )

        spell_level = int(spell_entry.get("level", 0) or 0)
        spent_slot = applied["payment"].get("economy") in {"slots", "pact_magic"}
        require_combat_spell_turn_legal(
            encounter,
            actor_id=actor_id,
            payment=payment,
            spell_level=spell_level,
            casting_time=normalized_casting_time,
            spent_slot=spent_slot,
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
        record_combat_spell_cast(
            next_encounter,
            actor_id=actor_id,
            spell_id=spell_id,
            spell_level=spell_level,
            payment=payment,
            casting_time=normalized_casting_time,
            spent_slot=spent_slot,
        )
        if magic_missile:
            assert normalized_allocations is not None
            resolution_id = f"spell-resolution-{uuid4().hex}"
            resolution = {
                "id": resolution_id,
                "kind": "magic_missile",
                "caster_id": actor_id,
                "spell_id": spell_id,
                "cast_level": int(applied.get("cast_level", cast_level or 1) or 1),
                "allocations": deepcopy(normalized_allocations),
                "shielded_target_ids": [],
            }
            defense_windows: list[dict[str, Any]] = []
            for allocation in normalized_allocations:
                target_id = str(allocation["target_id"])
                target_sheet = characters.get(target_id).sheet
                if any(
                    effect.get("active") and effect.get("kind") == "spell_shield"
                    for effect in target_sheet.get("effects", [])
                ):
                    resolution["shielded_target_ids"].append(target_id)
                    continue
                candidates = magic_missile_shield_defenses(campaign_id, target_id, next_encounter)
                if not candidates:
                    continue
                next_encounter = add_choice_window(
                    next_encounter,
                    kind="reaction",
                    actor_id_value=target_id,
                    event="spell.magic_missile.targeted",
                    candidates=[*candidates, {"id": "decline", "name": "Decline"}],
                )
                window = next_encounter["pending"][-1]
                window.update(
                    trigger="magic_missile_targeted",
                    caster_id=actor_id,
                    target_id=target_id,
                    spell_id=spell_id,
                    spell_resolution_id=resolution_id,
                    darts=int(allocation["darts"]),
                )
                defense_windows.append(deepcopy(window))
            sync_combatant_conditions(next_encounter, actor_id, applied["sheet"])
            if defense_windows:
                resolutions = dict(next_encounter.get("spell_resolutions") or {})
                resolutions[resolution_id] = resolution
                next_encounter["spell_resolutions"] = resolutions
                next_encounter["log"] = [
                    *list(next_encounter.get("log") or []),
                    {
                        "type": "magic_missile_targeted",
                        "resolution_id": resolution_id,
                        "caster_id": actor_id,
                        "spell_id": spell_id,
                        "allocations": deepcopy(normalized_allocations),
                        "choice_ids": [item["id"] for item in defense_windows],
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
                    operation="combat.spell.magic_missile.target",
                    actor=principal_id,
                    branch_id=resolved_branch_id,
                    idempotency_key=idempotency_key,
                    rule_receipts=[
                        *list(applied.get("rule_receipts") or []),
                        *core_receipts(
                            effective_rule_context(campaign_id),
                            [
                                "dnd5e.core.spell.magic_missile_darts",
                                "dnd5e.core.mcp.magic_missile_atomicity",
                            ],
                            "combat.spell.magic_missile.target",
                        ),
                    ],
                )
                response = {
                    "status": "pending_reaction",
                    "result": {
                        "kind": "magic_missile",
                        "spell_id": spell_id,
                        "cast_level": resolution["cast_level"],
                        "dart_count": sum(item["darts"] for item in normalized_allocations),
                        "allocations": normalized_allocations,
                        "payment": deepcopy(applied.get("payment") or {}),
                    },
                    "choices": defense_windows,
                    "combat": next_encounter,
                    "campaign_revision": mutation_revision(campaign_id),
                    "revisions": [asdict(item) for item in revisions_result or []],
                }
                return combat_response(
                    campaign_id,
                    principal_id,
                    remember_idempotent(scope, idempotency_key, payload, response, campaign_id),
                )
            next_encounter, resolved_sheets, result = settle_magic_missile_damage(
                campaign_id,
                next_encounter,
                resolution,
                next_revision=campaign.revision + 1,
                sheet_overrides={actor_id: applied["sheet"]},
            )
            next_state = {**dict(campaign.state or {}), "combat": next_encounter}
            updates = [
                CharacterStateUpdate(
                    character_id=target_id,
                    sheet=validate_character_sheet(sheet),
                    notes=validate_character_notes(characters.get(target_id).notes),
                    expected_revision=characters.get(target_id).revision,
                )
                for target_id, sheet in resolved_sheets.items()
            ]
            revisions_result = StateMutationService(storage.database).replace(
                campaign_id,
                campaign_state=validate_party_state(next_state),
                character_updates=updates,
                expected_campaign_revision=campaign.revision,
                operation="combat.spell.magic_missile.resolve",
                actor=principal_id,
                branch_id=resolved_branch_id,
                idempotency_key=idempotency_key,
                rule_receipts=[
                    *list(applied.get("rule_receipts") or []),
                    *core_receipts(
                        effective_rule_context(campaign_id),
                        [
                            "dnd5e.core.spell.magic_missile_darts",
                            "dnd5e.core.mcp.magic_missile_atomicity",
                        ],
                        "combat.spell.magic_missile.resolve",
                    ),
                ],
            )
            response = {
                "status": "committed",
                "result": {**result, "payment": deepcopy(applied.get("payment") or {})},
                "combat": next_encounter,
                "campaign_revision": mutation_revision(campaign_id),
                "revisions": [asdict(item) for item in revisions_result or []],
            }
            return combat_response(
                campaign_id,
                principal_id,
                remember_idempotent(scope, idempotency_key, payload, response, campaign_id),
            )
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
            rule_receipts=[
                *list(applied.get("rule_receipts") or []),
                *core_receipts(
                    effective_rule_context(campaign_id),
                    ["dnd5e.core.mcp.combat_spell_boundary"],
                    "combat.spell.cast",
                ),
            ],
        )
        response = {
            "status": "pending_ruling",
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
        spent_slot = applied["payment"].get("economy") in {"slots", "pact_magic"}
        require_combat_spell_turn_legal(
            encounter,
            actor_id=actor_id,
            payment="main_action",
            spell_level=spell_level,
            casting_time=applied["casting_time"],
            spent_slot=spent_slot,
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
        record_combat_spell_cast(
            next_encounter,
            actor_id=actor_id,
            spell_id=spell_id,
            spell_level=spell_level,
            payment="main_action",
            casting_time=applied["casting_time"],
            spent_slot=spent_slot,
            readied=True,
        )
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
            rule_receipts=list(applied.get("rule_receipts") or []),
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
    def combat_readied_action_trigger(
        campaign_id: str,
        readied_id: str,
        event: str,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """DM-confirm a generic Ready trigger and open its owning actor's reaction window."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        payload = {"readied_id": readied_id, "event": event, "branch_id": resolved_branch_id}
        scope = f"combat-ready-action-trigger:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        campaign, encounter = active_encounter(campaign_id)
        if campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        next_encounter = trigger_readied_action(encounter, readied_id=readied_id, event=event)
        next_state = {**dict(campaign.state or {}), "combat": next_encounter}
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            expected_campaign_revision=campaign.revision,
            operation="combat.ready.action.trigger",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
        )
        response = {
            "status": "triggered",
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
    def combat_readied_action_resolve(
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
        """Spend a reaction for a generic Ready action; settle its declared effect by ruling."""
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
        scope = f"combat-ready-action-resolve:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return combat_response(campaign_id, principal_id, replay)
        campaign, encounter = active_encounter(campaign_id)
        if campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        if release and choice_id not in {
            str(item.get("id")) for item in available_reactions(encounter, actor_id)
        }:
            raise CombatEngineError("actor cannot take this reaction")
        next_encounter, readied = resolve_readied_action_window(
            encounter, actor_id_value=actor_id, choice_id=choice_id, release=release
        )
        next_encounter["log"] = [
            *list(next_encounter.get("log") or []),
            {
                "type": "readied_action_released" if release else "readied_action_declined",
                "actor_id": actor_id,
                "readied_id": readied.get("id"),
                "declaration": declaration or {},
            },
        ][-100:]
        next_state = {**dict(campaign.state or {}), "combat": next_encounter}
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            expected_campaign_revision=campaign.revision,
            operation="combat.ready.action.release" if release else "combat.ready.action.decline",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
        )
        response = {
            "status": "pending_ruling" if release else "armed",
            "released": release,
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
        rule_context = effective_rule_context(
            campaign_id,
            facts={"actor_id": actor_id, "activity_id": activity_id},
            branch_id=resolved_branch_id,
        )
        try:
            applied = consume_activity(
                current.sheet,
                activity_id=activity_id,
                rules=rule_context,
            )
        except ActivityError as exc:
            raise CombatEngineError(str(exc)) from exc
        if applied.get("status") in {"pending_choice", "pending_ruling"}:
            return {
                "status": applied["status"],
                "result": {key: value for key, value in applied.items() if key != "sheet"},
                "campaign_revision": campaign.revision,
            }
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
        engine_owned_special = (
            activity_id == "dnd5e.content.srd2014.feature.fighter-action-surge"
        )
        if (
            activation_type == "special"
            and not engine_owned_special
            and not is_dm(campaign_id, principal_id)
        ):
            raise CombatEngineError("special activity triggers require a DM resolution")
        next_encounter = pay_activity_activation(
            encounter,
            actor_id_value=actor_id,
            activation_type=activation_type,
        )
        next_encounter, core_effect = settle_core_activity_effect(
            next_encounter,
            actor_id_value=actor_id,
            activity_id=activity_id,
            declaration=declaration,
        )
        if activity_id == "dnd5e.content.srd2014.feature.fighter-second-wind":
            second_wind = resolve_second_wind_to_sheet(applied["sheet"])
            applied["sheet"] = second_wind.pop("sheet")
            core_effect = second_wind
        if core_effect is not None:
            applied["requires_ruling"] = bool(core_effect.get("requires_ruling", False))
            applied["core_effect"] = core_effect
            mechanic_id = {
                "action_surge": "dnd5e.core.activity.action_surge",
                "cunning_action": "dnd5e.core.activity.cunning_action",
                "second_wind": "dnd5e.core.activity.second_wind",
            }[str(core_effect["kind"])]
            applied["rule_receipts"] = [
                *list(applied.get("rule_receipts") or []),
                *core_receipts(
                    rule_context,
                    [mechanic_id],
                    f"combat.activity.{core_effect['kind']}",
                ),
            ]
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
            rule_receipts=list(applied.get("rule_receipts") or []),
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
    def character_check(
        campaign_id: str,
        actor_id: str,
        kind: str,
        ability: str,
        dc: int = 0,
        proficient: bool = False,
        bonus: int = 0,
        advantage: bool = False,
        disadvantage: bool = False,
        rule_facts: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Resolve and audit a non-combat check using the branch's exact rule-pack lock."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_campaign_actor(campaign_id, actor_id)
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        settlement_facts = checked_rule_facts(rule_facts)
        payload = {
            "actor_id": actor_id,
            "kind": kind,
            "ability": ability,
            "dc": dc,
            "proficient": proficient,
            "bonus": bonus,
            "advantage": advantage,
            "disadvantage": disadvantage,
            "rule_facts": settlement_facts,
            "branch_id": resolved_branch_id,
        }
        scope = f"character-check:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        campaign = campaigns.get(campaign_id)
        if dict(campaign.state or {}).get("combat", {}).get("active", False):
            raise CombatEngineError("use combat_check while combat is active")
        if campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        result = resolve_actor_check(
            combat_actor_snapshot(actor_id),
            kind=kind,
            ability=ability,
            dc=dc,
            proficient=proficient,
            bonus=bonus,
            advantage=advantage,
            disadvantage=disadvantage,
            rules=effective_rule_context(
                campaign_id,
                facts={
                    **settlement_facts,
                    "actor_id": actor_id,
                    "kind": kind,
                    "ability": ability,
                    "dc": dc,
                },
                branch_id=resolved_branch_id,
            ),
        )
        next_state = dict(campaign.state or {})
        next_state["resolution_log"] = [
            *list(next_state.get("resolution_log") or []),
            {"type": kind, "actor_id": actor_id, "result": result},
        ][-100:]
        revisions_result = StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(next_state),
            expected_campaign_revision=campaign.revision,
            operation=f"character.{kind}",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
            rule_receipts=list(result.get("rule_receipts") or []),
        )
        response = {
            "status": "committed",
            "result": result,
            "campaign_revision": mutation_revision(campaign_id),
            "revisions": [asdict(item) for item in revisions_result or []],
        }
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
        )

    @mcp.tool()
    def combat_check(
        campaign_id: str,
        actor_id: str,
        kind: str,
        ability: str = "",
        target_id: str | None = None,
        action: str | None = None,
        dc: int = 0,
        proficient: bool = False,
        bonus: int = 0,
        advantage: bool = False,
        disadvantage: bool = False,
        rule_facts: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a check/save/death-save or an atomic Medicine stabilization."""
        access.require_actor(campaign_id, actor_id, principal_id, control=True)
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        settlement_facts = checked_rule_facts(rule_facts)
        if not is_dm(campaign_id, principal_id):
            if kind != "death_save":
                raise CombatEngineError("checks and saves require a DM-issued resolution")
            if advantage or disadvantage or proficient or bonus:
                raise CombatEngineError("death-save modifiers require a DM ruling")
            if settlement_facts:
                raise CombatEngineError("rule facts require a DM-issued resolution")
        if kind == "death_save" and settlement_facts:
            raise CombatEngineError("rule facts are not accepted for death saves")
        if kind == "death_save" and target_id is not None:
            raise CombatEngineError("death saves do not accept a target_id")
        if kind in {"death_save", "stabilize"} and action is not None:
            raise CombatEngineError(f"{kind} manages its own action boundary")
        if kind != "death_save" and not str(ability).strip():
            raise CombatEngineError("ability is required for checks, saves, and stabilization")
        normalized_check_action = (
            str(action).strip().lower().replace("-", "_") if action is not None else None
        )
        if normalized_check_action not in {
            None,
            "hide",
            "improvise",
            "influence",
            "search",
            "study",
            "utilize",
            "use_object",
        }:
            raise CombatEngineError("unsupported action-bound check")
        if kind == "stabilize":
            if not target_id:
                raise CombatEngineError("stabilize requires target_id")
            if target_id == actor_id:
                raise CombatEngineError("an unconscious actor cannot stabilize itself")
            if str(ability).casefold() not in {"wisdom", "wis", "medicine"}:
                raise CombatEngineError("stabilize uses a Wisdom (Medicine) check")
            if dc not in {0, 10} or proficient or bonus:
                raise CombatEngineError(
                    "stabilize derives its DC and Medicine modifier from the Core rules "
                    "and actor card"
                )
        elif target_id is not None:
            raise CombatEngineError("target_id is accepted only for kind=stabilize")
        payload = {
            "actor_id": actor_id,
            "target_id": target_id,
            "action": normalized_check_action,
            "kind": kind,
            "ability": ability,
            "dc": dc,
            "proficient": proficient,
            "bonus": bonus,
            "advantage": advantage,
            "disadvantage": disadvantage,
            "rule_facts": settlement_facts,
            "branch_id": resolved_branch_id,
        }
        scope = f"combat-check:{campaign_id}:{resolved_branch_id}:{principal_id}"
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
        stabilize_target: dict[str, Any] | None = None
        active_state = dict(campaign.state or {}).get("combat")
        if isinstance(active_state, dict) and active_state.get("active", False):
            require_encounter_combatant(active_state, actor_id, role="check actor")
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
        elif kind == "stabilize":
            assert target_id is not None
            _campaign, active = active_encounter(campaign_id)
            require_no_blocking_pending(active)
            acting = current_combatant(active)
            if acting is None or acting.get("actor_id") != actor_id:
                raise CombatEngineError("stabilization can be attempted only on this actor's turn")
            require_campaign_actor(campaign_id, target_id)
            combatants = {
                str(item.get("actor_id") or ""): item for item in active.get("combatants", [])
            }
            source_combatant = combatants.get(actor_id)
            target_combatant = combatants.get(target_id)
            if source_combatant is None or target_combatant is None:
                raise CombatEngineError("both actors must be present in the encounter")
            source_position = source_combatant.get("position")
            target_position = target_combatant.get("position")
            if not (
                isinstance(source_position, dict)
                and isinstance(target_position, dict)
                and "x" in source_position
                and "y" in source_position
                and "x" in target_position
                and "y" in target_position
            ):
                raise CombatEngineError("stabilization requires recorded map positions")
            cell_ft = int(
                dict(dict(active.get("battle_map") or {}).get("grid") or {}).get("cell_ft", 5) or 5
            )
            distance = int(
                max(
                    abs(float(source_position["x"]) - float(target_position["x"])),
                    abs(float(source_position["y"]) - float(target_position["y"])),
                )
                * cell_ft
            )
            if distance > 5:
                raise CombatEngineError("stabilization requires the target to be within 5 feet")
            stabilize_target = combat_actor_snapshot(target_id)
            stabilize_sheet(stabilize_target["sheet"])
        actor = combat_actor_snapshot(actor_id)
        normalized_ability = str(ability).strip().casefold().replace(" ", "_")
        derived_skill = normalized_ability in dict(actor["derived"].get("skills") or {})
        if kind in {"ability", "check"} and derived_skill and (proficient or bonus):
            raise CombatEngineError(
                "skill checks derive proficiency and bonuses from the actor card"
            )
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
        elif kind == "stabilize":
            assert target_id is not None and stabilize_target is not None
            medicine_total = int(actor["derived"]["skills"]["medicine"])
            wisdom_modifier = int(actor["derived"]["ability_modifiers"]["wisdom"])
            stabilize_context = effective_rule_context(
                campaign_id,
                facts={
                    **settlement_facts,
                    "actor_id": actor_id,
                    "target_id": target_id,
                    "kind": "stabilize",
                    "ability": "wisdom",
                    "dc": 10,
                },
                branch_id=resolved_branch_id,
            )
            check = resolve_actor_check(
                actor,
                kind="ability",
                ability="wisdom",
                dc=10,
                proficient=False,
                bonus=medicine_total - wisdom_modifier,
                advantage=advantage,
                disadvantage=disadvantage,
                ruleset=encounter.get("ruleset") if encounter else None,
                rules=stabilize_context,
            )
            encounter = resolve_common_action(
                encounter,
                actor_id_value=actor_id,
                action="stabilize",
                target_id=target_id,
                payload={"method": "medicine", "dc": 10},
            )
            result = {
                **check,
                "kind": "stabilize",
                "skill": "medicine",
                "target_id": target_id,
                "stabilized": bool(check["success"]),
                "rule_receipts": [
                    *list(check.get("rule_receipts") or []),
                    *core_receipts(
                        stabilize_context,
                        ["dnd5e.core.damage.zero_hp"],
                        "combat.stabilize",
                    ),
                ],
            }
            if check["success"]:
                applied = stabilize_sheet(stabilize_target["sheet"])
                result["stabilization"] = {
                    key: value for key, value in applied.items() if key != "sheet"
                }
                current_target = characters.get(target_id)
                updates.append(
                    CharacterStateUpdate(
                        character_id=target_id,
                        sheet=validate_character_sheet(applied["sheet"]),
                        notes=validate_character_notes(current_target.notes),
                        expected_revision=current_target.revision,
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
                ability=normalized_ability,
                dc=dc,
                proficient=proficient,
                bonus=bonus,
                advantage=advantage,
                disadvantage=disadvantage,
                ruleset=encounter.get("ruleset") if encounter else None,
                rules=effective_rule_context(
                    campaign_id,
                    facts={
                        **settlement_facts,
                        "actor_id": actor_id,
                        "kind": kind,
                        "ability": ability,
                        "dc": dc,
                    },
                    branch_id=resolved_branch_id,
                ),
            )
            if derived_skill:
                result = {**result, "skill": normalized_ability}
            if normalized_check_action is not None:
                if not encounter:
                    raise CombatEngineError("an action-bound check requires active combat")
                require_no_blocking_pending(encounter)
                acting = current_combatant(encounter)
                if acting is None or acting.get("actor_id") != actor_id:
                    raise CombatEngineError(
                        "an action-bound check can be made only on this actor's turn"
                    )
                encounter = resolve_common_action(
                    encounter,
                    actor_id_value=actor_id,
                    action=normalized_check_action,
                    payload={
                        "kind": kind,
                        "ability": normalized_ability,
                        "dc": dc,
                    },
                )
                result = {**result, "action": normalized_check_action}
        if encounter:
            for update in updates:
                sync_combatant_conditions(encounter, update.character_id, update.sheet)
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
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
            rule_receipts=list(result.get("rule_receipts") or []),
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
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        payload = {
            "target_id": target_id,
            "dc": dc,
            "effect_ids": list(effect_ids),
            "branch_id": resolved_branch_id,
        }
        scope = f"combat-concentration:{campaign_id}:{resolved_branch_id}:{principal_id}"
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
            rules=effective_rule_context(
                campaign_id,
                facts={
                    "actor_id": target_id,
                    "kind": "save",
                    "ability": "constitution",
                    "dc": dc,
                },
                branch_id=resolved_branch_id,
            ),
        )
        updated_sheet = apply_concentration_result(
            actor["sheet"], effect_ids=effect_ids, success=result["success"]
        )
        current = characters.get(target_id)
        next_state = dict(campaign.state or {})
        if isinstance(encounter, dict):
            reconcile_readied_spells(encounter, target_id, updated_sheet)
            active_effect_ids = {
                str(effect.get("id"))
                for effect in updated_sheet.get("effects", [])
                if effect.get("active") and effect.get("concentration")
            }
            encounter["pending"] = [
                item
                for item in encounter.get("pending", [])
                if item.get("id") != pending.get("id")
                and not (
                    item.get("kind") == "concentration"
                    and item.get("actor_id") == target_id
                    and not active_effect_ids.intersection(
                        {str(effect_id) for effect_id in item.get("effect_ids", [])}
                    )
                )
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
            branch_id=resolved_branch_id,
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
        knock_out: bool = False,
        melee: bool = False,
    ) -> dict[str, Any]:
        """Apply DM-approved damage parts; automatic trait and HP settlement is deterministic."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        require_campaign_actor(campaign_id, target_id)
        payload = {
            "target_id": target_id,
            "parts": parts,
            "critical": critical,
            "knock_out": knock_out,
            "melee": melee,
            "branch_id": resolved_branch_id,
        }
        scope = f"combat-damage:{campaign_id}:{resolved_branch_id}:{principal_id}"
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
        target_uses_death_saves = target.get("character_type") == "pc"
        ruleset = str(target["sheet"].get("edition") or "2014")
        if isinstance(existing_encounter, dict) and existing_encounter.get("active", False):
            require_no_blocking_pending(existing_encounter)
            ruleset = str(existing_encounter.get("ruleset") or ruleset)
            target_combatant = require_encounter_combatant(
                existing_encounter, target_id, role="damage target"
            )
            target_uses_death_saves = bool(target_combatant.get("death_saves", False))
        applied = apply_damage_parts_to_sheet(
            target["sheet"],
            parts,
            source=principal_id,
            critical=critical,
            ruleset=ruleset,
            death_saves=target_uses_death_saves,
            knock_out=knock_out,
            melee=melee,
        )
        applied_result = {key: value for key, value in applied.items() if key != "sheet"}
        damage_receipts = core_receipts(
            effective_rule_context(campaign_id, branch_id=resolved_branch_id),
            ["dnd5e.core.damage.zero_hp"] if int(applied["after_hp"]) == 0 else [],
            "damage.apply",
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
                {"type": "damage", "target_id": target_id, "result": applied_result},
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
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
            rule_receipts=damage_receipts,
        )
        response = {
            "status": "committed",
            "result": applied_result,
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
        source_actor_id: str | None = None,
        spell_id: str | None = None,
        spell_level: int | None = None,
    ) -> dict[str, Any]:
        """Apply source-aware healing with feature modifiers and max-HP clamping."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        require_campaign_actor(campaign_id, target_id)
        if source_actor_id:
            require_campaign_actor(campaign_id, source_actor_id)
        if int(amount) <= 0:
            raise CombatEngineError("healing amount must be positive")
        payload = {
            "target_id": target_id,
            "amount": amount,
            "branch_id": resolved_branch_id,
            "source_actor_id": source_actor_id,
            "spell_id": spell_id,
            "spell_level": spell_level,
        }
        scope = f"combat-heal:{campaign_id}:{resolved_branch_id}:{principal_id}"
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
        active = dict(campaign.state or {}).get("combat")
        if isinstance(active, dict) and active.get("active", False):
            require_no_blocking_pending(active)
            require_encounter_combatant(active, target_id, role="healing target")
            if source_actor_id is not None:
                require_encounter_combatant(active, source_actor_id, role="healing source")
        source = combat_actor_snapshot(source_actor_id) if source_actor_id else None
        applied = apply_healing_to_sheet(
            target["sheet"],
            amount=amount,
            source_sheet=source["sheet"] if source else None,
            spell_id=spell_id,
            spell_level=spell_level,
        )
        if applied.get("source") is not None:
            applied["source"]["actor_id"] = source_actor_id
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
            branch_id=resolved_branch_id,
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
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        require_campaign_actor(campaign_id, actor_id)
        payload = {
            "actor_id": actor_id,
            "event": event,
            "candidates": candidates or [],
            "kind": kind,
            "branch_id": resolved_branch_id,
        }
        scope = f"combat-choice-open:{campaign_id}:{resolved_branch_id}:{principal_id}"
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
            branch_id=resolved_branch_id,
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
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        payload = {
            "actor_id": actor_id,
            "choice_id": choice_id,
            "selection": selection,
            "branch_id": resolved_branch_id,
        }
        scope = f"combat-choice-resolve:{campaign_id}:{resolved_branch_id}:{principal_id}"
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
        if pending_choice and pending_choice.get("trigger") == "attack_hit_defense":
            raise CombatEngineError(
                "attack-defense windows must use combat_choice(action=resolve_defense)"
            )
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
        if checkout:
            source_snapshot_id = from_snapshot_id or branches.current(campaign_id).head_snapshot_id
            if source_snapshot_id:
                assert_snapshot_core_available(snapshots.get_by_id(campaign_id, source_snapshot_id))
            snapshots.assert_clean(campaign_id)
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
        target_branch = branches.get(campaign_id, branch_id)
        if target_branch.head_snapshot_id:
            assert_snapshot_core_available(
                snapshots.get_by_id(campaign_id, target_branch.head_snapshot_id)
            )
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
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
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
        assert_snapshot_core_available(snapshots.get(campaign_id, slot))
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
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        return {"valid": snapshots.verify(campaign_id, slot)}

    @mcp.tool()
    def snapshot_lineage(
        campaign_id: str,
        slot: int | None = None,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        """List the lineage of a save without mutating campaign history."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
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
        normalized_sheet = validate_character_sheet(
            sheet_value,
            rules=(effective_rule_context(campaign_id) if campaign_id else None),
        )
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
    def character_library_list(
        character_type: str | None = None,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        """List reusable D&D templates that are not bound to a campaign."""
        return [
            library_character_view(item, principal_id)
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
                sheet=validate_character_sheet(sheet, rules=effective_rule_context(campaign_id)),
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
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Atomically create a PC library template and independent campaign instance."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if not idempotency_key:
            raise ValueError("idempotency_key is required for character build")
        sheet_value = deepcopy(sheet or default_character_sheet())
        sheet_value["edition"] = str(campaigns.get(campaign_id).settings.get("edition") or "2024")
        normalized_sheet = validate_character_sheet(
            sheet_value,
            rules=effective_rule_context(campaign_id),
        )
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
            principal_id=principal_id,
            idempotency_key=idempotency_key,
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
        rule_receipts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Persist a D&D schema mutation with derived values recalculated."""
        current = characters.get(character_id)
        sheet_value = deepcopy(sheet)
        if current.campaign_id is not None:
            sheet_value["edition"] = str(
                campaigns.get(current.campaign_id).settings.get("edition") or "2024"
            )
        normalized_sheet = validate_character_sheet(
            sheet_value,
            rules=(effective_rule_context(current.campaign_id) if current.campaign_id else None),
        )
        return update_character(
            current,
            operation=operation,
            sheet=normalized_sheet,
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload=payload,
            response_extra=response_extra,
            rule_receipts=rule_receipts,
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
        require_outside_active_combat(current, "character sheet replacement")
        sheet_value = deepcopy(sheet)
        if current.campaign_id is not None:
            sheet_value["edition"] = str(
                campaigns.get(current.campaign_id).settings.get("edition") or "2024"
            )
        normalized_sheet = validate_character_sheet(
            sheet_value,
            rules=(effective_rule_context(current.campaign_id) if current.campaign_id else None),
        )
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
        require_outside_active_combat(current, "wallet adjustment")
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
        require_outside_active_combat(current, "inventory changes")
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
        require_outside_active_combat(current, "inventory changes")
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
        require_outside_active_combat(current, "inventory changes")
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
        require_outside_active_combat(current, "equipment changes")
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
        require_outside_active_combat(current, "ammunition consumption")
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
        require_outside_active_combat(source, "inventory transfer")
        require_outside_active_combat(target, "inventory transfer")
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
        access.require_actor(source.campaign_id, target.id, principal_id, control=True)
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
            "source": visible_character_view(source_after, principal_id),
            "target": visible_character_view(target_after, principal_id),
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
        require_outside_active_combat(current, "effect changes")
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
        require_outside_active_combat(current, "effect changes")
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
        hit_dice_spends: list[dict[str, Any]] | None = None,
        hit_dice_recovery: dict[str, int] | None = None,
        food_and_drink: bool = False,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Apply rest recovery and, on a long rest, atomically replace prepared spells."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        require_outside_active_combat(current, "rest")
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
            "hit_dice_spends": hit_dice_spends or [],
            "hit_dice_recovery": hit_dice_recovery or {},
            "food_and_drink": food_and_drink,
        }
        branch_id = require_current_branch(current.campaign_id, None)
        scope = f"character-rest:{current.campaign_id}:{branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        rest_rules = effective_rule_context(
            current.campaign_id,
            facts={"actor_id": character_id, "rest_type": rest_type},
        )
        applied = apply_rest(
            current.sheet,
            rest_type=rest_type,
            hit_dice_spends=hit_dice_spends,
            hit_dice_recovery=hit_dice_recovery,
            food_and_drink=food_and_drink,
            rules=rest_rules,
        )
        if applied.get("status") in {"pending_choice", "pending_ruling"}:
            return {
                "status": applied["status"],
                "result": {key: value for key, value in applied.items() if key != "sheet"},
                "character": character_view(current),
            }
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
            rule_receipts=[
                *list(applied.get("rule_receipts") or []),
                *(
                    core_receipts(
                        rest_rules,
                        ["dnd5e.core.spell.preparation"],
                        "spell.prepare.long_rest",
                    )
                    if prepared_spell_ids is not None
                    else []
                ),
            ],
        )
        response = {
            "status": applied["status"],
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

    def character_stable_recovery(
        character_id: str,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Roll and settle an unhealed Stable creature's automatic 1 HP recovery."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        require_outside_active_combat(current, "stable recovery")
        if current.campaign_id is None:
            raise ValueError("stable recovery requires a campaign-bound character")
        access.require_campaign(current.campaign_id, principal_id, roles={"owner", "dm"})
        if expected_revision is None or not idempotency_key:
            raise ValueError(
                "expected_revision and idempotency_key are required for stable recovery"
            )
        branch_id = require_current_branch(current.campaign_id, None)
        payload = {"character_id": character_id, "operation": "stable_recovery"}
        scope = f"character-write:{current.campaign_id}:{branch_id}:{principal_id}:{character_id}"
        request_payload = {
            "operation": "character.stable_recovery",
            "character_id": character_id,
            **payload,
        }
        replay = replay_idempotent(scope, idempotency_key, request_payload)
        if replay is not None:
            return replay
        if current.revision != expected_revision:
            raise ValueError(f"character revision conflict: {character_id}")
        # Validate the actor before touching RNG; the actual rolled duration is
        # applied to a fresh sheet inside the atomic mutation below.
        recover_stable_creature(current.sheet, recovery_hours=1)
        campaign = campaigns.get(current.campaign_id)
        next_state = validate_party_state(deepcopy(campaign.state or {}))
        world_time = dict(next_state.get("world_time") or {})
        if not world_time:
            raise ValueError("set the campaign clock before resolving stable recovery")
        recovery_roll = asdict(roll("1d4"))
        recovery_hours = int(recovery_roll["total"])
        elapsed = int(world_time.get("elapsed_minutes", 0) or 0) + recovery_hours * 60
        next_world_time = {
            "schema_version": 1,
            "day": elapsed // 1440 + 1,
            "hour": (elapsed % 1440) // 60,
            "minute": elapsed % 60,
            "elapsed_minutes": elapsed,
            "label": str(world_time.get("label") or ""),
        }
        next_state["world_time"] = next_world_time
        world_advanced: list[str] = []
        world_expired: list[str] = []
        for effect_period, amount in (
            ("minute", recovery_hours * 60),
            ("hour", recovery_hours),
        ):
            world_result = advance_world_effect_durations(
                next_state, period=effect_period, amount=amount
            )
            next_state = world_result["state"]
            world_advanced.extend(world_result["advanced"])
            world_expired.extend(world_result["expired"])
        rules = effective_rule_context(
            current.campaign_id,
            facts={"actor_id": character_id, "recovery_hours": recovery_hours},
        )
        receipts: list[dict[str, Any]] = []
        updates: list[CharacterStateUpdate] = []
        advanced: dict[str, list[str]] = {}
        expired: dict[str, list[str]] = {}
        applied: dict[str, Any] | None = None
        for character in characters.list(campaign_id=current.campaign_id):
            sheet = character.sheet
            character_advanced: list[str] = []
            character_expired: list[str] = []
            for effect_period, amount in (
                ("minute", recovery_hours * 60),
                ("hour", recovery_hours),
            ):
                duration_result = advance_effect_durations(
                    sheet, period=effect_period, amount=amount
                )
                extension = apply_rule_event(
                    duration_result["sheet"],
                    "duration.advance",
                    context_with_facts(
                        rules,
                        actor_id=character.id,
                        period=effect_period,
                        amount=amount,
                    ),
                )
                receipts.extend(extension.receipts)
                sheet = extension.sheet
                character_advanced.extend(duration_result["advanced"])
                character_expired.extend(duration_result["expired"])
            if character.id == character_id:
                applied = recover_stable_creature(sheet, recovery_hours=recovery_hours)
                sheet = applied["sheet"]
            if sheet != character.sheet:
                updates.append(
                    CharacterStateUpdate(
                        character_id=character.id,
                        sheet=validate_character_sheet(sheet),
                        notes=validate_character_notes(character.notes),
                        expected_revision=(
                            expected_revision
                            if character.id == character_id
                            else character.revision
                        ),
                    )
                )
            if character_advanced:
                advanced[character.id] = list(dict.fromkeys(character_advanced))
            if character_expired:
                expired[character.id] = list(dict.fromkeys(character_expired))
        assert applied is not None
        receipts.extend(
            core_receipts(
                rules,
                ["dnd5e.core.damage.stable_recovery"],
                "character.stable_recovery",
            )
        )
        StateMutationService(storage.database).replace(
            current.campaign_id,
            campaign_state=next_state,
            character_updates=updates,
            expected_campaign_revision=campaign.revision,
            operation="character.stable_recovery",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=idempotency_key,
            rule_receipts=receipts,
        )
        response = {
            "character": character_view(characters.get(character_id)),
            "status": "recovered",
            "recovery_roll": recovery_roll,
            "recovery_hours": applied["recovery_hours"],
            "before_hp": applied["before_hp"],
            "after_hp": applied["after_hp"],
            "world_time": next_world_time,
            "advanced": advanced,
            "expired": expired,
            "world_advanced": list(dict.fromkeys(world_advanced)),
            "world_expired": list(dict.fromkeys(world_expired)),
            "rule_receipts": receipts,
        }
        return remember_idempotent(
            scope,
            idempotency_key,
            request_payload,
            response,
            campaign_id=current.campaign_id,
        )

    def character_stand(
        character_id: str,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Stand a conscious Prone character outside active combat."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        require_outside_active_combat(current, "standing")
        if current.campaign_id is None:
            raise ValueError("standing requires a campaign-bound character")
        applied = stand_outside_combat(current.sheet)
        rules = effective_rule_context(
            current.campaign_id, facts={"actor_id": character_id, "condition": "prone"}
        )
        receipts = core_receipts(
            rules,
            ["dnd5e.core.movement.prone_crawl_stand"],
            "character.stand",
        )
        return update_sheet(
            character_id,
            applied["sheet"],
            operation="character.stand",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={"operation": "stand"},
            response_extra={
                "status": applied["status"],
                "removed_condition": applied["removed_condition"],
                "rule_receipts": receipts,
            },
            rule_receipts=receipts,
        )

    def character_level_advance(
        character_id: str,
        class_name: str,
        hp_method: str,
        reason: str,
        source_ref: str,
        hp_roll: int | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Advance one existing 2014 class level during the lobby phase."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        require_outside_active_combat(current, "level advancement")
        if current.campaign_id is None:
            raise ValueError("level advancement requires a campaign-bound character")
        if not is_dm(current.campaign_id, principal_id):
            raise PermissionError("level advancement requires the campaign DM")
        campaign = campaigns.get(current.campaign_id)
        state = dict(campaign.state or {})
        if state.get("game_phase", PROFILE_LOBBY) != PROFILE_LOBBY:
            raise CombatEngineError("switch to lobby before advancing a character level")
        if expected_revision is None or not idempotency_key:
            raise ValueError(
                "expected_revision and idempotency_key are required for level advancement"
            )
        normalized_reason = str(reason).strip()
        normalized_source_ref = str(source_ref).strip()
        if not normalized_reason or not normalized_source_ref:
            raise ValueError("reason and source_ref are required for audited level advancement")
        audit_source = f"{normalized_source_ref}: {normalized_reason}"
        if len(audit_source) > 300:
            raise ValueError("combined source_ref and reason must not exceed 300 characters")
        branch_id = require_current_branch(current.campaign_id, None)
        old_level = int(current.sheet.get("progression", {}).get("level", 0) or 0)
        context = level_advancement_content_context(
            current.campaign_id,
            current.sheet,
            class_name=class_name,
            new_level=old_level + 1,
            branch_id=branch_id,
        )
        applied = advance_single_class_level(
            current.sheet,
            class_name=class_name,
            hp_method=hp_method,
            hp_roll=hp_roll,
            hp_per_level_bonus=int(context["hp_per_level_bonus"]),
            source=audit_source,
        )
        rules = effective_rule_context(
            current.campaign_id,
            facts={
                "actor_id": character_id,
                "class_name": class_name,
                "old_level": applied["old_level"],
                "new_level": applied["new_level"],
                "source_ref": normalized_source_ref,
            },
        )
        receipts = core_receipts(
            rules,
            [
                "dnd5e.core.progression.hp_hit_dice",
                "dnd5e.core.progression.spellcasting",
            ],
            "character.level.advance",
        )
        follow_up = {
            "feature_artifacts": context["feature_options"],
            "subclass_options": context["subclass_options"],
            "spell_choices": applied["spell_choices"],
            "prepared_spell_event": (
                "level_up"
                if applied["spellcasting"].get("mode") in {"prepared", "spellbook"}
                else None
            ),
            "complete": not (
                context["feature_options"]
                or context["subclass_options"]
                or any(int(value) for value in applied["spell_choices"].values())
            ),
        }
        result = {key: value for key, value in applied.items() if key != "sheet"}
        result["hp_bonus_sources"] = context["hp_bonus_sources"]
        result["follow_up"] = follow_up
        return update_sheet(
            character_id,
            applied["sheet"],
            operation="character.level.advance",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={
                "class_name": class_name,
                "hp_method": hp_method,
                "hp_roll": hp_roll,
                "reason": normalized_reason,
                "source_ref": normalized_source_ref,
            },
            response_extra={
                "status": "committed",
                "advancement": result,
                "rule_receipts": receipts,
            },
            rule_receipts=receipts,
        )

    @mcp.tool()
    def character_cast_spell(
        character_id: str,
        spell_id: str,
        cast_level: int | None = None,
        ritual: bool = False,
        component_ruling: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Pay canonical spell resources and start concentration from a v2 spell card."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        require_outside_active_combat(current, "spell casting")
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
            "component_ruling": component_ruling or {},
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
            component_ruling=component_ruling,
            rules=effective_rule_context(
                current.campaign_id,
                facts={
                    "actor_id": character_id,
                    "spell_id": spell_id,
                    "cast_level": cast_level,
                },
            ),
        )
        if applied.get("status") in {"pending_choice", "pending_ruling"}:
            return {
                "status": applied["status"],
                "result": {key: value for key, value in applied.items() if key != "sheet"},
                "character": character_view(current),
            }
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
            rule_receipts=list(applied.get("rule_receipts") or []),
        )
        response = {
            "status": "pending_ruling",
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
        require_outside_active_combat(current, "activity use")
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
            applied = consume_activity(
                current.sheet,
                activity_id=activity_id,
                rules=effective_rule_context(
                    current.campaign_id,
                    facts={"actor_id": character_id, "activity_id": activity_id},
                ),
            )
        except ActivityError as exc:
            raise ValueError(str(exc)) from exc
        if applied.get("status") in {"pending_choice", "pending_ruling"}:
            return {
                "status": applied["status"],
                "result": {key: value for key, value in applied.items() if key != "sheet"},
                "character": character_view(current),
            }
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
            rule_receipts=list(applied.get("rule_receipts") or []),
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
        require_outside_active_combat(current, "resource changes")
        return update_sheet(
            character_id,
            set_resource_value(current.sheet, resource, value),
            operation="character.resource.set",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={"resource": resource, "value": value},
        )

    def character_apply_damage(
        character_id: str,
        parts: list[dict[str, Any]],
        *,
        critical: bool = False,
        knock_out: bool = False,
        melee: bool = False,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Apply DM-issued damage during play without mutating encounter state."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        require_outside_active_combat(current, "noncombat damage")
        if current.campaign_id is None:
            raise CombatEngineError("noncombat damage requires a campaign-bound actor")
        access.require_campaign(current.campaign_id, principal_id, roles={"owner", "dm"})
        campaign = campaigns.get(current.campaign_id)
        applied = apply_damage_parts_to_sheet(
            current.sheet,
            parts,
            source=principal_id,
            critical=critical,
            ruleset=str(campaign.settings.get("edition") or current.sheet.get("edition") or "2014"),
            death_saves=current.character_type == "pc",
            knock_out=knock_out,
            melee=melee,
        )
        result = {key: value for key, value in applied.items() if key != "sheet"}
        return update_sheet(
            character_id,
            applied["sheet"],
            operation="character.damage.apply",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={
                "parts": parts,
                "critical": critical,
                "knock_out": knock_out,
                "melee": melee,
            },
            response_extra={"result": result},
            rule_receipts=core_receipts(
                effective_rule_context(current.campaign_id),
                ["dnd5e.core.damage.zero_hp"] if int(applied["after_hp"]) == 0 else [],
                "damage.apply",
            ),
        )

    def character_apply_healing(
        character_id: str,
        amount: int,
        *,
        source_actor_id: str | None = None,
        spell_id: str | None = None,
        spell_level: int | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Apply DM-issued source-aware healing during play."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        require_outside_active_combat(current, "noncombat healing")
        if current.campaign_id is None:
            raise CombatEngineError("noncombat healing requires a campaign-bound actor")
        access.require_campaign(current.campaign_id, principal_id, roles={"owner", "dm"})
        if int(amount) <= 0:
            raise CombatEngineError("healing amount must be positive")
        source = None
        if source_actor_id is not None:
            source = require_campaign_actor(current.campaign_id, source_actor_id)
        applied = apply_healing_to_sheet(
            current.sheet,
            amount=amount,
            source_sheet=source.sheet if source is not None else None,
            spell_id=spell_id,
            spell_level=spell_level,
        )
        if applied.get("source") is not None:
            applied["source"]["actor_id"] = source_actor_id
        result = {key: value for key, value in applied.items() if key != "sheet"}
        return update_sheet(
            character_id,
            applied["sheet"],
            operation="character.heal.apply",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={
                "amount": amount,
                "source_actor_id": source_actor_id,
                "spell_id": spell_id,
                "spell_level": spell_level,
            },
            response_extra={"result": result},
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
            if state.get("game_phase", PROFILE_LOBBY) != PROFILE_LOBBY:
                raise CombatEngineError(
                    "live prepared-spell changes must be submitted atomically with character_rest"
                )
        preparation_rules = (
            effective_rule_context(current.campaign_id) if current.campaign_id else None
        )
        return update_sheet(
            character_id,
            set_spell_prepared(current.sheet, spell_id, prepared),
            operation="character.spell.prepare" if prepared else "character.spell.unprepare",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={"spell_id": spell_id, "prepared": prepared},
            rule_receipts=core_receipts(
                preparation_rules,
                ["dnd5e.core.spell.preparation"],
                "spell.prepare.setup",
            ),
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
            if state.get("game_phase", PROFILE_LOBBY) != PROFILE_LOBBY:
                raise CombatEngineError("switch to lobby for setup or level-up preparation changes")
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
        preparation_rules = (
            effective_rule_context(current.campaign_id) if current.campaign_id else None
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
            rule_receipts=core_receipts(
                preparation_rules,
                ["dnd5e.core.spell.preparation"],
                f"spell.prepare.{normalized_event}",
            ),
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
        require_outside_active_combat(current, "ability generation")
        sheet = apply_ability_generation(
            current.sheet,
            method=method,
            assignments=assignments,
            rolls=rolls,
        )
        ability_receipts = (
            core_receipts(
                effective_rule_context(current.campaign_id),
                ["dnd5e.core.ability_generation"],
                "character.ability.apply",
            )
            if current.campaign_id
            else []
        )
        return update_sheet(
            character_id,
            sheet,
            operation="character.ability.apply",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={"method": method, "assignments": assignments, "rolls": rolls},
            rule_receipts=ability_receipts,
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
        campaign = campaigns.get(campaign_id)
        character = characters.get(character_id)
        if character.campaign_id != campaign_id:
            raise ValueError("character must belong to the campaign")
        access.require_actor(campaign_id, character_id, principal_id, control=True)
        branch_id = require_current_branch(campaign_id, None)
        payload = {
            "campaign_id": campaign_id,
            "character_id": character_id,
            "item_id": item_id,
            "direction": direction,
            "quantity": quantity,
            "expected_campaign_revision": expected_campaign_revision,
            "expected_character_revision": expected_character_revision,
            "branch_id": branch_id,
        }
        scope = f"party-inventory:{campaign_id}:{branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
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
            actor=principal_id,
            branch_id=branch_id,
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
        campaign = campaigns.get(campaign_id)
        character = characters.get(character_id)
        if character.campaign_id != campaign_id:
            raise ValueError("character must belong to the campaign")
        access.require_actor(campaign_id, character_id, principal_id, control=True)
        branch_id = require_current_branch(campaign_id, None)
        payload = {
            "campaign_id": campaign_id,
            "character_id": character_id,
            "denomination": denomination,
            "amount": amount,
            "direction": direction,
            "expected_campaign_revision": expected_campaign_revision,
            "expected_character_revision": expected_character_revision,
            "branch_id": branch_id,
        }
        scope = f"party-wallet:{campaign_id}:{branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
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
            actor=principal_id,
            branch_id=branch_id,
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
        knowledge_disclosure_scope: str = "owner",
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Append a branch-local chronology event; an event is not actor knowledge."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if not idempotency_key:
            raise ValueError("idempotency_key is required for event writes")
        branch_id = require_current_branch(campaign_id, branch_id)
        if audience_scope == "actor" and not known_by_actor_ids:
            raise ValueError("actor-scoped events require known_by_actor_ids")
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
            "knowledge_disclosure_scope": knowledge_disclosure_scope,
        }
        scope = f"event-add:{campaign_id}:{branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, request_payload)
        if replay is not None:
            return replay
        if known_by_actor_ids:
            created, knowledge_ids = events.add_with_actor_knowledge(
                campaign_id,
                summary=summary,
                actor_ids=known_by_actor_ids,
                knowledge_key=knowledge_key,
                proposition=knowledge_proposition,
                event_type=event_type,
                payload=payload,
                audience_scope=audience_scope,
                disclosure_scope=knowledge_disclosure_scope,
                branch_id=branch_id,
            )
        else:
            created = events.add(
                campaign_id,
                summary=summary,
                event_type=event_type,
                payload=payload,
                audience_scope=audience_scope,
                branch_id=branch_id,
            )
            knowledge_ids = []
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
        resolved_branch_id = readable_branch(campaign_id, branch_id, principal_id)
        access.require_actor(
            campaign_id,
            actor_id,
            principal_id,
            private=True,
            branch_id=resolved_branch_id,
        )
        membership = access.require_campaign(campaign_id, principal_id)
        values = knowledge.list(
            campaign_id,
            actor_id=actor_id,
            branch_id=resolved_branch_id,
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
        resolved_branch_id = readable_branch(campaign_id, branch_id, principal_id)
        access.require_actor(
            campaign_id,
            actor_id,
            principal_id,
            private=True,
            branch_id=resolved_branch_id,
        )
        membership = access.require_campaign(campaign_id, principal_id)
        values = knowledge.search(
            campaign_id,
            actor_id=actor_id,
            query=query,
            branch_id=resolved_branch_id,
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
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
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
    def combat_map_patch(
        campaign_id: str,
        patches: list[dict[str, Any]],
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Record DM-confirmed world changes from an active temporary battle map."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        normalized: list[dict[str, Any]] = []
        for patch in patches:
            if not isinstance(patch, dict) or not isinstance(patch.get("key"), str):
                raise ValueError("each map patch needs a string key")
            normalized.append({"key": patch["key"], "value": deepcopy(patch.get("value"))})
        payload = {"patches": normalized, "branch_id": resolved_branch_id}
        scope = f"combat-map-patch:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        campaign, encounter = active_encounter(campaign_id)
        if campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        battle_map = dict(encounter.get("battle_map") or {})
        if not battle_map:
            raise CombatEngineError("active encounter has no temporary battle map")
        next_encounter = deepcopy(encounter)
        next_map = patch_battle_map(dict(next_encounter["battle_map"]), normalized)
        next_encounter["battle_map"] = next_map
        state = dict(campaign.state or {})
        state["combat"] = next_encounter
        runtime = dict(state.get("scene_runtime") or {})
        scene_id = str(dict(next_map.get("source") or {}).get("scene_id") or "")
        if scene_id:
            scene_state = dict(runtime.get(scene_id) or {})
            for patch in normalized:
                scene_state[patch["key"]] = patch["value"]
            runtime[scene_id] = scene_state
        state["scene_runtime"] = runtime
        StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=validate_party_state(state),
            expected_campaign_revision=campaign.revision,
            operation="combat.map.patch",
            actor=principal_id,
            branch_id=resolved_branch_id,
            idempotency_key=idempotency_key,
        )
        response = {"battle_map": next_map, "world_patches": normalized}
        return remember_idempotent(
            scope,
            idempotency_key,
            payload,
            response,
            campaign_id=campaign_id,
        )

    @mcp.tool()
    def combat_end(
        campaign_id: str,
        outcome: dict[str, Any] | None = None,
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
        outcome_value = dict(outcome or {})
        if outcome_value:
            allowed = {"status", "summary"}
            unknown = set(outcome_value) - allowed
            if unknown:
                raise ValueError(f"unsupported combat outcome fields: {sorted(unknown)}")
            status = str(outcome_value.get("status") or "").strip().lower()
            if status not in {
                "defeat",
                "interrupted",
                "truce",
                "victory",
                "withdrawal",
            }:
                raise ValueError("combat outcome status is invalid")
            summary = str(outcome_value.get("summary") or "").strip()
            if not summary or len(summary) > 2000:
                raise ValueError("combat outcome summary must contain 1 to 2000 characters")
            outcome_value = {"status": status, "summary": summary}
        payload = {"branch_id": resolved_branch_id, "outcome": outcome_value}
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
        if outcome_value:
            combat["outcome"] = outcome_value
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
            "outcome": outcome_value or None,
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
        branch_id = readable_branch(campaign_id, branch_id, principal_id)
        if membership.role not in {"owner", "dm"}:
            audience = "player"
            if actor_id:
                access.require_actor(
                    campaign_id,
                    actor_id,
                    principal_id,
                    private=True,
                    branch_id=branch_id,
                )
        elif actor_id:
            access.require_actor(
                campaign_id,
                actor_id,
                principal_id,
                private=True,
                branch_id=branch_id,
            )
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
    def module_import_job_create(
        campaign_id: str,
        artifact: str,
        title: str | None = None,
        source_key: str | None = None,
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a staged module package job before parsing or activating a revision."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if not idempotency_key:
            raise ValueError("idempotency_key is required for an import job")
        path = storage.artifact_module_path(artifact)
        preview = modules.preview_path(
            path, parser=MarkdownModuleParser(profile=DndModuleProfile())
        )
        logical_key = str(source_key or artifact).strip()
        payload = {
            "artifact": artifact,
            "title": title or path.stem,
            "source_key": logical_key,
        }
        scope = f"import-job-create:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        job = import_jobs.create(
            campaign_id=campaign_id,
            kind="module",
            artifact=artifact,
            artifact_checksum=str(preview.get("checksum") or ""),
            payload=payload,
        )
        response = {"job": asdict(job)}
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
        )

    @mcp.tool()
    def module_import_job_inspect(
        campaign_id: str,
        job_id: str,
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Persist parser preview, stable scene keys, and space evidence for a module job."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if not idempotency_key:
            raise ValueError("idempotency_key is required for module inspection")
        job = require_import_job(campaign_id, job_id, "module")
        payload = {"job_id": job_id, "operation": "inspect"}
        scope = f"import-job:{campaign_id}:{job_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        preview = modules.preview_path(
            storage.artifact_module_path(job.artifact),
            parser=MarkdownModuleParser(profile=DndModuleProfile()),
        )
        updated = import_jobs.record_inspection(job_id, preview)
        response = {"job": asdict(updated), "preview": preview}
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
        )

    @mcp.tool()
    def module_import_job_validate(
        campaign_id: str,
        job_id: str,
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Validate a staged module and preview scene/progress impact before importing it."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if not idempotency_key:
            raise ValueError("idempotency_key is required for module validation")
        job = require_import_job(campaign_id, job_id, "module")
        if job.state not in {"inspected", "validated", "failed"}:
            raise ValueError("module import job must be inspected before validation")
        payload = {"job_id": job_id, "operation": "validate"}
        scope = f"import-job:{campaign_id}:{job_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        preview = dict(job.inspection)
        diff = modules.diff_preview(
            campaign_id,
            source_key=str(job.payload.get("source_key") or job.artifact),
            preview=preview,
        )
        validation = {
            "valid": bool(preview.get("valid")),
            "errors": list(preview.get("errors") or []),
            "warnings": list(preview.get("warnings") or []),
            "preview": preview,
            "diff": diff,
        }
        updated = import_jobs.record_validation(
            job_id,
            validation,
            state="validated" if validation["valid"] else "failed",
        )
        response = {"job": asdict(updated), "validation": validation}
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
        )

    @mcp.tool()
    def module_import_job_import(
        campaign_id: str,
        job_id: str,
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Ingest a validated module inactive, preserving the current active module."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if not idempotency_key:
            raise ValueError("idempotency_key is required for module import")
        job = require_import_job(campaign_id, job_id, "module")
        if job.state not in {"validated", "imported"} or not job.validation.get("valid"):
            raise ValueError("module import job must pass validation before import")
        payload = {"job_id": job_id, "operation": "import"}
        scope = f"import-job:{campaign_id}:{job_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        values = dict(job.payload)
        embedder, vectors = storage.dense_components()
        result = modules.ingest_path(
            campaign_id=campaign_id,
            path=storage.artifact_module_path(job.artifact),
            source_key=str(values.get("source_key") or job.artifact),
            title=str(values.get("title") or job.artifact),
            parser=MarkdownModuleParser(profile=DndModuleProfile()),
            embedder=embedder,
            vector_store=vectors,
            activate=False,
            logical_source_key=str(values.get("source_key") or job.artifact),
        )
        updated = import_jobs.record_result(
            job_id,
            {**dict(job.result), "module_import": asdict(result)},
            state="imported",
            module_id=result.module_id,
        )
        response = {"job": asdict(updated), **asdict(result)}
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
        )

    @mcp.tool()
    def module_import_job_activate(
        campaign_id: str,
        job_id: str,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Atomically promote an imported module revision after its diff was reviewed."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        if dict(campaigns.get(campaign_id).state or {}).get("combat", {}).get("active", False):
            raise CombatEngineError("module activation cannot change during active combat")
        job = require_import_job(campaign_id, job_id, "module")
        if job.state not in {"imported", "activated"} or not job.module_id:
            raise ValueError("module import job must be imported before activation")
        payload = {"job_id": job_id, "operation": "activate", "module_id": job.module_id}
        scope = f"import-job:{campaign_id}:{job_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        before = campaigns.get(campaign_id)
        if before.revision != expected_revision:
            raise ValueError(f"campaign revision conflict: {campaign_id}")
        activation = modules.activate_candidate(campaign_id, job.module_id)
        state = dict(before.state or {})
        module_imports = dict(state.get("module_imports") or {})
        active_modules = dict(module_imports.get("active") or {})
        source_key = str(job.payload.get("source_key") or job.artifact)
        active_modules[source_key] = {
            "module_id": job.module_id,
            "checksum": job.artifact_checksum,
            "parser_profile": job.parser_profile,
            "parser_version": job.parser_version,
        }
        state["module_imports"] = {**module_imports, "active": active_modules}
        after = campaigns.update_audited(
            campaign_id,
            state=state,
            expected_revision=expected_revision,
            operation="module.import.activate",
            actor=principal_id,
            branch_id=current_branch_id(campaign_id),
            idempotency_key=idempotency_key,
            request_hash=request_hash(payload),
        )
        updated = import_jobs.record_result(
            job_id,
            {**dict(job.result), "activation": activation, "campaign_revision": after.revision},
            state="activated",
            module_id=job.module_id,
        )
        response = {
            "job": asdict(updated),
            "activation": activation,
            "campaign_revision": after.revision,
        }
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
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
        """Inspect a managed PDF/Markdown/text artifact before campaign import."""
        if not principal_id:
            raise PermissionError("authenticated caller identity is required for module artifacts")
        return modules.inspect_path(
            storage.artifact_module_path(artifact),
            parser=MarkdownModuleParser(profile=DndModuleProfile()),
        )

    @mcp.tool()
    def module_import_legacy(
        campaign_id: str,
        artifact: str,
        title: str | None = None,
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Import a managed module artifact into a campaign."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if not idempotency_key:
            raise ValueError("idempotency_key is required for module import")
        payload = {"artifact": artifact, "title": title}
        scope = f"module-import:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
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
        response = asdict(result)
        return remember_idempotent(
            scope,
            idempotency_key,
            payload,
            response,
            campaign_id=campaign_id,
        )

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

    def module_assets(
        campaign_id: str,
        module_id: str,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        return modules.list_assets(campaign_id, module_id)

    @mcp.tool()
    def module_page_render(
        campaign_id: str,
        module_id: str,
        page_number: int,
        source_asset_id: str | None = None,
        scale: float = 1.5,
        principal_id: str = "system:local",
    ) -> Any:
        """Render one imported PDF page as visual evidence for maps or handouts."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        assets = modules.list_assets(campaign_id, module_id)
        if source_asset_id:
            source_asset = modules.get_asset(campaign_id, source_asset_id)
            if source_asset["module_id"] != module_id:
                raise ValueError("source asset does not belong to module")
        else:
            candidates = [item for item in assets if item["media_type"] == "application/pdf"]
            if len(candidates) != 1:
                raise ValueError("source_asset_id is required unless the module has one PDF asset")
            source_asset = candidates[0]
        if source_asset["media_type"] != "application/pdf":
            raise ValueError("module page rendering requires a PDF source asset")
        rendered = render_pdf_page(source_asset["source_path"], page_number, scale=scale)
        if rendered.source_checksum != source_asset["checksum"]:
            raise RuntimeError("module PDF no longer matches its imported checksum")
        target = storage.store_rendered_module_page(
            module_id=module_id,
            source_checksum=rendered.source_checksum,
            page_number=rendered.page_number,
            scale=rendered.scale,
            checksum=rendered.checksum,
            content=rendered.content,
        )
        asset = modules.register_asset(
            campaign_id=campaign_id,
            module_id=module_id,
            source_path=str(target),
            media_type=rendered.media_type,
            checksum=rendered.checksum,
            metadata={
                "kind": "rendered_page",
                "derived_from_asset_id": source_asset["id"],
                "source_checksum": rendered.source_checksum,
                "source_page": rendered.page_number,
                "page_count": rendered.page_count,
                "width": rendered.width,
                "height": rendered.height,
                "scale": rendered.scale,
            },
        )
        return [
            {
                "campaign_id": campaign_id,
                "module_id": module_id,
                "asset": asset,
                "source_asset_id": source_asset["id"],
            },
            Image(path=target),
        ]

    @mcp.tool()
    def module_content_review(
        campaign_id: str,
        module_id: str,
        scene_id: str,
        content_key: str,
        normalized_content: str,
        source_asset_id: str,
        page_number: int,
        observation: str,
        content_kind: Literal["dnd5e_2014_statblock"] = "dnd5e_2014_statblock",
        metadata: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Validate and retain an executable transcription of image-only module content."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if not idempotency_key:
            raise ValueError("idempotency_key is required for module content review")
        parsed = parse_2014_statblock(
            normalized_content,
            source_key=f"module-review:{module_id}:{content_key}",
            name=None,
        )
        payload = {
            "module_id": module_id,
            "scene_id": scene_id,
            "content_key": content_key,
            "content_kind": content_kind,
            "normalized_content": normalized_content,
            "source_asset_id": source_asset_id,
            "page_number": page_number,
            "observation": observation,
            "metadata": metadata,
        }
        scope = f"module-content-review:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        review = modules.review_content(
            campaign_id=campaign_id,
            module_id=module_id,
            scene_id=scene_id,
            content_key=content_key,
            content_kind=content_kind,
            normalized_content=normalized_content,
            source_asset_id=source_asset_id,
            page_number=page_number,
            reviewer=principal_id,
            observation=observation,
            metadata=metadata,
        )
        response = {
            "review": review,
            "validation": {
                "name": parsed.name,
                "challenge_rating": parsed.challenge_rating,
                "experience_points": parsed.experience_points,
                "warnings": list(parsed.warnings),
                "settlement": "automatic" if not parsed.warnings else "mixed",
            },
        }
        return remember_idempotent(
            scope,
            idempotency_key,
            payload,
            response,
            campaign_id=campaign_id,
        )

    @mcp.tool()
    def module_read_scene(
        campaign_id: str,
        scene_id: str,
        scope_id: str = "party",
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Read one full scene, including its structured rooms and visibility metadata."""
        membership = access.require_campaign(campaign_id, principal_id)
        result = modules.read_scene(campaign_id, scene_id, scope_id=scope_id)
        visibility = result.get("visibility", "keeper")
        if membership.role in {"owner", "dm"} or visibility in {"public", "party"}:
            return result
        return {
            "campaign_id": campaign_id,
            "scene_id": scene_id,
            "redacted": True,
            "content": "[DM-only scene content hidden]",
        }

    def module_scene_readiness(
        campaign_id: str,
        scene_id: str,
        participant_manifest: dict[str, Any],
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Validate source-grounded combatants and reserves before an encounter starts."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if not isinstance(participant_manifest, dict):
            raise ValueError("participant_manifest must be an object")
        unknown_manifest = set(participant_manifest) - {"schema_version", "groups", "notes"}
        if unknown_manifest:
            raise ValueError(f"unsupported participant manifest fields: {sorted(unknown_manifest)}")
        schema_version = participant_manifest.get("schema_version", 1)
        if schema_version != 1:
            raise ValueError("participant_manifest schema_version must be 1")
        groups = participant_manifest.get("groups")
        if not isinstance(groups, list):
            raise ValueError("participant_manifest.groups must be a list")
        encounter_scene = modules.read_scene(campaign_id, scene_id)
        module_id = str(encounter_scene["module_id"])
        normalized_groups: list[dict[str, Any]] = []
        group_keys: set[str] = set()
        used_actor_ids: set[str] = set()
        initial_actor_ids: list[str] = []
        reinforcement_actor_ids: list[str] = []
        optional_actor_ids: list[str] = []

        for index, raw_group in enumerate(groups):
            if not isinstance(raw_group, dict):
                raise ValueError("each participant manifest group must be an object")
            allowed = {
                "key",
                "label",
                "role",
                "required_count",
                "actor_ids",
                "source_scene_id",
                "source_excerpt",
            }
            unknown = set(raw_group) - allowed
            if unknown:
                raise ValueError(f"unsupported participant group fields: {sorted(unknown)}")
            key = str(raw_group.get("key") or "").strip()
            if not key or key in group_keys:
                raise ValueError("participant manifest group keys must be non-empty and unique")
            group_keys.add(key)
            role = str(raw_group.get("role") or "").strip()
            if role not in {"combatant", "reinforcement", "optional"}:
                raise ValueError(
                    "participant manifest role must be combatant, reinforcement, or optional"
                )
            required_count = raw_group.get("required_count")
            if (
                isinstance(required_count, bool)
                or not isinstance(required_count, int)
                or required_count < 1
            ):
                raise ValueError("participant group required_count must be a positive integer")
            actor_ids_value = raw_group.get("actor_ids", [])
            if not isinstance(actor_ids_value, list):
                raise ValueError("participant group actor_ids must be a list")
            actor_ids = [str(item).strip() for item in actor_ids_value]
            if any(not item for item in actor_ids) or len(actor_ids) != len(set(actor_ids)):
                raise ValueError("participant group actor_ids must be non-empty and unique")
            overlap = used_actor_ids & set(actor_ids)
            if overlap:
                raise ValueError(
                    "actors cannot appear in multiple participant groups: "
                    + ", ".join(sorted(overlap))
                )
            if len(actor_ids) > required_count:
                raise ValueError(f"participant group {key!r} exceeds required_count")
            used_actor_ids.update(actor_ids)

            source_scene_id = str(raw_group.get("source_scene_id") or scene_id)
            source_scene = (
                encounter_scene
                if source_scene_id == scene_id
                else modules.read_scene(campaign_id, source_scene_id)
            )
            if str(source_scene.get("module_id")) != module_id:
                raise ValueError("participant evidence must belong to the encounter module")
            source_excerpt = " ".join(str(raw_group.get("source_excerpt") or "").split()).strip()
            if len(source_excerpt) < 8 or len(source_excerpt) > 500:
                raise ValueError("participant source_excerpt must contain 8 to 500 characters")
            normalized_content = " ".join(str(source_scene.get("content") or "").split())
            if source_excerpt.casefold() not in normalized_content.casefold():
                raise ValueError(
                    f"participant group {key!r} source_excerpt is not present in its scene"
                )

            actor_views = []
            for actor_id in actor_ids:
                actor = require_campaign_actor(campaign_id, actor_id)
                actor_views.append(
                    {
                        "id": actor.id,
                        "name": actor.name,
                        "character_type": actor.character_type,
                        "combat_card": combat_card_readiness(actor),
                    }
                )
            unready_actor_ids = [
                str(item["id"])
                for item in actor_views
                if not dict(item.get("combat_card") or {}).get("ready", False)
            ]
            missing_count = required_count - len(actor_ids)
            blocking = role != "optional"
            normalized_groups.append(
                {
                    "key": key,
                    "label": str(raw_group.get("label") or key).strip(),
                    "role": role,
                    "required_count": required_count,
                    "actor_ids": actor_ids,
                    "actors": actor_views,
                    "missing_count": missing_count,
                    "unready_count": len(unready_actor_ids),
                    "unready_actor_ids": unready_actor_ids,
                    "blocking": blocking,
                    "source_scene_id": source_scene_id,
                    "source_excerpt": source_excerpt,
                    "ordinal": index,
                }
            )
            target = (
                initial_actor_ids
                if role == "combatant"
                else reinforcement_actor_ids
                if role == "reinforcement"
                else optional_actor_ids
            )
            target.extend(actor_ids)

        ready = all(
            not item["blocking"] or (item["missing_count"] == 0 and item["unready_count"] == 0)
            for item in normalized_groups
        )
        complete = all(
            item["missing_count"] == 0 and item["unready_count"] == 0 for item in normalized_groups
        )
        normalized_manifest = {
            "schema_version": 1,
            "scene_id": scene_id,
            "module_id": module_id,
            "groups": normalized_groups,
            "notes": str(participant_manifest.get("notes") or "").strip(),
        }
        return {
            **normalized_manifest,
            "checksum": request_hash(normalized_manifest),
            "ready": ready,
            "complete": complete,
            "initial_actor_ids": initial_actor_ids,
            "reinforcement_actor_ids": reinforcement_actor_ids,
            "optional_actor_ids": optional_actor_ids,
        }

    @mcp.tool()
    def module_current(
        campaign_id: str,
        scope_id: str = "party",
        principal_id: str = "system:local",
    ) -> dict[str, Any] | None:
        """Read the current scene for party, group, or player scope with party fallback."""
        membership = access.require_campaign(campaign_id, principal_id)
        resolved_scope_id = readable_scene_scope(campaign_id, scope_id, principal_id)
        result = modules.current_scene(campaign_id, scope_id=resolved_scope_id)
        if result is None or membership.role in {"owner", "dm"}:
            return result
        if result.get("visibility", "keeper") in {"public", "party"}:
            return result
        return {
            "campaign_id": campaign_id,
            "redacted": True,
            "content": "[DM-only scene content hidden]",
        }

    def module_progress_index(
        campaign_id: str,
        scope_id: str = "party",
        module_id: str | None = None,
        principal_id: str = "system:local",
    ) -> list[dict[str, Any]]:
        """Project ordered scene progress without adding another public MCP tool."""
        membership = access.require_campaign(campaign_id, principal_id)
        resolved_scope_id = readable_scene_scope(campaign_id, scope_id, principal_id)
        result = modules.scene_progress_index(
            campaign_id,
            scope_id=resolved_scope_id,
            module_id=module_id,
        )
        if membership.role in {"owner", "dm"}:
            return result
        visible_scene_ids = {
            item["scene_id"] for item in module_index(campaign_id, module_id, principal_id)
        }
        return [item for item in result if item["scene_id"] in visible_scene_ids]

    @mcp.tool()
    def module_set_progress(
        campaign_id: str,
        scene_id: str,
        scope_id: str = "party",
        status: str | None = None,
        progress: int | None = None,
        state: dict[str, Any] | None = None,
        current_room: str | None = None,
        current_location_key: str | None = None,
        principal_id: str = "system:local",
        expected_state_version: int | None = None,
        idempotency_key: str | None = None,
        spatial_review: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist scoped progress or a source-backed visual atlas review."""
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
            "current_location_key": current_location_key,
            "expected_state_version": expected_state_version,
            "branch_id": branch_id,
            "spatial_review": spatial_review,
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
            current_location_key=current_location_key,
            expected_state_version=expected_state_version,
            spatial_review=(
                {
                    **dict(spatial_review),
                    "reviewer": principal_id,
                    "branch_id": branch_id,
                }
                if spatial_review is not None
                else None
            ),
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
    def rule_document_stage(
        campaign_id: str,
        source_path: str,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Stage an allowlisted PDF/Markdown/text rulebook in MCP-owned storage."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        return storage.stage_rulebook(source_path)

    @mcp.tool()
    def rule_document_inspect(
        campaign_id: str,
        artifact: str,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Run Core document normalization and report structure/warnings without importing."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        return rules.inspect_path(storage.artifact_rulebook_path(artifact))

    @mcp.tool()
    def rule_document_import(
        campaign_id: str,
        artifact: str,
        source_key: str,
        title: str,
        edition: str,
        locale: str = "en",
        publication_id: str = "",
        version: str = "",
        authority: str = "supplement",
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Import a staged rulebook through Core's shared structured parser and index."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if edition not in {"2014", "2024"}:
            raise ValueError("imported D&D rulebooks require edition 2014 or 2024")
        if not idempotency_key:
            raise ValueError("idempotency_key is required for rulebook import")
        payload = {
            "artifact": artifact,
            "source_key": source_key,
            "title": title,
            "edition": edition,
            "locale": locale,
            "publication_id": publication_id,
            "version": version,
            "authority": authority,
        }
        scope = f"rule-document-import:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        path = storage.artifact_rulebook_path(artifact)
        embedder, vectors = storage.dense_components()
        result = rules.ingest_path(
            system_id="dnd5e",
            path=path,
            source_key=source_key,
            title=title,
            locale=locale,
            edition=edition,
            publication_id=publication_id,
            version=version,
            authority=authority,
            embedder=embedder,
            vector_store=vectors,
        )
        source = rules.source(result.source_id)
        source_metadata = dict(source.get("metadata") or {})
        response = {
            **asdict(result),
            "artifact": artifact,
            "source_checksum": source_metadata.get("source_checksum"),
            "page_count": source_metadata.get("page_count"),
            "warnings": list(source_metadata.get("warnings") or []),
            "metadata": {
                key: value
                for key, value in source_metadata.items()
                if key not in {"source_path", "warnings", "source_checksum", "page_count"}
            },
        }
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
        )

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
    def rule_pack_draft(
        manifest: dict[str, Any],
        artifacts: list[dict[str, Any]] | None = None,
        mechanics: list[dict[str, Any]] | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or replace an inactive draft and validate its safe D&D mechanic IR."""
        return save_rule_pack_draft(
            manifest=manifest,
            artifacts=artifacts,
            mechanics=mechanics,
            provenance=provenance,
        )

    @mcp.tool()
    def rule_pack_draft_from_source(
        source_id: str,
        manifest: dict[str, Any],
        artifacts: list[dict[str, Any]] | None = None,
        mechanics: list[dict[str, Any]] | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Draft a pack whose citations are resolved from imported rule chunks."""
        source = rules.source(source_id)
        if source["system_id"] != "dnd5e":
            raise ValueError("rule source is not a D&D source")
        if str(manifest.get("system_id") or "") != "dnd5e":
            raise ValueError("source-bound D&D packs require manifest.system_id=dnd5e")
        editions = [str(item) for item in manifest.get("editions", [])]
        if not editions:
            raise ValueError("source-bound D&D packs must declare at least one edition")
        if str(source.get("edition") or "") not in editions:
            raise ValueError("rule source edition must be declared by the pack manifest")
        bound_mechanics: list[dict[str, Any]] = []
        for mechanic in mechanics or []:
            value = deepcopy(mechanic)
            supplied = list(value.get("citations") or [])
            if not supplied:
                raise ValueError("every executable mechanic requires an imported chunk citation")
            citations: list[dict[str, Any]] = []
            for citation in supplied:
                if not isinstance(citation, dict) or not citation.get("chunk_id"):
                    raise ValueError("source-bound citations require chunk_id")
                resolved = rules.citation(str(citation["chunk_id"]), source_id=source_id)
                note = citation.get("note")
                if note:
                    resolved["note"] = str(note)
                citations.append(resolved)
            value["citations"] = citations
            bound_mechanics.append(value)
        bound_artifacts: list[dict[str, Any]] = []
        for artifact in artifacts or []:
            value = deepcopy(artifact)
            chunk_ids = list(value.pop("source_chunk_ids", []) or [])
            if not chunk_ids:
                raise ValueError("source-bound artifacts require source_chunk_ids")
            citations = [
                rules.citation(str(chunk_id), source_id=source_id) for chunk_id in chunk_ids
            ]
            value["rule_refs"] = [
                f"{citation['source']}#chunk:{citation['chunk_id']}" for citation in citations
            ]
            value["source_citations"] = citations
            bound_artifacts.append(value)
        source_metadata = dict(source.get("metadata") or {})
        bound_provenance = {
            **dict(provenance or {}),
            "rule_source": {
                "source_id": source["id"],
                "source_key": source["source_key"],
                "title": source["title"],
                "edition": source["edition"],
                "publication_id": source["publication_id"],
                "normalized_checksum": source["checksum"],
                "source_checksum": source_metadata.get("source_checksum", source["checksum"]),
                "page_count": source_metadata.get("page_count"),
                "warnings": list(source_metadata.get("warnings") or []),
            },
        }
        validate_source_bound_mechanics(bound_mechanics, source_id=source_id)
        return save_rule_pack_draft(
            manifest=manifest,
            artifacts=bound_artifacts,
            mechanics=bound_mechanics,
            provenance=bound_provenance,
        )

    @mcp.tool()
    def rule_import_job_compile(
        campaign_id: str,
        job_id: str,
        manifest: dict[str, Any],
        mechanics: list[dict[str, Any]] | None = None,
        provenance: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Compile accepted candidates; incomplete cards remain catalog-only."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if not idempotency_key:
            raise ValueError("idempotency_key is required for pack compilation")
        job = require_import_job(campaign_id, job_id, "rulebook")
        if not job.source_id:
            raise ValueError("rule import job must be indexed before pack compilation")
        if job.state not in {"reviewed", "compiled", "validated", "failed"}:
            raise ValueError("all content candidates must be reviewed before pack compilation")
        pack_id = str(manifest.get("id") or "").strip()
        if not pack_id:
            raise ValueError("manifest.id is required")
        payload = {
            "job_id": job_id,
            "operation": "compile",
            "manifest": manifest,
            "mechanics": mechanics or [],
            "provenance": provenance or {},
        }
        scope = f"import-job:{campaign_id}:{job_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        artifacts = compiled_artifacts_from_candidates(job.candidates, pack_id=pack_id)
        draft = rule_pack_draft_from_source(
            source_id=job.source_id,
            manifest=manifest,
            artifacts=artifacts,
            mechanics=mechanics,
            provenance={**dict(provenance or {}), "import_job_id": job_id},
        )
        state = "compiled" if draft["status"] == "validated" else "failed"
        updated = import_jobs.record_validation(
            job_id,
            {"draft": draft, "accepted_artifact_count": len(artifacts)},
            state=state,
        )
        response = {"job": asdict(updated), "draft": draft}
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
        )

    @mcp.tool()
    def rule_import_job_install(
        campaign_id: str,
        job_id: str,
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Install the exact validated pack compiled by an import job without enabling it."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if not idempotency_key:
            raise ValueError("idempotency_key is required for pack installation")
        job = require_import_job(campaign_id, job_id, "rulebook")
        draft = dict(job.validation.get("draft") or {})
        if job.state not in {"compiled", "installed"} or draft.get("status") != "validated":
            raise ValueError("import job has no validated pack draft to install")
        pack_id = str(draft.get("pack_id") or "")
        version = str(draft.get("version") or "")
        payload = {"job_id": job_id, "operation": "install", "pack_id": pack_id, "version": version}
        scope = f"import-job:{campaign_id}:{job_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        installed = asdict(rule_packs.install(pack_id, version))
        updated = import_jobs.record_result(
            job_id,
            {**dict(job.result), "installed_pack": installed},
            state="installed",
            source_id=job.source_id,
        )
        response = {"job": asdict(updated), "installed": installed}
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
        )

    @mcp.tool()
    def rule_import_job_activate(
        campaign_id: str,
        job_id: str,
        principal_id: str = "system:local",
        branch_id: str | None = None,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Enable an installed imported pack only through the checked-out branch lock."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        job = require_import_job(campaign_id, job_id, "rulebook")
        installed = dict(job.result.get("installed_pack") or {})
        if job.state not in {"installed", "activated"}:
            raise ValueError("import job must install its pack before activation")
        pack_id = str(installed.get("pack_id") or "")
        version = str(installed.get("version") or "")
        payload = {
            "job_id": job_id,
            "operation": "activate",
            "pack_id": pack_id,
            "version": version,
            "branch_id": branch_id,
        }
        scope = f"import-job:{campaign_id}:{job_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        activation = campaign_rule_pack_set(
            campaign_id=campaign_id,
            pack_id=pack_id,
            version=version,
            principal_id=principal_id,
            branch_id=branch_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )
        updated = import_jobs.record_result(
            job_id,
            {**dict(job.result), "activation": activation},
            state="activated",
            source_id=job.source_id,
        )
        response = {"job": asdict(updated), "activation": activation}
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
        )

    @mcp.tool()
    def rule_pack_install(pack_id: str, version: str) -> dict[str, Any]:
        """Install one validated immutable version without enabling it for a campaign."""
        return asdict(rule_packs.install(pack_id, version))

    @mcp.tool()
    def rule_pack_list(pack_id: str | None = None) -> list[dict[str, Any]]:
        """List draft, rejected, validated, and installed rule-pack versions."""
        return [asdict(item) for item in rule_packs.list_versions(pack_id)]

    @mcp.tool()
    def rule_pack_inspect(pack_id: str, version: str) -> dict[str, Any]:
        """Inspect an exact draft or installed version, including validation evidence."""
        return asdict(rule_packs.get_version(pack_id, version))

    @mcp.tool()
    def rule_pack_test(pack_id: str, version: str) -> dict[str, Any]:
        """Run declarative positive/negative examples embedded in a pack manifest."""
        value = rule_packs.get_version(pack_id, version)
        return run_mechanic_tests(
            value.mechanics,
            list(value.manifest.get("tests") or []),
            fingerprint=value.checksum,
        )

    @mcp.tool()
    def rule_pack_remove(pack_id: str, version: str) -> dict[str, Any]:
        """Remove an unreferenced version; any branch lock makes removal fail closed."""
        rule_packs.remove_version(pack_id, version)
        return {"status": "removed", "pack_id": pack_id, "version": version}

    @mcp.tool()
    def campaign_rule_profile_get(
        campaign_id: str, principal_id: str = "system:local"
    ) -> dict[str, Any] | None:
        """Read the campaign edition/publication profile and exact branch-local pack lock."""
        access.require_campaign(campaign_id, principal_id)
        profile = rule_profiles.get(campaign_id)
        campaign = campaigns.get(campaign_id)
        try:
            effective = effective_ruleset_view(campaign_id)
            effective_error = None
        except RulePackError as error:
            effective = None
            effective_error = str(error)
        return {
            "profile": asdict(profile) if profile else None,
            "activations": [asdict(item) for item in rule_packs.activations(campaign_id)],
            "effective": effective,
            "effective_error": effective_error,
            "campaign_revision": campaign.revision,
        }

    @mcp.tool()
    def campaign_rule_profile_set(
        campaign_id: str,
        edition: str,
        locale: str = "en",
        publications: list[str] | None = None,
        options: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Set non-executable edition/publication metadata outside active combat."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        payload = {
            "edition": edition,
            "locale": locale,
            "publications": publications or [],
            "options": options or {},
        }
        scope = f"campaign-rule-profile:{campaign_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        if dict(campaigns.get(campaign_id).state or {}).get("combat", {}).get("active", False):
            raise CombatEngineError("rule profile cannot change during active combat")
        rule_packs.assert_edition_compatible(campaign_id, edition)
        profile = rule_profiles.set(
            campaign_id,
            edition=edition,
            locale=locale,
            publications=publications,
            options=profile_options_with_core_lock(edition, options),
            expected_campaign_revision=expected_revision,
        )
        response = {
            "profile": asdict(profile),
            "campaign_revision": mutation_revision(campaign_id),
        }
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
        )

    @mcp.tool()
    def campaign_core_relock(
        campaign_id: str,
        expected_core_fingerprint: str,
        reason: str,
        principal_id: str = "system:local",
        branch_id: str | None = None,
        expected_revision: int | None = None,
        expected_head_snapshot_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Explicitly adopt the current built-in Core after a checkpointed runtime upgrade."""

        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        if not expected_head_snapshot_id:
            raise ValueError("expected_head_snapshot_id is required for a Core relock")
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise ValueError("reason is required for a Core relock")
        if len(normalized_reason) > 500:
            raise ValueError("Core relock reason exceeds 500 characters")
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        payload = {
            "expected_core_fingerprint": expected_core_fingerprint,
            "reason": normalized_reason,
            "branch_id": resolved_branch_id,
            "expected_head_snapshot_id": expected_head_snapshot_id,
        }
        scope = f"campaign-core-relock:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        branch = branches.current(campaign_id)
        if branch.id != resolved_branch_id or branch.head_snapshot_id != expected_head_snapshot_id:
            raise ValueError("current branch head changed before Core relock")
        profile = rule_profiles.get(campaign_id)
        if profile is None:
            raise RulePackError("campaign has no rule profile to relock")
        options = dict(profile.options or {})
        previous = dict(options.get("_core_rule_pack_lock") or {})
        if previous.get("fingerprint") != expected_core_fingerprint:
            raise ValueError("expected_core_fingerprint does not match the campaign lock")
        latest = get_core_rule_pack(profile.edition)
        user_options = {
            key: value for key, value in options.items() if key != "_core_rule_pack_lock"
        }
        updated = rule_profiles.set(
            campaign_id,
            edition=profile.edition,
            locale=profile.locale,
            publications=list(profile.publications),
            options=profile_options_with_core_lock(profile.edition, user_options),
            expected_campaign_revision=expected_revision,
        )
        response = {
            "status": "relocked",
            "reason": normalized_reason,
            "previous_core_pack": previous,
            "core_pack": {
                "id": latest.id,
                "version": latest.version,
                "edition": latest.edition,
                "fingerprint": latest.fingerprint,
            },
            "profile": asdict(updated),
            "branch_id": resolved_branch_id,
            "checkpoint_snapshot_id": expected_head_snapshot_id,
            "campaign_revision": mutation_revision(campaign_id),
        }
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
        )

    @mcp.tool()
    def campaign_rule_pack_set(
        campaign_id: str,
        pack_id: str,
        version: str,
        enabled: bool = True,
        options: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        branch_id: str | None = None,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Explicitly pin and enable/disable an installed pack on one campaign branch."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        payload = {
            "pack_id": pack_id,
            "version": version,
            "enabled": enabled,
            "options": options or {},
            "branch_id": resolved_branch_id,
        }
        scope = f"campaign-rule-pack-set:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        activation = rule_packs.set_activation(
            campaign_id,
            pack_id=pack_id,
            version=version,
            enabled=enabled,
            options=options,
            branch_id=resolved_branch_id,
            expected_campaign_revision=expected_revision,
        )
        response = {
            "activation": asdict(activation),
            "effective": effective_ruleset_view(campaign_id, branch_id=activation.branch_id),
            "campaign_revision": mutation_revision(campaign_id),
        }
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
        )

    @mcp.tool()
    def campaign_rule_pack_remove(
        campaign_id: str,
        pack_id: str,
        principal_id: str = "system:local",
        branch_id: str | None = None,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Remove a future branch-local activation while preserving historical receipts."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        require_write_contract(expected_revision, idempotency_key)
        resolved_branch_id = require_current_branch(campaign_id, branch_id)
        payload = {"pack_id": pack_id, "branch_id": resolved_branch_id}
        scope = f"campaign-rule-pack-remove:{campaign_id}:{resolved_branch_id}:{principal_id}"
        replay = replay_idempotent(scope, idempotency_key, payload)
        if replay is not None:
            return replay
        rule_packs.remove_activation(
            campaign_id,
            pack_id,
            branch_id=resolved_branch_id,
            expected_campaign_revision=expected_revision,
        )
        response = {
            "effective": effective_ruleset_view(campaign_id, branch_id=resolved_branch_id),
            "campaign_revision": mutation_revision(campaign_id),
        }
        return remember_idempotent(
            scope, idempotency_key, payload, response, campaign_id=campaign_id
        )

    @mcp.tool()
    def campaign_rules_explain(
        campaign_id: str,
        event: str | None = None,
        principal_id: str = "system:local",
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        """Explain the exact lock, fingerprint, and source-cited mechanics used for settlement."""
        access.require_campaign(campaign_id, principal_id)
        effective = rule_packs.effective_ruleset(campaign_id, branch_id=branch_id)
        context = effective_rule_context(campaign_id, branch_id=branch_id)
        mechanics = [
            asdict(item) for item in context.mechanics if event is None or item.event == event
        ]
        return {
            "campaign_id": campaign_id,
            "branch_id": effective.branch_id,
            "fingerprint": context.fingerprint,
            "core_pack": {
                "id": context.core_pack.id,
                "version": context.core_pack.version,
                "edition": context.core_pack.edition,
                "fingerprint": context.core_pack.fingerprint,
            },
            "core_boundaries": [asdict(item) for item in context.core_pack.boundaries],
            "lock": list(effective.lock),
            "mechanics": mechanics,
            "coverage": sorted({item.event for item in context.mechanics}),
        }

    @mcp.tool()
    def campaign_rule_receipts(
        campaign_id: str,
        principal_id: str = "system:local",
        branch_id: str | None = None,
        mechanic_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read immutable historical rule evidence for committed settlements."""
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        resolved_branch_id = readable_branch(campaign_id, branch_id, principal_id)
        return [
            asdict(item)
            for item in rule_receipts.list(
                campaign_id,
                branch_id=resolved_branch_id,
                mechanic_id=mechanic_id,
                limit=limit,
            )
        ]

    def available_content_artifacts(
        campaign_id: str, *, kind: str | None = None, branch_id: str | None = None
    ) -> list[tuple[str, str, dict[str, Any]]]:
        profile = rule_profiles.get(campaign_id)
        values: list[tuple[str, str, dict[str, Any]]] = []
        if profile and profile.edition == "2014":
            try:
                core = rule_packs.get_version(CORE_CONTENT_PACK_ID, CORE_CONTENT_PACK_VERSION)
            except LookupError:
                # A headless server may intentionally run without the bundled
                # skill repository. Enabled user packs must remain usable.
                core = None
            if core is not None:
                values.extend((core.pack_id, core.version, dict(item)) for item in core.artifacts)
        for activation in rule_packs.activations(campaign_id, branch_id=branch_id):
            if not activation.enabled:
                continue
            pack = rule_packs.get_version(activation.pack_id, activation.version)
            values.extend((pack.pack_id, pack.version, dict(item)) for item in pack.artifacts)
        return [item for item in values if kind is None or item[2].get("kind") == kind]

    def level_advancement_content_context(
        campaign_id: str,
        sheet: dict[str, Any],
        *,
        class_name: str,
        new_level: int,
        branch_id: str,
    ) -> dict[str, Any]:
        """Resolve source-bound per-level modifiers and post-level catalog work."""
        candidates = available_content_artifacts(campaign_id, branch_id=branch_id)

        def exact_recorded_artifact(record: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
            artifact_id = str(record.get("artifact_id") or record.get("id") or "")
            pack_id = str(record.get("pack_id") or "")
            version = str(record.get("pack_version") or "")
            if not artifact_id or not pack_id or not version:
                raise ValueError(
                    "level-affecting content must record artifact id, pack id, and pack version"
                )
            try:
                pack = rule_packs.get_version(pack_id, version)
            except LookupError as error:
                raise RulesetUnavailableError(
                    f"recorded content pack is unavailable: {pack_id}@{version}"
                ) from error
            artifact = next(
                (item for item in pack.artifacts if str(item.get("id") or "") == artifact_id),
                None,
            )
            if artifact is None:
                embedded = next(
                    (
                        feature
                        for item in pack.artifacts
                        for feature in dict(dict(item.get("card") or {}).get("grants") or {}).get(
                            "features", []
                        )
                        if str(feature.get("id") or "") == artifact_id
                    ),
                    None,
                )
                if embedded is not None:
                    artifact = {"id": artifact_id, "kind": "feature", "card": embedded}
            if artifact is None:
                raise RulesetUnavailableError(
                    f"recorded artifact is unavailable: {artifact_id} in {pack_id}@{version}"
                )
            return pack_id, version, dict(artifact)

        hp_per_level_bonus = 0
        hp_bonus_sources: list[dict[str, Any]] = []
        for selection in sheet.get("content", {}).get("selections", []):
            artifact_id = str(selection.get("artifact_id") or "")
            if not artifact_id:
                continue
            pack_id, version, artifact = exact_recorded_artifact(selection)
            card = dict(artifact.get("card") or {})
            grants = dict(card.get("grants") or {})
            amount = int(grants.get("hp_per_level", 0) or 0)
            if amount:
                hp_per_level_bonus += amount
                hp_bonus_sources.append(
                    {
                        "artifact_id": artifact_id,
                        "pack_id": pack_id,
                        "pack_version": version,
                        "amount": amount,
                        "scope": "character_level",
                    }
                )
        feature_records = list(sheet.get("content", {}).get("features", []))
        present_features = {str(item.get("id") or "") for item in feature_records}
        for feature in feature_records:
            artifact_id = str(feature.get("id") or "")
            if not artifact_id or not feature.get("pack_id") or not feature.get("pack_version"):
                continue
            pack_id, version, artifact = exact_recorded_artifact(feature)
            card = dict(artifact.get("card") or {})
            grants = dict(card.get("mechanical_grants") or {})
            amount = int(grants.get("hp_per_level", 0) or 0)
            if str(card.get("class_name") or "").casefold() == class_name.casefold():
                amount += int(grants.get("hp_per_class_level", 0) or 0)
            if amount:
                hp_per_level_bonus += amount
                hp_bonus_sources.append(
                    {
                        "artifact_id": artifact_id,
                        "pack_id": pack_id,
                        "pack_version": version,
                        "amount": amount,
                        "scope": "class_level",
                    }
                )

        target_class = next(
            item
            for item in sheet["progression"]["classes"]
            if str(item.get("name") or "").casefold() == class_name.casefold()
        )
        subclass_name = str(target_class.get("subclass") or "")
        feature_options: list[dict[str, Any]] = []
        subclass_options: list[dict[str, Any]] = []
        for pack_id, version, artifact in candidates:
            if str(artifact.get("application_state") or "selection_ready") != "selection_ready":
                continue
            artifact_id = str(artifact.get("id") or "")
            card = dict(artifact.get("card") or {})
            kind = str(artifact.get("kind") or "")
            declared_class = str(card.get("class_name") or "")
            minimum_level = int(card.get("minimum_level", 1) or 1)
            if declared_class.casefold() != class_name.casefold() or minimum_level > new_level:
                continue
            if kind == "feature" and artifact_id not in present_features:
                declared_subclass = str(card.get("subclass_name") or "")
                if declared_subclass and declared_subclass.casefold() != subclass_name.casefold():
                    continue
                feature_options.append(
                    {
                        "artifact_id": artifact_id,
                        "name": str(card.get("name") or artifact_id),
                        "minimum_level": minimum_level,
                        "class_name": declared_class,
                        "subclass_name": declared_subclass,
                        "selection_requirements": deepcopy(
                            dict(card.get("selection_requirements") or {})
                        ),
                        "pack_id": pack_id,
                        "pack_version": version,
                        "rule_refs": list(artifact.get("rule_refs") or []),
                    }
                )
            if kind == "subclass" and not subclass_name:
                subclass_options.append(
                    {
                        "artifact_id": artifact_id,
                        "name": str(card.get("name") or artifact_id),
                        "minimum_level": minimum_level,
                        "pack_id": pack_id,
                        "pack_version": version,
                        "rule_refs": list(artifact.get("rule_refs") or []),
                    }
                )
        return {
            "hp_per_level_bonus": hp_per_level_bonus,
            "hp_bonus_sources": hp_bonus_sources,
            "feature_options": sorted(
                feature_options,
                key=lambda item: (item["minimum_level"], item["name"], item["artifact_id"]),
            ),
            "subclass_options": sorted(
                subclass_options, key=lambda item: (item["name"], item["artifact_id"])
            ),
        }

    @mcp.tool()
    def content_catalog_list(
        campaign_id: str,
        kind: str | None = None,
        query: str = "",
        principal_id: str = "system:local",
        branch_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List core and enabled-extension character options from one uniform catalog."""
        access.require_campaign(campaign_id, principal_id)
        resolved_branch_id = readable_branch(campaign_id, branch_id, principal_id)
        lowered = query.casefold().strip()
        result = []
        for pack_id, version, artifact in available_content_artifacts(
            campaign_id, kind=kind, branch_id=resolved_branch_id
        ):
            card = dict(artifact.get("card") or {})
            name = str(card.get("name") or artifact["id"])
            if (
                lowered
                and lowered not in name.casefold()
                and lowered not in str(artifact["id"]).casefold()
            ):
                continue
            artifact_kind = str(artifact.get("kind") or "")
            selection_requirements: dict[str, Any] = {}
            if artifact_kind == "spell":
                selection_requirements = {
                    "fields": ["source_class", "method"],
                    "level": int(card.get("level", 0) or 0),
                    "eligible_classes": list(card.get("classes") or []),
                    "methods": [
                        "known",
                        "spellbook",
                        "spellbook_copy",
                        "class_prepared",
                    ],
                    "spellbook_copy_fields": [
                        "source_owner",
                        "source_item_id",
                        "payment_owner",
                        "payment",
                    ],
                }
            elif artifact_kind == "subclass":
                selection_requirements = {
                    "fields": ["target_class_name"],
                    "class_name": str(card.get("class_name") or ""),
                    "minimum_level": int(card.get("minimum_level", 1) or 1),
                }
            elif artifact_kind == "background":
                grants = dict(card.get("background_grants") or {})
                choices = dict(grants.get("choices") or {})
                selection_requirements = {
                    "fields": ["languages"] if choices.get("language_count") else [],
                    "language_count": int(choices.get("language_count", 0) or 0),
                    "skill_proficiencies": list(card.get("skill_proficiencies") or []),
                }
            elif artifact_kind == "feat":
                selection_requirements = {
                    "fields": [],
                    "prerequisites": deepcopy(list(card.get("prerequisites") or [])),
                }
            elif artifact_kind == "feature":
                requirements = deepcopy(dict(card.get("selection_requirements") or {}))
                selection_requirements = {
                    "fields": [requirements["field"]] if requirements.get("field") else [],
                    "class_name": str(card.get("class_name") or ""),
                    "subclass_name": str(card.get("subclass_name") or ""),
                    "minimum_level": int(card.get("minimum_level", 1) or 1),
                    **requirements,
                }
            elif artifact_kind == "species":
                grants = dict(card.get("grants") or {})
                fields = []
                if int(grants.get("language_choice_count", 0) or 0):
                    fields.append("languages")
                if int(grants.get("skill_choice_count", 0) or 0):
                    fields.append("skills")
                if list(grants.get("tool_choices") or []):
                    fields.append("tools")
                if int(dict(grants.get("ability_choice") or {}).get("count", 0) or 0):
                    fields.append("abilities")
                if grants.get("cantrip_choice"):
                    fields.append("cantrip_artifact_id")
                selection_requirements = {
                    "fields": fields,
                    "base_species": str(card.get("base_species") or card.get("name") or ""),
                    "language_count": int(grants.get("language_choice_count", 0) or 0),
                    "skill_count": int(grants.get("skill_choice_count", 0) or 0),
                    "tool_options": list(grants.get("tool_choices") or []),
                    "ability_choice": deepcopy(dict(grants.get("ability_choice") or {})),
                    "cantrip_choice": deepcopy(grants.get("cantrip_choice")),
                }
            result.append(
                {
                    "id": artifact["id"],
                    "kind": artifact_kind,
                    "name": name,
                    "pack_id": pack_id,
                    "pack_version": version,
                    "rule_refs": list(artifact.get("rule_refs") or []),
                    "mechanic_refs": list(artifact.get("mechanic_refs") or []),
                    "source_citations": deepcopy(list(artifact.get("source_citations") or [])),
                    "selection_requirements": selection_requirements,
                    "application_state": str(
                        artifact.get("application_state") or "selection_ready"
                    ),
                }
            )
        return sorted(
            result,
            key=lambda item: (str(item["kind"]), str(item["name"]), str(item["id"])),
        )

    def spend_exact_wallet_payment(
        wallet: dict[str, Any], payment: Any, *, required_cp: int
    ) -> dict[str, int]:
        """Validate an explicit coin payment without inventing currency exchange or change."""
        if not isinstance(payment, dict):
            raise ValueError("spellbook copy selection.payment must be a coin object")
        multipliers = {"cp": 1, "sp": 10, "ep": 50, "gp": 100, "pp": 1000}
        unknown = set(payment) - set(multipliers)
        if unknown:
            raise ValueError(f"spellbook copy payment has unknown coins: {sorted(unknown)}")
        normalized: dict[str, int] = {}
        total_cp = 0
        for denomination, multiplier in multipliers.items():
            amount = payment.get(denomination, 0)
            if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
                raise ValueError("spellbook copy coin amounts must be non-negative integers")
            if amount > int(wallet.get(denomination, 0) or 0):
                raise ValueError(f"insufficient {denomination} for spellbook copy")
            normalized[denomination] = amount
            total_cp += amount * multiplier
        if total_cp != required_cp:
            raise ValueError(
                f"spellbook copy payment must equal exactly {required_cp} cp; got {total_cp} cp"
            )
        for denomination, amount in normalized.items():
            wallet[denomination] = int(wallet.get(denomination, 0) or 0) - amount
        return normalized

    def settle_spellbook_copy(
        *,
        current: Any,
        sheet: dict[str, Any],
        artifact_id: str,
        pack_id: str,
        version: str,
        level: int,
        school: str,
        selection: dict[str, Any],
        principal_id: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Pay, wait, expire effects, and record one discovered spell atomically."""
        assert current.campaign_id is not None
        campaign_id = current.campaign_id
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
        if level < 1:
            raise ValueError("cantrips cannot be copied from a spellbook")
        campaign = campaigns.get(campaign_id)
        next_state = validate_party_state(deepcopy(campaign.state or {}))
        if next_state.get("game_phase", PROFILE_LOBBY) != PROFILE_PLAY:
            raise CombatEngineError("spellbook copying is available only during play")
        source_owner = str(selection.get("source_owner") or "party").strip().casefold()
        if source_owner not in {"party", "character"}:
            raise ValueError("spellbook copy source_owner must be party or character")
        source_item_id = str(selection.get("source_item_id") or "").strip()
        if not source_item_id:
            raise ValueError("spellbook copy requires source_item_id")
        source_inventory = (
            next_state["party"]["inventory"]
            if source_owner == "party"
            else sheet["inventory"]
        )
        source_item = next(
            (
                item
                for item in source_inventory.get("items", [])
                if str(item.get("id") or "") == source_item_id
            ),
            None,
        )
        if source_item is None:
            raise ValueError("spellbook copy source item is not in the selected inventory")
        mechanics = dict(source_item.get("mechanics") or {})
        if source_item.get("kind") != "spellbook":
            raise ValueError("spellbook copy source item must have kind=spellbook")
        campaign_edition = str(campaign.settings.get("edition") or "2014")
        if str(mechanics.get("edition") or "") != campaign_edition:
            raise ValueError("spellbook copy source edition does not match the campaign")
        if not mechanics.get("copyable", False):
            raise ValueError("spellbook copy source is not marked copyable")
        if artifact_id not in set(mechanics.get("spell_ids") or []):
            raise ValueError("requested spell is not recorded in the source spellbook")

        feature_ids = {
            str(feature.get("id") or "")
            for feature in sheet.get("content", {}).get("features", [])
        }
        normalized_school = school.strip().casefold().split(" ", 1)[0]
        rule_facts = {
            "actor_id": current.id,
            "spell_id": artifact_id,
            "spell_level": level,
            "spell_school": normalized_school,
            "source_item_id": source_item_id,
            "source_was_previously_deciphered": bool(mechanics.get("deciphered", False)),
            **{f"has_feature:{feature_id}": True for feature_id in feature_ids if feature_id},
        }
        rule_context = effective_rule_context(campaign_id, facts=rule_facts)
        copy_rules = apply_rule_event(sheet, "spellbook.copy.before", rule_context)
        if copy_rules.status != "committed":
            return {
                "status": copy_rules.status,
                "pending": list(copy_rules.pending),
                "rule_receipts": list(copy_rules.receipts),
            }
        cost_percent = 100
        time_percent = 100
        for modifier in copy_rules.modifiers:
            if modifier.get("target") == "copy_cost_percent":
                cost_percent += int(modifier.get("value", 0) or 0)
            elif modifier.get("target") == "copy_time_percent":
                time_percent += int(modifier.get("value", 0) or 0)
        core_boundaries = ["dnd5e.core.spell.spellbook_copy"]
        if (
            normalized_school == "evocation"
            and "dnd5e.content.srd2014.feature.school-of-evocation-evocation-savant"
            in feature_ids
        ):
            cost_percent -= 50
            time_percent -= 50
            core_boundaries.append("dnd5e.core.spell.evocation_savant")
        if cost_percent <= 0 or time_percent <= 0:
            raise ValueError("spellbook copy modifiers must leave positive cost and time")
        base_cost_cp = level * 5000
        base_minutes = level * 120
        cost_cp = (base_cost_cp * cost_percent + 99) // 100
        minutes = (base_minutes * time_percent + 99) // 100
        hours = minutes / 60
        payment_owner = str(selection.get("payment_owner") or "character").strip().casefold()
        if payment_owner not in {"party", "character"}:
            raise ValueError("spellbook copy payment_owner must be party or character")
        payment_wallet = (
            next_state["party"]["inventory"]["wallet"]
            if payment_owner == "party"
            else sheet["inventory"]["wallet"]
        )
        payment = spend_exact_wallet_payment(
            payment_wallet, selection.get("payment"), required_cp=cost_cp
        )

        world_time = dict(next_state.get("world_time") or {})
        if not world_time:
            raise ValueError("set the campaign clock before copying a spell")
        elapsed = int(world_time.get("elapsed_minutes", 0) or 0) + minutes
        next_world_time = {
            "schema_version": 1,
            "day": elapsed // 1440 + 1,
            "hour": (elapsed % 1440) // 60,
            "minute": elapsed % 60,
            "elapsed_minutes": elapsed,
            "label": str(world_time.get("label") or ""),
        }
        next_state["world_time"] = next_world_time

        world_advanced: list[str] = []
        world_expired: list[str] = []
        world_duration_periods = [("minute", minutes)]
        if minutes % 60 == 0:
            world_duration_periods.append(("hour", minutes // 60))
        for effect_period, amount in world_duration_periods:
            world_result = advance_world_effect_durations(
                next_state, period=effect_period, amount=amount
            )
            next_state = world_result["state"]
            world_advanced.extend(world_result["advanced"])
            world_expired.extend(world_result["expired"])

        branch_id = require_current_branch(campaign_id, None)
        request_payload = {
            "operation": "character.spellbook.copy",
            "character_id": current.id,
            "artifact_id": artifact_id,
            "pack_id": pack_id,
            "version": version,
            "selection": selection,
        }
        scope = f"character-write:{campaign_id}:{branch_id}:{principal_id}:{current.id}"
        replay = replay_idempotent(scope, idempotency_key, request_payload)
        if replay is not None:
            return replay

        rule_context = context_with_facts(
            rule_context,
            copy_hours=hours,
            copy_minutes=minutes,
            copy_cost_cp=cost_cp,
            copy_cost_percent=cost_percent,
            copy_time_percent=time_percent,
        )
        receipts: list[dict[str, Any]] = list(copy_rules.receipts)
        updates: list[CharacterStateUpdate] = []
        advanced: dict[str, list[str]] = {}
        expired: dict[str, list[str]] = {}
        for character in characters.list(campaign_id=campaign_id):
            updated_sheet = sheet if character.id == current.id else character.sheet
            character_advanced: list[str] = []
            character_expired: list[str] = []
            duration_periods = [("minute", minutes)]
            if minutes % 60 == 0:
                duration_periods.append(("hour", minutes // 60))
            for period, amount in duration_periods:
                duration = advance_effect_durations(updated_sheet, period=period, amount=amount)
                extension = apply_rule_event(
                    duration["sheet"],
                    "duration.advance",
                    context_with_facts(
                        rule_context,
                        actor_id=character.id,
                        period=period,
                        amount=amount,
                    ),
                )
                receipts.extend(extension.receipts)
                updated_sheet = extension.sheet
                character_advanced.extend(duration["advanced"])
                character_expired.extend(duration["expired"])
            if updated_sheet != character.sheet:
                updates.append(
                    CharacterStateUpdate(
                        character_id=character.id,
                        sheet=validate_character_sheet(updated_sheet),
                        notes=validate_character_notes(character.notes),
                        expected_revision=(
                            expected_revision
                            if character.id == current.id
                            else character.revision
                        ),
                    )
                )
            if character_advanced:
                advanced[character.id] = list(dict.fromkeys(character_advanced))
            if character_expired:
                expired[character.id] = list(dict.fromkeys(character_expired))
        receipts.extend(
            core_receipts(
                rule_context,
                core_boundaries,
                "character.spellbook.copy",
            )
        )
        StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=next_state,
            character_updates=updates,
            expected_campaign_revision=campaign.revision,
            operation="character.spellbook.copy",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=idempotency_key,
            rule_receipts=receipts,
        )
        response = character_view(characters.get(current.id))
        response["spellbook_copy"] = {
            "spell_id": artifact_id,
            "source_owner": source_owner,
            "source_item_id": source_item_id,
            "deciphered_during_copy": not bool(mechanics.get("deciphered", False)),
            "payment_owner": payment_owner,
            "payment": payment,
            "cost_cp": cost_cp,
            "base_cost_cp": base_cost_cp,
            "cost_percent": cost_percent,
            "minutes": minutes,
            "hours": hours,
            "base_minutes": base_minutes,
            "time_percent": time_percent,
            "world_time": next_world_time,
            "advanced": advanced,
            "expired": expired,
            "world_advanced": list(dict.fromkeys(world_advanced)),
            "world_expired": list(dict.fromkeys(world_expired)),
            "rule_receipts": receipts,
        }
        return remember_idempotent(
            scope,
            idempotency_key,
            request_payload,
            response,
            campaign_id=campaign_id,
        )

    @mcp.tool()
    def character_content_apply(
        character_id: str,
        artifact_id: str,
        selection: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Apply a catalog option when its structured card has a safe character target."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        require_outside_active_combat(current, "content selection")
        if current.campaign_id is None:
            raise ValueError("content selection requires a campaign-bound character")
        if expected_revision is None or not idempotency_key:
            raise ValueError(
                "expected_revision and idempotency_key are required for content selection"
            )
        candidates = available_content_artifacts(current.campaign_id)
        match = next((item for item in candidates if item[2].get("id") == artifact_id), None)
        if match is None:
            raise LookupError("content artifact is not available for this campaign")
        pack_id, version, artifact = match
        application_state = str(artifact.get("application_state") or "selection_ready")
        if application_state != "selection_ready":
            return {
                "status": "pending_ruling",
                "reason": (
                    "catalog artifact is source-linked but not selection-ready; "
                    "complete reviewer validation before applying it to an actor"
                ),
            }
        kind = str(artifact.get("kind") or "")
        card = deepcopy(dict(artifact.get("card") or {}))
        selection = deepcopy(selection or {})
        sheet = deepcopy(current.sheet)
        campaign = campaigns.get(current.campaign_id)
        phase = str(dict(campaign.state or {}).get("game_phase") or PROFILE_LOBBY)
        spellbook_copy: dict[str, Any] | None = None
        requested_method = str(selection.get("method") or "").strip().casefold()
        operation = (
            "character.spellbook.copy"
            if requested_method == "spellbook_copy"
            else "character.content.apply"
        )
        branch_id = require_current_branch(current.campaign_id, None)
        request_payload = {
            "operation": operation,
            "character_id": current.id,
            "artifact_id": artifact_id,
            "pack_id": pack_id,
            "version": version,
            "selection": selection,
        }
        replay = replay_idempotent(
            f"character-write:{current.campaign_id}:{branch_id}:{principal_id}:{current.id}",
            idempotency_key,
            request_payload,
        )
        if replay is not None:
            return replay
        provenance = {
            "id": artifact_id,
            "pack_id": pack_id,
            "pack_version": version,
            "rule_refs": list(artifact.get("rule_refs") or []),
            "mechanic_refs": list(artifact.get("mechanic_refs") or []),
        }
        if kind == "spell":
            if any(item.get("id") == artifact_id for item in sheet["content"]["spells"]):
                raise ValueError("content spell is already present")
            try:
                source_class = validate_spell_grant(
                    sheet,
                    card,
                    source_class=selection.get("source_class"),
                )
            except CombatEngineError as error:
                return {"status": "pending_ruling", "reason": str(error)}
            level = int(card.get("level", 0) or 0)
            preparation_mode = str(
                sheet.get("spellcasting", {}).get("preparation", {}).get("mode") or "known"
            )
            method = str(selection.get("method") or "").strip().casefold()
            if not method:
                method = (
                    "known"
                    if level == 0 or preparation_mode == "known"
                    else ("spellbook" if preparation_mode == "spellbook" else "class_prepared")
                )
            if method not in {"known", "spellbook", "spellbook_copy", "class_prepared"}:
                raise ValueError(
                    "spell selection method must be known, spellbook, spellbook_copy, "
                    "or class_prepared"
                )
            if method in {"spellbook", "spellbook_copy"} and preparation_mode != "spellbook":
                raise ValueError("only a spellbook caster can select a spellbook grant")
            if method == "class_prepared" and preparation_mode != "prepared":
                raise ValueError("class_prepared requires prepared-caster configuration")
            if method == "known" and level > 0 and preparation_mode != "known":
                raise ValueError(
                    "this caster records level 1+ spells as prepared or spellbook grants"
                )
            if method == "spellbook_copy":
                if source_class != "wizard":
                    raise ValueError("only wizard spells can be copied into this spellbook")
                if phase != PROFILE_PLAY:
                    raise CombatEngineError("spellbook copying is available only during play")
                spellbook_copy = {"level": level}
            elif phase != PROFILE_LOBBY:
                raise CombatEngineError(
                    "content grants belong to lobby setup or level advancement; "
                    "only source-bound spellbook_copy is legal during play"
                )
            card["grant"] = {
                "source_type": "class",
                "source_key": source_class,
                "method": method,
            }
            card.setdefault("access", {})["known"] = method == "known"
            card["access"]["prepared"] = False
            if method in {"spellbook", "spellbook_copy"}:
                spellbook = sheet["spellcasting"]["spellbook"]
                if not spellbook.get("enabled"):
                    raise ValueError("spellbook grant requires spellcasting.spellbook.enabled")
                spellbook["spell_ids"] = [
                    *list(spellbook.get("spell_ids") or []),
                    artifact_id,
                ]
            # Class eligibility belongs to the catalog artifact. The actor card
            # stores only the selected grant source and exact pack provenance.
            card.pop("classes", None)
            card.update(provenance)
            sheet["content"]["spells"].append(card)
        elif kind == "feat":
            if any(item.get("id") == artifact_id for item in sheet["content"]["feats"]):
                raise ValueError("content feat is already present")
            for prerequisite in card.get("prerequisites", []):
                if prerequisite.get("kind") != "ability_minimum":
                    return {
                        "status": "pending_ruling",
                        "reason": "feat has a prerequisite that needs DM review",
                    }
                ability = str(prerequisite.get("ability") or "")
                score = int(sheet.get("abilities", {}).get(ability, {}).get("score", 0) or 0)
                if score < int(prerequisite.get("minimum", 0) or 0):
                    raise ValueError(f"feat prerequisite is not met: {ability}")
            # Eligibility is validated against the catalog card, but the actor
            # feature schema stores the selected feat and exact pack provenance.
            card.pop("prerequisites", None)
            card.update(provenance)
            sheet["content"]["feats"].append(card)
        elif kind == "subclass":
            classes = list(sheet["progression"]["classes"])
            if not classes:
                return {
                    "status": "pending_ruling",
                    "reason": "choose a base class before selecting a subclass",
                }
            declared_class = str(card.get("class_name") or "").strip()
            target_class = str(selection.get("target_class_name") or declared_class).strip()
            if not target_class:
                return {
                    "status": "pending_ruling",
                    "reason": "subclass artifact needs class_name or target_class_name",
                }
            if declared_class and target_class.casefold() != declared_class.casefold():
                raise ValueError("subclass does not belong to target_class_name")
            target = next(
                (
                    item
                    for item in classes
                    if str(item.get("name") or "").casefold() == target_class.casefold()
                ),
                None,
            )
            if target is None:
                raise ValueError("subclass target class is not on this actor card")
            minimum_level = int(card.get("minimum_level", 1) or 1)
            if int(target.get("level", 0) or 0) < minimum_level:
                raise ValueError(
                    f"{target_class} must reach level {minimum_level} for this subclass"
                )
            existing_subclass = str(target.get("subclass") or "")
            if existing_subclass and existing_subclass != str(card.get("name") or artifact_id):
                raise ValueError("target class already has a different subclass")
            target["subclass"] = str(card.get("name") or artifact_id)
            sheet["progression"]["classes"] = classes
            domain_spell_ids: list[str] = []
            for spell_grant in card.get("always_prepared_spells", []):
                if int(spell_grant.get("minimum_level", 1) or 1) > int(target.get("level", 0) or 0):
                    continue
                spell_name = str(spell_grant.get("name") or "").strip()
                spell_match = next(
                    (
                        item
                        for item in candidates
                        if item[2].get("kind") == "spell"
                        and str(dict(item[2].get("card") or {}).get("name") or "").casefold()
                        == spell_name.casefold()
                        and declared_class.casefold()
                        in {
                            str(value).casefold()
                            for value in dict(item[2].get("card") or {}).get("classes", [])
                        }
                    ),
                    None,
                )
                if spell_match is None:
                    return {
                        "status": "pending_ruling",
                        "reason": (
                            f"subclass spell is not available in the active catalog: {spell_name}"
                        ),
                    }
                spell_pack_id, spell_version, spell_artifact = spell_match
                spell_id = str(spell_artifact["id"])
                domain_spell_ids.append(spell_id)
                spell_card = next(
                    (item for item in sheet["content"]["spells"] if item.get("id") == spell_id),
                    None,
                )
                if spell_card is None:
                    spell_card = deepcopy(dict(spell_artifact.get("card") or {}))
                    spell_card.pop("classes", None)
                    sheet["content"]["spells"].append(spell_card)
                spell_card["grant"] = {
                    "source_type": "subclass",
                    "source_key": str(card.get("name") or artifact_id),
                    "method": "class_prepared",
                }
                spell_card.setdefault("access", {})["known"] = False
                spell_card["access"]["prepared"] = True
                spell_card["access"]["always_prepared"] = True
                spell_card.update(
                    id=spell_id,
                    pack_id=spell_pack_id,
                    pack_version=spell_version,
                    rule_refs=list(spell_artifact.get("rule_refs") or []),
                    mechanic_refs=list(spell_artifact.get("mechanic_refs") or []),
                )
            if domain_spell_ids:
                preparation = sheet["spellcasting"]["preparation"]
                preparation["selected_spell_ids"] = [
                    item
                    for item in preparation.get("selected_spell_ids", [])
                    if item not in set(domain_spell_ids)
                ]
        elif kind == "background":
            existing_background = str(sheet["progression"].get("background") or "")
            selected_background = str(card.get("name") or artifact_id)
            if existing_background and existing_background != selected_background:
                raise ValueError("character already has a different background")
            sheet["progression"]["background"] = str(card.get("name") or artifact_id)
            grants = dict(card.get("background_grants") or {})
            requirements = dict(grants.get("choices") or {})
            language_count = int(requirements.get("language_count", 0) or 0)
            selected_languages = [str(item).strip() for item in selection.get("languages", [])]
            if len(selected_languages) != language_count or any(
                not item for item in selected_languages
            ):
                return {
                    "status": "pending_ruling",
                    "reason": f"background requires exactly {language_count} language choices",
                }
            if len({item.casefold() for item in selected_languages}) != len(selected_languages):
                raise ValueError("background language choices must be distinct")
            grants["languages"] = selected_languages
            sheet["progression"]["background_grants"] = {
                **sheet["progression"]["background_grants"],
                **grants,
            }
            sheet["traits"]["languages"] = list(
                dict.fromkeys([*sheet["traits"]["languages"], *selected_languages])
            )
            for skill in card.get("skill_proficiencies", []):
                skill_key = str(skill).casefold()
                if skill_key not in sheet["skills"]:
                    raise ValueError(f"background references an unknown skill: {skill_key}")
                sheet["skills"][skill_key]["proficiency"] = "proficient"
        elif kind == "species":
            selected_species = str(card.get("name") or artifact_id)
            base_species = str(card.get("base_species") or selected_species)
            existing_species = str(sheet["progression"].get("species") or "")
            if existing_species and existing_species.casefold() not in {
                selected_species.casefold(),
                base_species.casefold(),
            }:
                raise ValueError("character already has a different species")
            if any(
                item.get("artifact_id") == artifact_id for item in sheet["content"]["selections"]
            ):
                raise ValueError("content species is already present")
            grants = dict(card.get("grants") or {})
            if grants.get("unresolved"):
                return {
                    "status": "pending_ruling",
                    "reason": "species has unresolved structured grants",
                    "missing": list(grants.get("unresolved") or []),
                }
            selected_languages = _validated_distinct_choices(
                selection.get("languages"),
                count=int(grants.get("language_choice_count", 0) or 0),
                label="species languages",
            )
            selected_skills = [
                item.casefold()
                for item in _validated_distinct_choices(
                    selection.get("skills"),
                    count=int(grants.get("skill_choice_count", 0) or 0),
                    label="species skills",
                )
            ]
            for skill in selected_skills:
                if skill not in sheet["skills"]:
                    raise ValueError(f"species references an unknown skill: {skill}")
            tool_options = {
                str(item).casefold(): str(item) for item in grants.get("tool_choices", [])
            }
            selected_tools = _validated_distinct_choices(
                selection.get("tools"),
                count=1 if tool_options else 0,
                label="species tools",
            )
            if any(item.casefold() not in tool_options for item in selected_tools):
                raise ValueError("species tool choice is not one of the allowed options")
            selected_tools = [tool_options[item.casefold()] for item in selected_tools]
            ability_choice = dict(grants.get("ability_choice") or {})
            selected_abilities = [
                item.casefold()
                for item in _validated_distinct_choices(
                    selection.get("abilities"),
                    count=int(ability_choice.get("count", 0) or 0),
                    label="species abilities",
                )
            ]
            excluded_abilities = {
                str(item).casefold() for item in ability_choice.get("exclude", [])
            }
            if any(item not in sheet["abilities"] for item in selected_abilities):
                raise ValueError("species ability choice is not a valid ability")
            if excluded_abilities.intersection(selected_abilities):
                raise ValueError("species ability choice cannot repeat a fixed increase")
            values_include_grants = bool(selection.get("values_include_species_grants", False))
            abilities_include_grants = bool(
                selection.get("ability_scores_include_species_grants", values_include_grants)
            )
            hp_includes_grants = bool(
                selection.get("hit_points_include_species_grants", values_include_grants)
            )
            if not abilities_include_grants:
                increases = dict(grants.get("ability_score_increases") or {})
                for ability in selected_abilities:
                    increases[ability] = int(increases.get(ability, 0)) + int(
                        ability_choice.get("amount", 0) or 0
                    )
                for ability, amount in increases.items():
                    sheet["abilities"][ability]["score"] = int(
                        sheet["abilities"][ability]["score"]
                    ) + int(amount)
            if not hp_includes_grants:
                hp_bonus = int(grants.get("hp_per_level", 0) or 0) * int(
                    sheet["progression"].get("level", 1) or 1
                )
                if hp_bonus:
                    sheet["combat"]["hp"]["max"] += hp_bonus
                    sheet["combat"]["hp"]["value"] += hp_bonus
            if grants.get("size"):
                sheet["traits"]["size"] = str(grants["size"])
            if int(grants.get("walk_speed", 0) or 0):
                sheet["combat"]["speed"]["walk"] = int(grants["walk_speed"])
            if int(grants.get("darkvision_ft", 0) or 0):
                sheet["traits"]["senses"]["darkvision"] = int(grants["darkvision_ft"])
            sheet["traits"]["languages"] = list(
                dict.fromkeys(
                    [
                        *sheet["traits"]["languages"],
                        *list(grants.get("languages") or []),
                        *selected_languages,
                    ]
                )
            )
            fixed_skills = [str(item).casefold() for item in grants.get("skill_proficiencies", [])]
            for skill in [*fixed_skills, *selected_skills]:
                if skill not in sheet["skills"]:
                    raise ValueError(f"species references an unknown skill: {skill}")
                sheet["skills"][skill]["proficiency"] = "proficient"
            proficiencies = sheet["traits"]["proficiencies"]
            proficiencies["weapons"] = list(
                dict.fromkeys(
                    [*proficiencies["weapons"], *list(grants.get("weapon_proficiencies") or [])]
                )
            )
            proficiencies["tools"] = list(
                dict.fromkeys(
                    [
                        *proficiencies["tools"],
                        *list(grants.get("tool_proficiencies") or []),
                        *selected_tools,
                    ]
                )
            )
            sheet["traits"]["resistances"] = list(
                dict.fromkeys(
                    [*sheet["traits"]["resistances"], *list(grants.get("resistances") or [])]
                )
            )
            cantrip_id = str(selection.get("cantrip_artifact_id") or "")
            cantrip_requirement = dict(grants.get("cantrip_choice") or {})
            if cantrip_requirement:
                cantrip_match = next(
                    (item for item in candidates if item[2].get("id") == cantrip_id), None
                )
                if cantrip_match is None:
                    raise ValueError("species cantrip_artifact_id is not available")
                cantrip_pack_id, cantrip_version, cantrip_artifact = cantrip_match
                cantrip_card = deepcopy(dict(cantrip_artifact.get("card") or {}))
                if (
                    cantrip_artifact.get("kind") != "spell"
                    or int(cantrip_card.get("level", -1)) != int(cantrip_requirement["level"])
                    or str(cantrip_requirement["class"]).casefold()
                    not in {str(item).casefold() for item in cantrip_card.get("classes", [])}
                ):
                    raise ValueError(
                        "species cantrip choice does not meet its class and level rule"
                    )
                if any(item.get("id") == cantrip_id for item in sheet["content"]["spells"]):
                    raise ValueError("species cantrip is already present")
                cantrip_card.pop("classes", None)
                cantrip_card["grant"] = {
                    "source_type": "species",
                    "source_key": selected_species,
                    "method": "known",
                }
                cantrip_card.setdefault("access", {})["known"] = True
                cantrip_card["access"]["prepared"] = False
                cantrip_card.update(
                    id=cantrip_id,
                    pack_id=cantrip_pack_id,
                    pack_version=cantrip_version,
                    rule_refs=list(cantrip_artifact.get("rule_refs") or []),
                    mechanic_refs=list(cantrip_artifact.get("mechanic_refs") or []),
                )
                sheet["content"]["spells"].append(cantrip_card)
            elif cantrip_id:
                raise ValueError("species does not grant a cantrip choice")
            feature_choices = {
                "languages": selected_languages,
                "skills": selected_skills,
                "tools": selected_tools,
                "abilities": selected_abilities,
                "cantrip_artifact_id": cantrip_id,
            }
            for feature in grants.get("features", []):
                feature_card = deepcopy(dict(feature))
                feature_card.update(
                    pack_id=pack_id,
                    pack_version=version,
                    rule_refs=list(artifact.get("rule_refs") or []),
                    mechanic_refs=list(artifact.get("mechanic_refs") or []),
                )
                if any(feature_choices.values()):
                    feature_card["choices"] = feature_choices
                sheet["content"]["features"].append(feature_card)
            sheet["progression"]["species"] = selected_species
        elif kind in {"feature", "activity"}:
            section = "features" if kind == "feature" else "activities"
            if any(item.get("id") == artifact_id for item in sheet["content"][section]):
                raise ValueError(f"content {kind} is already present")
            if kind == "feature":
                declared_class = str(card.get("class_name") or "").strip()
                declared_subclass = str(card.get("subclass_name") or "").strip()
                minimum_level = int(card.get("minimum_level", 1) or 1)
                target = next(
                    (
                        item
                        for item in sheet["progression"]["classes"]
                        if str(item.get("name") or "").casefold() == declared_class.casefold()
                    ),
                    None,
                )
                if declared_class and target is None:
                    raise ValueError("feature class is not on this actor card")
                if target is not None and int(target.get("level", 0) or 0) < minimum_level:
                    raise ValueError(
                        f"{declared_class} must reach level {minimum_level} for this feature"
                    )
                if declared_subclass and (
                    target is None
                    or str(target.get("subclass") or "").casefold() != declared_subclass.casefold()
                ):
                    raise ValueError("feature subclass is not selected on this actor card")
                requirements = dict(card.get("selection_requirements") or {})
                choice_field = str(requirements.get("field") or "")
                if choice_field:
                    selected = selection.get(choice_field)
                    if int(requirements.get("count", 1) or 1) == 1 and not isinstance(
                        selected, list
                    ):
                        selected_values = [str(selected or "").strip()]
                    else:
                        selected_values = _validated_distinct_choices(
                            selected,
                            count=int(requirements.get("count", 1) or 1),
                            label=f"feature {choice_field}",
                        )
                    if any(not item for item in selected_values):
                        raise ValueError(f"feature {choice_field} choice is required")
                    options = {str(item).casefold() for item in requirements.get("options", [])}
                    if options and any(item.casefold() not in options for item in selected_values):
                        raise ValueError("feature choice is not one of the allowed options")
                    if requirements.get("requires_existing_proficiency"):
                        for item in selected_values:
                            skill = sheet["skills"].get(item.casefold())
                            tool_known = item.casefold() in {
                                value.casefold()
                                for value in sheet["traits"]["proficiencies"]["tools"]
                            }
                            if not tool_known and (
                                skill is None or skill.get("proficiency") == "none"
                            ):
                                raise ValueError(
                                    "feature expertise choice requires an existing proficiency"
                                )
                            if skill is not None:
                                skill["proficiency"] = "expertise"
                mechanical_grants = dict(card.get("mechanical_grants") or {})
                armor = sheet["traits"]["proficiencies"]["armor"]
                sheet["traits"]["proficiencies"]["armor"] = list(
                    dict.fromkeys(
                        [*armor, *list(mechanical_grants.get("armor_proficiencies") or [])]
                    )
                )
                for resource_key, resource in dict(
                    mechanical_grants.get("resources") or {}
                ).items():
                    normalized_key = str(resource_key).strip()
                    if not normalized_key:
                        raise ValueError("feature resource grant has an empty key")
                    existing = sheet["resources"].get(normalized_key)
                    if existing is not None and existing != resource:
                        raise ValueError(
                            f"feature resource grant conflicts with existing resource: "
                            f"{normalized_key}"
                        )
                    sheet["resources"][normalized_key] = deepcopy(dict(resource))
                resource_key = str(card.get("resource_key") or "")
                if resource_key and resource_key not in sheet["resources"]:
                    raise ValueError(
                        f"feature requires an unapplied shared resource: {resource_key}"
                    )
                for metadata_key in (
                    "class_name",
                    "subclass_name",
                    "minimum_level",
                    "selection_requirements",
                    "mechanical_grants",
                ):
                    card.pop(metadata_key, None)
                if selection:
                    card["choices"] = {**dict(card.get("choices") or {}), **selection}
            card.update(provenance)
            sheet["content"][section].append(card)
        else:
            return {
                "status": "pending_ruling",
                "reason": f"{kind} is catalogued but needs a DM-reviewed application",
            }
        if kind in {"subclass", "background", "species"}:
            if any(
                item.get("artifact_id") == artifact_id for item in sheet["content"]["selections"]
            ):
                raise ValueError("content selection is already present")
            sheet["content"]["selections"].append(
                {
                    "artifact_id": artifact_id,
                    "kind": kind,
                    "name": str(card.get("name") or artifact_id),
                    "pack_id": pack_id,
                    "pack_version": version,
                    "rule_refs": list(artifact.get("rule_refs") or []),
                    "mechanic_refs": list(artifact.get("mechanic_refs") or []),
                    "selection": selection,
                }
            )
        if spellbook_copy is not None:
            return settle_spellbook_copy(
                current=current,
                sheet=sheet,
                artifact_id=artifact_id,
                pack_id=pack_id,
                version=version,
                level=int(spellbook_copy["level"]),
                school=str(card.get("definition", {}).get("school") or card.get("school") or ""),
                selection=selection,
                principal_id=principal_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        if phase != PROFILE_LOBBY:
            raise CombatEngineError(
                "content grants belong to lobby setup or level advancement"
            )
        return update_sheet(
            character_id,
            sheet,
            operation="character.content.apply",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={
                "artifact_id": artifact_id,
                "pack_id": pack_id,
                "version": version,
                "selection": selection,
            },
        )

    @mcp.tool()
    def character_rule_artifact_add(
        character_id: str,
        pack_id: str,
        version: str,
        artifact_id: str,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Add one activated pack card to an actor without copying executable rule logic."""
        current = characters.get(character_id)
        require_character_control(current, principal_id)
        require_outside_active_combat(current, "rule artifact changes")
        if current.campaign_id is None:
            raise ValueError("rule artifacts require a campaign-bound character")
        active = next(
            (
                item
                for item in rule_packs.activations(current.campaign_id)
                if item.pack_id == pack_id and item.enabled
            ),
            None,
        )
        if active is None or active.version != version:
            raise ValueError("the exact rule-pack version must be enabled on this branch")
        pack = rule_packs.get_version(pack_id, version)
        artifact = next((item for item in pack.artifacts if item.get("id") == artifact_id), None)
        if artifact is None:
            raise LookupError(artifact_id)
        section = {
            "feature": "features",
            "activity": "activities",
        }.get(str(artifact.get("kind") or ""))
        if section is None:
            raise ValueError(
                "spell, feat, subclass, and background artifacts must use "
                "character_content_apply for rule-aware validation"
            )
        sheet = deepcopy(current.sheet)
        if any(item.get("id") == artifact_id for item in sheet["content"][section]):
            raise ValueError("rule artifact is already present on this character")
        card = deepcopy(artifact.get("card") or {})
        card["id"] = artifact_id
        card["pack_id"] = pack_id
        card["pack_version"] = version
        card["rule_refs"] = list(artifact.get("rule_refs") or [])
        card["mechanic_refs"] = list(artifact.get("mechanic_refs") or [])
        sheet["content"][section].append(card)
        return update_sheet(
            character_id,
            sheet,
            operation="character.rule_artifact.add",
            principal_id=principal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={
                "pack_id": pack_id,
                "version": version,
                "artifact_id": artifact_id,
            },
        )

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
            "module_import(action='stage'), then inspect, validate, ingest, and activate the "
            "returned import job."
        )

    # The public MCP contract intentionally exposes domain facades rather than
    # one tool per storage operation.  These facades call the mature, narrowly
    # validated operations above; they must not reimplement writes or weaken
    # revision, idempotency, access, or combat guards.
    character_content_apply_legacy = character_content_apply
    character_spell_prepare_legacy = character_spell_prepare
    campaign_clock_set_legacy = campaign_clock_set
    campaign_advance_effects_legacy = campaign_advance_effects
    # These facade names intentionally replace same-named legacy tools.
    for replaced_tool_name in (
        "character_content_apply",
        "character_spell_prepare",
        "campaign_clock_set",
        "campaign_advance_effects",
    ):
        mcp.remove_tool(replaced_tool_name)

    def facade_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        return dict(payload)

    def required(payload: dict[str, Any], name: str) -> Any:
        value = payload.get(name)
        if value is None or value == "":
            raise ValueError(f"payload.{name} is required")
        return value

    def facade_result(action: str, result: Any) -> dict[str, Any]:
        status = result.get("status", "ok") if isinstance(result, dict) else "ok"
        return {"status": status, "action": action, "result": result}

    @mcp.tool()
    def import_query(
        campaign_id: str,
        view: Literal["get", "list"] = "list",
        job_id: str | None = None,
        kind: Literal["rulebook", "module"] | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Read staged rulebook or module import jobs without changing their state."""
        if view == "get":
            return facade_result(
                view,
                import_job_get(campaign_id, required({"job_id": job_id}, "job_id"), principal_id),
            )
        return facade_result(view, import_job_list(campaign_id, kind, principal_id))

    @mcp.tool()
    def rule_import(
        campaign_id: str,
        action: Literal[
            "stage",
            "inspect",
            "ingest",
            "extract_candidates",
            "review",
            "compile",
            "install",
            "activate",
        ],
        payload: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Run the reviewed rulebook-import state machine; direct rule ingestion is not public."""
        data = facade_payload(payload)
        if action == "stage":
            artifact = rule_document_stage(
                campaign_id, required(data, "source_path"), principal_id
            )["artifact"]
            result = rule_import_job_create(
                campaign_id,
                artifact,
                required(data, "source_key"),
                required(data, "title"),
                required(data, "edition"),
                data.get("locale", "en"),
                data.get("publication_id", ""),
                data.get("version", ""),
                data.get("authority", "supplement"),
                principal_id,
                idempotency_key,
            )
            return facade_result(action, {"artifact": artifact, **result})
        job_id = required(data, "job_id")
        if action == "inspect":
            return facade_result(
                action, rule_import_job_inspect(campaign_id, job_id, principal_id, idempotency_key)
            )
        if action == "ingest":
            return facade_result(
                action, rule_import_job_ingest(campaign_id, job_id, principal_id, idempotency_key)
            )
        if action == "extract_candidates":
            return facade_result(
                action,
                rule_content_candidates_extract(campaign_id, job_id, principal_id, idempotency_key),
            )
        if action == "review":
            return facade_result(
                action,
                import_job_review_candidates(
                    campaign_id, job_id, required(data, "decisions"), principal_id, idempotency_key
                ),
            )
        if action == "compile":
            return facade_result(
                action,
                rule_import_job_compile(
                    campaign_id,
                    job_id,
                    required(data, "manifest"),
                    data.get("mechanics"),
                    data.get("provenance"),
                    principal_id,
                    idempotency_key,
                ),
            )
        if action == "install":
            return facade_result(
                action, rule_import_job_install(campaign_id, job_id, principal_id, idempotency_key)
            )
        return facade_result(
            action,
            rule_import_job_activate(
                campaign_id, job_id, principal_id, branch_id, expected_revision, idempotency_key
            ),
        )

    @mcp.tool()
    def module_import(
        campaign_id: str,
        action: Literal["stage", "inspect", "validate", "ingest", "activate"],
        payload: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Run the staged module-import state machine and activate only reviewed revisions."""
        data = facade_payload(payload)
        if action == "stage":
            access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
            source_path = data.get("source_path")
            generated_fields = {"name", "content"}.intersection(data)
            if source_path is not None and generated_fields:
                raise ValueError("stage accepts either source_path or name+content, not both")
            if source_path is not None:
                staged = storage.stage_module(str(source_path))
                artifact = str(staged["artifact"])
            else:
                staged = module_write(
                    required(data, "name"), required(data, "content"), principal_id
                )
                artifact = staged["artifact"]
            result = module_import_job_create(
                campaign_id,
                artifact,
                data.get("title"),
                data.get("source_key"),
                principal_id,
                idempotency_key,
            )
            return facade_result(action, {**staged, "artifact": artifact, **result})
        job_id = required(data, "job_id")
        if action == "inspect":
            return facade_result(
                action,
                module_import_job_inspect(campaign_id, job_id, principal_id, idempotency_key),
            )
        if action == "validate":
            return facade_result(
                action,
                module_import_job_validate(campaign_id, job_id, principal_id, idempotency_key),
            )
        if action == "ingest":
            return facade_result(
                action, module_import_job_import(campaign_id, job_id, principal_id, idempotency_key)
            )
        return facade_result(
            action,
            module_import_job_activate(
                campaign_id, job_id, principal_id, expected_revision, idempotency_key
            ),
        )

    @mcp.tool()
    def module_query(
        campaign_id: str,
        view: Literal[
            "list",
            "index",
            "scene",
            "current",
            "progress",
            "readiness",
            "assets",
            "content",
        ] = "list",
        payload: dict[str, Any] | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Read module cards, indexes, one scene, or current scoped progress."""
        data = facade_payload(payload)
        if view == "list":
            result = module_list(campaign_id, principal_id)
        elif view == "index":
            result = module_index(campaign_id, data.get("module_id"), principal_id)
        elif view == "scene":
            result = module_read_scene(
                campaign_id,
                required(data, "scene_id"),
                data.get("scope_id", "party"),
                principal_id,
            )
        elif view == "current":
            result = module_current(campaign_id, data.get("scope_id", "party"), principal_id)
        elif view == "readiness":
            result = module_scene_readiness(
                campaign_id,
                required(data, "scene_id"),
                required(data, "participant_manifest"),
                principal_id,
            )
        elif view == "assets":
            result = module_assets(campaign_id, required(data, "module_id"), principal_id)
        elif view == "content":
            access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
            if data.get("review_id"):
                result = modules.get_content_review(campaign_id, str(data["review_id"]))
            else:
                result = modules.list_content_reviews(
                    campaign_id,
                    required(data, "module_id"),
                    content_kind=data.get("content_kind"),
                    content_key=data.get("content_key"),
                )
        else:
            result = module_progress_index(
                campaign_id,
                data.get("scope_id", "party"),
                data.get("module_id"),
                principal_id,
            )
        return facade_result(view, result)

    @mcp.tool()
    def rule_pack_compile(
        action: Literal["draft", "from_source"],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Compile a rule-pack draft, optionally bound to an indexed source."""
        data = facade_payload(payload)
        if action == "draft":
            result = rule_pack_draft(
                required(data, "manifest"),
                data.get("artifacts"),
                data.get("mechanics"),
                data.get("provenance"),
            )
        else:
            result = rule_pack_draft_from_source(
                required(data, "source_id"),
                required(data, "manifest"),
                data.get("artifacts"),
                data.get("mechanics"),
                data.get("provenance"),
            )
        return facade_result(action, result)

    @mcp.tool()
    def rule_pack_query(
        view: Literal["list", "inspect", "test", "content_catalog", "sources"] = "list",
        payload: dict[str, Any] | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """List, inspect, test, or browse selection-ready rule-pack content."""
        data = facade_payload(payload)
        if view == "list":
            result = rule_pack_list(data.get("pack_id"))
        elif view == "inspect":
            result = rule_pack_inspect(required(data, "pack_id"), required(data, "version"))
        elif view == "test":
            result = rule_pack_test(required(data, "pack_id"), required(data, "version"))
        elif view == "content_catalog":
            result = content_catalog_list(
                required(data, "campaign_id"),
                data.get("kind"),
                data.get("query", ""),
                principal_id,
                data.get("branch_id"),
            )
        else:
            result = rules.sources(
                system_id=data.get("system_id", "dnd5e"),
                edition=data.get("edition"),
            )
        return facade_result(view, result)

    @mcp.tool()
    def rule_pack_change(
        action: Literal["install", "remove"],
        pack_id: str,
        version: str,
    ) -> dict[str, Any]:
        """Install or remove a locally compiled rule-pack version."""
        result = (
            rule_pack_install(pack_id, version)
            if action == "install"
            else rule_pack_remove(pack_id, version)
        )
        return facade_result(action, result)

    @mcp.tool()
    def campaign_rules(
        campaign_id: str,
        action: Literal[
            "get_profile", "set_profile", "set_pack", "remove_pack", "explain", "receipts"
        ],
        payload: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Read and change the campaign rule profile and enabled pack set."""
        data = facade_payload(payload)
        if action == "get_profile":
            result = campaign_rule_profile_get(campaign_id, principal_id)
        elif action == "set_profile":
            result = campaign_rule_profile_set(
                campaign_id,
                required(data, "edition"),
                data.get("locale", "en"),
                data.get("publications"),
                data.get("options"),
                principal_id,
                expected_revision,
                idempotency_key,
            )
        elif action == "set_pack":
            result = campaign_rule_pack_set(
                campaign_id,
                required(data, "pack_id"),
                required(data, "version"),
                data.get("enabled", True),
                data.get("options"),
                principal_id,
                branch_id,
                expected_revision,
                idempotency_key,
            )
        elif action == "remove_pack":
            result = campaign_rule_pack_remove(
                campaign_id,
                required(data, "pack_id"),
                principal_id,
                branch_id,
                expected_revision,
                idempotency_key,
            )
        elif action == "explain":
            result = campaign_rules_explain(campaign_id, data.get("event"), principal_id, branch_id)
        else:
            result = campaign_rule_receipts(
                campaign_id,
                principal_id,
                branch_id,
                data.get("mechanic_id"),
                data.get("limit", 100),
            )
        return facade_result(action, result)

    @mcp.tool()
    def character_query(
        view: Literal["get", "list", "library"] = "list",
        payload: dict[str, Any] | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Read a character, campaign roster, or reusable character library."""
        data = facade_payload(payload)
        if view == "get":
            result = character_get(required(data, "character_id"), principal_id)
        elif view == "library":
            result = character_library_list(data.get("character_type"), principal_id)
        else:
            result = character_list(data.get("campaign_id"), principal_id)
        return facade_result(view, result)

    @mcp.tool()
    def character_create_from(
        mode: Literal["direct", "build", "template", "statblock", "module_statblock"],
        payload: dict[str, Any],
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create directly, by D&D build/template, or from an imported rule statblock."""
        data = facade_payload(payload)
        if mode == "direct":
            result = character_create(
                required(data, "name"),
                data.get("campaign_id"),
                data.get("character_type", "pc"),
                data.get("player_name"),
                data.get("summary", ""),
                data.get("sheet"),
                data.get("notes"),
                principal_id,
                idempotency_key,
            )
        elif mode == "build":
            result = character_build(
                required(data, "campaign_id"),
                required(data, "name"),
                data.get("player_name"),
                data.get("summary", ""),
                data.get("sheet"),
                data.get("notes"),
                principal_id,
                idempotency_key,
            )
        elif mode == "template":
            result = character_instantiate(
                required(data, "template_id"),
                required(data, "campaign_id"),
                data.get("name"),
                data.get("player_name"),
                principal_id,
            )
        elif mode == "module_statblock":
            campaign_id = str(required(data, "campaign_id"))
            review_id = str(required(data, "review_id"))
            access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
            campaign = campaigns.get(campaign_id)
            campaign_edition = str(campaign.settings.get("edition") or "2024")
            if campaign_edition != "2014":
                raise ValueError("reviewed module statblocks currently support D&D 2014 campaigns")
            review = modules.get_content_review(campaign_id, review_id)
            if review["content_kind"] != "dnd5e_2014_statblock":
                raise ValueError("module content review is not a D&D 2014 statblock")
            parsed = parse_2014_statblock(
                review["normalized_content"],
                source_key=f"module-review:{review_id}",
                rule_refs=[f"module-scene:{review['scene_id']}", f"module-review:{review_id}"],
                name=str(data.get("name") or "").strip() or None,
            )
            variant = data.get("variant")
            variant_evidence = statblock_variant_evidence(campaign_id, variant)
            sheet = (
                apply_statblock_variant(parsed.sheet, variant)
                if variant is not None
                else parsed.sheet
            )
            character_type = str(data.get("character_type") or "monster")
            if character_type not in {"npc", "monster"}:
                raise ValueError("module statblock import creates only npc or monster actors")
            notes = deepcopy(data.get("notes") or default_character_notes())
            profile = notes.setdefault("profile", {})
            if not str(profile.get("summary") or "").strip():
                profile["summary"] = parsed.summary
            evidence = dict(review.get("evidence") or {})
            provenance = (
                f"Reviewed module statblock: module-review:{review_id} "
                f"(module_id={review['module_id']}; scene_id={review['scene_id']}; "
                f"page={evidence.get('page')}; asset_checksum={evidence.get('asset_checksum')})."
            )
            if variant is not None:
                changed_fields = ", ".join(sorted(set(variant) - {"source_ref"})) or "none"
                provenance += (
                    f"\nVariant source: {variant['source_ref']}; applied fields: "
                    f"{changed_fields}."
                )
            if parsed.warnings:
                provenance += "\nManual rulings: " + "; ".join(parsed.warnings) + "."
            existing_dm_notes = str(profile.get("dm_notes") or "").strip()
            profile["dm_notes"] = "\n".join(
                item for item in (existing_dm_notes, provenance) if item
            )
            character = character_create(
                parsed.name,
                campaign_id,
                character_type,
                data.get("player_name"),
                str(data.get("summary") or parsed.summary),
                sheet,
                notes,
                principal_id,
                idempotency_key,
            )
            result = {
                "character": character,
                "source": review,
                "statblock": {
                    "challenge_rating": parsed.challenge_rating,
                    "experience_points": parsed.experience_points,
                    "warnings": list(parsed.warnings),
                    "settlement": "automatic" if not parsed.warnings else "mixed",
                },
                "variant": deepcopy(variant) if variant is not None else None,
                "variant_evidence": variant_evidence,
            }
        else:
            campaign_id = str(required(data, "campaign_id"))
            source_id = str(required(data, "source_id"))
            access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})
            campaign = campaigns.get(campaign_id)
            source = rules.source(source_id)
            if str(source.get("system_id") or "") != "dnd5e":
                raise ValueError("statblock source must belong to the dnd5e rule corpus")
            campaign_edition = str(campaign.settings.get("edition") or "2024")
            source_edition = str(source.get("edition") or "")
            if source_edition != campaign_edition:
                raise ValueError(
                    f"statblock source edition {source_edition!r} does not match "
                    f"campaign edition {campaign_edition!r}"
                )
            if source_edition != "2014":
                raise ValueError("structured statblock import currently supports D&D 2014 sources")

            available_chunks = rules.source_chunks(source_id)
            by_chunk_id = {str(item["id"]): item for item in available_chunks}
            selected_value = data.get("chunk_ids")
            if selected_value is None:
                selected_chunks = available_chunks
            else:
                if not isinstance(selected_value, list):
                    raise ValueError("payload.chunk_ids must be a list")
                chunk_ids = [str(item).strip() for item in selected_value]
                if any(not item for item in chunk_ids) or len(chunk_ids) != len(set(chunk_ids)):
                    raise ValueError("payload.chunk_ids must contain unique non-empty ids")
                missing = [item for item in chunk_ids if item not in by_chunk_id]
                if missing:
                    raise ValueError("statblock chunks do not belong to the requested source")
                selected_chunks = [by_chunk_id[item] for item in chunk_ids]
            if not selected_chunks:
                raise ValueError("statblock source has no indexed chunks")
            selected_chunks = sorted(
                selected_chunks, key=lambda item: (int(item.get("ordinal", 0)), str(item["id"]))
            )
            selected_chunk_ids = [str(item["id"]) for item in selected_chunks]
            rendered_chunks = []
            for item in selected_chunks:
                heading_path = [
                    str(value).strip()
                    for value in item.get("heading_path", [])
                    if str(value).strip()
                ]
                headings = "\n".join(
                    f"{'#' * min(6, 3 + index)} {heading}"
                    for index, heading in enumerate(heading_path)
                )
                rendered_chunks.append(
                    "\n\n".join(
                        value for value in (headings, str(item.get("content") or "")) if value
                    )
                )
            source_text = "\n\n".join(rendered_chunks)
            parsed = parse_2014_statblock(
                source_text,
                source_key=f"rule-source:{source['source_key']}",
                rule_refs=selected_chunk_ids,
                name=str(data.get("name") or source.get("title") or "").strip() or None,
            )
            variant = data.get("variant")
            variant_evidence = statblock_variant_evidence(campaign_id, variant)
            sheet = (
                apply_statblock_variant(parsed.sheet, variant)
                if variant is not None
                else parsed.sheet
            )
            character_type = str(data.get("character_type") or "npc")
            if character_type not in {"npc", "monster"}:
                raise ValueError("statblock import creates only npc or monster actors")
            notes = deepcopy(data.get("notes") or default_character_notes())
            profile = notes.setdefault("profile", {})
            if not str(profile.get("summary") or "").strip():
                profile["summary"] = parsed.summary
            provenance = (
                f"Statblock import: rule-source:{source['source_key']} "
                f"(source_id={source_id}; chunks={','.join(selected_chunk_ids)})."
            )
            if variant is not None:
                changed_fields = ", ".join(sorted(set(variant) - {"source_ref"})) or "none"
                provenance += (
                    f"\nVariant source: {variant['source_ref']}; applied fields: "
                    f"{changed_fields}."
                )
            if parsed.warnings:
                provenance += "\nManual rulings: " + "; ".join(parsed.warnings) + "."
            existing_dm_notes = str(profile.get("dm_notes") or "").strip()
            profile["dm_notes"] = "\n".join(
                item for item in (existing_dm_notes, provenance) if item
            )
            character = character_create(
                parsed.name,
                campaign_id,
                character_type,
                data.get("player_name"),
                str(data.get("summary") or parsed.summary),
                sheet,
                notes,
                principal_id,
                idempotency_key,
            )
            result = {
                "character": character,
                "source": {
                    "id": source_id,
                    "source_key": source["source_key"],
                    "title": source["title"],
                    "edition": source_edition,
                    "checksum": source["checksum"],
                    "chunk_ids": selected_chunk_ids,
                },
                "statblock": {
                    "challenge_rating": parsed.challenge_rating,
                    "experience_points": parsed.experience_points,
                    "warnings": list(parsed.warnings),
                    "settlement": "automatic" if not parsed.warnings else "mixed",
                },
                "variant": deepcopy(variant) if variant is not None else None,
                "variant_evidence": variant_evidence,
            }
        return facade_result(mode, result)

    @mcp.tool()
    def character_metadata_update(
        character_id: str,
        payload: dict[str, Any],
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Update identity and notes metadata; whole-sheet changes remain separate."""
        data = facade_payload(payload)
        prohibited = {"sheet", "state", "derived"} & set(data)
        if prohibited:
            raise ValueError(
                f"character metadata update cannot change: {', '.join(sorted(prohibited))}"
            )
        if not any(name in data for name in ("name", "player_name", "summary", "notes")):
            raise ValueError("payload must include at least one metadata field")
        result = character_update(
            character_id,
            data.get("name"),
            data.get("player_name"),
            data.get("summary"),
            None,
            data.get("notes"),
            principal_id,
            expected_revision,
            idempotency_key,
        )
        return facade_result("metadata", result)

    @mcp.tool()
    def character_content_apply(
        character_id: str,
        artifact_id: str,
        selection: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Apply a selection-ready class, background, feat, spell, or feature artifact."""
        return facade_result(
            "apply",
            character_content_apply_legacy(
                character_id,
                artifact_id,
                selection,
                principal_id,
                expected_revision,
                idempotency_key,
            ),
        )

    @mcp.tool()
    def inventory_change(
        owner: Literal["character", "party"],
        action: Literal["add", "update", "remove", "equip", "consume_ammunition"],
        owner_id: str,
        payload: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Change one character or party inventory while preserving owner-specific validation."""
        data = facade_payload(payload)
        if owner == "party" and action not in {"add", "remove"}:
            raise ValueError("party inventory supports only add and remove")
        if owner == "character":
            if action == "add":
                result = character_inventory_add(
                    owner_id,
                    required(data, "item"),
                    principal_id,
                    expected_revision,
                    idempotency_key,
                )
            elif action == "update":
                result = character_inventory_update(
                    owner_id,
                    required(data, "item_id"),
                    required(data, "patch"),
                    principal_id,
                    expected_revision,
                    idempotency_key,
                )
            elif action == "remove":
                result = character_inventory_remove(
                    owner_id,
                    required(data, "item_id"),
                    data.get("quantity"),
                    principal_id,
                    expected_revision,
                    idempotency_key,
                )
            elif action == "equip":
                result = character_inventory_equip(
                    owner_id,
                    required(data, "item_id"),
                    required(data, "slot"),
                    principal_id,
                    expected_revision,
                    idempotency_key,
                )
            else:
                result = character_ammunition_consume(
                    owner_id,
                    required(data, "weapon_id"),
                    data.get("quantity", 1),
                    principal_id,
                    expected_revision,
                    idempotency_key,
                )
        elif action == "add":
            result = party_inventory_add(
                owner_id, required(data, "item"), principal_id, expected_revision, idempotency_key
            )
        else:
            result = party_inventory_remove(
                owner_id,
                required(data, "item_id"),
                data.get("quantity"),
                principal_id,
                expected_revision,
                idempotency_key,
            )
        return facade_result(action, result)

    @mcp.tool()
    def inventory_transfer(
        mode: Literal["character_to_character", "party_to_character", "character_to_party"],
        payload: dict[str, Any],
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Transfer inventory with the revision contract required by every affected owner."""
        data = facade_payload(payload)
        if mode == "character_to_character":
            result = character_inventory_transfer(
                required(data, "source_character_id"),
                required(data, "target_character_id"),
                required(data, "item_id"),
                data.get("quantity"),
                principal_id,
                required(data, "expected_campaign_revision"),
                required(data, "expected_source_revision"),
                required(data, "expected_target_revision"),
                idempotency_key,
            )
        else:
            direction = "withdraw" if mode == "party_to_character" else "deposit"
            result = party_inventory_transfer(
                required(data, "campaign_id"),
                required(data, "character_id"),
                required(data, "item_id"),
                direction,
                data.get("quantity"),
                principal_id,
                required(data, "expected_campaign_revision"),
                required(data, "expected_character_revision"),
                idempotency_key,
            )
        return facade_result(mode, result)

    @mcp.tool()
    def wallet_change(
        owner: Literal["character", "party"],
        action: Literal["adjust", "transfer_to_character", "transfer_from_character"],
        owner_id: str,
        denomination: str,
        amount: int,
        payload: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Adjust a wallet or transfer money through the party with all affected revisions."""
        data = facade_payload(payload)
        if action == "adjust":
            result = (
                character_wallet_adjust(
                    owner_id, denomination, amount, principal_id, expected_revision, idempotency_key
                )
                if owner == "character"
                else party_wallet_adjust(
                    owner_id, denomination, amount, principal_id, expected_revision, idempotency_key
                )
            )
        else:
            if owner != "party":
                raise ValueError("wallet transfers use the party as owner")
            direction = "withdraw" if action == "transfer_to_character" else "deposit"
            result = party_wallet_transfer(
                owner_id,
                required(data, "character_id"),
                denomination,
                amount,
                direction,
                principal_id,
                required(data, "expected_campaign_revision"),
                required(data, "expected_character_revision"),
                idempotency_key,
            )
        return facade_result(action, result)

    @mcp.tool()
    def character_state_change(
        character_id: str,
        action: Literal[
            "effect_add",
            "effect_remove",
            "resource_set",
            "damage",
            "heal",
            "rest",
            "level_advance",
            "stable_recovery",
            "stand",
            "memory_add",
            "memory_resolve",
        ],
        payload: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Apply one noncombat character state transition with its D&D-specific validation."""
        data = facade_payload(payload)
        if action == "effect_add":
            result = character_effect_add(
                character_id,
                required(data, "effect"),
                principal_id,
                expected_revision,
                idempotency_key,
            )
        elif action == "effect_remove":
            result = character_effect_remove(
                character_id,
                required(data, "effect_id"),
                principal_id,
                expected_revision,
                idempotency_key,
            )
        elif action == "resource_set":
            result = character_resource_set(
                character_id,
                required(data, "resource"),
                required(data, "value"),
                principal_id,
                expected_revision,
                idempotency_key,
            )
        elif action == "damage":
            result = character_apply_damage(
                character_id,
                required(data, "parts"),
                critical=data.get("critical", False),
                knock_out=data.get("knock_out", False),
                melee=data.get("melee", False),
                principal_id=principal_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        elif action == "heal":
            result = character_apply_healing(
                character_id,
                required(data, "amount"),
                source_actor_id=data.get("source_actor_id"),
                spell_id=data.get("spell_id"),
                spell_level=data.get("spell_level"),
                principal_id=principal_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        elif action == "rest":
            result = character_rest(
                character_id,
                required(data, "rest_type"),
                data.get("prepared_spell_ids"),
                data.get("hit_dice_spends"),
                data.get("hit_dice_recovery"),
                data.get("food_and_drink", False),
                principal_id,
                expected_revision,
                idempotency_key,
            )
        elif action == "level_advance":
            result = character_level_advance(
                character_id,
                required(data, "class_name"),
                required(data, "hp_method"),
                required(data, "reason"),
                required(data, "source_ref"),
                data.get("hp_roll"),
                principal_id,
                expected_revision,
                idempotency_key,
            )
        elif action == "stable_recovery":
            result = character_stable_recovery(
                character_id,
                principal_id,
                expected_revision,
                idempotency_key,
            )
        elif action == "stand":
            result = character_stand(
                character_id,
                principal_id,
                expected_revision,
                idempotency_key,
            )
        elif action == "memory_add":
            result = character_memory_add(
                character_id,
                required(data, "memory"),
                principal_id,
                expected_revision,
                idempotency_key,
            )
        else:
            result = character_memory_resolve(
                character_id,
                required(data, "memory_id"),
                data.get("status", "resolved"),
                principal_id,
                expected_revision,
                idempotency_key,
            )
        return facade_result(action, result)

    @mcp.tool()
    def character_action(
        character_id: str,
        action: Literal["cast_spell", "use_activity"],
        payload: dict[str, Any],
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Commit one noncombat spell cast or activity activation through the rules engine."""
        data = facade_payload(payload)
        if action == "cast_spell":
            result = character_cast_spell(
                character_id,
                required(data, "spell_id"),
                data.get("cast_level"),
                data.get("ritual", False),
                data.get("component_ruling"),
                principal_id,
                expected_revision,
                idempotency_key,
            )
        else:
            result = character_use_activity(
                character_id,
                required(data, "activity_id"),
                data.get("declaration"),
                principal_id,
                expected_revision,
                idempotency_key,
            )
        return facade_result(action, result)

    @mcp.tool()
    def character_spell_prepare(
        character_id: str,
        mode: Literal["set", "replace_all"],
        payload: dict[str, Any],
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Set one prepared spell or replace the validated prepared-spell list."""
        data = facade_payload(payload)
        if mode == "set":
            result = character_spell_prepare_legacy(
                character_id,
                required(data, "spell_id"),
                required(data, "prepared"),
                principal_id,
                expected_revision,
                idempotency_key,
            )
        else:
            result = character_spell_prepare_list(
                character_id,
                required(data, "spell_ids"),
                data.get("event", "setup"),
                principal_id,
                expected_revision,
                idempotency_key,
            )
        return facade_result(mode, result)

    @mcp.tool()
    def campaign_query(
        view: Literal["list", "get", "party"] = "list",
        payload: dict[str, Any] | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Read campaigns, one campaign, or its party state."""
        data = facade_payload(payload)
        if view == "get":
            result = campaign_get(required(data, "campaign_id"), principal_id)
        elif view == "party":
            result = party_show(required(data, "campaign_id"), principal_id)
        else:
            result = campaign_list(data.get("status"), principal_id)
        return facade_result(view, result)

    @mcp.tool()
    def campaign_change(
        campaign_id: str,
        payload: dict[str, Any],
        action: Literal[
            "update", "clock_set", "clock_advance", "effect_add", "effect_remove"
        ] = "update",
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Update campaign metadata, its clock, or structured campaign-space effects."""
        data = facade_payload(payload)
        if action == "clock_set":
            result = campaign_clock_set_legacy(
                campaign_id,
                required(data, "day"),
                data.get("hour", 0),
                data.get("minute", 0),
                data.get("label", ""),
                principal_id,
                expected_revision,
                branch_id,
                idempotency_key,
            )
        elif action == "clock_advance":
            result = campaign_advance_effects_legacy(
                campaign_id,
                required(data, "period"),
                data.get("count", 1),
                principal_id,
                expected_revision,
                branch_id,
                idempotency_key,
            )
        elif action in {"effect_add", "effect_remove"}:
            result = campaign_world_effect_change(
                campaign_id,
                action,
                data,
                principal_id,
                expected_revision,
                branch_id,
                idempotency_key,
            )
        else:
            result = campaign_update(
                campaign_id,
                data.get("name"),
                data.get("status"),
                data.get("description"),
                data.get("settings"),
                data.get("state"),
                principal_id,
                expected_revision,
                idempotency_key,
            )
        return facade_result(action, result)

    @mcp.tool()
    def access_grant(
        scope: Literal["campaign", "actor"],
        campaign_id: str,
        principal_id: str,
        payload: dict[str, Any] | None = None,
        by_principal_id: str | None = None,
    ) -> dict[str, Any]:
        """Grant campaign membership or actor-level authority without exposing unrelated edits."""
        data = facade_payload(payload)
        if scope == "campaign":
            result = campaign_member_grant(
                campaign_id, principal_id, data.get("role", "player"), by_principal_id
            )
        else:
            result = actor_grant(
                campaign_id,
                principal_id,
                required(data, "actor_id"),
                data.get("can_control", False),
                data.get("can_view_private", False),
                by_principal_id,
            )
        return facade_result(scope, result)

    @mcp.tool()
    def campaign_event(
        campaign_id: str,
        action: Literal["add", "list"],
        payload: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Append an auditable campaign event or retrieve its branch-visible event log."""
        data = facade_payload(payload)
        if action == "add":
            result = event_add(
                campaign_id,
                required(data, "summary"),
                data.get("event_type", "narrative"),
                data.get("payload"),
                data.get("audience_scope", "dm"),
                data.get("branch_id"),
                data.get("known_by_actor_ids"),
                data.get("knowledge_key"),
                data.get("knowledge_proposition"),
                data.get("knowledge_disclosure_scope", "owner"),
                principal_id,
                idempotency_key,
            )
        else:
            result = event_list(
                campaign_id, data.get("limit", 50), data.get("branch_id"), principal_id
            )
        return facade_result(action, result)

    @mcp.tool()
    def memory_query(
        campaign_id: str,
        view: Literal["list", "search"] = "list",
        payload: dict[str, Any] | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Read objective campaign memory; actor knowledge remains a separate subjective store."""
        data = facade_payload(payload)
        result = (
            memory_search(
                campaign_id,
                required(data, "query"),
                data.get("limit", 8),
                data.get("branch_id"),
                principal_id,
            )
            if view == "search"
            else memory_list(campaign_id, data.get("kind"), data.get("branch_id"), principal_id)
        )
        return facade_result(view, result)

    @mcp.tool()
    def memory_change(
        campaign_id: str,
        content: str,
        kind: str = "fact",
        subject: str = "",
        metadata: dict[str, Any] | None = None,
        branch_id: str | None = None,
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Record an objective durable campaign fact."""
        return facade_result(
            "add",
            memory_add(
                campaign_id,
                content,
                kind,
                subject,
                metadata,
                branch_id,
                principal_id,
                idempotency_key,
            ),
        )

    @mcp.tool()
    def actor_knowledge_query(
        campaign_id: str,
        actor_id: str,
        view: Literal["list", "search"] = "list",
        payload: dict[str, Any] | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Read only one actor's branch-scoped, subjective knowledge."""
        data = facade_payload(payload)
        result = (
            actor_knowledge_search(
                campaign_id,
                actor_id,
                required(data, "query"),
                data.get("branch_id"),
                data.get("limit", 8),
                principal_id,
            )
            if view == "search"
            else actor_knowledge_list(campaign_id, actor_id, data.get("branch_id"), principal_id)
        )
        return facade_result(view, result)

    @mcp.tool()
    def actor_knowledge_change(
        action: Literal["add", "revise"],
        payload: dict[str, Any],
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Add or revise actor knowledge without crossing actor-knowledge boundaries."""
        data = facade_payload(payload)
        if action == "add":
            result = actor_knowledge_add(
                required(data, "campaign_id"),
                required(data, "actor_id"),
                required(data, "knowledge_key"),
                required(data, "proposition"),
                data.get("subject_ref", ""),
                data.get("epistemic_status", "known"),
                data.get("confidence", 3),
                data.get("source_event_id"),
                data.get("cause", "witnessed"),
                data.get("disclosure_scope", "dm"),
                data.get("branch_id"),
                principal_id,
                idempotency_key,
            )
        else:
            result = actor_knowledge_revise(
                required(data, "knowledge_id"),
                required(data, "proposition"),
                data.get("epistemic_status", "known"),
                data.get("confidence", 3),
                data.get("source_event_id"),
                data.get("cause", "told_by"),
                data.get("disclosure_scope", "dm"),
                data.get("branch_id"),
                principal_id,
                required(data, "expected_revision_id"),
                idempotency_key,
            )
        return facade_result(action, result)

    @mcp.tool()
    def branch_query(
        campaign_id: str,
        view: Literal["list", "compare"] = "list",
        payload: dict[str, Any] | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """List branches or compare two branch heads without changing checkout state."""
        data = facade_payload(payload)
        result = (
            branch_compare(
                campaign_id,
                required(data, "left_branch_id"),
                required(data, "right_branch_id"),
                principal_id,
            )
            if view == "compare"
            else branch_list(campaign_id, principal_id)
        )
        return facade_result(view, result)

    @mcp.tool()
    def branch_change(
        campaign_id: str,
        action: Literal["create", "checkout"],
        payload: dict[str, Any],
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        expected_branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create or checkout a branch under campaign and branch revision guards."""
        data = facade_payload(payload)
        if action == "create":
            result = branch_create(
                campaign_id,
                required(data, "name"),
                data.get("from_snapshot_id"),
                data.get("checkout", False),
                principal_id,
                expected_revision,
                expected_branch_id,
                idempotency_key,
            )
        else:
            result = branch_checkout(
                campaign_id,
                required(data, "branch_id"),
                principal_id,
                expected_revision,
                expected_branch_id,
                idempotency_key,
            )
        return facade_result(action, result)

    @mcp.tool()
    def snapshot_query(
        campaign_id: str,
        view: Literal["list", "verify", "lineage", "recap"] = "list",
        payload: dict[str, Any] | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Read snapshot history, integrity, lineage, or a regenerated recap."""
        data = facade_payload(payload)
        if view == "list":
            result = snapshot_list(campaign_id, principal_id)
        elif view == "verify":
            result = snapshot_verify(campaign_id, required(data, "slot"), principal_id)
        elif view == "lineage":
            result = snapshot_lineage(campaign_id, data.get("slot"), principal_id)
        else:
            result = snapshot_regenerate_recap(campaign_id, required(data, "slot"), principal_id)
        return facade_result(view, result)

    @mcp.tool()
    def state_revision(
        campaign_id: str,
        action: Literal["history", "undo", "redo"],
        payload: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Read revision history or perform guarded undo/redo."""
        data = facade_payload(payload)
        if action == "history":
            result = state_history(campaign_id, data.get("limit", 100), principal_id)
        elif action == "undo":
            result = state_undo(
                campaign_id, principal_id, data.get("expected_history_sequence"), idempotency_key
            )
        else:
            result = state_redo(
                campaign_id, principal_id, data.get("expected_history_sequence"), idempotency_key
            )
        return facade_result(action, result)

    @mcp.tool()
    def combat_query(
        campaign_id: str,
        view: Literal["status", "available_actions", "reactions"] = "status",
        actor_id: str | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Read combat status, legal actions, or legal reactions without committing combat state."""
        if view == "status":
            result = combat_status(campaign_id, principal_id)
        elif view == "available_actions":
            result = combat_available_actions(
                campaign_id, required({"actor_id": actor_id}, "actor_id"), principal_id
            )
        else:
            result = combat_reactions(
                campaign_id, required({"actor_id": actor_id}, "actor_id"), principal_id
            )
        return facade_result(view, result)

    @mcp.tool()
    def combat_movement(
        campaign_id: str,
        actor_id: str,
        action: Literal["move", "stand"],
        payload: dict[str, Any] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Move or stand a combatant while preserving movement and reaction checks."""
        data = facade_payload(payload)
        result = (
            combat_move(
                campaign_id,
                actor_id,
                required(data, "distance"),
                data.get("destination"),
                data.get("path"),
                data.get("movement_mode", "voluntary"),
                data.get("crawl", False),
                principal_id,
                expected_revision,
                branch_id,
                idempotency_key,
            )
            if action == "move"
            else combat_stand(
                campaign_id, actor_id, principal_id, expected_revision, branch_id, idempotency_key
            )
        )
        return facade_result(action, result)

    @mcp.tool()
    def combat_hp_change(
        campaign_id: str,
        target_id: str,
        action: Literal["damage", "heal"],
        payload: dict[str, Any],
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Apply structured damage or healing; damage parts and healing amounts stay distinct."""
        data = facade_payload(payload)
        result = (
            combat_apply_damage(
                campaign_id,
                target_id,
                required(data, "parts"),
                data.get("critical", False),
                principal_id,
                expected_revision,
                branch_id,
                idempotency_key,
                knock_out=data.get("knock_out", False),
                melee=data.get("melee", False),
            )
            if action == "damage"
            else combat_heal(
                campaign_id,
                target_id,
                required(data, "amount"),
                principal_id,
                expected_revision,
                branch_id,
                idempotency_key,
                source_actor_id=data.get("source_actor_id"),
                spell_id=data.get("spell_id"),
                spell_level=data.get("spell_level"),
            )
        )
        return facade_result(action, result)

    @mcp.tool()
    def combat_choice(
        campaign_id: str,
        actor_id: str,
        action: Literal["open", "resolve", "resolve_defense"],
        payload: dict[str, Any],
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Open or resolve a choice window; the engine still decides which choices are legal."""
        data = facade_payload(payload)
        if action == "open":
            result = combat_choice_open(
                campaign_id,
                actor_id,
                required(data, "event"),
                data.get("candidates"),
                data.get("kind", "reaction"),
                principal_id,
                expected_revision,
                branch_id,
                idempotency_key,
            )
        elif action == "resolve_defense":
            choice_id = required(data, "choice_id")
            _campaign, encounter = active_encounter(campaign_id)
            window = next(
                (item for item in encounter.get("pending", []) if item.get("id") == choice_id),
                None,
            )
            resolver = (
                combat_magic_missile_defense
                if isinstance(window, dict) and window.get("trigger") == "magic_missile_targeted"
                else combat_reaction_defense
            )
            result = resolver(
                campaign_id,
                actor_id,
                choice_id,
                required(data, "selection"),
                principal_id,
                branch_id,
                expected_revision,
                idempotency_key,
            )
        else:
            result = combat_choice_resolve(
                campaign_id,
                actor_id,
                required(data, "choice_id"),
                required(data, "selection"),
                principal_id,
                expected_revision,
                branch_id,
                idempotency_key,
            )
        return facade_result(action, result)

    @mcp.tool()
    def combat_ready(
        campaign_id: str,
        action: Literal[
            "ready_spell", "trigger_spell", "resolve_spell", "trigger_action", "resolve_action"
        ],
        payload: dict[str, Any],
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Run readied spell/action transitions without bypassing trigger or release validation."""
        data = facade_payload(payload)
        if action == "ready_spell":
            result = combat_ready_spell(
                campaign_id,
                required(data, "actor_id"),
                required(data, "spell_id"),
                required(data, "trigger"),
                data.get("cast_level"),
                data.get("declaration"),
                principal_id,
                expected_revision,
                branch_id,
                idempotency_key,
            )
        elif action == "trigger_spell":
            result = combat_readied_spell_trigger(
                campaign_id,
                required(data, "readied_id"),
                required(data, "event"),
                principal_id,
                expected_revision,
                branch_id,
                idempotency_key,
            )
        elif action == "resolve_spell":
            result = combat_readied_spell_resolve(
                campaign_id,
                required(data, "actor_id"),
                required(data, "choice_id"),
                required(data, "release"),
                data.get("declaration"),
                principal_id,
                expected_revision,
                branch_id,
                idempotency_key,
            )
        elif action == "trigger_action":
            result = combat_readied_action_trigger(
                campaign_id,
                required(data, "readied_id"),
                required(data, "event"),
                principal_id,
                expected_revision,
                branch_id,
                idempotency_key,
            )
        else:
            result = combat_readied_action_resolve(
                campaign_id,
                required(data, "actor_id"),
                required(data, "choice_id"),
                required(data, "release"),
                data.get("declaration"),
                principal_id,
                expected_revision,
                branch_id,
                idempotency_key,
            )
        return facade_result(action, result)

    @mcp.tool()
    def skill_query(
        kind: Literal["skill", "asset"],
        action: Literal["list", "read"],
        identifier: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        """List or read installed D&D skill documents and text assets."""
        if kind == "skill":
            result = (
                skill_list()
                if action == "list"
                else skill_read(required({"identifier": identifier}, "identifier"))
            )
        else:
            result = (
                skill_asset_list(source)
                if action == "list"
                else skill_asset_read(required({"identifier": identifier}, "identifier"))
            )
        return facade_result(action, result)

    @mcp.tool()
    def game_phase(
        campaign_id: str,
        action: Literal["get", "set"] = "get",
        tool_profile: Literal["lobby", "play"] | None = None,
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Get or set the persisted noncombat tool profile; combat is engine-controlled."""
        result = (
            game_phase_get(campaign_id, principal_id)
            if action == "get"
            else game_phase_set(
                campaign_id,
                required({"tool_profile": tool_profile}, "tool_profile"),
                principal_id,
                expected_revision,
                branch_id,
                idempotency_key,
            )
        )
        return facade_result(action, result)

    @mcp.tool()
    def exposure_open(
        campaign_id: str | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Start or replace this MCP session's server-owned tool exposure."""
        if campaign_id:
            access.require_campaign(campaign_id, principal_id)
            phase = authoritative_phase(campaign_id)
        else:
            phase = PROFILE_LOBBY
        request = mcp._request_session()
        if request is None:
            # Direct library callers have no protocol session. Give them an
            # explicit local scope rather than silently sharing state.
            session_key = f"direct:{principal_id}"
        else:
            session_key, _ = request
        exposure = exposures.open(
            session_key=session_key,
            principal_id=principal_id,
            campaign_id=campaign_id,
            phase=phase,
        )
        return {
            **exposures.status(exposure),
            "native_dynamic_tools": request is not None,
            "next": "Use exposure_search, exposure_inspect, then exposure_load.",
        }

    @mcp.tool()
    def exposure_status(exposure_id: str) -> dict[str, Any]:
        """Return the current phase, loaded groups, and visible tools for one exposure."""
        request = mcp._request_session()
        exposure = exposures.get(exposure_id, request[0] if request else None)
        if exposure.campaign_id:
            exposures.refresh_phase(exposure, authoritative_phase(exposure.campaign_id))
        return exposures.status(exposure)

    @mcp.tool()
    def exposure_search(
        query: str, phase: Literal["lobby", "play", "combat"] | None = None
    ) -> dict[str, Any]:
        """Search server capability groups before loading their full tool schemas."""
        return {"groups": exposures.search(query, phase), "catalog_version": "2026-07"}

    @mcp.tool()
    def exposure_inspect(group_id: str) -> dict[str, Any]:
        """Inspect one capability group, including its tools and phase boundary."""
        return exposures.inspect(group_id)

    @mcp.tool()
    async def exposure_load(
        exposure_id: str,
        group_id: str,
        ttl_calls: int | None = None,
    ) -> dict[str, Any]:
        """Expose one phase-compatible tool group to this MCP session."""
        request = mcp._request_session()
        exposure = exposures.get(exposure_id, request[0] if request else None)
        group = GROUP_BY_ID.get(group_id)
        if group is None:
            raise ExposureError(f"Unknown tool group: {group_id}")
        if group.roles:
            if exposure.campaign_id is None:
                raise ExposureError(f"Tool group {group_id!r} requires a campaign.")
            access.require_campaign(
                exposure.campaign_id, exposure.principal_id, roles=set(group.roles)
            )
        async with mcp._exposure_lock(exposure.id):
            if exposure.campaign_id:
                exposures.refresh_phase(exposure, authoritative_phase(exposure.campaign_id))
            exposures.load(exposure, group_id, ttl_calls)
        return exposures.status(exposure)

    @mcp.tool()
    async def exposure_unload(exposure_id: str, group_id: str) -> dict[str, Any]:
        """Remove a previously loaded tool group from this MCP session."""
        request = mcp._request_session()
        exposure = exposures.get(exposure_id, request[0] if request else None)
        async with mcp._exposure_lock(exposure.id):
            exposures.unload(exposure, group_id)
        return exposures.status(exposure)

    @mcp.tool()
    async def exposure_call(
        exposure_id: str,
        tool_id: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call an exposed tool when an MCP host cannot refresh native schemas."""
        if tool_id in CORE_TOOLS or tool_id.startswith("exposure_"):
            raise ExposureError("exposure_call only dispatches a loaded domain tool.")
        request = mcp._request_session()
        exposure = exposures.get(exposure_id, request[0] if request else None)
        if exposure.campaign_id:
            exposures.refresh_phase(exposure, authoritative_phase(exposure.campaign_id))
        bound_arguments = mcp._bind_exposure_principal(
            exposure, tool_id, dict(arguments or {}), inject_missing=True
        )
        validate_exposure_scope(exposure, tool_id, bound_arguments)
        context = mcp.get_context()
        async with mcp._exposure_lock(exposure.id):
            exposures.require_tool(exposure, tool_id)
            called = await mcp._tool_manager.call_tool(
                tool_id, bound_arguments, context=context, convert_result=True
            )
            if isinstance(called, tuple) and len(called) == 2:
                content, structured = called
                result = structured if structured is not None else content
            else:
                result = called
            exposure_changed = exposures.consume_tool(exposure, tool_id)
        target_campaign_id = str(bound_arguments.get("campaign_id") or "") or None
        if target_campaign_id and tool_id in {"game_phase", "combat_start", "combat_end"}:
            if request is not None:
                await mcp._refresh(request[0], target_campaign_id)
        elif exposure_changed and request is not None:
            await request[1].send_tool_list_changed()
        return {"tool_id": tool_id, "result": result, "exposure": exposures.status(exposure)}

    # No compatibility aliases: old tool names are removed before the server
    # advertises its capability list.  The underlying functions remain local
    # implementation seams so their validation is reused by the facades.
    retired_tool_names = {
        "campaign_list",
        "campaign_get",
        "campaign_member_grant",
        "campaign_update",
        "game_phase_get",
        "game_phase_set",
        "import_job_get",
        "import_job_list",
        "rule_import_job_create",
        "rule_import_job_inspect",
        "rule_import_job_ingest",
        "rule_content_candidates_extract",
        "import_job_review_candidates",
        "rule_import_job_compile",
        "rule_import_job_install",
        "rule_import_job_activate",
        "rule_document_stage",
        "rule_document_inspect",
        "rule_document_import",
        "rule_ingest",
        "module_import_job_create",
        "module_import_job_inspect",
        "module_import_job_validate",
        "module_import_job_import",
        "module_import_job_activate",
        "module_write",
        "module_inspect",
        "module_import_legacy",
        "module_list",
        "module_index",
        "module_read_scene",
        "module_current",
        "rule_pack_draft",
        "rule_pack_draft_from_source",
        "rule_pack_install",
        "rule_pack_list",
        "rule_pack_inspect",
        "rule_pack_test",
        "rule_pack_remove",
        "campaign_rule_profile_get",
        "campaign_rule_profile_set",
        "campaign_rule_pack_set",
        "campaign_rule_pack_remove",
        "campaign_rules_explain",
        "campaign_rule_receipts",
        "content_catalog_list",
        "character_create",
        "character_list",
        "character_library_list",
        "character_instantiate",
        "character_build",
        "character_get",
        "character_update",
        "character_rule_artifact_add",
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
        "character_spell_prepare_list",
        "character_memory_add",
        "character_memory_resolve",
        "party_show",
        "party_inventory_add",
        "party_inventory_remove",
        "party_inventory_transfer",
        "party_wallet_adjust",
        "party_wallet_transfer",
        "memory_add",
        "memory_list",
        "memory_search",
        "event_add",
        "event_list",
        "actor_grant",
        "actor_knowledge_add",
        "actor_knowledge_revise",
        "actor_knowledge_list",
        "actor_knowledge_search",
        "branch_list",
        "branch_compare",
        "branch_create",
        "branch_checkout",
        "snapshot_list",
        "snapshot_verify",
        "snapshot_lineage",
        "snapshot_regenerate_recap",
        "state_history",
        "state_undo",
        "state_redo",
        "combat_status",
        "combat_available_actions",
        "combat_reactions",
        "combat_move",
        "combat_stand",
        "combat_apply_damage",
        "combat_heal",
        "combat_choice_open",
        "combat_choice_resolve",
        "combat_ready_spell",
        "combat_readied_spell_trigger",
        "combat_readied_spell_resolve",
        "combat_readied_action_trigger",
        "combat_readied_action_resolve",
        "skill_list",
        "skill_read",
        "skill_asset_list",
        "skill_asset_read",
    }
    for retired_tool_name in retired_tool_names:
        mcp.remove_tool(retired_tool_name)

    registered_tools = mcp._tool_manager.list_tools()
    validate_profile_coverage(tool.name for tool in registered_tools)
    for registered_tool in registered_tools:
        registered_tool.meta = {
            **dict(registered_tool.meta or {}),
            "sagasmith_tool_profiles": list(profiles_for_tool(registered_tool.name)),
            "sagasmith_tool_groups": list(groups_for_tool(registered_tool.name)),
        }

    return mcp


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
