"""Hard quota budgeting for the YouTube Data API v3.

Costs verified 2026-10-01 against published quota tables (and Google's
official quota calculator): every call the library makes costs a fixed,
known number of units. The Budget enforces a daily cap as a HARD stop:
spend() raises QuotaExhausted rather than exceed, and callers degrade
gracefully (fewer pages, fewer videos) with a clear report of what was
skipped. Nothing is ever spent silently: every unit is recorded per
(Pacific-time quota day, area).

Quota day: the API resets at midnight America/Los_Angeles. All accounting
uses that day boundary, not UTC, not local time.

Cross-process safety: spending goes reserve -> API call -> reconcile
against a shared machine-local ledger. The reservation is the atomic
gate (one BEGIN IMMEDIATE transaction); the ledger is load-bearing —
if it is unavailable or corrupt, spending raises LedgerUnavailable and
nothing is spent (fail closed, never silent per-database accounting).
"""
from datetime import datetime
from math import ceil
from zoneinfo import ZoneInfo
import os
import sqlite3
import time
import uuid
from pathlib import Path

# Units per API page. Fixed by Google; verified 2026-10-01.
COSTS = {
    "search": 100,          # search.list, per page
    "commentThreads": 1,    # commentThreads.list, per page
}
DEFAULT_DAILY_CAP = 10000
RESET_TZ = "America/Los_Angeles"


# --- cross-database quota ledger -------------------------------------------
# YouTube quota belongs to the API key (Google Cloud project), but each
# voc database tracks usage in its own quota_usage table. Two databases
# sharing one key could each spend up to the daily cap — the blind spot.
# The shared ledger fixes it: every spend is ALSO recorded in one
# machine-local SQLite file, scoped by credential identity, and every
# budget check reads it. Scope is the vault credential name
# (VOC_CREDENTIAL, default custom.youtube-data-api) for the vault-CLI
# path, or a sha256 fingerprint (never the key) for with_api_key().
#
# Two hard guarantees:
# 1. Atomicity: check->spend is reserve->spend. The reservation (a row in
#    the reservations table, inserted in one BEGIN IMMEDIATE transaction
#    against the daily cap) is the cross-process gate: two processes can
#    no longer both pass the check and both spend past the cap. Actuals
#    are reconciled (committed + reservation dropped) in one transaction
#    after the call. A crashed holder's reservation expires on its own
#    after RESERVATION_TTL seconds.
# 2. Fail closed: the ledger is load-bearing. If it can't be opened,
#    read, or written, spending raises LedgerUnavailable and nothing is
#    spent — never a silent fallback to per-database accounting (which
#    under-counts and lets N databases each spend the full cap). Read-only
#    reports (status/estimate/fidelity) catch it and flag
#    shared_ledger_ok=False instead.
# One honest limitation: the ledger is per-machine; the same key used on
# two machines still can't see each other (see SKILL.md "Honest limits").


class LedgerUnavailable(Exception):
    """The shared quota ledger can't be opened, read, or written.

    Raised INSTEAD of spending: without the shared ledger, accounting
    silently under-counts and two databases on this credential could each
    spend the full daily cap. Fail closed — fix the ledger, then retry.
    """


RESERVATION_TTL = 300.0  # seconds; a crashed holder's reservation expires


def credential_scope():
    """Quota-owner identity for this process: the vault credential name,
    or 'key:<sha256>' when the caller passes an explicit API key."""
    return os.environ.get("VOC_CREDENTIAL", "custom.youtube-data-api")


def shared_ledger_path():
    return (Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
            / "voc" / "quota-ledger.db")


def _ledger_msg(path, reason):
    return (
        f"shared quota ledger unavailable: {path} ({reason}). "
        "Refusing to spend quota: without the shared ledger, two databases "
        "on this credential could each silently spend the full daily cap. "
        "Fix the ledger and retry: check disk space and write permission on "
        "the directory, delete stale -wal/-shm files if a crashed process "
        "held the lock, or point XDG_DATA_HOME at a writable location.")


def _ledger_conn():
    """Open the shared ledger, creating it if needed, and verify it is
    readable AND writable. Raises LedgerUnavailable on any failure —
    spend paths fail closed; read-only reports catch it."""
    p = shared_ledger_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise LedgerUnavailable(_ledger_msg(p, f"cannot create directory: {e}"))
    try:
        con = sqlite3.connect(str(p), timeout=30)
    except Exception as e:
        raise LedgerUnavailable(_ledger_msg(p, f"cannot open: {e}"))
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE IF NOT EXISTS shared_usage ("
                    "day TEXT NOT NULL, scope TEXT NOT NULL, "
                    "units INTEGER NOT NULL DEFAULT 0, "
                    "PRIMARY KEY (day, scope))")
        con.execute("CREATE TABLE IF NOT EXISTS reservations ("
                    "id TEXT PRIMARY KEY, day TEXT NOT NULL, "
                    "scope TEXT NOT NULL, units INTEGER NOT NULL, "
                    "created REAL NOT NULL, expires_at REAL NOT NULL)")
        # Migration for ledgers created before expires_at existed: a
        # crashed holder's reservation must expire, so backfill the
        # default window rather than letting old rows live forever.
        cols = [r[1] for r in con.execute("PRAGMA table_info(reservations)")]
        if "expires_at" not in cols:
            con.execute("ALTER TABLE reservations ADD COLUMN expires_at REAL")
            con.execute("UPDATE reservations SET expires_at = created + ? "
                        "WHERE expires_at IS NULL", (RESERVATION_TTL,))
        con.execute("SELECT 1 FROM shared_usage LIMIT 1")  # corrupt-file probe
    except Exception as e:
        try:
            con.close()
        except Exception:
            pass
        raise LedgerUnavailable(_ledger_msg(p, f"unreadable or corrupt: {e}"))
    return con


def _totals_in(con, day, scope, now):
    """(committed, live-reserved) inside an existing transaction. A
    reservation is live while now < expires_at; crashed holders' rows
    expire on their own."""
    row = con.execute("SELECT units FROM shared_usage WHERE day=? AND scope=?",
                      (day, scope)).fetchone()
    committed = row[0] if row else 0
    reserved = con.execute(
        "SELECT COALESCE(SUM(units),0) FROM reservations "
        "WHERE day=? AND scope=? AND expires_at > ?",
        (day, scope, now)).fetchone()[0]
    return committed, reserved


def shared_totals(day=None, scope=None):
    """(committed_units, live_reserved_units) today across ALL databases
    sharing this credential. Raises LedgerUnavailable when the ledger is
    unavailable — spend paths fail closed; read-only reports catch it."""
    day = day or quota_day()
    scope = scope or credential_scope()
    con = _ledger_conn()
    try:
        return _totals_in(con, day, scope, time.time())
    finally:
        con.close()


def shared_spent_today(day=None, scope=None):
    """Committed units spent today across ALL databases sharing this
    credential (live reservations not included). Raises LedgerUnavailable
    when the ledger is unavailable."""
    return shared_totals(day, scope)[0]


def reserve_shared(day, scope, units, cap, ttl=RESERVATION_TTL):
    """Atomically reserve `units` against the shared daily `cap`.

    One BEGIN IMMEDIATE transaction: drop expired reservations, then
    refuse (QuotaExhausted) if committed + live-reserved + units would
    exceed the cap, else insert the reservation row and commit. The
    BEGIN IMMEDIATE serializes concurrent reservers across processes, so
    two processes can never both hold reservations that exceed the cap.
    Returns the reservation id — pass it to reconcile_shared() after the
    API call, or cancel_reservation() on failure. Raises LedgerUnavailable
    when the ledger is broken: fail closed, nothing reserved.
    """
    con = _ledger_conn()
    rid = f"{os.getpid()}:{uuid.uuid4().hex}"
    try:
        con.execute("BEGIN IMMEDIATE")
        now = time.time()
        # Expired reservations (crashed holders) are garbage-collected
        # here; expiry is per-row (expires_at), set at insert time.
        con.execute("DELETE FROM reservations WHERE expires_at <= ?", (now,))
        committed, reserved = _totals_in(con, day, scope, now)
        if committed + reserved + units > cap:
            con.execute("ROLLBACK")
            raise QuotaExhausted(
                f"scope={scope} day={day}: reserving {units} units would exceed "
                f"the daily cap ({cap}): {committed} committed + {reserved} "
                "reserved by this and other databases today")
        con.execute("INSERT INTO reservations (id, day, scope, units, created, "
                    "expires_at) VALUES (?,?,?,?,?,?)",
                    (rid, day, scope, units, now, now + ttl))
        con.execute("COMMIT")
        return rid
    except (QuotaExhausted, LedgerUnavailable):
        raise
    except Exception as e:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise LedgerUnavailable(
            _ledger_msg(shared_ledger_path(), f"reservation failed: {e}"))
    finally:
        con.close()


def reconcile_shared(day, scope, reservation_id, actual_units):
    """Commit `actual_units` to the shared ledger and drop the reservation,
    in ONE transaction. Call after the API call succeeds (actual_units may
    differ from the reserved amount; the reservation is dropped either
    way). Raises LedgerUnavailable on failure — loud, never a silent
    under-count."""
    con = _ledger_conn()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT INTO shared_usage (day, scope, units) VALUES (?,?,?) "
                    "ON CONFLICT (day, scope) DO UPDATE "
                    "SET units = units + excluded.units",
                    (day, scope, actual_units))
        con.execute("DELETE FROM reservations WHERE id=?", (reservation_id,))
        con.execute("COMMIT")
    except LedgerUnavailable:
        raise
    except Exception as e:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise LedgerUnavailable(
            _ledger_msg(shared_ledger_path(), f"reconcile failed: {e}"))
    finally:
        con.close()


def cancel_reservation(reservation_id):
    """Drop a reservation without recording spend (the API call failed or
    was refused). Best-effort on purpose: a lost row expires on its own
    after RESERVATION_TTL seconds, so a failure here must not mask the
    real error — returns False instead of raising."""
    try:
        con = _ledger_conn()
    except LedgerUnavailable:
        return False
    try:
        con.execute("DELETE FROM reservations WHERE id=?", (reservation_id,))
        con.commit()
        return True
    except Exception:
        return False
    finally:
        try:
            con.close()
        except Exception:
            pass


def record_shared_usage(day, scope, units):
    """Commit `units` with no reservation to drop. Raises LedgerUnavailable
    on failure (fail closed — the old best-effort False return silently
    under-counted)."""
    reconcile_shared(day, scope, f"none:{uuid.uuid4().hex}", units)


class QuotaExhausted(Exception):
    """Raised instead of spending past the budget. A clean stop, not an
    error: the caller reports what was skipped and exits 0."""


def quota_day(now=None):
    """YYYY-MM-DD of the current YouTube quota day (midnight Pacific)."""
    now = now or datetime.now(ZoneInfo(RESET_TZ))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(RESET_TZ))
    return now.astimezone(ZoneInfo(RESET_TZ)).strftime("%Y-%m-%d")


def seconds_until_reset(now=None):
    """Seconds until the next quota-day reset (midnight Pacific). The
    estimates and budget caps use this to sanity-check planned spend against the day."""
    from datetime import timedelta
    now = now or datetime.now(ZoneInfo(RESET_TZ))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(RESET_TZ))
    now_pt = now.astimezone(ZoneInfo(RESET_TZ))
    nxt = (now_pt + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                               microsecond=0)
    return max(1.0, (nxt - now_pt).total_seconds())


class Budget:
    """A hard budget for one run of one area.

    run_cap: this run's slice of the day (see run_allowance()). spend()
    refuses to exceed EITHER the daily cap OR the run slice, whichever is
    tighter. Check-then-spend is done by budgeted_requester per API call,
    so a page is never issued without budget for it.

    Cross-process atomicity: every spend goes reserve -> API call ->
    reconcile against the shared ledger. The reservation is taken in one
    BEGIN IMMEDIATE transaction against the daily cap, so two processes
    can never both hold reservations that exceed it. If the shared ledger
    is unavailable or corrupt, spending raises LedgerUnavailable and
    nothing is spent — fail closed, never silent per-database accounting.
    """

    def __init__(self, store, area, daily_cap=DEFAULT_DAILY_CAP, run_cap=None,
                 now_fn=None, scope=None):
        self.store = store
        self.area = area
        self.daily_cap = daily_cap
        self.run_cap = run_cap
        self._now_fn = now_fn  # () -> aware datetime; virtual clock in sims
        self.run_spent = 0
        # Quota-owner identity: shared across databases on this machine so
        # two DBs on one key can't each spend the full daily cap.
        self.scope = scope or credential_scope()
        self.shared_ledger_ok = True  # refreshed by status() after each read

    def _day(self):
        now = self._now_fn() if self._now_fn else None
        return quota_day(now)

    def spent_today(self, area=None):
        return self.store.usage_today(area or self.area, day=self._day())

    def shared_spent_today(self):
        """Units spent today across ALL databases sharing this credential
        (raises LedgerUnavailable when the ledger is unavailable — fail
        closed)."""
        return shared_spent_today(self._day(), self.scope)

    def remaining(self):
        """Units left today: the tighter of the per-database cap and the
        cross-database shared cap for this credential. Live reservations
        (in-flight spends from any process on this machine) count against
        the shared cap. Raises LedgerUnavailable when the ledger is
        unavailable — spend paths fail closed; read-only reports catch
        it and flag shared_ledger_ok=False."""
        local = max(0, self.daily_cap - self.store.usage_today(None, day=self._day()))
        committed, reserved = shared_totals(self._day(), self.scope)
        return max(0, min(local, self.daily_cap - committed - reserved))

    def _check_local(self, cost):
        """Run-slice and per-database daily checks, no ledger read. Used
        by spend() after a reservation already covered the shared cap."""
        if self.run_cap is not None and cost > self.run_cap - self.run_spent:
            return False
        if cost > max(0, self.daily_cap - self.store.usage_today(None, day=self._day())):
            return False
        return True

    def run_remaining(self):
        """Units this run may still spend: the tighter of the run slice
        (when set) and the shared daily remainder. Raises
        LedgerUnavailable when the ledger can't be read (fail closed — a
        check that can't see the ledger can't be trusted)."""
        left = self.remaining()
        if self.run_cap is None:
            return left
        return max(0, min(self.run_cap - self.run_spent, left))

    def check(self, cost):
        """True if `cost` units can be spent right now (daily cap and run
        slice both respected). Raises LedgerUnavailable when the shared
        ledger is unavailable (fail closed — a check that can't see the
        ledger can't be trusted)."""
        return cost <= self.run_remaining()

    def reserve(self, cost, ttl=RESERVATION_TTL):
        """Atomically reserve `cost` units against the daily cap in the
        shared ledger. Returns the reservation id. Raises QuotaExhausted
        when committed + live-reserved + cost would exceed the cap;
        raises LedgerUnavailable when the ledger is broken (fail closed).
        """
        return reserve_shared(self._day(), self.scope, cost, self.daily_cap, ttl)

    def cancel_reservation(self, reservation_id):
        """Best-effort drop of a reservation (the API call failed or was
        refused). Returns False instead of raising — a lost row expires
        after RESERVATION_TTL seconds, so this must not mask the real
        error."""
        return cancel_reservation(reservation_id)

    def spend(self, cost, area=None, note="", reservation=None):
        """Reserve-then-record `cost` units. Raises QuotaExhausted instead
        of exceeding, LedgerUnavailable instead of spending when the
        shared ledger is broken (fail closed).

        area defaults to this budget's area; poll paths pass the video's
        area per call so accounting stays per-area. Pass a reservation id
        (from reserve()) when the caller already reserved — the shared
        portion is then reconciled instead of re-reserved."""
        if reservation is None:
            # The atomic gate: two processes racing here can't both hold
            # reservations that exceed the daily cap.
            reservation = self.reserve(cost)
        if not self._check_local(cost):
            self.cancel_reservation(reservation)
            raise QuotaExhausted(
                f"area={self.area} day={self._day()}: spending {cost} would exceed "
                f"budget (run {self.run_spent}/{self.run_cap}, "
                f"day {self.spent_today()}/{self.daily_cap}) {note}".strip())
        self.store.record_usage(self._day(), area or self.area, cost)
        # Reconcile: commit actuals and drop the reservation in one
        # transaction. If this raises LedgerUnavailable the failure is
        # loud (never a silent under-count); the local record stands and
        # the reservation expires on its own.
        reconcile_shared(self._day(), self.scope, reservation, cost)
        self.run_spent += cost

    def status(self):
        try:
            committed, reserved = shared_totals(self._day(), self.scope)
            shared, ledger_ok = committed, True
        except LedgerUnavailable:
            shared, reserved, ledger_ok = None, 0, False
        self.shared_ledger_ok = ledger_ok
        return {
            "day": self._day(),
            "area": self.area,
            "daily_cap": self.daily_cap,
            "scope": self.scope,
            "run_cap": self.run_cap,
            "run_spent": self.run_spent,
            "run_remaining": self.run_remaining() if ledger_ok else None,
            "spent_today_all_areas": self.store.usage_today(None, day=self._day()),
            "shared_spent_today": shared,
            "shared_reserved_today": reserved,
            "shared_ledger_ok": ledger_ok,
            "remaining_today": self.remaining() if ledger_ok else None,
            "reset_tz": RESET_TZ,
        }


def budgeted_requester(requester, budget):
    """Wrap a requester so every API page is reserve-then-spend.

    Reserves COSTS[endpoint] in the shared ledger BEFORE the call (the
    atomic gate: concurrent processes can't both hold reservations that
    exceed the daily cap), issues the call, then reconciles actuals.
    A reservation that can't be taken raises QuotaExhausted cleanly
    instead of issuing a call without budget; a broken ledger raises
    LedgerUnavailable and nothing is spent. On API failure the
    reservation is cancelled (best-effort; it expires on its own). A 403
    quotaExceeded from the API itself is also converted to QuotaExhausted
    (our pre-check should make this unreachable; belt and suspenders).
    """
    def wrapped(endpoint, params):
        cost = COSTS.get(endpoint)
        if cost is None:
            raise ValueError(f"unknown endpoint for quota accounting: {endpoint}")
        params = dict(params)
        area = params.pop("__area__", None)
        if not budget.check(cost):
            raise QuotaExhausted(
                f"area={budget.area}: no budget for {endpoint} page "
                f"({cost} units, run remaining {budget.run_remaining()})")
        reservation = budget.reserve(cost)
        try:
            data = requester(endpoint, params)
        except Exception as e:
            budget.cancel_reservation(reservation)
            if "quotaExceeded" in str(e):
                raise QuotaExhausted(f"API reports quota exceeded: {e}") from e
            raise
        budget.spend(cost, area=area, note=endpoint, reservation=reservation)
        return data
    return wrapped


def run_allowance(store, area_name, daily_cap=DEFAULT_DAILY_CAP, day=None):
    """This run's unit slice for an area: its steering weight-share of the
    daily cap, minus what it already spent today, divided by runs
    remaining today (from its cadence). Returns 0 when the area has no
    budget left or isn't steered.

    Weight-shares are the anti-starvation mechanism: a heavy area can
    never eat a light area's share, because each burst is capped at its
    own slice.
    """
    day = day or quota_day()
    weights = store.get_steering()["weights"]
    if weights.get(area_name, 0) <= 0:
        return 0
    cfg = store.get_area(area_name)
    total_weight = sum(weights.values()) or 1.0
    daily_share = daily_cap * (weights[area_name] / total_weight)
    spent = store.usage_today(area_name, day=day)
    runs_per_day = 24.0 / cfg["cadence_hours"] if cfg["cadence_hours"] > 0 else 1.0
    runs_done = store.runs_today(area_name, kind="poll", day=day)
    runs_left = max(1, ceil(runs_per_day - runs_done))
    return max(0, int((daily_share - spent) // runs_left))


def quota_status(store, daily_cap=DEFAULT_DAILY_CAP, day=None):
    """Plain-English quota report: spent today, remaining, per-area
    breakdown, projected burn at current cadence.

    projected_daily_burn: sum over areas of avg units/run (from the last
    7 days of runs; estimated from tracked video count when a area has no
    history) × runs/day from its cadence. Areas with no history are
    marked estimated:true so you can see what's measured vs guessed.
    """
    day = day or quota_day()
    areas = store.areas()
    weights = store.get_steering()["weights"]
    total_weight = sum(weights.values()) or 1.0
    spent_all = store.usage_today(None, day=day)
    by_area = {}
    projected = 0.0
    for t in areas:
        name = t["name"]
        spent = store.usage_today(name, day=day)
        avg, estimated = store.avg_run_cost(name, days=7)
        if avg is None:
            nvideos = len(store.videos(name))
            avg, estimated = nvideos * 1.2 + 2, True  # ~1 page/video + overhead
        runs_per_day = 24.0 / t["cadence_hours"] if t["cadence_hours"] > 0 else 1.0
        burn = avg * runs_per_day
        projected += burn
        w = weights.get(name, 0)
        by_area[name] = {
            "spent_today": spent,
            "weight": w,
            "steered": w > 0,
            "cadence_hours": t["cadence_hours"],
            "daily_share": round(daily_cap * w / total_weight, 1),
            "runs_today": store.runs_today(name, kind="poll", day=day),
            "avg_run_cost": round(avg, 1),
            "avg_run_cost_estimated": estimated,
            "projected_daily_burn": round(burn, 1),
        }
    projected = round(projected, 1)
    return {
        "day": day,
        "reset_tz": RESET_TZ,
        "daily_cap": daily_cap,
        "spent_today": spent_all,
        "remaining_today": max(0, daily_cap - spent_all),
        "by_area": by_area,
        "projected_daily_burn": projected,
        "projected_headroom": round(daily_cap - projected, 1),
        "over_budget": projected > daily_cap,
    }
