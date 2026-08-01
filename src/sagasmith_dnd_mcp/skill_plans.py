"""Phase- and exposure-aware bounded Skill reading plans."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sagasmith_dnd_mcp.skills import SkillCatalog

SKILL_PLAN_ASSET_ID = "dnd:full/data/skill-plan.v1.json"
SUPPORTED_PHASES = ("lobby", "play", "combat")
LOAD_POLICIES = {"session", "phase", "tool_group", "operation"}
VISIBILITIES = {"public", "member", "dm", "owner", "local_admin"}
RESULT_OPERATION_PHASES = {
    "combat_start:started": frozenset({"combat"}),
    "combat_end:closed": frozenset({"play"}),
    "continuity_context:npc_turn": frozenset({"play", "combat"}),
    "continuity_context:actor_turn": frozenset({"play", "combat"}),
    "continuity_context:audience_render": frozenset({"play", "combat"}),
    "continuity_context:faction_turn": frozenset({"play", "combat"}),
    "continuity_context:source_interpretation": frozenset(
        {"lobby", "play", "combat"}
    ),
    "continuity_context:bounded_ruling": frozenset({"lobby", "play", "combat"}),
}
RESULT_OPERATION_IDS = frozenset(RESULT_OPERATION_PHASES)


class SkillPlanError(ValueError):
    """Raised when an installed Skill-plan manifest is internally inconsistent."""


@dataclass(frozen=True)
class ResolvedPlanDocument:
    kind: str
    identifier: str
    action: str
    heading: str | None
    max_chars: int
    checksum: str
    chars: int
    approx_tokens: int

    @property
    def read_key(self) -> tuple[str, str, str, str]:
        return (
            self.kind,
            self.identifier,
            self.action,
            self.heading or "",
        )

    def public(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "identifier": self.identifier,
            "action": self.action,
            "max_chars": self.max_chars,
            "checksum": self.checksum,
            "chars": self.chars,
            "approx_tokens": self.approx_tokens,
        }
        if self.heading is not None:
            result["heading"] = self.heading
        return result


class SkillReadTracker:
    """Remember successful bounded reads for one MCP process session."""

    def __init__(
        self,
        *,
        max_sessions: int = 2_048,
        max_documents_per_session: int = 256,
    ) -> None:
        if max_sessions < 1 or max_documents_per_session < 1:
            raise ValueError("Skill read-tracker limits must be positive")
        self.max_sessions = max_sessions
        self.max_documents_per_session = max_documents_per_session
        self._reads: OrderedDict[
            str,
            OrderedDict[tuple[str, str, str, str], str],
        ] = OrderedDict()

    def mark(
        self,
        *,
        session_key: str,
        document: ResolvedPlanDocument,
    ) -> dict[str, Any]:
        reads = self._reads.setdefault(session_key, OrderedDict())
        self._reads.move_to_end(session_key)
        reads[document.read_key] = document.checksum
        reads.move_to_end(document.read_key)
        while len(reads) > self.max_documents_per_session:
            reads.popitem(last=False)
        while len(self._reads) > self.max_sessions:
            self._reads.popitem(last=False)
        return {
            **document.public(),
            "session_key_hash": hashlib.sha256(
                session_key.encode("utf-8")
            ).hexdigest()[:16],
        }

    def status(
        self,
        *,
        session_key: str,
        document: ResolvedPlanDocument,
    ) -> str:
        prior = self._reads.get(session_key, {}).get(document.read_key)
        if prior is None:
            return "unread"
        if prior != document.checksum:
            return "invalidated"
        return "satisfied"


class SkillPlanCatalog:
    """Load, validate, and expand the installed Skill-plan manifest."""

    def __init__(
        self,
        *,
        skills: SkillCatalog,
        expected_tool_groups: Iterable[str] | Mapping[str, Any],
        expected_operation_phases: Mapping[str, Iterable[str]],
        manifest_asset_id: str = SKILL_PLAN_ASSET_ID,
    ) -> None:
        self.skills = skills
        self.manifest_asset_id = manifest_asset_id
        self.required = (
            self.skills.root("dnd") / "full" / "SKILL.md"
        ).is_file()
        self.expected_tool_groups = frozenset(expected_tool_groups)
        self.expected_operation_phases = {
            operation: frozenset(phases)
            for operation, phases in expected_operation_phases.items()
        }
        self.expected_group_tools = (
            {
                group_id: frozenset(getattr(group, "tools", group))
                for group_id, group in expected_tool_groups.items()
            }
            if isinstance(expected_tool_groups, Mapping)
            else None
        )
        self.expected_group_access = (
            {
                group_id: {
                    "phase": str(getattr(group, "phase", "")),
                    "requires_campaign": bool(
                        getattr(group, "requires_campaign", True)
                    ),
                    "local_only": bool(getattr(group, "local_only", False)),
                    "roles": frozenset(getattr(group, "roles", ())),
                }
                for group_id, group in expected_tool_groups.items()
            }
            if isinstance(expected_tool_groups, Mapping)
            else None
        )
        self._manifest: dict[str, Any] | None = None
        self._manifest_checksum: str | None = None
        self._documents: dict[str, tuple[ResolvedPlanDocument, ...]] = {}
        self._load_error: str | None = None
        self._skill_documents: dict[str, Any] = {}
        self._asset_documents: dict[str, Any] = {}
        self.reload()

    @property
    def available(self) -> bool:
        return self._manifest is not None

    @property
    def load_error(self) -> str | None:
        return self._load_error

    @property
    def manifest_checksum(self) -> str | None:
        return self._manifest_checksum

    @property
    def budgets(self) -> dict[str, int]:
        if self._manifest is None:
            return {}
        return dict(self._manifest["budgets"])

    def reload(self) -> None:
        """Reload the source manifest so checksum changes invalidate prior reads."""

        self.skills.refresh()
        self._skill_documents = {}
        self._asset_documents = {}
        try:
            manifest_asset = self.skills.get_asset(self.manifest_asset_id)
        except LookupError:
            self._manifest = None
            self._manifest_checksum = None
            self._documents = {}
            self._load_error = (
                f"installed Skills do not provide {self.manifest_asset_id}"
            )
            return
        self._asset_documents[manifest_asset.id] = manifest_asset
        text = manifest_asset.path.read_text(encoding="utf-8")
        try:
            value = json.loads(text)
            if not isinstance(value, dict):
                raise SkillPlanError("Skill-plan manifest must be a JSON object")
            documents = self._validate(value)
        except (json.JSONDecodeError, LookupError, SkillPlanError, ValueError) as error:
            self._manifest = None
            self._manifest_checksum = None
            self._documents = {}
            self._load_error = str(error)
            return
        self._manifest = value
        self._manifest_checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self._documents = documents
        self._load_error = None

    def resolve_document(
        self,
        *,
        kind: str,
        identifier: str,
        action: str,
        heading: str | None,
    ) -> ResolvedPlanDocument | None:
        """Resolve a successful read to the document expected by a plan."""

        for documents in self._documents.values():
            for document in documents:
                if (
                    document.kind == kind
                    and document.identifier == identifier
                    and document.action == action
                    and document.heading == heading
                ):
                    return document
        return None

    def plan(
        self,
        *,
        phase: str,
        role: str,
        loaded_tool_groups: Iterable[str],
        session_key: str,
        tracker: SkillReadTracker,
        campaign_id: str | None = None,
        exposure_id: str | None = None,
        operation: str | None = None,
        focus_tool_groups: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Return unread documents plus satisfied and conditional Skill groups."""

        if phase not in SUPPORTED_PHASES:
            raise SkillPlanError(f"unsupported Skill-plan phase: {phase}")
        if self._manifest is None:
            return {
                "available": False,
                "required": self.required,
                "schema_version": None,
                "manifest_asset_id": self.manifest_asset_id,
                "error": self._load_error,
                "phase": phase,
                "role": role,
                "campaign_id": campaign_id,
                "exposure_id": exposure_id,
                "required_now": [],
                "already_satisfied": [],
                "invalidated": [],
                "conditional": [],
            }
        loaded = set(loaded_tool_groups)
        unknown = loaded - self.expected_tool_groups
        if unknown:
            raise SkillPlanError(
                "unknown loaded tool groups: " + ", ".join(sorted(unknown))
            )
        manifest = self._manifest
        seeds = set(manifest["core_groups"])
        seeds.update(manifest["phase_baselines"][phase])
        selected_tool_groups = (
            set(focus_tool_groups)
            if focus_tool_groups is not None
            else loaded
        )
        unknown_focus = selected_tool_groups - self.expected_tool_groups
        if unknown_focus:
            raise SkillPlanError(
                "unknown focused tool groups: "
                + ", ".join(sorted(unknown_focus))
            )
        for tool_group_id in selected_tool_groups:
            binding = manifest["tool_group_bindings"][tool_group_id]
            seeds.update(binding["required"])
        if operation:
            if operation not in self.expected_operation_phases:
                raise SkillPlanError(
                    f"unknown Skill-plan operation: {operation}"
                )
            if phase not in self.expected_operation_phases[operation]:
                raise SkillPlanError(
                    f"Skill-plan operation {operation!r} is unavailable in "
                    f"phase {phase!r}"
                )
            seeds.update(manifest["operation_bindings"].get(operation, []))
        unavailable_skill_groups = sorted(
            group_id
            for group_id in seeds
            if not self._role_allows(
                role,
                manifest["groups"][group_id]["visibility"],
            )
        )
        seeds.difference_update(unavailable_skill_groups)
        ordered_groups = self._dependency_order(seeds)

        required_now: list[dict[str, Any]] = []
        already_satisfied: list[str] = []
        invalidated: list[dict[str, Any]] = []
        for group_id in ordered_groups:
            group = manifest["groups"][group_id]
            document_states = [
                (document, tracker.status(session_key=session_key, document=document))
                for document in self._documents[group_id]
            ]
            if document_states and all(
                state == "satisfied" for _, state in document_states
            ):
                already_satisfied.append(group_id)
                continue
            unread_documents = []
            for document, state in document_states:
                if state == "satisfied":
                    continue
                if state == "invalidated":
                    invalidated.append(
                        {
                            "skill_group": group_id,
                            **document.public(),
                        }
                    )
                unread_documents.append(document.public())
            required_now.append(
                {
                    "skill_group": group_id,
                    "load_policy": group["load_policy"],
                    "visibility": group["visibility"],
                    "reason": group["reason"],
                    "documents": unread_documents,
                }
            )

        conditional = []
        relevant_tool_groups = loaded | selected_tool_groups
        for operation_id, group_ids in sorted(
            manifest["operation_bindings"].items()
        ):
            tool_id = operation_id.split(":", 1)[0]
            if relevant_tool_groups and not any(
                tool_id
                in manifest["tool_group_bindings"][group_id]["tools"]
                for group_id in relevant_tool_groups
            ):
                continue
            visible_groups = [
                group_id
                for group_id in group_ids
                if self._role_allows(
                    role,
                    manifest["groups"][group_id]["visibility"],
                )
            ]
            if visible_groups:
                conditional.append(
                    {
                        "operation": operation_id,
                        "skill_groups": visible_groups,
                    }
                )
        return {
            "available": True,
            "required": self.required,
            "schema_version": manifest["schema_version"],
            "manifest_asset_id": self.manifest_asset_id,
            "manifest_checksum": self._manifest_checksum,
            "phase": phase,
            "role": role,
            "campaign_id": campaign_id,
            "exposure_id": exposure_id,
            "loaded_tool_groups": sorted(loaded),
            "focused_tool_groups": sorted(selected_tool_groups),
            "operation": operation,
            "required_now": required_now,
            "already_satisfied": already_satisfied,
            "invalidated": invalidated,
            "unavailable_skill_groups": unavailable_skill_groups,
            "conditional": conditional,
            "budgets": deepcopy(manifest["budgets"]),
            "runtime_authority": (
                "Skill plans guide context loading; MCP phase, role, revision, "
                "idempotency, source, and transaction validation remain authoritative."
            ),
        }

    def summary(self) -> dict[str, Any]:
        if self._manifest is None:
            return {
                "available": False,
                "required": self.required,
                "manifest_asset_id": self.manifest_asset_id,
                "error": self._load_error,
            }
        return {
            "available": True,
            "required": self.required,
            "manifest_asset_id": self.manifest_asset_id,
            "schema_version": self._manifest["schema_version"],
            "manifest_checksum": self._manifest_checksum,
            "group_count": len(self._manifest["groups"]),
            "tool_group_count": len(self._manifest["tool_group_bindings"]),
            "operation_binding_count": len(
                self._manifest["operation_bindings"]
            ),
            "budgets": deepcopy(self._manifest["budgets"]),
        }

    def _validate(
        self,
        manifest: Mapping[str, Any],
    ) -> dict[str, tuple[ResolvedPlanDocument, ...]]:
        if manifest.get("schema_version") != 1:
            raise SkillPlanError("Skill-plan schema_version must equal 1")
        budgets = self._mapping(manifest.get("budgets"), "budgets")
        expected_budget_keys = {
            "core_chars",
            "phase_chars",
            "tool_group_chars",
            "automatic_chars",
        }
        if set(budgets) != expected_budget_keys or any(
            not isinstance(value, int) or value < 256
            for value in budgets.values()
        ):
            raise SkillPlanError(
                "Skill-plan budgets must define positive core_chars, phase_chars, "
                "tool_group_chars, and automatic_chars"
            )
        groups = self._mapping(manifest.get("groups"), "groups")
        core_groups = self._string_list(manifest.get("core_groups"), "core_groups")
        phase_baselines = self._mapping(
            manifest.get("phase_baselines"),
            "phase_baselines",
        )
        if set(phase_baselines) != set(SUPPORTED_PHASES):
            raise SkillPlanError(
                "phase_baselines must define lobby, play, and combat"
            )
        tool_bindings = self._mapping(
            manifest.get("tool_group_bindings"),
            "tool_group_bindings",
        )
        if set(tool_bindings) != self.expected_tool_groups:
            missing = sorted(self.expected_tool_groups - set(tool_bindings))
            extra = sorted(set(tool_bindings) - self.expected_tool_groups)
            raise SkillPlanError(
                f"tool_group_bindings coverage mismatch; missing={missing}, extra={extra}"
            )
        operation_bindings = self._mapping(
            manifest.get("operation_bindings"),
            "operation_bindings",
        )

        resolved: dict[str, tuple[ResolvedPlanDocument, ...]] = {}
        for group_id, raw_group in groups.items():
            if not isinstance(group_id, str) or not group_id:
                raise SkillPlanError("Skill group ids must be non-empty strings")
            group = self._mapping(raw_group, f"groups.{group_id}")
            if group.get("load_policy") not in LOAD_POLICIES:
                raise SkillPlanError(
                    f"groups.{group_id}.load_policy is unsupported"
                )
            if group.get("visibility") not in VISIBILITIES:
                raise SkillPlanError(
                    f"groups.{group_id}.visibility is unsupported"
                )
            if not str(group.get("reason") or "").strip():
                raise SkillPlanError(f"groups.{group_id}.reason is required")
            dependencies = self._string_list(
                group.get("depends_on", []),
                f"groups.{group_id}.depends_on",
            )
            group["depends_on"] = dependencies
            documents = group.get("documents")
            if not isinstance(documents, list) or not documents:
                raise SkillPlanError(
                    f"groups.{group_id}.documents must be a non-empty list"
                )
            resolved[group_id] = tuple(
                self._resolve_manifest_document(
                    document,
                    field=f"groups.{group_id}.documents[{index}]",
                )
                for index, document in enumerate(documents)
            )

        known_groups = set(groups)
        for group_id, group in groups.items():
            missing = set(group["depends_on"]) - known_groups
            if missing:
                raise SkillPlanError(
                    f"groups.{group_id} has unknown dependencies: "
                    + ", ".join(sorted(missing))
                )
            visibility_rank = {
                "public": 0,
                "member": 1,
                "dm": 2,
                "owner": 3,
                "local_admin": 4,
            }
            elevated_dependencies = [
                dependency
                for dependency in group["depends_on"]
                if visibility_rank[groups[dependency]["visibility"]]
                > visibility_rank[group["visibility"]]
            ]
            if elevated_dependencies:
                raise SkillPlanError(
                    f"groups.{group_id} depends on more privileged guidance: "
                    + ", ".join(sorted(elevated_dependencies))
                )
        for name, values in [
            ("core_groups", core_groups),
            *(
                (f"phase_baselines.{phase}", self._string_list(value, phase))
                for phase, value in phase_baselines.items()
            ),
        ]:
            missing = set(values) - known_groups
            if missing:
                raise SkillPlanError(
                    f"{name} contains unknown Skill groups: "
                    + ", ".join(sorted(missing))
                )
        for tool_group_id, raw_binding in tool_bindings.items():
            binding = self._mapping(
                raw_binding,
                f"tool_group_bindings.{tool_group_id}",
            )
            required = self._string_list(
                binding.get("required"),
                f"tool_group_bindings.{tool_group_id}.required",
            )
            tools = self._string_list(
                binding.get("tools"),
                f"tool_group_bindings.{tool_group_id}.tools",
            )
            if not tools:
                raise SkillPlanError(
                    f"tool_group_bindings.{tool_group_id}.tools cannot be empty"
                )
            if (
                self.expected_group_tools is not None
                and set(tools) != self.expected_group_tools[tool_group_id]
            ):
                missing_tools = sorted(
                    self.expected_group_tools[tool_group_id] - set(tools)
                )
                extra_tools = sorted(
                    set(tools) - self.expected_group_tools[tool_group_id]
                )
                raise SkillPlanError(
                    f"tool_group_bindings.{tool_group_id}.tools mismatch; "
                    f"missing={missing_tools}, extra={extra_tools}"
                )
            missing = set(required) - known_groups
            if missing:
                raise SkillPlanError(
                    f"tool_group_bindings.{tool_group_id} contains unknown groups: "
                    + ", ".join(sorted(missing))
                )
            if self.expected_group_access is not None:
                expected_visibility = self._expected_group_visibility(
                    self.expected_group_access[tool_group_id]
                )
                visibility_rank = {
                    "public": 0,
                    "member": 1,
                    "dm": 2,
                    "owner": 3,
                    "local_admin": 4,
                }
                highest_direct_visibility = max(
                    (
                        visibility_rank[groups[group_id]["visibility"]]
                        for group_id in required
                    ),
                    default=-1,
                )
                if (
                    highest_direct_visibility
                    != visibility_rank[expected_visibility]
                ):
                    raise SkillPlanError(
                        f"tool_group_bindings.{tool_group_id}.required must include "
                        f"{expected_visibility!r} guidance matching the tool-group "
                        "access boundary"
                    )
                bound_phase_groups = {
                    group_id.removeprefix("phase.")
                    for group_id in self._ordered_from(
                        required,
                        groups=groups,
                    )
                    if group_id.startswith("phase.")
                }
                invalid_phases = bound_phase_groups - {
                    self.expected_group_access[tool_group_id]["phase"]
                }
                if invalid_phases:
                    raise SkillPlanError(
                        f"tool_group_bindings.{tool_group_id}.required depends "
                        "on incompatible phase guidance: "
                        + ", ".join(sorted(invalid_phases))
                    )
        for operation, raw_groups in operation_bindings.items():
            if not isinstance(operation, str) or ":" not in operation:
                raise SkillPlanError(
                    "operation binding keys must use '<tool>:<selector>'"
                )
            if operation not in self.expected_operation_phases:
                raise SkillPlanError(
                    f"operation_bindings.{operation} is not a public selector "
                    "or an approved result transition"
                )
            values = self._string_list(
                raw_groups,
                f"operation_bindings.{operation}",
            )
            missing = set(values) - known_groups
            if missing:
                raise SkillPlanError(
                    f"operation_bindings.{operation} contains unknown groups: "
                    + ", ".join(sorted(missing))
                )
            bound_phase_groups = {
                group_id.removeprefix("phase.")
                for group_id in self._ordered_from(values, groups=groups)
                if group_id.startswith("phase.")
            }
            invalid_phases = (
                bound_phase_groups
                - self.expected_operation_phases[operation]
            )
            if invalid_phases:
                raise SkillPlanError(
                    f"operation_bindings.{operation} depends on incompatible "
                    "phase guidance: "
                    + ", ".join(sorted(invalid_phases))
                )

        self._assert_acyclic(groups)
        self._assert_budgets(
            manifest=manifest,
            resolved=resolved,
        )
        return resolved

    def _resolve_manifest_document(
        self,
        value: Any,
        *,
        field: str,
    ) -> ResolvedPlanDocument:
        document = self._mapping(value, field)
        kind = str(document.get("kind") or "")
        identifier = str(document.get("identifier") or "")
        action = str(document.get("action") or "")
        heading = (
            str(document["heading"])
            if document.get("heading") is not None
            else None
        )
        max_chars = document.get("max_chars")
        if kind not in {"skill", "asset"}:
            raise SkillPlanError(f"{field}.kind must be skill or asset")
        if not identifier:
            raise SkillPlanError(f"{field}.identifier is required")
        if action not in {"read", "section"}:
            raise SkillPlanError(f"{field}.action must be read or section")
        if action == "section" and not heading:
            raise SkillPlanError(f"{field}.heading is required for section")
        if action == "read" and heading is not None:
            raise SkillPlanError(f"{field}.heading is invalid for read")
        if not isinstance(max_chars, int) or not 256 <= max_chars <= 20_000:
            raise SkillPlanError(
                f"{field}.max_chars must be between 256 and 20000"
            )
        if kind == "skill":
            source = self._skill_documents.get(identifier)
            if source is None:
                try:
                    source = self.skills.get(identifier)
                except LookupError:
                    source = None
                else:
                    self._skill_documents[identifier] = source
        else:
            source = self._asset_documents.get(identifier)
            if source is None:
                try:
                    source = self.skills.get_asset(identifier)
                except LookupError:
                    source = None
                else:
                    self._asset_documents[identifier] = source
        if source is None:
            raise SkillPlanError(f"{field}.identifier is not installed: {identifier}")
        text = source.path.read_text(encoding="utf-8")
        checksum = source.checksum
        if action == "section":
            section = self.skills.section(
                kind=kind,
                identifier=identifier,
                heading=heading or "",
                max_chars=20_000,
            )
            if section["truncated"]:
                raise SkillPlanError(
                    f"{field} exceeds the maximum supported section size"
                )
            text = section["content"]
        chars = len(text)
        if chars > max_chars:
            raise SkillPlanError(
                f"{field} contains {chars} characters, above max_chars={max_chars}"
            )
        return ResolvedPlanDocument(
            kind=kind,
            identifier=identifier,
            action=action,
            heading=heading,
            max_chars=max_chars,
            checksum=checksum,
            chars=chars,
            approx_tokens=(len(text.encode("utf-8")) + 3) // 4,
        )

    def _dependency_order(self, seeds: Iterable[str]) -> list[str]:
        assert self._manifest is not None
        groups = self._manifest["groups"]
        ordered: list[str] = []
        visited: set[str] = set()

        def visit(group_id: str) -> None:
            if group_id in visited:
                return
            visited.add(group_id)
            for dependency in groups[group_id]["depends_on"]:
                visit(dependency)
            ordered.append(group_id)

        for group_id in sorted(set(seeds)):
            visit(group_id)
        return ordered

    @staticmethod
    def _mapping(value: Any, field: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise SkillPlanError(f"{field} must be an object")
        return value

    @staticmethod
    def _string_list(value: Any, field: str) -> list[str]:
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise SkillPlanError(f"{field} must be a list of non-empty strings")
        if len(set(value)) != len(value):
            raise SkillPlanError(f"{field} cannot contain duplicates")
        return list(value)

    @staticmethod
    def _assert_acyclic(groups: Mapping[str, Any]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(group_id: str) -> None:
            if group_id in visiting:
                raise SkillPlanError(
                    f"Skill group dependency cycle includes {group_id}"
                )
            if group_id in visited:
                return
            visiting.add(group_id)
            for dependency in groups[group_id]["depends_on"]:
                visit(dependency)
            visiting.remove(group_id)
            visited.add(group_id)

        for group_id in groups:
            visit(group_id)

    @staticmethod
    def _role_allows(role: str, visibility: str) -> bool:
        if role == "local_admin":
            return True
        allowed = {
            "public": {"public"},
            "observer": {"public", "member"},
            "player": {"public", "member"},
            "dm": {"public", "member", "dm"},
            "owner": {"public", "member", "dm", "owner"},
        }
        return visibility in allowed.get(role, {"public"})

    @staticmethod
    def _expected_group_visibility(access: Mapping[str, Any]) -> str:
        if access["local_only"]:
            return "local_admin"
        roles = set(access["roles"])
        if roles:
            role_rank = {
                "observer": ("member", 1),
                "player": ("member", 1),
                "dm": ("dm", 2),
                "owner": ("owner", 3),
            }
            known = [role_rank[role] for role in roles if role in role_rank]
            if known:
                return min(known, key=lambda item: item[1])[0]
            return "owner"
        return "member" if access["requires_campaign"] else "public"

    def _assert_budgets(
        self,
        *,
        manifest: Mapping[str, Any],
        resolved: Mapping[str, tuple[ResolvedPlanDocument, ...]],
    ) -> None:
        budgets = manifest["budgets"]
        groups = manifest["groups"]

        def chars(group_ids: Iterable[str]) -> int:
            return document_chars(
                self._ordered_from(group_ids, groups=groups)
            )

        def document_chars(group_ids: Iterable[str]) -> int:
            seen: set[tuple[str, str, str, str]] = set()
            total = 0
            for group_id in group_ids:
                for document in resolved[group_id]:
                    if document.read_key in seen:
                        continue
                    seen.add(document.read_key)
                    total += document.chars
            return total

        core_chars = chars(manifest["core_groups"])
        if core_chars > budgets["core_chars"]:
            raise SkillPlanError(
                f"Core Skill plan uses {core_chars} chars, above "
                f"{budgets['core_chars']}"
            )
        core_set = set(self._ordered_from(manifest["core_groups"], groups=groups))
        for phase, group_ids in manifest["phase_baselines"].items():
            expanded = [
                group_id
                for group_id in self._ordered_from(group_ids, groups=groups)
                if group_id not in core_set
            ]
            phase_chars = document_chars(expanded)
            if phase_chars > budgets["phase_chars"]:
                raise SkillPlanError(
                    f"{phase} baseline uses {phase_chars} chars, above "
                    f"{budgets['phase_chars']}"
                )
            automatic_chars = chars(
                [*manifest["core_groups"], *group_ids]
            )
            if automatic_chars > budgets["automatic_chars"]:
                raise SkillPlanError(
                    f"{phase} automatic plan uses {automatic_chars} chars, above "
                    f"{budgets['automatic_chars']}"
                )
        common_groups = {
            *core_set,
            *(
                group_id
                for phase_groups in manifest["phase_baselines"].values()
                for group_id in self._ordered_from(
                    phase_groups,
                    groups=groups,
                )
            ),
        }
        for tool_group_id, binding in manifest["tool_group_bindings"].items():
            delta_groups = [
                group_id
                for group_id in self._ordered_from(
                    binding["required"],
                    groups=groups,
                )
                if group_id not in common_groups
            ]
            direct_chars = document_chars(delta_groups)
            if direct_chars > budgets["tool_group_chars"]:
                raise SkillPlanError(
                    f"{tool_group_id} Skill delta uses {direct_chars} chars, above "
                    f"{budgets['tool_group_chars']}"
                )

    @staticmethod
    def _ordered_from(
        seeds: Iterable[str],
        *,
        groups: Mapping[str, Any],
    ) -> list[str]:
        ordered: list[str] = []
        visited: set[str] = set()

        def visit(group_id: str) -> None:
            if group_id in visited:
                return
            visited.add(group_id)
            for dependency in groups[group_id]["depends_on"]:
                visit(dependency)
            ordered.append(group_id)

        for group_id in seeds:
            visit(group_id)
        return ordered
