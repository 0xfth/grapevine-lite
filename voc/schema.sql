-- voc-listener schema. Focus areas are the core abstraction: one SQLite
-- store holds many areas; every video, episode, watermark, and quota unit
-- is namespaced by area. Steering (which areas get attention, at what
-- weight) is explicit agent state in the meta table.
-- Quota usage is recorded per (Pacific-time day, area): the YouTube Data
-- API quota day resets at midnight Pacific.

CREATE TABLE IF NOT EXISTS areas (
  name            TEXT PRIMARY KEY,
  person_terms    TEXT NOT NULL DEFAULT '[]',
  topic_terms     TEXT NOT NULL DEFAULT '[]',
  threshold       INTEGER NOT NULL DEFAULT 40,
  cadence_hours   REAL NOT NULL DEFAULT 8,
  depth           TEXT NOT NULL DEFAULT 'deep' CHECK(depth IN ('deep','shallow')),
  queries         TEXT NOT NULL DEFAULT '[]',
  created_at      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS videos (
  video_id        TEXT NOT NULL,
  area            TEXT NOT NULL DEFAULT '',
  title           TEXT DEFAULT '',
  channel         TEXT DEFAULT '',
  source          TEXT DEFAULT 'youtube',
  discovery_query TEXT DEFAULT '',
  last_polled     TEXT DEFAULT '',
  next_due        TEXT DEFAULT '',  -- optional: next poll due at (UTC ISO)
  added_at        TEXT DEFAULT '',
  PRIMARY KEY (video_id, area)
);
CREATE INDEX IF NOT EXISTS idx_videos_area_polled ON videos(area, last_polled);

CREATE TABLE IF NOT EXISTS episodes (
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
CREATE INDEX IF NOT EXISTS idx_episodes_area_status ON episodes(area, status, score DESC);

-- Quota accounting. day is YYYY-MM-DD in America/Los_Angeles.
CREATE TABLE IF NOT EXISTS quota_usage (
  day    TEXT NOT NULL,
  area   TEXT NOT NULL,
  units  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, area)
);

-- Run history: health checking, burn projection, fidelity measurement.
CREATE TABLE IF NOT EXISTS poll_runs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  day            TEXT NOT NULL,
  area           TEXT NOT NULL,
  kind           TEXT NOT NULL,   -- 'poll' or 'discover'
  started_at     TEXT DEFAULT '',
  finished_at    TEXT DEFAULT '',
  units_spent    INTEGER DEFAULT 0,
  videos_polled  INTEGER DEFAULT 0,
  videos_skipped INTEGER DEFAULT 0,
  episodes_new   INTEGER DEFAULT 0,
  pages          INTEGER DEFAULT 0,  -- API pages fetched (poll runs)
  complete       INTEGER DEFAULT 1,  -- 1 if the video's poll completed fully
  stopped_early  INTEGER DEFAULT 0,
  note           TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_day_area ON poll_runs(day, area, kind);

-- Steering: the agent's explicit attention split, e.g.
--   {"weights": {"adhd-jobs": 3, "sourdough": 1}, "updated_at": "..."}
-- Areas with weight > 0 get budget; steering stays fixed
-- until the agent steers again.
-- (meta table itself is created by Store; steering rows live there.)
