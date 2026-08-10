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
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "allowed_fields": sorted(allowed),
        "required_fields": list(required),
        "optional_fields": sorted(set(allowed) - set(required)),
        "additional_properties": False,
    }
    if when is not None:
        result["when"] = when
    return result


ACTION_PAYLOAD_CONTRACTS: dict[str, dict[str, list[dict[str, Any]]]] = {
    "npc_conversation": {
        "open": [
            _variant(
                ("branch_id", "idempotency_key", "participant_actor_ids", "query", "scope_id"),
                ("participant_actor_ids", "idempotency_key"),
            )
        ],
        "get": [_variant(("conversation_id",), ("conversation_id",))],
        "ingest": [
            _variant(
                (
                    "audience_facts",
                    "conversation_id",
                    "event",
                    "expected_conversation_revision",
                    "idempotency_key",
                ),
                (
                    "conversation_id",
                    "event",
                    "audience_facts",
                    "expected_conversation_revision",
                    "idempotency_key",
                ),
            )
        ],
        "publish": [
            _variant(
                (
                    "audience_facts",
                    "conversation_id",
                    "expected_conversation_revision",
                    "idempotency_key",
                    "publication_id",
                    "segment_audience_facts",
                ),
                (
                    "conversation_id",
                    "publication_id",
                    "audience_facts",
                    "expected_conversation_revision",
                    "idempotency_key",
                ),
            )
        ],
        "close": [
            _variant(
                (
                    "accepted_working_deltas",
                    "conversation_id",
                    "expected_conversation_revision",
                    "idempotency_key",
                ),
                ("conversation_id", "expected_conversation_revision", "idempotency_key"),
            )
        ],
        "abort": [
            _variant(
                ("conversation_id", "expected_conversation_revision", "idempotency_key"),
                ("conversation_id", "expected_conversation_revision", "idempotency_key"),
            )
        ],
    },
    "rulebook_draft": {
        "start": [
            _variant(
                (
                    "acknowledge_warnings",
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
        "get": [_variant(("job_id",))],
        "evidence": [
            _variant(
                ("include_ocr_text", "job_id", "kind", "page_number", "scale"),
                ("job_id", "kind", "page_number"),
                when="kind=page",
            ),
            _variant(
                ("job_id", "kind", "limit", "offset", "page_number", "query"),
                ("job_id", "kind"),
                when="kind=chunks",
            ),
        ],
        "edit": [
            _variant(
                ("decisions", "job_id", "operation"),
                ("job_id", "operation", "decisions"),
                when="operation=candidates",
            ),
            _variant(
                ("additions", "job_id", "operation", "rationale"),
                ("job_id", "operation", "additions", "rationale"),
                when="operation=catalog",
            ),
            _variant(
                (
                    "base_text_sha256",
                    "evidence_basis",
                    "job_id",
                    "operation",
                    "page_number",
                    "rationale",
                    "rendered_image_checksum",
                    "replacements",
                    "review_method",
                ),
                (
                    "operation",
                    "page_number",
                    "base_text_sha256",
                    "replacements",
                    "rationale",
                    "evidence_basis",
                ),
                when="operation=source_text",
            ),
            _variant(
                (
                    "agent_fill",
                    "correction_evidence_basis",
                    "job_id",
                    "name",
                    "ocr_corrections",
                    "operation",
                    "page_number",
                    "page_numbers",
                    "rendered_image_checksum",
                    "statblock_slot",
                ),
                ("job_id", "operation"),
                when="operation=statblock_recovery",
            ),
            _variant(
                (
                    "agent_fill",
                    "base_review_id",
                    "evidence_chunk_ids",
                    "evidence_exclusions",
                    "job_id",
                    "normalized_content",
                    "observation",
                    "operation",
                    "page_number",
                    "review_mode",
                ),
                ("job_id", "operation", "observation"),
                when="operation=statblock_review",
            ),
            _variant(
                ("acknowledge_warnings", "job_id", "operation"),
                ("job_id", "operation"),
                when="operation=advance",
            ),
        ],
        "finalize": [
            _variant(
                (
                    "confirmation",
                    "include_package",
                    "job_id",
                    "manifest",
                    "mechanics",
                    "metadata",
                    "provenance",
                ),
                ("job_id", "confirmation", "manifest"),
            )
        ],
    },
    "module_draft": {
        "start": [_variant(("content", "name", "source_key", "source_path", "title"))],
        "get": [_variant(("job_id",))],
        "evidence": [
            _variant(
                ("include_ocr_text", "job_id", "kind", "module_id", "page_number", "scale"),
                ("job_id", "page_number"),
                when="kind=page",
            ),
            _variant(
                ("include_ocr_text", "job_id", "kind", "module_id", "page_number", "scale"),
                ("module_id", "page_number"),
                when="kind=page by module_id",
            ),
            _variant(
                ("job_id", "kind", "limit", "module_id", "query", "scene_id"),
                ("job_id",),
                when="kind=chunks",
            ),
            _variant(
                ("job_id", "kind", "limit", "module_id", "query", "scene_id"),
                ("module_id",),
                when="kind=chunks by module_id",
            ),
        ],
        "edit": [
            _variant(
                (
                    "base_text_sha256",
                    "evidence_basis",
                    "job_id",
                    "operation",
                    "page_number",
                    "rationale",
                    "rendered_image_checksum",
                    "replacements",
                    "review_method",
                ),
                (
                    "operation",
                    "job_id",
                    "page_number",
                    "base_text_sha256",
                    "replacements",
                    "rationale",
                    "evidence_basis",
                ),
                when="operation=source_text",
            ),
            _variant(
                (
                    "agent_fill",
                    "content_key",
                    "content_kind",
                    "job_id",
                    "metadata",
                    "module_id",
                    "normalized_content",
                    "observation",
                    "operation",
                    "page_number",
                    "scene_id",
                    "source_asset_id",
                    "source_chunk_ids",
                ),
                (
                    "operation",
                    "scene_id",
                    "content_key",
                    "normalized_content",
                    "observation",
                ),
                when="operation=content",
            ),
            _variant(
                (
                    "agent_fill",
                    "content_key",
                    "job_id",
                    "module_id",
                    "name",
                    "operation",
                    "page_number",
                    "scene_id",
                    "source_asset_id",
                ),
                ("operation", "scene_id", "content_key", "name", "page_number"),
                when="operation=statblock",
            ),
            _variant(
                (
                    "asset_kind",
                    "job_id",
                    "location_key",
                    "metadata",
                    "module_id",
                    "operation",
                    "scene_id",
                    "source_path",
                    "title",
                ),
                ("operation", "source_path", "asset_kind"),
                when="operation=asset",
            ),
            _variant(
                (
                    "binding_kind",
                    "character_id",
                    "job_id",
                    "metadata",
                    "module_id",
                    "operation",
                    "actor_card_id",
                    "role",
                    "scene_id",
                ),
                ("operation", "character_id", "actor_card_id", "binding_kind"),
                when="operation=actor",
            ),
            _variant(
                (
                    "catalogs",
                    "dependencies",
                    "job_id",
                    "manifest",
                    "metadata",
                    "narrative",
                    "note",
                    "operation",
                    "version",
                ),
                ("job_id", "operation"),
                when="operation=package",
            ),
            _variant(("job_id", "operation"), ("job_id", "operation"), when="operation=advance"),
        ],
        "finalize": [
            _variant(
                (
                    "catalogs",
                    "confirmation",
                    "dependencies",
                    "include_package",
                    "job_id",
                    "manifest",
                    "metadata",
                    "narrative",
                    "pack_id",
                    "version",
                ),
                ("job_id", "pack_id", "confirmation"),
            )
        ],
    },
    "content_pack": {
        "list": [
            _variant(
                (
                    "addon_id",
                    "artifact_id",
                    "branch_id",
                    "campaign_id",
                    "edition",
                    "include_package",
                    "kind",
                    "pack_id",
                ),
                ("campaign_id", "kind"),
            )
        ],
        "get": [
            _variant(
                (
                    "addon_id",
                    "artifact",
                    "artifact_id",
                    "campaign_id",
                    "edition",
                    "include_package",
                    "kind",
                    "module_id",
                    "pack_id",
                    "source_path",
                    "version",
                ),
                ("campaign_id", "kind"),
            )
        ],
        "import": [
            _variant(
                (
                    "artifact",
                    "campaign_id",
                    "kind",
                    "progress_remaps",
                    "source_path",
                ),
                ("campaign_id", "kind"),
            )
        ],
        "export": [
            _variant(
                (
                    "addon_id",
                    "campaign_id",
                    "catalogs",
                    "dependencies",
                    "include_package",
                    "kind",
                    "manifest",
                    "metadata",
                    "module_id",
                    "narrative",
                    "pack_id",
                    "artifact_id",
                    "edition",
                    "version",
                ),
                ("campaign_id", "kind"),
            )
        ],
        "activate": [
            _variant(
                (
                    "addon_id",
                    "branch_id",
                    "campaign_id",
                    "enabled",
                    "kind",
                    "module_id",
                    "options",
                    "pack_id",
                    "progress_remaps",
                    "version",
                ),
                ("campaign_id", "kind"),
            )
        ],
        "deactivate": [
            _variant(
                (
                    "addon_id",
                    "branch_id",
                    "campaign_id",
                    "kind",
                    "module_id",
                    "options",
                    "pack_id",
                    "version",
                ),
                ("campaign_id", "kind"),
            )
        ],
        "remove": [
            _variant(
                ("addon_id", "campaign_id", "kind", "module_id", "pack_id", "version"),
                ("campaign_id", "kind"),
            )
        ],
    },
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
        "list": [_variant(())],
        "index": [_variant(("module_id",))],
        "scene": [_variant(("scene_id", "scope_id"), ("scene_id",))],
        "current": [_variant(("scope_id",))],
        "progress": [_variant(("module_id", "scope_id"))],
        "preflight": [
            _variant(
                ("participant_manifest", "scene_id"),
                ("scene_id", "participant_manifest"),
            )
        ],
        "assets": [_variant(("module_id",), ("module_id",))],
        "content": [
            _variant(
                ("review_id",),
                ("review_id",),
                when="read one content review",
            ),
            _variant(
                ("content_key", "content_kind", "module_id"),
                ("module_id",),
                when="list content reviews",
            ),
        ],
        "candidates": [_variant(("module_id",), ("module_id",))],
        "actors": [
            _variant(
                ("binding_kind", "module_id", "scene_id"),
                ("module_id",),
            )
        ],
    },
    "campaign_rules": {
        "get_profile": [_variant(())],
        "set_profile": [
            _variant(
                ("edition", "locale", "options", "publications"),
                ("edition",),
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
        "explain": [_variant(("event",))],
        "receipts": [_variant(("limit", "mechanic_id"))],
    },
    "character_query": {
        "catalog": [
            _variant(
                ("branch_id", "campaign_id", "include_context", "kind", "query"),
                ("campaign_id",),
            )
        ],
        "get": [_variant(("character_id",), ("character_id",))],
        "batch": [
            _variant(
                ("campaign_id", "character_ids"),
                ("campaign_id", "character_ids"),
            )
        ],
        "list": [_variant(("campaign_id",))],
        "library": [_variant(("character_type",))],
        "document": [
            _variant(
                ("campaign_id", "expected_checksum", "source_path"),
                ("campaign_id", "source_path"),
            )
        ],
        "rest": [
            _variant(
                (
                    "arcane_recovery",
                    "attune_item_id",
                    "attunement_prerequisite_confirmed",
                    "character_id",
                    "duration_minutes",
                    "hit_dice_spends",
                    "natural_recovery",
                    "rest_activity_minutes",
                    "rest_type",
                    "song_of_rest_source_actor_id",
                    "sorcerous_restoration_points",
                ),
                ("character_id", "duration_minutes", "rest_type"),
            )
        ],
        "advancement": [
            _variant(
                ("character_id", "class_name"),
                ("character_id", "class_name"),
            )
        ],
    },
    "character_create_from": {
        "direct": [
            _variant(
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
            _variant(
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
            _variant(
                ("campaign_id", "name", "player_name", "template_id"),
                ("template_id", "campaign_id"),
            )
        ],
        "statblock": [
            _variant(
                (
                    "campaign_id",
                    "character_type",
                    "chunk_ids",
                    "name",
                    "notes",
                    "replace_character_id",
                    "source_id",
                    "source_statblock_name",
                    "summary",
                    "variant",
                    "expected_revision",
                ),
                ("campaign_id", "source_id"),
            )
        ],
        "reviewed_rule_statblock": [
            _variant(
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
            _variant(
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
            _variant(
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
        "content_actor": [
            _variant(
                (
                    "artifact",
                    "artifact_id",
                    "campaign_id",
                    "name",
                    "player_name",
                    "source_path",
                ),
            )
        ],
    },
    "inventory_change": {
        "add": [_variant(("item",), ("item",))],
        "update": [_variant(("item_id", "patch"), ("item_id", "patch"))],
        "remove": [_variant(("item_id", "quantity"), ("item_id",))],
        "equip": [_variant(("item_id", "slot"), ("item_id", "slot"))],
        "recharge": [_variant(("item_id", "trigger"), ("item_id", "trigger"))],
        "consume_ammunition": [_variant(("quantity", "weapon_id"), ("weapon_id",))],
    },
    "inventory_transfer": {
        "character_to_character": [
            _variant(
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
            _variant(
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
            _variant(
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
        "adjust": [_variant(())],
        "transfer_to_character": [
            _variant(
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
            _variant(
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
        "effect_add": [_variant(("effect",), ("effect",))],
        "effect_remove": [_variant(("effect_id",), ("effect_id",))],
        "resource_set": [_variant(("resource", "value"), ("resource", "value"))],
        "exhaustion_set": [_variant(("value",), ("value",))],
        "damage": [
            _variant(
                ("critical", "knock_out", "melee", "parts"),
                ("parts",),
            )
        ],
        "heal": [
            _variant(
                (
                    "amount",
                    "source_actor_id",
                    "spell_id",
                    "spell_level",
                ),
                ("amount",),
            )
        ],
        "death_save": [_variant(())],
        "stabilize": [
            _variant(
                ("reason", "source_actor_id"),
                ("source_actor_id", "reason"),
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
            _variant(
                ("reason", "source_ref", "state"),
                ("state", "source_ref", "reason"),
            )
        ],
        "stand": [_variant(())],
        "knock_prone": [_variant(())],
    },
    "character_action": {
        "cast_spell": [
            _variant(
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
        "use_activity": [_variant(("activity_id", "declaration"), ("activity_id",))],
        "attack_source_object": [
            _variant(
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
        "set": [_variant(("prepared", "spell_id"), ("spell_id", "prepared"))],
        "replace_all": [_variant(("event", "spell_ids"), ("spell_ids",))],
    },
    "playthrough_manifest": {
        "get": [_variant(())],
        "initialize": [_variant(("manifest",), ("manifest",))],
        "replace": [_variant(("manifest",), ("manifest",))],
        "extend_modules": [_variant(("manifest",), ("manifest",))],
        "sync": [_variant(())],
        "verify_ending": [_variant(("condition_id",), ("condition_id",))],
    },
    "campaign_event": {
        "add": [
            _variant(
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
        "list": [_variant(("actor_id", "branch_id", "limit"))],
    },
    "memory_query": {
        "list": [_variant(("branch_id", "include_inactive", "kind"))],
        "search": [
            _variant(
                ("branch_id", "include_inactive", "limit", "query"),
                ("query",),
            )
        ],
        "diagnostics": [_variant(("branch_id",))],
    },
    "memory_change": {
        "add": [
            _variant(
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
            _variant(
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
            _variant(
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
            _variant(
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
                    "npc_turn",
                    "snapshot",
                ),
                ("event",),
            )
        ],
    },
    "actor_knowledge_query": {
        "list": [_variant(("branch_id",))],
        "search": [_variant(("branch_id", "limit", "query"), ("query",))],
    },
    "actor_knowledge_change": {
        "add": [
            _variant(
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
            _variant(
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
        "list": [_variant(())],
        "compare": [
            _variant(
                ("left_branch_id", "right_branch_id"),
                ("left_branch_id", "right_branch_id"),
            )
        ],
    },
    "branch_change": {
        "create": [_variant(("checkout", "from_snapshot_id", "name"), ("name",))],
        "checkout": [_variant(("branch_id",), ("branch_id",))],
        "create_core_upgrade": [
            _variant(
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
        "list": [_variant(())],
        "verify": [_variant(("slot",), ("slot",))],
        "lineage": [_variant(("slot",))],
        "recap": [_variant(("slot",), ("slot",))],
        "core": [_variant(("slot",), ("slot",))],
    },
    "state_revision": {
        "history": [_variant(("limit",))],
        "receipt": [
            _variant(
                ("branch_id", "idempotency_key"),
                ("idempotency_key",),
            )
        ],
        "undo": [_variant(("expected_history_sequence",))],
        "redo": [_variant(("expected_history_sequence",))],
    },
    "combat_common_action": {
        **{
            action: [_variant(())]
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
        "improvise": [_variant(("agent_ruling_commitment", "procedure_id"))],
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
    },
    "combat_query": {
        "status": [_variant(())],
        "available_actions": [_variant(())],
        "reactions": [_variant(())],
        "transaction_history": [_variant(("limit",))],
        "transaction_receipt": [
            _variant(
                ("branch_id", "idempotency_key"),
                ("idempotency_key",),
            )
        ],
    },
    "combat_movement": {
        "move": [
            _variant(
                (
                    "crawl",
                    "destination",
                    "distance",
                    "movement_mode",
                    "path",
                    "spatial_facts",
                ),
                ("distance",),
            )
        ],
        "stand": [_variant(())],
    },
    "combat_hp_change": {
        "damage": [
            _variant(
                ("critical", "knock_out", "melee", "parts"),
                ("parts",),
            )
        ],
        "heal": [
            _variant(
                (
                    "amount",
                    "source_actor_id",
                    "spell_id",
                    "spell_level",
                ),
                ("amount",),
            )
        ],
        "stabilize": [_variant(("source_excerpt",), ("source_excerpt",))],
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
                    "spatial_facts",
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
            _variant(
                ("actor_id", "cast_level", "declaration", "spell_id", "trigger"),
                ("actor_id", "spell_id", "trigger"),
            )
        ],
        "trigger_spell": [_variant(("event", "readied_id"), ("readied_id", "event"))],
        "resolve_spell": [
            _variant(
                ("actor_id", "choice_id", "declaration", "release"),
                ("actor_id", "choice_id", "release"),
            )
        ],
        "trigger_action": [_variant(("event", "readied_id"), ("readied_id", "event"))],
        "resolve_action": [
            _variant(
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
    "campaign_change": {
        "update": [_variant(("description", "name", "settings", "state", "status"))],
        "clock_set": [_variant(("day", "hour", "label", "minute"), ("day",))],
        "clock_advance": [
            _variant(
                (
                    "count",
                    "expected_elapsed_ticks",
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
        "stable_recovery": [_variant(("members", "resting_members"), ("members",))],
        "effect_add": [_variant(("effect",), ("effect",))],
        "effect_remove": [_variant(("effect_id", "reason"), ("effect_id",))],
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
        "open": [_variant(("candidates", "event", "kind"), ("event",))],
        "resolve": [_variant(("choice_id", "selection"), ("choice_id", "selection"))],
        "resolve_defense": [_variant(("choice_id", "selection"), ("choice_id", "selection"))],
        "on_hit_ruling": [_variant(("choice_id", "selection"), ("choice_id", "selection"))],
        "execute_plan": [_variant(("commitment",), ("commitment",))],
    },
}


def validate_action_payload(
    *,
    tool_id: str,
    selector: str | None,
    payload: Any,
) -> None:
    """Enforce the published exact payload variants before facade dispatch."""

    if selector is None:
        return
    variants = ACTION_PAYLOAD_CONTRACTS.get(tool_id, {}).get(selector)
    if variants is None:
        return
    if payload is None:
        data: dict[str, Any] = {}
    elif isinstance(payload, dict):
        data = payload
    else:
        raise ValueError("payload must be an object")
    fields = set(data)
    if any(
        set(variant["required_fields"]) <= fields and fields <= set(variant["allowed_fields"])
        for variant in variants
    ):
        return
    allowed_fields = {field for variant in variants for field in variant["allowed_fields"]}
    unknown = sorted(fields - allowed_fields)
    if unknown:
        raise ValueError(f"unsupported {tool_id}({selector}) payload fields: " + ", ".join(unknown))
    descriptions = [
        {
            "required": variant["required_fields"],
            "allowed": variant["allowed_fields"],
            **({"when": variant["when"]} if "when" in variant else {}),
        }
        for variant in variants
    ]
    raise ValueError(
        f"payload for {tool_id}({selector}) does not match an exact variant: {descriptions}"
    )


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
            if isinstance(properties.get(field), dict) and properties[field].get("enum")
        ),
        None,
    )
    selector_values = list(properties[selector_field]["enum"]) if selector_field is not None else []
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
                "contract_kind": "exact_field_contract",
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
