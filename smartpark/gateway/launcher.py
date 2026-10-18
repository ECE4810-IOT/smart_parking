"""Starts the gateway in a LOCKED state until the admin logs in.

This is Activity 3's "login before running the program": nothing that needs a
secret (Telegram, ThingSpeak, the user database, the gateway's private key)
can start until the admin password has decrypted the vault.
"""

from __future__ import annotations

import logging
import threading

from ..config import data_path
from ..security.vault import UnlockGuard, Vault, VaultError, VaultLocked
from .app import Gateway

log = logging.getLogger(__name__)


class GatewayLauncher:
    def __init__(self, cfg: dict, fake_cloud: bool = False, telegram=None):
        self.cfg = cfg
        self.fake_cloud = fake_cloud
        self.telegram = telegram
        self.vault_path = data_path(cfg, "vault.json")
        self.guard = UnlockGuard(data_path(cfg, "unlock_guard.json"))
        self.gateway: Gateway | None = None
        self._lock = threading.Lock()

    @property
    def locked(self) -> bool:
        return self.gateway is None

    def unlock(self, password: str) -> tuple[bool, str]:
        with self._lock:
            if self.gateway is not None:
                return True, "already unlocked"
            try:
                vault = Vault.unlock(self.vault_path, password, self.guard)
            except VaultLocked as e:
                return False, f"Too many attempts - locked for {int(e.retry_in)} s"
            except VaultError as e:
                st = self.guard.status()
                log.warning("Admin login failed: %s", e)
                if st["retry_in"]:
                    return False, f"Wrong password - locked for {st['retry_in']} s"
                return False, f"Wrong password ({st['attempts_left']} attempt(s) left)"
            self.gateway = Gateway(self.cfg, vault, telegram=self.telegram, fake_cloud=self.fake_cloud)
            self.gateway.start()
            log.info("Vault unlocked - gateway running")
            return True, "unlocked"

    def stop(self):
        if self.gateway:
            self.gateway.stop()
