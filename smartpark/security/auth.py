"""User enrolment and OTP access control (Activity 1b + 3).

Access flow at the kiosk
    1. user types a username
    2. if it is registered, a 6-digit OTP is generated (secrets), only an HMAC of
       it is stored, valid for 60 s and usable once
    3. Telegram receives C = Vigenere_digits(OTP, PIN) - never the OTP itself
    4. user types C and their PIN; the kiosk computes OTP' = C - PIN and checks
       HMAC(OTP') against the stored value in constant time
    => two factors: something you HAVE (your Telegram) + something you KNOW (PIN)

Defences
    * identical response whether or not the username exists (no enumeration)
    * per-user lockout with exponential back-off after `max_attempts` failures
    * per-user request cooldown + global rate limit (stops Telegram spam / DoS)
    * enrolment codes are single-use, hashed and expire
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from .crypto import const_eq, generate_otp, hmac_sha256, vigenere_digits_decrypt, vigenere_digits_encrypt

USERNAME_RE = re.compile(r"^[a-z0-9_]{3,20}$")
PIN_RE = re.compile(r"^\d{6}$")


def normalise_username(name: str) -> str:
    return (name or "").strip().lower()


def pin_problem(pin: str) -> str | None:
    if not PIN_RE.match(pin or ""):
        return "PIN must be exactly 6 digits"
    if len(set(pin)) == 1 or pin in "0123456789" or pin in "9876543210":
        return "PIN is too easy to guess"
    return None


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


@dataclass
class AccessRequest:
    request_id: str
    username: str | None        # None for unknown/locked users (dummy request)
    otp_mac: bytes
    expires: float
    attempts: int = 0
    created: float = field(default_factory=time.time)
    delivered_ms: float | None = None
    cipher_mac: bytes = b""     # lets the admin log tell 'wrong PIN' from 'mistyped code'


@dataclass
class RequestOutcome:
    request_id: str
    deliver_to: str | None = None   # Telegram chat id (None = nothing to send)
    cipher: str | None = None
    username: str | None = None
    reason: str = "ok"              # internal only - never shown on the kiosk
    expires_in: int = 60

    @property
    def ref(self) -> str:
        """Short reference shown on BOTH the kiosk and the Telegram message, so the user can
        tell which message belongs to this attempt. Derived from the random request id, so it
        reveals nothing (and exists for unknown users too)."""
        return hashlib.sha256(self.request_id.encode()).hexdigest()[:4].upper()


@dataclass
class VerifyOutcome:
    ok: bool
    username: str | None = None
    attempts_left: int = 0
    locked: bool = False
    expired: bool = False
    reason: str = ""


class AccessManager:
    def __init__(self, vault, access_cfg: dict):
        self.vault = vault
        self.cfg = access_cfg
        self._mac_key = secrets.token_bytes(32)  # per boot; OTPs never outlive a reboot
        self._requests: dict[str, AccessRequest] = {}
        self._fail: dict[str, dict] = {}         # username -> {count, lockouts, until}
        self._last_request: dict[str, float] = {}
        self._global = deque()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ users
    def _user(self, username: str) -> dict | None:
        return self.vault.users.get(username)

    def create_user(self, username: str) -> str:
        username = normalise_username(username)
        if not USERNAME_RE.match(username):
            raise ValueError("username must be 3-20 chars: a-z, 0-9, _")
        if username in self.vault.users:
            raise ValueError("user already exists")
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
        self.vault.users[username] = {
            "status": "pending", "chat_id": None, "pin": None, "created": time.time(),
            "enrol_hash": _hash_code(code),
            "enrol_expires": time.time() + self.cfg["enrol_code_ttl_min"] * 60,
        }
        self.vault.save()
        return code

    def _check_enrol(self, user: dict | None, code: str) -> bool:
        return bool(user and user.get("enrol_hash") and time.time() < user.get("enrol_expires", 0)
                    and const_eq(_hash_code((code or "").strip().upper()), user["enrol_hash"]))

    def link_telegram(self, username: str, code: str, chat_id: str) -> bool:
        """Step 2 of enrolment: user sends /register <username> <code> to the bot."""
        with self._lock:
            user = self._user(normalise_username(username))
            if not self._check_enrol(user, code):
                return False
            user["chat_id"] = str(chat_id)
            self.vault.save()
            return True

    def complete_setup(self, username: str, code: str, pin: str) -> tuple[bool, str]:
        """Step 3: at the kiosk, user enters username + code and chooses a PIN.
        The PIN never travels over Telegram."""
        with self._lock:
            username = normalise_username(username)
            user = self._user(username)
            if not self._check_enrol(user, code):
                return False, "Invalid or expired enrolment code"
            if not user.get("chat_id"):
                return False, "Link your Telegram first: send /register to the bot"
            problem = pin_problem(pin)
            if problem:
                return False, problem
            user.update(pin=pin, status="active", enrol_hash=None, enrol_expires=0,
                        activated=time.time())
            self.vault.save()
            return True, "Setup complete"

    def reset_pin(self, username: str) -> str:
        """Forgotten / mistyped PIN: keep the Telegram link, issue a new enrolment code so the
        user can choose a new PIN at the kiosk (New user setup)."""
        with self._lock:
            username = normalise_username(username)
            user = self._user(username)
            if not user:
                raise ValueError("no such user")
            code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
            user.update(status="pending", pin=None, enrol_hash=_hash_code(code),
                        enrol_expires=time.time() + self.cfg["enrol_code_ttl_min"] * 60)
            self._fail.pop(username, None)
            self.vault.save()
            return code

    def remove_user(self, username: str) -> bool:
        with self._lock:
            ok = self.vault.users.pop(normalise_username(username), None) is not None
            if ok:
                self.vault.save()
            return ok

    def list_users(self) -> list[dict]:
        out = []
        for name, u in sorted(self.vault.users.items()):
            out.append({"username": name, "status": u.get("status"),
                        "telegram": bool(u.get("chat_id")), "locked": self.is_locked(name)})
        return out

    def user_for_chat(self, chat_id: str) -> str | None:
        for name, u in self.vault.users.items():
            if u.get("chat_id") == str(chat_id) and u.get("status") == "active":
                return name
        return None

    def chat_for_user(self, username: str) -> str | None:
        u = self._user(username)
        return u.get("chat_id") if u else None

    # ---------------------------------------------------------------- lockout
    def is_locked(self, username: str) -> bool:
        st = self._fail.get(username)
        return bool(st and st["until"] > time.time())

    def unlock_user(self, username: str) -> None:
        self._fail.pop(normalise_username(username), None)

    def _register_failure(self, username: str) -> bool:
        st = self._fail.setdefault(username, {"count": 0, "lockouts": 0, "until": 0})
        st["count"] += 1
        if st["count"] >= self.cfg["max_attempts"]:
            st["lockouts"] += 1
            st["until"] = time.time() + self.cfg["lockout_base_s"] * 2 ** (st["lockouts"] - 1)
            st["count"] = 0
            return True
        return False

    # ----------------------------------------------------------------- OTP
    def _cleanup(self):
        now = time.time()
        for rid in [r for r, req in self._requests.items() if req.expires < now - 300]:
            self._requests.pop(rid, None)

    def request_otp(self, username: str) -> RequestOutcome:
        with self._lock:
            self._cleanup()
            username = normalise_username(username)
            now = time.time()
            ttl = self.cfg["otp_ttl_s"]
            rid = secrets.token_urlsafe(12)
            # always create a request so unknown users look identical to real ones
            dummy = AccessRequest(rid, None, secrets.token_bytes(32), now + ttl)

            while self._global and self._global[0] < now - 60:
                self._global.popleft()
            if len(self._global) >= self.cfg["global_requests_per_min"]:
                self._requests[rid] = dummy
                return RequestOutcome(rid, reason="global_rate_limit", expires_in=ttl)
            self._global.append(now)

            user = self._user(username)
            if not user or user.get("status") != "active" or not user.get("chat_id") or not user.get("pin"):
                self._requests[rid] = dummy
                return RequestOutcome(rid, username=username, reason="unknown_user", expires_in=ttl)
            if self.is_locked(username):
                self._requests[rid] = dummy
                return RequestOutcome(rid, username=username, reason="locked", expires_in=ttl)
            if now - self._last_request.get(username, 0) < self.cfg["request_cooldown_s"]:
                # Asked again too soon (e.g. pressed Back and re-entered the username). Do NOT hand
                # out a dummy request - the code already on their phone would then never work.
                # Re-use the still-valid pending request instead; no new Telegram message is sent.
                pending = next((q for q in self._requests.values()
                                if q.username == username and q.expires > now), None)
                if pending:
                    return RequestOutcome(pending.request_id, username=username, reason="cooldown_reuse",
                                          expires_in=int(pending.expires - now))
                self._requests[rid] = dummy
                return RequestOutcome(rid, username=username, reason="cooldown", expires_in=ttl)
            self._last_request[username] = now
            # invalidate any older pending request for this user
            for r in [r for r, q in self._requests.items() if q.username == username]:
                self._requests.pop(r)

            otp = generate_otp()
            cipher = vigenere_digits_encrypt(otp, user["pin"])
            self._requests[rid] = AccessRequest(rid, username, hmac_sha256(self._mac_key, otp.encode()),
                                                now + ttl,
                                                cipher_mac=hmac_sha256(self._mac_key, b"c:" + cipher.encode()))
            del otp  # plaintext OTP is not kept anywhere
            return RequestOutcome(rid, deliver_to=user["chat_id"], cipher=cipher, username=username,
                                  expires_in=ttl)

    def verify(self, request_id: str, code: str, pin: str) -> VerifyOutcome:
        with self._lock:
            req = self._requests.get(request_id)
            max_att = self.cfg["max_attempts"]
            if not req:
                return VerifyOutcome(False, reason="unknown_request", expired=True)
            if time.time() > req.expires:
                self._requests.pop(request_id, None)
                return VerifyOutcome(False, username=req.username, expired=True, reason="expired")
            code, pin = (code or "").strip(), (pin or "").strip()
            ok = False
            if code.isdigit() and pin.isdigit() and len(code) == len(pin) == 6:
                otp_guess = vigenere_digits_decrypt(code, pin)
                ok = const_eq(hmac_sha256(self._mac_key, otp_guess.encode()), req.otp_mac)
            if ok and req.username and not self.is_locked(req.username):
                self._requests.pop(request_id, None)            # single use
                self._fail.pop(req.username, None)
                return VerifyOutcome(True, username=req.username, attempts_left=max_att)
            req.attempts += 1
            locked = False
            if req.username:
                locked = self._register_failure(req.username)
            left = max(0, max_att - req.attempts)
            if left == 0 or locked:
                self._requests.pop(request_id, None)
            # Diagnosis for the admin log only (the kiosk always shows the same generic message):
            # code typed exactly as sent -> the PIN was wrong; otherwise the code itself was wrong
            # (mistyped, decoded by hand, an old message, or the two fields swapped).
            code_ok = bool(req.cipher_mac) and const_eq(hmac_sha256(self._mac_key, b"c:" + code.encode()),
                                                        req.cipher_mac)
            reason = "bad_pin" if code_ok else ("bad_code" if req.username else "no_such_request")
            return VerifyOutcome(False, username=req.username, attempts_left=left, locked=locked, reason=reason)

    def pending_request(self, request_id: str) -> AccessRequest | None:
        return self._requests.get(request_id)
