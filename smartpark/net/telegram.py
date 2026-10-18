"""Telegram Bot API client - efficient and resilient.

Improvements over the lab code (Activity 1 "efficient Telegram"):
  * long polling in its own thread (timeout=25 s): commands arrive instantly
    and polling never blocks sensing
  * an outbound queue + sender thread with retry/back-off and HTTP 429 handling
  * coalescing: events that happen within a few seconds become ONE message
  * offline queue: alerts survive an internet outage (time-sensitive messages
    such as OTPs carry a TTL and are dropped instead of delivered late)
  * deleteMessage: OTP messages are removed from the chat once used/expired
  * every send logs its latency to metrics/telegram.csv
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

import requests

from ..util import metric
from .pqueue import PersistentQueue

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


@dataclass
class OutMsg:
    chat_id: str
    text: str
    kind: str = "info"
    ttl_s: float | None = None
    coalesce: bool = False
    reply_markup: dict | None = None
    on_sent: Callable[[int | None], None] | None = None
    created: float = field(default_factory=time.time)
    op: str = "send"            # send | delete | answer
    extra: dict = field(default_factory=dict)


class TelegramClient:
    def __init__(self, token: str, outbox_path, poll: bool = True, poll_timeout_s: int = 25,
                 coalesce_window_s: float = 3.0, on_update: Callable[[dict], None] | None = None):
        self.token = token
        self.poll_enabled = poll
        self.poll_timeout = poll_timeout_s
        self.coalesce_window = coalesce_window_s
        self.on_update = on_update
        self._q: queue.Queue[OutMsg] = queue.Queue()
        self._outbox = PersistentQueue(outbox_path, "telegram")
        self._stop = threading.Event()
        self._offset: int | None = None
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tg-cmd")
        self._session = requests.Session()
        self.online = True
        self.stats = {"sent": 0, "failed": 0, "coalesced": 0, "queued_offline": 0}

    # ------------------------------------------------------------------ API
    def _call(self, method: str, timeout: float = 10, **params) -> dict:
        r = self._session.post(API.format(token=self.token, method=method), json=params, timeout=timeout)
        data = r.json()
        if r.status_code == 429:
            raise _RateLimited(data.get("parameters", {}).get("retry_after", 2))
        if not data.get("ok"):
            raise RuntimeError(data.get("description", f"HTTP {r.status_code}"))
        return data

    # --------------------------------------------------------------- public
    def start(self):
        threading.Thread(target=self._sender_loop, name="tg-sender", daemon=True).start()
        if self.poll_enabled:
            threading.Thread(target=self._poll_loop, name="tg-poller", daemon=True).start()

    def stop(self):
        self._stop.set()
        self._pool.shutdown(wait=False)

    def send(self, chat_id, text: str, *, kind: str = "info", ttl_s: float | None = None,
             coalesce: bool = False, reply_markup: dict | None = None,
             on_sent: Callable[[int | None], None] | None = None) -> None:
        if not chat_id:
            return
        self._q.put(OutMsg(str(chat_id), text, kind, ttl_s, coalesce, reply_markup, on_sent))

    def delete(self, chat_id, message_id: int) -> None:
        self._q.put(OutMsg(str(chat_id), "", "delete", op="delete", extra={"message_id": message_id}))

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._q.put(OutMsg("", text, "answer", op="answer", extra={"callback_query_id": callback_id}))

    # --------------------------------------------------------------- sender
    def _sender_loop(self):
        last_flush = 0.0
        while not self._stop.is_set():
            try:
                msg = self._q.get(timeout=1.0)
            except queue.Empty:
                if self.online and time.time() - last_flush > 10 and len(self._outbox):
                    self._flush_outbox()
                    last_flush = time.time()
                continue
            if msg.coalesce:
                msg = self._coalesce(msg)
            self._deliver(msg)

    def _coalesce(self, first: OutMsg) -> OutMsg:
        """Collect further coalescable messages for the same chat for a short window."""
        texts, deadline, others = [first.text], time.time() + self.coalesce_window, []
        while time.time() < deadline:
            try:
                m = self._q.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                break
            if m.coalesce and m.chat_id == first.chat_id:
                texts.append(m.text)
            else:
                others.append(m)
        for m in others:
            self._q.put(m)
        if len(texts) > 1:
            self.stats["coalesced"] += len(texts) - 1
            first.text = "\n".join(texts)
        return first

    def _deliver(self, msg: OutMsg, from_outbox: bool = False) -> bool:
        if msg.ttl_s and time.time() - msg.created > msg.ttl_s:
            log.info("Dropping expired %s message", msg.kind)
            return True
        for attempt in range(3):
            try:
                if msg.op == "delete":
                    self._call("deleteMessage", chat_id=msg.chat_id, message_id=msg.extra["message_id"])
                    return True
                if msg.op == "answer":
                    self._call("answerCallbackQuery", callback_query_id=msg.extra["callback_query_id"],
                               text=msg.text[:190])
                    return True
                params = {"chat_id": msg.chat_id, "text": msg.text[:4000]}
                if msg.reply_markup:
                    params["reply_markup"] = msg.reply_markup
                data = self._call("sendMessage", **params)
                self.online = True
                self.stats["sent"] += 1
                latency = (time.time() - msg.created) * 1000
                metric("telegram", kind=msg.kind, latency_ms=round(latency, 1), attempt=attempt + 1,
                       from_outbox=int(from_outbox))
                if msg.on_sent:
                    msg.on_sent(data["result"]["message_id"])
                return True
            except _RateLimited as e:
                time.sleep(e.retry_after)
            except requests.RequestException as e:
                self.online = False
                log.warning("Telegram unreachable (%s)", e.__class__.__name__)
                time.sleep(min(4, 0.5 * 2 ** attempt))
            except Exception as e:  # API error (bad chat id, deleted message...)
                log.warning("Telegram %s failed: %s", msg.op, e)
                return True  # not retryable
        self.stats["failed"] += 1
        if msg.op == "send" and not from_outbox and not msg.on_sent:
            self._outbox.push({"chat_id": msg.chat_id, "text": msg.text, "kind": msg.kind,
                               "created": msg.created}, ttl_s=msg.ttl_s or 24 * 3600)
            self.stats["queued_offline"] += 1
        elif msg.on_sent:
            msg.on_sent(None)
        return False

    def _flush_outbox(self):
        items = self._outbox.peek(20)
        done = []
        for qid, created, p in items:
            m = OutMsg(p["chat_id"], f"[delayed {time.strftime('%H:%M', time.localtime(created))}] {p['text']}",
                       p.get("kind", "info"), created=p.get("created", created))
            if self._deliver(m, from_outbox=True):
                done.append(qid)
            else:
                break
        self._outbox.remove(done)

    # --------------------------------------------------------------- poller
    def _poll_loop(self):
        self._skip_stale()
        while not self._stop.is_set():
            try:
                params = {"timeout": self.poll_timeout, "allowed_updates": ["message", "callback_query"]}
                if self._offset is not None:
                    params["offset"] = self._offset
                data = self._call("getUpdates", timeout=self.poll_timeout + 10, **params)
                self.online = True
                for upd in data.get("result", []):
                    self._offset = upd["update_id"] + 1
                    if self.on_update:
                        self._pool.submit(self._safe_dispatch, upd)
            except _RateLimited as e:
                time.sleep(e.retry_after)
            except Exception as e:
                self.online = False
                log.warning("Telegram polling error: %s", e)
                self._stop.wait(5)

    def _safe_dispatch(self, upd):
        try:
            self.on_update(upd)
        except Exception:
            log.exception("Telegram update handler failed")

    def _skip_stale(self):
        """Ignore commands sent while the system was offline (no replay on restart)."""
        try:
            data = self._call("getUpdates", timeout=15)
            if data.get("result"):
                self._offset = data["result"][-1]["update_id"] + 1
                log.info("Skipped %d stale Telegram update(s)", len(data["result"]))
        except Exception as e:
            log.warning("Could not skip stale updates: %s", e)


class _RateLimited(Exception):
    def __init__(self, retry_after):
        self.retry_after = float(retry_after)


class FakeTelegram:
    """Console stand-in used by the simulator/tests when no bot token is given."""

    def __init__(self, on_update=None, echo: bool = True):
        self.on_update = on_update
        self.echo = echo
        self.sent: list[dict] = []
        self._next_id = 1
        self.online = True
        self.stats = {"sent": 0, "failed": 0, "coalesced": 0, "queued_offline": 0}

    def start(self):
        pass

    def stop(self):
        pass

    def send(self, chat_id, text, *, kind="info", ttl_s=None, coalesce=False, reply_markup=None, on_sent=None):
        if not chat_id:
            return
        mid = self._next_id
        self._next_id += 1
        self.sent.append({"chat_id": str(chat_id), "text": text, "kind": kind, "message_id": mid,
                          "buttons": bool(reply_markup)})
        self.stats["sent"] += 1
        metric("telegram", kind=kind, latency_ms=0.0, attempt=1, from_outbox=0)
        if self.echo:
            print(f"\n[TELEGRAM -> {chat_id}] {text}\n", flush=True)
        if on_sent:
            on_sent(mid)

    def delete(self, chat_id, message_id):
        if self.echo:
            print(f"[TELEGRAM] (deleted message {message_id} in chat {chat_id})", flush=True)

    def answer_callback(self, callback_id, text=""):
        pass

    def inject(self, chat_id: str, text: str):
        """Pretend a user sent `text` to the bot."""
        if self.on_update:
            self.on_update({"update_id": 0, "message": {"chat": {"id": int(chat_id)}, "text": text}})
