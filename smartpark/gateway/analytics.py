"""Event store + data interpretation (Activity 1 'accurate data interpretation').

Every bay event is stored in SQLite on the gateway. From it we compute the
numbers the report needs: occupancy %, turnover, dwell time, peak hour,
false alarms avoided by the ML classifier, unauthorised parking, etc.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime


class EventStore:
    def __init__(self, path):
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self._db.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, bay TEXT, kind TEXT, changed INTEGER,
            occupied INTEGER, conf REAL, source TEXT, dwell_s REAL, authorised INTEGER, user TEXT,
            infer_ms REAL)""")
        self._db.execute("""CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, text TEXT)""")
        self._db.execute("CREATE INDEX IF NOT EXISTS ev_ts ON events(ts)")
        self._db.commit()

    def add_event(self, bay: str, ev: dict):
        def b(v):
            return None if v is None else int(bool(v))
        with self._lock:
            self._db.execute(
                "INSERT INTO events(ts,bay,kind,changed,occupied,conf,source,dwell_s,authorised,user,infer_ms) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ev.get("ts", time.time()), bay, ev.get("kind"), b(ev.get("state_changed")), b(ev.get("occupied")),
                 ev.get("confidence"), ev.get("source"), ev.get("dwell_s"), b(ev.get("authorised")),
                 ev.get("user"), ev.get("infer_ms")))
            self._db.commit()

    def add_alert(self, level: str, text: str):
        with self._lock:
            self._db.execute("INSERT INTO alerts(ts,level,text) VALUES (?,?,?)", (time.time(), level, text))
            self._db.commit()

    def recent_alerts(self, n: int = 10) -> list[tuple]:
        with self._lock:
            return self._db.execute("SELECT ts, level, text FROM alerts ORDER BY id DESC LIMIT ?", (n,)).fetchall()

    def events_since(self, since: float) -> list[dict]:
        with self._lock:
            cur = self._db.execute("SELECT ts,bay,kind,changed,occupied,conf,source,dwell_s,authorised,user,infer_ms "
                                   "FROM events WHERE ts >= ? ORDER BY ts", (since,))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    def report(self, bays: list[str], since: float | None = None, now: float | None = None) -> dict:
        now = now or time.time()
        since = since or datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        evs = self.events_since(since)
        span = max(1.0, now - since)
        changes = [e for e in evs if e["changed"]]
        arrivals = [e for e in changes if e["occupied"] == 1]
        departures = [e for e in changes if e["occupied"] == 0]
        dwell = [e["dwell_s"] for e in departures if e["dwell_s"]]

        # time-weighted occupancy per bay from state transitions
        occ_pct = {}
        for bay in bays:
            t_prev, occupied, busy = since, None, 0.0
            for e in (x for x in changes if x["bay"] == bay):
                if occupied is None:
                    occupied = not bool(e["occupied"])  # state before first transition
                if occupied:
                    busy += e["ts"] - t_prev
                t_prev, occupied = e["ts"], bool(e["occupied"])
            if occupied:
                busy += now - t_prev
            occ_pct[bay] = round(100 * busy / span, 1)

        hours = Counter(datetime.fromtimestamp(e["ts"]).hour for e in arrivals)
        per_bay = defaultdict(int)
        for e in arrivals:
            per_bay[e["bay"]] += 1
        infer = [e["infer_ms"] for e in evs if e["infer_ms"] is not None]
        return {
            "since": since,
            "arrivals": len(arrivals),
            "departures": len(departures),
            "avg_dwell_min": round(sum(dwell) / len(dwell) / 60, 1) if dwell else None,
            "max_dwell_min": round(max(dwell) / 60, 1) if dwell else None,
            "occupancy_pct": occ_pct,
            "avg_occupancy_pct": round(sum(occ_pct.values()) / len(occ_pct), 1) if occ_pct else 0.0,
            "turnover_per_bay": {b: per_bay.get(b, 0) for b in bays},
            "peak_hour": (f"{hours.most_common(1)[0][0]:02d}:00" if hours else None),
            "unauthorised": sum(1 for e in arrivals if e["authorised"] == 0),
            "transients_ignored": sum(1 for e in evs if e["kind"] == "transient" and not e["changed"]),
            "noise_ignored": sum(1 for e in evs if e["kind"] == "noise" and not e["changed"]),
            "reconciled": sum(1 for e in changes if e["source"] == "reconciled"),
            "avg_infer_ms": round(sum(infer) / len(infer), 3) if infer else None,
        }
