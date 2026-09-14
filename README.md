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
$env:LLM_API_KEY = "<rotated LLM key>"
$env:LLM_BASE_URL = "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
$env:LLM_MODEL = "deepseek-v4.1-flash"
```

可选模型：`deepseek-v4.1-flash`、`qwen3.8-max`。

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

当前 lexical search 使用不区分大小写的字面子串匹配；`find` 按名称子串或 glob 模式匹配，`scope` 是资源 ID 的路径前缀。Search 顺序按资源名称与命中行稳定排序，分页游标绑定查询和结果 snapshot。

## 检查

```powershell
python -m pytest -q --basetemp=.test-tmp
```

测试使用假 HTTP transport 和 scripted model，不会访问 MinerU、LLM API，也不需要密钥。

## POC 边界

- 文件 Provider 和 Asset Provider 当前以进程内 lexical 索引为主，适用于验证闭环，不适合大规模语料。
- Local PDF 解析只抽取已有文本层，不含 OCR；MinerU Cloud 提供复杂版式/OCR 解析路径。
- 本地 LLM 端点尚未验证；当前 OpenAI-compatible client 用于提供方给出的 POC 模型，Agent Loop 与解析 Provider 可独立配置。
- 暂不包含租户鉴权、权限同步、跨 Provider 联邦合并、向量索引、生产级恢复和审计。
