"""Paired remote devices and one-time pairing codes (remote-voice-gateway-20261020 T2).

One ``DeviceRegistry`` exists per process. The main API lifespan creates it, keeps
it on the main ``app.state`` (operator endpoints mint codes, list and revoke) and
injects the same instance into the gateway app (``POST /pair`` and the device
bearer-token dependency). A lock serialises every read and write, because the two
apps run on different threads.

Security model:
- A pairing code is held in memory only, lives 120 s, is single-use, and only one
  is live at a time (minting replaces it). After ``MAX_PAIR_ATTEMPTS`` wrong
  guesses the live code is burned and a fresh mint is required.
- A device token is ``secrets.token_urlsafe(32)``, returned once by ``exchange``
  and never stored or logged. The file keeps only its SHA-256 hex digest, compared
  with ``hmac.compare_digest``.
- The device file is loaded once at construction. A missing file is zero devices;
  a malformed one is zero devices plus one warning naming the error class only.
- Logs carry device ids and counts only, never codes, tokens, hashes or names.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
import unicodedata
from pathlib import Path

from opencohost.config import settings
from opencohost.config.storage import atomic_write_text

logger = logging.getLogger("OpenCohost.gateway")

PAIR_CODE_TTL_S = 120.0
MAX_PAIR_ATTEMPTS = 5
LAST_USED_WRITE_INTERVAL_S = 60.0
# No 0/O, 1/I/L or U: easy to read off a screen and type on a phone.
# 30 symbols ** 10 characters ~= 2**49 codes.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
CODE_LENGTH = 10
DEVICE_NAME_MAX_LEN = 40
DEFAULT_DEVICE_NAME = "Phone"
_MAX_PRESENTED_LEN = 256  # longest code or token worth hashing/comparing
_FILE_VERSION = 1
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_DEVICE_ID = re.compile(r"[0-9a-f]{16}")
_RECORD_KEYS = frozenset({"device_id", "name", "token_sha256", "created_at", "last_used_at", "revoked"})


class DeviceFileError(Exception):
    """The device file exists but is unreadable or has the wrong shape."""


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalize_code(value) -> str | None:
    if not isinstance(value, str) or len(value) > _MAX_PRESENTED_LEN:
        return None
    return "".join(value.split()).replace("-", "").upper()


def sanitize_device_name(value) -> str:
    """Drop control and format characters, collapse spaces, cap the length."""
    if not isinstance(value, str):
        return DEFAULT_DEVICE_NAME
    kept = "".join(ch for ch in value[: DEVICE_NAME_MAX_LEN * 8] if not unicodedata.category(ch).startswith("C"))
    name = " ".join(kept.split())[:DEVICE_NAME_MAX_LEN].strip()
    return name or DEFAULT_DEVICE_NAME


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_record(raw) -> dict:
    if not isinstance(raw, dict) or set(raw) != _RECORD_KEYS:
        raise DeviceFileError("device record has the wrong keys")
    if not isinstance(raw["device_id"], str) or not _DEVICE_ID.fullmatch(raw["device_id"]):
        raise DeviceFileError("device record has a bad device_id")
    if not isinstance(raw["token_sha256"], str) or not _SHA256_HEX.fullmatch(raw["token_sha256"]):
        raise DeviceFileError("device record has a bad token hash")
    if not isinstance(raw["name"], str) or not _is_number(raw["created_at"]):
        raise DeviceFileError("device record has a bad name or created_at")
    if raw["last_used_at"] is not None and not _is_number(raw["last_used_at"]):
        raise DeviceFileError("device record has a bad last_used_at")
    if not isinstance(raw["revoked"], bool):
        raise DeviceFileError("device record has a bad revoked flag")
    return dict(raw, name=sanitize_device_name(raw["name"]))


def _parse_device_file(text: str) -> dict[str, dict]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise DeviceFileError("device file is not JSON") from exc
    if not isinstance(data, dict) or data.get("version") != _FILE_VERSION:
        raise DeviceFileError("device file has the wrong version")
    entries = data.get("devices")
    if not isinstance(entries, list):
        raise DeviceFileError("device file has no device list")
    devices: dict[str, dict] = {}
    for raw in entries:
        record = _validate_record(raw)
        if record["device_id"] in devices:
            raise DeviceFileError("device file repeats a device_id")
        devices[record["device_id"]] = record
    return devices


class DeviceRegistry:
    """Thread-safe store of paired devices plus the single live pairing code."""

    def __init__(self, path=None, clock=time.time) -> None:
        self._path = Path(path if path is not None else settings.REMOTE_DEVICES_FILE)
        self._clock = clock
        self._lock = threading.Lock()
        self._code: str | None = None
        self._code_expires_at = 0.0
        self._failed_attempts = 0
        # device_id -> clock time its last_used_at was last written to disk.
        self._last_used_flushed: dict[str, float] = {}
        self._devices = self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> dict[str, dict]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            logger.warning("Remote devices file unreadable (%s); treating as zero devices", type(exc).__name__)
            return {}
        try:
            return _parse_device_file(text)
        except DeviceFileError as exc:
            logger.warning("Remote devices file malformed (%s); treating as zero devices", type(exc).__name__)
            return {}

    def _persist(self) -> None:
        """Write every record; raises OSError. Caller holds the lock."""
        payload = {"version": _FILE_VERSION, "devices": list(self._devices.values())}
        atomic_write_text(self._path, json.dumps(payload, indent=2))

    # -- pairing codes -------------------------------------------------------

    def mint_code(self) -> tuple[str, float]:
        """Mint the one live code, replacing any unused one. Memory only."""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        with self._lock:
            self._code = code
            self._code_expires_at = self._clock() + PAIR_CODE_TTL_S
            self._failed_attempts = 0
            expires_at = self._code_expires_at
        logger.info("Remote pairing code minted (ttl=%ss)", int(PAIR_CODE_TTL_S))
        return code, expires_at

    def exchange(self, code, device_name) -> tuple[str, str] | None:
        """Trade the live code for a new device token, once. None on any failure."""
        presented = _normalize_code(code)
        with self._lock:
            live = self._code
            if live is None:
                return None
            if self._clock() >= self._code_expires_at:
                self._code = None
                return None
            if presented is None or not hmac.compare_digest(presented.encode("utf-8"), live.encode("utf-8")):
                self._failed_attempts += 1
                if self._failed_attempts >= MAX_PAIR_ATTEMPTS:
                    self._code = None
                    logger.warning(
                        "Remote pairing code burned after %d failed attempts", self._failed_attempts
                    )
                return None
            self._code = None  # single use, consumed before anything can fail
            now = self._clock()
            device_id = secrets.token_hex(8)
            while device_id in self._devices:
                device_id = secrets.token_hex(8)
            token = secrets.token_urlsafe(32)
            self._devices[device_id] = {
                "device_id": device_id,
                "name": sanitize_device_name(device_name),
                "token_sha256": _hash_token(token),
                "created_at": now,
                "last_used_at": None,
                "revoked": False,
            }
            try:
                self._persist()
            except OSError as exc:
                # A device that cannot be persisted would vanish on restart: undo it.
                del self._devices[device_id]
                logger.warning("Remote device not paired: store write failed (%s)", type(exc).__name__)
                return None
            self._last_used_flushed[device_id] = now
            count = sum(not d["revoked"] for d in self._devices.values())
        logger.info("Remote device paired: device_id=%s active_devices=%d", device_id, count)
        return device_id, token

    # -- device tokens -------------------------------------------------------

    def verify(self, token) -> str | None:
        """Return the device id for a valid, non-revoked token, else None."""
        if not isinstance(token, str) or not token or len(token) > _MAX_PRESENTED_LEN:
            return None
        digest = _hash_token(token).encode("ascii")
        with self._lock:
            match = None
            # Compare against every live entry (no early exit) in constant time.
            for record in self._devices.values():
                if hmac.compare_digest(digest, record["token_sha256"].encode("ascii")) and not record["revoked"]:
                    match = record
            if match is None:
                return None
            now = self._clock()
            match["last_used_at"] = now
            device_id = match["device_id"]
            if now - self._last_used_flushed.get(device_id, float("-inf")) >= LAST_USED_WRITE_INTERVAL_S:
                self._last_used_flushed[device_id] = now
                try:
                    self._persist()
                except OSError as exc:
                    logger.warning("Remote device last-used write failed (%s)", type(exc).__name__)
        return device_id

    def list_devices(self) -> list[dict]:
        """Operator view: no token hashes."""
        with self._lock:
            return [
                {key: record[key] for key in ("device_id", "name", "created_at", "last_used_at", "revoked")}
                for record in self._devices.values()
            ]

    def revoke(self, device_id) -> bool:
        """Revoke a device; True when it exists. Effective in memory even if the write fails."""
        with self._lock:
            record = self._devices.get(device_id) if isinstance(device_id, str) else None
            if record is None:
                return False
            record["revoked"] = True
            try:
                self._persist()
            except OSError as exc:
                logger.warning(
                    "Remote device revoked in memory only, store write failed (%s): device_id=%s",
                    type(exc).__name__,
                    device_id,
                )
                return True
        logger.info("Remote device revoked: device_id=%s", device_id)
        return True
