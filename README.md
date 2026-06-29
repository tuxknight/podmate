# PodMate 🎙️

> 中文播客伴侣 — 订阅英文科技播客，自动转写、翻译摘要、中文配音。

**一条命令，搞定一期播客：**

```bash
# 发现 Lex Fridman 播客
podmate discover "lex fridman"

# 订阅
podmate sub 1

# 查看剧集
podmate list-episodes

# 看详情
podmate show 9   # Jensen Huang 那期

# 下载、转写、翻译、配音一条龙
podmate download 9

# 听原声
podmate play 9

# 听中文配音
podmate play 9 --dub

# 看状态
podmate status
```

## 安装

```bash
git clone <repo-url> ~/hermes-workspace/podmate
cd ~/hermes-workspace/podmate
pip3 install --user --break-system-packages -r requirements.txt
```

需要设置环境变量：`export DEEPSEEK_API_KEY=sk-...`

## 命令

| 命令 | 说明 |
|------|------|
| `discover <关键词>` | 在 iTunes 搜索播客 |
| `sub <编号\|RSS地址>` | 订阅播客 |
| `unsubscribe <ID>` | 取消订阅 |
| `list-episodes` | 显示剧集列表 |
| `list-episodes -s` | 显示已订阅的播客 |
| `show <ID>` | 查看剧集详情 |
| `download <ID>` | 下载 → 转写 → 翻译 → 配音 |
| `download <ID> --skip-dub` | 下载但跳过配音 |
| `play <ID>` | 播放原声 |
| `play <ID> --dub` | 播放中文配音 |
| `clean --keep 5 --force` | 清理旧剧集 |
| `status` | 显示统计信息 |

## 处理流水线

```
订阅 RSS → 选择剧集 → podmate download ↓

 ① httpx 下载 MP3
 ② faster-whisper 英文转写 (base model, CPU)
 ③ DeepSeek Chat → 中文翻译 + 摘要
 ④ edge-tts (Yunyang 云扬) → 中文配音 MP3
```

## 技术栈

- CLI: Python + Typer + Rich
- 转写: faster-whisper (base, CPU)
- 翻译: DeepSeek Chat API
- 配音: edge-tts (免费, 无需 GPU/API key)
- 播放: 自动检测 mpv → ffplay → aplay
- 存储: SQLite + 本地文件系统

## 存储

每期播客大约占 120MB（原始音频~60MB + 配音~60MB + 字幕/翻译 JSON~2MB）。
`podmate clean --keep 5` 自动保留最近 5 集。

## 路线图

- [x] CLI 原型（发现、订阅、下载、转写、翻译、配音、播放）
- [ ] Web 页面（Next.js + 播放器+中英对照字幕）
- [ ] 微信小程序（Taro）
- [ ] 音色克隆（Fish.Audio）
- [ ] 热门趋势发现
- [ ] 多用户/付费功能

## Q&A

**Q: 配音质量怎么样？**
A: 目前使用 Microsoft Edge TTS 的「云扬」男声（中文 Mainland），免费且接近真人。后续可以升级到 11labs 或 Fish.Audio 音色克隆。

**Q: 一期播客要处理多久？**
A: 下载 ~1-5min，转写 ~15-20min（CPU base model），翻译 ~1-3min，配音 ~1-3min。都在后台跑。

**Q: 费钱吗？**
A: 一期播客 DeepSeek API 费用约 ¥0.01-0.05，其他环节免费。极其便宜。
