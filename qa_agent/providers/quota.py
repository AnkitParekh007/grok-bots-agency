"""Persistent, atomic request/token reservations, independent of dollar budgets.

Unknown usage retains the worst-case reservation. A crashed request releases its
concurrency lease after the timeout, but never refunds its request/token charge.
Limits are administrator supplied; remote remaining quota is unknown until observed.
"""
import json
import math
import re
import time
import uuid
from email.utils import parsedate_to_datetime


class QuotaExceeded(Exception):
    pass


def retry_seconds(value, clock=time.time):
    if value is None:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - clock()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None


def reset_seconds(value):
    """Groq-style duration headers, e.g. 2m59.56s (never interpreted as code)."""
    if not isinstance(value, str) or not re.fullmatch(r"(?:\d+(?:\.\d+)?[hms])+", value):
        return None
    return sum(float(n)*{"h": 3600, "m": 60, "s": 1}[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)([hms])", value))


class QuotaManager:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock
        with store.lock:
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS quota_reservations(
                    id TEXT PRIMARY KEY, scope TEXT, ts REAL, tokens INTEGER,
                    active_until REAL, outcome TEXT);
                CREATE INDEX IF NOT EXISTS quota_scope_ts ON quota_reservations(scope,ts);
                CREATE TABLE IF NOT EXISTS quota_observations(
                    scope TEXT PRIMARY KEY, blocked_until REAL DEFAULT 0,
                    observed_at REAL, remaining_json TEXT);
            """)

    @staticmethod
    def validate(limits):
        for k, v in limits.items():
            if k not in {"rpm", "rpd", "tpm", "tpd", "concurrency", "minute_window_s", "day_window_s", "reset_offset_s"}:
                raise ValueError(f"unknown quota field {k}")
            if not isinstance(v, int) or isinstance(v, bool) or v < (0 if k == "reset_offset_s" else 1):
                raise ValueError(f"quota.{k} must be a positive integer")

    def reserve(self, scope, tokens, limits, lease_s=60):
        self.validate(limits)
        ts = self.clock()
        minute, day = limits.get("minute_window_s", 60), limits.get("day_window_s", 86400)
        offset = limits.get("reset_offset_s", 0)
        starts = {"m": ((ts-offset)//minute)*minute+offset, "d": ((ts-offset)//day)*day+offset}
        s = self.store
        with s.lock:
            s.db.execute("BEGIN IMMEDIATE")
            try:
                obs = s.db.execute("SELECT blocked_until,remaining_json FROM quota_observations WHERE scope=?", (scope,)).fetchone()
                if obs and obs[0] > ts:
                    raise QuotaExceeded(f"provider Retry-After active for {math.ceil(obs[0]-ts)} seconds")
                remaining = json.loads(obs[1]) if obs else {}
                for key, amount in (("requests", 1), ("tokens", tokens)):
                    if key in remaining and remaining.get(key+"_until", 0) > ts and remaining[key] < amount:
                        raise QuotaExceeded(f"provider observed remaining {key} quota exhausted")
                active = s.db.execute("SELECT COUNT(*) FROM quota_reservations WHERE scope=? AND active_until>?", (scope, ts)).fetchone()[0]
                if active >= limits.get("concurrency", 1):
                    raise QuotaExceeded("provider concurrency quota exhausted")
                for suffix in ("m", "d"):
                    count, used = s.db.execute("SELECT COUNT(*),COALESCE(SUM(tokens),0) FROM quota_reservations WHERE scope=? AND ts>=?",
                                               (scope, starts[suffix])).fetchone()
                    if limits.get("rp"+suffix) is not None and count+1 > limits["rp"+suffix]:
                        raise QuotaExceeded(f"provider rp{suffix} quota exhausted")
                    if limits.get("tp"+suffix) is not None and used+tokens > limits["tp"+suffix]:
                        raise QuotaExceeded(f"provider tp{suffix} quota exhausted")
                rid = uuid.uuid4().hex
                s.db.execute("INSERT INTO quota_reservations VALUES(?,?,?,?,?,?)", (rid, scope, ts, tokens, ts+lease_s+5, "reserved"))
                if obs:
                    for key, amount in (("requests", 1), ("tokens", tokens)):
                        if key in remaining: remaining[key] = max(0, remaining[key]-amount)
                    s.db.execute("UPDATE quota_observations SET remaining_json=? WHERE scope=?", (json.dumps(remaining), scope))
                s.db.execute("COMMIT")
                return rid
            except Exception:
                s.db.execute("ROLLBACK")
                raise

    def settle(self, rid, actual=None, outcome="unknown"):
        if rid is None:
            return
        # A reservation may be refunded only when the gateway proves nothing was sent.
        if outcome == "denied_not_sent":
            self.store.x("DELETE FROM quota_reservations WHERE id=?", (rid,))
        elif actual is None:
            self.store.x("UPDATE quota_reservations SET active_until=0,outcome=? WHERE id=?", (outcome, rid))
        else:
            self.store.x("UPDATE quota_reservations SET active_until=0,tokens=?,outcome=? WHERE id=?", (actual, outcome, rid))

    def observe(self, scope, headers):
        delay = retry_seconds(headers.get("retry-after"), self.clock)
        remaining = {}
        for source, dest in (("x-ratelimit-remaining-requests", "requests"), ("x-ratelimit-remaining-tokens", "tokens")):
            try:
                remaining[dest] = max(0, int(headers[source]))
            except (KeyError, ValueError, TypeError):
                pass
        for key in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
            if key in headers:
                remaining[key] = str(headers[key])[:60]
        if delay is None and not remaining:
            return
        ts = self.clock()
        for key, default in (("requests", 86400), ("tokens", 60)):
            if key in remaining:
                delay_reset = reset_seconds(headers.get("x-ratelimit-reset-"+key))
                remaining[key+"_until"] = ts + (delay_reset if delay_reset is not None else default)
        self.store.x("INSERT INTO quota_observations VALUES(?,?,?,?) ON CONFLICT(scope) DO UPDATE SET "
                     "blocked_until=MAX(blocked_until,excluded.blocked_until),observed_at=excluded.observed_at,remaining_json=excluded.remaining_json",
                     (scope, ts+(delay or 0), ts, json.dumps(remaining)))

    def status(self, scope, limits):
        rows = self.store.q("SELECT * FROM quota_observations WHERE scope=?", (scope,))
        r = rows[0] if rows else {}
        return {"limits": limits, "remaining": json.loads(r["remaining_json"]) if r else None,
                "observed_at": r.get("observed_at"), "blocked_until": r.get("blocked_until"),
                "local_reservations": self.store.q("SELECT COUNT(*) AS requests,COALESCE(SUM(tokens),0) AS tokens FROM quota_reservations WHERE scope=?", (scope,))[0]}
