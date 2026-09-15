# Knowledge Runtime：规模化方案与可复用组件

这份报告回答两个问题：当前延迟增长的根因是什么，以及哪些能力应直接采用成熟开源组件，哪些能力仍然属于 KR 的核心差异化。

## 先看当前事实

当前 POC 的数据路径是：解析后的 Markdown 进入 SQLite，切成带标题路径、行号和 revision 的 chunk；SQLite FTS5 负责词项候选；本地字符 n-gram 稀疏向量保存在 BLOB 中；混合检索阶段再对当前 chunk 做精确余弦扫描和全局排序。

这条路径适合验证 Evidence、Locator 和 benchmark 契约，但不是大规模索引。它的主要成本是：

1. `search_chunk_vectors()` 每次查询都读取当前范围内的全部向量，计算相似度后再排序，查询工作量是 `O(chunk 数)`，缓存只能减少部分读取开销，不能把扫描变成索引查找。
2. 每个新 revision 都会逐 chunk 写正文、FTS 投影和向量；SQLite 索引页、事务提交和 Python 对象构造叠加后，建库曲线会比文档数更陡。
3. Agent 端还有独立的模型延迟。最近的 Qwen 3.8 真实 benchmark 中，精确问题来源 Hit@1 为 4/4，但严格回答通过率为 1/4；search 总耗时约 1.08 秒，read 约 0.036 秒，而端到端 p50/p95 为 16.9/50.0 秒。模糊问题集合通过率为 2/6，search/read 约 1.35 秒，而端到端 p50/p95 为 27.2/112.8 秒。后者主要是重复改写、重复搜索、缺少逐 claim Evidence 引用和没有及时停止造成的。

因此目前不是单一的“RAG 不够好”问题，而是索引平面和 Agent 控制平面各有一个瓶颈。

## 业界已经解决的部分

| 能力 | 可直接复用的方案 | 适合 KR 的用法 |
|---|---|---|
| PDF、扫描件、表格、公式和版面解析 | 继续使用 [MinerU](https://github.com/opendatalab/MinerU)；本地隐私路径可评估 [Docling](https://docling.ai/)、[Unstructured](https://docs.unstructured.io/)、[Apache Tika](https://tika.apache.org/) | Provider 只负责统一输入、解析 provenance 和失败重试，不再自己实现 OCR、表格和版面算法 |
| 倒排、BM25、段落高亮、过滤和混合检索 | [OpenSearch vector search](https://docs.opensearch.org/latest/vector-search/)、[Elasticsearch hybrid search](https://www.elastic.co/docs/solutions/semantic-search/get-started/semantic-search) | 需要一个可运维的搜索服务、过滤条件多或希望一次请求完成 BM25+dense/sparse+RRF 时采用 |
| 私有部署的向量检索和 metadata 过滤 | [Qdrant](https://qdrant.tech/documentation/guides/) | 最适合 KR POC 的独立 index plane：单机运行即可，支持 HNSW、payload index、sparse/dense/named vectors 和后台 segment 优化 |
| 已经依赖 PostgreSQL 的团队 | [pgvector](https://github.com/pgvector/pgvector) | 把资产元数据、revision、chunk 和向量放在同一数据库；小到中等规模优先 HNSW，批量导入或内存受限时评估 IVFFlat/halfvec |
| 更大规模的分布式向量库 | [Milvus](https://blog.milvus.io/docs/index-explained.md) | 百万级以上向量、分片、DiskANN/IVF/HNSW 或稀疏倒排需求明显后再引入；当前 POC 使用它会增加运维面 |
| 本地或对象存储上的嵌入式检索 | [LanceDB](https://docs.lancedb.com/indexing) | 离线、单机或数据放在对象存储时使用 IVF/HNSW/PQ 和 FTS；它可以作为不引入常驻服务的中期路线 |
| 轻量本地 POC | SQLite [FTS5](https://www.sqlite.org/fts5.html)；[sqlite-vec](https://github.com/asg017/sqlite-vec) 可作为实验性向量扩展 | 保留现有 FTS5；sqlite-vec 仍处于 pre-v1，适合验证接口，不应直接当作大规模生产索引 |
| 检索算法库 | [Faiss](https://github.com/facebookresearch/faiss/wiki/Faiss-indexes)、[DiskANN](https://github.com/microsoft/DiskANN) | 只在要自建索引服务时使用；它们解决 ANN 算法，不替 KR 解决 revision、过滤、持久化、Evidence 和运维 |
| 摄取缓存、层级 chunk 和检索编排 | [LlamaIndex IngestionPipeline](https://docs.llamaindex.ai/en/stable/module_guides/loading/ingestion_pipeline/)、[Haystack retrievers](https://docs.haystack.deepset.ai/docs/retrievers) | 借用缓存、parent/child、BM25+dense、RRF 和 adapter 设计；不要把整个框架的对象模型直接变成 KR 公共契约 |

Lucene 系列搜索引擎采用不可变 segment，新增文档写入新 segment，后台 merge 清理删除内容；这正是“增量写入、后台合并、查询不全库重建”的成熟路径。不要在 KR 中继续通过每次建库重算全量向量来模拟它。

## 推荐的 KR 目标架构

KR 应该成为“知识资产和证据的控制层”，把索引引擎当作可替换的执行层：

```text
Provider(MinerU/Docling/local)
  -> ParseArtifact + content_hash + provenance
  -> Revision/Chunk store (SQLite/PostgreSQL)
  -> Index adapters (FTS / sparse / dense / metadata)
  -> candidate fusion + rerank
  -> Locator-aware Evidence read
  -> Agent answer + claim-local citations
```

KR 自己必须稳定维护以下契约：

- `asset/revision/chunk` 的不可变身份、content hash、解析器版本、chunker 版本和索引版本。
- 可重放的 Locator（revision、chunk、行号/页号/表格路径），以及只能由 `read` 产生的 Evidence。
- 过滤范围、来源权限接口、删除和 revision 切换语义；即使暂时不做鉴权，也要让索引结果可以回到 canonical store 校验。
- idempotent ingestion：相同 content hash 不重复解析和向量化；只处理变化的 revision；失败任务可重试；批量写入后再发布新 index version。
- 质量和延迟指标：source Hit@1/3、Evidence Hit@1/3、Evidence coverage、claim citation coverage、ingestion throughput、search p95、Agent e2e p95。

### 建议的近期落地组合

在 10 万 chunk 以内，建议采用“SQLite 继续保存 metadata、revision、正文和 Evidence；Qdrant 单机保存 sparse/dense 检索索引；SQLite FTS5 保留 lexical 候选”的组合。Qdrant 的 payload index 用于 source/revision 等过滤，KR 在两路候选上做 RRF，再从 SQLite 读取证据。这样不需要一次性迁移资料库，也不把证据正文复制到向量库。

如果希望只维护一个搜索服务，下一选择是 OpenSearch：BM25、向量、RRF、过滤、highlight 和 explainability 都在一个查询面内。它的运维和资源开销高于 Qdrant 单机，但更适合后续需要复杂过滤、观测和多租户隔离的阶段。

当前仍可保留纯 SQLite 路径作为回退和回归基线；它的边界应明确为“小规模或离线模式”，而不是未来的主索引。

## 建库和检索的具体改法

建库侧：

1. 先按 `content_hash` 查 manifest，未变化的资产直接复用 parse artifact、chunk 和向量。
2. 解析、chunk、向量化、索引 upsert 使用异步队列和批量提交；每批完成后写 checkpoint，避免一个大事务拖住所有文档。
3. 新 revision 先写入 shadow index，校验 chunk 数和索引版本后再原子切换 `current_revision_id`；删除和旧 revision 清理由后台 compact/merge 完成。
4. Qwen 只用于需要语义判断的 query rewrite、rerank 或回答；不要在每次建库时用大模型逐 chunk 反复生成不可缓存的知识摘要。

检索侧：

1. 先应用 scope、来源、日期和未来的 ACL 过滤。
2. FTS/BM25 与 sparse/dense ANN 并行取有限候选（例如每路 50–200），用 RRF 或可校准的融合排序。
3. 对前 20 个候选做 parent/neighbor expansion，再进行一次轻量 rerank；只读取最终候选的 Evidence。
4. Query rewrite 最多 1–2 次；若新增查询没有提高来源或 Evidence 分数，立即停止。答案生成前检查每个外部事实是否有就近 Evidence 引用。
5. 对 query rewrite、候选结果、Evidence 读取和最终回答分别缓存和计时，不能把模型等待时间归因到 KR search。

## 分阶段路线

**阶段 A：先冻结 POC 契约（现在）**

保留 MinerU、SQLite canonical store、现有 benchmark 和 Evidence/Locator API；把当前 Python 全量扫描标记为 baseline。新增 `IndexBackend` 接口和 Qdrant adapter，不改 Agent 能看到的 `list/find/search/read/stat` 契约。

**阶段 B：10 万 chunk 以内**

使用 Qdrant 单机的 HNSW 或 sparse index，批量 upsert，payload 过滤；SQLite FTS5 继续提供词项候选。目标是让 search p95 与总 chunk 数脱钩，并让增量导入只处理变化的 revision。

**阶段 C：10 万到百万级**

根据运维偏好在 OpenSearch、Milvus、LanceDB 中选择：需要统一全文和混合查询选 OpenSearch，需要向量分片和多种 ANN 选 Milvus，需要离线/对象存储优先选 LanceDB。此时再做冷热分层、压缩、分片和后台 merge。

**阶段 D：质量闭环**

建立 query rewrite、candidate、Evidence、answer 的分层 benchmark。把“来源找对但回答没逐 claim 引证”和“为了找证据而循环 8 次”作为 Agent 控制问题单独修复，而不是继续调整底层相似度。

## 明确不需要自己造轮子

不应自研 OCR/版面/表格解析、BM25/倒排、HNSW/IVF/PQ、RRF、segment merge、向量量化、通用摄取缓存或检索框架。KR 的价值应集中在 revision-aware 的知识生命周期、精确 Locator/Evidence、可替换索引后端、证据约束的 Agent loop 和可复现 benchmark。

