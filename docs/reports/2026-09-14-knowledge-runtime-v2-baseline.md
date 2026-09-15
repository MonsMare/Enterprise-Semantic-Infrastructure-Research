# Knowledge Runtime v2 POC 基线与验证记录

## 已实现的设计闭环

v2 已将知识运行时拆成四个稳定边界：

| 边界 | 当前实现 | 作用 |
|---|---|---|
| Provider/L1 | `LocalProvider`、`MinerUProvider`、`ParserRouter`、`QualityGate` | 将本地解析或 MinerU 输出统一成 `DocumentIR`，记录页、段落、表格、章节和解析质量 |
| Canonical | `InMemoryCanonicalStore`、`PostgresCanonicalStore`、`SchemaMigrator` | 以 document/revision/element 为事实来源；只有索引成功后才切换 current revision |
| Artifact | `FilesystemArtifactStore`、`S3ArtifactStore` | 按 document/revision 保存原始文件和解析产物，写入后以 SHA-256 保证不可变 |
| Index/L3 | `InMemoryIndexBackend`、`OpenSearchIndexBackend`、`ChunkSetBuilder` | 只保存可重建的检索派生数据，返回 `EvidenceRef`，不成为回答事实来源 |
| Context/Agent | 五个 Context 原语、`QwenAgentClient`、`AgentRetrievalLoop` | Search 只给引用和元数据，`get_evidence` 才读取规范 Evidence，回答逐句校验引用 |

语义 overlay 目前是可选 profile。`SemanticProposal` 先处于 proposal 生命周期，只有带
Evidence 的 proposal 才能进入 VERIFIED/CERTIFIED；Neo4j 和 OpenMetadata 不能阻塞核心
入库、Evidence 读取或检索。

## 验证结果

在隔离分支 `v2-core-implementation` 上执行：

```text
python -m pytest -q                         172 passed
python -m compileall knowledge_runtime      passed
git diff --check                             passed
docker compose -p kr-v2 -f deploy/kr-v2.compose.yml --profile core config  passed
```

测试覆盖了 Evidence 完整内容 hash、跨 revision 拒绝、Provider 私有 egress、质量重试、
Canonical 发布事务、Artifact 不可变性、embedding key 隔离、ChunkSet provenance、混合
索引、入库幂等、失败 revision 不替换 current、语义 proposal 生命周期、Context budget、
多轮 query rewrite、Qwen Agent 工具循环、局部 citation 校验、旧 Locator 适配、迁移和
Compose 资源前缀。

离线指标脚本：

```powershell
python -m benchmarks.v2.run_benchmark `
  --cases .\benchmarks\v2\cases\actuarial-v2.jsonl `
  --corpus .kr-data\actuarial-downloads `
  --offline `
  --output .kr-data\v2-actuarial.json
```

不提供 `--corpus` 时脚本会明确写出 `measured=false`，不会把缺少本地公开文档误报成
检索成功；提供下载后的精算语料后，脚本才计算 source Hit@k、Evidence phrase
coverage 和 Evidence Hit@k。当前仓库没有把第三方 PDF 放入 Git，因此本次代码验证不
声称已经完成公开文档的实测分数。

历史 SQLite 混合检索基线仍保留在
`docs/reports/2026-09-14-hybrid-retrieval-results.md`：10 份公开来源、1,412 个
chunk 的 source Hit@1 为 4/4；Evidence Hit@3 为 3/4；5,000 份合成语料的 search
p50/p95 为 732.5/767.6 ms。该数据属于旧 SQLite chunk 原型，不能直接当作 PostgreSQL+
OpenSearch v2 的性能结论，但可以作为迁移前对照。

## 私有化与 egress

默认 `RuntimeConfig` 是 fail-closed：远程 parser、embedding、Agent 都关闭。v2 的
Agent 只读取 `QWEN_LLM_API_KEY`，embedding 只读取 `DASHSCOPE_API_KEY`，模型分别固定
为 `qwen3.8-max` 和 `qwen3.7-text-embedding`。Compose core 的服务和卷全部以
`kr-v2-` 命名；脚本没有全局 stop、prune 或网络/卷清理命令。只有显式打开对应
`KR_ALLOW_REMOTE_*` 标志后，benchmark 才允许产生 egress 事件。

## 尚未关闭的验收项

1. 需要下载并固定 ASOP、EIOPA 等公开精算文档，运行 `--corpus` 实测 source/Evidence
   指标和规模曲线。
2. 需要在授权环境显式打开 `KR_ALLOW_REMOTE_AGENT=true`，运行真实 qwen3.8-max 多轮
   Agent benchmark；离线单元测试不会伪造该指标。
3. PostgreSQL、MinIO/S3、OpenSearch 的连接已经有实现和 Compose 入口，还需要在专用
   `kr-v2` 环境做一次集成运行，确认 schema migration、备份恢复和索引重建的运维数据。
4. Docling/Unstructured 的版式、扫描 OCR 和图片 Evidence 需要安装对应 local-parser
   extra 后做真实文件验收；缺少依赖时 v2 会显式拒绝复杂文档，不会静默降级。

