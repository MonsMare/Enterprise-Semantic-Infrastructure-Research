# C&CC Runner 第一版实施计划

## Goal

根据第一版设计实现独立的 `cacc_runner` Python 子项目：由 Codex Skill 组织任务包，通过本机 stdio MCP 显式启动一个 Claude Agent SDK Worker；Runner 保存本地状态和证据，Codex 逐包验收；Linear 仅镜像计划和阶段进度。

## Architecture

- 将 Runner 放在 Knowledge Runtime 仓库根目录 `cacc_runner/`，不改根 `pyproject.toml` 或 `knowledge_runtime/`。
- 通过小型 Python 模块分离配置、包校验、SQLite 状态、固定检查、Agent SDK 适配、Linear 同步和 MCP Server。
- 所有 Worker 会话都经过一个运行管理器；SQLite 的活动槽位保证同一时间至多一个未验收任务包。MCP Server 重启后，在 Runner 数据目录排他锁确认没有另一活跃实例后，把未结束的 `running` 记录标为 `awaiting_codex_review`、结果 `interrupted`，不自动重跑。
- Worker 工具集合限于 `Read`、`Glob`、`Grep`、`Edit`、`Write` 及内置 `run_project_check`；路径 Hook 再按仓库根目录、包允许路径和敏感文件规则校验每次工具访问。固定检查使用静态 argv，不经过 Shell。
- Linear GraphQL 写入固定根 Issue 描述中的标记区块，失败任务放入 SQLite 待同步队列，不影响本地执行结果，也不重启 Worker。
- Skill 源文件随 Runner 放在 `skills/cacc-supervisor/SKILL.md`；示例配置不带秘密。提供 Codex MCP 配置样例与安装说明。

## Tech Stack

- Python 3.11+，`mcp` Python SDK、`claude-agent-sdk`、标准库 `sqlite3` / `asyncio` / `urllib`。
- `pytest` 用于独立子项目测试。
- Claude SDK、Linear HTTP 和系统时钟通过窄接口注入；无真实 API key 的测试使用假传输和假 Worker。

## Spec

- 规范设计书：`docs/superpowers/specs/2026-09-15-codex-claude-linear-workbench-design.md`。
- 设计书在 C&CC 工作台镜像一份，实施前后都必须与此规范副本保持字节一致。
- 第一版工具：`runner_status`、`sync_plan_to_linear`、`start_package`、`get_package_status`、`cancel_package`、`accept_package`。
- 不实现 Linear webhook/轮询、自动重试、并行 Worker、自动合并、部署或生产操作。

## Global Constraints

- 中文用户说明、文档和 Skill 内容。
- 一个任务包结束并进入 Codex 验收后，必须显式 `accept_package` 才释放活动槽位；失败、阻塞和取消都不自动重试或放行。
- Worker 不接收任意路径、任意 shell 文本、Linear 凭据或未配置 MCP Server。
- Windows 原生不具备 Claude 命令沙箱；实现不得宣称 OS 级隔离，错误提示要求仅在可信开发仓库使用。
- 配置及报告不得写入 API 密钥；日志仅写 stderr/本地文件，不向 stdio MCP 的 stdout 输出普通日志。
- 新逻辑先增加能复现预期的失败测试并观察失败，再实现最小代码使测试通过；收尾执行本子项目全测试与项目边界核对。

## Tasks

### 1. 建立 Runner 子项目与配置/任务包协议

**Files:** `cacc_runner/pyproject.toml`, `cacc_runner/src/cacc_runner/__init__.py`, `config.py`, `task_package.py`, `tests/test_config.py`, `tests/test_task_package.py`。

1. 先写测试：登记项目与检查别名配置解析；拒绝缺字段/空验收条件/未知项目或检查；允许路径必须相对、规范化后留在注册仓库内；拒绝绝对路径、`..`、仓库外符号链接和敏感凭据路径。
2. 用 bundled Python 和 pytest 运行新测试，确认 RED。
3. 实现 `ProjectConfig`、静态 `CheckConfig`、严格的 TaskPackage v1 解析及解析后规范路径。
4. 运行 `python -m pytest -q`（工作目录 `cacc_runner`），确认 GREEN。

### 2. 实现持久化运行状态和单 Worker 槽位

**Files:** `run_store.py`, `tests/test_run_store.py`。

1. 先写 SQLite 测试：状态迁移、同一时刻一个未验收包、并发启动拒绝、显式验收才释放、失败/取消不自动放行、重启恢复时活动记录变为 awaiting review/interrupted、最新待同步 Linear 区块的幂等保存。
2. 确认测试 RED，再实现事务化状态迁移和 SQLite schema；数据库路径由配置给定，创建时限制仅当前用户访问（平台可支持时）。
3. 全部测试 GREEN，并验证关闭/重开数据库后状态、事件和待同步数据仍可读取。

### 3. 实现固定检查执行器和 Agent SDK Worker 适配

**Files:** `checks.py`, `worker.py`, `tests/test_checks.py`, `tests/test_worker.py`。

1. 先写假进程与假 SDK 测试：检查别名映射固定 argv/cwd、拒绝别名外命令、超时/退出码/输出留在本地；Worker 不提供 Bash 或外部 MCP，工具白名单固定；Hook 拒绝越权/敏感路径；结果消息生成阶段事件和摘要；取消调用 `interrupt()` 并读取至终止结果后清理客户端。
2. 确认 RED，然后实现 argv-only `asyncio.create_subprocess_exec` 检查器及可注入 SDK 边界。延迟导入 SDK，以便缺 SDK 时 MCP 状态仍可报告依赖缺失，启动任务时给出明确错误。
3. 全部 GREEN；不连接真实 Claude API。

### 4. 实现 Linear 计划/进度镜像

**Files:** `linear_sync.py`, `tests/test_linear_sync.py`。

1. 先写 fake GraphQL transport 测试：只向固定 Issue 查询/更新 description；根据成对标记替换唯一管理区块、保持区块外文字不变；重试幂等；拒绝缺失/重复标记；处理 HTTP、GraphQL errors 和 `success=false`；请求中只含计划/阶段摘要，不含完整对话、diff、日志或 token。
2. 确认 RED，再实现 urllib transport 注入接口、GraphQL 查询与 mutation、确定性 markdown 区块渲染；凭据只由环境变量在调用时读取，不回显。
3. 全部 GREEN；真实 Linear API 调用留给配置凭据后的手动验收。

### 5. 连接 Worker 生命周期与 MCP stdio 工具

**Files:** `server.py`, `tests/test_server.py`。

1. 先写测试：MCP 初始化/工具列表/工具调用；启动前验证任务包并取得槽位；重入拒绝；状态可查询；取消后不自动重试；只能在 awaiting_codex_review 时 accept；同步失败不重启任务且入队；MCP stdout 不输出普通日志。
2. 确认 RED，再集成运行协调器、SQLite、配置、Worker、Linear Sync 和官方 Python MCP SDK；支持通过 MCP progress context 汇报简短阶段摘要。
3. 用 MCP SDK 的本地测试客户端调用全部工具，确认 GREEN。

### 6. 完成 Skill、示例配置和安装/操作文档

**Files:** `skills/cacc-supervisor/SKILL.md`, `config/projects.toml.example`, `config/codex-mcp.toml.example`, `README.md`, C&CC 设计书镜像。

1. Skill 明确一次只发一个任务包、字段/路径限制、调用顺序、逐包差异与测试审阅、显式 accept、Linear 仅计划和进度；示例配置指向占位项目路径，不含凭据。
2. README 记录安装、环境变量、Codex MCP 注册方式、Windows 原生限制、测试与真实 API 的手动验收步骤。
3. 修改规范设计书与 C&CC 镜像中的实现信息仅在必要时同步；完成后检查二者 SHA256 一致。
4. 执行安装、全量 pytest、MCP 初始化/工具列表 smoke test 与仓库边界检查；不能声称未执行的真实 API 验收已经通过。
