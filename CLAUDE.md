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

## Architecture Constraints (Product Direction)

PodMate's long-term goal is Web + mobile, with user and admin sides.

### Current design implications
- **Module separation**: Core business logic (transcribe, translate, dub, storage) must stay separate from the presentation layer. CLI is just one client.
- **Clear API boundaries**: Every key operation (process episode, query episodes, manage feeds, export) needs a clean call interface for future REST/GraphQL exposure.
- **User model ready**: Avoid hardcoding single-user assumptions in data structures (config paths, data dirs, play history) — they should be scope-able in theory.
- **Admin vs user separation**: Admin features (global podcast library, user management, system monitoring) and user features (listen, manage subscriptions, personal settings) should be logically separated.
- **No business logic in CLI**: CLI commands parse arguments and call core APIs. No data aggregation, state machines, or complex logic in cli.py.

## Commands

```bash
# Lint
ruff check podmate/

# Format
ruff format podmate/

# Run CLI
python -m podmate --help
```
