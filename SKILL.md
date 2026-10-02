# voc-listener Lite skill

Query the real-time collective consciousness of the internet — ask about any
niche, get scored real-people pain back in minutes. Currently YouTube-only:
search a question, pull comment threads, score them for genuine human
struggle, stream the hits live.

This is the **free version** of voc-listener: the full listening loop with
the zero-inference regex scorer. **voc-listener Pro** ($10 one-time) adds
the Clef decision-model scorer, the triage status workflow, and one-command
research-report synthesis:

https://myagentlist.com/skills/voc-listener-pro
<!-- URL SCHEME PENDING DOT CONFIRMATION -->

This is a **skill for an agent, not a service for others.** You run it for
your operator. Every quota unit you spend is their money — the
**estimate-first rule** is absolute: show the cost before spending it.
Bring your own YouTube Data API v3 key (free from Google Cloud Console) —
the library never stores it.

## The loop (composable, not a pipeline)

Five operations. Use them in any order, skip any, repeat any. There is no
daemon, no schedule, no resident process — you decide when to ask.

1. **define** — `add-area`: name the question. Queries (YouTube search
   terms), `person_terms` (who: matched as whole words), `topic_terms`
   (what: matched as substrings), `threshold` (default 40), `depth`
   (`shallow`: 1 comment page/video, fast; `deep`: up to 10, thorough).
2. **estimate** — `estimate`: what will this cost? Prints units (expected +
   worst case), wall time, time-to-first-hit, remaining budget, expected
   hits from measured hit rate. **Show this to the operator and get a nod
   before spending.** The estimate ends with: *"results stream live as
   JSON lines — stop the run anytime (Ctrl-C); everything already printed
   is saved in the DB, nothing is lost."*
3. **discover** — seed/refresh tracked videos. 100 units per search query
   (YouTube's price, exact); rich queries can pull a 2nd page (+100). Or
   skip the separate step: `query --yes` runs discover + sweep in one go.
4. **sweep** — poll the area's videos **most-promising-first**: titles
   matching the question's vocabulary go first, then the strongest
   discovery queries, then the newest — and at equal relevance,
   never-polled videos sort ahead of already-polled ones. Hits print **the moment they're
   scored** as JSON lines on stdout (human mirror on stderr).
   `--max-new-hits N` stops at the first N hits (default 50 — this tool
   finds things, it doesn't firehose; raise it explicitly for bulk
   passes). `--max-hits-per-video N` (default 20) caps how many hits per
   video stream live — every hit is saved to the DB, the summary says how
   many were held back. Ctrl-C (or killing the run) loses nothing:
   episodes commit per video, a partial summary still prints, and the next
   sweep **resumes positionally** — it continues after the last polled
   video instead of re-ranking from the top, with zero duplicates. A
   firehose guard also stops the run by itself if filters prove too broad
   (sustained 150+ hits over 10+ videos at >15/video means broken
   vocabulary, not a rich niche) — the summary tells you to tighten terms
   or raise the threshold.
5. **inbox** — `inbox` shows unreviewed hits, highest score first. Read
   them in chat and decide what matters — quote the pain, never the
   person (author names stay anonymized). Marking episodes
   seen/handled/dismissed and drafting reports are Pro features.

`fidelity` answers "what did my queries cost and what did they buy":
units spent today, videos polled, hits, measured hit rate, measured pages
per video, thread completeness, last sweep's actuals (units/videos/hits —
the estimate itself isn't persisted, so compare against the estimate you
approved). Run it
anytime; it's free.

## The estimate-first rule

Never spend quota without the operator seeing the estimate first. In chat:

> "Querying 'freelance pricing pain': ~200–400 units for discovery, ~190
> units for the sweep, ~5 minutes, first hits in ~10 seconds. 9,600 units
> left today. Results stream live — stop me anytime. Go?"

Get the nod. Then spend. If the estimate doesn't fit the remaining budget,
say so and offer the partial run or waiting for the midnight-PT reset.

## Streaming contract

`sweep --format jsonl` (default): stdout is JSON lines, one event per line,
line-buffered:

- `{"type": "hit", "score":…, "direction": "question|pain|praise|mixed|mention",
  "published":…, "replies":…, "url":…, "text":…}`
  — self-contained; everything needed to judge it inline.
- `{"type": "progress", "videos_done":…, "videos_total":…, …}`
  — after each video.
- `{"type": "summary", …}` — always prints, even on interrupt or early stop.

stderr carries the human-readable mirror. `--format human` puts the story
on stderr and only the summary JSON on stdout; `--format quiet` prints just
the summary. Parse stdout as JSONL — never rely on stderr. (Pro's clef
mode adds per-video pre-spend `prescore` events; Lite's regex scoring costs
nothing, so there is no scoring spend to preview.)

## Crafting the question (the hard part)

The scorer is dumb; the vocabulary is smart. Spend your effort here:

- `queries`: 2–6 YouTube search phrases the way a real person would type
  them ("how to price freelance work", not "freelance pricing pain points
  analysis"). Specific beats clever.
- `person_terms`: who has the pain — matched as **whole words** (so "add"
  won't match "added"; use the form people actually write).
- `topic_terms`: what it's about — matched as **substrings** ("job"
  matches "jobs").
- A thread scores above the floor only if it has BOTH a person-term AND a
  topic-term, plus quality signals (length, first-person, questions,
  struggle words). Threshold 40 is the default we used; tune per niche.
- `depth`: `shallow` for a first pulse (fast, cheap), `deep` when the
  niche is worth a full pass.

Your vocabulary is a regex in disguise — `grep --print-regex` shows the
equivalent pattern. When the two term lists feel clumsy, skip them and
search like grep instead (next section).

## Scorer: regex (Lite)

One mode: `regex` — zero inference. The person/topic vocabulary gate
compiles to exactly one regex (see `compile_vocab_regex`); no API, no
cost, always available. This is the same scorer Pro uses in its default
mode — Lite just doesn't offer the alternative.

**Pro** adds the `clef` decision-model scorer: Cloudflare reads each
thread and returns probabilities for typed questions (person, topic,
relevance, pain — plus your own custom questions and gates), costing
input tokens only. Same 0-100 score and threshold semantics; smarter
judgment on ambiguous threads; per-run token budget with regex fallback.

## Grep mode: regex over comments

For agents, regex is often easier and more flexible than two term lists —
boolean logic is native: `(a|b)` is OR, `(?=.*a)(?=.*b)` is AND,
`\bterm\b` is a whole word, `(?!.*spam)` is NOT.

- `grep --area NAME --db … --pattern "regret|wish i (had|hadn't)|overpaid"`
  — search stored threads, free, no API calls. Exit 0 on match, 1 on no
  match (like grep). `--count` prints counts instead of matches;
  `--status new,low` filters; `--case-sensitive` opts out of the default
  insensitive matching. Omit `--pattern` to grep with the area's
  vocabulary compiled to its regex — the fastest way to see what your
  terms actually match.
- `sweep --match "REGEX"` (or `query --match`) — live grep: the regex
  replaces the vocabulary gate while polling. Every thread it matches
  becomes a hit (status `new`), regardless of the score threshold; the
  score still measures quality signals so the inbox can rank. A bad pattern
  is a usage error raised before any unit is spent. The firehose guard,
  `--max-new-hits`, and the per-video stream cap all still apply — grep
  doesn't mean firehose.
- Every hit carries a crude `direction` — `question`, `pain`, `praise`,
  `mixed`, or `mention` (word counts, not sentiment analysis) — so you can
  tell "I love this" apart from "I'm stuck" at a glance.

Tuning loop: run a small probe sweep, then `diagnose --area` (free) —
per-term match counts, dead terms flagged, near-miss samples showing which
side of the vocabulary missed, and a verdict on what to change first.
`videos --area` lists tracked titles (free) to sanity-check discovery
(`--detail` adds why each video ranks where it does). `simulate` runs a
zero-spend, zero-network dry run of estimate+discover+sweep on canned data
(real code paths, `DryRunBudget`) so you can rehearse the query engine
before spending.

## Quota facts

- 100 units per search page, ~1 per comment page. Steady-state polls
  early-stop after 1 page/video when nothing is new since the watermark.
- Daily cap default 10,000; day resets at midnight Pacific. `sweep`
  defaults its cap to the remaining daily budget — a run can never
  overspend the day by accident.
- `--max-units N` on `query` caps discover + sweep **combined**: discover
  spends against the whole cap first, the sweep gets only the unspent
  remainder (discover eats the cap → the sweep spends zero and says so).
  Without `--max-units`, discover is uncapped except by the daily cap and
  the sweep defaults to the remaining daily budget — same as running the
  two steps separately.
- Residual risk on `--max-units` (read before promising a number): the cap
  is enforced per API page inside one process — a page is only issued when
  the cap covers it, so a single run cannot overspend its own cap. What it
  does NOT cover: a re-run after an interruption starts a NEW cap (each run
  enforces its own), and two processes on the same key each enforce only
  their own slice (the shared ledger + reservations guard the daily cap,
  not a `--max-units` slice). One spend at a time per key when the number
  matters.
- Cross-database ledger (`~/.local/share/voc/quota-ledger.db`): every
  spend is recorded under the credential's scope (vault credential name,
  or a key fingerprint for `with_api_key`), so two databases on one key
  share one cap on this machine. Spending is reserve → API call →
  reconcile; the reservation is atomic across processes, so two
  concurrent sweeps can no longer both pass the check and overshoot.
- Fail closed: if the shared ledger is missing, unreadable, or corrupt,
  the run raises `LedgerUnavailable` and spends nothing — it never falls
  back to silent per-database accounting. Fix the ledger (disk space,
  permissions, stale -wal/-shm files, or XDG_DATA_HOME) and retry.
- Bursts pace themselves through the vault CLI subprocesses (~1s/page
  measured). No token bucket, no daemon.

## Auth (you never touch keys)

You need a YouTube Data API v3 key (free from Google Cloud Console).
Hand it to the CLIs one of two ways:

1. **API key (default):** pass `--api-key <KEY>` to `bin/yt_search.py` /
   `bin/yt_comments.py`, or export `YOUTUBE_API_KEY`. The key is used at
   call time only — never stored, logged, or persisted by the library.
2. **Secure Vault surrogate** (Muse-style agent runtimes): when no API key
   is given, the CLIs attach the operator's vault credential as a surrogate
   at request time. You never see, store, or log a key.

If a call fails with `credential:`, stop and tell the operator to fix their
key — never retry-loop. Programmatic alternative: inject a key at call time
with `voc.auth.with_api_key(key)` (the key stays in memory, never on disk).

## Multiple questions

**Deprecated:** `steer` (hidden from help) split one day's budget across
areas for the abandoned daemon model; it still runs but prints a
deprecation pointer. The query engine spends per area directly —
`estimate`, then `query --yes` / `sweep --max-units` with the operator's
nod — no steering needed. Single-question use never needed it.

## Extension points

- Custom scorer: any function with `score(thread) -> {"score", "breakdown"}`
  plugs into `Listener.poll_video`. The default is YouTube-comment-shaped;
  bring your own for other shapes.
- Custom requester: `Listener(area, store, requester=...)` — the sim's
  `FakeRequester` lets you dry-run everything with zero spend.

## Lite vs Pro

| | Lite (free, MIT) | Pro ($10 one-time) |
|---|---|---|
| Listening loop (define → estimate → discover → sweep → inbox) | ✅ | ✅ |
| Regex vocabulary scorer | ✅ | ✅ |
| grep / diagnose / videos / simulate / fidelity | ✅ | ✅ |
| BYOK (your YouTube key, never stored) | ✅ | ✅ |
| Clef decision-model scorer | — | ✅ |
| Triage status workflow (seen/handled/dismissed/starred) | — | ✅ |
| One-command research-report synthesis | — | ✅ |

**Lite listens. Pro works.** When the hits start mattering and you want
smarter scoring, a real work queue, and draft reports:

https://myagentlist.com/skills/voc-listener-pro
<!-- URL SCHEME PENDING DOT CONFIRMATION -->

## Honest limits

- YouTube-only. Reddit/forums/IRC are not feeds here (the schema has a
  `source` column for the future).
- Single-operator installs. Hosted/multi-tenant is a separate project.
- One machine, one ledger: the shared quota ledger is local to this
  machine. The same API key spending from a second machine is invisible
  to it, and no local code can fix that — YouTube offers no API to read
  current quota usage, so the ledger can only count what this machine
  spent. If one key serves several machines, split the daily cap by hand:
  give each machine its own budget (e.g. `--daily-cap 5000` on each of
  two machines) so the two ledgers can't sum past the real quota.
- The scorer finds *plausible pain*, not verified truth.
- Kill threshold for any engagement built on this data: fewer than 5
  genuinely relevant conversations in 2 weeks means the niche isn't
  talking — stop spending on it.
