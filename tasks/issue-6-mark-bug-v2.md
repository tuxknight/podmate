# Task: Fix `podmate mark -1 --read` parsing bug (take 2)

## Issue
Closes #6

## Bug
`podmate mark -1 --read` fails because Click (Typer's backend) parses `-1` as an option flag.
Even with `episode_id: str = typer.Argument(...)`, Click intercepts `-1` before it reaches the function.

## Fix approach
Add `episode_id` as BOTH a positional argument and an `--id` / `-i` option:

- Positional argument (type str): works for positive IDs as before
- `--id` / `-i` option (type int): works for negative IDs like `mark --id -1 --read`
- If both provided, `--id` wins
- Non-numeric `--id` shows error (type=int handles this)
- Error if neither provided

### Changes needed

#### `podmate/cli.py` — `mark()` function (line ~686)
```python
def mark(
    episode_id: str = typer.Argument("", help="剧集 ID (正数). 负数请用 --id"),
    id: int = typer.Option(None, "--id", "-i", help="剧集 ID（支持负数）"),
) -> None:
```
- Resolve: `episode_id_int = id if id is not None else (int(episode_id) if episode_id else None)`
- If `episode_id_int` is None: error "请指定剧集 ID"
- try/except ValueError: error "剧集 ID 必须是数字"

#### `podmate/cli.py` — `episode()` function (line ~727)
Same approach:
```python
def episode(
    episode_id: str = typer.Argument("", help="剧集 ID (正数). 负数请用 --id"),
    id: int = typer.Option(None, "--id", "-i", help="剧集 ID（支持负数）"),
) -> None:
```
Same resolution logic.

#### `tests/test_cli.py`
- test: `mark 1 --read` works (existing, unchanged)
- test: `mark --id -1 --read` works (NEW)
- test: `mark -1 --read` → exit_code 2 with "No such option" (known limitation, document it)
- test: `mark abc --read` shows numeric error
- test: `episode --id -1` works
- Update test expectations: `mark "--1" --star` → also exit_code 2

## 验收标准

Given user types `podmate mark 1 --read`
When  the command runs
Then  episode 1 is marked as read (existing behavior preserved)

Given user types `podmate mark --id -1 --read`
When  the command runs
Then  episode 1 is marked as read (or "未找到" if not exists)

Given user types `podmate mark abc --read`
When  the command runs
Then  error: "剧集 ID 必须是数字: abc"

Given user types `podmate episode --id -1`
When  the command runs
Then  episode 1 is shown (or "未找到" if not exists)

## 测试
```bash
pytest tests/ -v -k "mark or episode" --tb=short
ruff check podmate/cli.py
```
