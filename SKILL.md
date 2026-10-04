---
name: grapevine-lite
description: Investigate a focused research question in YouTube comments. Define a small set of search phrases, estimate the API work before collecting, and review a bounded local sample with source links. Requires the operator's YouTube Data API v3 key; Lite uses local regex scoring only.
---

# Grapevine Lite

Help the operator investigate a focused question using selected YouTube search phrases and comments. Lite stores research data locally and ranks observations with configured terms and local regex rules. It does not monitor in the background or send comments to a model provider.

## Before collection

1. Clarify the question and suggest two to six natural search phrases. Identify person terms (who) and topic terms (what) with the operator.
2. Check that the operator has made a YouTube Data API v3 key available through the local environment or supported call-time option. Never ask them to paste a key into chat. If credentials are missing or rejected, stop and explain that the operator must configure them.
3. Define or update a research area, then show the estimate before any API collection:

~~~sh
python -m voc add-area --name AREA --db research.db \
  --queries "phrase one" "phrase two" \
  --person-terms PERSON_TERM \
  --topic-terms TOPIC_TERM \
  --depth shallow

python -m voc estimate --area AREA --db research.db
~~~

The estimate reports source work and quota units, not a monetary price. Explain that the requested searches can return an incomplete sample.

4. Wait for the operator's explicit approval of the estimate. Do not run collection commands before approval. The query command's --yes option bypasses confirmation and must only be used after the operator has approved the displayed estimate.
5. After approval, run a bounded query and review the saved observations:

~~~sh
python -m voc query --area AREA --db research.db --yes --max-new-hits 10
python -m voc inbox --area AREA --db research.db
~~~

Use the approved search area and cap. If the operator wants a different scope or cap, update the estimate and ask again.

## Review results carefully

- Preserve source links when summarizing observations. Quote the relevant comment and distinguish it from your interpretation.
- Regex scores rank text against the configured vocabulary and quality signals; they do not verify truth, identity, sentiment, prevalence, or market demand.
- Search phrases, selected depth, source/API limits, and timing shape the sample. A zero-hit run does not show that a topic is absent from YouTube.
- Query output streams hits, progress, and a summary as JSON lines. If a run stops early, state that the sample may be incomplete; already completed video results remain saved locally.
- Use inbox to inspect saved observations. Use grep or diagnose for local text/vocabulary checks, and fidelity to review query usage. These checks read saved data and do not collect new comments.
- Do not describe Lite as supporting other sources, model scoring, triage statuses, or report synthesis.

## Data handling

Collection sends selected search phrases and API requests to YouTube. The local SQLite database stores research areas, observations, and query history. The library does not intentionally store the API key in that database. Do not reveal credentials or include them in notes and reports.
