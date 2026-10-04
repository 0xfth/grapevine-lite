---
myagentlist-public-about: true
---

# Grapevine Lite

Grapevine Lite is a free, local tool for estimate-first research in YouTube comments. Define a question, see the expected work before querying, then review the observations that match your topic. Results stay in a local SQLite database, with source links preserved for follow-up.

The public package is version **1.0.0** and uses the **MIT License**. The installed skill and Python compatibility namespace retain their earlier names: `voc-listener-lite` and `voc`.

## Install

Add the agent skill from the public repository:

```bash
npx skills add 0xfth/grapevine-lite --skill voc-listener-lite
```

Or install the Python package from a source checkout:

```bash
git clone https://github.com/0xfth/grapevine-lite.git
cd grapevine-lite
python -m pip install .
```

Python 3.10 or later is required. The distribution name is `voc-listener-lite`; the command examples below use its compatible `python -m voc` entry point.

## Quick start

Set `YOUTUBE_API_KEY` to your own YouTube Data API v3 key, then define a small research area:

```bash
python -m voc add-area --name sourdough --db q.db \\
  --queries "sourdough starter troubleshooting" \\
  --person-terms baker "home baker" \\
  --topic-terms sourdough starter \\
  --depth shallow
```

Estimate first, review the estimate, and run collection only when authorized:

```bash
python -m voc estimate --area sourdough --db q.db
python -m voc query --area sourdough --db q.db --yes --max-new-hits 10
python -m voc inbox --area sourdough --db q.db
python -m voc fidelity --db q.db
```

The query writes hit, progress, and summary records as JSON lines while it runs. You can stop a run early; completed video rows remain saved locally for later review. An early stop can leave the investigation incomplete.

## What Lite includes

- **Estimate-first workflow:** estimate YouTube discovery and comment polling before collection.
- **Local regex scoring:** vocabulary and configured signals rank comments without sending them to an inference provider.
- **Streaming results:** JSONL output makes hits and progress visible while a query runs.
- **Local review:** SQLite stores areas, observations, and query history; `inbox` displays saved hits.
- **Cost history:** `fidelity` compares estimated and observed query work for an area.
- **YouTube only:** Lite queries YouTube comments. It does not include Grapevine Pro's broader source set.

## Credentials and data

Lite needs an operator-supplied YouTube Data API v3 key. Set it in `YOUTUBE_API_KEY` or provide it to the supported command-line tools at call time with `--api-key`. The library does not intentionally store the key in its SQLite research database. Requests go to Google's YouTube API, and collected observations are saved locally.

There is no always-on daemon or schedule. You choose when to estimate and query.

## Coverage limits

Lite searches selected YouTube queries and comment threads; it does not cover all YouTube discussion or establish how representative a sample is. A zero-hit query can mean the search terms, chosen depth, or available comments missed the topic. Scores rank text for review and do not establish truth, identity, or prevalence.

## Technical references

- [Worked example](https://github.com/0xfth/grapevine-lite/blob/main/references/worked-example.md)
- [MIT License](https://github.com/0xfth/grapevine-lite/blob/main/LICENSE)

## Grapevine Pro status

Grapevine Pro is in development and has no public install or purchase link yet. The older VOC Listener Pro Agensi listing is a separate predecessor product; its current version has not been confirmed. The details above describe Lite 1.0.0 and do not imply that the predecessor listing is a Grapevine Pro release.
