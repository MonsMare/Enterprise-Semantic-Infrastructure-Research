---
name: cacc-supervisor
description: 使用本机 C&CC Claude Code Bridge 将目标拆成 Claude Code 任务包，并由 Codex 逐包验收；Linear 用一个总工单维护计划、进度和工单状态。
---

# C&CC Claude Code Bridge 任务监督

使用本 Skill 规划、启动和审阅由本机 C&CC Claude Code Bridge 执行的任务包。Codex 是计划、验收和 Linear 更新的唯一控制者；Claude Code 只执行当前任务包。

调用前先确认本会话实际提供了 Bridge MCP 工具。若工具缺失或 runner_status 显示 Claude CLI 不可用，不要声称任务已经启动。

## 必须遵守的边界

- 一次只启动一个任务包。前一包必须经 Codex 审阅并调用 accept_package 后，才可启动下一包。
- Claude Code 报告完成不等于验收。Codex 必须核对实际差异、任务范围、验收标准、测试证据和本地报告。
- 不自动重试、自动返工、自动启动替代 Worker 或自动放行下一包。失败、取消和中断都等待 Codex 的明确决定。
- Linear 只保存一个总工单中的计划、包编号、简要进度、Issue 状态和验收摘要。不要同步完整提示词、Claude 对话、差异、测试日志、凭据或本地工件路径。
- 使用已连接的 Codex Linear 插件同时更新总工单的 Issue 状态和描述；不要调用 Bridge 直连 Linear，因为 Bridge 没有 Linear 工具或凭据。
- 不在任务包中传递命令、工作目录、权限模式、模型设置或凭据。Claude Code 读取本机既有 CC Switch 设置。
- Windows 原生 Bridge 不提供 OS 级沙箱。任务包的 allowed_paths 是 Codex 指令和验收范围，不是对 Claude Code 的硬隔离。

## 任务包

每次只准备一个 cacc-task-package/v2：

~~~json
{
  "schema_version": "cacc-task-package/v2",
  "package_id": "KR-001",
  "project_id": "knowledge-runtime",
  "goal": "完成一个边界清楚、可独立验收的改动",
  "allowed_paths": ["knowledge_runtime/", "tests/"],
  "acceptance_criteria": ["目标行为可复现", "相关测试通过并已说明"]
}
~~~

- project_id 必须是 Bridge 本机配置中登记的别名；任务包不能指定仓库绝对路径。
- allowed_paths 只能是仓库内相对路径，列出当前包所需的最小范围。
- acceptance_criteria 必须可根据行为、差异和测试证据核对。

## 工作流

1. 调用 runner_status，确认 Claude CLI 可用、项目已登记且没有活动任务。
2. 形成总体计划和第一个任务包。使用 Linear 插件更新一个总工单的计划区块，写入包编号、目标、依赖和验收摘要，并将 Issue 设为 `Todo`。
3. 调用 start_package，只提交当前唯一任务包。以返回的 run_id 为准；启动成功后将总工单设为 `In Progress`。启动失败时保留 `Todo`，并写入简短原因。
4. 调用 get_package_status 读取短事件、最终摘要和本地报告路径。需要停止时调用 cancel_package。
5. Claude Code 结束后，更新总工单的当前进度为“等待 Codex 验收”，并保留 `In Progress`；检查实际工作区差异、允许路径、验收标准、测试结果和本地报告。不要把 Claude 的自述当作唯一证据。
6. 调用 accept_package 记录真实审阅说明并释放槽位。验收失败也应如实记录，不得把 Worker 终止结果改写为成功。若还有待启动包，将总工单设为 `Todo`；若总目标全部验收通过，设为 `Done`；用户终止总目标时才设为 `Canceled`。
7. 用 Linear 插件更新总工单的当前进度、验收摘要和实际 Issue 状态；再决定是否开始下一个包。

## Linear Issue 状态

C&CC Workbench 当前可用状态按以下方式使用：

- `Backlog`：尚未形成可执行任务包。
- `Todo`：任务包已经计划，等待启动。
- `In Progress`：Claude Code 正在执行，或 Codex 正在验收、处理失败或等待下一步决定。
- `Done`：总目标的全部任务包已验收通过。
- `Canceled`：用户已终止总目标。

当前工作区没有 `In Review` 或 `Blocked` 状态；审阅或阻塞原因写入总工单的“当前进度”。

## Linear 总工单模板

~~~markdown
<!-- cacc-plan:start -->
## 目标
[简短目标]

## 任务包
- KR-001｜进行中｜[目标、范围、验收摘要]
- KR-002｜等待 KR-001 验收｜[目标、范围、验收摘要]

## 当前进度
[当前包、阶段、简短结果、更新时间]

## Codex 验收记录
[包编号、结论、简短原因]
<!-- cacc-plan:end -->
~~~

Linear 暂时不可用不影响本地 Bridge 的 Worker 状态，也不能成为自动重跑 Claude Code 的理由。恢复后，Codex 应补写实际的 Issue 状态和简要进度。
