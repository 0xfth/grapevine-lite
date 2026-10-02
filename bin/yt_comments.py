#!/usr/bin/env python3
"""Fetch comment threads for a video via Data API v3 commentThreads.list.

Prints JSON to stdout. One call = 1 quota unit.
If comments are disabled on the video, prints {"comments_disabled": true}
and exits 0 (a normal skip, not an error).
"""
import argparse
import json
import sys
import urllib.parse
import urllib.request
import urllib.error

# Auth resolution order: explicit --api-key / YOUTUBE_API_KEY first (buyer
# machines), then the Secure Vault surrogate (Muse-style agent runtimes).
sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
try:
    import dynamic_credentials as dc
    _HAVE_VAULT = True
except ImportError:
    dc = None
    _HAVE_VAULT = False

import os
BASE = "https://www.googleapis.com/youtube/v3/commentThreads"
CRED = os.environ.get("VOC_CREDENTIAL", "custom.youtube-data-api")
HOSTS = ["www.googleapis.com"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video-id", required=True)
    ap.add_argument("--order", default="time")
    ap.add_argument("--max-results", type=int, default=100)
    ap.add_argument("--page-token", default=None)
    ap.add_argument("--api-key", default=None,
                    help="YouTube Data API v3 key. Falls back to YOUTUBE_API_KEY "
                         "env, then to the Secure Vault surrogate.")
    args = ap.parse_args()

    params = {
        "part": "snippet,replies",
        "videoId": args.video_id,
        "order": args.order,
        "maxResults": str(max(1, min(args.max_results, 100))),
        "textFormat": "plainText",
    }
    if args.page_token:
        params["pageToken"] = args.page_token

    api_key = args.api_key or os.environ.get("YOUTUBE_API_KEY")
    use_vault = not api_key
    if api_key:
        params["key"] = api_key
    url = BASE + "?" + urllib.parse.urlencode(params)
    if use_vault:
        if not _HAVE_VAULT:
            print(json.dumps({"error": "no API key (--api-key or YOUTUBE_API_KEY) and no "
                              "Secure Vault credential helper (expected at "
                              "/opt/hatch/skills/skill-creator/bin/dynamic_credentials.py)"}),
                  file=sys.stderr)
            sys.exit(5)
        try:
            url = dc.url_with_surrogate_query_param(url, CRED, allowed_hosts=HOSTS)
        except dc.DynamicCredentialError as exc:
            print(json.dumps({"error": f"credential: {exc}"}), file=sys.stderr)
            sys.exit(4)

    req = urllib.request.Request(url, headers={"User-Agent": "muse-voc-listener/1.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:2000]
        if "commentsDisabled" in body:
            print(json.dumps({"comments_disabled": True}))
            return
        print(json.dumps({"error": f"HTTP {e.code}", "body": body}), file=sys.stderr)
        sys.exit(3)  # 3 = API/HTTP error (exit 2 is reserved for argparse usage errors)
    data = dc.read_json_response(resp) if use_vault else json.load(resp)

    threads = []
    for it in data.get("items", []):
        sn = it.get("snippet", {}) or {}
        top = sn.get("topLevelComment", {}) or {}
        tsn = top.get("snippet", {}) or {}
        replies = []
        reply_authors = []
        for r in (sn.get("replies", {}) or {}).get("comments", []):
            rsn = (r.get("snippet", {}) or {})
            replies.append({
                "text": rsn.get("textDisplay") or "",
                "published_at": rsn.get("publishedAt"),
            })
            a = rsn.get("authorDisplayName")
            if a:
                reply_authors.append(a)
        threads.append({
            "thread_id": (top.get("id") or ""),
            "video_id": args.video_id,
            "top_text": tsn.get("textDisplay") or "",
            "top_published_at": tsn.get("publishedAt"),
            "top_like_count": tsn.get("likeCount", 0),
            "reply_count": sn.get("totalReplyCount", 0),
            "reply_authors": reply_authors,
            "replies": replies,
            "url": f"https://www.youtube.com/watch?v={args.video_id}&lc={top.get('id', '')}",
        })
    print(json.dumps({
        "threads": threads,
        "nextPageToken": data.get("nextPageToken"),
        "pageInfo": data.get("pageInfo", {}),
    }, indent=2))


if __name__ == "__main__":
    main()
