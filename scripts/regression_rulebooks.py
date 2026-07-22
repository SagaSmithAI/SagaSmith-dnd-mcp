"""Run the public staged rule-import workflow against a real document corpus."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path
from time import perf_counter
from typing import Any

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
    parser.add_argument("--edition", choices=("2014", "2024"), default="2014")
    parser.add_argument("--locale", default="en")
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--ocr-scale", type=float, default=2.0)
    parser.add_argument("--fail-on-warning", action="store_true")
    parser.add_argument(
        "--run-id",
        default="default",
        help="Logical run id; use a new value to exercise caches without idempotent replay",
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _key(relative_path: str, *, run_id: str = "default") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", Path(relative_path).stem.casefold()).strip("-")
    digest_input = relative_path if run_id == "default" else f"{relative_path}\0{run_id}"
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:10]
    return f"regression.{slug[:120] or 'rulebook'}.{digest}"


async def _call(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    _, result = await server.call_tool(name, arguments)
    return result


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.expanduser().resolve()
    home = args.home.expanduser().resolve()
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
    documents = discovery["result"]["documents"]
    report: dict[str, Any] = {
        "root": str(root),
        "home": str(home),
        "edition": args.edition,
        "run_id": args.run_id,
        "document_count": len(documents),
        "documents": [],
        "errors": [],
    }
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
                        "publication_id": source_key,
                        "authority": "supplement",
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
            hits = await _call(
                server,
                "rule_search",
                {
                    "query": Path(relative_path).stem,
                    "source_ids": [source_id],
                    "top_k": 1,
                },
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
                    "source_scoped_search_hit": bool(hits),
                    "seconds": round(perf_counter() - item_started, 3),
                }
            )
        except Exception as error:  # regression harness must report every book
            report["errors"].append(
                {"relative_path": relative_path, "error": f"{type(error).__name__}: {error}"}
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


def _run_token(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]


def main() -> int:
    args = _arguments()
    report = asyncio.run(_run(args))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
