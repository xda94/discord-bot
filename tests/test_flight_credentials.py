import logging

import pytest

import db
import flight_credentials
from flight_credentials import FlightCredentialsError

SECRET = "serpapi-secret-key-123"


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr(flight_credentials, "_warned_unset", False)
    monkeypatch.setattr(flight_credentials, "_cached", None)
    monkeypatch.delenv("FLIGHT_CREDENTIALS_KEY", raising=False)


@pytest.fixture
def fernet_key(monkeypatch):
    fernet = pytest.importorskip("cryptography.fernet")
    key = fernet.Fernet.generate_key().decode()
    monkeypatch.setenv("FLIGHT_CREDENTIALS_KEY", key)
    return key


def _raw_key(user_id):
    with db._connect() as c:
        c.execute(
            "SELECT api_key FROM flight_api_credentials WHERE user_id = ?", (user_id,)
        )
        row = c.fetchone()
    return row[0] if row else None


def _insert_raw(user_id, value, updated_at=1.0):
    with db._connect(commit=True) as c:
        c.execute(
            "INSERT INTO flight_api_credentials (user_id, api_key, updated_at) "
            "VALUES (?, ?, ?)",
            (user_id, value, updated_at),
        )


def test_encrypt_decrypt_round_trip(fernet_key):
    token = flight_credentials.encrypt(SECRET)
    assert token.startswith("enc:v1:")
    assert SECRET not in token
    assert flight_credentials.decrypt(token) == SECRET
    assert flight_credentials.decrypt("legacy-plain") == "legacy-plain"


def test_db_round_trip_stores_only_ciphertext(tmp_db, fernet_key):
    assert db.set_flight_api_credentials(123, SECRET) is True
    raw = _raw_key(123)
    assert raw.startswith("enc:v1:")
    assert SECRET not in raw
    credentials = db.get_flight_api_credentials(123)
    assert credentials["api_key"] == SECRET
    assert isinstance(credentials["updated_at"], float)
    assert set(credentials) == {"api_key", "updated_at"}


def test_legacy_plaintext_is_migrated_on_first_read(tmp_db, fernet_key):
    _insert_raw(123, SECRET, updated_at=42.0)
    assert db.get_flight_api_credentials(123) == {"api_key": SECRET, "updated_at": 42.0}
    raw = _raw_key(123)
    assert raw.startswith("enc:v1:")
    assert SECRET not in raw
    assert db.get_flight_api_credentials(123) == {"api_key": SECRET, "updated_at": 42.0}
    assert _raw_key(123) == raw


def test_unset_key_falls_back_to_plaintext_and_warns_once(tmp_db, caplog):
    caplog.set_level(logging.WARNING, logger="flight_credentials")
    assert db.set_flight_api_credentials(123, SECRET) is True
    assert _raw_key(123) == SECRET
    assert db.get_flight_api_credentials(123)["api_key"] == SECRET
    assert db.get_flight_api_credentials(123)["api_key"] == SECRET
    assert _raw_key(123) == SECRET
    warnings = [r for r in caplog.records if "FLIGHT_CREDENTIALS_KEY" in r.getMessage()]
    assert len(warnings) == 1


def test_wrong_key_fails_without_leaking(tmp_db, fernet_key, monkeypatch, caplog):
    fernet = pytest.importorskip("cryptography.fernet")
    db.set_flight_api_credentials(123, SECRET)
    raw = _raw_key(123)
    other_key = fernet.Fernet.generate_key().decode()
    monkeypatch.setenv("FLIGHT_CREDENTIALS_KEY", other_key)
    with pytest.raises(FlightCredentialsError) as excinfo:
        db.get_flight_api_credentials(123)
    message = str(excinfo.value)
    for secret in (SECRET, raw, fernet_key, other_key):
        assert secret not in message
        assert secret not in caplog.text
    assert excinfo.value.__suppress_context__


def test_encrypted_value_without_key_fails(tmp_db, fernet_key, monkeypatch):
    db.set_flight_api_credentials(123, SECRET)
    monkeypatch.delenv("FLIGHT_CREDENTIALS_KEY")
    with pytest.raises(FlightCredentialsError) as excinfo:
        db.get_flight_api_credentials(123)
    assert SECRET not in str(excinfo.value)


def test_invalid_key_fails_without_leaking(tmp_db, monkeypatch, caplog):
    pytest.importorskip("cryptography.fernet")
    bad_key = "not-a-valid-fernet-key"
    monkeypatch.setenv("FLIGHT_CREDENTIALS_KEY", bad_key)
    with pytest.raises(FlightCredentialsError) as excinfo:
        db.set_flight_api_credentials(123, SECRET)
    assert bad_key not in str(excinfo.value)
    assert SECRET not in str(excinfo.value)
    assert _raw_key(123) is None
    _insert_raw(456, SECRET)
    with pytest.raises(FlightCredentialsError):
        db.get_flight_api_credentials(456)
    assert bad_key not in caplog.text
    assert SECRET not in caplog.text


def test_api_reports_misconfigured_key_as_service_unavailable(monkeypatch):
    from web.app import create_app

    from db.connection import _connect

    with _connect(commit=True) as c:
        c.execute(
            "INSERT INTO flight_api_credentials (user_id, api_key, updated_at) VALUES (?, ?, ?)",
            (42, "enc:v1:abc", 1.0),
        )
    monkeypatch.setenv("FLIGHT_CREDENTIALS_KEY", "not-a-fernet-key")
    client = create_app({"TESTING": True, "API_TOKEN": "test-token"}).test_client()
    response = client.get(
        "/flights/credentials?user_id=42",
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 503
    assert "not-a-fernet-key" not in response.get_data(as_text=True)
