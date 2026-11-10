"""Shared pytest fixtures.

Every test runs against its own throwaway SQLite file (`isolated_db`, autouse),
so incidental writes such as analytics counters never reach the user's real
`responses.db`. Tests that work with the database explicitly use `tmp_db`,
which gives them a fresh, fully initialized file and returns its path.
"""

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db
from db import bot_data, connection


@pytest.fixture(autouse=True)
def llama_cpp_env(monkeypatch, tmp_path):
    """llama.cpp model config is required in production; set it for tests."""
    monkeypatch.setenv("LLAMA_CPP_ALLOWED_MODELS", "discord-bot,other-model")
    monkeypatch.setenv("LLAMA_CPP_DEFAULT_MODEL", "discord-bot")
    monkeypatch.setenv("BOT_ID", "999888777")
    monkeypatch.setenv("LLM_CAPACITY_DIR", str(tmp_path / "llm-capacity"))


@pytest.fixture(scope="session")
def _schema_template(tmp_path_factory):
    """One initialized database, copied for each test instead of re-migrating."""
    path = tmp_path_factory.mktemp("schema") / "template.db"
    original = connection.DB_FILE
    connection.DB_FILE = str(path)
    try:
        db.init_db()
    finally:
        connection.DB_FILE = original
    return path


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch, _schema_template):
    """Keep every test away from the real `responses.db`."""
    db_path = tmp_path / "isolated.db"
    shutil.copyfile(_schema_template, db_path)
    monkeypatch.setattr(connection, "DB_FILE", str(db_path))
    return db_path


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Isolated SQLite database for one test, with the full schema applied.

    Also invalidates the module-level response cache before and after so
    tests don't leak cached state into each other (the cache is a process-
    wide global by design, not per-instance)."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(connection, "DB_FILE", str(db_path))
    bot_data._invalidate_responses_cache()
    db.init_db()
    yield db_path
    bot_data._invalidate_responses_cache()
