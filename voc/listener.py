"""Listener: one area's query engine.

    from voc import Store, Listener
    store = Store("episodes.db")
    store.add_area("sourdough", queries=[...], person_terms=[...],
                    topic_terms=[...], threshold=40, depth="shallow")

    q = Listener("sourdough", store)
    q.estimate()            # what it will cost — show the operator, get a nod
    q.discover()            # seed/refresh tracked videos (budgeted)
    q.sweep(max_new_hits=10)  # poll most-promising-first, stream hits live,
                              # stop early once 10 new hits land
    q.inbox()               # triage in chat

Composable, not a pipeline: every step works alone. Nothing runs on a
schedule inside this library — the operator (or their cron) decides when
to ask. Results stream: hits print as JSON lines the moment they're
scored, so a run can be killed early with nothing lost.
"""
import re
from datetime import datetime, timedelta, timezone

from .api import discover as _discover, fetch_threads
from .auth import vault_cli_requester
from .plan import (DEPTHS, estimate as _estimate,
                   render_estimate as _render_estimate)
from .quota import (Budget, DEFAULT_DAILY_CAP, QuotaExhausted,
                    LedgerUnavailable, budgeted_requester, quota_day)
from .scoring import score as _regex_score_fn

# Firehose guard: this tool finds things, it doesn't spray them.
# Sustained hit rates above these levels mean the filters are broken
# (vocabulary too broad, threshold too low), not the niche rich.
# Normal measured rates are far below 1 hit/video (dogfood 2026-10-01:
# ~0.02). BOTH volume and rate must trip: promising-first ordering
# front-loads the richest videos, so a short run of good videos must
# never trigger this.
FIREHOSE_MIN_VIDEOS = 10
FIREHOSE_MIN_HITS = 150
FIREHOSE_HITS_PER_VIDEO = 15
from .stream import emit_hit, emit_progress, emit_summary
from .triage import format_episode, inbox as _inbox


def _title_relevance(title, person_terms, topic_terms):
    """Cheap promise signal, used ONLY for ordering (never for scoring):
    how much of the area's vocabulary shows up in the video title."""
    t = (title or "").lower()
    hits = 0
    for term in person_terms:
        if re.search(r"\b" + re.escape(term.lower()) + r"\b", t):
            hits += 2
    for term in topic_terms:
        if term.lower() in t:
            hits += 1
    return hits


def rank_videos(videos, cfg):
    """Most-promising-first order for a sweep: title vocabulary hits, then
    the discovery query's position (earlier queries are usually the most
    on-point), then recency. The point: first useful hits in minutes,
    completeness after."""
    queries = cfg.get("queries", []) or []
    qpos = {q: i for i, q in enumerate(queries)}

    def key(v):
        rel = _title_relevance(v.get("title", ""), cfg.get("person_terms", ()),
                               cfg.get("topic_terms", ()))
        qp = qpos.get(v.get("discovery_query", ""), len(queries))
        pub = v.get("published_at") or ""
        # unpolled videos before already-polled ones at equal relevance
        fresh = 0 if not v.get("last_polled") else 1
        return (-rel, fresh, qp, _invert_ts(pub))

    return sorted(videos, key=key)


def _invert_ts(ts):
    """Sortable inverse timestamp: newer first, empty last."""
    return "".join(chr(0x10FFFF - ord(c)) for c in ts) if ts else chr(0x10FFFF)


def _utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Listener:
    def __init__(self, area, store, requester=None,
                 daily_cap=DEFAULT_DAILY_CAP, utcnow_fn=None, budget_cls=None,
                 scorer="regex", scorer_params=None):
        self.area = area
        self.cfg = store.get_area(area)  # KeyError with guidance if missing
        self.store = store
        self.requester = requester or vault_cli_requester()
        self.daily_cap = daily_cap
        # Lite scorer: regex only (zero inference, zero cost). The Clef
        # decision-model scorer is a voc-listener Pro feature. _regex_score_fn
        # IS voc.scoring.score — the same function get_scorer("regex")
        # returns in Pro, so Lite scores exactly like Pro's default mode.
        if scorer is not None and str(scorer).lower() != "regex":
            raise ValueError(f"unknown scorer {scorer!r}: voc-listener "
                             "Lite scores with the regex vocabulary scorer "
                             "only (the Clef decision model is Pro-only)")
        self._scorer_name = "regex"
        self._score_fn = _regex_score_fn
        self._scorer_params = {}
        # Quota-owner identity for the cross-database shared ledger: the
        # requester carries it (vault credential name, or key fingerprint).
        # Budget defaults from the environment when the requester has none.
        self.scope = getattr(self.requester, "scope", None)
        # budget_cls: Budget subclass used for every run. Pass DryRunBudget
        # (voc.sim) to rehearse with enforced caps but zero recorded spend.
        self._budget_cls = budget_cls or Budget
        # utcnow_fn: () -> "%Y-%m-%dT%H:%M:%SZ". Injectable for the simulator;
        # defaults to real wall-clock UTC.
        self._utcnow = utcnow_fn or _utcnow

    # --- seeding ---
    def discover(self, max_units=None):
        """Run the area's queries, track new videos. Budgeted burst:
        stops cleanly with queries_skipped reported when the budget runs
        out. max_units caps THIS run (the query command passes the total
        cap here and gives the sweep only the remainder). KeyboardInterrupt
        (Ctrl-C/SIGTERM) also stops cleanly with interrupted=True — videos
        added so far are committed per video, so the next run resumes."""
        started = self._utcnow()
        budget = self._budget_cls(self.store, self.area, self.daily_cap,
                         scope=self.scope,
                        run_cap=max_units)
        req = budgeted_requester(self.requester, budget)
        report = {"area": self.area, "kind": "discover",
                  "queries_run": 0, "queries_skipped": [],
                  "videos_added": 0, "units_spent": 0,
                  "stopped_early": False, "interrupted": False}
        for q in self.cfg["queries"]:
            try:
                videos, _ = _discover([q], requester=req)
            except QuotaExhausted:
                report["stopped_early"] = True
                report["queries_skipped"].append(q)
                break
            except KeyboardInterrupt:
                # Ctrl-C / SIGTERM mid-discover: add_video commits per
                # video, so everything found so far is kept and the next
                # run resumes cleanly (re-checks queries, dedupes via
                # INSERT OR IGNORE). No traceback, just a partial report.
                report["interrupted"] = True
                report["queries_skipped"].append(q)
                break
            report["queries_run"] += 1
            for v in videos:
                before = len(self.store.videos(self.area))
                self.store.add_video(self.area, v["video_id"], title=v["title"],
                                     channel=v["channel"], query=q,
                                     published_at=v.get("published_at", ""))
                if len(self.store.videos(self.area)) > before:
                    report["videos_added"] += 1
        report["units_spent"] = budget.run_spent
        # Cross-database visibility: the summary carries what the shared
        # ledger saw today, so the operator notices other databases'
        # spend against the same key. A broken ledger degrades this
        # read-only summary (spend paths fail closed instead).
        try:
            shared = budget.shared_spent_today()
            ledger_ok = True
        except LedgerUnavailable:
            shared, ledger_ok = None, False
        report["shared_ledger_ok"] = ledger_ok
        report["shared_spent_today"] = shared
        if shared is not None:
            local_today = self.store.usage_today(None, day=quota_day())
            report["other_db_spent_today"] = max(0, shared - local_today)
        else:
            report["other_db_spent_today"] = 0
        self.store.log_run(quota_day(), self.area, "discover", started,
                           self._utcnow(), budget.run_spent, 0,
                           len(report["queries_skipped"]), 0,
                           report["stopped_early"], "")
        return report

    def _run_score(self, thread, **kw):
        """Score one thread with the regex scorer."""
        kw.update(self._scorer_params)
        return self._score_fn(thread, self.cfg["person_terms"],
                              self.cfg["topic_terms"], **kw)

    # --- single-video poll: the query engine's unit of work ---
    def poll_video(self, video, requester=None, max_pages=None, match_re=None,
                   on_prescore=None):
        """Poll ONE video: fetch new threads, score, store, advance the
        watermark. Returns a report dict including the new hit episodes
        (full dicts, for streaming).

        match_re: grep mode — a compiled regex replacing the vocabulary
        gate. Threads it matches become hits (status "new") regardless of
        the score threshold; the score still measures quality signals for
        triage ranking. None (default) uses the area's vocabulary gate.

        Watermark rules (lossless by construction):
        - complete poll (early-stop or no more pages): watermark = poll start.
        - truncated by max_pages: watermark = oldest fetched thread's ts.
        - comments disabled: watermark = poll start (nothing to fetch, ever).
        - QuotaExhausted / API error: watermark UNCHANGED; the next run
          resumes losslessly.
        Episodes commit per video, so a kill mid-sweep loses nothing already
        processed.
        """
        req = requester or self.requester
        # Tag spend with this area for per-area accounting.
        base_req = req
        def req_with_area(endpoint, params):
            params = dict(params)
            params["__area__"] = self.area
            return base_req(endpoint, params)

        if max_pages is None:
            max_pages = DEPTHS[self.cfg["depth"]]["max_pages"]

        started = self._utcnow()
        video_id = video["video_id"]
        since = video.get("last_polled") or ""
        threads, pages, units, complete = fetch_threads(
            video_id, since=since, max_pages=max_pages, requester=req_with_area)
        new_count = 0
        new_episodes = []
        if threads is None:
            # Comments disabled: nothing to fetch, ever. Advance both.
            self.store.set_watermark(self.area, video_id, started)
            clef_estimate = None
        else:
            # Lite is regex-only: scoring costs nothing, so there is no
            # pre-spend scoring estimate. (Pro's clef mode computes one
            # here and fires on_prescore before spending.)
            clef_estimate = None
            for t in threads:
                if match_re is not None:
                    # Grep mode: the regex decides relevance; the score
                    # measures quality signals only. Match = hit.
                    full = t["top_text"] + "\n" + "\n".join(
                        r.get("text", "") for r in t.get("replies") or [])
                    is_match = bool(match_re.search(full))
                    res = self._run_score(t,
                                           threshold=self.cfg["threshold"], gate=is_match)
                    hit = is_match
                else:
                    res = self._run_score(t,
                                           threshold=self.cfg["threshold"])
                    hit = res["score"] >= self.cfg["threshold"]
                ep = {
                    "area": self.area,
                    "thread_id": t["thread_id"],
                    "video_id": video_id,
                    "top_text": t["top_text"],
                    "top_published_at": t["top_published_at"],
                    "top_like_count": t["top_like_count"],
                    "reply_count": t["reply_count"],
                    "replies": t["replies"],
                    "url": t["url"],
                    "score": res["score"],
                    "score_breakdown": res["breakdown"],
                    "status": "new" if hit else "low",
                }
                eid = self.store.add_episode(ep)
                if eid:
                    ep["id"] = eid
                    if ep["status"] == "new":
                        new_count += 1
                        new_episodes.append(ep)
            if complete:
                self.store.set_watermark(self.area, video_id, started)
            elif threads:
                # Truncated: only advance past what we durably stored.
                oldest = min(t.get("top_published_at", started) for t in threads)
                self.store.set_watermark(self.area, video_id, oldest)
            # QuotaExhausted propagates without touching the watermark.
        due = (datetime.strptime(self._utcnow(), "%Y-%m-%dT%H:%M:%SZ").replace(
                   tzinfo=timezone.utc) +
               timedelta(hours=self.cfg["cadence_hours"])).strftime(
                   "%Y-%m-%dT%H:%M:%SZ")
        self.store.set_next_due(self.area, video_id, due)
        self.store.log_run(quota_day(), self.area, "poll", started, self._utcnow(),
                           units, 1, 0, new_count, False, video_id,
                           pages=pages, complete=complete)
        return {"area": self.area, "video_id": video_id, "pages": pages,
                "units": units, "complete": complete,
                "episodes_new": new_count,
                "new_episodes": new_episodes,
                "threads_seen": 0 if threads is None else len(threads),
                "clef_estimate": clef_estimate}

    # --- query paths ---
    def estimate(self, include_discover=True, max_units=None):
        """What will this cost? Pure function of config + history — no spend.
        Show the rendered estimate to the operator and get a nod before
        discover()/sweep(). The shared ledger is consulted under this
        listener's requester scope (not the VOC_CREDENTIAL default)."""
        return _estimate(self.store, self.area,
                         include_discover=include_discover,
                         max_units=max_units, daily_cap=self.daily_cap,
                         scope=self.scope)

    def render_estimate(self, include_discover=True, max_units=None):
        return _render_estimate(self.estimate(include_discover=include_discover,
                                             max_units=max_units))

    def poll(self, max_units=None, max_new_hits=None, order="promising",
             on_hit=None, on_progress=None, max_hits_per_video=20,
             match=None, on_prescore=None):
        """Poll tracked videos, most-promising-first, streaming as it goes.

        - order="promising" (default): title-vocabulary hits, then discovery
          query position, then recency — first useful hits in minutes.
          order="stalest": oldest watermark first (catch-up passes).
        - max_new_hits: stop early once this many new inbox hits land.
          The run reports stopped_early with the reason; everything found
          is already in the DB.
        - max_hits_per_video (default 20): per-video STREAM cap. Every hit
          is saved to the DB; only the first N per video are emitted live.
          One overbroad video can't drown the stream — the summary says
          how many were saved but not streamed.
        - match: grep mode. A regex string replacing the person/topic
          vocabulary gate: every thread it matches becomes a hit
          (status "new"), regardless of the score threshold. The score is
          still computed — it measures quality signals for triage ranking.
          Boolean logic in regex: (a|b) is OR, (?=.*a)(?=.*b) is AND,
          \\bterm\\b is a whole word, (?!.*spam) is NOT.
        - Positional resume: after every video, the remaining order is
          saved; a new sweep continues where the last one stopped instead
          of re-ranking from the top. A fresh discovery (new videos) or a
          completed pass re-ranks from scratch.
        - Firehose guard: independent of max_new_hits, the run stops itself
          if sustained hit volume looks like broken filters rather than a
          rich niche (>=150 hits over >=10 videos at >15/video). The stop
          reason says the filters are too broad.
        - on_hit(episode): called synchronously per new hit, right after it
          commits. on_progress(video_report, totals): called per video.
          on_prescore(prescore): called per video BEFORE any decision-model
          token is spent (clef mode only) — {"video_id", "threads_seen",
          "clef_estimate"}. The operator sees the price before the spend.
        - KeyboardInterrupt (Ctrl-C, or the agent killing the run): caught,
          partial report returned with interrupted=True. Nothing already
          streamed is lost — episodes commit per video, before their hit
          line prints.
        """
        budget = self._budget_cls(self.store, self.area, self.daily_cap, run_cap=max_units,
                         scope=self.scope)
        req = budgeted_requester(self.requester, budget)
        started_at = self._utcnow()
        # Grep mode: compile the agent's regex once; a bad pattern is a
        # usage error, raised before any unit is spent.
        match_re = None
        if match is not None:
            from .grep import compile_pattern
            try:
                match_re = compile_pattern(match)
            except ValueError as e:
                raise ValueError(f"--match: {e}")
        report = {"area": self.area, "kind": "sweep",
                  "videos_polled": 0, "videos_total": 0,
                  "videos_skipped": [], "episodes_new": 0,
                  "hits_saved_not_streamed": 0,
                  "units_spent": 0, "stopped_early": False,
                  "stop_reason": "", "interrupted": False,
                  "match": match}
        videos = self.store.videos(self.area)
        # Positional resume: if the last sweep left a cursor, continue with
        # the remaining videos in their saved order, then any videos not
        # in the cursor (new discoveries, or already-polled ones) ranked
        # fresh after. A brand-new area (no cursor) ranks from scratch.
        by_id = {v["video_id"]: v for v in videos}
        cursor = self.store.get_sweep_cursor(self.area)
        resumed = False
        if cursor:
            remaining = [by_id[vid] for vid in cursor if vid in by_id]
            if remaining:
                rest = [v for v in videos if v["video_id"] not in set(cursor)]
                if order == "promising":
                    rest = rank_videos(rest, self.cfg)
                videos = remaining + rest
                resumed = True
        if not resumed and order == "promising":
            videos = rank_videos(videos, self.cfg)
        report["resumed_from_cursor"] = resumed
        report["videos_total"] = len(videos)
        try:
            for i, v in enumerate(videos):
                # Firehose guard (checked before polling the next video):
                # if the filters are too broad, stop rather than drown the
                # operator in hits. Requires both volume and sustained rate
                # so rich-but-legit niches never trip it.
                if (report["videos_polled"] >= FIREHOSE_MIN_VIDEOS
                        and report["episodes_new"] >= FIREHOSE_MIN_HITS
                        and report["episodes_new"] / report["videos_polled"]
                        > FIREHOSE_HITS_PER_VIDEO):
                    rate = (report["episodes_new"]
                            / report["videos_polled"])
                    report["stopped_early"] = True
                    report["stop_reason"] = (
                        f"firehose guard tripped: {report['episodes_new']} "
                        f"hits across {report['videos_polled']} videos "
                        f"({rate:.1f}/video). Filters are too broad — "
                        "tighten person_terms/topic_terms or raise the area "
                        "threshold, then re-run")
                    report["videos_skipped"] = [x["video_id"]
                                                for x in videos[i:]]
                    break
                if not budget.check(1):
                    report["stopped_early"] = True
                    report["stop_reason"] = (
                        "budget exhausted: "
                        f"{len(videos) - i} videos unpolled, watermarks kept")
                    report["videos_skipped"] = [x["video_id"] for x in videos[i:]]
                    break
                try:
                    r = self.poll_video(v, requester=req, match_re=match_re,
                                        on_prescore=on_prescore)
                except QuotaExhausted:
                    # This video's watermark is untouched (poll_video only
                    # advances it on success): the next run resumes it
                    # losslessly. Everything remaining is skipped.
                    report["stopped_early"] = True
                    report["stop_reason"] = ("quota exhausted mid-video; "
                                             "watermarks kept, resume anytime")
                    report["videos_skipped"] = [x["video_id"] for x in videos[i:]]
                    break
                report["videos_polled"] += 1
                report["episodes_new"] += r["episodes_new"]
                # Per-video stream cap: save everything, emit the first N.
                # One overbroad video can't drown the stream.
                streamed = r["new_episodes"][:max_hits_per_video]
                held = r["new_episodes"][max_hits_per_video:]
                report["hits_saved_not_streamed"] += len(held)
                try:
                    for ep in streamed:
                        if on_hit:
                            on_hit(ep)
                except KeyboardInterrupt:
                    # Interrupted mid-stream: this video's episodes are saved
                    # and its watermark advanced, but the cursor was never
                    # written (the write below only runs per completed video).
                    # Point the cursor AT this video so the resume re-polls
                    # it — the since-filter makes that cost one page and the
                    # watermark dedupes episodes — instead of re-ranking from
                    # scratch and re-spending units on finished videos.
                    self.store.set_sweep_cursor(
                        self.area, [x["video_id"] for x in videos[i:]])
                    raise
                # Remember where we are, so the next sweep resumes here.
                self.store.set_sweep_cursor(
                    self.area, [x["video_id"] for x in videos[i + 1:]])
                if on_progress:
                    on_progress(r, {"videos_done": i + 1,
                                    "videos_total": len(videos),
                                    "total_hits": report["episodes_new"]})
                if (max_new_hits is not None
                        and report["episodes_new"] >= max_new_hits):
                    report["stopped_early"] = True
                    report["stop_reason"] = (
                        f"hit target reached: {report['episodes_new']} new hits; "
                        f"{len(videos) - i - 1} videos unpolled (resume with sweep())")
                    report["videos_skipped"] = [x["video_id"] for x in videos[i + 1:]]
                    break
        except KeyboardInterrupt:
            report["interrupted"] = True
            report["stop_reason"] = ("interrupted by operator — partial results "
                                     "kept, resume anytime")
        # A finished pass (every video polled) clears the cursor; anything
        # else keeps it so the next sweep resumes positionally.
        if report["videos_polled"] == len(videos) and not report["videos_skipped"]:
            self.store.clear_sweep_cursor(self.area)
        if report["hits_saved_not_streamed"]:
            report["stop_reason"] += (
                f" [{report['hits_saved_not_streamed']} further hits saved "
                f"to the DB but not streamed (per-video cap "
                f"{max_hits_per_video})]" if report["stop_reason"]
                else (f"{report['hits_saved_not_streamed']} further hits saved "
                      f"to the DB but not streamed (per-video cap "
                      f"{max_hits_per_video})"))
        report["units_spent"] = budget.run_spent
        # Cross-database visibility: the summary carries what the shared
        # ledger saw today, so the operator notices other databases'
        # spend against the same key. A broken ledger degrades this
        # read-only summary (spend paths fail closed instead).
        try:
            _shared = budget.shared_spent_today()
            _ledger_ok = True
        except LedgerUnavailable:
            _shared, _ledger_ok = None, False
        report["shared_ledger_ok"] = _ledger_ok
        report["shared_spent_today"] = _shared
        if _shared is not None:
            _local_today = self.store.usage_today(None, day=quota_day())
            report["other_db_spent_today"] = max(0, _shared - _local_today)
        else:
            report["other_db_spent_today"] = 0
        # Keep the skipped list bounded: the count is what operators need;
        # full enumeration of 1000+ IDs in every summary is noise.
        n_skipped = len(report["videos_skipped"])
        if n_skipped > 50:
            report["videos_skipped"] = report["videos_skipped"][:50]
            report["videos_skipped_truncated"] = True
        else:
            report["videos_skipped_truncated"] = False
        report["videos_skipped_count"] = n_skipped
        # Aggregate row: kind="sweep" marks run-level rows so fidelity can
        # tell them apart from per-video kind="poll" rows.
        self.store.log_run(quota_day(), self.area, "sweep", started_at,
                           self._utcnow(), report["units_spent"],
                           report["videos_polled"],
                           n_skipped,
                           report["episodes_new"],
                           report["stopped_early"],
                           note=report["stop_reason"])
        return report

    def sweep(self, max_units=None, max_new_hits=None,
              on_hit=None, on_progress=None, max_hits_per_video=20,
              match=None, on_prescore=None):
        """Ask the question right now: poll the area, streaming hits live.

        max_units defaults to the remaining daily budget (hard cap — the
        run can never overspend the day). Pass max_new_hits to stop at the
        first N hits. max_hits_per_video caps how many hits per video are
        streamed (all are saved). match: a regex string replacing the
        vocabulary gate — grep mode: every thread it matches is a hit.
        Wire on_hit/on_progress/on_prescore to stream (see voc.stream),
        or leave them None for a silent run that just returns the report.
        on_prescore is accepted but never fires in Lite (regex scoring costs nothing).
        """
        if max_units is None:
            budget = self._budget_cls(self.store, self.area, self.daily_cap,
                             scope=self.scope)
            max_units = budget.remaining()
        report = self.poll(max_units=max_units, max_new_hits=max_new_hits,
                           on_hit=on_hit, on_progress=on_progress,
                           max_hits_per_video=max_hits_per_video,
                           match=match, on_prescore=on_prescore)
        report["budget_cap_units"] = max_units
        if max_units == 0:
            # Deliberately source-neutral: a 0 cap also arrives from the
            # query command when discover spent the whole --max-units cap.
            report["note"] = ("run cap is 0: nothing was polled (daily "
                              "budget exhausted, --max-units given as 0, or "
                              "a query's discover phase spent the whole cap)")
        return report

    # --- inbox (read-only) ---
    def inbox(self, limit=20):
        return _inbox(self.store, self.area, limit=limit)

    def format(self, ep, verbose=False):
        return format_episode(ep, verbose=verbose)

    def quota(self):
        from .quota import quota_status
        return quota_status(self.store, self.daily_cap)
