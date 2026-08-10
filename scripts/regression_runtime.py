"""Shared process boundary for public stdio MCP regression drivers."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp.client.stdio import StdioServerParameters


def required_core_relock_reason(value: object) -> str:
    """Normalize the explicit audit reason shared by every relock driver."""

    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("relock-core requires --core-relock-reason")
    return normalized


def decode_mcp_result(result: Any) -> Any:
    """Decode the text-or-structured result contract shared by MCP drivers."""

    texts = [item.text for item in result.content if getattr(item, "text", None)]
    message = "\n".join(texts)
    if result.isError:
        raise RuntimeError(message or "MCP tool call failed")
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    if not message:
        return None
    return json.loads(message)


def exception_leaf_messages(error: BaseException) -> list[str]:
    """Flatten nested exception groups into stable driver report messages."""

    nested = getattr(error, "exceptions", ())
    if nested:
        return [
            message
            for child in nested
            for message in exception_leaf_messages(child)
        ]
    return [f"{type(error).__name__}: {error}"]


def regression_server_parameters(
    *,
    home: Path,
    auto_seed: bool,
    module_root: Path | None = None,
    profile_output: str = "",
) -> StdioServerParameters:
    """Build one canonical cold-process environment for every regression driver."""

    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "SAGASMITH_DND_MCP_HOME": str(home.expanduser().resolve()),
            "SAGASMITH_DND_MCP_AUTO_SEED": "1" if auto_seed else "0",
        }
    )
    if module_root is not None:
        env["SAGASMITH_DND_MCP_MODULE_IMPORT_ROOTS"] = str(
            module_root.expanduser().resolve()
        )
    server_args = ["-m", "sagasmith_dnd_mcp.server"]
    resolved_profile_output = (
        profile_output.strip()
        or str(env.get("SAGASMITH_SERVER_PROFILE_OUTPUT") or "").strip()
    )
    if resolved_profile_output:
        server_args = [
            "-m",
            "cProfile",
            "-o",
            resolved_profile_output,
            *server_args,
        ]
    return StdioServerParameters(
        command=sys.executable,
        args=server_args,
        cwd=repo,
        env=env,
    )
