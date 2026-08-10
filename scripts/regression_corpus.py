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
    "server_capabilities",
    "skill_query",
    "storage_status",
}
PACK_SUFFIX = ".sagasmith-pack"


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


def _declared_records(
    path: Path, workspace: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    manifest = _load_json(path)
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
            records.append(
                {
                    "source_kind": "raw_source",
                    "path": _relative(path, workspace),
                    "sha256": checksum,
                    "size": path.stat().st_size,
                    "classification": decision.get("classification", "unreviewed"),
                    "system_id": decision.get("system_id"),
                    "disposition": decision.get("disposition", "pending"),
                    "reason_code": decision.get("reason_code", "unreviewed_source_candidate"),
                    "campaign_line_id": decision.get("campaign_line_id"),
                    "title": decision.get("title", path.stem),
                }
            )
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
                    f"cold start tools differ from native core six: {sorted(initial)}"
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
                await session.call_tool(
                    "exposure",
                    {
                        "action": "open",
                        "campaign_id": campaign_id,
                        "principal_id": PRINCIPAL_ID,
                    },
                )
                await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": ["module_query"],
                        "principal_id": PRINCIPAL_ID,
                    },
                )
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
    decisions = dict(_load_json(args.decisions.resolve()).get("decisions_by_sha256") or {})
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
    runnable_statuses = {"runnable", "runnable_installed_pack_required"}
    runnable = [unit for unit in units if unit.get("status") in runnable_statuses]
    matrix = [
        {
            "campaign_line_id": unit["id"],
            "scenes_or_chapters": "route_fixture_required",
            "key_mechanisms": [
                "play_scene",
                "noncombat_check",
                "npc_conversation",
                "combat",
                "ending",
                "save_restore",
            ],
            "positioning_modes": [],
            "audiences": ["dm", "player"],
            "paths": ["normal", "restore"],
            "ending_status": "route_fixture_required",
        }
        for unit in runnable
    ]
    return {
        "schema_version": 1,
        "status": "pending_review" if pending else "inventoried",
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
        },
        "coverage_units": units,
        "coverage_matrix": matrix,
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
    return 2 if args.fail_on_pending and report["pending"] else 0


def main() -> None:
    raise SystemExit(asyncio.run(_run(_arguments())))


if __name__ == "__main__":
    main()
