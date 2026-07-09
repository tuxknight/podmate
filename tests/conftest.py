"""Shared test fixtures — temporary SQLite database, mocked config."""

import pytest


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    """Redirect database and data dir to tmp_path for test isolation."""
    import podmate.config as config_mod
    import podmate.db as db_mod

    test_config = {
        "deepgram": {"api_key": "", "api_url": "", "model": "", "diarize": False},
        "deepseek": {"api_key": "", "api_url": "", "model": "", "temperature": 0.3},
        "dubbing": {"voice": "", "rate": "", "volume": ""},
        "storage": {"data_dir": str(tmp_path), "keep_episodes": 5},
    }

    monkeypatch.setattr(config_mod, "_config", test_config)
    monkeypatch.setattr("podmate.cli.load_config", lambda: test_config)

    db_path = str(tmp_path / "feeds.db")
    monkeypatch.setattr(db_mod, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    monkeypatch.setattr(db_mod, "_conn", None)

    db_mod.init_db()
    yield
    if db_mod._conn:
        db_mod._conn.close()
    db_mod._conn = None
