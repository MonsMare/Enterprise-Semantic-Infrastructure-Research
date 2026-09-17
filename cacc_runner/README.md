# C&CC Claude Code Bridge

这是 Codex 与本机 Claude Code 的最小桥接器。Codex 负责拆解任务、发起任务包、审阅差异和验收；桥接器只启动一个 `claude -p` 进程、读取其 JSON 事件、保存本地工件并维持单 Worker 槽位。

```text
Codex ── stdio MCP ── C&CC Claude Code Bridge ── claude -p ── CC Switch
  └────────────────────────────── Linear 插件：一个总工单的状态、计划和进度
```

不使用 Claude Agent SDK、SQLite、Linear GraphQL、`ANTHROPIC_API_KEY` 或 `LINEAR_API_KEY`。

## 前置条件

1. 本机已安装 `claude` 命令。
2. CC Switch 已为 Claude Code 配置模型和本地路由。
3. Claude Code 当前用户配置能够完成所需的文件修改和测试权限确认。
4. Python 3.11 及 MCP Python SDK 可用。

桥接器不会传 `--bare`，因此 Claude Code 会正常读取现有 CC Switch 用户配置；它也不会自动传跳过权限的参数。

## 本地配置

复制并按实际路径修改 `config/projects.toml.example`：

```toml
[runner]
data_dir = "../.cacc-claude-bridge-data"
claude_command = "claude"
max_turns = 40

[projects."knowledge-runtime"]
root = "../.."
```

`projects` 只登记 Codex 可以委派 Claude Code 操作的本机项目根目录。配置中不保存任何模型、路由、Linear 或 API 凭据。

将 `config/codex-mcp.toml.example` 的 `[mcp_servers.cacc_runner]` 段合并到本机 Codex 配置，替换 Python 与 `projects.toml` 的绝对路径。MCP 启动后可调用 `runner_status` 确认 Claude CLI 是否可见。

## 任务包

```json
{
  "schema_version": "cacc-task-package/v2",
  "package_id": "KR-001",
  "project_id": "knowledge-runtime",
  "goal": "完成一个可独立验收的代码改动",
  "allowed_paths": ["knowledge_runtime/", "tests/"],
  "acceptance_criteria": [
    "目标行为符合要求",
    "相关测试通过并在结果中说明"
  ]
}
```

`allowed_paths` 只能使用项目根内的相对路径。桥接器拒绝绝对路径、`..` 越界和敏感路径，但它不是 OS 级沙箱；Codex 必须在验收时检查实际差异是否仍在任务范围内。

## MCP 工具与调用顺序

| 工具 | 用途 |
|---|---|
| `runner_status` | 读取 Claude CLI 可用性、登记项目和当前任务。 |
| `start_package` | 验证并启动唯一任务包。 |
| `get_package_status` | 读取阶段、最终结果和本地工件路径。 |
| `cancel_package` | 取消当前 Claude Code 进程，且不自动重试。 |
| `accept_package` | 写入 Codex 审阅说明并释放下一包。 |

流程：Codex 拆包后将总工单设为 `Todo`；`start_package` 成功后设为 `In Progress`；Claude 结束后仍保持 `In Progress`，由 Codex 检查代码差异、测试结果和报告；完成全部包的验收后设为 `Done`。失败、取消或中断的任务仍会占用槽位，直到 Codex 显式审阅。

本地工件保存在数据目录：`active-run.json`、`runs/<run-id>/task-package.json`、`events.jsonl`、`stderr.log` 和 `report.json`。完整对话和日志不会同步到 Linear。

## Linear 总工单

使用 Codex 已连接的 Linear 插件维护一个总工单。Issue 状态表示生命周期：`Backlog`、`Todo`、`In Progress`、`Done` 或 `Canceled`；描述中保留计划、任务包阶段和验收摘要：

```markdown
<!-- cacc-plan:start -->
## 目标
...

## 任务包
- KR-001｜进行中｜目标与验收摘要

## 当前进度
当前包、阶段、简短结果、更新时间。

## Codex 验收记录
KR-001｜已验收／待处理｜简短原因。
<!-- cacc-plan:end -->
```

不要把完整提示词、Claude 对话、测试日志、差异、凭据或本地路径写入 Linear。

## 验证

```powershell
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests -q
.\.venv\Scripts\python.exe -B -m cacc_runner.server
```

第二条命令需要 `CACC_RUNNER_CONFIG` 指向本机 `projects.toml`。实际 Claude Code 验证应使用无文件修改的最小提示，确认返回事件经过 CC Switch 正常回到桥接器。
