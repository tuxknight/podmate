# Task: Podmate poll optimization + configurable interval

## Issue
Closes #4

## Current state
`podmate poll`:
- Full output every time (episode details for every podcast)
- No summary line at the end
- Zero changes still shows the full "checking" output
- Poll interval (6h) is hardcoded in Hermes cronjob, not in podmate itself

## What to change

### 1. `podmate poll` — Summary line at the end
After processing all feeds, print ONE summary line:
```python
console.print(f"[dim]📊 检查 {feeds_checked} 个播客，发现 {total_new} 集新内容，已入库 {added_count} 集[/dim]")
```
If `total_new == 0` and `feeds_checked > 0`, print just:
```python
console.print("[dim][podmate] 暂无新剧集[/dim]")
```
This is the "silent when zero changes" behavior.

### 2. `podmate poll` — No per-episode detail output
Remove the loop that prints each individual new episode title. Keep only the feed-level summary (`🎙️ Feed → 发现 N 集新内容`).

Actually, re-reading the requirements: "每次 poll 后输出变化摘要（新剧集数、下载进度）" — keep feed-level line, remove per-episode title list. The summary line at the end replaces the verbose output.

### 3. Config key for poll interval
Add to config defaults:
```toml
[poll]
interval_hours = 6
```

In `podmate/config.py`, add this to default config.
In `cli.py`, `poll()` command reads `config["poll"]["interval_hours"]` (int, default 6) — this is informational only. The actual cron scheduling is in Hermes.

### 4. `podmate poll --summary-only` (optional)
Add a flag that skips feed-level detail and only shows summary line. For cron use.

Actually, keep it simpler: the new summary line behavior IS the default. No separate flag needed.

### 5. Message for Hermes cron (optional)
Output a brief, cron-friendly message:
- With new episodes: `[podmate] 发现 N 集新内容，来自 M 个播客`
- Without: `[podmate] 暂无新剧集`

## Files to change
- `podmate/cli.py` — poll() function (modify output format)
- `podmate/config.py` — add poll section to defaults
- `tests/test_cli.py` — update existing poll tests + add new ones

## Existing tests that must NOT break
Search for tests with "poll" in the name:
```bash
pytest -v -k "poll" --tb=short -q
```
These tests check exit code and key output text. Update assertions if the output format changes.

## New tests
1. test_poll_shows_summary_when_no_new_episodes — zero changes = "[podmate] 暂无新剧集"
2. test_poll_shows_summary_with_new_episodes — has new = "📊 检查" summary line
3. test_poll_config_interval_default — config["poll"]["interval_hours"] == 6
4. test_poll_config_interval_custom — after setting, reads correctly

## 验收标准

Given all feeds are up to date
When  `podmate poll` runs
Then  output ends with "[podmate] 暂无新剧集"

Given new episodes are found in a feed
When  `podmate poll` runs
Then  shows feed-level summary AND final "📊 检查 N 个播客，发现 M 集新内容" line

Given `podmate config show`
When  poll section is displayed
Then  interval_hours defaults to 6

## 测试
```bash
pytest -v -k "poll" --tb=short -q
pytest --tb=short -q
ruff check .
```
