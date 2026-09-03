-- ai-hub initial schema.
--
-- items.seq is declared INTEGER PRIMARY KEY AUTOINCREMENT so that it *is* the
-- rowid. That single column then serves three purposes: the FTS5 join key, the
-- keyset pagination cursor, and the broadcast delivery watermark. item_id holds
-- the public ULID.

CREATE TABLE topics (
  topic_id     TEXT PRIMARY KEY,
  display_name TEXT    NOT NULL,
  description  TEXT    NOT NULL DEFAULT '',
  status       TEXT    NOT NULL DEFAULT 'provisional'
                       CHECK (status IN ('provisional','active','deprecated')),
  merged_into  TEXT    REFERENCES topics(topic_id) ON DELETE SET NULL,
  item_count   INTEGER NOT NULL DEFAULT 0,
  created_ms   INTEGER NOT NULL,
  updated_ms   INTEGER NOT NULL
);
CREATE INDEX idx_topics_status ON topics(status, item_count DESC);

CREATE TABLE items (
  seq                   INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id               TEXT    NOT NULL UNIQUE,
  kind                  TEXT    NOT NULL DEFAULT 'note'
                                CHECK (kind IN ('note','message','handoff','issue','decision','artifact')),
  title                 TEXT    NOT NULL DEFAULT '',
  summary               TEXT    NOT NULL DEFAULT '',
  topic_id              TEXT    REFERENCES topics(topic_id) ON DELETE SET NULL,
  importance            INTEGER NOT NULL DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
  priority              TEXT    NOT NULL DEFAULT 'normal'
                                CHECK (priority IN ('low','normal','high')),
  status                TEXT    NOT NULL DEFAULT 'new'
                                CHECK (status IN ('new','archived','deleted')),
  pinned                INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0,1)),

  sender                TEXT    NOT NULL,
  sender_host           TEXT    NOT NULL DEFAULT '',
  sender_repo           TEXT    NOT NULL DEFAULT '',
  sender_ref            TEXT    NOT NULL DEFAULT '',
  is_broadcast          INTEGER NOT NULL DEFAULT 1 CHECK (is_broadcast IN (0,1)),

  classification_status TEXT    NOT NULL DEFAULT 'pending'
                                CHECK (classification_status IN ('pending','running','done','failed','skipped')),
  classification_source TEXT    NOT NULL DEFAULT 'none'
                                CHECK (classification_source IN ('none','claude','heuristic','manual')),
  classification_conf   REAL    NOT NULL DEFAULT 0.0,
  classified_ms         INTEGER,

  body_storage          TEXT    NOT NULL DEFAULT 'db' CHECK (body_storage IN ('db','file')),
  body_bytes            INTEGER NOT NULL DEFAULT 0,
  body_sha256           TEXT    NOT NULL DEFAULT '',
  payload_sha256        TEXT    NOT NULL DEFAULT '',
  client_msg_id         TEXT,
  refs_json             TEXT    NOT NULL DEFAULT '[]',
  meta_json             TEXT    NOT NULL DEFAULT '{}',

  created_ms            INTEGER NOT NULL,
  updated_ms            INTEGER NOT NULL,
  expires_ms            INTEGER
);
CREATE INDEX idx_items_topic_created  ON items(topic_id, seq DESC);
CREATE INDEX idx_items_created        ON items(created_ms DESC);
CREATE INDEX idx_items_status_seq     ON items(status, seq DESC);
CREATE INDEX idx_items_sender         ON items(sender, seq DESC);
CREATE INDEX idx_items_kind           ON items(kind, seq DESC);
CREATE INDEX idx_items_broadcast      ON items(is_broadcast, seq);
CREATE INDEX idx_items_needs_class    ON items(classification_status, seq)
                                      WHERE classification_status IN ('pending','failed');
-- Scoped by sender: a global unique key would let one agent receive another
-- agent's item_id (and therefore read its body) by reusing the same key.
CREATE UNIQUE INDEX idx_items_client_msg ON items(sender, client_msg_id)
                                      WHERE client_msg_id IS NOT NULL;

CREATE TABLE item_bodies (
  item_id  TEXT PRIMARY KEY REFERENCES items(item_id) ON DELETE CASCADE,
  body     TEXT,
  rel_path TEXT
);

CREATE TABLE tags (
  tag_id     TEXT PRIMARY KEY,
  label      TEXT    NOT NULL,
  use_count  INTEGER NOT NULL DEFAULT 0,
  created_ms INTEGER NOT NULL
);
CREATE INDEX idx_tags_use ON tags(use_count DESC);

CREATE TABLE item_tags (
  item_id TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
  tag_id  TEXT NOT NULL REFERENCES tags(tag_id)  ON DELETE CASCADE,
  source  TEXT NOT NULL DEFAULT 'claude' CHECK (source IN ('claude','heuristic','manual','client')),
  PRIMARY KEY (item_id, tag_id)
);
CREATE INDEX idx_item_tags_tag ON item_tags(tag_id, item_id);

CREATE TABLE attachments (
  attachment_id TEXT    PRIMARY KEY,
  item_id       TEXT    NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
  filename      TEXT    NOT NULL,
  media_type    TEXT    NOT NULL DEFAULT 'application/octet-stream',
  size_bytes    INTEGER NOT NULL,
  sha256        TEXT    NOT NULL,
  rel_path      TEXT    NOT NULL,
  created_ms    INTEGER NOT NULL
);
CREATE INDEX idx_attachments_item ON attachments(item_id);
CREATE INDEX idx_attachments_sha  ON attachments(sha256);

CREATE TABLE agents (
  label         TEXT PRIMARY KEY,
  first_seen_ms INTEGER NOT NULL,
  last_seen_ms  INTEGER NOT NULL,
  sent_count    INTEGER NOT NULL DEFAULT 0,
  -- 'sender' means a real session has used this label; 'addressed' means it has
  -- only ever appeared in someone's `to` field and may well be a typo.
  seen_as       TEXT    NOT NULL DEFAULT 'sender'
                        CHECK (seen_as IN ('sender','addressed'))
);

-- Direct (addressed) delivery state. One row per (item, recipient).
CREATE TABLE deliveries (
  item_id      TEXT    NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
  recipient    TEXT    NOT NULL,
  seq          INTEGER NOT NULL,
  state        TEXT    NOT NULL DEFAULT 'pending'
                       CHECK (state IN ('pending','acked')),
  queued_ms    INTEGER NOT NULL,
  acked_ms     INTEGER,
  note         TEXT    NOT NULL DEFAULT '',
  PRIMARY KEY (item_id, recipient)
);
CREATE INDEX idx_deliveries_pending ON deliveries(recipient, state, seq);

-- Broadcast watermark: everything at or below broadcast_seq is acknowledged.
CREATE TABLE agent_cursors (
  recipient     TEXT    PRIMARY KEY,
  broadcast_seq INTEGER NOT NULL DEFAULT 0,
  updated_ms    INTEGER NOT NULL
);

-- Individual acknowledgements above the watermark. A scalar cursor alone cannot
-- express "102 handled, 100 and 101 not yet": advancing it would drop the two
-- unread items, and not advancing it would resurface 102 forever. Contiguous
-- runs are folded into the watermark and deleted from here.
CREATE TABLE broadcast_acks (
  recipient TEXT    NOT NULL,
  seq       INTEGER NOT NULL,
  acked_ms  INTEGER NOT NULL,
  PRIMARY KEY (recipient, seq)
);

CREATE TABLE classification_jobs (
  job_id         TEXT    PRIMARY KEY,
  item_id        TEXT    NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
  input_hash     TEXT    NOT NULL,
  state          TEXT    NOT NULL DEFAULT 'queued'
                         CHECK (state IN ('queued','running','succeeded','failed','abandoned')),
  engine         TEXT    NOT NULL DEFAULT 'claude' CHECK (engine IN ('claude','heuristic')),
  attempt        INTEGER NOT NULL DEFAULT 0,
  max_attempts   INTEGER NOT NULL DEFAULT 2,
  next_run_ms    INTEGER NOT NULL,
  lease_until_ms INTEGER NOT NULL DEFAULT 0,
  started_ms     INTEGER,
  finished_ms    INTEGER,
  duration_ms    INTEGER,
  error_kind     TEXT    NOT NULL DEFAULT ''
                         CHECK (error_kind IN ('','not_found','auth','timeout','bad_json','schema','rate_limit','unknown')),
  error_detail   TEXT    NOT NULL DEFAULT '',
  created_ms     INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_jobs_idem     ON classification_jobs(item_id, input_hash);
CREATE INDEX idx_jobs_runnable        ON classification_jobs(state, next_run_ms)
                                      WHERE state IN ('queued','running');

CREATE TABLE counters (
  name       TEXT PRIMARY KEY,
  value      INTEGER NOT NULL DEFAULT 0,
  window_key TEXT    NOT NULL DEFAULT ''
);

CREATE VIRTUAL TABLE items_fts USING fts5(
  title,
  summary,
  body,
  body_bi,
  tags,
  tokenize = "unicode61 remove_diacritics 2 tokenchars '_-.'",
  prefix = '2 3'
);

-- Keep the index clean when an item is removed, including cascade deletes.
CREATE TRIGGER trg_items_after_delete AFTER DELETE ON items BEGIN
  DELETE FROM items_fts WHERE rowid = old.seq;
END;

INSERT INTO topics (topic_id, display_name, description, status, item_count, created_ms, updated_ms)
VALUES ('unsorted', 'Unsorted', '분류되지 않았거나 분류에 실패한 아이템', 'active', 0, 0, 0),
       ('general',  'General',  '특정 주제로 묶이지 않는 일반 노트',      'active', 0, 0, 0);
