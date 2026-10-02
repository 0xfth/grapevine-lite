"""Streaming output: JSON lines on stdout, human narrative on stderr.

The query-engine contract: every scored hit prints THE MOMENT it is
processed — line-buffered, self-contained per line. The agent running the
tool watches stdout live and kills the run early once it has what it needs.
A kill loses nothing already printed: episodes commit to SQLite per video,
before their hit line is emitted.

Event types on stdout (one JSON object per line, flush=True):
  {"type": "hit", ...}       — a new inbox-worthy episode, full context inline
  {"type": "prescore", ...}  — per-video, BEFORE scoring: thread count and the
                               decision-model pre-spend estimate. The operator
                               sees the price before a single token is spent.
  {"type": "progress", ...}  — per-video progress (videos done/total, hits so far)
  {"type": "summary", ...}   — final (or partial, on interrupt) run report

stderr carries the human-readable mirror. --format quiet suppresses hit
and progress lines; the summary always prints.
"""
import json
import sys


def _emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _note(msg):
    print(msg, file=sys.stderr, flush=True)


def _direction_of(ep):
    d = ep.get("score_breakdown") or {}
    return d.get("direction", "?") if isinstance(d, dict) else "?"


def hit_event(ep):
    """One scored hit, self-contained: everything needed to judge it inline."""
    return {
        "type": "hit",
        "id": ep.get("id"),
        "area": ep.get("area"),
        "score": ep.get("score"),
        "direction": _direction_of(ep),
        "published": (ep.get("top_published_at") or "")[:10],
        "replies": ep.get("reply_count", 0),
        "url": ep.get("url"),
        "text": (ep.get("top_text") or "")[:600],
    }


def progress_event(done, total, video_id, pages, new_hits, total_hits,
                   stopped_early=False, clef_estimate=None):
    ev = {
        "type": "progress",
        "videos_done": done,
        "videos_total": total,
        "video_id": video_id,
        "pages": pages,
        "new_hits_this_video": new_hits,
        "total_hits": total_hits,
        "stopped_early": stopped_early,
    }
    # Pre-spend decision-model estimate, when this video scores with clef.
    if clef_estimate is not None:
        ev["clef_estimate"] = clef_estimate
    return ev


def summary_event(report):
    return {"type": "summary", **report}


def prescore_event(video_id, threads_seen, clef_estimate):
    """Fires BEFORE any decision-model token is spent on this video.

    clef_estimate is the estimate_run_cost() dict (threads, est_input_tokens,
    questions, max_chars, gated). None only if the scorer isn't clef — the
    event itself is only emitted for clef-mode videos about to be scored.
    """
    return {
        "type": "prescore",
        "video_id": video_id,
        "threads_seen": threads_seen,
        "clef_estimate": clef_estimate,
    }


def emit_hit(ep, quiet=False):
    _emit(hit_event(ep))
    if not quiet:
        t = (ep.get("top_text") or "").replace("\n", " ")[:160]
        _note(f"  HIT score={ep.get('score')} [{_direction_of(ep)}] "
              f"[{(ep.get('top_published_at') or '')[:10]}] "
              f"{ep.get('url')}\n      {t}")


def emit_progress(done, total, video_id, pages, new_hits, total_hits,
                  quiet=False, clef_estimate=None):
    _emit(progress_event(done, total, video_id, pages, new_hits, total_hits,
                         clef_estimate=clef_estimate))
    if not quiet:
        clef_bit = (f" [clef: {clef_estimate['threads']} threads, "
                    f"~{clef_estimate['est_input_tokens']} tokens]"
                    if clef_estimate else "")
        _note(f"[{done}/{total}] {video_id} — {pages}p, "
              f"{new_hits} new ({total_hits} total hits){clef_bit}")


def emit_prescore(video_id, threads_seen, clef_estimate, quiet=False):
    """The spend event: always a JSON line (costs are part of the
    interface), human mirror on stderr unless quiet."""
    _emit(prescore_event(video_id, threads_seen, clef_estimate))
    if not quiet:
        _note(f"  scoring {video_id}: {threads_seen} threads, "
              f"clef estimate ~{clef_estimate['est_input_tokens']} input tokens "
              f"({clef_estimate['threads']} model calls planned)")


def emit_summary(report, quiet=False):
    _emit(summary_event(report))
    if not quiet:
        r = report
        _note(f"done: {r.get('videos_polled', 0)} videos, "
              f"{r.get('units_spent', 0)} units, "
              f"{r.get('episodes_new', 0)} new hits"
              + (" (INTERRUPTED — partial results kept)"
                 if r.get("interrupted") else "")
              + (" (stopped early)" if r.get("stopped_early") else ""))
