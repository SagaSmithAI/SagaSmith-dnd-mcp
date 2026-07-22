"""SagaSmith D&D MCP server."""

from __future__ import annotations

from typing import Any

__all__ = ["create_server"]


def __getattr__(name: str) -> Any:
    """Export the server factory without double-loading ``server`` under ``-m``."""
    if name == "create_server":
        from sagasmith_dnd_mcp.server import create_server

        return create_server
    raise AttributeError(name)
