"""Shared pytest fixtures.

Every test that needs a database uses the `tmp_db` fixture, which points
`db.DB_FILE` at a per-test SQLite file in pytest's `tmp_path` and runs
`init_db()` so the full schema is available. This means tests can mutate
state freely without affecting each other or the user's real `responses.db`.
"""

import sys
from pathlib import Path

import pytest

# Make `import db` etc. work without installing the project as a package.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402  (sys.path tweak needs to happen first)


@pytest.fixture(autouse=True)
def ollama_env(monkeypatch):
    """Ollama model config is required from .env in production; set for tests."""
    monkeypatch.setenv("OLLAMA_ALLOWED_MODELS", "llama3.2:3b,other-model")
    monkeypatch.setenv("OLLAMA_DEFAULT_MODEL", "llama3.2:3b")


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Isolated SQLite database for one test, with the full schema applied.

    Also invalidates the module-level response cache before and after so
    tests don't leak cached state into each other (the cache is a process-
    wide global by design, not per-instance)."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_FILE", str(db_path))
    db._invalidate_responses_cache()
    db.init_db()
    yield db_path
    db._invalidate_responses_cache()
