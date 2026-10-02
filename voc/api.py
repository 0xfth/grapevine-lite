"""Discovery and polling primitives. Quota-aware, watermark-driven.

    from voc import discover, fetch_threads
    from voc.auth import vault_cli_requester

    videos, units = discover(["sourdough starter tips"], requester=vault_cli_requester())
    threads, pages, units, complete = fetch_threads(video_id, since="2026-09-25T00:00:00Z")

Quota (YouTube Data API default 10,000 units/day):
  search.list page      = 100 units
  commentThreads page   = 1 unit
discover() and fetch_threads() return units spent so the caller can
budget. Query runs call through budgeted_requester, which
paces via the token bucket and records via the hard Budget.
"""
from . import auth as _auth


def discover(queries, max_results=50, page2_new_threshold=15,
             requester=None, max_units=None):
    """Search YouTube for videos matching each query.

    Returns (videos, units_spent). videos: list of
    {"video_id","title","channel","published_at","query"}.
    A query's second page is fetched only when the first page surfaced
    >= page2_new_threshold videos not already known to the caller
    (pass known_ids to dedupe; else it always fetches page 2).
    """
    requester = requester or _auth.vault_cli_requester()
    videos, units = [], 0
    known_ids = set()

    def collect(data, q):
        new_ids = []
        for it in data.get("items", []):
            if it["video_id"] not in known_ids:
                known_ids.add(it["video_id"])
                new_ids.append(it["video_id"])
                videos.append({**it, "query": q})
        return new_ids

    for q in queries:
        params = {"part": "snippet", "q": q, "type": "video",
                  "maxResults": max_results, "order": "relevance"}
        data = requester("search", params)
        units += 100
        new_ids = collect(data, q)
        next_token = data.get("nextPageToken")
        if (next_token and len(new_ids) >= page2_new_threshold
                and (max_units is None or units < max_units)):
            params["pageToken"] = next_token
            data = requester("search", params)
            units += 100
            collect(data, q)
    return videos, units


def fetch_threads(video_id, since="", max_pages=5, requester=None,
                  max_units=None):
    """Fetch comment threads for a video, newest first.

    Returns (threads, pages_fetched, units_spent, complete). threads: list of
    normalized thread dicts (see auth._normalize / bin/yt_comments.py).
    complete=False means pagination hit max_pages with more threads
    possibly available — the caller must NOT advance the watermark past
    the oldest fetched thread. Stops early when the newest thread on a
    page is older than `since` (watermark string comparison, ISO 8601),
    so repeat polls cost ~1 unit.
    Returns (None, 0, 0, True) when comments are disabled (normal; skip it).
    """
    requester = requester or _auth.vault_cli_requester()
    threads, pages, units = [], 0, 0
    token = None
    while pages < max_pages:
        if max_units is not None and units >= max_units:
            return threads, pages, units, False
        params = {"part": "snippet,replies", "videoId": video_id,
                  "order": "time", "maxResults": 100,
                  "textFormat": "plainText"}
        if token:
            params["pageToken"] = token
        data = requester("commentThreads", params)
        units += 1
        if data.get("comments_disabled"):
            return None, 0, 0, True
        batch = data.get("threads", [])
        pages += 1
        if since and batch and batch[0].get("top_published_at", "") < since:
            break
        threads.extend(batch)
        token = data.get("nextPageToken")
        if not token:
            break
    complete = not (pages >= max_pages and token)
    return threads, pages, units, complete
