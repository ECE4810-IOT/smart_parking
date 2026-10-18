"""Micro-benchmarks for every cryptographic operation (evidence for Activity 3
'efficient / optimised performance'). Run on the Pi:  python -m smartpark bench-crypto"""

from __future__ import annotations

import os
import platform
import statistics
import time

from . import crypto as c
from .channel import DeviceIdentity, HandshakeInitiator, HandshakeResponder
from .tickets import issue_ticket, verify_ticket


def _time(fn, n):
    samples = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t) * 1000)
    samples.sort()
    return statistics.median(samples), samples[int(0.95 * (len(samples) - 1))]


def bench_crypto():
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    gw = DeviceIdentity.load_or_create("gateway", tmp / "gw")
    bay = DeviceIdentity.load_or_create("bay1", tmp / "bay")
    gw.trust("bay1", bay.public_pem)
    bay.trust("gateway", gw.public_pem)
    key = os.urandom(32)
    msg = os.urandom(256)
    blob = c.aes_encrypt(key, msg)
    sig = c.rsa_sign(gw.private_key, msg)
    ticket = issue_ticket(gw.private_key, "alice", "bay1", 3600)
    dh_a, dh_b = c.dh_generate(), c.dh_generate()
    pub_b = c.dh_public_bytes(dh_b)

    def handshake():
        ini = HandshakeInitiator(bay)
        w, _ = HandshakeResponder(gw).respond(ini.hello())
        ini.finish(w)

    ops = [
        ("AES-256-GCM encrypt (256 B)", lambda: c.aes_encrypt(key, msg), 2000),
        ("AES-256-GCM decrypt (256 B)", lambda: c.aes_decrypt(key, blob), 2000),
        ("HMAC-SHA256", lambda: c.hmac_sha256(key, msg), 5000),
        ("OTP generate + Vigenere encrypt", lambda: c.vigenere_digits_encrypt(c.generate_otp(), "246810"), 5000),
        ("X25519 key exchange", lambda: c.dh_shared(dh_a, pub_b), 500),
        ("RSA-2048 sign (PSS)", lambda: c.rsa_sign(gw.private_key, msg), 100),
        ("RSA-2048 verify", lambda: c.rsa_verify(gw.private_key.public_key(), sig, msg), 1000),
        ("Ticket verify", lambda: verify_ticket(gw.private_key.public_key(), ticket, "bay1"), 500),
        ("Full authenticated handshake", handshake, 30),
        ("scrypt vault key (N=2^15)", lambda: c.derive_key_from_password("pw", b"0" * 16), 5),
    ]
    print(f"Crypto benchmark on {platform.machine()} / Python {platform.python_version()}")
    print(f"{'operation':40s} {'median ms':>10s} {'p95 ms':>10s}")
    for name, fn, n in ops:
        med, p95 = _time(fn, n)
        print(f"{name:40s} {med:10.4f} {p95:10.4f}")
