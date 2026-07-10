# Task: Test coverage improvement (57% → 70%+)

## Issue
Closes #5

## Goal
Increase overall test coverage from ~57% to 70%+ by adding smoke tests for untested modules.

## Current gaps

| Module | Lines | Coverage | Missing |
|--------|-------|----------|---------|
| player.py | 58 | 0% | audio playback |
| translator.py | 156 | 11% | DeepSeek API translation |
| dubbing.py | 158 | 15% | edge-tts dubbing |
| downloader.py | 18 | 22% | HTTP download |
| transcriber.py | 162 | 52% | Deepgram & faster-whisper |

## Requirements
Add smoke tests ONLY — these tests verify that:
1. Functions can be called without errors
2. Error paths return appropriate exceptions
3. No imports are broken

**Do NOT** test actual API calls or real audio processing. Use pytest's `monkeypatch` and `unittest.mock`.

## Files to add tests for

### `podmate/player.py` (0% → ~70%)
- Test `play_episode()` with `monkeypatch` to mock subprocess calls
- Test error path when player (vlc/ffplay) not found

### `podmate/translator.py` (11% → ~70%)
- Test `translate_segments()` with mocked httpx.AsyncClient
- Test empty segments returns empty quickly
- Test error path when API call fails

### `podmate/dubbing.py` (15% → ~70%)
- Test `dub_translation()` with mocked edge_tts (or httpx)
- Test empty input handling
- Test error path

### `podmate/downloader.py` (22% → ~70%)
- Test `download_episode()` with mocked httpx response
- Test resume functionality (if exists)
- Test error path (HTTP error, network failure)

### `podmate/transcriber.py` (52% → ~70%)
- Test `transcribe_via_deepgram()` with mocked httpx
- Test offline/transcription skipped when model not available
- Test fallback from faster-whisper to Deepgram

## Mock patterns (CRITICAL — follow these exactly)

### httpx.AsyncClient mock
Use the same pattern already in test_cli.py:
```python
from unittest.mock import AsyncMock, MagicMock, patch

def _mock_httpx_client(json_data):
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=json_data)
    mock_resp.iter_bytes = MagicMock(return_value=iter([b"audio data"]))

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.post = AsyncMock(return_value=mock_resp)

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)
    return mock_ctx
```

### edge_tts mock
```python
mock_subprocess = MagicMock()
mock_subprocess.return_value.returncode = 0
```

### subprocess mock (for player)
```python
mock_run = MagicMock()
mock_run.return_value.returncode = 0
```

## Output format
Place all new tests in `tests/test_cli.py`. Each test module gets a section comment:
```python
# ── downloader ────────────────────────────────────
```
```python
# ── translator ────────────────────────────────────
```

## Do NOT
- Test actual API calls (no network)
- Import heavy dependencies (faster-whisper, deepgram SDK)
- Test `__main__.py` (trivial entry point)
- Delete or modify existing tests

## Coverage target
After adding these tests, verify:
```bash
pytest --cov=podmate --cov-report=term-missing --tb=short -q
```
Target: Total coverage ≥ 70%, each module ≥ 60% (except player which can be lower since it's subprocess-based).

## 验收标准

Given pytest with --cov=podmate
When the test suite runs
Then total coverage ≥ 70%

Given a smoke test for player.play_episode()
When called with mocked subprocess
Then no real audio plays

Given a smoke test for translator.translate_segments()
When called with mocked httpx
Then no real API call is made

## 测试
```bash
pytest -v -k "player or translator or dubbing or downloader or transcriber or coverage" --tb=short -q
pytest --cov=podmate --cov-report=term-missing --tb=short -q
ruff check .
```
