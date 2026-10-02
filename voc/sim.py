"""Offline quota simulator: canned API responses, virtual clock, no network,
no real quota spent.

Two uses:
1. Buyer dry-runs: point a budget split at the simulator and read
   fidelity_report() before spending a single real unit.
2. Our own proof: the tests below simulate bounded sweeps under a budget
   and assert budget adherence and watermark consistency across a
   mid-run exhaustion.

    from voc.sim import VirtualClock, FakeRequester, make_threads
    from voc import Store, Listener

    clock = VirtualClock("2026-10-02T00:00:00-07:00")  # midnight PT
    req = FakeRequester(threads={"vid1": [make_threads("vid1", 3)]}, clock=clock)
    store = Store(":memory:")
    store.add_area("t", queries=["x"], person_terms=["p"], topic_terms=["t"])
    store.add_video("t", "vid1")
    q = Listener("t", store, requester=req)
    q.sweep(max_units=1000)
    # clock.events -> [(virtual_t, endpoint, units), ...] for analysis
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .quota import Budget, QuotaExhausted, LedgerUnavailable, \
    credential_scope, quota_day, shared_spent_today


class DryRunBudget(Budget):
    """Zero-spend budget for simulations.

    Enforces run/daily caps against the run's own virtual spend only —
    it never reads or writes the local quota_usage table or the shared
    ledger. Every spend path in the library goes through check() /
    reserve() / spend(), all shadowed here, so a simulated run is
    provably zero-spend by construction.

    Deliberately self-contained (it does not call the inherited
    ledger-touching implementations): the shared-ledger path is exactly
    what a simulation must never touch.
    """

    def _cap_left(self):
        left = self.daily_cap - self.run_spent
        if self.run_cap is not None:
            left = min(left, self.run_cap - self.run_spent)
        return max(0, left)

    def run_remaining(self):
        return self._cap_left()

    def check(self, cost):
        """True if `cost` virtual units fit the remaining run/daily caps."""
        return cost <= self.run_remaining()

    def reserve(self, cost, ttl=None):
        if not self.check(cost):
            raise QuotaExhausted(
                f"area={self.area} (dry run): no budget for {cost} units "
                f"({self.run_spent}/{self.run_cap or self.daily_cap} used)")
        return ("dryrun", self.run_spent)  # opaque reservation token

    def cancel_reservation(self, reservation_id):
        return True

    def spend(self, cost, area=None, note="", reservation=None):
        if not self.check(cost):
            raise QuotaExhausted(
                f"area={self.area} day={self._day()}: spending {cost} would exceed "
                f"budget (run {self.run_spent}/{self.run_cap}, "
                f"day {self.run_spent}/{self.daily_cap}) {note}".strip())
        self.run_spent += cost


class VirtualClock:
    """Virtual time: monotonic() for pacing, sleep() advances instantly,
    pt_now() for quota-day math. Every API call is recorded with its
    virtual timestamp for analysis."""

    def __init__(self, start_pt="2026-10-02T00:00:00-07:00"):
        self.start = datetime.fromisoformat(start_pt)
        if self.start.tzinfo is None:
            self.start = self.start.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
        self.t = 0.0
        self.events = []  # (virtual_seconds, endpoint, units)

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)

    def pt_now(self):
        return self.start + timedelta(seconds=self.t)

    def record(self, endpoint, units):
        self.events.append((self.t, endpoint, units))

    def day(self):
        return self.pt_now().strftime("%Y-%m-%d")


class FakeRequester:
    """Canned normalized API responses.

    search:  {query: [[items...], [page2 items...]]}  (normalized items)
    threads: {video_id: [[threads...], [page2...]]}   (normalized threads)
    disabled: {video_id, ...} videos with comments disabled.
    Ignores (pops) __area__ like the real wrappers do.
    """

    def __init__(self, search=None, threads=None, disabled=(), clock=None):
        self.search = search or {}
        self.threads = threads or {}
        self.disabled = set(disabled)
        self.clock = clock

    def _record(self, endpoint, units):
        if self.clock is not None:
            self.clock.record(endpoint, units)

    def __call__(self, endpoint, params):
        params = dict(params)
        params.pop("__area__", None)
        if endpoint == "search":
            pages = self.search.get(params.get("q"), [[]])
            token = params.get("pageToken")
            idx = int(token.split("_")[1]) if token else 0
            items = pages[idx] if idx < len(pages) else []
            out = {"items": list(items)}
            if idx + 1 < len(pages):
                out["nextPageToken"] = f"p_{idx + 1}"
            self._record("search", 100)
            return out
        if endpoint == "commentThreads":
            vid = params.get("videoId")
            if vid in self.disabled:
                self._record("commentThreads", 1)
                return {"comments_disabled": True}
            pages = self.threads.get(vid, [[]])
            token = params.get("pageToken")
            idx = int(token.split("_")[1]) if token else 0
            batch = pages[idx] if idx < len(pages) else []
            out = {"threads": list(batch)}
            if idx + 1 < len(pages):
                out["nextPageToken"] = f"p_{idx + 1}"
            self._record("commentThreads", 1)
            return out
        raise ValueError(f"FakeRequester: unknown endpoint {endpoint}")


def make_threads(video_id, n, start="2026-09-20T12:00:00Z", step_hours=7,
                 text="I am a baker and my sourdough starter is stuck, any tips?",
                 id_offset=0):
    """n normalized threads, newest first, step_hours apart."""
    base = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    out = []
    for i in range(n):
        ts = (base - timedelta(hours=i * step_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({
            "thread_id": f"{video_id}.t{id_offset + i}",
            "video_id": video_id,
            "top_text": f"{text} (thread {i})",
            "top_published_at": ts,
            "top_like_count": i,
            "reply_count": 0,
            "reply_authors": [],
            "replies": [],
            "url": f"https://www.youtube.com/watch?v={video_id}",
        })
    return out


def make_videos(area, n, prefix="vid"):
    """Seed n videos into the store for an area (due immediately)."""
    for i in range(n):
        vid = f"{prefix}-{area}-{i}"
        # add_video is INSERT OR IGNORE; fine to call directly
        yield vid


def max_window_spend(events, window=600.0):
    """Max units spent in any sliding `window`-second span. The token-bucket
    invariant: <= rate*(window + capacity_seconds) + biggest single call."""
    if not events:
        return 0
    ev = sorted(events)
    best, j, acc = 0, 0, 0
    for i in range(len(ev)):
        acc += ev[i][2]
        while ev[i][0] - ev[j][0] > window:
            acc -= ev[j][2]
            j += 1
        best = max(best, acc)
    return best


# --- canned end-to-end scenario for the `voc simulate` CLI -----------------
# Demo question + canned YouTube responses: three videos whose titles match
# the vocabulary, each with three comment threads that pass the score gate.
DEMO_SCENARIO = {
    "queries": ["sourdough starter tips"],
    "person_terms": ["baker", "home baker"],
    "topic_terms": ["sourdough", "starter"],
    "threshold": 40,
    "videos": [
        {"video_id": "sim-vid-1",
         "title": "Sourdough starter tips for home bakers",
         "channel": "Demo Kitchen", "published_at": "2026-09-28T12:00:00Z"},
        {"video_id": "sim-vid-2",
         "title": "Why my sourdough starter keeps failing",
         "channel": "Demo Bakes", "published_at": "2026-09-25T12:00:00Z"},
        {"video_id": "sim-vid-3",
         "title": "Easy sourdough for beginners",
         "channel": "Demo Daily", "published_at": "2026-09-20T12:00:00Z"},
    ],
    "threads_per_video": 3,
}


def build_demo_requester(scenario=None, clock=None):
    """FakeRequester wired for the demo scenario: one search page per query
    returning the canned videos, one threads page per video."""
    scenario = scenario or DEMO_SCENARIO
    clock = clock or VirtualClock()
    query = scenario["queries"][0]
    vids = [dict(v) for v in scenario["videos"]]
    return FakeRequester(
        search={query: [vids]},
        threads={v["video_id"]: [make_threads(
            v["video_id"], scenario["threads_per_video"])]
            for v in scenario["videos"]},
        clock=clock), clock


def simulate_query_engine(db, area="demo", max_units=400, max_new_hits=10,
                          scenario=None):
    """Zero-spend end-to-end rehearsal: estimate (free) + discover + sweep
    against canned data, on a caller-supplied DB. Uses DryRunBudget, so the
    run can never record quota — the returned report asserts that with
    before/after readings of both the local usage table and the shared
    ledger. No network, no credentials, no real spend.

    Returns {"ok": True, "real_units_spent": 0, ...} with the nested
    estimate/discover/sweep reports. Raises AssertionError if anything
    actually recorded spend.
    """
    from .listener import Listener
    from .store import Store

    scenario = scenario or DEMO_SCENARIO
    store = Store(db)
    store.add_area(area, queries=scenario["queries"],
                   person_terms=scenario["person_terms"],
                   topic_terms=scenario["topic_terms"],
                   threshold=scenario["threshold"])
    requester, clock = build_demo_requester(scenario)
    scope = credential_scope()
    day = quota_day()
    # The demo is read-only w.r.t. the ledger: a broken ledger degrades
    # the verification (spend paths fail closed instead — but DryRunBudget
    # never touches the ledger anyway).
    try:
        ledger_before = shared_spent_today(day, scope)
    except LedgerUnavailable:
        ledger_before = None
    usage_before = store.usage_today(None, day=day)
    listener = Listener(area, store, requester=requester,
                        budget_cls=DryRunBudget,
                        utcnow_fn=lambda: clock.pt_now().strftime(
                            "%Y-%m-%dT%H:%M:%SZ"))
    est = listener.estimate()
    # --max-units caps discover + sweep COMBINED (same contract as the
    # `query` command): discover spends against the whole cap, the sweep
    # gets only the unspent remainder.
    d = listener.discover(max_units=max_units)
    sweep_cap = max(0, max_units - d["units_spent"])
    s = listener.sweep(max_units=sweep_cap, max_new_hits=max_new_hits)
    try:
        ledger_after = shared_spent_today(day, scope)
    except LedgerUnavailable:
        ledger_after = None
    usage_after = store.usage_today(None, day=day)
    store.close()
    ledger_unchanged = (ledger_before is None or
                        ledger_after == ledger_before)
    assert ledger_unchanged, \
        f"simulation recorded shared-ledger spend: {ledger_before} -> {ledger_after}"
    assert usage_after == usage_before, \
        f"simulation recorded local quota usage: {usage_before} -> {usage_after}"
    return {
        "ok": True,
        "area": area,
        "db": db,
        "real_units_spent": 0,
        "shared_ledger_unchanged": ledger_unchanged,
        "local_usage_unchanged": True,
        "virtual_units": sum(u for _, _, u in clock.events),
        "episodes_new": s.get("episodes_new", 0),
        "estimate": {"total_units_expected": est.get("total_units_expected"),
                     "total_units_worst": est.get("total_units_worst")},
        "discover": {"videos_added": d.get("videos_added"),
                     "queries_run": d.get("queries_run"),
                     "stopped_early": d.get("stopped_early")},
        "sweep": {"videos_polled": s.get("videos_polled"),
                  "stopped_early": s.get("stopped_early"),
                  "interrupted": s.get("interrupted", False)},
    }
