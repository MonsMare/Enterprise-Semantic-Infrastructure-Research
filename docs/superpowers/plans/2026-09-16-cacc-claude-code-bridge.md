# C&CC Claude Code Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 将现有 C&CC Runner 改为一个通过 Claude Code CLI 与 CC Switch 通信的单 Worker 本地 MCP 桥接器，并由 Codex 的 Linear 插件维护一个总工单。

**Architecture:** Codex 继续负责计划、任务拆分和逐包验收。stdio MCP 服务只验证任务包、启动一个 `claude -p` 子进程、收集其 JSON 事件、保留本地工件并在 Codex 明确验收后释放唯一槽位；它不使用 Claude Agent SDK、SQLite 或 Linear API。Claude Code 正常读取用户级 CC Switch 设置，不能使用 `--bare`。

**Tech Stack:** Python 3.11、MCP Python SDK、`asyncio.create_subprocess_exec`、Claude Code CLI、JSON 本地工件、Codex Linear 插件。

**Spec:** `docs/superpowers/specs/2026-09-15-codex-claude-linear-workbench-design.md`

## Global Constraints

- 不依赖 `claude-agent-sdk`、`ANTHROPIC_API_KEY`、`LINEAR_API_KEY`、SQLite 或 Linear GraphQL。
- 子进程必须通过参数数组启动；不得使用 shell、`--bare` 或 `--dangerously-skip-permissions`。
- 运行时仅允许一个活动任务包；任何结束结果都必须由 Codex 显式 `accept_package` 后才释放槽位。
- Linear 由 Codex 通过已连接插件更新一个总工单；桥接器不保存或调用 Linear 凭据。
- Windows 原生实现不宣称 OS 级沙箱；任务包路径校验用于约束指令和审阅范围。
- 现有主工作树有未提交的第一版实现；本次不自动创建提交。

---

### Task 1: 更新配置与任务包协议

**Files:**
- Modify: `cacc_runner/src/cacc_runner/config.py`
- Modify: `cacc_runner/src/cacc_runner/task_package.py`
- Modify: `cacc_runner/tests/test_config.py`
- Modify: `cacc_runner/tests/test_task_package.py`

**Interfaces:**
- Produces: `RunnerConfig(data_dir, claude_command, max_turns, projects)`。
- Produces: `parse_task_package(payload, projects) -> ValidatedTaskPackage`，只接受 `cacc-task-package/v2`。

- [x] **Step 1: 写入失败测试**

```python
def test_valid_v2_package_has_no_api_key_or_shell_check_field():
    package = parse_task_package(valid_payload(), projects)
    assert package.package.checks == ()

def test_config_uses_claude_command_without_linear_or_sdk_settings():
    settings = load_config(config_path)
    assert settings.claude_command == "claude"
```

- [x] **Step 2: 运行指定测试并确认失败原因是旧版协议仍要求 `checks` 或旧配置字段。**

- [x] **Step 3: 以最小实现改为 v2 任务包与 Claude CLI 配置。**

- [x] **Step 4: 重新运行配置和任务包测试。**

### Task 2: 实现 Claude Code CLI 适配器

**Files:**
- Create: `cacc_runner/src/cacc_runner/claude_code.py`
- Create: `cacc_runner/tests/test_claude_code.py`
- Delete: `cacc_runner/src/cacc_runner/worker.py`
- Delete: `cacc_runner/tests/test_worker.py`

**Interfaces:**
- Produces: `ClaudeCodeWorker.ensure_available()`，只检查 `claude` 命令可用。
- Produces: `ClaudeCodeWorker.run_package(validated, run_id, run_dir, on_progress, on_session_id)`。
- Produces: `ClaudeCodeWorker.cancel(run_id)`，结束对应 CLI 子进程并等待结果。

- [x] **Step 1: 写入失败测试，断言构建的 argv 含 `-p`、`--output-format stream-json`、`--verbose`，且不含 `--bare`、`--dangerously-skip-permissions` 和 API Key 预检。**

- [x] **Step 2: 运行该测试，确认导入失败或缺少 `ClaudeCodeWorker`。**

- [x] **Step 3: 用 `asyncio.create_subprocess_exec` 实现参数数组调用、stdout JSONL 解析、stderr 本地日志、会话 ID/摘要提取与取消。**

- [x] **Step 4: 运行适配器测试，验证成功、失败和取消结果。**

### Task 3: 用 JSON 运行记录替换 SQLite 状态机

**Files:**
- Create: `cacc_runner/src/cacc_runner/run_record.py`
- Modify: `cacc_runner/src/cacc_runner/server.py`
- Create: `cacc_runner/tests/test_run_record.py`
- Modify: `cacc_runner/tests/test_server.py`
- Delete: `cacc_runner/src/cacc_runner/run_store.py`
- Delete: `cacc_runner/src/cacc_runner/checks.py`
- Delete: `cacc_runner/src/cacc_runner/linear_sync.py`
- Delete: `cacc_runner/tests/test_run_store.py`
- Delete: `cacc_runner/tests/test_checks.py`
- Delete: `cacc_runner/tests/test_linear_sync.py`

**Interfaces:**
- Produces: `RunJournal.create/read/update/clear`，以 `active-run.json` 表示唯一槽位。
- Produces: `RunnerService.start_package/get_package_status/cancel_package/accept_package/runner_status`。

- [x] **Step 1: 写入失败测试，覆盖同一时间只能启动一个任务包、完成后进入待 Codex 审阅、显式验收释放槽位，以及重启后保留中断记录。**

- [x] **Step 2: 运行服务和记录测试，确认旧 SQLite 服务接口无法满足 JSON 行为。**

- [x] **Step 3: 实现原子 JSON 记录、报告写入、进度摘要和 Claude Code Worker 调度；删除 Linear 同步和固定检查执行逻辑。**

- [x] **Step 4: 运行服务和记录测试，验证不需要任何 API Key。**

### Task 4: 缩减 MCP 表面并更新安装配置

**Files:**
- Modify: `cacc_runner/src/cacc_runner/server.py`
- Modify: `cacc_runner/tests/test_mcp_server.py`
- Modify: `cacc_runner/pyproject.toml`
- Modify: `cacc_runner/config/projects.toml.example`
- Modify: `cacc_runner/config/codex-mcp.toml.example`

**Interfaces:**
- Produces exactly: `runner_status`、`start_package`、`get_package_status`、`cancel_package`、`accept_package`。

- [x] **Step 1: 写入失败 MCP 集成测试，断言不再暴露 `sync_plan_to_linear`，并验证剩余五个工具的调用顺序。**

- [x] **Step 2: 运行测试，确认旧服务仍暴露六个工具。**

- [x] **Step 3: 删除 SDK 依赖和 Linear 配置字段，改写 MCP 工具描述与示例。**

- [x] **Step 4: 运行完整 `cacc_runner` 测试集和 stdio 初始化冒烟测试。**

### Task 5: 更新 Skill、设计书、README 与 Linear 总工单

**Files:**
- Modify: `docs/superpowers/specs/2026-09-15-codex-claude-linear-workbench-design.md`
- Modify: `cacc_runner/README.md`
- Modify: `cacc_runner/skills/cacc-supervisor/SKILL.md`
- Modify: `C:/Users/meta/.codex/skills/cacc-supervisor/SKILL.md`

**Interfaces:**
- Produces: Skill 指引 Codex 用 Linear 插件更新一个总工单，而非调用 Runner 直连 Linear。

- [x] **Step 1: 改写设计书，写明简化架构、CC Switch 配置继承、无 OS 沙箱承诺和总工单格式。**

- [x] **Step 2: 更新 Skill 和 README，提供 v2 任务包、MCP 调用顺序与 Linear 总工单描述模板。**

- [x] **Step 3: 使用已连接的 Linear 插件创建或更新一个 C&CC 总工单，只写计划、任务包状态和验收摘要。**

- [x] **Step 4: 对比安装后的 Skill 与仓库 Skill，运行完整测试、编译检查和真实 CLI 最小往返。**

## Self-Review

- [x] 设计书中的每个运行时组件都有对应任务。
- [x] 没有保留 Agent SDK、SQLite、Linear GraphQL 或环境密钥依赖。
- [x] 所有新生产行为均先由失败测试覆盖。
- [x] 所有 MCP 工具、任务包版本和 Skill 文案使用相同名称。


