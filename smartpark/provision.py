"""Device provisioning: create the gateway vault and RSA identities, exchange public keys."""

from __future__ import annotations

import getpass
import shutil
import time
from pathlib import Path

from .config import data_path
from .security.channel import DeviceIdentity
from .security.crypto import rsa_generate, rsa_private_pem, rsa_public_pem
from .security.vault import Vault


def password_problem(pw: str) -> str | None:
    if len(pw) < 10:
        return "use at least 10 characters"
    if pw.lower() == pw or pw.upper() == pw or not any(c.isdigit() for c in pw):
        return "mix upper case, lower case and digits"
    return None


def create_gateway_vault(cfg: dict, password: str, secrets: dict, users: dict | None = None,
                         overwrite: bool = False, scrypt_n: int | None = None) -> Path:
    vault_path = data_path(cfg, "vault.json")
    if vault_path.exists() and not overwrite:
        raise FileExistsError(f"{vault_path} already exists (use --force to replace it)")
    key = rsa_generate()
    data = {"created": time.time(), "secrets": secrets, "users": users or {},
            "gateway_private_key": rsa_private_pem(key).decode()}
    Vault.create(vault_path, password, data, n=scrypt_n)
    keys_dir = data_path(cfg, "keys", "x").parent
    (keys_dir / "trusted").mkdir(parents=True, exist_ok=True)
    (keys_dir / "gateway_public.pem").write_bytes(rsa_public_pem(key))
    return keys_dir / "gateway_public.pem"


def init_gateway_interactive(cfg: dict, force: bool = False):
    print("=== SmartPark gateway setup ===")
    print("The admin password encrypts every secret on this device. It cannot be recovered.\n")
    while True:
        pw = getpass.getpass("New admin password: ")
        problem = password_problem(pw)
        if problem:
            print(f"  Weak password: {problem}")
            continue
        if getpass.getpass("Repeat password: ") != pw:
            print("  Passwords do not match")
            continue
        break
    secrets = {
        "telegram_token": getpass.getpass("Telegram bot token (hidden, blank = console only): ").strip(),
        "admin_chat_id": input("Admin Telegram chat id: ").strip(),
        "thingspeak_write_key": getpass.getpass("ThingSpeak write API key (hidden, blank = disabled): ").strip(),
        "thingspeak_channel_id": input("ThingSpeak channel id (for offline bulk upload): ").strip(),
    }
    pub = create_gateway_vault(cfg, pw, secrets, overwrite=force)
    print(f"\nVault created: {data_path(cfg, 'vault.json')}")
    print(f"Gateway public key: {pub}")
    print("Copy this public key to every bay:  python -m smartpark init-bay --config <bay.toml> "
          f"--gateway-key {pub.name}")
    print("Then copy each bay's public key back here:  python -m smartpark trust --config <gateway.toml> "
          "--peer bay1 --key bay1_public.pem")


def init_bay(cfg: dict, gateway_key: str | None = None) -> Path:
    keys_dir = data_path(cfg, "keys", "x").parent
    ident = DeviceIdentity.load_or_create(cfg["device_id"], keys_dir)
    if gateway_key:
        ident.trust("gateway", Path(gateway_key).read_bytes())
    pub = keys_dir / f"{cfg['device_id']}_public.pem"
    print(f"Bay identity ready: {cfg['device_id']} (fingerprint {ident.fingerprint()})")
    print(f"Public key: {pub}")
    if ident.trusted_key("gateway") is None:
        print("WARNING: gateway public key not trusted yet - rerun with --gateway-key gateway_public.pem")
    return pub


def trust_peer(cfg: dict, peer: str, key_path: str):
    keys_dir = data_path(cfg, "keys", "x").parent
    ident = DeviceIdentity(cfg["device_id"], keys_dir)
    ident.trust(peer, Path(key_path).read_bytes())
    print(f"{cfg['device_id']} now trusts {peer}")


def copy_key(src: Path, dst_dir: Path, name: str):
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dst_dir / name)
