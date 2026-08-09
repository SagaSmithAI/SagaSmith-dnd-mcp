"""Run the public staged rule-import workflow against a real document corpus."""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import hashlib
import json
import re
import secrets
import shutil
import sys
import tracemalloc
from collections import Counter
from pathlib import Path
from time import perf_counter
from typing import Any

from sagasmith_core.content_pack import loads_content_archive
from sagasmith_core.text import ascii_slug
from sagasmith_dnd.character_schema import default_character_sheet
from sagasmith_dnd.core_content import (
    PACK_ID as SRD2014_PACK_ID,
)
from sagasmith_dnd.core_content import (
    PACK_VERSION as SRD2014_PACK_VERSION,
)
from sagasmith_dnd.core_content_2024 import (
    PACK_ID as SRD2024_PACK_ID,
)
from sagasmith_dnd.core_content_2024 import (
    PACK_VERSION as SRD2024_PACK_VERSION,
)
from sagasmith_dnd.editions import SUPPORTED_DND_EDITIONS
from sagasmith_dnd.statblocks import (
    OCR_STATBLOCK_RECOVERY_VERSION,
    StatblockImportError,
    dependent_actor_template_solution_errors,
    parameterized_statblock_requirements,
    parse_2014_statblock,
    parse_2014_statblock_template_preview,
)

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import (
    _bounded_ocr_heading_equivalent,
    _statblock_index_recovery_hints,
    create_server,
)

DEFAULT_CONTENT_CATALOG_MANIFEST = (
    Path(__file__).resolve().parents[1] / "fixtures" / "books_catalog_review_all_v1.json"
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Allowlisted root containing rule documents")
    parser.add_argument(
        "--home",
        type=Path,
        required=True,
        help="Disposable or persistent MCP home used for the regression index/cache",
    )
    parser.add_argument("--edition", choices=SUPPORTED_DND_EDITIONS, default="2014")
    parser.add_argument("--locale", default="en")
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="Run only relative paths matching this case-insensitive glob; repeatable",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="Skip relative paths matching this case-insensitive glob; repeatable",
    )
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument(
        "--baseline-only",
        action="store_true",
        help=(
            "Stop after deterministic candidate extraction. This records parser metrics without "
            "running source review, Agent recovery, compilation, or package round trips."
        ),
    )
    parser.add_argument("--ocr-scale", type=float, default=2.0)
    parser.add_argument(
        "--ocr-model",
        choices=("small", "medium"),
        default="medium",
        help=(
            "Preferred PP-OCRv6 profile. Statblock recovery automatically tries "
            "the other profile only when the preferred result cannot be verified."
        ),
    )
    parser.add_argument(
        "--primary-reviewer",
        default="deterministic:typed-card-author-v1",
        help="Identity recorded for the primary per-candidate catalog review.",
    )
    parser.add_argument(
        "--primary-review-method",
        choices=("agent", "deterministic", "human"),
        default="deterministic",
    )
    parser.add_argument(
        "--critic-reviewer",
        default="deterministic:source-contract-critic-v1",
        help="Different identity recorded for the independent catalog review.",
    )
    parser.add_argument(
        "--critic-review-method",
        choices=("agent", "deterministic", "human"),
        default="deterministic",
    )
    parser.add_argument(
        "--document-cache",
        type=Path,
        help=(
            "Optional shared, content-addressed normalized-document cache. "
            "Campaign databases and exported packages remain isolated per --home."
        ),
    )
    parser.add_argument(
        "--catalog-manifest",
        type=Path,
        help=(
            "Optional source-reviewed JSON manifest for catalog additions, "
            "corrections, and rejections. Stable source selectors are resolved "
            "to indexed chunk ids before the public augment_catalog call."
        ),
    )
    parser.add_argument(
        "--dependency-addon",
        type=Path,
        action="append",
        default=[],
        help=(
            "Previously reviewed private addon package available as a semantic "
            "dependency; repeatable. A document review must select it by exact "
            "addon id and version before any component is embedded."
        ),
    )
    parser.add_argument("--fail-on-warning", action="store_true")
    parser.add_argument(
        "--content-roundtrip",
        action="store_true",
        help=(
            "Compile each complete private source catalog with build-time semantic "
            "resolution, export it, import it into an isolated MCP home, and require "
            "an identical re-export"
        ),
    )
    parser.add_argument(
        "--content-target-home",
        type=Path,
        help=("Isolated receiver home for --content-roundtrip; defaults to a sibling of --home"),
    )
    parser.add_argument(
        "--run-id",
        default="default",
        help="Logical run id; use a new value to exercise caches without idempotent replay",
    )
    parser.add_argument(
        "--addon-output-dir",
        type=Path,
        help=(
            "Write each complete private addon package to this directory after "
            "its isolated public-MCP round trip succeeds"
        ),
    )
    parser.add_argument(
        "--measure-memory",
        action="store_true",
        help=(
            "Record peak traced Python allocations for the selected regression run. "
            "This is opt-in because tracing changes parser performance."
        ),
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _key(relative_path: str, *, run_id: str = "default") -> str:
    slug = ascii_slug(Path(relative_path).stem)
    digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:10]
    return f"user.rulebook.{slug[:120] or 'rulebook'}.{digest}"


def _catalog_review_token(review_spec: dict[str, Any]) -> str:
    """Bind mutable review work to the exact replayable Agent decision set.

    The PDF conversion cache remains content-addressed. Every durable import
    transaction is additionally scoped to this token so an improved
    source-bound Agent review cannot replay a stale compiled job; page parsing
    can still reuse the unchanged normalized-document cache.
    """

    canonical = json.dumps(
        review_spec,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


async def _call(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    try:
        _, result = await server.call_tool(name, arguments)
    except Exception as error:
        operation = str(arguments.get("action") or arguments.get("view") or "call")
        raise RuntimeError(f"{name}({operation}) failed: {error}") from error
    return result


def _rendered_page_metadata(response: Any) -> dict[str, Any]:
    """Read structured page evidence without discarding native image content."""

    structured = getattr(response, "structuredContent", None)
    if isinstance(structured, dict):
        return dict(structured)
    if isinstance(response, tuple) and len(response) == 2:
        response = response[1]
    if isinstance(response, dict) and "transcription" in response:
        return dict(response)
    content = getattr(response, "content", None)
    if isinstance(content, list) and content:
        text = getattr(content[0], "text", None)
        if isinstance(text, str):
            metadata = json.loads(text)
            if isinstance(metadata, dict):
                return metadata
    raise RuntimeError("rulebook_draft(render_page) returned no structured page metadata")


def _transcription_review_specs(review_spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate source-bound Agent/human OCR corrections stored in a manifest."""

    raw_reviews = review_spec.get("text_reviews") or []
    if not isinstance(raw_reviews, list):
        raise ValueError("catalog manifest text_reviews must be a list")
    reviews: list[dict[str, Any]] = []
    for index, raw_review in enumerate(raw_reviews):
        if not isinstance(raw_review, dict):
            raise ValueError(f"text review {index} must be an object")
        unknown = set(raw_review) - {
            "page_number",
            "base_text_sha256",
            "replacements",
            "rationale",
            "evidence_basis",
            "rendered_image_checksum",
            "review_method",
        }
        if unknown:
            raise ValueError(f"text review {index} has unsupported fields: {sorted(unknown)}")
        page_number = raw_review.get("page_number")
        if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
            raise ValueError(f"text review {index} page_number must be positive")
        base_text_sha256 = str(raw_review.get("base_text_sha256") or "")
        if len(base_text_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in base_text_sha256
        ):
            raise ValueError(f"text review {index} base_text_sha256 must be lowercase SHA-256")
        replacements = raw_review.get("replacements")
        if not isinstance(replacements, list) or not replacements:
            raise ValueError(f"text review {index} replacements must be nonempty")
        for replacement_index, replacement in enumerate(replacements):
            if not isinstance(replacement, dict) or set(replacement) != {"old", "new"}:
                raise ValueError(
                    f"text review {index} replacement {replacement_index} "
                    "must contain only old and new"
                )
            if (
                not isinstance(replacement["old"], str)
                or not isinstance(replacement["new"], str)
                or not replacement["new"]
                or replacement["old"] == replacement["new"]
                or (
                    not replacement["old"]
                    and str(raw_review.get("evidence_basis") or "") != "rendered_page"
                )
            ):
                raise ValueError(f"text review {index} replacement {replacement_index} is invalid")
        rationale = str(raw_review.get("rationale") or "").strip()
        if len(rationale) < 8:
            raise ValueError(f"text review {index} rationale is too short")
        evidence_basis = str(raw_review.get("evidence_basis") or "")
        if evidence_basis not in {"cross_text", "agent_context", "rendered_page"}:
            raise ValueError(f"text review {index} evidence_basis is invalid")
        review_method = str(raw_review.get("review_method") or "agent")
        if review_method not in {"agent", "human"}:
            raise ValueError(f"text review {index} review_method is invalid")
        rendered_image_checksum = raw_review.get("rendered_image_checksum")
        if evidence_basis == "rendered_page":
            rendered_image_checksum = str(rendered_image_checksum or "")
            if len(rendered_image_checksum) != 64 or any(
                character not in "0123456789abcdef" for character in rendered_image_checksum
            ):
                raise ValueError(
                    f"text review {index} rendered_image_checksum must be lowercase SHA-256"
                )
        elif rendered_image_checksum is not None:
            raise ValueError(
                f"text review {index} rendered_image_checksum requires rendered_page evidence"
            )
        reviews.append(
            {
                "page_number": page_number,
                "base_text_sha256": base_text_sha256,
                "replacements": json.loads(json.dumps(replacements)),
                "rationale": rationale,
                "evidence_basis": evidence_basis,
                "review_method": review_method,
                **(
                    {"rendered_image_checksum": rendered_image_checksum}
                    if evidence_basis == "rendered_page"
                    else {}
                ),
            }
        )
    return reviews


def _statblock_slot_review_specs(review_spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate replayable Agent names for mechanically proven page slots."""

    raw_reviews = review_spec.get("statblock_slot_reviews") or []
    if not isinstance(raw_reviews, list):
        raise ValueError("statblock_slot_reviews must be an array")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    allowed = {
        "page_number",
        "statblock_slot",
        "name",
        "expected_identity",
        "note",
        "ocr_corrections",
        "correction_evidence_basis",
        "rendered_image_checksum",
    }
    for index, raw in enumerate(raw_reviews):
        if not isinstance(raw, dict) or set(raw) - allowed:
            raise ValueError(f"statblock_slot_reviews[{index}] contains unsupported fields")
        page_number = raw.get("page_number")
        statblock_slot = raw.get("statblock_slot")
        name = " ".join(str(raw.get("name") or "").split())
        if (
            isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or page_number < 1
            or isinstance(statblock_slot, bool)
            or not isinstance(statblock_slot, int)
            or statblock_slot < 1
            or not 2 <= len(name) <= 200
        ):
            raise ValueError(
                f"statblock_slot_reviews[{index}] requires positive page/slot and a name"
            )
        identity = " ".join(str(raw.get("expected_identity") or "").split())
        note = " ".join(str(raw.get("note") or "").split())
        ocr_corrections = raw.get("ocr_corrections")
        correction_evidence_basis = str(
            raw.get("correction_evidence_basis") or "staged_text"
        ).strip()
        rendered_image_checksum = str(raw.get("rendered_image_checksum") or "").strip().lower()
        if raw.get("expected_identity") is not None and not identity:
            raise ValueError(f"statblock_slot_reviews[{index}].expected_identity must be nonempty")
        if raw.get("note") is not None and not 8 <= len(note) <= 2000:
            raise ValueError(
                f"statblock_slot_reviews[{index}].note must contain 8 to 2000 characters"
            )
        if correction_evidence_basis not in {"staged_text", "rendered_page"}:
            raise ValueError(
                f"statblock_slot_reviews[{index}].correction_evidence_basis is invalid"
            )
        if ocr_corrections is None and (
            correction_evidence_basis != "staged_text" or rendered_image_checksum
        ):
            raise ValueError(
                f"statblock_slot_reviews[{index}] correction evidence requires ocr_corrections"
            )
        if correction_evidence_basis == "rendered_page":
            if re.fullmatch(r"[0-9a-f]{64}", rendered_image_checksum) is None:
                raise ValueError(
                    f"statblock_slot_reviews[{index}] rendered_page evidence requires "
                    "rendered_image_checksum"
                )
        elif rendered_image_checksum:
            raise ValueError(
                f"statblock_slot_reviews[{index}] rendered_image_checksum requires "
                "rendered_page evidence"
            )
        if ocr_corrections is not None:
            if (
                not isinstance(ocr_corrections, dict)
                or not ocr_corrections
                or set(ocr_corrections) - {"abilities", "text_replacements"}
            ):
                raise ValueError(
                    f"statblock_slot_reviews[{index}].ocr_corrections supports "
                    "only abilities and text_replacements"
                )
            abilities = ocr_corrections.get("abilities")
            if abilities is not None and (not isinstance(abilities, dict) or not abilities):
                raise ValueError(
                    f"statblock_slot_reviews[{index}].ocr_corrections.abilities must be nonempty"
                )
            normalized_abilities: dict[str, str] = {}
            for raw_ability, raw_value in dict(abilities or {}).items():
                ability = str(raw_ability or "").strip().lower()
                value = " ".join(str(raw_value or "").split())
                if ability not in {"str", "dex", "con", "int", "wis", "cha"}:
                    raise ValueError(f"statblock_slot_reviews[{index}] has an unknown ability")
                if not value:
                    raise ValueError(f"statblock_slot_reviews[{index}] has an empty ability value")
                normalized_abilities[ability] = value
            text_replacements = ocr_corrections.get("text_replacements")
            if text_replacements is not None and (
                not isinstance(text_replacements, list)
                or not text_replacements
                or len(text_replacements) > 20
            ):
                raise ValueError(
                    f"statblock_slot_reviews[{index}].ocr_corrections."
                    "text_replacements must contain 1 to 20 entries"
                )
            normalized_replacements: list[dict[str, str]] = []
            seen_old: set[str] = set()
            for replacement_index, raw_replacement in enumerate(text_replacements or []):
                if not isinstance(raw_replacement, dict) or set(raw_replacement) != {
                    "old",
                    "new",
                }:
                    raise ValueError(
                        f"statblock_slot_reviews[{index}].ocr_corrections."
                        f"text_replacements[{replacement_index}] requires old and new"
                    )
                old = " ".join(str(raw_replacement.get("old") or "").split())
                new = " ".join(str(raw_replacement.get("new") or "").split())
                if not old or not new or old == new or len(old) > 500 or len(new) > 2000:
                    raise ValueError(
                        f"statblock_slot_reviews[{index}] has an invalid OCR text replacement"
                    )
                old_key = old.casefold()
                if old_key in seen_old:
                    raise ValueError(
                        f"statblock_slot_reviews[{index}] has duplicate OCR text "
                        "replacement anchors"
                    )
                seen_old.add(old_key)
                normalized_replacements.append({"old": old, "new": new})
            ocr_corrections = {
                **({"abilities": normalized_abilities} if normalized_abilities else {}),
                **(
                    {"text_replacements": normalized_replacements}
                    if normalized_replacements
                    else {}
                ),
            }
            if not ocr_corrections:
                raise ValueError(
                    f"statblock_slot_reviews[{index}].ocr_corrections must be nonempty"
                )
        key = (page_number, statblock_slot)
        if key in seen:
            raise ValueError("statblock_slot_reviews must identify unique page slots")
        seen.add(key)
        normalized.append(
            {
                "page_number": page_number,
                "statblock_slot": statblock_slot,
                "name": name,
                **({"expected_identity": identity} if identity else {}),
                **({"note": note} if note else {}),
                **({"ocr_corrections": ocr_corrections} if ocr_corrections is not None else {}),
                **(
                    {
                        "correction_evidence_basis": correction_evidence_basis,
                        **(
                            {"rendered_image_checksum": rendered_image_checksum}
                            if correction_evidence_basis == "rendered_page"
                            else {}
                        ),
                    }
                    if ocr_corrections is not None
                    else {}
                ),
            }
        )
    return normalized


def _statblock_slot_review_token(spec: dict[str, Any]) -> str:
    """Bind one idempotent replay to only that Agent correction."""

    canonical = json.dumps(
        spec,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


async def _apply_statblock_slot_reviews(
    server: Any,
    *,
    campaign_id: str,
    job_id: str,
    review_spec: dict[str, Any],
    id_key: str,
) -> dict[str, Any] | None:
    """Replay Agent semantic names while the engine owns numeric extraction."""

    reviews = _statblock_slot_review_specs(review_spec)
    if not reviews:
        return None
    applied: list[dict[str, Any]] = []
    revision = 0
    for index, spec in enumerate(reviews, start=1):
        correction_token = _statblock_slot_review_token(spec)
        print(
            "  [Agent OCR "
            f"{index}/{len(reviews)}] page {spec['page_number']} "
            f"slot {spec['statblock_slot']}: {spec['name']}",
            flush=True,
        )
        if spec.get("correction_evidence_basis") == "rendered_page":
            rendered = await server.call_tool(
                "rulebook_draft",
                {
                    "campaign_id": campaign_id,
                    "action": "evidence",
                    "payload": {
                        "kind": "page",
                        "job_id": job_id,
                        "page_number": spec["page_number"],
                        "scale": 1.5,
                        "include_ocr_text": False,
                    },
                },
            )
            rendered_metadata = _rendered_page_metadata(rendered)
            if str(rendered_metadata.get("image_checksum") or "") != str(
                spec["rendered_image_checksum"]
            ):
                raise RuntimeError(
                    "Agent statblock correction image checksum drifted: "
                    f"page={spec['page_number']}, slot={spec['statblock_slot']}"
                )
        try:
            response = await _call(
                server,
                "rulebook_draft",
                {
                    "campaign_id": campaign_id,
                    "action": "edit",
                    "payload": {
                        "operation": "statblock_recovery",
                        "job_id": job_id,
                        "name": spec["name"],
                        "page_number": spec["page_number"],
                        "statblock_slot": spec["statblock_slot"],
                        **(
                            {"ocr_corrections": spec["ocr_corrections"]}
                            if "ocr_corrections" in spec
                            else {}
                        ),
                        **(
                            {
                                "correction_evidence_basis": spec["correction_evidence_basis"],
                                **(
                                    {"rendered_image_checksum": spec["rendered_image_checksum"]}
                                    if "rendered_image_checksum" in spec
                                    else {}
                                ),
                            }
                            if "correction_evidence_basis" in spec
                            else {}
                        ),
                    },
                    "idempotency_key": (
                        "regression-agent-statblock-slot-wrapper-v2-"
                        f"r{OCR_STATBLOCK_RECOVERY_VERSION}-{id_key}-"
                        f"{correction_token}"
                    ),
                },
            )
        except RuntimeError as exc:
            raise RuntimeError(
                "Agent statblock slot review failed: "
                f"index={index}, page={spec['page_number']}, "
                f"slot={spec['statblock_slot']}, name={spec['name']!r}: {exc}"
            ) from exc
        result = dict(response["result"])
        evidence = dict(dict(result["recovery"])["evidence"])
        slot_summary = dict(evidence.get("statblock_slot_summary") or {})
        expected_identity = spec.get("expected_identity")
        if expected_identity and _fold_text(slot_summary.get("identity")) != _fold_text(
            expected_identity
        ):
            raise RuntimeError(
                "Agent statblock slot identity changed: "
                f"page={spec['page_number']}, slot={spec['statblock_slot']}"
            )
        if evidence.get("heading_match_mode") != "agent_named_structural_slot":
            raise RuntimeError("Agent statblock review did not use its exact structural slot")
        revision = int(dict(result["job"])["revision"])
        review = dict(result["review"])
        applied.append(
            {
                **spec,
                "review_id": review["id"],
                "derived_from_review_id": review.get("derived_from_review_id"),
                "source_checksum": review["source_checksum"],
                "image_checksum": review["image_checksum"],
                "normalized_content_sha256": review["normalized_content_sha256"],
                "slot_summary": slot_summary,
            }
        )
    return {"count": len(applied), "job_revision": revision, "reviews": applied}


async def _apply_transcription_reviews(
    server: Any,
    *,
    campaign_id: str,
    job_id: str,
    initial_revision: int,
    review_spec: dict[str, Any],
    id_key: str,
) -> dict[str, Any] | None:
    """Replay reviewed OCR repairs through the public, revisioned MCP facade."""

    reviews = _transcription_review_specs(review_spec)
    if not reviews:
        return None
    revision = initial_revision
    applied: list[dict[str, Any]] = []
    inspection: dict[str, Any] | None = None
    for index, review in enumerate(reviews, start=1):
        rendered = await server.call_tool(
            "rulebook_draft",
            {
                "campaign_id": campaign_id,
                "action": "evidence",
                "payload": {
                    "kind": "page",
                    "job_id": job_id,
                    "page_number": review["page_number"],
                    "scale": 1.5,
                    "include_ocr_text": False,
                },
            },
        )
        metadata = _rendered_page_metadata(rendered)
        # A resumed run sees the already revised page here.  Submit the exact
        # original request again so the public idempotency record can replay;
        # if no replay exists, the server still rejects the stale base hash
        # before writing anything.
        if (
            review["evidence_basis"] == "rendered_page"
            and str(metadata.get("image_checksum") or "") != review["rendered_image_checksum"]
        ):
            raise RuntimeError(
                f"text review {index} image checksum drifted on page {review['page_number']}"
            )
        response = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign_id,
                "action": "edit",
                "payload": {"operation": "source_text", "job_id": job_id, **review},
                "expected_revision": revision,
                "idempotency_key": f"regression-text-review-{id_key}-{index}",
            },
        )
        result = dict(response["result"])
        revision = int(result["job"]["revision"])
        inspection = dict(result["inspection"])
        applied.append(dict(result["review"]))
    return {
        "count": len(applied),
        "reviews": applied,
        "inspection": inspection,
        "job_revision": revision,
    }


async def _augment_catalog_batches(
    server: Any,
    *,
    campaign_id: str,
    job_id: str,
    additions: list[dict[str, Any]],
    rationale: str,
    expected_revision: int,
    idempotency_key: str,
) -> dict[str, Any]:
    """Apply a complete source review through bounded public MCP transactions."""

    if not additions:
        raise ValueError("catalog augmentation batches require at least one addition")
    added_candidate_ids: list[str] = []
    candidates: list[dict[str, Any]] = []
    revision = expected_revision
    batch_count = 0
    for offset in range(0, len(additions), 100):
        batch = additions[offset : offset + 100]
        response = await _call(
            server,
            "rulebook_draft",
            {
                "campaign_id": campaign_id,
                "action": "edit",
                "payload": {
                    "operation": "catalog",
                    "job_id": job_id,
                    "rationale": rationale,
                    "additions": batch,
                },
                "expected_revision": revision,
                "idempotency_key": f"{idempotency_key}-batch-{batch_count + 1}",
            },
        )
        result = dict(response["result"])
        revision = int(result["job"]["revision"])
        candidates = list(result["candidates"])
        added_candidate_ids.extend(result["added_candidate_ids"])
        batch_count += 1
    return {
        "job_revision": revision,
        "candidates": candidates,
        "added_candidate_ids": added_candidate_ids,
        "batch_count": batch_count,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.expanduser().resolve()
    home = args.home.expanduser().resolve()
    document_cache = args.document_cache.expanduser().resolve() if args.document_cache else None
    catalog_manifest_path = _effective_catalog_manifest_path(
        args.catalog_manifest,
        content_roundtrip=args.content_roundtrip,
    )
    catalog_manifest = _load_catalog_manifest(catalog_manifest_path)
    dependency_addons = _load_dependency_addons(args.dependency_addon)
    probe_attempt_id = secrets.token_hex(8)
    addon_output_dir = (
        args.addon_output_dir.expanduser().resolve() if args.addon_output_dir else None
    )
    if args.baseline_only and (args.content_roundtrip or addon_output_dir is not None):
        raise ValueError("--baseline-only cannot build or round-trip content packages")
    if addon_output_dir is not None:
        addon_output_dir.mkdir(parents=True, exist_ok=True)
    if str(args.primary_reviewer).strip() == str(args.critic_reviewer).strip():
        raise ValueError("primary and critic reviewer identities must differ")
    config = McpConfig.from_environment()
    config = McpConfig(
        home=home,
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=config.dnd_skills_dir,
        modulegen_skills_dir=config.modulegen_skills_dir,
        auto_seed_rules=True,
        rule_import_roots=tuple(
            sorted(
                {root, *(path.expanduser().resolve().parent for path in args.dependency_addon)},
                key=lambda path: str(path).casefold(),
            )
        ),
        module_import_roots=(),
        rule_ocr_enabled=not args.no_ocr,
        rule_ocr_scale=args.ocr_scale,
        rule_ocr_model=args.ocr_model,
        document_cache_dir=document_cache,
    )
    server = create_server(config)
    campaign = await _call(
        server,
        "campaign_create",
        {
            "name": (
                f"Rulebook regression: {root.name}"
                if args.run_id == "default"
                else f"Rulebook regression: {root.name} [{_run_token(args.run_id)}]"
            ),
            "edition": args.edition,
            "locale": args.locale,
            "idempotency_key": (
                "rulebook-regression-campaign"
                if args.run_id == "default"
                else f"rulebook-regression-campaign-{_run_token(args.run_id)}"
            ),
        },
    )
    discovery = await _call(
        server,
        "rulebook_draft",
        {"campaign_id": campaign["id"], "action": "discover"},
    )
    discovered_documents = discovery["result"]["documents"]
    documents = [
        document
        for document in discovered_documents
        if _matches_includes(str(document["relative_path"]), args.include)
        and not _matches_includes(str(document["relative_path"]), args.exclude, empty=False)
    ]
    if args.include and not documents:
        raise ValueError(
            "--include matched no discovered rule documents: "
            + ", ".join(str(pattern) for pattern in args.include)
        )
    target_server: Any | None = None
    target_campaign: dict[str, Any] | None = None
    target_home: Path | None = None
    if args.content_roundtrip:
        target_home = (
            args.content_target_home.expanduser().resolve()
            if args.content_target_home
            else home.with_name(f"{home.name}-content-target")
        )
        if target_home == home:
            raise ValueError("content target home must differ from the source MCP home")
        target_config = McpConfig(
            home=target_home,
            database_url=None,
            chroma_url=None,
            chroma_path_override=None,
            dnd_skills_dir=config.dnd_skills_dir,
            modulegen_skills_dir=config.modulegen_skills_dir,
            auto_seed_rules=True,
            rule_import_roots=tuple(
                sorted(
                    {
                        config.portable_packages_dir,
                        *(path.expanduser().resolve().parent for path in args.dependency_addon),
                    },
                    key=lambda path: str(path).casefold(),
                )
            ),
            module_import_roots=(),
            rule_ocr_enabled=not args.no_ocr,
            rule_ocr_scale=args.ocr_scale,
            rule_ocr_model=args.ocr_model,
            document_cache_dir=document_cache,
        )
        target_server = create_server(target_config)
        target_campaign = await _call(
            target_server,
            "campaign_create",
            {
                "name": f"Portable rulebook receiver [{_run_token(args.run_id)}]",
                "edition": args.edition,
                "locale": args.locale,
                "idempotency_key": (f"rulebook-portable-target-campaign-{_run_token(args.run_id)}"),
            },
        )
        await _enable_core_content_pack(
            target_server,
            campaign_id=str(target_campaign["id"]),
            edition=args.edition,
            run_id=args.run_id,
        )
    report: dict[str, Any] = {
        "root": str(root),
        "home": str(home),
        "document_cache": str(document_cache) if document_cache else None,
        "edition": args.edition,
        "ocr_model": args.ocr_model,
        "catalog_review": {
            "manifest": (
                {
                    "path": str(catalog_manifest_path),
                    "sha256": hashlib.sha256(
                        json.dumps(
                            catalog_manifest,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    "strict": catalog_manifest.get("strict") is True,
                }
                if catalog_manifest_path is not None
                else None
            ),
            "primary": {
                "reviewer": str(args.primary_reviewer),
                "method": str(args.primary_review_method),
            },
            "critic": {
                "reviewer": str(args.critic_reviewer),
                "method": str(args.critic_review_method),
            },
        },
        "run_id": args.run_id,
        "baseline_only": bool(args.baseline_only),
        "probe_attempt_id": probe_attempt_id,
        "document_count": len(documents),
        "discovered_document_count": len(discovered_documents),
        "include": list(args.include),
        "exclude": list(args.exclude),
        "documents": [],
        "failed_documents": [],
        "errors": [],
        "content_roundtrip": args.content_roundtrip,
        "content_target_home": str(target_home) if target_home else None,
        "addon_output_dir": str(addon_output_dir) if addon_output_dir else None,
    }
    if args.measure_memory:
        tracemalloc.start()
    started = perf_counter()
    for index, document in enumerate(documents, start=1):
        relative_path = str(document["relative_path"])
        source_key = _key(relative_path, run_id=args.run_id)
        id_key = hashlib.sha256(f"{relative_path}\0{args.run_id}".encode("utf-8")).hexdigest()[:16]
        document_review = _catalog_document_review(
            catalog_manifest,
            relative_path,
        )
        review_token = _catalog_review_token(document_review)
        # A changed source review is a new workflow attempt even when the PDF,
        # logical run id, and content-addressed document cache remain the same.
        # This prevents a prior compiled job from replaying stale pre-review
        # stage/inspection responses while later calls use the new decisions.
        workflow_id_key = f"{id_key}-{review_token}"
        item_started = perf_counter()
        candidate_extraction_seconds = 0.0
        failure_snapshot: dict[str, Any] | None = None
        print(f"[{index}/{len(documents)}] {relative_path}", file=sys.stderr, flush=True)
        try:
            publication_id, authority = _publication_metadata(relative_path)
            staged = await _call(
                server,
                "rulebook_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "start",
                    "payload": {
                        "source_path": document["path"],
                        "source_key": source_key,
                        "title": Path(relative_path).stem,
                        "edition": args.edition,
                        "locale": args.locale,
                        "publication_id": publication_id,
                        "authority": authority,
                    },
                    "idempotency_key": f"regression-stage-{workflow_id_key}",
                },
            )
            job_id = staged["result"]["job"]["id"]
            inspected = await _call(
                server,
                "rulebook_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "get",
                    "payload": {"job_id": job_id},
                    "idempotency_key": f"regression-inspect-{workflow_id_key}",
                },
            )
            inspection = inspected["result"]["inspection"]
            review_id_key = workflow_id_key
            transcription_review = await _apply_transcription_reviews(
                server,
                campaign_id=str(campaign["id"]),
                job_id=job_id,
                initial_revision=int(inspected["result"]["job"]["revision"]),
                review_spec=document_review,
                id_key=workflow_id_key,
            )
            if transcription_review is not None:
                inspection = dict(transcription_review["inspection"])
            warnings = list(inspection.get("warnings") or [])
            if warnings and args.fail_on_warning:
                raise RuntimeError("; ".join(warnings))
            ingested = await _call(
                server,
                "rulebook_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "get",
                    "payload": {
                        "job_id": job_id,
                        "acknowledge_warnings": bool(warnings),
                    },
                    "idempotency_key": f"regression-ingest-{workflow_id_key}",
                },
            )
            source_id = ingested["result"]["source"]["id"]
            extraction_started = perf_counter()
            extracted = await _call(
                server,
                "rulebook_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "get",
                    "payload": {"job_id": job_id},
                    "idempotency_key": f"regression-extract-{review_id_key}",
                },
            )
            candidate_extraction_seconds += perf_counter() - extraction_started
            candidates = extracted["result"]["candidates"]
            inventory = extracted["result"]["inventory"]
            failure_snapshot = _failed_document_parsing_snapshot(
                relative_path,
                candidates,
                document_review,
                candidate_extraction_seconds=candidate_extraction_seconds,
                phase="extract_candidates",
            )
            if args.baseline_only:
                report["documents"].append(
                    {
                        "relative_path": relative_path,
                        "artifact": staged["result"]["artifact"],
                        "source_id": source_id,
                        "checksum": staged["result"]["checksum"],
                        "pages": inspection["page_count"],
                        "sections": inspection["sections"],
                        "chunks": inspection["chunks"],
                        "warnings": warnings,
                        "metadata": inspection["metadata"],
                        "candidate_count": len(candidates),
                        "candidate_kinds": _kind_counts(candidates),
                        "parsing_baseline": failure_snapshot["parsing_baseline"],
                        "content_inventory": {
                            key: value for key, value in inventory.items() if key != "ledger"
                        },
                        "review_pipeline_skipped": True,
                        "seconds": round(perf_counter() - item_started, 3),
                    }
                )
                print(
                    f"[OK {index}/{len(documents)}] {relative_path} "
                    f"({perf_counter() - item_started:.1f}s; baseline only)",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            catalog_revision = int(extracted["result"]["job"]["revision"])
            source_chunks: list[dict[str, Any]] | None = None
            resolved_catalog_additions: list[dict[str, Any]] | None = None
            statblock_recovery: dict[str, Any] | None = None
            statblock_slot_review: dict[str, Any] | None = None
            if args.edition == "2014":
                source_chunks = await _source_chunks(server, source_id)
                if document_review.get("additions"):
                    resolved_catalog_additions = _resolve_catalog_additions(
                        document_review["additions"],
                        source_chunks,
                        relative_path=relative_path,
                    )
                refresh_statblocks = False
                recovery_candidates = _prefer_reviewed_statblock_additions(
                    candidates,
                    resolved_catalog_additions or [],
                    source_chunks,
                )
                if _statblock_recovery_needed(
                    recovery_candidates,
                    source_chunks,
                ) or _expected_actor_recovery_needed(
                    recovery_candidates,
                    document_review.get("expected_actor_names"),
                ):
                    recovery_response = await _call(
                        server,
                        "rulebook_draft",
                        {
                            "campaign_id": campaign["id"],
                            "action": "edit",
                            "payload": {"operation": "statblock_recovery", "job_id": job_id},
                            "idempotency_key": (f"regression-recover-catalog-{workflow_id_key}"),
                        },
                    )
                    statblock_recovery = recovery_response["result"]
                    catalog_revision = int(statblock_recovery["job"]["revision"])
                    refresh_statblocks = True
                else:
                    statblock_recovery = {
                        "schema_version": 1,
                        "status": "not_required",
                        "reason": (
                            "all extracted statblocks are parser-ready or "
                            "source-proven dependent actor templates"
                        ),
                    }
                statblock_slot_review = await _apply_statblock_slot_reviews(
                    server,
                    campaign_id=str(campaign["id"]),
                    job_id=job_id,
                    review_spec=document_review,
                    id_key=workflow_id_key,
                )
                if statblock_slot_review is not None:
                    catalog_revision = int(statblock_slot_review["job_revision"])
                    refresh_statblocks = True
                if refresh_statblocks:
                    # Both deterministic recovery and Agent-named slots persist
                    # checksum-bound reviews. Re-extraction is the sole public
                    # operation that projects their preferred versions back into
                    # catalog candidates.
                    extraction_started = perf_counter()
                    refreshed = await _call(
                        server,
                        "rulebook_draft",
                        {
                            "campaign_id": campaign["id"],
                            "action": "get",
                            "payload": {"job_id": job_id},
                            "idempotency_key": (f"regression-extract-recovered-{review_id_key}"),
                        },
                    )
                    candidate_extraction_seconds += perf_counter() - extraction_started
                    candidates = refreshed["result"]["candidates"]
                    inventory = refreshed["result"]["inventory"]
                    failure_snapshot = _failed_document_parsing_snapshot(
                        relative_path,
                        candidates,
                        document_review,
                        candidate_extraction_seconds=candidate_extraction_seconds,
                        phase="extract_candidates_after_review",
                    )
                    catalog_revision = int(refreshed["result"]["job"]["revision"])
            catalog_augmentation: dict[str, Any] | None = None
            if document_review.get("additions"):
                if resolved_catalog_additions is None:
                    if source_chunks is None:
                        source_chunks = await _source_chunks(server, source_id)
                    resolved_catalog_additions = _resolve_catalog_additions(
                        document_review["additions"],
                        source_chunks,
                        relative_path=relative_path,
                    )
                additions = resolved_catalog_additions
                if source_chunks is None:
                    source_chunks = await _source_chunks(server, source_id)
                additions = _bind_catalog_addition_replacements(additions, candidates)
                augmented = await _augment_catalog_batches(
                    server,
                    campaign_id=str(campaign["id"]),
                    job_id=job_id,
                    additions=additions,
                    rationale=str(
                        document_review.get("rationale")
                        or "Agent reviewed the complete indexed source catalog."
                    ),
                    expected_revision=catalog_revision,
                    idempotency_key=f"regression-augment-{review_id_key}",
                )
                candidates = augmented["candidates"]
                failure_snapshot = _failed_document_parsing_snapshot(
                    relative_path,
                    candidates,
                    document_review,
                    candidate_extraction_seconds=candidate_extraction_seconds,
                    phase="augment_catalog",
                )
                catalog_revision = int(augmented["job_revision"])
                catalog_augmentation = {
                    "added_candidate_ids": augmented["added_candidate_ids"],
                    "added": len(additions),
                    "batches": augmented["batch_count"],
                }
            hits = await _call(
                server,
                "rule_search",
                {
                    "query": Path(relative_path).stem,
                    "source_ids": [source_id],
                    "top_k": 1,
                },
            )
            portable: dict[str, Any] | None = None
            if target_server is not None and target_campaign is not None:
                portable = await _content_roundtrip(
                    source_server=server,
                    source_archive_dir=config.portable_packages_dir,
                    source_campaign_id=str(campaign["id"]),
                    target_server=target_server,
                    target_campaign_id=str(target_campaign["id"]),
                    source_id=source_id,
                    source_key=source_key,
                    job_id=job_id,
                    candidates=candidates,
                    relative_path=relative_path,
                    edition=args.edition,
                    run_id=args.run_id,
                    # Candidate extraction is a deliberate invalidation
                    # boundary. Even an identical re-extraction resets review
                    # state, so its fresh revision must not reuse older
                    # primary/critic mutation receipts.
                    id_key=f"{review_id_key}-r{catalog_revision}",
                    probe_attempt_id=probe_attempt_id,
                    addon_output_dir=addon_output_dir,
                    primary_reviewer=str(args.primary_reviewer),
                    primary_review_method=str(args.primary_review_method),
                    critic_reviewer=str(args.critic_reviewer),
                    critic_review_method=str(args.critic_review_method),
                    review_spec=document_review,
                    available_dependency_addons=dependency_addons,
                )
                generated_addon = portable.pop("_generated_addon")
                _register_generated_dependency_addon(
                    dependency_addons,
                    generated_addon,
                )
            report["documents"].append(
                {
                    "relative_path": relative_path,
                    "artifact": staged["result"]["artifact"],
                    "source_id": source_id,
                    "checksum": staged["result"]["checksum"],
                    "pages": inspection["page_count"],
                    "sections": inspection["sections"],
                    "chunks": inspection["chunks"],
                    "warnings": warnings,
                    "metadata": inspection["metadata"],
                    "candidate_count": len(candidates),
                    "candidate_kinds": _kind_counts(candidates),
                    "parsing_baseline": _candidate_baseline_metrics(
                        candidates,
                        document_review,
                    )
                    | {
                        "candidate_extraction_seconds": round(
                            candidate_extraction_seconds,
                            3,
                        )
                    },
                    "candidate_catalog": [
                        {
                            "id": item["id"],
                            "kind": item["kind"],
                            "name": item["name"],
                            "source_heading_path": item.get("source_heading_path", []),
                            "page_start": item.get("page_start"),
                            "page_end": item.get("page_end"),
                            "execution_state": item.get("execution_state"),
                        }
                        for item in candidates
                    ],
                    "content_inventory": {
                        key: value for key, value in inventory.items() if key != "ledger"
                    },
                    "transcription_review": (
                        {
                            key: value
                            for key, value in transcription_review.items()
                            if key != "inspection"
                        }
                        if transcription_review is not None
                        else None
                    ),
                    "statblock_recovery": statblock_recovery,
                    "statblock_slot_review": statblock_slot_review,
                    "catalog_augmentation": catalog_augmentation,
                    "source_scoped_search_hit": bool(hits),
                    "portable": portable,
                    "seconds": round(perf_counter() - item_started, 3),
                }
            )
            print(
                f"[OK {index}/{len(documents)}] {relative_path} "
                f"({perf_counter() - item_started:.1f}s)",
                file=sys.stderr,
                flush=True,
            )
        except Exception as error:  # regression harness must report every book
            message = f"{type(error).__name__}: {error}"
            report["errors"].append({"relative_path": relative_path, "error": message})
            if failure_snapshot is not None:
                report["failed_documents"].append(failure_snapshot | {"error": message})
            print(
                f"[FAIL {index}/{len(documents)}] {relative_path}: {message}",
                file=sys.stderr,
                flush=True,
            )
    report["seconds"] = round(perf_counter() - started, 3)
    report["parsing_baseline"] = _aggregate_baseline_metrics(report["documents"])
    report["observed_parsing_baseline"] = _aggregate_baseline_metrics(
        [*report["documents"], *report["failed_documents"]]
    )
    if args.measure_memory:
        _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        report["parsing_baseline"]["peak_traced_python_bytes"] = peak_bytes
        report["observed_parsing_baseline"]["peak_traced_python_bytes"] = peak_bytes
    else:
        report["parsing_baseline"]["peak_traced_python_bytes"] = None
        report["observed_parsing_baseline"]["peak_traced_python_bytes"] = None
    report["parsing_baseline"]["memory_tracing_enabled"] = bool(args.measure_memory)
    report["observed_parsing_baseline"]["memory_tracing_enabled"] = bool(args.measure_memory)
    report["passed"] = not report["errors"] and len(report["documents"]) == len(documents)
    return report


def _kind_counts(candidates: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for candidate in candidates:
        kind = str(candidate.get("kind") or "unknown")
        result[kind] = result.get(kind, 0) + 1
    return dict(sorted(result.items()))


def _failed_document_parsing_snapshot(
    relative_path: str,
    candidates: list[dict[str, Any]],
    review_spec: dict[str, Any],
    *,
    candidate_extraction_seconds: float,
    phase: str,
) -> dict[str, Any]:
    """Preserve extraction evidence when a later review or compile gate fails."""

    return {
        "relative_path": relative_path,
        "candidate_count": len(candidates),
        "candidate_kinds": _kind_counts(candidates),
        "failure_phase": phase,
        "parsing_baseline": _candidate_baseline_metrics(candidates, review_spec)
        | {"candidate_extraction_seconds": round(candidate_extraction_seconds, 3)},
    }


def _candidate_baseline_metrics(
    candidates: list[dict[str, Any]],
    review_spec: dict[str, Any],
) -> dict[str, Any]:
    """Return source-auditable parser metrics without changing expectations."""

    statuses: dict[str, int] = {}
    source_bound = 0
    source_chunk_ids: set[str] = set()
    identities: dict[tuple[str, str, tuple[str, ...]], int] = {}
    for candidate in candidates:
        status = str(candidate.get("review_status") or "unresolved").strip().casefold()
        statuses[status] = statuses.get(status, 0) + 1
        candidate_source_ids = {
            str(chunk_id)
            for chunk_id in candidate.get("source_chunk_ids") or []
            if str(chunk_id).strip()
        }
        if candidate_source_ids:
            source_bound += 1
            source_chunk_ids.update(candidate_source_ids)
        identity = (
            _fold_text(candidate.get("kind")),
            _fold_text(candidate.get("name")),
            tuple(_fold_text(value) for value in candidate.get("source_heading_path") or []),
        )
        identities[identity] = identities.get(identity, 0) + 1
    decisions = review_spec.get("decisions") or []
    reviewed_rejections = sum(
        1
        for decision in decisions
        if isinstance(decision, dict)
        and str(decision.get("status") or "").strip().casefold() == "rejected"
    )
    unresolved = sum(
        count
        for status, count in statuses.items()
        if status in {"pending", "needs_review", "needs-review", "unresolved"}
    )
    candidate_count = len(candidates)
    return {
        "entity_count": candidate_count,
        "fragment_count": sum(
            1
            for candidate in candidates
            if str(candidate.get("kind") or "").strip().casefold() == "source_fragment"
        ),
        "review_status_counts": dict(sorted(statuses.items())),
        "unresolved_count": unresolved,
        "reviewed_rejected_count": reviewed_rejections,
        "source_bound_entity_count": source_bound,
        "source_coverage_ratio": (
            round(source_bound / candidate_count, 6) if candidate_count else 1.0
        ),
        "unique_source_chunk_count": len(source_chunk_ids),
        "duplicate_identity_count": sum(count - 1 for count in identities.values() if count > 1),
    }


def _aggregate_baseline_metrics(documents: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = [dict(document.get("parsing_baseline") or {}) for document in documents]
    entity_count = sum(int(metric.get("entity_count") or 0) for metric in metrics)
    source_bound = sum(int(metric.get("source_bound_entity_count") or 0) for metric in metrics)
    return {
        "schema_version": 1,
        "document_count": len(documents),
        "entity_count": entity_count,
        "fragment_count": sum(int(metric.get("fragment_count") or 0) for metric in metrics),
        "unresolved_count": sum(int(metric.get("unresolved_count") or 0) for metric in metrics),
        "reviewed_rejected_count": sum(
            int(metric.get("reviewed_rejected_count") or 0) for metric in metrics
        ),
        "source_bound_entity_count": source_bound,
        "source_coverage_ratio": round(source_bound / entity_count, 6) if entity_count else 1.0,
        "duplicate_identity_count": sum(
            int(metric.get("duplicate_identity_count") or 0) for metric in metrics
        ),
        "candidate_extraction_seconds": round(
            sum(float(metric.get("candidate_extraction_seconds") or 0.0) for metric in metrics),
            3,
        ),
        "pipeline_seconds": round(
            sum(float(document.get("seconds") or 0.0) for document in documents),
            3,
        ),
    }


def _runtime_path_value(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                raise RuntimeError(f"runtime probe path is absent: {path}")
            current = current[part]
        elif isinstance(current, list) and part.isdecimal():
            index = int(part)
            if index >= len(current):
                raise RuntimeError(f"runtime probe path index is absent: {path}")
            current = current[index]
        else:
            raise RuntimeError(f"runtime probe path is not traversable: {path}")
    return current


def _assert_runtime_expectations(value: Any, expectations: Any) -> None:
    if not isinstance(expectations, list):
        raise ValueError("runtime probe expect must be an array")
    for index, raw_expectation in enumerate(expectations):
        if not isinstance(raw_expectation, dict):
            raise ValueError(f"runtime probe expect[{index}] must be an object")
        unknown = set(raw_expectation) - {
            "path",
            "equals",
            "contains",
            "contains_names",
            "length",
        }
        if unknown:
            raise ValueError(
                f"runtime probe expect[{index}] has unsupported fields: {sorted(unknown)}"
            )
        path = str(raw_expectation.get("path") or "").strip()
        operators = [
            key
            for key in ("equals", "contains", "contains_names", "length")
            if key in raw_expectation
        ]
        if not path or len(operators) != 1:
            raise ValueError(f"runtime probe expect[{index}] needs a path and exactly one operator")
        actual = _runtime_path_value(value, path)
        operator = operators[0]
        expected = raw_expectation[operator]
        if operator == "equals":
            passed = actual == expected
        elif operator == "contains":
            passed = isinstance(actual, (list, str, dict)) and expected in actual
        elif operator == "contains_names":
            if not isinstance(actual, list) or not isinstance(expected, list):
                passed = False
            else:
                actual_names = [
                    _fold_text(item.get("name")) for item in actual if isinstance(item, dict)
                ]
                passed = all(_fold_text(item) in actual_names for item in expected)
        else:
            passed = hasattr(actual, "__len__") and len(actual) == expected
        if not passed:
            raise RuntimeError(
                f"runtime probe expectation failed at {path}: "
                f"{operator}={expected!r}, actual={actual!r}"
            )


def _runtime_probe_artifact_matches(
    artifact: dict[str, Any],
    *,
    kind: str,
    name: str,
) -> bool:
    """Match reviewed display identities without depending on source capitalization."""

    return _fold_text(artifact.get("kind")) == _fold_text(kind) and _fold_text(
        dict(artifact.get("card") or {}).get("name")
    ) == _fold_text(name)


def _unwrapped_tool_result(value: Any) -> Any:
    if isinstance(value, dict) and "result" in value:
        return value["result"]
    return value


def _is_lower_sha256(value: Any) -> bool:
    return re.fullmatch(r"[0-9a-f]{64}", str(value or "")) is not None


def _complete_probe_hit_points(sheet: dict[str, Any], *, source: str) -> None:
    """Give a synthetic runtime probe a legal fixed-average HP progression."""

    classes = list(dict(sheet.get("progression") or {}).get("classes") or [])
    if len(classes) != 1:
        raise ValueError("runtime probes require exactly one class for HP generation")
    class_entry = dict(classes[0])
    level = int(dict(sheet["progression"]).get("level", 0) or 0)
    hit_die = int(class_entry.get("hit_die", 0) or 0)
    constitution = int(dict(sheet["abilities"]["constitution"]).get("score", 0) or 0)
    if not 1 <= level <= 20 or hit_die not in {6, 8, 10, 12}:
        raise ValueError("runtime probe needs a legal level and class hit die")
    constitution_modifier = (constitution - 10) // 2
    gains = [
        max(
            1,
            (hit_die if current_level == 1 else hit_die // 2 + 1) + constitution_modifier,
        )
        for current_level in range(1, level + 1)
    ]
    maximum = sum(gains)
    sheet["combat"]["hp"] = {"value": maximum, "max": maximum, "temp": 0}
    sheet["combat"]["hit_dice"] = {
        f"d{hit_die}": {
            "label": f"d{hit_die}",
            "value": level,
            "max": level,
            "recovers_on": "long_rest",
            "source_key": str(class_entry.get("name") or source),
        }
    }
    sheet["combat"]["hp_progression"] = [
        {
            "level": current_level,
            "method": "fixed",
            "value": gain,
            "source": source,
        }
        for current_level, gain in enumerate(gains, 1)
    ]


async def _run_content_runtime_probes(
    *,
    server: Any,
    campaign_id: str,
    edition: str,
    package: dict[str, Any],
    probes: Any,
    id_key: str,
) -> list[dict[str, Any]]:
    if probes in (None, []):
        return []
    if not isinstance(probes, list):
        raise ValueError("runtime_probes must be an array")
    if package.get("format") != "sagasmith.content-package":
        raise ValueError("runtime probes require a unified content package")
    content = package.get("content")
    if not isinstance(content, dict):
        raise ValueError("runtime probe package has no unified content object")
    artifacts = list(content.get("artifacts") or [])
    summaries: list[dict[str, Any]] = []
    for probe_index, raw_probe in enumerate(probes):
        if not isinstance(raw_probe, dict):
            raise ValueError(f"runtime_probes[{probe_index}] must be an object")
        unknown = set(raw_probe) - {
            "name",
            "level",
            "class_name",
            "ability_scores",
            "steps",
        }
        if unknown:
            raise ValueError(
                f"runtime_probes[{probe_index}] has unsupported fields: {sorted(unknown)}"
            )
        probe_name = str(raw_probe.get("name") or f"probe-{probe_index}").strip()
        steps = raw_probe.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"runtime_probes[{probe_index}] needs nonempty steps")
        sheet = default_character_sheet()
        sheet["edition"] = edition
        level = int(raw_probe.get("level", 1) or 1)
        if not 1 <= level <= 20:
            raise ValueError(f"runtime_probes[{probe_index}].level must be 1..20")
        class_name = str(raw_probe.get("class_name") or "Fighter").strip()
        sheet["progression"]["level"] = level
        sheet["progression"]["classes"] = [
            {"name": class_name, "level": level, "subclass": "", "hit_die": 10}
        ]
        ability_scores = raw_probe.get("ability_scores") or {}
        if not isinstance(ability_scores, dict):
            raise ValueError(f"runtime_probes[{probe_index}].ability_scores must be an object")
        for ability, raw_score in ability_scores.items():
            normalized_ability = str(ability).casefold()
            if normalized_ability not in sheet["abilities"]:
                raise ValueError(f"runtime probe has unknown ability: {ability}")
            score = int(raw_score)
            if not 1 <= score <= 30:
                raise ValueError("runtime probe ability scores must be 1..30")
            sheet["abilities"][normalized_ability]["score"] = score
        _complete_probe_hit_points(sheet, source=f"addon runtime probe {probe_name}")
        probe_key = f"{id_key}-content-{probe_index}"
        probe_identity = hashlib.sha256(probe_key.encode("utf-8")).hexdigest()[:10]
        created_response = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign_id,
                    "name": f"Addon runtime probe {probe_name} [{probe_identity}]",
                    "sheet": sheet,
                },
                "idempotency_key": f"regression-addon-content-character-{probe_key}",
            },
        )
        current = dict(_unwrapped_tool_result(created_response))
        step_summaries: list[dict[str, Any]] = []
        for step_index, raw_step in enumerate(steps):
            if not isinstance(raw_step, dict):
                raise ValueError("runtime probe step must be an object")
            unknown_step = set(raw_step) - {
                "kind",
                "name",
                "selection",
                "expect",
                "expect_error",
            }
            if unknown_step:
                raise ValueError(
                    f"runtime probe step has unsupported fields: {sorted(unknown_step)}"
                )
            kind = str(raw_step.get("kind") or "").strip()
            artifact_name = str(raw_step.get("name") or "").strip()
            matches = [
                artifact
                for artifact in artifacts
                if _runtime_probe_artifact_matches(
                    artifact,
                    kind=kind,
                    name=artifact_name,
                )
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    f"runtime probe needs exactly one {kind} named {artifact_name!r}; "
                    f"found {len(matches)}"
                )
            artifact_id = str(matches[0]["id"])
            arguments = {
                "character_id": current["id"],
                "artifact_id": artifact_id,
                "selection": dict(raw_step.get("selection") or {}),
                "expected_revision": current["revision"],
                "idempotency_key": f"regression-addon-content-{probe_key}-{step_index}",
            }
            expected_error = str(raw_step.get("expect_error") or "").strip()
            if expected_error:
                try:
                    await _call(server, "character_content_apply", arguments)
                except Exception as error:
                    if expected_error not in str(error):
                        raise RuntimeError(
                            f"runtime probe rejected for the wrong reason: {error}"
                        ) from error
                else:
                    raise RuntimeError("runtime probe expected content application to fail")
                fetched = await _call(
                    server,
                    "character_query",
                    {
                        "view": "get",
                        "payload": {"character_id": current["id"]},
                    },
                )
                after_failure = dict(_unwrapped_tool_result(fetched))
                if after_failure["revision"] != current["revision"]:
                    raise RuntimeError("failed runtime probe mutated the character revision")
                step_summaries.append(
                    {"artifact_id": artifact_id, "rejected": True, "revision": current["revision"]}
                )
                continue
            applied_response = await _call(server, "character_content_apply", arguments)
            applied = dict(_unwrapped_tool_result(applied_response))
            if applied.get("status") in {"pending_choice", "pending_ruling"}:
                raise RuntimeError(f"runtime probe did not materialize {artifact_name}: {applied}")
            _assert_runtime_expectations(applied, raw_step.get("expect") or [])
            artifact = matches[0]
            selection_contract = dict(artifact.get("selection_contract") or {})
            expected_materializer = str(selection_contract.get("materializer") or "")
            portable_reviewed_content_hash = str(
                selection_contract.get("reviewed_content_hash") or ""
            )
            content_context = dict(applied.get("content_context") or {})
            runtime_selection_contract = dict(content_context.get("selection_contract") or {})
            runtime_reviewed_content_hash = str(
                runtime_selection_contract.get("reviewed_content_hash") or ""
            )
            context_drift = {
                field: {"expected": expected, "actual": actual}
                for field, expected, actual in (
                    ("artifact_id", artifact_id, content_context.get("artifact_id")),
                    ("pack_id", package.get("id"), content_context.get("pack_id")),
                    (
                        "pack_version",
                        package.get("version"),
                        content_context.get("pack_version"),
                    ),
                    ("selection_status", "ready", runtime_selection_contract.get("status")),
                    (
                        "materializer",
                        expected_materializer,
                        runtime_selection_contract.get("materializer"),
                    ),
                )
                if actual != expected
            }
            if not _is_lower_sha256(content_context.get("content_hash")):
                context_drift["content_hash"] = {
                    "expected": "lowercase SHA-256",
                    "actual": content_context.get("content_hash"),
                }
            if not _is_lower_sha256(runtime_reviewed_content_hash):
                context_drift["runtime_reviewed_content_hash"] = {
                    "expected": "lowercase SHA-256",
                    "actual": runtime_reviewed_content_hash,
                }
            if not _is_lower_sha256(content_context.get("catalog_review_hash")):
                context_drift["catalog_review_hash"] = {
                    "expected": "lowercase SHA-256",
                    "actual": content_context.get("catalog_review_hash"),
                }
            if context_drift:
                raise RuntimeError(
                    "runtime probe returned incomplete content context for "
                    f"{artifact_name}: {json.dumps(context_drift, ensure_ascii=False)}"
                )
            response_receipts = [
                dict(receipt)
                for receipt in applied.get("rule_receipts") or []
                if isinstance(receipt, dict) and receipt.get("artifact_id") == artifact_id
            ]
            if len(response_receipts) != 1:
                raise RuntimeError(f"runtime probe needs one content receipt for {artifact_name}")
            content_receipt = response_receipts[0]
            if (
                content_receipt.get("mechanic_id") != expected_materializer
                or content_receipt.get("reviewed_content_hash") != runtime_reviewed_content_hash
                or content_receipt.get("selection") != dict(raw_step.get("selection") or {})
            ):
                raise RuntimeError(f"runtime probe content receipt drifted for {artifact_name}")
            replayed_response = await _call(server, "character_content_apply", arguments)
            replayed = dict(_unwrapped_tool_result(replayed_response))
            if replayed != applied:
                raise RuntimeError(f"runtime probe was not idempotent for {artifact_name}")
            persisted_response = await _call(
                server,
                "campaign_rules",
                {
                    "campaign_id": campaign_id,
                    "action": "receipts",
                    "payload": {"mechanic_id": expected_materializer, "limit": 200},
                },
            )
            persisted_receipts = list(_unwrapped_tool_result(persisted_response))
            if not any(
                isinstance(entry, dict)
                and isinstance(entry.get("receipt"), dict)
                and entry["receipt"].get("artifact_id") == artifact_id
                and entry["receipt"].get("mechanic_id") == expected_materializer
                and entry["receipt"].get("reviewed_content_hash") == runtime_reviewed_content_hash
                for entry in persisted_receipts
            ):
                raise RuntimeError(f"runtime probe receipt was not persisted for {artifact_name}")
            current = applied
            step_summaries.append(
                {
                    "artifact_id": artifact_id,
                    "rejected": False,
                    "revision": current["revision"],
                    "content_context_hash": content_context["content_hash"],
                    "portable_reviewed_content_hash": portable_reviewed_content_hash,
                    "runtime_reviewed_content_hash": runtime_reviewed_content_hash,
                    "content_receipt": content_receipt,
                    "idempotent": True,
                    "persisted_receipt": True,
                }
            )
        summaries.append(
            {
                "name": probe_name,
                "character_id": current["id"],
                "steps": step_summaries,
            }
        )
    return summaries


def _core_content_dependency(edition: str) -> dict[str, str]:
    if edition == "2014":
        return {"id": SRD2014_PACK_ID, "version": SRD2014_PACK_VERSION}
    if edition == "2024":
        return {"id": SRD2024_PACK_ID, "version": SRD2024_PACK_VERSION}
    raise ValueError(f"unsupported D&D edition: {edition}")


def _statblock_recovery_needed(
    candidates: list[dict[str, Any]],
    source_chunks: list[dict[str, Any]],
) -> bool:
    """Run visual OCR only when indexed source cannot prove a usable card.

    A source-dependent companion is intentionally not a static actor card and
    must not trigger increasingly large OCR models merely because its printed
    HP field is a formula.  Documents with no extracted statblock still run
    discovery recovery so a missed visual card cannot disappear silently.
    """

    statblocks = [
        candidate for candidate in candidates if str(candidate.get("kind") or "") == "statblock"
    ]
    if not statblocks:
        return True
    page_count = max(
        (int(chunk.get("page_end") or chunk.get("page_start") or 0) for chunk in source_chunks),
        default=0,
    )
    index_hints = _statblock_index_recovery_hints(
        source_chunks,
        statblocks,
        page_count=page_count,
    )
    indexed_names = [name for names in index_hints["by_page"].values() for name in names]
    if any(
        not any(
            _bounded_ocr_heading_equivalent(
                indexed_name,
                str(candidate.get("name") or ""),
            )
            for candidate in statblocks
        )
        for indexed_name in indexed_names
    ):
        return True
    claimed_chunk_ids = {
        str(chunk_id)
        for candidate in statblocks
        for chunk_id in candidate.get("source_chunk_ids") or []
    }
    for chunk in source_chunks:
        if str(chunk.get("id") or "") in claimed_chunk_ids:
            continue
        folded = " ".join(str(chunk.get("content") or "").split()).casefold()
        if all(label in folded for label in ("armor class", "hit points", "speed")):
            return True
    chunks_by_id = {
        str(chunk.get("id") or ""): str(chunk.get("content") or "") for chunk in source_chunks
    }
    for candidate in statblocks:
        card = dict(dict(candidate.get("artifact") or {}).get("card") or {})
        normalized = str(
            card.get("normalized_content") or candidate.get("normalized_content") or ""
        ).strip()
        raw_source = "\n\n".join(
            chunks_by_id.get(str(chunk_id), "").strip()
            for chunk_id in candidate.get("source_chunk_ids") or []
            if chunks_by_id.get(str(chunk_id), "").strip()
        )
        probe = normalized or (
            f"# {str(candidate.get('name') or '').strip()}\n\n{raw_source}" if raw_source else ""
        )
        template_requirement = parameterized_statblock_requirements(probe)
        if template_requirement is not None:
            if dependent_actor_template_solution_errors(template_requirement):
                return True
            try:
                parse_2014_statblock_template_preview(
                    probe,
                    source_key=str(candidate.get("id") or "regression-template"),
                    rule_refs=[],
                    name=str(candidate.get("name") or "").strip() or None,
                )
            except (StatblockImportError, ValueError):
                return True
            continue
        if not normalized:
            return True
        try:
            parse_2014_statblock(
                normalized,
                source_key=str(candidate.get("id") or "regression-statblock"),
                rule_refs=[],
            )
        except (StatblockImportError, ValueError):
            return True
    return False


def _expected_actor_recovery_needed(
    candidates: list[dict[str, Any]],
    expected_actor_names: Any,
) -> bool:
    """Require every source-reviewed actor before skipping visual recovery.

    Layout parsing can attach a complete card to the preceding actor, leaving no
    unclaimed AC/HP/Speed chunk for the generic recovery heuristic to notice.
    The review manifest is the authoritative bounded inventory. Agent-authored
    corrected additions participate as candidates before this check, so a
    source-bound correction prevents redundant OCR while an unexplained omission
    still forces recovery.
    """

    if expected_actor_names is None:
        return False
    if not isinstance(expected_actor_names, list) or any(
        not isinstance(name, str) or not name.strip() for name in expected_actor_names
    ):
        raise ValueError("expected_actor_names must be an array of nonempty strings")
    statblock_names = [
        str(dict(dict(candidate.get("artifact") or {}).get("card") or {}).get("name") or "")
        or str(candidate.get("name") or "")
        for candidate in candidates
        if str(candidate.get("kind") or "") == "statblock"
    ]
    return any(
        not any(
            _bounded_ocr_heading_equivalent(expected_name, candidate_name)
            for candidate_name in statblock_names
        )
        for expected_name in expected_actor_names
    )


def _prefer_reviewed_statblock_additions(
    candidates: list[dict[str, Any]],
    additions: list[dict[str, Any]],
    source_chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Let an exact Agent-authored card satisfy recovery before visual OCR.

    A complete catalog review can bind a corrected statblock directly to the
    indexed source chunks.  Running full-page OCR first wastes substantial CPU
    and can reintroduce layout contamination that the reviewed boundary already
    removed.  The normal recovery gate still parses every preferred card and
    falls back to OCR if the Agent supplied incomplete mechanics.
    """

    reviewed = [
        addition
        for addition in additions
        if str(addition.get("kind") or "") == "statblock"
        and str(dict(addition.get("card") or {}).get("normalized_content") or "").strip()
    ]
    if not reviewed:
        return list(candidates)
    for index, addition in enumerate(reviewed):
        if any(
            _bounded_ocr_heading_equivalent(
                str(addition.get("name") or ""),
                str(other.get("name") or ""),
            )
            for other in reviewed[index + 1 :]
        ):
            raise ValueError(
                "reviewed statblock additions contain an ambiguous identity: "
                f"{addition.get('name')}"
            )
    retained = [
        candidate
        for candidate in candidates
        if str(candidate.get("kind") or "") != "statblock"
        or not any(
            _bounded_ocr_heading_equivalent(
                str(candidate.get("name") or ""),
                str(addition.get("name") or ""),
            )
            for addition in reviewed
        )
    ]
    chunks_by_id = {
        str(chunk.get("id") or ""): chunk for chunk in source_chunks if str(chunk.get("id") or "")
    }
    for index, addition in enumerate(reviewed):
        source_chunk_ids = [
            str(chunk_id) for chunk_id in addition.get("source_chunk_ids") or [] if str(chunk_id)
        ]
        page_starts = [
            int(chunks_by_id[chunk_id]["page_start"])
            for chunk_id in source_chunk_ids
            if chunk_id in chunks_by_id
            and isinstance(chunks_by_id[chunk_id].get("page_start"), int)
            and not isinstance(chunks_by_id[chunk_id].get("page_start"), bool)
        ]
        card = json.loads(json.dumps(addition["card"]))
        card.setdefault("name", str(addition.get("name") or "").strip())
        retained.append(
            {
                "id": f"agent-reviewed-statblock-{index}",
                "kind": "statblock",
                "name": str(addition.get("name") or "").strip(),
                "source_chunk_ids": source_chunk_ids,
                "page_start": min(page_starts) if page_starts else None,
                "artifact": {"kind": "statblock", "card": card},
            }
        )
    return retained


async def _enable_core_content_pack(
    server: Any,
    *,
    campaign_id: str,
    edition: str,
    run_id: str,
) -> dict[str, Any]:
    """Enable the installed built-in content dependency on a receiver branch.

    Auto-seeding installs the immutable package globally, but activation is a
    separate campaign/branch mutation.  Addon activation must therefore prove
    the exact dependency is enabled through the public revisioned facade.
    """

    dependency = _core_content_dependency(edition)
    profile = await _call(
        server,
        "campaign_rules",
        {"campaign_id": campaign_id, "action": "get_profile"},
    )
    result = dict(profile["result"])
    matching = [
        item
        for item in result.get("activations") or []
        if str(item.get("pack_id") or "") == dependency["id"]
        and str(item.get("version") or "") == dependency["version"]
        and item.get("enabled") is True
    ]
    if matching:
        return matching[0]
    revision = int(result["campaign_revision"])
    activated = await _call(
        server,
        "content_pack",
        {
            "action": "activate",
            "payload": {
                "kind": "rule",
                "campaign_id": campaign_id,
                "pack_id": dependency["id"],
                "version": dependency["version"],
            },
            "expected_revision": revision,
            "idempotency_key": (
                f"rulebook-portable-core-activation-{_run_token(run_id)}-r{revision}"
            ),
        },
    )
    activation = dict(activated["result"]["activation"])
    if activation.get("enabled") is not True:
        raise RuntimeError("portable receiver did not enable its core content dependency")
    return activation


def _load_catalog_manifest(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"version": 1, "documents": {}}

    def load(resolved: Path, ancestors: tuple[Path, ...]) -> dict[str, Any]:
        if resolved in ancestors:
            chain = " -> ".join(str(item) for item in (*ancestors, resolved))
            raise ValueError(f"catalog manifest include cycle: {chain}")
        payload = json.loads(resolved.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("catalog manifest must be a version 1 JSON object")
        own_documents = payload.get("documents")
        if not isinstance(own_documents, dict):
            raise ValueError("catalog manifest documents must be an object")
        raw_includes = payload.get("includes") or []
        if not isinstance(raw_includes, list) or any(
            not isinstance(item, str) or not item.strip() for item in raw_includes
        ):
            raise ValueError("catalog manifest includes must be an array of paths")
        documents: dict[str, Any] = {}
        for include in raw_includes:
            included_path = (resolved.parent / include).resolve()
            included = load(included_path, (*ancestors, resolved))
            duplicates = set(documents).intersection(included["documents"])
            if duplicates:
                raise ValueError(
                    f"catalog manifest includes duplicate documents: {sorted(duplicates)}"
                )
            documents.update(included["documents"])
        duplicates = set(documents).intersection(own_documents)
        if duplicates:
            raise ValueError(f"catalog manifest includes duplicate documents: {sorted(duplicates)}")
        documents.update(own_documents)
        return {
            **payload,
            "documents": documents,
        }

    return load(path.expanduser().resolve(), ())


def _effective_catalog_manifest_path(
    path: Path | None,
    *,
    content_roundtrip: bool,
) -> Path | None:
    """Fail closed to the bundled strict review for portable content builds."""

    if path is not None:
        return path.expanduser().resolve()
    if not content_roundtrip:
        return None
    resolved = DEFAULT_CONTENT_CATALOG_MANIFEST.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"bundled content catalog manifest is missing: {resolved}")
    return resolved


def _catalog_document_review(
    manifest: dict[str, Any],
    relative_path: str,
) -> dict[str, Any]:
    documents = dict(manifest.get("documents") or {})
    normalized_path = relative_path.replace("/", "\\").casefold()
    matches = [
        value
        for key, value in documents.items()
        if str(key).replace("/", "\\").casefold() == normalized_path
    ]
    if len(matches) > 1:
        raise ValueError(f"catalog manifest duplicates {relative_path}")
    if not matches:
        if manifest.get("strict") is True:
            raise ValueError(f"catalog manifest has no complete review for {relative_path}")
        return {}
    review = matches[0]
    if not isinstance(review, dict):
        raise ValueError(f"catalog manifest entry for {relative_path} must be an object")
    if review.get("complete_review") is not True:
        raise ValueError(
            f"catalog manifest entry for {relative_path} is not marked complete_review"
        )
    unknown = set(review) - {
        "complete_review",
        "rationale",
        "additions",
        "addition_default_status",
        "decisions",
        "default_status",
        "default_status_by_kind",
        "expected_catalog",
        "expected_catalog_sha256",
        "expected_counts",
        "expected_actor_names",
        "expected_actor_names_sha256",
        "expected_dependent_actor_names",
        "expected_actor_cards",
        "runtime_probes",
        "dependency_addons",
        "text_reviews",
        "statblock_slot_reviews",
    }
    if unknown:
        raise ValueError(
            f"catalog manifest entry for {relative_path} has unsupported fields: {sorted(unknown)}"
        )
    expected_counts = review.get("expected_counts")
    if isinstance(expected_counts, dict) and int(expected_counts.get("statblock") or 0) > 0:
        if "expected_actor_names" not in review and "expected_actor_names_sha256" not in review:
            raise ValueError(
                f"catalog manifest entry for {relative_path} has statblocks but no exact "
                "expected actor-name inventory"
            )
    return review


def _load_dependency_addons(paths: list[Path]) -> dict[tuple[str, str], dict[str, Any]]:
    """Load exact unified addon archives for later public-MCP validation."""

    packages: dict[tuple[str, str], dict[str, Any]] = {}
    for path in paths:
        resolved = path.expanduser().resolve()
        package, _blobs = loads_content_archive(resolved.read_bytes())
        if package.get("kind") != "addon":
            raise ValueError(f"dependency addon is not an addon package: {resolved}")
        package["_local_archive_path"] = str(resolved)
        identity = (str(package.get("id") or ""), str(package.get("version") or ""))
        if not all(identity):
            raise ValueError(f"dependency addon has no exact id/version: {resolved}")
        prior = packages.get(identity)
        if prior is not None and prior != package:
            raise ValueError(
                f"dependency addon identity has conflicting envelopes: {identity[0]}@{identity[1]}"
            )
        packages[identity] = package
    return packages


def _register_generated_dependency_addon(
    available: dict[tuple[str, str], dict[str, Any]],
    package: dict[str, Any],
) -> None:
    """Make this run's reviewed addon authoritative for later documents."""

    if not isinstance(package, dict) or package.get("kind") != "addon":
        raise ValueError("generated dependency is not an addon package")
    identity = (str(package.get("id") or ""), str(package.get("version") or ""))
    checksum = str(package.get("checksum") or "")
    if not all(identity) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
        raise ValueError("generated dependency addon needs exact id, version, and checksum")
    # A command-line dependency bootstraps a run whose dependent document may
    # sort after its provider. Once that provider is rebuilt and round-tripped
    # in this run, its newly reviewed envelope must replace the bootstrap copy.
    available[identity] = package


def _selected_dependency_addons(
    review_spec: dict[str, Any],
    available: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve only the exact dependency addons declared by this source review."""

    requirements = review_spec.get("dependency_addons") or []
    if not isinstance(requirements, list):
        raise ValueError("dependency_addons must be an array")
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict) or set(requirement) - {
            "id",
            "version",
            "checksum",
        }:
            raise ValueError(
                f"dependency_addons[{index}] must contain only id, version, and optional checksum"
            )
        identity = (
            str(requirement.get("id") or ""),
            str(requirement.get("version") or ""),
        )
        if not all(identity):
            raise ValueError(f"dependency_addons[{index}] requires exact id and version")
        if identity in seen:
            raise ValueError(f"duplicate dependency addon: {identity[0]}@{identity[1]}")
        package = available.get(identity)
        if package is None:
            raise ValueError(
                "required dependency addon was not supplied with --dependency-addon: "
                f"{identity[0]}@{identity[1]}"
            )
        expected_checksum = str(requirement.get("checksum") or "")
        if expected_checksum and expected_checksum != str(package.get("checksum") or ""):
            raise ValueError(f"dependency addon checksum mismatch: {identity[0]}@{identity[1]}")
        selected.append(package)
        seen.add(identity)
    return selected


def _dependency_rule_components(
    packages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collect exact rule definitions in dependency order without embedding them."""

    components: list[dict[str, Any]] = []
    by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    for addon in packages:
        content = addon.get("content")
        if not isinstance(content, dict):
            raise ValueError("dependency addon has no unified content object")
        for component in content.get("rule_definitions") or []:
            identity = (
                str(component.get("id") or ""),
                str(component.get("version") or ""),
            )
            if not all(identity):
                raise ValueError("dependency addon contains a rule pack without id/version")
            normalized = {
                "id": identity[0],
                "version": identity[1],
                "checksum": str(component.get("definition_checksum") or ""),
            }
            prior = by_identity.get(identity)
            if prior is not None:
                if prior != normalized:
                    raise ValueError(
                        f"dependency rule component conflict: {identity[0]}@{identity[1]}"
                    )
                continue
            by_identity[identity] = normalized
            components.append(normalized)
    return components


async def _import_dependency_addons(
    *,
    server: Any,
    campaign_id: str,
    archive_dir: Path,
    dependencies: list[dict[str, Any]],
    receiver: str,
) -> None:
    """Install exact dependency envelopes before compiling or receiving an addon."""

    for dependency in dependencies:
        dependency_path = Path(dependency["_local_archive_path"]).resolve()
        dependency_payload = (
            {"artifact": dependency_path.name}
            if dependency_path.parent == archive_dir.resolve()
            else {"source_path": str(dependency_path)}
        )
        imported = await _call(
            server,
            "content_pack",
            {
                "action": "import",
                "payload": {
                    "kind": "addon",
                    "campaign_id": campaign_id,
                    **dependency_payload,
                },
                "idempotency_key": (
                    f"regression-dependency-addon-import-{receiver}-"
                    f"{hashlib.sha256(str(dependency['checksum']).encode('utf-8')).hexdigest()[:20]}"
                ),
            },
        )
        if imported["result"]["installed"] is not True:
            raise RuntimeError(
                f"dependency addon did not install on {receiver} through the public MCP facade"
            )


async def _set_dependency_addons_enabled(
    *,
    server: Any,
    campaign_id: str,
    dependencies: list[dict[str, Any]],
    enabled: bool,
) -> None:
    """Acquire dependency branch locks in order and release them in reverse order."""

    ordered = dependencies if enabled else list(reversed(dependencies))
    for dependency in ordered:
        profile = await _call(
            server,
            "campaign_rules",
            {"campaign_id": campaign_id, "action": "get_profile"},
        )
        revision = int(profile["result"]["campaign_revision"])
        changed = await _call(
            server,
            "content_pack",
            {
                "action": "activate" if enabled else "deactivate",
                "payload": {
                    "kind": "addon",
                    "campaign_id": campaign_id,
                    "addon_id": str(dependency["id"]),
                    "version": str(dependency["version"]),
                },
                "expected_revision": revision,
                "idempotency_key": (
                    "regression-dependency-addon-"
                    f"{'enable' if enabled else 'disable'}-"
                    f"{hashlib.sha256(str(dependency['checksum']).encode('utf-8')).hexdigest()[:20]}-"
                    f"r{revision}"
                ),
            },
        )
        if changed["result"]["activation"]["enabled"] is not enabled:
            raise RuntimeError(
                f"dependency addon {dependency['id']} did not reach enabled={enabled}"
            )


async def _source_chunks(server: Any, source_id: str) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    while True:
        response = await _call(
            server,
            "content_pack",
            {
                "action": "get",
                "payload": {
                    "kind": "source",
                    "source_id": source_id,
                    "limit": 200,
                    "offset": len(chunks),
                },
            },
        )
        page = list(response["result"])
        chunks.extend(page)
        if len(page) < 200:
            return chunks


def _fold_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _source_selector_matches(
    chunk: dict[str, Any],
    selector: dict[str, Any],
) -> bool:
    unknown = set(selector) - {
        "heading_exact",
        "heading_contains",
        "content_contains",
        "page_start",
        "page_end",
        "start_contains",
        "end_before_contains",
        "end_contains",
        "match_all",
    }
    if unknown:
        raise ValueError(f"source selector has unsupported fields: {sorted(unknown)}")
    if not selector:
        raise ValueError("source selector cannot be empty")
    heading_parts = [str(value) for value in chunk.get("heading_path") or []]
    heading = " > ".join(heading_parts)
    if "heading_exact" in selector:
        expected = selector["heading_exact"]
        if isinstance(expected, list):
            if [_fold_text(value) for value in expected] != [
                _fold_text(value) for value in heading_parts
            ]:
                return False
        elif _fold_text(expected) not in {
            _fold_text(heading),
            _fold_text(heading_parts[-1] if heading_parts else ""),
        }:
            return False
    if "heading_contains" in selector:
        expected_heading = str(selector["heading_contains"])
        if _fold_text(expected_heading) not in _fold_text(heading) and not any(
            _bounded_ocr_heading_equivalent(expected_heading, part) for part in heading_parts
        ):
            return False
    if "content_contains" in selector and _fold_text(
        selector["content_contains"]
    ) not in _fold_text(chunk.get("content")):
        return False
    for key in ("page_start", "page_end"):
        if key in selector and int(chunk.get(key) or -1) != int(selector[key]):
            return False
    return True


def _resolve_catalog_additions(
    additions: Any,
    chunks: list[dict[str, Any]],
    *,
    relative_path: str,
) -> list[dict[str, Any]]:
    if not isinstance(additions, list) or not additions:
        raise ValueError(f"catalog additions for {relative_path} must be a nonempty list")
    resolved: list[dict[str, Any]] = []
    for index, addition in enumerate(additions):
        if not isinstance(addition, dict):
            raise ValueError(f"catalog addition {index} for {relative_path} must be an object")
        unknown = set(addition) - {
            "kind",
            "name",
            "source_selectors",
            "card",
            "note",
            "replace_existing",
        }
        if unknown:
            raise ValueError(
                f"catalog addition {index} for {relative_path} has unsupported fields: "
                f"{sorted(unknown)}"
            )
        selectors = addition.get("source_selectors")
        if not isinstance(selectors, list) or not selectors:
            raise ValueError(f"catalog addition {index} for {relative_path} needs source_selectors")
        chunk_ids: list[str] = []
        source_spans: list[dict[str, Any]] = []
        for selector_index, selector in enumerate(selectors):
            if not isinstance(selector, dict):
                raise ValueError(
                    f"source selector {selector_index} for {relative_path} must be an object"
                )
            matches = [chunk for chunk in chunks if _source_selector_matches(chunk, selector)]
            match_all = selector.get("match_all", False)
            if not isinstance(match_all, bool):
                raise ValueError("source selector match_all must be a boolean")
            if (not match_all and len(matches) != 1) or (match_all and not matches):
                raise ValueError(
                    f"source selector {selector_index} for catalog addition {index} "
                    f"{str(addition.get('kind') or '')}:{str(addition.get('name') or '')} "
                    f"in {relative_path} matched "
                    f"{len(matches)} chunks; expected "
                    + ("one or more" if match_all else "exactly one")
                )
            for chunk in sorted(
                matches,
                key=lambda item: (
                    int(item.get("ordinal") or 0),
                    str(item.get("id") or ""),
                ),
            ):
                chunk_id = str(chunk["id"])
                content = str(chunk.get("content") or "")
                chunk_ids.append(chunk_id)
                if not content:
                    continue
                start = 0
                if "start_contains" in selector:
                    needle = str(selector["start_contains"])
                    start = content.casefold().find(needle.casefold())
                    if start < 0:
                        raise ValueError(
                            f"source selector {selector_index} for {relative_path} "
                            "did not find start_contains"
                        )
                end = len(content)
                if "end_before_contains" in selector:
                    needle = str(selector["end_before_contains"])
                    end = content.casefold().find(needle.casefold(), start + 1)
                    if end < 0:
                        raise ValueError(
                            f"source selector {selector_index} for {relative_path} "
                            "did not find end_before_contains"
                        )
                elif "end_contains" in selector:
                    needle = str(selector["end_contains"])
                    found = content.casefold().find(needle.casefold(), start)
                    if found < 0:
                        raise ValueError(
                            f"source selector {selector_index} for {relative_path} "
                            "did not find end_contains"
                        )
                    end = found + len(needle)
                while start < end and content[start].isspace():
                    start += 1
                while end > start and content[end - 1].isspace():
                    end -= 1
                if start >= end:
                    raise ValueError(
                        f"source selector {selector_index} for {relative_path} "
                        "resolved an empty source span"
                    )
                exact_text = content[start:end]
                source_spans.append(
                    {
                        "source_chunk_id": chunk_id,
                        "start": start,
                        "end": end,
                        "checksum": hashlib.sha256(exact_text.encode("utf-8")).hexdigest(),
                    }
                )
        if not source_spans:
            raise ValueError(
                f"catalog addition {index} {str(addition.get('kind') or '')}:"
                f"{str(addition.get('name') or '')} in {relative_path} "
                "matched a heading with no indexed text"
            )
        resolved.append(
            {
                key: addition[key]
                for key in ("kind", "name", "card", "note", "replace_existing")
                if key in addition
            }
            | {
                "source_chunk_ids": list(dict.fromkeys(chunk_ids)),
                "source_spans": source_spans,
            }
        )
    return resolved


def _bind_catalog_addition_replacements(
    additions: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Turn source-review entries into replacements when extraction found them."""

    def identity(kind: Any, name: Any) -> tuple[str, str]:
        return (
            str(kind or "").casefold(),
            "".join(character for character in str(name or "").casefold() if character.isalnum()),
        )

    candidates_by_identity: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for candidate in candidates:
        candidates_by_identity.setdefault(
            identity(candidate.get("kind"), candidate.get("name")), []
        ).append(candidate)
    bound: list[dict[str, Any]] = []
    consumed_candidates: set[int] = set()
    for addition in additions:
        item = json.loads(json.dumps(addition))
        if "replace_existing" not in item:
            matches = candidates_by_identity.get(identity(item.get("kind"), item.get("name")), [])
            source_chunk_ids = {
                str(chunk_id) for chunk_id in item.get("source_chunk_ids", []) if str(chunk_id)
            }
            if source_chunk_ids:
                matches = [
                    candidate
                    for candidate in matches
                    if source_chunk_ids.intersection(
                        str(chunk_id) for chunk_id in candidate.get("source_chunk_ids", [])
                    )
                ]
            if len(matches) > 1:
                raise ValueError(
                    "catalog addition matches multiple source candidates: "
                    f"{item.get('kind')}:{item.get('name')}"
                )
            if matches and id(matches[0]) not in consumed_candidates:
                item["replace_existing"] = True
                consumed_candidates.add(id(matches[0]))
            elif matches:
                # Repeated headings can be merged into one extracted candidate.
                # Replace that candidate once, then add each remaining reviewed
                # source slice as an independent same-name artifact.
                item["replace_existing"] = False
        bound.append(item)
    return bound


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(base))
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = json.loads(json.dumps(value))
    return result


def _refresh_patched_statblock_evidence(
    artifact: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Bind a catalog Agent's boundary repair to its exact reviewed text."""

    if artifact.get("kind") != "statblock":
        return artifact
    patched_card = patch.get("card")
    if not isinstance(patched_card, dict) or "normalized_content" not in patched_card:
        return artifact
    card = dict(artifact.get("card") or {})
    source_text = str(card.get("normalized_content") or "").strip()
    evidence = card.get("review_evidence")
    if not source_text or not isinstance(evidence, dict):
        return artifact
    evidence = dict(evidence)
    evidence["normalized_content_sha256"] = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    card["review_evidence"] = evidence
    return {**artifact, "card": card}


def _review_spec_decisions(
    candidates: list[dict[str, Any]],
    review_spec: dict[str, Any],
    *,
    reviewer: str,
    method: str,
) -> list[dict[str, Any]]:
    raw_rules = review_spec.get("decisions") or []
    if not isinstance(raw_rules, list):
        raise ValueError("catalog manifest decisions must be a list")
    rules: list[dict[str, Any]] = []
    for index, rule in enumerate(raw_rules):
        if not isinstance(rule, dict):
            raise ValueError(f"catalog decision {index} must be an object")
        unknown = set(rule) - {
            "kind",
            "name",
            "candidate_origin",
            "source_heading_exact",
            "source_heading_contains",
            "status",
            "artifact_patch",
            "note",
        }
        if unknown:
            raise ValueError(f"catalog decision {index} has unsupported fields: {sorted(unknown)}")
        key = (_fold_text(rule.get("kind")), _fold_text(rule.get("name")))
        if not all(key):
            raise ValueError(f"catalog decision {index} has an invalid identity")
        candidate_origin = rule.get("candidate_origin")
        if candidate_origin not in {None, "extracted", "agent_addition"}:
            raise ValueError(
                f"catalog decision {index}.candidate_origin must be extracted or agent_addition"
            )
        rules.append(rule)
    default_status = str(review_spec.get("default_status") or "accepted")
    if default_status not in {"accepted", "rejected"}:
        raise ValueError("catalog manifest default_status must be accepted or rejected")
    addition_default_status = str(review_spec.get("addition_default_status") or default_status)
    if addition_default_status not in {"accepted", "rejected"}:
        raise ValueError("catalog manifest addition_default_status must be accepted or rejected")
    raw_defaults_by_kind = review_spec.get("default_status_by_kind") or {}
    if not isinstance(raw_defaults_by_kind, dict):
        raise ValueError("catalog manifest default_status_by_kind must be an object")
    defaults_by_kind = {
        _fold_text(kind): str(status) for kind, status in raw_defaults_by_kind.items()
    }
    if any(not kind for kind in defaults_by_kind) or any(
        status not in {"accepted", "rejected"} for status in defaults_by_kind.values()
    ):
        raise ValueError(
            "catalog manifest default_status_by_kind must map nonempty kinds "
            "to accepted or rejected"
        )
    decisions: list[dict[str, Any]] = []
    matched: set[int] = set()
    for candidate in candidates:
        key = (_fold_text(candidate.get("kind")), _fold_text(candidate.get("name")))
        heading_path = [_fold_text(value) for value in candidate.get("source_heading_path") or []]
        matching_rules: list[tuple[int, dict[str, Any]]] = []
        for rule_index, rule in enumerate(rules):
            rule_key = (_fold_text(rule.get("kind")), _fold_text(rule.get("name")))
            if key[0] != rule_key[0] or (
                key[1] != rule_key[1]
                and not _bounded_ocr_heading_equivalent(
                    str(candidate.get("name") or ""),
                    str(rule.get("name") or ""),
                )
            ):
                continue
            candidate_origin = rule.get("candidate_origin")
            is_agent_addition = bool(candidate.get("agent_catalog_addition"))
            if candidate_origin == "extracted" and is_agent_addition:
                continue
            if candidate_origin == "agent_addition" and not is_agent_addition:
                continue
            exact = rule.get("source_heading_exact")
            if exact is not None:
                expected_path = (
                    [_fold_text(value) for value in exact]
                    if isinstance(exact, list)
                    else [_fold_text(exact)]
                )
                if isinstance(exact, list):
                    exact_matches = expected_path == heading_path
                else:
                    exact_matches = (
                        expected_path == heading_path or expected_path == heading_path[-1:]
                    )
                if not exact_matches:
                    continue
            contains = rule.get("source_heading_contains")
            if contains is not None and _fold_text(contains) not in " > ".join(heading_path):
                continue
            matching_rules.append((rule_index, rule))
        if len(matching_rules) > 1:
            raise ValueError(f"multiple catalog decisions match candidate {key}")
        rule = matching_rules[0][1] if matching_rules else {}
        if matching_rules:
            matched.add(matching_rules[0][0])
        status = str(
            rule.get("status")
            or (addition_default_status if candidate.get("agent_catalog_addition") else None)
            or defaults_by_kind.get(key[0])
            or default_status
        )
        if status not in {"accepted", "rejected"}:
            raise ValueError(f"catalog decision for {key} has invalid status {status}")
        decision: dict[str, Any] = {
            "id": candidate["id"],
            "review_status": status,
        }
        if status == "accepted":
            note = str(
                rule.get("note")
                or (
                    "Reviewed typed card identity, classification, entry boundary, "
                    "and exact indexed references."
                )
            )
            decision["catalog_review_decision"] = _catalog_review_decision(
                role="primary",
                reviewer=reviewer,
                method=method,
                notes=note,
            )
            artifact_patch = rule.get("artifact_patch")
            if artifact_patch is not None:
                if not isinstance(artifact_patch, dict):
                    raise ValueError(f"artifact_patch for {key} must be an object")
                patched_artifact = _deep_merge(
                    dict(candidate.get("artifact") or {}),
                    artifact_patch,
                )
                decision["artifact"] = _refresh_patched_statblock_evidence(
                    patched_artifact,
                    artifact_patch,
                )
        elif rule.get("artifact_patch") is not None:
            raise ValueError(f"rejected catalog decision for {key} cannot patch its artifact")
        decisions.append(decision)
    unmatched = [index for index in range(len(rules)) if index not in matched]
    if unmatched:
        raise ValueError(
            "catalog manifest decisions matched no candidate: "
            + ", ".join(
                f"{rules[index].get('kind')}:{rules[index].get('name')}" for index in unmatched
            )
        )
    expected = review_spec.get("expected_catalog")
    if expected is not None:
        if not isinstance(expected, list):
            raise ValueError("expected_catalog must be a list")
        expected_keys = Counter(
            (_fold_text(item.get("kind")), _fold_text(item.get("name")))
            for item in expected
            if isinstance(item, dict)
        )
        accepted_keys = Counter(
            _reviewed_candidate_identity(candidate, decision)
            for candidate, decision in zip(candidates, decisions, strict=True)
            if decision["review_status"] == "accepted"
        )
        if expected_keys != accepted_keys:
            raise ValueError(
                "catalog manifest expected_catalog differs from the accepted catalog: "
                f"missing={sorted((expected_keys - accepted_keys).elements())}, "
                f"unexpected={sorted((accepted_keys - expected_keys).elements())}"
            )
    accepted_identities = [
        _reviewed_candidate_identity(candidate, decision)
        for candidate, decision in zip(candidates, decisions, strict=True)
        if decision["review_status"] == "accepted"
    ]
    expected_catalog_sha256 = review_spec.get("expected_catalog_sha256")
    if expected_catalog_sha256 is not None:
        if (
            not isinstance(expected_catalog_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_catalog_sha256) is None
        ):
            raise ValueError("expected_catalog_sha256 must be a lowercase SHA-256 digest")
        actual_catalog_sha256 = hashlib.sha256(
            json.dumps(
                sorted(accepted_identities),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if expected_catalog_sha256 != actual_catalog_sha256:
            raise ValueError(
                "catalog manifest expected_catalog_sha256 differs from the accepted "
                f"catalog: expected={expected_catalog_sha256}, "
                f"actual={actual_catalog_sha256}"
            )
    expected_counts = review_spec.get("expected_counts")
    if expected_counts is not None:
        if not isinstance(expected_counts, dict) or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in expected_counts.values()
        ):
            raise ValueError("expected_counts must map content kinds to nonnegative integers")
        actual_counts = dict(sorted(Counter(kind for kind, _name in accepted_identities).items()))
        normalized_expected = {
            str(kind): count for kind, count in sorted(expected_counts.items()) if count
        }
        if normalized_expected != actual_counts:
            raise ValueError(
                "catalog manifest expected_counts differs from the accepted catalog: "
                f"expected={normalized_expected}, actual={actual_counts}"
            )
    return decisions


def _reviewed_candidate_identity(
    candidate: dict[str, Any],
    decision: dict[str, Any],
) -> tuple[str, str]:
    artifact = dict(decision.get("artifact") or candidate.get("artifact") or {})
    card = dict(artifact.get("card") or {})
    return (
        _fold_text(artifact.get("kind") or candidate.get("kind")),
        _fold_text(card.get("name") or candidate.get("name")),
    )


def _require_expected_actor_names(
    review_spec: dict[str, Any],
    preset_export: dict[str, Any] | None,
) -> None:
    expected = review_spec.get("expected_actor_names")
    expected_sha256 = review_spec.get("expected_actor_names_sha256")
    if expected is None and expected_sha256 is None:
        return
    if expected is not None and (
        not isinstance(expected, list)
        or any(not isinstance(name, str) or not name.strip() for name in expected)
    ):
        raise ValueError("expected_actor_names must be an array of nonempty strings")
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
    ):
        raise ValueError("expected_actor_names_sha256 must be a lowercase SHA-256")
    cards = (
        list(dict(dict(preset_export or {}).get("package") or {}).get("actors") or [])
        if preset_export is not None
        else []
    )
    actual_names = Counter(_fold_text(card.get("name")) for card in cards if isinstance(card, dict))
    if expected is not None:
        expected_names = Counter(_fold_text(name) for name in expected)
        if actual_names != expected_names:
            raise RuntimeError(
                "actor preset names differ from the source-reviewed manifest: "
                f"missing={sorted((expected_names - actual_names).elements())}, "
                f"unexpected={sorted((actual_names - expected_names).elements())}"
            )
    if expected_sha256 is not None:
        canonical_names = json.dumps(
            sorted(actual_names.elements()),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        actual_sha256 = hashlib.sha256(canonical_names.encode("utf-8")).hexdigest()
        if actual_sha256 != expected_sha256:
            raise RuntimeError(
                "actor preset name manifest checksum differs from the "
                f"source-reviewed manifest: expected={expected_sha256}, "
                f"actual={actual_sha256}"
            )


def _require_expected_dependent_actor_names(
    review_spec: dict[str, Any],
    dependent_actor_templates: Any,
) -> None:
    expected = review_spec.get("expected_dependent_actor_names")
    if expected is None:
        return
    if not isinstance(expected, list) or any(
        not isinstance(name, str) or not name.strip() for name in expected
    ):
        raise ValueError("expected_dependent_actor_names must be an array of nonempty strings")
    actual = Counter(
        _fold_text(dict(template).get("name"))
        for template in list(dependent_actor_templates or [])
        if isinstance(template, dict)
    )
    wanted = Counter(_fold_text(name) for name in expected)
    if actual != wanted:
        raise RuntimeError(
            "dependent actor template names differ from the source-reviewed manifest: "
            f"missing={sorted((wanted - actual).elements())}, "
            f"unexpected={sorted((actual - wanted).elements())}"
        )


def _require_expected_actor_cards(
    review_spec: dict[str, Any],
    preset_export: dict[str, Any] | None,
) -> None:
    """Verify source-reviewed actor payload boundaries, not only their names."""

    expected = review_spec.get("expected_actor_cards")
    if expected is None:
        return
    if not isinstance(expected, list) or any(not isinstance(item, dict) for item in expected):
        raise ValueError("expected_actor_cards must be an array of objects")
    cards = list(dict(dict(preset_export or {}).get("package") or {}).get("actors") or [])
    by_name: dict[str, list[dict[str, Any]]] = {}
    for card in cards:
        if not isinstance(card, dict):
            continue
        payload = dict(card)
        by_name.setdefault(_fold_text(payload.get("name")), []).append(payload)
    seen: set[str] = set()
    allowed = {
        "name",
        "source_boundary_sha256",
        "inventory_item_names",
        "activity_names",
        "forbidden_text",
    }
    for index, contract in enumerate(expected):
        unknown = set(contract) - allowed
        if unknown:
            raise ValueError(
                f"expected_actor_cards[{index}] has unsupported fields: {sorted(unknown)}"
            )
        name = str(contract.get("name") or "").strip()
        folded_name = _fold_text(name)
        if not folded_name or folded_name in seen:
            raise ValueError("expected_actor_cards must name unique nonempty actors")
        seen.add(folded_name)
        matches = by_name.get(folded_name, [])
        if len(matches) != 1:
            raise RuntimeError(
                f"expected actor card {name!r} resolved to {len(matches)} exported cards"
            )
        payload = matches[0]
        for field in ("inventory_item_names", "activity_names", "forbidden_text"):
            value = contract.get(field)
            if value is not None and (
                not isinstance(value, list)
                or any(not isinstance(item, str) or not item.strip() for item in value)
            ):
                raise ValueError(
                    f"expected_actor_cards[{index}].{field} must be an array of nonempty strings"
                )
        sheet = dict(payload.get("sheet") or {})
        if "inventory_item_names" in contract:
            inventory = dict(sheet.get("inventory") or {})
            actual_items = Counter(
                _fold_text(dict(item).get("name"))
                for item in inventory.get("items") or []
                if isinstance(item, dict)
            )
            expected_items = Counter(_fold_text(item) for item in contract["inventory_item_names"])
            if actual_items != expected_items:
                raise RuntimeError(
                    f"actor card {name!r} inventory differs from source review: "
                    f"expected={sorted(expected_items.elements())}, "
                    f"actual={sorted(actual_items.elements())}"
                )
        if "activity_names" in contract:
            content = dict(sheet.get("content") or {})
            actual_activities = Counter(
                _fold_text(dict(item).get("name"))
                for item in content.get("activities") or []
                if isinstance(item, dict)
            )
            expected_activities = Counter(_fold_text(item) for item in contract["activity_names"])
            if actual_activities != expected_activities:
                raise RuntimeError(
                    f"actor card {name!r} activities differ from source review: "
                    f"expected={sorted(expected_activities.elements())}, "
                    f"actual={sorted(actual_activities.elements())}"
                )
        expected_source_hash = contract.get("source_boundary_sha256")
        if expected_source_hash is not None:
            if (
                not isinstance(expected_source_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_source_hash) is None
            ):
                raise ValueError(
                    f"expected_actor_cards[{index}].source_boundary_sha256 must be "
                    "a lowercase SHA-256"
                )
            provenance = dict(payload.get("provenance") or {})
            actual_source_hash = str(provenance.get("source_text_hash") or "")
            if actual_source_hash != expected_source_hash:
                raise RuntimeError(
                    f"actor card {name!r} source boundary checksum differs: "
                    f"expected={expected_source_hash}, actual={actual_source_hash}"
                )
            if not list(provenance.get("source_refs") or []):
                raise RuntimeError(
                    f"actor card {name!r} source boundary has no portable source refs"
                )
        serialized = json.dumps(payload, ensure_ascii=False).casefold()
        leaked = [
            item for item in contract.get("forbidden_text") or [] if item.casefold() in serialized
        ]
        if leaked:
            raise RuntimeError(
                f"actor card {name!r} contains source-reviewed forbidden text: {leaked}"
            )


def _matches_includes(
    relative_path: str,
    patterns: list[str],
    *,
    empty: bool = True,
) -> bool:
    """Match user-facing include globs consistently across host filesystems."""

    folded_path = relative_path.casefold()
    return (
        empty
        if not patterns
        else any(fnmatch.fnmatch(folded_path, pattern.casefold()) for pattern in patterns)
    )


def _catalog_review_decision(
    *,
    role: str,
    reviewer: str,
    method: str,
    notes: str,
) -> dict[str, Any]:
    return {
        "role": role,
        "reviewer": reviewer,
        "method": method,
        "checks": {
            "identity": True,
            "classification": True,
            "entry_boundary": True,
            "references": True,
        },
        "notes": notes,
    }


async def _content_roundtrip(
    *,
    source_server: Any,
    source_archive_dir: Path,
    source_campaign_id: str,
    target_server: Any,
    target_campaign_id: str,
    source_id: str,
    source_key: str,
    job_id: str,
    candidates: list[dict[str, Any]],
    relative_path: str,
    edition: str,
    run_id: str,
    id_key: str,
    addon_output_dir: Path | None = None,
    primary_reviewer: str,
    primary_review_method: str,
    critic_reviewer: str,
    critic_review_method: str,
    review_spec: dict[str, Any] | None = None,
    available_dependency_addons: dict[tuple[str, str], dict[str, Any]] | None = None,
    probe_attempt_id: str = "test",
) -> dict[str, Any]:
    """Compile the entire reviewed catalog and round-trip its self-contained addon."""

    dependency_addons = _selected_dependency_addons(
        review_spec or {}, available_dependency_addons or {}
    )
    dependency_rule_components = _dependency_rule_components(dependency_addons)
    await _import_dependency_addons(
        server=source_server,
        campaign_id=source_campaign_id,
        archive_dir=source_archive_dir,
        dependencies=dependency_addons,
        receiver="source",
    )
    manifest_dependencies = [_core_content_dependency(edition)]
    manifest_dependencies.extend(
        {
            "id": str(component["id"]),
            "version": str(component["version"]),
        }
        for component in dependency_rule_components
    )
    manifest_dependencies = list(
        {(item["id"], item["version"]): item for item in manifest_dependencies}.values()
    )
    chunks = await _source_chunks(source_server, source_id)
    if not chunks:
        raise RuntimeError("indexed source has no chunk available for a portable probe")
    pack_id = _content_pack_id(relative_path, run_id=run_id)
    version = "1.0.0"
    title = Path(relative_path).stem
    extracted_candidates = list(candidates)
    if not extracted_candidates:
        catalog_chunks = [
            (index, chunk)
            for index, chunk in enumerate(chunks)
            if str(chunk.get("content") or "").strip()
        ]
        if not catalog_chunks:
            raise RuntimeError("indexed source has no nonempty chunk for its catalog")
        candidates = []
        for index, chunk in catalog_chunks:
            content = " ".join(str(chunk.get("content") or "").split())
            heading = " > ".join(
                str(item).strip() for item in chunk.get("heading_path") or [] if str(item).strip()
            )
            ordinal = int(chunk.get("ordinal", index) or 0)
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
            candidates.append(
                {
                    "kind": "feature",
                    "artifact": {
                        "id": f"{pack_id}.source-chunk-{ordinal:04d}-{digest}",
                        "kind": "feature",
                        "card": {
                            "name": (
                                f"Source section {ordinal + 1}: {heading}"
                                if heading
                                else f"Source section {ordinal + 1}: {title}"
                            )[:240]
                        },
                        "application_state": "catalog_only",
                        # No specialized candidate does not prove descriptive
                        # content. Bind every nonempty source chunk to its own
                        # build-time direct Agent ruling so tables and novel
                        # procedures cannot disappear behind one sample probe.
                        "mechanical_scope": "mechanical",
                        "source_chunk_ids": [chunk["id"]],
                    },
                }
            )
        draft_response = await _call(
            source_server,
            "content_pack",
            {
                "action": "build",
                "payload": {
                    "kind": "source_rule",
                    "source_id": source_id,
                    "manifest": {
                        "id": pack_id,
                        "version": version,
                        "title": title,
                        "namespace": pack_id,
                        "system_id": "dnd5e",
                        "editions": [edition],
                        "dependencies": manifest_dependencies,
                        "conflicts": [],
                        "capabilities": [],
                        "content_kinds": ["feature"],
                    },
                    "artifacts": [item["artifact"] for item in candidates],
                    "provenance": {
                        "distribution": "private",
                        "regression_only": False,
                        "source_key": source_key,
                        "complete_catalog": True,
                        "source_catalog_fallback": ("per_chunk_source_bound_agent_ruling"),
                        "source_catalog_artifact_count": len(candidates),
                        "empty_source_chunk_count": len(chunks) - len(catalog_chunks),
                    },
                },
            },
        )
        draft_response = {"result": {"draft": draft_response["result"]}}
    else:
        primary_decisions = _review_spec_decisions(
            candidates,
            review_spec or {},
            reviewer=primary_reviewer,
            method=primary_review_method,
        )
        primary_reviewed = await _call(
            source_server,
            "rulebook_draft",
            {
                "campaign_id": source_campaign_id,
                "action": "edit",
                "payload": {
                    "operation": "candidates",
                    "job_id": job_id,
                    "decisions": primary_decisions,
                },
                "idempotency_key": f"regression-review-primary-{id_key}",
            },
        )
        if any(
            item["review_status"] not in {"accepted", "rejected"}
            for item in primary_reviewed["result"]["candidates"]
        ):
            raise RuntimeError("Agent editing did not disposition the complete catalog")
        finalized = await _call(
            source_server,
            "rulebook_draft",
            {
                "campaign_id": source_campaign_id,
                "action": "finalize",
                "payload": {
                    "job_id": job_id,
                    "note": (
                        "Agent completed the source-bound catalog editing loop and "
                        "confirmed every include or exclude disposition."
                    ),
                },
                "expected_revision": primary_reviewed["result"]["job"]["revision"],
                "idempotency_key": f"regression-review-finalize-{id_key}",
            },
        )
        if any(
            item["review_status"] not in {"accepted", "rejected"}
            for item in finalized["result"]["candidates"]
        ):
            raise RuntimeError("full candidate catalog was not finalized")
        original_by_id = {str(item.get("id")): item for item in candidates}
        candidates = [
            {
                **dict(original_by_id.get(str(item.get("id"))) or {}),
                **item,
            }
            for item in finalized["result"]["candidates"]
            if item["review_status"] == "accepted"
        ]
        if not candidates:
            raise RuntimeError("catalog review rejected every extracted candidate")
        draft_response = await _call(
            source_server,
            "rulebook_draft",
            {
                "campaign_id": source_campaign_id,
                "action": "finalize",
                "payload": {
                    "job_id": job_id,
                    "manifest": {
                        "id": pack_id,
                        "version": version,
                        "title": title,
                        "namespace": pack_id,
                        "system_id": "dnd5e",
                        "editions": [edition],
                        "dependencies": manifest_dependencies,
                        "conflicts": [],
                        "capabilities": [],
                        "content_kinds": sorted(_kind_counts(candidates)),
                    },
                    "provenance": {
                        "distribution": "private",
                        "regression_only": False,
                        "source_key": source_key,
                        "complete_catalog": True,
                    },
                },
                "idempotency_key": f"regression-compile-all-{id_key}",
            },
        )
    draft = draft_response["result"]["draft"]
    if draft["status"] != "validated":
        raise RuntimeError(f"portable probe draft was not validated: {draft['status']}")
    export_response = await _call(
        source_server,
        "content_pack",
        {
            "action": "export",
            "payload": {
                "kind": "rule",
                "campaign_id": source_campaign_id,
                "pack_id": pack_id,
                "version": version,
                "metadata": {
                    "distribution": "private",
                    "regression_only": False,
                },
                "include_package": True,
            },
        },
    )
    exported = export_response["result"]
    package = exported["package"]
    addon_component_artifacts = [exported["artifact"]["artifact"]]
    preset_export: dict[str, Any] | None = None
    preset_summary: dict[str, Any] = {
        "cards": 0,
        "failures": [],
        "complete": True,
        "deferred": 0,
        "dependent_actor_templates": [],
    }
    if any(candidate.get("kind") == "statblock" for candidate in candidates):
        preset_response = await _call(
            source_server,
            "content_pack",
            {
                "action": "build",
                "payload": {
                    "kind": "preset",
                    "campaign_id": source_campaign_id,
                    "pack_id": pack_id,
                    "version": version,
                    "portable_id": f"{pack_id}.actors",
                    "allow_partial": True,
                    "catalog_review_decisions": [
                        _catalog_review_decision(
                            role="primary",
                            reviewer=primary_reviewer,
                            method=primary_review_method,
                            notes=(
                                "Reviewed the exact source-backed portable actor "
                                "identity, type, statblock boundary, and references."
                            ),
                        ),
                        _catalog_review_decision(
                            role="critic",
                            reviewer=critic_reviewer,
                            method=critic_review_method,
                            notes=(
                                "Independently verified the complete actor payload "
                                "against its immutable source evidence."
                            ),
                        ),
                    ],
                    "metadata": {
                        "distribution": "private",
                        "license": "user-supplied",
                    },
                    "include_package": True,
                },
            },
        )
        preset_summary = dict(preset_response["result"]["summary"])
        if preset_response["result"].get("package") is not None:
            preset_export = preset_response["result"]
            addon_component_artifacts.append(preset_export["artifact"]["artifact"])
        if (
            preset_summary.get("complete") is not True
            or int(preset_summary.get("deferred", 0) or 0) != 0
            or list(preset_summary.get("failures") or [])
        ):
            raise RuntimeError(
                "actor preset export is incomplete: "
                f"deferred={preset_summary.get('deferred', 0)}, "
                f"failures={preset_summary.get('failures', [])}"
            )
    _require_expected_actor_names(review_spec or {}, preset_export)
    _require_expected_dependent_actor_names(
        review_spec or {},
        preset_summary.get("dependent_actor_templates"),
    )
    _require_expected_actor_cards(review_spec or {}, preset_export)
    addon_id = f"{pack_id}.addon"
    classification = _addon_classification(relative_path)
    addon_response = await _call(
        source_server,
        "content_pack",
        {
            "action": "build",
            "payload": {
                "kind": "addon",
                "campaign_id": source_campaign_id,
                "portable_id": addon_id,
                "version": version,
                "manifest": {
                    "id": addon_id,
                    "version": version,
                    "system_id": "dnd5e",
                    "title": title,
                    "editions": [edition],
                    "classification": classification,
                    "content_summary": _kind_counts(candidates),
                    "activation": {
                        "rule_policy": "branch",
                        "preset_policy": "library" if preset_export else "none",
                        "module_policy": "none",
                    },
                },
                "component_artifacts": addon_component_artifacts,
                "metadata": {
                    "title": title,
                    "distribution": "private",
                    "license": "user-supplied",
                    "source_path": relative_path,
                    "complete_source": True,
                },
                "include_package": True,
            },
        },
    )
    addon = addon_response["result"]["package"]
    addon_archive_path = (
        source_archive_dir / addon_response["result"]["artifact"]["artifact"]
    ).resolve()
    addon_validation = dict(addon_response["result"]["summary"]["validation"])
    if addon_validation.get("complete") is not True:
        blockers = [
            blocker
            for dimension in ("source", "catalog", "selection", "runtime")
            for blocker in dict(addon_validation.get(dimension) or {}).get("blockers") or []
        ]
        raise RuntimeError(
            "addon failed source/catalog/selection/runtime validation: "
            + json.dumps(blockers[:20], ensure_ascii=False)
        )
    addon_output_path = None
    if addon_output_dir is not None:
        output_name = (
            f"{ascii_slug(Path(relative_path).stem) or 'rulebook'}-"
            f"{hashlib.sha256(relative_path.encode('utf-8')).hexdigest()[:10]}"
            ".sagasmith-pack"
        )
        addon_output_path = addon_output_dir / output_name
        shutil.copyfile(addon_archive_path, addon_output_path)
    await _import_dependency_addons(
        server=target_server,
        campaign_id=target_campaign_id,
        archive_dir=source_archive_dir,
        dependencies=dependency_addons,
        receiver="target",
    )
    await _set_dependency_addons_enabled(
        server=target_server,
        campaign_id=target_campaign_id,
        dependencies=dependency_addons,
        enabled=True,
    )
    import_response = await _call(
        target_server,
        "content_pack",
        {
            "action": "import",
            "payload": {
                "kind": "addon",
                "campaign_id": target_campaign_id,
                "source_path": str(addon_archive_path),
            },
            "idempotency_key": f"regression-addon-import-{id_key}",
        },
    )
    imported = import_response["result"]
    if imported["installed"] is not True or imported["activated"] is not False:
        raise RuntimeError("addon import did not stop at the installed/inactive boundary")
    if any(item["status"] != "installed" for item in imported["components"]):
        raise RuntimeError("addon global components were not installed")
    profile_response = await _call(
        target_server,
        "campaign_rules",
        {"campaign_id": target_campaign_id, "action": "get_profile"},
    )
    activated_response = await _call(
        target_server,
        "content_pack",
        {
            "action": "activate",
            "payload": {
                "kind": "addon",
                "campaign_id": target_campaign_id,
                "addon_id": addon_id,
                "version": version,
            },
            "expected_revision": profile_response["result"]["campaign_revision"],
            "idempotency_key": (
                f"regression-addon-enable-{id_key}-"
                f"r{profile_response['result']['campaign_revision']}"
            ),
        },
    )
    if activated_response["result"]["activation"]["enabled"] is not True:
        raise RuntimeError("addon did not activate its exact branch lock")
    runtime_probe_key = f"{id_key}-attempt-{probe_attempt_id}"
    content_runtime_probes = await _run_content_runtime_probes(
        server=target_server,
        campaign_id=target_campaign_id,
        edition=edition,
        package=package,
        probes=dict(review_spec or {}).get("runtime_probes") or [],
        id_key=runtime_probe_key,
    )
    dependent_actor_runtime_probes: list[dict[str, Any]] = []
    for template_index, template in enumerate(
        preset_summary.get("dependent_actor_templates") or []
    ):
        requirement = dict(template.get("requirement") or {})
        solution = dict(requirement.get("solution") or {})
        artifact_id = str(template.get("artifact_id") or "")
        if not artifact_id or requirement.get("runtime_ready") is not True:
            raise RuntimeError("dependent actor template is not runtime-ready")
        numeric_parameters = set(solution.get("numeric_parameters") or [])
        source_class_names = [
            str(value).strip()
            for value in solution.get("owner_class_names") or []
            if str(value).strip()
        ]
        owner_class_name = str(requirement.get("owner_class_name") or "").strip()
        if not owner_class_name and source_class_names:
            if len(source_class_names) != 1:
                raise RuntimeError("dependent actor template has ambiguous owner class")
            owner_class_name = source_class_names[0].title()
        owner_class_binding = str(requirement.get("owner_class_binding") or "")
        if "owner_class_level" in numeric_parameters and owner_class_binding not in {
            "source_formula",
            "reviewed_context",
            "owner_selection",
        }:
            raise RuntimeError(
                f"dependent actor template {artifact_id!r} lacks an explicit "
                "owner_class_binding policy"
            )
        # A source that says "this class" can intentionally remain reusable.
        # The runtime then requires an explicit class selection for multiclass
        # owners and can infer the only class for this single-class probe.
        probe_owner_class_name = owner_class_name or "Template Owner"
        owner_sheet = default_character_sheet()
        owner_sheet["edition"] = edition
        owner_sheet["progression"]["level"] = 10
        owner_sheet["progression"]["classes"] = [
            {
                "name": probe_owner_class_name,
                "level": 10,
                "subclass": "",
                "hit_die": 8,
            }
        ]
        for ability in owner_sheet["abilities"].values():
            ability["score"] = 18
        owner_sheet["spellcasting"]["ability"] = "intelligence"
        _complete_probe_hit_points(
            owner_sheet,
            source=f"addon dependent actor owner {artifact_id}",
        )
        probe_key = f"{runtime_probe_key}-{template_index}"
        owner_response = await _call(
            target_server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": target_campaign_id,
                    "name": f"Addon template owner {probe_key}",
                    "sheet": owner_sheet,
                },
                "idempotency_key": f"regression-addon-owner-{probe_key}",
            },
        )
        instantiate_arguments = {
            "campaign_id": target_campaign_id,
            "artifact_id": artifact_id,
            "owner_character_id": owner_response["result"]["id"],
            "idempotency_key": f"regression-addon-actor-{probe_key}",
            **({"casting_slot_level": 9} if "casting_slot_level" in numeric_parameters else {}),
            **(
                {"template_variant": str(solution["variant_options"][0])}
                if solution.get("variant_options")
                else {}
            ),
        }
        actor_response = await _call(
            target_server,
            "addon_actor_instantiate",
            instantiate_arguments,
        )
        actor_replay = await _call(
            target_server,
            "addon_actor_instantiate",
            instantiate_arguments,
        )
        actor_result = actor_response
        expected_weapon_names = {
            str(value).strip()
            for value in template.get("expected_weapon_names") or []
            if str(value).strip()
        }
        materialized_weapon_names = {
            str(item.get("name") or "").strip()
            for item in actor_result["character"]["sheet"]["inventory"]["items"]
            if item.get("kind") == "weapon" and str(item.get("name") or "").strip()
        }
        if (
            actor_result["character"]["id"] != actor_replay["character"]["id"]
            or actor_result.get("actor_knowledge_imported") is not False
            or int(actor_result["character"]["sheet"]["combat"]["hp"]["max"]) < 1
            or not expected_weapon_names.issubset(materialized_weapon_names)
        ):
            raise RuntimeError("dependent actor template runtime probe failed")
        dependent_actor_runtime_probes.append(
            {
                "artifact_id": artifact_id,
                "actor_id": actor_result["character"]["id"],
                "owner_id": owner_response["result"]["id"],
                "content_receipt": actor_result["content_receipt"],
                "expected_weapon_names": sorted(expected_weapon_names),
                "materialized_weapon_names": sorted(materialized_weapon_names),
                "idempotent": True,
                "actor_knowledge_imported": False,
            }
        )
    campaign_response = await _call(
        target_server,
        "campaign_query",
        {"view": "get", "payload": {"campaign_id": target_campaign_id}},
    )
    disabled_response = await _call(
        target_server,
        "content_pack",
        {
            "action": "deactivate",
            "payload": {
                "kind": "addon",
                "campaign_id": target_campaign_id,
                "addon_id": addon_id,
                "version": version,
            },
            "expected_revision": campaign_response["result"]["revision"],
            "idempotency_key": (
                f"regression-addon-disable-{id_key}-r{campaign_response['result']['revision']}"
            ),
        },
    )
    if disabled_response["result"]["activation"]["enabled"] is not False:
        raise RuntimeError("addon did not release its branch lock")
    await _set_dependency_addons_enabled(
        server=target_server,
        campaign_id=target_campaign_id,
        dependencies=dependency_addons,
        enabled=False,
    )
    reexport_response = await _call(
        target_server,
        "content_pack",
        {
            "action": "export",
            "payload": {
                "kind": "rule",
                "campaign_id": target_campaign_id,
                "pack_id": pack_id,
                "version": version,
                "metadata": {
                    "distribution": "private",
                    "regression_only": False,
                },
                "include_package": True,
            },
        },
    )
    reexported = reexport_response["result"]["package"]
    if reexported != package:
        raise RuntimeError("portable package changed after cross-instance re-export")
    addon_detail = await _call(
        target_server,
        "content_pack",
        {
            "action": "get",
            "payload": {
                "kind": "addon",
                "campaign_id": target_campaign_id,
                "addon_id": addon_id,
                "version": version,
                "include_package": True,
            },
        },
    )
    if addon_detail["result"]["package"] != addon:
        raise RuntimeError("portable addon changed after cross-instance import")
    imported_sources = await _call(
        target_server,
        "content_pack",
        {"action": "list", "payload": {"kind": "source", "edition": edition}},
    )
    imported_source_ids = [
        str(item["id"])
        for item in imported_sources["result"]
        if item.get("source_key") == source_key
    ]
    if not imported_source_ids or source_id in imported_source_ids:
        raise RuntimeError("portable addon did not materialize fresh source identities")
    return {
        "pack_id": pack_id,
        "version": version,
        "package_checksum": package["checksum"],
        "definition_checksum": package["content"]["rule_definitions"][0]["definition_checksum"],
        "package_artifact": exported["artifact"],
        "addon_id": addon_id,
        "addon_checksum": addon["checksum"],
        "validation": addon_validation,
        "addon_artifact": addon_response["result"]["artifact"],
        "addon_output": str(addon_output_path) if addon_output_path else None,
        "catalog_artifacts": len(candidates),
        "actor_presets": preset_summary["cards"],
        "deferred_actor_presets": preset_summary.get("deferred", 0),
        "dependent_actor_templates": preset_summary.get("dependent_actor_templates", []),
        "dependent_actor_runtime_probes": dependent_actor_runtime_probes,
        "content_runtime_probes": content_runtime_probes,
        "dependency_addons": [
            {
                "id": str(item["id"]),
                "version": str(item["version"]),
                "checksum": str(item["checksum"]),
            }
            for item in dependency_addons
        ],
        "dependency_rule_components": [
            {
                "id": str(item["id"]),
                "version": str(item["version"]),
                "checksum": str(item["checksum"]),
            }
            for item in dependency_rule_components
        ],
        "preset_failures": preset_summary.get("failures", []),
        "target_source_ids": imported_source_ids,
        "fresh_source_ids": True,
        "_generated_addon": {**addon, "_local_archive_path": str(addon_archive_path)},
        "draft_status": "validated",
        "installed": True,
        "activated": True,
        "deactivated": True,
        "addon_reexport_identical": True,
        "reexport_identical": True,
    }


def _content_pack_id(relative_path: str, *, run_id: str) -> str:
    digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:12]
    slug = ascii_slug(Path(relative_path).stem)[:80] or "rulebook"
    return f"dnd5e.addon.rulebook.{slug}.{digest}"


def _publication_metadata(relative_path: str) -> tuple[str, str]:
    """Return a stable publication identity and its standard-rule authority."""

    folded = relative_path.casefold().replace("’", "'")
    publications = (
        ("player's handbook", "phb2014", "core"),
        ("dungeon master's guide", "dmg2014", "core"),
        ("monster manual", "mm2014", "core"),
        ("eberron - rising from the last war", "erlw2014", "supplement"),
        ("elemental evil player's companion", "eepc2014", "supplement"),
        ("guildmasters' guide to ravnica", "ggr2014", "supplement"),
        ("mordenkainen's tome of foes", "mtof2014", "supplement"),
        ("sword coast adventurer's guide", "scag2014", "supplement"),
        ("tasha's cauldron of everything", "tcoe2014", "supplement"),
        ("the tortle package", "tortle2014", "supplement"),
        ("volo's guide to monsters", "vgm2014", "supplement"),
        ("wayfinders guide to eberron", "wgte2014", "supplement"),
        ("xanathar's guide to everything", "xgte2014", "supplement"),
    )
    for needle, publication_id, authority in publications:
        if needle in folded:
            return publication_id, authority
    slug = ascii_slug(Path(relative_path).stem) or "user-source"
    return f"user-{slug[:100]}", "supplement"


def _addon_classification(relative_path: str) -> str:
    folded = relative_path.casefold()
    if any(
        title in folded
        for title in (
            "player's handbook",
            "dungeon master's guide",
            "monster manual",
        )
    ):
        return "official_core"
    if "blood hunter" in folded or "nlrme" in folded:
        return "third_party"
    if "school of geometry" in folded:
        return "homebrew"
    if "\\uo\\" in f"\\{folded}" or folded.startswith("uo\\"):
        return "playtest"
    if "wayfinder's guide" in folded or "wayfinders guide" in folded:
        return "official_legacy"
    return "official_supplement"


def _run_token(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]


def main() -> int:
    args = _arguments()
    report = asyncio.run(_run(args))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    # PowerShell commonly gives redirected Python processes a legacy code page.
    # The report is Unicode JSON, so write bytes explicitly instead of losing
    # valid source text (for example non-breaking spaces) at the final print.
    stdout_payload = rendered
    if args.output:
        stdout_payload = json.dumps(
            {
                "passed": report["passed"],
                "document_count": report["document_count"],
                "completed_document_count": len(report["documents"]),
                "error_count": len(report["errors"]),
                "seconds": report["seconds"],
                "output": str(args.output.expanduser().resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    sys.stdout.buffer.write((stdout_payload + "\n").encode("utf-8"))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
