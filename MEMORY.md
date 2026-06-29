# PodMate — 项目记忆

> 播客伴侣 — 订阅英文科技播客，自动转写、翻译摘要、中文配音。

---

## 1. 需求

**原始需求：** 一条命令搞定播客「下载→转写→翻译→配音」全流程。

**目标用户：** 想听英文科技播客但听不太懂的中文用户。

**核心价值：**
- 自动转写（Deepgram nova-2，说话人分离）
- 中文翻译 + 摘要（DeepSeek）
- 多人中文配音（edge-tts，不同声线对应不同 speaker）
- 极低成本（一期约 ¥0.01-0.05）

---

## 2. 技术方案

| 项 | 选择 |
|---|---|
| 语言 | Python 3.10+ |
| CLI | Typer + Rich |
| 转写 | Deepgram API (nova-2, 说话人分离) |
| 翻译 | DeepSeek Chat API |
| 配音 | edge-tts (免费, 多人声线) |
| 播放 | mpv → ffplay → aplay (自动检测) |
| 存储 | SQLite + 本地文件系统 |
| 配置 | `~/.config/podmate/config.toml` |
| 数据 | `~/.local/share/podmate/`（待重构） |

**模块架构：**
```
podmate/
├── cli.py         — Typer CLI 入口
├── config.py      — TOML 配置管理
├── feed.py        — RSS 发现 + 解析 + 订阅
├── downloader.py  — MP3 下载
├── transcriber.py — Deepgram 转写
├── translator.py  — DeepSeek 翻译 + 摘要
├── dubbing.py     — edge-tts 配音
├── player.py      — 音频播放
├── pipeline.py    — 流水线编排
├── db.py          — SQLite 数据层
└── models.py      — 数据模型
```

---

## 3. 项目看板

| Backlog | Todo | In Progress | Review | Done |
|---|---|---|---|---|
| GitHub CI (pytest+lint) | data 路径重构 | — | — | CLI 全流程实现 |
| PyPI 发布 | — | — | — | 多人配音 |
| | — | — | — | SSML 语气调节 |
| | — | — | — | 代码清理（无硬编码 key） |

**当前阻塞**: GitHub push

---

## 4. Backlog（优先级排序）

| # | 事项 | 类型 | 优先级 | 预估工时 | 依赖 |
|---|---|---|---|---|---|
| 1 | 创建 GitHub repo + push 代码 | 工程 | 🔴 P0 | — | GitHub 账号 |
| 2 | Data 路径重构（3文件: config.py/db.py/pipeline.py） | 改进 | 🔴 P0 | ClaudeCode 3轮 | — |
| 3 | GitHub Actions CI（pytest + ruff/black lint） | CI | 🟡 P1 | 5轮 | push 解封 |
| 4 | 拍平历史 commit（可选：squash 为单个 commit） | 工程 | 🔵 P2 | 1轮 | — |
| 5 | PyPI 发布准备（setup.py 完善、README 补充） | 运营 | 🔵 P3 | 3轮 | — |

---

## 5. 阻塞项

**与 NeckAngle 相同：GitHub 账号找回 + SSH key + token contents:write 权限**

---

## 6. 常用命令

```bash
# 初始化配置
podmate config init
podmate config set deepgram.api_key "xxx"
podmate config set deepseek.api_key "xxx"

# 搜索播客
podmate discover "lex fridman"

# 全流程
podmate sub 1              # 订阅
podmate list                # 列出剧集
podmate download 9          # 下载→转写→翻译→配音
podmate play 9 --dub        # 听中文版
```

---

*最后更新: 2026-06-28*
