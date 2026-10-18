"""Authenticated, encrypted gateway <-> bay channel.

Handshake (bay = initiator, gateway = responder), modelled on TLS 1.3 / SIGMA:

    bay  -> gw : hello   {device, eph_B, nonce_B, ts}            signed RSA_bay
    gw   -> bay: welcome {sid, eph_G, nonce_G, ts, eph_B, nonce_B} signed RSA_gw
    both       : shared = X25519(eph) ;  keys = HKDF(shared, nonce_B||nonce_G, sid)

  * X25519 Diffie-Hellman (Lecture 9a) gives a fresh session key every boot ->
    forward secrecy: stealing a device key later does not decrypt old traffic.
  * RSA-2048 signatures (Lecture 9b) on both halves stop a man-in-the-middle:
    each side only accepts keys signed by a device in its trusted list.
  * The welcome echoes eph_B/nonce_B so an old welcome cannot be replayed.
  * Separate keys per direction prevent reflection attacks.

Every message afterwards: AES-256-GCM with AAD = {sid, seq, ts, from}. The
receiver rejects stale timestamps and repeated sequence numbers (sliding
anti-replay window, as in IPsec), so captured packets cannot be re-sent.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

from .crypto import (
    InvalidTag, aes_decrypt, aes_encrypt, b64d, b64e, canonical, dh_generate, dh_public_bytes, dh_shared,
    fingerprint, hkdf, load_private_pem, load_public_pem, rsa_generate, rsa_private_pem, rsa_public_pem,
    rsa_sign, rsa_verify,
)

REPLAY_WINDOW = 128


class ChannelError(Exception):
    pass


class NoSession(ChannelError):
    pass


# ---------------------------------------------------------------------------
# Device identity (RSA key pair + trusted peers)
# ---------------------------------------------------------------------------
class DeviceIdentity:
    """keys/<id>_private.pem (0600), keys/<id>_public.pem, keys/trusted/<peer>_public.pem"""

    def __init__(self, device_id: str, keys_dir: Path, private_key=None):
        self.device_id = device_id
        self.keys_dir = Path(keys_dir)
        self.trusted_dir = self.keys_dir / "trusted"
        self.trusted_dir.mkdir(parents=True, exist_ok=True)
        self.private_key = private_key
        self._trusted_cache: dict[str, object] = {}

    @classmethod
    def load_or_create(cls, device_id: str, keys_dir: Path) -> "DeviceIdentity":
        keys_dir = Path(keys_dir)
        keys_dir.mkdir(parents=True, exist_ok=True)
        priv_path = keys_dir / f"{device_id}_private.pem"
        if priv_path.exists():
            key = load_private_pem(priv_path.read_bytes())
        else:
            key = rsa_generate()
            priv_path.write_bytes(rsa_private_pem(key))
            os.chmod(priv_path, 0o600)
        (keys_dir / f"{device_id}_public.pem").write_bytes(rsa_public_pem(key))
        return cls(device_id, keys_dir, key)

    @property
    def public_pem(self) -> bytes:
        return rsa_public_pem(self.private_key)

    def trust(self, peer_id: str, public_pem: bytes) -> None:
        load_public_pem(public_pem)  # validate
        (self.trusted_dir / f"{peer_id}_public.pem").write_bytes(public_pem)
        self._trusted_cache.pop(peer_id, None)

    def trusted_key(self, peer_id: str):
        if peer_id not in self._trusted_cache:
            p = self.trusted_dir / f"{peer_id}_public.pem"
            if not p.exists():
                return None
            self._trusted_cache[peer_id] = load_public_pem(p.read_bytes())
        return self._trusted_cache[peer_id]

    def fingerprint(self) -> str:
        return fingerprint(self.private_key.public_key())


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
class Session:
    def __init__(self, sid: str, me: str, peer: str, send_key: bytes, recv_key: bytes, skew_s: int):
        self.sid, self.me, self.peer = sid, me, peer
        self._send_key, self._recv_key = send_key, recv_key
        self.skew_s = skew_s
        self._send_seq = 0
        self._recv_max = 0
        self._seen: set[int] = set()
        self._lock = threading.Lock()
        self.created = time.time()
        self.last_seen = time.time()

    def seal(self, obj) -> dict:
        with self._lock:
            self._send_seq += 1
            seq = self._send_seq
        header = {"sid": self.sid, "seq": seq, "ts": int(time.time()), "from": self.me}
        ct = aes_encrypt(self._send_key, json.dumps(obj).encode(), canonical(header))
        return {**header, "ct": b64e(ct)}

    def open(self, env: dict):
        try:
            header = {"sid": env["sid"], "seq": int(env["seq"]), "ts": int(env["ts"]), "from": env["from"]}
            ct = b64d(env["ct"])
        except (KeyError, TypeError, ValueError) as e:
            raise ChannelError("malformed envelope") from e
        if header["sid"] != self.sid or header["from"] != self.peer:
            raise ChannelError("envelope for another session")
        if abs(time.time() - header["ts"]) > self.skew_s:
            raise ChannelError("stale message (possible replay)")
        try:
            obj = json.loads(aes_decrypt(self._recv_key, ct, canonical(header)))
        except InvalidTag:
            raise ChannelError("authentication failed (tampered or wrong key)") from None
        with self._lock:  # anti-replay check only after authentication
            seq = header["seq"]
            if seq <= self._recv_max - REPLAY_WINDOW or seq in self._seen:
                raise ChannelError("replayed sequence number")
            self._seen.add(seq)
            self._recv_max = max(self._recv_max, seq)
            self._seen = {s for s in self._seen if s > self._recv_max - REPLAY_WINDOW}
        self.last_seen = time.time()
        return obj


def _derive(shared: bytes, nonce_b: bytes, nonce_g: bytes, sid: str, bay: str) -> tuple[bytes, bytes]:
    okm = hkdf(shared, nonce_b + nonce_g, f"smartpark-v1|{sid}|{bay}".encode(), 64)
    return okm[:32], okm[32:]   # (bay->gw key, gw->bay key)


# ---------------------------------------------------------------------------
# Initiator (bay)
# ---------------------------------------------------------------------------
class HandshakeInitiator:
    def __init__(self, identity: DeviceIdentity, gateway_id: str = "gateway", skew_s: int = 60):
        self.identity, self.gateway_id, self.skew_s = identity, gateway_id, skew_s
        self._eph = dh_generate()
        self._nonce = secrets.token_bytes(16)

    def hello(self) -> dict:
        msg = {"type": "hello", "device": self.identity.device_id, "eph": b64e(dh_public_bytes(self._eph)),
               "nonce": b64e(self._nonce), "ts": int(time.time())}
        return {"msg": msg, "sig": b64e(rsa_sign(self.identity.private_key, canonical(msg)))}

    def finish(self, welcome: dict) -> Session:
        gw_key = self.identity.trusted_key(self.gateway_id)
        if gw_key is None:
            raise ChannelError("gateway public key not trusted - copy gateway_public.pem to keys/trusted/")
        msg, sig = welcome.get("msg", {}), b64d(welcome.get("sig", ""))
        if not rsa_verify(gw_key, sig, canonical(msg)):
            raise ChannelError("welcome signature invalid - possible man-in-the-middle")
        if msg.get("peer") != self.identity.device_id or msg.get("peer_eph") != b64e(dh_public_bytes(self._eph)) \
                or msg.get("peer_nonce") != b64e(self._nonce):
            raise ChannelError("welcome does not match our hello (replay?)")
        if abs(time.time() - msg.get("ts", 0)) > self.skew_s:
            raise ChannelError("welcome is stale")
        shared = dh_shared(self._eph, b64d(msg["eph"]))
        k_b2g, k_g2b = _derive(shared, self._nonce, b64d(msg["nonce"]), msg["sid"], self.identity.device_id)
        return Session(msg["sid"], self.identity.device_id, self.gateway_id, k_b2g, k_g2b, self.skew_s)


# ---------------------------------------------------------------------------
# Responder (gateway)
# ---------------------------------------------------------------------------
class HandshakeResponder:
    def __init__(self, identity: DeviceIdentity, skew_s: int = 60):
        self.identity, self.skew_s = identity, skew_s
        self._recent_nonces: dict[str, float] = {}
        self._lock = threading.Lock()

    def respond(self, hello: dict) -> tuple[dict, Session]:
        msg, sig_s = hello.get("msg", {}), hello.get("sig", "")
        device = msg.get("device", "")
        peer_key = self.identity.trusted_key(device)
        if peer_key is None:
            raise ChannelError(f"unknown device '{device}'")
        if not rsa_verify(peer_key, b64d(sig_s), canonical(msg)):
            raise ChannelError(f"bad hello signature from '{device}'")
        if abs(time.time() - msg.get("ts", 0)) > self.skew_s:
            raise ChannelError("stale hello")
        with self._lock:
            now = time.time()
            self._recent_nonces = {n: t for n, t in self._recent_nonces.items() if t > now - 2 * self.skew_s}
            if msg["nonce"] in self._recent_nonces:
                raise ChannelError("replayed hello")
            self._recent_nonces[msg["nonce"]] = now
        eph = dh_generate()
        nonce_g = secrets.token_bytes(16)
        sid = secrets.token_urlsafe(12)
        welcome = {"type": "welcome", "device": self.identity.device_id, "peer": device, "sid": sid,
                   "eph": b64e(dh_public_bytes(eph)), "nonce": b64e(nonce_g), "ts": int(time.time()),
                   "peer_eph": msg["eph"], "peer_nonce": msg["nonce"]}
        signed = {"msg": welcome, "sig": b64e(rsa_sign(self.identity.private_key, canonical(welcome)))}
        shared = dh_shared(eph, b64d(msg["eph"]))
        k_b2g, k_g2b = _derive(shared, b64d(msg["nonce"]), nonce_g, sid, device)
        return signed, Session(sid, self.identity.device_id, device, k_g2b, k_b2g, self.skew_s)
