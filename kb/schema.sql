-- Riphah knowledge base.
--
-- Two halves, per the architecture:
--   * structured tables (programs / fees / offerings) -> exact lookups, no hallucinated numbers
--   * chunks + embeddings                             -> semantic search over prose
--
-- Every row carries source_url + fetched_at so the agent can cite and date its answers.

PRAGMA journal_mode = WAL;

-- ---------------------------------------------------------------- prose side

CREATE TABLE IF NOT EXISTS pages (
    url             TEXT PRIMARY KEY,
    title           TEXT,
    section         TEXT,          -- top-level path segment: admissions, fc, about, ...
    faculty         TEXT,          -- resolved from slug when the page belongs to a faculty
    content_type    TEXT NOT NULL DEFAULT 'html',   -- html | pdf
    text            TEXT NOT NULL,
    char_count      INTEGER NOT NULL,
    content_hash    TEXT NOT NULL, -- skip re-embedding when the page hasn't changed
    lastmod         TEXT,          -- from sitemap.xml
    fetched_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL REFERENCES pages(url) ON DELETE CASCADE,
    ordinal         INTEGER NOT NULL,
    heading         TEXT,
    text            TEXT NOT NULL,
    -- denormalised for cheap metadata filtering at query time
    section         TEXT,
    faculty         TEXT,
    campus          TEXT,
    embedding       BLOB,          -- float32 vector, EMBED_DIMENSIONS long
    embed_model     TEXT,
    fetched_at      TEXT NOT NULL,
    UNIQUE (url, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_chunks_section ON chunks(section);
CREATE INDEX IF NOT EXISTS idx_chunks_faculty ON chunks(faculty);
CREATE INDEX IF NOT EXISTS idx_chunks_embedded ON chunks(embedding) WHERE embedding IS NULL;

-- Keyword fallback. Vector search misses exact program codes ("BSSE", "Pharm-D");
-- FTS catches them. retrieve.py fuses both.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    heading,
    content='chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS chunks_fts_insert AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, heading) VALUES (new.id, new.text, new.heading);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_delete AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, heading)
        VALUES ('delete', old.id, old.text, old.heading);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_update AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, heading)
        VALUES ('delete', old.id, old.text, old.heading);
    INSERT INTO chunks_fts(rowid, text, heading) VALUES (new.id, new.text, new.heading);
END;

-- ----------------------------------------------------------- structured side

CREATE TABLE IF NOT EXISTS programs (
    program_id      INTEGER PRIMARY KEY,      -- Riphah's own ?p= id
    name            TEXT NOT NULL,
    abbreviation    TEXT,
    program_type    TEXT,                     -- U | M | D | CD | AD
    type_label      TEXT,
    faculty         TEXT,
    academic_unit   TEXT,
    detail_url      TEXT,
    overview        TEXT,
    eligibility     TEXT,
    selection_criteria TEXT,
    sections        TEXT,          -- JSON: every anchor-delimited detail-page section
    duration        TEXT,
    credit_hours    TEXT,
    description     TEXT,
    fetched_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_programs_type ON programs(program_type);
CREATE INDEX IF NOT EXISTS idx_programs_name ON programs(name);

-- Where a program is actually offered, and on what terms.
CREATE TABLE IF NOT EXISTS offerings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    program_id      INTEGER REFERENCES programs(program_id) ON DELETE CASCADE,
    program_name    TEXT NOT NULL,
    campus          TEXT NOT NULL,
    city            TEXT,
    academic_unit   TEXT,
    intake          TEXT,                     -- "Fall", "Spring/Fall"
    seats           TEXT,                     -- "100 Male", "50 Male/Female"
    timings         TEXT,
    days            TEXT,
    admission_status TEXT,                    -- Open / Closed
    source_url      TEXT,
    fetched_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_offerings_program ON offerings(program_id);
CREATE INDEX IF NOT EXISTS idx_offerings_campus ON offerings(campus);

-- First-semester fee breakdown, per program per campus.
-- Amounts are stored as INTEGER PKR. NULL means "-" on the site (not charged),
-- which is different from 0 — keep the distinction so the agent can say so.
CREATE TABLE IF NOT EXISTS fees (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    program_id      INTEGER,
    program_name    TEXT NOT NULL,
    campus          TEXT NOT NULL,
    city            TEXT,
    faculty         TEXT,
    academic_unit   TEXT,
    applies_to      TEXT NOT NULL DEFAULT 'Pakistani Nationals',  -- vs International Students
    -- Riphah quotes international fees in USD and local fees in PKR, in the same
    -- table. Dropping the unit turns "USD 17,000" into "PKR 17,000" — a ~25x
    -- understatement. Never format an amount without this column.
    currency        TEXT NOT NULL DEFAULT 'PKR',
    admission_fee   INTEGER,
    registration_fee INTEGER,
    card_fee        INTEGER,
    tuition_fee     INTEGER,
    exam_fee        INTEGER,
    enrollment_fee  INTEGER,
    lab_fee         INTEGER,
    other_fees      TEXT,      -- JSON: any column the site adds that we don't model
    total_fee       INTEGER,
    credit_hours    TEXT,
    per_credit_hour INTEGER,
    intake          TEXT,
    seats           TEXT,
    timings         TEXT,
    days            TEXT,
    admission_status TEXT,
    notes           TEXT,      -- hostel charges, tax disclaimers, revision clause
    source_url      TEXT,
    fetched_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fees_program ON fees(program_name);
CREATE INDEX IF NOT EXISTS idx_fees_campus ON fees(campus);

-- The fee endpoint treats f=0 as "no faculty filter", so it returns every
-- program in addition to the genuine cross-faculty units. We still request all
-- nine faculties (explicit coverage if that behaviour ever changes) and let this
-- constraint collapse the overlap.
CREATE UNIQUE INDEX IF NOT EXISTS idx_fees_unique
    ON fees(program_name, campus, applies_to);

-- Important dates (application deadlines, test dates, semester start).
CREATE TABLE IF NOT EXISTS important_dates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    intake          TEXT,          -- "Fall 2026"
    event           TEXT NOT NULL,
    date_text       TEXT NOT NULL, -- kept as printed; never re-formatted
    applies_to      TEXT,
    source_url      TEXT,
    fetched_at      TEXT NOT NULL
);

-- Campus / office contacts, so the agent can always hand off to a human.
CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT NOT NULL,
    campus          TEXT,
    city            TEXT,
    address         TEXT,
    phone           TEXT,
    email           TEXT,
    source_url      TEXT,
    fetched_at      TEXT NOT NULL
);

-- --------------------------------------------------------------- bookkeeping

CREATE TABLE IF NOT EXISTS crawl_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stage           TEXT NOT NULL,
    status          TEXT NOT NULL,     -- ok | error
    items           INTEGER DEFAULT 0,
    detail          TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);

-- Answered-question log. Feeds the eval set and shows which topics the KB misses.
CREATE TABLE IF NOT EXISTS query_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asked_at        TEXT NOT NULL,
    language        TEXT,
    question        TEXT NOT NULL,
    normalized      TEXT,
    tool_used       TEXT,
    hit             INTEGER,           -- 1 if the KB had an answer
    top_similarity  REAL
);

-- ------------------------------------------------------- conversation history

-- Voice and text share one store. A conversation survives a page reload, a
-- dropped WebRTC connection, and a server restart — which matters because the
-- Realtime API itself is stateless: reconnecting starts a blank session unless
-- we replay context into it.
CREATE TABLE IF NOT EXISTS conversations (
    id              TEXT PRIMARY KEY,      -- uuid4, minted server-side
    user_id         TEXT,                  -- owner; every query is scoped to it
    title           TEXT,                  -- first user turn, truncated
    mode            TEXT NOT NULL DEFAULT 'text',   -- voice | text | mixed
    language        TEXT,                  -- last detected language tag
    turn_count      INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    archived_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_conversations_updated
    ON conversations(updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_conversations_user
    ON conversations(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,         -- user | assistant | tool
    content         TEXT,
    -- Tool turns are recorded too, so a replayed transcript shows *why* an
    -- answer said what it did, not just what it said.
    tool_name       TEXT,
    tool_input      TEXT,                  -- JSON
    tool_found      INTEGER,               -- 1 hit / 0 miss / NULL n-a
    channel         TEXT,                  -- voice | text
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, id);
