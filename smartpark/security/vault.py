"""Encrypted vault - the only place secrets live.

The admin password (Activity 3 "login before running the program") is not
compared against a stored hash: it is run through scrypt to derive the AES
key that decrypts the vault. A wrong password therefore cannot unlock
anything - AES-GCM authentication simply fails. Stealing the SD card yields
only ciphertext.

File format (JSON): {"v":1, "kdf":"scrypt", "n":32768, "salt":..., "blob":...}
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

from .crypto import InvalidTag, aes_decrypt, aes_encrypt, b64d, b64e, const_eq, derive_key_from_password

AAD = b"smartpark-vault-v1"


class VaultError(Exception):
    pass


class VaultLocked(VaultError):
    """Too many failed unlock attempts - retry after `retry_in` seconds."""

    def __init__(self, retry_in: float):
        super().__init__(f"locked, retry in {int(retry_in)} s")
        self.retry_in = retry_in


class Vault:
    def __init__(self, path: Path, key: bytes, salt: bytes, n: int, data: dict):
        self.path = Path(path)
        self._key = key
        self._salt = salt
        self._n = n
        self.data = data
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    @classmethod
    def create(cls, path, password: str, data: dict, n: int | None = None) -> "Vault":
        from .crypto import SCRYPT_N

        n = n or SCRYPT_N
        salt = secrets.token_bytes(16)
        key = derive_key_from_password(password, salt, n)
        v = cls(Path(path), key, salt, n, data)
        v.save()
        return v

    @classmethod
    def unlock(cls, path, password: str, guard: "UnlockGuard | None" = None) -> "Vault":
        path = Path(path)
        if not path.exists():
            raise VaultError(f"vault not found at {path} - run 'python -m smartpark init-gateway'")
        if guard:
            guard.check()
        meta = json.loads(path.read_text())
        salt, n = b64d(meta["salt"]), int(meta["n"])
        key = derive_key_from_password(password, salt, n)
        try:
            data = json.loads(aes_decrypt(key, b64d(meta["blob"]), AAD))
        except InvalidTag:
            if guard:
                guard.failure()
            raise VaultError("wrong password") from None
        if guard:
            guard.success()
        return cls(path, key, salt, n, data)

    def save(self) -> None:
        """Re-encrypt with a fresh nonce and write atomically (tmp + rename)."""
        with self._lock:
            blob = aes_encrypt(self._key, json.dumps(self.data).encode(), AAD)
            meta = {"v": 1, "kdf": "scrypt", "n": self._n, "salt": b64e(self._salt), "blob": b64e(blob)}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(meta))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)

    def verify_password(self, password: str) -> bool:
        """Re-authentication for admin actions (constant-time compare)."""
        return const_eq(derive_key_from_password(password, self._salt, self._n), self._key)

    # convenience accessors ---------------------------------------------
    def secret(self, name: str, default=None):
        return self.data.get("secrets", {}).get(name, default)

    @property
    def users(self) -> dict:
        return self.data.setdefault("users", {})


class UnlockGuard:
    """Exponential lockout for failed admin logins, persisted across restarts."""

    def __init__(self, path, max_attempts: int = 3, base_s: int = 30):
        self.path = Path(path)
        self.max_attempts = max_attempts
        self.base_s = base_s

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {"failures": 0, "lockouts": 0, "until": 0}

    def _save(self, st: dict):
        self.path.write_text(json.dumps(st))

    def check(self):
        st = self._load()
        if st["until"] > time.time():
            raise VaultLocked(st["until"] - time.time())

    def failure(self) -> int:
        st = self._load()
        st["failures"] += 1
        if st["failures"] >= self.max_attempts:
            st["lockouts"] += 1
            st["until"] = time.time() + self.base_s * 2 ** (st["lockouts"] - 1)
            st["failures"] = 0
        self._save(st)
        return self.max_attempts - st["failures"]

    def success(self):
        self._save({"failures": 0, "lockouts": 0, "until": 0})

    def status(self) -> dict:
        st = self._load()
        return {"attempts_left": self.max_attempts - st["failures"],
                "retry_in": max(0, int(st["until"] - time.time()))}
