# Task: podmate sync-cbrain — batch export transcripts to cbrain

## Issue
Closes #3

## Requirements
1. `podmate sync-cbrain` — find episodes with transcript_path set but not yet exported, batch export to cbrain dir
2. `podmate sync-cbrain --dry-run` — preview mode (show what would be exported, don't actually copy)
3. `podmate sync-cbrain --since 2026-07-01` — only export episodes created after a date
4. After export, mark episode as exported in DB (so subsequent runs skip it)
5. Copy both .md (always) and .json (if exists) to cbrain dir
6. After all episodes are exported, run `_update_podcasts_index()` to regenerate index.md

## Implementation plan

### 1. `podmate/models.py` — Episode model
Add field: `exported_to_cbrain: bool = False`

### 2. `podmate/db.py` — DB layer
- `init_db()`: add `exported_to_cbrain INTEGER DEFAULT 0` column (via `_add_column_if_missing`)
- `_row_to_episode()`: read `exported_to_cbrain` field
- Add function: `get_unexported_episodes(since: str | None = None) -> list[Episode]`
  - Query: `SELECT e.*, f.title AS feed_title FROM episodes e LEFT JOIN feeds f ON e.feed_id = f.id WHERE e.transcript_path IS NOT NULL AND (e.exported_to_cbrain IS NULL OR e.exported_to_cbrain = 0) AND e.status IN ('transcribed', 'translated', 'error') ORDER BY e.created_at DESC`
  - If since is set: add `AND e.created_at >= ?`
- Add function: `mark_episode_exported(episode_id: int) -> None`
  - `UPDATE episodes SET exported_to_cbrain = 1 WHERE id = ?`

### 3. `podmate/pipeline.py` — Mark exported after pipeline
- In `run_pipeline()`, after successful cbrain export `_export_to_cbrain()`, call `mark_episode_exported(episode_id)`

### 4. `podmate/cli.py` — New command
```python
@app.command()
def sync_cbrain(
    dry_run: bool = typer.Option(False, "--dry-run", help="预览模式，不实际导出"),
    since: str = typer.Option("", "--since", help="只导出指定日期后的剧集 (YYYY-MM-DD)"),
) -> None:
```
- Get all unexported episodes with transcript
- If dry_run: show what WOULD be exported, don't copy
- If not dry_run: copy .md + .json to cbrain dir + mark exported + rebuild index
- Summary: `📊 已同步 N 集到 cbrain ({cbrain_path})`
- No unexported episodes: `[podmate] 所有转写稿已同步到 cbrain`

### 5. conftest.py
- Add `exported_to_cbrain` field to episode creation in test config (if needed)

## Files to change
- `podmate/models.py` — Episode field
- `podmate/db.py` — new functions + migration
- `podmate/pipeline.py` — mark exported
- `podmate/cli.py` — sync-cbrain command
- `tests/test_cli.py` — existing tests + new tests
- `tests/conftest.py` if needed

## Existing tests that MUST NOT break
ALL of them. Check with:
```bash
pytest --tb=short -q
```

## New tests
1. test_sync_cbrain_no_unexported — "所有转写稿已同步"
2. test_sync_cbrain_dry_run — shows list, doesn't copy
3. test_sync_cbrain_actual_sync — copies files, marks exported
4. test_sync_cbrain_with_since — only exports episodes after date
5. test_sync_cbrain_rebuilds_index — after sync, index.md is regenerated

## 验收标准

Given all episodes are exported
When  `podmate sync-cbrain` runs
Then  output: "[podmate] 所有转写稿已同步到 cbrain"

Given some episodes are not exported
When  `podmate sync-cbrain --dry-run` runs
Then  shows list of episodes that would be exported, no files copied

Given some episodes are not exported
When  `podmate sync-cbrain` runs
Then  exports .md + .json, marks exported, rebuilds index

## 测试
```bash
pytest -v -k "sync_cbrain or exported" --tb=short -q
pytest --tb=short -q
ruff check .
```
