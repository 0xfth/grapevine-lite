"""Storage the agent never writes SQL for. Focus areas are the core
abstraction: one Store holds many areas; every video, episode, watermark,
and quota unit is namespaced by area. The agent steers attention
explicitly with steer({area: weight}); steering persists until steered
again.

    from voc import Store, Listener
    store = Store("episodes.db")
    store.add_area("sourdough",
                   queries=["sourdough starter troubleshooting"],
                   person_terms=["baker", "home baker"],
                   topic_terms=["sourdough", "starter", "crumb"],
                   threshold=40, cadence_hours=4, depth="deep")
    store.steer({"sourdough": 3, "houseplants": 1})
    heavy = Listener("sourdough", store)
    heavy.sweep()
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("new", "seen", "handled", "dismissed", "starred", "low")

_SCHEMA = Path(__file__).resolve().parent / "schema.sql"


def _utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _decode_episode(d):
    """Parse an episode row's JSON columns in place.

    Corrupt stored JSON names the episode and the column instead of
    surfacing a bare JSONDecodeError (which the CLI can't explain).
    """
    eid = d.get("id", "?")
    try:
        d["replies"] = json.loads(d.pop("replies_json") or "[]")
    except json.JSONDecodeError as e:
        raise ValueError(f"episode {eid} has corrupt replies_json: {e}")
    try:
        d["score_breakdown"] = json.loads(d.pop("score_breakdown_json") or "{}")
    except json.JSONDecodeError as e:
        raise ValueError(f"episode {eid} has corrupt score_breakdown_json: {e}")
    return d


class Store:
    def __init__(self, path):
        self.path = str(path)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        with open(_SCHEMA) as f:
            self._db.executescript(f.read())
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        # Migration for stores created before next_due existed.
        cols = [r["name"] for r in self._db.execute("PRAGMA table_info(videos)")]
        if "next_due" not in cols:
            self._db.execute("ALTER TABLE videos ADD COLUMN next_due TEXT DEFAULT ''")
        # Migration for stores created before published_at existed (used to
        # rank fresh videos first in promising-first sweep order).
        if "published_at" not in cols:
            self._db.execute("ALTER TABLE videos ADD COLUMN published_at TEXT DEFAULT ''")
        # Migration for the "starred" triage status: widen the episodes
        # CHECK constraint on stores created before it existed. SQLite can't
        # ALTER a CHECK, so rebuild the table (data-preserving).
        sql = self._db.execute(
            "SELECT sql FROM sqlite_master WHERE name='episodes'").fetchone()["sql"]
        if "'starred'" not in sql:
            self._db.executescript("""
                CREATE TABLE episodes_new (
                  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                  area                TEXT NOT NULL DEFAULT '',
                  thread_id           TEXT UNIQUE NOT NULL,
                  video_id            TEXT DEFAULT '',
                  top_text            TEXT DEFAULT '',
                  top_published_at    TEXT DEFAULT '',
                  top_like_count      INTEGER DEFAULT 0,
                  reply_count         INTEGER DEFAULT 0,
                  replies_json        TEXT DEFAULT '[]',
                  url                 TEXT DEFAULT '',
                  score               INTEGER DEFAULT 0,
                  score_breakdown_json TEXT DEFAULT '{}',
                  status              TEXT DEFAULT 'new'
                    CHECK(status IN ('new','seen','handled','dismissed','starred','low')),
                  created_at          TEXT DEFAULT ''
                );
                INSERT INTO episodes_new SELECT * FROM episodes;
                DROP TABLE episodes;
                ALTER TABLE episodes_new RENAME TO episodes;
                CREATE INDEX IF NOT EXISTS idx_episodes_area_status
                  ON episodes(area, status, score DESC);
            """)
        self._db.commit()

    def close(self):
        self._db.close()

    # --- meta (steering, cursors, run state) ---
    def meta_get(self, key):
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def meta_set(self, key, value):
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))
        self._db.commit()

    # --- sweep cursor: positional resume across interrupted runs ---
    # poll() stores the remaining video order here after every video; a new
    # sweep picks it up instead of re-ranking from the top, so an
    # interrupted run continues where it stopped rather than re-polling
    # videos it already did. Cleared when a pass completes. Keyed per area.
    def get_sweep_cursor(self, area):
        raw = self.meta_get(f"sweep_cursor:{area}")
        if not raw:
            return None
        try:
            ids = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        return ids if isinstance(ids, list) else None

    def set_sweep_cursor(self, area, remaining_ids):
        self.meta_set(f"sweep_cursor:{area}", json.dumps(list(remaining_ids)))

    def clear_sweep_cursor(self, area):
        self._db.execute("DELETE FROM meta WHERE key=?",
                         (f"sweep_cursor:{area}",))
        self._db.commit()

    def avg_seconds_per_video(self, area, days=7):
        """(avg wall seconds per video poll, measured?) from per-video
        poll_runs rows. (None, True) when unmeasured."""
        # Timestamps are "%Y-%m-%dT%H:%M:%SZ"; normalize for julianday().
        rows = self._db.execute(
            """SELECT AVG(
                 (julianday(replace(replace(finished_at,'T',' '),'Z',''))
                  - julianday(replace(replace(started_at,'T',' '),'Z','')))
                   * 86400.0
               ) a, COUNT(*) c FROM poll_runs
               WHERE area=? AND kind='poll' AND videos_polled>0
               AND started_at != '' AND finished_at != ''
               AND day >= date('now','-7 days')""",
            (area,)).fetchone()
        if not rows or not rows["c"] or rows["a"] is None:
            return None, True
        return float(rows["a"]), False

    # --- areas ---
    # --- focus areas ---
    def add_area(self, name, queries=(), person_terms=(), topic_terms=(),
                 threshold=40, cadence_hours=8, depth="deep"):
        """Define a focus area: what to watch (queries + terms) and how
        (cadence, depth). Attention itself is steered separately via
        steer() — a new area gets no attention until steered to."""
        if not name or "/" in name or " " in name:
            raise ValueError("area name must be a non-empty slug (no spaces/slashes)")
        if depth not in ("deep", "shallow"):
            raise ValueError("depth must be 'deep' or 'shallow'")
        if cadence_hours <= 0:
            raise ValueError("cadence_hours must be > 0")
        self._db.execute(
            """INSERT INTO areas (name, person_terms, topic_terms, threshold,
                                   cadence_hours, depth, queries, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (name, json.dumps(list(person_terms)), json.dumps(list(topic_terms)),
             threshold, cadence_hours, depth, json.dumps(list(queries)),
             _utcnow()))
        self._db.commit()

    def get_area(self, name):
        row = self._db.execute("SELECT * FROM areas WHERE name=?", (name,)).fetchone()
        if row is None:
            raise KeyError(f"unknown area {name!r}; add it with add_area() first")
        d = dict(row)
        for k in ("person_terms", "topic_terms", "queries"):
            d[k] = json.loads(d[k] or "[]")
        return d

    def areas(self):
        return [self._row_area(r) for r in
                self._db.execute("SELECT * FROM areas ORDER BY name")]

    def _row_area(self, row):
        d = dict(row)
        for k in ("person_terms", "topic_terms", "queries"):
            d[k] = json.loads(d[k] or "[]")
        return d

    def update_area(self, name, **fields):
        allowed = {"person_terms", "topic_terms", "threshold", "cadence_hours",
                   "depth", "queries"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown area fields: {sorted(bad)}")
        if "depth" in fields and fields["depth"] not in ("deep", "shallow"):
            raise ValueError("depth must be 'deep' or 'shallow'")
        sets, params = [], []
        for k, v in fields.items():
            if k in ("person_terms", "topic_terms", "queries"):
                v = json.dumps(list(v))
            sets.append(f"{k}=?")
            params.append(v)
        params.append(name)
        cur = self._db.execute(f"UPDATE areas SET {', '.join(sets)} WHERE name=?",
                               params)
        self._db.commit()
        if cur.rowcount == 0:
            raise KeyError(f"unknown area {name!r}")
        if "cadence_hours" in fields:
            # Recompute due times from the new cadence
            # picks up the change immediately.
            from datetime import timedelta
            cfg = self.get_area(name)
            for v in self.videos(name):
                base = v["last_polled"] or v["added_at"] or _utcnow()
                try:
                    bdt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc)
                except ValueError:
                    bdt = datetime.now(timezone.utc)
                due = (bdt + timedelta(hours=cfg["cadence_hours"])).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
                self.set_next_due(name, v["video_id"], due)

    # --- steering: optional per-day budget split across areas ---
    def steer(self, weights):
        """Split one day's budget across areas: {area_name: weight}.

        Query-engine use: when you're running several areas in one day,
        steering caps each area's share so one question can't eat the whole
        budget. Replaces the whole split — areas omitted or at 0 get no
        spend. Persists until steered again. At least one weight must be > 0.
        Single-area use doesn't need this at all."""
        if not weights or not any(w > 0 for w in weights.values()):
            raise ValueError("steer() needs at least one area with weight > 0")
        known = {a["name"] for a in self.areas()}
        unknown = set(weights) - known
        if unknown:
            raise KeyError(f"steer() unknown areas: {sorted(unknown)}; "
                           f"add them with add_area() first")
        if any(w < 0 for w in weights.values()):
            raise ValueError("weights must be >= 0")
        payload = {"weights": {k: float(v) for k, v in weights.items()},
                   "updated_at": _utcnow()}
        self.meta_set("steering", json.dumps(payload))
        return payload

    def get_steering(self):
        """{"weights": {...}, "updated_at": ...}. Empty weights when the
        agent hasn't steered yet."""
        raw = self.meta_get("steering")
        if raw is None:
            return {"weights": {}, "updated_at": None}
        return json.loads(raw)

    # --- videos (per-area watermarks + optional due times) ---
    def add_video(self, area, video_id, title="", channel="", query="",
                  source="youtube", last_polled="", published_at=""):
        self._db.execute(
            """INSERT OR IGNORE INTO videos
               (video_id, area, title, channel, source, discovery_query,
                last_polled, next_due, added_at, published_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (video_id, area, title, channel, source, query, last_polled,
             _utcnow(), _utcnow(), published_at))
        self._db.commit()

    def videos(self, area, order="stalest"):
        q = ("SELECT video_id, area, title, channel, discovery_query, "
             "published_at, last_polled, next_due, added_at FROM videos WHERE area=?")
        q += " ORDER BY last_polled ASC" if order == "stalest" else " ORDER BY added_at"
        return [dict(r) for r in self._db.execute(q, (area,))]

    def set_watermark(self, area, video_id, timestamp):
        self._db.execute(
            "UPDATE videos SET last_polled=? WHERE video_id=? AND area=?",
            (timestamp, video_id, area))
        self._db.commit()

    def set_next_due(self, area, video_id, timestamp):
        self._db.execute(
            "UPDATE videos SET next_due=? WHERE video_id=? AND area=?",
            (timestamp, video_id, area))
        self._db.commit()

    def due_video_count(self, area, now):
        row = self._db.execute(
            "SELECT COUNT(*) c FROM videos WHERE area=? AND next_due <= ?",
            (area, now)).fetchone()
        return row["c"]

    def stalest_due_video(self, area, now):
        row = self._db.execute(
            """SELECT video_id, area, title, channel, discovery_query,
                      last_polled, next_due, added_at FROM videos
               WHERE area=? AND next_due <= ? ORDER BY next_due ASC LIMIT 1""",
            (area, now)).fetchone()
        return dict(row) if row else None

    def seconds_until_next_due(self, area, now):
        row = self._db.execute(
            "SELECT MIN(next_due) m FROM videos WHERE area=?", (area,)).fetchone()
        if not row or not row["m"]:
            return None
        try:
            due = datetime.strptime(row["m"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
            nowdt = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
            return max(0, int((due - nowdt).total_seconds()))
        except ValueError:
            return 0

    # --- episodes ---
    def add_episode(self, ep):
        """ep must include area and thread_id. Returns row id, or None on
        duplicate thread_id (dedupe key). Author names are never stored."""
        try:
            cur = self._db.execute(
                """INSERT INTO episodes
                   (area, thread_id, video_id, top_text, top_published_at,
                    top_like_count, reply_count, replies_json, url,
                    score, score_breakdown_json, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ep["area"], ep["thread_id"], ep.get("video_id", ""),
                 ep.get("top_text", ""), ep.get("top_published_at", ""),
                 ep.get("top_like_count", 0), ep.get("reply_count", 0),
                 json.dumps(ep.get("replies", []), ensure_ascii=False),
                 ep.get("url", ""), ep.get("score", 0),
                 json.dumps(ep.get("score_breakdown", {}), ensure_ascii=False),
                 ep.get("status", "new"), _utcnow()))
            self._db.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def episodes(self, area=None, status=None, limit=20):
        if status and status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        q = ("SELECT id, area, thread_id, video_id, top_text, top_published_at,"
             " top_like_count, reply_count, replies_json, url, score,"
             " score_breakdown_json, status, created_at FROM episodes")
        clauses, params = [], []
        if area:
            clauses.append("area=?")
            params.append(area)
        if status:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY score DESC, id DESC LIMIT ?"
        params.append(limit)
        out = []
        for r in self._db.execute(q, params):
            out.append(_decode_episode(dict(r)))
        return out

    def get(self, episode_id):
        rows = self._db.execute(
            "SELECT id, area, thread_id, video_id, top_text, top_published_at,"
            " top_like_count, reply_count, replies_json, url, score,"
            " score_breakdown_json, status, created_at FROM episodes WHERE id=?",
            (episode_id,)).fetchall()
        if not rows:
            return None
        return _decode_episode(dict(rows[0]))

    def set_status(self, episode_id, status):
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        cur = self._db.execute("UPDATE episodes SET status=? WHERE id=?",
                               (status, episode_id))
        self._db.commit()
        return cur.rowcount

    def stats(self, area=None):
        clauses, params = [], []
        if area:
            clauses.append("area=?")
            params.append(area)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        total = self._db.execute(
            f"SELECT COUNT(*) c FROM episodes{where}", params).fetchone()["c"]
        by_status = {r["status"]: r["c"] for r in self._db.execute(
            f"SELECT status, COUNT(*) c FROM episodes{where} GROUP BY status", params)}
        vwhere = " WHERE area=?" if area else ""
        vparams = [area] if area else []
        nvideos = self._db.execute(
            f"SELECT COUNT(*) c FROM videos{vwhere}", vparams).fetchone()["c"]
        return {"episodes": total, "by_status": by_status, "videos": nvideos,
                "areas": [t["name"] for t in self.areas()]}

    # --- quota accounting ---
    def record_usage(self, day, area, units):
        self._db.execute(
            """INSERT INTO quota_usage (day, area, units) VALUES (?,?,?)
               ON CONFLICT(day, area) DO UPDATE SET units=units+excluded.units""",
            (day, area, units))
        self._db.commit()

    def usage_today(self, area=None, day=None):
        if area:
            row = self._db.execute(
                "SELECT units FROM quota_usage WHERE day=? AND area=?",
                (day, area)).fetchone()
            return row["units"] if row else 0
        row = self._db.execute(
            "SELECT COALESCE(SUM(units),0) s FROM quota_usage WHERE day=?",
            (day,)).fetchone()
        return row["s"]

    # --- run history (health + burn projection) ---
    def log_run(self, day, area, kind, started_at, finished_at, units_spent,
                videos_polled, videos_skipped, episodes_new, stopped_early,
                note="", pages=0, complete=True):
        cur = self._db.execute(
            """INSERT INTO poll_runs
               (day, area, kind, started_at, finished_at, units_spent,
                videos_polled, videos_skipped, episodes_new, pages, complete,
                stopped_early, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (day, area, kind, started_at, finished_at, units_spent,
             videos_polled, videos_skipped, episodes_new, pages,
             1 if complete else 0, 1 if stopped_early else 0, note))
        self._db.commit()
        return cur.lastrowid

    def runs_today(self, area, kind="poll", day=None):
        row = self._db.execute(
            "SELECT COUNT(*) c FROM poll_runs WHERE day=? AND area=? AND kind=?",
            (day, area, kind)).fetchone()
        return row["c"]

    def avg_run_cost(self, area, kind="poll", days=7):
        """(avg units/run, estimated?) over recent runs. (None, True) when
        the area has no history."""
        rows = self._db.execute(
            """SELECT AVG(units_spent) a, COUNT(*) c FROM poll_runs
               WHERE area=? AND kind=? AND day >= date('now','-7 days')""",
            (area, kind)).fetchone()
        if not rows or not rows["c"]:
            return None, True
        return float(rows["a"] or 0), False

    def avg_pages_per_video(self, area, days=7):
        """(avg API pages per video poll, estimated?) — feeds the fidelity
        report's refresh-interval math."""
        rows = self._db.execute(
            """SELECT AVG(pages) a, COUNT(*) c FROM poll_runs
               WHERE area=? AND kind='poll' AND videos_polled>0
               AND day >= date('now','-7 days')""",
            (area,)).fetchone()
        if not rows or not rows["c"]:
            return None, True
        return float(rows["a"] or 0), False

    def thread_completeness(self, area, days=7):
        """Fraction of recent video polls that completed fully (no page
        truncation). None when unmeasured."""
        rows = self._db.execute(
            """SELECT AVG(complete) a, COUNT(*) c FROM poll_runs
               WHERE area=? AND kind='poll' AND videos_polled>0
               AND day >= date('now','-7 days')""",
            (area,)).fetchone()
        if not rows or not rows["c"]:
            return None
        return float(rows["a"])

    def avg_new_hits_per_poll(self, area, days=7):
        """(avg new inbox hits per video polled, estimated?) over recent
        runs. Feeds the estimate's expected-hits line. (None, True) when
        the area has no measured history."""
        rows = self._db.execute(
            """SELECT SUM(episodes_new)*1.0/SUM(videos_polled) a, COUNT(*) c
               FROM poll_runs
               WHERE area=? AND kind='poll' AND videos_polled>0
               AND day >= date('now','-7 days')""",
            (area,)).fetchone()
        if not rows or not rows["c"] or not rows["a"]:
            return None, True
        return float(rows["a"]), False

    def last_runs(self, area=None, limit=10):
        q = ("SELECT day, area, kind, started_at, finished_at, units_spent,"
             " videos_polled, videos_skipped, episodes_new, pages, complete,"
             " stopped_early, note FROM poll_runs")
        params = []
        if area:
            q += " WHERE area=?"
            params.append(area)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._db.execute(q, params)]

    def day_runs(self, area, day):
        """ALL run rows for an area on one quota day, newest first.
        (last_runs' limit would silently truncate busy days — fidelity
        accounting must not sample.)"""
        return [dict(r) for r in self._db.execute(
            "SELECT day, area, kind, started_at, finished_at, units_spent,"
            " videos_polled, videos_skipped, episodes_new, pages, complete,"
            " stopped_early, note FROM poll_runs"
            " WHERE area=? AND day=? ORDER BY id DESC",
            (area, day))]
