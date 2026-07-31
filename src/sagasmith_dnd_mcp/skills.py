"""Read-only adapters for the D&D and module-generation skill repositories."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

TEXT_ASSET_EXTENSIONS = {
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
TEXT_ASSET_DIRECTORIES = {
    "data",
    "reference",
    "references",
    "template",
    "templates",
}


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
        self._documents: tuple[SkillDocument, ...] | None = None
        self._document_by_id: dict[str, SkillDocument] = {}
        self._assets: tuple[SkillAsset, ...] | None = None
        self._asset_by_id: dict[str, SkillAsset] = {}

    def refresh(self) -> None:
        """Discard filesystem indexes before an explicit installation reload."""

        self._documents = None
        self._document_by_id = {}
        self._assets = None
        self._asset_by_id = {}

    def root(self, source: str) -> Path:
        """Return one configured repository root without exposing mutation."""

        try:
            return self._roots[source]
        except KeyError as error:
            raise LookupError(f"unknown skill source {source!r}") from error

    def list(self) -> list[SkillDocument]:
        if self._documents is not None:
            return list(self._documents)
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
        self._documents = tuple(documents)
        self._document_by_id = {document.id: document for document in documents}
        return list(self._documents)

    def get(self, skill_id: str) -> SkillDocument:
        if self._documents is None:
            self.list()
        try:
            return self._document_by_id[skill_id]
        except KeyError as error:
            raise LookupError(f"unknown skill document {skill_id!r}") from error

    def read(self, skill_id: str) -> str:
        return self.get(skill_id).path.read_text(encoding="utf-8")

    def assets(self) -> list[SkillAsset]:
        """List text references, data, and templates from installed skill repositories."""
        if self._assets is not None:
            return list(self._assets)
        assets: list[SkillAsset] = []
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
                is_asset = (
                    bool(path_parts & TEXT_ASSET_DIRECTORIES)
                    or "template" in path.stem.lower()
                )
                if (
                    not is_asset
                    or path.suffix.lower() not in TEXT_ASSET_EXTENSIONS
                ):
                    continue
                assets.append(
                    SkillAsset(
                        id=f"{source}:{relative}",
                        source=source,
                        path=path,
                        checksum=self._checksum(path),
                    )
                )
        self._assets = tuple(assets)
        self._asset_by_id = {asset.id: asset for asset in assets}
        return list(self._assets)

    def read_asset(self, asset_id: str) -> str:
        return self.get_asset(asset_id).path.read_text(encoding="utf-8")

    def get_asset(self, asset_id: str) -> SkillAsset:
        """Resolve one installed text asset together with its stable checksum."""

        cached = self._asset_by_id.get(asset_id)
        if cached is not None:
            return cached
        if self._assets is not None:
            raise LookupError(f"unknown skill asset {asset_id!r}")
        source, separator, relative_value = asset_id.partition(":")
        root = self._roots.get(source)
        if not separator or root is None or not relative_value:
            raise LookupError(f"unknown skill asset {asset_id!r}")
        root = root.resolve()
        relative = Path(*PurePosixPath(relative_value).parts)
        if relative.is_absolute() or ".." in relative.parts:
            raise LookupError(f"unknown skill asset {asset_id!r}")
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise LookupError(f"unknown skill asset {asset_id!r}") from error
        relative_parts = {part.lower() for part in relative.parts}
        is_asset = (
            bool(relative_parts & TEXT_ASSET_DIRECTORIES)
            or "template" in path.stem.lower()
        )
        if (
            not path.is_file()
            or self._is_install_shadow(path, root)
            or not is_asset
            or path.suffix.lower() not in TEXT_ASSET_EXTENSIONS
        ):
            raise LookupError(f"unknown skill asset {asset_id!r}")
        asset = SkillAsset(
            id=asset_id,
            source=source,
            path=path,
            checksum=self._checksum(path),
        )
        self._asset_by_id[asset_id] = asset
        return asset

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

    def outline(self, *, kind: str, identifier: str) -> dict[str, Any]:
        """Return a compact heading index without loading a whole skill document."""

        text = self._text(kind=kind, identifier=identifier)
        headings = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
            if match is None:
                continue
            headings.append(
                {
                    "level": len(match.group(1)),
                    "title": match.group(2),
                    "line": line_number,
                }
            )
        return {
            "kind": kind,
            "identifier": identifier,
            "bytes": len(text.encode("utf-8")),
            "approx_tokens": (len(text.encode("utf-8")) + 3) // 4,
            "headings": headings,
        }

    def section(
        self,
        *,
        kind: str,
        identifier: str,
        heading: str,
        max_chars: int = 12_000,
    ) -> dict[str, Any]:
        """Read one Markdown section with an explicit bounded-output contract."""

        if max_chars < 256 or max_chars > 20_000:
            raise ValueError("max_chars must be between 256 and 20000")
        text = self._text(kind=kind, identifier=identifier)
        lines = text.splitlines()
        candidates: list[tuple[int, int, str]] = []
        wanted = heading.strip().casefold()
        for index, line in enumerate(lines):
            match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
            if match is None:
                continue
            title = match.group(2).strip()
            if title.casefold() == wanted:
                candidates = [(index, len(match.group(1)), title)]
                break
            if wanted and wanted in title.casefold():
                candidates.append((index, len(match.group(1)), title))
        if not candidates:
            raise LookupError(
                f"unknown heading {heading!r} in {kind} document {identifier!r}"
            )
        if len(candidates) > 1:
            raise LookupError(
                f"ambiguous heading {heading!r}; matches: "
                + ", ".join(item[2] for item in candidates[:10])
            )
        start, level, title = candidates[0]
        end = len(lines)
        for index in range(start + 1, len(lines)):
            match = re.match(r"^(#{1,6})\s+", lines[index])
            if match is not None and len(match.group(1)) <= level:
                end = index
                break
        content = "\n".join(lines[start:end]).strip() + "\n"
        truncated = len(content) > max_chars
        if truncated:
            content = content[:max_chars].rstrip() + "\n"
        return {
            "kind": kind,
            "identifier": identifier,
            "heading": title,
            "line_start": start + 1,
            "line_end": end,
            "truncated": truncated,
            "content": content,
        }

    def search(
        self,
        *,
        kind: str,
        query: str,
        identifier: str | None = None,
        limit: int = 8,
        context_chars: int = 900,
    ) -> dict[str, Any]:
        """Search installed guidance and return bounded, line-addressed excerpts."""

        if not query.strip():
            raise ValueError("query is required")
        if limit < 1 or limit > 20:
            raise ValueError("limit must be between 1 and 20")
        if context_chars < 200 or context_chars > 2_000:
            raise ValueError("context_chars must be between 200 and 2000")
        terms = [item.casefold() for item in query.split() if item.strip()]
        documents: list[tuple[str, str]] = []
        if identifier is not None:
            documents.append((identifier, self._text(kind=kind, identifier=identifier)))
        elif kind == "skill":
            documents.extend((item.id, self.read(item.id)) for item in self.list())
        elif kind == "asset":
            documents.extend((item.id, self.read_asset(item.id)) for item in self.assets())
        else:
            raise ValueError("kind must be 'skill' or 'asset'")
        matches: list[dict[str, Any]] = []
        for document_id, text in documents:
            lines = text.splitlines()
            current_heading = ""
            for line_number, line in enumerate(lines, start=1):
                heading_match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
                if heading_match is not None:
                    current_heading = heading_match.group(1)
                lowered = line.casefold()
                score = sum(term in lowered for term in terms)
                if score == 0:
                    continue
                start = max(0, line_number - 3)
                end = min(len(lines), line_number + 2)
                excerpt = "\n".join(lines[start:end]).strip()
                matches.append(
                    {
                        "identifier": document_id,
                        "heading": current_heading,
                        "line": line_number,
                        "score": score,
                        "excerpt": excerpt[:context_chars],
                    }
                )
        matches.sort(key=lambda item: (-item["score"], item["identifier"], item["line"]))
        return {
            "kind": kind,
            "query": query,
            "matches": matches[:limit],
            "truncated": len(matches) > limit,
        }

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

    def _text(self, *, kind: str, identifier: str) -> str:
        if kind == "skill":
            return self.read(identifier)
        if kind == "asset":
            return self.read_asset(identifier)
        raise ValueError("kind must be 'skill' or 'asset'")

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
