"""C&CC Claude Code Bridge 的本地静态配置。"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class ConfigError(ValueError):
    """本地桥接器配置无效。"""


_ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class ProjectConfig:
    project_id: str
    root: Path


@dataclass(frozen=True)
class RunnerConfig:
    data_dir: Path
    claude_command: str
    max_turns: int
    projects: Mapping[str, ProjectConfig]


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} 必须是 TOML 表")
    return value


def _local_path(raw: object, base: Path, name: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{name} 必须是非空路径")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ConfigError(f"{name} 无法解析") from exc


def _claude_command(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ConfigError("runner.claude_command 必须是非空命令路径")
    return value.strip()


def _max_turns(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200:
        raise ConfigError("runner.max_turns 必须在 1 到 200 之间")
    return value


def load_config(path: str | Path) -> RunnerConfig:
    """加载登记项目与 Claude Code CLI 的本地配置。"""
    config_path = Path(path).expanduser().resolve(strict=False)
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取桥接器 TOML 配置：{config_path}") from exc

    runner_data = _mapping(raw.get("runner", {}), "runner")
    allowed_runner_keys = {"data_dir", "claude_command", "max_turns"}
    if set(runner_data) - allowed_runner_keys:
        raise ConfigError("runner 配置含有未知字段")
    data_dir = _local_path(
        runner_data.get("data_dir", ".cacc-claude-bridge"),
        config_path.parent,
        "runner.data_dir",
    )
    claude_command = _claude_command(runner_data.get("claude_command", "claude"))
    max_turns = _max_turns(runner_data.get("max_turns", 40))

    raw_projects = _mapping(raw.get("projects"), "projects")
    if not raw_projects:
        raise ConfigError("至少需要登记一个本地项目")
    projects: dict[str, ProjectConfig] = {}
    for project_id, project_value in raw_projects.items():
        if not _ALIAS_RE.fullmatch(project_id):
            raise ConfigError(f"项目别名无效：{project_id}")
        project_data = _mapping(project_value, f"projects.{project_id}")
        if set(project_data) != {"root"}:
            raise ConfigError(f"projects.{project_id} 只允许 root 字段")
        root = _local_path(project_data.get("root"), config_path.parent, f"projects.{project_id}.root")
        if not root.exists() or not root.is_dir():
            raise ConfigError(f"项目根目录不存在或不是目录：{project_id}")
        projects[project_id] = ProjectConfig(project_id, root)

    return RunnerConfig(data_dir, claude_command, max_turns, projects)
