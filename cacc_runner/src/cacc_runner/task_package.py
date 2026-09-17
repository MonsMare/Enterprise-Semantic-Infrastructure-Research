"""校验 Codex 发给 Claude Code Bridge 的 v2 任务包。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Mapping

from .config import ProjectConfig


class PackageValidationError(ValueError):
    """任务包不符合 v2 协议或超出登记项目范围。"""


@dataclass(frozen=True)
class TaskPackage:
    schema_version: str
    package_id: str
    project_id: str
    goal: str
    allowed_paths: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]


@dataclass(frozen=True)
class ValidatedTaskPackage:
    package: TaskPackage
    project: ProjectConfig
    allowed_roots: tuple[Path, ...]


_PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SENSITIVE_EXACT = {
    ".aws", ".azure", ".codex-credentials", ".git", ".gnupg", ".ssh",
    "credentials", "id_rsa", "id_ed25519", "secrets",
}
_SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
_CREDENTIAL_PATTERNS = (
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\blin_api_[A-Za-z0-9_-]{6,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(?:api[_ -]?key|authorization|password|secret|token)\s*[:=]\s*\S+"),
)


def _is_sensitive(path: Path) -> bool:
    for component in path.parts:
        lowered = component.casefold()
        if lowered in _SENSITIVE_EXACT or lowered.startswith(".env") or Path(lowered).suffix in _SENSITIVE_SUFFIXES:
            return True
    return False


def _contains_credential(value: str) -> bool:
    return any(pattern.search(value) for pattern in _CREDENTIAL_PATTERNS)


def _normalise_relative(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise PackageValidationError("allowed_paths 必须是非空相对路径")
    value = raw.replace("\\", "/")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(raw)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or ".." in posix.parts:
        raise PackageValidationError(f"allowed_paths 不得使用绝对路径或 ..：{raw}")
    parts = tuple(part for part in posix.parts if part not in ("", "."))
    if not parts:
        return ()
    normalized = Path(*parts)
    if _is_sensitive(normalized):
        raise PackageValidationError(f"allowed_paths 不得包含敏感路径：{raw}")
    return parts


def parse_task_package(data: object, projects: Mapping[str, ProjectConfig]) -> ValidatedTaskPackage:
    """在启动 Claude Code 前验证包字段、项目别名和范围。"""
    if not isinstance(data, dict):
        raise PackageValidationError("任务包必须是对象")
    required = {
        "schema_version", "package_id", "project_id", "goal", "allowed_paths", "acceptance_criteria",
    }
    if set(data) != required:
        missing = required - set(data)
        unknown = set(data) - required
        raise PackageValidationError(f"任务包字段不符（缺少：{sorted(missing)}；未知：{sorted(unknown)}）")
    if data["schema_version"] != "cacc-task-package/v2":
        raise PackageValidationError("只支持 cacc-task-package/v2")
    package_id = data["package_id"]
    if not isinstance(package_id, str) or not _PACKAGE_ID_RE.fullmatch(package_id):
        raise PackageValidationError("package_id 格式无效")
    project_id = data["project_id"]
    if not isinstance(project_id, str) or project_id not in projects:
        raise PackageValidationError("project_id 未在桥接器本地登记")
    goal = data["goal"]
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 8000:
        raise PackageValidationError("goal 必须是 1 到 8000 字符的非空说明")
    if _contains_credential(goal):
        raise PackageValidationError("任务包不得包含凭据或授权值")

    paths_raw = data["allowed_paths"]
    if not isinstance(paths_raw, list) or not paths_raw:
        raise PackageValidationError("allowed_paths 必须是非空数组")
    path_parts: list[tuple[str, ...]] = []
    for raw in paths_raw:
        parts = _normalise_relative(raw)
        if parts not in path_parts:
            path_parts.append(parts)

    criteria_raw = data["acceptance_criteria"]
    if not isinstance(criteria_raw, list) or not criteria_raw or any(
        not isinstance(item, str) or not item.strip() or len(item) > 2000 for item in criteria_raw
    ):
        raise PackageValidationError("acceptance_criteria 必须包含非空字符串")
    if any(_contains_credential(item) for item in criteria_raw):
        raise PackageValidationError("任务包不得包含凭据或授权值")

    project = projects[project_id]
    root = project.root.resolve(strict=False)
    allowed_roots: list[Path] = []
    for parts in path_parts:
        candidate = project.root.joinpath(*parts) if parts else project.root
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise PackageValidationError(f"allowed_paths 超出项目根目录或无法解析：{'/'.join(parts)}") from exc
        if _is_sensitive(resolved):
            raise PackageValidationError(f"allowed_paths 指向敏感路径：{'/'.join(parts)}")
        allowed_roots.append(resolved)

    task = TaskPackage(
        "cacc-task-package/v2",
        package_id,
        project_id,
        goal.strip(),
        tuple("/".join(parts) if parts else "." for parts in path_parts),
        tuple(item.strip() for item in criteria_raw),
    )
    return ValidatedTaskPackage(task, project, tuple(allowed_roots))
