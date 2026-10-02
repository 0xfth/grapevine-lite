"""Relevance scoring: does this thread sound like a real person with the
pain this niche is about?

score(thread, person_terms, topic_terms, ...) -> {"score": int,
"breakdown": {...}}.

thread: dict with "top_text" (str), "replies" (list of {"text": str}),
"reply_authors" (list of str).

Gate (must pass BOTH to score above the floor):
  person half: any person_term matches as a word boundary
    ("add" must NOT match "added")
  area half:  any area_term matches as a substring
    ("job" matches "jobs"), OR topic_context=True when the video title
    itself establishes the area (pass True when you know it does)

Signals (additive, capped where noted; tuned for forums, override freely):
  base 30 | length tiers: 200+ chars +8, 500+ +12, 1000+ +16
  first-person (" i ", " my ", " i'm") +8 | "?" +6 | help-seeking +6
  "i wish" +8 | struggle words +5 each, cap 15 | multi-author +6

score() returns {"score", "breakdown"}. Whether a score counts as "new"
vs "low" is the caller's decision (threshold=40 is the default we used).

This is the regex scorer — one of the two scoring modes ("regex" and
"clef"). Zero inference: the person/topic vocabulary gate compiles to
exactly one regex (see compile_vocab_regex), plus quality signals for
triage ranking. No API, no cost, always available; Cloudflare is only
needed for the "clef" mode. The extension point: write your own function
with the same input/output contract and use it instead.
See the skill's "Extension points" section.
"""
import json
import re
import sys

DEFAULT_WEIGHTS = {
    "base": 30,
    "length_tiers": [[200, 8], [500, 12], [1000, 16]],
    "first_person": 8,
    "question": 6,
    "help_seeking": 6,
    "i_wish": 8,
    "struggle_each": 5,
    "struggle_cap": 15,
    "multi_author": 6,
}

DEFAULT_STRUGGLE_WORDS = [
    "struggling", "frustrated", "overwhelmed", "anxious", "stuck",
    "hard", "difficult", "impossible", "exhausting", "discouraged",
    "hopeless", "stressed", "confused", "lost", "desperate",
]

DEFAULT_HELP_WORDS = [
    "any tips", "any advice", "how do i", "how can i", "what should i",
    "please help", "anyone else", "does anyone", "am i the only",
    "suggestions", "recommendations", "help me",
]

# Crude sentiment lexicon for the "direction" signal. This is NOT sentiment
# analysis — just word counts, reported honestly as such. It exists so the
# operator can tell a praise hit from a pain hit at a glance during triage.
DEFAULT_PRAISE_WORDS = [
    "love", "loving", "amazing", "awesome", "perfect", "excellent",
    "recommend", "highly recommend", "best ", "game changer", "game-changer",
    "fantastic", "incredible", "brilliant", "obsessed",
]


def compile_vocab_regex(person_terms, topic_terms):
    """Compile an area's vocabulary to the equivalent regex.

    The vocabulary gate (person term as a whole word AND topic term as a
    substring) is exactly:

        (?=.*\\b(?:p1|p2)\\b)(?=.*(?:t1|t2))

    with re.IGNORECASE | re.DOTALL. So the "easy" vocabulary UI and raw
    regex are the same thing — grep --area uses this when no --pattern is
    given, and agents can start from it and add their own boolean logic:

        OR:  (beginner|newbie|newcomer)
        AND: (?=.*beginner)(?=.*keyboard)
        NOT: (?!.*sponsored)
        whole word: \\bterm\\b      substring: term
    """
    person_alt = "|".join(re.escape(p) for p in person_terms) or r"(?!)"
    topic_alt = "|".join(re.escape(t) for t in topic_terms) or r"(?!)"
    return (r"(?=.*\b(?:" + person_alt + r")\b)"
            r"(?=.*(?:" + topic_alt + r"))")


def match_terms(text, person_terms, topic_terms):
    """Which vocabulary terms match this text? Returns
    (person_hits, topic_hits): the matched terms from each list.
    Person terms match as whole words; topic terms as substrings —
    the same semantics score() gates on."""
    low = (text or "").lower()
    person_hits = [p for p in person_terms
                   if re.search(r"\b" + re.escape(p.lower()) + r"\b", low)]
    topic_hits = [t for t in topic_terms if t.lower() in low]
    return person_hits, topic_hits


def direction(text, struggle_words=None, praise_words=None):
    """Crude direction signal: what KIND of hit is this?

    question — asks something ("?", help-seeking phrasing)
    pain     — struggle words, no praise words
    praise   — praise words, no struggle words
    mixed    — both struggle and praise words
    mention  — neither (matched the vocabulary, that's all we know)

    Word counts, not sentiment analysis. Reported as-is so triage can
    sort "I love this" apart from "I'm stuck" without reading everything.
    """
    struggle_words = (struggle_words if struggle_words is not None
                      else DEFAULT_STRUGGLE_WORDS)
    praise_words = (praise_words if praise_words is not None
                    else DEFAULT_PRAISE_WORDS)
    low = (text or "").lower()
    is_question = "?" in (text or "")
    has_struggle = any(s in low for s in struggle_words)
    has_praise = any(p in low for p in praise_words)
    if is_question:
        return "question"
    if has_struggle and has_praise:
        return "mixed"
    if has_struggle:
        return "pain"
    if has_praise:
        return "praise"
    return "mention"


def score(thread, person_terms, topic_terms, threshold=40, weights=None,
          struggle_words=None, help_words=None, topic_context=False,
          gate=None):
    """Score a comment thread for niche relevance.

    Returns {"score": int, "breakdown": {...}}. Returns score 0 with a
    "gate_failed" reason when the person/area gate doesn't pass.

    gate: None (default) computes the person/topic gate from the
    vocabulary; pass True/False to override it — used by grep mode
    (--match), where the regex decides relevance and the score only
    measures quality signals for triage ranking.
    """
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)
    struggle_words = struggle_words if struggle_words is not None else DEFAULT_STRUGGLE_WORDS
    help_words = help_words if help_words is not None else DEFAULT_HELP_WORDS

    text = (thread.get("top_text") or "")
    for r in thread.get("replies") or []:
        text += " " + (r.get("text") or "")
    low = text.lower()

    person_hits, topic_hits = match_terms(text, person_terms, topic_terms)
    has_person = bool(person_hits)
    has_topic = bool(topic_hits) or topic_context
    if gate is None:
        gate = has_person and has_topic

    breakdown = {
        "has_person_term": has_person,
        "has_topic_term": has_topic,
        "topic_context": bool(topic_context),
        "person_terms_matched": person_hits,
        "topic_terms_matched": topic_hits,
        "gate_overridden": gate is not (has_person and has_topic),
    }
    if not gate:
        breakdown["gate"] = "failed"
        return {"score": 0, "breakdown": breakdown}
    breakdown["gate"] = "passed"

    length = len(low)
    length_pts = 0
    for chars, pts in w["length_tiers"]:
        if length >= chars:
            length_pts = pts
    fp = 1 if re.search(r"\b(i|i'm|my|me)\b", low) else 0
    q = 1 if "?" in text else 0
    help_seek = 1 if any(h in low for h in help_words) else 0
    wish = 1 if "i wish" in low else 0
    struggle_hits = [s for s in struggle_words if s in low]
    struggle_pts = min(len(struggle_hits) * w["struggle_each"], w["struggle_cap"])
    authors = set(thread.get("reply_authors") or [])
    multi = 1 if len(authors) > 1 else 0

    total = (w["base"] + length_pts + fp * w["first_person"]
             + q * w["question"] + help_seek * w["help_seeking"]
             + wish * w["i_wish"] + struggle_pts + multi * w["multi_author"])

    breakdown.update({
        "length_chars": length,
        "length_pts": length_pts,
        "first_person": bool(fp),
        "question": bool(q),
        "help_seeking": bool(help_seek),
        "i_wish": bool(wish),
        "struggle_words": struggle_hits,
        "struggle_pts": struggle_pts,
        "multi_author": bool(multi),
        "threshold": threshold,
        # Crude direction signal (word counts, not sentiment analysis):
        # question | pain | praise | mixed | mention.
        "direction": direction(text, struggle_words=struggle_words),
    })
    return {"score": int(total), "breakdown": breakdown}


def main():
    """Thin CLI: thread JSON on stdin -> {"score","breakdown"} on stdout.

    python -m voc.scoring --params niche.json
      where niche.json has person_terms, topic_terms, threshold (optional),
      weights (optional), struggle_words (optional), help_words (optional)
    or pass terms directly:
      python -m voc.scoring --person-terms baker "home baker" \
        --topic-terms sourdough starter --threshold 40
    """
    import argparse
    ap = argparse.ArgumentParser(description="Score a comment thread JSON on stdin")
    ap.add_argument("--params", help="JSON file with niche params")
    ap.add_argument("--person-terms", nargs="*", default=None)
    ap.add_argument("--topic-terms", nargs="*", default=None)
    ap.add_argument("--threshold", type=int, default=None)
    ap.add_argument("--weights-json", default=None, help="JSON object overriding weights")
    ap.add_argument("--struggle-words", nargs="*", default=None)
    ap.add_argument("--help-words", nargs="*", default=None)
    ap.add_argument("--topic-context", action="store_true",
                    help="video title establishes area; satisfies area half of gate")
    ap.add_argument("--scorer", choices=["regex", "clef"], default="regex",
                    help="regex: zero-inference vocabulary/regex matching (default). "
                         "clef: Cloudflare decision model; needs "
                         "CLOUDFLARE_ACCOUNT_ID/CLOUDFLARE_AUTH_TOKEN in env "
                         "(or the cloudflare skill's vault CLI).")
    args = ap.parse_args()

    params = {}
    if args.params:
        try:
            with open(args.params) as f:
                params = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(json.dumps({"error": f"bad params file: {e}"}), file=sys.stderr)
            sys.exit(2)

    person_terms = args.person_terms if args.person_terms is not None else params.get("person_terms")
    topic_terms = args.topic_terms if args.topic_terms is not None else params.get("topic_terms")
    threshold = args.threshold if args.threshold is not None else params.get("threshold", 40)
    weights = params.get("weights")
    if args.weights_json:
        try:
            weights = dict(weights or {})
            weights.update(json.loads(args.weights_json))
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"bad weights json: {e}"}), file=sys.stderr)
            sys.exit(2)
    struggle_words = args.struggle_words if args.struggle_words is not None else params.get("struggle_words")
    help_words = args.help_words if args.help_words is not None else params.get("help_words")

    if not person_terms or not topic_terms:
        print(json.dumps({"error": "person_terms and topic_terms are required "
                                   "(--params or --person-terms/--topic-terms)"}),
              file=sys.stderr)
        sys.exit(2)

    try:
        thread = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"bad thread JSON on stdin: {e}"}), file=sys.stderr)
        sys.exit(2)

    # Lazy import: scoring_clef imports this module for its fallback.
    from .scoring_clef import get_scorer
    scorer_name = args.scorer if args.scorer != "regex" else params.get("scorer", "regex")
    score_fn = get_scorer(scorer_name)
    # scorer_params (clef only): clef_questions, clef_state_fields,
    # clef_max_chars, clef_gates, clef_max_input_tokens — direct control
    # over the decision model's questions, state, and spend. Ignored for
    # the regex scorer.
    extra = {}
    if scorer_name == "clef":
        extra = params.get("scorer_params", {}) or {}

    print(json.dumps(score_fn(thread, person_terms, topic_terms,
                              threshold=threshold, weights=weights,
                              struggle_words=struggle_words, help_words=help_words,
                              topic_context=args.topic_context, **extra)))


if __name__ == "__main__":
    main()
