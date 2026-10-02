"""Triage helpers: the inbox the agent reads in chat.

    from voc import Store, inbox, format_episode
    store = Store("episodes.db")
    for ep in inbox(store, "sourdough"):   # status="new", highest score first
        print(format_episode(ep))

Triage itself is a conversation: the agent reads each episode, decides
new -> seen (noted, nothing to do) / handled (acted on, e.g. answered
or folded into research) / dismissed (false positive), and calls
store.set_status(). Custom triage flows (auto-dismiss rules, routing)
plug in here — see the skill's "Extension points".
"""
from .store import Store


def inbox(store, area, limit=20):
    """Highest-scoring unreviewed episodes for a area."""
    return store.episodes(area=area, status="new", limit=limit)


def format_episode(ep, verbose=False):
    """One episode as readable chat text. Never includes author names."""
    lines = [
        f"[{ep['id']}] score {ep['score']} | {ep['top_published_at'][:10]} | "
        f"{ep['reply_count']} repl{'y' if ep['reply_count']==1 else 'ies'} | "
        f"{ep['url']}",
        ep["top_text"][:600],
    ]
    if verbose:
        for r in ep.get("replies", [])[:5]:
            lines.append("  ↳ " + r["text"][:300])
        bd = ep.get("score_breakdown", {})
        lines.append(f"  gate={bd.get('gate')} "
                     f"struggle={','.join(bd.get('struggle_words', [])) or 'none'}")
    return "\n".join(lines)
