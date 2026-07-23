"""Read-only adapters for the D&D and module-generation skill repositories."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillDocument:
    id: str
    title: str
    source: str
    path: Path
    checksum: str


@dataclass(frozen=True)
class SkillAsset:
    id: str
    source: str
    path: Path
    checksum: str


class SkillCatalog:
    def __init__(self, *, dnd_root: Path, modulegen_root: Path) -> None:
        self._roots = {"dnd": dnd_root, "modulegen": modulegen_root}

    def list(self) -> list[SkillDocument]:
        documents: list[SkillDocument] = []
        for source, root in self._roots.items():
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("SKILL.md")):
                if self._is_install_shadow(path, root):
                    continue
                relative = path.relative_to(root).parent
                suffix = "root" if relative == Path(".") else ".".join(relative.parts)
                documents.append(
                    SkillDocument(
                        id=f"{source}.{suffix}",
                        title=self._title(path, suffix),
                        source=source,
                        path=path,
                        checksum=self._checksum(path),
                    )
                )
        return documents

    def get(self, skill_id: str) -> SkillDocument:
        for document in self.list():
            if document.id == skill_id:
                return document
        raise LookupError(f"unknown skill document {skill_id!r}")

    def read(self, skill_id: str) -> str:
        return self.get(skill_id).path.read_text(encoding="utf-8")

    def assets(self) -> list[SkillAsset]:
        """List text references, data, and templates from installed skill repositories."""
        assets: list[SkillAsset] = []
        text_extensions = {
            ".csv",
            ".json",
            ".md",
            ".rst",
            ".toml",
            ".tsv",
            ".txt",
            ".yaml",
            ".yml",
        }
        asset_directories = {"data", "reference", "references", "template", "templates"}
        for source, root in self._roots.items():
            if not root.is_dir():
                continue
            paths = (
                item
                for item in root.rglob("*")
                if item.is_file() and not self._is_install_shadow(item, root)
            )
            for path in sorted(paths):
                relative = path.relative_to(root).as_posix()
                path_parts = {part.lower() for part in Path(relative).parts}
                is_asset = bool(path_parts & asset_directories) or "template" in path.stem.lower()
                if not is_asset or path.suffix.lower() not in text_extensions:
                    continue
                assets.append(
                    SkillAsset(
                        id=f"{source}:{relative}",
                        source=source,
                        path=path,
                        checksum=self._checksum(path),
                    )
                )
        return assets

    def read_asset(self, asset_id: str) -> str:
        for asset in self.assets():
            if asset.id == asset_id:
                return asset.path.read_text(encoding="utf-8")
        raise LookupError(f"unknown skill asset {asset_id!r}")

    @staticmethod
    def resource_id(asset_id: str) -> str:
        """Encode a slash-containing asset id for a single MCP URI path segment."""
        return base64.urlsafe_b64encode(asset_id.encode("utf-8")).decode("ascii").rstrip("=")

    def read_resource_asset(self, resource_id: str) -> str:
        padding = "=" * (-len(resource_id) % 4)
        try:
            asset_id = base64.urlsafe_b64decode(resource_id + padding).decode("utf-8")
        except (UnicodeDecodeError, ValueError) as error:
            raise LookupError(f"invalid skill asset resource id {resource_id!r}") from error
        return self.read_asset(asset_id)

    def manifest(self) -> list[dict[str, str]]:
        """Return a deterministic workflow-version manifest for event/snapshot provenance."""
        return [
            {
                "id": document.id,
                "source": document.source,
                "checksum": document.checksum,
            }
            for document in self.list()
        ]

    @staticmethod
    def _checksum(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _is_install_shadow(path: Path, root: Path) -> bool:
        """Ignore hidden package-manager mirrors such as nested .agents installs."""
        return any(part.startswith(".") for part in path.relative_to(root).parts)

    @staticmethod
    def _title(path: Path, fallback: str) -> str:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return fallback
