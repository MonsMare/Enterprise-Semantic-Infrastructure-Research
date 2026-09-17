"""通过 Claude Code CLI 执行一个 Codex 任务包。"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .task_package import ValidatedTaskPackage


class WorkerDependencyError(RuntimeError):
    """本机未安装或无法找到 Claude Code CLI。"""


ProgressCallback = Callable[[str, str], Any | Awaitable[Any]]
SessionCallback = Callable[[str], Any | Awaitable[Any]]
ProcessFactory = Callable[..., Awaitable[Any]]

_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|password|secret|authorization)(\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)\bBearer\s+[^\s,;]+"),
    re.compile(r"\b(?:sk-ant|lin_api)_[A-Za-z0-9_-]{6,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


@dataclass(frozen=True)
class ClaudeCodeResult:
    outcome: str
    summary: str
    session_id: str | None
    terminal_reason: str | None
    duration_ms: int | None
    num_turns: int | None
    exit_code: int | None
    events_path: Path
    stderr_path: Path


def _redact_text(value: str) -> str:
    text = value
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 3:
            text = pattern.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


def _redact_value(value: object) -> object:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    return value


async def _notify(callback: ProgressCallback | SessionCallback | None, *args: str) -> None:
    if callback is None:
        return
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


def build_task_prompt(validated: ValidatedTaskPackage) -> str:
    """生成只描述当前包意图和验收边界的 Claude Code 提示。"""
    package = validated.package
    paths = "；".join(package.allowed_paths)
    criteria = "；".join(package.acceptance_criteria)
    # Windows 的 npm .cmd 包装器会把提示中的真实换行当作命令分隔符，
    # 所以 -p 只能接收单行文本。句子分隔保留了相同的任务语义。
    return " ".join(
        (
            "你是 Codex 委派的 Claude Code 执行 Worker。只处理当前这一个任务包。",
            f"任务包：{package.package_id}。",
            f"目标：{package.goal}。",
            f"允许修改的仓库内路径：{paths}。",
            f"验收标准：{criteria}。",
            "只完成当前任务包；需要修改范围外文件时停止并在最终报告中说明。",
            "完成前运行相关测试或验证，并说明命令和结果。",
            "不要写入 Linear，也不要启动其他 Agent。",
            "最终回答按完成情况、修改文件、验证、遗留风险四项简要说明。",
        )
    )


class ClaudeCodeWorker:
    """以一个 Claude Code CLI 子进程实现一个任务包。"""

    def __init__(
        self,
        command: str,
        *,
        max_turns: int = 40,
        command_finder: Callable[[str], str | None] = shutil.which,
        process_factory: ProcessFactory = asyncio.create_subprocess_exec,
    ) -> None:
        self.command = command
        self.max_turns = max_turns
        self._command_finder = command_finder
        self._process_factory = process_factory
        self._processes: dict[str, Any] = {}
        self._cancel_requested: set[str] = set()
        self._resolved_command: str | None = None

    def _resolve_command(self) -> str:
        resolved = self._command_finder(self.command)
        if not resolved:
            raise WorkerDependencyError(
                f"找不到 Claude Code 命令：{self.command}。请确认 Claude Code 与 CC Switch 已在本机配置。"
            )
        self._resolved_command = resolved
        return resolved

    async def ensure_available(self) -> None:
        self._resolve_command()

    def build_argv(self, validated: ValidatedTaskPackage) -> tuple[str, ...]:
        return (
            self._resolved_command or self._resolve_command(),
            "-p",
            build_task_prompt(validated),
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--max-turns",
            str(self.max_turns),
        )

    async def run_package(
        self,
        validated: ValidatedTaskPackage,
        run_id: str,
        run_dir: Path,
        on_progress: ProgressCallback | None,
        on_session_id: SessionCallback | None,
    ) -> ClaudeCodeResult:
        run_dir.mkdir(parents=True, exist_ok=True)
        events_path = run_dir / "events.jsonl"
        stderr_path = run_dir / "stderr.log"
        argv = self.build_argv(validated)
        await _notify(on_progress, "starting", "正在启动 Claude Code")
        process = await self._process_factory(
            *argv,
            cwd=str(validated.project.root),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._processes[run_id] = process
        session_id: str | None = None
        result_payload: dict[str, object] | None = None

        async def consume_stdout() -> None:
            nonlocal session_id, result_payload
            with events_path.open("w", encoding="utf-8") as handle:
                while True:
                    raw = await process.stdout.readline()
                    if not raw:
                        break
                    text = raw.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    try:
                        event = json.loads(text)
                    except json.JSONDecodeError:
                        event = {"type": "unparsed", "summary": _redact_text(text[:2000])}
                    safe_event = _redact_value(event)
                    handle.write(json.dumps(safe_event, ensure_ascii=False) + "\n")
                    event_type = event.get("type") if isinstance(event, dict) else None
                    candidate_session = event.get("session_id") if isinstance(event, dict) else None
                    if isinstance(candidate_session, str) and candidate_session and candidate_session != session_id:
                        session_id = candidate_session
                        await _notify(on_session_id, session_id)
                    if event_type == "system" and isinstance(event, dict) and event.get("subtype") == "init":
                        await _notify(on_progress, "connected", "Claude Code 已连接")
                    elif event_type == "assistant":
                        await _notify(on_progress, "working", "Claude Code 正在执行任务包")
                    elif event_type == "result" and isinstance(event, dict):
                        result_payload = event
                        await _notify(
                            on_progress,
                            "finalizing",
                            "Claude Code 正在返回最终结果",
                        )

        async def consume_stderr() -> None:
            with stderr_path.open("w", encoding="utf-8") as handle:
                while True:
                    raw = await process.stderr.readline()
                    if not raw:
                        break
                    handle.write(_redact_text(raw.decode("utf-8", errors="replace")))

        try:
            stdout_task = asyncio.create_task(consume_stdout())
            stderr_task = asyncio.create_task(consume_stderr())
            exit_code = await process.wait()
            await asyncio.gather(stdout_task, stderr_task)
        finally:
            self._processes.pop(run_id, None)

        payload = result_payload or {}
        payload_session = payload.get("session_id")
        if isinstance(payload_session, str) and payload_session and payload_session != session_id:
            session_id = payload_session
            await _notify(on_session_id, session_id)
        payload_summary = payload.get("result")
        if not isinstance(payload_summary, str) or not payload_summary.strip():
            payload_summary = payload.get("error")
        terminal_reason = payload.get("subtype") if isinstance(payload.get("subtype"), str) else None
        duration_ms = payload.get("duration_ms") if isinstance(payload.get("duration_ms"), int) else None
        num_turns = payload.get("num_turns") if isinstance(payload.get("num_turns"), int) else None
        if run_id in self._cancel_requested:
            outcome = "cancelled"
            fallback = "Codex 请求取消 Claude Code 任务"
        elif bool(payload.get("is_error")) or exit_code != 0:
            outcome = "failed"
            fallback = f"Claude Code 进程以退出码 {exit_code} 结束"
        else:
            outcome = "completed"
            fallback = "Claude Code 已完成任务，但未返回最终摘要"
        self._cancel_requested.discard(run_id)
        return ClaudeCodeResult(
            outcome=outcome,
            summary=_redact_text(payload_summary.strip() if isinstance(payload_summary, str) else fallback),
            session_id=session_id,
            terminal_reason=terminal_reason,
            duration_ms=duration_ms,
            num_turns=num_turns,
            exit_code=exit_code,
            events_path=events_path,
            stderr_path=stderr_path,
        )

    async def cancel(self, run_id: str) -> None:
        process = self._processes.get(run_id)
        if process is None:
            raise RuntimeError("当前运行没有可取消的 Claude Code 进程")
        self._cancel_requested.add(run_id)
        if process.returncode is None:
            process.terminate()
