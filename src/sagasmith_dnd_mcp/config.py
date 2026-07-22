"""Configuration and local data paths owned by the MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class McpConfig:
    """Resolve all local state beneath one portable MCP home directory."""

    home: Path
    database_url: str | None
    chroma_url: str | None
    chroma_path_override: Path | None
    dnd_skills_dir: Path
    modulegen_skills_dir: Path
    auto_seed_rules: bool = True
    rule_import_roots: tuple[Path, ...] = ()
    module_import_roots: tuple[Path, ...] = ()
    rule_ocr_enabled: bool = True
    rule_ocr_scale: float = 2.0

    @classmethod
    def from_environment(cls) -> "McpConfig":
        root = _workspace_root()
        home = Path(os.environ.get("SAGASMITH_DND_MCP_HOME", root / ".sagasmith-dnd-mcp"))
        dnd_skills_dir = Path(
            os.environ.get("SAGASMITH_DND_SKILLS_DIR", root / "SagaSmith-dnd-skills")
        ).expanduser().resolve()
        raw_chroma_path = os.environ.get("CHROMA_DB_PATH")
        raw_rule_roots = os.environ.get("SAGASMITH_DND_MCP_RULE_IMPORT_ROOTS")
        raw_module_roots = os.environ.get("SAGASMITH_DND_MCP_MODULE_IMPORT_ROOTS")
        rule_roots = (
            tuple(
                Path(value).expanduser().resolve()
                for value in raw_rule_roots.split(os.pathsep)
                if value.strip()
            )
            if raw_rule_roots is not None
            else (
                root / "reference" / "DnD-Books",
                dnd_skills_dir / "full" / "skills" / "dnd-dm" / "srd",
            )
        )
        module_roots = (
            tuple(
                Path(value).expanduser().resolve()
                for value in raw_module_roots.split(os.pathsep)
                if value.strip()
            )
            if raw_module_roots is not None
            else (root / "test_pdfs",)
        )
        return cls(
            home=home.expanduser().resolve(),
            database_url=os.environ.get("SAGASMITH_DATABASE_URL"),
            chroma_url=os.environ.get("CHROMA_DB_URL"),
            chroma_path_override=(
                Path(raw_chroma_path).expanduser().resolve() if raw_chroma_path else None
            ),
            dnd_skills_dir=dnd_skills_dir,
            modulegen_skills_dir=Path(
                os.environ.get(
                    "SAGASMITH_MODULEGEN_SKILLS_DIR",
                    root / "SagaSmith-module-gen-skills",
                )
            )
            .expanduser()
            .resolve(),
            auto_seed_rules=os.environ.get("SAGASMITH_DND_MCP_AUTO_SEED", "1") == "1",
            rule_import_roots=tuple(path.resolve() for path in rule_roots),
            module_import_roots=tuple(path.resolve() for path in module_roots),
            rule_ocr_enabled=os.environ.get("SAGASMITH_DND_MCP_RULE_OCR", "1") == "1",
            rule_ocr_scale=float(
                os.environ.get("SAGASMITH_DND_MCP_RULE_OCR_SCALE", "2.0")
            ),
        )

    @property
    def database_path(self) -> Path:
        return self.home / "data" / "ttrpgbase.db"

    @property
    def chroma_path(self) -> Path:
        return self.chroma_path_override or self.home / "data" / "chroma_db"

    @property
    def artifacts_dir(self) -> Path:
        return self.home / "artifacts"

    @property
    def modules_dir(self) -> Path:
        return self.artifacts_dir / "modules"

    @property
    def rulebooks_dir(self) -> Path:
        return self.artifacts_dir / "rulebooks"

    @property
    def normalized_rulebooks_dir(self) -> Path:
        return self.artifacts_dir / "normalized-rulebooks"

    @property
    def module_assets_dir(self) -> Path:
        return self.artifacts_dir / "module-assets"

    def prepare(self) -> None:
        for directory in (
            self.database_path.parent,
            self.chroma_path,
            self.modules_dir,
            self.module_assets_dir,
            self.rulebooks_dir,
            self.normalized_rulebooks_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("SAGASMITH_DATA_DIR", str(self.home / "data"))
        if self.chroma_url is None:
            os.environ.setdefault("CHROMA_DB_PATH", str(self.chroma_path))
