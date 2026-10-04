---
myagentlist-public-about: true
---

# Grapevine Lite

Investigate a focused question in YouTube comments with an estimate before collection, a local record of observations, and source links for follow-up.

The public Lite release is version **1.0.0** under the **MIT License**. It uses local regex scoring and requires your own YouTube Data API v3 key.

## Install the agent skill

~~~bash
npx skills add 0xfth/grapevine-lite --skill grapevine-lite
~~~

## Install the Python package

~~~bash
git clone https://github.com/0xfth/grapevine-lite.git
cd grapevine-lite
python -m pip install .
~~~

Python 3.10 or later is required. The Python distribution keeps its compatibility name **voc-listener-lite**; import and command examples use **voc** and **python -m voc**. The agent skill has its own name, **grapevine-lite**.

## Run a first research question

Make your YouTube Data API v3 key available to the process as **YOUTUBE_API_KEY** or through the supported call-time **--api-key** option. Define a small area with two to six concrete search phrases, terms that describe who and what you are investigating, and a collection depth:

~~~bash
python -m voc add-area --name sourdough --db q.db \
  --queries "sourdough starter troubleshooting" \
  --person-terms baker "home baker" \
  --topic-terms "sourdough starter" \
  --depth shallow
~~~

Review the estimate before collection:

~~~bash
python -m voc estimate --area sourdough --db q.db
~~~

After reviewing and approving that estimate, run a bounded collection and inspect the saved observations:

~~~bash
python -m voc query --area sourdough --db q.db --yes --max-new-hits 10
python -m voc inbox --area sourdough --db q.db
python -m voc fidelity --db q.db
~~~

Query output streams hits, progress, and a summary as JSON lines. Stopping early keeps completed video results in the local database, but the resulting sample may be incomplete.

## What Lite includes

- Estimate-first, on-demand research over selected YouTube search phrases and comments.
- Local regex scoring based on the configured vocabulary and quality signals; it does not send comments to an inference provider.
- A local SQLite database for areas, observations, and query history.
- An inbox for reviewing saved observations, with source links preserved for follow-up.
- Helpers for checking saved results and query history, including grep, diagnose, videos, simulate, and fidelity.

Lite covers YouTube comments only. It does not run a background monitor or schedule collections. It does not include the broader source set or optional model scoring described for Grapevine Pro.

## Data and coverage

Collections call the YouTube Data API using your key. The library does not intentionally store that key in its SQLite research database. Research data and query history are stored locally.

Results are limited to videos and comments reached by the chosen phrases, depth, timing, and API limits. A zero-hit run does not show that a topic is absent from YouTube. Scores help rank observations for review; they do not establish truth, identity, sentiment, or prevalence.

## References

- [Worked example](https://github.com/0xfth/grapevine-lite/blob/main/references/worked-example.md)
- [MIT License](https://github.com/0xfth/grapevine-lite/blob/main/LICENSE)

Grapevine Pro is a separate edition in development. This guide describes only the available Lite 1.0.0 release.
