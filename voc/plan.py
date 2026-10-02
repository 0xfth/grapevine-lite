"""Pre-spend planning: what a query will cost BEFORE any units are spent.

The query-engine contract: the agent prints the estimate, the operator nods,
then (and only then) the spend happens. estimate() never makes an API call —
it is a pure function of the area config + measured history.

Rates (measured 2026-10-01, dogfood deployment):
- discovery: 100 units per search.list page — YouTube's price, exact.
  A query can pull a 2nd page (+100) when the first page is rich; the
  estimate reports discovery as a range.
- polling: ~1 unit per commentThreads page; steady-state polls early-stop
  after 1 page/video (no new comments since the watermark).
- burst pacing: subprocess-per-call via the vault CLIs. Calibrated below;
  deliberately conservative — real runs usually beat it.

History beats constants: when the area has past runs, estimate() uses
measured avg pages/video and measured hit rate, and says so. Fresh areas
get conservative defaults, clearly marked estimated.
"""
from .quota import (DEFAULT_DAILY_CAP, COSTS, shared_spent_today,
                    credential_scope, LedgerUnavailable)

# Depth knobs: how deep a first poll may paginate. (Shared with listener.py,
# which imports it from here.)
DEPTHS = {"deep": {"max_pages": 10}, "shallow": {"max_pages": 1}}

# --- calibration: burst pacing, measured 2026-10-01 -----------------------
# Conservative wall-time per API page when bursting (no pacer). Re-calibrate
# by timing a real sweep and updating these; estimate() stays honest by
# rounding UP and labeling everything estimated vs measured.
# Measured 2026-10-01 (fresh 2-query/shallow test area, 187 videos):
#   discover: 4 search pages in 7s  -> ~2s/page
#   sweep:    96 comment pages in 93s -> ~1s/page
# Constants sit above measured: estimates should under-promise.
SEC_PER_SEARCH_CALL = 2.0    # search.list page: subprocess + TLS + parse
SEC_PER_COMMENT_PAGE = 1.25  # commentThreads.list page: same
# ---------------------------------------------------------------------------


def _human_seconds(s):
    s = int(round(s))
    if s < 60:
        return f"~{max(1, s)}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"~{m}m" + (f"{sec}s" if sec else "")
    h, m = divmod(m, 60)
    return f"~{h}h{m:02d}m"


def estimate(store, area, include_discover=True, max_units=None,
             daily_cap=DEFAULT_DAILY_CAP, scope=None):
    """Estimate quota + wall time for discover and/or sweep on an area.

    Returns a dict with expected/worst-case units, wall-time strings, the
    remaining-budget check, expected new hits, and the streaming note.
    Pure function of stored state: no API calls, no spend.

    scope: quota-owner identity used to consult the shared ledger. Pass
    the requester's scope (e.g. the "key:<sha256>" fingerprint from
    with_api_key()) so the estimate reflects spend by THAT credential —
    without it the ledger falls back to the VOC_CREDENTIAL default, which
    silently attributes spend to the wrong scope for explicit-key flows.
    Listener.estimate() passes its own requester-derived scope.
    """
    from .quota import quota_day
    cfg = store.get_area(area)
    videos = store.videos(area)
    n_queries = len(cfg["queries"])
    depth_pages = DEPTHS[cfg["depth"]]["max_pages"]

    # --- discovery ---
    disc_expected = n_queries * COSTS["search"]          # 100/query, one page
    disc_worst = n_queries * COSTS["search"] * 2        # rich queries pull p2
    disc_wall = n_queries * SEC_PER_SEARCH_CALL

    # --- sweep ---
    new_videos = [v for v in videos if not v["last_polled"]]
    known_videos = [v for v in videos if v["last_polled"]]
    avg_pages, pages_estimated = store.avg_pages_per_video(area)
    if avg_pages is None:
        avg_pages, pages_estimated = (
            1.5 if cfg["depth"] == "deep" else 1.0), True
    # Wall time: measured sec/video beats the per-page constant when the
    # area has run history (it captures real pacing, subprocess overhead,
    # and early-stops as actually observed).
    sec_per_video, time_estimated = store.avg_seconds_per_video(area)
    if sec_per_video is None:
        sec_per_video, time_estimated = SEC_PER_COMMENT_PAGE, True
    # Fresh videos have no watermark: first poll can paginate up to max_pages.
    first_poll_pages = min(depth_pages, max(2.0, avg_pages * 2))
    sweep_expected = len(known_videos) * avg_pages + len(new_videos) * first_poll_pages
    sweep_worst = (len(known_videos) * avg_pages
                   + len(new_videos) * depth_pages)
    sweep_wall = (len(videos) * sec_per_video if not time_estimated
                  else sweep_expected * sec_per_video)

    total_expected = (disc_expected if include_discover else 0) + sweep_expected
    total_worst = (disc_worst if include_discover else 0) + sweep_worst
    total_wall = (disc_wall if include_discover else 0) + sweep_wall

    # --- budget check (cross-database: the shared ledger sees spend from
    # every database on this machine sharing the credential) ---
    day = quota_day()
    local_spent = store.usage_today(None, day=day)
    scope = scope or credential_scope()
    try:
        shared_spent = shared_spent_today(day, scope)
        ledger_ok = True
    except LedgerUnavailable:
        # Read-only report: degrade to per-database accounting with the
        # flag set, never a hard error. Spend paths fail closed instead.
        shared_spent, ledger_ok = None, False
    # The ledger starts empty on upgrade; max() keeps pre-ledger local
    # history authoritative until the ledger catches up.
    spent = max(local_spent, shared_spent) if ledger_ok else local_spent
    remaining = max(0, daily_cap - spent)
    other_db_spent = max(0, shared_spent - local_spent) if ledger_ok else 0
    fits = total_expected <= remaining
    cap_note = None
    if max_units is not None and total_expected > max_units:
        # The cap covers discover + sweep COMBINED: discover runs first
        # against the whole cap, the sweep gets only the unspent remainder.
        disc_share = disc_expected if include_discover else 0
        sweep_share = max(0, max_units - disc_share)
        coverable = int(sweep_share / max(avg_pages, 0.01))
        cap_note = (f"capped at {max_units} units for discover + sweep "
                    f"combined: discover takes ~{disc_share} first, leaving "
                    f"~{sweep_share} for the sweep (~{coverable} videos at "
                    f"current avg pages/video)")
    if not fits:
        coverable = int(remaining / max(avg_pages, 0.01))
        cap_note = (f"exceeds remaining budget ({remaining} units left today): "
                    f"expect a partial run (~{coverable} videos), or wait for "
                    f"the midnight-PT reset")

    # --- expected hits (measured hit rate when available) ---
    hit_rate, hr_estimated = store.avg_new_hits_per_poll(area)
    expected_hits = (round(hit_rate * len(videos), 1)
                     if hit_rate is not None else None)

    # --- time to first insight: poll the 5 most promising videos first ---
    first_insight_wall = 5 * sec_per_video

    return {
        "area": area,
        "kind": "estimate",
        "discovery": {
            "queries": n_queries,
            "units_expected": disc_expected,
            "units_worst": disc_worst,
            "wall": _human_seconds(disc_wall),
            "included": include_discover,
        },
        "sweep": {
            "videos_tracked": len(videos),
            "videos_never_polled": len(new_videos),
            "depth": cfg["depth"],
            "avg_pages_per_video": round(avg_pages, 2),
            "avg_pages_measured": not pages_estimated,
            "sec_per_video": round(sec_per_video, 2),
            "sec_per_video_measured": not time_estimated,
            "units_expected": int(round(sweep_expected)),
            "units_worst": int(round(sweep_worst)),
            "wall": _human_seconds(sweep_wall),
        },
        "total_units_expected": int(round(total_expected)),
        "total_units_worst": int(round(total_worst)),
        "total_wall": _human_seconds(total_wall),
        "first_hits_eta": _human_seconds(first_insight_wall),
        "budget": {
            "day": day,
            "daily_cap": daily_cap,
            "scope": scope,
            "remaining_today": remaining,
            "fits": fits,
            "cap_note": cap_note,
            "run_cap_units": max_units,
            "shared_ledger_ok": ledger_ok,
            "other_db_spent_today": other_db_spent,
        },
        "expected_new_hits": expected_hits,
        "hit_rate_measured": not hr_estimated if hit_rate is not None else False,
        "streaming": ("results stream live as JSON lines — stop the run "
                      "anytime (Ctrl-C); everything already printed is saved "
                      "in the DB, nothing is lost"),
    }


def render_estimate(rep):
    """Plain-text estimate for chat: the thing the operator nods at."""
    L = [f"Query estimate — area '{rep['area']}'"]
    d, s = rep["discovery"], rep["sweep"]
    if d["included"]:
        L.append(f"  discover: {d['queries']} queries → "
                 f"{d['units_expected']}–{d['units_worst']} units, {d['wall']}")
    L.append(f"  sweep: {s['videos_tracked']} videos "
             f"({s['videos_never_polled']} never polled) @ {s['depth']} → "
             f"{s['units_expected']}–{s['units_worst']} units, {s['wall']} "
             f"({'measured' if s['avg_pages_measured'] else 'estimated'} "
             f"{s['avg_pages_per_video']} pages/video, "
             f"{'measured' if s['sec_per_video_measured'] else 'estimated'} "
             f"{s['sec_per_video']}s/video)")
    if s["videos_tracked"] > 0:
        hits_note = f"; first hits in {rep['first_hits_eta']}"
    else:
        # No videos tracked: there can be no hits. Say so instead of
        # printing a time-to-first-hit for a sweep that finds nothing.
        hits_note = "; no videos tracked — run discover first (no hits possible)"
    L.append(f"  total: ~{rep['total_units_expected']} units "
             f"(worst ~{rep['total_units_worst']}), {rep['total_wall']}{hits_note}")
    b = rep["budget"]
    L.append(f"  budget: {b['remaining_today']}/{b['daily_cap']} left today "
             f"({'fits' if b['fits'] else 'DOES NOT FIT'})")
    if not b["shared_ledger_ok"]:
        L.append("  budget warning: shared quota ledger unavailable — "
                 "accounting is per-database only right now")
    elif b["other_db_spent_today"] > 0:
        L.append(f"  note: {b['other_db_spent_today']} units spent today by "
                 f"other databases on this credential")
    if b["cap_note"]:
        L.append(f"  note: {b['cap_note']}")
    if rep["expected_new_hits"] is not None:
        src = "measured" if rep["hit_rate_measured"] else "estimated"
        L.append(f"  expected new hits: ~{rep['expected_new_hits']} ({src} hit rate)")
    else:
        L.append("  hit volume: no measured hit rate yet — first run is a "
                 "probe; the firehose guard stops it if filters prove too "
                 "broad (>=150 hits over >=10 videos at >15/video)")
    L.append(rep["streaming"])
    return "\n".join(L)
