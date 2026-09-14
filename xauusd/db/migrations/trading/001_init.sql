-- Trading database: the ledger. synchronous=FULL (see xauusd/db/store.py).
--
-- P0 creates only the notification outbox. The order ledger (`intents`,
-- `orders`, `positions`, `decisions`, `daily_state`, `bot_state`) lands with
-- the execution engine in P3, as a later numbered migration — not stubbed here,
-- because an empty table that looks like a ledger is worse than no table.

-- ---------------------------------------------------------------------------
-- outbox: transactional outbox for every operator-facing message.
-- ---------------------------------------------------------------------------
-- Why a table and not a Telegram call at the point of the event: a Telegram
-- outage must never block or delay execution. The row is INSERTed in the same
-- transaction as the state change it describes, so "we moved the stop" and "we
-- said we moved the stop" commit together or not at all. A separate loop drains
-- it.
--
-- Delivery is AT LEAST ONCE, and that is a deliberate choice rather than an
-- oversight. The Bot API has no client-supplied idempotency key, so a crash
-- between "sent" and "marked sent" must resolve to a possible duplicate rather
-- than a possible silence — a duplicate SL_HIT notice is noise, a missing one
-- is an operator who does not know their stop was hit.
CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%Z'),

    chat_id         INTEGER NOT NULL,
    -- SL_HIT, BE_MOVED, TP1_FILLED, TP2_FILLED, FINAL_TP, REJECTION,
    -- HEARTBEAT, ARCHIVER_STATUS, ... Free text so a new event type does not
    -- need a migration; the sender does not branch on it.
    kind            TEXT    NOT NULL,
    -- Lower is sooner. A SL_HIT must not queue behind an hour of heartbeats
    -- accumulated during an outage.
    priority        INTEGER NOT NULL,
    body            TEXT    NOT NULL,

    -- Supersession key. A pending row with the same (chat_id, collapse_key) is
    -- REPLACED in place rather than queued behind. This is what stops a
    -- ten-minute outage at three-minute heartbeats from delivering a backlog of
    -- stale position updates after recovery. Event messages pass NULL: none of
    -- them may ever be collapsed away.
    collapse_key    TEXT,

    state           TEXT    NOT NULL
                    CHECK (state IN ('pending', 'claimed', 'sent', 'failed')),
    attempts        INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),

    -- A lease, not a boolean. If the drainer dies mid-send, a boolean 'claimed'
    -- strands the row forever; an expiry lets exactly one reclaim happen after
    -- the lease runs out.
    lease_expires_at TEXT,
    next_attempt_at TEXT    NOT NULL CHECK (next_attempt_at LIKE '____-__-__T__:__:__%Z'),

    sent_at         TEXT,
    last_error      TEXT,

    CHECK (state <> 'sent' OR sent_at IS NOT NULL),
    CHECK (state <> 'claimed' OR lease_expires_at IS NOT NULL)
) STRICT;

-- The claim query: eligible rows in priority then insertion order.
CREATE INDEX IF NOT EXISTS outbox_claimable
    ON outbox (priority, next_attempt_at, id)
    WHERE state IN ('pending', 'claimed');

-- Enforces the collapse invariant in the database rather than in the caller:
-- at most one pending row per (chat_id, collapse_key). A partial index, so the
-- many NULL-keyed event rows are unaffected.
CREATE UNIQUE INDEX IF NOT EXISTS outbox_collapse_pending
    ON outbox (chat_id, collapse_key)
    WHERE state = 'pending' AND collapse_key IS NOT NULL;

-- Undeliverable messages are never dropped: an SL_HIT that never reached the
-- operator is evidence about the incident, so 'failed' is a terminal state that
-- keeps the body.
CREATE INDEX IF NOT EXISTS outbox_failed ON outbox (created_at) WHERE state = 'failed';
