"""Machine-readable field contracts for compact MCP facades.

The public facade schemas intentionally keep ``payload`` as an object so the
tool catalog remains compact.  A zero-knowledge client can query these
action-specific contracts through ``exposure_inspect`` before calling a
facade.  Runtime validation remains authoritative.
"""

from __future__ import annotations

from typing import Any


def _variant(
    allowed: tuple[str, ...],
    required: tuple[str, ...] = (),
    *,
    when: str | None = None,
    additional_properties: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "allowed_fields": sorted(allowed),
        "required_fields": list(required),
        "optional_fields": sorted(set(allowed) - set(required)),
        "additional_properties": additional_properties,
    }
    if when is not None:
        result["when"] = when
    return result


def _guide(
    allowed: tuple[str, ...],
    required: tuple[str, ...] = (),
    *,
    when: str | None = None,
) -> dict[str, Any]:
    """Describe fields used by a legacy permissive facade without overstating strictness."""

    return _variant(
        allowed,
        required,
        when=when,
        additional_properties=True,
    )


ACTION_PAYLOAD_CONTRACTS: dict[str, dict[str, list[dict[str, Any]]]] = {
    "campaign_query": {
        "list": [_variant(("status",))],
        "get": [_variant(("campaign_id",), ("campaign_id",))],
        "party": [_variant(("campaign_id",), ("campaign_id",))],
        "binding": [_variant(("campaign_id",), ("campaign_id",))],
        "resume": [
            _variant(
                (
                    "actor_id",
                    "audience",
                    "budget_chars",
                    "campaign_id",
                    "limit",
                    "query",
                    "related_refs",
                    "scope_id",
                ),
                ("campaign_id",),
            )
        ],
    },
    "module_query": {
        "list": [_guide(())],
        "index": [_guide(("module_id",))],
        "scene": [_guide(("scene_id", "scope_id"), ("scene_id",))],
        "current": [_guide(("scope_id",))],
        "progress": [_guide(("module_id", "scope_id"))],
        "readiness": [
            _guide(
                ("participant_manifest", "scene_id"),
                ("scene_id", "participant_manifest"),
            )
        ],
        "assets": [_guide(("module_id",), ("module_id",))],
        "content": [
            _guide(
                ("review_id",),
                ("review_id",),
                when="read one content review",
            ),
            _guide(
                ("content_key", "content_kind", "module_id"),
                ("module_id",),
                when="list content reviews",
            ),
        ],
        "candidates": [_guide(("module_id",), ("module_id",))],
        "actors": [
            _guide(
                ("binding_kind", "module_id", "scene_id"),
                ("module_id",),
            )
        ],
        "package": [
            _guide(
                (
                    "dependencies",
                    "include_package",
                    "metadata",
                    "module_id",
                    "portable_id",
                    "version",
                ),
                ("module_id", "portable_id"),
            )
        ],
    },
    "rule_pack_compile": {
        "draft": [
            _guide(
                ("artifacts", "manifest", "mechanics", "provenance"),
                ("manifest",),
            )
        ],
        "from_source": [
            _guide(
                (
                    "artifacts",
                    "manifest",
                    "mechanics",
                    "provenance",
                    "source_id",
                ),
                ("source_id", "manifest"),
            )
        ],
    },
    "rule_pack_query": {
        "list": [_guide(("pack_id",))],
        "inspect": [_guide(("pack_id", "version"), ("pack_id", "version"))],
        "test": [_guide(("pack_id", "version"), ("pack_id", "version"))],
        "content_catalog": [
            _guide(
                ("branch_id", "campaign_id", "kind", "query"),
                ("campaign_id",),
            )
        ],
        "sources": [_guide(("edition", "system_id"))],
        "source_chunks": [
            _guide(
                ("limit", "page", "query", "source_id"),
                ("source_id",),
            )
        ],
        "actor_presets": [
            _guide(
                ("artifact_id", "edition", "include_package"),
                ("edition",),
            )
        ],
        "addons": [
            _variant(("addon_id", "branch_id", "campaign_id"), ("campaign_id",))
        ],
        "addon": [
            _variant(
                ("addon_id", "campaign_id", "include_package", "version"),
                ("campaign_id", "addon_id", "version"),
            )
        ],
        "addon_package": [
            _variant(
                (
                    "campaign_id",
                    "components",
                    "include_package",
                    "manifest",
                    "metadata",
                    "portable_id",
                    "version",
                ),
                ("campaign_id", "portable_id", "version", "manifest", "components"),
            )
        ],
        "preset_package": [
            _variant(
                (
                    "allow_partial",
                    "campaign_id",
                    "include_package",
                    "metadata",
                    "pack_id",
                    "portable_id",
                    "version",
                ),
                ("campaign_id", "pack_id", "version", "portable_id"),
            )
        ],
        "package": [
            _guide(
                (
                    "campaign_id",
                    "include_package",
                    "metadata",
                    "pack_id",
                    "version",
                ),
                ("campaign_id", "pack_id", "version"),
            )
        ],
        "release": [
            _guide(
                (
                    "campaign_id",
                    "components",
                    "include_manifest",
                    "metadata",
                    "portable_id",
                    "version",
                ),
                ("campaign_id", "portable_id", "version", "components"),
            )
        ],
    },
    "campaign_rules": {
        "get_profile": [_guide(())],
        "set_profile": [
            _guide(
                ("edition", "locale", "options", "publications"),
                ("edition",),
            )
        ],
        "set_pack": [
            _guide(
                ("enabled", "options", "pack_id", "version"),
                ("pack_id", "version"),
            )
        ],
        "remove_pack": [_guide(("pack_id",), ("pack_id",))],
        "set_addon": [
            _variant(
                ("addon_id", "enabled", "options", "version"),
                ("addon_id", "version"),
            )
        ],
        "core_relock": [
            _variant(
                (
                    "expected_core_fingerprint",
                    "expected_head_snapshot_id",
                    "reason",
                ),
                (
                    "expected_core_fingerprint",
                    "reason",
                    "expected_head_snapshot_id",
                ),
            )
        ],
        "explain": [_guide(("event",))],
        "receipts": [_guide(("limit", "mechanic_id"))],
    },
    "character_query": {
        "get": [_guide(("character_id",), ("character_id",))],
        "batch": [
            _guide(
                ("campaign_id", "character_ids"),
                ("campaign_id", "character_ids"),
            )
        ],
        "list": [_guide(("campaign_id",))],
        "library": [_guide(("character_type",))],
        "document": [
            _guide(
                ("campaign_id", "expected_checksum", "source_path"),
                ("campaign_id", "source_path"),
            )
        ],
        "rest": [
            _guide(
                (
                    "arcane_recovery",
                    "attune_item_id",
                    "attunement_prerequisite_confirmed",
                    "character_id",
                    "duration_minutes",
                    "hit_dice_spends",
                    "natural_recovery",
                    "rest_activity_minutes",
                    "rest_schedule",
                    "rest_type",
                    "song_of_rest_source_actor_id",
                    "sorcerous_restoration_points",
                ),
                ("character_id", "duration_minutes", "rest_type"),
            )
        ],
        "advancement": [
            _guide(
                ("character_id", "class_name"),
                ("character_id", "class_name"),
            )
        ],
        "portable_card": [
            _guide(
                (
                    "bindings",
                    "character_id",
                    "dependencies",
                    "metadata",
                    "portable_id",
                    "provenance",
                    "version",
                ),
                ("character_id",),
            )
        ],
    },
    "character_create_from": {
        "direct": [
            _guide(
                (
                    "campaign_id",
                    "character_type",
                    "name",
                    "notes",
                    "player_name",
                    "sheet",
                    "summary",
                ),
                ("name",),
            )
        ],
        "build": [
            _guide(
                (
                    "campaign_id",
                    "name",
                    "notes",
                    "player_name",
                    "sheet",
                    "summary",
                ),
                ("campaign_id", "name"),
            )
        ],
        "template": [
            _guide(
                ("campaign_id", "name", "player_name", "template_id"),
                ("template_id", "campaign_id"),
            )
        ],
        "statblock": [
            _guide(
                (
                    "campaign_id",
                    "character_type",
                    "chunk_ids",
                    "name",
                    "notes",
                    "source_id",
                    "source_statblock_name",
                    "summary",
                    "variant",
                ),
                ("campaign_id", "source_id"),
            )
        ],
        "reviewed_rule_statblock": [
            _guide(
                (
                    "campaign_id",
                    "character_type",
                    "job_id",
                    "name",
                    "notes",
                    "review_id",
                    "summary",
                    "variant",
                ),
                ("campaign_id", "job_id", "review_id"),
            )
        ],
        "module_statblock": [
            _guide(
                (
                    "campaign_id",
                    "character_type",
                    "name",
                    "notes",
                    "review_id",
                    "summary",
                    "variant",
                ),
                ("campaign_id", "review_id"),
            )
        ],
        "narrative_npc": [
            _guide(
                (
                    "campaign_id",
                    "identity_agent_ruling",
                    "instance_key",
                    "name",
                    "role",
                    "source_excerpt",
                    "source_identity",
                    "source_ref",
                    "summary",
                ),
                (
                    "campaign_id",
                    "name",
                    "role",
                    "summary",
                    "source_excerpt",
                    "source_ref",
                ),
            )
        ],
        "portable_card": [
            _guide(
                (
                    "artifact",
                    "artifact_id",
                    "campaign_id",
                    "card",
                    "name",
                    "player_name",
                    "source_path",
                ),
            )
        ],
    },
    "inventory_change": {
        "add": [_guide(("item",), ("item",))],
        "update": [_guide(("item_id", "patch"), ("item_id", "patch"))],
        "remove": [_guide(("item_id", "quantity"), ("item_id",))],
        "equip": [_guide(("item_id", "slot"), ("item_id", "slot"))],
        "recharge": [_guide(("item_id", "trigger"), ("item_id", "trigger"))],
        "consume_ammunition": [
            _guide(("quantity", "weapon_id"), ("weapon_id",))
        ],
    },
    "inventory_transfer": {
        "character_to_character": [
            _guide(
                (
                    "expected_campaign_revision",
                    "expected_source_revision",
                    "expected_target_revision",
                    "item_id",
                    "quantity",
                    "source_character_id",
                    "target_character_id",
                ),
                (
                    "source_character_id",
                    "target_character_id",
                    "item_id",
                    "expected_campaign_revision",
                    "expected_source_revision",
                    "expected_target_revision",
                ),
            )
        ],
        "party_to_character": [
            _guide(
                (
                    "campaign_id",
                    "character_id",
                    "expected_campaign_revision",
                    "expected_character_revision",
                    "item_id",
                    "quantity",
                ),
                (
                    "campaign_id",
                    "character_id",
                    "item_id",
                    "expected_campaign_revision",
                    "expected_character_revision",
                ),
            )
        ],
        "character_to_party": [
            _guide(
                (
                    "campaign_id",
                    "character_id",
                    "expected_campaign_revision",
                    "expected_character_revision",
                    "item_id",
                    "quantity",
                ),
                (
                    "campaign_id",
                    "character_id",
                    "item_id",
                    "expected_campaign_revision",
                    "expected_character_revision",
                ),
            )
        ],
    },
    "wallet_change": {
        "adjust": [_guide(())],
        "transfer_to_character": [
            _guide(
                (
                    "character_id",
                    "expected_campaign_revision",
                    "expected_character_revision",
                ),
                (
                    "character_id",
                    "expected_campaign_revision",
                    "expected_character_revision",
                ),
            )
        ],
        "transfer_from_character": [
            _guide(
                (
                    "character_id",
                    "expected_campaign_revision",
                    "expected_character_revision",
                ),
                (
                    "character_id",
                    "expected_campaign_revision",
                    "expected_character_revision",
                ),
            )
        ],
    },
    "character_state_change": {
        "effect_add": [_guide(("effect",), ("effect",))],
        "effect_remove": [_guide(("effect_id",), ("effect_id",))],
        "resource_set": [
            _guide(("resource", "value"), ("resource", "value"))
        ],
        "exhaustion_set": [_guide(("value",), ("value",))],
        "damage": [
            _guide(
                ("critical", "knock_out", "melee", "parts"),
                ("parts",),
            )
        ],
        "heal": [
            _guide(
                (
                    "amount",
                    "source_actor_id",
                    "spell_id",
                    "spell_level",
                ),
                ("amount",),
            )
        ],
        "revive": [
            _variant(
                (
                    "body_intact",
                    "elapsed_days",
                    "reason",
                    "soul_willing",
                    "source_actor_id",
                    "source_ref",
                ),
                (
                    "elapsed_days",
                    "soul_willing",
                    "body_intact",
                    "source_ref",
                    "reason",
                ),
            )
        ],
        "level_advance": [
            _variant(
                ("class_name", "hp_method", "reason", "source_ref"),
                ("class_name", "hp_method", "reason", "source_ref"),
            )
        ],
        "resource_sync": [_variant(("reason",), ("reason",))],
        "source_state": [
            _guide(
                ("reason", "source_ref", "state"),
                ("state", "source_ref", "reason"),
            )
        ],
        "stand": [_guide(())],
        "knock_prone": [_guide(())],
    },
    "character_action": {
        "cast_spell": [
            _guide(
                (
                    "cast_level",
                    "component_ruling",
                    "ritual",
                    "signature_free_cast",
                    "source_item_id",
                    "spell_id",
                    "target_character_ids",
                    "willing_target_ids",
                ),
                ("spell_id",),
            )
        ],
        "use_activity": [
            _guide(("activity_id", "declaration"), ("activity_id",))
        ],
        "attack_source_object": [
            _guide(
                (
                    "advantage",
                    "disadvantage",
                    "expected_campaign_revision",
                    "object",
                    "reason",
                    "source_ref",
                    "weapon_id",
                ),
                (
                    "object",
                    "weapon_id",
                    "source_ref",
                    "reason",
                    "expected_campaign_revision",
                ),
            )
        ],
    },
    "character_spell_prepare": {
        "set": [
            _guide(("prepared", "spell_id"), ("spell_id", "prepared"))
        ],
        "replace_all": [
            _guide(("event", "spell_ids"), ("spell_ids",))
        ],
    },
    "playthrough_manifest": {
        "get": [_guide(())],
        "initialize": [_guide(("manifest",), ("manifest",))],
        "replace": [_guide(("manifest",), ("manifest",))],
        "extend_modules": [_guide(("manifest",), ("manifest",))],
        "sync": [_guide(())],
        "verify_ending": [
            _guide(("condition_id",), ("condition_id",))
        ],
    },
    "campaign_event": {
        "add": [
            _guide(
                (
                    "audience_scope",
                    "branch_id",
                    "event_type",
                    "knowledge_disclosure_scope",
                    "knowledge_key",
                    "knowledge_proposition",
                    "known_by_actor_ids",
                    "payload",
                    "summary",
                ),
                ("summary",),
            )
        ],
        "list": [_guide(("actor_id", "branch_id", "limit"))],
    },
    "memory_query": {
        "list": [_guide(("branch_id", "include_inactive", "kind"))],
        "search": [
            _guide(
                ("branch_id", "include_inactive", "limit", "query"),
                ("query",),
            )
        ],
        "diagnostics": [_variant(("branch_id",))],
    },
    "memory_change": {
        "add": [
            _guide(
                (
                    "branch_id",
                    "content",
                    "disclosure_scope",
                    "fact_key",
                    "importance",
                    "kind",
                    "metadata",
                    "predicate",
                    "source_event_ids",
                    "status",
                    "subject",
                    "subject_ref",
                    "valid_from",
                    "valid_to",
                ),
                ("content",),
            )
        ],
        "upsert": [
            _guide(
                (
                    "branch_id",
                    "content",
                    "disclosure_scope",
                    "expected_revision_id",
                    "fact_key",
                    "importance",
                    "kind",
                    "metadata",
                    "predicate",
                    "source_event_ids",
                    "status",
                    "subject",
                    "subject_ref",
                    "valid_from",
                    "valid_to",
                ),
                ("fact_key", "content"),
            )
        ],
        "revise": [
            _guide(
                (
                    "branch_id",
                    "content",
                    "disclosure_scope",
                    "expected_revision_id",
                    "importance",
                    "memory_id",
                    "metadata",
                    "source_event_ids",
                    "status",
                    "valid_from",
                    "valid_to",
                ),
                ("memory_id", "expected_revision_id", "content"),
            )
        ],
        "supersede": [
            _guide(
                (
                    "branch_id",
                    "content",
                    "disclosure_scope",
                    "expected_revision_id",
                    "importance",
                    "memory_id",
                    "metadata",
                    "source_event_ids",
                    "valid_from",
                    "valid_to",
                ),
                ("memory_id", "expected_revision_id"),
            )
        ],
        "commit": [
            _variant(
                (
                    "actor_knowledge",
                    "branch_id",
                    "context_receipt",
                    "event",
                    "facts",
                    "snapshot",
                ),
                ("event",),
            )
        ],
    },
    "actor_knowledge_query": {
        "list": [_guide(("branch_id",))],
        "search": [
            _guide(("branch_id", "limit", "query"), ("query",))
        ],
    },
    "actor_knowledge_change": {
        "add": [
            _guide(
                (
                    "actor_id",
                    "branch_id",
                    "campaign_id",
                    "cause",
                    "confidence",
                    "disclosure_scope",
                    "epistemic_status",
                    "knowledge_key",
                    "proposition",
                    "source_event_id",
                    "subject_ref",
                ),
                (
                    "campaign_id",
                    "actor_id",
                    "knowledge_key",
                    "proposition",
                ),
            )
        ],
        "revise": [
            _guide(
                (
                    "branch_id",
                    "cause",
                    "confidence",
                    "disclosure_scope",
                    "epistemic_status",
                    "expected_revision_id",
                    "knowledge_id",
                    "proposition",
                    "source_event_id",
                ),
                (
                    "knowledge_id",
                    "proposition",
                    "expected_revision_id",
                ),
            )
        ],
    },
    "branch_query": {
        "list": [_guide(())],
        "compare": [
            _guide(
                ("left_branch_id", "right_branch_id"),
                ("left_branch_id", "right_branch_id"),
            )
        ],
    },
    "branch_change": {
        "create": [
            _guide(("checkout", "from_snapshot_id", "name"), ("name",))
        ],
        "checkout": [_guide(("branch_id",), ("branch_id",))],
        "create_core_upgrade": [
            _guide(
                (
                    "expected_runtime_core_fingerprint",
                    "expected_snapshot_core_fingerprint",
                    "name",
                    "reason",
                    "slot",
                ),
                (
                    "slot",
                    "name",
                    "expected_snapshot_core_fingerprint",
                    "expected_runtime_core_fingerprint",
                    "reason",
                ),
            )
        ],
    },
    "snapshot_query": {
        "list": [_guide(())],
        "verify": [_guide(("slot",), ("slot",))],
        "lineage": [_guide(("slot",))],
        "recap": [_guide(("slot",), ("slot",))],
        "core": [_guide(("slot",), ("slot",))],
    },
    "state_revision": {
        "history": [_guide(("limit",))],
        "receipt": [
            _guide(
                ("branch_id", "idempotency_key"),
                ("idempotency_key",),
            )
        ],
        "undo": [_guide(("expected_history_sequence",))],
        "redo": [_guide(("expected_history_sequence",))],
    },
    "combat_common_action": {
        **{
            action: [
                _guide(
                    (
                        "end_action_description",
                        "end_ongoing_effect_id",
                        "source_excerpt",
                    )
                )
            ]
            for action in (
                "dash",
                "disengage",
                "dodge",
                "escape",
                "help",
                "hide",
                "influence",
                "ready",
                "search",
                "stabilize",
                "study",
                "use_object",
                "utilize",
            )
        },
        "improvise": [
            _guide(("agent_ruling_commitment", "procedure_id"))
        ],
        "interact_object": [
            _variant(
                ("interaction", "object_description"),
                ("object_description", "interaction"),
                when="ordinary object interaction",
            ),
            _variant(
                (
                    "agent_ruling",
                    "interaction",
                    "object_description",
                    "remove_source_condition",
                    "source_excerpt",
                    "source_ref",
                ),
                (
                    "object_description",
                    "interaction",
                    "remove_source_condition",
                    "source_ref",
                    "source_excerpt",
                    "agent_ruling",
                ),
                when="remove an encounter-owned source condition",
            ),
        ],
        "shake_hypnotic_pattern": [_variant(())],
        "sustain_spell": [
            _variant(
                ("agent_ruling", "effect_id", "target_total_cover"),
                ("effect_id", "target_total_cover", "agent_ruling"),
            )
        ],
        "detach_attachment": [
            _guide(("effect_id",), ("effect_id",))
        ],
    },
    "combat_query": {
        "status": [_guide(())],
        "available_actions": [_guide(())],
        "reactions": [_guide(())],
        "transaction_history": [_guide(("limit",))],
        "transaction_receipt": [
            _guide(
                ("branch_id", "idempotency_key"),
                ("idempotency_key",),
            )
        ],
    },
    "combat_movement": {
        "move": [
            _guide(
                (
                    "crawl",
                    "destination",
                    "distance",
                    "movement_mode",
                    "path",
                ),
                ("distance",),
            )
        ],
        "stand": [_guide(())],
    },
    "combat_hp_change": {
        "damage": [
            _guide(
                ("critical", "knock_out", "melee", "parts"),
                ("parts",),
            )
        ],
        "heal": [
            _guide(
                (
                    "amount",
                    "source_actor_id",
                    "spell_id",
                    "spell_level",
                ),
                ("amount",),
            )
        ],
        "stabilize": [
            _guide(("source_excerpt",), ("source_excerpt",))
        ],
        "save_damage": [
            _variant(
                (
                    "agent_ruling",
                    "damage_expression",
                    "damage_type",
                    "half_on_success",
                    "mechanic_source_excerpt",
                    "save_ability",
                    "save_advantage",
                    "save_dc",
                    "save_disadvantage",
                    "source_actor_id",
                    "source_card_id",
                    "source_card_kind",
                    "target_ids",
                ),
                (
                    "source_actor_id",
                    "source_card_id",
                    "source_card_kind",
                    "save_ability",
                    "save_dc",
                    "damage_expression",
                    "damage_type",
                    "half_on_success",
                    "mechanic_source_excerpt",
                    "agent_ruling",
                ),
            )
        ],
    },
    "combat_ready": {
        "ready_spell": [
            _guide(
                ("actor_id", "cast_level", "declaration", "spell_id", "trigger"),
                ("actor_id", "spell_id", "trigger"),
            )
        ],
        "trigger_spell": [
            _guide(("event", "readied_id"), ("readied_id", "event"))
        ],
        "resolve_spell": [
            _guide(
                ("actor_id", "choice_id", "declaration", "release"),
                ("actor_id", "choice_id", "release"),
            )
        ],
        "trigger_action": [
            _guide(("event", "readied_id"), ("readied_id", "event"))
        ],
        "resolve_action": [
            _guide(
                ("actor_id", "choice_id", "declaration", "release"),
                ("actor_id", "choice_id", "release"),
            )
        ],
    },
    "character_check": {
        "reroll": [
            _variant(
                (
                    "actor_id",
                    "expected_original_roll",
                    "resolution_id",
                    "roll_index",
                ),
                (
                    "actor_id",
                    "resolution_id",
                    "roll_index",
                    "expected_original_roll",
                ),
            )
        ],
        "check": [
            _variant(
                (
                    "ability",
                    "actor_id",
                    "advantage",
                    "bonus",
                    "dc",
                    "disadvantage",
                    "kind",
                    "proficient",
                    "rule_facts",
                ),
                ("actor_id", "kind", "ability"),
            )
        ],
        "group": [
            _variant(
                (
                    "ability",
                    "actor_ids",
                    "advantage",
                    "bonus",
                    "dc",
                    "disadvantage",
                    "proficient",
                    "rule_facts",
                ),
                ("actor_ids", "ability", "dc"),
            )
        ],
        "contest": [
            _variant(
                (
                    "source_ability",
                    "source_actor_id",
                    "source_advantage",
                    "source_bonus",
                    "source_disadvantage",
                    "source_proficient",
                    "source_rule_facts",
                    "target_ability",
                    "target_actor_id",
                    "target_advantage",
                    "target_bonus",
                    "target_disadvantage",
                    "target_proficient",
                    "target_rule_facts",
                ),
                (
                    "source_actor_id",
                    "target_actor_id",
                    "source_ability",
                    "target_ability",
                ),
            )
        ],
    },
    "chase": {
        "query": [_variant(())],
        "start": [
            _variant(
                (
                    "close_transition",
                    "initial_distance_ft",
                    "name",
                    "participant_config",
                    "participant_ids",
                    "quarry_ids",
                    "scene_id",
                    "source_excerpt",
                    "source_ref",
                ),
                (
                    "participant_ids",
                    "quarry_ids",
                    "initial_distance_ft",
                    "scene_id",
                    "source_ref",
                    "source_excerpt",
                ),
            )
        ],
        "take_turn": [
            _variant(
                (
                    "actor_id",
                    "complication_choice",
                    "expected_actor_revision",
                    "quarry_visibility",
                    "stand_from_prone",
                    "turn_action",
                ),
                (
                    "actor_id",
                    "turn_action",
                    "stand_from_prone",
                    "quarry_visibility",
                    "expected_actor_revision",
                ),
            )
        ],
        "end": [
            _variant(
                ("source_excerpt", "source_ref", "status", "summary"),
                ("status", "summary", "source_ref", "source_excerpt"),
            )
        ],
    },
    "rule_import": {
        "discover": [_variant(())],
        "import_addon": [
            _variant(("addon", "artifact", "source_path"))
        ],
        "import_package": [
            _variant(("artifact", "package", "source_path"))
        ],
        "inspect_release": [
            _variant(("artifact", "release_manifest", "source_path"))
        ],
        "stage": [
            _variant(
                (
                    "authority",
                    "edition",
                    "locale",
                    "publication_id",
                    "source_key",
                    "source_path",
                    "title",
                    "version",
                ),
                ("source_path", "source_key", "title", "edition"),
            )
        ],
        "inspect": [_variant(("job_id",), ("job_id",))],
        "render_page": [
            _variant(
                ("include_ocr_text", "job_id", "page_number", "scale"),
                ("job_id", "page_number"),
            )
        ],
        "recover_statblock": [
            _variant(
                ("agent_fill", "job_id", "name", "page_number"),
                ("job_id", "name"),
            )
        ],
        "recover_statblocks": [
            _variant(("job_id", "page_numbers"), ("job_id",))
        ],
        "ingest": [
            _variant(("acknowledge_warnings", "job_id"), ("job_id",))
        ],
        "review_statblock": [
            _variant(
                (
                    "agent_fill",
                    "evidence_chunk_ids",
                    "evidence_exclusions",
                    "job_id",
                    "normalized_content",
                    "observation",
                    "page_number",
                    "review_mode",
                ),
                ("job_id", "page_number", "normalized_content", "observation"),
                when="new review (base_review_id omitted)",
            ),
            _variant(
                (
                    "agent_fill",
                    "base_review_id",
                    "job_id",
                    "observation",
                ),
                ("job_id", "base_review_id", "observation", "agent_fill"),
                when="retry an existing review (base_review_id supplied)",
            ),
        ],
        "extract_candidates": [_variant(("job_id",), ("job_id",))],
        "augment_catalog": [
            _variant(
                ("additions", "job_id", "rationale"),
                ("job_id", "additions", "rationale"),
            )
        ],
        "review": [_variant(("decisions", "job_id"), ("job_id", "decisions"))],
        "compile": [
            _variant(
                ("job_id", "manifest", "mechanics", "provenance"),
                ("job_id", "manifest"),
            )
        ],
        "install": [_variant(("job_id",), ("job_id",))],
        "activate": [_variant(("job_id",), ("job_id",))],
    },
    "module_import": {
        "stage": [
            _variant(("content", "name", "source_key", "source_path", "title"))
        ],
        "attach_asset": [
            _variant(
                (
                    "asset_kind",
                    "location_key",
                    "metadata",
                    "module_id",
                    "scene_id",
                    "source_path",
                    "title",
                ),
                ("module_id", "source_path", "asset_kind"),
            )
        ],
        "inspect": [_variant(("job_id",), ("job_id",))],
        "validate": [_variant(("job_id",), ("job_id",))],
        "ingest": [_variant(("job_id",), ("job_id",))],
        "activate": [
            _variant(("job_id", "progress_remaps"), ("job_id",))
        ],
        "bind_actor": [
            _variant(
                (
                    "binding_kind",
                    "character_id",
                    "metadata",
                    "module_id",
                    "portable_actor_id",
                    "role",
                    "scene_id",
                ),
                (
                    "module_id",
                    "character_id",
                    "portable_actor_id",
                    "binding_kind",
                ),
            )
        ],
        "import_package": [
            _variant(("activate", "artifact", "package", "source_path"))
        ],
    },
    "module_review": {
        "render_page": [
            _variant(
                (
                    "include_ocr_text",
                    "module_id",
                    "page_number",
                    "scale",
                    "source_asset_id",
                ),
                ("module_id", "page_number"),
            )
        ],
        "recover_statblock": [
            _variant(
                (
                    "agent_fill",
                    "content_key",
                    "module_id",
                    "name",
                    "page_number",
                    "scene_id",
                    "source_asset_id",
                ),
                ("module_id", "scene_id", "content_key", "name", "page_number"),
            )
        ],
        "submit_content": [
            _variant(
                (
                    "agent_fill",
                    "content_key",
                    "content_kind",
                    "metadata",
                    "module_id",
                    "normalized_content",
                    "observation",
                    "page_number",
                    "scene_id",
                    "source_asset_id",
                    "source_chunk_ids",
                ),
                (
                    "module_id",
                    "scene_id",
                    "content_key",
                    "normalized_content",
                    "observation",
                ),
            )
        ],
    },
    "campaign_change": {
        "update": [
            _variant(("description", "name", "settings", "state", "status"))
        ],
        "clock_set": [
            _variant(("day", "hour", "label", "minute"), ("day",))
        ],
        "clock_advance": [
            _variant(
                (
                    "count",
                    "expected_elapsed_ticks",
                    "expected_world_time",
                    "period",
                ),
                ("period",),
            )
        ],
        "party_rest": [
            _variant(
                ("duration_minutes", "members", "rest_type"),
                ("members",),
            )
        ],
        "stable_recovery": [
            _variant(("members", "resting_members"), ("members",))
        ],
        "effect_add": [_variant(("effect",), ("effect",))],
        "effect_remove": [
            _variant(("effect_id", "reason"), ("effect_id",))
        ],
        "advancement_configure": [_variant(("mode",), ("mode",))],
        "experience_award": [
            _variant(
                ("awards", "reason", "source_ref"),
                ("awards", "reason", "source_ref"),
            )
        ],
        "loot_acquire": [
            _variant(
                (
                    "acquisition_id",
                    "coins",
                    "items",
                    "reason",
                    "source_ref",
                ),
                ("acquisition_id", "reason", "source_ref"),
            )
        ],
        "currency_spend": [
            _variant(
                ("coins", "reason", "rule_ref", "source_ref", "spend_id"),
                ("spend_id", "coins", "reason", "source_ref", "rule_ref"),
            )
        ],
        "item_spend": [
            _variant(
                (
                    "character_id",
                    "expected_character_revision",
                    "item_id",
                    "quantity",
                    "reason",
                    "source_ref",
                    "spend_id",
                ),
                ("spend_id", "item_id", "quantity", "reason", "source_ref"),
            )
        ],
        "consumable_use": [
            _variant(
                (
                    "expected_character_revision",
                    "item_id",
                    "reason",
                    "target_character_id",
                    "use_id",
                ),
                (
                    "use_id",
                    "item_id",
                    "target_character_id",
                    "expected_character_revision",
                    "reason",
                ),
            )
        ],
        "combat_cleanup": [_variant(("outcome",), ("outcome",))],
    },
    "content_solution": {
        "query": [_variant(())],
        "compile": [
            _variant(
                ("agent_ruling", "resolution_plan"),
                ("resolution_plan", "agent_ruling"),
            )
        ],
    },
    "combat_choice": {
        "open": [
            _variant(("candidates", "event", "kind"), ("event",))
        ],
        "resolve": [
            _variant(("choice_id", "selection"), ("choice_id", "selection"))
        ],
        "resolve_defense": [
            _variant(("choice_id", "selection"), ("choice_id", "selection"))
        ],
        "on_hit_ruling": [
            _variant(("choice_id", "selection"), ("choice_id", "selection"))
        ],
        "execute_plan": [
            _variant(("commitment",), ("commitment",))
        ],
        "resolve_death_trigger": [
            _variant(
                ("choice_id", "environment_ruling"),
                ("choice_id", "environment_ruling"),
            )
        ],
    },
}


def action_payload_contract(
    *,
    tool_id: str,
    input_schema: dict[str, Any],
    selector: str | None = None,
) -> dict[str, Any]:
    """Return selector values plus exact field contracts where they exist."""

    properties = dict(input_schema.get("properties") or {})
    selector_field = next(
        (
            field
            for field in ("action", "view", "mode")
            if isinstance(properties.get(field), dict)
            and properties[field].get("enum")
        ),
        None,
    )
    selector_values = (
        list(properties[selector_field]["enum"]) if selector_field is not None else []
    )
    exact = ACTION_PAYLOAD_CONTRACTS.get(tool_id, {})
    if selector is not None and selector_values and selector not in selector_values:
        raise ValueError(
            f"unsupported {selector_field} {selector!r}; expected one of "
            + ", ".join(selector_values)
        )
    selected_values = [selector] if selector is not None else selector_values
    actions: dict[str, Any] = {}
    for value in selected_values:
        variants = exact.get(value)
        actions[value] = (
            {
                "contract_kind": (
                    "exact_field_contract"
                    if all(
                        not variant["additional_properties"]
                        for variant in variants
                    )
                    else "runtime_field_guide"
                ),
                "payload_variants": variants,
            }
            if variants is not None
            else {
                "contract_kind": "top_level_schema_and_workflow_reference",
                "payload_variants": None,
                "guidance_ref": "dnd:full/references/mcp-contract.md",
                "guidance_query": f"{tool_id} {selector_field}={value}",
            }
        )
    return {
        "tool_id": tool_id,
        "selector_field": selector_field,
        "selector_values": selector_values,
        "selected": selector,
        "top_level_input_schema": input_schema,
        "actions": actions,
        "runtime_validation_authoritative": True,
    }
