# Knowledge Runtime POC 设计

## 目标

实现一个 Codex-like Knowledge Runtime POC：文档由可替换的解析后端处理，产出统一的结构化 Knowledge Asset；Agent 通过 `list/find/search/read/stat` 逐步定位并读取文档；只有 `read` 返回带来源、版本和哈希的 Evidence，Evidence 作为最终回答的实际依据。

## 范围

- 支持 `MinerU Cloud` 和 `Local` 两种解析后端。
- 两种后端共享同一份 `KnowledgeAsset`、Locator、Evidence 和查询协议。
- POC 使用本地文件作为原始来源，并将派生 Markdown/JSON/索引保存到本地。
- Agent Loop 只暴露 KR 五个工具，支持最大迭代次数和读取大小限制。
- 通过内存 Provider、文件 Provider 和契约测试验证 Provider 可替换性。

## 明确不做

- 鉴权、租户、合规、数据驻留和生产级审计。
- 分布式索引、HA、灾备和跨 Provider 全局排序。
- 通用语义 reranker、向量库和自动 query planner。
- 真实 API Key 写入代码、配置、日志或测试数据。

## 核心模型

```text
SourceProvider
  -> ExtractionBackend
  -> KnowledgeAssetStore
  -> KnowledgeProvider
  -> AgentRetrievalLoop
  -> Evidence
```

`Locator` 必须包含版本、Provider、稳定资源 ID、revision 和可验证 selector。POC 中所有 SearchHit 必须返回可直接传给 Read 的 Locator。`Evidence` 必须回显实际 locator、source revision、内容哈希、表示形式和截断状态。

## 解析后端

`MinerUCloudBackend` 通过环境变量读取 `MINERU_API_KEY`、`MINERU_BASE_URL` 和 `MINERU_MODEL`，使用异步提交/轮询方式获取解析结果；原始文件和下载后的派生结果都落本地。`LocalBackend` 不调用外网，使用本地解析器，并可选调用 OpenAI-compatible 本地或云端 LLM 做结构增强。两者都必须产出相同的 Knowledge Asset。

## Agent Loop

模型通过 OpenAI-compatible `/chat/completions` 接口获得工具定义。每轮最多调用一个工具，工具结果作为下一轮消息输入。达到 `max_iterations`、模型返回最终答案或发生不可恢复错误时结束。最终答案只能引用本轮产生的 Evidence ID。
