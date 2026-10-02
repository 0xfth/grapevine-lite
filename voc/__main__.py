"""voc entry points: the query-engine surface.

    python -m voc add-area --name sourdough --queries q1 q2 \\
        --person-terms baker "home baker" --topic-terms sourdough starter \\
        --depth shallow --db episodes.db
    python -m voc estimate --area sourdough --db episodes.db
    python -m voc query --area sourdough --db episodes.db [--yes]
    python -m voc discover --area sourdough --db episodes.db
    python -m voc sweep --area sourdough --db episodes.db \\
        [--max-units 500] [--max-new-hits 10] [--format jsonl|human|quiet] \\
        [--match REGEX] [--max-hits-per-video 20]
    python -m voc fidelity --db episodes.db   # what queries cost vs bought
    python -m voc inbox --area sourdough --db episodes.db
    python -m voc grep --area sourdough --db episodes.db --pattern "regret|overpaid"
    python -m voc diagnose --area sourdough --db episodes.db   # free vocab check
    python -m voc videos --area sourdough --db episodes.db    # free title list
    python -m voc simulate [--db sim.db]    # zero-spend dry run on canned data

Streaming: sweep/query print JSON lines on stdout as hits are found
(type=hit|progress|summary, line-buffered) and a human mirror on stderr.
Kill the run anytime (Ctrl-C): episodes commit per video, so everything
already printed is safe, and a partial summary still prints.

Exit codes: 0 ok (a clean quota stop or an early hit-target stop is 0 —
the summary says stopped_early/interrupted), 2 usage error, 3 API error
(message on stderr, no traceback), 4 credential missing/broken (stop,
tell the operator).
"""
import argparse
import json
import os
import signal
import sqlite3
import sys

from .auth import ApiError, CredentialError
from .diagnose import diagnose as _diagnose_area, render as _render_diagnose
from .fidelity import fidelity_report, render as _render_fidelity
from .grep import grep_store, render_match
from .listener import Listener, rank_videos
from .plan import estimate as _estimate, render_estimate as _render_estimate
from .quota import LedgerUnavailable
from .quota import DEFAULT_DAILY_CAP
from .scoring import compile_vocab_regex, match_terms
from .store import STATUSES, Store
from .stream import _emit, _note, emit_hit, emit_prescore, emit_progress, emit_summary
from .triage import format_episode, inbox


def _term_to_interrupt(signum, frame):
    """An agent killing the run gets the same clean partial summary as
    Ctrl-C: episodes already committed, summary still prints."""
    raise KeyboardInterrupt()


def _add_area(a):
    p = a.add_parser("add-area", help="define a question area")
    p.add_argument("--name", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--queries", nargs="*", default=[])
    p.add_argument("--person-terms", nargs="*", default=[])
    p.add_argument("--topic-terms", nargs="*", default=[])
    p.add_argument("--threshold", type=int, default=40)
    p.add_argument("--cadence-hours", type=float, default=8)
    p.add_argument("--depth", choices=["deep", "shallow"], default="shallow")


def _steer(a):
    # Legacy alias, hidden from help: the daemon model it served was
    # abandoned in favor of the on-demand query engine (estimate/query/
    # sweep per area). Still works, but prints a deprecation pointer.
    p = a.add_parser("steer", help=argparse.SUPPRESS)
    p.add_argument("--db", required=True)
    p.add_argument("weights", nargs="+", help="NAME=WEIGHT ...")


def _estimate_cmd(a):
    p = a.add_parser("estimate", help="what will this cost? (no spend)")
    p.add_argument("--area", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--daily-cap", type=int, default=DEFAULT_DAILY_CAP)
    p.add_argument("--no-discover", action="store_true",
                   help="estimate the sweep only (videos already seeded)")
    p.add_argument("--max-units", type=int, default=None)


def _query(a):
    p = a.add_parser("query", help="estimate, then (with --yes) discover + "
                                  "sweep with live streaming")
    p.add_argument("--area", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--daily-cap", type=int, default=DEFAULT_DAILY_CAP)
    p.add_argument("--yes", action="store_true",
                   help="spend the budget: run discover + sweep after printing "
                        "the estimate")
    p.add_argument("--max-units", type=int, default=None,
                   help="total unit cap for discover + sweep COMBINED: "
                        "discover spends against the whole cap first, the "
                        "sweep gets only the unspent remainder (cap "
                        "exhausted by discover = zero-spend sweep)")
    p.add_argument("--max-new-hits", type=int, default=50,
                   help="stop the sweep after this many new hits "
                        "(default 50: the tool finds things, it doesn't "
                        "firehose; raise explicitly for bulk passes)")
    p.add_argument("--max-hits-per-video", type=int, default=20,
                   help="per-video stream cap (default 20): every hit is "
                        "saved, only the first N per video stream live")
    p.add_argument("--match", default=None, metavar="REGEX",
                   help="grep mode: regex replacing the vocabulary gate — "
                        "every thread it matches becomes a hit. Boolean: "
                        "(a|b) OR, (?=.*a)(?=.*b) AND, \\bterm\\b whole word; "
                        "values starting with - need the --match=REGEX form")
    p.add_argument("--format", choices=["jsonl", "human", "quiet"],
                   default="jsonl")


def _discover(a):
    p = a.add_parser("discover", help="seed/refresh tracked videos")
    p.add_argument("--area", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--daily-cap", type=int, default=DEFAULT_DAILY_CAP)
    p.add_argument("--max-units", type=int, default=None)


def _sweep(a):
    p = a.add_parser("sweep", help="poll now: most-promising-first, hits stream live")
    p.add_argument("--area", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--daily-cap", type=int, default=DEFAULT_DAILY_CAP)
    p.add_argument("--max-units", type=int, default=None,
                   help="unit cap; default = remaining daily budget")
    p.add_argument("--max-new-hits", type=int, default=50,
                   help="stop early after this many new hits (default 50: "
                        "the tool finds things, it doesn't firehose; raise "
                        "explicitly for bulk passes)")
    p.add_argument("--max-hits-per-video", type=int, default=20,
                   help="per-video stream cap (default 20): every hit is "
                        "saved, only the first N per video stream live")
    p.add_argument("--match", default=None, metavar="REGEX",
                   help="grep mode: regex replacing the vocabulary gate — "
                        "every thread it matches becomes a hit. Boolean: "
                        "(a|b) OR, (?=.*a)(?=.*b) AND, \\bterm\\b whole word; "
                        "values starting with - need the --match=REGEX form")
    p.add_argument("--order", choices=["promising", "stalest"],
                   default="promising")
    p.add_argument("--format", choices=["jsonl", "human", "quiet"],
                   default="jsonl")


def _diagnose(a):
    p = a.add_parser("diagnose", help="why is this area finding nothing (or "
                                      "the wrong things)? free vocabulary "
                                      "diagnostics over stored threads")
    p.add_argument("--area", required=True)
    p.add_argument("--db", required=True)


def _videos(a):
    p = a.add_parser("videos", help="list tracked videos (free): titles, "
                                    "source query, last poll — sanity-check "
                                    "discovery before spending on a sweep")
    p.add_argument("--area", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--unpolled", action="store_true",
                   help="only videos never polled")
    p.add_argument("--detail", action="store_true",
                   help="why each video ranks where it does: title-vocabulary "
                        "relevance (which terms matched), discovery-query "
                        "position, in most-promising-first order")


def _grep(a):
    p = a.add_parser("grep", help="regex search over stored threads (free): "
                                  "grep for comments. exit 0 on match, 1 on "
                                  "no match")
    p.add_argument("--area", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--pattern", default=None, metavar="REGEX",
                   help="regex to search (default: the area's vocabulary "
                        "compiled to its equivalent regex); values starting "
                        "with - need the --pattern=REGEX form")
    p.add_argument("--print-regex", action="store_true",
                   help="print the area's vocabulary as its equivalent "
                        "regex and exit")
    p.add_argument("--case-sensitive", action="store_true")
    p.add_argument("--status", default=None,
                   help="comma-separated statuses to search "
                        "(default: all)")
    p.add_argument("--count", action="store_true",
                   help="print match counts instead of matches")
    p.add_argument("--limit", type=int, default=100)


def _simulate(a):
    p = a.add_parser("simulate", help="dry-run discover+sweep end-to-end on "
                                      "canned data: zero API spend, zero "
                                      "network, real code paths")
    p.add_argument("--area", default="demo")
    p.add_argument("--db", default=None,
                   help="where to keep the rehearsal DB (default: a new "
                        "temp file, kept for inspection)")
    p.add_argument("--max-units", type=int, default=400)
    p.add_argument("--max-new-hits", type=int, default=10,
                   help="stop the simulated sweep after this many new hits")


def _fidelity(a):
    p = a.add_parser("fidelity", help="what queries cost vs what they bought")
    p.add_argument("--db", required=True)
    p.add_argument("--daily-cap", type=int, default=DEFAULT_DAILY_CAP)


def _inbox(a):
    p = a.add_parser("inbox", help="show unreviewed episodes for triage")
    p.add_argument("--area", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--verbose", action="store_true")


def _wire_streaming(fmt):
    """Return (on_hit, on_progress, on_prescore) callbacks for the chosen format."""
    quiet = fmt == "quiet"
    if fmt == "human":
        # human: narrative to stderr only; stdout gets just the summary
        def on_hit(ep):
            t = (ep.get("top_text") or "").replace("\n", " ")[:200]
            d = (ep.get("score_breakdown") or {})
            direction = d.get("direction", "?") if isinstance(d, dict) else "?"
            _note(f"  HIT score={ep.get('score')} [{direction}] "
                  f"[{(ep.get('top_published_at') or '')[:10]}] {ep.get('url')}\n"
                  f"      {t}")
        def on_progress(r, totals):
            _note(f"[{totals['videos_done']}/{totals['videos_total']}] "
                  f"{r['video_id']} — {r['pages']}p, {r['episodes_new']} new "
                  f"({totals['total_hits']} total)")
        def on_prescore(p):
            # stdout stays machine-only (summary); spend events go to stderr.
            est = p["clef_estimate"]
            _note(f"  scoring {p['video_id']}: {p['threads_seen']} threads — "
                  f"clef estimate ~{est['est_input_tokens']} input tokens "
                  f"before any are spent")
        return on_hit, on_progress, on_prescore
    def on_hit(ep):
        emit_hit(ep, quiet=quiet)
    def on_progress(r, totals):
        emit_progress(totals["videos_done"], totals["videos_total"],
                      r["video_id"], r["pages"], r["episodes_new"],
                      totals["total_hits"], quiet=quiet,
                      clef_estimate=r.get("clef_estimate"))
    def on_prescore(p):
        emit_prescore(p["video_id"], p["threads_seen"], p["clef_estimate"],
                      quiet=quiet)
    return on_hit, on_progress, on_prescore


def _run_sweep(listener, args, max_units=None):
    """Run the sweep phase. max_units overrides args.max_units when given —
    the query command passes the cap remainder left after discover."""
    on_hit, on_progress, on_prescore = _wire_streaming(args.format)
    if max_units is None:
        max_units = args.max_units
    rep = listener.sweep(max_units=max_units,
                         max_new_hits=args.max_new_hits,
                         max_hits_per_video=args.max_hits_per_video,
                         match=args.match,
                         on_hit=on_hit, on_progress=on_progress,
                         on_prescore=on_prescore)
    tail_bits = []
    if rep.get("match"):
        tail_bits.append(f"grep mode: {rep['match']!r}")
    if rep.get("resumed_from_cursor"):
        tail_bits.append("resumed where the last sweep stopped")
    if rep.get("hits_saved_not_streamed"):
        tail_bits.append(f"{rep['hits_saved_not_streamed']} further hits "
                         f"saved but not streamed (per-video cap "
                         f"{args.max_hits_per_video})")
    if rep.get("interrupted"):
        tail_bits.append("INTERRUPTED — partial results kept")
    elif rep.get("stopped_early"):
        tail_bits.append("stopped early")
    tail = (" (" + "; ".join(tail_bits) + ")") if tail_bits else ""
    if args.format == "human":
        # stdout: machine-readable summary only; the story went to stderr
        _emit({"type": "summary", **rep})
        _note(f"done: {rep['videos_polled']} videos, {rep['units_spent']} units, "
              f"{rep['episodes_new']} new hits{tail}")
    else:
        emit_summary(rep, quiet=(args.format == "quiet"))
        if tail_bits and args.format != "quiet":
            _note("note: " + "; ".join(tail_bits))
    return rep


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m voc",
                                 description="query YouTube's collective pulse")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for fn in (_add_area, _steer, _estimate_cmd, _query, _discover, _sweep,
               _fidelity, _inbox, _diagnose, _videos, _grep, _simulate):
        fn(sub)
    args = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, _term_to_interrupt)

    try:
        if args.cmd == "add-area":
            store = Store(args.db)
            try:
                store.add_area(args.name, queries=args.queries,
                               person_terms=args.person_terms,
                               topic_terms=args.topic_terms,
                               threshold=args.threshold,
                               cadence_hours=args.cadence_hours,
                               depth=args.depth)
            except sqlite3.IntegrityError:
                raise ValueError(f"area '{args.name}' already exists")
            print(json.dumps({"ok": True, "area": args.name,
                              "next": "run `estimate`, show it to the operator, "
                                      "then discover/sweep"}))
            store.close()
        elif args.cmd == "steer":
            print("warning: `steer` is deprecated — the daemon model it "
                  "served was abandoned. The query engine spends per area "
                  "directly: `estimate` / `query` / `sweep --max-units` with "
                  "the operator's nod. `steer` still runs for now.",
                  file=sys.stderr)
            store = Store(args.db)
            weights = {}
            for w in args.weights:
                name, _, val = w.partition("=")
                weights[name] = float(val)
            payload = store.steer(weights)
            print(json.dumps({"ok": True, "steering": payload["weights"],
                              "note": "optional per-day budget split; "
                                      "single-area queries ignore it"}))
            store.close()
        elif args.cmd == "estimate":
            store = Store(args.db)
            rep = _estimate(store, args.area,
                            include_discover=not args.no_discover,
                            max_units=args.max_units,
                            daily_cap=args.daily_cap)
            print(_render_estimate(rep))
            store.close()
        elif args.cmd == "query":
            store = Store(args.db)
            listener = Listener(args.area, store, daily_cap=args.daily_cap)
            if args.match is not None:
                # A bad pattern is a usage error: fail BEFORE the estimate
                # the operator is about to approve, and long before any
                # unit is spent (discover used to bill first, then die).
                from .grep import compile_pattern
                try:
                    compile_pattern(args.match)
                except ValueError as e:
                    raise ValueError(f"--match: {e}")
            rep = listener.estimate(max_units=args.max_units)
            print(_render_estimate(rep), flush=True)
            if not args.yes:
                ignored = [f for f, default in
                           (("--max-new-hits", 50), ("--format", "jsonl"),
                            ("--max-hits-per-video", 20))
                           if getattr(args, f[2:].replace("-", "_")) != default]
                if ignored:
                    print(f"note: {', '.join(ignored)} only affect the "
                          "sweep; ignored without --yes", file=sys.stderr)
                print(json.dumps({"ok": True, "spent": 0,
                                  "next": "re-run with --yes to spend the "
                                          "budget above"}))
                store.close()
                return
            _note("spending: discover, then sweep (Ctrl-C anytime — "
                  "everything found is kept)")
            # --max-units caps discover + sweep COMBINED: discover spends
            # against the whole cap, the sweep gets only the unspent
            # remainder. (Previously discover ran uncapped and only the
            # sweep saw the cap.)
            d = listener.discover(max_units=args.max_units)
            _note(f"discover: {d['videos_added']} new videos, "
                  f"{d['units_spent']} units"
                  + (" (stopped early)" if d["stopped_early"] else "")
                  + (" (interrupted)" if d.get("interrupted") else ""))
            if d.get("interrupted"):
                _note("interrupted during discover — videos found so far "
                      "are kept; re-run to continue")
                store.close()
                return
            sweep_cap = None
            if args.max_units is not None:
                sweep_cap = max(0, args.max_units - d["units_spent"])
                if sweep_cap == 0:
                    _note(f"--max-units {args.max_units} was fully spent by "
                          f"discover ({d['units_spent']} units): the sweep "
                          f"runs with a zero cap and spends nothing")
            _run_sweep(listener, args, max_units=sweep_cap)
            store.close()
        elif args.cmd == "discover":
            store = Store(args.db)
            rep = Listener(args.area, store,
                           daily_cap=args.daily_cap).discover(max_units=args.max_units)
            print(json.dumps(rep, indent=1))
            store.close()
        elif args.cmd == "sweep":
            store = Store(args.db)
            listener = Listener(args.area, store, daily_cap=args.daily_cap)
            _run_sweep(listener, args)
            store.close()
        elif args.cmd == "fidelity":
            store = Store(args.db)
            rep = fidelity_report(store, args.daily_cap)
            print(_render_fidelity(rep))
            store.close()
        elif args.cmd == "inbox":
            store = Store(args.db)
            for ep in inbox(store, args.area, limit=args.limit):
                print(format_episode(ep, verbose=args.verbose))
                print("---")
            store.close()
        elif args.cmd == "diagnose":
            store = Store(args.db)
            print(_render_diagnose(_diagnose_area(store, args.area)))
            store.close()
        elif args.cmd == "videos":
            store = Store(args.db)
            vids = store.videos(args.area)
            if args.unpolled:
                vids = [v for v in vids if not v["last_polled"]]
            vids = vids[:args.limit]
            if args.detail:
                cfg = store.get_area(args.area)
                ordered = rank_videos(vids, cfg)
                queries = cfg.get("queries", []) or []
                print(f"{len(ordered)} tracked videos — area '{args.area}' "
                      f"(most-promising-first order)")
                for rank, v in enumerate(ordered, 1):
                    ph, th = match_terms(v.get("title", ""),
                                         cfg.get("person_terms", ()),
                                         cfg.get("topic_terms", ()))
                    rel = 2 * len(ph) + len(th)
                    dq = v.get("discovery_query") or "?"
                    qpos = (queries.index(dq) if dq in queries else "?")
                    polled = (v["last_polled"] or "")[:10] or "never"
                    pub = (v.get("published_at") or "")[:10] or "?"
                    print(f"  #{rank} {v['video_id']}  rel={rel} "
                          f"(person:{','.join(ph) or '—'} "
                          f"topic:{','.join(th) or '—'}) "
                          f"query[{qpos}]={dq[:40]!r} polled:{polled} pub:{pub}")
                    print(f"       {v['title'][:100]}")
            else:
                print(f"{len(vids)} tracked videos — area '{args.area}'"
                      + (" (never polled)" if args.unpolled else ""))
                for v in vids:
                    polled = (v["last_polled"] or "")[:10] or "never"
                    pub = (v.get("published_at") or "")[:10] or "?"
                    print(f"  {v['video_id']}  polled:{polled} pub:{pub} "
                          f"[{v['discovery_query']}] {v['title'][:80]}")
            store.close()
        elif args.cmd == "simulate":
            from tempfile import mkstemp
            from .sim import simulate_query_engine
            db = args.db
            if db is None:
                fd, db = mkstemp(prefix="voc_sim_", suffix=".db")
                os.close(fd)
            try:
                rep = simulate_query_engine(db, area=args.area,
                                            max_units=args.max_units,
                                            max_new_hits=args.max_new_hits)
            except AssertionError as e:
                # zero-spend proof failed: loud failure, never silent
                print(f"simulate FAILED: {e}", file=sys.stderr)
                sys.exit(3)
            print(json.dumps(rep, indent=1))
        elif args.cmd == "grep":
            store = Store(args.db)
            if args.print_regex:
                cfg = store.get_area(args.area)
                print(compile_vocab_regex(cfg["person_terms"],
                                          cfg["topic_terms"]))
                store.close()
                return
            statuses = None
            if args.status:
                statuses = [s.strip() for s in args.status.split(",")]
                bad = [s for s in statuses if s not in STATUSES]
                if bad:
                    raise ValueError(f"unknown status(es): {bad}")
            try:
                pat, matches = grep_store(store, args.area,
                                          pattern=args.pattern,
                                          case_sensitive=args.case_sensitive,
                                          statuses=statuses, limit=args.limit)
            except ValueError as e:
                raise ValueError(str(e))
            if args.count:
                by_status = {}
                for m in matches:
                    by_status[m["status"]] = by_status.get(m["status"], 0) + 1
                print(json.dumps({"pattern": pat, "matches": len(matches),
                                  "by_status": by_status}))
            else:
                print(f"pattern: {pat}")
                for m in matches:
                    print(render_match(m))
                    print("---")
            store.close()
            # grep-like exit code: 0 on match, 1 on no match
            sys.exit(0 if matches else 1)
    except CredentialError as e:
        print(f"credential: {e}", file=sys.stderr)
        sys.exit(4)
    except ApiError as e:
        print(f"api error: {e}", file=sys.stderr)
        sys.exit(3)
    except LedgerUnavailable as e:
        print(f"quota ledger: {e}", file=sys.stderr)
        sys.exit(3)
    except (KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
