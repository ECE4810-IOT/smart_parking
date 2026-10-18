"""Cryptographic building blocks.

| Purpose                         | Primitive (library)                | Lecture link |
|---------------------------------|------------------------------------|--------------|
| Vault / message encryption      | AES-256-GCM (cryptography)         | 8b symmetric |
| Password -> key                 | scrypt (cryptography)              | 8b           |
| Hub<->node key agreement        | X25519 Diffie-Hellman              | 9a           |
| Device authentication, tickets  | RSA-2048 PSS signatures            | 9b           |
| Session key derivation          | HKDF-SHA256                        |              |
| OTP integrity / storage         | HMAC-SHA256 (stdlib)               |              |
| OTP sent to the user            | digit-wise Vigenere (own code)     | 8c           |
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from cryptography.exceptions import InvalidSignature, InvalidTag  # noqa: F401  (re-exported)
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from ..util import metric

# ---------------------------------------------------------------------------
# encoding helpers
# ---------------------------------------------------------------------------

def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def canonical(obj) -> bytes:
    """Deterministic JSON so signatures/AAD are byte-identical on both ends."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


class _Timer:
    """Context manager that logs crypto timings to metrics/crypto.csv."""

    def __init__(self, op: str, **extra):
        self.op, self.extra = op, extra

    def __enter__(self):
        self.t = time.perf_counter()
        return self

    def __exit__(self, *exc):
        metric("crypto", op=self.op, ms=round((time.perf_counter() - self.t) * 1000, 4), **self.extra)


# ---------------------------------------------------------------------------
# key derivation
# ---------------------------------------------------------------------------
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1   # ~32 MiB, ~0.1-0.3 s on a Pi 4


def derive_key_from_password(password: str, salt: bytes, n: int = SCRYPT_N) -> bytes:
    """scrypt is memory-hard, so GPU brute force of a stolen vault is expensive."""
    with _Timer("scrypt_kdf"):
        return Scrypt(salt=salt, length=32, n=n, r=SCRYPT_R, p=SCRYPT_P).derive(password.encode())


def hkdf(secret: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(secret)


# ---------------------------------------------------------------------------
# AES-256-GCM authenticated encryption
# ---------------------------------------------------------------------------

def aes_encrypt(key: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    """Returns nonce(12) || ciphertext||tag. GCM gives confidentiality AND
    integrity: any modified byte makes decryption fail."""
    with _Timer("aes_gcm_encrypt", bytes=len(plaintext)):
        nonce = os.urandom(12)
        return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def aes_decrypt(key: bytes, blob: bytes, aad: bytes = b"") -> bytes:
    with _Timer("aes_gcm_decrypt", bytes=len(blob)):
        return AESGCM(key).decrypt(blob[:12], blob[12:], aad)  # raises InvalidTag


# ---------------------------------------------------------------------------
# RSA-2048 signatures (device identity, Kerberos-style tickets)
# ---------------------------------------------------------------------------

def rsa_generate() -> rsa.RSAPrivateKey:
    with _Timer("rsa2048_keygen"):
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def rsa_private_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def rsa_public_pem(key) -> bytes:
    pub = key.public_key() if hasattr(key, "public_key") else key
    return pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def load_private_pem(pem: bytes):
    return serialization.load_pem_private_key(pem, password=None)


def load_public_pem(pem: bytes):
    return serialization.load_pem_public_key(pem)


_PSS = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH)


def rsa_sign(private_key, data: bytes) -> bytes:
    with _Timer("rsa2048_sign"):
        return private_key.sign(data, _PSS, hashes.SHA256())


def rsa_verify(public_key, signature: bytes, data: bytes) -> bool:
    with _Timer("rsa2048_verify"):
        try:
            public_key.verify(signature, data, _PSS, hashes.SHA256())
            return True
        except InvalidSignature:
            return False


def fingerprint(public_key) -> str:
    return hashlib.sha256(rsa_public_pem(public_key)).hexdigest()[:16]


# ---------------------------------------------------------------------------
# X25519 Diffie-Hellman
# ---------------------------------------------------------------------------

def dh_generate():
    return x25519.X25519PrivateKey.generate()


def dh_public_bytes(priv) -> bytes:
    return priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def dh_shared(priv, peer_public: bytes) -> bytes:
    with _Timer("x25519_exchange"):
        return priv.exchange(x25519.X25519PublicKey.from_public_bytes(peer_public))


# ---------------------------------------------------------------------------
# HMAC / constant-time helpers
# ---------------------------------------------------------------------------

def hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def const_eq(a: bytes | str, b: bytes | str) -> bool:
    if isinstance(a, str):
        a = a.encode()
    if isinstance(b, str):
        b = b.encode()
    return hmac.compare_digest(a, b)


# ---------------------------------------------------------------------------
# OTP + digit-wise Vigenere cipher (own implementation, Lecture 8c)
# ---------------------------------------------------------------------------
OTP_DIGITS = 6


def generate_otp(digits: int = OTP_DIGITS) -> str:
    """Cryptographically secure, uniformly random OTP (secrets, not random)."""
    return f"{secrets.randbelow(10 ** digits):0{digits}d}"


def vigenere_digits_encrypt(otp: str, pin: str) -> str:
    """C_i = (OTP_i + PIN_i) mod 10.

    Security argument (perfect secrecy): OTP is fresh and uniformly random for
    every request, so C is uniformly random and statistically independent of
    the PIN. An attacker who intercepts any number of Telegram messages learns
    nothing about the OTP or the PIN. The PIN could only be recovered from a
    (C, OTP) pair - and the OTP is never transmitted or stored in plaintext.
    """
    _check_digits(otp, pin)
    return "".join(str((int(o) + int(k)) % 10) for o, k in zip(otp, pin))


def vigenere_digits_decrypt(cipher: str, pin: str) -> str:
    """OTP_i = (C_i - PIN_i) mod 10 - performed at the kiosk."""
    _check_digits(cipher, pin)
    return "".join(str((int(c) - int(k)) % 10) for c, k in zip(cipher, pin))


def _check_digits(a: str, b: str):
    if not (a.isdigit() and b.isdigit() and len(a) == len(b)):
        raise ValueError("expected two digit strings of equal length")
