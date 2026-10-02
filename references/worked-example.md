# Worked example: a query-engine session

A real session from 2026-10-01 (fresh DB, real YouTube API). The operator
asked: *"what do freelancers struggle with around pricing?"* — and wanted
the live pulse, fast.

## 1. Define the question

```bash
python -m voc add-area --name freelance-pricing --db q.db \
  --queries "how to price freelance work" "freelance client red flags" \
  --person-terms freelancer client \
  --topic-terms freelance pricing portfolio \
  --depth shallow
```

Two plain-phrased queries, small vocabulary. Shallow depth: this is a first
pulse, not a full pass.

## 2. Estimate — show the operator, get the nod

```bash
python -m voc estimate --area freelance-pricing --db q.db
```

```
Query estimate — area 'freelance-pricing'
  discover: 2 queries → 200–400 units, ~4s
  sweep: 0 videos (0 never polled) @ shallow → 0–0 units, ~1s (estimated 1.0 pages/video)
  total: ~200 units (worst ~400), ~5s; first hits in ~8s
  budget: 10000/10000 left today (fits)
results stream live as JSON lines — stop the run anytime (Ctrl-C); everything already printed is saved in the DB, nothing is lost
```

In chat: *"~200–400 units for discovery, sweep cost once we see how many
videos land, ~5 minutes wall, first hits in ~10 seconds. 10,000 units left
today. Results stream live — stop me anytime. Go?"* Operator nods.

## 3. Discover — seed the videos

```bash
python -m voc discover --area freelance-pricing --db q.db
# {"queries_run": 2, "videos_added": 187, "units_spent": 400}  (~7s)
```

Both queries pulled a 2nd page — 400 units, exactly the estimate's worst
case. Re-estimate now prices the sweep honestly:

```
  sweep: 187 videos (187 never polled) @ shallow → 187–187 units, ~4m, first hits in ~8s
```

## 4. Sweep — hits stream live, stop early

```bash
python -m voc sweep --area freelance-pricing --db q.db --max-new-hits 12
```

stdout (JSON lines, line-buffered) — the first hit lands ~9 seconds in,
on the 9th video polled (most-promising-first ordering):

```json
{"type": "hit", "id": 87, "score": 46, "published": "2023-07-21", "replies": 2,
 "url": "https://www.youtube.com/watch?v=iL-IwyKJs9w&lc=UgzNX5GhKaPk5q5l8El4AaABAg",
 "text": "Hi! I want ur help. I have got one ui design project where i have to design a website so according to you how much should i ask to them to pay me for my first project as a freelancer…"}
{"type": "progress", "videos_done": 10, "videos_total": 187, "video_id": "…", "pages": 1, "new_hits_this_video": 1, "total_hits": 2}
…
{"type": "summary", "videos_polled": 96, "videos_total": 187, "episodes_new": 12,
 "units_spent": 96, "stopped_early": true,
 "stop_reason": "hit target reached: 12 new hits; 91 videos unpolled (resume with sweep())"}
```

93 seconds wall, 96 units, 12 hits — then it stopped itself at the target
instead of burning the remaining 91 videos. The operator could also have
killed it after hit #3 and lost nothing: every hit commits to the DB
before its line prints, and the next sweep resumes with zero duplicates
(verified in tests).

## 5. Inbox — read the hits

```bash
python -m voc inbox --area freelance-pricing --db q.db --limit 12
# read in chat; quote the pain, never the person.
# Triage status workflow + report synthesis are voc-listener Pro features.
```

## 6. Fidelity — what it cost vs what it bought

```bash
python -m voc fidelity --db q.db
```

```
Cost vs value — 2026-10-01: 496/10000 units spent (9504 left)
- freelance-pricing: 187 videos @ shallow, today 496u / 1 sweeps / 12 hits; hit rate 0.125/video, 0.99pp, completeness 98%
    last sweep (2026-10-01): 96u, 96 videos, 12 hits (stopped early)
```

496 units total for a real answer in under two minutes of operator time.
The measured hit rate (0.125/video) and pages/video (0.99) now feed the
next estimate for this area — estimates get sharper the more you ask.
