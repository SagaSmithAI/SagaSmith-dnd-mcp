"""Run the public staged rule-import workflow against a real document corpus."""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from time import perf_counter
from typing import Any

from sagasmith_core.text import ascii_slug
from sagasmith_dnd.editions import SUPPORTED_DND_EDITIONS

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


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
    parser.add_argument("--ocr-scale", type=float, default=2.0)
    parser.add_argument(
        "--ocr-model",
        choices=("small", "medium"),
        default="small",
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
    parser.add_argument("--fail-on-warning", action="store_true")
    parser.add_argument(
        "--portable-roundtrip",
        action="store_true",
        help=(
            "Compile each complete private source catalog with build-time semantic "
            "resolution, export it, import it into an isolated MCP home, and require "
            "an identical re-export"
        ),
    )
    parser.add_argument(
        "--portable-target-home",
        type=Path,
        help=(
            "Isolated receiver home for --portable-roundtrip; defaults to a sibling "
            "of --home"
        ),
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
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _key(relative_path: str, *, run_id: str = "default") -> str:
    slug = ascii_slug(Path(relative_path).stem)
    digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:10]
    return f"user.rulebook.{slug[:120] or 'rulebook'}.{digest}"


async def _call(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    _, result = await server.call_tool(name, arguments)
    return result


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.expanduser().resolve()
    home = args.home.expanduser().resolve()
    document_cache = (
        args.document_cache.expanduser().resolve() if args.document_cache else None
    )
    catalog_manifest = _load_catalog_manifest(args.catalog_manifest)
    addon_output_dir = (
        args.addon_output_dir.expanduser().resolve()
        if args.addon_output_dir
        else None
    )
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
        auto_seed_rules=False,
        rule_import_roots=(root,),
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
        "rule_import",
        {"campaign_id": campaign["id"], "action": "discover"},
    )
    discovered_documents = discovery["result"]["documents"]
    documents = [
        document
        for document in discovered_documents
        if _matches_includes(str(document["relative_path"]), args.include)
        and not _matches_includes(str(document["relative_path"]), args.exclude, empty=False)
    ]
    target_server: Any | None = None
    target_campaign: dict[str, Any] | None = None
    target_home: Path | None = None
    if args.portable_roundtrip:
        target_home = (
            args.portable_target_home.expanduser().resolve()
            if args.portable_target_home
            else home.with_name(f"{home.name}-portable-target")
        )
        if target_home == home:
            raise ValueError("portable target home must differ from the source MCP home")
        target_config = McpConfig(
            home=target_home,
            database_url=None,
            chroma_url=None,
            chroma_path_override=None,
            dnd_skills_dir=config.dnd_skills_dir,
            modulegen_skills_dir=config.modulegen_skills_dir,
            auto_seed_rules=False,
            rule_import_roots=(),
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
                "idempotency_key": (
                    f"rulebook-portable-target-campaign-{_run_token(args.run_id)}"
                ),
            },
        )
    report: dict[str, Any] = {
        "root": str(root),
        "home": str(home),
        "document_cache": str(document_cache) if document_cache else None,
        "edition": args.edition,
        "ocr_model": args.ocr_model,
        "catalog_review": {
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
        "document_count": len(documents),
        "discovered_document_count": len(discovered_documents),
        "include": list(args.include),
        "exclude": list(args.exclude),
        "documents": [],
        "errors": [],
        "portable_roundtrip": args.portable_roundtrip,
        "portable_target_home": str(target_home) if target_home else None,
        "addon_output_dir": str(addon_output_dir) if addon_output_dir else None,
    }
    release_components: list[dict[str, Any]] = []
    started = perf_counter()
    for index, document in enumerate(documents, start=1):
        relative_path = str(document["relative_path"])
        source_key = _key(relative_path, run_id=args.run_id)
        id_key = hashlib.sha256(
            f"{relative_path}\0{args.run_id}".encode("utf-8")
        ).hexdigest()[:16]
        item_started = perf_counter()
        print(f"[{index}/{len(documents)}] {relative_path}", file=sys.stderr, flush=True)
        try:
            publication_id, authority = _publication_metadata(relative_path)
            staged = await _call(
                server,
                "rule_import",
                {
                    "campaign_id": campaign["id"],
                    "action": "stage",
                    "payload": {
                        "source_path": document["path"],
                        "source_key": source_key,
                        "title": Path(relative_path).stem,
                        "edition": args.edition,
                        "locale": args.locale,
                        "publication_id": publication_id,
                        "authority": authority,
                    },
                    "idempotency_key": f"regression-stage-{id_key}",
                },
            )
            job_id = staged["result"]["job"]["id"]
            inspected = await _call(
                server,
                "rule_import",
                {
                    "campaign_id": campaign["id"],
                    "action": "inspect",
                    "payload": {"job_id": job_id},
                    "idempotency_key": f"regression-inspect-{id_key}",
                },
            )
            inspection = inspected["result"]["inspection"]
            warnings = list(inspection.get("warnings") or [])
            if warnings and args.fail_on_warning:
                raise RuntimeError("; ".join(warnings))
            ingested = await _call(
                server,
                "rule_import",
                {
                    "campaign_id": campaign["id"],
                    "action": "ingest",
                    "payload": {
                        "job_id": job_id,
                        "acknowledge_warnings": bool(warnings),
                    },
                    "idempotency_key": f"regression-ingest-{id_key}",
                },
            )
            source_id = ingested["result"]["source"]["id"]
            statblock_recovery: dict[str, Any] | None = None
            if target_server is not None and args.edition == "2014":
                recovery_response = await _call(
                    server,
                    "rule_import",
                    {
                        "campaign_id": campaign["id"],
                        "action": "recover_statblocks",
                        "payload": {"job_id": job_id},
                        "idempotency_key": f"regression-recover-catalog-{id_key}",
                    },
                )
                statblock_recovery = recovery_response["result"]
            extracted = await _call(
                server,
                "rule_import",
                {
                    "campaign_id": campaign["id"],
                    "action": "extract_candidates",
                    "payload": {"job_id": job_id},
                    "idempotency_key": f"regression-extract-{id_key}",
                },
            )
            candidates = extracted["result"]["candidates"]
            inventory = extracted["result"]["inventory"]
            document_review = _catalog_document_review(
                catalog_manifest,
                relative_path,
            )
            catalog_augmentation: dict[str, Any] | None = None
            if document_review.get("additions"):
                source_chunks = await _source_chunks(server, source_id)
                additions = _resolve_catalog_additions(
                    document_review["additions"],
                    source_chunks,
                    relative_path=relative_path,
                )
                augmented = await _call(
                    server,
                    "rule_import",
                    {
                        "campaign_id": campaign["id"],
                        "action": "augment_catalog",
                        "payload": {
                            "job_id": job_id,
                            "rationale": str(
                                document_review.get("rationale")
                                or "Agent reviewed the complete indexed source catalog."
                            ),
                            "additions": additions,
                        },
                        "expected_revision": extracted["result"]["job"]["revision"],
                        "idempotency_key": f"regression-augment-{id_key}",
                    },
                )
                candidates = augmented["result"]["candidates"]
                catalog_augmentation = {
                    "added_candidate_ids": augmented["result"][
                        "added_candidate_ids"
                    ],
                    "added": len(additions),
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
                portable = await _portable_roundtrip(
                    source_server=server,
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
                    id_key=id_key,
                    addon_output_dir=addon_output_dir,
                    primary_reviewer=str(args.primary_reviewer),
                    primary_review_method=str(args.primary_review_method),
                    critic_reviewer=str(args.critic_reviewer),
                    critic_review_method=str(args.critic_review_method),
                    review_spec=document_review,
                )
                release_components.append(
                    {
                        "kind": "rule_pack",
                        "id": portable["pack_id"],
                        "version": portable["version"],
                        "checksum": portable["package_checksum"],
                        "optional": False,
                    }
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
                    "candidate_catalog": [
                        {
                            "id": item["id"],
                            "kind": item["kind"],
                            "name": item["name"],
                            "page_start": item.get("page_start"),
                            "page_end": item.get("page_end"),
                            "execution_state": item.get("execution_state"),
                        }
                        for item in candidates
                    ],
                    "content_inventory": {
                        key: value
                        for key, value in inventory.items()
                        if key != "ledger"
                    },
                    "statblock_recovery": statblock_recovery,
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
            report["errors"].append(
                {"relative_path": relative_path, "error": message}
            )
            print(
                f"[FAIL {index}/{len(documents)}] {relative_path}: {message}",
                file=sys.stderr,
                flush=True,
            )
    if (
        target_server is not None
        and target_campaign is not None
        and release_components
        and len(release_components) == len(documents)
    ):
        try:
            release = await _portable_release_check(
                source_server=server,
                source_campaign_id=str(campaign["id"]),
                target_server=target_server,
                target_campaign_id=str(target_campaign["id"]),
                components=release_components,
                run_id=args.run_id,
            )
            report["release"] = release
        except Exception as error:
            report["errors"].append(
                {"relative_path": "<release_manifest>", "error": f"{type(error).__name__}: {error}"}
            )
    report["seconds"] = round(perf_counter() - started, 3)
    report["passed"] = not report["errors"] and len(report["documents"]) == len(documents)
    return report


def _kind_counts(candidates: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for candidate in candidates:
        kind = str(candidate.get("kind") or "unknown")
        result[kind] = result.get(kind, 0) + 1
    return dict(sorted(result.items()))


def _load_catalog_manifest(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"version": 1, "documents": {}}
    resolved = path.expanduser().resolve()
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("catalog manifest must be a version 1 JSON object")
    documents = payload.get("documents")
    if not isinstance(documents, dict):
        raise ValueError("catalog manifest documents must be an object")
    return payload


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
        "decisions",
        "default_status",
        "expected_catalog",
    }
    if unknown:
        raise ValueError(
            f"catalog manifest entry for {relative_path} has unsupported fields: "
            f"{sorted(unknown)}"
        )
    return review


async def _source_chunks(server: Any, source_id: str) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    while True:
        response = await _call(
            server,
            "rule_pack_query",
            {
                "view": "source_chunks",
                "payload": {
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
    if "heading_contains" in selector and _fold_text(
        selector["heading_contains"]
    ) not in _fold_text(heading):
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
        }
        if unknown:
            raise ValueError(
                f"catalog addition {index} for {relative_path} has unsupported fields: "
                f"{sorted(unknown)}"
            )
        selectors = addition.get("source_selectors")
        if not isinstance(selectors, list) or not selectors:
            raise ValueError(
                f"catalog addition {index} for {relative_path} needs source_selectors"
            )
        chunk_ids: list[str] = []
        for selector_index, selector in enumerate(selectors):
            if not isinstance(selector, dict):
                raise ValueError(
                    f"source selector {selector_index} for {relative_path} must be an object"
                )
            matches = [
                chunk for chunk in chunks if _source_selector_matches(chunk, selector)
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"source selector {selector_index} for {relative_path} matched "
                    f"{len(matches)} chunks; expected exactly one"
                )
            chunk_ids.append(str(matches[0]["id"]))
        resolved.append(
            {
                key: addition[key]
                for key in ("kind", "name", "card", "note")
                if key in addition
            }
            | {"source_chunk_ids": list(dict.fromkeys(chunk_ids))}
        )
    return resolved


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(base))
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = json.loads(json.dumps(value))
    return result


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
            "source_heading_exact",
            "source_heading_contains",
            "status",
            "artifact_patch",
            "note",
        }
        if unknown:
            raise ValueError(
                f"catalog decision {index} has unsupported fields: {sorted(unknown)}"
            )
        key = (_fold_text(rule.get("kind")), _fold_text(rule.get("name")))
        if not all(key):
            raise ValueError(f"catalog decision {index} has an invalid identity")
        rules.append(rule)
    default_status = str(review_spec.get("default_status") or "accepted")
    if default_status not in {"accepted", "rejected"}:
        raise ValueError("catalog manifest default_status must be accepted or rejected")
    decisions: list[dict[str, Any]] = []
    matched: set[int] = set()
    for candidate in candidates:
        key = (_fold_text(candidate.get("kind")), _fold_text(candidate.get("name")))
        heading_path = [
            _fold_text(value) for value in candidate.get("source_heading_path") or []
        ]
        matching_rules: list[tuple[int, dict[str, Any]]] = []
        for rule_index, rule in enumerate(rules):
            if key != (_fold_text(rule.get("kind")), _fold_text(rule.get("name"))):
                continue
            exact = rule.get("source_heading_exact")
            if exact is not None:
                expected_path = (
                    [_fold_text(value) for value in exact]
                    if isinstance(exact, list)
                    else [_fold_text(exact)]
                )
                if expected_path != heading_path and expected_path != heading_path[-1:]:
                    continue
            contains = rule.get("source_heading_contains")
            if contains is not None and _fold_text(contains) not in " > ".join(
                heading_path
            ):
                continue
            matching_rules.append((rule_index, rule))
        if len(matching_rules) > 1:
            raise ValueError(f"multiple catalog decisions match candidate {key}")
        rule = matching_rules[0][1] if matching_rules else {}
        if matching_rules:
            matched.add(matching_rules[0][0])
        status = str(rule.get("status") or default_status)
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
                decision["artifact"] = _deep_merge(
                    dict(candidate.get("artifact") or {}),
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
                f"{rules[index].get('kind')}:{rules[index].get('name')}"
                for index in unmatched
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
            (_fold_text(candidate.get("kind")), _fold_text(candidate.get("name")))
            for candidate, decision in zip(candidates, decisions, strict=True)
            if decision["review_status"] == "accepted"
        )
        if expected_keys != accepted_keys:
            raise ValueError(
                "catalog manifest expected_catalog differs from the accepted catalog: "
                f"missing={sorted((expected_keys - accepted_keys).elements())}, "
                f"unexpected={sorted((accepted_keys - expected_keys).elements())}"
            )
    return decisions


def _matches_includes(
    relative_path: str,
    patterns: list[str],
    *,
    empty: bool = True,
) -> bool:
    """Match user-facing include globs consistently across host filesystems."""

    folded_path = relative_path.casefold()
    return empty if not patterns else any(
        fnmatch.fnmatch(folded_path, pattern.casefold()) for pattern in patterns
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


async def _portable_roundtrip(
    *,
    source_server: Any,
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
) -> dict[str, Any]:
    """Compile the entire reviewed catalog and round-trip its self-contained addon."""

    chunks = await _source_chunks(source_server, source_id)
    if not chunks:
        raise RuntimeError("indexed source has no chunk available for a portable probe")
    pack_id = _portable_pack_id(relative_path, run_id=run_id)
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
                str(item).strip()
                for item in chunk.get("heading_path") or []
                if str(item).strip()
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
            "rule_pack_compile",
            {
                "action": "from_source",
                "payload": {
                    "source_id": source_id,
                    "manifest": {
                        "id": pack_id,
                        "version": version,
                        "title": title,
                        "namespace": pack_id,
                        "system_id": "dnd5e",
                        "editions": [edition],
                        "dependencies": [],
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
                        "source_catalog_fallback": (
                            "per_chunk_source_bound_agent_ruling"
                        ),
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
            "rule_import",
            {
                "campaign_id": source_campaign_id,
                "action": "review",
                "payload": {
                    "job_id": job_id,
                    "decisions": primary_decisions,
                },
                "idempotency_key": f"regression-review-primary-{id_key}",
            },
        )
        if any(
            item["review_status"] not in {"needs_revision", "rejected"}
            for item in primary_reviewed["result"]["candidates"]
        ):
            raise RuntimeError("primary catalog review did not reach the critic gate")
        reviewable = [
            item
            for item in primary_reviewed["result"]["candidates"]
            if item["review_status"] == "needs_revision"
        ]
        reviewed = await _call(
            source_server,
            "rule_import",
            {
                "campaign_id": source_campaign_id,
                "action": "review",
                "payload": {
                    "job_id": job_id,
                    "decisions": [
                        {
                            "id": candidate["id"],
                            "review_status": "accepted",
                            "catalog_review_decision": _catalog_review_decision(
                                role="critic",
                                reviewer=critic_reviewer,
                                method=critic_review_method,
                                notes=(
                                    "Independently verified immutable reviewed content, "
                                    "selection contract, and exact source citations."
                                ),
                            ),
                        }
                        for candidate in reviewable
                    ],
                },
                "idempotency_key": f"regression-review-critic-{id_key}",
            },
        )
        if any(
            item["review_status"] not in {"accepted", "rejected"}
            for item in reviewed["result"]["candidates"]
        ):
            raise RuntimeError("full candidate catalog was not independently approved")
        original_by_id = {str(item.get("id")): item for item in candidates}
        candidates = [
            {
                **dict(original_by_id.get(str(item.get("id"))) or {}),
                **item,
            }
            for item in reviewed["result"]["candidates"]
            if item["review_status"] == "accepted"
        ]
        if not candidates:
            raise RuntimeError("catalog review rejected every extracted candidate")
        draft_response = await _call(
            source_server,
            "rule_import",
            {
                "campaign_id": source_campaign_id,
                "action": "compile",
                "payload": {
                    "job_id": job_id,
                    "manifest": {
                        "id": pack_id,
                        "version": version,
                        "title": title,
                        "namespace": pack_id,
                        "system_id": "dnd5e",
                        "editions": [edition],
                        "dependencies": [],
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
        "rule_pack_query",
        {
            "view": "package",
            "payload": {
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
    addon_components = [package]
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
            "rule_pack_query",
            {
                "view": "preset_package",
                "payload": {
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
            addon_components.append(preset_export["package"])
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
    addon_id = f"{pack_id}.addon"
    classification = _addon_classification(relative_path)
    addon_response = await _call(
        source_server,
        "rule_pack_query",
        {
            "view": "addon_package",
            "payload": {
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
                "components": addon_components,
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
    resolution_readiness = dict(
        addon["payload"]["manifest"]["resolution_readiness"]
    )
    if (
        resolution_readiness.get("complete") is not True
        or resolution_readiness.get("first_use_compilation_required") is not False
        or list(resolution_readiness.get("unresolved") or [])
    ):
        raise RuntimeError("addon escaped with incomplete build-time resolution")
    addon_readiness = dict(addon["payload"]["manifest"].get("readiness") or {})
    if (
        addon["payload"]["manifest"].get("readiness_policy")
        != "build_time_complete"
        or addon_readiness.get("complete") is not True
    ):
        blockers = [
            *list(dict(addon_readiness.get("source") or {}).get("blockers") or []),
            *list(dict(addon_readiness.get("catalog") or {}).get("blockers") or []),
            *list(dict(addon_readiness.get("selection") or {}).get("blockers") or []),
            *list(dict(addon_readiness.get("runtime") or {}).get("blockers") or []),
        ]
        raise RuntimeError(
            "addon failed source/catalog/selection/runtime readiness: "
            + json.dumps(blockers[:20], ensure_ascii=False)
        )
    addon_output_path = None
    if addon_output_dir is not None:
        output_name = (
            f"{ascii_slug(Path(relative_path).stem) or 'rulebook'}-"
            f"{hashlib.sha256(relative_path.encode('utf-8')).hexdigest()[:10]}"
            ".addon.sagasmith.json"
        )
        addon_output_path = addon_output_dir / output_name
        addon_output_path.write_text(
            json.dumps(addon, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    import_response = await _call(
        target_server,
        "rule_import",
        {
            "campaign_id": target_campaign_id,
            "action": "import_addon",
            "payload": {"addon": addon},
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
        "campaign_rules",
        {
            "campaign_id": target_campaign_id,
            "action": "set_addon",
            "payload": {"addon_id": addon_id, "version": version},
            "expected_revision": profile_response["result"]["campaign_revision"],
            "idempotency_key": (
                f"regression-addon-enable-{id_key}-"
                f"r{profile_response['result']['campaign_revision']}"
            ),
        },
    )
    if activated_response["result"]["activation"]["enabled"] is not True:
        raise RuntimeError("addon did not activate its exact branch lock")
    campaign_response = await _call(
        target_server,
        "campaign_query",
        {"view": "get", "payload": {"campaign_id": target_campaign_id}},
    )
    disabled_response = await _call(
        target_server,
        "campaign_rules",
        {
            "campaign_id": target_campaign_id,
            "action": "set_addon",
            "payload": {
                "addon_id": addon_id,
                "version": version,
                "enabled": False,
            },
            "expected_revision": campaign_response["result"]["revision"],
            "idempotency_key": (
                f"regression-addon-disable-{id_key}-"
                f"r{campaign_response['result']['revision']}"
            ),
        },
    )
    if disabled_response["result"]["activation"]["enabled"] is not False:
        raise RuntimeError("addon did not release its branch lock")
    reexport_response = await _call(
        target_server,
        "rule_pack_query",
        {
            "view": "package",
            "payload": {
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
        "rule_pack_query",
        {
            "view": "addon",
            "payload": {
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
        "rule_pack_query",
        {"view": "sources", "payload": {"edition": edition}},
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
        "definition_checksum": package["metadata"]["definition_checksum"],
        "package_artifact": exported["artifact"],
        "addon_id": addon_id,
        "addon_checksum": addon["checksum"],
        "resolution_readiness": resolution_readiness,
        "readiness": addon_readiness,
        "addon_artifact": addon_response["result"]["artifact"],
        "addon_output": str(addon_output_path) if addon_output_path else None,
        "catalog_artifacts": len(candidates),
        "actor_presets": preset_summary["cards"],
        "deferred_actor_presets": preset_summary.get("deferred", 0),
        "dependent_actor_templates": preset_summary.get(
            "dependent_actor_templates", []
        ),
        "preset_failures": preset_summary.get("failures", []),
        "target_source_ids": imported_source_ids,
        "fresh_source_ids": True,
        "draft_status": "validated",
        "installed": True,
        "activated": True,
        "deactivated": True,
        "addon_reexport_identical": True,
        "reexport_identical": True,
    }


async def _portable_release_check(
    *,
    source_server: Any,
    source_campaign_id: str,
    target_server: Any,
    target_campaign_id: str,
    components: list[dict[str, Any]],
    run_id: str,
) -> dict[str, Any]:
    release_id = f"dnd5e.regression.books-release.{_run_token(run_id)}"
    release_response = await _call(
        source_server,
        "rule_pack_query",
        {
            "view": "release",
            "payload": {
                "campaign_id": source_campaign_id,
                "portable_id": release_id,
                "version": "1.0.0",
                "components": components,
                "metadata": {
                    "title": "Private rulebook regression release",
                    "distribution": "private",
                    "regression_only": True,
                },
                "include_manifest": True,
            },
        },
    )
    released = release_response["result"]
    inspect_response = await _call(
        target_server,
        "rule_import",
        {
            "campaign_id": target_campaign_id,
            "action": "inspect_release",
            "payload": {"release_manifest": released["release_manifest"]},
        },
    )
    inspected = inspect_response["result"]
    if inspected["authority"] != "manifest_only":
        raise RuntimeError("release manifest unexpectedly gained runtime authority")
    if inspected["auto_install"] is not False or inspected["auto_activate"] is not False:
        raise RuntimeError("release inspection crossed an install or activation boundary")
    if any(item["local_status"] != "installed" for item in inspected["components"]):
        raise RuntimeError("release receiver does not contain every installed rule package")
    if any(
        item["portable_checksum_status"] != "match" for item in inspected["components"]
    ):
        raise RuntimeError("release component envelope checksum mismatch")
    return {
        "id": release_id,
        "version": "1.0.0",
        "checksum": inspected["release"]["checksum"],
        "artifact": released["artifact"],
        "component_count": len(inspected["components"]),
        "authority": inspected["authority"],
        "auto_install": inspected["auto_install"],
        "auto_activate": inspected["auto_activate"],
        "all_components_installed": True,
        "all_envelope_checksums_match": True,
    }


def _portable_pack_id(relative_path: str, *, run_id: str) -> str:
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
    sys.stdout.buffer.write((rendered + "\n").encode("utf-8"))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
