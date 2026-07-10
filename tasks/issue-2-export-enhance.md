# Task: podmate export enhancements (--format, --output)

## Issue
Closes #2

## Current state
`podmate export` already exists from PR #7:
- `podmate export <episode-id>` — exports .md to default cbrain dir
- `podmate export --rebuild-index` — rebuilds podcasts/index.md

## New requirements
1. `podmate export <episode-id> --format json` — export JSON transcript instead of .md
2. `podmate export <episode-id> --output ~/some/path/` — export to custom directory
3. Both options combine: `--format json --output ~/my/backup/`

## Implementation

### `podmate/cli.py` — `export()` command (already exists, added in PR #7)
Current signature (read the file first):
```python
def export(
    episode_id: int | None = typer.Argument(None, ...),
    rebuild_index: bool = typer.Option(False, "--rebuild-index", ...),
) -> None:
```

**Changes:**
1. Change `episode_id` type from `int | None` to `str | None` (same pattern as Issue #6 fix — `str` positional + `--id` option for negative IDs)
2. Add `output: str = typer.Option("", "--output", help="目标目录（默认 cbrain 目录）")` — if empty, use default cbrain dir
3. Add `format: str = typer.Option("md", "--format", help="导出格式: md 或 json")` — validate "md" or "json", else error
4. For JSON export: read the .json file (in transcripts dir), copy to destination with same filename
5. For MD export: existing behavior (copy .md file from transcripts dir, or regenerate from .json if .md missing)
6. Support `--id` option like Issue #6 for negative episode IDs

### Validation
- If episode not found: `❌ 未找到剧集 ID: {id}`
- If no transcript: `📝 剧集 #{id} 尚未转写`
- If --format is not "md" or "json": `❌ 不支持的格式: {fmt}，支持: md, json`
- If --output doesn't exist and user specified it: auto-create directory
- If --output is empty: use default cbrain podcasts dir (from config or ~/cbrain/docs/fuyuans-kb/podcasts/)

### Existing tests to preserve
- test_cli_export_rebuild_index
- test_cli_export_episode_no_transcript
- test_cli_export_episode_md_missing
- test_cli_export_episode_success
- test_cli_export_episode_not_found
- test_cli_export_no_args
- test_export_rebuild_index_empty_dir
- test_update_index_* (5 tests)
- test_extract_title_* (5 tests)

### New tests needed
1. `export 1 --format json` — exports .json instead of .md
2. `export 1 --output /tmp/my-podcasts/` — exports to custom path
3. `export 1 --format json --output /tmp/` — combines both
4. `export 1 --format txt` — error: unsupported format
5. `export 1 --id -1 --format md` — negative ID via --id option

## 验收标准

Given user types `podmate export 1 --format json`
When  the command runs
Then  the .json transcript file is copied (not .md)

Given user types `podmate export 1 --output ~/my-backup/`
When  the command runs
Then  .md file is copied to ~/my-backup/

Given user types `podmate export 1 --format txt`
When  the command runs
Then  error: `❌ 不支持的格式: txt，支持: md, json`

## 测试
```bash
pytest tests/ -v -k "export" --tb=short -q
ruff check podmate/cli.py
```
