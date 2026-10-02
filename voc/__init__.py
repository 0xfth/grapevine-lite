"""voc: a library for querying YouTube's comment sections like a live pulse.

Ask about any niche, get scored real-people pain back in minutes.

    from voc import Store, Listener
    store = Store("episodes.db")
    store.add_area("sourdough",
                   queries=["sourdough starter troubleshooting"],
                   person_terms=["baker", "home baker"],
                   topic_terms=["sourdough", "starter", "crumb"],
                   threshold=40, depth="shallow")

    q = Listener("sourdough", store)
    print(q.render_estimate())   # cost first — operator nods, then spend
    q.discover()                 # seed videos (100 units/query, YouTube's price)
    q.sweep(max_new_hits=10)     # most-promising-first, hits stream live

Composable operations, no daemon, no schedule inside this library: the
operator (or their cron) decides when to ask. Episodes commit per video,
so killing a run early loses nothing already found.

Auth seam (voc.auth): credentials are NEVER stored or logged by this
library. By default, API calls go through bin/yt_search.py and
bin/yt_comments.py, which attach the operator's vault credential as a
surrogate at request time. Or inject a key at call time with
voc.auth.with_api_key(key) and pass the returned requester in.
"""

__version__ = "1.0.0"

from .api import discover, fetch_threads
from .auth import vault_cli_requester, with_api_key
from .fidelity import fidelity_report
from .listener import Listener, rank_videos
from .plan import estimate, render_estimate
from .quota import (Budget, QuotaExhausted, budgeted_requester, quota_status,
                    run_allowance)
from .scoring import (
    DEFAULT_HELP_WORDS,
    DEFAULT_STRUGGLE_WORDS,
    DEFAULT_WEIGHTS,
    score,
)
from .sim import DryRunBudget, simulate_query_engine
from .store import STATUSES, Store
from .stream import emit_hit, emit_progress, emit_summary
from .triage import format_episode, inbox

__all__ = [
    "Store", "Listener", "rank_videos",
    "discover", "fetch_threads",
    "estimate", "render_estimate",
    "vault_cli_requester", "with_api_key",
    "score", "DEFAULT_WEIGHTS", "DEFAULT_STRUGGLE_WORDS", "DEFAULT_HELP_WORDS",
    "STATUSES",
    "format_episode", "inbox",
    "emit_hit", "emit_progress", "emit_summary",
    "Budget", "QuotaExhausted", "budgeted_requester", "quota_status",
    "run_allowance",
    "fidelity_report",
    "DryRunBudget", "simulate_query_engine",
]
