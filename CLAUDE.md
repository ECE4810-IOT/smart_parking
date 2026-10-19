# SmartPark - handoff notes for Claude Code

Context for a Claude session running on the Raspberry Pis. Read `context.md` for the background, design
rationale and history, and README.md for full setup/usage.

## Project
ECE4810 (Monash) IoT project, due 21 Oct 2026. Secure multi-node smart parking system:
- **Gateway** = `rafberrypi` (user `berry`): fog node, Touch Display 2 kiosk (Chromium -> http://127.0.0.1:8080),
  barrier LED (servo mocked), Telegram bot, ThingSpeak uploader, encrypted vault.
  Service: `smartpark-gateway`. Starts LOCKED until admin logs in on the kiosk (vault password).
- **Bays** = bay1 `leanjieberrypi` (user `lean-jie-chang`), others: `chloeberrypi`, `damienberrypi`, + one more.
  HC-SR04 on BOARD pins 7/11, LEDs 13/15/16. Service: `smartpark-bay`.
- Code: `~/smart_parking` (git, origin github.com/ECE4810-IOT/smart_parking). Runtime data: `~/smartpark_data`
  (vault, keys, events.db, metrics/*.csv, logs). Python venv: `.venv` (uses apt packages).
- Tests: `.venv/bin/python -m pytest -q` (41 tests). Simulator: `python -m smartpark simulate`.

## Design decisions (keep)
- device_id must be `gateway` / `bay1..bay4`; Tailscale hostnames go in the *address* fields only.
- Crypto via libraries (AES-GCM vault via scrypt, X25519 + RSA-signed handshake, anti-replay envelopes,
  RSA-signed Kerberos-style permit tickets). Own code only for the digit-Vigenere OTP: C = (OTP + PIN) mod 10.
- ThingSpeak: one channel, 8 fields, gateway only (free tier = 4 channels).
- MATLAB dashboard lives on the Windows laptop (dashboard/ParkingDashboard_v4.m), not on the Pis.

## Issues already found and fixed (don't reintroduce)
- Telegram `_call()` HTTP timeout kwarg clashed with getUpdates `timeout` -> renamed to `http_timeout`.
- Sensor fault flapping -> fault after 10 bad reads AND >= 3 s with no valid reading (a car moving in loses ~1 s of echoes at 10 Hz), recover after 5 good; echo-low wait; timeout 0.04 s;
  gateway rate-limits fault alerts (1 per bay per 10 min).
- Kiosk: stale per-boot token after gateway restart made buttons silently fail -> page auto-reloads (boot id)
  and every action shows an error message.
- PIN box auto-filled by Chromium password manager (log showed `bad_pin` with correct code) -> masked fields
  are `type=text` + CSS `-webkit-text-security`, digits-only filter, cleared on screen change.
- OTP re-request within cooldown used to hand out a dummy request -> now reuses the pending one; Ref tag shown
  on kiosk + Telegram.
- Auth log reasons (metrics/auth.csv, journal): `bad_pin`, `bad_code`, `pin_not_6_digits`, `expired`, `+locked`.

## Environment gotchas
- Tailscale SSH on some Pis uses "check" mode -> first SSH prints an auth URL and appears to hang.
- Hotspot path had MTU < 1200 on Tailscale; fix if needed: `sudo ip link set dev tailscale0 mtu 1120`.
- `git pull` blocked by chmod on scripts -> `git checkout -- <file>; git config core.fileMode false`.
- Bay config files set `echo_timeout_s` explicitly - must be 0.04.
- Clocks must be within 60 s (handshake), Pi 4 has no RTC.

## Useful commands
- Logs: `journalctl -u smartpark-gateway -f`, `journalctl -u smartpark-bay -f`
- Auth diagnosis: `tail ~/smartpark_data/metrics/auth.csv`
- Sensor wiring check (stop service first): `.venv/bin/python -m smartpark sensor-test --config config/bay.toml`
- Key exchange (on gateway): `bash scripts/exchange_keys.sh user@<bay-hostname>`
- Telegram admin: /status all, /report, /adduser, /resetpin, /unlockuser, /users, /night, /lights all

## Next steps
1. Bring up remaining bays (pull, echo_timeout 0.04, exchange keys, restart) and confirm `/status all`.
2. Full end-to-end run: kiosk unlock -> bay yellow -> park (red, Telegram) -> leave.
3. Record ML training data on each bay (`python -m smartpark bay --config config/bay.toml --record <label>`,
   labels arrival/departure/transient/noise, ~40 each), train on a Pi, copy model to bays.
4. Collect metrics CSVs + `bench-crypto` / `benchmark` output for the report.
