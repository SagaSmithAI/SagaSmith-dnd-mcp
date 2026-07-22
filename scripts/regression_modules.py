"""Import a real module corpus through phase-scoped stdio MCP sessions.

The harness deliberately uses only the public exposure facade.  It does not
import the server implementation or read its database, so it exercises the
same staged module workflow available to an external Agent.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from time import perf_counter
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

PRINCIPAL_ID = "system:local"
SUPPORTED_SUFFIXES = {".md", ".markdown", ".pdf", ".txt"}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Allowlisted root containing module documents")
    parser.add_argument("--home", type=Path, required=True, help="MCP home for the regression")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--edition", choices=("2014", "2024"), default="2014")
    parser.add_argument("--locale", default="en")
    parser.add_argument(
        "--document",
        action="append",
        default=[],
        help="Relative document path under root; repeat to select a subset",
    )
    parser.add_argument(
        "--run-id",
        default="campaign-module-corpus-v1",
        help="Stable namespace for campaign names and idempotency keys",
    )
    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="Treat parser warnings as a failed document after inspection",
    )
    parser.add_argument(
        "--include-scene-index",
        action="store_true",
        help="Include compact scene and spatial summaries for parser diagnosis",
    )
    parser.add_argument(
        "--campaign-layout",
        choices=("document", "campaign-folder"),
        default="document",
        help=(
            "Create one campaign per document, or group documents below the same "
            "top-level folder into one campaign while keeping root documents separate"
        ),
    )
    return parser.parse_args()


def _server_parameters(args: argparse.Namespace, root: Path, home: Path) -> StdioServerParameters:
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "SAGASMITH_DND_MCP_HOME": str(home),
            "SAGASMITH_DND_MCP_AUTO_SEED": "1",
            "SAGASMITH_DND_MCP_MODULE_IMPORT_ROOTS": str(root),
        }
    )
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "sagasmith_dnd_mcp.server"],
        cwd=repo,
        env=env,
    )


def _decode(result: Any) -> Any:
    texts = [item.text for item in result.content if getattr(item, "text", None)]
    message = "\n".join(texts)
    if result.isError:
        raise RuntimeError(message or "MCP tool call failed")
    if not message:
        return result.structuredContent
    return json.loads(message)


def _facade_value(payload: Any) -> Any:
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return payload


def _token(value: str, *, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _slug(value: str) -> str:
    rendered = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return rendered[:80] or "module"


def _documents(root: Path, selected: list[str]) -> list[Path]:
    if selected:
        result: list[Path] = []
        for value in selected:
            path = (root / value).resolve()
            if not path.is_relative_to(root):
                raise ValueError(f"document escapes root: {value}")
            if not path.is_file():
                raise FileNotFoundError(path)
            if path.suffix.casefold() not in SUPPORTED_SUFFIXES:
                raise ValueError(f"unsupported module document: {value}")
            result.append(path)
        return result
    return sorted(
        path.resolve()
        for path in root.rglob("*")
        if path.is_file() and path.suffix.casefold() in SUPPORTED_SUFFIXES
    )


def _campaign_key(root: Path, path: Path, layout: str) -> str:
    relative = path.relative_to(root)
    if layout == "campaign-folder" and len(relative.parts) > 1:
        return relative.parts[0]
    return path.stem


class ExposureClient:
    def __init__(self, session: ClientSession) -> None:
        self.session = session
        self.exposure_id = ""

    async def core(self, tool_id: str, arguments: dict[str, Any]) -> Any:
        return _decode(await self.session.call_tool(tool_id, arguments))

    async def open(self, campaign_id: str | None = None) -> dict[str, Any]:
        arguments: dict[str, Any] = {"principal_id": PRINCIPAL_ID}
        if campaign_id:
            arguments["campaign_id"] = campaign_id
        opened = await self.core("exposure_open", arguments)
        self.exposure_id = str(opened["exposure_id"])
        return opened

    async def load(self, *group_ids: str) -> None:
        for group_id in group_ids:
            await self.core(
                "exposure_load",
                {"exposure_id": self.exposure_id, "group_id": group_id},
            )

    async def domain(self, tool_id: str, arguments: dict[str, Any]) -> Any:
        wrapped = await self.core(
            "exposure_call",
            {
                "exposure_id": self.exposure_id,
                "tool_id": tool_id,
                "arguments": arguments,
            },
        )
        return _facade_value(wrapped["result"])


def _preview_audit(preview: dict[str, Any]) -> dict[str, Any]:
    page_count = preview.get("page_count")
    scenes = list(preview.get("scenes") or [])
    stable_keys = [str(scene.get("stable_key") or "") for scene in scenes]
    invalid_pages: list[dict[str, Any]] = []
    for scene in scenes:
        start = scene.get("page_start")
        end = scene.get("page_end")
        if preview.get("media_type") != "application/pdf":
            continue
        if not (
            isinstance(start, int)
            and isinstance(end, int)
            and isinstance(page_count, int)
            and 1 <= start <= end <= page_count
        ):
            invalid_pages.append(
                {
                    "stable_key": scene.get("stable_key"),
                    "page_start": start,
                    "page_end": end,
                }
            )
    return {
        "scene_count": len(scenes),
        "duplicate_stable_keys": sorted(
            {key for key in stable_keys if key and stable_keys.count(key) > 1}
        ),
        "blank_stable_keys": sum(not key for key in stable_keys),
        "invalid_page_ranges": invalid_pages,
        "spatial_location_count": sum(
            len(((scene.get("spatial") or {}).get("locations") or [])) for scene in scenes
        ),
        "explicit_connection_count": sum(
            len(((scene.get("spatial") or {}).get("connections") or [])) for scene in scenes
        ),
    }


def _scene_summary(scene: dict[str, Any]) -> dict[str, Any]:
    spatial = dict(scene.get("spatial") or {})
    return {
        key: scene.get(key)
        for key in (
            "scene_id",
            "stable_key",
            "chapter",
            "title",
            "page_start",
            "page_end",
            "scene_type",
            "visibility",
            "tags",
        )
    } | {
        "locations": [
            {
                key: location.get(key)
                for key in ("key", "title", "kind", "confidence", "dimensions_ft")
            }
            for location in spatial.get("locations") or []
        ],
        "connection_count": len(spatial.get("connections") or []),
    }


async def _import_document(
    client: ExposureClient,
    *,
    args: argparse.Namespace,
    root: Path,
    path: Path,
    campaign_key: str,
    campaign: dict[str, Any],
    index: int,
    total: int,
) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    identity = _token(f"{args.run_id}\0{relative}")
    print(f"[{index}/{total}] {relative}", file=sys.stderr, flush=True)
    started = perf_counter()

    campaign_id = str(campaign["id"])
    await client.open(campaign_id)
    await client.load(
        "lobby.campaign",
        "lobby.rules",
        "lobby.modules",
        "lobby.characters",
    )

    profile = await client.domain(
        "campaign_rules", {"campaign_id": campaign_id, "action": "get_profile"}
    )
    explained = await client.domain(
        "campaign_rules", {"campaign_id": campaign_id, "action": "explain"}
    )
    document_inspection = await client.domain(
        "character_query",
        {
            "view": "document",
            "payload": {
                "campaign_id": campaign_id,
                "source_path": str(path),
            },
        },
    )
    if document_inspection.get("document_kind") != "unknown":
        return {
            "relative_path": relative,
            "document_role": document_inspection["document_kind"],
            "campaign_key": campaign_key,
            "campaign_id": campaign_id,
            "checksum": (document_inspection.get("source") or {}).get("checksum"),
            "page_count": (document_inspection.get("source") or {}).get("page_count"),
            "character_document": document_inspection,
            "core_profile": {
                "edition": ((profile.get("profile") or {}).get("edition")),
                "campaign_revision": profile.get("campaign_revision"),
                "effective_fingerprint": (profile.get("effective") or {}).get("fingerprint"),
                "core_pack": (profile.get("effective") or {}).get("core_pack"),
                "explain_core_pack": ((explained.get("effective") or {}).get("core_pack")),
            },
            "seconds": round(perf_counter() - started, 3),
        }
    stage_started = perf_counter()
    staged = await client.domain(
        "module_import",
        {
            "campaign_id": campaign_id,
            "action": "stage",
            "payload": {
                "source_path": str(path),
                "source_key": f"regression.{_slug(path.stem)}.{_token(relative, length=10)}",
                "title": path.stem,
            },
            "idempotency_key": f"module-corpus-stage-{identity}",
        },
    )
    stage_seconds = perf_counter() - stage_started
    job_id = str(staged["job"]["id"])

    inspect_started = perf_counter()
    inspected = await client.domain(
        "module_import",
        {
            "campaign_id": campaign_id,
            "action": "inspect",
            "payload": {"job_id": job_id},
            "idempotency_key": f"module-corpus-inspect-{identity}",
        },
    )
    inspect_seconds = perf_counter() - inspect_started
    preview = dict(inspected["preview"])
    warnings = list(preview.get("warnings") or [])
    audit = _preview_audit(preview)
    if args.fail_on_warning and warnings:
        raise RuntimeError("; ".join(warnings))
    if not preview.get("valid"):
        raise RuntimeError("; ".join(preview.get("errors") or ["module preview is invalid"]))

    validated = await client.domain(
        "module_import",
        {
            "campaign_id": campaign_id,
            "action": "validate",
            "payload": {"job_id": job_id},
            "idempotency_key": f"module-corpus-validate-{identity}",
        },
    )
    if not validated["validation"]["valid"]:
        raise RuntimeError("module validation rejected the inspected preview")

    ingest_started = perf_counter()
    ingested = await client.domain(
        "module_import",
        {
            "campaign_id": campaign_id,
            "action": "ingest",
            "payload": {"job_id": job_id},
            "idempotency_key": f"module-corpus-ingest-{identity}",
        },
    )
    ingest_seconds = perf_counter() - ingest_started
    campaign = _facade_value(
        await client.core(
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign_id},
                "principal_id": PRINCIPAL_ID,
            },
        )
    )
    activated = await client.domain(
        "module_import",
        {
            "campaign_id": campaign_id,
            "action": "activate",
            "payload": {"job_id": job_id},
            "expected_revision": campaign["revision"],
            "idempotency_key": f"module-corpus-activate-{identity}",
        },
    )
    module_id = str(activated["activation"]["module_id"])
    module_index = await client.domain(
        "module_query",
        {"campaign_id": campaign_id, "view": "index", "payload": {"module_id": module_id}},
    )
    assets = await client.domain(
        "module_query",
        {"campaign_id": campaign_id, "view": "assets", "payload": {"module_id": module_id}},
    )
    indexed_scenes = list(
        module_index
        if isinstance(module_index, list)
        else module_index.get("scenes") or []
    )
    if len(indexed_scenes) != audit["scene_count"]:
        raise RuntimeError(
            f"preview/index scene mismatch: {audit['scene_count']} != {len(indexed_scenes)}"
        )
    if audit["blank_stable_keys"] or audit["duplicate_stable_keys"]:
        raise RuntimeError("preview contains invalid stable scene keys")
    if audit["invalid_page_ranges"]:
        raise RuntimeError("preview contains invalid PDF page ranges")

    effective_profile = dict(profile.get("effective") or {})
    core_pack = dict(effective_profile.get("core_pack") or {})

    return {
        "relative_path": relative,
        "document_role": "module",
        "campaign_key": campaign_key,
        "campaign_id": campaign_id,
        "module_id": module_id,
        "job_id": job_id,
        "checksum": staged.get("checksum"),
        "artifact": staged.get("artifact"),
        "page_count": preview.get("page_count"),
        "warnings": warnings,
        "metadata": preview.get("metadata"),
        "parser_profile": preview.get("parser_profile"),
        "parser_version": preview.get("parser_version"),
        "preview_audit": audit,
        "index_scene_count": len(indexed_scenes),
        "scene_index": (
            [_scene_summary(scene) for scene in indexed_scenes]
            if args.include_scene_index
            else []
        ),
        "asset_count": len(assets if isinstance(assets, list) else assets.get("assets") or []),
        "core_profile": {
            "edition": ((profile.get("profile") or {}).get("edition")),
            "campaign_revision": profile.get("campaign_revision"),
            "effective_fingerprint": effective_profile.get("fingerprint"),
            "core_pack": core_pack,
            "explain_core_pack": ((explained.get("effective") or {}).get("core_pack")),
        },
        "core_fingerprint": core_pack.get("fingerprint"),
        "ingest": {
            key: ingested.get(key)
            for key in ("module_id", "skipped", "chapters", "scenes", "chunks", "embeddings")
        },
        "seconds": round(perf_counter() - started, 3),
        "stage_seconds": round(stage_seconds, 3),
        "inspect_seconds": round(inspect_seconds, 3),
        "ingest_seconds": round(ingest_seconds, 3),
    }


async def _create_baseline_snapshot(
    client: ExposureClient,
    *,
    campaign_key: str,
    campaign_id: str,
    run_id: str,
) -> dict[str, Any]:
    baseline_identity = _token(f"{run_id}\0{campaign_key}")
    await client.open(campaign_id)
    await client.load("lobby.campaign")
    campaign = await client.core(
        "campaign_query",
        {
            "view": "get",
            "payload": {"campaign_id": campaign_id},
            "principal_id": PRINCIPAL_ID,
        },
    )
    campaign = _facade_value(campaign)
    branches = await client.domain(
        "branch_query", {"campaign_id": campaign_id, "view": "list"}
    )
    current_branch = next((item for item in branches if item.get("is_current")), None)
    if current_branch is None:
        raise RuntimeError(f"campaign has no current branch: {campaign_key}")
    snapshot = await client.domain(
        "snapshot_create",
        {
            "campaign_id": campaign_id,
            "label": f"Imported campaign baseline: {campaign_key}",
            "expected_revision": campaign["revision"],
            "expected_head_snapshot_id": current_branch.get("head_snapshot_id") or "",
            "idempotency_key": f"module-corpus-baseline-{baseline_identity}",
        },
    )
    verification = await client.domain(
        "snapshot_query",
        {
            "campaign_id": campaign_id,
            "view": "verify",
            "payload": {"slot": snapshot["slot"]},
        },
    )
    return {"snapshot": snapshot, "verification": verification}


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.expanduser().resolve()
    home = args.home.expanduser().resolve()
    documents = _documents(root, args.document)
    report: dict[str, Any] = {
        "root": str(root),
        "home": str(home),
        "run_id": args.run_id,
        "edition": args.edition,
        "campaign_layout": args.campaign_layout,
        "document_count": len(documents),
        "campaigns": [],
        "documents": [],
        "errors": [],
    }
    started = perf_counter()
    server = _server_parameters(args, root, home)
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            client = ExposureClient(session)
            storage = await client.core("storage_status", {})
            report["storage"] = storage
            campaigns: dict[str, dict[str, Any]] = {}
            campaign_reports: dict[str, dict[str, Any]] = {}
            failed_campaigns: set[str] = set()
            for index, path in enumerate(documents, start=1):
                campaign_key = _campaign_key(root, path, args.campaign_layout)
                try:
                    campaign = campaigns.get(campaign_key)
                    if campaign is None:
                        campaign_identity = _token(
                            f"{args.run_id}\0campaign\0{campaign_key}"
                        )
                        await client.open()
                        await client.load("lobby.bootstrap")
                        campaign = await client.domain(
                            "campaign_create",
                            {
                                "name": (
                                    f"Campaign regression: {campaign_key} "
                                    f"[{_token(args.run_id, length=8)}]"
                                ),
                                "edition": args.edition,
                                "locale": args.locale,
                                "principal_id": PRINCIPAL_ID,
                                "idempotency_key": (
                                    f"module-corpus-campaign-{campaign_identity}"
                                ),
                            },
                        )
                        campaigns[campaign_key] = campaign
                        campaign_report = {
                            "campaign_key": campaign_key,
                            "campaign_id": campaign["id"],
                            "name": campaign.get("name"),
                        }
                        campaign_reports[campaign_key] = campaign_report
                        report["campaigns"].append(campaign_report)
                    result = await _import_document(
                        client,
                        args=args,
                        root=root,
                        path=path,
                        campaign_key=campaign_key,
                        campaign=campaign,
                        index=index,
                        total=len(documents),
                    )
                    report["documents"].append(result)
                except Exception as error:  # corpus runs must report every document
                    failed_campaigns.add(campaign_key)
                    report["errors"].append(
                        {
                            "relative_path": path.relative_to(root).as_posix(),
                            "campaign_key": campaign_key,
                            "error": f"{type(error).__name__}: {error}",
                        }
                    )
            for campaign_key, campaign in campaigns.items():
                if campaign_key in failed_campaigns:
                    continue
                try:
                    baseline = await _create_baseline_snapshot(
                        client,
                        campaign_key=campaign_key,
                        campaign_id=str(campaign["id"]),
                        run_id=args.run_id,
                    )
                    campaign_reports[campaign_key]["baseline"] = baseline
                except Exception as error:
                    report["errors"].append(
                        {
                            "campaign_key": campaign_key,
                            "error": f"baseline {type(error).__name__}: {error}",
                        }
                    )
    report["seconds"] = round(perf_counter() - started, 3)
    report["passed"] = not report["errors"] and len(report["documents"]) == len(documents)
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
