import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cacc_runner.claude_code import ClaudeCodeWorker, WorkerDependencyError
from cacc_runner.config import ProjectConfig
from cacc_runner.task_package import parse_task_package


def task_package():
    return {
        "schema_version": "cacc-task-package/v2",
        "package_id": "KR-CLI-001",
        "project_id": "repo",
        "goal": "增加一个可验证的小改动",
        "allowed_paths": ["src/", "tests/"],
        "acceptance_criteria": ["行为正确", "相关测试通过"],
    }


class FakeStream:
    def __init__(self, lines=()):
        self.lines = [line if isinstance(line, bytes) else line.encode("utf-8") for line in lines]

    async def readline(self):
        return self.lines.pop(0) if self.lines else b""


class FakeProcess:
    def __init__(self, stdout_lines, stderr_lines=(), exit_code=0):
        self.stdout = FakeStream(stdout_lines)
        self.stderr = FakeStream(stderr_lines)
        self.pid = 4321
        self.returncode = None
        self.exit_code = exit_code
        self.terminated = False

    async def wait(self):
        self.returncode = self.exit_code
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


class ClaudeCodeWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        self.validated = parse_task_package(
            task_package(), {"repo": ProjectConfig("repo", self.root)}
        )
        self.run_dir = self.root / "run"

    async def _cleanup(self):
        self.temp.cleanup()

    async def test_uses_headless_stream_json_without_api_key_or_bare_mode(self):
        process = FakeProcess([])
        calls = []

        async def factory(*argv, **kwargs):
            calls.append((argv, kwargs))
            return process

        worker = ClaudeCodeWorker(
            "claude",
            max_turns=13,
            command_finder=lambda command: "C:/tools/claude.exe",
            process_factory=factory,
        )
        with patch.dict(os.environ, {}, clear=True):
            await worker.ensure_available()
            await worker.run_package(self.validated, "run-1", self.run_dir, None, None)

        argv, kwargs = calls[0]
        self.assertEqual(argv[0], "C:/tools/claude.exe")
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", argv)
        self.assertIn("--include-partial-messages", argv)
        self.assertEqual(argv[argv.index("--max-turns") + 1], "13")
        self.assertNotIn("--bare", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        prompt = argv[argv.index("-p") + 1]
        self.assertNotIn("\n", prompt)
        self.assertIn("允许修改的仓库内路径", prompt)
        self.assertEqual(kwargs["cwd"], str(self.root))

    async def test_reports_structured_result_and_persists_local_event_log(self):
        events = [
            json.dumps({"type": "system", "subtype": "init", "session_id": "session-7"}) + "\n",
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "正在修改"}]}}) + "\n",
            json.dumps({
                "type": "result", "subtype": "success", "is_error": False,
                "session_id": "session-7", "result": "修改完成，测试通过", "duration_ms": 25, "num_turns": 2,
            }) + "\n",
        ]
        process = FakeProcess(events, ["diagnostic only\n"])
        progress = []
        sessions = []

        async def factory(*_argv, **_kwargs):
            return process

        worker = ClaudeCodeWorker(
            "claude", command_finder=lambda _command: "claude", process_factory=factory
        )
        result = await worker.run_package(
            self.validated,
            "run-2",
            self.run_dir,
            lambda phase, summary: progress.append((phase, summary)),
            sessions.append,
        )

        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.session_id, "session-7")
        self.assertEqual(result.summary, "修改完成，测试通过")
        self.assertEqual(result.duration_ms, 25)
        self.assertEqual(result.num_turns, 2)
        self.assertTrue(result.events_path.is_file())
        self.assertTrue(result.stderr_path.is_file())
        self.assertEqual(sessions, ["session-7"])
        self.assertIn(("working", "Claude Code 正在执行任务包"), progress)

    async def test_missing_claude_command_fails_without_checking_anthropic_key(self):
        worker = ClaudeCodeWorker("claude", command_finder=lambda _command: None)
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=True):
            with self.assertRaises(WorkerDependencyError) as raised:
                await worker.ensure_available()
        self.assertIn("Claude Code", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
