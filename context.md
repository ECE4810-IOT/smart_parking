# SmartPark - project context

Background, decisions and history behind this repo. `CLAUDE.md` is the short operational handoff;
this file explains **why** things are the way they are. No secrets belong here (the repo is public).

---

## 1. The assignment

ECE4810 *Internet of Things: Communication, Data and Security* (Monash Malaysia), group project, 20 marks.
Submission **21 Oct 2026**; demo in week 12 (about 1 min summary + 4 min live demo + Q&A).

The brief asks for a smart system that **detects presence and authenticates people** entering a protected
premise, built from ultrasonic sensors, with:

| Activity | Requirement (brief) | Marks |
|---|---|---|
| 1 | Ultrasonic presence detection -> Telegram alert; username -> password generated and sent to Telegram -> entered to gain access; minimal sensors | 2 |
| 2 | ML that classifies movement of a person/object; fast, efficient inference with evidence; sustainability/societal considerations | 2 |
| 3 | Login before running; encryption/decryption (e.g. encrypted password sent); public health & safety, whole-life cost, net zero, resource/cultural/societal/environmental | 2 |
| 4 | Additional features, justified, "market-ready", "unique" | 4 |
| 5 | Report <= 20 pages: intro, methodology + flowcharts, results, feasibility + cost-benefit analysis, conclusion | 5 |
| 6 | Demonstration | 3 |
| 7-8 | Individual reflective summary (contribution, communication) | 2 |

Rubric top bands repeatedly ask for **measured evidence** (accuracy, efficiency, optimised performance) and
**market-ready / unique** features - hence the metrics CSVs, benchmarks and analytics built into the system.

## 2. How the idea evolved

1. **Lab work:** per-spot smart parking - one Pi + HC-SR04 per bay, Telegram notifications, ThingSpeak
   channel per spot, MATLAB dashboard (`dashboard/`, earlier versions on the laptop).
2. **Considered and dropped:**
   - *Hand-gesture unlock* with ultrasonic sensors - judged gimmicky and unreliable.
   - *Two-sensor entry-gate* (direction/tailgating) - sound, but too big a rebuild before the deadline.
3. **Chosen:** keep the lab's per-bay system and extend it into a **secure, multi-node, reserved-bay car park**:
   kiosk sign-in with username + encrypted Telegram OTP + PIN, permits issued to specific bays, ML event
   classification on each bay, encrypted gateway <-> bay links.

## 3. Architecture (fog computing, Lecture 5a)

```
Cloud : Telegram (alerts, admin/user commands, OTP)   ThingSpeak (1 channel, 8 fields)
Fog   : Gateway Pi  - kiosk (Touch Display 2), vault, OTP/PIN access control, permits, analytics, barrier
Edge  : 4 x Bay Pi  - HC-SR04, adaptive sampling 2->10 Hz, ML event classifier, traffic-light LEDs
```

Hardware: 5 x Raspberry Pi 4B, Raspberry Pi Touch Display 2 (7", SC1635) on the gateway, HC-SR04 + green/
yellow/red LEDs per bay, LED as barrier stand-in (SG90 servo supported in code, "mock" until bought).

Team devices (Tailscale tailnet + campus Wi-Fi, phone hotspot as backup):
gateway `rafberrypi`; bays `leanjieberrypi` (bay1), `chloeberrypi`, `damienberrypi`, + one more.

## 4. Design decisions and rationale

**Sensing / occupancy**
- Hysteresis (occupied < 10 cm, vacant > 12 cm, scale model) + time debounce -> no flicker.
- Adaptive sampling: 2 Hz idle, 10 Hz when distance changes -> CPU/energy saving (net-zero argument).
- Every threshold crossing captures a 4 s window that is classified as arrival / departure / transient / noise.
  Only confident arrival/departure changes state; transients are reported as movement (night-mode alerts)
  but never counted as a car. A "reconciliation" safety net corrects missed changes after 6 s.

**ML (Activity 2)**
- 15 hand-crafted features per window; models compared: k-NN, decision tree, random forest, SVM (grid search,
  stratified CV, held-out test) **vs a rule-based baseline** (justifies using ML at all).
- Optimisation: permutation importance -> top-6-feature model, compact depth-4 tree; deployed model chosen on
  F1 then latency. Leave-one-bay-out test for generalisation. `benchmark` measures latency/memory on the Pi.
- A synthetic generator exists for pipeline testing only - its numbers must never be reported.

**Security (Activity 3)** - "use vetted libraries, engineer the design":
- Admin password -> scrypt -> AES-256-GCM key for the vault (secrets, users, gateway RSA key). Wrong password
  cannot decrypt anything. This is the brief's "login before running the program".
- OTP: random 6 digits, only an HMAC stored, 60 s, single use. Telegram receives C = (OTP + PIN) mod 10 per
  digit (Vigenere, Lecture 8c; perfect secrecy because OTP is fresh and uniform). Kiosk decrypts with the PIN.
- Gateway <-> bay: X25519 Diffie-Hellman (9a) signed with RSA-2048 (9b), HKDF keys per direction,
  AES-GCM envelopes with timestamp + anti-replay window.
- Permits = RSA-signed Kerberos-style tickets (9c) bound to one bay with an expiry.
- Lockout with exponential back-off, username-enumeration resistance, rate limits, role-based Telegram commands.

**Additional features (Activity 4)**: reserved bays + bay guidance (yellow LED), unauthorised-vehicle alert,
no-show release, overstay reminder/alert, `/extend`, car-park-full alert, sensor self-diagnosis, offline queues
(Telegram + ThingSpeak bulk replay), night security mode, inline Telegram buttons, systemd auto-restart,
touch kiosk with admin panel, `/report` analytics, MATLAB dashboard v4 with CSV/PNG export.

**ThingSpeak**: single gateway channel (free tier limit 4 channels): fields 1-4 bay status
(0 free, 1 occupied, 2 reserved, 3 unauthorised, -1 offline/fault), 5 free bays, 6 events, 7 alerts, 8 avg stay.

## 5. Deployment history (what went wrong and how it was fixed)

| Problem seen | Root cause | Fix |
|---|---|---|
| SSH to a Pi timed out on hotspot | Tailscale path MTU < 1200 bytes; also Tailscale SSH "check" prompt hidden by VS Code | MTU 1120 on tailscale0 if needed; `remote.SSH.showLoginTerminal` |
| `exchange_keys.sh` "Connection closed" | used `bay1` (device id) instead of hostname; script hid ssh errors | script validates and shows errors |
| Telegram commands ignored, log spam | kwarg clash `timeout` in `_call()` | `http_timeout` + unit test with fake Bot API |
| Flood of sensor fault/recovered alerts | flapping + events queued while gateway locked | hysteresis 10/5, queue dedupe, alert rate limit, `sensor-test` |
| Kiosk keyboard unusable | no shift state/symbols, no hide, OS keyboard popping up | rebuilt keyboard (preview bar, shift/caps, ?123, hide), `inputmode=none`; optional `keyboard="system"` (wvkbd) |
| Buttons did nothing after gateway restart | stale per-boot kiosk token; errors swallowed | boot-id auto-reload; visible errors everywhere |
| Correct PIN rejected (`bad_pin` with correct code) | Chromium password manager auto-filled the PIN box | masked fields are text + CSS masking, digits-only, cleared per screen, browser flags |
| `git pull` blocked | `setup.sh` chmod counted as local change | `git config core.fileMode false`; scripts committed executable |

## 6. Current status (19 Oct 2026)

- Gateway running, kiosk working, Telegram commands working, bay1 connected over the secure channel.
- Remaining bays: pull latest, `echo_timeout_s = 0.04`, key exchange, restart.
- Not yet done: real ML data collection + training, full end-to-end demo run on hardware, metrics for report,
  MATLAB dashboard v4 test against the live channel.

## 7. Evidence plan for the report

| Rubric item | Evidence source |
|---|---|
| A1 efficient Telegram / data interpretation | `metrics/telegram.csv`, `/report`, `events.db`, dashboard export |
| A2 accuracy, optimisation, efficiency | `models/report.md`, `confusion_matrix.png`, `benchmark` output, `metrics/events.csv` |
| A3 performance, security | `bench-crypto`, `metrics/crypto.csv`, `metrics/auth.csv`, threat/mitigation table (README) |
| A1/A3 energy, net zero | `metrics/sampling.csv` (idle vs active Hz and CPU %) |
| Cost-benefit (Lecture 10) | hardware cost per gate/bay vs guard hours and unauthorised-parking losses |
