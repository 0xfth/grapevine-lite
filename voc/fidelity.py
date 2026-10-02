"""Fidelity reframed: what a query cost and what it bought.

No daemon, no attention shares — just honest per-area accounting from the
run history:

- units_spent_today / videos_polled_today / episodes_new_today
- hit_rate: measured new-hits-per-video-polled (7d), the number the
  estimate's expected-hits line is built from
- avg_pages_per_video: measured, feeds the next estimate
- thread_completeness: fraction of recent polls that finished untruncated
- last_sweep: the most recent sweep's units/videos/hits (actuals only —
  the estimate is never persisted, so compare against the estimate the
  operator approved)

fidelity_report() is pure stored state: no API calls, safe any time.
cli/db.py's `fidelity` command renders this.
"""
from .quota import (DEFAULT_DAILY_CAP, quota_day, shared_spent_today,
                    credential_scope, LedgerUnavailable)


def fidelity_report(store, daily_cap=DEFAULT_DAILY_CAP, day=None, scope=None):
    """Per-area cost-vs-value. Returns {"day", "daily_cap", "areas": {...}}.

    scope: quota-owner identity used to consult the shared ledger. Pass
    the requester's scope (e.g. the "key:<sha256>" fingerprint from
    with_api_key()) so the report reflects spend by THAT credential —
    without it the ledger falls back to the VOC_CREDENTIAL default, which
    silently attributes spend to the wrong scope for explicit-key flows.
    """
    day = day or quota_day()
    areas = {}
    for a in store.areas():
        name = a["name"]
        nvideos = len(store.videos(name))
        spent = store.usage_today(name, day=day)
        # Aggregate kind="sweep" rows: today's sweeps. (Per-video kind="poll"
        # rows feed the measured rates below, not these sums.)
        today = [r for r in store.day_runs(name, day) if r["kind"] == "sweep"]
        runs = store.last_runs(area=name, limit=50)
        videos_today = sum(r["videos_polled"] for r in today)
        hits_today = sum(r["episodes_new"] for r in today)
        hit_rate, hr_est = store.avg_new_hits_per_poll(name)
        avg_pages, pg_est = store.avg_pages_per_video(name)
        completeness = store.thread_completeness(name)
        last = next((r for r in runs if r["kind"] == "sweep"), None)
        areas[name] = {
            "videos_tracked": nvideos,
            "depth": a["depth"],
            "units_spent_today": spent,
            "sweeps_today": len(today),
            "videos_polled_today": videos_today,
            "new_hits_today": hits_today,
            "hit_rate_per_video": round(hit_rate, 3) if hit_rate is not None else None,
            "hit_rate_measured": not hr_est if hit_rate is not None else False,
            "avg_pages_per_video": round(avg_pages, 2) if avg_pages is not None else None,
            "pages_measured": not pg_est if avg_pages is not None else False,
            "thread_completeness": (round(completeness, 3)
                                    if completeness is not None else None),
            "last_sweep": ({
                "day": last["day"],
                "units": last["units_spent"],
                "videos": last["videos_polled"],
                "hits": last["episodes_new"],
                "stopped_early": bool(last["stopped_early"]),
            } if last else None),
        }
    spent_all = store.usage_today(None, day=day)
    # Cross-database truth: the shared ledger sees every database on this
    # machine sharing the credential. The ledger starts empty on upgrade;
    # max() keeps pre-ledger local history authoritative until it catches up.
    scope = scope or credential_scope()
    try:
        shared = shared_spent_today(day, scope)
        ledger_ok = True
    except LedgerUnavailable:
        # Read-only report: degrade with the flag set, never a hard error.
        # Spend paths fail closed instead.
        shared, ledger_ok = None, False
    spent_all = max(spent_all, shared) if ledger_ok else spent_all
    other_db = max(0, shared - store.usage_today(None, day=day)) if ledger_ok else 0
    return {
        "day": day,
        "daily_cap": daily_cap,
        "scope": scope,
        "spent_today_all_areas": spent_all,
        "remaining_today": max(0, daily_cap - spent_all),
        "shared_ledger_ok": ledger_ok,
        "other_db_spent_today": other_db,
        "areas": areas,
    }


def render(report):
    """One-page plain-text rendering for chat / the operator."""
    lines = [f"Cost vs value — {report['day']}: "
             f"{report['spent_today_all_areas']}/{report['daily_cap']} units spent "
             f"({report['remaining_today']} left)"]
    if not report["shared_ledger_ok"]:
        lines.append("  warning: shared quota ledger unavailable — "
                     "per-database accounting only")
    elif report["other_db_spent_today"] > 0:
        lines.append(f"  includes {report['other_db_spent_today']}u spent today by "
                     f"other databases on this credential")
    for name, a in report["areas"].items():
        hr = (f"{a['hit_rate_per_video']}/video"
              if a["hit_rate_per_video"] is not None
              else "unmeasured") + ("" if a["hit_rate_measured"] else " (est.)")
        pg = (f"{a['avg_pages_per_video']}pp"
              if a["avg_pages_per_video"] is not None else "?pp")
        comp = (f"{a['thread_completeness']:.0%}"
                if a["thread_completeness"] is not None else "unmeasured")
        lines.append(
            f"- {name}: {a['videos_tracked']} videos @ {a['depth']}, "
            f"today {a['units_spent_today']}u / {a['videos_polled_today']} videos / "
            f"{a['new_hits_today']} hits; hit rate {hr}, {pg}, "
            f"completeness {comp}")
        if a["last_sweep"]:
            ls = a["last_sweep"]
            lines.append(f"    last sweep ({ls['day']}): {ls['units']}u, "
                         f"{ls['videos']} videos, {ls['hits']} hits"
                         + (" (stopped early)" if ls["stopped_early"] else ""))
    return "\n".join(lines)
