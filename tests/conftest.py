"""Shared test fixtures for fast, isolated current-schema databases."""

from __future__ import annotations

import gc
import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from sagasmith_dnd_mcp.storage import SagaSmithStorage


def _backup_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_connection:
        with sqlite3.connect(target) as target_connection:
            source_connection.backup(target_connection)


class _TestDatabaseTemplates:
    """Lazily build current-schema database templates for isolated test copies."""

    def __init__(self, root: Path, *, enabled: bool) -> None:
        self.root = root
        self.enabled = enabled
        self.building = False
        self.workspace = Path(__file__).resolve().parents[2]
        self._templates: dict[bool, Path] = {}

    def _config(self, home: Path, *, auto_seed_rules: bool) -> McpConfig:
        return McpConfig(
            home=home,
            database_url=None,
            chroma_url=None,
            chroma_path_override=None,
            dnd_skills_dir=self.workspace / "SagaSmith-dnd-skills",
            modulegen_skills_dir=self.workspace / "SagaSmith-module-gen-skills",
            auto_seed_rules=auto_seed_rules,
        )

    def get(self, auto_seed_rules: bool) -> Path:
        existing = self._templates.get(auto_seed_rules)
        if existing is not None:
            return existing
        if not self.enabled:
            raise RuntimeError("test database templates are disabled")

        config = self._config(
            self.root / f"bootstrap-{int(auto_seed_rules)}",
            auto_seed_rules=auto_seed_rules,
        )
        if auto_seed_rules:
            config.database_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.get(False), config.database_path)

        server: FastMCP | None = None
        self.building = True
        try:
            server = create_server(config)
            template = self.root / f"template-{int(auto_seed_rules)}.db"
            _backup_sqlite(config.database_path, template)
            self._templates[auto_seed_rules] = template
            return template
        finally:
            if server is not None:
                del server
            self.building = False
            gc.collect()


@pytest.fixture(scope="session")
def _test_database_templates(
    tmp_path_factory: pytest.TempPathFactory,
) -> _TestDatabaseTemplates:
    return _TestDatabaseTemplates(
        tmp_path_factory.mktemp("database-templates"),
        enabled=os.environ.get("SAGASMITH_TEST_DB_TEMPLATES", "1") != "0",
    )


@pytest.fixture(autouse=True)
def _use_test_database_template(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    _test_database_templates: _TestDatabaseTemplates,
) -> None:
    original_migrate = SagaSmithStorage.migrate
    templated_paths: set[Path] = set()
    fresh_database = request.node.get_closest_marker("fresh_database") is not None

    def migrate(storage: SagaSmithStorage) -> None:
        if _test_database_templates.building:
            original_migrate(storage)
            return
        if (
            _test_database_templates.enabled
            and not fresh_database
            and storage.config.database_url is None
        ):
            target = storage.config.database_path.resolve()
            if target in templated_paths and target.exists():
                return
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(
                    _test_database_templates.get(storage.config.auto_seed_rules),
                    target,
                )
                templated_paths.add(target)
                return
        original_migrate(storage)

    monkeypatch.setattr(SagaSmithStorage, "migrate", migrate)
