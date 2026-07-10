# Task: Phase 2.4 — podcasts/index.md 自动生成

## Issue
Closes #1

## 需求
每次转写完成后，自动更新 `cbrain/docs/fuyuans-kb/podcasts/index.md`，包含所有已转写播客的目录。
同时新增 `podmate export --rebuild-index` 手动重建索引。

## 现状
- pipeline.py 中 `_export_to_cbrain()` 已实现：转录后将 .md 文件写入 `~/cbrain/docs/fuyuans-kb/podcasts/<guid>.md`
- 但 `podcasts/index.md` 从未更新，访问 `/podcasts/` 只能看到手动添加的 `index.md`
- 当前 index.md 只包含一个手动测试条目

## 需要改的文件
1. `podmate/pipeline.py` — 在 `_export_to_cbrain()` 成功后，调用更新索引
2. `podmate/cli.py` — 新增 `export --rebuild-index` 命令
3. `tests/test_cli.py` — 新增测试

## 具体实现要求

### 1. 更新索引函数 `_update_podcasts_index()`
- 签名：`def _update_podcasts_index(cbrain_dir: str, export_dir: str) -> None`
- 扫描 `{export_dir}/*.md`（排除 index.md 自己）
- 解析每个 .md 文件的 YAML frontmatter，提取：
  - title → `title` 字段
  - 如果没有 frontmatter，用文件名去掉 `.md` 作为标题
- 如果没有转写记录，写入：
  ```markdown
  # 🎙 播客转写稿

  暂无转写记录。
  ```
- 如果有记录，生成表格：
  ```markdown
  # 🎙 播客转写稿

  | # | 标题 |
  |---|------|
  | 1 | [Episode Title](guid.md) |
  ...
  ```
- 检查 index.md 的内容是否相同，相同就不写（避免不必要的 git 变动）
- 放在 `podmate/pipeline.py` 中

### 2. CLI 新增 `export` 命令组
- `podmate export --rebuild-index` — 扫描所有已导出的转写稿，重建 index.md
- `podmate export <episode-id>` — 手动导出指定剧集的转写稿（不重建索引）
  - 如果 `<episode-id>` 没有转写记录，提示错误
  - 导出后提示 "已导出到 {path}"
- 位置：`podmate/cli.py`

### 3. pipeline.py 修改
- 在 `_export_to_cbrain()` 成功导出 .md 文件后，调用 `_update_podcasts_index()`
- 注意：不要因为索引更新失败影响转写主流程（try/except）

### 4. 配置
- cbrain 目录路径使用已有配置：`podmate config get storage.cbrain_dir`（默认 `~/cbrain/docs/fuyuans-kb/podcasts/`）
- 如果配置不存在，用默认路径 `~/cbrain/docs/fuyuans-kb/podcasts/`
- 如果目录不存在，自动创建

### 5. 测试要求
- 测试空目录（"暂无转写记录"）
- 测试有 .md 文件时生成正确表格
- 测试内容无变化时不做写入
- 测试 frontmatter 不完整时优雅降级
- 测试 CLI `export --rebuild-index`
- 测试 CLI `export <episode-id>` 无转写记录时的报错
- 测试 CLI `export <episode-id>` 成功导出
- 所有测试用 tmp_path mock，不碰真实 cbrain 目录

### 6. 不做的
- 不改 `podmate export` 的 `--format` 参数（#2 再实现）
- 不改 db 结构
- 不做 CI 集成

## 运行方式
```bash
# 测试
pytest tests/ -v -k "index or export" --tb=short

# 代码检查
ruff check podmate/cli.py podmate/pipeline.py
```
