"""Kerberos-style service tickets (Lecture 9c).

Analogy:
    Kerberos KDC/TGS   -> gateway (it authenticated the user with OTP + PIN)
    service            -> a bay node
    service ticket     -> {user, bay, issued, expires, ticket id} signed by the gateway
    lifetime           -> the parking permit duration

The bay verifies the ticket with the gateway's RSA public key - it never needs
the user database or a call back to the gateway, exactly like a Kerberos
service checks a ticket without contacting the KDC. Kerberos uses symmetric
keys; we use RSA signatures so a compromised bay cannot forge tickets for
other bays.
"""

from __future__ import annotations

import secrets
import time

from .crypto import b64d, b64e, canonical, rsa_sign, rsa_verify


class TicketError(Exception):
    pass


def issue_ticket(gateway_private_key, username: str, bay_id: str, lifetime_s: int) -> dict:
    now = int(time.time())
    payload = {"user": username, "bay": bay_id, "iat": now, "exp": now + int(lifetime_s),
               "tid": secrets.token_hex(8), "iss": "gateway"}
    return {"payload": payload, "sig": b64e(rsa_sign(gateway_private_key, canonical(payload)))}


def verify_ticket(gateway_public_key, ticket: dict, bay_id: str, skew_s: int = 60) -> dict:
    try:
        payload, sig = ticket["payload"], b64d(ticket["sig"])
    except (KeyError, TypeError, ValueError) as e:
        raise TicketError("malformed ticket") from e
    if not rsa_verify(gateway_public_key, sig, canonical(payload)):
        raise TicketError("bad signature")
    if payload.get("bay") != bay_id:
        raise TicketError("ticket is for another bay")
    now = time.time()
    if payload.get("iat", 0) > now + skew_s:
        raise TicketError("ticket issued in the future")
    if payload.get("exp", 0) < now:
        raise TicketError("ticket expired")
    return payload
