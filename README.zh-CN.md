# Knowledge Runtime POC

一个 evidence-first、渐进式读取的 Knowledge Runtime 原型。模型通过 `list/find/search/read/stat` 工具自行导航；Search 只返回 Locator，Read 才生成带来源 revision 与 SHA-256 的 Evidence。MinerU Cloud 和 Local Extraction Backend 产出同一类 Knowledge Asset，后续问答不依赖解析后端。

## 运行环境

需要 Python 3.11+。POC 主体使用 Python 标准库；pytest 用于测试；可选 `pypdf` 支持本地读取 PDF 文本。

```powershell
python -m pip install -e ".[dev,pdf]"
```

运行时从进程环境读取密钥；`.env.example` 只列出变量名，不会被程序自动加载。你之前粘贴到会话里的 MinerU 和 LLM 密钥都应先撤销并重新生成。

```powershell
$env:MINERU_API_KEY = "<rotated MinerU token>"
$env:QWEN_LLM_API_KEY = "<rotated Qwen key>"
$env:LLM_BASE_URL = "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
$env:LLM_MODEL = "qwen3.8-max"
```

当前 Agent `ask` 和 `benchmark` 命令固定使用 `qwen3.8-max`。OpenAI-compatible client 保留 DeepSeek 适配能力供非 Agent 用途；Agent 评测不接受切换到其他模型。

## 文档解析

Local Backend 默认在本机解析 Markdown、TXT、HTML、DOCX、PPTX；安装 `pypdf` 后可解析文本型 PDF。图像、扫描 PDF 及版式复杂的文档建议使用 MinerU。

```powershell
python -m knowledge_runtime.cli ingest .\docs\runbook.md --backend local
python -m knowledge_runtime.cli ingest .\docs\contract.pdf --backend mineru
python -m knowledge_runtime.cli ingest .\docs\scanned.pdf --backend mineru --ocr
```

MinerU Cloud 会把原始文件上传给 MinerU。若本地解析完成后需要生成摘要、别名和章节导航，可以显式添加 `--enrich`；这会把文档文本片段发送到配置的 OpenAI-compatible 模型端点。私有文档不要使用云端 MinerU 或云端 LLM；未来接入本地模型时，将 `LLM_BASE_URL` 指向本地兼容端点，并移除云端调用。

原始文件仍是 canonical source。`.kr-data/assets/` 中存放可重建的解析投影、内容列表及解析器 provenance。POC 不会把生成摘要用作回答证据。

## Evidence-first 问答

```powershell
python -m knowledge_runtime.cli ask "恢复码多久过期？" --model qwen3.8-max
```

Agent Loop 最多执行 8 次模型请求，单轮累计读取默认限制为 20 KB。回答必须引用 Read 产生的 Evidence ID；没有读取到 Evidence 时会拒答。可以通过 `--max-iterations` 与 `--max-read-bytes` 调整 POC 限制。

Memory Provider 的 lexical search 使用不区分大小写的字面子串匹配。SQLite 资产库将当前 revision 切成段落级 chunk，使用 FTS5 词项排序和本地稀疏字符 n-gram 相似度，经 RRF 合并后返回 chunk Locator；Read 再按 revision 校验并返回原文 Evidence。这里的“语义”是确定性的本地词形相似度，不是 LLM embedding。`case_sensitive=true` 保持完整查询的大小写敏感连续字面匹配语义。`find` 按名称子串或 glob 模式匹配，`scope` 是资源 ID 的路径前缀。Search 顺序和分页游标都绑定当前查询 snapshot。

## 数据库存储与 benchmark

将 `--store` 指向 `.db`、`.sqlite` 或 `.sqlite3` 文件时，Runtime 使用 SQLite 资产库：

```powershell
python .\benchmarks\actuarial\fetch_sources.py --output .kr-data/actuarial-downloads
python -m knowledge_runtime.cli ingest .kr-data/actuarial-downloads --backend local --store .kr-data/knowledge.db
python -m knowledge_runtime.cli catalog --store .kr-data/knowledge.db
python -m knowledge_runtime.cli benchmark .\benchmarks\actuarial\questions.jsonl --store .kr-data/knowledge.db --model qwen3.8-max --output .kr-data/actuarial-report.json
```

SQLite 会事务化保存当前资产、每次 source revision、解析器 provenance、Markdown、content list、metadata、段落 chunk 和压缩的稀疏向量；同一资产的历史 source revision 不会被覆盖。`AssetKnowledgeProvider` 在 SQLite 中查询当前版本，并在读取前检查资产 revision，因此重新入库后旧 Locator 会失效，而不是继续读取旧内容。当前稀疏向量通过有界内存缓存做精确扫描，规模增长时搜索耗时近似线性；这版适合验证 POC，不代表大规模 ANN 检索方案。

从旧 schema 升级后，SQLite 删除旧索引表不会自动缩小现有数据库文件。若要回收迁移产生的空闲页，请在没有并发写入时安排离线 `VACUUM`；它会重写整个数据库文件，细节见[迭代结果](docs/reports/2026-09-14-hybrid-retrieval-results.md)。

精算公开文档目录位于 `benchmarks/actuarial/public_sources.jsonl`，涵盖 ASB、CAS、SOA、NAIC、EIOPA 和 GAD。目录保存官方来源地址和评测元数据；下载清单记录来源、抓取时间和 SHA-256。文档保存在 `.kr-data/`，不会进入 Git。下载前应核对来源机构的许可和使用条款。

Benchmark JSONL 按预期来源、回答关键短语和 Evidence 必含原文短语评分。报告分别给出来源排名与证据片段排名、Evidence 定位率、Hit@1/3、MRR、端到端 p50/p95、search/read 用时和 Evidence 字节数。要跑真实 Qwen 评测，需先在本机设置 `QWEN_LLM_API_KEY`（兼容旧变量 `LLM_API_KEY`）；测试套件使用 ScriptedModel，不会发起模型 API 调用。本轮离线迭代结果和限制见[混合检索评测报告](docs/reports/2026-09-14-hybrid-retrieval-results.md)，规模化选型见[规模化方案与可复用组件](docs/reports/2026-09-14-industry-scaling-options.md)，私有化和现有数据平台接入路径见[私有化与现有数据平台接入参考路径](docs/reports/2026-09-14-private-deployment-reference-path.md)。

模糊、简短和不完整的用户表达使用独立数据集验证：

```powershell
python -m knowledge_runtime.cli benchmark-retrieval .\benchmarks\actuarial\questions-fuzzy.jsonl --store .kr-data\actuarial.db --output .kr-data\actuarial-fuzzy-retrieval-report.json
python -m knowledge_runtime.cli benchmark .\benchmarks\actuarial\questions-fuzzy.jsonl --store .kr-data\actuarial.db --model qwen3.8-max --output .kr-data\actuarial-fuzzy-agent-report.json
```

其中 `benchmark-retrieval` 测试“原始弱查询直接交给 KR”时能否命中，真实 Agent benchmark 另外记录 Agent 发出的每个 search query、首次命中排名和查询锚点覆盖率，从而区分“KR 检索能力”和“Agent 查询改写能力”。`ambiguous` 与 `underspecified` 用例要求没有上下文时不生成有依据的答案。

语料规模实验使用 SQLite chunk FTS + 本地稀疏向量扫描 → RRF → Locator → Read → Evidence 路径，不调用 LLM：

```powershell
python -m benchmarks.scale.run_scale_benchmark --sizes 20,100,1000,5000 --case-count 20 --top-k 20 --output .kr-data/scale-benchmark-report.json
```

它同时测量文档数量、Markdown/SQLite 字节数、索引构建时间、Search/Read p50/p95、Hit@1/3/5/10/20、MRR 和 Evidence 原文定位率，并将精确查询与宽泛查询分开。宽泛控制查询故意使用语料中共享的词，不能辨别某一份指定文档；其指定来源命中率仅作不可区分性压力信号，不是应答准确率，也不能单独解释为解析失败。

## 检查

```powershell
python -m pytest -q --basetemp=.test-tmp
```

测试使用假 HTTP transport 和 scripted model，不会访问 MinerU、LLM API，也不需要密钥。

## POC 边界

- 文件 Provider 和 Asset Provider 当前以进程内 lexical 索引为主，适用于验证闭环，不适合大规模语料。
- Local PDF 解析只抽取已有文本层，不含 OCR；MinerU Cloud 提供复杂版式/OCR 解析路径。
- 本地 LLM 端点尚未验证；当前 OpenAI-compatible client 用于提供方给出的 POC 模型，Agent Loop 与解析 Provider 可独立配置。
- 暂不包含租户鉴权、权限同步、跨 Provider 联邦合并、专用向量数据库或 ANN 索引、生产级恢复和审计。
