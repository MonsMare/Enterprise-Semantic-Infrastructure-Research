# Codex–Claude Code–Linear C&CC Workbench 简化第一版设计书

> 状态：已实施并验证<br>
> 日期：2026-09-16<br>
> Linear 工作区：C&CC Workbench
> 实现位置：Knowledge Runtime 仓库顶层的独立 `cacc_runner` 子项目

## 1. 修订原因

上一版把 Claude Agent SDK、SQLite 状态机、Runner 直连 Linear GraphQL、固定工具白名单和自定义文件 Hook 组合在一起。这些组件与已安装的 Claude Code、CC Switch 和 Codex Linear 插件的职责重叠，且 Agent SDK 强制要求 `ANTHROPIC_API_KEY`，不会继承本机 Claude Code 的 CC Switch 路由设置。

本版改为直接调用 Claude Code CLI。Claude Code 正常读取用户现有的 CC Switch 配置，Codex 通过已连接的 Linear 插件维护一个总工单。保留本地 MCP 的原因只有一个：为 Codex 提供可靠的进程启动、状态读取、取消和单 Worker 互斥入口。

## 2. 目标与边界

### 2.1 目标

1. Codex 拆分工作、发出一个任务包，并审阅每个结果。
2. 本地 MCP 桥接器把任务包转为一次 `claude -p` 调用，接收 Claude Code 的结构化事件和最终反馈。
3. 同一时间只运行一个 Claude Code 任务；当前任务经 Codex 显式验收后，才可开始下一包。
4. Claude Code 通过现有 CC Switch 路由使用已配置模型，不要求 Anthropic API Key。
5. Linear 中维护一个总工单；其 Issue 状态展示生命周期，描述展示目标、任务包、阶段进度和 Codex 验收摘要。
6. 完整 Claude 输出、命令输出、代码差异和本地工件只保存在本机。

### 2.2 不做

- Claude Agent SDK、`ANTHROPIC_API_KEY` 预检或 SDK 会话；
- Runner 直连 Linear、`LINEAR_API_KEY`、GraphQL、Webhook 或同步队列；
- SQLite、自动重试、自动开始下一包、并行 Worker、自动合并或部署；
- 声称 Windows 原生环境具备 OS 级 Claude Code 沙箱；
- 把 Linear 当作唯一 Worker 状态机、日志仓库或验收依据。

## 3. 架构

```text
用户
  │ 目标、范围、计划确认
  ▼
Codex Supervisor + cacc-supervisor Skill
  │ 任务包、审阅、验收决定
  ├─────────────────────────────► Linear 插件
  │                                 一个总工单：Issue 状态、计划与简要进度
  ▼ 本机 stdio MCP
C&CC Claude Code Bridge
  │ 进程启动 / 状态 / 取消 / 单包互斥 / 本地工件
  ▼
claude -p --output-format stream-json
  │ 读取现有 ~/.claude 配置
  ▼
CC Switch 本地路由 → 已选模型
```

### 3.1 职责

| 组件 | 负责 | 不负责 |
|---|---|---|
| 用户 | 目标、范围变化、计划确认 | 日常代码和测试指令的逐条批准 |
| Codex | 任务拆分、调用桥接器、检查差异/测试、验收、更新 Linear 总工单状态与摘要 | 让 Claude 自行推进下一个包 |
| `cacc-supervisor` Skill | 规定任务包格式、调用顺序、验收清单和 Linear 状态与摘要格式 | 启动进程或直连 Linear API |
| MCP 桥接器 | 验证任务包、启动/取消一个 Claude Code 进程、保存本地 JSON 工件 | 代码验收、Linear 写入、权限升级 |
| Claude Code | 实现、测试、返回结果 | 写 Linear、选择下一包、决定验收 |
| CC Switch | Claude Code 的模型和路由配置 | 任务编排、工件管理 |
| Linear | 总 Issue 状态、计划和简要进度 | Worker 控制、完整日志、验收事实来源 |

## 4. Claude Code 与 CC Switch

桥接器以参数数组调用 Claude Code：

```text
claude -p <任务提示> --output-format stream-json --verbose --include-partial-messages --max-turns <配置值>
```

它不会传入 `--bare`，因为该模式会跳过用户设置、OAuth 和 Keychain，可能使 CC Switch 的本机设置失效。它也不会自动添加 `--dangerously-skip-permissions` 或权限模式参数。Claude Code 使用用户既有的权限和 CC Switch 设置；桥接器只在本机任务包层面限制范围并让 Codex 审阅实际差异。

桥接器不检查 `ANTHROPIC_API_KEY`，也不读取或记录 CC Switch 的令牌。它只检查 `claude` 命令是否可执行。Claude Code 的 stdout 使用 `stream-json` 事件；桥接器保存脱敏后的本地事件流和 stderr，向 Codex 返回短阶段摘要、会话 ID、最终结果和工件路径。

## 5. 任务包协议

任务包是 Codex 发送给桥接器的唯一执行输入：

```json
{
  "schema_version": "cacc-task-package/v2",
  "package_id": "KR-001",
  "project_id": "knowledge-runtime",
  "goal": "完成一个范围明确、可独立验收的代码改动",
  "allowed_paths": ["knowledge_runtime/", "tests/"],
  "acceptance_criteria": [
    "目标行为可复现",
    "相关测试通过并在结果中说明"
  ]
}
```

`project_id` 只能引用本机配置中登记的项目。`allowed_paths` 必须是仓库内相对路径，拒绝绝对路径、盘符、`..` 越界和空路径。该校验保证 Codex 给出的任务范围清晰，不能替代操作系统级隔离；Claude Code 的实际文件修改由 Codex 在验收时根据差异核对。

传给 Claude Code 的提示明确包含目标、允许路径、验收标准和以下约束：只完成当前包；范围外需求先报告；完成前运行相关测试；最终回答须包含完成情况、修改文件、验证结果和遗留风险。

## 6. 单 Worker 与验收流程

```text
idle
  → running
  → awaiting_codex_review
  → accepted
  → idle
```

1. Codex 在 Linear 总工单写入计划和第一个任务包的简要状态，并将 Issue 设为 `Todo`。
2. Codex 调用 `start_package`。桥接器校验包、以原子 `active-run.json` 占用唯一槽位，并启动 Claude Code；启动成功后，Codex 将 Issue 设为 `In Progress`。启动失败时保留 `Todo` 并写入简短原因。
3. Codex 调用 `get_package_status` 读取短事件、最终摘要和本地工件路径；需要停止时调用 `cancel_package`。
4. 无论 Claude Code 成功、失败还是取消，桥接器都保存报告并进入 `awaiting_codex_review`，不会自动重试。
5. Codex 检查实际差异、任务范围、验收标准和测试结果；在验收期间保留 `In Progress`，并更新 Linear 总工单的简要状态。
6. Codex 调用 `accept_package` 写入审阅说明并释放槽位。此调用不把失败或取消改写成成功。若还有待启动包，Issue 回到 `Todo`；若总目标全部验收通过，Issue 设为 `Done`；用户终止总目标时设为 `Canceled`。
7. 只有完成第 6 步后，才允许启动下一任务包。

运行时异常退出时，桥接器不尝试恢复或重连 Claude Code。下次启动会把未完成的本地记录标为 `interrupted` 并继续占用槽位，直到 Codex 核对现场后明确验收。它不会自动启动替代进程。

## 7. MCP 工具

桥接器只提供五个工具：

| 工具 | 作用 |
|---|---|
| `runner_status` | 显示已登记项目、Claude CLI 可用性和当前唯一运行记录；不显示密钥。 |
| `start_package` | 校验 v2 任务包并启动唯一 Claude Code 进程。 |
| `get_package_status` | 返回当前或已完成任务的简短状态、事件和本地工件路径。 |
| `cancel_package` | 终止当前 Claude Code 进程并等待其结果；不会重试。 |
| `accept_package` | 记录 Codex 的真实审阅说明，释放下一包资格。 |

不提供 `sync_plan_to_linear`。Linear 的 Issue 状态和描述均由 Codex 当前会话中已连接的插件更新。

## 8. 本地状态与工件

运行数据目录只保存：

```text
active-run.json                 当前唯一槽位
runs/<run-id>/task-package.json 输入包
runs/<run-id>/events.jsonl      脱敏后的 Claude stream-json 事件
runs/<run-id>/stderr.log        脱敏后的 stderr
runs/<run-id>/report.json       结果、时间、会话 ID、退出码、审阅说明
```

不使用数据库。`active-run.json` 由独占创建和原子替换维护；接受任务时删除该文件。报告不写入 API Key 或完整环境变量。

## 9. Linear 总工单

Codex 通过 Linear 插件创建和维护一个总工单。Issue 状态用于展示粗粒度生命周期，描述中只包含固定区块：

| 本地阶段 | Linear Issue 状态 | 描述中的当前进度 |
|---|---|---|
| 尚未形成可执行包 | `Backlog` | 等待任务范围明确 |
| 已拆包，等待启动 | `Todo` | 当前包、范围和依赖 |
| Claude 执行、Codex 验收或等待下一步决定 | `In Progress` | 执行中、等待验收或阻塞原因 |
| 全部任务包验收通过 | `Done` | 最终验收摘要 |
| 用户终止总目标 | `Canceled` | 终止原因 |

C&CC Workbench 当前没有 `In Review` 或 `Blocked` 状态；这两类细节写在“当前进度”中。

```markdown
<!-- cacc-plan:start -->
## 目标
...

## 任务包
- KR-001｜进行中｜目标、范围、验收摘要
- KR-002｜等待 KR-001 验收｜目标、范围、验收摘要

## 当前进度
当前包、阶段、简短结果、更新时间。

## Codex 验收记录
包编号、验收结论、简要原因。
<!-- cacc-plan:end -->
```

不写入完整提示词、Claude 对话、测试日志、差异、凭据、本地路径或运行控制数据。Linear 更新失败不影响本地 Worker 状态；Codex 可在下次可用时补写实际的 Issue 状态和描述，但不能因此自动重跑 Claude Code。

## 10. 实现结构

```text
cacc_runner/
  src/cacc_runner/
    config.py          项目根、数据目录、Claude 命令和最大轮数
    task_package.py    v2 任务包和路径校验
    claude_code.py     CLI 子进程、stream-json 解析、取消和脱敏
    run_record.py      单个 JSON 运行记录和原子写入
    server.py          stdio MCP 服务与单 Worker 生命周期
  skills/cacc-supervisor/SKILL.md
  config/
  tests/
```

删除旧的 `worker.py`、`run_store.py`、`checks.py` 和 `linear_sync.py`，以及它们的测试。`pyproject.toml` 只保留 MCP 运行依赖。

## 11. 验收标准

1. 无 `ANTHROPIC_API_KEY` 时，桥接器能启动且 `runner_status` 正确报告 Claude CLI 可用性。
2. 桥接器调用 Claude Code 时使用 `-p` 和 JSON 流格式，不使用 `--bare` 或跳过权限的参数。
3. 合法 v2 任务包可启动；非法项目、越界路径、空验收标准在启动进程前被拒绝。
4. 一个未验收任务存在时，第二个任务包被拒绝；成功、失败、取消和中断均需显式验收。
5. Claude stdout/stderr、退出码、会话 ID和最终摘要可从本地报告读取，状态接口只返回短摘要。
6. MCP 只暴露五个桥接工具，不暴露 Linear 同步工具。
7. Codex 可通过已连接 Linear 插件更新一个总工单的 Issue 状态、计划、当前包和验收摘要。
8. 完整单元测试、stdio MCP 冒烟测试和一次无文件修改的真实 Claude Code 最小往返通过 CC Switch 完成。

## 12. 参考

- [Claude Code headless / CLI](https://code.claude.com/docs/en/headless)
- [Claude Code CLI 参考](https://code.claude.com/docs/en/cli-usage)
- [Codex MCP 配置](https://developers.openai.com/codex/mcp/)
- [CC Switch Claude-Codex 路由指南](https://github.com/farion1231/cc-switch/blob/main/docs/guides/claude-codex-routing-guide-en.md)

## 13. 实施验证（2026-09-16）

- 自动化测试、stdio MCP 冒烟和依赖检查通过。
- 真实 Claude Code 最小任务已通过 CC Switch 返回结构化结果，并经过 Bridge 的等待审阅和显式验收流程。
- Windows 上已处理 npm `claude.cmd` 的完整路径解析，以及多行 `-p` 提示被包装器截断的问题；回归测试覆盖这两个行为。
