# PodMate 订阅管理设计稿 (Phase 0-1)

> 目标：修 `sub` bug + 补全基本订阅管理，让你能正常用 PodMate 订阅播客并管理列表。
> 不涉及转写升级、文字稿格式、cbrain 对接。

## 一、问题分析

### Bug: `sub <编号>` 跨进程失败

**根因：** `discover` 把搜索结果显示在终端，结果存在内存变量 `_last_discover_results` 里。每次 `podmate sub 1` 是新进程，`_last_discover_results` 是空的。

**当前绕过：** `podmate sub <RSS_URL>` 可以正常用。

### 解决方案

`sub` 的参数不再依赖跨进程内存，而是支持三种输入：

```
podmate sub https://rss.url/feed.xml    ← RSS URL（已有，保留）
podmate sub "The Pragmatic Engineer"     ← 关键词搜索，取第一个结果订阅
podmate sub "The Pragmatic Engineer" --pick 2  ← 搜索结果中选择第 2 个
```

**逻辑：**
1. 如果参数以 `http://` 或 `https://` 开头 → 当作 RSS URL 处理
2. 否则 → 调用 iTunes 搜索 → 显示结果列表 + 交互式选择 → 订阅选定结果
3. `--pick N` → 跳过交互选择，直接选第 N 个

## 二、命令需求

### 1. `sub` — 订阅播客（修 bug + 支持搜索）

```
podmate sub <url|name> [--pick N]
```

**行为：**
- URL 模式：同现有逻辑，直接解析 RSS 并订阅
- 关键词模式：调用 iTunes 搜索 → 显示结果 + 交互选择(或 `--pick`) → 选中的结果 → 解析 RSS → 存入 DB

**交互式选择：** 使用 `rich.prompt.Prompt.ask` 或 `typer.confirm` 让用户输入编号。

**成功输出：** 同现有 Panel，显示播客名、作者、RSS、剧集数、最近 5 集列表。

### 2. `list` — 显示已订阅列表

```
podmate list          # 显示已订阅的播客
podmate list --feed <id>   # 显示某播客的剧集
```

**播客列表（默认）：**
| ID | 播客名称 | 作者 | 剧集数 | 收集时间 |
|----|----------|------|--------|----------|
| 1  | The Pragmatic Engineer | Gergely Orosz | 68 | 2026-07-09 |

**剧集列表（`--feed`）：**
| ID | 标题 | 日期 | 时长 | 状态 | 本地文件 |
|----|------|------|------|------|----------|
| 42 | Slow down to speed up | 2026-06-23 | 45min | transcribed | ✅ |

### 3. `unsub` — 取消订阅

已存在 `unsubscribe` 命令。确认可用就行。

### 4. `describe` — 播客详情

```
podmate describe <feed-id>
```

显示：播客名、作者、描述、RSS URL、订阅时间、总剧集数 + 每种状态的剧集数。

### 5. `describe episode`

```
podmate describe episode <episode-id>
```

或者：

```
podmate episode <episode-id>
```

显示：标题、所属播客、发布时间、时长、描述、当前状态（none/downloaded/transcribed/...）、进度、本地文件路径。

### 6. `status` — 数据总览

```
podmate status
```

显示：播客数、剧集数、各状态统计（有多少已转写、有多少下载中）。

当前已有 `status` 命令？检查一下。如果没有直接加。

## 三、现有代码分析

- `cli.py`: `sub()` 命令已实现 URL 模式 + 编号模式（编号模式是 bug 来源）。如果输入是关键词，不会自动搜 iTunes。
- `feed.py`: `search_itunes()` 已实现，`parse_feed()` 已实现。
- `db.py`: 所有 CRUD 已实现（add_feed, delete_feed, get_feeds, get_episodes, count_stats）。
- `models.py`: `Feed` 和 `Episode` 数据类已定义。

## 四、不需要改的

- 数据库 schema（足够用）
- `feed.py`（逻辑完整）
- `db.py`（完整）
- `models.py`（完整）

## 五、验证标准

```
Given 一个没运行过 discover 的终端
When  podmate sub "The Pragmatic Engineer"
Then  显示搜索结果，选择后成功订阅

Given 搜索结果列表
When  podmate sub "The Pragmatic Engineer" --pick 1
Then  直接订阅第一个结果，不交互

Given 已订阅多个播客
When  podmate list
Then  显示表格：ID、播客名、作者、剧集数

Given 已订阅的播客有剧集
When  podmate list --feed <id>
Then  显示剧集表格：ID、标题、日期、时长、状态

Given 已订阅的播客
When  podmate describe <feed-id>
Then  显示播客详情 + 剧集状态统计

Given 存在的剧集
When  podmate episode <episode-id>
Then  显示剧集详情
```
