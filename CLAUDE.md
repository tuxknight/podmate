# CLAUDE.md - PodMate

Podcast management CLI with transcription (Deepgram), translation (OpenAI), and cbrain export.

## Project Structure

```
podmate/          - Main Python package
  cli.py          - CLI commands (export, episode, pipeline, etc.)
  config.py       - Configuration management
  db.py           - SQLite database layer
  downloader.py   - RSS/episode downloading
  dubbing.py      - Audio dubbing
  feed.py         - RSS feed parsing
  models.py       - Pydantic/sqlmodel data models
  pipeline.py     - End-to-end processing pipeline
  player.py       - Audio playback
  transcriber.py  - Deepgram transcription + transcript formatting
  translator.py   - OpenAI translation
```

## Conventions

- **Issue specs**: Task specifications live in `issue-specs/` at the project root (e.g., `issue-specs/issue-19.md`). This directory is git-ignored.
- **Python**: Ruff for linting/formatting. No mypy.
- **Tests**: pytest with 180+ tests. Run `python -m pytest tests/ -x -q` before PR.

## Commands

```bash
# Lint
ruff check podmate/

# Format
ruff format podmate/

# Run CLI
python -m podmate --help
```
