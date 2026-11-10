from __future__ import annotations

import logging
import os

logger = logging.getLogger("flight_credentials")

ENV_VAR = "FLIGHT_CREDENTIALS_KEY"
PREFIX = "enc:v1:"

_warned_unset = False
_cached = None


class FlightCredentialsError(RuntimeError):
    pass


def _fernet():
    global _warned_unset, _cached
    raw = os.getenv(ENV_VAR, "").strip()
    if not raw:
        if not _warned_unset:
            _warned_unset = True
            logger.warning(
                "%s is not set; SerpApi keys are stored without encryption", ENV_VAR
            )
        return None
    if _cached is not None and _cached[0] == raw:
        return _cached[1]
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        raise FlightCredentialsError(
            f"{ENV_VAR} is set but the cryptography package is not installed"
        ) from None
    try:
        fernet = Fernet(raw.encode("ascii"))
    except Exception:
        raise FlightCredentialsError(f"{ENV_VAR} is not a valid Fernet key") from None
    _cached = (raw, fernet)
    return fernet


def is_configured() -> bool:
    return _fernet() is not None


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(api_key: str) -> str:
    fernet = _fernet()
    if fernet is None:
        return api_key
    token = fernet.encrypt(api_key.encode("utf-8")).decode("ascii")
    return PREFIX + token


def decrypt(value: str) -> str:
    if not is_encrypted(value):
        return value
    fernet = _fernet()
    if fernet is None:
        raise FlightCredentialsError(
            f"A stored SerpApi key is encrypted but {ENV_VAR} is not set"
        )
    try:
        return fernet.decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:
        raise FlightCredentialsError(
            f"A stored SerpApi key could not be decrypted with {ENV_VAR}"
        ) from None
