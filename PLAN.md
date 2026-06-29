# PodMate — 完整产品方案 PLAN.md

## 一句话定位

**英文科技播客 → 多说话人识别的中文配音平台。**

用户发现播客 → 订阅 → 自动抓取最新剧集 → 转写 + 说话人分离 → 中文翻译 + 摘要 → 高质量中文配音（多人区分，语气贴近原声）

---

## 第一期 MVP 范围

本次只做 CLI 原型 + 单集处理 pipeline 的完整 happy path。

### MVP 要做的事

1. **播客发现与订阅** — 搜索 iTunes API，结果展示，订阅 RSS
2. **单集全流程** — 选中一集 → 下载 → **Deepgram 转写（说话人分离）** → **DeepSeek 翻译** → **高质量中文配音**
3. **配音质量** — 多人区分（Speaker A / B / C 各用不同中文声线）、语气贴近原声

### MVP 不做的事

- ❌ Web 前端 / 小程序
- ❌ 用户系统
- ❌ 自动定时抓取新集
- ❌ 本地 Whisper（已确认 RPi 扛不住，走 Deepgram API）

---

## 架构总览

```
CLI (typer + rich)
  │
  ├─ config.py         ─── 系统配置层（TOML，统一管理 API key）
  ├─ feed.py           ─── RSS 发现 + 解析 + 订阅
  ├─ downloader.py     ─── MP3 下载（httpx 流式）
  ├─ transcriber.py    ─── 转写（Deepgram API，说话人分离）
  ├─ translator.py     ─── 翻译 + 摘要（DeepSeek API）
  ├─ dubbing.py        ─── 中文配音（edge-tts，多人声线）
  ├─ player.py         ─── 播放（mpv / ffplay）
  ├─ pipeline.py       ─── 流水线编排器
  └─ db.py / models.py ─── 数据层
```

---

## 配置系统（已完成）

`~/.config/podmate/config.toml`，统一管理所有 API key。

```
podmate config init     → 创建配置
podmate config show     → 查看（key 脱敏）
podmate config set k=v  → 设置 key
```

---

## 配音质量方案 — 核心设计

这是重点。目前 edge-tts 只有单一中文声线，没有多人区分。需要改进：

### 多人声线映射表

```
Speaker A → zh-CN-YunxiNeural  （云希，年轻男声，适合主持/采访者）
Speaker B → zh-CN-YunyangNeural （云扬，沉稳男声，适合被访者/专家）
Speaker C → zh-CN-XiaoxiaoNeural（晓晓，温柔女声，适合女性说话人）
Speaker D → zh-CN-YunjianNeural （云健，活力男声，适合活跃讨论）
```

Deepgram 返回的 speaker label（A/B/C/D）映射到不同 edge-tts 声线。每个说话人的配音用对应声线分段生成。

### 语气调节

通过 SSML 控制：
- `<prosody rate="slow">` — 语速慢，适合沉稳语气
- `<prosody rate="fast">` — 语速快，适合兴奋/急迫
- `<prosody pitch="+10%">` — 音调高，更有活力

从 DeepSeek 翻译结果中提取 `tone` 字段，动态调整 prosody。

### 配音流程

```
翻译稿（带 speaker 标记）
  │
  ├─ 按 speaker 分组
  │   ├─ Speaker A 的段落 → Yunxi 声线
  │   ├─ Speaker B 的段落 → Yunyang 声线
  │   └─ Speaker C 的段落 → Xiaoxiao 声线
  │
  ├─ 每个段落加上 SSML 语气调节
  │
  ├─ 分段生成音频（edge-tts）
  │
  └─ ffmpeg 拼接 → 完整中文配音 .mp3
```

---

## Happy Path 完整流程

```bash
# 1. 初始化配置（一次）
podmate config init
podmate config set deepgram.api_key "xxx"
podmate config set deepseek.api_key "xxx"

# 2. 搜索播客
podmate discover "lex fridman"

# 3. 订阅（用 # 编号引用搜索结果）
podmate sub 1

# 4. 查看剧集列表
podmate list --feed 1

# 5. 选中一集，跑完整流水线（下载→转写→翻译→配音）
podmate download 15

# 6. 听中文配音
podmate play 15 --dub
```

---

## Task 分割

### Task 1: 配音模块重构（多人声线 + SSML 调节）
- dubbing.py 增加 `_get_voice_for_speaker(speaker: str) -> str`
- 增加 SSML 包裹函数 `_wrap_with_tone(text: str, tone: str) -> str`
- `dub_translation()` 按 speaker 分组，各用不同声线
- ffmpeg 拼接

### Task 2: 翻译模块输出 speaker tone 信息
- DeepSeek prompt 增加说话人语气分析
- 翻译结果增加每个 speaker 段的 `tone` 字段

### Task 3: CLI 命令补全 + 美化
- 中文 UI，所有提示 Panel 化
- 进度显示

### Task 4: Happy Path 端到端验证
- 用 Lex Fridman 一集跑通全流程
- 输出配音效果评估
