# Task 1：revision 级段落/标题证据片段索引

## 改动

- 在 `knowledge_runtime.models` 新增不可变 `KnowledgeChunk`，保存 chunk、asset、revision、来源、标题路径、起止行和原文。
- 在 SQLite schema 新增 `asset_chunks`（保留所有 revision）以及当前 revision 的 `chunk_fts` 投影；打开旧数据库时自动补建当前 revision 的片段索引，保留原有 `user_version=2` 兼容行为。
- 入库时按 Markdown 标题、空行段落和 `<!-- page:N -->` 页标记切分，使用由 asset/revision/行范围/文本确定的 chunk id；过长段落按 4,000 字符上限切分。
- 新增 `list_current_chunks` 与 `search_chunks`，支持 asset id、source name、source path scope，并在无 FTS5 时使用 SQLite token fallback。
- 新增模型、片段范围、scope 搜索和 revision 投影回归测试。

## TDD 与验证命令

1. RED：`python -m pytest tests/test_assets.py -k chunk -q` —— 3 项失败，原因是 `list_current_chunks`/`search_chunks` 尚不存在；`python -m pytest tests/test_models.py -k chunk -q` 在收集阶段因 `KnowledgeChunk` 尚不存在而失败。
2. GREEN：`python -m pytest tests/test_assets.py -k chunk -q` —— 3 passed。
3. GREEN：`python -m pytest tests/test_models.py -k chunk -q` —— 1 passed。
4. 相关全量：`python -m pytest tests/test_assets.py tests/test_models.py -q` —— 31 passed。
5. 全套：`python -m pytest -q` —— 89 passed。
6. 语法检查：`python -m compileall -q knowledge_runtime` —— 通过。
7. 差异检查：`git -c safe.directory='C:/Users/meta/Code/研究中心/Knowledge Runtime' diff --check` —— 无差异错误。

## 未解决问题

- 测试运行环境对 `.pytest_cache` 写入有权限警告，但不影响测试结果；生产代码和测试均通过。
- 混合语义编码与 provider chunk locator 属于后续 Task 2，本任务仅提供 SQLite FTS 候选片段索引。
