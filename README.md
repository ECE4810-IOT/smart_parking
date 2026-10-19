# SmartPark: secure multi-node smart parking (ECE4810 project)

A Raspberry Pi system with one gateway and four bay nodes:

- Each bay detects cars with an ultrasonic sensor, uses ML to classify what happened (car arrived, car left, something passed by, sensor noise) and shows its state on traffic-light LEDs.
- Drivers sign in at a touch-screen kiosk with a username, an encrypted one-time password (OTP) sent by Telegram, and a PIN.
- Every link between devices is authenticated and encrypted.
- Telegram is used for alerts and admin commands. ThingSpeak is used for analytics.

```
                     Telegram (alerts, commands, OTP)        ThingSpeak (1 channel, 8 fields)
                                ^                                       ^
                                |  HTTPS                                |  HTTPS
 +------------------------------+---------------------------------------+-----------+
 |  GATEWAY  (Pi 1, fog node)   Touch Display 2 kiosk  |  barrier (servo/LED)        |
 |  vault (AES-256-GCM, unlocked by admin password)  |  OTP + PIN access control   |
 |  permit tickets (RSA-signed)  |  analytics (SQLite)  |  health monitor            |
 +-----^-------------^-------------^-------------^------------------------------------+
       | X25519 + RSA handshake, then AES-256-GCM envelopes with anti-replay (LAN / Tailscale)
 +-----+----+  +-----+----+  +-----+----+  +-----+----+
 |  BAY 1   |  |  BAY 2   |  |  BAY 3   |  |  BAY 4   |   edge nodes (Pis 2-5)
 | HC-SR04  |  | HC-SR04  |  | HC-SR04  |  | HC-SR04  |   adaptive 2 Hz -> 10 Hz sampling
 | ML event |  | ML event |  | ML event |  | ML event |   edge inference
 | G/Y/R LED|  | G/Y/R LED|  | G/Y/R LED|  | G/Y/R LED|
 +----------+  +----------+  +----------+  +----------+
```

## Contents

1. [How it works](#how-it-works)
2. [Wiring](#wiring)
3. [Installation](#installation)
4. [First-time setup (keys, vault, users)](#first-time-setup)
5. [Running](#running)
6. [Telegram commands](#telegram-commands)
7. [Machine learning: collecting data and training](#machine-learning)
8. [Simulator (no hardware needed)](#simulator)
9. [MATLAB dashboard](#matlab-dashboard)
10. [Evidence for the report](#evidence-for-the-report)
11. [Security design](#security-design)
12. [Troubleshooting](#troubleshooting)

---

## How it works

**A driver arrives**

1. They type their username on the kiosk.
2. If the username is registered, the gateway generates a random 6-digit OTP and sends its *encrypted* form C to the user's Telegram. The encryption is a digit-wise Vigenère cipher: C = (OTP + PIN) mod 10.
3. The driver types C and their PIN. The kiosk decrypts the code and checks it.
4. If it matches, the barrier opens. The gateway picks a free bay and sends that bay an RSA-signed permit ticket, and the bay's LED turns **yellow**.
5. The kiosk tells the driver which bay to go to.

**At the bay**

- The car parks, and the sensor sees the distance drop.
- The bay node captures a 4-second window of readings and classifies it as *arrival*.
- Because the bay holds a valid ticket, it turns **red** (occupied), and the user and admin get a Telegram message.
- If a car parks **without** a ticket, the LED flashes red and the admin gets an **unauthorised vehicle** alert.
- A person or object passing through is classified as *transient*. It is reported as movement but **never** counted as a car. At night (closed hours) it triggers a security alert.

**After that**

- The gateway tracks the permit:
  - no-show release after 10 min
  - a reminder before the permit expires
  - an overstay alert after it expires
  - `/extend` from Telegram adds time
- When the car leaves, the time it stayed is recorded and the bay turns green again.

**In the background**

- Heartbeats every 10 s detect offline bays.
- Sensor self-diagnosis detects wiring faults.
- If the internet drops, alerts and ThingSpeak data are queued and sent when it returns.
- The gateway sends one ThingSpeak update containing all bays every 20 s.

## Wiring

All pin numbers are **physical (BOARD)** numbers, the same scheme as Lab 1.

**Bay nodes (Pis 2–5)**

| Part | Pin | Notes |
|---|---|---|
| HC-SR04 VCC | 2 (5 V) | |
| HC-SR04 GND | 6 | |
| HC-SR04 TRIG | 7 | |
| HC-SR04 ECHO | 11 | **through the 1 kΩ / 2 kΩ divider** (echo is 5 V, GPIO is 3.3 V) |
| Green LED | 13 (GPIO27) | 330 Ω to GND (pin 14) |
| Yellow LED | 15 (GPIO22) | 330 Ω to GND |
| Red LED | 16 (GPIO23) | 330 Ω to GND |

**Gateway (Pi 1)**

| Part | Pin | Notes |
|---|---|---|
| Touch Display 2 | DSI connector + 5 V/GND | follow the display's guide |
| Barrier LED | 18 (GPIO24) | 330 Ω to GND (pin 20). Lights while the barrier is open |
| SG90 servo signal (optional) | 12 (GPIO18) | servo 5 V = pin 4, GND = pin 9. Set `barrier.mode = "servo"` |

## Installation

Do this on **every Pi**. The project folder should be `~/smart_parking`.

```bash
cd ~/smart_parking
bash setup.sh gateway   # on the gateway Pi
bash setup.sh bay       # on each bay Pi
```

`setup.sh` does the following:

- installs the apt packages (`python3-cryptography`, `python3-rpi.gpio`, `python3-sklearn`, …)
- creates `.venv` so that it can see those apt packages (avoids pip errors on Raspberry Pi OS)
- copies the example config
- installs a systemd service
- on the gateway, sets Chromium to open the kiosk full-screen on the touch display

Then **edit the config**:

- `config/gateway.toml`: set the `[bays]` addresses. Tailscale names or `100.x.y.z` IPs work, and also work when campus Wi-Fi blocks devices talking to each other.
- `config/bay.toml` on each bay: set `device_id = "bay1"` (bay2, …) and the `gateway` address.

## First-time setup

### 1. Telegram bot (do this first)

- The old bot token from the lab code was in the source and has been shared, so **revoke it**: in @BotFather send `/revoke`. Then create a fresh token.
- Get your own chat id from @getmyid_bot. This becomes the **admin** chat.
- Regenerate the ThingSpeak write key too.

### 2. ThingSpeak channel

Create one channel with 8 fields:

| Field | Content |
|---|---|
| 1–4 | Bay 1–4 status (0 = free, 1 = occupied, 2 = reserved, 3 = unauthorised, −1 = offline/fault) |
| 5 | Free bays |
| 6 | Arrivals + departures per interval |
| 7 | Alerts per interval |
| 8 | Average stay (min) |

### 3. Gateway vault

```bash
.venv/bin/python -m smartpark init-gateway --config config/gateway.toml
```

This asks for:

- **Admin password:** at least 10 characters, mixing upper case, lower case and digits. It cannot be recovered.
- **Bot token**
- **Admin chat id**
- **ThingSpeak key and channel id**

Everything is encrypted into `~/smartpark_data/vault.json`, and the gateway's RSA key pair is created.

### 4. Exchange device keys

Run this from the gateway (needs SSH to the bays):

```bash
bash scripts/exchange_keys.sh user@leanjieberrypi user@chloeberrypi user@damienberrypi user@<4th-bay-pi>   # real hostnames, not bay1..bay4
```

To do it by hand instead:

1. Copy `~/smartpark_data/keys/gateway_public.pem` to the bay.
2. On the bay, run `python -m smartpark init-bay --config config/bay.toml --gateway-key gateway_public.pem`.
3. Copy the bay's `~/smartpark_data/keys/bayN_public.pem` back to the gateway.
4. On the gateway, run `python -m smartpark trust --config config/gateway.toml --peer bayN --key bayN_public.pem`.

### 5. Add users

1. The admin sends `/adduser alice` to the bot, which replies with an 8-character enrolment code. Give it to Alice **in person**.
2. Alice sends `/register alice <code>` to the bot. This links her Telegram.
3. At the kiosk, Alice taps **New user setup**, enters her username and the same code, and chooses a 6-digit PIN. The PIN never travels over Telegram.

## Running

```bash
sudo systemctl start smartpark-gateway        # gateway: kiosk shows "System locked"
sudo systemctl start smartpark-bay            # each bay
journalctl -u smartpark-bay -f                # live logs
```

- **Gateway:** it starts **locked**. Log in on the touch display with the admin password. This is the brief's "login before running the program", and it decrypts the vault.
- **Without the display:** run `python -m smartpark gateway --config config/gateway.toml --terminal` for a terminal kiosk over SSH.

## Telegram commands

**Admin**

| Command | What it does |
|---|---|
| `/menu` | Button panel |
| `/status all` or `/status bay2` | Live bay status |
| `/report` | Today's analytics (see below) |
| `/enable`, `/disable` | Pause or resume monitoring on a bay or all |
| `/threshold bay1 12` | Set the occupied threshold (cm) |
| `/adduser`, `/removeuser`, `/users`, `/unlockuser` | User management |
| `/night on` or `/night off` | Closed-hours security mode |
| `/open` | Open the barrier manually |
| `/alerts` | Last 10 alerts |
| `/lights all` | LED self-test |

`/report` includes: occupancy %, turnover, average and maximum stay, peak hour, unauthorised count, false alarms avoided by ML, classifier corrections, and inference time.

**Users:** `/free`, `/mybay`, `/extend <hours>`, `/help`

**Strangers:** only `/register` works.

## Machine learning

Each bay classifies every threshold crossing as `arrival`, `departure`, `transient` or `noise`.

- Until a trained model exists, a rule-based classifier is used. It is also the **baseline** that the ML models are compared against.
- Each window yields 15 features: start and end distance, spread, time below the threshold, number of crossings, trend, jitter, dropouts, and others.

**1. Collect labelled data** on a bay (stop the service first):

```bash
sudo systemctl stop smartpark-bay
.venv/bin/python -m smartpark bay --config config/bay.toml --record arrival
#   drive the model car in ~40 times at different speeds, Ctrl+C
.venv/bin/python -m smartpark bay --config config/bay.toml --record departure
.venv/bin/python -m smartpark bay --config config/bay.toml --record transient  # hand / person / object passing
.venv/bin/python -m smartpark bay --config config/bay.toml --record noise      # tap, cloth, angled objects
```

- Windows are appended to `~/smartpark_data/dataset/windows.csv`.
- Collect on **several bays**, and the trainer will also report leave-one-bay-out accuracy.

**2. Train and compare** (on a Pi, so the scikit-learn version matches the bays):

```bash
.venv/bin/python -m smartpark train --data ~/smartpark_data/dataset/windows.csv --out ~/smartpark_data/models
```

This trains and compares k-NN, a decision tree, a random forest and an SVM. Each uses a 5-fold cross-validated grid search, is scored on a held-out test set, and is compared with the rule baseline. The trainer then:

- runs permutation feature importance and retrains the best model on the top 6 features
- tests a compact depth-4 tree
- picks the deployed model by F1 score first, then speed

It writes these files to `~/smartpark_data/models/`:

- `report.md`: comparison table, best settings, feature importance, confusion matrix
- `confusion_matrix.png`
- `event_model.joblib`: copy this to every bay's `~/smartpark_data/models/` and restart the bays

**3. Measure efficiency on the Pi:**

```bash
.venv/bin/python -m smartpark benchmark --model ~/smartpark_data/models/event_model.joblib --data ~/smartpark_data/dataset/windows.csv
.venv/bin/python -m smartpark bench-crypto
```

**Pipeline test only:** `python -m smartpark synth` writes a **SYNTHETIC** dataset. `train` refuses it unless you pass `--allow-synthetic`, and the report is stamped as synthetic. **Never report those numbers.**

## Simulator

The whole 5-Pi system runs on one laptop:

- **Real parts:** the encryption, handshakes, tickets, OTP, analytics and kiosk.
- **Mocked parts:** the sensors, LEDs and servo. Telegram is printed to the console.

```bash
python -m smartpark simulate            # kiosk at http://127.0.0.1:8080 (password DemoAdmin2026)
sim> demo                               # scripted scenario covering every feature
sim> access alice                       # full OTP flow (alice PIN 246810, bob PIN 135792)
sim> arrive 2 | leave 2 | walk 3 | noise 1 | fault 4 | fix 4 | offline 1 | online 1
sim> tg /status all                     # Telegram as the admin
sim> tgu alice /mybay                   # Telegram as a user
```

- `python -m smartpark simulate --script` runs the demo and exits. Record it as a **backup demo video**.
- `--real-telegram` with `SMARTPARK_TELEGRAM_TOKEN` and `SMARTPARK_ADMIN_CHAT` set sends to a real bot.

**Tests:** `python -m pytest -q` (31 tests covering cryptography, the channel, the vault, OTP, the occupancy logic and a full integration run).

## MATLAB dashboard

`dashboard/ParkingDashboard_v4.m` reads the gateway's ThingSpeak channel and shows:

- free bays, average occupancy, visits, average stay, alerts and whether the gateway is live
- a status lamp and statistics for each bay
- charts: occupied vs free over time, a status timeline per bay, arrivals by hour (peak hour), events and alerts

Run `ParkingDashboard_v4` in MATLAB, enter the channel ID and read key, and press **Connect**. They are remembered on that computer and never stored in the file. **Demo data** works with no channel at all. **Export for report** saves a CSV plus PNGs of the dashboard and each chart.

## Evidence for the report

Every run writes CSV files to `~/smartpark_data/metrics/`:

| File | Contents | Rubric item |
|---|---|---|
| `telegram.csv` | Delay of every Telegram message (event → sent) | A1 efficient Telegram |
| `sampling.csv` | Sampling rate and CPU % per bay, idle vs active | A1 efficiency, A2/A3 energy and net zero |
| `events.csv` | Every classified event: label, confidence, feature/inference ms | A1 data interpretation, A2 speed |
| `auth.csv` | OTP request → delivered → verified timings and outcomes | A1b, A3 performance |
| `crypto.csv` | Time of every cryptographic operation | A3 optimised performance |
| `thingspeak.csv` | Upload success and delay | A1, offline resilience |

Other sources:

- `/report` and `events.db`: occupancy, turnover, stay times, peak hour, unauthorised parking, false alarms avoided (A1 data interpretation).
- `models/report.md` and `confusion_matrix.png`: model comparison, optimisation, baseline comparison (A2).
- `bench-crypto` and `benchmark` output: tables for A2 and A3.

## Security design

| Threat | Mitigation |
|---|---|
| Someone intercepts the Telegram OTP | Only C = OTP + PIN (mod 10) is sent. A fresh random OTP makes C reveal nothing (one-time pad). The kiosk also needs the PIN. |
| Brute-forcing OTP or PIN | 3 attempts per code. The user is locked out with exponential back-off, and both the admin and the user are alerted. |
| Discovering valid usernames | The kiosk gives an identical response for unknown, locked and valid users. The admin is alerted after 3 unknown usernames in 5 min. |
| Telegram spam or denial of service | Per-user cooldown and a global request rate limit |
| Stolen SD card | All secrets are in an AES-256-GCM vault. The key is derived from the admin password with scrypt (memory-hard). |
| Admin password guessing | Lockout persisted to disk with exponential back-off. Kiosk admin sessions expire after 5 min. |
| Fake bay or fake gateway (man-in-the-middle) | RSA-2048 signed X25519 handshake. Only trusted device keys are accepted. |
| Sniffing or tampering with LAN traffic | AES-256-GCM on every message, with the header authenticated as associated data |
| Replaying captured packets | Timestamps, an anti-replay window on sequence numbers, and single-use handshake nonces |
| Forged "unlock" commands | Permits are RSA-signed tickets bound to a bay with an expiry (Kerberos-style). A compromised bay cannot forge tickets. |
| Old traffic decrypted after a key leak | A fresh Diffie-Hellman session key every boot (forward secrecy) |
| Kiosk abused from another web page | Server bound to localhost, per-boot request token, CSP and X-Frame-Options headers |
| Commands from strangers on Telegram | Role-based access by chat id |

Standard cryptography comes from `cryptography`, `hashlib`, `hmac` and `secrets`. The only hand-written cipher is the digit-wise Vigenère OTP layer (Lecture 8c).

## Troubleshooting

- **Bay log shows "gateway not listening yet" / connection refused:** expected while the gateway is **locked**. Its port only opens after the admin logs in on the kiosk. The bay retries every 10 s and connects within seconds of the login.
- **"Sensor fault" alerts:** stop the bay service and run `python -m smartpark sensor-test --config config/bay.toml`. It prints 40 readings and says *why* pings fail: no echo (wiring/power), echo too long (nothing in range, or a soft/angled target), echo stuck high (clone module or divider). Fault alerts are limited to one per bay per 10 minutes.
- **Kiosk buttons do nothing / "Gateway restarted - reloading":** the page was loaded before the gateway restarted, so its security token was stale. The kiosk now detects this and reloads itself. Every failure now shows a message instead of failing silently.
- **Keyboard choice:** `[kiosk] keyboard = "builtin"` (default) uses the kiosk's own keyboard. `keyboard = "system"` uses the Raspberry Pi OS on-screen keyboard (wvkbd/squeekboard) instead. In system mode the kiosk browser is started with Wayland input support and as a maximised app window instead of `--kiosk` full screen, because labwc draws the on-screen keyboard *underneath* fullscreen windows (`setup.sh` sets the Raspberry Pi taskbar to auto-hide, `autohide=true` in `~/.config/wf-panel-pi/wf-panel-pi.ini`; touch the top edge to bring it back). `setup.sh` adds a labwc window rule (`~/.config/labwc/rc.xml`, app id `chrome-127.0.0.1_*`) that removes the title bar. If the OS keyboard does not pop up, enable it in Raspberry Pi Configuration → Display → On-screen keyboard. Restart the gateway and the kiosk browser after changing this.
- **The OS keyboard pops up over the kiosk:** the kiosk has its own keyboard and tells the browser not to show one. If Raspberry Pi OS still shows its keyboard, turn it off in Raspberry Pi Configuration → Display → On-screen keyboard (or `sudo raspi-config` → Display Options).

- **Bays never connect / "stale hello":** the clocks are out of sync. The handshake rejects anything more than 60 s off, and the Pi 4 has no real-time clock. Check `timedatectl`. If campus Wi-Fi blocks the time server, use the phone hotspot.
- **"unknown device" in the gateway log:** that bay's public key isn't trusted. Re-run the key exchange.
- **Bays unreachable on campus Wi-Fi:** use the Tailscale names or IPs in `[bays]` and `gateway`.
- **Wrong distances:** check the echo voltage divider. Tune with `/threshold bayN <cm>` and set `temperature_c`.
- **Model won't load on a bay:** train on a Pi (same scikit-learn version) or delete the model to use the rules.
