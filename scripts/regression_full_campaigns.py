"""Import the complete local campaign corpus through public phase-scoped MCP tools.

The checked-in corpus manifest owns campaign grouping, continuation order, auxiliary
material roles, checksums, source-derived party requirements, and explicit review
blocks. This driver never imports the server implementation and never reads or writes
the campaign database.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from time import perf_counter
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import stdio_client
from sagasmith_dnd.editions import SUPPORTED_DND_EDITIONS
from sagasmith_dnd.playthrough import new_playthrough_manifest, validate_playthrough_manifest
from sagasmith_dnd.vocabulary import ADVANCEMENT_MODES

from scripts.regression_modules import (
    PRINCIPAL_ID,
    ExposureClient,
    _create_baseline_snapshot,
    _import_document,
    _server_parameters,
    _token,
)


def _arguments() -> argparse.Namespace:
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Root containing the 21-file campaign corpus")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=repo / "fixtures" / "full_campaign_corpus.json",
    )
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", default="full-campaign-playthrough-v1")
    parser.add_argument("--edition", choices=SUPPORTED_DND_EDITIONS, default="2014")
    parser.add_argument("--locale", default="en")
    parser.add_argument("--campaign", action="append", default=[])
    parser.add_argument("--include-scene-index", action="store_true")
    parser.add_argument("--fail-on-warning", action="store_true")
    return parser.parse_args()


def _line_entries(line: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *list(line.get("modules") or []),
        *list(line.get("player_materials") or []),
        *list(line.get("assets") or []),
    ]


def _manifest_entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    entries = [
        entry for line in manifest.get("campaign_lines") or [] for entry in _line_entries(line)
    ]
    return [*entries, *list(manifest.get("unassigned_assets") or [])]


def _load_and_verify_manifest(manifest_path: Path, root: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError("campaign corpus manifest schema_version must be 1")
    entries = _manifest_entries(manifest)
    expected_count = int(manifest.get("expected_asset_count") or 0)
    if len(entries) != expected_count:
        raise ValueError(
            f"campaign corpus entry count mismatch: {len(entries)} != {expected_count}"
        )
    paths = [str(entry.get("path") or "") for entry in entries]
    if not all(paths) or len(paths) != len(set(paths)):
        raise ValueError("campaign corpus paths must be non-empty and unique")
    verified: list[dict[str, Any]] = []
    for entry in entries:
        relative = Path(str(entry["path"]))
        source = (root / relative).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise FileNotFoundError(relative.as_posix())
        size = source.stat().st_size
        if size != int(entry["size"]):
            raise ValueError(f"campaign corpus size mismatch: {relative.as_posix()}")
        with source.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        if checksum != str(entry["sha256"]):
            raise ValueError(f"campaign corpus checksum mismatch: {relative.as_posix()}")
        verified.append(
            {
                "path": relative.as_posix(),
                "size": size,
                "sha256": checksum,
                "role": entry["role"],
            }
        )
    return {**manifest, "verification": {"valid": True, "assets": verified}}


def _selected_lines(manifest: dict[str, Any], selected: list[str]) -> list[dict[str, Any]]:
    lines = list(manifest.get("campaign_lines") or [])
    if not selected:
        return lines
    requested = set(selected)
    by_id = {str(line["id"]): line for line in lines}
    unknown = sorted(requested - set(by_id))
    if unknown:
        raise ValueError(f"unknown campaign line ids: {unknown}")
    return [line for line in lines if line["id"] in requested]


async def _create_campaign(
    client: ExposureClient,
    *,
    line: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    await client.open()
    await client.load()
    line_id = str(line["id"])
    identity = _token(f"{args.run_id}\0full-campaign\0{line_id}")
    campaign = await client.domain(
        "campaign_create",
        {
            "name": f"Full campaign regression: {line['title']} [{_token(args.run_id, length=8)}]",
            "edition": args.edition,
            "locale": args.locale,
            "random_seed": f"{args.run_id}:full-campaign:{line_id}",
            "principal_id": PRINCIPAL_ID,
            "idempotency_key": f"full-campaign-create-{identity}",
        },
    )
    await client.open(str(campaign["id"]))
    await client.load()
    advancement = dict(line["play_requirements"].get("advancement") or {})
    selected_advancement = str(advancement.get("selected") or "")
    if selected_advancement not in ADVANCEMENT_MODES:
        raise ValueError(f"campaign line {line_id} must select xp or milestone advancement")
    current_advancement = str(
        dict(dict(campaign.get("settings") or {}).get("advancement") or {}).get("mode") or ""
    )
    if current_advancement == selected_advancement:
        return campaign
    configured = await client.domain(
        "campaign_change",
        {
            "campaign_id": campaign["id"],
            "action": "advancement_configure",
            "payload": {"mode": selected_advancement},
            "expected_revision": campaign["revision"],
            "idempotency_key": (
                f"full-campaign-advancement-{identity}-"
                f"r{campaign['revision']}-{selected_advancement}"
            ),
        },
    )
    return dict(configured["campaign"])


async def _resolve_scene(
    client: ExposureClient,
    *,
    campaign_id: str,
    module_id: str,
    title: str,
) -> dict[str, Any]:
    hits = await client.domain(
        "module_search",
        {"campaign_id": campaign_id, "query": title, "top_k": 20},
    )
    matches: dict[str, dict[str, Any]] = {}
    for hit in hits or []:
        chunk_id = str(hit.get("chunk_id") or hit.get("id") or "")
        if not chunk_id:
            continue
        expanded = await client.domain("module_expand", {"chunk_id": chunk_id})
        module = dict(expanded.get("module") or {})
        scene = dict(expanded.get("scene") or {})
        if module.get("id") != module_id:
            continue
        if str(scene.get("title") or "").strip().casefold() != title.strip().casefold():
            continue
        matches[str(scene["id"])] = {
            "scene_id": scene["id"],
            "scene_title": scene["title"],
            "module_id": module_id,
            "evidence_chunk_id": expanded["chunk_id"],
            "page_start": expanded.get("page_start"),
            "page_end": expanded.get("page_end"),
        }
    if len(matches) != 1:
        raise RuntimeError(
            f"scene association must resolve exactly once: {title!r} -> {len(matches)}"
        )
    return next(iter(matches.values()))


async def _attach_asset(
    client: ExposureClient,
    *,
    args: argparse.Namespace,
    root: Path,
    line: dict[str, Any],
    campaign_id: str,
    module_id: str,
    entry: dict[str, Any],
    scene: dict[str, Any] | None = None,
) -> dict[str, Any]:
    relative = str(entry["path"])
    source = (root / relative).resolve()
    identity = _token(f"{args.run_id}\0{line['id']}\0asset\0{relative}")
    payload: dict[str, Any] = {
        "module_id": module_id,
        "source_path": str(source),
        "asset_kind": str(entry["role"]),
        "title": source.stem,
        "metadata": {
            "corpus_id": "sagasmith-local-dnd5e-campaigns-20260723",
            "corpus_path": relative,
            "corpus_sha256": entry["sha256"],
        },
    }
    if scene is not None:
        payload["scene_id"] = scene["scene_id"]
    attached = await client.domain(
        "module_draft",
        {
            "campaign_id": campaign_id,
            "action": "edit",
            "payload": {"operation": "asset", **payload},
            "idempotency_key": f"full-campaign-attach-{identity}",
        },
    )
    return {
        "path": relative,
        "role": entry["role"],
        "module_id": module_id,
        "scene": scene,
        "attachment": attached,
    }


def _line_review_blocks(
    line: dict[str, Any],
    player_documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for document in player_documents:
        inspection = dict(document.get("character_document") or {})
        declared = dict(document.get("declared_player_material") or {})
        if (
            inspection.get("document_kind") == "character_sheet"
            and not inspection.get("ready_to_create")
            and declared.get("review_status")
            not in {
                "reviewed_excluded_from_party",
                "reviewed_not_module_pregen",
            }
        ):
            blocks.append(
                {
                    "kind": "incomplete_character_template",
                    "campaign_line_id": line["id"],
                    "path": document["relative_path"],
                    "missing_fields": list(inspection.get("missing_fields") or []),
                }
            )
    return blocks


def _build_playthrough_manifest(
    *,
    line: dict[str, Any],
    module_ids: list[str],
    run_id: str,
    review_blocks: list[dict[str, Any]],
    source_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    requirements = dict(line["play_requirements"])
    party_size = dict(requirements["recommended_party_size"])
    manifest = new_playthrough_manifest(
        run_id=run_id,
        campaign_line_id=str(line["id"]),
        module_ids=list(module_ids),
        recommended_party_minimum=party_size.get("minimum"),
        recommended_party_maximum=party_size.get("maximum"),
        selected_party_size=party_size.get("selected"),
        source_refs=deepcopy(
            list(source_refs if source_refs is not None else requirements.get("source_refs") or [])
        ),
        review_blocks=deepcopy(review_blocks),
        party_size_status=str(party_size.get("status") or ""),
        party_size_review=deepcopy(dict(party_size.get("review") or {})),
    )
    manifest["current"] = {
        **dict(manifest["current"]),
        "module_id": module_ids[0],
        "objective": "Complete the lobby quality gate and establish the legal party.",
    }
    manifest["world_state"] = {
        "continuation": deepcopy(dict(requirements.get("continuity") or {})),
        "pregenerated_characters": deepcopy(
            dict(requirements.get("pregenerated_characters") or {})
        ),
    }
    return validate_playthrough_manifest(manifest)


async def _resolve_playthrough_source_refs(
    client: ExposureClient,
    *,
    campaign_id: str,
    line: dict[str, Any],
    module_documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve corpus metadata citations to exact MCP-managed chunks."""

    modules_by_checksum: dict[str, dict[str, Any]] = {}
    for raw_document in module_documents:
        document = dict(raw_document)
        checksum = str(document.get("checksum") or "").casefold()
        module_id = str(document.get("module_id") or "")
        if not checksum or not module_id:
            raise RuntimeError("imported module metadata is missing checksum or module_id")
        previous = modules_by_checksum.get(checksum)
        if previous is not None and str(previous["module_id"]) != module_id:
            raise RuntimeError(f"imported module checksum identifies multiple modules: {checksum}")
        modules_by_checksum[checksum] = document
    resolved: list[dict[str, Any]] = []
    for index, raw in enumerate(list(dict(line["play_requirements"]).get("source_refs") or [])):
        source = deepcopy(dict(raw))
        asset_sha256 = str(source.get("asset_sha256") or "").casefold()
        module_document = modules_by_checksum.get(asset_sha256)
        if module_document is None:
            raise RuntimeError(
                f"source_refs[{index}] asset hash does not identify an imported module"
            )
        module_id = str(module_document.get("module_id") or "")
        heading_path = [str(item) for item in source.get("heading_path") or []]
        query = next(
            (item.strip() for item in reversed(heading_path) if item.strip()),
            "",
        )
        if not query:
            raise RuntimeError(f"source_refs[{index}] has no searchable heading")
        hits = await client.domain(
            "module_search",
            {
                "campaign_id": campaign_id,
                "module_ids": [module_id],
                "query": query,
                "top_k": 50,
            },
        )
        if isinstance(hits, dict) and isinstance(hits.get("result"), list):
            hits = hits["result"]
        if not isinstance(hits, list) or any(not isinstance(hit, dict) for hit in hits):
            raise RuntimeError(f"source_refs[{index}] module_search returned an invalid collection")
        matches: dict[str, dict[str, Any]] = {}
        for hit in hits or []:
            chunk_id = str(hit.get("chunk_id") or hit.get("id") or "")
            if not chunk_id or chunk_id in matches:
                continue
            expanded = await client.domain("module_expand", {"chunk_id": chunk_id})
            managed_ref = dict(expanded.get("source_ref") or {})
            content = str(expanded.get("content") or "")
            content_sha256 = str(expanded.get("content_sha256") or "").casefold()
            if hashlib.sha256(content.encode("utf-8")).hexdigest() != content_sha256:
                raise RuntimeError(f"source_refs[{index}] expanded content hash is inconsistent")
            if (
                str(managed_ref.get("module_id") or "") != module_id
                or content_sha256 != str(source.get("content_sha256") or "").casefold()
                or int(managed_ref.get("page_start") or 0) != int(source.get("page_start") or 0)
                or int(managed_ref.get("page_end") or 0) != int(source.get("page_end") or 0)
            ):
                continue
            scene_id = str(managed_ref.get("scene_id") or "")
            if not scene_id:
                raise RuntimeError(f"source_refs[{index}] exact chunk has no managed scene_id")
            matches[chunk_id] = {
                **source,
                "page_start": int(managed_ref["page_start"]),
                "page_end": int(managed_ref["page_end"]),
                "heading_path": list(managed_ref.get("heading_path") or []),
                "content_sha256": content_sha256,
                "module_id": module_id,
                "scene_id": scene_id,
                "chunk_id": chunk_id,
                "excerpt": content.strip(),
            }
        if len(matches) != 1:
            raise RuntimeError(
                f"source_refs[{index}] must resolve exactly once: {query!r} -> {len(matches)}"
            )
        resolved.append(next(iter(matches.values())))
    return resolved


async def _initialize_playthrough_manifest(
    client: ExposureClient,
    *,
    line: dict[str, Any],
    module_documents: list[dict[str, Any]],
    campaign_id: str,
    run_id: str,
    review_blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    campaign = await client.core(
        "campaign_query",
        {
            "view": "get",
            "payload": {"campaign_id": campaign_id},
            "principal_id": PRINCIPAL_ID,
        },
    )
    if isinstance(campaign, dict) and "result" in campaign:
        campaign = campaign["result"]
    module_ids = [str(document["module_id"]) for document in module_documents]
    source_refs = await _resolve_playthrough_source_refs(
        client,
        campaign_id=campaign_id,
        line=line,
        module_documents=module_documents,
    )
    manifest = _build_playthrough_manifest(
        line=line,
        module_ids=module_ids,
        run_id=run_id,
        review_blocks=review_blocks,
        source_refs=source_refs,
    )
    identity = _token(f"{run_id}\0{line['id']}\0playthrough-manifest")
    return await client.domain(
        "playthrough_manifest",
        {
            "campaign_id": campaign_id,
            "action": "initialize",
            "payload": {"manifest": manifest},
            "expected_revision": campaign["revision"],
            "idempotency_key": f"full-campaign-manifest-{identity}",
        },
    )


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.expanduser().resolve()
    home = args.home.expanduser().resolve()
    manifest_path = args.manifest.expanduser().resolve()
    manifest = _load_and_verify_manifest(manifest_path, root)
    lines = _selected_lines(manifest, args.campaign)
    report: dict[str, Any] = {
        "action": "full-campaign-corpus-import",
        "transport": "stdio",
        "root": str(root),
        "home": str(home),
        "manifest": str(manifest_path),
        "manifest_schema_version": manifest["schema_version"],
        "run_id": args.run_id,
        "verification": manifest["verification"],
        "campaigns": [],
        "unassigned_assets": list(manifest.get("unassigned_assets") or []),
        "review_blocks": [],
        "errors": [],
    }
    report["review_blocks"].extend(
        {
            "kind": "unassigned_asset",
            "path": entry["path"],
            "reason": entry["reason"],
        }
        for entry in manifest.get("unassigned_assets") or []
    )
    started = perf_counter()
    server = _server_parameters(args, root, home)
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            client = ExposureClient(session)
            report["storage"] = await client.core("storage_status", {})
            total_documents = sum(
                len(line.get("modules") or []) + len(line.get("player_materials") or [])
                for line in lines
            )
            document_index = 0
            for line in lines:
                line_started = perf_counter()
                line_report: dict[str, Any] = {
                    "campaign_line_id": line["id"],
                    "title": line["title"],
                    "documents": [],
                    "attachments": [],
                    "review_blocks": [],
                }
                report["campaigns"].append(line_report)
                try:
                    campaign = await _create_campaign(client, line=line, args=args)
                    campaign_id = str(campaign["id"])
                    line_report["campaign_id"] = campaign_id
                    modules_by_sequence: dict[int, dict[str, Any]] = {}
                    for entry in line.get("modules") or []:
                        document_index += 1
                        document = await _import_document(
                            client,
                            args=args,
                            root=root,
                            path=(root / entry["path"]).resolve(),
                            campaign_key=str(line["id"]),
                            campaign=campaign,
                            index=document_index,
                            total=total_documents,
                        )
                        line_report["documents"].append(document)
                        if document.get("document_role") != "module":
                            classified = document.get("document_role")
                            raise RuntimeError(
                                f"declared module was classified as {classified}: {entry['path']}"
                            )
                        modules_by_sequence[int(entry["sequence"])] = document
                    primary = modules_by_sequence[1]
                    player_documents: list[dict[str, Any]] = []
                    await client.open(campaign_id)
                    await client.load()
                    for entry in line.get("player_materials") or []:
                        document_index += 1
                        document = await _import_document(
                            client,
                            args=args,
                            root=root,
                            path=(root / entry["path"]).resolve(),
                            campaign_key=str(line["id"]),
                            campaign=campaign,
                            index=document_index,
                            total=total_documents,
                        )
                        document["declared_player_material"] = deepcopy(entry)
                        player_documents.append(document)
                        line_report["documents"].append(document)
                        line_report["attachments"].append(
                            await _attach_asset(
                                client,
                                args=args,
                                root=root,
                                line=line,
                                campaign_id=campaign_id,
                                module_id=str(primary["module_id"]),
                                entry=entry,
                            )
                        )
                    for entry in line.get("assets") or []:
                        target = modules_by_sequence[int(entry["attach_to_module_sequence"])]
                        scene = None
                        if entry.get("scene_search"):
                            scene = await _resolve_scene(
                                client,
                                campaign_id=campaign_id,
                                module_id=str(target["module_id"]),
                                title=str(entry["scene_search"]),
                            )
                        line_report["attachments"].append(
                            await _attach_asset(
                                client,
                                args=args,
                                root=root,
                                line=line,
                                campaign_id=campaign_id,
                                module_id=str(target["module_id"]),
                                entry=entry,
                                scene=scene,
                            )
                        )
                    line_report["review_blocks"] = _line_review_blocks(line, player_documents)
                    report["review_blocks"].extend(line_report["review_blocks"])
                    line_report["playthrough_manifest"] = await _initialize_playthrough_manifest(
                        client,
                        line=line,
                        module_documents=[
                            dict(modules_by_sequence[index])
                            for index in sorted(modules_by_sequence)
                        ],
                        campaign_id=campaign_id,
                        run_id=args.run_id,
                        review_blocks=line_report["review_blocks"],
                    )
                    line_report["baseline"] = await _create_baseline_snapshot(
                        client,
                        campaign_key=str(line["id"]),
                        campaign_id=campaign_id,
                        run_id=args.run_id,
                    )
                    line_report["ready_for_party_build"] = not line_report["review_blocks"]
                    line_report["seconds"] = round(perf_counter() - line_started, 3)
                except Exception as error:
                    line_report["error"] = f"{type(error).__name__}: {error}"
                    report["errors"].append(
                        {
                            "campaign_line_id": line["id"],
                            "error": line_report["error"],
                        }
                    )
    report["seconds"] = round(perf_counter() - started, 3)
    report["passed"] = not report["errors"]
    report["ready_for_play"] = report["passed"] and not report["review_blocks"]
    return report


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")
    args = _arguments()
    report = asyncio.run(_run(args))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
