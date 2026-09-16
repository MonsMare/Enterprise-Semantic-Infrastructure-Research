# Codex–Claude Code–Linear C&CC Workbench 设计书

> 状态：Draft，等待用户评审
>
> 日期：2026-09-15
>
> 适用工作区：C&CC Workbench
>
> 适用代码项目：Knowledge Runtime 及后续由 C&CC Workbench 管理的代码项目

## 1. 设计摘要

本系统把 Codex 定义为长期运行的项目 Supervisor，把 Claude Code 定义为受控的代码执行 Worker，把 Linear 定义为任务状态、人工审批和审计记录中心，把 Runner 定义为连接 Linear 与 Claude Code 的最薄执行协调层。

Codex 在一个持续的 Supervisor 会话中负责：

- 与用户讨论目标、约束和设计；
- 生成并维护总体设计和迭代计划；
- 将计划拆解为独立任务包；
- 为每个任务选择预批准的执行策略；
- 读取 Claude Code 的交接报告并做验收判断；
- 自动处理可判断的失败、重试和返工；
- 在涉及范围变化、越权、安全、生产发布或长期不确定性时请求人工决策；
- 在整个计划完成后向用户汇报最终结果。

Claude Code 在隔离 Worker 中负责：

- 读取任务说明和项目代码；
- 编写、修改和重构代码；
- 运行单元测试、集成测试和构建；
- 按预批准策略部署到 Staging；
- 生成代码差异、测试证据、失败原因和下一步建议。

Claude Code 不拥有生产凭据，不直接执行生产发布，也不能自行扩大执行权限。

第一版只实现一个 Runner、一个 Codex Supervisor、一个 Linear 工作区和一个隔离 Worker，不引入 Kafka、Temporal、Kubernetes、独立控制台或复杂权限管理平台。

## 2. 设计目标

### 2.1 主要目标

1. 用户只需要与一个长期 Codex Supervisor 会话沟通。
2. Codex 不亲自承担代码构建、测试和部署的主要 Token 消耗。
3. Codex 可以在同一任务上下文中连续指挥多个 Claude Code Worker。
4. Claude Code 可以在没有人工频繁点击权限确认的情况下完成普通代码作业。
5. Linear 能够展示计划、任务、当前状态、Claude 结果、Codex 判断和人工决策。
6. Claude 的失败可以自动重试或返工，但不会无限循环。
7. 所有超出预批准范围的操作都能稳定地升级给 Codex 或人类。
8. 生产环境与 Claude Worker 的权限边界清晰、独立、可审计。

### 2.2 成功标准

一个已经批准的项目计划能够完成以下闭环：

~~~text
Codex 设计
  -> Linear 父任务和子任务
  -> Runner 启动 Claude Worker
  -> Claude 修改代码并测试
  -> Runner 回写结果
  -> Codex 验收、重试或升级
  -> Staging 验证
  -> Codex 汇报最终结果
~~~

用户不需要在 Claude Code 的每一次普通命令执行时手动批准，也不需要重新向 Codex 解释已经发生过的任务背景。

### 2.3 非目标

本设计第一阶段不包含：

- 自动把 Claude Code 直接连接到生产环境；
- 允许 Claude 持有长期生产密钥；
- 让 Codex 直接修改业务代码；
- 建设通用多租户 Agent 平台；
- 实现任意 Shell 命令的动态权限审批系统；
- 建设独立于 Linear 的复杂项目管理前端；
- 让多个 Claude Worker 共享同一个可变工作目录；
- 把完整模型推理过程写入 Linear。

## 3. 核心原则

### 3.1 Codex 是控制平面，Claude 是执行平面

Codex 只做设计、调度、判断、验收和升级；Claude 做代码、测试、构建和 Staging 操作。

Runner 不做产品判断，不修改业务代码，不自行决定放宽安全策略。Runner 只负责：

- 接收合法的任务状态变化；
- 创建隔离执行环境；
- 启动指定的 Claude Code Worker；
- 收集结构化事件和工件；
- 将结果写回 Linear；
- 执行固定的重试、超时和恢复规则。

### 3.2 Linear 是可见的事实账本，不是代码执行环境

Linear 保存任务状态、交接结果、人工决策和审计摘要。代码、日志和测试工件保存在代码仓库、Worker 工件存储或 CI 工件存储中，只在 Linear 中保存链接和摘要。

### 3.3 任务指令描述意图，执行 Profile 决定能力

任务描述说明“希望完成什么”，不能通过自然语言授予额外权限。

Runner 根据任务包中的 execution_profile 选择预定义能力集合。任务文本不能将 code Profile 变成 staging Profile，也不能将 staging Profile 变成 production Profile。

### 3.4 长期会话与持久化状态分离

Codex 使用一个长期 Supervisor 会话保持连续的讨论上下文，但会话不是唯一数据库。Linear 父任务中的状态摘要、决策记录和任务地图是可恢复的事实来源。

这样既能保持同一会话的连贯性，又能抵御上下文压缩、应用重启和 Runner 重启。

### 3.5 先少并发，再扩展

第一版默认一次只运行一个 Claude Worker。确认状态机、审计和恢复逻辑可靠后，才允许最多两个互不依赖的任务并行执行。

## 4. 总体架构

~~~text
┌─────────────────────────────────────────────────────────────┐
│                    Codex Supervisor 会话                   │
│  设计 / 计划 / 调度 / 验收 / 重试 / 人工升级 / 最终汇报      │
└───────────────────────┬─────────────────────────────────────┘
                        │ 读取和更新 Linear
                        ▼
┌─────────────────────────────────────────────────────────────┐
│                       Linear                                │
│  父任务 / 子任务 / 状态 / 评论 / 人工决策 / 审计摘要          │
└───────────────────────┬─────────────────────────────────────┘
                        │ Webhook + 定期对账
                        ▼
┌─────────────────────────────────────────────────────────────┐
│                       Runner                               │
│  Webhook 接收 / 幂等 / 调度 / Worktree / CC Switch 适配器    │
│  Worker 启动 / 事件收集 / 结果回写 / 超时和恢复              │
└───────────────────────┬─────────────────────────────────────┘
                        │ 一次性执行环境
                        ▼
┌─────────────────────────────────────────────────────────────┐
│              Linux VM 或 WSL2 隔离 Worker                   │
│  Claude Code / Worktree / 测试 / 构建 / Staging 包装器        │
└─────────────────────────────────────────────────────────────┘

生产发布链路：

Claude 完成 Staging
  -> Codex 验收
  -> 人工或受保护 CI/CD 审批
  -> CI/CD 使用生产凭据发布
~~~

## 5. 组件职责

| 组件 | 负责 | 不负责 |
|---|---|---|
| 用户 | 目标、设计批准、范围变化、生产发布决策 | 逐条批准普通开发命令 |
| Codex Supervisor | 设计、拆解、调度、判断、验收、升级 | 亲自编写业务代码和执行主要测试 |
| Linear | 状态、任务、评论、人工决策、审计摘要 | 保存密钥、执行命令、保存完整日志 |
| Runner | Webhook、幂等、隔离 Worker、事件转发、固定恢复策略 | 产品判断、动态放宽权限、修改业务代码 |
| CC Switch | Provider、API 路由、模型和 effort 的配置管理 | 任务状态、安全隔离、生产审批 |
| Claude Code Worker | 代码、测试、构建、Staging 作业 | 生产发布、扩大权限、修改 Runner 策略 |
| CI/CD | Staging 或 Production 的受保护发布 | 设计任务、代码修复和 Codex 判断 |

## 6. Codex Supervisor 设计

### 6.1 Supervisor 会话

每个项目计划对应一个 Codex Supervisor 会话。会话启动时绑定：

~~~text
supervisor_thread_id
linear_workspace_id
linear_project_id
root_issue_id
repository_id
repository_default_branch
~~~

Codex 会话始终围绕同一个父任务工作，不为每个 Claude 子任务创建新的 Codex 讨论上下文。

### 6.2 Supervisor 的持久化状态

父任务中维护四个固定区块：

~~~markdown
## Mission Brief
项目目标、非目标、关键约束和成功标准。

## Current Plan
任务树、依赖关系、当前阶段和完成比例。

## Decision Log
已经批准的技术选择、范围变化和人工决策。

## Current State
最近一次 Claude 交接、当前 Worker、失败次数、待处理问题和下一步。
~~~

Claude 的原始输出不全部注入 Codex 会话。Runner 先生成结构化交接摘要，Codex 只在需要时读取相关日志或代码差异。

### 6.3 Supervisor 唤醒方式

第一版使用固定的 Codex 定期检查机制：

1. Runner 把结果写入 Linear；
2. Linear 任务进入 Needs Codex Review 或 Needs Human；
3. Supervisor 检查有变化的任务；
4. Codex 作出决定并更新 Linear。

Runner 不直接依赖某个桌面 Codex 会话的内部调用接口。这样即使 Codex 应用重启，Linear 中的状态仍然完整。

后续可以将 Supervisor 迁移到一个受限的 API Agent，使用持久化 conversation 或 previous response state 保持多轮上下文；该 Agent 只暴露 Linear 和 Runner 控制工具，不暴露文件系统或 Shell。

### 6.4 Codex 可执行动作

Codex Supervisor 只能执行以下高层动作：

~~~text
accept_task
retry_same_task
create_rework_task
create_followup_task
select_preapproved_profile
request_human_decision
block_task
accept_result
prepare_production_promotion
~~~

Codex 不能执行以下动作：

~~~text
直接修改代码
直接运行任意 Shell
直接读取生产密钥
直接给 Claude 增加任意权限
直接在生产机器上部署
~~~

## 7. Linear 信息架构

### 7.1 工作区与项目

工作区使用已经连接的 C&CC Workbench。

第一版建议创建一个面向当前项目的 Linear Project：

~~~text
C&CC Workbench / Knowledge Runtime Delivery
~~~

后续每个独立产品或长期项目使用一个 Project；每个 Project 只有一个 Codex 父任务作为计划根节点。

### 7.2 Issue 结构

~~~text
Project
  └── Root Issue：项目目标与批准的总体计划
        ├── Task Issue：功能任务
        ├── Task Issue：测试任务
        ├── Task Issue：集成任务
        └── Task Issue：Staging 验证任务
~~~

父任务承载总体上下文，子任务承载可执行任务包。不要把所有 Claude 日志都创建成独立 Issue，普通过程信息使用评论。

### 7.3 最小状态集合

~~~text
Draft
Approved
Ready for Claude
Running
Needs Codex Review
Needs Human
Blocked
Accepted
~~~

环境通过标签表达，而不是继续增加状态：

~~~text
env:code
env:staging
env:production-request
kind:implementation
kind:test
kind:integration
kind:rework
~~~

### 7.4 评论约定

Runner 使用固定标题写入评论：

~~~text
[C&CC RUN STARTED]
[C&CC PROGRESS]
[C&CC HANDOFF]
[C&CC CODEX DECISION]
[C&CC HUMAN DECISION]
[C&CC BLOCKED]
~~~

每条评论包含 run_id 和 attempt，避免多个 Worker 的结果混淆。

## 8. 任务包协议

Codex 只有在任务包字段齐全后，才允许把子任务置为 Ready for Claude。

任务包的逻辑结构如下：

~~~json
{
  "schema_version": "task-package/v1",
  "task_id": "LIN-123",
  "root_issue_id": "LIN-100",
  "parent_task_id": null,
  "goal": "用一句话说明本任务的可验证目标",
  "scope": {
    "allowed_paths": ["knowledge_runtime/", "tests/"],
    "forbidden_paths": [".github/workflows/production/", ".env*"]
  },
  "acceptance_criteria": [
    "新增行为满足……",
    "现有测试全部通过",
    "新增回归测试覆盖……"
  ],
  "dependencies": [],
  "execution_profile": "code",
  "cc_switch_profile": "runner-default",
  "test_contract": {
    "required": true,
    "commands": ["runner-test-suite"]
  },
  "escalation_policy": {
    "max_auto_retries": 2,
    "ask_human_on_scope_change": true,
    "ask_human_on_production": true
  },
  "attempt": 1
}
~~~

其中 commands 不是让 Claude 直接运行任意命令，而是映射到 Runner 已注册的包装器名称，例如 runner-test-suite。

## 9. Runner 设计

### 9.1 Runner 的最小模块

Runner 作为独立项目实现，不放进 knowledge_runtime 包中。建议分成以下职责单元：

~~~text
linear_webhook.py       接收和验证 Linear Webhook
linear_client.py        读取和更新 Linear
state_store.py          SQLite 幂等、运行锁和状态
task_parser.py          解析和校验任务包
worktree_manager.py     创建和清理隔离 Worktree
cc_switch_adapter.py    调用已安装 CC Switch 的受支持入口
worker_launcher.py      启动隔离 Claude Worker
event_collector.py      收集进度、结果和失败事件
reporter.py             写回 Linear 和工件链接
reconciler.py           处理丢失 Webhook、孤儿 Worker 和超时
~~~

第一版可以仍然使用一个进程，通过模块边界保持清晰，不拆成多个服务。

### 9.2 Webhook 处理

Linear Webhook 接收器必须：

1. 使用原始 HTTP body 验证 HMAC 签名；
2. 检查时间戳，拒绝过期请求；
3. 使用 Linear-Delivery 做幂等去重；
4. 快速返回 HTTP 200；
5. 将实际处理交给本地任务队列或后台线程；
6. 定期通过 Linear API 对账，弥补丢失或延迟事件。

Linear 文档要求 Webhook 接收端使用公开 HTTPS 地址，并在约定时间内返回 200；失败会重试，因此 Runner 不能在 HTTP 请求中同步等待 Claude 完成。[Linear Webhooks](https://linear.app/developers/webhooks)

### 9.3 启动前预检

Runner 在启动 Claude 前必须检查：

~~~text
任务状态仍然是 Ready for Claude
任务尚未被其他 Runner 锁定
任务包 schema 正确
依赖任务已经完成
CC Switch Profile 存在且可用
模型和 effort 可识别
Worktree 路径正确
沙箱可用
网络策略可用
执行 Profile 与任务目标匹配
~~~

任一预检失败，Runner 不启动 Claude，而是写入 Blocked 和具体原因。

### 9.4 运行锁和幂等

每次执行使用：

~~~text
run_id = task_id + attempt + unique_execution_id
~~~

本地 SQLite 保存：

~~~text
linear_delivery_id
task_id
attempt
run_id
worker_id
status
started_at
finished_at
profile_fingerprint
worktree_path
commit_sha
~~~

同一个 Linear Webhook 重复到达时，Runner 只确认已处理，不重新启动 Worker。

## 10. CC Switch 接入设计

### 10.1 角色定位

CC Switch 是 Claude 的配置中心，负责：

- Provider 选择；
- API 路由；
- API Key 管理；
- 模型选择；
- effort 配置；
- 相关 Claude Code 配置切换。

Runner 不复制 API Key，不把 CC Switch 数据库同步到 Linear，也不实现第二套 Provider 管理系统。

### 10.2 适配器原则

cc_switch_adapter 只依赖安装版本提供的稳定入口：

~~~text
resolve_active_profile()
health_check()
launch_claude(task_context, execution_policy)
read_effective_metadata()
~~~

如果安装版本提供 CLI 或启动器，优先调用该入口；如果提供本地代理，Runner 让 Worker 通过受控代理访问模型；不通过解析内部 SQLite 或私有缓存文件实现核心功能。

### 10.3 Profile 锁定

任务开始时记录：

~~~text
profile_name
provider
model
effort
config_directory
proxy_endpoint
profile_fingerprint
~~~

运行期间禁止切换 Profile。如果检测到 Profile 指纹变化，Runner 停止当前任务并标记 Blocked，避免同一个计划中途更换模型或 Provider。

### 10.4 WSL2 和本地代理边界

如果 CC Switch 运行在 Windows，而 Claude Worker 运行在 WSL2：

- Windows 的配置目录不会自动成为 WSL2 的配置目录；
- Windows 的 127.0.0.1 不是 WSL2 Worker 的同一网络命名空间；
- 不应通过 Windows 互操作接口绕过 Worker 沙箱。

优先级从高到低为：

1. 在隔离 Linux Worker 内使用同等的 CC Switch 配置和代理；
2. 使用一个只转发模型 API 的受控网关；
3. 由 Runner 通过受支持的 Profile 导出机制注入临时配置。

API Key 不写入 Linear、Git、任务文本、命令行参数或普通日志。

## 11. Claude Code Worker 设计

### 11.1 Worker 生命周期

~~~text
创建临时 Worker
  -> 创建独立 Worktree
  -> 注入任务上下文和执行 Profile
  -> 运行 Claude Code
  -> 运行测试和构建
  -> 生成交接报告
  -> 保存工件
  -> 清理 Worker 或保留失败现场
~~~

失败任务默认保留 Worktree 和日志一段时间，方便 Codex 复盘；成功任务可以在工件确认后清理。

### 11.2 执行 Profile

#### code

允许：

- 读取和修改当前 Worktree；
- 运行项目测试；
- 运行构建；
- 访问代码仓库和模型代理。

禁止：

- Staging 部署；
- 生产网络；
- 读取宿主机凭据；
- 使用 Docker Socket；
- 使用任意 PowerShell 或 Windows 主机命令。

#### staging

在 code 基础上增加：

- 短时有效的 Staging 凭据；
- 固定的 deploy-staging 包装器；
- Staging 健康检查地址；
- Staging 日志读取权限。

#### production-request

Claude 不直接执行该 Profile。它只允许生成部署计划和发布请求，最终由受保护 CI/CD 和人工审批完成。

### 11.3 Claude 启动约束

无人值守 Worker 使用显式模型和 effort，并且使用不等待人工点击的权限模式：

~~~text
model: 来自 CC Switch 的实际模型
effort: max
permission mode: dontAsk
restricted: true
sandbox: required
unsandboxed commands: false
~~~

Claude Code 文档说明 dontAsk 会让未获授权的操作直接拒绝；allowedTools 本身并不等于限制全部可用工具，因此 Runner 还需要使用工具集合、拒绝规则和沙箱共同约束。[Claude Code 权限](https://code.claude.com/docs/en/permissions) [CLI 使用](https://code.claude.com/docs/en/cli-usage)

### 11.4 沙箱策略

最低要求：

~~~text
sandbox.enabled = true
sandbox.failIfUnavailable = true
sandbox.allowUnsandboxedCommands = false
write scope = current Worktree and required temporary build directory
network = provider proxy, source repository, approved Staging endpoints only
~~~

Claude Code 的沙箱通过操作系统级文件系统和网络边界限制 Bash 及其子进程；沙箱不可用时必须失败，而不能自动降级到无沙箱运行。[Claude Code Sandboxing](https://code.claude.com/docs/en/sandboxing)

原生 Windows 不作为正式隔离 Worker。开发验证可以使用 WSL2，接近生产的执行应使用一次性 Linux VM 或专用隔离容器主机。

## 12. Claude 事件和交接协议

Runner 将 Claude 的输出归一化为以下事件：

~~~json
{
  "schema_version": "run-event/v1",
  "run_id": "LIN-123-attempt-1-x7k2",
  "task_id": "LIN-123",
  "sequence": 12,
  "type": "test_failed",
  "phase": "verification",
  "summary": "provider contract test failed",
  "artifacts": [
    {
      "kind": "test-report",
      "uri": "artifact://runs/LIN-123/test-report.json",
      "sha256": "..."
    }
  ],
  "request": null,
  "created_at": "2026-09-15T00:00:00Z"
}
~~~

请求类事件使用固定结构：

~~~json
{
  "type": "needs_clarification",
  "request": {
    "category": "scope_change",
    "question": "当前实现需要修改任务范围之外的配置文件，是否允许？",
    "proposed_action": "修改 config/extra.yaml",
    "risk": "medium",
    "requires_human": true
  }
}
~~~

事件中只保存摘要、证据链接和请求信息，不保存隐藏推理链，也不把完整密钥环境变量写入日志。

### 12.1 交接报告

Claude 完成或失败时必须生成：

~~~markdown
## Result
完成 / 部分完成 / 失败

## Changes
修改的文件、commit 或 Worktree 信息

## Verification
执行的测试、构建和结果

## Evidence
日志、测试报告、截图或部署地址

## Risks
已知限制、未覆盖情况和潜在回归

## Recommendation
建议验收、重试、返工或人工决策
~~~

## 13. Codex 验收和决策策略

Codex 收到 Needs Codex Review 后按以下顺序判断：

1. 任务是否仍然符合原始目标；
2. 所有验收标准是否有证据；
3. 测试失败是代码问题、环境问题还是任务描述问题；
4. 是否修改了允许范围之外的路径；
5. 是否触及安全、凭据、生产或范围扩张；
6. 是否可以用当前任务包自动重试；
7. 是否需要创建一个新的返工任务。

### 13.1 自动处理矩阵

| 情况 | Codex 默认决定 | 最大次数 |
|---|---|---:|
| 测试失败且失败原因明确 | 生成修复说明并重试当前任务 | 2 |
| 临时网络、依赖或 Worker 启动失败 | 重试原任务 | 2 |
| 任务描述缺少非敏感信息 | 更新任务包后重试 | 1 |
| 需要修改任务范围之外的文件 | 请求人工确认 | 0 |
| 请求新的网络域名或凭据 | 请求人工确认 | 0 |
| 请求生产权限 | 转生产审批流程 | 0 |
| 连续失败且原因不稳定 | 创建返工任务并暂停原任务 | 0 |
| 验收标准全部满足 | 接受任务并推进依赖任务 | - |

## 14. 并行、依赖和合并策略

### 14.1 并行条件

两个任务只有同时满足以下条件才允许并行：

- 修改路径基本不重叠；
- 没有未完成的数据迁移或接口契约依赖；
- 测试环境不会互相污染；
- 每个任务使用独立 Worktree；
- 最终存在一个明确的集成任务。

### 14.2 合并策略

Runner 不自行判断代码是否应该合并。它只报告每个 Worker 的 commit。

Codex 验收各子任务后，创建一个集成任务，由 Claude Worker：

1. 合并或 cherry-pick 已接受的 commit；
2. 解决冲突；
3. 运行完整测试；
4. 生成最终集成交接报告。

## 15. 失败、超时和恢复

### 15.1 Webhook 重复

使用 Linear-Delivery 去重。已处理事件不重新启动 Worker。

### 15.2 Runner 重启

Runner 启动时扫描本地 SQLite：

- Running 且 Worker 仍存在：恢复监控；
- Running 但 Worker 不存在：标记 environment_failed；
- Running 超过超时时间：终止并进入重试判断；
- Needs Codex Review：不重复执行，只等待 Supervisor。

### 15.3 Claude 进程崩溃

保留：

~~~text
run_id
Worker 日志
Worktree 路径
最后 commit
退出码
最后一个结构化事件
~~~

Codex 根据退出码和最后事件决定重试或升级。

### 15.4 任务循环保护

同一任务默认最多两次自动重试。超过上限后必须进入 Needs Human 或创建新的返工任务，禁止自动无限重试。

## 16. 安全设计

### 16.1 威胁模型

主要风险不是 Claude 不理解任务，而是：

- 仓库中存在提示注入内容；
- 任务文本诱导执行未授权命令；
- Shell 命令通过参数、重定向或子进程扩大影响；
- Worker 读取宿主机凭据；
- API Key 被日志或 Linear 评论泄露；
- Staging 凭据被用于生产；
- Webhook 被伪造或重复投递；
- 多个 Worker 共享目录造成互相污染；
- Claude 任务无限重试造成费用或资源失控。

### 16.2 必须控制的边界

1. 任务提示词只表达工作意图，不作为安全授权。
2. Worker 使用 fail-closed 沙箱。
3. Worker 不挂载生产密钥、宿主机用户目录或 Docker Socket。
4. 网络使用白名单，不允许任意外连。
5. CC Switch API Key 不写入 Linear、Git 或普通日志。
6. Staging 和 Production 使用不同凭据和不同网络入口。
7. Codex 只能选择预批准 Profile，不能创建任意权限规则。
8. Webhook 必须校验签名和时间戳。
9. Runner 必须有运行锁、超时和最大重试次数。
10. 生产发布必须由受保护 CI/CD 环境完成。

### 16.3 任务指令与沙箱的关系

任务指令解决“Claude 应该做什么”；沙箱解决“Claude 即使判断错误最多能影响什么”。两者不能互相替代。

对于第一版，不建设复杂的逐命令权限编辑器，只使用三个固定执行 Profile、沙箱、网络白名单、固定包装器和 dontAsk。

## 17. Staging 和 Production 部署

### 17.1 Staging

Claude 可以通过固定包装器完成：

~~~text
build
push staging artifact
deploy staging
health check
collect staging logs
~~~

Staging 凭据只在 Worker 生命周期内可用，并且限定到 Staging 资源。

### 17.2 Production

Claude 不获得生产部署权限。完整流程为：

~~~text
Claude 完成代码和 Staging
  -> Codex 验收
  -> Codex 生成 Production Promotion 请求
  -> 人工确认或受保护审批规则通过
  -> CI/CD 使用生产凭据发布
  -> CI/CD 结果回写 Linear
~~~

Codex 可以准备和提交发布请求，但不在本地直接使用生产凭据。

## 18. 可观测性和审计

### 18.1 Linear 中记录

每次运行写入：

~~~text
run_id
task_id
attempt
worker_id
CC Switch profile 名称
模型和 effort
Claude Code 版本
sandbox 状态
policy fingerprint
commit SHA
测试摘要
Staging 地址或 CI 工件地址
Codex 决定
人工决策
~~~

### 18.2 Runner 日志

Runner 保存：

- Webhook 接收结果；
- 状态迁移；
- Worker 生命周期；
- 退出码和超时；
- 结构化 Claude 事件；
- 工件 SHA-256；
- 重试和恢复原因。

日志必须进行敏感信息过滤。出现疑似 API Key、Authorization header 或生产密钥时，Runner 应进行脱敏并触发安全事件。

### 18.3 关键指标

第一版只记录：

~~~text
任务完成率
自动重试率
人工升级率
平均任务耗时
Worker 崩溃率
沙箱预检失败率
Webhook 重复率
Staging 验证通过率
~~~

## 19. 分阶段落地计划

### Phase 0：设计和手工演练

目标：验证 Linear 状态、任务包格式和人工升级流程。

结果：

- 建立父任务和子任务模板；
- 手工模拟 Ready for Claude、Needs Codex Review 和 Needs Human；
- 确认 Codex 的验收报告格式。

### Phase 1：Runner 骨架

目标：不启动真实 Claude，先完成事件和状态闭环。

结果：

- Webhook 签名验证；
- SQLite 幂等；
- 状态迁移；
- Fake Worker；
- Linear 回写；
- Webhook 对账。

### Phase 2：真实 Claude Code Worker

目标：在 code Profile 中完成代码和测试。

结果：

- Worktree 隔离；
- CC Switch 接入；
- 模型和 effort 预检；
- WSL2 或 Linux Worker；
- Claude 交接报告；
- 自动重试和失败现场保留。

### Phase 3：Codex Supervisor 自动验收

目标：Codex 自动处理普通失败和任务推进。

结果：

- 固定 Supervisor 会话；
- Linear 状态检查；
- 自动验收；
- 自动返工任务；
- 人工升级问题。

### Phase 4：Staging

目标：Claude 在受限 Staging Profile 中完成部署和集成验证。

结果：

- Staging 部署包装器；
- Staging 凭据隔离；
- 健康检查；
- CI 或运行环境结果回写 Linear。

### Phase 5：Production Promotion

目标：只允许受保护 CI/CD 完成生产发布。

结果：

- Production 环境保护规则；
- 人工或 Codex 审批记录；
- 生产凭据不进入 Claude Worker；
- 发布结果回写 Linear。

## 20. 验收标准

实现完成后必须满足：

1. 将 Linear Issue 改为 Ready for Claude 能启动一次且仅一次 Claude Worker。
2. 重复 Webhook 不会重复启动 Worker。
3. Worker 使用独立 Worktree，不能修改主工作目录。
4. CC Switch Profile、模型和 effort 能被记录并在运行期间锁定。
5. 沙箱不可用时任务直接进入 Blocked，不会降级执行。
6. Claude 普通测试流程不需要连续人工点击权限。
7. 未授权操作会转为结构化拒绝事件，而不是无限等待。
8. 测试失败可以自动重试，最多两次。
9. 超出任务范围、请求新凭据或涉及生产时会升级人工。
10. Codex 可以在同一 Supervisor 会话中连续处理多个 Claude 子任务。
11. Codex 可以从 Linear 的父任务状态恢复上下文。
12. Linear 能看到任务进度、测试结果、commit、失败原因和 Codex 决定。
13. Claude Worker 没有生产密钥和生产部署权限。
14. 生产发布只能由受保护 CI/CD 执行。

## 21. 已确定的默认决策

| 项目 | 默认决策 |
|---|---|
| Codex 角色 | 长期 Supervisor 会话 |
| Claude 角色 | 隔离代码执行 Worker |
| 任务系统 | C&CC Workbench 中的 Linear Project |
| 编排方式 | 一个薄 Runner，不拆微服务 |
| 配置管理 | 直接复用 CC Switch，通过适配器接入 |
| 第一版执行环境 | WSL2 或 Linux Worker，原生 Windows 不作为正式沙箱 |
| 第一版并发 | 1 个 Worker |
| 后续并发 | 最多 2 个独立 Worktree Worker |
| 权限策略 | 任务指令 + 固定 Profile + 沙箱 + dontAsk |
| 代码目录 | 每个 Claude 任务独立 Worktree |
| 失败重试 | 自动最多 2 次 |
| Staging | Claude 可执行，但使用独立凭据 |
| Production | Claude 不直接执行，受保护 CI/CD 发布 |
| 人工入口 | Codex 对话 + Linear Needs Human |
| 生产凭据 | 不进入 Codex、Linear 或 Claude Worker |

## 22. 需要在实现前确认的具体事项

整体架构已经确定，实施前只需要核实以下环境事实：

1. 当前安装的 CC Switch 具体发行版是否提供稳定的 CLI、启动器或本地代理接口；
2. Claude Worker 最终使用 WSL2 还是独立 Linux VM；
3. 当前 Linear 团队的状态名称和 Webhook 管理权限；
4. 当前代码仓库的默认分支、测试入口和 Staging 部署入口；
5. Codex Supervisor 第一版使用 Codex 定期检查机制，还是使用独立的 API Agent。

这些事项不改变总体架构，只决定适配器和部署方式。

## 23. 外部参考

- [Linear Webhooks](https://linear.app/developers/webhooks)
- [Linear GraphQL API](https://linear.app/developers/graphql)
- [Claude Code Model Configuration](https://code.claude.com/docs/en/model-config)
- [Claude Code Permissions](https://code.claude.com/docs/en/permissions)
- [Claude Code CLI Usage](https://code.claude.com/docs/en/cli-usage)
- [Claude Code Sandboxing](https://code.claude.com/docs/en/sandboxing)
- [Claude Code Configuration](https://code.claude.com/docs/en/configuration)
- [CC Switch 示例项目](https://github.com/Hortus-Edenensis/cc-switch)
- [OpenAI Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)

