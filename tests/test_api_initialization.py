import sqlite3
from unittest.mock import Mock

import db
from web.app import create_app


def test_api_database_initialization_is_lazy(monkeypatch):
    initialize = Mock()
    monkeypatch.setattr(db, "init_db", initialize)
    app = create_app({"TESTING": True, "API_TOKEN": None})

    initialize.assert_not_called()
    with app.test_client() as client:
        assert client.get("/missing-route").status_code == 404
        assert client.get("/missing-route").status_code == 404

    initialize.assert_called_once_with()


def test_api_retries_database_initialization_after_failure(monkeypatch):
    initialize = Mock(side_effect=[sqlite3.OperationalError("unavailable"), None])
    monkeypatch.setattr(db, "init_db", initialize)
    app = create_app({"TESTING": True, "API_TOKEN": None})

    with app.test_client() as client:
        failed = client.get("/missing-route")
        assert failed.status_code == 503
        assert failed.get_json() == {"error": "Database is temporarily unavailable"}
        assert client.get("/missing-route").status_code == 404
        assert client.get("/missing-route").status_code == 404

    assert initialize.call_count == 2


def test_api_persistent_database_failure_keeps_retrying(monkeypatch):
    initialize = Mock(side_effect=sqlite3.OperationalError("unavailable"))
    monkeypatch.setattr(db, "init_db", initialize)
    app = create_app({"TESTING": True, "API_TOKEN": None})

    with app.test_client() as client:
        assert client.get("/missing-route").status_code == 503
        assert client.get("/missing-route").status_code == 503

    assert initialize.call_count == 2
