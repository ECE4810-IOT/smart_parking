// SmartPark kiosk front-end (Touch Display 2). No external libraries.
"use strict";

const TOKEN = document.querySelector('meta[name="kiosk-token"]').content;
// "builtin" = our own keyboard; "system" = let the OS on-screen keyboard (wvkbd) handle typing
const KB_MODE = (document.querySelector('meta[name="kiosk-keyboard"]') || {}).content === "system" ? "system" : "builtin";
const $ = (id) => document.getElementById(id);
const IDLE_MS = 60000;

let current = null;
let requestId = null;
let requestRef = "";
let timerHandle = null;
let adminToken = null;
let lastActivity = Date.now();
let lastState = null;

// ------------------------------------------------------------------ API
let bootId = null;

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Kiosk-Token": TOKEN },
    body: JSON.stringify(body),
  };
  let r;
  try {
    r = await fetch(path, opts);
  } catch (e) {
    throw new Error("Cannot reach the SmartPark gateway - is it running?");
  }
  let data = {};
  try { data = await r.json(); } catch (e) { /* non-JSON error page */ }
  if (r.status === 403 && data.error === "bad_token") {
    // the gateway restarted since this page loaded -> its security token changed
    location.reload();
    throw new Error("Gateway restarted - reloading the kiosk...");
  }
  if (r.status === 423) { await poll(); throw new Error("System is locked - log in as admin first"); }
  if (!r.ok) throw new Error(data.error ? `Gateway error: ${data.error}` : `Gateway error (HTTP ${r.status})`);
  return data;
}

// run an action and ALWAYS show a visible message if anything fails (never fail silently)
async function guarded(msgId, fn) {
  try {
    await fn();
  } catch (e) {
    console.error(e);
    if (msgId && $(msgId)) setMsg(msgId, e.message || String(e));
    else toast(e.message || String(e));
  }
}

function toast(text) {
  let t = $("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.appendChild(t); }
  t.textContent = text;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 5000);
}

// ------------------------------------------------------------------ screens
function show(name) {
  document.querySelectorAll(".screen").forEach((s) => s.classList.remove("active"));
  $("scr-" + name).classList.add("active");
  current = name;
  hideKb();
  const first = $("scr-" + name).querySelector("input");
  if (first) { setTimeout(() => focusField(first), 50); }
}

function clearInputs(...ids) { ids.forEach((id) => { $(id).value = ""; }); }
function setMsg(id, text, ok = false) { const el = $(id); el.textContent = text || ""; el.classList.toggle("ok", ok); }

function goHome() {
  stopTimer();
  requestId = null;
  clearInputs("username", "code", "pin", "setup-user", "setup-code", "setup-pin", "setup-pin2", "admin-pw", "admin-arg");
  ["code-msg", "setup-msg", "admin-login-msg"].forEach((m) => setMsg(m, ""));
  show(lastState && lastState.locked ? "locked" : "home");
}

function result(kind, title, text) {
  const icon = $("result-icon");
  icon.className = "result-icon " + kind;
  icon.textContent = kind === "ok" ? "✓" : kind === "bad" ? "✕" : "!";
  $("result-title").textContent = title;
  $("result-text").textContent = text;
  show("result");
}

// ------------------------------------------------------------------ state polling
function renderState(s) {
  lastState = s;
  if (s.locked) {
    if (current !== "locked") show("locked");
    if (s.retry_in) setMsg("unlock-msg", `Locked - try again in ${s.retry_in} s`);
    return;
  }
  if (current === "locked" || current === null) show("home");
  $("free-n").textContent = s.free;
  $("total-n").textContent = s.total;
  const map = $("bay-map");
  map.innerHTML = "";
  s.bays.forEach((b) => {
    const d = document.createElement("div");
    d.className = "bay " + b.status;
    const label = { vacant: "free", assigned: "reserved", unauthorised: "unauthorised!" }[b.status] || b.status;
    d.innerHTML = `<div class="name"></div><div class="st"></div>`;
    d.querySelector(".name").textContent = b.label;
    d.querySelector(".st").textContent = label;
    map.appendChild(d);
  });
  const feed = $("feed");
  feed.innerHTML = "";
  (s.feed || []).slice(0, 7).forEach((f) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="t"></span><span></span>`;
    li.children[0].textContent = f.time;
    li.children[1].textContent = f.text;
    li.children[1].className = f.level;
    feed.appendChild(li);
  });
  pill("pill-net", s.telegram_online ? "Online" : "Offline - queuing", s.telegram_online ? "ok" : "bad", true);
  pill("pill-night", "Night mode", "warn", s.night_mode);
  pill("pill-barrier", "Barrier open", "ok", s.barrier_open);
}

function pill(id, text, cls, visible) {
  const p = $(id);
  p.textContent = text;
  p.className = "pill " + cls + (visible ? "" : " hidden");
}

async function poll() {
  try {
    const st = await api("/api/state");
    if (bootId && st.boot && st.boot !== bootId) { location.reload(); return; }   // server restarted
    bootId = st.boot || bootId;
    renderState(st);
  } catch (e) { pill("pill-net", "Kiosk server unreachable", "bad", true); }
}

// ------------------------------------------------------------------ actions
const actions = {
  unlock: () => guarded("unlock-msg", async () => {
    const pw = $("unlock-pw").value;
    if (!pw) return setMsg("unlock-msg", "Enter the admin password");
    setMsg("unlock-msg", "Decrypting vault...", true);
    const r = await api("/api/unlock", { password: pw });
    $("unlock-pw").value = "";
    setMsg("unlock-msg", r.ok ? "" : (r.message || "Login failed"), r.ok);
    if (r.ok) { await poll(); show("home"); }
  }),
  home: goHome,
  "go-username": () => { clearInputs("username"); show("username"); },
  "go-setup": () => show("setup"),
  "go-admin-login": () => show("admin-login"),

  request: () => guarded("username-msg", async () => {
    const u = $("username").value.trim();
    if (u.length < 3) return setMsg("username-msg", "Enter your username (at least 3 characters)");
    setMsg("username-msg", "");
    const r = await api("/api/access/request", { username: u });
    if (r.status === "full") return result("warn", "Car park full", "There are no free bays right now. Please try again later.");
    requestId = r.request_id;
    requestRef = r.ref || "";
    $("code-ref").textContent = requestRef ? `Use the Telegram message marked Ref ${requestRef}` : "";
    clearInputs("code", "pin");
    setMsg("code-msg", "");
    show("code");
    startTimer(r.expires_in || 60);
  }),

  verify: () => guarded("code-msg", async () => {
    const code = $("code").value.trim(), pin = $("pin").value.trim();
    if (code.length !== 6 || pin.length !== 6) return setMsg("code-msg", "Code and PIN must both be 6 digits");
    const r = await api("/api/access/verify", { request_id: requestId, code, pin });
    if (r.ok) {
      stopTimer();
      return result("ok", `Welcome, ${r.user}`, `Proceed to ${r.bay_label} (yellow light). Your permit is valid until ${r.until}.`);
    }
    if (r.no_bay) return result("warn", "No bay available", "Your code was correct but no bay could be reserved. Please contact the admin.");
    if (r.locked) { stopTimer(); return result("bad", "Account locked", "Too many wrong attempts. The admin has been notified."); }
    if (r.expired) { stopTimer(); return result("warn", "Code expired", "Please start again to get a new code."); }
    clearInputs("pin");
    if (r.attempts_left <= 0) { stopTimer(); return result("bad", "Access denied", "Please start again."); }
    setMsg("code-msg", `Incorrect code or PIN - ${r.attempts_left} attempt(s) left. Check the message says Ref ${requestRef}.`);
  }),

  setup: () => guarded("setup-msg", async () => {
    const u = $("setup-user").value.trim(), c = $("setup-code").value.trim();
    const p1 = $("setup-pin").value.trim(), p2 = $("setup-pin2").value.trim();
    if (!u || !c) return setMsg("setup-msg", "Enter your username and the enrolment code");
    if (p1.length !== 6) return setMsg("setup-msg", "The PIN must be 6 digits");
    if (p1 !== p2) return setMsg("setup-msg", "PINs do not match");
    setMsg("setup-msg", "Saving...", true);
    const r = await api("/api/setup", { username: u, code: c, pin: p1 });
    if (r.ok) return result("ok", "Setup complete", "You can now unlock a bay with your username, the Telegram code and your PIN.");
    clearInputs("setup-pin", "setup-pin2");
    setMsg("setup-msg", r.message || "Setup failed");
  }),

  "admin-login": () => guarded("admin-login-msg", async () => {
    const r = await api("/api/admin/login", { password: $("admin-pw").value });
    $("admin-pw").value = "";
    if (!r.ok) return setMsg("admin-login-msg", r.message || "Login failed");
    adminToken = r.token;
    $("admin-out").textContent = "Logged in. Session expires after 5 minutes.";
    show("admin");
  }),
  "admin-logout": () => { adminToken = null; goHome(); },
};

async function adminAction(action) {
  const arg = $("admin-arg").value.trim();
  let r;
  try {
    r = await api("/api/admin/action", { token: adminToken, action, arg });
  } catch (e) {
    if (String(e.message).includes("admin_session_expired")) { adminToken = null; $("admin-out").textContent = ""; return show("admin-login"); }
    $("admin-out").textContent = e.message;
    return;
  }
  const out = $("admin-out");
  if (r.error === "admin_session_expired") { adminToken = null; out.textContent = ""; return show("admin-login"); }
  if (r.users) out.textContent = r.users.length ? r.users.map((u) =>
      `${u.username.padEnd(16)} ${u.status}${u.telegram ? "  telegram" : ""}${u.locked ? "  LOCKED" : ""}`).join("\n") : "No users yet.";
  else if (r.report) out.textContent = formatReport(r.report);
  else if (r.alerts) out.textContent = r.alerts.map((a) =>
      `${new Date(a.ts * 1000).toLocaleTimeString()}  [${a.level}] ${a.text}`).join("\n") || "No alerts.";
  else out.textContent = r.message || JSON.stringify(r);
  if (r.code) out.textContent += `\n\nGive this code to the user in person (valid ${r.ttl_min} min).`;
}

function formatReport(r) {
  const occ = Object.entries(r.occupancy_pct).map(([b, p]) => `${b} ${p}%`).join(", ");
  return [
    `Arrivals ${r.arrivals}   Departures ${r.departures}`,
    `Average occupancy ${r.avg_occupancy_pct}%  (${occ})`,
    `Average stay ${r.avg_dwell_min ?? "-"} min   Peak hour ${r.peak_hour ?? "-"}`,
    `Unauthorised parking ${r.unauthorised}`,
    `False alarms avoided by ML: ${r.transients_ignored} movement, ${r.noise_ignored} noise`,
    `Avg edge inference ${r.avg_infer_ms ?? "-"} ms`,
  ].join("\n");
}

// ------------------------------------------------------------------ OTP countdown
function startTimer(seconds) {
  stopTimer();
  const end = Date.now() + seconds * 1000;
  const tick = () => {
    const left = Math.max(0, Math.round((end - Date.now()) / 1000));
    $("timer-bar").style.width = (100 * left / seconds) + "%";
    $("timer-text").textContent = left ? `Code expires in ${left} s` : "Code expired";
    if (!left) { stopTimer(); if (current === "code") result("warn", "Code expired", "Please start again to get a new code."); }
  };
  tick();
  timerHandle = setInterval(tick, 1000);
}
function stopTimer() { if (timerHandle) clearInterval(timerHandle); timerHandle = null; }

// ------------------------------------------------------------------ on-screen keyboard
// Built-in keyboard so the OS keyboard is never needed on the touch display.
// - preview bar shows what you are typing (password fields masked, with a Show toggle)
// - Shift = one capital letter, double-tap Shift = caps lock (highlighted when active)
// - ?123 switches to digits + symbols (admin passwords can contain symbols)
// - Hide minimises the keyboard; tap any field to bring it back
let kbTarget = null;
let kbLayer = "letters";      // letters | symbols | digits
let shift = "off";            // off | once | lock
let lastShiftTap = 0;
let reveal = false;
const LETTERS = ["1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm"];
const SYMBOLS = ["1234567890", "!@#$%^&*()", "-_=+.,:;/?", "'\"<>[]{}~|\\"];
const DIGITS = ["123", "456", "789"];

function focusField(input) {
  if (KB_MODE === "system") {           // OS keyboard: just focus the field, it does the rest
    input.focus();
    setTimeout(() => input.scrollIntoView({ block: "center", behavior: "smooth" }), 300);
    return;
  }
  kbTarget = input;
  reveal = false;
  kbLayer = input.dataset.kb === "digits" ? "digits" : "letters";
  shift = "off";
  input.focus({ preventScroll: true });
  renderKb();
  setTimeout(() => input.scrollIntoView({ block: "center", behavior: "smooth" }), 30);
}

function renderKb() {
  const kb = $("kb");
  if (!kbTarget) return hideKb();
  kb.innerHTML = "";
  kb.className = "kb " + (kbLayer === "digits" ? "digits" : "text");

  // preview bar: field name + what has been typed
  const bar = document.createElement("div");
  bar.className = "kb-preview";
  const lbl = document.createElement("span");
  lbl.className = "kb-label";
  lbl.textContent = kbTarget.dataset.label || kbTarget.placeholder || "Input";
  const val = document.createElement("span");
  val.className = "kb-value";
  const isPw = kbTarget.type === "password";
  const v = kbTarget.value;
  val.textContent = v ? (isPw && !reveal ? "•".repeat(v.length) : v) : " ";
  bar.append(lbl, val);
  if (isPw) bar.appendChild(key(reveal ? "Mask" : "Show", () => { reveal = !reveal; renderKb(); }, "mini"));
  bar.appendChild(key("Hide ▾", hideKb, "mini"));
  kb.appendChild(bar);
  requestAnimationFrame(() => { val.scrollLeft = val.scrollWidth; });   // always show the end of the text

  const rows = kbLayer === "digits" ? DIGITS : kbLayer === "symbols" ? SYMBOLS : LETTERS;
  const caps = shift !== "off";
  rows.forEach((r) => {
    const row = document.createElement("div");
    row.className = "kr";
    [...r].forEach((ch) => {
      const shown = caps && /[a-z]/.test(ch) ? ch.toUpperCase() : ch;
      row.appendChild(key(shown, () => type(shown)));
    });
    kb.appendChild(row);
  });

  const last = document.createElement("div");
  last.className = "kr";
  if (kbLayer === "digits") {
    last.appendChild(key("⌫", backspace, "wide"));
    last.appendChild(key("0", () => type("0")));
  } else {
    if (kbLayer === "letters") {
      last.appendChild(key(shift === "lock" ? "CAPS" : "⇧ Shift", onShift,
        "wide" + (shift !== "off" ? " active" : "")));
    }
    last.appendChild(key(kbLayer === "letters" ? "?123" : "ABC",
      () => { kbLayer = kbLayer === "letters" ? "symbols" : "letters"; renderKb(); }, "wide"));
    last.appendChild(key("_", () => type("_")));
    last.appendChild(key("space", () => type(" "), "space"));
    last.appendChild(key("⌫", backspace, "wide"));
  }
  last.appendChild(key("Done", enter, "wide go"));
  kb.appendChild(last);
  kb.classList.remove("hidden");
  document.body.classList.add("kb-open");
  document.body.classList.toggle("kb-digits", kbLayer === "digits");
}

function onShift() {
  const now = Date.now();
  if (shift === "lock") shift = "off";
  else if (now - lastShiftTap < 400) shift = "lock";   // double tap = caps lock
  else shift = shift === "once" ? "off" : "once";
  lastShiftTap = now;
  renderKb();
}

function key(label, fn, cls = "") {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = label;
  b.className = cls;
  b.addEventListener("pointerdown", (e) => { e.preventDefault(); e.stopPropagation(); fn(); lastActivity = Date.now(); });
  return b;
}

function type(ch) {
  if (!kbTarget) return;
  const max = kbTarget.maxLength > 0 ? kbTarget.maxLength : 64;
  if (kbTarget.value.length >= max) return;
  kbTarget.value += ch;
  if (shift === "once") shift = "off";
  renderKb();
}

function backspace() { if (kbTarget) { kbTarget.value = kbTarget.value.slice(0, -1); renderKb(); } }

function enter() {
  if (!kbTarget) return hideKb();
  const inputs = [...$("scr-" + current).querySelectorAll("input")];
  const i = inputs.indexOf(kbTarget);
  if (i >= 0 && i < inputs.length - 1) return focusField(inputs[i + 1]);
  hideKb();
  const primary = $("scr-" + current).querySelector(".btn.primary");
  if (primary) primary.click();
}

function hideKb() {
  $("kb").classList.add("hidden");
  document.body.classList.remove("kb-open", "kb-digits");
  if (kbTarget) kbTarget.blur();
  kbTarget = null;
}

// ------------------------------------------------------------------ wiring
document.addEventListener("click", (e) => {
  lastActivity = Date.now();
  const a = e.target.closest("[data-action]");
  if (a && actions[a.dataset.action]) return actions[a.dataset.action]();
  const ad = e.target.closest("[data-admin]");
  if (ad) return adminAction(ad.dataset.admin);
});
// stop the OS on-screen keyboard from popping up on the touch display - we draw our own
document.querySelectorAll("input[data-kb]").forEach((el) => {
  el.setAttribute("inputmode", KB_MODE === "system" ? (el.dataset.kb === "digits" ? "numeric" : "text") : "none");
  el.setAttribute("autocomplete", "off");
  el.setAttribute("autocorrect", "off");
  el.setAttribute("autocapitalize", "off");
  el.setAttribute("spellcheck", "false");
});
document.addEventListener("focusin", (e) => { if (e.target.matches("input[data-kb]") && e.target !== kbTarget) focusField(e.target); });
// tapping a field again after "Hide" brings the keyboard back
document.addEventListener("pointerdown", (e) => { if (KB_MODE === "builtin" && e.target.matches("input[data-kb]") && $("kb").classList.contains("hidden")) focusField(e.target); });
if (KB_MODE === "system") document.body.classList.add("system-kb");
// keep the preview in sync when a physical keyboard is used
document.addEventListener("input", (e) => { if (e.target === kbTarget) renderKb(); });
document.addEventListener("keydown", (e) => {   // physical keyboard still works
  lastActivity = Date.now();
  if (e.key === "Enter" && e.target.matches("input")) { e.preventDefault(); kbTarget = e.target; enter(); }
});

setInterval(() => { $("clock").textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }, 1000);
setInterval(() => {   // return to the home screen when idle (privacy on a shared kiosk)
  if (current && !["home", "locked"].includes(current) && Date.now() - lastActivity > IDLE_MS) { adminToken = null; goHome(); }
}, 5000);
setInterval(poll, 2000);
poll();
