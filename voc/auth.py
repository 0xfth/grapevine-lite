"""The auth seam. The library never stores, logs, or persists credentials.

Two ways to give API calls a credential:

1. Vault CLI seam (default): voc.discover() / voc.fetch_threads() shell
   out to bin/yt_search.py and bin/yt_comments.py with no key involved.
   Those CLIs resolve the operator's vault credential as a surrogate at
   request time. The library never sees a key value. Use this when the
   buyer runs the listener from their own machine/account.

2. Call-time injection: with_api_key(key) returns a `requester` callable
   you pass to discover()/fetch_threads(). The key lives only in the
   returned closure for the duration of the call. The library does not
   write it anywhere, print it, or keep it past the call.

A `requester` is a callable with signature:
    requester(endpoint, params) -> dict
where endpoint is "search" or "commentThreads", params is a dict of
query parameters, and the return value is the NORMALIZED response dict:
  search:         {"items": [{"video_id","title","channel","published_at"}]}
  commentThreads: {"threads": [thread...]} or {"comments_disabled": True}

Internal convention: voc's own wrappers may inject params["__area__"]
to attribute spend per area. The built-in wrappers pop it before the
call reaches you. A custom requester MUST ignore (or pop) unknown params
like __area__ rather than choke on them.

You can also supply your own requester (a mock for tests, a proxy,
another key store) as long as it honors this contract.
"""
import json
import os
import hashlib
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

_API = "https://www.googleapis.com/youtube/v3"


def vault_cli_requester(bin_dir=None):
    """Return a requester that shells out to the bin/ CLIs.

    The CLIs attach the operator's vault credential; this function and
    the library never handle a key value.
    """
    bin_dir = Path(bin_dir) if bin_dir else Path(__file__).resolve().parent.parent / "bin"
    search_cli = bin_dir / "yt_search.py"
    threads_cli = bin_dir / "yt_comments.py"

    def request(endpoint, params):
        if endpoint == "search":
            cmd = [sys.executable, str(search_cli), "--query", params["q"],
                   "--max-results", str(params.get("maxResults", 50))]
            if params.get("pageToken"):
                cmd += ["--page-token", params["pageToken"]]
        elif endpoint == "commentThreads":
            cmd = [sys.executable, str(threads_cli),
                   f"--video-id={params['videoId']}",
                   "--order", "time", "--max-results",
                   str(params.get("maxResults", 100))]
            if params.get("pageToken"):
                cmd += ["--page-token", params["pageToken"]]
        else:
            raise ValueError(f"unknown endpoint: {endpoint}")
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode in (4, 5) or "credential:" in p.stderr.lower():
            raise CredentialError(
                "vault credential missing or broken (CLI exit 4/5: 4 = bad "
                "credential, 5 = Secure Vault helper unavailable). "
                "Stop and tell the operator to fix their key; do not retry-loop.")
        if p.returncode != 0:
            raise ApiError(f"{endpoint} CLI failed (exit {p.returncode}): "
                           f"{p.stderr.strip()[:300]}")
        try:
            return json.loads(p.stdout)
        except json.JSONDecodeError as e:
            raise ApiError(f"{endpoint} CLI returned bad JSON: {e}")

    # Quota-owner identity for the shared ledger: the vault credential
    # name (no key material — the library never sees the key itself).
    request.scope = os.environ.get("VOC_CREDENTIAL", "custom.youtube-data-api")
    return request


def with_api_key(key):
    """Return a requester that calls the YouTube Data API directly.

    The key is used at call time only: appended as ?key= to each request
    URL, never stored, logged, or persisted by the library. Only use this
    when the operator explicitly passes a key for the current call.
    """
    def request(endpoint, params):
        if endpoint == "search":
            url = f"{_API}/search"
        elif endpoint == "commentThreads":
            url = f"{_API}/commentThreads"
        else:
            raise ValueError(f"unknown endpoint: {endpoint}")
        q = dict(params)
        q["key"] = key  # call-time only; never logged
        req = urllib.request.Request(url + "?" + urllib.parse.urlencode(q),
                                     headers={"User-Agent": "voc-listener/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:500]
            if "commentsDisabled" in body:
                return {"comments_disabled": True}
            raise ApiError(f"{endpoint} HTTP {e.code}: {body}")
        except urllib.error.URLError as e:
            raise ApiError(f"{endpoint} network error: {e}")
        return _normalize(endpoint, data)

    # Quota-owner identity for the shared ledger: a sha256 fingerprint of
    # the key — enough to scope shared accounting, never the key itself.
    request.scope = "key:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return request


def _normalize(endpoint, data):
    """Shape raw API responses into the normalized contract."""
    if endpoint == "search":
        items = []
        for it in data.get("items", []):
            vid = (it.get("id") or {}).get("videoId")
            if not vid:
                continue
            sn = it.get("snippet", {})
            items.append({
                "video_id": vid,
                "title": sn.get("title", ""),
                "channel": sn.get("channelTitle", ""),
                "published_at": sn.get("publishedAt", ""),
            })
        out = {"items": items}
        if data.get("nextPageToken"):
            out["nextPageToken"] = data["nextPageToken"]
        return out
    if endpoint == "commentThreads":
        threads = []
        for item in data.get("items", []):
            sn = item.get("snippet", {})
            top = sn.get("topLevelComment", {}).get("snippet", {}) or {}
            thread = {
                "thread_id": item.get("id", ""),
                "video_id": (sn.get("videoId") or ""),
                "top_text": top.get("textDisplay", ""),
                "top_published_at": top.get("publishedAt", ""),
                "top_like_count": top.get("likeCount", 0),
                "reply_count": sn.get("totalReplyCount", 0),
                "reply_authors": [],
                "replies": [],
                "url": "https://www.youtube.com/watch?v=" + (sn.get("videoId") or ""),
            }
            for rep in item.get("replies", {}).get("comments", []):
                rsn = rep.get("snippet", {}) or {}
                thread["reply_authors"].append(rsn.get("authorDisplayName", ""))
                thread["replies"].append({
                    "text": rsn.get("textDisplay", ""),
                    "published_at": rsn.get("publishedAt", ""),
                })
            threads.append(thread)
        out = {"threads": threads}
        if data.get("nextPageToken"):
            out["nextPageToken"] = data["nextPageToken"]
        return out
    raise ValueError(f"unknown endpoint: {endpoint}")


class CredentialError(Exception):
    """The vault credential is missing or broken. Tell the operator; stop."""


class ApiError(Exception):
    """The API call itself failed (network, quota, bad params)."""
