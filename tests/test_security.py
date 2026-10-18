import time

import pytest

from smartpark.security import crypto as c
from smartpark.security.auth import AccessManager, pin_problem
from smartpark.security.channel import ChannelError, DeviceIdentity, HandshakeInitiator, HandshakeResponder
from smartpark.security.tickets import TicketError, issue_ticket, verify_ticket
from smartpark.security.vault import UnlockGuard, Vault, VaultError, VaultLocked

FAST_N = 2 ** 12


# ----------------------------------------------------------------- primitives
def test_aes_gcm_roundtrip_and_tamper():
    key = b"k" * 32
    blob = c.aes_encrypt(key, b"secret", b"aad")
    assert c.aes_decrypt(key, blob, b"aad") == b"secret"
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(c.InvalidTag):
        c.aes_decrypt(key, tampered, b"aad")
    with pytest.raises(c.InvalidTag):
        c.aes_decrypt(key, blob, b"other-aad")


def test_vigenere_digits_roundtrip_and_independence():
    for _ in range(200):
        otp, pin = c.generate_otp(), c.generate_otp()
        cipher = c.vigenere_digits_encrypt(otp, pin)
        assert c.vigenere_digits_decrypt(cipher, pin) == otp
    # for a fixed PIN, ciphertext digits are uniform because OTP is uniform
    pin = "246810"
    counts = [0] * 10
    for _ in range(5000):
        counts[int(c.vigenere_digits_encrypt(c.generate_otp(), pin)[0])] += 1
    assert min(counts) > 350 and max(counts) < 650


def test_rsa_sign_verify():
    k = c.rsa_generate()
    sig = c.rsa_sign(k, b"msg")
    assert c.rsa_verify(k.public_key(), sig, b"msg")
    assert not c.rsa_verify(k.public_key(), sig, b"msg2")


def test_tickets():
    gw = c.rsa_generate()
    t = issue_ticket(gw, "alice", "bay1", 60)
    assert verify_ticket(gw.public_key(), t, "bay1")["user"] == "alice"
    with pytest.raises(TicketError):
        verify_ticket(gw.public_key(), t, "bay2")
    forged = {"payload": dict(t["payload"], user="mallory"), "sig": t["sig"]}
    with pytest.raises(TicketError):
        verify_ticket(gw.public_key(), forged, "bay1")
    expired = issue_ticket(gw, "alice", "bay1", -1)
    with pytest.raises(TicketError):
        verify_ticket(gw.public_key(), expired, "bay1")
    other = c.rsa_generate()
    with pytest.raises(TicketError):
        verify_ticket(other.public_key(), t, "bay1")


# ----------------------------------------------------------------- channel
@pytest.fixture
def identities(tmp_path):
    gw = DeviceIdentity.load_or_create("gateway", tmp_path / "gw")
    bay = DeviceIdentity.load_or_create("bay1", tmp_path / "bay")
    gw.trust("bay1", bay.public_pem)
    bay.trust("gateway", gw.public_pem)
    return gw, bay


def _connect(gw, bay):
    ini = HandshakeInitiator(bay)
    welcome, s_gw = HandshakeResponder(gw).respond(ini.hello())
    return s_gw, ini.finish(welcome)


def test_handshake_and_messages(identities):
    s_gw, s_bay = _connect(*identities)
    assert s_gw.open(s_bay.seal({"a": 1})) == {"a": 1}
    assert s_bay.open(s_gw.seal({"b": 2})) == {"b": 2}


def test_replay_rejected(identities):
    s_gw, s_bay = _connect(*identities)
    env = s_bay.seal({"cmd": "x"})
    s_gw.open(env)
    with pytest.raises(ChannelError):
        s_gw.open(env)


def test_tampered_envelope_rejected(identities):
    s_gw, s_bay = _connect(*identities)
    env = s_bay.seal({"cmd": "x"})
    env["seq"] += 100          # header is authenticated as AAD
    with pytest.raises(ChannelError):
        s_gw.open(env)


def test_reflection_rejected(identities):
    s_gw, s_bay = _connect(*identities)
    env = s_bay.seal({"x": 1})
    with pytest.raises(ChannelError):   # bay cannot be fed its own message back
        s_bay.open(env)


def test_untrusted_device_rejected(tmp_path, identities):
    gw, _ = identities
    rogue = DeviceIdentity.load_or_create("bay1", tmp_path / "rogue")  # same name, different key
    rogue.trust("gateway", gw.public_pem)
    with pytest.raises(ChannelError):
        HandshakeResponder(gw).respond(HandshakeInitiator(rogue).hello())


def test_replayed_hello_rejected(identities):
    gw, bay = identities
    responder = HandshakeResponder(gw)
    hello = HandshakeInitiator(bay).hello()
    responder.respond(hello)
    with pytest.raises(ChannelError):
        responder.respond(hello)


def test_mitm_welcome_rejected(tmp_path, identities):
    gw, bay = identities
    fake_gw = DeviceIdentity.load_or_create("gateway", tmp_path / "fake")
    fake_gw.trust("bay1", bay.public_pem)
    ini = HandshakeInitiator(bay)
    welcome, _ = HandshakeResponder(fake_gw).respond(ini.hello())
    with pytest.raises(ChannelError):
        ini.finish(welcome)


# ----------------------------------------------------------------- vault
def test_vault(tmp_path):
    p = tmp_path / "vault.json"
    Vault.create(p, "CorrectHorse9", {"secrets": {"x": "TOPSECRET-TOKEN"}}, n=FAST_N)
    assert b"TOPSECRET" not in p.read_bytes()
    v = Vault.unlock(p, "CorrectHorse9")
    assert v.secret("x") == "TOPSECRET-TOKEN"
    assert v.verify_password("CorrectHorse9") and not v.verify_password("wrong")
    with pytest.raises(VaultError):
        Vault.unlock(p, "wrong")


def test_unlock_guard(tmp_path):
    p = tmp_path / "vault.json"
    Vault.create(p, "CorrectHorse9", {}, n=FAST_N)
    g = UnlockGuard(tmp_path / "guard.json", max_attempts=2, base_s=60)
    for _ in range(2):
        with pytest.raises(VaultError):
            Vault.unlock(p, "bad", g)
    with pytest.raises(VaultLocked):
        Vault.unlock(p, "CorrectHorse9", g)


# ----------------------------------------------------------------- access control
ACCESS = {"otp_ttl_s": 60, "max_attempts": 3, "lockout_base_s": 300, "request_cooldown_s": 0,
          "global_requests_per_min": 100, "enrol_code_ttl_min": 30}


@pytest.fixture
def access(tmp_path):
    v = Vault.create(tmp_path / "v.json", "CorrectHorse9", {"users": {}}, n=FAST_N)
    am = AccessManager(v, dict(ACCESS))
    code = am.create_user("alice")
    assert am.link_telegram("alice", code, "1001")
    assert am.complete_setup("alice", code, "246810")[0]
    return am


def test_enrolment_code_single_use(access):
    assert not access.complete_setup("alice", "WHATEVER", "135792")[0]


def test_pin_rules():
    assert pin_problem("111111") and pin_problem("123456") and pin_problem("12345")
    assert pin_problem("246810") is None


def test_otp_success_and_single_use(access):
    out = access.request_otp("alice")
    assert out.deliver_to == "1001" and len(out.cipher) == 6
    assert access.verify(out.request_id, out.cipher, "246810").ok
    assert not access.verify(out.request_id, out.cipher, "246810").ok   # cannot reuse


def test_intercepted_code_without_pin_fails(access):
    out = access.request_otp("alice")
    r = access.verify(out.request_id, out.cipher, "000000")   # attacker typed the intercepted code
    assert not r.ok and r.attempts_left == 2


def test_lockout_after_failures(access):
    for _ in range(3):
        out = access.request_otp("alice")
        r = access.verify(out.request_id, "000000", "111112")
    assert r.locked and access.is_locked("alice")
    out = access.request_otp("alice")
    assert out.deliver_to is None and out.reason == "locked"


def test_unknown_user_looks_identical(access):
    real = access.request_otp("alice")
    fake = access.request_otp("nobody")
    assert set(vars(real)) == set(vars(fake)) and fake.deliver_to is None
    assert not access.verify(fake.request_id, "123456", "246810").ok


def test_expiry(access):
    out = access.request_otp("alice")
    access._requests[out.request_id].expires = time.time() - 1
    r = access.verify(out.request_id, out.cipher, "246810")
    assert not r.ok and r.expired


def test_rerequest_within_cooldown_keeps_the_code_valid(tmp_path):
    v = Vault.create(tmp_path / "v.json", "CorrectHorse9", {"users": {}}, n=FAST_N)
    am = AccessManager(v, dict(ACCESS, request_cooldown_s=30))
    code = am.create_user("carol")
    am.link_telegram("carol", code, "1003")
    am.complete_setup("carol", code, "246810")
    first = am.request_otp("carol")
    again = am.request_otp("carol")          # user pressed Back and asked again within 30 s
    assert again.deliver_to is None and again.request_id == first.request_id and again.ref == first.ref
    assert am.verify(again.request_id, first.cipher, "246810").ok   # the code on the phone still works


def test_failure_reason_distinguishes_pin_from_code(access):
    out = access.request_otp("alice")
    assert access.verify(out.request_id, out.cipher, "135792").reason == "bad_pin"
    out = access.request_otp("alice")
    assert access.verify(out.request_id, "000000", "246810").reason == "bad_code"


def test_reset_pin_keeps_telegram(access):
    code = access.reset_pin("alice")
    assert access.vault.users["alice"]["chat_id"] == "1001"
    assert access.request_otp("alice").deliver_to is None          # pending until new PIN is set
    assert access.complete_setup("alice", code, "802468")[0]
    out = access.request_otp("alice")
    assert access.verify(out.request_id, out.cipher, "802468").ok
