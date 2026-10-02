#!/usr/bin/env python3
"""Search YouTube videos via Data API v3 search.list. Prints JSON to stdout.

Quota: 100 units per call. Use sparingly.
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
BASE = "https://www.googleapis.com/youtube/v3/search"
CRED = os.environ.get("VOC_CREDENTIAL", "custom.youtube-data-api")
HOSTS = ["www.googleapis.com"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--max-results", type=int, default=25)
    ap.add_argument("--order", default="relevance")
    ap.add_argument("--type", default="video")
    ap.add_argument("--page-token", default=None)
    ap.add_argument("--published-after", default=None)
    ap.add_argument("--api-key", default=None,
                    help="YouTube Data API v3 key. Falls back to YOUTUBE_API_KEY "
                         "env, then to the Secure Vault surrogate.")
    args = ap.parse_args()

    params = {
        "part": "snippet",
        "q": args.query,
        "type": args.type,
        "maxResults": str(max(1, min(args.max_results, 50))),
        "order": args.order,
    }
    if args.page_token:
        params["pageToken"] = args.page_token
    if args.published_after:
        params["publishedAfter"] = args.published_after

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
        print(json.dumps({"error": f"HTTP {e.code}", "body": body}), file=sys.stderr)
        sys.exit(3)  # 3 = API/HTTP error (exit 2 is reserved for argparse usage errors)
    data = dc.read_json_response(resp) if use_vault else json.load(resp)

    items = []
    for it in data.get("items", []):
        sid = it.get("id", {}) or {}
        sn = it.get("snippet", {}) or {}
        items.append({
            "video_id": sid.get("videoId"),
            "title": sn.get("title"),
            "channel": sn.get("channelTitle"),
            "published_at": sn.get("publishedAt"),
            "description": (sn.get("description") or "")[:500],
        })
    print(json.dumps({
        "items": items,
        "nextPageToken": data.get("nextPageToken"),
        "pageInfo": data.get("pageInfo", {}),
    }, indent=2))


if __name__ == "__main__":
    main()
