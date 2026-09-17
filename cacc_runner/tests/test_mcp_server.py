import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from mcp import Client, StdioServerParameters

from cacc_runner.claude_code import ClaudeCodeResult
from cacc_runner.config import ProjectConfig, RunnerConfig
from cacc_runner.server import RunnerService, create_mcp_server


class FastWorker:
    async def ensure_available(self):
        return None

    async def run_package(self, _validated, _run_id, run_dir, on_progress, on_session_id):
        run_dir.mkdir(parents=True, exist_ok=True)
        events_path = run_dir / "events.jsonl"
        stderr_path = run_dir / "stderr.log"
        events_path.write_text("{}\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        await on_session_id("mcp-session")
        await on_progress("working", "Claude Code 正在执行任务包")
        return ClaudeCodeResult(
            outcome="completed",
            summary="MCP 集成测试完成",
            session_id="mcp-session",
            terminal_reason="success",
            duration_ms=1,
            num_turns=1,
            exit_code=0,
            events_path=events_path,
            stderr_path=stderr_path,
        )

    async def cancel(self, _run_id):
        return None


def task_package():
    return {
        "schema_version": "cacc-task-package/v2",
        "package_id": "KR-MCP-001",
        "project_id": "repo",
        "goal": "验证 MCP 桥接工具边界",
        "allowed_paths": ["src/"],
        "acceptance_criteria": ["固定测试通过"],
    }


class McpServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "src").mkdir()
        settings = RunnerConfig(
            data_dir=self.root / ".bridge-data",
            claude_command="claude",
            max_turns=40,
            projects={"repo": ProjectConfig("repo", self.root)},
        )
        self.service = RunnerService(settings, worker=FastWorker())
        self.service.open()
        self.mcp = create_mcp_server(self.service)

    async def _cleanup(self):
        await self.service.aclose()
        self.temp.cleanup()

    async def test_initialize_lists_exactly_five_bridge_tools(self):
        async with Client(self.mcp, raise_exceptions=True) as client:
            listed = await client.list_tools()
            result = await client.call_tool("runner_status")

        self.assertEqual(
            {tool.name for tool in listed.tools},
            {"runner_status", "start_package", "get_package_status", "cancel_package", "accept_package"},
        )
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["service"], "cacc_claude_code_bridge")

    async def test_mcp_runs_package_then_requires_explicit_acceptance(self):
        async with Client(self.mcp, raise_exceptions=True) as client:
            started = await client.call_tool("start_package", {"package": task_package()})
            run_id = started.structured_content["run_id"]
            await self.service.wait_for_run(run_id)
            status = await client.call_tool("get_package_status", {"run_id": run_id})
            accepted = await client.call_tool(
                "accept_package", {"run_id": run_id, "review_note": "已检查本地报告"}
            )

        self.assertFalse(started.is_error)
        self.assertEqual(status.structured_content["status"], "awaiting_codex_review")
        self.assertEqual(accepted.structured_content["status"], "accepted")

    async def test_stdio_process_initializes_without_linear_or_sdk_settings(self):
        config_path = self.root / "projects.toml"
        config_path.write_text(
            "[runner]\n"
            'data_dir = "./stdio-data"\n'
            'claude_command = "claude"\n'
            "[projects.repo]\n"
            f'root = "{self.root.as_posix()}"\n',
            encoding="utf-8",
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "cacc_runner.server"],
            env={"CACC_RUNNER_CONFIG": str(config_path)},
        )

        async with Client(params, raise_exceptions=True) as client:
            listed = await client.list_tools()
            result = await client.call_tool("runner_status")

        self.assertIn("start_package", {tool.name for tool in listed.tools})
        self.assertNotIn("sync_plan_to_linear", {tool.name for tool in listed.tools})
        self.assertEqual(result.structured_content["service"], "cacc_claude_code_bridge")


if __name__ == "__main__":
    unittest.main()
