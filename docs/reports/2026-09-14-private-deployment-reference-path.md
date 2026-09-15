# Knowledge Runtime：私有化与现有数据平台接入参考路径

## 决策结论

KR 不应把 Databricks、PolarDB、Supabase 中任何一个产品的对象模型变成自己的核心模型。更稳定的做法是冻结四个小接口：

```text
CanonicalStore  : asset / revision / chunk / evidence / provenance
ArtifactStore   : raw file / parse artifact / page image / table asset
IndexBackend    : lexical / sparse / dense / metadata filter / rank
ModelGateway    : parse / embed / rewrite / rerank / answer
```

KR 的标准实现路径是：

```text
Provider(MinerU/Docling/local)
  -> S3-compatible ArtifactStore
  -> PostgreSQL-compatible CanonicalStore
  -> pgvector + full-text baseline
  -> optional Qdrant/OpenSearch/Milvus IndexBackend
  -> Locator-aware Evidence read
  -> Agent answer and citation validator
```

这样，数据库产品可以替换，原始资料、revision、Evidence 和 benchmark 契约不变。索引是可重建的派生数据，不能成为事实来源。

## 现有产品的轻量接入方式

### Supabase

Supabase 的核心是 PostgreSQL，向量能力通过 pgvector 提供，官方同时支持语义、关键词和混合搜索。应用直接使用 PostgreSQL DSN、SQL migration 和标准 pgvector 类型即可，不需要编写 Supabase 专用的 KR 存储模型。[Supabase AI and Vectors](https://supabase.com/docs/guides/ai) 和 [Vector columns](https://supabase.com/docs/guides/ai/vector-columns) 给出了这种方式。

Supabase Storage 使用 S3-compatible 后端，并把对象元数据放在 PostgreSQL 中，适合承载原始文件和 MinerU/Docling 产物。[Storage reference](https://supabase.com/docs/reference/self-hosting-storage) 可以作为 `ArtifactStore` 的实现参考。

如果选择自托管，官方推荐 Docker Compose；数据不需要离开自己的服务器，但数据库维护、备份、灾备、升级、高可用和监控都由部署方负责。自托管 Supabase 没有托管平台的 PITR、托管备份和平台管理能力，因此它适合作为完整应用平台，不能自动替代数据库运维。[Self-hosting](https://supabase.com/docs/guides/self-hosting)

### PolarDB

PolarDB for PostgreSQL 的 PGVector 兼容标准 PostgreSQL/pgvector 使用方式，支持 HNSW 和 IVFFlat。KR 可以直接复用 PostgreSQL 适配器，用 SQL、事务和 `COPY` 批量导入，不需要针对基本向量存储重新设计一套 API。[PolarDB PGVector](https://www.alibabacloud.com/help/en/polardb/polardb-for-postgresql/pgvector)

需要更大规模混合检索时，可以把 PolarSearch 作为可选 `IndexBackend`。它兼容 Elasticsearch/OpenSearch 生态，提供全文、向量和混合检索；PolarDB 官方把 PostgreSQL 引擎路径定位为 PostgreSQL-native 工作负载，把 PolarSearch 定位为分布式大规模搜索路径。[PolarDB vector search overview](https://www.alibabacloud.com/help/en/polardb/polardb-for-postgresql/polardb-for-postgresql-vector-search-overview)

PolarDB 部署在 Alibaba Cloud VPC 内时，属于云上私网隔离；它不等于完全断网的本地私有化。需要严格不出云时，应使用自建 PostgreSQL/pgvector，或确认 PolarDB 专有云产品和合同边界。

### Databricks

Databricks 更适合作为数据源、治理和离线处理平面。AI Search 的 Delta Sync Index 能从 Delta Table 自动增量同步到搜索索引，但需要 Unity Catalog 和相应的 serverless/endpoint 能力。[Create AI Search indexes](https://docs.databricks.com/aws/en/vector-search/create-vector-search)

因此建议提供一个 `DeltaSyncSource`：

1. 从 Unity Catalog/Delta 表读取已批准的文档、chunk 或结构化知识。
2. 使用 `content_hash` 和 `revision_id` 做增量同步。
3. 把 canonical metadata、chunk 和 Evidence materialize 到 KR 的 PostgreSQL store。
4. 可选地把 Databricks AI Search 作为检索后端，但 Agent 最终仍通过 KR 的 Locator 和 Evidence API 读取原文。

不建议在每次回答时直接查询 Delta Lake 或把 Databricks SQL Warehouse 当作低延迟 Evidence store。湖仓适合批处理、治理和历史数据；KR 在线请求需要有界延迟、可重放 Locator 和稳定的读接口。Unity Catalog 对外部系统的访问也需要额外配置存储凭证和云端权限，不能把直接读底层对象存储当作绕过治理的方式。[Access Databricks data using external systems](https://docs.databricks.com/aws/en/external-access)

## 私有化部署的三种成熟模式

### 模式 A：PostgreSQL-native，最适合 KR 第一条稳定路径

```text
PostgreSQL + pgvector + PostgreSQL FTS
MinIO/S3-compatible object storage
KR workers + local MinerU/Docling + local model gateway
```

这套方案可以直接落在自建 PostgreSQL、Supabase self-hosted 或 PolarDB PostgreSQL 上。小到中等规模时，metadata、revision、chunk、Evidence、向量和过滤条件都在一个事务系统中，最容易保证一致性，也最容易备份和迁移。

如果 PostgreSQL 内置 FTS 的 BM25 能力不足，可以评估 [ParadeDB pg_search](https://www.paradedb.com/blog/introducing-paradedb)；它把 BM25 全文索引以 PostgreSQL 扩展方式提供，仍然保持 SQL 接入形态。需要注意扩展版本、许可证和运维支持要在企业交付前单独审核。

这应当成为 KR 的默认 reference backend：不是因为它在所有规模都最快，而是因为它最容易验证 revision、Evidence 和事务语义。

### 模式 B：PostgreSQL canonical store + 专用索引，适合规模增长

```text
PostgreSQL/PolarDB/Supabase : 事实来源、Evidence、metadata
MinIO/S3                    : 原始文件和解析产物
Qdrant                      : dense/sparse ANN 和 payload filter
或 OpenSearch               : BM25、向量、RRF、highlight、filter
```

Qdrant 有开源、自托管、Hybrid Cloud 和 Private Cloud 部署模式；私有云可以放在自己的 Kubernetes、云上或边缘环境。生产环境需要自己配置 TLS、API key、审计、备份和持久化存储，默认自托管实例并不是安全配置。[Qdrant deployment overview](https://qdrant.tech/documentation/guides/)、[Private Cloud setup](https://qdrant.tech/documentation/private-cloud/private-cloud-setup/)、[Security](https://qdrant.tech/documentation/security/)

OpenSearch 适合希望用一个搜索服务完成全文、向量、过滤和混合排序的场景；它可以用 Docker 自建，也可以采用商业支持或云托管版本。[OpenSearch vector search](https://docs.opensearch.org/latest/vector-search/getting-started/index/)

无论选择哪一个，索引都只保存 `chunk_id`、revision、检索字段和向量；回答阶段必须回到 PostgreSQL/ArtifactStore 读取 Evidence。这样索引损坏或更换后端不会丢失知识事实。

### 模式 C：大规模专用向量平台或商业 BYOC

当 chunk 数、查询并发或分片需求明显超过单库能力时，可以采用：

- Milvus 自托管或 Milvus Operator；
- Zilliz Cloud BYOC，把 data plane 部署在自己的 VPC；
- Qdrant Private Cloud；
- Pinecone BYOC，把数据平面放入自己的云账户；
- Elastic/OpenSearch 的自管集群或企业版。

Zilliz BYOC 和 Pinecone BYOC 都属于“自己的 VPC + 厂商托管控制面”模式，适合需要托管升级、扩缩容和支持，但它们不等于完全离线。Zilliz BYOC 的数据平面在客户 VPC 内，[官方部署说明](https://docs.zilliz.com/docs/byoc/quick-start)明确了这一点；Pinecone BYOC 也把数据平面部署在客户云账户，但其官方文档当前仍标注为 public preview，不能作为 KR 默认基线。[Pinecone BYOC](https://docs.pinecone.io/guides/production/bring-your-own-cloud)

Milvus Operator 可以在 Kubernetes 中声明式部署完整 Milvus 及其依赖，适合已经有 Kubernetes 运维能力的团队；单机 POC 不建议直接引入它。[Milvus Operator](https://milvus.io/docs/configure_operator.md)

## 私有化边界必须分清

“私有化”至少有三种强度：

| 边界 | 含义 | 可选方案 |
|---|---|---|
| 私网访问 | 数据不经过公网，但仍在云厂商或 SaaS 账户内 | PolarDB VPC、Databricks 私网、Pinecone PrivateLink、托管向量库私网端点 |
| 客户 VPC 数据平面 | 数据和查询在客户云账户，厂商保留控制面 | Qdrant Private Cloud、Zilliz BYOC、Pinecone BYOC |
| 完全自持/断网 | 数据、索引、模型、镜像、日志和备份都在企业环境 | 自建 PostgreSQL/pgvector、MinIO、Qdrant/OpenSearch、MinerU/Docling、本地模型 |

KR 的部署清单必须逐项记录：原始文件、解析产物、向量、查询文本、Evidence、日志、备份和模型调用分别在哪里；只说“数据库在私网”不足以证明数据私有化。

## 推荐冻结的实现路线

### POC 到中等规模

采用 PostgreSQL-native：

```text
PostgreSQL + pgvector + FTS
MinIO
KR worker
MinerU 或 Docling
本地 embedding / reranker / LLM gateway
```

KR 只需要实现一个 PostgreSQL adapter 和一个 S3 adapter。Supabase、PolarDB、自建 PostgreSQL 通过 DSN 切换；原始文件通过 S3 API 切换到 Supabase Storage、OSS、MinIO 或 S3。

### 规模增长

先不改变 KR 公共 API，只增加 `IndexBackend`：

1. PostgreSQL `tsvector` + pgvector 作为准确基线。
2. Qdrant 或 OpenSearch 作为加速索引。
3. 用 `revision_id`、`content_hash` 和 `index_version` 做异步增量 upsert。
4. 用同一套 Evidence benchmark 比较后端，而不是凭主观体验拆换架构。

### Databricks 客户

只增加 `DeltaSyncSource` 和可选 `DatabricksAISearchBackend`。Databricks 负责湖仓、治理和批处理；KR 负责在线证据契约。这样既能接入已有 Databricks 投资，也不会让 KR 被绑定到 Databricks 的 endpoint、serverless 和 Unity Catalog 生命周期。

## 验证“成熟路径”的验收门槛

任何新后端必须同时通过：

- revision 切换后旧 Evidence 不可见、新 Evidence 可重放；
- 相同 content hash 重复导入不产生重复 chunk 和重复向量；
- 索引删除、重建、迁移后 source Hit、Evidence Hit 和 Locator 不下降；
- 有过滤条件时不会返回越权或错误 revision 的 chunk；
- 增量导入不触发全库重建；
- search p95、ingestion throughput、Agent e2e p95 都有固定 benchmark；
- 能从 canonical store 完整重建索引；
- 私有部署时模型、解析器、数据库、对象存储、日志和备份的网络边界都有明确记录。

最终建议是把 KR 做成“标准数据平面之上的证据控制层”：默认依赖 PostgreSQL/S3 标准接口，按规模接入 Qdrant/OpenSearch/Milvus，按客户环境接入 Supabase、PolarDB 或 Databricks。这样可以借用已经验证过的数据库和索引能力，同时保留 KR 真正需要提供的 revision、Locator、Evidence 和 Agent 约束。

