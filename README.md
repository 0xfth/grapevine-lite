# voc-listener Lite

A free library + skill for querying YouTube's comment sections like a live
pulse. Your agent defines a question, sees what it will cost **before**
spending, then sweeps most-promising-first while hits stream live as JSON
lines — killable mid-run with nothing lost.

**This is the free version of voc-listener** (MIT). It runs the full
listening loop with the zero-inference regex scorer: define → estimate →
discover → sweep → inbox. **voc-listener Pro** ($10 one-time) adds the
Clef decision-model scorer, the triage status workflow, and one-command
research-report synthesis:

https://myagentlist.com/skills/voc-listener-pro
<!-- URL SCHEME PENDING DOT CONFIRMATION -->

**This is not a configured product and not a service.** Nobody's niche is
pre-loaded. The value is leverage: `SKILL.md` teaches an agent the query
loop, and the `voc/` package is the reusable machinery (area configs,
pre-spend estimates, budgeted polling, relevance scoring, lossless
watermarks, episode storage, read-only inbox).

## The 30-second version

```bash
python -m voc add-area --name sourdough --db q.db \
  --queries "sourdough starter troubleshooting" \
  --person-terms baker "home baker" --topic-terms sourdough starter \
  --depth shallow
python -m voc estimate --area sourdough --db q.db   # cost first — show the operator
python -m voc query --area sourdough --db q.db --yes --max-new-hits 10
# hits stream as JSONL; first insight in seconds; stop anytime
python -m voc inbox --area sourdough --db q.db      # read the hits in chat
python -m voc fidelity --db q.db   # what queries cost vs what they bought
```

## Design

- **Estimate-first.** Every spend is preceded by a cost estimate (units,
  wall time, time-to-first-hit, remaining budget, expected hits). The
  operator nods before a single unit moves.
- **Streaming.** Hits print as JSON lines the moment they're scored
  (`{"type":"hit"|"progress"|"summary"}`); stderr carries the human
  mirror. The agent watches live and stops early with `--max-new-hits`
  or Ctrl-C.
- **Kill-safe.** Episodes commit per video before their hit line prints.
  A kill mid-run loses nothing; the next sweep resumes losslessly with
  zero duplicates (tested).
- **Most-promising-first.** Sweeps order videos by title-vocabulary match,
  discovery-query strength, then recency — first useful hits in minutes,
  completeness after.
- **Honest accounting.** `fidelity` reports cost vs value from measured
  history; estimates get sharper the more an area is queried.
- **Credentials never touch the library.** API calls go through
  `bin/yt_search.py` / `bin/yt_comments.py`, which use your key at call
  time only — via `--api-key` / `YOUTUBE_API_KEY`, or the Secure Vault
  surrogate on Muse-style agent runtimes.

The always-on daemon model was retired (estimate-first query engine only —
no resident process, no schedule). `references/worked-example.md` walks a
real query session end to end with measured numbers.

## Lite vs Pro

| | Lite (free, MIT) | Pro ($10 one-time) |
|---|---|---|
| Listening loop (define → estimate → discover → sweep → inbox) | ✅ | ✅ |
| Regex vocabulary scorer | ✅ | ✅ |
| grep / diagnose / videos / simulate / fidelity | ✅ | ✅ |
| BYOK (your YouTube key, never stored) | ✅ | ✅ |
| Clef decision-model scorer | — | ✅ |
| Triage status workflow | — | ✅ |
| One-command research-report synthesis | — | ✅ |

**Lite listens. Pro works:**
https://myagentlist.com/skills/voc-listener-pro
<!-- URL SCHEME PENDING DOT CONFIRMATION -->

## Auth

Get a free YouTube Data API v3 key (Google Cloud Console) and hand it to
the CLIs via `--api-key` or the `YOUTUBE_API_KEY` environment variable —
used at call time only, never stored or logged. (On Muse-style agent
runtimes the CLIs can instead use the Secure Vault surrogate; see
`voc/auth.py`.) The library never persists a key value.
