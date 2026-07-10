# Task: Fix `podmate mark -1 --read` parsing bug

## Issue
Closes #6

## Bug
`podmate mark -1 --read` fails because Typer parses `-1` as a shorthand option instead of a positional argument. The user must type `podmate mark -- -1 --read` which is unintuitive.

## Root cause
`episode_id: int = typer.Argument(...)` — Typer treats `-<number>` as an option flag.

## Fix
Change `episode_id` type from `int` to `str`, then convert to `int` inside the function.

### Changes needed

#### `podmate/cli.py` — `mark()` function (line ~687)
- Change `episode_id: int = typer.Argument(...)` → `episode_id: str = typer.Argument(...)`
- After receiving input, convert: `episode_id_int = int(episode_id)` (wrapped in try/except ValueError for non-numeric input)
- Use `episode_id_int` for all DB lookups
- Error on non-numeric: `[red]❌ 剧集 ID 必须是数字: {episode_id}[/red]`

#### `podmate/cli.py` — `episode()` function (line ~726)
Same fix — `episode_id: int` → `episode_id: str` with int conversion.

#### `tests/test_cli.py`
- Add test: `mark -1 --read` works
- Add test: `mark "--1" --star` works (quoted)
- Add test: `mark abc --read` shows non-numeric error
- Add test: `episode -1` works
- Add test: `episode abc` shows non-numeric error

#### Not changing
- `mark`命令其他逻辑不变
- `export`命令的 `episode_id: int | None` 不在这里改

## 验收标准

Given user types `podmate mark -1 --read`
When  the command runs
Then  episode with ID 1 is marked as read (or error if not found)

Given user types `podmate mark abc --read`  
When  the command runs
Then  clean error: "剧集 ID 必须是数字: abc"

Given user types `podmate episode -1`
When  the command runs
Then  episode with ID 1 is shown (or error if not found)

## 测试
```bash
pytest tests/ -v -k "mark or episode" --tb=short
ruff check podmate/cli.py
```
