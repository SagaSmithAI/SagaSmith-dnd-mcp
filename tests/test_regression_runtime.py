from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.regression_runtime import (
    decode_mcp_result,
    exception_leaf_messages,
    regression_server_parameters,
)


def test_decode_mcp_result_owns_text_structured_and_error_contracts() -> None:
    text_result = SimpleNamespace(
        content=[SimpleNamespace(text='{"value": 3}')],
        isError=False,
        structuredContent={"ignored": True},
    )
    structured_result = SimpleNamespace(
        content=[],
        isError=False,
        structuredContent={"value": 4},
    )
    error_result = SimpleNamespace(
        content=[SimpleNamespace(text="denied")],
        isError=True,
        structuredContent=None,
    )

    assert decode_mcp_result(text_result) == {"value": 3}
    assert decode_mcp_result(structured_result) == {"value": 4}
    with pytest.raises(RuntimeError, match="denied"):
        decode_mcp_result(error_result)


def test_exception_leaf_messages_flattens_nested_exception_groups() -> None:
    error = ExceptionGroup(
        "outer",
        [ValueError("first"), ExceptionGroup("inner", [RuntimeError("second")])],
    )

    assert exception_leaf_messages(error) == [
        "ValueError: first",
        "RuntimeError: second",
    ]


def test_regression_process_boundary_owns_environment_and_optional_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile = tmp_path / "server.prof"
    monkeypatch.setenv("SAGASMITH_SERVER_PROFILE_OUTPUT", str(profile))

    parameters = regression_server_parameters(
        home=tmp_path / "home",
        auto_seed=False,
        module_root=tmp_path / "modules",
    )

    assert parameters.command == sys.executable
    assert parameters.args == [
        "-m",
        "cProfile",
        "-o",
        str(profile),
        "-m",
        "sagasmith_dnd_mcp.server",
    ]
    assert parameters.env["PYTHONIOENCODING"] == "utf-8"
    assert parameters.env["SAGASMITH_DND_MCP_AUTO_SEED"] == "0"
    assert parameters.env["SAGASMITH_DND_MCP_HOME"] == str((tmp_path / "home").resolve())
    assert parameters.env["SAGASMITH_DND_MCP_MODULE_IMPORT_ROOTS"] == str(
        (tmp_path / "modules").resolve()
    )
