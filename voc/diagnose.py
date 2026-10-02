"""Vocabulary diagnostics: why is a question area finding nothing (or the
wrong things)? Free — pure function of stored threads.

`diagnose(store, area)` reports:
  - per-term match counts: which person/topic terms ever fire, and which
    are dead weight (0 matches across everything stored)
  - near-misses: stored threads that matched the person side but not the
    topic side (and vice versa) — the vocabulary gap in concrete examples
  - a verdict: what to change first

Run it after a probe sweep (even a tiny one) — it needs stored threads
to have opinions about.
"""
from .scoring import match_terms
from .grep import thread_text


def diagnose(store, area, sample_limit=5):
    cfg = store.get_area(area)  # KeyError with guidance if missing
    person_terms = cfg["person_terms"]
    topic_terms = cfg["topic_terms"]
    episodes = store.episodes(area=area, status=None, limit=100000)

    person_counts = {p: 0 for p in person_terms}
    topic_counts = {t: 0 for t in topic_terms}
    person_only = []   # matched person side, not topic side
    topic_only = []    # matched topic side, not person side
    neither = 0
    both = 0

    for ep in episodes:
        text = thread_text(ep["top_text"], ep.get("replies"))
        ph, th = match_terms(text, person_terms, topic_terms)
        for p in ph:
            person_counts[p] += 1
        for t in th:
            topic_counts[t] += 1
        if ph and th:
            both += 1
        elif ph and len(person_only) < sample_limit:
            person_only.append(ep)
        elif th and len(topic_only) < sample_limit:
            topic_only.append(ep)
        elif not ph and not th:
            neither += 1

    n = len(episodes)
    verdicts = []
    dead_person = [p for p, c in person_counts.items() if c == 0]
    dead_topic = [t for t, c in topic_counts.items() if c == 0]
    if n == 0:
        verdicts.append("no stored threads yet — run a small probe sweep "
                        "first (e.g. sweep --max-units 10), then diagnose again")
    else:
        if dead_person:
            verdicts.append(f"person terms never matching anything: "
                            f"{', '.join(dead_person)} — drop or reword them")
        if dead_topic:
            verdicts.append(f"topic terms never matching anything: "
                            f"{', '.join(dead_topic)} — drop or reword them")
        if person_only and not topic_only:
            verdicts.append(f"{len(person_only)}+ threads match the person side "
                            f"but not the topic side — topic_terms are too "
                            f"narrow; steal wording from the samples below")
        elif topic_only and not person_only:
            verdicts.append(f"{len(topic_only)}+ threads match the topic side "
                            f"but not the person side — person_terms are too "
                            f"narrow")
        elif person_only and topic_only:
            verdicts.append("both sides have near-misses — the vocabulary is "
                            "close; widen the weaker side using the samples")
        if both == 0 and n > 0:
            verdicts.append("nothing matched both sides yet — consider the "
                            "regex route: `grep --area` with a hand-written "
                            "pattern is often faster than tuning two term lists")
    return {
        "area": area,
        "threads_examined": n,
        "person_term_hits": person_counts,
        "topic_term_hits": topic_counts,
        "matched_both": both,
        "matched_neither": neither,
        "person_only_samples": person_only,
        "topic_only_samples": topic_only,
        "verdicts": verdicts,
    }


def render(rep):
    L = [f"diagnose — area '{rep['area']}' "
         f"({rep['threads_examined']} stored threads)"]
    L.append("  person terms:")
    for term, c in rep["person_term_hits"].items():
        flag = "  <-- never matches" if c == 0 else ""
        L.append(f"    {c:5d}  {term}{flag}")
    L.append("  topic terms:")
    for term, c in rep["topic_term_hits"].items():
        flag = "  <-- never matches" if c == 0 else ""
        L.append(f"    {c:5d}  {term}{flag}")
    L.append(f"  matched both sides: {rep['matched_both']}, "
             f"neither: {rep['matched_neither']}")
    for label, samples in (("person-side only (topic terms missed these)",
                             rep["person_only_samples"]),
                            ("topic-side only (person terms missed these)",
                             rep["topic_only_samples"])):
        if samples:
            L.append(f"  near-misses — {label}:")
            for ep in samples:
                t = (ep["top_text"] or "").replace("\n", " ")[:160]
                L.append(f"    [{ep['id']}] {t}")
    if rep["verdicts"]:
        L.append("  verdict:")
        for v in rep["verdicts"]:
            L.append(f"    - {v}")
    return "\n".join(L)
