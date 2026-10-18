"""ThingSpeak uploader (runs on the gateway only - fog aggregation).

Instead of every Pi writing to its own channel (5 channels, over the free
4-channel limit, and 4x the messages), the gateway sends ONE update with all
bays every `interval_s`. Field map (configure the channel the same way):

    field1..4  bay1..bay4 status   (0 vacant, 1 occupied, 2 assigned, 3 unauthorised, -1 offline)
    field5     free bays
    field6     events in this interval (arrivals + departures)
    field7     alerts in this interval
    field8     average dwell time of departures in this interval (minutes)

Offline: failed updates are queued with their timestamp and replayed through
the bulk_update API when the connection returns, so the history has no gaps.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable

import requests

from ..util import metric
from .pqueue import PersistentQueue

log = logging.getLogger(__name__)


class ThingSpeakUploader:
    def __init__(self, write_key: str, channel_id: str | None, interval_s: int,
                 snapshot: Callable[[], dict], queue_path, fake: bool = False):
        self.write_key = write_key
        self.channel_id = channel_id
        self.interval = max(15, int(interval_s))  # free tier minimum
        self.snapshot = snapshot
        self.fake = fake
        self._backlog = PersistentQueue(queue_path, "thingspeak")
        self._stop = threading.Event()
        self.last_ok: float | None = None
        self.stats = {"sent": 0, "failed": 0, "replayed": 0}

    def start(self):
        threading.Thread(target=self._loop, name="thingspeak", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self.interval):
            fields = self.snapshot()
            if self.fake:
                log.debug("ThingSpeak (fake) %s", fields)
                self.stats["sent"] += 1
                continue
            t0 = time.perf_counter()
            try:
                r = requests.post("https://api.thingspeak.com/update",
                                  data={"api_key": self.write_key, **fields}, timeout=10)
                if r.status_code != 200 or r.text.strip() == "0":
                    raise RuntimeError(f"rejected ({r.status_code} {r.text[:40]})")
                self.stats["sent"] += 1
                self.last_ok = time.time()
                metric("thingspeak", ok=1, latency_ms=round((time.perf_counter() - t0) * 1000, 1))
                if len(self._backlog):
                    self._replay()
            except Exception as e:
                self.stats["failed"] += 1
                metric("thingspeak", ok=0, latency_ms=round((time.perf_counter() - t0) * 1000, 1))
                log.warning("ThingSpeak update failed (%s) - queued", e)
                stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000")
                self._backlog.push({"created_at": stamp, **fields}, ttl_s=7 * 24 * 3600)

    def _replay(self):
        if not self.channel_id:
            log.warning("ThingSpeak backlog exists but channel_id is not set - cannot bulk upload")
            return
        items = self._backlog.peek(100)
        try:
            r = requests.post(f"https://api.thingspeak.com/channels/{self.channel_id}/bulk_update.json",
                              json={"write_api_key": self.write_key, "updates": [p for _, _, p in items]},
                              timeout=15)
            if r.status_code in (200, 202):
                self._backlog.remove([i for i, _, _ in items])
                self.stats["replayed"] += len(items)
                log.info("Replayed %d queued ThingSpeak updates", len(items))
        except Exception as e:
            log.warning("ThingSpeak bulk replay failed: %s", e)
