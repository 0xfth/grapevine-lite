"""grep for comments: regex search over stored threads, and the live
--match mode's shared machinery.

An agent's vocabulary (person_terms + topic_terms) is just a regex in
disguise — see scoring.compile_vocab_regex. grep lets agents skip the
vocabulary UI and search comment threads directly:

    python -m voc grep --area keeb-regrets --db x.db \\
        --pattern "(?i)regret|wish i (had|hadn't)|overpaid"

Boolean logic in regex:
    OR:         (beginner|newbie|newcomer)
    AND:        (?=.*beginner)(?=.*keyboard)
    NOT:        (?!.*sponsored)regret
    whole word: \\bterm\\b        substring: term

Exit code mirrors grep: 0 when something matched, 1 when nothing did.
"""
import json
import re

from .scoring import compile_vocab_regex

FLAGS = re.IGNORECASE | re.DOTALL


def compile_pattern(pattern, case_sensitive=False):
    """Compile a user regex with the flags grep mode needs (DOTALL so .
    spans newlines in comment text; IGNORECASE unless --case-sensitive).
    Raises ValueError with a plain message on bad patterns — the CLI turns
    it into a usage error, not a traceback."""
    flags = re.DOTALL if case_sensitive else FLAGS
    try:
        return re.compile(pattern, flags)
    except re.error as e:
        raise ValueError(f"bad regex {pattern!r}: {e}")


def thread_text(top_text, replies):
    """Full searchable text of a stored thread: top comment + replies."""
    parts = [top_text or ""]
    for r in replies or []:
        parts.append((r.get("text") or "") if isinstance(r, dict) else "")
    return "\n".join(p for p in parts if p)


def match_snippet(text, rx, window=120):
    """A grep-like excerpt: `window` chars around the first match, with
    newlines flattened and the match wrapped in «». Zero-width matches
    (e.g. pure-lookahead patterns like the compiled vocabulary) get no
    markers — just the window."""
    m = rx.search(text)
    if not m:
        return ""
    start = max(0, m.start() - window // 2)
    end = min(len(text), m.end() + window // 2)
    pre = "…" if start > 0 else ""
    post = "…" if end < len(text) else ""
    body = text[start:end].replace("\n", " ")
    if m.end() == m.start():
        return f"{pre}{body}{post}"
    ms, me = m.start() - start, m.end() - start
    return f"{pre}{body[:ms]}«{body[ms:me]}»{body[me:]}{post}"


def grep_store(store, area, pattern=None, case_sensitive=False,
               statuses=None, limit=100):
    """Search stored threads. pattern=None uses the area's vocabulary
    compiled to its equivalent regex. Returns (compiled_pattern_str,
    matches): matches are episode dicts with a _match_snippet key,
    newest first. Free — no API calls."""
    cfg = store.get_area(area)  # KeyError with guidance if missing
    pat = pattern if pattern is not None else compile_vocab_regex(
        cfg["person_terms"], cfg["topic_terms"])
    rx = compile_pattern(pat, case_sensitive=case_sensitive)
    episodes = store.episodes(area=area, status=None, limit=100000)
    matches = []
    for ep in episodes:
        if statuses and ep["status"] not in statuses:
            continue
        text = thread_text(ep["top_text"], ep.get("replies"))
        if rx.search(text):
            ep = dict(ep)
            ep["_match_snippet"] = match_snippet(text, rx)
            matches.append(ep)
            if len(matches) >= limit:
                break
    return pat, matches


def render_match(ep):
    d = ep.get("score_breakdown") or {}
    direction = d.get("direction", "?") if isinstance(d, dict) else "?"
    head = (f"[{ep['id']}] score={ep['score']} {direction} "
            f"({ep['status']}) {ep['url']}")
    snippet = ep.get("_match_snippet") or (
        ep["top_text"] or "").replace("\n", " ")[:200]
    return f"{head}\n    {snippet}"
