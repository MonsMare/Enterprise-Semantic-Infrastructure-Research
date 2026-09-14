# Knowledge Runtime 段落级证据与混合检索设计

## 目标

将 KR 的检索单位从“整篇文档”提升为“可追溯的结构化证据片段”，用词项检索与可替换的语义编码器共同召回候选片段，再以融合分数返回给 Agent。原始文档、解析版本、片段、定位信息和派生索引都保存在 SQLite 中。

## 约束

- 保留现有 `KnowledgeAsset`、revision、locator 和 `Evidence` 合约。
- 不把模型生成的摘要当作原始证据；派生文本必须标记为派生索引。
- POC 不依赖云端 embedding API；默认提供确定性的本地字符/词项向量编码器，并允许注入真正的本地或云端编码器。
- 现有 provider 合约和旧数据库可以继续读取；打开数据库时按当前资产版本重建片段索引。
- `scope` 必须在 SQLite 检索路径中生效，不能退化为整串字面搜索。

## 数据流

1. 解析器生成 Markdown、`content_list` 和 provenance。
2. 入库时按标题、页标记和段落边界切分 chunk，保存 `asset_id`、`revision_id`、`chunk_id`、起止行、标题路径和原文。
3. FTS5 索引 chunk 的标题、文件名和正文；语义编码器生成固定长度向量并保存在 chunk 索引表中。
4. 查询先规范化并分词，分别计算 FTS rank 与向量相似度，通过 reciprocal rank fusion 得到候选排序；`scope`、limit 和 cursor 在结果层生效。
5. `SearchHit` 的 locator 直接指向 chunk 的行范围，`read` 返回带 revision、selector 和 source label 的 Evidence；相邻 chunk 只在显式扩展时读取。

## 质量门禁和评测

需要分别记录文档命中、chunk Hit@K/MRR、证据短语覆盖率、Evidence 字节量和延迟。原有精确问题、弱查询、模糊 Agent 测试保持不变，并新增段落级回归测试、scope 回归测试和混合排序对照。结果中明确区分“人工 gold query”与“Agent 原始问题”。

## 不在本次迭代

租户鉴权、权限过滤、知识图谱、在线学习、远程 embedding 服务和 ChatGPT 内部实现复刻。
