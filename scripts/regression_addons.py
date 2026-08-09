"""Cold-start regression for unified SagaSmith addon packages.

The driver intentionally uses only public MCP tools for import, inspection,
branch activation, catalog exposure, deactivation, and exact re-export checks.
It never mutates runtime storage or the database directly.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from time import perf_counter
from typing import Any

from sagasmith_core.content_pack import loads_content_archive

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "roots",
        nargs="+",
        type=Path,
        help="Directories containing addon *.sagasmith-pack files.",
    )
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--edition", default="2014")
    parser.add_argument("--locale", default="en")
    parser.add_argument("--run-id", default="default")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


async def _call(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    _, result = await server.call_tool(name, arguments)
    return result


def _documents(roots: list[Path]) -> list[Path]:
    files = {
        path.expanduser().resolve()
        for root in roots
        for path in root.expanduser().resolve().glob("*.sagasmith-pack")
        if path.is_file() and loads_content_archive(path.read_bytes())[0]["kind"] == "addon"
    }
    return sorted(files, key=lambda path: str(path).casefold())


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    files = _documents(args.roots)
    if not files:
        raise ValueError("no addon *.sagasmith-pack packages were found")
    base = McpConfig.from_environment()
    config = McpConfig(
        home=args.home.expanduser().resolve(),
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=base.dnd_skills_dir,
        modulegen_skills_dir=base.modulegen_skills_dir,
        auto_seed_rules=False,
        rule_import_roots=tuple(root.expanduser().resolve() for root in args.roots),
        module_import_roots=(),
    )
    server = create_server(config)
    run_token = hashlib.sha256(args.run_id.encode("utf-8")).hexdigest()[:12]
    campaign = await _call(
        server,
        "campaign_create",
        {
            "name": f"Addon cold-start regression [{run_token}]",
            "edition": args.edition,
            "locale": args.locale,
            "idempotency_key": f"addon-regression-campaign-{run_token}",
        },
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "roots": [str(root.expanduser().resolve()) for root in args.roots],
        "home": str(config.home),
        "edition": args.edition,
        "run_id": args.run_id,
        "package_count": len(files),
        "packages": [],
        "errors": [],
    }
    started = perf_counter()
    for index, path in enumerate(files, start=1):
        item_started = perf_counter()
        print(f"[{index}/{len(files)}] {path.name}", file=sys.stderr, flush=True)
        try:
            package, _blobs = loads_content_archive(path.read_bytes())
            checksum = str(package.get("checksum") or "")
            if package["kind"] != "addon":
                raise RuntimeError("content package is not an addon")
            if "readiness" in package:
                raise RuntimeError("addon still contains the removed readiness field")
            key = hashlib.sha256(f"{path}\0{checksum}\0{args.run_id}".encode("utf-8")).hexdigest()[
                :20
            ]
            imported = await _call(
                server,
                "content_pack",
                {
                    "action": "import",
                    "payload": {
                        "kind": "addon",
                        "campaign_id": campaign["id"],
                        "source_path": str(path),
                    },
                    "idempotency_key": f"addon-regression-import-{key}",
                },
            )
            value = imported["result"]
            if value["stored"] is not True or value["activated"] is not False:
                raise RuntimeError("addon import crossed the stored/inactive boundary")
            invalid_components = [
                item
                for item in value["components"]
                if item["status"] not in {"stored", "campaign_import_required"}
            ]
            if invalid_components:
                raise RuntimeError("addon contains components that were not stored")
            detail = await _call(
                server,
                "content_pack",
                {
                    "action": "get",
                    "payload": {
                        "kind": "addon",
                        "campaign_id": campaign["id"],
                        "addon_id": package["id"],
                        "version": package["version"],
                        "include_package": True,
                    },
                },
            )
            if detail["result"]["package"] != package:
                raise RuntimeError("stored addon differs from the detached package")
            profile = await _call(
                server,
                "campaign_rules",
                {"campaign_id": campaign["id"], "action": "get_profile"},
            )
            revision = profile["result"]["campaign_revision"]
            activated = await _call(
                server,
                "content_pack",
                {
                    "action": "activate",
                    "payload": {
                        "kind": "addon",
                        "campaign_id": campaign["id"],
                        "addon_id": package["id"],
                        "version": package["version"],
                    },
                    "expected_revision": revision,
                    "idempotency_key": f"addon-regression-enable-{key}-r{revision}",
                },
            )
            if activated["result"]["activation"]["enabled"] is not True:
                raise RuntimeError("addon did not activate")
            catalog = await _call(
                server,
                "content_pack",
                {
                    "action": "list",
                    "payload": {"kind": "catalog", "campaign_id": campaign["id"]},
                },
            )
            current = await _call(
                server,
                "campaign_query",
                {"view": "get", "payload": {"campaign_id": campaign["id"]}},
            )
            revision = current["result"]["revision"]
            disabled = await _call(
                server,
                "content_pack",
                {
                    "action": "deactivate",
                    "payload": {
                        "kind": "addon",
                        "campaign_id": campaign["id"],
                        "addon_id": package["id"],
                        "version": package["version"],
                    },
                    "expected_revision": revision,
                    "idempotency_key": f"addon-regression-disable-{key}-r{revision}",
                },
            )
            if disabled["result"]["activation"]["enabled"] is not False:
                raise RuntimeError("addon did not deactivate")
            if detail["result"]["package"] != package:
                raise RuntimeError("public addon re-export differs from the input package")
            report["packages"].append(
                {
                    "path": str(path),
                    "id": package["id"],
                    "version": package["version"],
                    "checksum": checksum,
                    "classification": package["manifest"]["classification"],
                    "content_summary": package["manifest"]["content_summary"],
                    "validation": "accepted by content_pack(import)",
                    "components": value["components"],
                    "catalog_artifacts_while_active": len(catalog["result"]),
                    "stored": True,
                    "activated": True,
                    "deactivated": True,
                    "reexport_identical": True,
                    "seconds": round(perf_counter() - item_started, 3),
                }
            )
            print(
                f"[OK {index}/{len(files)}] {path.name}",
                file=sys.stderr,
                flush=True,
            )
        except Exception as error:  # package audit must continue after one failure
            message = f"{type(error).__name__}: {error}"
            report["errors"].append({"path": str(path), "error": message})
            print(
                f"[FAIL {index}/{len(files)}] {path.name}: {message}",
                file=sys.stderr,
                flush=True,
            )
    report["seconds"] = round(perf_counter() - started, 3)
    report["passed"] = not report["errors"] and len(report["packages"]) == len(files)
    return report


def main() -> int:
    args = _arguments()
    report = asyncio.run(_run(args))
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.expanduser().resolve().write_text(output, encoding="utf-8")
    print(output)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
