"""Discover the complete D&D module corpus and emit a coverage matrix.

Discovery is additive: current Pack archives, catalog indexes, declared corpus
assets, configured raw source roots, and modules visible through a real stdio
MCP session are unioned.  Source-specific classifications live in the audit
fixture; an unknown candidate is reported as pending instead of disappearing.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import stdio_client
from sagasmith_core import DOCUMENT_SOURCE_SUFFIXES

from scripts.regression_modules import PRINCIPAL_ID, _facade_value, _server_parameters
from scripts.regression_runtime import decode_mcp_result

CORE_TOOLS = {
    "campaign_query",
    "exposure",
    "game_phase",
    "resolution_presentation",
    "server_capabilities",
    "skill_query",
    "storage_status",
}
PACK_SUFFIX = ".sagasmith-pack"
RUNNABLE_STATUSES = {"runnable", "runnable_installed_pack_required"}
REQUIRED_DIMENSIONS = {
    "positioning_mode": {"grid", "agent"},
    "audience": {"dm", "player"},
    "path": {"normal", "recovery"},
}
REQUIRED_MECHANISMS = {
    "preparation",
    "play_scene",
    "noncombat_check",
    "resource_settlement",
    "npc_conversation",
    "conversation_to_mechanic",
    "conversation_to_combat",
    "combat",
    "combat_render",
    "ending",
    "save_restore",
    "idempotent_retry",
    "revision_conflict_refresh",
    "phase_exposure_refresh",
}
REQUIRED_RECOVERY_OPERATIONS = {
    "process_restart",
    "snapshot_restore",
    "branch_checkout",
    "undo_redo",
}


def _arguments() -> argparse.Namespace:
    repo = Path(__file__).resolve().parents[1]
    workspace = repo.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=workspace)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--declared-corpus",
        type=Path,
        default=repo / "fixtures" / "full_campaign_corpus.json",
    )
    parser.add_argument(
        "--decisions",
        type=Path,
        default=repo / "fixtures" / "module_corpus_decisions.json",
    )
    parser.add_argument("--source-root", type=Path, action="append", default=[])
    parser.add_argument("--pack-root", type=Path, action="append", default=[])
    parser.add_argument("--catalog-root", type=Path, action="append", default=[])
    parser.add_argument(
        "--installed-home",
        type=Path,
        action="append",
        default=[],
        help="MCP home to inspect through campaign_query/module_query; repeatable",
    )
    parser.add_argument("--fail-on-pending", action="store_true")
    parser.add_argument("--fail-on-incomplete-coverage", action="store_true")
    return parser.parse_args()


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _relative(path: Path, workspace: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _coverage_routes(decisions: dict[str, Any]) -> dict[str, dict[str, Any]]:
    routes = decisions.get("coverage_routes") or []
    if not isinstance(routes, list):
        raise ValueError("coverage_routes must be a list")
    indexed: dict[str, dict[str, Any]] = {}
    for route in routes:
        if not isinstance(route, dict):
            raise ValueError("coverage route must be an object")
        line_id = str(route.get("campaign_line_id") or "")
        if not line_id:
            raise ValueError("coverage route is missing campaign_line_id")
        if line_id in indexed:
            raise ValueError(f"duplicate coverage route: {line_id}")
        indexed[line_id] = route
    return indexed


def _build_coverage_matrix(
    units: list[dict[str, Any]], routes: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Join route evidence onto dynamically discovered runnable units."""

    discovered_ids = {str(unit["id"]) for unit in units}
    runnable = {
        str(unit["id"]): unit
        for unit in units
        if unit.get("status") in RUNNABLE_STATUSES
    }
    orphaned = sorted(set(routes) - discovered_ids)
    if orphaned:
        raise ValueError(
            "coverage routes do not match dynamically discovered runnable units: "
            + ", ".join(orphaned)
        )

    matrix: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    for line_id, unit in runnable.items():
        route = routes.get(line_id)
        if route is None:
            coverage.append(
                {
                    "campaign_line_id": line_id,
                    "status": "incomplete",
                    "gaps": ["coverage_route_missing"],
                    "scenario_count": 0,
                }
            )
            continue

        source_checksums = {str(value) for value in unit.get("module_sha256") or []}
        evidence_by_id: dict[str, dict[str, Any]] = {}
        for evidence in route.get("evidence") or []:
            if not isinstance(evidence, dict):
                raise ValueError(f"{line_id}: evidence must be an object")
            evidence_id = str(evidence.get("id") or "")
            if not evidence_id or evidence_id in evidence_by_id:
                raise ValueError(f"{line_id}: evidence id must be unique and non-empty")
            source_sha256 = str(evidence.get("source_sha256") or "")
            content_sha256 = str(evidence.get("content_sha256") or "")
            heading_path = evidence.get("heading_path")
            page_start = evidence.get("page_start")
            page_end = evidence.get("page_end", page_start)
            if source_sha256 not in source_checksums:
                raise ValueError(
                    f"{line_id}/{evidence_id}: source checksum is not part of discovered unit"
                )
            if len(content_sha256) != 64 or any(
                character not in "0123456789abcdef" for character in content_sha256
            ):
                raise ValueError(f"{line_id}/{evidence_id}: invalid content_sha256")
            if not isinstance(heading_path, list) or not heading_path or not all(
                isinstance(value, str) and value.strip() for value in heading_path
            ):
                raise ValueError(f"{line_id}/{evidence_id}: heading_path is required")
            if (
                not isinstance(page_start, int)
                or not isinstance(page_end, int)
                or page_start < 1
                or page_end < page_start
            ):
                raise ValueError(f"{line_id}/{evidence_id}: invalid page range")
            evidence_by_id[evidence_id] = evidence

        scenarios = route.get("scenarios") or []
        if not isinstance(scenarios, list):
            raise ValueError(f"{line_id}: scenarios must be a list")
        seen_scenarios: set[str] = set()
        dimensions = {name: set() for name in REQUIRED_DIMENSIONS}
        mechanisms: set[str] = set()
        recovery_operations: set[str] = set()
        has_legal_ending = False
        for scenario in scenarios:
            if not isinstance(scenario, dict):
                raise ValueError(f"{line_id}: scenario must be an object")
            scenario_id = str(scenario.get("id") or "")
            if not scenario_id or scenario_id in seen_scenarios:
                raise ValueError(f"{line_id}: scenario id must be unique and non-empty")
            seen_scenarios.add(scenario_id)
            evidence_id = str(scenario.get("evidence_id") or "")
            if evidence_id not in evidence_by_id:
                raise ValueError(f"{line_id}/{scenario_id}: unknown evidence_id {evidence_id!r}")
            chapter_or_scene = str(scenario.get("chapter_or_scene") or "")
            if not chapter_or_scene:
                raise ValueError(f"{line_id}/{scenario_id}: chapter_or_scene is required")
            scenario_mechanisms = scenario.get("mechanisms") or []
            if not isinstance(scenario_mechanisms, list) or not all(
                isinstance(value, str) and value for value in scenario_mechanisms
            ):
                raise ValueError(f"{line_id}/{scenario_id}: mechanisms must be strings")
            for name, allowed in REQUIRED_DIMENSIONS.items():
                value = scenario.get(name)
                if value not in allowed:
                    raise ValueError(
                        f"{line_id}/{scenario_id}: invalid {name} {value!r}"
                    )
                dimensions[name].add(str(value))
            scenario_recovery = scenario.get("recovery_operations") or []
            if not isinstance(scenario_recovery, list) or not all(
                isinstance(value, str) and value for value in scenario_recovery
            ):
                raise ValueError(
                    f"{line_id}/{scenario_id}: recovery_operations must be strings"
                )
            if scenario["path"] == "recovery" and not scenario_recovery:
                raise ValueError(
                    f"{line_id}/{scenario_id}: recovery path requires recovery_operations"
                )
            ending_status = scenario.get("ending_status", "not_applicable")
            if ending_status not in {"legal_complete", "not_applicable"}:
                raise ValueError(
                    f"{line_id}/{scenario_id}: invalid ending_status {ending_status!r}"
                )
            ending_prerequisites = scenario.get("ending_prerequisites") or []
            if not isinstance(ending_prerequisites, list) or not all(
                isinstance(value, dict)
                and str(value.get("id") or "")
                and str(value.get("receipt") or "")
                for value in ending_prerequisites
            ):
                raise ValueError(
                    f"{line_id}/{scenario_id}: ending_prerequisites must be objects "
                    "with non-empty id and receipt"
                )
            allowed_ending_receipts = {
                "loot_acquire",
                "semantic_event",
                "character_check",
                "item_spend",
            }
            unknown_ending_receipts = {
                str(value.get("receipt"))
                for value in ending_prerequisites
                if value.get("receipt") not in allowed_ending_receipts
            }
            if unknown_ending_receipts:
                raise ValueError(
                    f"{line_id}/{scenario_id}: unknown ending receipts "
                    + ", ".join(sorted(unknown_ending_receipts))
                )
            if ending_prerequisites and ending_status != "legal_complete":
                raise ValueError(
                    f"{line_id}/{scenario_id}: ending_prerequisites require a legal ending"
                )
            ending_prerequisites_by_id = {
                str(value["id"]): value for value in ending_prerequisites
            }
            if len(ending_prerequisites_by_id) != len(ending_prerequisites):
                raise ValueError(
                    f"{line_id}/{scenario_id}: ending prerequisite ids must be unique"
                )
            for index, prerequisite in enumerate(ending_prerequisites):
                receipt = prerequisite["receipt"]
                if receipt == "semantic_event":
                    if not str(prerequisite.get("fact_key") or ""):
                        raise ValueError(
                            f"{line_id}/{scenario_id}: semantic_event requires fact_key"
                        )
                    reduction = prerequisite.get("dc_reduction")
                    if reduction is not None and (
                        not isinstance(reduction, int)
                        or isinstance(reduction, bool)
                        or reduction <= 0
                    ):
                        raise ValueError(
                            f"{line_id}/{scenario_id}: semantic_event requires positive "
                            "integer dc_reduction"
                        )
                if receipt == "character_check" and "base_dc" in prerequisite:
                    base_dc = prerequisite["base_dc"]
                    reducer_ids = prerequisite.get("applied_reducer_ids")
                    if (
                        not isinstance(base_dc, int)
                        or isinstance(base_dc, bool)
                        or not isinstance(reducer_ids, list)
                        or not reducer_ids
                        or not all(isinstance(value, str) and value for value in reducer_ids)
                    ):
                        raise ValueError(
                            f"{line_id}/{scenario_id}: reduced character_check requires "
                            "integer base_dc and non-empty applied_reducer_ids"
                        )
                    preceding = ending_prerequisites[:index]
                    reducers = {
                        str(value["id"]): value
                        for value in preceding
                        if value["receipt"] == "semantic_event"
                        and value.get("dc_reduction") is not None
                    }
                    if set(reducer_ids) != set(reducers):
                        raise ValueError(
                            f"{line_id}/{scenario_id}: applied_reducer_ids must name all "
                            "preceding semantic_event prerequisites"
                        )
                    expected_dc = base_dc - sum(
                        int(reducers[reducer_id]["dc_reduction"])
                        for reducer_id in reducer_ids
                    )
                    if prerequisite.get("dc") != expected_dc:
                        raise ValueError(
                            f"{line_id}/{scenario_id}: reduced character_check dc must equal "
                            "base_dc minus declared reducers"
                        )
            unknown_recovery = set(scenario_recovery) - REQUIRED_RECOVERY_OPERATIONS
            if unknown_recovery:
                raise ValueError(
                    f"{line_id}/{scenario_id}: unknown recovery operations "
                    + ", ".join(sorted(unknown_recovery))
                )
            mechanisms.update(scenario_mechanisms)
            recovery_operations.update(scenario_recovery)
            if (
                scenario["path"] == "normal"
                and ending_status == "legal_complete"
            ):
                has_legal_ending = True
            evidence = evidence_by_id[evidence_id]
            matrix.append(
                {
                    "campaign_line_id": line_id,
                    "scenario_id": scenario_id,
                    "chapter_or_scene": chapter_or_scene,
                    "key_mechanisms": scenario_mechanisms,
                    "positioning_mode": scenario["positioning_mode"],
                    "audience": scenario["audience"],
                    "path": scenario["path"],
                    "recovery_operations": scenario_recovery,
                    "ending_status": ending_status,
                    "source_ref": {
                        "source_sha256": evidence["source_sha256"],
                        "heading_path": evidence["heading_path"],
                        "content_sha256": evidence["content_sha256"],
                        "page_start": evidence["page_start"],
                        "page_end": evidence.get("page_end", evidence["page_start"]),
                    },
                }
            )

        gaps: list[str] = []
        for name, required in REQUIRED_DIMENSIONS.items():
            for missing in sorted(required - dimensions[name]):
                gaps.append(f"missing_{name}:{missing}")
        for missing in sorted(REQUIRED_MECHANISMS - mechanisms):
            gaps.append(f"missing_mechanism:{missing}")
        for missing in sorted(REQUIRED_RECOVERY_OPERATIONS - recovery_operations):
            gaps.append(f"missing_recovery_operation:{missing}")
        if not has_legal_ending:
            gaps.append("missing_normal_legal_ending")
        coverage.append(
            {
                "campaign_line_id": line_id,
                "status": "complete" if not gaps else "incomplete",
                "gaps": gaps,
                "scenario_count": len(scenarios),
                "covered_dimensions": {
                    name: sorted(values) for name, values in dimensions.items()
                },
                "covered_mechanisms": sorted(mechanisms),
                "covered_recovery_operations": sorted(recovery_operations),
            }
        )
    return matrix, coverage


def _declared_records(
    path: Path, workspace: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    manifest = _load_json(path)
    edition = str(manifest.get("edition") or "").strip()
    if not edition:
        raise ValueError(f"declared corpus is missing edition: {path}")
    root = workspace / "reference" / "DnD-Books" / "5e" / "Campaign"
    records: list[dict[str, Any]] = []
    units: list[dict[str, Any]] = []
    for line in manifest.get("campaign_lines") or []:
        module_records: list[dict[str, Any]] = []
        for entry in line.get("modules") or []:
            source = (root / str(entry["path"])).resolve()
            record = {
                "source_kind": "declared_corpus",
                "path": _relative(source, workspace),
                "sha256": str(entry["sha256"]),
                "size": int(entry["size"]),
                "classification": str(entry["role"]),
                "disposition": (
                    "companion" if entry["role"] == "dm_guide" else "runnable"
                ),
                "reason_code": (
                    "companion_covered_with_primary"
                    if entry["role"] == "dm_guide"
                    else "declared_campaign_module"
                ),
                "campaign_line_id": str(line["id"]),
                "title": str(line["title"]),
                "edition": edition,
                "exists": source.is_file(),
            }
            if source.is_file():
                record["checksum_valid"] = _sha256(source) == record["sha256"]
            records.append(record)
            module_records.append(record)
        units.append(
            {
                "id": str(line["id"]),
                "title": str(line["title"]),
                "module_sha256": [item["sha256"] for item in module_records],
                "module_paths": [item["path"] for item in module_records],
                "status": "runnable",
                "edition": edition,
                "advancement_mode": str(
                    dict(line.get("play_requirements") or {})
                    .get("advancement", {})
                    .get("selected")
                    or ""
                ),
                "play_requirements": dict(line.get("play_requirements") or {}),
                "evidence": ["declared_corpus"],
            }
        )
        for category in ("player_materials", "assets"):
            for entry in line.get(category) or []:
                source = (root / str(entry["path"])).resolve()
                records.append(
                    {
                        "source_kind": "declared_corpus",
                        "path": _relative(source, workspace),
                        "sha256": str(entry["sha256"]),
                        "size": int(entry["size"]),
                        "classification": str(entry["role"]),
                        "disposition": "excluded",
                        "reason_code": "manifest_declared_player_or_auxiliary_material",
                        "campaign_line_id": str(line["id"]),
                        "exists": source.is_file(),
                    }
                )
    return records, units


def _pack_record(path: Path, workspace: Path) -> dict[str, Any]:
    record: dict[str, Any] = {
        "source_kind": "content_pack_archive",
        "path": _relative(path, workspace),
        "archive_sha256": _sha256(path),
        "size": path.stat().st_size,
    }
    try:
        with zipfile.ZipFile(path) as archive:
            descriptor = json.loads(archive.read("package.sagasmith.json"))
    except (KeyError, OSError, ValueError, zipfile.BadZipFile) as exc:
        return {
            **record,
            "disposition": "excluded",
            "reason_code": "invalid_content_pack_archive",
            "error": f"{type(exc).__name__}: {exc}",
        }
    record.update(
        {
            "package_id": descriptor.get("id"),
            "package_version": descriptor.get("version"),
            "package_kind": descriptor.get("kind"),
            "package_schema_version": descriptor.get("schema_version"),
            "package_checksum": descriptor.get("checksum"),
        }
    )
    if descriptor.get("kind") != "module":
        return {**record, "disposition": "excluded", "reason_code": "pack_not_module"}
    source_checksums = sorted(
        {
            str(asset.get("checksum"))
            for asset in descriptor.get("assets") or []
            if asset.get("kind") == "source_asset" and asset.get("checksum")
        }
    )
    readiness = dict(descriptor.get("readiness") or {})
    metadata = dict(descriptor.get("metadata") or {})
    manifest = dict(descriptor.get("manifest") or {})
    compatibility = dict(manifest.get("compatibility") or {})
    finalization = metadata.get("agent_finalization")
    record.update(
        {
            "title": manifest.get("title") or metadata.get("title"),
            "source_sha256": source_checksums,
            "readiness_complete": readiness.get("complete") is True,
            "agent_finalized": isinstance(finalization, dict)
            and finalization.get("confirmed") is True,
            "ending_count": int(
                dict(manifest.get("content_summary") or {}).get("endings") or 0
            ),
            "classification": manifest.get("classification"),
            "editions": list(compatibility.get("editions") or []),
        }
    )
    if not record["readiness_complete"] or not record["agent_finalized"]:
        return {
            **record,
            "disposition": "excluded",
            "reason_code": "module_pack_not_agent_finalized",
        }
    if record["classification"] == "dm_guide":
        return {
            **record,
            "disposition": "companion",
            "reason_code": "companion_covered_with_primary",
        }
    if record["ending_count"] < 1:
        return {
            **record,
            "disposition": "excluded",
            "reason_code": "module_pack_missing_source_defined_ending",
        }
    return {**record, "disposition": "runnable", "reason_code": "finalized_module_pack"}


def _catalog_records(root: Path, workspace: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("index.json")) if root.is_dir() else []:
        try:
            catalog = _load_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if catalog.get("schema") != "sagasmith.content-library.v1":
            continue
        for package in catalog.get("packages") or []:
            records.append(
                {
                    "source_kind": "content_library_catalog",
                    "path": _relative(path, workspace),
                    "package_id": package.get("id"),
                    "package_kind": package.get("kind"),
                    "package_version": package.get("version"),
                    "package_checksum": package.get("checksum"),
                    "disposition": (
                        "candidate" if package.get("kind") == "module" else "excluded"
                    ),
                    "reason_code": (
                        "catalog_module_requires_archive_inspection"
                        if package.get("kind") == "module"
                        else "catalog_entry_not_module"
                    ),
                }
            )
    return records


def _raw_records(
    roots: list[Path], workspace: Path, decisions: dict[str, Any], declared_hashes: set[str]
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    suffixes = {str(item).casefold() for item in DOCUMENT_SOURCE_SUFFIXES}
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.casefold() not in suffixes:
                continue
            checksum = _sha256(path)
            if checksum in declared_hashes:
                continue
            decision = dict(decisions.get(checksum) or {})
            record = {
                "source_kind": "raw_source",
                "path": _relative(path, workspace),
                "sha256": checksum,
                "size": path.stat().st_size,
                "classification": decision.get("classification", "unreviewed"),
                "system_id": decision.get("system_id"),
                "edition": decision.get("edition"),
                "advancement_mode": decision.get("advancement_mode"),
                "disposition": decision.get("disposition", "pending"),
                "reason_code": decision.get("reason_code", "unreviewed_source_candidate"),
                "campaign_line_id": decision.get("campaign_line_id"),
                "title": decision.get("title", path.stem),
            }
            if isinstance(decision.get("play_requirements"), dict):
                record["play_requirements"] = dict(decision["play_requirements"])
            records.append(record)
    return records


async def _installed_records(
    home: Path, workspace: Path
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    notifications: list[str] = []

    async def on_message(message: Any) -> None:
        notifications.append(type(getattr(message, "root", message)).__name__)

    args = argparse.Namespace()
    params = _server_parameters(args, workspace, home)
    records: list[dict[str, Any]] = []
    session_audit: dict[str, Any] = {"home": _relative(home, workspace)}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, message_handler=on_message) as session:
            await session.initialize()
            initial = {tool.name for tool in (await session.list_tools()).tools}
            session_audit["initial_native_tools"] = sorted(initial)
            session_audit["initial_core_exact"] = initial == CORE_TOOLS
            if initial != CORE_TOOLS:
                raise RuntimeError(
                    f"cold start tools differ from native core set: {sorted(initial)}"
                )
            listed = decode_mcp_result(
                await session.call_tool(
                    "campaign_query",
                    {"view": "list", "payload": {}, "principal_id": PRINCIPAL_ID},
                )
            )
            campaigns = _facade_value(listed)
            for campaign in campaigns or []:
                campaign_id = str(campaign["id"])
                notifications.clear()
                try:
                    decode_mcp_result(
                        await session.call_tool(
                            "exposure",
                            {
                                "action": "open",
                                "campaign_id": campaign_id,
                                "principal_id": PRINCIPAL_ID,
                            },
                        )
                    )
                    decode_mcp_result(
                        await session.call_tool(
                            "exposure",
                            {
                                "action": "set",
                                "add_tool_ids": ["module_query"],
                                "principal_id": PRINCIPAL_ID,
                            },
                        )
                    )
                except RuntimeError as exc:
                    records.append(
                        {
                            "source_kind": "installed_campaign",
                            "home": _relative(home, workspace),
                            "campaign_id": campaign_id,
                            "campaign_name": campaign.get("name"),
                            "disposition": "pending",
                            "reason_code": "installed_campaign_not_inspectable",
                            "error": str(exc),
                        }
                    )
                    continue
                await asyncio.sleep(0)
                visible = {tool.name for tool in (await session.list_tools()).tools}
                if "module_query" not in visible:
                    raise RuntimeError("module_query was not exposed through native tools/list")
                result = decode_mcp_result(
                    await session.call_tool(
                        "module_query",
                        {
                            "campaign_id": campaign_id,
                            "view": "list",
                            "payload": {},
                            "principal_id": PRINCIPAL_ID,
                        },
                    )
                )
                modules = _facade_value(result)
                for module in modules or []:
                    records.append(
                        {
                            "source_kind": "installed_module",
                            "home": _relative(home, workspace),
                            "campaign_id": campaign_id,
                            "campaign_name": campaign.get("name"),
                            "module_id": module.get("id"),
                            "title": module.get("title"),
                            "source_sha256": module.get("source_checksum"),
                            "active": module.get("active") is True,
                            "scene_count": module.get("scene_count"),
                            "disposition": "runnable" if module.get("active") else "installed",
                            "reason_code": (
                                "active_installed_module"
                                if module.get("active")
                                else "installed_inactive_module"
                            ),
                        }
                    )
            session_audit["tools_list_changed_count"] = notifications.count(
                "ToolListChangedNotification"
            )
    return records, session_audit


def _default_roots(workspace: Path) -> tuple[list[Path], list[Path], list[Path], list[Path]]:
    source_roots = [
        workspace / "reference" / "DnD-Books" / "5e" / "Campaign",
        workspace / "reference" / "DnD-Books" / "5e" / "One Shots",
        workspace / "test_pdfs",
        workspace / "SagaSmith-dnd-mcp" / "fixtures",
    ]
    pack_roots = [
        workspace / "tmp" / "unified-content-build-cache",
        workspace / "SagaSmith-dnd-content-library" / "public" / "content-library",
    ]
    catalog_roots = [workspace / "SagaSmith-dnd-content-library" / "public" / "content-library"]
    installed_homes = [
        workspace / ".sagasmith-dnd-mcp-regression",
        workspace / ".runs" / "full-campaign-playthrough" / "grouped-full-home",
    ]
    return source_roots, pack_roots, catalog_roots, installed_homes


async def build_report(args: argparse.Namespace) -> dict[str, Any]:
    workspace = args.workspace.resolve()
    defaults = _default_roots(workspace)
    source_roots = [path.resolve() for path in (args.source_root or defaults[0])]
    pack_roots = [path.resolve() for path in (args.pack_root or defaults[1])]
    catalog_roots = [path.resolve() for path in (args.catalog_root or defaults[2])]
    installed_homes = [
        path.resolve() for path in (args.installed_home or defaults[3]) if path.is_dir()
    ]
    declared, units = _declared_records(args.declared_corpus.resolve(), workspace)
    decision_fixture = _load_json(args.decisions.resolve())
    decisions = dict(decision_fixture.get("decisions_by_sha256") or {})
    coverage_routes = _coverage_routes(decision_fixture)
    pack_records = [
        _pack_record(path, workspace)
        for root in pack_roots
        if root.is_dir()
        for path in sorted(root.rglob(f"*{PACK_SUFFIX}"))
    ]
    catalogs = [record for root in catalog_roots for record in _catalog_records(root, workspace)]
    raw = _raw_records(
        source_roots,
        workspace,
        decisions,
        {str(item["sha256"]) for item in declared},
    )
    installed: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    for home in installed_homes:
        found, audit = await _installed_records(home, workspace)
        installed.extend(found)
        sessions.append(audit)

    unit_by_id = {str(unit["id"]): unit for unit in units}
    for record in raw:
        line_id = record.get("campaign_line_id")
        if line_id and line_id not in unit_by_id:
            unit = {
                "id": line_id,
                "title": record.get("title"),
                "module_sha256": [record["sha256"]],
                "module_paths": [record["path"]],
                "status": record["disposition"],
                "edition": record.get("edition"),
                "advancement_mode": record.get("advancement_mode"),
                "play_requirements": dict(record.get("play_requirements") or {}),
                "evidence": ["raw_source_decision"],
            }
            units.append(unit)
            unit_by_id[line_id] = unit

    packs_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in pack_records:
        for checksum in record.get("source_sha256") or []:
            packs_by_source[str(checksum)].append(record)
    installed_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    installed_by_title: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in installed:
        checksum = str(record.get("source_sha256") or "")
        if checksum:
            installed_by_source[checksum].append(record)
        title = str(record.get("title") or "").strip().casefold()
        if title:
            installed_by_title[title].append(record)
    for unit in units:
        checksums = list(unit.get("module_sha256") or [])
        unit["packs"] = [
            item for checksum in checksums for item in packs_by_source.get(str(checksum), [])
        ]
        unit["installed_modules"] = [
            item for checksum in checksums for item in installed_by_source.get(str(checksum), [])
        ]
        if not unit["installed_modules"]:
            unit["installed_modules"] = list(
                installed_by_title.get(str(unit.get("title") or "").strip().casefold(), [])
            )
        if unit["status"] == "runnable_installed_pack_required" and not unit["installed_modules"]:
            unit["status"] = "blocked"
            unit["blocker"] = "declared active install was not discoverable through public MCP"

    candidates = [*declared, *pack_records, *catalogs, *raw, *installed]
    pending = [item for item in candidates if item.get("disposition") in {"pending", "candidate"}]
    exclusions = [item for item in candidates if item.get("disposition") == "excluded"]
    runnable = [unit for unit in units if unit.get("status") in RUNNABLE_STATUSES]
    matrix, coverage = _build_coverage_matrix(units, coverage_routes)
    incomplete_coverage = [item for item in coverage if item["status"] != "complete"]
    return {
        "schema_version": 2,
        "status": (
            "pending_review"
            if pending
            else "coverage_incomplete"
            if incomplete_coverage
            else "inventoried"
        ),
        "workspace": workspace.as_posix(),
        "discovery": {
            "source_roots": [_relative(path, workspace) for path in source_roots],
            "pack_roots": [_relative(path, workspace) for path in pack_roots],
            "catalog_roots": [_relative(path, workspace) for path in catalog_roots],
            "installed_homes": [_relative(path, workspace) for path in installed_homes],
            "sessions": sessions,
        },
        "summary": {
            "candidate_records": len(candidates),
            "coverage_units": len(units),
            "runnable_units": len(runnable),
            "excluded_records": len(exclusions),
            "pending_records": len(pending),
            "coverage_scenarios": len(matrix),
            "incomplete_coverage_units": len(incomplete_coverage),
        },
        "coverage_units": units,
        "coverage_matrix": matrix,
        "coverage_validation": coverage,
        "exclusions": exclusions,
        "pending": pending,
        "records": candidates,
    }


async def _run(args: argparse.Namespace) -> int:
    report = await build_report(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report["summary"], ensure_ascii=False))
    if args.fail_on_pending and report["pending"]:
        return 2
    if (
        args.fail_on_incomplete_coverage
        and report["summary"]["incomplete_coverage_units"]
    ):
        return 3
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run(_arguments())))


if __name__ == "__main__":
    main()
