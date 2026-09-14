-- Archive database: untrusted third-party content, unbounded growth.
--
-- Nothing here is ever UPDATEd destructively and nothing is ever DELETEd. The
-- archive is evidence: it has to be able to answer "what exactly did the
-- provider post, and when did we see it" months later, including for a message
-- the provider has since edited or deleted.
--
-- STRICT on every table. SQLite's default affinity would happily store the
-- string '2650' in an INTEGER column and the text 'null' in a timestamp, and
-- the archive is precisely where that kind of rot goes unnoticed for months.

-- ---------------------------------------------------------------------------
-- messages: the immutable first-seen record of a message.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    chat_id        INTEGER NOT NULL,
    message_id     INTEGER NOT NULL,

    -- When Telegram says it was posted, and when we first stored it. The gap
    -- between them is ingestion lag, which spec §12's staleness gate needs and
    -- which cannot be reconstructed from one column.
    posted_at      TEXT    NOT NULL CHECK (posted_at LIKE '____-__-__T__:__:__%Z'),
    first_seen_at  TEXT    NOT NULL CHECK (first_seen_at LIKE '____-__-__T__:__:__%Z'),

    -- NULL for an anonymous broadcast post, where the channel itself is the
    -- author. That is why `allowed_sender_ids` may legitimately be empty for a
    -- channel but never for a group.
    sender_id      INTEGER,

    -- Our own message. Must never be read as a signal even if it somehow lands
    -- in a source chat: the bot quoting a price back would be a feedback loop.
    is_outgoing    INTEGER NOT NULL CHECK (is_outgoing IN (0, 1)),
    is_forwarded   INTEGER NOT NULL CHECK (is_forwarded IN (0, 1)),
    -- Joins, pins, title changes. Structurally incapable of being a signal.
    is_service     INTEGER NOT NULL CHECK (is_service IN (0, 1)),

    -- Spec §18's highest-confidence (1.0) correlation link.
    reply_to_id    INTEGER,
    -- Album id: several attachments posted as one logical message.
    grouped_id     INTEGER,

    -- Verbatim. Never normalised, never trimmed, '' for media-only. The stored
    -- text is the evidence for what trade was specified, so normalisation is
    -- confined to the hash.
    text           TEXT    NOT NULL,

    media_count    INTEGER NOT NULL CHECK (media_count >= 0),

    -- NULL while media digests are still unknown, and NULL forever for a
    -- message with no comparable content. A NULL deliberately does not collide
    -- in an index: "cannot be compared" is not "equal to every other empty
    -- thing". See xauusd/archive/content.py.
    content_hash   TEXT    CHECK (content_hash IS NULL OR length(content_hash) = 64),
    hash_version   INTEGER,
    content_state  TEXT    NOT NULL
                   CHECK (content_state IN ('complete', 'pending_media', 'media_failed')),

    edit_count     INTEGER NOT NULL DEFAULT 0 CHECK (edit_count >= 0),
    deleted_at     TEXT,

    PRIMARY KEY (chat_id, message_id)
) STRICT;

-- The duplicate-detection lookup (spec §23). An index, never a constraint:
-- two identical screenshots are two real archived messages.
CREATE INDEX IF NOT EXISTS messages_content
    ON messages (chat_id, content_hash, posted_at)
    WHERE content_hash IS NOT NULL;

-- Chronological scans for backfill reporting and corpus export.
CREATE INDEX IF NOT EXISTS messages_posted ON messages (chat_id, posted_at);

-- Finding the messages whose hash is still unresolved, so a failed media
-- download can be retried without a full table scan.
CREATE INDEX IF NOT EXISTS messages_incomplete
    ON messages (chat_id, message_id)
    WHERE content_state <> 'complete';

-- ---------------------------------------------------------------------------
-- message_edits: append-only revision log.
-- ---------------------------------------------------------------------------
-- A provider editing "SL 2645" to "SL 2635" after we entered is a real
-- scenario, and the SL we traded must stay provable. So an edit is a new row,
-- never an overwrite of `messages.text`.
CREATE TABLE IF NOT EXISTS message_edits (
    chat_id      INTEGER NOT NULL,
    message_id   INTEGER NOT NULL,
    revision     INTEGER NOT NULL CHECK (revision >= 1),
    edited_at    TEXT    NOT NULL CHECK (edited_at LIKE '____-__-__T__:__:__%Z'),
    seen_at      TEXT    NOT NULL CHECK (seen_at LIKE '____-__-__T__:__:__%Z'),
    text         TEXT    NOT NULL,
    content_hash TEXT    CHECK (content_hash IS NULL OR length(content_hash) = 64),
    hash_version INTEGER,
    PRIMARY KEY (chat_id, message_id, revision),
    FOREIGN KEY (chat_id, message_id) REFERENCES messages (chat_id, message_id)
) STRICT;

-- ---------------------------------------------------------------------------
-- media: one row per attachment.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS media (
    chat_id        INTEGER NOT NULL,
    message_id     INTEGER NOT NULL,
    idx            INTEGER NOT NULL CHECK (idx >= 0),

    kind           TEXT    NOT NULL CHECK (kind IN ('photo', 'document')),

    -- What Telegram CLAIMED before download. Kept because a rejection has to be
    -- explainable after the fact, and because a mismatch against the stored
    -- bytes is itself a signal.
    declared_mime  TEXT,
    declared_bytes INTEGER,
    declared_w     INTEGER,
    declared_h     INTEGER,
    -- UNTRUSTED, sender-controlled. Stored as data only. Never used to build a
    -- path: that would be a traversal bug, and on Windows a reserved-device
    -- and alternate-data-stream bug too.
    declared_name  TEXT,

    state          TEXT    NOT NULL
                   CHECK (state IN ('pending', 'stored', 'rejected', 'failed')),
    -- Structured reason from media.RejectReason; never a bare 'rejected'.
    reject_reason  TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error     TEXT,

    -- Facts about what actually landed.
    sha256         TEXT    CHECK (sha256 IS NULL OR length(sha256) = 64),
    stored_bytes   INTEGER,
    -- Path relative to the configured media root, POSIX separators, built
    -- entirely from ids we assign.
    rel_path       TEXT,
    stored_at      TEXT,

    PRIMARY KEY (chat_id, message_id, idx),
    FOREIGN KEY (chat_id, message_id) REFERENCES messages (chat_id, message_id),

    -- A 'stored' row without its bytes accounted for is a corrupt archive, and
    -- the corpus builder would silently skip it. Refuse it at write time.
    CHECK (state <> 'stored' OR (sha256 IS NOT NULL AND rel_path IS NOT NULL
                                 AND stored_bytes IS NOT NULL)),
    CHECK (state <> 'rejected' OR reject_reason IS NOT NULL)
) STRICT;

-- The retry work list: attachments still owed bytes.
CREATE INDEX IF NOT EXISTS media_unresolved
    ON media (chat_id, message_id, idx)
    WHERE state IN ('pending', 'failed');

-- Identical screenshots across messages, for corpus de-duplication in P1.
CREATE INDEX IF NOT EXISTS media_digest ON media (sha256) WHERE sha256 IS NOT NULL;

-- ---------------------------------------------------------------------------
-- message_deletions: recorded, never applied.
-- ---------------------------------------------------------------------------
-- A provider deleting a losing call is exactly what the archive exists to
-- remember. `messages.deleted_at` mirrors the first deletion for cheap
-- filtering; this table keeps every notification, since Telegram can report a
-- deletion for a message we never saw.
CREATE TABLE IF NOT EXISTS message_deletions (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    noticed_at TEXT    NOT NULL CHECK (noticed_at LIKE '____-__-__T__:__:__%Z'),
    -- 0 when Telegram reported a deletion for a message we had never archived,
    -- which is itself worth knowing: it means a gap in coverage.
    was_known  INTEGER NOT NULL CHECK (was_known IN (0, 1)),
    PRIMARY KEY (chat_id, message_id, noticed_at)
) STRICT;

-- ---------------------------------------------------------------------------
-- scan_ranges: inclusive id intervals actually looked at.
-- ---------------------------------------------------------------------------
-- Telegram ids are not contiguous — deletions and service messages consume
-- them — so "id 4243 is absent" does not mean it was missed. Coverage is
-- therefore recorded as intervals scanned, and the complement is the work
-- still to do. Rows are kept merged and non-overlapping by
-- xauusd/archive/ranges.py; see its docstring for why a high-water mark
-- cannot express a hole left by an interrupted backward backfill.
CREATE TABLE IF NOT EXISTS scan_ranges (
    chat_id    INTEGER NOT NULL,
    lo         INTEGER NOT NULL CHECK (lo >= 1),
    hi         INTEGER NOT NULL,
    updated_at TEXT    NOT NULL CHECK (updated_at LIKE '____-__-__T__:__:__%Z'),
    PRIMARY KEY (chat_id, lo),
    CHECK (hi >= lo)
) STRICT;

-- ---------------------------------------------------------------------------
-- archive_meta: small operational key/value.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS archive_meta (
    key        TEXT NOT NULL PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL CHECK (updated_at LIKE '____-__-__T__:__:__%Z')
) STRICT;
